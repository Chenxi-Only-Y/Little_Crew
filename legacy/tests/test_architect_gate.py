"""审核闸门的测试 —— 用户明确要求的两条：

    ① "每次参与执行需要用户审核"
    ② "允许删节点，但需显式二次确认"

测试**不依赖真实 LLM**：用 Proposal 直接构造提案走接口，这样测的是
**闸门本身**（这是要守住的东西），而不是模型今天心情如何。
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

    uname = f"arch_{int(time.time() * 1000)}"
    token, msg = get_user_store().register(uname, "test1234")
    assert token, f"注册失败：{msg}"
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clean_graph(monkeypatch):
    """每个测试前后把图与版本恢复 —— 这些测试会**真的换图**。"""
    from app.workflows.graph_manager import MANAGER

    keep_graph = MANAGER.current_graph()
    keep_version = MANAGER.version
    keep_hist = list(MANAGER._history)
    yield
    monkeypatch.setattr(MANAGER, "_graph", keep_graph)
    monkeypatch.setattr(MANAGER, "_version", keep_version)
    monkeypatch.setattr(MANAGER, "_history", keep_hist)


def _make_proposal(client, auth, *, ops, summary="测试提案", request="测试"):
    """绕开 LLM，直接把一份提案写进 store 并返回它的公开形状。"""
    from app.workflows import architect, proposal_store as store
    from app.workflows.proposal import Proposal

    prop = Proposal(summary=summary, ops=ops, author="test")
    pv = architect.preview(prop)
    rec = {
        "id": prop.id, "summary": summary, "author": "test", "request": request,
        "ops": ops, "risks": [], "needs_second_confirm": prop.needs_second_confirm,
        "removes_nodes": prop.removes_nodes, "status": "pending_review",
        "preview": {k: v for k, v in pv.items() if k != "spec"},
        "_spec": pv.get("spec"), "events": [],
    }
    store.save(rec)
    return store.as_public(store.load(prop.id))


# ------------------------------------------------------------------
# ① 提案必须经过审核才生效
# ------------------------------------------------------------------

def test_proposal_is_not_applied_until_approved(client, auth, clean_graph):
    """**关键**：只"提提案"绝不能改图。"""
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    p = _make_proposal(client, auth, ops=[
        {"op": "remove_node", "key": "critics"},
        {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
        {"op": "set_branch", "from": "review_critics", "router": "simple_review",
         "branches": {"continue": "review_script", "redo": "screenwriter"}},
    ])
    assert MANAGER.version == v0, "刚提完提案就换图了 —— 审核闸门形同虚设"

    r = client.get(f"/api/workflow/proposal/{p['id']}", headers=auth)
    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "pending_review"


def test_approve_requires_delete_confirmation(client, auth, clean_graph):
    """② **删节点必须显式二次确认** —— 不带 confirm_removals 要 403。"""
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    p = _make_proposal(client, auth, ops=[
        {"op": "remove_node", "key": "critics"},
        {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
        {"op": "set_branch", "from": "review_critics", "router": "simple_review",
         "branches": {"continue": "review_script", "redo": "screenwriter"}},
    ])
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "二次确认" in detail["message"]
    assert detail["removes_nodes"] == ["critics"]
    assert MANAGER.version == v0, "被 403 挡住了却还是换了图"


def test_approve_with_confirmation_swaps_graph(client, auth, clean_graph):
    """确认后 → 拓扑校验 → **真的换图**，版本 +1 且记下提案 id。"""
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    p = _make_proposal(client, auth, ops=[
        {"op": "remove_node", "key": "critics"},
        {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
        {"op": "set_branch", "from": "review_critics", "router": "simple_review",
         "branches": {"continue": "review_script", "redo": "screenwriter"}},
    ])
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"confirm_removals": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert body["graph_version"] == v0 + 1
    assert "critics" not in MANAGER.current_graph().nodes

    hist = MANAGER.history()
    assert hist[-1]["source"] == f"architect_proposal:{p['id']}"
    assert hist[-1]["proposal"]["id"] == p["id"]


def test_already_applied_proposal_cannot_be_applied_twice(client, auth, clean_graph):
    p = _make_proposal(client, auth, ops=[
        {"op": "set_entry", "key": "story_planner"}])
    r1 = client.post(f"/api/workflow/proposal/{p['id']}/approve", headers=auth, json={})
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"/api/workflow/proposal/{p['id']}/approve", headers=auth, json={})
    assert r2.status_code == 409, r2.text


# ------------------------------------------------------------------
# 拓扑校验失败 → 必须拒绝，且**不改任何东西**
# ------------------------------------------------------------------

def test_invalid_result_is_rejected_and_changes_nothing(client, auth, clean_graph):
    """删掉上游节点导致下游**走不到** → 422，图不动。"""
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    before = set(MANAGER.current_graph().nodes)
    p = _make_proposal(client, auth, ops=[
        {"op": "remove_node", "key": "image_generator"}])

    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"confirm_removals": True})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "未生效" in detail["message"]
    # 必须报出"为什么"
    assert any("走不到" in p0["message"] for p0 in detail["problems"]), detail

    assert MANAGER.version == v0, "校验没过却换了图"
    assert set(MANAGER.current_graph().nodes) == before

    # 事件流里要留痕
    rec = client.get(f"/api/workflow/proposal/{p['id']}", headers=auth).json()["proposal"]
    assert any(e["event"] == "rejected_by_validation" for e in rec["events"])


def test_approve_can_skip_applying(client, auth, clean_graph):
    """`apply=false`：只批准、先不生效（留给 A3 走代码补丁那条路）。"""
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    p = _make_proposal(client, auth, ops=[
        {"op": "set_entry", "key": "story_planner"}])
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"apply": False})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False
    assert MANAGER.version == v0


# ------------------------------------------------------------------
# 驳回 / 回滚
# ------------------------------------------------------------------

def test_reject_changes_nothing(client, auth, clean_graph):
    from app.workflows.graph_manager import MANAGER

    v0 = MANAGER.version
    p = _make_proposal(client, auth, ops=[{"op": "remove_node", "key": "critics"}])
    r = client.post(f"/api/workflow/proposal/{p['id']}/reject",
                    headers=auth, json={"reason": "先不动"})
    assert r.status_code == 200
    assert r.json()["proposal"]["status"] == "rejected"
    assert MANAGER.version == v0


def test_rollback_returns_to_previous_graph(client, auth, clean_graph):
    from app.workflows.graph_manager import MANAGER

    before = set(MANAGER.current_graph().nodes)
    v0 = MANAGER.version

    p = _make_proposal(client, auth, ops=[
        {"op": "remove_node", "key": "critics"},
        {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
        {"op": "set_branch", "from": "review_critics", "router": "simple_review",
         "branches": {"continue": "review_script", "redo": "screenwriter"}},
    ])
    client.post(f"/api/workflow/proposal/{p['id']}/approve",
                headers=auth, json={"confirm_removals": True})
    assert "critics" not in MANAGER.current_graph().nodes
    assert MANAGER.version == v0 + 1

    r = client.post("/api/workflow/rollback", headers=auth, json={})
    assert r.status_code == 200, r.text
    assert set(MANAGER.current_graph().nodes) == before, "回滚没回到原来的图"


def test_rollback_without_history_is_409(client, auth, monkeypatch):
    from app.workflows.graph_manager import MANAGER

    monkeypatch.setattr(MANAGER, "_history", [])
    r = client.post("/api/workflow/rollback", headers=auth, json={})
    assert r.status_code == 409


# ------------------------------------------------------------------
# 观测端点 / 404
# ------------------------------------------------------------------

def test_graph_endpoint_reports_version(client, auth):
    r = client.get("/api/workflow/graph", headers=auth)
    assert r.status_code == 200
    g = r.json()["graph"]
    for k in ("version", "nodes", "checkpointer", "can_rollback", "history"):
        assert k in g

    # ⚠️ 这里**不要写死数字**：编译后的图比业务节点多两个内部节点
    # （`__start__` / `__end__`），所以 23 个业务节点会报成 24。
    # 我一开始写了 `== 23`，在组合跑时红 —— 浪费了一轮排查。
    # 正确做法是与**当前真实图**对比。
    from app.workflows.graph_manager import MANAGER

    assert g["nodes"] == len(MANAGER.current_graph().nodes)
    assert g["nodes"] >= 23, "节点数比业务节点还少，图明显不对"
    assert g["version"] >= 1
    assert g["checkpointer"], "应该报出用的是哪种 checkpointer"


def test_unknown_proposal_is_404(client, auth):
    assert client.get("/api/workflow/proposal/p-nope", headers=auth).status_code == 404
    assert client.post("/api/workflow/proposal/p-nope/approve",
                       headers=auth, json={}).status_code == 404


def test_architect_endpoints_require_auth(client):
    assert client.get("/api/workflow/proposals").status_code in (401, 403)
    assert client.get("/api/workflow/graph").status_code in (401, 403)
    assert client.post("/api/workflow/proposal", json={"request": "x"}).status_code in (401, 403)


# ------------------------------------------------------------------
# 架构师 Agent 本身（不依赖真实 LLM）
# ------------------------------------------------------------------

def test_architect_propose_without_request_gives_reason():
    from app.workflows import architect

    p = architect.propose("")
    assert p.ops == []
    assert p.risks, "没给诉求时必须说明为什么没法提"


def test_architect_preview_reports_problems():
    """预演要能提前告诉人"这份提案有问题"，而不是等审批时才炸。"""
    from app.workflows import architect
    from app.workflows.proposal import Proposal

    bad = Proposal(summary="乱改", ops=[{"op": "run_anything"}])
    pv = architect.preview(bad)
    assert pv["ok"] is False
    assert pv["problems"], "非法操作没被预演报出来"


def test_architect_preview_flags_second_confirm():
    from app.workflows import architect
    from app.workflows.proposal import Proposal

    pv = architect.preview(Proposal(summary="删", ops=[
        {"op": "remove_node", "key": "critics"}]))
    assert pv["needs_second_confirm"] is True
