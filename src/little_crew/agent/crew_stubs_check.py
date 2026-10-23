"""5 个核心职位的规则桩与端到端链路 —— 自检。

重点验证**协作机制真的有效**，而不只是"流程能跑"：
  · 首稿的缺陷**真的被检出**
  · 返工指令**真的驱动了文本修改**
  · 复检**真的通过**（而不是永远不通过或永远通过）
  · 桩的产出与 LLM 的产出走**同一条消息通道**

运行： python -m little_crew.agent.crew_stubs_check
"""

from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.crew_demo import DEMO_SOURCE
from little_crew.agent.crew_stubs import (
    CORE_CREW_STUBS,
    DramaturgeRules,
    ReviewBoardRules,
    ScreenwriterRules,
    StoryEditorRules,
    StoryboardArtistRules,
    _flaws_of,
    _parse_scenes,
    default_crew_stubs,
)
from little_crew.agent.messages import (
    MAX_SHOT_SECONDS,
    Dimension,
    ReviewFeedback,
    ScriptDraft,
    Severity,
    Shot,
    ShotList,
    Verdict,
)
from little_crew.agent.orchestrator import CrewOrchestrator
from little_crew.agent.protocol import Budget, CallEnvelope, CallResult

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _call(impl: Any, kind: str, payload: dict[str, Any]) -> CallResult:
    """按**运行时的真实方式**调用一个职位实现。

    契约是 `impl(*, envelope, runtime)` —— 两个都是**关键字参数**。
    写成位置参数会得到 `TypeError`，写成 `**kwargs` 则会让签名不符
    **静默通过**（本项目已因此踩过一次坑）。
    """
    env = CallEnvelope.root(kind, kind, payload, Budget.unlimited(),
                            callee_kind="agent")
    return impl(envelope=env, runtime=None)


# ==========================================================================
# A. 剧本解析与结构检测
# ==========================================================================

def _parsing() -> None:
    scenes = _parse_scenes(DEMO_SOURCE, "E001")
    check("A1 结构化剧本按 [场N] 正确切分", len(scenes) == 2,
          f"切出 {len(scenes)} 场")
    check("A2 场次 id 规范（SC-<集>-<序号>）",
          all(s["id"].startswith("SC-E001-") for s in scenes),
          scenes[0]["id"])
    check("A3 场次正文归属正确（含首句）",
          "@林晚" in scenes[0]["text"] and "@陈默" in scenes[0]["text"])

    # 无标记时应整体视为一场，而不是解析失败
    one = _parse_scenes("只有一段没有场次标记的文本。", "E009")
    check("A4 无场次标记时整体视为一场（不解析失败）", len(one) == 1)

    # 结构检测：隐性冲突也要认
    implicit = {"heading": "", "text": "她甩开他的手，没有回头。"}
    check("A5 隐性冲突（动作层面）能被检出",
          "conflict" not in _flaws_of(implicit), str(_flaws_of(implicit)))
    empty = {"heading": "", "text": "天气很好。他们坐了一会儿。"}
    check("A6 确实无冲突的文本被检出缺冲突",
          "conflict" in _flaws_of(empty), str(_flaws_of(empty)))


# ==========================================================================
# B. 编剧桩
# ==========================================================================

