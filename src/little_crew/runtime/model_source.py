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

__all__ = ["apply_control", "apply_zimage_source", "aux_loader_kind_from"]

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
) -> dict[str, str]:
    """就地把 `nodes` 的模型源换成 Z-Image 分离加载。

    返回 `{"src_id", "clip_ref", "vae_ref"}` —— 后两个是辅助加载器的节点 id
    （该端口没有对应加载器时为空串）。**`apply_control` 要用它们。**

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
    return {"src_id": src_id,
            "clip_ref": remap["CLIP"],
            "vae_ref": remap["VAE"]}


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


def apply_control(
    nodes: list[dict[str, Any]],
    *,
    src: Mapping[str, str],
    staged_image: str,
    blur_radius: int = 0,
    blur_sigma: float = 0.0,
    strength: float = 1.0,
    channel: str = "",
) -> None:
    """就地把**白模控制**接进 `nodes`（第 ③ 步）。

    `staged_image` 必须是**已投放进 ComfyUI `input/` 的裸文件名** ——
    `LoadImage` 走 `folder_paths.get_annotated_filepath()`，只认相对名。

    ## 三件事，顺序不能换

    ```
    image.file ──IMAGE──> [composition.image_blur] ──IMAGE──┐
    model.source_zimage ──MODEL─────────────────────────────┼─> composition.zimage_controlnet
    model.zimage_vae ────VAE────────────────────────────────┘            │
                                                                         MODEL
                                                                          v
                                                                  sample.ksampler
    ```

    ⚠️ **模糊必须做成图节点，不能在平台侧先模糊好。**
    做成节点时 `blur_radius` 进入 IR 哈希 —— 改半径就是改 IR，缓存正确失效。
    若在平台侧模糊，改半径时是"文件内容变了但 IR 没变"，命中旧缓存，
    表现为「调了参数没反应」。

    ⚠️ **采样器的 `model` 输入必须改接到控制节点**，否则控制图只是挂在图上没人用。
    """
    node_ids = {n["id"] for n in nodes}
    src_id = src["src_id"]

    img_id = "n_ctrl_img"
    if img_id not in node_ids:
        nodes.append({"id": img_id, "kind": "image.file",
                      "params": {"image": staged_image}})
        node_ids.add(img_id)

    feed_id, feed_port = img_id, "IMAGE"
    blur = int(blur_radius)
    if blur > 0 and not 1 <= blur <= 31:
        # ComfyUI 的 ImageBlur 约束是 1..31，超范围它会在**提交时**拒绝。
        # 在这里先拦下来，把「提交被拒」变成「本地就能看懂的错」。
        raise ValueError(
            f"blur_radius={blur} 超出 ComfyUI ImageBlur 的允许范围 "
            f"1..31（0 表示不模糊）")
    blur_id = "n_ctrl_blur"
    if blur > 0 and blur_id not in node_ids:
        nodes.append({
            "id": blur_id, "kind": "composition.image_blur",
            "inputs": {"image": {"ref": img_id, "port": "IMAGE"}},
            "params": {"blur_radius": blur, "sigma": float(blur_sigma)},
            "meta": {"title": f"白模模糊（半径 {blur}）"},
        })
        node_ids.add(blur_id)
        feed_id, feed_port = blur_id, "IMAGE"

    ctrl_id = "n_ctrl"
    if ctrl_id not in node_ids:
        nodes.append({
            "id": ctrl_id, "kind": "composition.zimage_controlnet",
            "inputs": {"model": {"ref": src_id, "port": "MODEL"},
                       "vae": {"ref": src["vae_ref"] or src_id, "port": "VAE"},
                       "image": {"ref": feed_id, "port": feed_port}},
            "params": {"strength": strength},
            "meta": {"title": f"白模控制（{channel}）"},
        })
        for n in nodes:
            if n.get("kind") == "sample.ksampler":
                n.setdefault("inputs", {})["model"] = {"ref": ctrl_id,
                                                       "port": "MODEL"}
                break
