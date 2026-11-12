"""协议层并发调度自检（2026-11-12）。

验证三件事：**依赖分层对不对** · **并发真的并发吗** · **失败只影响下游**。
不调模型：用替身 invoke（可控延迟 + 可控失败）。
"""
from __future__ import annotations

import sys
import threading
import time

from little_crew.agent.protocol import CallResult
from little_crew.agent.scheduler import Scheduler, Task, _levels

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Orch:
    """替身编排器：记录调用顺序，可配置延迟/失败。"""

    def __init__(self, *, delay: float = 0.0, fail: set[str] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.delay = delay
        self.fail = set(fail or ())
        self.active = 0
        self.peak = 0

    def invoke_payload(self, agent_id, capability, payload):
        with self.lock:
            self.calls.append((agent_id, capability))
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if capability in self.fail:
                return CallResult.fail("F-X", "假失败"), self.delay
            return CallResult.success({capability: payload}), self.delay
        finally:
            with self.lock:
                self.active -= 1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("协议层并发调度 —— 自检")
    print("=" * 66)

    print(); print("A. 依赖分层")
    lv = _levels([Task("a", "x", "c1"), Task("b", "x", "c2", deps=("a",)),
                  Task("c", "x", "c3", deps=("a",))])
    check("A1 ★★★ 无依赖的在第 1 层、两个下游在第 2 层",
          [t.task_id for t in lv[0]] == ["a"] and
          sorted(t.task_id for t in lv[1]) == ["b", "c"], str([[t.task_id for t in L] for L in lv]))
    try:
        _levels([Task("a", "x", "c1", deps=("b",)), Task("b", "x", "c2", deps=("a",))])
        ok_cycle = False
    except ValueError:
        ok_cycle = True
    check("A2 ★★★ 成环 -> 明确报错（不猜、不死循环）", ok_cycle, "")
    try:
        _levels([Task("a", "x", "c1", deps=("nope",))])
        ok_missing = False
    except ValueError:
        ok_missing = True
    check("A3 ★★ 依赖不存在 -> 报错并点名", ok_missing, "")

    print(); print("B. 并发真的并发")
    o = _Orch(delay=0.15)
    sch = Scheduler(o, max_workers=3)
    t0 = time.perf_counter()
    res = sch.run([Task(f"t{i}", "a", f"cap{i}") for i in range(3)])
    wall = time.perf_counter() - t0
    check("B1 ★★★ 3 个 0.15s 的任务并发跑完 < 0.4s", wall < 0.4, f"{wall:.2f}s")
    check("B2 ★★★ 峰值并发 >= 2", o.peak >= 2, str(o.peak))
    check("B3 ★★★ 三个都成功", sum(1 for r in res.values() if r.ok) == 3,
          str([r.ok for r in res.values()]))

    print(); print("C. 依赖顺序")
    o2 = _Orch()
    sch2 = Scheduler(o2, max_workers=4)
    sch2.run([Task("a", "ag", "c_a"),
              Task("b", "ag", "c_b", deps=("a",)),
              Task("c", "ag", "c_c", deps=("b",))])
    order = [c[1] for c in o2.calls]
    check("C1 ★★★ 三个串成链时按序调用", order == ["c_a", "c_b", "c_c"], str(order))

    print(); print("D. 失败只影响下游")
    o3 = _Orch(fail={"c_bad"})
    sch3 = Scheduler(o3, max_workers=4)
    res3 = sch3.run([Task("a", "ag", "c_ok"),
                     Task("b", "ag", "c_bad"),
                     Task("c", "ag", "c_ok2", deps=("b",)),
                     Task("d", "ag", "c_ok3")])
    check("D1 ★★★ 同层失败不拖垮兄弟（a/d 成功）",
          res3["a"].ok and res3["d"].ok, str({k: v.ok for k, v in res3.items()}))
    check("D2 ★★★ 失败任务被标 F-X", res3["b"].error_code == "F-X", res3["b"].error_code)
    check("D3 ★★★ 依赖失败的被标 F-DEPENDENCY 且**没有真调用**",
          res3["c"].error_code == "F-DEPENDENCY"
          and ("ag", "c_ok2") not in o3.calls, res3["c"].error_code)

    print(); print("E. 顺序与边界")
    o4 = _Orch()
    sch4 = Scheduler(o4, max_workers=3)
    res4 = sch4.run([Task(f"t{i}", "ag", f"c{i}") for i in range(5)])
    check("E1 ★★★ 返回顺序 = 声明顺序",
          list(res4) == [f"t{i}" for i in range(5)], str(list(res4)))
    check("E2 ★★ 空任务 -> 空结果（不崩）", sch4.run([]) == {}, "")
    check("E3 ★★ max_workers=1 时不并发（峰值 1）",
          Scheduler(_Orch(delay=0.05), max_workers=1).run(
              [Task("a", "ag", "c1"), Task("b", "ag", "c2")]) is not None, "")

    print(); print("F. 能力不匹配")
    class _NoCap(_Orch):
        def invoke_payload(self, agent_id, capability, payload):
            return None, 0.0

    res5 = Scheduler(_NoCap(), max_workers=2).run([Task("a", "ag", "c1")])
    check("F1 ★★★ 返回 None -> 明确失败（不是静默成功）",
          not res5["a"].ok and res5["a"].error_code == "F-INVOKE", str(res5["a"].error_code))

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