def _screenwriter() -> None:
    sw = ScreenwriterRules()
    r = _call(sw, "script.draft", {"source.novel": DEMO_SOURCE, "episodeId": "E001"})
    check("B1 编剧返回 CallResult 成功且含 script.draft",
          r.ok and isinstance(r.output, dict) and "script.draft" in r.output)
    d: ScriptDraft = r.output["script.draft"]
    check("B2 产出是 ScriptDraft 消息（带 from/to）",
          isinstance(d, ScriptDraft) and d.frm == "screenwriter" and d.to)
    check("B3 首稿 revision=1", d.revision == 1)

    # 首稿应留下可检出的缺陷（最后一场缺"转折"）
    last = d.scenes[-1]
    check("B4 首稿**故意**留下可检出的结构缺陷",
          "turn" in _flaws_of(last), str(_flaws_of(last)))
    check("B5 缺陷只影响最后一场（前面的场次完好）",
          "turn" not in _flaws_of(d.scenes[0]), str(_flaws_of(d.scenes[0])))

    # 第二稿：给了返工指令应真的修好
    critique = {"issues": [{"problem": "SC-E001-002 缺少「转折」：本场没有转折",
                            "severity": "major",
                            "location": {"scene": "SC-E001-002"}}],
                "repairHints": [{"op": "insert_shot",
                                 "args": {"purpose": "转折",
                                          "after": "SC-E001-002"},
                                 "graphId": "G-E001"}]}
    r2 = _call(sw, "script.draft", {"source.novel": DEMO_SOURCE, "episodeId": "E001",
                                  "script.critique": critique})
    d2: ScriptDraft = r2.output["script.draft"]
    check("B6 第二稿真的修改了文本（返工不是走过场）",
          d2.scenes[-1]["text"] != d.scenes[-1]["text"])
    check("B7 修补后缺陷消失（补上了「转折」）",
          "turn" not in _flaws_of(d2.scenes[-1]), str(_flaws_of(d2.scenes[-1])))
    check("B8 记录了收到返工指令", len(sw.received_critiques) == 1)
    check("B9 记录了本轮修补的结构元素（返工有实证）",
          sw.patched[-1] == ("turn",), str(sw.patched))
    check("B9b 第二稿的 note 写明修补了什么",
          "turn" in d2.note, d2.note)

    # 关键：**无返工指令时，缺陷会重新出现** ——
    # 因为每次都是从原始素材重新出稿，缺陷是"这一稿还没修"的状态。
    # 若缺陷自己消失，"修订后通过"就没有因果意义了。
    sw3 = ScreenwriterRules()
    r3a = _call(sw3, "script.draft",
                {"source.novel": DEMO_SOURCE, "episodeId": "E001"})
    r3b = _call(sw3, "script.draft",
                {"source.novel": DEMO_SOURCE, "episodeId": "E001"})
    check("B10 无返工指令时缺陷**不会自己消失**（否则闭环是假的）",
          "turn" in _flaws_of(r3a.output["script.draft"].scenes[-1])
          and "turn" in _flaws_of(r3b.output["script.draft"].scenes[-1]),
          f"{_flaws_of(r3a.output['script.draft'].scenes[-1])} / "
          f"{_flaws_of(r3b.output['script.draft'].scenes[-1])}")
    check("B10b 无返工指令时不做任何修补",
          sw3.patched == [(), ()], str(sw3.patched))

    # introduce_flaw=False 时首稿即无缺陷
    sw4 = ScreenwriterRules(introduce_flaw=False)
    r4 = _call(sw4, "script.draft", {"source.novel": DEMO_SOURCE, "episodeId": "E001"})
    check("B11 可关闭缺陷注入（便于测「一次通过」路径）",
          not _flaws_of(r4.output["script.draft"].scenes[-1]))


# ==========================================================================
# C. 剧本顾问桩
# ==========================================================================

def _dramaturge() -> None:
    dr = DramaturgeRules()
    sw = ScreenwriterRules()
    draft = _call(sw, "script.draft",
                    {"source.novel": DEMO_SOURCE, "episodeId": "E001"}).output["script.draft"]

    r = _call(dr, "script.critique", {"script.draft": draft})
    check("C1 剧本顾问返回 ReviewFeedback",
          r.ok and isinstance(r.output["script.critique"], ReviewFeedback))
    fb: ReviewFeedback = r.output["script.critique"]
    check("C2 检出首稿的缺陷并给 REVISE", fb.verdict is Verdict.REVISE,
          f"verdict={fb.verdict.value} issues={len(fb.issues)}")
    check("C3 意见带**结构化定位**（可驱动自动返工）",
          all(i.location.scene for i in fb.issues),
          str([i.location.scene for i in fb.issues]))
    check("C4 MAJOR 及以上意见都给了建议",
          all(i.suggestion for i in fb.issues if i.severity is not Severity.INFO))
    check("C5 每条形而上意见都配了可执行的返工指令",
          len(fb.repair_hints) >= len(fb.issues),
          f"issues={len(fb.issues)} hints={len(fb.repair_hints)}")
    check("C6 返工指令的 op 在平台白名单内",
          all(h.op in h.KNOWN_OPS for h in fb.repair_hints),
          str([h.op for h in fb.repair_hints]))
    check("C7 跨族声明（qwen 审 deepseek）",
          fb.cross_family_ok and fb.reviewer_model != fb.generator_model,
          f"{fb.reviewer_model} vs {fb.generator_model}")
    check("C8 未收到 ScriptDraft 时明确失败（不静默）",
          not _call(dr, "script.critique", {}).ok)

    # 好稿应给 PASS
    sw2 = ScreenwriterRules(introduce_flaw=False)
    good = _call(sw2, "script.draft",
                    {"source.novel": DEMO_SOURCE, "episodeId": "E001"}).output["script.draft"]
    r2 = _call(dr, "script.critique", {"script.draft": good})
    check("C9 无缺陷稿给 PASS（不是永远不通过）",
          r2.output["script.critique"].verdict is Verdict.PASS,
          f"issues={len(r2.output['script.critique'].issues)}")


