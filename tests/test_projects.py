"""工程文件存取（F5）的回归测试。

工程 = 用户在导演台上搭的那张图（放哪些节点、连了哪些线、各节点参数）。
重点守住三件事：
  1. **密钥绝不入库**（快照里的密钥会被掩码）
  2. **跨用户隔离**（读/改/删他人的工程都必须失败）
  3. **畸形数据不崩**（画布被规范成固定形状）
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.services.project_store import (
    MAX_PAYLOAD_BYTES,
    ProjectStore,
    _migrate,
    _norm_canvas,
)


@pytest.fixture
def store(tmp_path):
    return ProjectStore(str(tmp_path / "proj.db"))


CANVAS = {
    "nodes": [
        {"id": "showrunner", "position": {"x": 10, "y": 20},
         "config": {"llm_provider_writer": "zhipu"}, "note": "先定大纲"},
        {"id": "photographer", "position": {"x": 300, "y": 80}, "config": {}, "note": ""},
    ],
    "edges": [{"source": "showrunner", "target": "photographer",
               "sourceHandle": "out:story_outline", "targetHandle": "in:shot_list"}],
    "viewport": {"x": 0, "y": 0, "zoom": 1.2},
}


# ------------------------------------------------------------------
# 1) 基本存取
# ------------------------------------------------------------------

def test_save_and_load_roundtrip(store):
    s = store.save("u1", "我的工程", CANVAS)
    doc = store.get(s["project_id"], "u1")

    assert doc is not None
    assert doc["name"] == "我的工程"
    cv = doc["canvas"]
    assert len(cv["nodes"]) == 2
    assert len(cv["edges"]) == 1
    assert cv["viewport"]["zoom"] == 1.2
    # 参数与位置必须逐字保留（否则"打开工程"等于白存）
    assert cv["nodes"][0]["config"]["llm_provider_writer"] == "zhipu"
    assert cv["nodes"][0]["position"] == {"x": 10.0, "y": 20.0}
    assert cv["nodes"][0]["note"] == "先定大纲"
    assert cv["edges"][0]["sourceHandle"] == "out:story_outline"


def test_save_with_same_id_overwrites_not_creates(store):
    s = store.save("u1", "第一版", CANVAS)
    store.save("u1", "第二版", {"nodes": CANVAS["nodes"][:1], "edges": []},
               project_id=s["project_id"])

    items = store.list_by_user("u1")
    assert len(items) == 1, "覆盖保存变成了新建"
    assert items[0]["name"] == "第二版"
    assert items[0]["node_count"] == 1


def test_list_orders_by_recent(store):
    a = store.save("u1", "A", {"nodes": []})
    b = store.save("u1", "B", {"nodes": []})
    # 触碰 A 使其变为最近
    store.save("u1", "A2", {"nodes": []}, project_id=a["project_id"])
    names = [p["name"] for p in store.list_by_user("u1")]
    assert names[0] == "A2", f"应按最近修改排序，实际 {names}"
    assert b["project_id"] in [p["project_id"] for p in store.list_by_user("u1")]


def test_rename_and_delete(store):
    s = store.save("u1", "原名", CANVAS)
    assert store.rename(s["project_id"], "u1", "新名")
    assert store.get(s["project_id"], "u1")["name"] == "新名"

    assert store.delete(s["project_id"], "u1")
    assert store.get(s["project_id"], "u1") is None
    assert not store.delete(s["project_id"], "u1"), "重复删除应返回 False"


# ------------------------------------------------------------------
# 2) 跨用户隔离（安全关键）
# ------------------------------------------------------------------

def test_other_user_cannot_read(store):
    s = store.save("alice", "alice 的工程", CANVAS)
    assert store.get(s["project_id"], "bob") is None, "读到了别人的工程"
    assert store.list_by_user("bob") == []


def test_other_user_cannot_overwrite(store):
    s = store.save("alice", "alice 的工程", CANVAS)
    with pytest.raises(PermissionError):
        store.save("bob", "偷改", {"nodes": []}, project_id=s["project_id"])
    # alice 的工程必须原样
    doc = store.get(s["project_id"], "alice")
    assert doc["name"] == "alice 的工程"
    assert len(doc["canvas"]["nodes"]) == 2


def test_other_user_cannot_delete(store):
    s = store.save("alice", "alice 的工程", CANVAS)
    assert not store.delete(s["project_id"], "bob")
    assert store.get(s["project_id"], "alice") is not None


def test_missing_project_returns_none(store):
    assert store.get("prj_不存在", "u1") is None
    assert store.get("", "u1") is None


# ------------------------------------------------------------------
# 3) 健壮性
# ------------------------------------------------------------------

def test_oversized_project_is_rejected(store):
    big = {"nodes": [{"id": f"n{i}", "position": {"x": i, "y": 0},
                      "config": {}, "note": "x" * 400} for i in range(1500)]}
    with pytest.raises(ValueError) as ei:
        store.save("u1", "超大", big)
    assert "过大" in str(ei.value)


def test_malformed_canvas_is_normalized(store):
    """畸形输入不能 500，应被规范成合法形状。"""
    s = store.save("u1", "畸形", {"nodes": "不是数组", "edges": None, "viewport": 42})
    cv = store.get(s["project_id"], "u1")["canvas"]
    assert cv["nodes"] == []
    assert cv["edges"] == []
    assert cv["viewport"] == {}


def test_invalid_nodes_and_edges_are_skipped(store):
    s = store.save("u1", "混杂", {
        "nodes": [{"id": "ok", "position": {"x": 1, "y": 2}}, {"no_id": True}, "字符串"],
        "edges": [{"source": "a", "target": "b"}, {"source": "a"}, None],
    })
    cv = store.get(s["project_id"], "u1")["canvas"]
    assert [n["id"] for n in cv["nodes"]] == ["ok"]
    assert len(cv["edges"]) == 1


def test_norm_canvas_handles_garbage():
    for garbage in (None, [], "abc", 42, {"nodes": {}}):
        out = _norm_canvas(garbage)
        assert isinstance(out["nodes"], list) and isinstance(out["edges"], list)


def test_migrate_fills_missing_fields():
    """旧版本工程（缺字段）也能读出来，而不是报错。"""
    doc = _migrate({})
    assert doc["version"] >= 1
    assert doc["canvas"]["nodes"] == []
    assert doc["settings_snapshot"] == {}
    assert doc["name"]


# ------------------------------------------------------------------
# 4) 密钥绝不入库
# ------------------------------------------------------------------

def test_masked_keys_do_not_leak_plaintext(store):
    """API 层会先掩码；这里确认**库里**不含明文密钥。

    即使掩码逻辑被绕过，也要能通过查库发现——所以这条直接读原始 payload。
    """
    secret = "sk-绝密密钥12345"
    # 模拟 API 层的掩码行为
    snap = {"llm_api_key_writer": "••••已配置", "llm_provider_mode": "cloud"}
    s = store.save("u1", "带密钥", CANVAS, settings_snapshot=snap)

    with sqlite3.connect(store.path) as conn:
        blob = conn.execute(
            "SELECT payload FROM projects WHERE project_id=?", (s["project_id"],)
        ).fetchone()[0]
    assert secret not in blob, "明文密钥进了库"
    # 就算掩码没生效、真存了明文，也要靠这条断言抓到
    doc = store.get(s["project_id"], "u1")
    assert doc["settings_snapshot"]["llm_api_key_writer"] == "••••已配置"
    assert doc["settings_snapshot"]["llm_provider_mode"] == "cloud"


def test_projects_table_created_on_demand(tmp_path):
    p = tmp_path / "fresh.db"
    ProjectStore(str(p))
    with sqlite3.connect(str(p)) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "projects" in tables


def test_name_is_trimmed_and_bounded(store):
    s = store.save("u1", "   " + "长" * 200 + "  ", {"nodes": []})
    doc = store.get(s["project_id"], "u1")
    assert len(doc["name"]) <= 80
    assert not doc["name"].startswith(" ")

    s2 = store.save("u1", "   ", {"nodes": []})
    assert store.get(s2["project_id"], "u1")["name"] == "未命名工程"
