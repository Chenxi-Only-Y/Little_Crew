"""提案机制的测试 —— 用户要的「每次参与执行需审核」＋「删节点需二次确认」。

重点守住四件事：

  1. **封闭的操作集合** —— 提案里出现没想过的操作必须被拒绝，
     不能"顺手执行"；
  2. **两道校验缺一不可** —— 操作本身合法（`validate_ops`）
     ＋ 改完的图能跑（`graph_spec.validate_spec`）；
  3. **删节点必须触发二次确认**（用户明确要求）；
  4. **LLM 输出格式脏也要能用** —— 包了 markdown、多说了话都不该让用户
     看到"没反应"；真不合法的操作要给**人话原因**。
"""

from __future__ import annotations

import pytest

from app.workflows import graph_spec as gs
from app.workflows import proposal as P


# ------------------------------------------------------------------
# 操作集合是封闭的
# ------------------------------------------------------------------

def test_unknown_op_is_rejected():
    probs = P.validate_ops([{"op": "run_arbitrary_code", "code": "os.system('x')"}])
    assert probs, "未知操作居然通过了校验"
    assert "不支持的操作" in probs[0].message


def test_add_node_referencing_unimplemented_fn_is_rejected():
    """新增"真实业务环节"不在 A2 能力内 —— 必须被拦住并说清为什么。"""
    probs = P.validate_ops([{
        "op": "add_node", "key": "subtitle_gen", "kind": "business",
        "fn": "node_subtitle_gen",
    }])
    assert probs
    assert "未实现" in probs[0].message
    assert "先写代码" in probs[0].message


def test_add_review_node_is_allowed():
    """新增**确认点**是允许的（`_review` 是通用实现，不需要新写业务代码）。"""
    probs = P.validate_ops([{
        "op": "add_node", "key": "review_budget", "kind": "review",
    }])
    # `review_budget` 没有对应实现函数，所以 `add_node` 本身合法，
    # 但搭图时会被 build_graph 拒绝 —— 这是**两道校验**的分工。
    assert probs == [], [p.message for p in probs]

    with pytest.raises(ValueError):
        from app.workflows.main_workflow import build_graph

        spec, _ = P.apply_ops(gs.default_spec(), [{
            "op": "add_node", "key": "review_budget", "kind": "review"}])
        build_graph(spec)


def test_bad_router_rejected():
    probs = P.validate_ops([{
        "op": "set_branch", "from": "review_critics",
        "router": "no_such_router", "branches": {"continue": "review_script"},
    }])
    assert probs and "未知路由" in probs[0].message


def test_bad_branch_value_rejected():
    probs = P.validate_ops([{
        "op": "set_branch", "from": "review_rhythm",
        "router": "simple_review", "branches": {"back": "storyboarder"},
    }])
    assert probs and "没有返回值" in probs[0].message


# ------------------------------------------------------------------
# 删节点必须二次确认（用户的明确要求）
# ------------------------------------------------------------------

def test_removing_a_node_requires_second_confirm():
    p = P.Proposal(summary="精简流程", ops=[
        {"op": "remove_node", "key": "critics", "why": "剧评这步不需要"},
    ])
    assert p.removes_nodes == ["critics"]
    assert p.needs_second_confirm is True


def test_non_destructive_proposal_needs_no_second_confirm():
    p = P.Proposal(summary="加一个确认点", ops=[
        {"op": "add_node", "key": "review_x", "kind": "review"},
        {"op": "set_edge", "from": "a", "to": "b"},
    ])
    assert p.removes_nodes == []
    assert p.needs_second_confirm is False


# ------------------------------------------------------------------
# 应用 + 两道校验
# ------------------------------------------------------------------

def test_apply_switch_off_a_node_keeps_spec_valid():
    """**关掉一个节点**：应用后结构校验必须过（用户要的"开关现有节点"）。"""
    ops = [
        {"op": "remove_node", "key": "critics"},
        {"op": "set_edge", "from": "screenwriter", "to": "review_critics"},
        {"op": "set_branch", "from": "review_critics", "router": "simple_review",
         "branches": {"continue": "review_script", "redo": "screenwriter"}},
    ]
    assert P.validate_ops(ops) == []
    spec, log = P.apply_ops(gs.default_spec(), ops)
    errs = gs.errors_of(gs.validate_spec(spec))
    assert not errs, [e.message for e in errs]
    assert any("删除节点 critics" in x for x in log)

    from app.workflows.main_workflow import build_graph

    g = build_graph(spec)
    assert "critics" not in g.nodes