# ==========================================================================
# D. 文学策划桩
# ==========================================================================

def _story_editor() -> None:
    se = StoryEditorRules()
    sw = ScreenwriterRules(introduce_flaw=False)
    clean = _call(sw, "script.draft",
                    {"source.novel": DEMO_SOURCE, "episodeId": "E001"}).output["script.draft"]
    r = _call(se, "style.guard", {"script.draft": clean})
    check("D1 文学策划返回 ReviewFeedback",
          r.ok and isinstance(r.output["style.guard"], ReviewFeedback))

    # 塞入俗套表达应被检出
    dirty = ScriptDraft(
        frm="screenwriter", to="*", episode_id="E001",
        scenes=({"id": "SC-E001-001", "text": "她不禁缓缓地转过身，深深地叹了口气。"},))
    fb: ReviewFeedback = _call(se, "style.guard", {"script.draft": dirty}).output["style.guard"]
    check("D2 俗套表达被检出", len(fb.issues) >= 1, f"{len(fb.issues)} 条")
    check("D3 俗套问题定为 MINOR（不阻断）",
          all(i.severity is Severity.MINOR for i in fb.issues))
    check("D4 俗套问题给出替代表达建议",
          all(i.suggestion for i in fb.issues))
    check("D5 与剧本顾问**关注维度不同**（结构 vs 品质）",
          any(i.dimension is Dimension.QUALITY for i in fb.issues),
          str([i.dimension.value for i in fb.issues]))

    # ---- D6/D7：文风意见必须**真的能改到稿子**（2026-10-23 审计 M-2）----
    #
    # 生产方（文学策划）写 `{"scene", "avoid"}`，而消费方（编剧的 `_apply_critique`）
    # 原来只读 `{"purpose", "after"}`、且只在 `problem` 里找三个**结构**标签 ——
    # 于是文风审查的每条意见**两处都落空**，被静默丢弃：
    # 编剧收到"有意见"，但一个字都没改，而报告里一切正常。
    scenes = [{"id": "SC-E001-001", "text": "她不禁缓缓地转过身。"}]
    sw_probe = ScreenwriterRules(introduce_flaw=False)
    done = sw_probe._apply_critique(scenes, fb.as_dict())
    check("D6 ★★ 文风意见能改到稿子（不再被静默丢弃）",
          bool(done) and "不禁" not in scenes[0]["text"],
          f"applied={done} text={scenes[0]['text'][:24]}")
    check("D7 ★ 修补记录写明删了哪个俗套表达（可追溯）",
          any(str(x).startswith("avoid:") for x in done), str(done))

    # 结构类修补不得因此回归（两条协议并存）
    sc2 = [{"id": "SC-E001-001", "text": "她走了。"}]
    done2 = sw_probe._apply_critique(
        sc2, {"repairHints": [{"op": "add_turn",
                               "args": {"purpose": "转折",
                                        "after": "SC-E001-001"}}]})
    check("D8 结构类修补仍然有效（两种协议并存，不是替换）",
          done2 == ("turn",), str(done2))


