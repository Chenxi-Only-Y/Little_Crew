"""注册表回归测试（P0 护栏之一）。

用途：重构期间保证注册表始终是自洽的唯一数据源。
覆盖：三层结构完整性、依赖可推导、拓扑校验（正例/反例）、快照可序列化。

运行：
    pytest tests/test_registry.py -q
"""

from __future__ import annotations

import json

import pytest

from app.registry import api


# ------------------------------------------------------------------
# 结构完整性
# ------------------------------------------------------------------

def test_scale():
    """部门 7 / 职业 19 / 节点含系统节点 / 产物与确认点非空。

    职业数变化史：
        16 → **19**（2026-10-07，用户"缺的 agent 也补上"）：
        `inpainter` 修图师、`motion_compositor` 动态合成师、`video_assessor` 视频质检师。
        同日「质检总监」→「图像质检师」是**改名**（key 不变），不改变数量。
        「美术总监·一致性审查」是**系统节点**（没有独立 Role），它被删除后
        职业数不变（19），系统节点数从 2 变成 **1**（只剩美术设定包解析）。
    """
    assert len(api.all_departments()) == 7
    assert len(api.all_roles()) == 19
    assert len(api.all_nodes()) == len(api.all_roles()) + 1  # +1 个系统节点（美术设定包解析）
    assert len(api.snapshot()["artifacts"]) > 20
    assert len(api.all_checkpoints()) >= 9


def test_role_keys_unique():
    keys = [r.key for r in api.all_roles()]
    assert len(keys) == len(set(keys)), f"职业 key 重复：{keys}"


def test_node_keys_unique():
    keys = [n.key for n in api.all_nodes()]
    assert len(keys) == len(set(keys)), f"节点 key 重复：{keys}"


def test_every_role_dept_exists():
    """每个职业的部门必须存在（防止部门改名后悬空）。"""
    for r in api.all_roles():
        assert api.department(r.dept) is not None, f"{r.key} 指向不存在的部门 {r.dept}"


def test_every_role_node_exists():
    """每个职业的主节点必须在节点表里。"""
    for r in api.all_roles():
        assert api.node(r.node) is not None, f"{r.key} 指向不存在的节点 {r.node}"


def test_every_panel_binds_existing_node():
    for r in api.all_roles():
        for p in r.panels:
            assert api.node(p.node) is not None, f"面板 {p.key} 绑定不存在的节点 {p.node}"
        assert r.main_panel is not None, f"{r.key} 没有面板"


def test_every_checkpoint_binds_existing_node():
    for c in api.all_checkpoints():
        assert api.node(c.node) is not None, f"确认点 {c.key} 绑定不存在的节点 {c.node}"


def test_all_ports_are_declared_artifacts():
    """节点的 produces/consumes 必须都是已声明的产物（防止拼写错误）。"""
    for n in api.all_nodes():
        for a in (*n.produces, *n.consumes):
            assert api.artifact(a) is not None, f"节点 {n.key} 引用了未声明的产物 {a}"


def test_config_keys_include_role_key():
    """per-role 配置字段的键名必须以该节点的**配置命名空间**开头。

    命名空间 = **运行时实际读取的那个 role**（`Node.role_namespace`），
    而**不是**节点 key。二者对多数节点相同，但对少数不同，例如：
        节点 technical_director  →  运行时 role 是 "director"
        所以键必须是 `llm_provider_director`（Agent 读的就是它），
        若按节点 key 生成 `llm_provider_technical_director`，**界面改了运行不生效**。

    这条守的是 v1 踩过的同类坑：两套命名并存导致"设置页写入的键 ≠ 运行读取的键"。
    """
    #: 允许的例外：**该节点没有独立职业**时（如质检的"崩坏修复"），
    #: 用能力名做命名空间，避免与真正拥有该职业的节点（摄影师）抢同一个键。
    allowed_exceptions = {
        "image_provider_image_edit": "质检的崩坏修复：无独立职业，用能力名避让",
    }
    for n in api.all_nodes():
        for c in n.config:
            if c.scope != "role":
                continue
            if c.key in allowed_exceptions:
                continue
            assert n.role_namespace in c.key, (
                f"节点 {n.key}（配置命名空间 {n.role_namespace}）的 role 级配置键 "
                f"{c.key} 未包含该命名空间，会导致界面写入的键与运行读取的键不一致"
            )


# ------------------------------------------------------------------
# 依赖与顺序
# ------------------------------------------------------------------

def test_execution_order_derivable():
    """执行顺序可由依赖推导（不依赖硬编码阶段表）。"""
    order = api.execution_order()
    assert len(order) == len(api.all_nodes())
    assert set(order) == {n.key for n in api.all_nodes()}


def test_dependencies_precede_consumers():
    """每个节点都排在其依赖的生产者之后。"""
    order = api.execution_order()
    pos = {k: i for i, k in enumerate(order)}
    for n in api.all_nodes():
        for a in n.consumes:
            for producer in api.all_nodes():
                if a in producer.produces and producer.key != n.key:
                    assert pos[producer.key] < pos[n.key], (
                        f"{producer.key} 产出 {a}，应排在消费它的 {n.key} 之前"
                    )


# ------------------------------------------------------------------
# 拓扑校验
# ------------------------------------------------------------------

def test_validate_full_chain_ok():
    """从策划到生图的完整链路应校验通过。"""
    nodes = [k for k in api.execution_order() if api.node(k).dept != "post"]
    res = api.validate_topology(nodes, available_inputs=["user_input", "art_brief"])
    assert res["ok"], f"完整链路被误判为非法：{res['errors']}"


