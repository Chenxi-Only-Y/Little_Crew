"""门禁：把 IR 的约束编译成断言并执行（C3 的落地）。

在流水线里的位置
----------------
```
IR 的 constraint 边（不编译成执行图节点）
    ↓  Gate.evaluate(constraints, observations)
ConstraintAssertion 列表
    ↓  Gate.verdict(...)
GateVerdict  KEEP / RETRY / ESCALATE
```

**为什么门禁要独立于渲染**（策划书 §3.3 的硬性约束）
--------------------------------------------------
`review` 包**不做生成**，`runtime` 包**不做判定**。

从结构上杜绝"既当运动员又当裁判"：
  · 判定逻辑写在 `review/`，它拿不到通道、也没有提交作业的能力
  · `runtime/` 能提交作业，但它不决定产物合不合格
  · 两边只有**数据**往来（约束 + 观测值 -> 断言）

这条靠**依赖方向**强制（`review` 与 `runtime` 同层，互不 import），
不靠文档约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from little_crew.review.constraint import (
    REGISTRY,
    AssertionError_,
    ConstraintAssertion,
    Enforcement,
)

__all__ = [
    "GateVerdict",
    "GateResult",
    "Gate",
    "Observation",
]


class GateVerdict(str, Enum):
    """门禁结论 —— 三态，不是二态。

    **必须三态的理由**：`RETRY`（产物不合格，返工）与 `ESCALATE`
    （**判定不可信**，要人来看）是两件完全不同的事。
    合并成一个"失败"的后果：
      · 判定不可信时也去返工 -> 白烧算力，而且下次还是不可信
      · 产物不合格时去叫人 -> 人被迫做机器能做的事
    """

    #: 全部硬门禁通过 —— 可以交付
    KEEP = "KEEP"
    #: 有硬门禁不通过 —— 带返工指令回去重做
    RETRY = "RETRY"
    #: **判定本身不可信**（算子缺失、观测值拿不到、约束自相矛盾）—— 升级人工
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class Observation:
    """一个产物的**观测值集合**。

    `values` 是算子要的原始观测（如 `{"similarity": 0.71}`）。
    `source` 说明这些值从哪来 —— 必须给，
    因为"这个数字是怎么得到的"决定了断言结论可不可信。
    """

    artifact_id: str
    values: Mapping[str, Any] = field(default_factory=dict)
    #: 观测来源（如 "comfy_history" / "ffprobe" / "embedder:v1"）
    source: str = "unspecified"

    def as_dict(self) -> dict[str, Any]:
        return {"artifactId": self.artifact_id,
                "values": dict(self.values),
                "source": self.source}


@dataclass(frozen=True)
class GateResult:
    """一次门禁执行的完整结果。"""

    artifact_id: str
    verdict: GateVerdict
    assertions: tuple[ConstraintAssertion, ...] = ()
    #: 返工指令（面向 `RETRY`）—— **给人看的句子**
    repairs: tuple[str, ...] = ()
    #: 返工动作 —— **给机器执行的编辑**。
    #:
    #: 与 `repairs` 是同一件事的两种表达：
    #:   · `repairs`        人类可读，进报告
    #:   · `repair_actions` 结构化，进返工循环（见 `review/repair.py`）
    #:
    #: **为什么两者都要**：只有句子则循环无法执行；只有动作则报告读不懂。
    #: 而"报告读不懂"会让返工决定失去依据 —— 人无法判断该不该接受它。
    repair_actions: tuple[Any, ...] = ()
    #: 升级原因（面向 `ESCALATE`）
    escalations: tuple[str, ...] = ()
    #: 判定不可信但是否仍记录断言（审计用）
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict is GateVerdict.KEEP

    @property
    def repairable(self) -> bool:
        """是否存在**能真正改变 IR** 的动作。

        全是不可应用的动作时不该重跑 —— 那只会拿到同一张图
        （甚至直接命中缓存），是"空转的循环"。
        """
        from little_crew.review.repair import is_repairable

        return is_repairable(self.repair_actions)

    @property
    def blocking(self) -> tuple[ConstraintAssertion, ...]:
        return tuple(a for a in self.assertions if a.blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "verdict": self.verdict.value,
            "assertions": [a.as_dict() for a in self.assertions],
            "repairs": list(self.repairs),
            "repairActions": [
                a.as_dict() if hasattr(a, "as_dict") else str(a)
                for a in self.repair_actions
            ],
            "repairable": self.repairable,
            "escalations": list(self.escalations),
            "notes": list(self.notes),
            "counts": {
                "total": len(self.assertions),
                "passed": sum(1 for a in self.assertions if a.passed),
                "blocking": len(self.blocking),
                "hard": sum(1 for a in self.assertions
                            if a.enforcement is Enforcement.HARD),
            },
        }


class Gate:
    """确定性验收门禁。

    **无状态**：同一个 `Gate` 实例可以对任意多个产物反复求值，
    结果只取决于入参（约束 + 观测）。
    这正是"确定性"在类层面的体现 —— 有状态的门禁会因调用顺序改变结论。
    """

    def __init__(self, *, strict_unknown_ops: bool = True) -> None:
        #: 遇到未注册算子时的行为。
        #:
        #: `True`（默认）-> ESCALATE。**这是刻意的**：
        #: 未知算子意味着"这条约束我们其实没检查"，
        #: 而**静默放行**正是 C3 要消灭的自我签收。
        #: `False` 只用于"约束先行、算子后补"的过渡期，
        #: 且届时会在 notes 里留痕。
        self.strict_unknown_ops = strict_unknown_ops

    # ---- 单条断言 --------------------------------------------------------

    def evaluate_one(
        self,
        constraint: Mapping[str, Any],
        observed: Mapping[str, Any],
    ) -> tuple[ConstraintAssertion | None, str]:
        """求值一条约束。

        返回 `(断言 或 None, 升级原因)`。
        `None` 表示**无法求值**（算子缺失/约束结构非法），此时第二个返回值非空。
        """
        ref = str(constraint.get("ref") or "")
        if not ref:
            raise AssertionError_("约束缺少 ref —— 没有 ref 就无法追溯，"
                                  "报告里会出现一条不知道在说谁的断言")

        assertion = constraint.get("assertion") or {}
        if not isinstance(assertion, Mapping):
            raise AssertionError_(f"{ref}: assertion 必须是对象，收到 "
                                  f"{type(assertion).__name__}")

        op_name = str(assertion.get("op") or "")
        if not op_name:
            raise AssertionError_(f"{ref}: assertion 缺少 op —— "
                                  f"约束必须说明「要检查什么」")

        enforcement = Enforcement(
            str(constraint.get("enforcement") or Enforcement.HARD.value))

        # 算子不存在 -> 无法判定。
        #
        # ⚠️ **仍然产出一条断言记录，只是 `passed=False`。**
        # 早期版本这里 `return None`，于是该约束在报告里**彻底消失** ——
        # 一份"4 条约束"的报告只列出 3 条，而缺的那条恰是**没被检查**的那条。
        # 那正是 C3 要消灭的模式：约束声称被检查了，实际没有，
        # 而且从报告上**看不出来**。
        try:
            version, fn = REGISTRY.get(op_name)
        except KeyError:
            msg = (f"{ref}: 未知算子 {op_name!r}（已注册："
                   f"{', '.join(REGISTRY.names())}）—— **该约束未被检查**")
            if self.strict_unknown_ops:
                return ConstraintAssertion(
                    constraint_ref=ref, op=op_name, enforcement=enforcement,
                    passed=False, expected=dict(assertion), observed={},
                    evidence={"evaluated": False, "reason": "unknown_operator"},
                    detail=f"⚠️ 无法判定：{msg}",
                ), msg
            return ConstraintAssertion(
                constraint_ref=ref, op=op_name, enforcement=enforcement,
                passed=True, expected=dict(assertion), observed={},
                evidence={"evaluated": False, "reason": "unknown_operator",
                          "permissiveMode": True},
                detail=f"⚠️ 算子缺失，**未检查**即放行（过渡期模式）：{msg}",
            ), ""

        # 执行算子。算子**结构错**（约束写错）不该被当成"产物不合格"
        try:
            outcome = fn(assertion, observed)
        except AssertionError_ as e:
            # 约束本身非法 -> 判定不可信 -> 升级（不是返工产物）
            return ConstraintAssertion(
                constraint_ref=ref, op=op_name, enforcement=enforcement,
                passed=False, expected=dict(assertion), observed={},
                evidence={"evaluated": False, "reason": "invalid_constraint"},
                detail=f"⚠️ 约束结构非法，无法判定：{e}",
            ), f"{ref}: 约束结构非法 —— {e}"

        # 注意：算子的 `inconclusive`（"判定不可信"）**不在这里处理** ——
        # 它是**整批**的性质，要等所有约束都算完才知道（见 `evaluate`）。
        # 放在这里会因为看不到其他断言而误判。
        return ConstraintAssertion(
            constraint_ref=ref,
            op=op_name,
            enforcement=enforcement,
            passed=outcome.passed,
            expected=dict(assertion),
            observed=dict(outcome.observed),
            evidence={**dict(outcome.evidence),
                      "evaluated": True,
                      "operatorVersion": version,
                      "observedSource": observed.get("__source__", "")},
            detail=outcome.detail,
            # 透传"判定不可信"标记 —— `evaluate` 据此把它算进
            # `escalations`（升级人工）而不是 `repairs`（返工）
            inconclusive=bool(getattr(outcome, "inconclusive", False)),
        ), ""

    # ---- 整批 ------------------------------------------------------------

    def evaluate(
        self,
        constraints: Iterable[Mapping[str, Any]],
        observation: Observation,
    ) -> GateResult:
        """对一个产物的全部约束求值，并给出结论。"""
        observed = dict(observation.values)
        # 让算子能看到观测来源（写进 evidence，便于追溯）
        observed.setdefault("__source__", observation.source)

        assertions: list[ConstraintAssertion] = []
        escalations: list[str] = []
        notes: list[str] = []

        for c in constraints:
            a, esc = self.evaluate_one(c, observed)
            if a is not None:
                assertions.append(a)
            if esc:
                escalations.append(esc)

        # 结论裁定。**顺序即优先级**：
        #   ① 判定不可信 -> ESCALATE（连结论都不该信，更不该去返工）
        #   ② 有硬门禁不通过 -> RETRY
        #   ③ 否则 -> KEEP
        #
        # ⚠️ ①必须排在②前面。反过来会导致"算子都缺了还去返工产物"——
        # 白烧算力，而且下次判定依然不可信。
        #
        # ① 的 `escalations` 有三个来源，都必须进：
        #   · 未知算子（约束引用了一个没注册的算子）
        #   · 约束结构非法（少写 `min` 之类）
        #   · **算子自己判定"这次判定不算数"**（`inconclusive`）——
        #     L3 盲读因同族审核作废就走这条
        for a in assertions:
            if a.inconclusive:
                escalations.append(
                    f"{a.constraint_ref}: **判定不可信** —— "
                    f"{a.detail or a.op}")

        if escalations:
            verdict = GateVerdict.ESCALATE
        elif any(a.blocking for a in assertions):
            verdict = GateVerdict.RETRY
        else:
            verdict = GateVerdict.KEEP

        soft_failed = [a for a in assertions
                       if not a.passed and a.enforcement is Enforcement.SOFT]
        audit_failed = [a for a in assertions
                        if not a.passed and a.enforcement is Enforcement.AUDIT]
        if soft_failed:
            notes.append(
                f"软门禁不通过 {len(soft_failed)} 条（不阻断，已记录）："
                + "；".join(f"{a.constraint_ref}" for a in soft_failed))
        if audit_failed:
            notes.append(
                f"审计级不通过 {len(audit_failed)} 条（仅观测，用于阈值标定）："
                + "；".join(f"{a.constraint_ref}({a.op})" for a in audit_failed))

        if verdict is GateVerdict.RETRY:
            repairs = self.repairs_for(assertions)
            # 同一件事的**机器可执行**表达（见 `review/repair.py`）。
            # 语句给人，动作给循环 —— 缺任一个，返工闭环都不成立：
            #   · 只有句子 -> 循环没法执行
            #   · 只有动作 -> 报告读不懂，人无法判断该不该接受
            from little_crew.review.repair import plan_repairs

            actions = plan_repairs(assertions)
        else:
            repairs = ()
            actions = ()

        return GateResult(
            artifact_id=observation.artifact_id,
            verdict=verdict,
            assertions=tuple(assertions),
            repairs=repairs,
            repair_actions=actions,
            escalations=tuple(escalations),
            notes=tuple(notes),
        )

    # ---- 返工指令 --------------------------------------------------------

    @staticmethod
    def repairs_for(assertions: Sequence[ConstraintAssertion]) -> tuple[str, ...]:
        """由失败的硬断言生成**可执行**的返工指令。

        每条指令必须满足三条（否则就是废纸）：
          ① **说清哪个约束**（可追溯）
          ② **说清差多少**（可判断"改到什么程度算够"）
          ③ **说清往哪改**（算子的 `op` 决定了改哪一环）

        第 ③ 条是这里唯一有"知识"的地方 —— 从算子名映射到返工动作。
        映射表**显式列出**而不是猜：猜错会让人改错地方，
        那比不给指令更糟。
        """
        #: op -> (返工动作, 该动作作用的对象)
        ACTION: Mapping[str, str] = {
            "identity_similarity":
                "调高身份锁定强度：① 检查参考图/锚点图是否用了正确角色；"
                "② 提高 IPAdapter 权重或 LoRA 强度；③ 在提示词中强化外观描述",
            "pairwise_similarity":
                "修正跨镜漂移：把**对照镜**的锚点图与外观片段作为本镜的输入，"
                "不要只依赖提示词里的一致性描述",
            "token_diff":
                "修正文本：以剧本原文为准重写台词/字幕，不要意译",
            "count_at_least":
                "补齐缺失项：检查上游是否漏产（分镜表条数、产物清单条数）",
            "count_at_most":
                "削减超限项：检查提示词是否引入了额外主体，或分镜表本身超限",
            "duration_within":
                "调整时长：裁剪或补足到目标区间；若镜头本身规划有误，"
                "改分镜表而不是硬拉时长",
            "resolution_matches":
                "改回目标分辨率：检查编译出的 latent 尺寸与采样器参数",
            "aspect_ratio_within":
                "改回目标宽高比：同上",
            "byte_size_within":
                "检查编码：文件过小通常是 ffmpeg 编码失败或输入帧缺失",
            "not_empty":
                "上游未产出内容：检查该节点是否被跳过（可选能力降级）",
        }

        out: list[str] = []
        for a in assertions:
            if not a.blocking:
                continue
            action = ACTION.get(a.op, "（无预设返工动作）按约束语义修正该维度")
            diff = a.detail or "（算子未给出差异说明）"
            out.append(
                f"[{a.constraint_ref}] {diff}。返工方向：{action}")
        return tuple(out)
