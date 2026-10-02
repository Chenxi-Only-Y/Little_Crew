"""小剧组 (Little Crew) — 显式运行上下文（RunContext）。

背景（已实测，见 `docs/recon/08-solutions.md §2`）：
    原先"当前用户 / 当前会话 / 当前职业"三个值放在 `runtime_settings._local`
    这个 `threading.local()` 里。它有三个问题：
      1. `ThreadPoolExecutor` 里**不会继承** → 并发出图时 worker 拿到空值，
         产物落到 `_global/no-session-*/`（01-usage §6.2）
      2. `uvicorn --workers N` 下每个进程各有各的值，跨进程不可见
      3. 隐式依赖：任何函数都可能"偷偷"读到别人设的值，难以推理与测试

本模块把上下文变成**显式对象**：
    - `RunContext` 是冻结数据类，可随 State 传递、可落库、可跨线程传
    - `bind(ctx)` 在需要处临时绑定（内部仍写 thread-local，以兼容既有读取点）
这样既保留既有 `user()` / `run_session()` 的调用方式，又把"谁在跑"变成可传参的事实。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

#: 会话 ID 的合法字符（与 `sessions.session_id` 的取值一致：短 hex）
_MAX_ID_LEN = 64


@dataclass(frozen=True)
class RunContext:
    """一次运行的上下文：谁、在哪个会话、以哪个职业身份。

    Attributes:
        user_id:    用户标识（UID 或用户名；空 = 匿名/local）
        session_id: 会话标识（用于产物归档与状态查询）
        role:       当前职业 key（如 "photographer"），用于按职业读配置
    """

    user_id: str = ""
    session_id: str = ""
    role: str = ""

    def as_dict(self) -> dict:
        """可放进 LangGraph State 的普通 dict（State 需可序列化）。"""
        return {"user_id": self.user_id, "session_id": self.session_id, "role": self.role}

    @classmethod
    def from_dict(cls, d: dict | None) -> "RunContext":
        d = d or {}
        return cls(
            user_id=str(d.get("user_id") or "")[:_MAX_ID_LEN],
            session_id=str(d.get("session_id") or "")[:_MAX_ID_LEN],
            role=str(d.get("role") or "")[:_MAX_ID_LEN],
        )

    @classmethod
    def from_state(cls, state: dict | None) -> "RunContext":
        """从工作流 State 里取出上下文。

        优先级：
            1. State 里的 `run_context`（推荐：显式、可跨进程）
            2. 当前线程已绑定的上下文（兼容既有调用方式，如测试直接 `set_context` 后跑图）
            3. State 的 `session_id` + 匿名用户（兜底）

        保留第 2 条是为了**不破坏既有用法**：不少脚本/测试是
        `set_context(user, session)` 之后直接 `run_once(...)`，此时 State 里没有
        `run_context`，若只认 State 就会丢掉用户与会话。
        """
        s = state or {}
        raw = s.get("run_context")
        if isinstance(raw, dict) and raw:
            return cls.from_dict(raw)

        from app.services import runtime_settings as rs

        current = rs.capture()
        if current.get("user") not in ("", "_global") or current.get("session_id"):
            return cls(user_id=current.get("user") or "",
                       session_id=current.get("session_id") or "")

        return cls(user_id="", session_id=str(s.get("session_id") or ""))


@contextmanager
def bind(ctx: RunContext | None) -> Iterator[RunContext]:
    """把上下文绑定到当前线程，退出时恢复原值。

    内部仍写 `runtime_settings` 的 thread-local —— 这是**有意为之**：
    既有代码（`image._run_output_dir` / `video._video_dir` / `llm._resolve`）
    都通过 `user()` / `run_session()` 读取，改它们的签名会波及大量调用点。
    本函数让"绑定"变成一处显式、可回滚的动作，而不是散落的隐式赋值。
    """
    from app.services import runtime_settings as rs

    if ctx is None:
        yield RunContext()
        return

    prev = rs.capture()
    try:
        rs.set_context(ctx.user_id or None, ctx.session_id or None)
        yield ctx
    finally:
        rs.restore(prev)
