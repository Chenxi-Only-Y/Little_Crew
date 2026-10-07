"""运行期事件广播（SSE）的回归测试。

对应 F3：导演台需要**实时**看到环节推进与 AI 思考过程，而不是轮询整包状态。

覆盖：
  1. 事件中心本身（发布 / 订阅 / 补发 / 退订 / 上限）
  2. `task_runner` 在真实运行中确实广播了 status / stage / thinking / review / progress
  3. 被 start/end 区间跳过的环节被如实标为 skipped（而非"没发生"）
  4. 确认点会广播 review 事件并把会话置为 waiting_review
"""

from __future__ import annotations

import queue
import time

import pytest

from app.core.config import settings
from app.services.events import EventHub, get_hub


# ==================================================================
# 一、事件中心自身
# ==================================================================

def test_publish_and_subscribe():
    hub = EventHub()
    q = hub.subscribe("s1", replay=False)
    hub.publish("s1", "stage", stage="a", status="done")
    ev = q.get_nowait()
    assert ev.type == "stage"
    assert ev.data["stage"] == "a"
    assert ev.seq == 1


def test_sequence_increments_per_session():
    hub = EventHub()
    hub.publish("s1", "status", status="running")
    hub.publish("s1", "status", status="done")
    assert [e.seq for e in hub.history("s1")] == [1, 2]


def test_replay_gives_history_to_late_subscriber():
    """晚连上的订阅者应能补上已发生的事件（否则看不到已有进度）。"""
    hub = EventHub()
    hub.publish("s1", "stage", stage="a", status="done")
    hub.publish("s1", "stage", stage="b", status="done")

    q = hub.subscribe("s1", replay=True)
    got = [q.get_nowait().data["stage"] for _ in range(2)]
    assert got == ["a", "b"]


def test_replay_can_be_disabled():
    hub = EventHub()
    hub.publish("s1", "stage", stage="a", status="done")
    q = hub.subscribe("s1", replay=False)
    assert q.empty()


def test_multiple_subscribers_each_receive():
    hub = EventHub()
    q1 = hub.subscribe("s1", replay=False)
    q2 = hub.subscribe("s1", replay=False)
    hub.publish("s1", "status", status="running")
    assert q1.get_nowait().type == "status"
    assert q2.get_nowait().type == "status"


def test_unsubscribe_stops_delivery():
    hub = EventHub()
    q = hub.subscribe("s1", replay=False)
    hub.unsubscribe("s1", q)
    assert hub.subscriber_count("s1") == 0
    hub.publish("s1", "status", status="running")
    assert q.empty()


def test_sessions_are_isolated():
    hub = EventHub()
    q1 = hub.subscribe("s1", replay=False)
    hub.publish("s2", "status", status="done")
    assert q1.empty()


def test_history_is_bounded():
    """历史有上限，避免长时间运行把内存吃满。"""
    hub = EventHub(history_size=10)
    for i in range(25):
        hub.publish("s1", "stage", stage=f"n{i}", status="done")
    assert len(hub.history("s1")) == 10
    # 保留的是最近的
    assert hub.history("s1")[-1].data["stage"] == "n24"


def test_slow_consumer_does_not_block_and_drops_oldest():
    """慢消费者不能让生产者阻塞：队列满时丢最旧的。"""
    hub = EventHub()
    q = hub.subscribe("s1", replay=False)
    for i in range(600):          # 超过 _QUEUE_MAX(512)
        hub.publish("s1", "stage", stage=f"n{i}", status="done")
    assert q.full()
    # 队尾应是最后发布的那条
    items = []
    while not q.empty():
        items.append(q.get_nowait().data["stage"])
    assert items[-1] == "n599"


def test_reset_clears_history_and_seq():
    hub = EventHub()
    hub.publish("s1", "status", status="running")
    hub.reset("s1")
    assert hub.history("s1") == []
    assert hub.publish("s1", "status", status="done").seq == 1


def test_stream_yields_sse_frames_and_closes():
    hub = EventHub()
    hub.publish("s1", "stage", stage="a", status="done")
    hub.close_stream("s1")            # 先放哨兵，让生成器会结束

    frames = list(hub.stream("s1", replay=True, heartbeat=0.05))
    assert any(f.startswith(": connected") for f in frames)
    assert any("event: stage" in f for f in frames)
    assert any('"stage": "a"' in f for f in frames)