def test_validate_missing_dependency():
    """只选下游节点应报缺失依赖。"""
    res = api.validate_topology(["photographer"], available_inputs=[])
    assert not res["ok"]
    assert any(e["code"] == "missing_dependency" for e in res["errors"])


def test_validate_incompatible_edge():
    """端口不兼容的连线应被拒绝。"""
    res = api.validate_topology(
        ["screenwriter", "photographer"],
        edges=[("screenwriter", "photographer")],
        available_inputs=["user_input", "art_brief"],
    )
    assert not res["ok"]
    assert any(e["code"] == "incompatible_edge" for e in res["errors"])


def test_validate_compatible_edge_ok():
    """端口兼容的连线应通过。

    用「依赖已闭合」的真实子集：分镜师 -> 摄影师 是合法连线（storyboard 可被消费）。
    子集取自分镜师之前的所有节点（保证依赖闭合），显式只连这一条边。
    """
    order = api.execution_order()
    upto = order[:order.index("photographer") + 1]
    # 只保留依赖闭合的生成链（去掉不属于该链的系统节点不影响闭合性）
    nodes = [k for k in upto if k != "workflow_architect"]
    res = api.validate_topology(
        nodes,
        edges=[("storyboarder", "photographer")],
        available_inputs=["user_input", "art_brief"],
    )
    assert res["ok"], f"合法连线被误判：{res['errors']}"


def test_validate_compatible_edge_requires_closed_deps():
    """仅连一条合法边但依赖不闭合时，仍应报缺失依赖（证明校验是完整的）。"""
    res = api.validate_topology(
        ["storyboarder", "photographer"],
        edges=[("storyboarder", "photographer")],
        available_inputs=["user_input", "art_brief"],
    )
    assert not res["ok"]
    assert all(e["code"] == "missing_dependency" for e in res["errors"])


def test_validate_unknown_node():
    res = api.validate_topology(["not_a_node"], available_inputs=[])
    assert not res["ok"]
    assert any(e["code"] == "unknown_node" for e in res["errors"])


# ------------------------------------------------------------------
# 快照
# ------------------------------------------------------------------

def test_snapshot_serializable():
    """快照必须可 JSON 序列化（供 GET /api/registry 直接返回）。"""
    snap = api.snapshot()
    s = json.dumps(snap, ensure_ascii=False)
    assert len(s) > 1000
    for key in ("departments", "artifacts", "roles", "nodes", "checkpoints", "execution_order"):
        assert key in snap


def test_snapshot_has_no_duplicate_role_keys():
    snap = api.snapshot()
    keys = [r["key"] for r in snap["roles"]]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("legacy,expected", [
    ("writer", "showrunner"),
    ("story_planner", "showrunner"),
    ("critics", "script_editor"),
    ("director", "technical_director"),
    ("rhythm_director", "supervising_editor"),
    ("continuity_checker", "continuity"),
    ("image_generator", "photographer"),
    ("aggregator", "editor"),
])
def test_legacy_key_lookup(legacy, expected):
    """旧命名应能反查到新职业（迁移期兼容）。"""
    r = api.role(legacy)
    assert r is not None, f"旧 key {legacy} 无法反查"
    assert r.key == expected


# ------------------------------------------------------------------
# 校验规格（供前端预校验复用同一份规则）
# ------------------------------------------------------------------

def test_validation_spec_shape():
    """规格必须包含前端做校验所需的全部数据。"""
    spec = api.validation_spec()
    for key in ("artifacts", "nodes", "compatible_kinds", "external_inputs"):
        assert key in spec, f"validation_spec 缺 {key}"

    assert set(spec["nodes"]) == {n.key for n in api.all_nodes()}
    for node_key, info in spec["nodes"].items():
        assert set(info) >= {"label", "produces", "consumes", "can_skip"}
        assert info["label"], f"{node_key} 缺 label"

    assert "user_input" in spec["external_inputs"]


def test_validation_spec_in_snapshot():
    """快照里必须带上规格（供离线开发的前端使用）。"""
    snap = api.snapshot()
    assert "validation_spec" in snap
    assert snap["validation_spec"]["nodes"]


def test_validation_spec_kinds_reference_real_artifacts():
    """兼容矩阵引用的形态必须是真实存在的产物形态。"""
    spec = api.validation_spec()
    real_kinds = {a["kind"] for a in spec["artifacts"].values()}
    for src_kind, targets in spec["compatible_kinds"].items():
        assert src_kind in real_kinds, f"兼容矩阵的键 {src_kind} 不是真实形态"
        for t in targets:
            assert t in real_kinds, f"兼容矩阵的值 {t} 不是真实形态"


# ------------------------------------------------------------------
# 公开端点（前端在登录前就要能渲染）
# ------------------------------------------------------------------

def test_registry_endpoint_is_public():
    """`/api/registry` 必须公开：它是纯静态 schema，前端未登录时也要能渲染画布。"""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/api/registry")
    assert r.status_code == 200, f"注册表端点未公开：{r.status_code}"
    body = r.json()
    assert body["status"] == "ok"
    assert body["registry"]["roles"]
    assert body["registry"]["validation_spec"]


def test_providers_endpoint_is_public():
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    r = c.get("/api/providers")
    assert r.status_code == 200
    assert r.json()["count"] > 0


def test_validate_endpoint_matches_spec_rules():
    """`/api/registry/validate` 与本地 `validate_topology` 结果应一致。"""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    res = c.post("/api/registry/validate", json={
        "nodes": ["photographer"], "available_inputs": [],
    }).json()
    local = api.validate_topology(["photographer"], available_inputs=[])
    assert res["ok"] == local["ok"]
    assert len(res["errors"]) == len(local["errors"])
