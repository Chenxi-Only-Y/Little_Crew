"""Unified IR canonicalization and content-hash property tests.

核心性质（P1-P8）
----------------
P1 键序无关        语义相同的图，字段书写顺序不同 -> 哈希相同
P2 省略=显式       缺省字段与显式写默认值 -> 哈希相同
P3 非语义无关      meta.ui / createdAt / contentHash 变化 -> 哈希不变
P4 浮点噪声无关    0.1+0.2 与 0.3 -> 哈希相同
P5 语义敏感        真正改变语义的修改 -> 哈希必变
P6 集合顺序无关    节点/边/约束的数组顺序 -> 哈希不变
P7 稳定性          同输入多次调用 -> 同一哈希
P8 纯函数          不修改入参

零第三方依赖： python -m little_crew.ir.test_canonical
"""

from __future__ import annotations

import copy
import sys

from little_crew.ir.canonical import (
    CanonicalizationError,
    cache_key,
    canonical_json,
    condition_fingerprint,
    content_hash,
    derive_seed,
    normalize,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def expect_raises(name: str, exc: type[BaseException], fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc:
        check(name, True)
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"期望 {exc.__name__}，实得 {type(e).__name__}: {e}")
    else:
        check(name, False, f"期望 {exc.__name__}，但未抛错")


# ==========================================================================
# 夹具：一个语义完整的镜头图
# ==========================================================================

def shot_graph() -> dict:
    return {
        "irVersion": "little_crew.ir/v1",
        "graphId": "G-ep001-sh005",
        "scope": "shot",
        "bindings": {
            "CHAR-0007": {
                "anchorSet": {"front": "assets://char/CHAR-0007/anchor_front_v3.png"},
                "anchorGeneration": 3,
                "loraFile": "linwan_v3.safetensors",
                "loraStrength": 0.75,
                "embeddingId": "emb_8f21",
            }
        },
        "nodes": [
            {
                "id": "n_prompt",
                "kind": "prompt.text",
                "inputs": {"text": {"ref": "n_fuse", "port": "out"}},
                "semantic": {
                    "characterRef": "CHAR-0007",
                    "constraintRefs": ["cr_costume_007", "cr_weather"],
                },
                "meta": {"title": "正向提示词", "ui": {"x": 120, "y": 340}},
            },
            {
                "id": "n_ks",
                "kind": "sample.ksampler",
                "inputs": {
                    "model": {"ref": "n_lora", "port": "model"},
                    "positive": {"ref": "n_prompt", "port": "text"},
                    "negative": {"ref": "n_neg", "port": "text"},
                    "latent_image": {"ref": "n_latent", "port": "latent"},
                },
                "params": {
                    "steps": 20, "cfg": 5.5,
                    "sampler": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
                },
                "execution": {
                    "sideEffect": "pure",
                    "recoveryMode": "replay",
                    "estimatedVramMb": 8192,
                },
                "meta": {"title": "镜头采样"},
            },
            {
                "id": "n_lora",
                "kind": "identity.lora",
                "inputs": {"bindingRef": "CHAR-0007"},
                "params": {"strength": 0.75},
                "semantic": {"characterRef": "CHAR-0007", "constraintRefs": ["cr_face_007"]},
            },
            {"id": "n_neg", "kind": "prompt.negative", "inputs": {"text": "多手,服装变色"}},
            {"id": "n_latent", "kind": "latent.empty", "params": {"width": 768, "height": 1344}},
        ],
        "edges": [
            {
                "id": "e1", "type": "data",
                "from": {"node": "n_lora", "port": "model"},
                "to": {"node": "n_ks", "port": "model"},
            },
            {
                "id": "e2", "type": "constraint",
                "from": {"node": "n_lora", "port": "model"},
                "to": {"node": "n_ks", "port": "latent"},
                "assertion": {
                    "op": "identity_similarity", "threshold": 0.82,
                    "against": "CHAR-0007.anchor_set",
                },
                "enforcement": "hard",
            },
        ],
        "provenance": {
            "authorAgent": "storyboard.shot_composer",
            "authorAgentVersion": "1.4.2",
            "createdAt": "2026-10-11T10:20:00+08:00",
        },
    }


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 74)
    print("Unified IR 规范化与内容哈希 —— 性质测试")
    print("=" * 74)

    base = shot_graph()
    h_base = content_hash(base)

    # ---------- P8 纯函数 ----------
    snapshot = copy.deepcopy(base)
    content_hash(base)
    check("P8 纯函数：不修改入参", base == snapshot)

    # ---------- P7 稳定性 ----------
    check("P7 稳定性：同输入同哈希",
          content_hash(shot_graph()) == h_base)

    # ---------- P1 键序无关 ----------
    shuffled = shot_graph()
    shuffled["nodes"] = list(reversed(shuffled["nodes"]))
    for n in shuffled["nodes"]:
        if "params" in n:
            n["params"] = dict(reversed(list(n["params"].items())))
        if "inputs" in n:
            n["inputs"] = dict(reversed(list(n["inputs"].items())))
    shuffled["bindings"] = dict(reversed(list(shuffled["bindings"].items())))
    shuffled["edges"] = list(reversed(shuffled["edges"]))
    check("P1 键序/数组序无关", content_hash(shuffled) == h_base)

    reordered = {k: shot_graph()[k] for k in reversed(list(shot_graph()))}
    check("P1 顶层键序无关", content_hash(reordered) == h_base)

    # ---------- P2 省略 = 显式默认值 ----------
    with_defaults = shot_graph()
    for n in with_defaults["nodes"]:
        if n["id"] == "n_ks":
            n["execution"] = {
                "sideEffect": "pure",
                "recoveryMode": "replay",
                "maxAutomaticAttempts": 3,
                "backoffClass": "bounded_exponential",
                "estimatedVramMb": 8192,
            }
    check("P2 省略 = 显式默认值（execution）", content_hash(with_defaults) == h_base,
          "maxAutomaticAttempts/backoffClass 应被视作默认值")

    with_edge_default = shot_graph()
    for e in with_edge_default["edges"]:
        if e["type"] == "constraint":
            e["enforcement"] = "hard"
    check("P2 省略 = 显式默认值（edge.enforcement）",
          content_hash(with_edge_default) == h_base)

    # ---------- P3 非语义字段无关 ----------
    ui_moved = shot_graph()
    ui_moved["nodes"][0]["meta"]["ui"] = {"x": 9999, "y": -42}
    check("P3 meta.ui 变化不影响哈希", content_hash(ui_moved) == h_base)

    time_changed = shot_graph()
    time_changed["provenance"]["createdAt"] = "2030-01-01T00:00:00+08:00"
    check("P3 provenance.createdAt 变化不影响哈希",
          content_hash(time_changed) == h_base)

    hash_injected = shot_graph()
    hash_injected["provenance"]["contentHash"] = "sha256:" + "0" * 64
    check("P3 provenance.contentHash 自身不参与哈希",
          content_hash(hash_injected) == h_base)

    # ---------- P4 浮点噪声无关 ----------
    fp = shot_graph()
    for n in fp["nodes"]:
        if n["id"] == "n_ks":
            n["params"]["cfg"] = 5.500000000000001
    check("P4 浮点表示噪声无关（cfg）", content_hash(fp) == h_base)

    # ---------- P5 语义敏感 ----------
    seed_added = shot_graph()
    for n in seed_added["nodes"]:
        if n["id"] == "n_ks":
            n["params"]["seed"] = 12345
    check("P5 新增语义参数（seed）改变哈希", content_hash(seed_added) != h_base)

    cfg_changed = shot_graph()
    for n in cfg_changed["nodes"]:
        if n["id"] == "n_ks":
            n["params"]["cfg"] = 7.0
    check("P5 参数值变更改变哈希", content_hash(cfg_changed) != h_base)

    thr_changed = shot_graph()
    for e in thr_changed["edges"]:
        if e["type"] == "constraint":
            e["assertion"]["threshold"] = 0.74
    check("P5 约束阈值变更改变哈希（关键：降级会改写阈值）",
          content_hash(thr_changed) != h_base)

    title_changed = shot_graph()
    title_changed["nodes"][0]["meta"]["title"] = "另一个标题"
    check("P5 meta.title 参与哈希（影响 _meta 与人工排查基线）",
          content_hash(title_changed) != h_base)

    anchor_gen_changed = shot_graph()
    anchor_gen_changed["bindings"]["CHAR-0007"]["anchorGeneration"] = 4
    check("P5 锚点代数变更改变哈希（触发失效传播）",
          content_hash(anchor_gen_changed) != h_base)

    check("P5 不同 graphId 得到不同哈希",
          content_hash({**shot_graph(), "graphId": "G-ep001-sh006"}) != h_base)

    # ---------- P6 约束数组顺序 ----------
    cons = shot_graph()
    cons["constraints"] = [
        {"ref": "cr_weather",
         "assertion": {"op": "token_contains_all", "tokens": ["雨", "夜"]}},
        {"ref": "cr_costume_007",
         "assertion": {"op": "token_contains_all", "tokens": ["米色", "风衣"]}},
    ]
    cons_rev = copy.deepcopy(cons)
    cons_rev["constraints"] = list(reversed(cons_rev["constraints"]))
    check("P6 图级约束数组顺序无关", content_hash(cons) == content_hash(cons_rev))

    # ---------- 错误路径 ----------
    expect_raises("错误：缺少必填字段", CanonicalizationError,
                  content_hash, {"irVersion": "little_crew.ir/v1", "graphId": "G-x"})
    expect_raises("错误：节点 id 重复", CanonicalizationError, content_hash,
                  {**shot_graph(),
                   "nodes": shot_graph()["nodes"] + [shot_graph()["nodes"][0]]})
    expect_raises("错误：边 id 重复", CanonicalizationError, content_hash,
                  {**shot_graph(), "edges": shot_graph()["edges"] * 2})
    expect_raises("错误：边 type 非法", CanonicalizationError, content_hash,
                  {**shot_graph(), "edges": [{"id": "e9", "type": "bogus",
                                              "from": {"node": "n_ks", "port": "x"},
                                              "to": {"node": "n_ks", "port": "y"}}]})
    expect_raises("错误：端点缺 port", CanonicalizationError, content_hash,
                  {**shot_graph(), "edges": [{"id": "e9", "type": "data",
                                              "from": {"node": "n_ks"},
                                              "to": {"node": "n_ks", "port": "y"}}]})
    expect_raises("错误：非有限浮点", CanonicalizationError, content_hash,
                  {**shot_graph(),
                   "nodes": [{**shot_graph()["nodes"][1],
                              "params": {"cfg": float("nan")}}]})

    # ---------- 规范 JSON 形态 ----------
    cj = canonical_json({"b": 1, "a": "中文", "c": [1, 2]})
    check("规范 JSON：键已排序", cj.startswith('{"a":'), cj)
    check("规范 JSON：中文不转义", "中文" in cj, cj)
    check("规范 JSON：无多余空白", " " not in cj, cj)

    # ---------- 种子派生 ----------
    s1 = derive_seed(42, 1, 3, 5, attempt=0)
    s2 = derive_seed(42, 1, 3, 5, attempt=0)
    check("种子派生确定性", s1 == s2, str(s1))
    check("种子派生：attempt 变化换种子",
          derive_seed(42, 1, 3, 5, attempt=1) != s1)
    check("种子派生：镜头索引变化换种子",
          derive_seed(42, 1, 3, 6) != s1)
    check("种子派生：避免算术组合碰撞（(1,0,0) vs (0,1,0)）",
          derive_seed(42, 1, 0, 0) != derive_seed(42, 0, 1, 0),
          "简单加法组合会让二者相等")

    # ---------- 条件指纹 ----------
    cf1 = condition_fingerprint(prompt="雨夜天台",
                                character_refs=["CHAR-0007", "CHAR-0002"],
                                scene_ref="SC-E001-003", width=768, height=1344,
                                frames=81, fps=24)
    cf2 = condition_fingerprint(prompt="雨夜天台",
                                character_refs=["CHAR-0002", "CHAR-0007"],
                                scene_ref="SC-E001-003", width=768, height=1344,
                                frames=81, fps=24)
    check("条件指纹：角色集合顺序无关", cf1 == cf2, f"{cf1} vs {cf2}")
    cf3 = condition_fingerprint(prompt="雨夜天台",
                                character_refs=["CHAR-0002", "CHAR-0007"],
                                scene_ref="SC-E001-003", width=768, height=1344,
                                frames=80, fps=24)
    check("条件指纹：帧数变化改变指纹", cf1 != cf3)

    # ---------- 三级缓存键 ----------
    cat = "sha256:" + "a" * 64
    t1a = cache_key(h_base, cat, "little_crew.compile/v1")
    t1b = cache_key(h_base, cat, "little_crew.compile/v1")
    t1c = cache_key(h_base, cat, "little_crew.compile/v2")
    t2 = cache_key(h_base)
    check("Tier-1 缓存键确定", t1a == t1b and t1a.startswith("t1:"))
    check("Tier-1 缓存键随规则版本失效", t1a != t1c,
          "规则升级必须使旧缓存失效（版本化变换原则）")
    check("Tier-2 缓存键与环境无关", t2.startswith("t2:"), t2)
    check("Tier-1 与 Tier-2 不同空间", t1a != t2)

    # ---------- 汇总 ----------
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if not ok or detail:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    print(f"基准图内容哈希：{h_base}")
    print(f"规范字节数：{len(canonical_json(normalize(base)))}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
