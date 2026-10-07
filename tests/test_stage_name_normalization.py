"""`start_stage` / `end_stage` 的**命名归一**与**未知值报错**。

## 为什么需要这些测试

用户看到的是**画布/注册表上的名字**，而 `_STAGE_ORDER` 用的是**算法阶段名**
—— 两套命名在历史上分化了：

    画布上叫               算法阶段名
    showrunner        →  writer
    script_editor     →  critics
    technical_director →  director
    photographer      →  photographer   （恰好同名）
    editor            →  aggregator

实测（2026-10-07）：传 `end_stage="showrunner"` **会被静默忽略** ——
`_skipped_by_scope` 判"不在表里"就返回 False → **整条流程照跑**。
用户以为设了终点，其实没有，**还照样花钱**。这是最糟的失败方式。

修法：`_normalize_stage()` 归一两套命名；认不出的用
`unknown_scope_values()` 报出来，别装没看见。
"""

from __future__ import annotations

import pytest

from app.workflows.main_workflow import (
    _STAGE_ALIASES,
    _STAGE_ORDER,
    _normalize_stage,
    _skipped_by_scope,
    unknown_scope_values,
)


# ----------------------------------------------------------------------
# 归一
# ----------------------------------------------------------------------

@pytest.mark.parametrize("given,expect", [
    # 画布/注册表名 → 算法阶段名
    ("showrunner", "writer"),
    ("script_editor", "critics"),
    ("technical_director", "director"),
    ("photographer", "photographer"),
    ("editor", "aggregator"),
    ("continuity", "continuity_checker"),
    ("supervising_editor", "rhythm_director"),
    # 图里的节点名也认
    ("story_planner", "writer"),
    ("prompt_compiler", "director"),
    ("image_generator", "photographer"),
    # 本来就一致的
    ("screenwriter", "screenwriter"),
    ("character_designer", "character_designer"),
    # 认不出的 → 原样（调用方据此报错）
    ("storyborder", "storyborder"),
    ("", ""),
])
def test_normalize_stage(given, expect):
    assert _normalize_stage(given) == expect


def test_all_aliases_point_to_real_stages():
    """别名表的**目标**必须都是合法阶段名，否则归一等于没做。"""
    for k, v in _STAGE_ALIASES.items():
        assert v in _STAGE_ORDER, f"别名 {k} → {v} 不在 _STAGE_ORDER 里"


def test_every_execution_role_maps_to_a_stage():
    """注册表里**在执行链上**的职业都该能归一到一个阶段。

    不能归一就意味着：用户在设置里选了那个名字当终点，会被静默忽略。

    ⚠️ 排除 `workflow_architect` —— 它是**工具**（导演台的架构师抽屉），
    它的 Node 的 `workflow_node` 为空、本就不该是执行步骤，所以没有阶段是对的。
    （`workflow_node` 在 **Node** 上，不在 Role 上。）
    """
    from app.registry.api import all_nodes, all_roles

    by_node = {n.key: n for n in all_nodes()}
    missing = []
    for r in all_roles():
        n = by_node.get(r.node)
        if not (getattr(n, "workflow_node", "") or ""):
            continue          # 不在执行链上（工具），无需阶段
        if _normalize_stage(r.key) not in _STAGE_ORDER:
            missing.append(r.key)
    assert not missing, f"这些职业名无法归一成阶段（设了会被静默忽略）：{missing}"


def test_workflow_architect_is_the_only_non_execution_role():
    """钉住上一条的排除理由，防止有人给工具硬塞一个阶段。"""
    from app.registry.api import all_nodes

    no_node = [n.key for n in all_nodes() if not (getattr(n, "workflow_node", "") or "")]
    assert no_node == ["workflow_architect"], (
        f"不在执行链上的节点变了：{no_node} —— 若是新工具，请同步上一条测试的说明")


# ----------------------------------------------------------------------
# 区间跳过：用画布名也能生效
# ----------------------------------------------------------------------

@pytest.mark.parametrize("end,stage,expect", [
    # end_stage 之后的环节要跳过；它自己与更早的不跳
    ("showrunner", "writer", False),              # 归一成 writer，就是它自己
    ("showrunner", "screenwriter", True),         # screenwriter 在 writer 之后
    ("photographer", "writer", False),            # writer 在摄影师之前
    ("photographer", "quality_assessor", True),   # 质检在摄影师之后
    ("technical_director", "photographer", True),  # 摄影师在技术指导之后
    ("editor", "quality_assessor", False),        # 质检在聚合之前
    ("editor", "aggregator", False),              # 聚合就是它自己
])
def test_scope_works_with_canvas_names(end, stage, expect):
    """**核心**：传画布上的名字也要真的生效（原先静默忽略）。"""
    assert _skipped_by_scope({"end_stage": end}, stage) is expect


@pytest.mark.parametrize("start,stage,expect", [
    ("photographer", "writer", True),             # start 之前 → 跳过
    ("photographer", "photographer", False),      # 自己 → 不跳
    ("photographer", "aggregator", False),        # 之后 → 不跳
    ("showrunner", "screenwriter", False),        # showrunner=writer，之后不跳
])
def test_start_scope_works_with_canvas_names(start, stage, expect):
    assert _skipped_by_scope({"start_stage": start}, stage) is expect


# ----------------------------------------------------------------------
# 未知值要能被报出来（不再静默忽略）
# ----------------------------------------------------------------------

def test_unknown_values_are_reported():
    """拼错的名字必须是"可被报出来的"，而不是静默放过。"""
    assert unknown_scope_values({"end_stage": "storyborder"}) == ["end_stage=storyborder"]
    assert unknown_scope_values({"start_stage": "不存在"}) == ["start_stage=不存在"]
    assert unknown_scope_values({"start_stage": "x", "end_stage": "y"}) == [
        "start_stage=x", "end_stage=y"]


def test_known_values_are_not_reported():
    """画布名与算法阶段名都算"认识"，不该误报。"""
    for ok in ("showrunner", "photographer", "editor", "writer", "aggregator",
               "technical_director"):
        assert unknown_scope_values({"end_stage": ok}) == [], f"{ok} 被误报为不认识"


def test_no_scope_set_reports_nothing():
    assert unknown_scope_values({}) == []
    assert unknown_scope_values({"start_stage": "", "end_stage": None}) == []
