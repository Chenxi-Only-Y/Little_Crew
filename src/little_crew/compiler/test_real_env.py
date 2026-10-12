"""真实 ComfyUI 环境回归测试。

与 `test_compiler.py` 的分工
---------------------------
`test_compiler.py` 用**手写夹具**测逻辑分支（降级、阈值联动、错误路径）。
本文件用**真实环境的 object_info 快照**测"规则表与真实节点目录是否吻合"。

**为什么两者都需要**：夹具是我按官方源码写的，它只能覆盖我**已经知道**的形状。
R1 首次接入真实环境（ComfyUI 0.39.0 / 970 节点）时，立刻暴露出 6 处夹具
完全掩盖的规则表冲突 —— 例如 `identity.reference` 曾把 `ReferenceLatent`
列为候选，而后者输出 CONDITIONING、且没有 `model`/`image` 端口，
在未装 IPAdapter 的机器上会被选中并编译出**类型完全错位**的图。

快照来源
--------
`config/env/local-4060.json` —— 本机 ComfyUI 0.39.0 的 `/object_info` 探测结果
（970 节点）。属项目配置第 ④ 类（环境能力快照），**入库**，使得本测试
在**没有 ComfyUI 的机器上也能运行** —— 这是"项目内不存工具、但可复现"的关键。

零第三方依赖： python -m little_crew.compiler.test_real_env
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from little_crew.capability.capability import CapabilityMatrix, DriftKind, Severity
from little_crew.capability.comfy_interfaces import DeterminismClass
from little_crew.compiler.compiler import (
    DEFAULT_RULES,
    CompileError,
    CompileErrorCode,
    CompileRuleTable,
    ComfyCompiler,
    NoticeKind,
)

#: 快照路径：src/little_crew/compiler/ -> 项目根/config/env/
_SNAPSHOT = (Path(__file__).resolve().parents[3]
             / "config" / "env" / "local-4060.json")

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, cond, detail))


def expect_error(name: str, code: str, fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except CompileError as e:
        check(name, e.code == code, f"期望 {code}，实得 {e.code}: {str(e.message)[:80]}")
    except BaseException as e:  # noqa: BLE001
        check(name, False, f"期望 CompileError({code})，实得 {type(e).__name__}: {e}")
    else:
        check(name, False, f"期望 CompileError({code})，但未抛错")


def load() -> CapabilityMatrix:
    snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return CapabilityMatrix.from_snapshot(snap)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("真实 ComfyUI 环境回归 —— 基于 /object_info 快照")
    print("=" * 78)

    if not _SNAPSHOT.exists():
        print(f"❌ 快照不存在: {_SNAPSHOT}")
        print("   生成方式：连上真实 ComfyUI 后调用 /object_info，"
              "用 probe_from_object_info(...).to_snapshot() 落盘")
        return 2

    m = load()
    comp = ComfyCompiler(m)

    # ---------- R1 快照本身 ----------
    check("R1 快照可加载并还原节点数", m.node_count() == 970,
          f"{m.node_count()} 节点")
    check("R1 快照带版本与环境哈希",
          bool(m.comfy_version) and m.catalog_hash().startswith("sha256:"),
          f"comfy={m.comfy_version} hash={m.catalog_hash()[:24]}…")
    check("R1 ComfyUI 版本为 0.39.0", m.comfy_version == "0.39.0", m.comfy_version)

    # ---------- R2 规则表与真实环境吻合（这是 R1 的核心产出）----------
    conflicts = comp.validate_rule_invariants()
    check("R2 规则表**候选间**无契约冲突", conflicts == [],
          "; ".join(f"{p['kind']}:{p['conflictingClassType']}" for p in conflicts))

    decl = comp.validate_rule_declarations()
    check("R2 规则**声明**与真实 spec 全部吻合", decl == [],
          "; ".join(f"{p['kind']}:{p['diffs']}" for p in decl))

    # ---------- R3 回归锁定：曾出过错的候选 ----------
    # identity.reference 早期把 ReferenceLatent 列为候选。它输出 CONDITIONING、
    # 无 model/image 端口 —— 未装 IPAdapter 时会被选中，产出类型错位的图。
    ref_rule = comp.rules.get("identity.reference")
    check("R3 identity.reference 不再包含 ReferenceLatent（历史缺陷）",
          "ReferenceLatent" not in ref_rule.class_types,
          str(ref_rule.class_types))
    check("R3 本机确实未装 IPAdapter（正是它暴露了该缺陷）",
          not m.has("IPAdapterAdvanced") and not m.has("IPAdapterUnifiedLoader"))

    # 同 kind 内候选的**输出元数**必须一致：这是"能否共用一套 port_map"的判据
    for kind in ("sample.ksampler", "sample.ksampler_advanced"):
        rule = comp.rules.get(kind)
        outs = {ct: tuple(m.spec(ct).outputs) for ct in rule.class_types if m.has(ct)}
        check(f"R3 {kind} 候选输出元数一致", len(set(outs.values())) <= 1, str(outs))

    # ---------- R4 关键节点的缓存失效钩子（源码级核实的落地验证）----------
    li = m.spec("LoadImage")
    check("R4 LoadImage 被识别为 HOOKED（IS_CHANGED 哈希文件内容）",
          li.fingerprint_hook == "IS_CHANGED"
          and li.determinism == DeterminismClass.HOOKED.value
          and li.fingerprint_source == "known_table",
          f"{li.fingerprint_hook}/{li.determinism}/{li.fingerprint_source}")
    check("R4 官方确定性节点（KSampler 等）被判为 PURE 而非 UNKNOWN（降噪）",
          m.spec("KSampler").determinism == DeterminismClass.PURE.value,
          m.spec("KSampler").determinism)

    # ---------- R5 本机实际具备的能力 ----------
    present = ["LoadImage", "CheckpointLoaderSimple", "CLIPTextEncode", "LoraLoader",
               "KSampler", "VAEDecode", "SaveImage", "EmptyLatentImage",
               "VAEEncode", "ConditioningConcat", "ConditioningCombine",
               "ControlNetApplyAdvanced", "ControlNetApply", "KSamplerAdvanced",
               "LoraLoaderModelOnly", "UNETLoader", "CLIPLoader", "VAELoader",
               "ReferenceLatent", "WanImageToVideo", "MiniMaxH3ImageToVideo",
               "MiniMaxH3ReferenceToVideo", "SVD_img2vid_Conditioning"]
    missing = [ct for ct in present if not m.has(ct)]
    check("R5 全部预期节点存在于真实环境", missing == [], f"缺失: {missing}")

    absent = ["IPAdapterAdvanced", "IPAdapterUnifiedLoader", "MiniMaxH3Ref2VA",
              "QwenTTSNode", "MiniMaxH3MemoryEfficientSageAttentionPatch",
              "MiniMaxH3Extension"]
    wrongly_present = [ct for ct in absent if m.has(ct)]
    check("R5 未安装的第三方节点确实不在环境里（快照真实性）",
          wrongly_present == [], f"意外存在: {wrongly_present}")

    # ---------- R6 无模型权重时编译被正确阻断 ----------
    sys.argv = ["x"]
    from little_crew.compiler.test_compiler import shot_ir

    expect_error("R6 无权重环境阻断编译（而非产出必错的图）",
                 CompileErrorCode.MISSING_REQUIRED_NODE, comp.compile, shot_ir())

    # ---------- R7 多参考图缺口（已知限制，如实锁定）----------
    # MiniMaxH3ReferenceToVideo 的参考图走 COMFY_AUTOGROW_V3 动态端口，
    # 而 IR 目前只表达单一参考图 —— 故规则**不声明** image 映射。
    ref2v = comp.rules.get("video.ref2v")
    check("R7 video.ref2v 未声明 image 映射（IR 尚未支持多参考资产）",
          "image" not in ref2v.port_map,
          str(dict(ref2v.port_map)))
    ref2v_spec = m.spec("MiniMaxH3ReferenceToVideo")
    has_grow = any(
        "AUTOGROW" in str(p.dtype)
        for p in list(ref2v_spec.inputs_optional.values())
    )
    check("R7 真实节点确实使用动态端口（COMFY_AUTOGROW_V3）", has_grow,
          str({k: v.dtype for k, v in ref2v_spec.inputs_optional.items()}))

    # ---------- R8 规则表与环境的整体适配度 ----------
    total = len(DEFAULT_RULES)
    hit = sum(1 for r in DEFAULT_RULES if m.first_available(r.class_types))
    req_missing = [r.kind for r in DEFAULT_RULES
                   if r.required and not m.first_available(r.class_types)]
    check("R8 全部**必需** kind 在真实环境可用", req_missing == [],
          f"缺失: {req_missing}")
    check("R8 规则表整体命中率合理（>= 60%）", hit / total >= 0.6,
          f"{hit}/{total}")

    # ---------- R9 漂移基线对比可检出钩子变化 ----------
    from little_crew.capability.capability import probe_from_object_info
    snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    # 用同一份 nodes 但显式声明 LoadImage 无钩子 -> 应报 FINGERPRINT_HOOK_CHANGED
    nodes_only = snap["nodes"]
    from little_crew.capability.capability import PortSpec, NodeSpec
    stripped: dict[str, NodeSpec] = {}
    for ct, body in nodes_only.items():
        if ct == "LoadImage":
            stripped[ct] = NodeSpec(
                class_type=ct, category=body["category"],
                inputs_required={k: PortSpec(k, v["dtype"], True,
                                              tuple(v["enumValues"]) if v.get("enumValues") else None,
                                              v.get("default"), bool(v.get("isFileInput")))
                                 for k, v in body["required"].items()},
                inputs_optional={},
                outputs=tuple(body["outputs"]),
                output_node=bool(body["outputNode"]),
                fingerprint_hook=None, determinism=DeterminismClass.PURE.value,
            )
        else:
            stripped[ct] = m.spec(ct)
    m2 = CapabilityMatrix(nodes=stripped, comfy_version=m.comfy_version)
    drift = m2.detect_drift({}, baseline=m)
    check("R9 基线对比可检出缓存钩子变化（ABI 变化）",
          any(d.kind == DriftKind.FINGERPRINT_HOOK_CHANGED.value for d in drift),
          str([d.as_dict() for d in drift if d.kind == DriftKind.FINGERPRINT_HOOK_CHANGED.value]))

    # ---------- 汇总 ----------
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if not ok or detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    if npass == len(_results):
        print()
        print(f"  环境快照   : {_SNAPSHOT.name}  ({_SNAPSHOT.stat().st_size/1024**2:.2f} MB)")
        print(f"  ComfyUI    : {m.comfy_version}   节点 {m.node_count()}")
        print(f"  catalogHash: {m.catalog_hash()}")
        print(f"  规则表     : {total} kind，命中 {hit}")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
