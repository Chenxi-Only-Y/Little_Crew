"""职业捆绑组与「美术总监·一致性审查」删除的回归测试。

用户 2026-10-07 的四条要求：
  1. 策划类捆绑一起   → `planning`
  2. 图片类捆绑一起   → `imagery`
  3. 视频类捆绑一起   → `video`
  4. 场记 + 技术指导捆绑 → `continuity`
  5. 「美术总监·一致性审查」整个环节去掉，职责归图像质检师
"""

from __future__ import annotations

import pytest

EXPECTED = {
    "planning": {"showrunner", "script_editor", "screenwriter", "storyboarder"},
    "imagery": {"photographer", "quality_assessor", "inpainter"},
    "video": {"motion_compositor", "video_assessor", "editor"},
    "continuity": {"continuity", "technical_director"},
}


# ----------------------------------------------------------------------
# 捆绑组
# ----------------------------------------------------------------------

def test_bundles_match_what_the_user_specified():
    """四个捆绑组的成员必须与用户点名的一致。"""
    from app.registry.roles import BUNDLES

    got = {bkey: set(keys) for bkey, _label, keys in BUNDLES}
    assert got == EXPECTED, f"捆绑组不符：\n  期望 {EXPECTED}\n  实际 {got}"


def test_every_bundled_role_is_tagged():
    """组的成员在 `Role.bundle` 上必须打对标记（两处数据不能打架）。"""
    from app.registry.roles import BUNDLES, ROLES

    by_key = {r.key: r for r in ROLES}
    for bkey, label, keys in BUNDLES:
        for k in keys:
            r = by_key.get(k)
            assert r is not None, f"捆绑组「{label}」引用了不存在的职业 {k}"
            assert r.bundle == bkey, (
                f"{r.label} 在 BUNDLES 里属于 {bkey}，但 Role.bundle={r.bundle!r}")


def test_roles_not_in_any_bundle_have_empty_tag():
    """不在任何组里的职业，`bundle` 必须是空串（别误打标记）。"""
    from app.registry.roles import BUNDLE_BY_ROLE, ROLES

    for r in ROLES:
        if r.key not in BUNDLE_BY_ROLE:
            assert r.bundle == "", f"{r.label} 不在任何捆绑组，却标了 {r.bundle!r}"


def test_no_role_is_in_two_bundles():
    from app.registry.roles import BUNDLES

    seen: dict[str, str] = {}
    for bkey, _label, keys in BUNDLES:
        for k in keys:
            assert k not in seen, f"{k} 同时属于 {seen.get(k)} 和 {bkey}"
            seen[k] = bkey


def test_bundles_are_in_the_frontend_snapshot():
    """快照必须带上捆绑组 —— 前端要靠它做"选一个带上一组"。"""
    import json
    import pathlib

    d = json.loads(pathlib.Path("web/src/registry.snapshot.json").read_text(encoding="utf-8"))
    assert "bundles" in d, "快照顶层没有 bundles"
    got = {b["key"]: set(b["roles"]) for b in d["bundles"]}
    assert got == EXPECTED, f"快照里的捆绑组不符：{got}"
    # 每个角色条目也要带 bundle 字段
    tagged = {r["key"] for r in d["roles"] if r.get("bundle")}
    assert tagged == set().union(*EXPECTED.values())


def test_bundles_are_not_in_the_validation_spec():
    """捆绑组是**使用约束**，不是拓扑校验数据 —— 别混进 validation_spec。"""
    import json
    import pathlib

    d = json.loads(pathlib.Path("web/src/registry.snapshot.json").read_text(encoding="utf-8"))
    assert "bundles" not in d.get("validation_spec", {}), (
        "bundles 混进 validation_spec 了 —— 那一段是校验用的")


def test_bundle_members_cross_departments():
    """至少有一组是**跨部门**的 —— 这正是捆绑组与部门不同的地方。"""
    from app.registry.roles import BUNDLES, ROLES

    by_key = {r.key: r for r in ROLES}
    cross = [
        bkey for bkey, _l, keys in BUNDLES
        if len({by_key[k].dept for k in keys if k in by_key}) > 1
    ]
    assert cross, "所有捆绑组都在单一部门内 —— 那和部门就重复了"
    # 「场记与技术指导」应当是跨部门的（动态 + 基建）
    assert "continuity" in cross


# ----------------------------------------------------------------------
# 「美术总监·一致性审查」已删除
# ----------------------------------------------------------------------

def test_consistency_auditor_is_gone_from_the_graph():
    from app.workflows.main_workflow import build_graph

    assert "consistency_auditor" not in build_graph().nodes


def test_consistency_auditor_is_gone_from_the_registry():
    from app.registry.api import all_nodes

    assert not [n for n in all_nodes() if n.key == "consistency_auditor"]


def test_designers_now_converge_at_asset_review():
    """汇聚点从已删除的美术总监移到「资产确认」。"""
    from app.workflows.main_workflow import build_graph

    edges = set(build_graph().edges)
    assert ("character_designer", "review_assets") in edges
    assert ("scene_designer", "review_assets") in edges


def test_quality_assessor_owns_the_consistency_outputs():
    """两个一致性产出归图像质检师（它本来就在算，原先只是**重复声明**）。"""
    from app.registry.api import all_nodes

    qa = next(n for n in all_nodes() if n.key == "quality_assessor")
    assert "consistency_score" in qa.produces
    assert "character_consistency_report" in qa.produces
    # 它要比对场景参考图（原由已删除的美术总监消费）
    assert "scene_reference_images" in qa.consumes


def test_consistency_agent_module_is_deleted():
    """那个 Agent 模块该删就删，别留孤儿文件。"""
    import pathlib

    assert not pathlib.Path("app/agents/stage2/consistency_auditor.py").exists()


def test_clip_helpers_moved_to_shared_module():
    """CLIP 辅助函数搬到了共享模块（删 Agent 时不能把还在用的东西一起删掉）。"""
    from app.services import clip_similarity

    assert hasattr(clip_similarity, "_try_load_clip")
    assert hasattr(clip_similarity, "_cosine_similarity")


def test_quality_assessor_no_longer_imports_the_deleted_agent():
    """图像质检师不能再 import 已删除的模块（否则会 ImportError）。"""
    import pathlib

    src = pathlib.Path("app/agents/quality_assessor.py").read_text(encoding="utf-8")
    assert "consistency_auditor" not in src.replace(
        "# 兼容 _assets_content / consistency_auditor 字段", ""), (
        "quality_assessor 仍在 import 已删除的 consistency_auditor")