# ==========================================================================
# E. 分镜师桩
# ==========================================================================

def _storyboard() -> None:
    sa = StoryboardArtistRules()
    sw = ScreenwriterRules(introduce_flaw=False)
    final = _call(sw, "script.draft",
                    {"source.novel": DEMO_SOURCE, "episodeId": "E001"}).output["script.draft"]
    r = _call(sa, "shot.list", {"script.final": final})
    check("E1 分镜师返回 ShotList", r.ok and isinstance(r.output["shot.list"], ShotList))
    sl: ShotList = r.output["shot.list"]
    check("E2 每场至少 2 镜", sl.shot_count >= 2 * len(final.scenes),
          f"{sl.shot_count} 镜 / {len(final.scenes)} 场")
    check("E3 所有镜头都在硬上限内",
          all(s.duration_sec <= MAX_SHOT_SECONDS for s in sl.shots),
          f"max={max(s.duration_sec for s in sl.shots)}")
    check("E4 镜头 id 规范且唯一",
          len({s.shot_id for s in sl.shots}) == sl.shot_count
          and all(s.shot_id.startswith("SH-E001-") for s in sl.shots))
    check("E5 景别在轮换（不连排同景别）",
          len({s.shot_size for s in sl.shots}) >= 3,
          str(sorted({s.shot_size for s in sl.shots})))
    check("E6 运镜模板名与白模渲染一致（可直驱预演）",
          all(s.camera_movement in ("static", "dolly_in", "dolly_out",
                                    "truck_left", "truck_right")
              for s in sl.shots),
          str(sorted({s.camera_movement for s in sl.shots})))
    check("E7 动作模板名与白模动作库一致",
          all(s.action in ("stand", "walk", "run") for s in sl.shots))
    check("E8 总时长等于各镜之和",
          abs(sl.total_seconds - sum(s.duration_sec for s in sl.shots)) < 0.01)
    check("E9 同时产出 shot.breakdown",
          isinstance(r.output.get("shot.breakdown"), dict))
    check("E10 未收到定稿时明确失败",
          not _call(sa, "shot.list", {}).ok)


# ==========================================================================
# F. 审片桩
# ==========================================================================

def _review_board() -> None:
    rb = ReviewBoardRules()
    sa = StoryboardArtistRules()
    sw = ScreenwriterRules(introduce_flaw=False)
    final = _call(sw, "script.draft",
                    {"source.novel": DEMO_SOURCE, "episodeId": "E001"}).output["script.draft"]
    sl: ShotList = _call(sa, "shot.list", {"script.final": final}).output["shot.list"]

    fb: ReviewFeedback = _call(rb, "review.board", {"shot.list": sl}).output["review.verdict"]
    check("F1 审片返回 ReviewFeedback", isinstance(fb, ReviewFeedback))
    check("F2 合规镜头的镜头表给 PASS", fb.verdict is Verdict.PASS,
          f"verdict={fb.verdict.value} issues={len(fb.issues)}")
    check("F3 QC-5 五维齐全",
          len(fb.score.dimensions) == 5, str(sorted(d.value for d in fb.score.dimensions)))
    check("F4 跨族盲读（qwen3-vl-max 审 deepseek-flash）",
          fb.cross_family_ok and fb.reviewer_model == "qwen3-vl-max")

    # 超长镜头应被 BLOCKER 拦住
    bad = ShotList(
        frm="storyboard_artist", to="*", episode_id="E001",
        shots=tuple(
            Shot(frm="storyboard_artist", to="*", shot_id=f"SH-E001-001-{i:03d}",
                 scene_id="SC-E001-001", duration_sec=14.0,
                 characters=("CHAR-0001",), shot_size="full")
            for i in (1, 2)),
        total_seconds=28.0,
    )
    fb2: ReviewFeedback = _call(rb, "review.board", {"shot.list": bad}).output["review.verdict"]
    check("F5 超 10 秒镜头被判 BLOCKER", 
          any(i.severity is Severity.BLOCKER for i in fb2.issues),
          f"issues={len(fb2.issues)}")
    check("F6 BLOCKER 均带 suggestion（构造时强制）",
          all(i.suggestion for i in fb2.issues
              if i.severity is Severity.BLOCKER))
    check("F7 超长镜头给 REVISE 而非 PASS", fb2.verdict is Verdict.REVISE)
    check("F8 可执行性维度因超长而下降",
          fb2.score.dimensions[Dimension.FEASIBILITY] < 100.0,
          f"{fb2.score.dimensions[Dimension.FEASIBILITY]}")
    check("F9 未收到 ShotList 时明确失败",
          not _call(rb, "review.board", {}).ok)

    # 缺 characters 应被一致性维度抓到
    nolist = ShotList(
        frm="storyboard_artist", to="*", episode_id="E001",
        shots=(Shot(frm="storyboard_artist", to="*", shot_id="SH-X-001-001",
                    scene_id="SC-X-001", duration_sec=5.0, characters=()),),
        total_seconds=5.0)
    fb3: ReviewFeedback = _call(rb, "review.board", {"shot.list": nolist}).output["review.verdict"]
    check("F10 未声明出场角色 → 一致性维度报出问题",
          any(i.dimension is Dimension.CONSISTENCY for i in fb3.issues))


