"""小剧组 (Little Crew) — LangGraph 主工作流（含 Human-in-the-Loop）。

拓扑（9 节点）：
    START
      -> story_planner    (Agent 1 故事策划)
      -> screenwriter     (Agent 2 编剧)
      -> review_script    (确认点① 剧本审核 —— interrupt)
         ↙ redo→screenwriter / continue→storyboarder
      -> storyboarder     (Agent 3 分镜构建)
      -> review_storyboard(确认点② 分镜审核 —— interrupt)
         ↙ redo→storyboarder / continue→prompt_compiler
      -> prompt_compiler  (Agent 4 提示词编译)
      -> image_generator  (Agent 5 图片生成)
      -> review_images    (确认点③ 图片质量 —— interrupt)
         ↙ redo→image_generator / continue→aggregator
      -> aggregator       (Agent 6 结果聚合)
      -> END

HITL 说明：
  - settings.hitl=True：review 节点调用 interrupt() 暂停，等待人工 approve/redo(feedback)；
    通过 Command(resume=...) 原地续跑，redo 时回到对应阶段节点（不重跑上游）。
  - settings.hitl=False：review 节点直接 approve，整条流程无停顿跑完（脚本/CLI 用）。
"""

from typing import Literal

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from app.core.config import settings
from app.core.state import CrewState

from app.agents.planner import StoryPlanner
from app.agents.stage2.character_designer import CharacterDesigner
from app.agents.stage2.scene_designer import SceneDesigner
from app.agents.stage2.art_brief_parser import ArtBriefParser
from app.agents.stage2.consistency_auditor import ConsistencyAuditor
from app.agents.writer import Screenwriter
from app.agents.critics import ScriptCritic
from app.agents.shot_composer import Storyboarder
from app.agents.prompt_refiner import PromptCompiler
from app.agents.image_generator import ImageGenerator
from app.agents.quality_assessor import QualityAssessor
from app.agents.stage3.rhythm_director import RhythmDirector
from app.agents.stage3.continuity_checker import ContinuityChecker
from app.agents.aggregator import Aggregator

REVIEW_SCRIPT_POINT = 1
REVIEW_ASSETS_POINT = 2
REVIEW_STORYBOARD_POINT = 3
REVIEW_IMAGES_POINT = 4
REVIEW_CRITICS_POINT = 5
REVIEW_RHYTHM_POINT = 6
REVIEW_CONTINUITY_POINT = 7
REVIEW_PROMPTS_POINT = 8
REVIEW_QUALITY_POINT = 9


# ------------------- Agent 节点（含「分段角色」能力） -------------------

# 分段顺序：用于判断 start_stage 之前应跳过哪些环节（13 环节完整流水线）。
# 注意：quality_assessor / aggregator / distributor 为汇总/分发节点，不受 start/end 跳过，
#       它们始终会运行（仅展示为环节）。
_STAGE_ORDER = [
    "writer", "critics", "screenwriter", "character_designer", "scene_designer",
    "consistency_auditor", "storyboarder", "rhythm_director", "continuity_checker",
    "director", "photographer", "quality_assessor", "aggregator",
]


def _skipped_by_scope(state: CrewState, stage_name: str) -> bool:
    """该环节是否因 start_stage / end_stage 区间被跳过。

    - start_stage 更靠后 -> 该环节跳过
    - end_stage 更靠前   -> 该环节跳过
    - 环节不在 _STAGE_ORDER（如 distributor）或未设 start/end -> 不跳过
    """
    start_stage = state.get("start_stage")
    end_stage = state.get("end_stage")
    if stage_name not in _STAGE_ORDER:
        return False
    idx = _STAGE_ORDER.index(stage_name)
    if start_stage and start_stage in _STAGE_ORDER and idx < _STAGE_ORDER.index(start_stage):
        return True
    if end_stage and end_stage in _STAGE_ORDER and idx > _STAGE_ORDER.index(end_stage):
        return True
    return False


def _is_complete(val) -> bool:
    """判断一个节点的产物是否算「已完成」（可跳过）。

    - 空 -> 未完成
    - 列表：仅当非空且每一项都成功（无 status 或 status=='ok'）才算完成；
      否则（存在失败镜头，如图片生成失败）应允许节点重新生成，避免死循环。
    - 其它（dict/str 等）：非空即完成。
    """
    if not val:
        return False
    if isinstance(val, list):
        return all(not isinstance(it, dict) or it.get("status", "ok") == "ok" for it in val)
    return True


# 阶段 -> 中文名（用于「AI 思考过程」）
_STAGE_LABEL = {
    "writer": "策划总监", "critics": "文学策划", "screenwriter": "编剧",
    "character_designer": "角色设计师", "scene_designer": "场景设计师",
    "consistency_auditor": "美术总监·一致性", "storyboarder": "分镜师",
    "rhythm_director": "剪辑指导", "continuity_checker": "场记",
    "director": "技术指导", "photographer": "摄影师",
    "quality_assessor": "质检总监", "aggregator": "结果聚合",
}


