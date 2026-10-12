"""`/api/previs/scene` 的测试 —— AI 出预演初稿。

对应需求："把「预演」构成「白模预演师」一部分" + "AI 生成初稿，人工微调"。

重点：
  · 能从 `sessionId` 里取分镜（前端不必重复传大对象）；
  · 没分镜要**明确报 400**，而不是生成一个空场景让用户以为坏了；
  · 模型不可用时返回 **400 + 可读原因**，不是 500；
  · 产出的结构与前端 `PrevisScene` 对齐。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture
def auth(client):
    from app.services.auth import get_user_store

    uname = f"previs_{int(time.time() * 1000)}"
    token, msg = get_user_store().register(uname, "test1234")
    assert token, f"注册失败：{msg}"
    # 返回 (headers, username)：`sessionId` 那条测试需要用户名来写会话，
    # 否则 `_require_owner` 会 403 —— 而我第一版靠 `hasattr(...,"username_of")`
    # 去猜用户名，猜不到就静默跳过断言（**假通过**）。
    return {"Authorization": f"Bearer {token}"}, uname


def test_scene_requires_storyboard(client, auth):
    auth, _ = auth
    r = client.post("/api/previs/scene", headers=auth, json={})
    assert r.status_code == 400
    assert "分镜" in r.json()["detail"]


def test_scene_reports_llm_unavailable_as_400(client, auth, monkeypatch):
    auth, _ = auth
    """模型不可用 → 400 带可读原因（不是 500）。"""
    from app.services import llm as llm_mod

    def boom(*a, **kw):
        raise RuntimeError("未选择「llm」的提供者")

    monkeypatch.setattr(llm_mod, "get_llm", boom)
    r = client.post("/api/previs/scene", headers=auth,
                    json={"storyboard": [{"shot_id": "s1", "camera": "固定"}]})
    assert r.status_code == 400
    assert "提供者" in r.json()["detail"] or "模型" in r.json()["detail"]


def test_scene_ok_shape_matches_previs_scene(client, auth, monkeypatch):
    auth, _ = auth
    """成功时结构必须是 `{ground, actors, props, shots}`（前端按这个渲染）。"""

    class FakeLLM:
        mock = False

        def chat_json(self, *a, **kw):
            return {
                "ground": [12, 12],
                "actors": [{"id": "a1", "name": "小明", "pose": "walk",
                            "position": [0, 0, 0]}],
                "props": [{"id": "p1", "name": "桌子", "position": [0, 0, -2],
                           "size": [1, 1, 1]}],
                "shots": [{"id": "s1", "name": "镜头1", "duration": 4, "fps": 24,
                           "cameraTracks": [{"id": "t1", "name": "主机位", "keys": [
                               {"time": 0, "position": [0, 1.6, 5],
                                "target": [0, 1, 0], "fov": 40},
                               {"time": 4, "position": [0, 1.6, 3],
                                "target": [0, 1, 0], "fov": 40}]}]}],
            }

    from app.services import llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())
    r = client.post("/api/previs/scene", headers=auth,
                    json={"storyboard": [{"shot_id": "s1"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    scene = body["scene"]
    for k in ("ground", "actors", "props", "shots"):
        assert k in scene, f"PrevisScene 缺 {k}"
    assert scene["actors"][0]["pose"] == "walk"
    assert scene["shots"][0]["cameraTracks"][0]["keys"][-1]["time"] == 4
    assert body["attempts"] >= 1


def test_scene_can_read_storyboard_from_session(client, auth, monkeypatch):
    """只给 `sessionId` 也要能出初稿（从会话状态里取分镜与角色）。

    ⚠️ 我第一版这条**是假通过的**：用户名靠 `hasattr(store,"username_of")` 去猜，
    猜不到就 `if me:` 静默跳过断言。现在 fixture 直接给出用户名，断言必执行。
    """
    from app.services import llm as llm_mod
    from app.services.session_store import get_session_store

    headers, uname = auth

    class FakeLLM:
        mock = False

        def chat_json(self, *a, **kw):
            return {"ground": [8, 8],
                    "actors": [{"id": "a1", "name": "甲", "pose": "idle"}],
                    "props": [],
                    "shots": [{"id": "s1", "duration": 3, "cameraTracks": [
                        {"id": "t1", "keys": [
                            {"time": 0, "position": [0, 1.6, 4], "target": [0, 1, 0]},
                            {"time": 3, "position": [0, 1.6, 4], "target": [0, 1, 0]}]}]}]}

    monkeypatch.setattr(llm_mod, "get_llm", lambda: FakeLLM())

    sid = f"pv_{int(time.time() * 1000)}"
    get_session_store().save(
        sid, uname, "一句话", "done", "done",
        {"storyboard": [{"shot_id": "s1", "camera": "推近"}],
         "character_profiles": {"甲": {"appearance": "高"}}})

    r = client.post("/api/previs/scene", headers=headers, json={"sessionId": sid})
    assert r.status_code == 200, r.text
    scene = r.json()["scene"]
    assert scene["actors"], "从会话里取分镜后应该能出初稿"
    assert scene["shots"]


def test_scene_requires_auth(client):
    r = client.post("/api/previs/scene", json={"storyboard": [{"shot_id": "s1"}]})
    assert r.status_code in (401, 403)