def test_apply_reorder_edges_keeps_spec_valid():
    """**重排连线**：应用后结构校验必须过（用户要的"重排"）。"""
    ops = [
        {"op": "remove_edge", "from": "character_designer", "to": "review_assets"},
        {"op": "set_edge", "from": "character_designer", "to": "storyboarder"},
        {"op": "remove_edge", "from": "storyboarder", "to": "review_storyboard"},
        {"op": "set_edge", "from": "storyboarder", "to": "review_assets"},
        {"op": "set_branch", "from": "review_assets", "router": "review_assets",
         "branches": {"continue": "review_storyboard", "redo": "character_designer",
                      "back": "screenwriter"}},
    ]
    assert P.validate_ops(ops) == []
    spec, _ = P.apply_ops(gs.default_spec(), ops)
    errs = gs.errors_of(gs.validate_spec(spec))
    assert not errs, [e.message for e in errs]

    from app.workflows.main_workflow import build_graph

    g = build_graph(spec)
    assert ("character_designer", "storyboarder") in set(g.edges)


def test_apply_does_not_mutate_original_spec():
    """应用必须打在**副本**上 —— 否则"提案还没审核"就把生产图改了。"""
    base = gs.default_spec()
    snapshot = repr(base)
    P.apply_ops(base, [{"op": "remove_node", "key": "critics"}])
    assert repr(base) == snapshot, "原规格被改动了"


def test_deleting_a_node_upstream_makes_downstream_unreachable():
    """删掉一个上游节点 → 下游从入口**走不到** → 结构校验必须报出来。

    这演示了**两道校验缺一不可**：`remove_node` 操作本身合法
    （而且 `apply_ops` 会正确清掉连到它的边与分支，不留悬空引用），
    但改完的图里会出现一个**再也进不去的死环节**。

    ⚠️ 这正是我补"可达性检查"的由来：原来的入边检查**漏掉这种情况** ——
    `quality_assessor` 自己也是 `retry` 的起点，所以"有没有入边"判它没问题，
    可从入口已经走不到它了。
    """
    ops = [{"op": "remove_node", "key": "image_generator"}]
    assert P.validate_ops(ops) == []          # 操作本身没问题
    spec, _ = P.apply_ops(gs.default_spec(), ops)

    # ① 不留悬空引用：没有边/分支还指向被删的节点
    assert not [e for e in spec["edges"]
                if "image_generator" in (e["from"], e["to"])]
    assert not [1 for e in spec["conditional"]
                for v in (e.get("branches") or {}).values()
                if v == "image_generator"]

    # ② 但图已经坏了：下游走不到
    errs = gs.errors_of(gs.validate_spec(spec))
    assert errs, "下游已经走不到了，结构校验居然通过"
    assert any("走不到" in e.message for e in errs), [e.message for e in errs]
    assert any(e.where in ("quality_assessor", "review_quality", "review_images")
               for e in errs), [e.where for e in errs]


def test_reachability_check_catches_orphaned_cycle():
    """自环/环里的节点如果从入口进不去，也要被报出来。"""
    spec = gs.clone_spec(gs.default_spec())
    # 把 story_planner 的入边来源（START）保住，但把 critics 整条链切断：
    # 让 screenwriter 直接进 review_script，critics 变成孤立环
    spec["edges"] = [e for e in spec["edges"]
                     if not (e["from"] == "screenwriter" and e["to"] == "critics")]
    spec["edges"].append({"from": "critics", "to": "critics"})
    errs = gs.errors_of(gs.validate_spec(spec))
    assert any("走不到" in e.message for e in errs), [e.message for e in errs]


# ------------------------------------------------------------------
# LLM 输出解析：脏格式也要能用
# ------------------------------------------------------------------

def test_parse_markdown_fenced_json():
    text = """好的，我建议这样改：

```json
{"summary": "加一个预算确认点",
 "ops": [{"op": "set_edge", "from": "critics", "to": "review_critics"}],
 "risks": ["会增加一次人工确认"]}
```
"""
    p = P.parse_proposal(text, author="mock")
    assert p.summary == "加一个预算确认点"
    assert len(p.ops) == 1
    assert p.risks == ["会增加一次人工确认"]


def test_parse_json_with_surrounding_prose():
    text = '我先说结论：{"summary": "只重排", "ops": []} 就这些。'
    p = P.parse_proposal(text)
    assert p.summary == "只重排"


def test_parse_garbage_does_not_raise():
    """模型胡说时不能抛异常 —— 要给出"无法应用"的提案，让上层如实告诉用户。"""
    p = P.parse_proposal("我觉得这个工作流挺好的，不需要改。")
    assert p.ops == []
    assert p.risks, "没解析出 JSON 时必须给出原因"


def test_parse_non_object_json():
    p = P.parse_proposal("[1, 2, 3]")
    assert p.ops == []
    assert p.risks


def test_parse_tolerates_string_risks():
    p = P.parse_proposal('{"summary": "x", "ops": [], "risks": "只有一个风险"}')
    assert p.risks == ["只有一个风险"]


def test_proposal_as_dict_shape():
    p = P.Proposal(summary="s", ops=[{"op": "set_entry", "key": "story_planner"}])
    d = p.as_dict()
    for k in ("id", "summary", "ops", "risks", "removes_nodes",
              "needs_second_confirm"):
        assert k in d
    assert d["needs_second_confirm"] is False
