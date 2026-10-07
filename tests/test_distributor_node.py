"""发行总监节点的测试（用户："剪辑师不是必需的，可以点跳过"）。

## 为什么单独测它

发行总监的输入（成片）来自**独立的异步视频任务**，不在图状态里。
所以它必须**在没有成片时优雅跳过**，而不是报错或阻塞 ——
否则跑整条流程会卡在最后一步。

⚠️ 这里顺带守住一个坑：`Distributor.run()` 读的是 `state["video"]`，
而图状态里那玩意儿叫 `final_video` —— **名字不一致**。
第一版我漏了这层转换，会被它内部当成"没有视频"静默跳过。
"""

from __future__ import annotations

import pytest


def test_skips_without_final_video():
    """没有成片 → 跳过并说明，**不报错**。"""
    from app.services.post_production import publish

    out = publish({})
    assert out["publish_results"] == {}
    assert "跳过" in out["distributor_note"]
    assert "不是必需" in out["distributor_note"]


def test_skip_message_is_actionable():
    """跳过时要说清怎么办（用户看得到这条提示）。"""
    from app.services.post_production import publish

    out = publish({"final_video": None})
    note = out["distributor_note"]
    assert "生成视频" in note or "再跑" in note


def test_maps_final_video_to_video_key(monkeypatch):
    """`final_video` → `video` 的字段名转换必须发生。

    `Distributor.run()` 的 docstring 写的是 `video: dict 最终视频`，
    而图状态里叫 `final_video`。不做转换它会当成没有视频。
    """
    seen = {}

    class FakeDistributor:
        def run(self, state, platforms=None):
            seen["state"] = dict(state)
            return {"publish_results": {"bilibili": {"status": "mock"}}}

    import app.agents.distributor as dist_mod
    from app.services.post_production import publish

    monkeypatch.setattr(dist_mod, "Distributor", FakeDistributor)
    out = publish({"final_video": {"dir_or_path": "out/videos", "segments": 3}})

    assert "video" in seen["state"], "没把 final_video 补成 video —— 会被静默跳过"
    assert seen["state"]["video"]["produced"] == 3
    assert seen["state"]["video"]["dir_or_path"] == "out/videos"
    assert out["publish_results"], "发行结果没带回来"


def test_prefers_session_video_over_final_video(monkeypatch):
    """会话 payload 里有真实视频信息时优先用它（比图状态的推断更准）。"""
    seen = {}

    class FakeDistributor:
        def run(self, state, platforms=None):
            seen["video"] = dict(state.get("video") or {})
            return {"publish_results": {"x": {}}}

    import app.agents.distributor as dist_mod
    from app.services import post_production

    monkeypatch.setattr(dist_mod, "Distributor", FakeDistributor)
    monkeypatch.setattr(post_production, "_session_payload",
                        lambda state: {"video": {"dir_or_path": "real/", "produced": 7}})

    post_production.publish({
        "session_id": "s1",
        "final_video": {"dir_or_path": "stale/", "segments": 1},
    })
    assert seen["video"]["produced"] == 7
    assert seen["video"]["dir_or_path"] == "real/"


def test_distributor_failure_does_not_raise(monkeypatch):
    """发行失败不该让整条流程挂掉（发行本来就是附加环节）。"""

    class Boom:
        def run(self, state, platforms=None):
            raise RuntimeError("平台接口挂了")

    import app.agents.distributor as dist_mod
    from app.services.post_production import publish

    monkeypatch.setattr(dist_mod, "Distributor", Boom)
    out = publish({"final_video": {"dir_or_path": "x", "segments": 1}})
    assert out["publish_results"] == {}
    assert "平台接口挂了" in out["distributor_note"]


def test_no_separate_editor_node_exists():
    """**不该有单独的 editor 节点** —— 剪辑师就是 `aggregator`。

    注册表里 `editor.workflow_node == 'aggregator'`（历史命名）。
    我第一版另外加了个 `editor` 图节点，等于两个剪辑师（标签还撞名）。
    这条守着它别回来。
    """
    from app.registry.roles import ALL_NODES
    from app.workflows.main_workflow import build_graph

    built = set(build_graph().nodes)
    assert "editor" not in built, "又出现独立的 editor 节点了 —— 与 aggregator 重复"
    ed = next(n for n in ALL_NODES if n.key == "editor")
    assert ed.workflow_node == "aggregator"
    assert "aggregator" in built


def test_no_dead_edit_function():
    """`edit_final_video` 已删 —— 它是撤掉重复节点后遗留的死代码。"""
    from app.services import post_production

    assert not hasattr(post_production, "edit_final_video"), (
        "edit_final_video 回来了但没有调用者 —— 会是死代码")
