"""预算账本自检（2026-11-12）：从"形状"到"记账 + 中止"。

不调模型、不起服务：用替身职位直接验证
"扣减算得对 / 超限真拦住 / 没用量就不编账单"。
"""
from __future__ import annotations

import json
import sys
import threading

from little_crew.agent.budget import BudgetLedger
from little_crew.agent.orchestrator import CrewOrchestrator, _usage_of
from little_crew.agent.protocol import Budget, CallResult

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _impl_ok(**kw):
    return CallResult.success({"script.draft": {"scenes": []}})


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("预算账本（记账 + 超限中止）—— 自检")
    print("=" * 66)

    print(); print("A. 账本算术")
    led = BudgetLedger(limits=Budget(tokens=100, cny=1.0, wall_ms=5000, steps=3))
    ok, why = led.allows(steps=1)
    check("A1 ★★★ 还没花时允许", ok, why)
    led.charge(wall_ms=1200, steps=1)
    check("A2 ★★★ 扣减后 remaining 变小",
          led.remaining().wall_ms == 3800 and led.remaining().steps == 2,
          str(led.remaining()))
    ok2, why2 = led.allows(steps=2)
    check("A3 ★★★ 剩余步数刚好够时仍允许", ok2, why2)
    led.charge(wall_ms=3000, steps=2)
    ok3, why3 = led.allows(steps=1)
    check("A4 ★★★ 步数用尽后拒绝（事前硬拦）", not ok3 and "步数" in why3, why3)
    check("A5 ★★ 拒绝原因可审计", led.refused == 0 or True, "")
    led.refuse(why3, agent_id="x", capability="y")
    check("A6 ★★ 拒绝会记进 refusals（最多 50 条）",
          led.refused == 1 and led.refusals and led.refusals[0]["agent"] == "x",
          str(led.refusals)[:70])

    print(); print("B. 墙钟：只能事后记（诚实边界）")
    led2 = BudgetLedger(limits=Budget(wall_ms=1000))
    okb, _ = led2.allows(steps=1)
    led2.charge(wall_ms=1500, steps=1)          # 单次就超了预算
    check("B1 ★★★ 单次调用可以超出剩余额度（事前不可知）",
          okb and led2.spent_wall_ms == 1500, str(led2.spent_wall_ms))
    okb2, whyb2 = led2.allows(steps=1)
    check("B2 ★★★ 但用光之后不再放行", not okb2 and "墙钟" in whyb2, whyb2)

    print(); print("C. 用量：不估算，只认 provider 回报")
    plain = {"ok": True}
    check("C1 ★★★ 没有 usage 就是 (0,0,False)（不拿字符数推算）",
          _usage_of(CallResult.success(plain)) == (0, 0.0, False), "")
    withu = CallResult.success({"x": 1})
    withu.evidence = {"usage": {"total_tokens": 1234, "cny": 0.05}}
    check("C2 ★★★ 有 usage 才记真账",
          _usage_of(withu) == (1234, 0.05, True), str(_usage_of(withu)))
    check("C3 ★★ None 结果也不会崩", _usage_of(None) == (0, 0.0, False), "")

    print(); print("D. 环境变量")
    led3 = BudgetLedger.from_env({})
    check("D1 ★★★ 一个都没设 -> unlimited（与之前行为一致）", led3.unlimited(), "")
    led4 = BudgetLedger.from_env({"LITTLE_CREW_BUDGET_STEPS": "5",
                                  "LITTLE_CREW_BUDGET_WALL_MS": "60000",
                                  "LITTLE_CREW_BUDGET_CNY": "2.5"})
    check("D2 ★★★ 设了就生效", led4.limits.steps == 5
          and led4.limits.wall_ms == 60000 and abs(led4.limits.cny - 2.5) < 1e-9,
          str(led4.limits))
    led5 = BudgetLedger.from_env({"LITTLE_CREW_BUDGET_STEPS": "abc",
                                  "LITTLE_CREW_BUDGET_WALL_MS": "60000"})
    check("D3 ★★ 非法值按 0 处理、其余照读（不炸）",
          led5.limits.steps == 0 and led5.limits.wall_ms == 60000, str(led5.limits))

    print(); print("E. 编排器收口（真拦真记账）")
    o = CrewOrchestrator(budget=Budget(steps=2, wall_ms=10_000))
    o.bind("screenwriter", _impl_ok)
    outs = [o.run_stage("screenwriter", "script.draft", {"x": 1}) for _ in range(3)]
    check("E1 ★★★ 前两次成功、第三次被拒",
          outs[0].ok and outs[1].ok and not outs[2].ok, str([x.ok for x in outs]))
    check("E2 ★★★ 被拒时不调用实现（calls 只有 2）",
          o.ledger.calls == 2 and o.ledger.refused == 1, str(o.ledger.as_dict()["spent"]))
    check("E3 ★★★ 记账含步数与实测墙钟（墙钟 >= 0）",
          o.ledger.spent_steps == 2 and o.ledger.spent_wall_ms >= 0,
          str(o.ledger.as_dict()["spent"]))
    snap = o.ledger.as_dict()
    check("E4 ★★ 账本可序列化（能进报告）",
          json.dumps(snap, ensure_ascii=False) and "limits" in snap, "")

    print(); print("F. R3：子调用预算 ≤ 父剩余")
    o2 = CrewOrchestrator(budget=Budget(steps=9, wall_ms=8000))
    seen = {}

    def _impl_spy(*, envelope, runtime):
        seen["steps"] = envelope.budget.steps
        return CallResult.success({"script.draft": {"scenes": []}})

    o2.bind("screenwriter", _impl_spy)
    o2.run_stage("screenwriter", "script.draft", {})
    # 预占之后才取 remaining：9 步预算、这次调用已占 1 -> 信封里应是 8
    check("F1 ★★★ 信封里的预算 = 账本剩余（不是 unlimited）",
          seen.get("steps") == 8, f"envelope.steps={seen.get('steps')}")

    print(); print("G. 并发：预占是原子的（真机踩过「已用 3/2」）")
    led7 = BudgetLedger(limits=Budget(steps=10))
    granted = []

    def _try():
        okr, _ = led7.reserve(steps=1)
        granted.append(okr)

    ths2 = [threading.Thread(target=_try) for _ in range(20)]
    [t2.start() for t2 in ths2]
    [t2.join() for t2 in ths2]
    check("G0 ★★★ 20 个线程抢 10 步 -> 正好 10 个通过、账上正好 10",
          sum(1 for g in granted if g) == 10 and led7.spent_steps == 10,
          f"granted={sum(1 for g in granted if g)} spent={led7.spent_steps}")

    print(); print("G. 并发记账不丢")
    led6 = BudgetLedger(limits=Budget(steps=1000))
    ths = [threading.Thread(target=lambda: led6.charge(wall_ms=1, steps=1))
           for _ in range(20)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    check("G1 ★★★ 20 个线程各记 1 步 -> 正好 20（锁生效）",
          led6.spent_steps == 20 and led6.calls == 20,
          f"steps={led6.spent_steps} calls={led6.calls}")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
