"""后端用途守卫：**代码强制**的 `allowed_agents`（2026-11-13）。

## 为什么必须有它

接第三方图像 API 时，最容易出的不是技术故障，而是**误用**：
主链路渲染一旦被路由到云端生图 ——

- 一集 40 镜的成本直接失控（云端按张计费）；
- 画面一致性从"本地同一底模"变成"每次远端漂移"；
- 而且**看起来一切正常**（图出来了、还挺好看），只是账单和一致性坏了。

所以"这个后端只给某个职位用"必须是**代码强制**，不能靠文档提醒。

## 语义

| `allowed_agents` | 含义 |
|---|---|
| 空元组 | 不限（内置模型都是这样） |
| `("character_designer",)` | **只有**该职位能调用；主链路渲染（`render`）会被拒 |

拒绝时抛 `BackendNotAllowed`（`ValueError` 子类），消息里写清：
**谁**请求了**哪个**后端、**允许的是谁** —— 否则现场只能看到一句"不允许"，查不动。
"""

from __future__ import annotations

from typing import Any, Iterable


class BackendNotAllowed(ValueError):
    """某职位调用了用途限定的后端。"""


def allowed_agents(backend: Any) -> tuple[str, ...]:
    """取某个后端的用途限定（空 = 不限）。"""
    return tuple(str(a) for a in (getattr(backend, "allowed_agents", ()) or ()))


def is_allowed(backend: Any, agent: str) -> bool:
    """`agent` 是否可以调用 `backend`。"""
    allow = allowed_agents(backend)
    if not allow:
        return True
    return str(agent) in allow


def assert_allowed(backend: Any, agent: str, *, what: str = "") -> None:
    """不满足就抛 `BackendNotAllowed`（消息可读、可定位）。"""
    if is_allowed(backend, agent):
        return
    allow = allowed_agents(backend)
    ep = str(getattr(backend, "endpoint", "") or "")
    raise BackendNotAllowed(
        f"后端 {ep or '(未命名)'} 限定给 {list(allow)} 使用，"
        f"但本次调用方是 {agent!r}"
        + (f"（{what}）" if what else "")
        + "。若这是主链路渲染，说明它被错误地路由到了专属云端通道 —— 拒绝执行。"
    )


def filter_allowed(backends: Iterable[Any], agent: str) -> list[Any]:
    """按用途限定过滤候选后端（路由时用；不会抛错）。"""
    return [b for b in backends if is_allowed(b, agent)]
