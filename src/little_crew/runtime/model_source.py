"""把 IR 的**集成式模型源**换成 Z-Image 的**分离式加载**（UNET + CLIP + VAE）。

## 为什么单独一个模块

这段逻辑原来埋在 `EpisodeRunner._apply_white_model()` 里。抽出来的理由：

1. **它不是 `EpisodeRunner` 的私事** —— "哪些模型源可用、怎么换"是
   Z-Image 通路的公共知识，任何要直连 Z-Image 的调用方都得做同样的变换
   （白模 A/B 实验脚本就是第二个调用方）。
2. **复制即漂移** —— 实验脚本若复制一份，等生产代码改了，实验跑的就不是同一件事，
   而那种不一致**看不出来**。

## 变换两步（第 ③ 步控制图不在这里）

| 步 | 做什么 |
|---|---|
| ① | `model.checkpoint` → `model.source_zimage`（UNET/CLIP/VAE 三件套） |
| ② | 把指向模型源的 **CLIP / VAE 端口**改指到显式声明的辅助加载器 |

### 第 ② 步不是可选的

`shot_to_ir` 把 prompt 节点的 `clip` 端口**显式**指向模型源
（`{"ref": "n_ckpt", "port": "CLIP"}`）。换源之后 `n_ckpt` 是 `UNETLoader`，
它的 `output[0]` 是 **MODEL** —— ComfyUI 会以
`return_type_mismatch: clip, received_type(MODEL)` 拒绝。

### 为什么必须"显式声明"加载器，不能只改引用

只改引用的后果：IR 指向 `n_zimage_vae`，而该节点是编译器**自动注入**的 ——
IR 里不存在它，拓扑排序直接报 `DANGLING_REF: 引用了不存在的节点`。

显式声明还有个好处：编译器看到 IR 已声明同类加载器，
就**不会重复注入**（`_inject_aux_loaders` 的幂等规则）。
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

__all__ = ["apply_zimage_source", "aux_loader_kind_from"]

#: Z-Image 的 CLIP 加载器需要 `type` 参数（ComfyUI 要求指明文本编码器家族）。
ZIMAGE_CLIP_TYPE = "qwen_image"


def apply_zimage_source(
    nodes: list[dict[str, Any]],
    *,
    unet_name: str,
    clip_name: str,
    vae_name: str,
    aux_loader_kind: Callable[[str], str | None],
    src_node_id: str = "n_ckpt",
) -> str:
    """就地把 `nodes` 的模型源换成 Z-Image 分离加载，返回新的源节点 id。

    `nodes` 是**可变的 dict 列表**（调用方已复制过的 IR 节点）。
    `aux_loader_kind(port)` 返回该端口的辅助加载器 kind（`None` 表示该端口
    没有对应加载器，不改引用）。
    """
    params: dict[str, Any] = {
        "unet_name": unet_name,
        "clip_name": clip_name,
        "vae_name": vae_name,
        # `weight_dtype` 是 UNETLoader 的**必需**参数；缺了会被
        # `required_input_missing` 拒绝。规则默认值只在自动注入路径生效，
        # 走显式声明时必须自己带上。
        "weight_dtype": "default",
    }

    # ---- ① 模型源：集成 -> 分离 ----
    old_src = next((n["id"] for n in nodes
                    if n.get("kind") == "model.checkpoint"), src_node_id)
    for n in nodes:
        if n.get("kind") == "model.checkpoint":
            n["kind"] = "model.source_zimage"
            n["params"] = params
            n["meta"] = {**(n.get("meta") or {}),
                         "title": "Z-Image 模型源（分离加载）"}
            break
    else:
        # 原本没有模型源节点（已经是分离式）—— 补一个，别静默跳过
        nodes.insert(0, {"id": old_src, "kind": "model.source_zimage",
                         "params": params})
    src_id = next((n["id"] for n in nodes
                   if n.get("kind") == "model.source_zimage"), old_src)

    # ---- ② 显式声明 CLIP / VAE 加载器，并把引用改指过去 ----
    clip_kind = aux_loader_kind("CLIP")
    vae_kind = aux_loader_kind("VAE")
    node_ids = {n["id"] for n in nodes}

    for lkind, lname, lparams in (
        (clip_kind, "n_zimage_clip",
         {"clip_name": clip_name, "type": ZIMAGE_CLIP_TYPE}),
        (vae_kind, "n_zimage_vae", {"vae_name": vae_name}),
    ):
        if lkind and lname not in node_ids:
            nodes.append({"id": lname, "kind": lkind, "params": lparams,
                          "meta": {"title": f"{lkind}（模型源的一部分）"}})
            node_ids.add(lname)

    remap = {"CLIP": "n_zimage_clip" if clip_kind else "",
             "VAE": "n_zimage_vae" if vae_kind else ""}
    for n in nodes:
        for port, ref in list((n.get("inputs") or {}).items()):
            if not isinstance(ref, Mapping):
                continue
            p = str(ref.get("port", "")).upper()
            target = remap.get(p)
            if ref.get("ref") == src_id and target:
                n["inputs"][port] = {"ref": target, "port": p}
    return src_id


def aux_loader_kind_from(compiler: Any) -> Callable[[str], str]:
    """从**编译器的规则表**查出"哪个辅助加载器提供该 dtype"。

    从规则表读、不写死 —— 换模型源实现时不必改调用方。

    这条逻辑原来也是 `EpisodeRunner` 的私有方法，抽出来的理由同上：
    **A/B 脚本要用它，而复制即漂移。**
    编译器不可得时返回空串（调用方退回原引用，不会炸）。
    """
    def _kind(dtype: str) -> str:
        try:
            rules = getattr(compiler, "rules", None)
            if rules is None:
                return ""
            src = rules.get(getattr(compiler, "model_source_kind",
                                    "model.checkpoint"))
            if src is None:
                return ""
            for lkind, ldtype in (src.requires_loader or {}).items():
                if str(ldtype).upper() == str(dtype).upper():
                    return str(lkind)
        except Exception:                                     # noqa: BLE001
            pass
        return ""
    return _kind
