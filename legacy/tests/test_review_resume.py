"""确认点审核与续跑（F4）的回归测试。

覆盖导演台的"审核 → 继续"链路：
  1. 首轮停在确认点，且 review 事件带可展示的内容
  2. `redo` 带反馈后**回到同一确认点**（不是跳过、也不是从头重跑）
  3. 连续 `approve` 能跑到 `done` 并产出完整结果
  4. 非确认点/不存在会话的 resume 被明确拒绝
  5. `back` 决策可用

⚠️ 时序要点：`resume_run` 会**同步**把 DB 状态置为 running，
后台线程随后才跑到下一个确认点写成 review。因此**必须轮询等待状态稳定**，
不能在 resume 返回后立刻断言——否则读到中间的 running（这是实测踩过的坑）。
"""

from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.services import task_runner as tr
from app.services.session_store import get_session_store


@pytest.fixture
def mock_env(monkeypatch):
    for k in ("mock_llm", "mock_image", "mock_quality", "mock_critics", "mock_video"):
        monkeypatch.setattr(settings, k, True)
    monkeypatch.setattr(settings, "hitl", True)
    yield


def _wait_status(sid: str, targets: set[str], timeout: float = 90.0) -> tuple[str, dict]:
    """轮询直到状态落入 targets。返回 (状态, 会话行)。"""
    store = get_session_store()
    deadline = time.time() + timeout
    row: dict = {}
    while time.time() < deadline:
        row = store.get(sid) or {}
        if row.get("status") in targets:
            return row.get("status", ""), row
        time.sleep(0.2)
    return row.get("status", ""), row


def test_first_run_stops_at_checkpoint(mock_env):
    sid = "f4_first_1"
    tr.start_run(sid, "首轮确认点测试", "tester")
    status, row = _wait_status(sid, {"review", "done", "error"})

    assert status == "review", f"未停在确认点：{status}"
    payload = row.get("payload") or {}
    assert payload.get("review_point"), "确认点缺少 review_point"
    assert payload.get("title"), "确认点缺少标题"
    assert payload.get("content"), "确认点缺少可展示内容"


def test_redo_returns_to_same_checkpoint(mock_env):
    """redo 应回到同一确认点（而非跳过或从头重跑）。"""
    sid = "f4_redo_1"
    tr.start_run(sid, "redo 测试", "tester")
    status, row = _wait_status(sid, {"review", "done", "error"})
    assert status == "review"
    first_point = (row.get("payload") or {}).get("review_point")

    started, msg = tr.resume_run(sid, "tester", "redo", {"note": "冲突再强一点"})
    assert started, f"redo 未被接受：{msg}"

    status2, row2 = _wait_status(sid, {"review", "done", "error"})
    assert status2 == "review", f"redo 后未回到确认点：{status2}"
    assert (row2.get("payload") or {}).get("review_point") == first_point, (
        "redo 后停在了不同的确认点，说明没有回到原环节"
    )


def test_approve_chain_reaches_done(mock_env):
    """连续 approve 应能跑到 done，并产出图/分镜/提示词。"""
    sid = "f4_chain_1"
    tr.start_run(sid, "审核链测试", "tester")

    status, _ = _wait_status(sid, {"review", "done", "error"})
    approvals = 0
    while status == "review" and approvals < 12:
        started, msg = tr.resume_run(sid, "tester", "approve", {})
        assert started, f"approve 未被接受：{msg}"
        approvals += 1
        status, _ = _wait_status(sid, {"review", "done", "error", "cancelled"})

    assert status == "done", f"未能跑到完成：{status}（approve {approvals} 次）"
    assert approvals >= 1, "至少应经过一次确认点"

    row = get_session_store().get(sid) or {}
    # 注意：payload 是**顶层扁平**结构（images/storyboard/prompts 直接在顶层），
    # 不存在 payload["result"] 这一层——aggregator 只产出 `result` 字段，
    # 而 _run_stream 把它写进了 payload 顶层。
    payload = row.get("payload") or {}
    assert payload.get("images"), "完成后没有图片产物"
    assert payload.get("storyboard"), "完成后没有分镜产物"
    assert payload.get("prompts"), "完成后没有提示词产物"


def test_resume_rejected_for_unknown_session():
    started, msg = tr.resume_run("完全不存在的会话", "tester", "approve", {})
    assert not started
    assert msg, "拒绝时应给出原因"


def test_resume_rejected_when_not_at_checkpoint(mock_env):
    """已完成（或运行中）的会话再 resume，应被拒绝而不是静默重跑。"""
    sid = "f4_notreview_1"
    tr.start_run(sid, "非确认点测试", "tester")
    status, _ = _wait_status(sid, {"review", "done", "error"})
    assert status == "review"

    # 先跑到完成
    approvals = 0
    while status == "review" and approvals < 12:
        tr.resume_run(sid, "tester", "approve", {})
        approvals += 1
        status, _ = _wait_status(sid, {"review", "done", "error", "cancelled"})
    assert status == "done"

    started, msg = tr.resume_run(sid, "tester", "approve", {})
    assert not started, "已完成的会话不应被再次续跑"
    assert "确认点" in msg or "丢失" in msg


def test_back_decision_is_accepted(mock_env):
    """`back`（返回上一步）应被接受并可继续。"""
    sid = "f4_back_1"
    tr.start_run(sid, "back 测试", "tester")
    status, _ = _wait_status(sid, {"review", "done", "error"})
    assert status == "review"

    started, msg = tr.resume_run(sid, "tester", "back", {})
    assert started, f"back 未被接受：{msg}"

    status2, _ = _wait_status(sid, {"review", "done", "error", "cancelled"})
    assert status2 in ("review", "done"), f"back 后状态异常：{status2}"
