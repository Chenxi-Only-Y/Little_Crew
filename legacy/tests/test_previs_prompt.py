"""预演初稿生成（AI）+ 白模预演师节点 的测试。

对应两个用户要求：

  ① "把「预演」构成「白模预演师」一部分" —— 它得真的在执行图里有个位置
  ② "AI 生成初稿，人工微调" —— 初稿要能被生成出来，且**失败不能拖垮工作流**

所以测试分三块：
  · `normalize_scene` 的**归一化**（模型输出脏也要救回来，且说明改了什么）
  · `generate_scene` 的**失败路径**（不抛异常、给可读原因、mock 标注清楚）
  · 节点在**无推理服务**时仍让工作流继续（这是最容易出人命的地方）
"""

from __future__ import annotations

import pytest

from app.services import previs_prompt as pp


# ------------------------------------------------------------------
# 归一化：脏输出也要救回来，并说明改了什么
# ------------------------------------------------------------------

def test_normalize_fills_missing_ground():
    scene, notes = pp.normalize_scene({})
    assert scene["ground"] == [20, 20]
    assert notes


def test_normalize_replaces_illegal_pose():
    """`pose` 只能是 6 个预设之一 —— 给了别的要替换，**不能丢掉整个角色**。"""
    scene, notes = pp.normalize_scene({
        "actors": [{"id": "a1", "name": "小明", "pose": "飞天"}]})
    assert len(scene["actors"]) == 1, "一个非法 pose 把整个角色丢了"
    assert scene["actors"][0]["pose"] == "idle"
    assert any("pose" in n for n in notes), "替换了却没说明"


def test_normalize_grounds_actors():
    """人物 y 一律压到 0（脚底贴地）—— 模型常给错。"""
    scene, _ = pp.normalize_scene({
        "actors": [{"id": "a1", "name": "x", "position": [1, 3.5, 2]}]})
    assert scene["actors"][0]["position"][1] == 0.0


def test_normalize_keeps_camera_above_ground():
    """机位不能穿进地面（y >= 0.3）。"""
    scene, _ = pp.normalize_scene({
        "shots": [{"id": "s1", "duration": 4, "cameraTracks": [
            {"id": "t1", "keys": [
                {"time": 0, "position": [0, -2, 5], "target": [0, 1, 0]},
                {"time": 4, "position": [0, 0.1, 5], "target": [0, 1, 0]},
            ]}]}]})
    for k in scene["shots"][0]["cameraTracks"][0]["keys"]:
        assert k["position"][1] >= 0.3, k


def test_normalize_pads_single_keyframe():
    """只有 1 个关键帧 = 没有运镜 → 补齐成 2 帧。"""
    scene, notes = pp.normalize_scene({
        "shots": [{"id": "s1", "duration": 5, "cameraTracks": [
            {"id": "t1", "keys": [{"time": 0, "position": [0, 1.6, 5],
                                   "target": [0, 1, 0]}]}]}]})
    keys = scene["shots"][0]["cameraTracks"][0]["keys"]
    assert len(keys) >= 2
    assert keys[0]["time"] == 0
    assert keys[-1]["time"] == 5
    assert any("关键帧" in n for n in notes)


def test_normalize_aligns_first_and_last_keyframe_time():
    """首帧必须 time=0、末帧必须 time=duration（否则运镜区间不对）。"""
    scene, notes = pp.normalize_scene({
        "shots": [{"id": "s1", "duration": 6, "cameraTracks": [
            {"id": "t1", "keys": [
                {"time": 1.5, "position": [0, 1.6, 5], "target": [0, 1, 0]},
                {"time": 4.0, "position": [0, 1.6, 3], "target": [0, 1, 0]},
            ]}]}]})
    keys = scene["shots"][0]["cameraTracks"][0]["keys"]
    assert keys[0]["time"] == 0.0
    assert keys[-1]["time"] == 6.0
    assert any("首帧" in n for n in notes) and any("末帧" in n for n in notes)


def test_normalize_adds_default_camera_when_shot_has_none():
    scene, notes = pp.normalize_scene({"shots": [{"id": "s1", "duration": 3}]})
    tracks = scene["shots"][0]["cameraTracks"]
    assert len(tracks) == 1 and len(tracks[0]["keys"]) >= 2
    assert any("没有机位" in n for n in notes)


def test_normalize_clamps_fov_and_duration():
    scene, _ = pp.normalize_scene({
        "shots": [{"id": "s1", "duration": 999, "fps": 500, "cameraTracks": [
            {"id": "t1", "keys": [
                {"time": 0, "position": [0, 1.6, 5], "target": [0, 1, 0], "fov": 9999},
                {"time": 999, "position": [0, 1.6, 5], "target": [0, 1, 0], "fov": 1},
            ]}]}]})
    sh = scene["shots"][0]
    assert 0.5 <= sh["duration"] <= 30
    assert 6 <= sh["fps"] <= 60
    for k in sh["cameraTracks"][0]["keys"]:
        assert 10 <= k["fov"] <= 120