def _stage_thinking(stage_name: str, out: dict) -> str:
    """从该节点产出的 dict 推导一段简短「思考/决策」叙述（零额外 LLM 调用）。"""
    out = out or {}
    if stage_name == "writer":
        o = out.get("story_outline") or {}
        chars = o.get("characters") or []
        names = "、".join(str(c.get("name")) for c in chars if isinstance(c, dict))[:40] or "无明确角色"
        return f"把你的创意提炼成故事大纲「{o.get('title','未命名')}」，风格 {o.get('style','未定') or '未定'}，含 {len(chars)} 个角色：{names}。"
    if stage_name == "critics":
        r = out.get("script_review") or {}
        sc = r.get("score", 1)
        return f"审读剧本，给出评分 {sc}；剧情/人物/节奏是否成立、有哪些问题与改进建议。"
    if stage_name == "screenwriter":
        o = out.get("script") or {}
        sc = o.get("scenes") or []
        return f"把大纲落成剧本，划分 {len(sc)} 个场景；每个场景写清地点、氛围、动作与台词。"
    if stage_name == "character_designer":
        profs = out.get("character_profiles") or {}
        names = "、".join(str(k) for k in profs.keys())[:40] or "无"
        return f"识别出 {len(profs)} 名角色（{names}），为每人生成正面/侧面/半身三姿态参考图并选最佳作主参考图。"
    if stage_name == "scene_designer":
        refs = out.get("scene_reference_images") or {}
        return f"为 {len(refs)} 个场景各生成一张氛围参考图，确定舞台美术风格。"
    if stage_name == "consistency_auditor":
        score = out.get("consistency_score")
        rep = out.get("character_consistency_report") or {}
        fg = rep.get("flagged") or {}
        return f"核对角色跨镜头一致性，整体评分 {round(score,1) if score is not None else '待定'}，标记问题角色 {len(fg.get('characters') or [])} 个。"
    if stage_name == "storyboarder":
        sb = out.get("storyboard") or []
        return f"把剧本切成 {len(sb)} 个镜头：确定景别、机位、动作与时长，形成分镜。"
    if stage_name == "rhythm_director":
        rp = out.get("rhythm_plan") or []
        return f"为 {len(rp)} 个镜头排节奏：给每镜建议时长与快慢标签。"
    if stage_name == "continuity_checker":
        rep = out.get("continuity_report") or []
        return f"核查 {len(rep)} 处可能的穿帮/逻辑问题（场景、道具、服装、时间线）。"
    if stage_name == "director":
        pr = out.get("prompts") or []
        return f"把 {len(pr)} 个镜头转成给 AI 看得懂的提示词（画面、镜头语言、负面词）。"
    if stage_name == "photographer":
        imgs = out.get("images") or []
        ok = sum(1 for i in imgs if i.get("status") == "ok")
        return f"按提示词拍摄各镜头：共 {len(imgs)} 个，成功 {ok} 个；失败/低质将重试或降级。"
    if stage_name == "quality_assessor":
        q = out.get("quality_report") or {}
        return f"质检画面：整体评分 {q.get('overall_score','待定')}，标出最低分镜头与需重拍项。"
    if stage_name == "previs_artist":
        pv = out.get("previs") or {}
        scene = pv.get("scene") or {}
        if pv.get("error"):
            return (f"预演初稿生成失败（{pv['error']}）。"
                    f"可以在预演编辑器里从空白开始手动搭。")
        if pv.get("skipped"):
            return f"跳过预演：{pv['skipped']}。"
        return (f"白模预演：出了 {len(scene.get('shots') or [])} 个镜头的初稿"
                f"（{len(scene.get('actors') or [])} 个人物、"
                f"{len(scene.get('props') or [])} 个道具）；"
                f"走位与机位可在预演编辑器里微调。")
    if stage_name == "aggregator":
        return "汇总全部产物，打包成可展示的结果。"
    return f"完成「{_STAGE_LABEL.get(stage_name, stage_name)}」这一步。"


def _attach_thinking(stage_name: str, out: dict) -> dict:
    """给节点产出的 dict 追加一条 thinking_log（{label, text}）；若该环节报错则附上异常。"""
    text = _stage_thinking(stage_name, out)
    err = (out or {}).get("error") if isinstance(out, dict) else None
    if err:
        text = f"{text}（异常：{err}）" if text else f"异常：{err}"
    if not text:
        return out
    out = dict(out or {})
    out["thinking_log"] = [{"label": _STAGE_LABEL.get(stage_name, stage_name), "text": text}]
    return out


