"""预算账本：把 `Budget` 从"形状"变成"记账 + 中止"（2026-11-12）。

## 为什么需要它

`protocol.Budget` 早就有 `tokens / cny / wall_ms / steps` 与
`can_afford()` / `minus()`，但**编排器从来没有记账**：
`run_stage` 默认 `Budget.unlimited()`，既不扣减、也不因超限中止。
于是"预算只减不增"（R3）在代码里是**形状**，不是约束。

本模块补上"记账"这一半：每次职位调用按**实测耗时 + 步数**扣减，
把消耗累计成可审计的账本；超限时**拒绝发起调用**（`F-BUDGET`）。

## 三条诚实边界（写在这里，免得被当成魔法）

1 **wall_ms 只能事后记**：调用前不知道要跑多久，所以只能
   "开始前检查剩余 > 0，结束后按实测扣"。因此单次调用**可能超出**剩余额度 ——
   真正能**事前硬拦**的是 `steps`（每次 +1，调用前可知）。
2 **tokens / cny 只在提供方回报用量时才有**：本机模型多数不回
   `usage` 字段，此时记 0 并标 `usageReported: false`，
   **绝不拿"字符数 ÷ 4"之类的估算冒充真实账单**。
3 **默认不设限**：不传预算就是 `unlimited()`，行为与之前完全一致；
   要启用就在构造编排器时给一个真实预算（或用 `from_env()`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from little_crew.agent.protocol import Budget


@dataclass
class BudgetLedger:
    """预算账本：限额 + 已花 + 拒绝记录。

    线程安全：编排器里存在并发调用（剧本双审、分镜并发），
    因此所有读写都走一把锁 —— 否则"已花"会算少，超限就拦不住。
    """

    limits: Budget = field(default_factory=Budget.unlimited)
    spent_tokens: int = 0
    spent_cny: float = 0.0
    spent_wall_ms: int = 0
    spent_steps: int = 0
    calls: int = 0
    refused: int = 0
    #: 最近若干条拒绝原因（审计用，不无限增长）
    refusals: list[dict] = field(default_factory=list)
    #: 有几次调用提供了真实用量（否则 tokens/cny 记 0）
    usage_reported: int = 0
    _lock: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        import threading

        if self._lock is None:
            self._lock = threading.Lock()

    # ---- 构造 ----------------------------------------------------------

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BudgetLedger":
        """从环境变量读预算；一个都没设就是 unlimited。

        变量（都可选）：
          LITTLE_CREW_BUDGET_WALL_MS   整集墙钟预算（毫秒）
          LITTLE_CREW_BUDGET_STEPS     整集职位调用次数上限
          LITTLE_CREW_BUDGET_TOKENS    整集 token 上限（provider 回报时才有意义）
          LITTLE_CREW_BUDGET_CNY       整集花费上限（元）
        """
        e = dict(env if env is not None else os.environ)

        def _int(name: str) -> int:
            try:
                return max(0, int(str(e.get(name) or 0)))
            except (TypeError, ValueError):
                return 0

        def _float(name: str) -> float:
            try:
                return max(0.0, float(str(e.get(name) or 0)))
            except (TypeError, ValueError):
                return 0.0

        limits = Budget(tokens=_int("LITTLE_CREW_BUDGET_TOKENS"),
                        cny=_float("LITTLE_CREW_BUDGET_CNY"),
                        wall_ms=_int("LITTLE_CREW_BUDGET_WALL_MS"),
                        steps=_int("LITTLE_CREW_BUDGET_STEPS"))
        # ⚠️ `Budget.unlimited()` 不是全 0，而是一个**很大的数**。
        # 所以"没设任何预算" -> 四个值都是 0 -> 必须显式换成 unlimited()，
        # 否则会变成"预算 0：第一次调用就被拒"——那是最糟的默认行为。
        if not any((limits.tokens, limits.cny, limits.wall_ms, limits.steps)):
            return cls(limits=Budget.unlimited())
        return cls(limits=limits)

    # ---- 查询 ----------------------------------------------------------

    def remaining(self) -> Budget:
        with self._lock:
            lim = self.limits
            return Budget(
                tokens=max(0, lim.tokens - self.spent_tokens),
                cny=max(0.0, round(lim.cny - self.spent_cny, 6)),
                wall_ms=max(0, lim.wall_ms - self.spent_wall_ms),
                steps=max(0, lim.steps - self.spent_steps),
            )

    def unlimited(self) -> bool:
        return self.limits == Budget.unlimited()

    def allows(self, *, steps: int = 1, wall_ms: int = 0,
               tokens: int = 0, cny: float = 0.0) -> tuple[bool, str]:
        """调用**之前**能判定的部分：步数与（已知的）tokens/cny。

        wall_ms 只在"已经用光"时拒绝 —— 不能预测下一次要跑多久。
        """
        if self.unlimited():
            return True, ""
        with self._lock:
            lim = self.limits
            if lim.steps and self.spent_steps + steps > lim.steps:
                return False, (f"步数预算用尽（已用 {self.spent_steps}/{lim.steps}，"
                               f"本次需要 {steps}）")
            if lim.tokens and tokens and self.spent_tokens + tokens > lim.tokens:
                return False, (f"token 预算用尽（已用 {self.spent_tokens}/{lim.tokens}）")
            if lim.cny and cny and self.spent_cny + cny > lim.cny + 1e-9:
                return False, (f"花费预算用尽（已用 {self.spent_cny:.4f}/{lim.cny:.4f} 元）")
            if lim.wall_ms and self.spent_wall_ms >= lim.wall_ms:
                return False, (f"墙钟预算用尽（已用 {self.spent_wall_ms}/{lim.wall_ms} ms）")
        return True, ""

    # ---- 预占（并发安全） ----------------------------------------------

    def reserve(self, *, steps: int = 1, wall_ms: int = 0,
                tokens: int = 0, cny: float = 0.0) -> tuple[bool, str]:
        """**原子地**检查并预占步数。

        为什么必须有它：`allows()` 与 `charge()` 是两次加锁 ——
        中间那段窗口里，并发的第二个调用会看到"还有步数"然后一起冲进去。
        真机实测：预算 2 步，剧本双审并发时**用掉了 3 步**（"已用 3/2"）。
        所以判定与占用必须在**同一把锁**里完成。
        """
        if self.unlimited():
            with self._lock:
                self.spent_steps += max(0, int(steps))
            return True, ""
        with self._lock:
            lim = self.limits
            if lim.steps and self.spent_steps + steps > lim.steps:
                return False, (f"步数预算用尽（已用 {self.spent_steps}/{lim.steps}，"
                               f"本次需要 {steps}）")
            if lim.tokens and tokens and self.spent_tokens + tokens > lim.tokens:
                return False, f"token 预算用尽（已用 {self.spent_tokens}/{lim.tokens}）"
            if lim.cny and cny and self.spent_cny + cny > lim.cny + 1e-9:
                return False, f"花费预算用尽（已用 {self.spent_cny:.4f}/{lim.cny:.4f} 元）"
            if lim.wall_ms and self.spent_wall_ms >= lim.wall_ms:
                return False, f"墙钟预算用尽（已用 {self.spent_wall_ms}/{lim.wall_ms} ms）"
            self.spent_steps += max(0, int(steps))
            return True, ""

    def settle(self, *, wall_ms: int = 0, tokens: int = 0, cny: float = 0.0,
               usage_reported: bool = False) -> None:
        """调用结束后的结算（步数已在 `reserve()` 里占过，这里不再加）。"""
        with self._lock:
            self.spent_wall_ms += max(0, int(wall_ms))
            self.spent_tokens += max(0, int(tokens))
            self.spent_cny = round(self.spent_cny + max(0.0, float(cny)), 6)
            self.calls += 1
            if usage_reported:
                self.usage_reported += 1

    # ---- 记账 ----------------------------------------------------------

    def charge(self, *, wall_ms: int = 0, steps: int = 1,
               tokens: int = 0, cny: float = 0.0,
               usage_reported: bool = False) -> None:
        with self._lock:
            self.spent_wall_ms += max(0, int(wall_ms))
            self.spent_steps += max(0, int(steps))
            self.spent_tokens += max(0, int(tokens))
            self.spent_cny = round(self.spent_cny + max(0.0, float(cny)), 6)
            self.calls += 1
            if usage_reported:
                self.usage_reported += 1

    def refuse(self, reason: str, *, agent_id: str = "",
               capability: str = "") -> None:
        with self._lock:
            self.refused += 1
            if len(self.refusals) < 50:
                self.refusals.append({"agent": agent_id,
                                      "capability": capability,
                                      "reason": str(reason)[:200]})

    # ---- 呈现 ----------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            lim = self.limits
            return {
                "limits": {"tokens": lim.tokens, "cny": lim.cny,
                           "wall_ms": lim.wall_ms, "steps": lim.steps},
                "spent": {"tokens": self.spent_tokens, "cny": self.spent_cny,
                          "wall_ms": self.spent_wall_ms, "steps": self.spent_steps},
                "calls": self.calls,
                "refused": self.refused,
                "usageReported": self.usage_reported,
                "unlimited": self.unlimited(),
                "refusals": list(self.refusals),
            }