def test_stream_emits_keepalive_when_idle():
    hub = EventHub()
    gen = hub.stream("s1", replay=False, heartbeat=0.05)
    next(gen)                          # connected
    time.sleep(0.12)
    assert next(gen) == ": keep-alive\n\n"
    gen.close()


def test_get_hub_is_singleton():
    assert get_hub() is get_hub()


def test_close_stream_then_subscribe_still_terminates():
    """先结束、后订阅：必须立刻能拿到哨兵并结束，不能永久等待。

    这条针对一个真实缺陷：`close_stream` 在**没有订阅者**时无处投递哨兵，
    若订阅者之后才连上、又没有 ended 标记，`stream()` 会一直空转。
    """
    hub = EventHub()
    hub.publish("s1", "done", phase="x")
    hub.close_stream("s1")               # 此时还没有订阅者

    frames = list(hub.stream("s1", replay=True, heartbeat=0.05))
    assert any("event: done" in f for f in frames)


def test_stream_terminates_even_if_sentinel_cannot_be_queued():
    """哨兵因队列满而放不进去时，仍必须能结束（靠 closed 标记 + 排空判定）。

    这是上面那条的强化版：把队列灌满后再 close，`stream()` 不能永久阻塞。
    """
    hub = EventHub()
    q = hub.subscribe("s1", replay=False)
    for i in range(600):                 # 灌满（_QUEUE_MAX=512）
        hub.publish("s1", "stage", stage=f"n{i}", status="done")
    assert q.full()
    hub.close_stream("s1")               # 哨兵放不进去

    frames = []
    for f in hub.stream("s1", replay=False, heartbeat=0.05):
        frames.append(f)
        if len(frames) > 1000:           # 保险：不该走到这里
            pytest.fail("stream 未终止，疑似永久阻塞")
    assert frames[0].startswith(": connected")


def test_publish_reopens_closed_session():
    """同一 session 再次运行时应视为未结束（支持反复运行）。"""
    hub = EventHub()
    hub.close_stream("s1")
    assert hub.is_closed("s1")
    hub.publish("s1", "status", status="running")
    assert not hub.is_closed("s1")


# ==================================================================
# 二、真实运行中的广播（端到端）
# ==================================================================

@pytest.fixture
def mock_env(monkeypatch):
    for k in ("mock_llm", "mock_image", "mock_quality", "mock_critics", "mock_video"):
        monkeypatch.setattr(settings, k, True)
    monkeypatch.setattr(settings, "hitl", True)
    yield


def _drain(hub, sid, timeout=60.0):
    """把某会话的事件收全（到 sentinel 或超时）。"""
    q = hub.subscribe(sid, replay=True)
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ev = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if ev is None:
            break
        events.append(ev)
        if ev.type == "status" and ev.data.get("status") in ("done", "failed", "cancelled"):
            break
    hub.unsubscribe(sid, q)
    return events


def test_run_broadcasts_stage_and_thinking(mock_env):
    """一次真实运行必须广播环节状态与 AI 思考过程。"""
    from app.services import task_runner as tr

    sid = "evt_run_1"
    hub = get_hub()
    hub.reset(sid)
    q = hub.subscribe(sid, replay=True)

    tr.start_run(sid, "一只会说话的小猫想当侦探", "tester")
    events = _drain(hub, sid, timeout=60)

    types = {e.type for e in events}
    assert "stage" in types, "没有广播环节状态"
    assert "thinking" in types, "没有广播 AI 思考过程"
    assert "progress" in types, "没有广播进度快照"

    stages = [e.data["stage"] for e in events if e.type == "stage"]
    # 事件用注册表 key（showrunner = 工作流节点 story_planner）
    assert "showrunner" in stages, f"策划总监环节未上报；stages={stages[:8]}"
    assert "story_planner" not in stages, "事件未翻译成注册表 key"
    assert any(e.data.get("status") == "done" for e in events if e.type == "stage")


