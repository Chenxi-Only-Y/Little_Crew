"""剧组编排器 —— 多智能体协作闭环。

这一层是什么，不是什么
--------------------
**是**：把 18 个剧组职位按协作规则串成闭环的执行引擎。它负责
"谁先做、谁并行审、意见怎么合并、什么时候返工、什么时候收工"。

**不是**：不做图编排（那是 `compiler` 的事），不做生成（那是 `runtime` 的事），
不做模型决策（那是各职位自己的推理循环）。

一次完整协作循环
----------------
```
编剧出稿
   ↓
┌─ 剧本顾问  ┐   并行双维审查
└─ 文学策划  ┘   （不同视角，防止单一视角自证）
   ↓
编排器合并：最严裁定 + 加权总分 + 去重意见
   ↓
判断：PASS 就收工；REVISE 就生成返工指令回给编剧
   ↓
编剧修订 → 再审查（最多 max_rounds 轮，防止无限循环）
```

三条不可绕过的规则（这里强制）
----------------------------
| 规则 | 做法 |
|---|---|
| **服务端权威算分** | 总分由维度明细加权算出；`claimedScore` 只用于偏差检测 |
| **跨模型族盲读** | 生成方与审核方模型族相同时，**整个判定作废**为 ESCALATE |
| **不自我签收** | 按**产出归属**判定：某职位交付的产物不得由它自己审核 |

第三条的判据容易被写错 —— 早期版本按"是否具备审核能力"判定，
结果把**只审别人稿**的剧本顾问与文学策划误判为违规。
正确判据是产出归属：**审核者不得是该产物的产出者**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from little_crew.agent.messages import (
    DEFAULT_QC5_WEIGHTS,
    DIMENSION_LABELS,
    PHASE_QC5_WEIGHTS,
    SELF_REPORT_TOLERANCE,
    Dimension,
    IssueLocation,
    Message,
    MessageKind,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    ScriptDraft,
    Severity,
    Verdict,
    envelope,
    validate_message,
)
from little_crew.agent.protocol import (
    AgentManifest,
    Blackboard,
    Budget,
    CallResult,
    CapabilityRegistry,
    LayerRuntime,
)
from little_crew.agent.roster import AgentSpec, ExecutionKind, Stage, build_registry

__all__ = [
    "StageOutcome",
    "RoundResult",
    "CycleResult",
    "CrewOrchestrator",
    "merge_reviews",
    "check_no_self_signoff",
]


# ==========================================================================
# 结果类型
# ==========================================================================

@dataclass
class StageOutcome:
    """一个阶段的执行结果。"""

    stage: str
    agent_id: str
    ok: bool
    #: 写入黑板的能力键
    produced: tuple[str, ...] = ()
    message: Message | None = None
    error: str = ""
    #: 该阶段的耗时（秒），供成本核算
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "agent": self.agent_id,
            "ok": self.ok,
            "produced": list(self.produced),
            "error": self.error,
            "seconds": round(self.seconds, 3),
        }


@dataclass
class RoundResult:
    """一轮"出稿 → 审查 → 合并"的结果。"""

    round_index: int
    draft: ScriptDraft
    reviews: tuple[ReviewFeedback, ...] = ()
    #: 编排器合并后的最终裁定
    verdict: Verdict = Verdict.PASS
    #: 服务端权威总分
    total: float = 0.0
    #: 合并去重后的意见
    issues: tuple[ReviewIssue, ...] = ()
    #: 返工指令（verdict 为 REVISE 时非空）
    repair_hints: tuple[RepairHint, ...] = ()
    #: 各阶段的执行留痕
    outcomes: tuple[StageOutcome, ...] = ()
    #: 整体作废的原因（跨族失败 / 自报不可信 / 自签收）
    invalidated_reason: str = ""

    @property
    def accepted(self) -> bool:
        return self.verdict is Verdict.PASS and not self.invalidated_reason

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_index,
            "verdict": self.verdict.value,
            "total": round(self.total, 2),
            "issueCount": len(self.issues),
            "blockerCount": sum(1 for i in self.issues
                                if i.severity is Severity.BLOCKER),
            "repairHintCount": len(self.repair_hints),
            "reviewerCount": len(self.reviews),
            "invalidatedReason": self.invalidated_reason,
            "stages": [o.as_dict() for o in self.outcomes],
        }


@dataclass
class CycleResult:
    """一次完整协作循环（可能含多轮返工）。"""

    rounds: tuple[RoundResult, ...] = ()
    #: 最终接受的草稿
    final: ScriptDraft | None = None
    #: 是否在轮次上限内被接受
    accepted: bool = False
    #: 未接受时的说明
    reason: str = ""
        #: **失败分类**（2026-10-17 加）—— 让报告能区分"服务抽风"与"产物不行"。
    #:
    #: ## 为什么需要它
    #:
    #: 在此之前，未通过只有 `accepted=False` + 一句自由文本 `reason`。
    #: 于是报告上**这三种完全不同的情况长得一模一样**：
    #:
    #: | 实际情况 | 该怎么做 | 报告上的样子 |
    #: |---|---|---|
    #: | 评审调用失败（网络/限流） | **重派**，与产物无关 | 未通过 |
    #: | 判定作废（自报不自洽/同族） | 换审核方重审 | 未通过 |
    #: | 产物真的不够好 | **返工** / 升级人工 | 未通过 |
    #:
    #: 实测里 C 组 5 次有 2 次是**第一种**，但报告只写"未通过、0 分"——
    #: 读者会合理地认为"这个题材质量差"，而那完全是误读。
    #:
    #: ## 取值
    #:
    #: | 值 | 含义 | 建议动作 |
    #: |---|---|---|
    #: | `""` | 通过（无失败） | — |
    #: | `script-failed` | 编剧阶段执行失败 | 重试 |
    #: | `script-invalid` | 编剧产出格式非法 | 改提示词 |
    #: | `review-failed` | **评审调用失败** | **重派**（与产物无关） |
    #: | `review-invalid` | 判定作废（自报/同族） | 换族重审 |
    #: | `rejected` | 被明确驳回 | 人工 |
    #: | `escalated` | 已升级人工 | 人工 |
    #: | `blocked` | 仍有阻断级问题 | 返工 |
    #: | `below-threshold` | 分数不够（无阻断问题） | 返工/放宽 |
    failure_kind: str = ""

    #: 轮次用尽后是否应**升级人工**（2026-10-16 口径）。
    #:
    #: 非空时值为 `ESCALATE` 的字面量 —— 让调用方能区分
    #: **"还在改"**（只是没改完，可继续）与 **"改不动了"**（需要人看）。
    #: 用字符串而不是枚举，是为了让 `CycleResult` 的序列化保持简单。
    exhausted: str = ""

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    @property
    def best(self) -> "RoundResult | None":
        """**分数最高**的那一轮（可能不是最后一轮）。

        ## 为什么需要它

        10 次标定运行（`research/threshold-calibration/`）显示：
        多轮的运行里 **5 次改完变差、只有 3 次变好**。
        所以"最后一轮"经常是**最差的一份** ——
        把它交给人是浪费，人该看候选里最好的那一版。

        ## 与 `final` 的区别

        | 字段 | 含义 |
        |---|---|
        | `final` | **通过了的**定稿；没通过就是 `None` |
        | `best` | 无论通过与否，**分数最高**的那一轮 |

        未通过时 `final is None` 这条语义**必须保住** ——
        否则"有没有定稿"就再也分不出来（`E2` 那条测试守的就是它）。
        """
        return _best_round(self.rounds)

    @property
    def total_seconds(self) -> float:
        return sum(o.seconds for r in self.rounds for o in r.outcomes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds": len(self.rounds),
            "accepted": self.accepted,
            "reason": self.reason,
            "totalSeconds": round(self.total_seconds, 3),
            "detail": [r.as_dict() for r in self.rounds],
        }


# ==========================================================================
# 合并规则（纯函数，可独立测试）
# ==========================================================================

#: 裁定严重度排序 —— 合并时取**最严**。
#: 理由：两个审核者里有一个说"必须改"，那就必须改。
#: 取平均会让一个宽松的审核者抵消一个严格的审核者，而这正是
#: "单一视角自证"的另一种形式。
_VERDICT_RANK = {
    Verdict.PASS: 0,
    Verdict.REVISE: 1,
    Verdict.REJECT: 2,
    Verdict.ESCALATE: 3,
}


#: **什么算「阻断」** —— 只有 `BLOCKER` 级意见才让流程停下。
#:
#: ## 为什么需要这条定义（2026-10-16 补）
#:
#: 此前 `accepted` 等价于「**零意见**」：
#:
#:     def accepted(self): return self.verdict is Verdict.PASS
#:
#: 那对**确定性门禁**是对的（分辨率不对就该拦），但对**语义审核方**是不可能的：
#:
#: > **一个尽责的剧评人几乎永远不会说「完全没问题」。**
#: > 它总能指出"这里节奏可以更好"。
#:
#: 于是"有意见 → REVISE → 不 accepted"导致**任何一轮都过不了**，
#: 流水线在剧本阶段就停住（实测：`--llm` 跑一集永远 `分镜 0 个`）。
#:
#: ## 两类判断必须分开
#:
#: | 类型 | 例子 | 它说的是 | 有否决权吗 |
#: |---|---|---|---|
#: | **确定性门禁** | 分辨率、时长、角色数、格式 | 「**违规**」（客观、可复现） | ✅ **有** |
#: | **语义批评** | 剧评人、文字编辑 | 「**可以更好**」（主观、连续） | ❌ **无** |
#:
#: 用同一套"最严合并"处理两者，等于**用主观意见把客观流程堵死**。
#:
#: ## 不阻断 ≠ 丢弃
#:
#: `advisory` 意见**仍然进入 `issues` 与证据链** —— 只是不让流程停下。
#: 这与 §5.9.3 里「L2 一致性描述子只做 `audit`」的判据**同源**：
#: 测得出趋势、但定不出阈值的度量，不该用来阻断。
BLOCKING_SEVERITY: Severity = Severity.BLOCKER


#: **通过线** —— 权威总分（0~100）达到它，且无 `BLOCKER` 级意见，才算通过。
#:
#: ## 为什么单靠"无阻断意见"不够（有实测）
#:
#: 只要求"无 `BLOCKER`"时，实测一集跑了 3 轮：
#:
#: | 轮 | 权威总分 | 阻断问题数 |
#: |---|---|---|
#: | 1 | 74.3 | 4 |
#: | 2 | 77.8 | 3 |
#: | 3 | **81.1** | **2** |
#:
#: **明确在变好** —— 但"零阻断"没到达，所以永远不过。
#: **那串上升的分数就是被浪费掉的收敛信号。**
#:
#: 一个有能力的剧评人**总能找到下一条**真问题（实测里它指出的
#: "反转凭一句话抛出、作废了前面所有铺垫"确实是真缺陷）。
#: 所以"零阻断"和"零意见"一样，是个**达不到的标准**。
#:
#: ## 80 是怎么定的
#:
#: 落在实测数据的中间：**77.8 不过、81.1 过** ——
#: 即"改了 3 轮、分数确实上来了"应当给过。
#:
#: 这个数**不是精确刻度，是一条政策线**。换数据集应当重新标定，
#: 而不是当作普适常数。
#:
#: ## ⚠️⚠️ 已知局限：**总分本身有噪声，阈值对它是脆的**
#:
#: 两次跑**同一集、同样 3 轮**，分数不同：
#:
#: | 运行 | 第 1 轮 | 第 2 轮 | 第 3 轮 | 阈值 80 下 |
#: |---|---|---|---|---|
#: | A | 74.3 | 77.8 | **81.1** | 通过 |
#: | B | 74.3 | 70.7 | **77.8** | 不通过 |
#:
#: **第 3 轮在 77.8 ~ 81.1 摆动 —— 80 正好切在噪声带中间。**
#:
#: 根因要说准确（这里容易自我欺骗）：
#:
#: | 环节 | 可复现吗 |
#: |---|---|
#: | 从**给定**的问题列表算总分 | ✅ 纯函数 |
#: | **问题列表本身**（LLM 报的） | ❌ **每次不同** |
#: | 所以**总分** | ❌ **不可复现** |
#:
#: **"算术可复现"不等于"结果可复现"。**
#:
#: ## 65 是怎么定的 —— **10 次实测标定**（2026-10-17）
#:
#: 数据在 `research/threshold-calibration/`。要点：
#:
#: | 指标 | 首轮 | 末轮 | **取最好一轮** |
#: |---|---|---|---|
#: | 均值 | 61.5 | 63.4 | **70.3** |
#: | σ | 11.6 | 12.8 | **7.7** |
#: | p50 | 62.8 | 57.6 | **67.9** |
#:
#: **关键发现一：返工多数在让质量变差。**
#: 10 次里多轮的运行：**变好 3 次 / 变差 5 次 / 没动 1 次**。
#: 所以旧行为"取末轮"经常交出去的是**几次里最差的一份** ——
#: 已改为**取最好一轮**（`_best_round`）：均值 +7 分、波动降 40%。
#:
#: **关键发现二：80 这条线太严。** 旧阈值下收敛率只有 **3/10 = 30%**。
#: 按"取最好一轮"的分布：
#:
#: | 阈值 | 通过率 |
#: |---|---|
#: | 55 / 60 | 100% |
#: | **65** | **60%** ← 选它 |
#: | 70 | 40% |
#: | 75 / 80 | 30% |
#:
#: **为什么选 65**：它让约六成的运行自动通过、四成升级人工。
#: 那两个数字是**工作量分配**，不是质量指标 ——
#: 想让更多集的自动通过就往下调，想更严就往上调。
#:
#: ⚠️ **这不是一个"正确"的数，是一条可调政策线。** 换题材/换模型/
#: 换提示词都会让分布变化，届时应重新跑一次标定，而不是沿用 65。
#:
#: ⚠️ **样本量只有 10**：p50 这类分位数的置信区间很宽。
#: 这个数够用作"有依据的起点"，但**不够用作"精确刻度"**。
#:
#: ## ⚠️ 通过 ≠ 完美
#:
#: 两条判据是**与**的关系，不是或：
#:
#:   · 有 `BLOCKER` -> 不通过（**分数再高也不通过** —— 硬约束违规不能被高分掩盖）
#:   · 无 `BLOCKER` 但分数不够 -> 不通过
#:   · 两条都过 -> 通过，**但可能仍带着 advisory 意见**
#:
#: 所以证据链**必须原样保留所有意见**（`RoundResult.issues` 已带），
#: 否则会出现"通过了，但没人知道它其实带着 2 个已知缺陷"。
#: **这与项目的诚实原则一致：通过只是"达到了我们定的线"。**
#: **每种失败该做什么** —— 与 `CycleResult.failure_kind` 一一对应。
#:
#: ## 为什么这张表必须只有一份
#:
#: 它描述的是"**遇到这类失败该采取什么动作**"，属于**契约**。
#: 如果 CLI 一份、演示脚本一份，两处会漂 ——
#: 而漂的后果是"同一件事在不同入口给出不同建议"，比没有建议更糟。
#:
#: 内容刻意写成**可执行的话**（重跑 / 换族重审 / 返工），
#: 而不是"失败类型说明" —— 用户看到的是下一步，不是分类学。
FAILURE_ACTIONS: dict[str, str] = {
    "review-failed": "评审调用失败 —— 与产物无关，重跑即可",
    "review-invalid": "判定作废 —— 换一个模型族的审核方重审",
    "blocked": "仍有阻断级问题 —— 需要返工",
    "below-threshold": "没有硬伤但分数不够 —— 可返工或放宽阈值",
    "script-failed": "编剧阶段失败 —— 重试",
    "script-invalid": "编剧产出格式非法 —— 需要改提示词",
    "storyboard-failed": "分镜阶段失败 —— 需要改提示词",
    "render-failed": "出图失败 —— 检查本地生成服务（与剧本无关）",
    "rejected": "被明确驳回 —— 需要人工介入",
    "escalated": "已升级人工",
}


PASS_THRESHOLD: float = 65.0


def _best_round(rounds: Sequence["RoundResult"]) -> "RoundResult | None":
    """取**权威总分最高**的那一轮。

    为什么需要它（2026-10-17 实测）：10 次标定运行里，多轮的运行中
    **5 次改完变差、只有 3 次变好**。所以"最后一轮"经常是**最差的一份**，
    把它交给人工是浪费 —— 人应该看候选里最好的那一版。

    **不改判定**：它只影响"未通过时交哪份稿子"，不影响 `accepted`。
    """
    usable = [r for r in rounds
              if r.draft is not None and not r.invalidated_reason]
    if not usable:
        return None
    return max(usable, key=lambda r: r.total)

def _exhausted_kind(rounds: Sequence["RoundResult"]) -> str:
    """轮次用尽时的失败分类。**按优先级**取主因。

    | 优先级 | 判据 | 抛出 |
    |---|---|---|
    | 1 | 末轮被判作废 | `review-invalid` |
    | 2 | 末轮仍有 `BLOCKER` 级意见 | `blocked` |
    | 3 | 其余（无阻断、分数不够） | `below-threshold` |

    **为什么作废优先**：作废意味着"这一轮的结论不被采信"，
    而不是"产物不合格"。若把它归进 `below-threshold`，
    读者会以为"质量不够"，而实际是**审核链没给出可用判定**。
    """
    if not rounds:
        return "below-threshold"
    last = rounds[-1]
    if last.invalidated_reason:
        return "review-invalid"
    # ⚠️ 用 `issues`（**合并后的权威列表**），不是遍历各家的 `reviews`。
    # 两家审核者的意见已经按"取更严"合并过 —— 遍历原始 reviews 会漏掉
    # 合并规则做过的事，也可能把已被合并掉的重复算两遍。
    # （这个 bug 是 `orchestrator_check` H3 抓到的：夹具只填了 `issues`，
    #   而实现读的是 `reviews`，于是 BLOCKER 永远判不出来。）
    for iss in (last.issues or ()):
        if _SEV_RANK.get(iss.severity, 0) >= _SEV_RANK[BLOCKING_SEVERITY]:
            return "blocked"
    return "below-threshold"



def merge_reviews(
    reviews: Sequence[ReviewFeedback],
    *,
    phase: str = "draft",
    weights: Mapping[Dimension, float] | None = None,
    model_families: Mapping[str, str] | None = None,
) -> tuple[Verdict, float, tuple[ReviewIssue, ...], tuple[RepairHint, ...], str]:
    """合并多份审查意见。

    返回 ``(裁定, 权威总分, 去重意见, 返工指令, 作废原因)``。

    **作废优先于一切**：只要有一份审查不可信（跨族失败 / 自报不可信 /
    自签收），整轮判定作废为 ESCALATE —— 不是"扣几分"，而是
    **不采信这一轮的结论**。因为不可信的审查无法区分"真的没问题"
    与"审核者没看出来"。
    """
    if not reviews:
        return (Verdict.ESCALATE, 0.0, (), (), "没有任何审查意见 —— 无法裁定")

    # ---- 1) 可信性检查（一票否决） ----
    for r in reviews:
        if not r.self_report_ok:
            return (Verdict.ESCALATE, r.score.total if r.score else 0.0, (), (),
                    f"{r.frm} 的自报分与自身维度明细不自洽"
                    f"（偏差 {r.score.deviation if r.score else '?'}，"
                    f"容差 {SELF_REPORT_TOLERANCE}）"
                    f"—— 该职位的判定过程不可靠，整轮作废")
        if not r.cross_family_ok:
            return (Verdict.ESCALATE, r.score.total if r.score else 0.0, (), (),
                    f"{r.frm}（{r.reviewer_model or '?'}）与生成方 "
                    f"{r.generator_model or '?'} 同属模型族"
                    f"—— 同族审核等于自证，判定无效")

    # ---- 2) 权威总分：按维度加权，不信任自报 ----
    w = dict(weights or PHASE_QC5_WEIGHTS.get(phase, DEFAULT_QC5_WEIGHTS))
    merged_dim: dict[Dimension, float] = {}
    for r in reviews:
        if r.score is None:
            continue
        for dim, val in r.score.dimensions.items():
            # 同一维度多个审核者 → 取**较低**分（保守）
            merged_dim[dim] = min(merged_dim.get(dim, float(val)), float(val))

    if not merged_dim:
        return (Verdict.ESCALATE, 0.0, (), (),
                "审查意见未包含任何维度评分 —— 无法算分")

    total_w = sum(w.get(d, 0.0) for d in merged_dim) or 1.0
    total = sum(float(v) * w.get(d, 0.0) for d, v in merged_dim.items()) / total_w

    # ---- 3) 裁定 ----
    #
    # ⚠️ **从「取最严」改为「双判据」**（2026-10-16，口径两次收紧）。
    #
    # 演变过程（值得记，因为每一步都是被实测逼出来的）：
    #
    # | 版本 | 判据 | 实测结果 |
    # |---|---|---|
    # | 初版 | `verdict is PASS`（等价「零意见」） | 语义审核方一上就**永久堵死** |
    # | 二版 | 无 `BLOCKER` 级意见 | 仍不收敛：一个能干的剧评人**总能找到下一条** |
    # | 三版 | 无 `BLOCKER` **且** 总分 >= `PASS_THRESHOLD` | ← 本版 |
    #
    # 二版为什么不收敛（有实测数据）：一集跑了 3 轮，
    # 总分 **74.3 -> 77.8 -> 81.1**、阻断问题 **4 -> 3 -> 2** ——
    # **明确在变好**，但"零阻断"没有到达，所以永远不过。
    # **那串上升的分数就是被浪费掉的收敛信号。**
    #
    # 三版的判据：
    #   ① 有 `ESCALATE` / 作废  -> ESCALATE（判定不可信，最高优先）
    #   ② 有 `REJECT`           -> REJECT
    #   ③ 有 `BLOCKER` 级意见    -> REVISE
    #   ④ 总分 < `PASS_THRESHOLD` -> REVISE（分数不够）
    #   ⑤ 其余                   -> PASS（可能有 advisory 意见）
    #
    # ⚠️ **没有放松确定性门禁**：门禁报的问题本来就是 `BLOCKER`
    # （分辨率/时长/合规违规是客观事实），所以它们**照旧阻断**，
    # 而且**不受阈值影响** —— 分数再高也不能掩盖一条硬约束违规。
    blocking = tuple(
        iss for r in reviews for iss in r.issues
        if _SEV_RANK[iss.severity] >= _SEV_RANK[BLOCKING_SEVERITY]
    )
    hard = max((r.verdict for r in reviews), key=lambda v: _VERDICT_RANK[v])
    below = total < PASS_THRESHOLD
    if hard in (Verdict.ESCALATE, Verdict.REJECT):
        verdict = hard                      # 作废/驳回优先
    elif blocking:
        verdict = Verdict.REVISE            # 阻断级意见优先于分数
    elif below:
        verdict = Verdict.REVISE            # 没有阻断意见，但分数不够
    else:
        verdict = Verdict.PASS              # 两条都过 —— 可能有 advisory 意见

    # ---- 4) 意见去重（同维度同定位视为重复，保留更严的） ----
    seen: dict[tuple, ReviewIssue] = {}
    for r in reviews:
        for iss in r.issues:
            key = (iss.dimension, iss.location.scene, iss.location.shot,
                   iss.location.line, iss.location.asset)
            prev = seen.get(key)
            if prev is None or _SEV_RANK[iss.severity] > _SEV_RANK[prev.severity]:
                seen[key] = iss
    issues = tuple(sorted(seen.values(),
                          key=lambda i: (-_SEV_RANK[i.severity], i.dimension.value)))

    # ---- 5) 返工指令：裁定非 PASS 时汇总 ----
    hints: tuple[RepairHint, ...] = ()
    if verdict is not Verdict.PASS:
        collected: list[RepairHint] = []
        for r in reviews:
            collected.extend(r.repair_hints)
        # 按 (图, 操作, 目标) 去重
        uniq: dict[tuple, RepairHint] = {}
        for h in collected:
            uniq.setdefault((h.graph_id, h.op, str(sorted(h.args.items()))), h)
        hints = tuple(uniq.values())

        # **BLOCKER 必须给出可执行的返工指令** —— 否则编排器拿到
        # 一个"必须改但不知道怎么改"的结论，只能把这轮原样退回。
        blockers = [i for i in issues if i.severity is Severity.BLOCKER]
        if blockers and not hints:
            return (Verdict.ESCALATE, total, issues, (),
                    f"存在 {len(blockers)} 条阻塞级问题，但没有任何可执行的返工指令"
                    f"—— 编排器无法据此驱动修订，需人工介入")

    return (verdict, total, issues, hints, "")


def _placeholder_draft(agent_id: str) -> ScriptDraft:
    """构造**占位**草稿，仅用于「出稿阶段就失败」时的结果记录。

    不能直接 ``ScriptDraft()`` —— 消息基类强制要求 from/to，缺了就抛错。
    而失败路径恰恰最需要留下可追溯的记录。
    """
    return ScriptDraft(frm=agent_id, to="*",
                       note="（占位：出稿阶段未产出有效草稿）")


_SEV_RANK = {
    Severity.INFO: 0,
    Severity.MINOR: 1,
    Severity.MAJOR: 2,
    Severity.BLOCKER: 3,
}


def _family_label(reviewer: str, families: Mapping[str, str] | None) -> str:
    if families and reviewer in families:
        return families[reviewer]
    return "?"


def check_no_self_signoff(
    reviewer_id: str,
    artifact_owner_id: str,
    *,
    artifact_key: str = "",
) -> None:
    """校验"不自我签收"：**按产出归属**判定。

    ⚠️ 这条判据曾被写错。早期版本按"是否具备审核能力"判定，
    于是把**剧本顾问**与**文学策划**判为违规 —— 而那两位**只**审别人的稿，
    审核正是它们唯一的职责。错在把"有审核能力"当成了"自我签收"。

    正确判据是**产出归属**：某职位交付的产物，不得由它自己审核。
    """
    if reviewer_id == artifact_owner_id:
        raise PermissionError(
            f"违反「不自我签收」：{reviewer_id} 是 "
            f"{artifact_key or '该产物'} 的产出者，不得审核自己的产出。"
            f"审核者必须与产出者不同 —— 否则「审核」退化为「自证」。"
        )


# ==========================================================================
# 编排器
# ==========================================================================

#: 审查某产物时的**并行审核者**。
#:
#: 为什么是并行双维而不是单一审核者：单一视角会**自证** ——
#: 一个审核者倾向于认可与自己判断一致的稿子。两个不同部门的
#: 审核者（剧本顾问看戏剧结构、文学策划看风格一致性）关注点不同，
#: 合并时取最严，能显著降低漏检。
_REVIEWERS_BY_ARTIFACT: dict[str, tuple[str, ...]] = {
    "script.draft": ("dramaturge", "story_editor"),
    "shot.list": ("dramaturge",),
    "shot.image": ("continuity_supervisor", "review_board"),
}

#: 审查职位 -> 它把 ``ReviewFeedback`` 放在哪个黑板键下。
#:
#: **为什么需要这张表**：黑板键是职位自己声明的产物名（``produces``），
#: 不一定等于能力名。审片的能力是 ``review.board``，但产物键是
#: ``review.verdict`` —— 若按能力名去取就会拿到 None，
#: 表现为"审核者没有产出审查意见"，而它其实产出了。
_REVIEW_FEEDBACK_KEY: dict[str, str] = {
    "dramaturge": "script.critique",
    "story_editor": "style.guard",
    "continuity_supervisor": "continuity.report",
    "compliance_officer": "compliance.report",
    "pacing_editor": "pacing.plan",
    "review_board": "review.verdict",
}


class CrewOrchestrator:
    """把剧组职位串成协作闭环。

    与 LangGraph 一类框架的区别：**没有图**。编排逻辑就是本类的
    几个方法，而调用关系由 `LayerRuntime` 在运行时校验（R1–R4）。
    因此"编排"不是一个需要另外学习的概念 —— 它就是方法的调用顺序，
    且每次调用都留痕在 span 账本里。
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry | None = None,
        blackboard: Blackboard | None = None,
        max_rounds: int = 3,
        phase: str = "draft",
        model_families: Mapping[str, str] | None = None,
    ) -> None:
        self.registry = registry or build_registry()
        self.blackboard = blackboard or Blackboard()
        self.runtime = LayerRuntime(self.registry, self.blackboard)
        self.max_rounds = max(1, int(max_rounds))
        self.phase = phase
        #: 职位 -> 模型族（用于跨族盲读判定）
        self.model_families = dict(model_families or {})
        #: 已绑定的职位实现
        self._impls: dict[str, Callable[..., CallResult]] = {}
        #: 产物归属账本：能力键 -> 产出者 agent_id
        self._ownership: dict[str, str] = {}
        #: 最近一次审查产生的阶段留痕（供 run_script_cycle 汇总）
        self._last_outcomes: tuple[StageOutcome, ...] = ()

    # ---- 绑定实现 ------------------------------------------------------

    def bind(self, agent_id: str, impl: Callable[..., CallResult]) -> "CrewOrchestrator":
        """绑定某个职位的实现。

        **实现的调用契约**（由 ``LayerRuntime.call_agent`` 决定，不是本类的选择）::

            def impl(*, envelope: CallEnvelope, runtime: LayerRuntime) -> CallResult

        入参在 ``envelope.payload`` 里，**不是** ``payload=`` 关键字 ——
        这一点容易写错，而写错的后果是"参数静默为空的实现"：
        实现收不到 payload，却仍然成功返回一个默认结果。
        因此测试里专门有一条断言覆盖"返工 payload 确实传到了编剧"。

        未绑定的职位在用到时**明确失败**，而非静默跳过。
        """
        self.runtime.bind_agent(agent_id, impl)
        self._impls[agent_id] = impl
        return self

    def bind_many(self, impls: Mapping[str, Callable[..., CallResult]]
                  ) -> "CrewOrchestrator":
        for k, v in impls.items():
            self.bind(k, v)
        return self

    def missing_impls(self, agent_ids: Iterable[str]) -> list[str]:
        return [a for a in agent_ids if a not in self._impls]

    # ---- 产物归属 ------------------------------------------------------

    def owner_of(self, artifact_key: str) -> str | None:
        return self._ownership.get(artifact_key)

    def _record_ownership(self, key: str, agent_id: str) -> None:
        self._ownership[key] = agent_id

    # ---- 单职位调用 ----------------------------------------------------

    def run_stage(
        self, agent_id: str, capability: str, payload: Mapping[str, Any],
        *, budget: Budget | None = None,
    ) -> StageOutcome:
        """调用一个职位完成一个能力。**失败不抛异常，而是返回 ok=False** ——
        编排层需要看到失败并决策（重试/降级/中止），而不是被异常打断。"""
        import time

        if agent_id not in self._impls:
            return StageOutcome(stage=capability, agent_id=agent_id, ok=False,
                                error=f"{agent_id} 未绑定实现")
        spec = self._spec(agent_id)
        stage_name = spec.stage.value if spec else capability
        # 用该职位自己的 manifest 所允许的能力做校验
        if spec and capability not in spec.capabilities:
            return StageOutcome(
                stage=stage_name, agent_id=agent_id, ok=False,
                error=f"{agent_id}（{spec.crew_title}）不具备能力 {capability}；"
                      f"它具备 {sorted(spec.capabilities)}")

        t0 = time.perf_counter()
        try:
            result = self._invoke_as(agent_id, capability, payload, budget)
        except Exception as exc:                              # noqa: BLE001
            return StageOutcome(stage=stage_name, agent_id=agent_id, ok=False,
                                error=f"{type(exc).__name__}: {exc}",
                                seconds=time.perf_counter() - t0)

        dt = time.perf_counter() - t0
        if not result.ok:
            return StageOutcome(stage=stage_name, agent_id=agent_id, ok=False,
                                error=result.error_message or result.error_code
                                or "未说明的失败", seconds=dt)

        produced = tuple(spec.produces) if spec else ()
        payload_out = result.output if isinstance(result.output, Mapping) else {}
        # 该职位声明它"需要"的上游产物即为依赖 —— 接上 deps 后，
        # 上游变更会经黑板的失效传播标记下游为 stale，
        # 这正是"改一句只重跑受影响镜头"的机制基础。
        #
        # ⚠️ **必须解析成 `entry_id`，不能直接塞能力名**（2026-10-23 审计 C-3）。
        # `Blackboard._dependents_of` 比对的是 `e.entry_id`（`bb_000001`），
        # 而 `spec.requires` 是能力名（`shot.image`）——两个命名空间互不相交，
        # 于是 `"bb_000001" in ("shot.image",)` 恒为 False，
        # `_propagate_stale` **永远不标记任何条目**：失效传播彻底死掉。
        # 症状是"改了设定但画面没变"，而报告显示一切正常 —— 静默错误。
        deps = self._dependency_entry_ids(spec.requires if spec else ())
        # ⚠️ 别名只在「能力名不在产物名里」时登记。
        # 反例（会踩）：分镜师的能力就是 `shot.list`，而它同时产出
        # `shot.list` 与 `shot.breakdown` —— 若把能力名当别名，
        # 第二次写 `shot.breakdown` 会**顺着别名找回 `shot.list` 那条并覆盖它**，
        # 表现是 `blackboard.value("shot.list")` 变成了一个 dict。
        alias = (capability,) if capability not in produced else ()
        for key in produced:
            if key in payload_out:
                self.blackboard.put(key, payload_out[key], owner=agent_id,
                                    deps=deps, aliases=alias)
                self._record_ownership(key, agent_id)
                if alias:
                    self._record_ownership(capability, agent_id)
        return StageOutcome(stage=stage_name, agent_id=agent_id, ok=True,
                            produced=produced, seconds=dt)

    def _dependency_entry_ids(self, requires: Sequence[str]) -> tuple[str, ...]:
        """把「需要的能力名」解析成黑板上**已存在条目**的 `entry_id`。

        为什么查不到就跳过（而不是塞个占位）：依赖只能指向**已经存在**的条目。
        上游还没产出时，这条依赖与被记录的时刻无关 —— 与其存一个永远对不上的
        名字（那正是本 bug 的形态），不如不记，靠"上游先写、下游后写"的顺序保证。
        """
        out: list[str] = []
        for name in requires:
            e = self.blackboard.get(name)
            if e is None:
                continue
            if e.entry_id not in out:
                out.append(e.entry_id)
        return tuple(out)

    def _invoke_as(self, agent_id: str, capability: str,
                   payload: Mapping[str, Any], budget: Budget | None) -> CallResult:
        """以指定职位的身份发起调用（绕过能力解析，直接落到该职位）。"""
        from little_crew.agent.protocol import CallEnvelope

        env = CallEnvelope.root(agent_id, capability, payload,
                                budget or Budget.unlimited(), callee_kind="agent")
        return self.runtime.call_agent(env)

    def _spec(self, agent_id: str) -> AgentSpec | None:
        from little_crew.agent.roster import DEFAULT_ROSTER

        for a in DEFAULT_ROSTER:
            if a.agent_id == agent_id:
                return a
        return None

    # ---- 审查 ----------------------------------------------------------

    def review_artifact(
        self, artifact_key: str, *, generator_id: str,
        payload: Mapping[str, Any] | None = None,
        reviewers: Sequence[str] | None = None,
    ) -> tuple[tuple[ReviewFeedback, ...], str]:
        """对某产物发起**并行双维审查**。返回 (审查意见, 作废原因)。

        **不自我签收在这里强制**：审核者若正是产出者，直接判非法。
        """
        rids = tuple(reviewers or _REVIEWERS_BY_ARTIFACT.get(artifact_key, ()))
        if not rids:
            return (), f"产物 {artifact_key} 没有配置审核者"

        for rid in rids:
            try:
                check_no_self_signoff(rid, generator_id, artifact_key=artifact_key)
            except PermissionError as exc:
                return (), str(exc)

        reviews: list[ReviewFeedback] = []
        stage_outs: list[StageOutcome] = []
        for rid in rids:
            spec = self._spec(rid)
            if spec is None or rid not in self._impls:
                self._last_outcomes = tuple(stage_outs)
                return (), f"审核者 {rid} 未注册或未绑定实现"
            cap = next(iter(spec.capabilities), "")
            out = self.run_stage(rid, cap, dict(payload or {}))
            stage_outs.append(out)
            if not out.ok:
                self._last_outcomes = tuple(stage_outs)
                return (), f"审核者 {rid} 执行失败：{out.error}"
            # 黑板键是职位声明的**产物名**，不一定等于能力名：
            # 审片的能力是 review.board，产物键是 review.verdict。
            # 按能力名去取会拿到 None —— 表现为"它没产出意见"，而其实产出了。
            key = _REVIEW_FEEDBACK_KEY.get(rid) or f"{cap}.feedback"
            fb = self.blackboard.value(key)
            if fb is None:
                fb = self.blackboard.value(cap)
            if isinstance(fb, ReviewFeedback):
                reviews.append(fb)
            elif isinstance(fb, Mapping):
                # 送信方就是这位审核者 —— dict 里没写 `from` 时用它兜底
                reviews.append(ReviewFeedback.from_dict(fb, frm=rid))
            else:
                self._last_outcomes = tuple(stage_outs)
                return (), (f"审核者 {rid} 未产出可解析的审查意见"
                            f"（查键 {key!r}，得到 {type(fb).__name__}）")
        self._last_outcomes = tuple(stage_outs)
        return tuple(reviews), ""

    # ---- 完整循环 ------------------------------------------------------

    def run_script_cycle(
        self,
        source: Mapping[str, Any],
        *,
        screenwriter: str = "screenwriter",
        max_rounds: int | None = None,
    ) -> CycleResult:
        """跑完整的"出稿 → 并行审查 → 合并 → 返工 → 修订"闭环。

        ``source`` 是输入素材（例如 ``{"source.novel": "..."}``）。
        """
        budget = Budget.unlimited()
        rounds: list[RoundResult] = []
        limit = max_rounds or self.max_rounds
        draft: ScriptDraft | None = None

        for r in range(1, limit + 1):
            outcomes: list[StageOutcome] = []

            # ---- 1) 出稿 / 修订 ----
            cap = "script.revise" if (r > 1 and draft is not None) else "script.draft"
            payload: dict[str, Any] = dict(source)
            if r > 1 and draft is not None:
                prev = rounds[-1]
                payload["script.draft"] = draft
                payload["script.critique"] = {
                    "issues": [i.as_dict() if hasattr(i, "as_dict") else {
                        "severity": i.severity.value, "dimension": i.dimension.value,
                        "problem": i.problem, "suggestion": i.suggestion,
                    } for i in prev.issues],
                    "repairHints": [h.as_dict() for h in prev.repair_hints],
                }
            out = self.run_stage(screenwriter, cap, payload, budget=budget)
            outcomes.append(out)
            if not out.ok:
                rounds.append(RoundResult(round_index=r, draft=draft or _placeholder_draft(screenwriter),
                                          outcomes=tuple(outcomes),
                                          verdict=Verdict.ESCALATE,
                                          invalidated_reason=f"编剧阶段失败：{out.error}"))
                return CycleResult(rounds=tuple(rounds), final=None, accepted=False,
                                   failure_kind="script-failed",
                                   reason=f"第 {r} 轮编剧阶段失败：{out.error}")

            new_draft = self.blackboard.value("script.draft")
            if isinstance(new_draft, Mapping):
                # ⚠️ 这里原来没有 try/except，而 `from_dict` 曾经**不存在** ——
                # 于是整个 episode 带 traceback 崩掉，而不是返回
                # `script-invalid`（2026-10-23 审计 C-2）。
                # 现在 dict 能还原了；还原不出来仍然如实报 `script-invalid`。
                try:
                    new_draft = ScriptDraft.from_dict(new_draft, frm=screenwriter)
                except ValueError as exc:
                    new_draft = None
                    invalid_reason = f"草稿 dict 无法还原：{exc}"
                else:
                    invalid_reason = ""
            else:
                invalid_reason = ""
            if not isinstance(new_draft, ScriptDraft):
                rounds.append(RoundResult(
                    round_index=r, draft=draft or _placeholder_draft(screenwriter),
                    outcomes=tuple(outcomes), verdict=Verdict.ESCALATE,
                    invalidated_reason=(invalid_reason
                                        or "编剧未产出可解析的 ScriptDraft"
                                           f"（得到 {type(new_draft).__name__}）")))
                return CycleResult(rounds=tuple(rounds), final=None, accepted=False,
                                   failure_kind="script-invalid",
                                   reason="编剧产出格式非法")
            draft = new_draft

            # ---- 2) 并行双维审查 ----
            reviews, why = self.review_artifact(
                "script.draft", generator_id=screenwriter,
                payload={"script.draft": draft})
            outcomes.extend(self._last_outcomes)

            if why:
                rounds.append(RoundResult(
                    round_index=r, draft=draft, reviews=reviews,
                    outcomes=tuple(outcomes), verdict=Verdict.ESCALATE,
                    invalidated_reason=why))
                return CycleResult(rounds=tuple(rounds), final=None, accepted=False,
                                   failure_kind="review-failed",
                                   reason=why)

            # ---- 3) 合并 ----
            verdict, total, issues, hints, inval = merge_reviews(
                reviews, phase=self.phase, model_families=self.model_families)

            rr = RoundResult(round_index=r, draft=draft, reviews=reviews,
                             verdict=verdict, total=total, issues=issues,
                             repair_hints=hints, outcomes=tuple(outcomes),
                             invalidated_reason=inval)
            rounds.append(rr)

            # ---- 4) 收工判定 ----
            if inval:
                return CycleResult(rounds=tuple(rounds), final=None, accepted=False,
                                   failure_kind="review-invalid",
                                   reason=inval)
            if verdict is Verdict.PASS:
                self.blackboard.put("script.final", draft, owner=screenwriter)
                self._record_ownership("script.final", screenwriter)
                return CycleResult(rounds=tuple(rounds), final=draft, accepted=True,
                                   reason=f"第 {r} 轮通过（权威总分 {total:.1f}）")
            if verdict in (Verdict.REJECT, Verdict.ESCALATE):
                return CycleResult(rounds=tuple(rounds), final=None, accepted=False,
                                   failure_kind=(
                                       "rejected" if verdict is Verdict.REJECT
                                       else "escalated"),
                                   reason=f"第 {r} 轮裁定 {verdict.value}")
            # REVISE → 进入下一轮

        # ---- 轮次用尽仍未通过 -> **ESCALATE，不是 REVISE**（2026-10-16 口径）----
        #
        # 为什么升级人工而不是"再改一轮"：
        # **第 N 轮还不通过，说明这不是「再改一次就好」的问题。**
        # 继续耗轮次只会重复同样的失败，而每轮都要付费调模型。
        #
        # 这条也把 `ESCALATE` 的语义补齐了 —— 在此之前它只由
        # "判定不可信"（作废/同族自证）触发；现在**"改不动"也升级**。
        # 两者共同点：**继续自动处理不会有更好的结果**。
        last = rounds[-1] if rounds else None
        why = ("无有效轮次" if last is None
               else f"最后一轮权威总分 {last.total:.1f}"
                    f"（{last.verdict.value}）")
        # ⚠️ **返回「最好那一轮」的稿子，不是最后一轮**（2026-10-17 实测依据）
        #
        # 10 次标定运行（`research/threshold-calibration/`）显示：
        # 多轮里 **5 次分数变差、只有 3 次变好** —— 也就是说
        # **"末轮"经常是几次里最差的那一份**。
        #
        # 取最好一轮的实测收益：
        #   均值 63.4 -> **70.3**   σ 12.8 -> **7.7**（波动降 40%）
        #
        # 注意：`accepted` 仍为 False、`exhausted` 仍为 ESCALATE ——
        # **这不改变"没通过"这个事实**，只是把"交给人的那一份稿子"
        # 从末轮换成最好的一轮。人拿到的应该是候选里最好的，
        # 而不是"最后一次尝试"。
        best = _best_round(rounds)
        return CycleResult(
            rounds=tuple(rounds),
            # **轮次用尽时也要分类** —— 否则"服务抽风"与"改不好"混成一个"未通过"。
            # 判据按优先级：作废 > 阻断问题 > 分数不够。
            failure_kind=_exhausted_kind(rounds),
            # ⚠️ **`final` 仍为 `None`** —— 它的语义是"**通过了的**定稿"，
            # 未通过就不该有。想拿"最好的候选"用 `CycleResult.best`。
            final=None,
            accepted=False,
            #: 让调用方能区分"还在改"（REVISE）与"改不动了"（ESCALATE）
            exhausted=Verdict.ESCALATE.value,
            reason=f"达到轮次上限 {limit} 仍未通过，升级人工；{why}"
                   + (f"；最好的是第 {best.round_index} 轮"
                      f"（{best.total:.1f} 分）" if best is not None else ""),
        )

    # ---- 可观测性 ------------------------------------------------------

    def trace(self) -> list[str]:
        return self.runtime.trace_tree()

    def snapshot(self) -> dict[str, Any]:
        return {
            "blackboard": self.blackboard.snapshot(),
            "ownership": dict(self._ownership),
            "spans": len(self.runtime.spans()),
        }
