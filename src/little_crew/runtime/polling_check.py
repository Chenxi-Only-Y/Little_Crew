"""**轮询必须节流** —— 结构断言。

## 这条测试守的是一个真事故（2026-10-18）

异步渲染路径**从来没跑通过**。表现是"随机失败"，报错长这样：

```
URLError: <urlopen error [WinError 10048] 通常每个套接字地址
（协议/网络地址/端口）只允许使用一次。>
```

本来会以为是"服务端不稳"。实际量出来的数字是：

| 观测 | 值 |
|---|---|
| 130 秒内向 local-gen 发的连接数 | **5725**（≈44/秒） |
| Windows 临时端口池 | **16384** |
| `TIME_WAIT` 累积 | **21427** ← 超过端口池 |

**根因在 `RenderSession.render` 自己**：

```python
while ...:
    h = self.channel.poll(h)      # 紧循环，没有 sleep
```

`poll_interval_s` 存进了 `self`，**却从来没用过** —— 于是每个 mock 上的
"1 秒轮询"实际是"能多快就多快"，把本机的临时端口打光了。

> **这类 bug 的特征**：报错指向网络、指向服务端，**而凶手是自己**。
> 而且它只影响**异步**后端（`async_task=True`）—— 同步路径一次请求就结束，
> 永远碰不到。所以单元测试全绿、演示脚本走直连 urllib，谁都没发现。

## 守什么

| 断言 | 守什么 |
|---|---|
| P1 | `poll` 之前**必须有 `sleep`**（源码顺序检查） |
| P2 | 用的是 `self.poll_interval_s`，不是硬编码数字 |
| P3 | `poll_interval_s` 默认值合理（≥ 0.2s） |
"""
from __future__ import annotations

import inspect
import sys

from little_crew.runtime import render as render_mod
from little_crew.runtime.render import RenderSession

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _throttle() -> None:
    print()
    print("A. 轮询节流")
    # ⚠️ 轮询循环在 **`render_http`** 里，不是 `render` ——
    # `render` 与 `render_http` 共用提交/幂等/回收逻辑，
    # 而**循环体在 `_await` 这一个地方**。
    # （第一版我是查 `render` 的，于是 `poll@-1`；
    #   **测试自己先要指对地方** —— 这是查代码，不是猜。）
    src = ""
    where = ""
    # 实测定位：轮询循环在 **`_await`** 里（`render` 与 `render_http`
    # 都把它委托出去）。这个顺序是先"找"再"写死"，不是猜的。
    for name in ("_await", "render_http", "render", "_poll_until_done"):
        fn = getattr(RenderSession, name, None)
        if fn is None:
            continue
        try:
            s = inspect.getsource(fn)
        except Exception:                                     # noqa: BLE001
            continue
        if "self.channel.poll(" in s:
            src, where = s, name
            break
    check("P0 找到含 `poll` 的方法（测试自己要指对地方）",
          bool(src), "在 render_http / render 里都没找到 self.channel.poll(")
    if not src:
        return
    print(f"       （轮询循环在 `{where}` 里）")
    i_poll = src.find("self.channel.poll(")
    i_sleep = src.rfind("time.sleep(", 0, i_poll) if i_poll > 0 else -1

    check("P1 ★★ `poll` 之前有 `sleep`（否则会打光临时端口）",
          i_poll > 0 and 0 < i_sleep < i_poll,
          f"poll@{i_poll} sleep@{i_sleep}")
    check("P2 ★ 用的是 `self.poll_interval_s`（不是硬编码数字，可配置）",
          0 <= i_sleep and "self.poll_interval_s" in src[i_sleep:i_sleep + 60],
          (src[i_sleep:i_sleep + 60] if i_sleep >= 0 else "没有 sleep"))

    # 默认值必须够大 —— 1.0s 意味着 340 秒的任务只轮询 ~340 次。
    # 若默认是 0.01，即使"有 sleep"也照样打光端口。
    import dataclasses
    try:
        fields = {f.name: f for f in dataclasses.fields(RenderSession)}
        default = getattr(fields["poll_interval_s"], "default", None)
    except Exception:                                        # noqa: BLE001
        default = None
    if default is None or default is dataclasses.MISSING:
        # 不是 dataclass 字段就退而查签名默认值
        sig = inspect.signature(RenderSession.__init__)
        default = sig.parameters["poll_interval_s"].default
    check("P3 ★ 默认轮询间隔 ≥ 0.2 秒",
          isinstance(default, (int, float)) and float(default) >= 0.2,
          f"默认 {default!r}")

    # 文档里要写清"为什么必须节流"，否则后来人会把 sleep 当冗余删掉
    doc = inspect.getdoc(RenderSession.render) or ""
    src_has_note = "10048" in src or "端口" in src or "TIME_WAIT" in src
    check("P4 源码里写明了原因（防止后人把 sleep 当冗余删掉）",
          src_has_note, "没找到关于端口/10048 的注释")


def _module_imports() -> None:
    print()
    print("B. 依赖")
    src = inspect.getsource(render_mod)
    check("P5 模块导入了 time（sleep 可用）",
          "import time" in src, "没找到 import time")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("轮询节流 —— 结构自检（不联网）")
    print("=" * 74)

    _throttle()
    _module_imports()

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
