"""同一模型的调用串行化 —— 自检。

## 这条守的是一个真事故（2026-10-19）

接入 GLM（第三个模型族）后，剧本闭环稳定报：

```
审核者 story_editor 执行失败：[empty-output] 模型 story_editor 的审查意见为空
```

一开始以为是 `max_tokens` 不够（它确实是**推理模型**，reasoning 吃额度），
但把额度从 4096 提到 16384 后**仍然空**。

真正的原因是**并发**：

| 条件 | 结果 |
|---|---|
| 并发 2 个 | 253 字 / **0 字**（各 77~79 秒，明显在排队） |
| 串行 2 个 | 231 字 / 209 字（39.6s / 15.0s）**都正常** |

该端点**并发受限**，第二个请求返回**空内容** ——
而上层只能看到 `[empty-output]`，**极难归因到"并发"**。

而项目的「并行双维审查」正好是并发 2 个，所以这不是偶发，是必现。

## 修法

`llm._MODEL_LOCKS`：按 `model_id` 分锁，`chat_json` 在锁内调用。
**按模型分锁而不是一把全局锁** —— 否则 DeepSeek 与 MiMo 也会互相等，
而它们本来能并行。

## 守什么

| 断言 | 守什么 |
|---|---|
| L1 | 同一 `model_id` 拿到**同一把**锁 |
| L2 | 不同 `model_id` **不同锁**（否则所有模型被串成一条线） |
| **L3** | **并发调用同一模型时，实际不会同时进入**（这条才是实质） |
| L4 | `chat_json` 的源码里确实用了锁（防回退） |
"""
from __future__ import annotations

import inspect
import sys
import threading
import time

from little_crew.agent import llm as llm_mod
from little_crew.agent.llm import LlmClient, _lock_for

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _locks() -> None:
    print()
    print("A. 锁的粒度")
    check("L1 同一 model_id -> 同一把锁",
          _lock_for("glm-x") is _lock_for("glm-x"))
    check("L2 ★ 不同 model_id -> 不同锁（否则所有模型被串成一条线）",
          _lock_for("glm-x") is not _lock_for("mimo-y"))


def _serialization() -> None:
    print()
    print("B. 真的串行了吗")

    # 用真实的锁做一次并发实验：两个线程抢同一把锁，记录"同时进入"的次数
    lock = _lock_for("serialization-probe")
    inside = [0]
    max_inside = [0]
    guard = threading.Lock()

    def worker() -> None:
        with lock:
            with guard:
                inside[0] += 1
                max_inside[0] = max(max_inside[0], inside[0])
            time.sleep(0.05)
            with guard:
                inside[0] -= 1

    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("L3 ★★ 同一把锁下，**同时进入的线程数从不超过 1**",
          max_inside[0] == 1, f"最大同时进入 {max_inside[0]}")

    # 对照：不加锁时确实会同时进入（否则上一条的通过是假的）
    inside2 = [0]
    max2 = [0]

    def worker2() -> None:
        with guard:
            inside2[0] += 1
            max2[0] = max(max2[0], inside2[0])
        time.sleep(0.05)
        with guard:
            inside2[0] -= 1

    ts2 = [threading.Thread(target=worker2) for _ in range(4)]
    for t in ts2:
        t.start()
    for t in ts2:
        t.join()
    check("L4 ★ 对照：不加锁时**会**同时进入（证明 L3 不是空测）",
          max2[0] > 1, f"最大同时进入 {max2[0]}")


def _wired() -> None:
    print()
    print("C. 接线")
    src = inspect.getsource(LlmClient.chat_json)
    check("L5 ★ `chat_json` 在锁内调用（防回退到无锁并发）",
          "_lock_for(" in src and "with _lock_for" in src, src[:160])
    check("L6 锁按 self.model_id 取（不是一把全局锁）",
          "_lock_for(self.model_id)" in src)
    check("L7 模块导入了 threading",
          "import threading" in inspect.getsource(llm_mod))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("同一模型调用串行化 —— 自检（不联网）")
    print("=" * 74)

    _locks()
    _serialization()
    _wired()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
