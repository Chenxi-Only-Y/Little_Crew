"""图规格的测试：**期望拓扑逐条写死**，与实际搭出来的图对比。

`build_graph()` 从"23 个 add_node + 41 个 add_edge 硬编码"改成"读 graph_spec"
之后，最大的风险是**搬数据时搬错一条边** —— 那会静默改变执行路径
（少一条出边 = 执行卡死；多一条 = 走错分支）。这个文件就是那层安全网。

⚠️ **它是有意会红的**：任何对工作流的有意改动都必须同步更新下面的期望值。
已经同步过一次 —— 把「白模预演师」正式接进执行链（用户要求
"把「预演」构成「白模预演师」一部分"）：

    节点 +1（previs_artist）
    边  +1（previs_artist → rhythm_director）
    review_storyboard 的 continue：rhythm_director → previs_artist

⚠️ 踩过的坑（写在测试里，免得下次又白查）：
    · `StateGraph.edges` 是 **`{(src, dst)}` 元组集合**，不是带
      `.source/.target` 的对象 —— 按属性读会得到"0 条边"的假象；
    · **条件边不在 `edges` 里**，它们在 `branches`（`BranchSpec.ends`）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 期望拓扑（基线：重构前那份硬编码图；此后每处有意改动都在此同步）
# ---------------------------------------------------------------------------
EXPECTED_NODES = {
    "story_planner", "screenwriter", "critics", "review_script",
    "art_brief_parser", "character_designer", "scene_designer",
    "consistency_auditor", "review_assets", "storyboarder",
    "review_storyboard", "previs_artist", "rhythm_director", "continuity_checker",
    "prompt_compiler", "image_generator", "quality_assessor",
    "review_images", "review_critics", "review_rhythm",
    "review_continuity", "review_prompts", "review_quality", "aggregator",
}

EXPECTED_EDGES = {
    ("__start__", "story_planner"),
    ("story_planner", "screenwriter"),
    ("screenwriter", "critics"),
    ("critics", "review_critics"),
    ("art_brief_parser", "character_designer"),
    ("art_brief_parser", "scene_designer"),
    ("character_designer", "consistency_auditor"),
    ("scene_designer", "consistency_auditor"),
    ("consistency_auditor", "review_assets"),
    ("storyboarder", "review_storyboard"),
    # 白模预演师接线后新增
    ("previs_artist", "rhythm_director"),
    ("rhythm_director", "review_rhythm"),
    ("continuity_checker", "review_continuity"),
    ("prompt_compiler", "review_prompts"),
    ("image_generator", "quality_assessor"),
    ("aggregator", "__end__"),
}

EXPECTED_CONDITIONAL = {
    "review_critics": {"continue": "review_script", "redo": "critics"},
    "review_script": {"continue": "art_brief_parser", "redo": "screenwriter",
                      "back": "story_planner"},
    "review_assets": {"continue": "storyboarder", "redo": "character_designer",
                      "back": "screenwriter"},
    # 分镜确认后**先进预演师**（出初稿），再进节奏
    "review_storyboard": {"continue": "previs_artist", "redo": "storyboarder",
                          "back": "character_designer"},
    "review_rhythm": {"continue": "continuity_checker", "redo": "rhythm_director"},
    "review_continuity": {"continue": "prompt_compiler", "redo": "continuity_checker"},
    "review_prompts": {"continue": "image_generator", "redo": "prompt_compiler"},
    "quality_assessor": {"retry": "image_generator", "continue": "review_quality"},
    "review_quality": {"continue": "review_images", "redo": "quality_assessor"},
    "review_images": {"continue": "aggregator", "redo": "image_generator",
                      "back": "storyboarder"},
}


def test_graph_matches_expected_topology():
    """节点 + 普通边 + 条件边，全部逐条比对。"""
    from app.workflows.main_workflow import build_graph

    g = build_graph()

    assert set(g.nodes) == EXPECTED_NODES, (
        f"节点不一致：多={sorted(set(g.nodes) - EXPECTED_NODES)} "
        f"少={sorted(EXPECTED_NODES - set(g.nodes))}"
    )

    got_edges = set(g.edges)
    assert got_edges == EXPECTED_EDGES, (
        f"普通边不一致：缺={sorted(EXPECTED_EDGES - got_edges)} "
        f"多={sorted(got_edges - EXPECTED_EDGES)}"
    )

    for src, want in EXPECTED_CONDITIONAL.items():
        br = g.branches.get(src)
        assert br, f"缺条件边：{src}"
        ends: dict = {}
        for spec in br.values():
            ends.update(getattr(spec, "ends", {}) or {})
        assert ends == want, f"条件边 {src} 不符：期望 {want}，实际 {ends}"


def test_every_node_has_out_edge():
    """每个节点都必须有出边 —— 否则执行会**卡死在那个节点**。

    （这条也是 `graph_spec.validate_spec()` 的检查项，这里从"实际搭出来的图"
    再验一遍：规格校验通过 ≠ 图搭出来一定对。）
    """
    from app.workflows.main_workflow import build_graph

    g = build_graph()
    has_out = {src for src, _ in g.edges} | set(g.branches)
    missing = sorted(set(g.nodes) - has_out)
    assert not missing, f"这些节点没有出边，执行会卡死：{missing}"


def test_build_graph_rejects_unimplemented_node():
    """规格引用了**没有实现**的函数时，必须**明确报错**而不是搭出一张坏图。

    这是 A2 的边界：运行时改图只能重新组装**已有**的实现函数；
    新增业务环节需要先有人写代码。
    """
    import pytest

    from app.workflows import graph_spec as gs
    from app.workflows.main_workflow import build_graph

    spec = gs.clone_spec(gs.default_spec())
    spec["nodes"].append({"key": "subtitle_gen", "kind": "business",
                          "fn": "node_subtitle_gen"})
    with pytest.raises(ValueError, match="未实现"):
        build_graph(spec)


def test_validate_spec_catches_broken_graphs():
    """`validate_spec` 必须抓住"图会直接坏掉"的几种情况。"""
    from app.workflows import graph_spec as gs

    # 基线：默认规格必须**零错误**
    assert gs.errors_of(gs.validate_spec(gs.default_spec())) == []

    # ① 节点没有出边 → 会卡死
    spec = gs.clone_spec(gs.default_spec())
    spec["edges"] = [e for e in spec["edges"] if e["from"] != "story_planner"]
    spec["conditional"] = [e for e in spec["conditional"]
                           if e["from"] != "story_planner"]
    errs = gs.errors_of(gs.validate_spec(spec))
    assert any("卡死" in e.message for e in errs), [e.message for e in errs]

    # ② 节点没有入边 → 永远不会被执行
    spec = gs.clone_spec(gs.default_spec())
    spec["edges"] = [e for e in spec["edges"]
                     if e["to"] != "continuity_checker"]
    spec["conditional"] = [
        {**e, "branches": {k: v for k, v in e["branches"].items()
                           if v != "continuity_checker"}}
        for e in spec["conditional"]]
    errs = gs.errors_of(gs.validate_spec(spec))
    assert any("永远不会被执行" in e.message for e in errs), [e.message for e in errs]

    # ③ 边指向不存在的节点
    spec = gs.clone_spec(gs.default_spec())
    spec["edges"].append({"from": "critics", "to": "不存在的节点"})
    errs = gs.errors_of(gs.validate_spec(spec))
    assert any("不存在" in e.message for e in errs), [e.message for e in errs]

    # ④ 未知路由函数
    spec = gs.clone_spec(gs.default_spec())
    spec["conditional"][0]["router"] = "no_such_router"
    errs = gs.errors_of(gs.validate_spec(spec))
    assert any("未知路由" in e.message for e in errs), [e.message for e in errs]

    # ⑤ 删节点（用户要求：允许但需二次确认）在**结构校验**上必须报出来：
    #    删掉一个有下游的节点会让下游没有入边
    spec = gs.clone_spec(gs.default_spec())
    spec["nodes"] = [n for n in spec["nodes"] if n["key"] != "image_generator"]
    errs = gs.errors_of(gs.validate_spec(spec))
    assert errs, "删掉有下游的节点居然校验通过 —— 会搭出一张断链的图"


def test_switching_node_off_keeps_graph_valid():
    """**关掉一个节点**（用户确认 A2 要支持的能力）必须能搭出合法的图。

    以「配音指导」这类可选环节为例：它不是主链上的必经点，
    关掉它（从 nodes/edges 里移除）后整张图仍应通过校验 ——
    这正是"开关现有节点"这类提案要能做到的。
    """
    from app.workflows import graph_spec as gs

    spec = gs.clone_spec(gs.default_spec())
    # 关掉「文学策划」环节：把 critics 从链上摘掉，让 screenwriter 直接进确认点
    spec["nodes"] = [n for n in spec["nodes"] if n["key"] != "critics"]
    spec["edges"] = [e for e in spec["edges"]
                     if e["from"] != "critics" and e["to"] != "critics"]
    spec["edges"].append({"from": "screenwriter", "to": "review_critics"})
    # ⚠️ 条件边要重写：review_critics 的 redo 原本指向已删掉的 critics。
    # 这里必须**逐条构造新 dict**，不能写成
    #     [{**e, ...} if cond else e for e in ...]
    # —— 那样三元表达式会作用在**整个 dict 字面量**上，列表元素变成
    #    一个 {k: v} 映射，后面校验时把"值"当分支读，
    #    报 `TypeError: unhashable type: 'dict'`（我踩过）。
    rebuilt = []
    for e in spec["conditional"]:
        if e["from"] == "review_critics":
            rebuilt.append({
                **e,
                "branches": {k: ("review_script" if v == "critics" else v)
                             for k, v in e["branches"].items()},
            })
        else:
            rebuilt.append(e)
    spec["conditional"] = rebuilt

    errs = gs.errors_of(gs.validate_spec(spec))
    assert not errs, f"关掉 critics 后图不合法：{[e.message for e in errs]}"

    from app.workflows.main_workflow import build_graph

    g = build_graph(spec)
    assert "critics" not in g.nodes
    assert ("screenwriter", "review_critics") in set(g.edges)


def test_reordering_edges_is_possible():
    """**重排连线**（用户确认 A2 要支持的另一种能力）必须能生效。

    把「分镜师」插到「一致性审查」之前（原来它在审查之后）——
    这纯粹是改数据，不需要碰任何实现代码。
    """
    from app.workflows import graph_spec as gs
    from app.workflows.main_workflow import build_graph

    spec = gs.clone_spec(gs.default_spec())
    # 让 consistency_auditor 直接进 review_assets 之前先过 storyboarder
    spec["edges"] = [
        e for e in spec["edges"]
        if not (e["from"] == "consistency_auditor" and e["to"] == "review_assets")
    ]
    spec["edges"].append({"from": "consistency_auditor", "to": "storyboarder"})
    # 原来 storyboarder 进的 review_storyboard 改成先进 review_assets
    spec["edges"] = [
        e for e in spec["edges"]
        if not (e["from"] == "storyboarder" and e["to"] == "review_storyboard")
    ]
    spec["edges"].append({"from": "storyboarder", "to": "review_assets"})
    # 让 review_assets 的 continue 指向 review_storyboard（补上原链）
    for e in spec["conditional"]:
        if e["from"] == "review_assets":
            e["branches"]["continue"] = "review_storyboard"

    errs = gs.errors_of(gs.validate_spec(spec))
    assert not errs, f"重排后图不合法：{[e.message for e in errs]}"

    g = build_graph(spec)
    assert ("consistency_auditor", "storyboarder") in set(g.edges)
    assert ("storyboarder", "review_assets") in set(g.edges)
