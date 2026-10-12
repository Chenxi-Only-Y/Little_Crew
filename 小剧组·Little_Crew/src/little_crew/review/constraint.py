"""约束与断言：C3「确定性验收门禁」的**落地表示**。

设计要点（对应策划书 §3.3）
---------------------------
IR 的约束边不编译成执行图节点，而是编译成**可执行断言**：

```
IR 的 constraint 边
    {"ref": "cr_face_007",
     "assertion": {"op": "identity_similarity", "characterRef": "CHAR-0007",
                   "min": 0.82},
     "enforcement": "hard"}
        ↓ 本模块
ConstraintAssertion(
    constraint_ref="cr_face_007", op="identity_similarity",
    expected={...}, observed={...}, passed=False,
    enforcement=HARD, evidence={...})
```

**为什么断言必须是"数据"而不是"代码"**：
  · 断言要进入审片报告、交付证据链、论文数据表 —— 都得可序列化
  · 断言失败要能**自动生成返工指令**，"差多少"必须能算出来
  · 同一断言要能在不重跑生成的前提下**反复复核**（换阈值、换算子实现）

**"确定性"的确切含义**（本模块的硬性要求）：

    同一 (op, expected, observed) -> 必然同一 passed，且必然同一 evidence

不满足这条的算子**不允许注册** —— 因为门禁一旦有随机性，
"拦截率"这个指标就没有意义了（重跑一次结论就变）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "Enforcement",
    "AssertionOutcome",
    "ConstraintAssertion",
    "AssertionOperator",
    "OperatorRegistry",
    "REGISTRY",
    "register",
    "UnknownOperatorError",
    "AssertionError_",
]


class UnknownOperatorError(KeyError):
    """断言引用了未注册的算子。

    **这是一个必须显式失败的情形**，不能"跳过未知算子" ——
    跳过等于**静默放行**：约束声称被检查了，实际没有。
    那正是 C3 要消灭的"自我签收"的变体。
    """


class AssertionError_(Exception):
    """断言**结构本身**非法（与"断言未通过"是两件事）。

    不通过 = 产物不合格（正常业务结果）
    结构非法 = 约束写错了（配置错误，必须立刻暴露）

    混为一谈的后果：把"约束写错"当成"产物不合格"，
    于是永远在返工产物，而真正的问题在约束定义上。
    """


class Enforcement(str, Enum):
    """约束的强制级别。"""

    #: 不通过则**阻断**交付（硬门禁）
    HARD = "hard"
    #: 不通过记录在案但不阻断（软门禁）
    SOFT = "soft"
    #: 只观测不判定 —— 用于**冷启动期**收集数据，阈值尚未标定
    AUDIT = "audit"


@dataclass(frozen=True)
class AssertionOutcome:
    """一个算子的执行结果。

    `observed` 与 `evidence` 的区别很重要：
      · `observed`  是**判定所依据的值**（如相似度 0.71）
      · `evidence`  是**复核时可重算的原始材料**（如两图的哈希、直方图）

    只留 `observed` 的话，阈值改了就没法回算；
    只留 `evidence` 的话，每次看报告都要重算一遍。
    **两者都要。**
    """

    passed: bool
    observed: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    #: 失败时的人类可读说明（会进返工指令）
    detail: str = ""
    #: **失败的原因是"判定不可信"，而不是"产物不合格"。**
    #:
    #: ⚠️ 这个区分是门禁三态铁律的落地点，漏了它后果很严重：
    #:
    #: | | 含义 | 该走哪条路 |
    #: |---|---|---|
    #: | `inconclusive=False` | 产物**真的不合格** | `RETRY` —— 给返工指令 |
    #: | `inconclusive=True`  | **这次判定不算数** | `ESCALATE` —— **不给返工指令** |
    #:
    #: 反例（实测踩过）：L3 盲读因"审核方与生成方同族"而作废时，
    #: 门禁照常判了 `RETRY` 并生成了返工指令 —— 那条指令是
    #: **基于一份作废的观察**写的，等于让施工方照着一张不可信的照片返工。
    #: 那比不返工更糟：白烧算力，而且下次判定依然不可信。
    #:
    #: ⚠️ **"缺观测"不算 `inconclusive`。**
    #: 缺观测是"**能力不在**"（本机没配 VLM），该走能力的预检跳过路径；
    #: `inconclusive` 是"**能力在，但这次判定不可信**"（同族审核、
    #: 未知裁定），该升级人工。两者混为一谈会让"没配模型"也去升级人工 ——
    #: 那正是 `ObservationPlan` 的文档里警告过的"门禁等于没有"。
    inconclusive: bool = False


@dataclass(frozen=True)
class ConstraintAssertion:
    """一条约束的断言结果 —— 对应设计文档的 `ConstraintAssertion` 表。"""

    constraint_ref: str
    op: str
    enforcement: Enforcement
    passed: bool
    expected: Mapping[str, Any]
    observed: Mapping[str, Any]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""
    #: 失败的原因是**判定不可信**，而不是产物不合格。
    #:
    #: 与 `AssertionOutcome.inconclusive` 是同一个概念，在这里是
    #: **透传**：门禁据此把它算进 `escalations` 而不是 `repairs`，
    #: 于是这一镜走 `ESCALATE`（升级人工）而非 `RETRY`（返工）。
    #:
    #: 报告里也要留这个字段 —— 事后必须能看出"当初是判它不合格，
    #: 还是判这次判定不算数"。两者的后续动作完全不同。
    inconclusive: bool = False

    @property
    def blocking(self) -> bool:
        """是否应当**阻断**交付。

        ⚠️ `inconclusive` 的断言**也算阻断** —— 它虽然不代表
        "产物有问题"，但代表"这次判定不算数"，同样**不能就这么交付**。
        区别在于**走哪条路**（升级人工 vs 返工），不在于拦不拦。
        """
        if self.inconclusive:
            return True
        return self.enforcement is Enforcement.HARD and not self.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "constraintRef": self.constraint_ref,
            "op": self.op,
            "enforcement": self.enforcement.value,
            "passed": self.passed,
            "blocking": self.blocking,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
            "evidence": dict(self.evidence),
            "detail": self.detail,
            # 进证据：事后要能看出"当初是判它不合格，还是判这次判定不算数"
            "inconclusive": self.inconclusive,
        }


# ---------------------------------------------------------------------------
# 算子注册表
# ---------------------------------------------------------------------------

#: 算子 = (name, version, 判定函数)
#:
#: `version` 存在的理由：算子实现变了，**已发出的报告要能说明它当时用哪一版判的**。
#: 否则"同一份产物上次通过这次不通过"会被当成随机性，而其实是算子换了。
AssertionOperator = Any  # Callable[[Mapping[str, Any], Mapping[str, Any]], AssertionOutcome]


class OperatorRegistry:
    """确定性断言算子的注册表。

    **注册即承诺确定性。** 注册表本身不做校验（无法自动证明确定性），
    但每个算子的 docstring 必须写明它依赖什么 ——
    凡是依赖时间、随机、外部状态的算子都不能注册。
    """

    def __init__(self) -> None:
        self._ops: dict[str, tuple[str, AssertionOperator]] = {}

    def register(self, name: str, fn: AssertionOperator, *,
                 version: str = "1") -> None:
        if name in self._ops:
            raise AssertionError_(
                f"算子 {name!r} 已注册（现有版本 {self._ops[name][0]}）。"
                f"重复注册会让「到底哪个实现在生效」变得不确定")
        self._ops[name] = (version, fn)

    def replace(self, name: str, fn: AssertionOperator, *,
                version: str) -> None:
        """**显式**替换算子实现（版本必须变）。

        单独提供 `replace` 而不是允许 `register` 覆盖：
        覆盖是静默的，而"门禁的判定逻辑被静默换掉"是最不该发生的事。
        """
        if name not in self._ops:
            raise AssertionError_(f"算子 {name!r} 未注册，无法替换")
        self._ops[name] = (version, fn)

    def get(self, name: str) -> tuple[str, AssertionOperator]:
        if name not in self._ops:
            raise UnknownOperatorError(
                f"未知断言算子 {name!r}。已注册：{sorted(self._ops)}")
        return self._ops[name]

    def version_of(self, name: str) -> str:
        return self.get(name)[0]

    def names(self) -> list[str]:
        return sorted(self._ops)

    def describe(self) -> list[dict[str, str]]:
        """算子清单（给 UI 与文档用）。"""
        out = []
        for name in self.names():
            ver, fn = self._ops[name]
            doc = (fn.__doc__ or "").strip().splitlines()
            out.append({
                "name": name,
                "version": ver,
                "summary": doc[0] if doc else "",
            })
        return out


#: 全局注册表。`ops.py` 在 import 时把内置算子登记进来。
REGISTRY = OperatorRegistry()


def register(name: str, *, version: str = "1"):
    """装饰器形式的注册。"""
    def deco(fn: AssertionOperator) -> AssertionOperator:
        REGISTRY.register(name, fn, version=version)
        return fn
    return deco
