"""节点排查修掉的 bug 的回归测试。

2026-10-07 做了一轮系统排查（29 个节点 + 工厂 + 图规格），下面每条测试对应
一个**实测确认过**的真 bug，防止改回去。

每条都注明"症状"，因为这类 bug 的共同点是**不报错、只是行为不对**。
"""

from __future__ import annotations

import pytest


# ----------------------------------------------------------------------
# 1. 「返回上一步」是空操作
# ----------------------------------------------------------------------

def test_back_triggers_regeneration_like_redo():
    """`back` 必须和 `redo` 一样触发**重新生成**。

    症状：确认点 #5/#6/#7/#8 点「返回上一步」→ 回到同一确认点、**内容一字未改**
    （用户看到同一个面板反复弹出，像卡死）。而前端 `ReviewPanel` 在**每个**
    确认点都渲染了「返回上一步」按钮，所以这是用户点得到的。

    根因：`route_simple_review` 把 `back` 路由成 `redo` 送回该阶段，
    但阶段节点只判 `review_decision == "redo"` → `back` 落到"已有产物就跳过"。
    """
    from app.workflows.main_workflow import _redo_requested

    assert _redo_requested({"review_decision": "redo"}) is True
    assert _redo_requested({"review_decision": "back"}) is True, \
        "back 不触发重做 —— 又变成空操作了"
    assert _redo_requested({"review_decision": "approve"}) is False
    assert _redo_requested({}) is False


def test_no_stage_node_still_compares_only_to_redo():
    """阶段节点里不该再有"只判 `== 'redo'`"的写法（要用 `_redo_requested`）。

    ⚠️ 我的第一版检查**误报了**：它抓到了 `_redo_requested` **自己**的实现
    （`state.get("review_decision") in ("redo", "back")`）—— 那正是**正确写法**。
    所以这里要排除那个 helper 的函数体。
    """
    import pathlib
    import re

    src = pathlib.Path("app/workflows/main_workflow.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    # 找到 _redo_requested 的函数体范围，跳过它
    start = end = None
    for i, line in enumerate(lines):
        if line.startswith("def _redo_requested("):
            start = i
        elif start is not None and line.startswith("def ") and i > start:
            end = i
            break
    skip = set(range(start, end if end is not None else len(lines))) if start is not None else set()

    bad = []
    for i, line in enumerate(lines):
        if i in skip:
            continue
        code = line.split("#")[0]
        # 只判 redo / != redo，而**没有**引用 _redo_requested
        if re.search(r'review_decision"\)\s*(==|!=)\s*"redo"', code):
            bad.append(f"{i + 1}: {line.strip()}")
    assert not bad, (
        "这些地方还在只判 =='redo'（back 会变成空操作），应该用 _redo_requested：\n  "
        + "\n  ".join(bad))


# ----------------------------------------------------------------------
# 2. 编辑后失效下游：错清 / 漏清
# ----------------------------------------------------------------------

def test_dict_fields_cover_all_dict_typed_outputs():
    """dict 类型的产物失效时要清成 `{}`，不是 `[]`。

    症状：编辑剧本后 `quality_report` 被清成 `[]`（类型错误），而
    `review_quality_node` 的 `if not state.get("quality_report")` 会把 `[]`
    当成"没有报告"→ **自动放行该确认点**（用户再也看不到质量确认）。
    """
    from app.workflows.main_workflow import _DICT_FIELDS

    assert "quality_report" in _DICT_FIELDS
    assert "character_consistency_report" in _DICT_FIELDS


def test_editing_profiles_does_not_wipe_reference_images():
    """编辑角色外貌**不能清空角色参考图**。

    症状：参考图的生产者 `character_designer` 在确认点**上游**，本次遍历不会再
    经过它 → 参考图被**永久清空** → `ImageGenerator` 的 IP-Adapter 静默退化成
    普通文生图（角色一致性功能没了），而图片被全量重生成（白花钱）。
    """
    from app.workflows.main_workflow import _DOWNSTREAM_BY_KEY

    assert "character_reference_images" not in _DOWNSTREAM_BY_KEY["character_profiles"]


def test_editing_profiles_invalidates_prompts():
    """编辑角色外貌必须清 `prompts`。

    症状：提示词里注入了角色外貌，不清 prompts 的话 `prompt_compiler` 见产物完整
    就跳过 → 图片仍按**旧外貌**重生成，用户这次编辑对最终画面**完全没有效果**。
    """
    from app.workflows.main_workflow import _DOWNSTREAM_BY_KEY

    assert "prompts" in _DOWNSTREAM_BY_KEY["character_profiles"]


@pytest.mark.parametrize("key", ["script", "storyboard"])
def test_editing_script_or_storyboard_invalidates_rhythm_plan(key):
    """编辑剧本/分镜必须清 `rhythm_plan`。

    症状：节奏方案留着旧 `shot_id`，而 `node_rhythm_director` 见已有产物就跳过
    → `result.rhythm_plan` 里的镜头号与最终分镜**完全不相交**（剪辑对不上镜头）。
    """
    from app.workflows.main_workflow import _DOWNSTREAM_BY_KEY

    assert "rhythm_plan" in _DOWNSTREAM_BY_KEY[key]


def test_invalidation_types_match_field_declarations():
    """失效表里的每个字段，清空类型都必须与 CrewState 的声明一致。"""
    from app.core.state import CrewState
    from app.workflows.main_workflow import _DICT_FIELDS, _DOWNSTREAM_BY_KEY

    hints = CrewState.__annotations__
    bad = []
    for key, downs in _DOWNSTREAM_BY_KEY.items():
        for k in downs:
            if k not in hints:
                bad.append(f"{key}->{k}（不在 CrewState 里，清了也没用）")
                continue
            is_dict = hints[k] is dict
            clears_to_dict = k in _DICT_FIELDS
            if is_dict != clears_to_dict:
                bad.append(
                    f"{key}->{k}: CrewState 声明 {hints[k]}，"
                    f"但失效时清成 {'{}' if clears_to_dict else '[]'}（类型不符）")
    assert not bad, "\n  ".join(bad)


# ----------------------------------------------------------------------
# 3. 发行总监永远跳过
# ----------------------------------------------------------------------

def test_publish_uses_session_video_not_only_final_video(monkeypatch):
    """发行不能只看 `state["final_video"]`。

    症状：`final_video` 这个字段**全项目没有生产者**，真正的视频在会话 payload
    里。原先先判 `final_video` 空就 return → **图里的发行环节永远走"跳过"分支**，
    会话里明明有视频也不发。
    """
    from app.services import post_production as pp

    monkeypatch.setattr(pp, "_session_payload",
                        lambda st: {"video": {"dir_or_path": "v/", "produced": 2}})

    seen = {}

    class FakeDistributor:
        def run(self, state, platforms=None):
            seen["video"] = dict(state.get("video") or {})
            return {"publish_results": {"bilibili": {"status": "mock"}}}

    import app.agents.distributor as dist_mod

    monkeypatch.setattr(dist_mod, "Distributor", FakeDistributor)
    out = pp.publish({"session_id": "s1"})       # 注意：**没有** final_video
    assert out["publish_results"], "会话里有视频却还是跳过了发行"
    assert seen["video"]["produced"] == 2


def test_publish_still_skips_when_truly_no_video(monkeypatch):
    """两者都空时才跳过（别把跳过逻辑删了）。"""
    from app.services import post_production as pp

    monkeypatch.setattr(pp, "_session_payload", lambda st: {})
    out = pp.publish({"session_id": "s1"})
    assert out["publish_results"] == {}
    assert "跳过" in out["distributor_note"]


# ----------------------------------------------------------------------
# 4. 预演规格：模型给脏类型不能崩
# ----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"actors": 1}, {"props": 3}, {"shots": True},
                                 {"actors": 1, "props": 3, "shots": True}])
