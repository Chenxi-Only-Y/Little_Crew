"""`runtime_settings.restore()` 的键名契约。

## 查出的真 bug（2026-10-07）

`restore(ctx)` 原先只认 **`ctx["user"]`**（`capture()` 的格式），
而 `RunContext.as_dict()` 给的是 **`user_id`** —— 两者混用时 `ctx.get("user")`
取到 `None` → `set_user(None)` → **删掉线程本地的用户** → 上下文静默变成 `_global`。

实测后果：角色设计师在图里 `restore(run_context.as_dict())` 之后
`user()` 仍是 `_global`，三张角色参考图落进
`_global/{工程名}/资产/character_designer/`（**账号丢失**，
与同一次运行的分镜图被劈成两个目录）。

这个坑特别隐蔽：`restore()` **不报错**，只是静默不生效；
而 `capture()`/`restore()` 在项目里被大量使用（并发出图、视频线程、白模帧）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean():
    from app.services import runtime_settings as rs

    rs.set_context(None, None)
    yield
    rs.set_context(None, None)


def test_restore_accepts_capture_format():
    """`capture()` 的格式（键 `user`）必须能 restore。"""
    from app.services import runtime_settings as rs

    rs.set_context("u1", "s1", "p1")
    ctx = rs.capture()
    assert ctx["user"] == "u1"

    rs.set_context(None, None)
    assert rs.user() == "_global"
    rs.restore(ctx)
    assert rs.user() == "u1"
    assert rs.run_session() == "s1"
    assert rs.run_project() == "p1"


def test_restore_accepts_run_context_as_dict():
    """`RunContext.as_dict()` 的格式（键 `user_id`）也必须能 restore。

    **这就是那个 bug**：原先只认 `user`，传 `as_dict()` 会静默变成 `_global`。
    """
    from app.core.context import RunContext
    from app.services import runtime_settings as rs

    d = RunContext(user_id="tester", session_id="sess-x", project="P").as_dict()
    assert "user_id" in d and "user" not in d, "as_dict 的键名变了？测试要跟着改"

    rs.set_context(None, None)
    rs.restore(d)
    assert rs.user() == "tester", "restore(as_dict()) 没生效 —— 上下文会掉到 _global"
    assert rs.run_session() == "sess-x"
    assert rs.run_project() == "P"


def test_restore_none_is_a_noop():
    """传 None 不动上下文（`bind()` 的 finally 会用到）。"""
    from app.services import runtime_settings as rs

    rs.set_context("u1", "s1")
    rs.restore(None)
    assert rs.user() == "u1"


def test_capture_restore_roundtrip_is_lossless():
    """capture → restore 往返不丢东西（这是并发路径赖以正确的基础）。"""
    from app.services import runtime_settings as rs

    rs.set_context("u9", "s9", "p9")
    before = rs.capture()
    rs.set_context(None, None)
    rs.restore(before)
    assert rs.capture() == before