def test_normalize_non_dict_gives_empty_scene():
    scene, notes = pp.normalize_scene("这不是 JSON 对象")
    assert scene["actors"] == [] and scene["shots"] == []
    assert notes


# ------------------------------------------------------------------
# 生成：失败不抛异常、mock 要标注
# ------------------------------------------------------------------

def test_generate_returns_error_when_llm_unavailable(monkeypatch):
    """**没有推理服务时必须软失败** —— 返回 error，不抛异常。

    这里能抛异常的话，节点就会把整条工作流带崩（预演只是锦上添花）。
    """
    from app.services import llm as llm_mod

    def boom(*a, **kw):
        raise RuntimeError("没有可用的推理服务")

    monkeypatch.setattr(llm_mod, "get_llm", boom)
    res = pp.generate_scene([{"shot_id": "s1"}])
    assert res["status"] == "error"
    assert "推理服务" in res["message"] or "模型" in res["message"]


def test_generate_rejects_non_json_output(monkeypatch):
    class FakeLLM:
        mock = False

        def chat_json(self, *a, **kw):
            return {"_parse_error": True, "error": "不是 JSON", "raw": "嗯…"}

    from app.services import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())
    res = pp.generate_scene([{"shot_id": "s1"}])
    assert res["status"] == "error"
    assert "JSON" in res["message"]


def test_generate_retries_once_when_scene_is_empty(monkeypatch):
    """第一次给出空场景 → 回灌错误重试一次（与白模规格同一套做法）。"""
    calls = {"n": 0}

    class FakeLLM:
        mock = False

        def chat_json(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ground": [10, 10], "actors": [], "props": [], "shots": []}
            return {
                "ground": [10, 10],
                "actors": [{"id": "a1", "name": "小明", "pose": "walk"}],
                "shots": [{"id": "s1", "duration": 4, "fps": 24, "cameraTracks": [
                    {"id": "t1", "keys": [
                        {"time": 0, "position": [0, 1.6, 5], "target": [0, 1, 0]},
                        {"time": 4, "position": [0, 1.6, 3], "target": [0, 1, 0]}]}]}],
            }

    from app.services import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())
    res = pp.generate_scene([{"shot_id": "s1"}])
    assert res["status"] == "ok"
    assert res["attempts"] == 2
    assert calls["n"] == 2


def test_generate_mock_is_labelled(monkeypatch):
    """mock 模式必须**明确标注**，不能假装是真实分析。"""

    class FakeLLM:
        mock = True

    from app.services import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())
    res = pp.generate_scene([{"shot_id": "s1"}])
    assert res["status"] == "ok"
    assert any("mock" in n.lower() for n in res["notes"]), res["notes"]
    assert res["scene"]["actors"], "mock 也该给一个可用场景（否则编辑器没东西可改）"


# ------------------------------------------------------------------
# 节点：失败不能拖垮工作流
# ------------------------------------------------------------------

def test_node_without_storyboard_skips_not_fails():
    from app.services.previs_artist import generate_previs_scene

    out = generate_previs_scene({})
    assert out["current_phase"] == "previs_artist"
    assert out["previs"]["scene"] is None
    assert "没有分镜" in out["previs"]["skipped"]


def test_node_reports_error_but_does_not_raise(monkeypatch):
    """**最关键的一条**：推理服务不可用时，节点要返回 error 而不是抛异常。

    它要是抛了，整条工作流就断在预演这一步 —— 而预演只是可选的锦上添花。
    """
    from app.services import llm as llm_mod
    from app.services.previs_artist import generate_previs_scene

    def boom(*a, **kw):
        raise RuntimeError("未选择「llm」的提供者")

    monkeypatch.setattr(llm_mod, "get_llm", boom)
    out = generate_previs_scene({"storyboard": [{"shot_id": "s1"}]})
    assert out["previs"]["scene"] is None
    assert out["previs"]["error"]
    assert out["thinking_log"], "失败也要留下可读的思考记录"


def test_node_puts_scene_into_state(monkeypatch):
    class FakeLLM:
        mock = False

        def chat_json(self, *a, **kw):
            return {
                "ground": [15, 15],
                "actors": [{"id": "a1", "name": "小明", "pose": "idle"}],
                "props": [{"id": "p1", "name": "桌子"}],
                "shots": [{"id": "s1", "duration": 5, "fps": 24, "cameraTracks": [
                    {"id": "t1", "keys": [
                        {"time": 0, "position": [0, 1.6, 5], "target": [0, 1, 0]},
                        {"time": 5, "position": [0, 1.6, 2], "target": [0, 1, 0]}]}]}],
            }

    from app.services import llm as llm_mod
    from app.services.previs_artist import generate_previs_scene

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())
    out = generate_previs_scene({"storyboard": [{"shot_id": "s1"}]})
    scene = out["previs"]["scene"]
    assert scene["actors"] and scene["shots"]
    assert out["previs"]["error"] == ""
    assert "镜头" in out["thinking_log"][0]["text"]
