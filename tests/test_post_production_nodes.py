"""后期三环的测试：动态合成师（=视频生成）/ 视频质检师 / 修图师。

用户 2026-10-07 的两条决定，直接对应这里的断言：
  · "**动态合成师就是视频生成**" —— 它的实现必须真的去调视频服务；
  · "**修图师目前先保留，后续开发，流程直接跳过**" —— 它必须跳过，且**不假装修过**。

## 为什么这些节点必须"不阻塞"

视频生成要**几分钟**（多镜头 + 轮询），而工作流节点是**线性同步**的。
所以动态合成师只**提交**后台任务就返回 —— 这条如果坏了，整张图会卡死。
"""

from __future__ import annotations

import pytest


# ----------------------------------------------------------------------
# 动态合成师（= 视频生成）
# ----------------------------------------------------------------------

def test_motion_skips_without_session():
    from app.services.post_production import composite_motion

    out = composite_motion({})
    assert "会话" in out["motion_note"]


def test_motion_skips_without_images():
    """视频以图片为首帧，没图就没什么可生成的 —— 说明原因，不报错。"""
    from app.services.post_production import composite_motion

    out = composite_motion({"session_id": "s1"})
    assert "图片" in out["motion_note"]


def test_motion_does_not_block(monkeypatch):
    """**最关键的一条**：节点必须**立即返回**，不能等视频跑完。

    做法：让后台线程的目标函数把这次调用记下来，然后断言 node 调用
    **在线程真正干活之前**就返回了。
    """
    import app.services.post_production as pp

    started = {"n": 0}

    class FakeThread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            started["n"] += 1      # 只标记"已提交"，**不真的执行**

        def is_alive(self):
            return False

    monkeypatch.setattr(pp.threading, "Thread", FakeThread)
    out = pp.composite_motion({"session_id": "s1",
                               "images": [{"shot_id": "1", "local_path": "x"}]})
    assert started["n"] == 1, "没有提交后台任务"
    assert "后台" in out["motion_note"], "应该说明是后台任务、不在流程里等"


def test_motion_does_not_duplicate_when_video_exists(monkeypatch):
    """会话里已有视频、且没要求重跑 → 不重复生成（省时间和钱）。"""
    import app.services.post_production as pp

    monkeypatch.setattr(pp, "_session_payload",
                        lambda st: {"video": {"dir_or_path": "v/", "produced": 4}})
    called = {"n": 0}

    class FakeThread:
        def __init__(self, **kw):
            pass

        def start(self):
            called["n"] += 1

        def is_alive(self):
            return False

    monkeypatch.setattr(pp.threading, "Thread", FakeThread)
    out = pp.composite_motion({"session_id": "s1", "images": [{"shot_id": "1"}]})
    assert called["n"] == 0, "已有视频却又生成了一遍"
    assert out["video"]["produced"] == 4


def test_motion_regenerate_flag_forces_rerun(monkeypatch):
    """`video_regenerate` 为真时即使已有视频也重跑（用户显式要求）。"""
    import app.services.post_production as pp

    monkeypatch.setattr(pp, "_session_payload",
                        lambda st: {"video": {"dir_or_path": "v/", "produced": 4}})
    called = {"n": 0}

    class FakeThread:
        def __init__(self, **kw):
            pass

        def start(self):
            called["n"] += 1

        def is_alive(self):
            return False

    monkeypatch.setattr(pp.threading, "Thread", FakeThread)
    pp.composite_motion({"session_id": "s1", "images": [{"shot_id": "1"}],
                         "video_regenerate": True})
    assert called["n"] == 1


def test_motion_skips_when_job_already_running(monkeypatch):
    """同一个会话已有任务在跑 → 不重复提交。"""
    import app.services.post_production as pp

    class Alive:
        def is_alive(self):
            return True

    pp._JOBS["s1"] = Alive()
    try:
        called = {"n": 0}
        monkeypatch.setattr(pp.threading, "Thread",
                            lambda **kw: called.__setitem__("n", called["n"] + 1))
        out = pp.composite_motion({"session_id": "s1", "images": [{"shot_id": "1"}]})
        assert called["n"] == 0
        assert "正在生成" in out["motion_note"]
    finally:
        pp._JOBS.pop("s1", None)


# ----------------------------------------------------------------------
# 视频质检师
# ----------------------------------------------------------------------

def test_video_assessor_skips_when_no_video():
    from app.services.post_production import assess_video

    out = assess_video({})
    assert out["video_report"] == {}
    assert "还没生成" in out["video_assessor_note"]


def test_video_assessor_reports_video_error(monkeypatch):
    """视频生成失败时，质检要报出原因，而不是当成"没问题"。"""
    from app.services import post_production

    monkeypatch.setattr(post_production, "_session_payload",
                        lambda st: {"video": {"error": "显存不足"}})
    out = post_production.assess_video({"session_id": "s1"})
    assert "显存不足" in out["video_assessor_note"]


def test_video_assessor_does_not_fabricate_scores(monkeypatch):
    """**不编造评分** —— 逐帧检测没接入就只能如实说，不能给个假分数。

    编一个假的"闪烁评分 0.9（合格）"比不检查更糟：用户会以为检过了。
    """
    import app.services.post_production as pp

    monkeypatch.setattr(pp, "_session_payload",
                        lambda st: {"video": {"dir_or_path": "v/", "produced": 3}})
    out = pp.assess_video({"session_id": "s1"})
    rep = out["video_report"]
    assert rep["clips"] == 3
    assert rep["checked"] is False
    assert "未接入" in rep["reason"]
    assert "score" not in rep and "flicker" not in rep, "不能编造评分字段"


# ----------------------------------------------------------------------
# 修图师（占位）
# ----------------------------------------------------------------------

def test_inpainter_skips_and_says_why():
    """用户："修图师目前先保留，后续开发，流程直接跳过"。"""
    from app.services.post_production import inpaint

    out = inpaint({"images": [{"shot_id": "1"}, {"shot_id": "2"}]})
    assert out["fixed_images"] == []
    assert out["current_phase"] == "inpainter"
    assert "跳过" in out["inpainter_note"]
    assert "未接入" in out["inpainter_note"], "要说清是后端没接，不是图没问题"


def test_inpainter_does_not_pretend(monkeypatch):
    """不能返回"修复成功"的假象 —— 比如把原图当修复结果塞回去。"""
    from app.services.post_production import inpaint

    imgs = [{"shot_id": "1", "local_path": "a.png"}]
    out = inpaint({"images": imgs})
    assert out["fixed_images"] == [], "把原图当修复结果返回 = 假装修过了"


# ----------------------------------------------------------------------
# 图接线
# ----------------------------------------------------------------------

def test_three_nodes_are_in_the_graph():
    """三环必须在执行图里，且按 修图→合成→视频质检→剪辑 串起来。"""
    from app.workflows.main_workflow import build_graph

    g = build_graph()
    for k in ("inpainter", "motion_compositor", "video_assessor"):
        assert k in g.nodes, f"{k} 不在执行图里"
    edges = set(g.edges)
    assert ("inpainter", "motion_compositor") in edges
    assert ("motion_compositor", "video_assessor") in edges
    assert ("video_assessor", "aggregator") in edges