def _bind_context(fn):
    """把一个节点函数包成"进入时绑定 RunContext"的版本。

    为什么放在这一层：`RunContext`（用户/会话/职业）原本靠 thread-local 隐式传递，
    在多 worker / 线程池下会丢。把它放进 State 并在这里统一绑定，
    既让"谁在跑"成为可传递的事实，又不必改动每个 Agent 的签名。

    见 `app/core/context.py`。
    """
    from functools import wraps

    from app.core.context import RunContext, bind

    @wraps(fn)
    def wrapper(state: CrewState) -> dict:
        with bind(RunContext.from_state(state)):
            return fn(state)

    return wrapper


def _ctx(name: str):
    """按名字取一个已绑定上下文的节点函数（供 `add_node` 使用）。

    这样不必给每个节点函数加装饰器，也不改变它们的定义顺序语义。
    """
    return _bind_context(globals()[name])


def _make_stage_node(output_key: str, stage_name: str, agent_cls):
    """构造一个「分段」节点：
      - start_stage 更靠后 / end_stage 更靠前 -> 该环节跳过（AI 执行指定区间）
      - 人工 redo -> 交给 agent 重新生成（agent 内部按 feedback 重做）
      - 已有且完整产物 -> 跳过
      - 否则交给 AI agent 生成
    """
    @_bind_context
    def node(state: CrewState) -> dict:
        if _skipped_by_scope(state, stage_name):
            return {"current_phase": stage_name}
        # 人工重做：交还 agent（内部依据 feedback / force_redo 重新生成），
        # 确保 redo 不是“跳过”而是真正重新生成。
        if state.get("review_decision") == "redo":
            return _attach_thinking(stage_name, agent_cls().run(state))
        # 已有且完整产物 -> 跳过
        if _is_complete(state.get(output_key)):
            return {"current_phase": stage_name}
        return _attach_thinking(stage_name, agent_cls().run(state))
    return node


# Agent 1 故事策划（作家）
node_story_planner = _make_stage_node("story_outline", "writer", StoryPlanner)
# Agent 2 编剧
node_screenwriter = _make_stage_node("script", "screenwriter", Screenwriter)
# Agent 8 剧评（自动评审：已有则跳过）
node_critics = _make_stage_node("script_review", "critics", ScriptCritic)
# Agent 3 分镜师
node_storyboarder = _make_stage_node("storyboard", "storyboarder", Storyboarder)
# Agent 4 提示词编译（技术指导）
node_prompt_compiler = _make_stage_node("prompts", "director", PromptCompiler)
# Agent 5 图片生成（摄影师）
node_image_generator = _make_stage_node("images", "photographer", ImageGenerator)


def node_quality_assessor(state: CrewState) -> dict:
    """Agent 7 视觉质量评估节点。"""
    return _attach_thinking("quality_assessor", QualityAssessor().run(state))


def node_character_designer(state: CrewState) -> dict:
    """方案B 角色设计师节点（已有角色资产但非重做时跳过，redo 时重新生成）。"""
    if _skipped_by_scope(state, "character_designer"):
        return {"current_phase": "character_designer"}
    if state.get("character_profiles") and state.get("review_decision") != "redo":
        return {"current_phase": "character_designer"}
    return _attach_thinking("character_designer", CharacterDesigner().run(state))


def node_scene_designer(state: CrewState) -> dict:
    """方案B 场景设计师节点（已有场景资产但非重做时跳过，redo 时重新生成）。"""
    if _skipped_by_scope(state, "scene_designer"):
        return {"current_phase": "scene_designer"}
    if state.get("scene_design_profiles") and state.get("review_decision") != "redo":
        return {"current_phase": "scene_designer"}
    return _attach_thinking("scene_designer", SceneDesigner().run(state))


def node_art_brief_parser(state: CrewState) -> dict:
    """美术设定包解析节点：把用户「美术设定包」解析为 characters/scenes，供资产部独立生成。

    美术资产部被越过（start_stage 在其后）或无 brief 时，直接空放行，不拦截资产部。
    """
    if _skipped_by_scope(state, "character_designer"):
        return {"current_phase": "art_brief_parser"}
    return ArtBriefParser().run(state)


def node_rhythm_director(state: CrewState) -> dict:
    """方案B 剪辑指导/节奏导演节点（分镜完成后执行；已有 rhythm_plan 且非重做时跳过）。"""
    if _skipped_by_scope(state, "rhythm_director"):
        return {"current_phase": "rhythm_director"}
    if state.get("rhythm_plan") and state.get("review_decision") != "redo":
        return {"current_phase": "rhythm_director"}
    return _attach_thinking("rhythm_director", RhythmDirector().run(state))


def node_continuity_checker(state: CrewState) -> dict:
    """方案B 场记/连续性检查节点（节奏导演之后执行；仅检查不改动）。"""
    if _skipped_by_scope(state, "continuity_checker"):
        return {"current_phase": "continuity_checker"}
    if state.get("continuity_report") and state.get("review_decision") != "redo":
        return {"current_phase": "continuity_checker"}
    return _attach_thinking("continuity_checker", ContinuityChecker().run(state))