# ==========================================================================
# G. 端到端链路
# ==========================================================================

def _pipeline() -> None:
    stubs = default_crew_stubs()
    o = CrewOrchestrator(max_rounds=3, model_families={
        "screenwriter": "deepseek", "storyboard_artist": "deepseek",
        "dramaturge": "qwen", "story_editor": "qwen", "review_board": "qwen"})
    o.bind_many(stubs)

    cycle = o.run_script_cycle({"source.novel": DEMO_SOURCE,
                                "episodeId": "E001", "title": "雨夜信"})
    check("G1 剧本闭环通过", cycle.accepted, cycle.reason[:60])
    # ⚠️ 这里原本断言"恰好 2 轮（缺陷 -> 修订 -> 通过）"。
    #
    # 2026-10-17 阈值从 80 降到 65 之后，**这一处刻意注入的缺陷
    # （2 场里缺 1 场转折，76.9 分）已经高于通过线**，于是第 1 轮就过 ——
    # 断言随之失效。
    #
    # 这不是缺陷：**返工闭环的覆盖不靠这里**。`orchestrator_check`
    # 的 D 段用**显式低分**（48 分）+ 第二轮高分，确定性地验证
    # "REVISE -> 修订 -> PASS" 这条路径，那比依赖"夹具分数恰好低于线"稳得多。
    #
    # 所以这里改为断言**实际行为**：能收敛、且产出的稿子是有效的。
    check("G2 在轮次上限内收敛（轮数 <= 3）",
          1 <= cycle.round_count <= 3, f"{cycle.round_count} 轮")
    check("G3 通过了就一定有定稿",
          cycle.final is not None, str(cycle.final))
    check("G4 每轮都记录了权威总分",
          all(r.total > 0 for r in cycle.rounds),
          str([round(r.total, 1) for r in cycle.rounds]))
    check("G5 编剧被调用的次数与轮数一致",
          stubs["screenwriter"].calls == cycle.round_count,
          f"calls={stubs['screenwriter'].calls} rounds={cycle.round_count}")
    # 第 2 轮起必须带着上一轮的意见 —— 这条与轮数无关，始终成立
    check("G6 若发生过返工，编剧确实收到了意见",
          cycle.round_count == 1
          or len(stubs["screenwriter"].received_critiques)
          == cycle.round_count - 1,
          f"{len(stubs['screenwriter'].received_critiques)} 次 / "
          f"{cycle.round_count} 轮")
    check("G7 script.final 归属编剧",
          o.owner_of("script.final") == "screenwriter")

    # 分镜
    out = o.run_stage("storyboard_artist", "shot.list",
                      {"script.final": cycle.final})
    check("G8 分镜阶段成功", out.ok, out.error[:50])
    sl = o.blackboard.value("shot.list")
    check("G9 黑板上有 shot.list 且为 ShotList",
          isinstance(sl, ShotList) and sl.shot_count > 0,
          f"{getattr(sl, 'shot_count', '?')} 镜")
    check("G10 shot.list 归属分镜师",
          o.owner_of("shot.list") == "storyboard_artist")

    # 审片
    reviews, why = o.review_artifact("shot.image", generator_id="cinematographer",
                                     payload={"shot.list": sl},
                                     reviewers=("review_board",))
    check("G11 审片阶段成功返回意见", not why and len(reviews) == 1, why[:60])
    check("G12 审片结论为 PASS", reviews and reviews[0].verdict is Verdict.PASS)
    check("G13 审片与生成跨族",
          reviews and reviews[0].cross_family_ok)

    # 可观测性
    spans = o.runtime.spans()
    # ⚠️ 原断言是 `>= 8`，那是按"剧本恰好 2 轮"写死的。
    # 2026-10-17 阈值降到 65 后本夹具 1 轮通过（见 G2 的说明），
    # span 变成 5 个 —— 固定下界就失效了。
    #
    # 改成**与轮数挂钩**：下游（分镜等）固定若干 span，
    # 剧本每多一轮至少多一个。这样它验的是"每次调用都留痕"，
    # 而不是"这一集恰好调用了 8 次"——**后者是夹具的偶然属性，不是契约**。
    check("G14 全链路留痕（每个阶段/每轮都有 span）",
          len(spans) >= 4 + cycle.round_count,
          f"{len(spans)} span / {cycle.round_count} 轮")
    check("G15 黑板含全部关键产物",
          {"script.final", "script.draft", "shot.list",
           "review.verdict"} <= set(o.snapshot()["blackboard"]),
          str(sorted(o.snapshot()["blackboard"])))

    # 产物归属：不自我签收的账本依据
    own = o.snapshot()["ownership"]
    check("G16 每个产物的归属都不等于其审核者",
          own.get("script.draft") == "screenwriter"
          and own.get("shot.list") == "storyboard_artist"
          and own.get("review.verdict") == "review_board",
          str(own))