def test_normalize_scene_survives_scalar_lists(bad):
    """模型把 actors/props/shots 给成数字或 bool 时**不能抛异常**。

    症状：`TypeError: 'int' object is not iterable` 会**逃出**本模块
    "出错不抛异常"的声明 → 冒到图节点 → `task_runner` 把**整次运行**判成 error。
    预演只是锦上添花，不该拖垮流程。
    """
    from app.services.previs_prompt import normalize_scene

    scene, notes = normalize_scene(bad)
    assert scene["actors"] == []
    assert scene["shots"] == []


# ----------------------------------------------------------------------
# 5. 产物出口：配音 / 预演 / 后期三环不能被聚合器吞掉
# ----------------------------------------------------------------------

def test_aggregator_result_exports_the_new_artifacts():
    """聚合器的 result 白名单必须带上新产物。

    症状：`task_runner` 结束时用 `result` **整体替换**会话 payload，
    `/api/crew/status` 也只回 payload → 不在白名单里的产物**对外没有任何出口**：
    配音 mp3 已落盘前端拿不到、预演初稿已生成却只能再花一次 LLM 调用重出。
    """
    from app.agents.aggregator import Aggregator
    from app.core.state import CrewState

    marker = "present"
    state = {k: marker for k in ("voice_track", "bgm_track", "previs",
                                 "video_report", "fixed_images")}
    state["voice_track"] = [{"shot_id": "1"}]
    out = Aggregator().run(state)
    result = out["result"]
    for k in ("voice_track", "bgm_track", "previs", "video_report", "fixed_images"):
        assert k in result, f"聚合器没导出 {k}（产物对外没有出口）"
    assert result["voice_track"] == [{"shot_id": "1"}]


# ----------------------------------------------------------------------
# 6. 分段执行要管得住新环节
# ----------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["previs_artist", "voice_director", "inpainter",
                                   "motion_compositor", "video_assessor"])
def test_new_stages_are_in_stage_order(stage):
    """新环节必须在 `_STAGE_ORDER` 里。

    症状：`_skipped_by_scope` 对**不在表里的名字恒返回 False** →
    `end_stage="storyboarder"` 时预演照调 LLM、配音照起 TTS 子进程、
    动态合成照提交几十秒到几分钟的后台视频任务（**还会花钱**），
    与"AI 只执行到指定环节"的语义相反。
    """
    from app.workflows.main_workflow import _STAGE_ORDER

    assert stage in _STAGE_ORDER, f"{stage} 不在 _STAGE_ORDER —— start/end_stage 管不住它"


@pytest.mark.parametrize("stage,node_fn", [
    ("previs_artist", "node_previs_artist"),
    ("voice_director", "node_voice_director"),
    ("inpainter", "node_inpainter"),
    ("motion_compositor", "node_motion_compositor"),
    ("video_assessor", "node_video_assessor"),
])
def test_new_nodes_honour_scope(stage, node_fn):
    """这些节点必须真去查 `_skipped_by_scope`（光进表不够）。"""
    from app.workflows import main_workflow as mw

    fn = getattr(mw, node_fn)
    # end_stage 在它之前 → 应该被跳过（只返回 current_phase）
    out = fn({"end_stage": "storyboarder", "storyboard": [{"shot_id": "s1"}],
              "images": [{"shot_id": "s1"}]})
    assert out.get("current_phase") == stage
    assert len(out) == 1, f"{stage} 没被跳过，还返回了 {sorted(out)}"