def node_consistency_auditor(state: CrewState) -> dict:
    """方案B 一致性审查节点（资产确认点前，为角色参考图/风格打分）。

    无角色资产（被跳过）时直接空放行，避免无意义计算。
    """
    if not state.get("character_reference_images") and not state.get("story_outline"):
        return {
            "consistency_score": None,
            "character_consistency_report": {},
            "current_phase": "consistency_auditor",
        }
    return _attach_thinking("consistency_auditor", ConsistencyAuditor().run(state))


def node_aggregator(state: CrewState) -> dict:
    """Agent 6 结果聚合节点。"""
    return _attach_thinking("aggregator", Aggregator().run(state))


# ------------------- 人工确认点（HITL） -------------------

def _parse_decision(decision, point: int) -> dict:
    """把人工返回（approve / redo+feedback / back+上一步 / content 编辑）写入状态。"""
    d = decision if isinstance(decision, dict) else {}
    dec = d.get("decision", "approve")
    if dec in ("redo", "back"):
        out = {"review_decision": dec, "review_point": point, "feedback": d.get("feedback", {})}
    else:
        out = {"review_decision": "approve", "review_point": point, "feedback": {}}
    if d.get("content") is not None:
        out["review_edited"] = d.get("content")
    return out


def _checkpoint_on(state: CrewState, key: str) -> bool:
    """该检查点是否被用户勾选。

    - 未设置(enabled_checkpoints 为 None/缺省) = 全部开启（向后兼容）
    - 空列表([]) = 用户取消全部检查点 -> 全部自动放行
    - 非空列表 = 仅勾选的关键牌启用
    """
    cps = state.get("enabled_checkpoints")
    if cps is None:
        return True
    return key in set(cps)


def _review(node_name: str, point: int, title: str, content, enabled: bool = True):
    """人工确认点通用实现：hitl 关闭或该检查点未勾选时自动放行，否则 interrupt 暂停等待人工。"""
    if not settings.hitl or not enabled:
        return {"review_decision": "approve", "review_point": point, "feedback": {}}
    decision = interrupt({"node": node_name, "review_point": point, "title": title, "content": content})
    return _parse_decision(decision, point)


# 编辑某阶段产物后需失效（清空）的下游字段，避免「用了编辑后的输入、却保留旧的产出」的自我矛盾。
# 取值类型：dict 类字段清成 {}，其余(list)清成 []，使下游节点重新生成。
_INVALIDATE_NOTE = "editor"

# 哪些字段是 dict 类型
_DICT_FIELDS = {
    "scene_reference_images", "scene_design_profiles",
    "character_reference_images", "character_profiles", "critic_report",
}

_DOWNSTREAM_BY_KEY = {
    "script": ["storyboard", "prompts", "images", "scene_reference_images",
               "scene_design_profiles", "character_reference_images",
               "character_profiles", "character_consistency_report",
               "quality_report", "continuity_report"],
    "character_profiles": ["character_reference_images", "character_consistency_report",
                           "images", "quality_report"],
    "storyboard": ["prompts", "images", "quality_report", "continuity_report"],
    "images": ["quality_report"],
}


def _apply_review_edit(rv: dict, state: CrewState, key: str, extract=None) -> dict:
    """用户「编辑后继续」：把 review_edited 写回该阶段产物。extract 用于从编辑内容取出对应字段。

    写回后清空下游产物，强制在后续节点重新生成，避免编辑后的输入与旧产出自相矛盾。
    """
    ed = rv.get("review_edited")
    if ed is not None:
        from copy import deepcopy
        val = extract(ed) if extract else ed
        if val is not None:
            rv[key] = val
            for k in _DOWNSTREAM_BY_KEY.get(key, []):
                rv[k] = {} if k in _DICT_FIELDS else []
    return rv


def review_script_node(state: CrewState) -> dict:
    """确认点①：剧本审核。无剧本(被跳过)时自动放行；有则返回剧本+剧评供确认。"""
    script = state.get("script")
    if not script:
        return {"review_decision": "approve", "review_point": REVIEW_SCRIPT_POINT, "feedback": {}}
    critique = state.get("script_review")
    content = {"script": script, "review": critique} if critique else script
    rv = _review("script", REVIEW_SCRIPT_POINT, "剧本审核", content, enabled=_checkpoint_on(state, "script"))
    # 用户编辑后继续：把改后的剧本写回
    return _apply_review_edit(rv, state, "script",
                              extract=lambda ed: ed.get("script") if isinstance(ed, dict) and "script" in ed else ed)


