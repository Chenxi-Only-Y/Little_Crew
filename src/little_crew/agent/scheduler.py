"""协议层并发调度器（2026-11-12）。

## 为什么需要它

以前"并发"是**各处手写**的：
`storyboard_stage.per_scene` 自己 `ThreadPoolExecutor`，`_review_artifact_parallel`
又自己一份。三处代码各自处理"顺序 / 预算 / 失败"，于是：
  · 预算在并发下超支（真机踩过：2 步预算用掉 3 步）
  · 结果顺序靠"列表推导恰好有序"，不是**保证**
  · 一处失败怎么影响下游，各写各的

本模块把"并发"变成**协议能力**：任务带依赖 -> 分成层 -> 层内并发 ->
结果按原顺序返回；失败只影响依赖它的任务。

## 三条语义（都是可测的）

1 **依赖分层**：同一层内互不依赖才并发；`deps` 未完成的层不会开跑。
2 **失败隔离**：某任务失败，只有**依赖它的**任务被标 `F-DEPENDENCY`（跳过），
   同层其余任务照常完成 —— 并发不该把局部失败放大成整体失败。
3 **预算收口**：任务一律走 `orch.invoke_payload()`（内部已 `reserve()` 预占），
   本模块**不自己记账** —— 预算只有一处真相，否则一定对不上。

## 不做什么

不写黑板。并发写同一个键会互相覆盖（2026-11-11 真机踩过）；
需要进黑板的产物，由调用方在**主线程**统一补写。

## 接线现状（2026-11-12）

| 并发点 | 用了吗 | 说明 |
|---|---|---|
| 剧本双审（剧评人 + 文风编辑） | ✅ | `_review_artifact_parallel` 改走本调度器；结果顺序与串行一致（有测试） |
| 逐场分镜（`LITTLE_CREW_SB_PARALLEL`） | ❌ 保留手写 | 默认关闭且**实测更慢**（33.5s 串行 vs 95.5s 4 线程，本地 GPU 独占）；<br>它还需要"每场独立重试"的语义，换成调度器没有收益 |
| 逐镜渲染 | ❌ | 本地 GPU 独占，并行只会排队 |

所以"协议层并发调度"当前的落点是**审核并行**这一处 —— 它也正是全链路里
唯一"互不依赖且不吃同一块 GPU"的并发点。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from little_crew.agent.protocol import CallResult


@dataclass
class Task:
    """一个可并发的调用单元。`deps` 里的任务完成后它才开跑。"""

    task_id: str
    agent_id: str
    capability: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    deps: tuple = ()

    def __post_init__(self) -> None:
        self.task_id = str(self.task_id)
        self.deps = tuple(str(d) for d in (self.deps or ()))


def _levels(tasks: Sequence[Task]) -> list[list[Task]]:
    """按依赖分层；有环直接抛错（**不猜**）。"""
    by_id = {t.task_id: t for t in tasks}
    if len(by_id) != len(tasks):
        raise ValueError("task_id 重复")
    done: set[str] = set()
    remaining = list(tasks)
    levels: list[list[Task]] = []
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.deps)]
        if not ready:
            missing = sorted({d for t in remaining for d in t.deps if d not in by_id})
            if missing:
                raise ValueError(f"依赖不存在：{missing}")
            raise ValueError("依赖成环，无法分层")
        levels.append(ready)
        done.update(t.task_id for t in ready)
        remaining = [t for t in remaining if t.task_id not in done]
    return levels


@dataclass
class Scheduler:
    """依赖分层的并发调度器。"""

    orchestrator: Any
    max_workers: int = 4
    #: 供测试注入（默认走 `orch.invoke_payload`）
    invoke: Callable[..., Any] | None = None

    def run(self, tasks: Iterable[Task]) -> dict[str, CallResult]:
        tasks = list(tasks)
        if not tasks:
            return {}
        levels = _levels(tasks)
        results: dict[str, CallResult] = {}
        workers = max(1, min(int(self.max_workers), len(tasks)))
        for level in levels:
            ready = [t for t in level if self._deps_ok(t, results)]
            for t in level:
                if t not in ready:
                    results[t.task_id] = CallResult.fail(
                        "F-DEPENDENCY",
                        f"依赖未成功：{[d for d in t.deps if not self._ok(results, d)]}")
            if not ready:
                continue
            if len(ready) == 1 or workers == 1:
                for t in ready:
                    results[t.task_id] = self._one(t)
                continue
            with ThreadPoolExecutor(max_workers=min(workers, len(ready))) as ex:
                outs = list(ex.map(self._one, ready))
            for t, r in zip(ready, outs):
                results[t.task_id] = r
        # 返回顺序 = 任务声明顺序（并发不改变可读性）
        return {t.task_id: results[t.task_id] for t in tasks if t.task_id in results}

    # ---- 内部 ----------------------------------------------------------

    def _one(self, t: Task) -> CallResult:
        t0 = time.perf_counter()
        try:
            if self.invoke is not None:
                out = self.invoke(t.agent_id, t.capability, dict(t.payload))
            else:
                out = self.orchestrator.invoke_payload(
                    t.agent_id, t.capability, dict(t.payload))
            res, _secs = out if isinstance(out, tuple) else (out, None)
            if res is None:
                return CallResult.fail("F-INVOKE", f"{t.agent_id} 没有该能力 {t.capability}")
            return res
        except Exception as exc:                              # noqa: BLE001
            return CallResult.fail("F-INVOKE", f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _ok(results: Mapping[str, CallResult], tid: str) -> bool:
        r = results.get(tid)
        return bool(r is not None and getattr(r, "ok", False))

    def _deps_ok(self, t: Task, results: Mapping[str, CallResult]) -> bool:
        return all(self._ok(results, d) for d in t.deps)
