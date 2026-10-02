"""确认点续跑 / 断点丢失 的回归测试。

对应 `docs/recon/01-usage.md §6.1`（实测确认的缺陷）：
    服务重启后 `MemorySaver` 里的工作流状态消失，但 `sessions.status` 仍写着 `review`；
    此时 `resume` 不报错，却在**空 checkpoint 上从头重放整张图**——
    真实 provider 下等于把已完成的工作重做一遍（重复烧钱）。

本测试守住两件事：
  1. `has_checkpoint()` 能正确区分"有断点"与"断点已丢"
  2. 断点丢失时 `resume_run()` **拒绝续跑**并把会话标记为 failed，而不是静默重放
"""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.services import task_runner as tr


def _wait_status(session_id: str, targets: set[str], timeout: float = 60.0) -> str:
    """轮询会话状态直到落入 targets 或超时。"""
    from app.services.session_store import get_session_store

    store = get_session_store()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        row = store.get(session_id) or {}
        last = row.get("status") or ""
        if last in targets:
            return last
        time.sleep(0.2)
    return last


@pytest.fixture
def mock_env(monkeypatch):
    """全 mock、HITL 开启（要停在确认点）。"""
    monkeypatch.setattr(settings, "mock_llm", True)
    monkeypatch.setattr(settings, "mock_image", True)
    monkeypatch.setattr(settings, "mock_quality", True)
    monkeypatch.setattr(settings, "mock_critics", True)
    monkeypatch.setattr(settings, "hitl", True)
    yield


def test_has_checkpoint_false_for_unknown_thread():
    assert tr.has_checkpoint("thread-that-never-existed") is False


def test_resume_rejected_when_checkpoint_missing(mock_env):
    """断点丢失时应拒绝续跑（而不是静默重放）。"""
    sid = "t_lost_1"
    # 造一个"DB 里在等审核、但 checkpointer 里没这个线程"的会话
    from app.services.session_store import get_session_store

    get_session_store().save(sid, "tester", "测试创意", "review", "review", {})

    started, reason = tr.resume_run(sid, "tester", "approve", {})
    assert started is False, "断点已丢失却仍启动了续跑——会导致静默重放"
    assert "丢失" in reason or "重启" in reason

    row = get_session_store().get(sid) or {}
    assert row.get("status") == "failed", "会话应被标记为 failed"
    assert (row.get("payload") or {}).get("reason") == "checkpoint_lost"


def test_has_checkpoint_true_at_review(mock_env):
    """正常停在确认点时，`has_checkpoint` 应为 True，`resume_run` 应放行。"""
    sid = "t_ok_1"
    tr.start_run(sid, "一只会说话的小猫想当侦探", "tester")
    status = _wait_status(sid, {"review", "done", "error"})
    assert status == "review", f"未停在确认点（status={status}）"

    assert tr.has_checkpoint(sid) is True, "停在确认点却没有 checkpoint"

    started, message = tr.resume_run(sid, "tester", "approve", {})
    assert started is True, f"正常续跑被误拒：{message}"

    final = _wait_status(sid, {"review", "done", "error"})
    assert final in ("review", "done"), f"续跑后状态异常：{final}"


def test_resume_after_simulated_restart_is_rejected(mock_env, monkeypatch):
    """模拟服务重启：换一个全新的空 checkpointer，旧会话的续跑必须被拒绝。

    这是 §6.1 的**核心复现**：此前该场景会静默重放整张图。
    """
    sid = "t_restart_1"
    tr.start_run(sid, "重启复现", "tester")
    status = _wait_status(sid, {"review", "done", "error"})
    assert status == "review"
    assert tr.has_checkpoint(sid) is True

    # --- 模拟重启：换成一个全新的 workflow 实例（内存 checkpointer 为空）---
    from langgraph.checkpoint.memory import MemorySaver

    from app.workflows.main_workflow import build_graph

    fresh = build_graph().compile(checkpointer=MemorySaver())
    monkeypatch.setattr(tr, "app_workflow", fresh)

    assert tr.has_checkpoint(sid) is False, "新实例不应看到旧线程的 checkpoint"

    started, reason = tr.resume_run(sid, "tester", "approve", {})
    assert started is False, "重启后仍在静默重放——这正是要修的缺陷"

    from app.services.session_store import get_session_store

    row = get_session_store().get(sid) or {}
    assert row.get("status") == "failed"


def test_api_returns_409_when_checkpoint_lost(monkeypatch):
    """HTTP 层：断点丢失时 `/api/crew/resume` 必须返回 409，而不是 200 accepted。

    否则前端会以为"续跑已启动"，实际什么都没发生（或已重放）。
    """
    import time as _t

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.auth import get_user_store
    from app.services.session_store import get_session_store

    uname = f"ckpt_{int(_t.time())}"
    token, msg = get_user_store().register(uname, "test1234")
    assert token, f"注册失败：{msg}"
    h = {"Authorization": f"Bearer {token}"}

    sid = f"t_api_{int(_t.time())}"
    # 造一个"DB 在等审核、checkpointer 无此线程"的会话
    get_session_store().save(sid, uname, "续跑接口测试", "review", "review", {})

    c = TestClient(app)
    r = c.post("/api/crew/resume", headers=h,
               json={"session_id": sid, "decision": "approve", "feedback": {}})
    assert r.status_code == 409, f"期望 409，实际 {r.status_code}：{r.text[:200]}"
    body = r.json()
    assert body.get("reason") == "checkpoint_lost"
    assert "丢失" in body.get("message", "") or "重启" in body.get("message", "")

    # 会话应被标记为 failed，避免前端继续显示"等待审核"
    row = get_session_store().get(sid) or {}
    assert row.get("status") == "failed"