# ==========================================================================
# H. 桩的契约一致性
# ==========================================================================

def _contract() -> None:
    check("H1 注册了恰好 5 个核心职位桩", len(CORE_CREW_STUBS) == 5,
          str(sorted(CORE_CREW_STUBS)))
    expect = {"screenwriter", "dramaturge", "story_editor",
              "storyboard_artist", "review_board"}
    check("H2 覆盖的正是名册里的 5 个核心职位",
          set(CORE_CREW_STUBS) == expect, str(sorted(set(CORE_CREW_STUBS) ^ expect)))

    # 所有桩都必须是 impl(*, envelope, runtime) 签名
    import inspect

    bad: list[str] = []
    for name, cls in CORE_CREW_STUBS.items():
        sig = inspect.signature(cls.__call__)
        params = set(sig.parameters)
        if not {"envelope", "runtime"} <= params:
            bad.append(name)
        # 不应有 **kwargs 兜底 —— 那会让签名不匹配静默通过
        if any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in sig.parameters.values()):
            bad.append(name + "(**kwargs)")
    check("H3 所有桩都是 impl(*, envelope, runtime) 签名（无 **kwargs 兜底）",
          not bad, str(bad))

    # 桩的产出键必须落在名册声明的 produces 内
    from little_crew.agent.roster import DEFAULT_ROSTER

    spec = {a.agent_id: a for a in DEFAULT_ROSTER}
    mism: list[str] = []
    for name in CORE_CREW_STUBS:
        declared = set(spec[name].produces)
        if name == "screenwriter":
            want = {"script.draft"}
        elif name == "dramaturge":
            want = {"script.critique"}
        elif name == "story_editor":
            want = {"style.guard"}
        elif name == "storyboard_artist":
            want = {"shot.list"}
        else:
            want = set(spec[name].produces)      # review_board 声明即 review.verdict
        if name == "review_board":
            want = {"review.verdict"}
        if not want <= declared:
            mism.append(f"{name}: {want - declared}")
    check("H4 桩的产出键都在名册声明的 produces 内", not mism, str(mism))

    # 桩应被编排器的审查者表覆盖
    from little_crew.agent.orchestrator import _REVIEW_FEEDBACK_KEY

    check("H5 审查者都有反馈键映射（否则取不到意见）",
          {"dramaturge", "story_editor", "review_board"} <= set(_REVIEW_FEEDBACK_KEY),
          str(sorted(_REVIEW_FEEDBACK_KEY)))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("5 个核心职位规则桩 —— 自检")
    print("=" * 78)

    _parsing()
    _screenwriter()
    _dramaturge()
    _story_editor()
    _storyboard()
    _review_board()
    _pipeline()
    _contract()
    _completeness_length_independent()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1



