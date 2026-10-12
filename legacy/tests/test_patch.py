"""A3（代码补丁）的测试。

用户要的是 **A2 + A3**：

    A2 换运行时内存里的图 —— 立刻能试，**重启即失效**
    A3 把改动落成**代码补丁** —— 进版本库、可 review、可回滚

所以这里要守住：
  · 批准时**自动生成**补丁，并且补丁内容真的反映那次变更；
  · 补丁里既有**人看的 diff**，也有**机读的完整规格**；
  · 生成补丁失败**不影响**已经生效的换图（两者解耦）；
  · 能重新生成、能下载。
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

    uname = f"patch_{int(time.time() * 1000)}"
    token, msg = get_user_store().register(uname, "test1234")
    assert token, f"注册失败：{msg}"
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def clean_graph(monkeypatch):
    from app.workflows.graph_manager import MANAGER

    keep_graph = MANAGER.current_graph()
    keep_version = MANAGER.version
    keep_hist = list(MANAGER._history)
    yield
    monkeypatch.setattr(MANAGER, "_graph", keep_graph)
    monkeypatch.setattr(MANAGER, "_version", keep_version)
    monkeypatch.setattr(MANAGER, "_history", keep_hist)


def _proposal(client, auth, ops, summary="补丁测试"):
    from app.workflows import architect, proposal_store as store
    from app.workflows.proposal import Proposal

    prop = Proposal(summary=summary, ops=ops, author="test")
    pv = architect.preview(prop)
    rec = {
        "id": prop.id, "summary": summary, "author": "test", "request": "测试",
        "ops": ops, "risks": ["测试风险"], "needs_second_confirm":
            prop.needs_second_confirm,
        "removes_nodes": prop.removes_nodes, "status": "pending_review",
        "preview": {k: v for k, v in pv.items() if k != "spec"},
        "_spec": pv.get("spec"), "events": [],
    }
    store.save(rec)
    return store.as_public(store.load(prop.id))


# 关掉 critics（含删节点，需二次确认），并补好连通性
OPS_REMOVE_CRITICS = [
    {"op": "remove_node", "key": "critics"},
    {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
    {"op": "set_branch", "from": "review_critics", "router": "simple_review",
     "branches": {"continue": "review_script", "redo": "screenwriter"}},
]


def test_approve_generates_patch(client, auth, clean_graph):
    """批准 → 换图 + **自动生成补丁**，两者都要有。"""
    p = _proposal(client, auth, OPS_REMOVE_CRITICS)
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"confirm_removals": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert body["proposal"]["patch_path"], "批准后没生成补丁路径"

    rec = client.get(f"/api/workflow/proposal/{p['id']}", headers=auth).json()["proposal"]
    events = [e["event"] for e in rec["events"]]
    assert "applied" in events
    assert "patch_generated" in events, f"没有留补丁生成的事件：{events}"


def test_patch_content_has_diff_and_spec(client, auth, clean_graph):
    """补丁要同时包含**人看的 diff** 与**机读的完整规格**。"""
    p = _proposal(client, auth, OPS_REMOVE_CRITICS)
    client.post(f"/api/workflow/proposal/{p['id']}/approve",
                headers=auth, json={"confirm_removals": True})

    r = client.get(f"/api/workflow/proposal/{p['id']}/patch", headers=auth)
    assert r.status_code == 200, r.text
    text = r.json()["patch"]

    # 头部：可追溯信息
    assert p["id"] in text
    assert "摘要" in text and "审批人" in text
    assert "如何应用" in text, "补丁要告诉人怎么用"
    # 变更日志
    assert "变更日志" in text
    assert "删除节点 critics" in text, f"变更日志没反映这次改动：{text[:600]}"
    # diff
    assert "@@" in text, "没有 unified diff 的 hunk"
    assert "critics" in text
    # 机读规格
    assert "SPEC" in text
    assert '"nodes"' in text and '"edges"' in text


def test_patch_diff_reflects_the_change_only(client, auth, clean_graph):
    """diff 必须**只反映这次改动** —— 所以规格要规范化（排序键）后再比。"""
    from app.workflows.patch import _canon
    from app.workflows import graph_spec as gs
    from app.workflows.proposal import apply_ops

    spec, _ = apply_ops(gs.default_spec(), OPS_REMOVE_CRITICS)
    before = _canon(gs.default_spec()).splitlines()
    after = _canon(spec).splitlines()

    import difflib

    diff = list(difflib.unified_diff(before, after, n=0))
    removed = [ln for ln in diff if ln.startswith("-") and not ln.startswith("---")]
    added = [ln for ln in diff if ln.startswith("+") and not ln.startswith("+++")]

    # 只应动到 critics 相关的若干行，不该把整份规格重写
    assert 0 < len(removed) < 40, f"删除行数不合理（{len(removed)}）—— 规格可能没规范化"
    assert 0 < len(added) < 40, f"新增行数不合理（{len(added)}）"
    assert any("critics" in ln for ln in removed + added)


def test_patch_generation_failure_does_not_undo_swap(client, auth, clean_graph,
                                                     monkeypatch):
    """**补丁失败不能回滚已经生效的换图** —— 两者必须解耦。

    用户已经批准并生效了；补丁只是"顺手落盘"。落盘失败不该把图弄回去。
    """
    from app.workflows import patch as patch_mod
    from app.workflows.graph_manager import MANAGER

    def boom(*a, **kw):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(patch_mod, "make_patch", boom)

    p = _proposal(client, auth, OPS_REMOVE_CRITICS)
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"confirm_removals": True})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True, "补丁失败把换图也弄失败了"
    assert "critics" not in MANAGER.current_graph().nodes

    rec = client.get(f"/api/workflow/proposal/{p['id']}", headers=auth).json()["proposal"]
    assert any(e["event"] == "patch_failed" for e in rec["events"]), \
        "补丁失败却没留痕"


def test_patch_can_be_regenerated(client, auth, clean_graph):
    """`regenerate=1` 能按当前规格重新出补丁（批准时只选"不生效"也能补）。"""
    p = _proposal(client, auth, OPS_REMOVE_CRITICS)
    # 只批准、不生效
    r = client.post(f"/api/workflow/proposal/{p['id']}/approve",
                    headers=auth, json={"apply": False,
                                        "confirm_removals": True})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is False

    r2 = client.get(f"/api/workflow/proposal/{p['id']}/patch?regenerate=1",
                    headers=auth)
    assert r2.status_code == 200, r2.text
    assert "删除节点 critics" in r2.json()["patch"]


def test_patch_download_is_text(client, auth, clean_graph):
    p = _proposal(client, auth, OPS_REMOVE_CRITICS)
    client.post(f"/api/workflow/proposal/{p['id']}/approve",
                headers=auth, json={"confirm_removals": True})
    r = client.get(f"/api/workflow/proposal/{p['id']}/patch/download", headers=auth)
    assert r.status_code == 200, r.text
    assert "text/plain" in r.headers.get("content-type", "")
    assert p["id"] in r.text


def test_patch_endpoints_require_auth(client):
    assert client.get("/api/workflow/proposal/x/patch").status_code in (401, 403)
    assert client.get("/api/workflow/proposal/x/patch/download").status_code in (401, 403)


def test_patch_for_unknown_proposal_is_404(client, auth):
    assert client.get("/api/workflow/proposal/p-nope/patch",
                      headers=auth).status_code == 404
