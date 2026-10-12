"""返工动作 —— 把「断言失败」变成**可执行的结构化修正**。

为什么需要这一层
----------------
`gate.repairs_for()` 产出的是**给人看的句子**：

    [cr_res_001] 分辨率 64x48 != 要求 832x480。返工方向：改回目标分辨率…

那句话**机器读不懂**。所以"返工闭环"缺的不是"知道哪里错了"，
而是"**知道该改什么**"。

本模块补的就是这一环：每条断言失败时，除了给人看的句子，
再产出一个**结构化的动作**：

    RepairAction(kind="ir.set_param",
                 target_node="n_latent",
                 params={"width": 832, "height": 480},
                 reason="分辨率不符")

然后 `EpisodeRunner` 把它应用到 IR 上、**重跑**、**复检**。

三条铁律
--------
**① 动作必须改变 IR 的哈希。**
否则重跑会命中缓存、拿回同一张图，循环空转。
`apply()` 返回新 IR 后必须验证哈希变了 —— 这是循环能否前进的唯一判据。

**② 修不了的动作要如实标记，不能假装能修。**
`identity_similarity` 的失败**本机修不了**（缺锚点图、缺 IPAdapter 权重）。
把它标成"可修"会让循环白跑 N 次然后失败，而真正的问题是能力缺失。
所以有 `applicable` 字段 —— 不可应用的动作**不触发重跑**。

**③ 动作是"对 IR 的编辑"，不是"对图的编辑"。**
平台的一切可复现性建立在"IR 是唯一真相"上。
直接改图会绕开哈希链，让"这张图是怎么来的"无从追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from little_crew.review.constraint import ConstraintAssertion

__all__ = [
    "RepairAction",
    "plan_repairs",
    "apply_repairs",
    "is_repairable",
]


@dataclass(frozen=True)
class RepairAction:
    """一条可执行的修正。

    **`params` 是给 IR 的，不是给人的。** 人看的是 `gate.repairs_for()`
    产出的句子；这里只放机器要用的东西。
    """

    #: 动作类型（见 `_PLANNERS`）
    kind: str
    #: 作用到哪个 IR 节点上（`None` 表示图级）
    target_node: str | None = None
    #: 要写入的参数
    params: Mapping[str, Any] = field(default_factory=dict)
    #: 来源约束（可追溯）
    constraint_ref: str = ""
    #: 为什么这么改（进日志与报告）
    reason: str = ""
    #: **能否在本机应用**。`False` 的动作不触发重跑（见模块 docstring 铁律②）
    applicable: bool = True
    #: 不可应用时说明原因
    blocked_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "targetNode": self.target_node,
            "params": dict(self.params),
            "constraintRef": self.constraint_ref,
            "reason": self.reason,
            "applicable": self.applicable,
            "blockedBy": self.blocked_by,
        }


# ---------------------------------------------------------------------------
# 由断言推导动作
# ---------------------------------------------------------------------------

def _plan_resolution(a: ConstraintAssertion) -> RepairAction | None:
    """分辨率不符 -> 改 `latent.empty` 的 width/height。"""
    exp = a.expected
    w, h = exp.get("width"), exp.get("height")
    if not (isinstance(w, int) and isinstance(h, int)):
        return None
    return RepairAction(
        kind="ir.set_params", target_node="n_latent",
        params={"width": w, "height": h},
        constraint_ref=a.constraint_ref,
        reason=f"分辨率应为 {w}x{h}（实际 "
               f"{a.observed.get('width')}x{a.observed.get('height')}）",
    )


def _plan_aspect(a: ConstraintAssertion) -> RepairAction | None:
    """宽高比不符 -> 按目标比例与当前高度重算宽度。"""
    ratio = a.expected.get("ratio")
    h = a.observed.get("height")
    if not (isinstance(ratio, (int, float)) and isinstance(h, int)):
        return None
    return RepairAction(
        kind="ir.set_params", target_node="n_latent",
        params={"width": max(1, round(float(ratio) * h)), "height": h},
        constraint_ref=a.constraint_ref,
        reason=f"宽高比应为 {ratio}（保持高度 {h} 重算宽度）",
    )


def _plan_duration(a: ConstraintAssertion) -> RepairAction | None:
    """时长不符 -> 改镜头的 `duration_sec`。

    ⚠️ 时长**不在 IR 节点里**（它是镜头属性，写进了 `provenance`），
    所以这个动作的目标是"镜头"而不是 IR —— 用 `shot.set_field` 区分。
    """
    exp = a.expected
    obs = a.observed.get("durationSec")
    mx = exp.get("max")
    target = None
    if isinstance(mx, (int, float)):
        target = float(mx)
    elif isinstance(exp.get("min"), (int, float)):
        target = float(exp["min"])
    if target is None:
        return None
    return RepairAction(
        kind="shot.set_duration", params={"durationSec": target},
        constraint_ref=a.constraint_ref,
        reason=f"时长应为 {target:g} 秒（实际 {obs}）",
    )


def _plan_cast(a: ConstraintAssertion) -> RepairAction | None:
    """同框角色超限 -> **不可自动修**。

    削减角色是**创作决策**（删谁？合镜？拆镜？），不该由规则自动做。
    如实标为不可应用，让上层决定是升级人工还是放宽约束。
    """
    mx = a.expected.get("max")
    got = a.observed.get(a.expected.get("field", "count"))
    return RepairAction(
        kind="shot.reduce_cast",
        params={"max": mx, "actual": got},
        constraint_ref=a.constraint_ref,
        reason=f"同框角色 {got} 超过上限 {mx}",
        applicable=False,
        blocked_by=("削减角色是创作决策（删谁/合镜/拆镜），"
                    "不该由规则自动做；需人工介入或放宽约束"),
    )


def _plan_identity(a: ConstraintAssertion) -> RepairAction | None:
    """身份相似度不足 -> **本机不可修**。

    修它需要锚点图（一张都没有）或 IPAdapter 权重（本机没有）。
    标为不可应用是**诚实的**：假装可修会让循环白跑 N 次，
    而真正的问题是能力缺失。
    """
    return RepairAction(
        kind="identity.strengthen",
        params={"min": a.expected.get("min"),
                "actual": a.observed.get("similarity")},
        constraint_ref=a.constraint_ref,
        reason=f"身份相似度 {a.observed.get('similarity')} 低于 "
               f"{a.expected.get('min')}",
        applicable=False,
        blocked_by=("需要角色锚点图或 IPAdapter 权重 —— "
                    "本机两者都没有（见策划书 §5.8）"),
    )


def _plan_text(a: ConstraintAssertion) -> RepairAction | None:
    """文本差异过大 -> 不可自动修（要以剧本原文为准重写）。"""
    return RepairAction(
        kind="text.rewrite",
        params={"maxRatio": a.expected.get("maxRatio"),
                "diffRatio": a.observed.get("diffRatio")},
        constraint_ref=a.constraint_ref,
        reason=f"文本差异 {a.observed.get('diffRatio')} 超过 "
               f"{a.expected.get('maxRatio')}",
        applicable=False,
        blocked_by="需要按剧本原文重写台词，属生成决策",
    )


def _plan_size(a: ConstraintAssertion) -> RepairAction | None:
    """文件大小异常 -> 不可直接修（要查编码链路）。"""
    n = a.observed.get("bytes")
    return RepairAction(
        kind="media.reencode",
        params={"bytes": n, "minBytes": a.expected.get("minBytes")},
        constraint_ref=a.constraint_ref,
        reason=f"文件仅 {n} 字节 —— 很可能是编码失败",
        applicable=False,
        blocked_by="需检查编码链路（ffmpeg 参数/输入帧），不是参数问题",
    )


#: 算子 -> 动作规划器。**显式列出，不靠猜**：
#: 猜错会让人/循环改错地方，那比"没有动作"更糟。
_PLANNERS: Mapping[str, Any] = {
    "resolution_matches": _plan_resolution,
    "aspect_ratio_within": _plan_aspect,
    "duration_within": _plan_duration,
    "count_at_most": _plan_cast,
    "identity_similarity": _plan_identity,
    "pairwise_similarity": _plan_identity,
    "token_diff": _plan_text,
    "byte_size_within": _plan_size,
    "not_empty": _plan_size,
    "count_at_least": _plan_cast,
}


def plan_repairs(assertions: Sequence[ConstraintAssertion]
                 ) -> tuple[RepairAction, ...]:
    """由**失败的硬断言**推导可执行动作。

    只处理硬断言 —— 软约束失败不阻断交付，也就不触发返工
    （否则重跑会因为一条不阻断的约束而无限进行）。
    """
    out: list[RepairAction] = []
    for a in assertions:
        if a.passed or not a.blocking:
            continue
        planner = _PLANNERS.get(a.op)
        if planner is None:
            # 没有规划器 -> **如实标记**，不要静默跳过
            out.append(RepairAction(
                kind="unknown", constraint_ref=a.constraint_ref,
                reason=a.detail or f"{a.op} 未通过",
                applicable=False,
                blocked_by=f"算子 {a.op!r} 没有预设的返工动作",
            ))
            continue
        act = planner(a)
        if act is not None:
            out.append(act)
    return tuple(out)


def is_repairable(actions: Sequence[RepairAction]) -> bool:
    """这一批动作里**有没有**能真正改变 IR 的。

    判据是"至少一条 `applicable`"。全是不可应用的动作时，
    重跑只会拿到同一张图（甚至直接命中缓存）——
    **空转的循环比不循环更糟**：它烧时间、刷日志，却什么都没改。
    """
    return any(a.applicable for a in actions)


# ---------------------------------------------------------------------------
# 应用到 IR
# ---------------------------------------------------------------------------

def apply_repairs(ir: Mapping[str, Any],
                  actions: Sequence[RepairAction]
                  ) -> tuple[dict[str, Any], list[str]]:
    """把动作应用到 IR（或镜头字段）。

    返回 `(新 IR, 应用日志)`。**不修改入参** —— 原始 IR 要留着做对照，
    否则"改了什么"就无从追溯（而那是复现的前提）。

    只处理 `applicable` 的动作；其余**在日志里说明为什么没动**。
    """
    import copy

    out = copy.deepcopy(dict(ir))
    log: list[str] = []
    nodes = {n.get("id"): n for n in out.get("nodes", ())}

    for a in actions:
        if not a.applicable:
            log.append(f"跳过 {a.kind}（{a.constraint_ref}）：{a.blocked_by}")
            continue

        if a.kind == "ir.set_params":
            node = nodes.get(a.target_node)
            if node is None:
                log.append(f"跳过 {a.constraint_ref}：IR 里没有节点 "
                           f"{a.target_node!r}")
                continue
            params = node.setdefault("params", {})
            before = {k: params.get(k) for k in a.params}
            params.update(a.params)
            log.append(f"应用 {a.constraint_ref}：节点 {a.target_node} "
                       f"{before} -> {dict(a.params)}")

        elif a.kind == "shot.set_duration":
            # 时长在 `provenance` 里（它不是 IR 节点参数）
            prov = out.setdefault("provenance", {})
            before = prov.get("durationSec")
            prov["durationSec"] = a.params.get("durationSec")
            log.append(f"应用 {a.constraint_ref}：镜头时长 "
                       f"{before} -> {prov['durationSec']}")

        else:
            log.append(f"跳过 {a.kind}（{a.constraint_ref}）："
                       f"没有对应的应用器")

    return out, log