# ---------- I. 完整性必须与剧本长度无关（2026-10-17）----------
#
# ## 这一段守什么
#
# 2026-10-17 之前，完整性用的是**比例**：
#     completeness = 100 * (1 - 缺陷场次/总场次)
#
# 条件对比实验（research/threshold-calibration/）实测到两个后果：
#   · 同一份素材切成 2 场后，总分比 5 场低 **17.7 分** —— 同样的绝对缺陷量
#   · 2 场里两场都有缺陷 -> 完整性归零 -> 总分固定 41.4，
#     5 次里 4 次停在这个"地板"上 —— 那是地板的形状，不是质量分布
#
# 已改成**按场次的绝对惩罚**（`COMPLETENESS_PENALTY_PER_SCENE`）。
# 这一段钉住的正是那个修复的性质：**1 个缺陷场次，在任何长度的剧本里
# 都该扣同样多的完整性分**。
#
# ⚠️ 它**不**主张"2 场剧本合格"—— 只主张"缺陷的代价可比"。
# "一集只有 2 场"是不是问题，是另一个独立判断，不该藏在比例公式里。

def _completeness_length_independent() -> None:
    from little_crew.agent.crew_stubs import DramaturgeRules
    from little_crew.agent.messages import COMPLETENESS_PENALTY_PER_SCENE

    # 三类结构信号都齐的"完整"场；以及一个都不含的"不完整"场。
    RICH = "她拒绝了，但那要付出代价。忽然她转身离开。"
    FLAT = "屋里很安静，光线柔和，她坐着。"

    def _draft(n: int, bad: int) -> ScriptDraft:
        scenes = []
        for i in range(n):
            text = FLAT if i < bad else RICH
            scenes.append({"id": f"S{i+1:02d}", "heading": "内景",
                           "characters": ("甲",), "text": text})
        return ScriptDraft(frm="screenwriter", to="dramaturge",
                           episode_id="E900", title="T",
                           scenes=tuple(scenes), revision=1)

    def _comp(n: int, bad: int) -> float:
        p = {"script.draft": _draft(n, bad)}
        out = DramaturgeRules()(envelope=CallEnvelope.root(
            "dramaturge", "script.critique", p, Budget.unlimited(),
            callee_kind="agent"), runtime=None)          # type: ignore[arg-type]
        assert out.ok, out.error_message
        fb = out.output["script.critique"]
        return float(fb.score.dimensions[Dimension.COMPLETENESS])

    expect = 100.0 - COMPLETENESS_PENALTY_PER_SCENE
    c2 = _comp(2, 1)
    c8 = _comp(8, 1)
    check("I1 ★ 1 个缺陷场次：2 场与 8 场得到**相同**完整性",
          abs(c2 - c8) < 0.01, f"2 场={c2}  8 场={c8}")
    check(f"I2 该值等于 100 - 惩罚({COMPLETENESS_PENALTY_PER_SCENE})",
          abs(c2 - expect) < 0.01, f"{c2} vs {expect}")

    # 无缺陷时满分；缺陷翻倍时线性下降（**不再有"短剧本撞 0"**）
    check("I3 无缺陷 -> 100", abs(_comp(2, 0) - 100.0) < 0.01)
    check("I4 2 场全缺 -> 50（旧比例公式会给 0）",
          abs(_comp(2, 2) - (100.0 - 2 * COMPLETENESS_PENALTY_PER_SCENE)) < 0.01,
          f"{_comp(2, 2)}")
    check("I5 4 个缺陷场次 -> 归零，不会变成负数",
          _comp(5, 4) == 0.0, f"{_comp(5, 4)}")


if __name__ == "__main__":
    raise SystemExit(main())