def test_skipped_stages_are_reported_as_skipped(mock_env):
    """本次没被执行的环节要标 skipped，而不是不出现、也不是误标成 done。

    这条针对 01-usage §6.3：状态语义必须区分 done/skipped，
    否则前端进度会"停在上一环"。

    命名空间说明：事件里用**注册表 key**（`showrunner`），
    工作流内部节点叫 `story_planner`，由 `registry.api.to_registry_node()` 翻译。

    判定方式：跑完后仍在 pending 的节点 → skipped（**按实际发生了什么**），
    不按索引与注册表顺序做差——工作流的实际执行顺序与注册表拓扑顺序并不一致。
    """
    from app import registry  # noqa: F401  确保注册表可用
    from app.registry import api as registry_api
    from app.services import task_runner as tr

    sid = "evt_skip_1"
    hub = get_hub()
    hub.reset(sid)

    # 从编剧开始：策划总监（注册表 key=showrunner）之前的 v2 新角色应保持未执行
    tr.start_run(sid, "跳段测试", "tester", seed={"start_stage": "screenwriter"})
    events = _drain(hub, sid, timeout=60)

    stage_events = [e for e in events if e.type == "stage"]
    by_status: dict[str, list[str]] = {}
    for e in stage_events:
        by_status.setdefault(e.data.get("status", "?"), []).append(e.data["stage"])

    assert by_status.get("skipped"), f"没有 skipped 事件；实际={by_status}"
    assert "screenwriter" in by_status.get("done", []), (
        f"编剧应完成，实际 done={by_status.get('done')}"
    )
    assert "screenwriter" not in by_status.get("skipped", []), "编剧被误标为跳过"

    # 事件里的阶段名必须都是注册表 key（不能漏出工作流内部命名）
    known = {n.key for n in registry_api.all_nodes()}
    leaked = [s for s in by_status.get("done", []) if s not in known]
    assert not leaked, f"事件里出现未翻译的工作流节点名：{leaked}"

    # 本次没跑到（首轮就停在确认点）的**后期环节**应被如实标为跳过
    #
    # ⚠️ 这条原先断言的是 `previs_artist`，理由是"尚未进图的 v2 角色"。
    # 它 2026-10-07 已经**真的进图了**（接在分镜确认之后），所以那个理由过期了
    # —— 而且它恰好排在 `screenwriter` **之后**，`start_stage=screenwriter`
    # 本来就不该跳过它。换成本轮确实跑不到、且语义稳定的后期环节。
    for late in ("inpainter", "motion_compositor", "video_assessor"):
        assert late in by_status.get("skipped", []), (
            f"本轮没跑到的后期环节 {late} 应标 skipped，实际 skipped={by_status.get('skipped')}"
        )


def test_review_checkpoint_broadcasts_review_and_status(mock_env):
    """到达确认点时要广播 review 内容，并把整体状态置为 waiting_review。"""
    from app.services import task_runner as tr

    sid = "evt_review_1"
    hub = get_hub()
    hub.reset(sid)
    tr.start_run(sid, "确认点事件测试", "tester")
    events = _drain(hub, sid, timeout=60)

    reviews = [e for e in events if e.type == "review"]
    assert reviews, "没有广播 review 事件"
    payload = reviews[0].data.get("review")
    assert isinstance(payload, dict)
    assert "review_point" in payload and "title" in payload

    statuses = [e.data.get("status") for e in events if e.type == "status"]
    assert "waiting_review" in statuses, f"未见 waiting_review；statuses={statuses}"


def test_progress_snapshot_covers_expected_nodes(mock_env):
    """进度快照应覆盖注册表里的全部节点（前端据此整体对齐）。"""
    from app.registry import api as registry
    from app.services import task_runner as tr

    sid = "evt_prog_1"
    hub = get_hub()
    hub.reset(sid)
    tr.start_run(sid, "进度快照测试", "tester")
    events = _drain(hub, sid, timeout=60)

    snaps = [e for e in events if e.type == "progress"]
    assert snaps, "没有 progress 事件"
    nodes = snaps[-1].data.get("nodes") or {}
    assert set(nodes) == set(registry.execution_order())
    assert all(v in ("pending", "running", "done", "skipped", "review", "failed") for v in nodes.values())