def _assets_content(state: CrewState) -> dict:
    """组装「资产确认」content：角色参考图 + 外貌 + 一致性评分。

    结构：{"characters": [{"name","appearance","reference_image","score","flagged","issues"}]}
    """
    profiles = dict(state.get("character_profiles") or {})
    refs = dict(state.get("character_reference_images") or {})
    report = state.get("character_consistency_report") or {}
    scores = report.get("character_scores") or {}
    flagged = report.get("flagged", {}).get("characters", []) or []
    suggestions = report.get("suggestions") or []

    chars = []
    for name, profile in profiles.items():
        chars.append({
            "name": name,
            "appearance": profile.get("appearance", ""),
            "clothing": profile.get("clothing", ""),
            "accessories": profile.get("accessories", []),
            "reference_image": refs.get(name, ""),
            "score": scores.get(name),
            "flagged": name in flagged,
        })
    overall = state.get("consistency_score") or report.get("consistency_score")
    # 场景参考图（供资产确认一起展示）
    scene_refs = dict(state.get("scene_reference_images") or {})
    scene_profs = dict(state.get("scene_design_profiles") or {})
    scenes = []
    for sid, ref in scene_refs.items():
        prof = scene_profs.get(sid, {}) or {}
        scenes.append({"scene_id": sid, "reference_image": ref, "setting": prof.get("setting", "")})
    return {"characters": chars, "scenes": scenes,
            "consistency_score": overall,
            "suggestions": suggestions}


def review_assets_node(state: CrewState) -> dict:
    """确认点②：资产确认。无角色资产(被跳过)时自动放行。"""
    if not state.get("character_profiles"):
        return {"review_decision": "approve", "review_point": REVIEW_ASSETS_POINT, "feedback": {}}
    rv = _review("assets", REVIEW_ASSETS_POINT, "资产确认", _assets_content(state),
                 enabled=_checkpoint_on(state, "assets"))

    def _extract(ed):
        # 用户编辑了 characters -> 写回 character_profiles
        if isinstance(ed, dict) and isinstance(ed.get("characters"), list):
            profs = {}
            for c in ed["characters"]:
                if isinstance(c, dict) and c.get("name"):
                    profs[c["name"]] = {"appearance": c.get("appearance", ""),
                                        "clothing": c.get("clothing", ""),
                                        "accessories": c.get("accessories", [])}
            return profs
        return None

    return _apply_review_edit(rv, state, "character_profiles", extract=_extract)


def review_storyboard_node(state: CrewState) -> dict:
    """确认点③：分镜审核。无分镜(被跳过)时自动放行。"""
    if not state.get("storyboard"):
        return {"review_decision": "approve", "review_point": REVIEW_STORYBOARD_POINT, "feedback": {}}
    rv = _review("storyboard", REVIEW_STORYBOARD_POINT, "分镜审核", state.get("storyboard"),
                 enabled=_checkpoint_on(state, "storyboard"))
    return _apply_review_edit(rv, state, "storyboard")


def review_images_node(state: CrewState) -> dict:
    """确认点④：图片质量审核。无图片(被跳过)时自动放行。"""
    if not state.get("images"):
        return {"review_decision": "approve", "review_point": REVIEW_IMAGES_POINT, "feedback": {}}
    rv = _review("images", REVIEW_IMAGES_POINT, "图片质量审核", state.get("images"),
                 enabled=_checkpoint_on(state, "images"))
    return _apply_review_edit(rv, state, "images")


# ------------------- 扩展人工检查点（每个职业都可勾选） -------------------
def review_critics_node(state: CrewState) -> dict:
    """检查点：文学策划(剧评)审核。无剧评则自动放行。"""
    if not state.get("script_review"):
        return {"review_decision": "approve", "review_point": REVIEW_CRITICS_POINT, "feedback": {}}
    return _review("critics", REVIEW_CRITICS_POINT, "文学策划审核",
                   state.get("critic_report") or state.get("script_review"),
                   enabled=_checkpoint_on(state, "critics"))


def review_rhythm_node(state: CrewState) -> dict:
    """检查点：剪辑指导(节奏)审核。无 rhythm_plan 则自动放行。"""
    if not state.get("rhythm_plan"):
        return {"review_decision": "approve", "review_point": REVIEW_RHYTHM_POINT, "feedback": {}}
    return _review("rhythm", REVIEW_RHYTHM_POINT, "剪辑指导审核", state.get("rhythm_plan"),
                   enabled=_checkpoint_on(state, "rhythm"))


def review_continuity_node(state: CrewState) -> dict:
    """检查点：场记(连贯性)审核。无 continuity_report 则自动放行。"""
    if not state.get("continuity_report"):
        return {"review_decision": "approve", "review_point": REVIEW_CONTINUITY_POINT, "feedback": {}}
    return _review("continuity", REVIEW_CONTINUITY_POINT, "场记·连贯性审核", state.get("continuity_report"),
                   enabled=_checkpoint_on(state, "continuity"))


