"""结构化消息协议 —— Agent 之间**不传自由文本**。

这是「多智能体协作」与「多轮对话」的分水岭
==========================================

若 Agent 之间传自由文本，"协作"退化为"多轮对话"：
**无法审计**（谁说了什么、依据是什么）、**无法量化**（返工到底改善了多少）、
**无法复现**（同样的输入得不到同样的流程）。

因此本模块强制所有跨 Agent 通信走带 schema 的结构化消息。

核心设计：**审查意见必须可定位、可执行**
----------------------------------------
一条审查意见若只写"第三场节奏有问题"，它是**不可执行的** ——
编排器不知道改哪里，也不知道改完算不算好。

所以 `ReviewIssue` 强制要求：

  · `location`  **结构化定位**（场 / 镜 / 行），不是散文
  · `severity`  严重度，决定是否阻断
  · `suggestion` 可操作的修改建议

而 `ReviewFeedback.repair_hints` 更进一步：**每条返工指令都必须能映射成 IR 变更**。
这是协作闭环的落点 —— 审查 → 返工指令 → IR 修改 → 重编译 → 再审。

服务端权威算分（不信任模型自报）
--------------------------------
模型在评分任务上有**系统性乐观偏差**：自报分与其给出的维度明细常常不自洽
（如自报 90 分，但四个维度里两个是 60）。

因此：
  · 总分的权威来源是 `QcScore`（由**维度明细加权算出**），不是模型报的数字
  · 模型自报分只作为 `claimed_score` 记录，用于**偏差检测**
  · 偏差 > `SELF_REPORT_TOLERANCE` 则整个报告作废重审

这条规则的价值不在于"更准"，而在于**不可被绕过**：模型即使想放水，
也无法让一个自相矛盾的报告通过。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "MessageKind",
    "Severity",
    "Verdict",
    "Dimension",
    "QC5_WEIGHTS",
    "DIMENSION_LABELS",
    "SELF_REPORT_TOLERANCE",
    "COMPLETENESS_PENALTY_PER_SCENE",
    "IssueLocation",
    "ReviewIssue",
    "QcScore",
    "ArtifactRef",
    "RepairHint",
    "Message",
    "ReviewFeedback",
    "ScriptDraft",
    "envelope",
    "validate_message",
]


# ==========================================================================
# 枚举
# ==========================================================================

class MessageKind(str, Enum):
    """消息类型。**白名单**：未登记的类型一律拒绝，防止协议悄悄膨胀。"""

    #: 交稿（编剧交剧本草稿）
    DELIVERABLE = "deliverable"
    #: 审查意见（剧本顾问 / 文学策划 / 场记 / 审片）
    REVIEW = "review_feedback"
    #: 返工指令（编排器合并多方意见后下发）
    REVISION_REQUEST = "revision_request"
    #: 阶段完成通知
    STAGE_DONE = "stage_done"
    #: 上报问题（阻塞、需要人介入）
    BLOCKER = "blocker"


class Severity(str, Enum):
    """问题严重度。决定是否阻断返工。"""

    INFO = "info"          # 记录，不影响通过
    MINOR = "minor"        # 建议改，不阻断
    MAJOR = "major"        # 必须改
    BLOCKER = "blocker"    # 阻断：不改就不能进下一阶段


class Verdict(str, Enum):
    """审查结论。"""

    PASS = "PASS"              # 通过
    REVISE = "REVISE"          # 需修订后重审
    REJECT = "REJECT"          # 不可修订，需重做
    ESCALATE = "ESCALATE"      # 需人介入


#: **每有一个"结构性不完整"的场次，完整性扣多少分。**
#:
#: ## 为什么不用"缺陷场次 / 总场次"这个比例（2026-10-17 实测后改）
#:
#: 旧公式 `100 × (1 - 缺陷场次/总场次)` 有两个后果，实测都发生了：
#:
#: | 剧本长度 | 缺 1 场时不完整度 |
#: |---|---|
#: | **2 场** | **50%** |
#: | 5 场 | 20% |
#: | 8 场 | 12.5% |
#:
#: **后果一：同样的绝对缺陷量，短剧本被罚 3~4 倍。**
#: 条件对比实验（`research/threshold-calibration/`）实测：
#: 同一份素材强制切成 2 场后，总分均值比 5 场的低 **17.7 分**。
#:
#: **后果二：短剧本会撞到 0 的"地板"。**
#: 2 场里两场都有缺陷 -> 完整性直接归零 -> 总分固定 41.4。
#: 实测里短剧本组 5 次有 4 次停在这个地板分数上 ——
#: 那是**地板的形状，不是质量的连续分布**，阈值在它上面没有意义。
#:
#: ## 新公式：按场次的**绝对**惩罚
#:
#: ```
#: completeness = max(0, 100 - COMPLETENESS_PENALTY_PER_SCENE × 缺陷场次数)
#: ```
#:
#: | 缺陷场次 | 完整性 |
#: |---|---|
#: | 0 | 100 |
#: | 1 | 75 |
#: | 2 | 50 |
#: | 4 及以上 | 0 |
#:
#: **与剧本长度无关** —— "一场缺转折"在任何长度的剧本里都是同一件事。
#:
#: ## ⚠️ 这个改动**不**意味着短剧本变合格了
#:
#: 它只是让"每个缺陷的代价"变得可比。**"一集只有 2 场"本身是不是问题，
#: 是另一个独立的判断** —— 不该藏在比例公式里当一个隐形惩罚。
#: 现在它会被如实看见：2 场、2 场有缺陷 -> 完整性 50，而不是被压成 0。
#:
#: 取 25 的依据：4 个缺陷场次即归零。对 5~8 场的剧本来说，
#: 4 场结构不完整已经是"该重写"的程度，不该再细分。
COMPLETENESS_PENALTY_PER_SCENE: float = 25.0


class Dimension(str, Enum):
    """QC-5 五维品控。"""

    COMPLETENESS = "completeness"    # 完整性
    CONSISTENCY = "consistency"      # 一致性（权重最高）
    QUALITY = "quality"              # 品质感
    FEASIBILITY = "feasibility"      # 可执行性
    COMPLIANCE = "compliance"        # 合规性


#: QC-5 各维度的默认权重（合计 1.0）。
#:
#: **为什么一致性权重最高（0.40）**：漫剧的核心难点是长程一致性崩塌 ——
#: 角色脸/服装跨镜漂移是观众最先察觉、也最难靠后期补救的问题。
#: 单张图的"品质感"再高，脸不像同一个人，整集就废了。
DEFAULT_QC5_WEIGHTS: Mapping[Dimension, float] = {
    Dimension.CONSISTENCY: 0.40,
    Dimension.COMPLETENESS: 0.25,
    Dimension.QUALITY: 0.25,
    Dimension.FEASIBILITY: 0.05,
    Dimension.COMPLIANCE: 0.05,
}

#: 分阶段权重。**草稿阶段放宽一致性**（此时还没有锚点图可比对），
#: **成片阶段收紧**（锚点图已在位，必须严查）。
PHASE_QC5_WEIGHTS: Mapping[str, Mapping[Dimension, float]] = {
    "draft": {
        Dimension.CONSISTENCY: 0.20,
        Dimension.COMPLETENESS: 0.35,
        Dimension.QUALITY: 0.30,
        Dimension.FEASIBILITY: 0.10,
        Dimension.COMPLIANCE: 0.05,
    },
    "final": DEFAULT_QC5_WEIGHTS,
}

#: 兼容旧名。
QC5_WEIGHTS = DEFAULT_QC5_WEIGHTS

DIMENSION_LABELS: Mapping[Dimension, str] = {
    Dimension.COMPLETENESS: "完整性",
    Dimension.CONSISTENCY: "一致性",
    Dimension.QUALITY: "品质感",
    Dimension.FEASIBILITY: "可执行性",
    Dimension.COMPLIANCE: "合规性",
}

#: 模型自报分与权威算分的最大允许偏差。超出则报告作废重审。
SELF_REPORT_TOLERANCE: float = 5.0


# ==========================================================================
# 定位与问题
# ==========================================================================

@dataclass(frozen=True)
class IssueLocation:
    """**结构化定位**。散文式定位无法驱动自动返工，因此必须是字段。

    三个字段可单独或组合使用：
      · 剧本阶段用 ``scene`` / ``line``
      · 分镜与渲染阶段用 ``shot``
      · 资产阶段用 ``asset``
    """

    scene: str = ""      # 场次 id，如 "SC-E001-003"
    shot: str = ""       # 镜头 id，如 "SH-E001-003-007"
    line: int = 0        # 行号（剧本阶段）
    asset: str = ""      # 资产 id，如 "CHAR-0007"

    def is_empty(self) -> bool:
        return not (self.scene or self.shot or self.line or self.asset)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.scene:
            d["scene"] = self.scene
        if self.shot:
            d["shot"] = self.shot
        if self.line:
            d["line"] = self.line
        if self.asset:
            d["asset"] = self.asset
        return d

    def describe(self) -> str:
        parts = []
        if self.scene:
            parts.append(self.scene)
        if self.shot:
            parts.append(self.shot)
        if self.line:
            parts.append(f"第{self.line}行")
        if self.asset:
            parts.append(self.asset)
        return " / ".join(parts) or "(未定位)"


@dataclass(frozen=True)
class ReviewIssue:
    """一条审查意见。**四个字段都是必需的** —— 缺一个就无法自动处理。"""

    severity: Severity
    dimension: Dimension
    location: IssueLocation
    problem: str          # 问题是什么（可读）
    suggestion: str = ""  # 怎么改（可操作）

    def __post_init__(self) -> None:
        if not self.problem:
            raise ValueError("ReviewIssue.problem 不能为空 —— 无问题的意见没有意义")
        if self.severity is Severity.BLOCKER and not self.suggestion:
            raise ValueError(
                f"BLOCKER 级问题必须给出 suggestion（位置 {self.location.describe()}），"
                f"否则编排器无法产生可执行的返工指令"
            )

    @property
    def blocking(self) -> bool:
        return self.severity in (Severity.MAJOR, Severity.BLOCKER)

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "dimension": self.dimension.value,
            "location": self.location.as_dict(),
            "problem": self.problem,
            "suggestion": self.suggestion,
        }


# ==========================================================================
# 评分（服务端权威）
# ==========================================================================

@dataclass(frozen=True)
class QcScore:
    """QC-5 评分。**总分由维度明细加权算出，不是模型报的数字。**

    这是"服务端权威算分"的落点 —— 见本模块 docstring 的说明。
    """

    dimensions: Mapping[Dimension, float]
    phase: str = "final"
    claimed_score: float | None = None    # 模型自报分（仅用于偏差检测）

    def __post_init__(self) -> None:
        for dim, v in self.dimensions.items():
            if not (0.0 <= float(v) <= 100.0):
                raise ValueError(f"维度 {dim.value} 的分数 {v} 超出 0~100")

    # ---- 权威计算 ------------------------------------------------------

    @property
    def weights(self) -> Mapping[Dimension, float]:
        return PHASE_QC5_WEIGHTS.get(self.phase, DEFAULT_QC5_WEIGHTS)

    @property
    def total(self) -> float:
        """加权总分。**权威值** —— 由维度明细算出，与自报分无关。"""
        w = self.weights
        num = sum(float(self.dimensions.get(d, 0.0)) * w[d] for d in w)
        den = sum(w[d] for d in w if d in self.dimensions)
        return round(num / den, 2) if den else 0.0

    # ---- 偏差检测 ------------------------------------------------------

    @property
    def deviation(self) -> float | None:
        """自报分与权威分之差（绝对值）。None = 未自报。"""
        if self.claimed_score is None:
            return None
        return round(abs(float(self.claimed_score) - self.total), 2)

    @property
    def self_report_trustworthy(self) -> bool:
        """自报分是否可信。

        不可信时**整个报告作废重审** —— 不是因为自报分错了，
        而是因为"自报分与其自身维度明细不自洽"这件事本身说明
        该 Agent 的判定过程不可靠，它的定性意见也不该采信。
        """
        dev = self.deviation
        return dev is None or dev <= SELF_REPORT_TOLERANCE

    # ---- 最低维度的短板 ------------------------------------------------

    @property
    def weakest(self) -> tuple[Dimension, float] | None:
        """最弱维度。**返工优先级应看它而非总分** ——
        总分 80 但一致性 45 的镜头仍然不可用。
        """
        if not self.dimensions:
            return None
        d = min(self.dimensions, key=lambda k: float(self.dimensions[k]))
        return d, float(self.dimensions[d])

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimensions": {d.value: float(v) for d, v in self.dimensions.items()},
            "weights": {d.value: w for d, w in self.weights.items()},
            "total": self.total,
            "claimedScore": self.claimed_score,
            "deviation": self.deviation,
            "selfReportTrustworthy": self.self_report_trustworthy,
            "weakest": ({"dimension": self.weakest[0].value,
                         "score": self.weakest[1]} if self.weakest else None),
            "phase": self.phase,
        }


# ==========================================================================
# 产物引用与返工指令
# ==========================================================================

@dataclass(frozen=True)
class ArtifactRef:
    """产物引用。消息里传**引用**而非内容 —— 内容在黑板/存储里。"""

    key: str                 # 黑板键或存储路径
    kind: str = ""           # script | image | video | audio | report
    sha256: str = ""
    version: int = 0

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"key": self.key}
        if self.kind:
            d["kind"] = self.kind
        if self.sha256:
            d["sha256"] = self.sha256
        if self.version:
            d["version"] = self.version
        return d


@dataclass(frozen=True)
class RepairHint:
    """返工指令。**必须能映射成 IR 变更** —— 这是协作闭环的落点。

    `ir_ops` 里每一项都是一条**声明式**的图操作，由下游翻译成实际的 IR JSON 补丁。
    用声明式而非直接给 JSON 补丁的理由：补丁与 IR 版本强耦合，
    而声明式的操作意图可以在编译期校验（如"该镜头是否存在"）。
    """

    #: 目标 IR 图 id
    graph_id: str
    #: 操作类型："insert_shot" | "modify_shot" | "update_binding" | "relax_constraint"
    op: str
    #: 操作参数
    args: Mapping[str, Any] = field(default_factory=dict)
    #: 这条指令来自哪条审查意见（可追溯）
    from_issue: str = ""

    #: 白名单：未登记的操作类型一律拒绝
    KNOWN_OPS: frozenset[str] = frozenset({
        "insert_shot", "remove_shot", "modify_shot",
        "update_binding", "add_constraint", "relax_constraint",
        "regenerate_asset", "adjust_prompt",
    })

    def __post_init__(self) -> None:
        if self.op not in self.KNOWN_OPS:
            raise ValueError(
                f"未知的返工操作 {self.op!r}；可用：{sorted(self.KNOWN_OPS)}")
        if not self.graph_id:
            raise ValueError("RepairHint.graph_id 不能为空 —— 返工必须指明目标图")

    def as_dict(self) -> dict[str, Any]:
        return {"graphId": self.graph_id, "op": self.op,
                "args": dict(self.args), "fromIssue": self.from_issue}


# ==========================================================================
# 消息封装
# ==========================================================================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(payload: Mapping[str, Any]) -> str:
    """计算消息的内容哈希。用于去重与可复现校验。

    规范化方式与 IR 保持一致（排序键、紧凑分隔符），
    使"语义等价 ⇒ 哈希相同"这条不变量在消息层也成立。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ==========================================================================