def review_prompts_node(state: CrewState) -> dict:
    """检查点：技术指导(提示词)审核。无 prompts 则自动放行。"""
    if not state.get("prompts"):
        return {"review_decision": "approve", "review_point": REVIEW_PROMPTS_POINT, "feedback": {}}
    return _review("prompts", REVIEW_PROMPTS_POINT, "技术指导·提示词审核", state.get("prompts"),
                   enabled=_checkpoint_on(state, "prompts"))


def review_quality_node(state: CrewState) -> dict:
    """检查点：质检总监(质量)审核。无 quality_report 则自动放行。"""
    if not state.get("quality_report"):
        return {"review_decision": "approve", "review_point": REVIEW_QUALITY_POINT, "feedback": {}}
    return _review("quality", REVIEW_QUALITY_POINT, "质检总监审核", state.get("quality_report"),
                   enabled=_checkpoint_on(state, "quality"))


# ------------------- 确认点路由 -------------------

def route_review_assets(state: CrewState) -> Literal["continue", "redo", "back"]:
    """确认点②：approve→分镜；redo→回到角色设计师；back→回到上一步(重审剧本)。"""
    rd = state.get("review_decision")
    return "back" if rd == "back" else ("redo" if rd == "redo" else "continue")


def route_review_script(state: CrewState) -> Literal["continue", "redo", "back"]:
    """确认点①：approve→分镜；redo→回到编剧；back→回到故事策划。"""
    rd = state.get("review_decision")
    return "back" if rd == "back" else ("redo" if rd == "redo" else "continue")


def route_review_storyboard(state: CrewState) -> Literal["continue", "redo", "back"]:
    """确认点③：approve→提示词编译；redo→回到分镜；back→回到上一步(重审资产)。"""
    rd = state.get("review_decision")
    return "back" if rd == "back" else ("redo" if rd == "redo" else "continue")


def route_review_images(state: CrewState) -> Literal["continue", "redo", "back"]:
    """确认点④：approve→聚合；redo→回到图片生成；back→回到上一步(重审分镜)。"""
    rd = state.get("review_decision")
    return "back" if rd == "back" else ("redo" if rd == "redo" else "continue")


def route_quality(state: CrewState) -> Literal["continue", "retry"]:
    """视觉质量自动重试：有失败(低质)镜头且未超限则回到图片生成，否则进入人工审核。"""
    failed = [im for im in state.get("images", []) if im.get("status") != "ok"]
    if failed and state.get("retry_count", {}).get("image_generator", 0) < 3:
        return "retry"
    return "continue"


def route_simple_review(state: CrewState) -> Literal["continue", "redo"]:
    """扩展检查点的简单路由：通过→继续；重做/上一步→回到该阶段。"""
    rd = state.get("review_decision")
    return "redo" if rd in ("redo", "back") else "continue"


# ------------------- 图构建 / 编译 -------------------

def build_graph(spec: dict | None = None) -> StateGraph:
    """构建含人工确认点的 StateGraph。

    ## 从"硬编码"改成"读规格"（A2 的前提）

    原先这里 23 个 `add_node` + 41 个 `add_edge` 一行行写死，于是
    **"运行时改图"根本没有落点**（改一条边就得改代码）。

    现在拓扑来自 `graph_spec.default_spec()`，`build_graph(spec)` 按数据搭图。
    于是「开关节点 / 重排连线」= 改数据 + 重新 `build_graph`，
    这正是 A2 换图要的能力。

    Args:
        spec: 图规格；`None` 表示用默认（即当前生产图）。
              调用方负责先用 `graph_spec.validate_spec()` 校验 ——
              这里**不重复做业务校验**，只在拿到坏数据时让 LangGraph 自己报错。

    ⚠️ 等价性有测试守着：`tests/test_graph_spec.py` 会把这张数据图与
    重构前的硬编码拓扑**逐边对比**。改错一条边就会被抓到。
    """
    from app.workflows import graph_spec as gs

    spec = spec or gs.default_spec()
    g = StateGraph(CrewState)

    # ---- 节点：按 kind 选"怎么包" ----
    #     业务节点用 `_ctx("node_xxx")` 包一层（注入阶段名）；
    #     确认点各自一个独立函数（内部复用 `_review`）。
    for n in spec.get("nodes") or []:
        key = n["key"]
        kind = n.get("kind")
        if kind == "review":
            # 确认点函数名约定为 `{key}_node`
            fn = REVIEW_NODE_FNS_BY_KEY.get(key)
            if fn is None:
                raise ValueError(f"确认点 {key!r} 没有对应实现（应为 {key}_node）")
        else:
            # 业务节点函数名约定为 `node_{key}`，除非规格显式给了 `fn`
            fn_name = n.get("fn") or f"node_{key}"
            raw = BUSINESS_NODE_FNS_BY_NAME.get(fn_name)
            if raw is None:
                raise ValueError(
                    f"节点 {key!r} 引用了未实现的函数 {fn_name!r} —— "
                    f"A2 只能重新组装**已有**的实现函数；新增业务环节需要先写代码"
                )
            # 统一在这里包一次上下文（`_ctx` = `_bind_context(globals()[name])`）
            fn = _ctx(fn_name)
        g.add_node(key, fn)

    # ---- 入口 ----
    g.add_edge(START, spec.get("entry"))

    # ---- 普通边 ----
    for e in spec.get("edges") or []:
        src, dst = e["from"], e["to"]
        g.add_edge(src, END if dst == "__END__" else dst)

    # ---- 条件边 ----
    for e in spec.get("conditional") or []:
        router = ROUTERS_BY_NAME.get(e.get("router"))
        if router is None:
            raise ValueError(f"未知路由函数 {e.get('router')!r}")
        branches = {k: (END if v == "__END__" else v)
                    for k, v in (e.get("branches") or {}).items()}
        g.add_conditional_edges(e["from"], router, branches)

    return g


#: 确认点 key → 实现函数（约定 `{key}_node`）
REVIEW_NODE_FNS_BY_KEY: dict[str, object] = {}
#: 业务节点实现名 → 原始函数（**未**包上下文；包装由 build_graph 调 `_ctx` 做一次）
BUSINESS_NODE_FNS_BY_NAME: dict[str, object] = {}
#: 路由名 → 路由 callable
ROUTERS_BY_NAME: dict[str, object] = {}


def node_previs_artist(state: CrewState) -> dict:
    """白模预演师：生成**3D 预演初稿**（AI），供预演编辑器人工微调。

    用户要求："把「预演」构成「白模预演师」一部分，目前的预演相当于人工修改的"
    + "AI 生成初稿，人工微调"。

    所以这一步只出**初稿**；微调发生在 `#/previs` 编辑器里。
    生成失败**不抛异常**（预演是可选的结构输入），只记 `state["previs"]["error"]`。

    思考记录由 `_bind_context` 那层的 `_attach_thinking` 统一生成
    （见 `_stage_thinking` 里 `previs_artist` 那一支）——
    所以这里**只返回产物**，不要自己拼 thinking_log（我第一版就编了个
    不存在的 `_append_thinking`，白查一轮）。
    """
    from app.services.previs_artist import generate_previs_scene

    return generate_previs_scene(state)


def node_distributor(state: CrewState) -> dict:
    """发行总监：生成发布物料 + 各平台状态（不做真实上传，需 OAuth）。

    同样**可跳过**：没有成片就跳过并说明，不报错。
    """
    from app.services.post_production import publish

    return publish(state)


def node_inpainter(state: CrewState) -> dict:
    """修图师：**占位，直接跳过**（用户："先保留，后续开发，流程直接跳过"）。

    为什么不假装能修：`ImageService` 只有 `generate()`，没有 inpaint 方法。
    详见 `app/services/post_production.py::inpaint`。
    """
    from app.services.post_production import inpaint

    return inpaint(state)


def node_motion_compositor(state: CrewState) -> dict:
    """动态合成师（用户："**动态合成师就是视频生成**"）。

    只**提交**后台视频任务就返回 —— 视频要几分钟，不能在图里等。
    """
    from app.services.post_production import composite_motion

    return composite_motion(state)


def node_video_assessor(state: CrewState) -> dict:
    """视频质检师：视频还没好就跳过（它在后台跑）。"""
    from app.services.post_production import assess_video

    return assess_video(state)


def node_voice_director(state: CrewState) -> dict:
    """配音指导：把分镜台词合成成逐镜头音频（TTS）。

    用户要求"1、2 先做"里的 ① —— 原先注册表声明了它、但 `workflow_node`
    为空（从没进过执行图），而且 `voice_track` / `bgm_track` 这两个字段在
    `CrewState` 里**根本不存在**。现在补齐字段并接入。

    只做**对白**；BGM 不做（`music` 能力在提供者表里是空的，已知空缺）。
    失败**不抛异常**（用户可以后期自己配），在条目里记 `status` / `reason`。
    """
    from app.services.voice_director import synthesize_voice_track

    return synthesize_voice_track(state)