# dict -> 消息（`as_dict` 的逆操作）
# ==========================================================================

def _enum_or(enum_cls: Any, raw: Any, default: Any) -> Any:
    """把字符串还原成枚举。**认不出来就退回默认值**，不抛错。

    为什么宽容：消息的 dict 形态来自 agent 输出（其中就有 LLM 生成的 JSON），
    `verdict` 写成 `"pass"`、漏字段都是**常态** —— 那是数据问题，不是编程错误。
    编程错误该炸，数据问题该降级并留痕。
    """
    if isinstance(raw, enum_cls):
        return raw
    if raw is None or raw == "":
        return default
    try:
        return enum_cls(str(raw))
    except ValueError:
        return default


def _int_or(raw: Any, default: int) -> int:
    try:
        return int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _float_or(raw: Any, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _msg_kwargs(d: Mapping[str, Any], *, frm: str, to: str) -> dict[str, Any]:
    """还原 `Message` 基类字段。缺失的 `from`/`to` 用调用方给的默认值。"""
    out: dict[str, Any] = {
        "kind": _enum_or(MessageKind, d.get("kind"), MessageKind.DELIVERABLE),
        "msg_id": str(d.get("msgId") or ""),
        "frm": str(d.get("from") or frm or "unknown"),
        "to": str(d.get("to") or to or "*"),
        "reply_to": str(d.get("replyTo") or ""),
        "trace_id": str(d.get("traceId") or ""),
        "stage": str(d.get("stage") or ""),
        "refs": tuple(
            ArtifactRef(key=str(r.get("key") or ""),
                        kind=str(r.get("kind") or ""),
                        sha256=str(r.get("sha256") or ""),
                        version=_int_or(r.get("version"), 0))
            for r in (d.get("refs") or ())
            if isinstance(r, Mapping) and r.get("key")),
        "note": str(d.get("note") or ""),
    }
    if d.get("createdAt"):
        out["created_at"] = str(d["createdAt"])
    return out


def _location_from(raw: Any) -> IssueLocation:
    if isinstance(raw, IssueLocation):
        return raw
    if not isinstance(raw, Mapping):
        return IssueLocation()
    try:
        line = int(raw.get("line") or 0)
    except (TypeError, ValueError):
        line = 0
    return IssueLocation(scene=str(raw.get("scene") or ""),
                         shot=str(raw.get("shot") or ""),
                         line=line,
                         asset=str(raw.get("asset") or ""))


def _issue_from(raw: Mapping[str, Any]) -> ReviewIssue | None:
    """一条意见。**构造不出就降级保留，不丢**。

    ## 为什么不能丢（自查发现，2026-10-23）
    第一版写的是「构造不出返回 None，由调用方丢弃并计数」。但 `note` 是
    「仅供人读，不参与任何判定」的字段 —— 于是丢掉一条 **BLOCKER**
    就等于把「有阻断问题」变成「没有问题」：

        issues 2 条 -> 1 条
        blocking_issues 1 -> 0
        effective_verdict REVISE -> PASS      ← 阻断被静默降级成通过

    那正是本项目最忌讳的「看起来通过」。所以：
      · `problem` 为空 -> 真的没有内容，返回 None（调用方计数）
      · BLOCKER 缺 `suggestion` -> **保留**，等级记为 MAJOR（同样是 blocking），
        并把「模型没给建议」写进 suggestion，让返工环节看得见
    """
    problem = str(raw.get("problem") or "").strip()
    if not problem:
        return None
    severity = _enum_or(Severity, raw.get("severity"), Severity.MINOR)
    suggestion = str(raw.get("suggestion") or "").strip()
    if severity is Severity.BLOCKER and not suggestion:
        severity = Severity.MAJOR
        suggestion = "（模型未给出修改建议 —— 仍按阻断处理，需人工确认）"
    try:
        return ReviewIssue(
            severity=severity,
            dimension=_enum_or(Dimension, raw.get("dimension"), Dimension.QUALITY),
            location=_location_from(raw.get("location")),
            problem=problem,
            suggestion=suggestion,
        )
    except ValueError:
        return None


def _hint_from(raw: Mapping[str, Any]) -> RepairHint | None:
    """一条返工指令。op 不在白名单 / 缺 graph_id 时返回 None。

    ⚠️ `args` 若不是 Mapping，`dict(...)` 会抛 **TypeError**（不是 ValueError）——
    只捕 ValueError 会让一条畸形指令把整轮审查打崩（2026-10-23 自查）。
    """
    args = raw.get("args")
    try:
        return RepairHint(
            graph_id=str(raw.get("graphId") or raw.get("graph_id") or ""),
            op=str(raw.get("op") or ""),
            args=dict(args) if isinstance(args, Mapping) else {},
            from_issue=str(raw.get("fromIssue") or raw.get("from_issue") or ""),
        )
    except (TypeError, ValueError):
        return None


def _score_from(raw: Any) -> QcScore | None:
    """QC 评分。维度名认不出来就丢掉那一维；一维都不剩则返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, QcScore):
        return raw
    if not isinstance(raw, Mapping):
        return None
    dims: dict[Dimension, float] = {}
    src = raw.get("dimensions")
    if isinstance(src, Mapping):
        for k, v in src.items():
            dim = _enum_or(Dimension, k, None)
            if dim is None:
                continue
            try:
                dims[dim] = float(v)
            except (TypeError, ValueError):
                continue
    if not dims:
        return None
    claimed = raw.get("claimedScore")
    try:
        claimed_f = float(claimed) if claimed is not None else None
    except (TypeError, ValueError):
        claimed_f = None
    try:
        return QcScore(dimensions=dims,
                       phase=str(raw.get("phase") or "final"),
                       claimed_score=claimed_f)
    except ValueError:
        # 分数越界（模型常见错误）-> 不作分，但**不阻断**：定性意见仍然有效
        return None


@dataclass
class Message:
    """消息基类字段。所有具体消息都带这些。

    ⚠️ **所有字段都必须有默认值**：本类被继承（`ReviewFeedback` / `ScriptDraft`），
    而 dataclass 的字段顺序规则要求"有默认值的字段不能排在无默认值字段之前"。
    给基类全部字段默认值，子类才能自由追加字段。

    同理，``msg_id`` / ``frm`` / ``to`` 看似必填，实际在 ``__post_init__`` 中校验 ——
    **报错信息比 TypeError 更可读**（会说清是哪个字段、缺在哪条消息上）。
    """

    kind: MessageKind = MessageKind.DELIVERABLE
    msg_id: str = ""
    frm: str = ""               # 发送方 agent_id（用 frm 避开 from 关键字）
    to: str = ""                # 接收方 agent_id；"*" 表示广播给编排器
    reply_to: str = ""          # 回复哪条消息（审查链可追溯）
    trace_id: str = ""
    stage: str = ""
    created_at: str = field(default_factory=_now_iso)
    #: 该消息引用了哪些产物
    refs: tuple[ArtifactRef, ...] = ()
    #: 附注（自由文本，**仅供人读，不参与任何判定**）
    note: str = ""

    def __post_init__(self) -> None:
        if not self.msg_id:
            # 由内容确定性地派生，保证"同内容同 id"（可复现）
            seed = f"{self.kind.value}|{self.frm}|{self.to}|{self.stage}|{self.created_at}"
            self.msg_id = "msg_" + hashlib.sha256(
                seed.encode("utf-8")).hexdigest()[:16]
        if not self.frm:
            raise ValueError("消息缺 from（发送方 agent_id）")
        if not self.to:
            raise ValueError("消息缺 to（接收方 agent_id，广播用 '*'）")

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "msgId": self.msg_id,
            "from": self.frm,
            "to": self.to,
            "replyTo": self.reply_to,
            "traceId": self.trace_id,
            "stage": self.stage,
            "createdAt": self.created_at,
            "refs": [r.as_dict() for r in self.refs],
            "note": self.note,
        }

    @property
    def content_hash(self) -> str:
        return envelope(self.as_dict())


@dataclass
class ReviewFeedback(Message):
    """审查意见。**生成方与审核方不同族、且审核方不得审自己的产出。**"""

    kind: MessageKind = MessageKind.REVIEW
    verdict: Verdict = Verdict.PASS
    score: QcScore | None = None
    issues: tuple[ReviewIssue, ...] = ()
    #: 建议的返工动作（编排器据此产生正式返工指令）
    repair_hints: tuple[RepairHint, ...] = ()
    #: 本轮是第几次审查（用于限制返工轮数，防死循环）
    round_no: int = 1
    #: 审核方用的模型 id —— 用于校验"与生成方不同族"
    reviewer_model: str = ""
    #: 生成方用的模型 id
    generator_model: str = ""

    # ---- 派生判定 ------------------------------------------------------

    @property
    def blocking_issues(self) -> tuple[ReviewIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)

    @property
    def self_report_ok(self) -> bool:
        return self.score is None or self.score.self_report_trustworthy

    @property
    def cross_family_ok(self) -> bool:
        """审核方与生成方是否**不同模型族**。

        未声明模型时返回 True（不阻拦），但会由编排器记为"未验证"。
        族别判定取模型 id 的首段（"minimax-h3" -> "minimax"）。
        """
        if not self.reviewer_model or not self.generator_model:
            return True
        return _family(self.reviewer_model) != _family(self.generator_model)

    def effective_verdict(self) -> Verdict:
        """**权威结论**。综合三条规则得出，不直接采信 `verdict` 字段。

        1. 自报分不可信      -> ESCALATE（判定过程不可靠，需人看）
        2. 审核方与生成方同族 -> ESCALATE（自我签收，结论无效）
        3. 有阻断级问题      -> REVISE
        4. 否则              -> 采信原 verdict
        """
        if not self.self_report_ok:
            return Verdict.ESCALATE
        if not self.cross_family_ok:
            return Verdict.ESCALATE
        if self.blocking_issues and self.verdict is Verdict.PASS:
            # 自相矛盾：说有阻断问题却判通过 —— 以问题为准
            return Verdict.REVISE
        return self.verdict

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update({
            "verdict": self.verdict.value,
            "effectiveVerdict": self.effective_verdict().value,
            "score": self.score.as_dict() if self.score else None,
            "issues": [i.as_dict() for i in self.issues],
            "repairHints": [h.as_dict() for h in self.repair_hints],
            "roundNo": self.round_no,
            "reviewerModel": self.reviewer_model,
            "generatorModel": self.generator_model,
            "crossFamilyOk": self.cross_family_ok,
            "selfReportOk": self.self_report_ok,
            "blockingCount": len(self.blocking_issues),
        })
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *,
                  frm: str = "", to: str = "*") -> "ReviewFeedback":
        """从 dict 还原（`as_dict` 的逆操作）。

        ## 为什么必须有这个方法
        审核实现可能把 **dict**（而不是 `ReviewFeedback` 对象）写进黑板 ——
        `crew_llm` / `crew_stubs` 都支持那种数据形态，外部 agent 更是只会给 JSON。
        而 `CrewOrchestrator.review_artifact` 会用本方法还原。

        ⚠️ 2026-10-23 审计 C-2：**它此前从来不存在**。于是 dict 路径必然
        `AttributeError`；而紧随其后的 `else` 分支（「未产出可解析的审查意见」）
        恰好被 `isinstance(fb, Mapping)` 截走 —— 那条诊断永远打不出来。
        **死代码保护了不存在的实现。**

        宽容原则：单条意见/指令构造不出时**丢掉它并写进 note**（可审计的降级），
        而不是让整轮审查崩掉。
        """
        if isinstance(d, ReviewFeedback):
            return d
        issues: list[ReviewIssue] = []
        dropped_i = 0
        coerced_i = 0
        for r in (d.get("issues") or ()):
            if not isinstance(r, Mapping):
                continue
            got = _issue_from(r)
            if got is None:
                dropped_i += 1
                continue
            issues.append(got)
            if (str(r.get("severity") or "").lower() == "blocker"
                    and not str(r.get("suggestion") or "").strip()):
                coerced_i += 1
        hints: list[RepairHint] = []
        dropped_h = 0
        for r in (d.get("repairHints") or d.get("repair_hints") or ()):
            if not isinstance(r, Mapping):
                continue
            got = _hint_from(r)
            if got is None:
                dropped_h += 1
            else:
                hints.append(got)
        note = str(d.get("note") or "")
        if dropped_i or dropped_h or coerced_i:
            # **不可用必须报出来**：丢掉的条目必须留痕，否则就是"看起来正常"
            parts = []
            if dropped_i:
                parts.append(f"丢弃空意见 {dropped_i} 条")
            if dropped_h:
                parts.append(f"丢弃无法构造的返工指令 {dropped_h} 条")
            if coerced_i:
                parts.append(f"{coerced_i} 条 BLOCKER 缺建议（按 MAJOR 保留，仍阻断）")
            note = f"{note} [还原告警] " + " / ".join(parts)
            note = note.strip()
        kw = _msg_kwargs(d, frm=frm, to=to)
        kw["kind"] = MessageKind.REVIEW
        kw["note"] = note
        return cls(
            **kw,
            verdict=_enum_or(Verdict, d.get("verdict"), Verdict.PASS),
            score=_score_from(d.get("score")),
            issues=tuple(issues),
            repair_hints=tuple(hints),
            round_no=_int_or(d.get("roundNo") or d.get("round_no"), 1),
            reviewer_model=str(d.get("reviewerModel")
                               or d.get("reviewer_model") or ""),
            generator_model=str(d.get("generatorModel")
                                or d.get("generator_model") or ""),
        )


def _family(model_id: str) -> str:
    """取模型族。用于"跨模型族盲读"判定。

    "minimax-h3" -> "minimax"；"qwen3-vl-max" -> "qwen3"；
    "deepseek-flash" -> "deepseek"
    """
    if not model_id:
        return ""
    head = model_id.replace("_", "-").split("-")[0]
    # 去掉尾部纯数字版本号（qwen3 -> qwen）以便同族不同代互判为同族
    return head.rstrip("0123456789") or head


# ==========================================================================
# 剧本消息（第一种具体交付物）
# ==========================================================================

@dataclass
class ScriptDraft(Message):
    """剧本草稿/定稿。**结构化**，不是散文 —— 否则无法逐条定位审查意见。"""

    kind: MessageKind = MessageKind.DELIVERABLE
    episode_id: str = ""
    title: str = ""
    #: 场次列表。每项含 id / 场景 / 人物 / 对白 / 时长估计
    scenes: tuple[Mapping[str, Any], ...] = ()
    #: 本轮是第几稿
    revision: int = 1
    #: 字面总时长估计（秒）—— 用于对照"每镜 10 秒硬上限"做可执行性检查
    estimated_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update({
            "episodeId": self.episode_id,
            "title": self.title,
            "scenes": [dict(s) for s in self.scenes],
            "revision": self.revision,
            "estimatedSeconds": self.estimated_seconds,
        })
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], *,
                  frm: str = "", to: str = "*") -> "ScriptDraft":
        """从 dict 还原（`as_dict` 的逆操作）。

        与 `ReviewFeedback.from_dict` 同源（2026-10-23 审计 C-2）：
        `CrewOrchestrator.run_script_cycle` 会用它把黑板上的 dict 草稿还原成对象，
        而它此前不存在 —— 那条路径必然 `AttributeError`，且**没有 try/except**，
        于是整个 episode 带 traceback 崩掉，而不是返回 `script-invalid`。
        """
        if isinstance(d, ScriptDraft):
            return d
        kw = _msg_kwargs(d, frm=frm, to=to)
        kw["kind"] = MessageKind.DELIVERABLE
        return cls(
            **kw,
            episode_id=str(d.get("episodeId") or d.get("episode_id") or ""),
            title=str(d.get("title") or ""),
            scenes=tuple(dict(s) for s in (d.get("scenes") or ())
                         if isinstance(s, Mapping)),
            revision=_int_or(d.get("revision"), 1),
            estimated_seconds=_float_or(
                d.get("estimatedSeconds") if d.get("estimatedSeconds")
                is not None else d.get("estimated_seconds"), 0.0),
        )


#: 镜头动作 -> **白模渲染模板名**的映射。
#
#: ⚠️ 这张表是 `Shot.action` 字段的**合法取值定义**，所以它属于消息层，
#: 不属于执行层。
#
#: 踩过的坑：它原本定义在 `runtime/episode.py` 的 `EpisodeRunner.PREVIS_ACTIONS`。
#: 后来 `agent/storyboard_llm.py` 需要它来校验 LLM 的输出 ——
#: 而 `agent`(层 6) **不能** import `runtime`(层 8)，守卫立刻报反向依赖。
#: **正确的反应不是放宽守卫，而是把东西放到对的地方**：
#: 它定义的是 `Shot.action` 能取什么值，而 `Shot` 就在本模块。
#:
#: 为什么用映射而不是直接用动作名：白模的 8 个动作模板
#: （`build_character.ACTION_TEMPLATES`）比镜头的 `action` 字段粗，
#: 需要一个显式的对应关系，否则名字对不上就**静默退回站姿**。
PREVIS_ACTIONS: Mapping[str, str] = {
    "walk": "act_walk",
    "run": "act_run",
    "sit_down": "act_sit_down",
    "sit": "act_sit_down",
    "stand": "act_stand",
    "wave": "act_wave",
    "recoil": "act_recoil",
    "look_back": "act_look_back",
    "point_forward": "act_point_forward",
    "point": "act_point_forward",
}


@dataclass
class Shot(Message):
    """一个镜头。

    **时长硬上限**由平台常量约束（本机 H3 实测 10 秒）——
    分镜师若排出更长的镜头，下游必然降级或失败，
    因此在**消息层**就挡住，而不是等编译时才报错。
    """

    kind: MessageKind = MessageKind.DELIVERABLE
    shot_id: str = ""
    scene_id: str = ""
    #: 景别（与白模渲染的 shot_size 同名，便于直接驱动预演）
    shot_size: str = "medium"
    #: 相机运动模板名
    camera_movement: str = "static"
    #: 动作模板名
    action: str = "stand"
    duration_sec: float = 5.0
    #: 出场角色（资产 id）
    characters: tuple[str, ...] = ()
    #: 画面描述（驱动文生图）
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update({
            "shotId": self.shot_id,
            "sceneId": self.scene_id,
            "shotSize": self.shot_size,
            "cameraMovement": self.camera_movement,
            "action": self.action,
            "durationSec": self.duration_sec,
            "characters": list(self.characters),
            "description": self.description,
        })
        return d


@dataclass
class ShotList(Message):
    """镜头表。分镜师的交付物，也是下游生成与验收的输入。"""

    kind: MessageKind = MessageKind.DELIVERABLE
    episode_id: str = ""
    shots: tuple[Shot, ...] = ()
    #: 总时长（秒）
    total_seconds: float = 0.0

    @property
    def shot_count(self) -> int:
        return len(self.shots)

    def as_dict(self) -> dict[str, Any]:
        d = super().as_dict()
        d.update({
            "episodeId": self.episode_id,
            "shotCount": self.shot_count,
            "totalSeconds": self.total_seconds,
            "shots": [s.as_dict() for s in self.shots],
        })
        return d


#: 单镜时长硬上限（秒）。本机 H3 实测 12 秒必 OOM，10 秒是安全上限。
MAX_SHOT_SECONDS = 10.0

#: 单个镜头同框角色的上限。
#:
#: 与 `config/project.toml` 的 `max_characters_per_shot` 同源。
#: **为什么要有这个上限**：身份锁定能力（参考图 / LoRA）在角色越多时
#: 摊得越薄 —— 三个人的画面里，每张脸分到的条件强度只有一个人的三分之一。
#: 超限不是"不能生成"，而是"生成了也不像"。
MAX_CHARACTERS_PER_SHOT = 3


# ==========================================================================
# 校验
# ==========================================================================

def validate_message(msg: Mapping[str, Any]) -> list[str]:
    """校验一条已序列化的消息。返回问题列表（空 = 合法）。

    用于：(a) 收消息时把关；(b) 测试中校验协议的稳定性。
    """
    problems: list[str] = []

    kind = msg.get("kind")
    if kind not in {k.value for k in MessageKind}:
        problems.append(f"未知消息类型 {kind!r}（协议白名单外）")

    for required in ("msgId", "from", "to", "createdAt"):
        if not msg.get(required):
            problems.append(f"缺必填字段 {required!r}")

    if kind == MessageKind.REVIEW.value:
        issues = msg.get("issues") or []
        for i, issue in enumerate(issues):
            loc = issue.get("location") or {}
            if not any(loc.get(k) for k in ("scene", "shot", "line", "asset")):
                problems.append(f"第 {i} 条 problem 缺少结构化定位（location 全空）")
            if not issue.get("problem"):
                problems.append(f"第 {i} 条 problem 为空")
            if issue.get("severity") == Severity.BLOCKER.value and not issue.get("suggestion"):
                problems.append(f"第 {i} 条为 BLOCKER 但无 suggestion（不可执行）")

        score = msg.get("score")
        if score is not None:
            dims = score.get("dimensions") or {}
            for d in dims.values():
                if not (0 <= float(d) <= 100):
                    problems.append(f"维度分数 {d} 超出 0~100")

        for j, hint in enumerate(msg.get("repairHints") or []):
            if not hint.get("graphId"):
                problems.append(f"第 {j} 条 repairHint 缺 graphId")
            if hint.get("op") not in RepairHint.KNOWN_OPS:
                problems.append(f"第 {j} 条 repairHint 的操作 {hint.get('op')!r} 未登记")

    return problems