def _register_node_fns() -> None:
    """把"零件表"填好（模块导入末尾调用一次）。

    ## 两个容易搞错的点

    1. **为什么用函数而不是模块级字典字面量**：这些函数定义在本文件**上面**，
       字面量写在 `build_graph` 附近会引用到尚未定义的名字（NameError）。

    2. **存的是"原始函数"，不是 `_ctx(...)` 包装后的**：
       `_ctx(name)` 的定义是 `_bind_context(globals()[name])` —— 它**已经**包一层
       上下文绑定。我第一版在这里存了 `_ctx(name)`，`build_graph` 里又调一次
       `_ctx`，于是**重复绑定**（而且 `globals()["node_story_planner"]` 取到的
       正是包装后的值，等于绑了两次）。现在统一：这里存原始函数，
       搭图时由 `build_graph` 调一次 `_ctx`。
    """
    import sys

    ns = sys.modules[__name__].__dict__

    for key in ("review_script", "review_critics", "review_assets",
                "review_storyboard", "review_rhythm", "review_continuity",
                "review_prompts", "review_quality", "review_images"):
        fn = ns.get(f"{key}_node")
        if fn is not None:
            REVIEW_NODE_FNS_BY_KEY[key] = fn

    for name in ("story_planner", "screenwriter", "critics", "art_brief_parser",
                 "character_designer", "scene_designer", "consistency_auditor",
                 "storyboarder", "rhythm_director", "continuity_checker",
                 "prompt_compiler", "image_generator", "quality_assessor",
                 "previs_artist", "voice_director",
                 "distributor", "aggregator",
                 "inpainter", "motion_compositor", "video_assessor"):
        # ⚠️ 键名是 **`node_xxx`**（带前缀）—— 因为 `build_graph` 是按
        # `spec["nodes"][i].fn`（默认 `f"node_{key}"`）查的。
        # 我第一版存的是不带前缀的 `name`，于是查 `node_story_planner` 查不到，
        # 报"引用了未实现的函数"（那个报错还把人往"没写代码"上引，实际是键名不匹配）。
        fn_name = f"node_{name}"
        raw = ns.get(fn_name)
        if raw is not None:
            # 存**原始函数**；包装交给 build_graph（见上面第 2 点）
            BUSINESS_NODE_FNS_BY_NAME[fn_name] = raw

    for name, fn in (("simple_review", route_simple_review),
                     ("review_script", route_review_script),
                     ("review_assets", route_review_assets),
                     ("review_storyboard", route_review_storyboard),
                     ("review_images", route_review_images),
                     ("quality", route_quality)):
        ROUTERS_BY_NAME[name] = fn


# ⚠️ 必须在这里调用 —— **早于** `app_workflow = _install_initial_graph()`。
# 我第一版把它放在文件最末尾，于是 `_install_initial_graph()`（第 719 行）
# 先跑、`build_graph()` 去查零件表时它还是空的，报
# "引用了未实现的函数 node_story_planner"。位置本身就是个坑。
_register_node_fns()





def compile_graph(checkpointer=None, spec: dict | None = None):
    """构建并编译图（带 checkpointer，支持 interrupt 断点续跑）。

    ⚠️ **checkpointer 必须共享**：原先这里每次 `MemorySaver()` 都新建一个，
    于是"运行时换一张图"会把所有在途会话的断点全部丢掉（正在确认点等用户
    点"通过"的会话会突然变成"检查点丢失"）。

    现在默认取 `graph_manager` 里那个**唯一实例**，换图不换记忆。
    传 `checkpointer=` 可以显式指定（测试里用得上，便于隔离）。

    Args:
        spec: 图规格（A2 换图时传入改过的规格）；`None` = 当前生产图。
              ⚠️ 调用方必须用 **`compile_graph(spec=...)`** 而不是
              `build_graph(spec)` —— 后者只返回**未编译**的 `StateGraph` 骨架，
              没有 `.stream()` / `.get_state()`，换进 `graph_manager` 会让
              **所有**会话跑不起来（我踩过：`AttributeError: 'StateGraph'
              object has no attribute 'stream'`，而且是全局性故障）。
    """
    if checkpointer is None:
        from app.workflows.graph_manager import MANAGER

        checkpointer = MANAGER.checkpointer()
    return build_graph(spec).compile(checkpointer=checkpointer)


def _install_initial_graph():
    """把首张图装进 `graph_manager`，并返回它。

    为什么要经过 manager：运行时换图（A2）后，**所有**要执行图的代码都得拿
    到"当前那张"，而不是导入时绑定的第一张。见 `graph_manager` 的模块说明。
    """
    from app.workflows.graph_manager import MANAGER

    graph = compile_graph()
    MANAGER.install_initial(graph)
    return graph


# 模块级编译实例：供 API / 脚本直接 import 复用。
#
# ⚠️ 语义已降级为「**初始那张图**」。运行时要执行图的代码请用
# `graph_manager.current_graph()` —— 否则 A2 换图后你拿到的还是第一版。
app_workflow = _install_initial_graph()


def run_once(state: dict) -> dict:
    """非交互单次运行（hitl 关闭时使用），返回最终状态。"""
    from app.workflows.graph_manager import current_graph

    config = {"configurable": {"thread_id": state.get("session_id", "default")}}
    return current_graph().invoke(state, config=config)

