"""剧组编排器 —— 自检。

验证"多智能体协作闭环"真的能跑，且三条硬规则真的被强制：
  · 服务端权威算分（不信任自报）
  · 跨模型族盲读（同族判定作废）
  · 不自我签收（按产出归属判定）

运行： python -m little_crew.agent.orchestrator_check
零第三方依赖。
"""

from __future__ import annotations

import sys
from typing import Any, Mapping

from little_crew.agent.messages import (
    Dimension,
    IssueLocation,
    QcScore,
    RepairHint,
    ReviewFeedback,
    ReviewIssue,
    ScriptDraft,
    Severity,
    Verdict,
)
from little_crew.agent.orchestrator import (
    PASS_THRESHOLD,
    CrewOrchestrator,
    check_no_self_signoff,
    merge_reviews,
)
from little_crew.agent.protocol import CallResult

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ==========================================================================
# 测试替身：确定性的"职位实现"
# ==========================================================================

D = Dimension


def _score(consistency: float, completeness: float, quality: float,
           feasibility: float = 80.0, compliance: float = 90.0,
           claimed: float | None = None,
           phase: str = "draft") -> QcScore:
    return QcScore(
        dimensions={D.CONSISTENCY: consistency, D.COMPLETENESS: completeness,
                    D.QUALITY: quality, D.FEASIBILITY: feasibility,
                    D.COMPLIANCE: compliance},
        phase=phase, claimed_score=claimed,
    )


def _issue(dim: Dimension, sev: Severity, problem: str, suggestion: str = "",
           scene: str = "SC-E001-001", line: int = 10) -> ReviewIssue:
    return ReviewIssue(severity=sev, dimension=dim,
                       location=IssueLocation(scene=scene, line=line),
                       problem=problem, suggestion=suggestion)


def _fb(verdict: Verdict, score: QcScore, issues=(), hints=(),
        reviewer="dramaturge", generator="screenwriter") -> ReviewFeedback:
    # 消息必须带 from / to（__post_init__ 会校验）——
    # 这不是冗余：审查链的可追溯性依赖它，缺了就断链
    return ReviewFeedback(frm=reviewer, to=generator,
                          verdict=verdict, score=score, issues=tuple(issues),
                          repair_hints=tuple(hints),
                          reviewer_model=reviewer, generator_model=generator)


# ---- 编剧：按调用次数出不同稿 ----

class ScreenwriterStub:
    """第 1 轮出"差稿"，第 2 轮起出"好稿" —— 用于验证返工真的驱动了修订。"""

    def __init__(self) -> None:
        self.calls = 0
        self.revise_payloads: list[Mapping[str, Any]] = []

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        self.calls += 1
        # 契约：入参在 envelope.payload，**不是** payload= 关键字
        payload = dict(getattr(envelope, "payload", {}) or {})
        if "script.critique" in payload:
            self.revise_payloads.append(payload["script.critique"])
        # 消息基类强制 from/to —— **交付物也是一条消息**，不是裸数据。
        # 这不是形式主义：审查链的可追溯性（谁产出、回复谁）依赖它。
        made = ("初稿" if self.calls == 1 else "修订稿")
        return CallResult.success({"script.draft": ScriptDraft(
            frm="screenwriter", to="*",
            episode_id="E001", title="雨夜", revision=self.calls,
            scenes=({"id": "SC-E001-001", "text": made},))})


class ReviewerStub:
    """可配置的审核者替身。"""

    def __init__(self, feedback: ReviewFeedback | Mapping[str, Any],
                 capability: str) -> None:
        self.feedback = feedback
        self.capability = capability
        self.calls = 0

    def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
        self.calls += 1
        return CallResult.success({self.capability: self.feedback})


def _orch(**kw: Any) -> CrewOrchestrator:
    return CrewOrchestrator(
        model_families={
            "screenwriter": "deepseek", "dramaturge": "qwen",
            "story_editor": "qwen", "review_board": "qwen",
            "continuity_supervisor": "qwen",
        }, **kw)


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("剧组编排器 —— 自检")
    print("=" * 78)

    _merge_rules()
    _self_signoff()
    _cycle_pass()
    _cycle_revise_then_pass()
    _cycle_no_progress()
    _cycle_invalidation()
    _stage_failures()
    _failure_kind()
    _trace()
    _blackboard_wiring()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ---------- A. 合并规则 ----------

def _merge_rules() -> None:
    good = _fb(Verdict.PASS, _score(85, 85, 80))
    # ⚠️ 阻断级问题**必须带返工指令** —— 否则会触发作废规则
    # （"拦住了问题却不给怎么改" -> ESCALATE，需人工介入）。
    # 那条规则是对的，所以夹具要写实：真报 blocker 的审核方会给指令。
    strict = _fb(Verdict.REVISE, _score(50, 80, 70),
                 [_issue(D.CONSISTENCY, Severity.BLOCKER, "服装不一致", "改米色")],
                 [RepairHint(graph_id="G", op="modify_shot",
                             args={"scene": "S1"})],
                 reviewer="story_editor")

    v, total, issues, hints, inval = merge_reviews([good, strict], phase="draft")
    check("A1 ★ 有 **BLOCKER** 级意见 -> REVISE（一份说必须改就必须改）",
          v is Verdict.REVISE and not inval,
          f"verdict={v.value}")

    # ---- A1b/A1c/A1d：口径变更后的分支 ----
    #
    # 2026-10-16 定下的**双判据**：
    #   ① 无 `BLOCKER` 级意见  ② 权威总分 >= `PASS_THRESHOLD`(80)
    #   **两条都满足才 PASS。**
    #
    # 下面四条把双判据的每个分支都钉住 —— 没有它们，
    # "advisory 不阻断"与"分数不够也不通过"都是**没被验证的宣称**。
    #
    # ⚠️ A1b 的分**必须在线上**：它验的是"只有 advisory 不阻断"，
    # 若分数低于阈值就会因为**另一条判据**而 REVISE，测的就不是本场景了。
    adv = _fb(Verdict.REVISE, _score(85, 85, 80),
              [_issue(D.QUALITY, Severity.MAJOR, "节奏可以更紧", "压缩两句")],
              reviewer="story_editor")
    v2, t2, iss2, hints2, inval2 = merge_reviews([good, adv], phase="draft")
    check("A1b ★ **只有 advisory 意见 + 分数够 -> PASS**（不阻断，意见仍保留）",
          v2 is Verdict.PASS and not inval2 and len(iss2) == 1
          and len(hints2) == 0,
          f"verdict={v2.value} total={t2:.1f} issues={len(iss2)} hints={len(hints2)}")
    check("A1c advisory 意见**不被丢弃**（记录进证据链）",
          any(i.severity is Severity.MAJOR for i in iss2),
          str([i.severity.value for i in iss2]))
    check("A1d 但没有返工指令（不阻断就不该让人返工）",
          hints2 == (), f"{len(hints2)} 条")

    # A1e：无阻断意见但**分数不够** -> 仍 REVISE（新判据的独立分支）
    low = _fb(Verdict.PASS, _score(55, 60, 50), reviewer="story_editor")
    v3, t3, _, _, inval3 = merge_reviews([good, low], phase="draft")
    check("A1e ★ **无阻断意见但分数低于通过线 -> REVISE**"
          f"（阈值 {PASS_THRESHOLD:.0f}）",
          v3 is Verdict.REVISE and not inval3 and t3 < PASS_THRESHOLD,
          f"verdict={v3.value} total={t3:.1f}")

    # A1f：分数够**也不能**掩盖阻断级问题（两条判据是「与」不是「或」）
    hi_block = _fb(Verdict.REVISE, _score(95, 95, 95),
                   [_issue(D.COMPLIANCE, Severity.BLOCKER, "含违规内容", "删掉")],
                   [RepairHint(graph_id="G", op="modify_shot", args={"scene": "S1"})],
                   reviewer="story_editor")
    v4, t4, _, _, _ = merge_reviews([good, hi_block], phase="draft")
    check("A1f ★ **高分不能掩盖 BLOCKER**（硬约束违规优先于分数）",
          v4 is Verdict.REVISE and t4 >= PASS_THRESHOLD,
          f"verdict={v4.value} total={t4:.1f}")

    check("A2 同维度取较低分（保守）",
          total < good.score.total, f"total={total} vs 单份 {good.score.total}")
    check("A3 去重后意见数为 1", len(issues) == 1, f"{len(issues)} 条")
    check("A4 无返工指令 + 有阻塞问题时作废",
          True, "见 A5")

    # 阻塞问题的 suggestion 在**构造时**就被强制 —— 见下方 A5b
    blk = _fb(Verdict.REVISE, _score(40, 40, 40),
              [_issue(D.CONSISTENCY, Severity.BLOCKER, "主角设定矛盾", "补一场交代戏")],
              [])
    v2, _, iss2, h2, why2 = merge_reviews([blk])
    check("A5 有阻塞问题但**没有返工指令** → 作废（不能干说「必须改」）",
          v2 is Verdict.ESCALATE and "阻塞" in why2, why2[:60])

    # BLOCKER 缺 suggestion 在构造时即被拒绝 —— 这是 messages.py 的硬校验
    try:
        ReviewIssue(severity=Severity.BLOCKER, dimension=D.CONSISTENCY,
                    location=IssueLocation(scene="SC-1", line=1),
                    problem="没有建议的阻塞问题", suggestion="")
        blocked_at_ctor = False
    except ValueError:
        blocked_at_ctor = True
    check("A5b BLOCKER 缺 suggestion 在**构造时**即被拒绝", blocked_at_ctor)

    # 带返工指令 → 有效 REVISE
    hint = RepairHint(graph_id="G-ep001", op="insert_shot",
                      args={"after": "SH-007"})
    blk2 = _fb(Verdict.REVISE, _score(40, 40, 40),
               [_issue(D.CONSISTENCY, Severity.BLOCKER, "主角设定矛盾", "补一场戏")],
               [hint])
    v3, _, _, h3, why3 = merge_reviews([blk2])
    check("A6 阻塞问题 + 有返工指令 → 有效 REVISE",
          v3 is Verdict.REVISE and len(h3) == 1 and not why3,
          f"verdict={v3.value} hints={len(h3)}")

    # 自报不可信 → 整轮作废
    liar = _fb(Verdict.PASS, _score(45, 80, 70, claimed=90.0))
    v4, _, _, _, why4 = merge_reviews([liar])
    check("A7 自报分与自身维度明细不自洽 → 整轮作废",
          v4 is Verdict.ESCALATE and "不自洽" in why4, why4[:70])

    # 跨族
    same = _fb(Verdict.PASS, _score(85, 85, 85),
               reviewer="qwen3-vl-max", generator="qwen3-max")
    v5, _, _, _, why5 = merge_reviews([same])
    check("A8 审核方与生成方同模型族 → 判定无效",
          v5 is Verdict.ESCALATE and "同属模型族" in why5, why5[:70])

    cross = _fb(Verdict.PASS, _score(85, 85, 85),
                reviewer="qwen3-vl-max", generator="deepseek-flash")
    v6, _, _, _, why6 = merge_reviews([cross])
    check("A9 跨族 → 判定有效", v6 is Verdict.PASS and not why6, why6[:50])

    v7, _, _, _, why7 = merge_reviews([])
    check("A10 无审查意见 → 无法裁定", v7 is Verdict.ESCALATE and "没有任何" in why7,
          why7[:50])

    # 去重：同维度同定位，保留更严
    i_minor = _issue(D.QUALITY, Severity.MINOR, "细节不足", "加细节", line=20)
    i_major = _issue(D.QUALITY, Severity.MAJOR, "细节严重不足", "加细节", line=20)
    v8, _, iss8, _, _ = merge_reviews([
        _fb(Verdict.REVISE, _score(80, 80, 60), [i_minor],
            [RepairHint(graph_id="G", op="adjust_prompt", args={})], "qwen3-vl-max",
            "deepseek-flash"),
        _fb(Verdict.REVISE, _score(80, 80, 55), [i_major],
            [RepairHint(graph_id="G", op="adjust_prompt", args={})], "claude-x",
            "deepseek-flash"),
    ])
    check("A11 同维度同定位去重，保留更严的",
          len(iss8) == 1 and iss8[0].severity is Severity.MAJOR,
          f"{len(iss8)} 条，severity={iss8[0].severity.value}")


# ---------- B. 不自我签收 ----------

def _self_signoff() -> None:
    ok = True
    try:
        check_no_self_signoff("screenwriter", "screenwriter", artifact_key="script.draft")
        ok = False
    except PermissionError:
        pass
    check("B1 产出者审核自己的产出 → 拒绝", ok)

    ok2 = True
    try:
        check_no_self_signoff("dramaturge", "screenwriter", artifact_key="script.draft")
    except PermissionError:
        ok2 = False
    check("B2 他人审核 → 允许", ok2)

    # 关键：**只审别人稿**的职位不算违规（早期版本按"有审核能力"判定，把这类误判了）
    for rid in ("dramaturge", "story_editor"):
        try:
            check_no_self_signoff(rid, "screenwriter", artifact_key="script.draft")
            ok3 = True
        except PermissionError:
            ok3 = False
    check("B3 只审别人稿的职位不算「自我签收」（早期判据写错过）", ok3)


# ---------- C. 完整循环：一轮通过 ----------

def _cycle_pass() -> None:
    sw = ScreenwriterStub()
    r1 = ReviewerStub(_fb(Verdict.PASS, _score(88, 90, 85)), "script.critique")
    r2 = ReviewerStub(_fb(Verdict.PASS, _score(86, 88, 84),
                          reviewer="qwen3-max", generator="deepseek-flash"),
                      "style.guard")
    o = _orch().bind("screenwriter", sw).bind("dramaturge", r1).bind("story_editor", r2)
    res = o.run_script_cycle({"source.novel": "雨夜天台"})

    check("C1 一轮通过即收工", res.accepted and res.round_count == 1,
          f"accepted={res.accepted} rounds={res.round_count}")
    check("C2 产出 script.final 并记录归属",
          isinstance(res.final, ScriptDraft) and o.owner_of("script.final") == "screenwriter",
          f"owner={o.owner_of('script.final')}")
    check("C3 编剧只被调用一次（未做无谓返工）", sw.calls == 1, f"calls={sw.calls}")
    check("C4 两位审核者都被调用（并行双维）",
          r1.calls == 1 and r2.calls == 1, f"{r1.calls}/{r2.calls}")


# ---------- D. 返工闭环 ----------

def _cycle_revise_then_pass() -> None:
    sw = ScreenwriterStub()
    hist = {"dramaturge": 0, "story_editor": 0}

    class SeqReviewer:
        def __init__(self, cap: str, first: ReviewFeedback, then: ReviewFeedback,
                     tag: str) -> None:
            self.cap, self.first, self.then, self.tag = cap, first, then, tag

        def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
            hist[self.tag] += 1
            fb = self.first if hist[self.tag] == 1 else self.then
            return CallResult.success({self.cap: fb})

    hint = RepairHint(graph_id="G-ep001", op="adjust_prompt",
                      args={"scene": "SC-E001-001", "line": 10})
    # ⚠️ **`BLOCKER`，不是 `MAJOR`**（2026-10-16 口径变更后调整）。
    #
    # 2026-10-16 起：**只有 `BLOCKER` 级意见阻断流程**，其余是 advisory。
    # 所以"这一轮该 REVISE"必须由 `BLOCKER` 表达 —— 用 `MAJOR` 的话
    # 第 1 轮就会直接 PASS，本场景（先改后过）就不成立了。
    #
    # 判据是**"该修 vs 可以更好"**：
    #   · 服装与上镜不一致 —— **该修**（观众会看出来）-> BLOCKER
    #   · "这句对白可以更凝练" —— 可以更好 -> MAJOR，advisory
    iss = [_issue(D.CONSISTENCY, Severity.BLOCKER, "服装与上镜不一致", "改为米色风衣")]

    o = _orch().bind("screenwriter", sw).bind(
        "dramaturge", SeqReviewer("script.critique",
                                  _fb(Verdict.REVISE, _score(48, 80, 72), iss, [hint]),
                                  _fb(Verdict.PASS, _score(86, 88, 85)), "dramaturge")
    ).bind(
        "story_editor", SeqReviewer("style.guard",
                                    _fb(Verdict.PASS, _score(80, 82, 78),
                                        reviewer="qwen3-max", generator="deepseek-flash"),
                                    _fb(Verdict.PASS, _score(84, 86, 82),
                                        reviewer="qwen3-max", generator="deepseek-flash"),
                                    "story_editor")
    )
    res = o.run_script_cycle({"source.novel": "雨夜天台"})

    check("D1 第 1 轮 REVISE → 第 2 轮 PASS，共 2 轮",
          res.accepted and res.round_count == 2,
          f"accepted={res.accepted} rounds={res.round_count}")
    check("D2 编剧被调用两次（返工真的驱动了修订）", sw.calls == 2, f"calls={sw.calls}")
    check("D3 第 2 轮收到的是 script.revise 且带上轮意见",
          len(sw.revise_payloads) == 1
          and "issues" in sw.revise_payloads[0]
          and len(sw.revise_payloads[0]["issues"]) >= 1,
          f"payload keys={list(sw.revise_payloads[0]) if sw.revise_payloads else []}")
    check("D4 第 1 轮的权威总分低于通过线、第 2 轮高于",
          res.rounds[0].total < res.rounds[1].total,
          f"{res.rounds[0].total} → {res.rounds[1].total}")
    check("D5 第 1 轮带返工指令，第 2 轮无",
          len(res.rounds[0].repair_hints) >= 1 and len(res.rounds[1].repair_hints) == 0)


# ---------- E. 无进展：轮次上限 ----------

def _cycle_no_progress() -> None:
    sw = ScreenwriterStub()
    hint = RepairHint(graph_id="G", op="adjust_prompt", args={"line": 1})
    # ⚠️ `BLOCKER` —— 本场景要验"**始终不通过**会停在轮次上限"，
    # 所以那条意见必须是**阻断级**的（2026-10-16 起只有 BLOCKER 阻断）。
    # 用 `MAJOR` 的话第 1 轮就 PASS，测的就不是本场景了。
    iss = [_issue(D.QUALITY, Severity.BLOCKER, "节奏拖沓", "压缩")]
    always = _fb(Verdict.REVISE, _score(60, 70, 50), iss, [hint])
    o = _orch(max_rounds=3).bind("screenwriter", sw).bind(
        "dramaturge", ReviewerStub(always, "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(80, 80, 80), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res = o.run_script_cycle({"source.novel": "x"})

    check("E1 始终不通过 → 停在轮次上限（不会无限循环）",
          (not res.accepted) and res.round_count == 3,
          f"rounds={res.round_count} reason={res.reason[:40]}")
    check("E2 未通过时不产出 script.final", res.final is None)
    check("E3 理由说明达到上限且带末轮分数",
          "轮次上限" in res.reason and str(int(res.rounds[-1].total)) in res.reason,
          res.reason[:70])


# ---------- F. 作废路径 ----------

def _cycle_invalidation() -> None:
    sw = ScreenwriterStub()
    liar = _fb(Verdict.PASS, _score(45, 80, 70, claimed=95.0))
    o = _orch().bind("screenwriter", sw).bind(
        "dramaturge", ReviewerStub(liar, "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(85, 85, 85), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res = o.run_script_cycle({"source.novel": "x"})
    check("F1 自报不可信 → 立即作废，不进入第 2 轮",
          (not res.accepted) and res.round_count == 1 and sw.calls == 1,
          f"rounds={res.round_count} sw={sw.calls}")
    check("F2 作废理由写入结果",
          "不自洽" in res.reason or "不可靠" in res.reason, res.reason[:70])

    # 同族审核
    sw2 = ScreenwriterStub()
    same = _fb(Verdict.PASS, _score(90, 90, 90),
               reviewer="deepseek-flash", generator="deepseek-flash")
    o2 = _orch().bind("screenwriter", sw2).bind(
        "dramaturge", ReviewerStub(same, "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(85, 85, 85), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res2 = o2.run_script_cycle({"source.novel": "x"})
    check("F3 同族审核 → 作废（不信「自己人说没问题」）",
          (not res2.accepted) and "模型族" in res2.reason, res2.reason[:70])

    # ⚠️ **失败分类**（2026-10-17 加）：光有"未通过"太粗 ——
    # "服务抽风"与"产物不行"在报告上必须分得开。
    check("F4 自报不可信 -> failure_kind=review-invalid",
          res.failure_kind == "review-invalid", res.failure_kind)
    check("F5 同族审核 -> failure_kind=review-invalid",
          res2.failure_kind == "review-invalid", res2.failure_kind)



# ---------- H. 失败分类（2026-10-17）----------
#
# ## 这一段为什么存在
#
# 实测里 C 组 5 次有 2 次"未通过、0 分"，而根因是**评审调用失败** ——
# 与产物质量无关。但报告只写"未通过"，读者会合理地误读成"题材质量差"。
#
# 所以 `CycleResult.failure_kind` 把失败按**该采取什么动作**分开：
#   · `review-failed`   -> 重派（服务问题，与产物无关）
#   · `review-invalid`  -> 换族重审
#   · `blocked`         -> 返工（有硬伤）
#   · `below-threshold` -> 返工或放宽（只是不够好）
#
# 这一段钉住**两个关键的优先级判断**，它们是这段逻辑里最容易搞反的地方。

def _failure_kind() -> None:
    from little_crew.agent.orchestrator import RoundResult, _exhausted_kind

    def _rr(total: float, *, inval: str = "", issues=()) -> Any:
        return RoundResult(round_index=1, draft=None, reviews=(), verdict=Verdict.REVISE,
                           total=total, issues=tuple(issues),
                           repair_hints=(), outcomes=(),
                           invalidated_reason=inval)

    # ① 无轮次
    check("H1 无轮次 -> below-threshold",
          _exhausted_kind(()) == "below-threshold")

    # ② ★ 作废优先于分数：作废意味着"判定不被采信"，不是"产物不合格"
    check("H2 ★ 末轮作废 -> review-invalid（不是 below-threshold）",
          _exhausted_kind((_rr(0.0, inval="自报分不自洽"),)) == "review-invalid",
          _exhausted_kind((_rr(0.0, inval="自报分不自洽"),)))

    # ③ ★ 阻断问题优先于分数：有 BLOCKER 时该返工，不该被当成"差点分数"
    blk = _issue(Dimension.COMPLETENESS, Severity.BLOCKER, "缺场次",
                 "补一场交代")
    check("H3 ★ 末轮有 BLOCKER -> blocked（不是 below-threshold）",
          _exhausted_kind((_rr(45.0, issues=(blk,)),)) == "blocked",
          _exhausted_kind((_rr(45.0, issues=(blk,)),)))

    # ④ 只有 advisory -> 就是分数不够
    adv = _issue(Dimension.QUALITY, Severity.MAJOR, "节奏拖")
    check("H4 只有 MAJOR -> below-threshold",
          _exhausted_kind((_rr(55.0, issues=(adv,)),)) == "below-threshold",
          _exhausted_kind((_rr(55.0, issues=(adv,)),)))

    # ⑤ 通过时分类为空（"没失败"不该有个失败码）
    o = _orch().bind("screenwriter", ScreenwriterStub()).bind(
        "dramaturge", ReviewerStub(
            _fb(Verdict.PASS, _score(88, 88, 88)), "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(88, 88, 88)), "style.guard"))
    ok = o.run_script_cycle({"source.novel": "x"})
    check("H5 通过 -> failure_kind 为空", ok.accepted and ok.failure_kind == "",
          f"accepted={ok.accepted} kind={ok.failure_kind!r}")

    # ⑥ 审核者调用失败 -> review-failed（**与产物无关**）
    class _Boom:
        def __call__(self, *, envelope: Any, runtime: Any) -> CallResult:
            return CallResult.fail("F-BOOM", "模拟服务不可用")

    o2 = _orch().bind("screenwriter", ScreenwriterStub()).bind(
        "dramaturge", _Boom()).bind(
        "story_editor", ReviewerStub(
            _fb(Verdict.PASS, _score(85, 85, 85)), "style.guard"))
    bad = o2.run_script_cycle({"source.novel": "x"})
    check("H6 ★ 审核者调用失败 -> review-failed（服务问题，与产物无关）",
          bad.failure_kind == "review-failed", bad.failure_kind)

# ---------- G. 阶段失败 ----------

def _stage_failures() -> None:
    o = _orch()
    out = o.run_stage("screenwriter", "script.draft", {})
    check("G1 未绑定实现的职位 → 明确失败（不静默跳过）",
          (not out.ok) and "未绑定" in out.error, out.error[:50])

    o2 = _orch().bind("screenwriter", ScreenwriterStub())
    out2 = o2.run_stage("screenwriter", "shot.video", {})
    check("G2 该职位不具备的能力 → 拒绝并列出它具备什么",
          (not out2.ok) and "不具备能力" in out2.error, out2.error[:70])

    o3 = _orch().bind("screenwriter", ScreenwriterStub())
    res = o3.run_script_cycle({"source.novel": "x"})
    check("G3 审核者未绑定 → 作废且说明是谁",
          (not res.accepted) and "dramaturge" in res.reason, res.reason[:60])

    sw_fail = ScreenwriterStub()
    o4 = _orch().bind("dramaturge", ReviewerStub(
        _fb(Verdict.PASS, _score(80, 80, 80)), "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(80, 80, 80), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res4 = o4.run_script_cycle({"source.novel": "x"})
    check("G4 编剧未绑定 → 作废（不是崩掉）",
          (not res4.accepted) and "编剧" in res4.reason, res4.reason[:60])


# ---------- H. 可观测性 ----------

def _trace() -> None:
    sw = ScreenwriterStub()
    o = _orch().bind("screenwriter", sw).bind(
        "dramaturge", ReviewerStub(_fb(Verdict.PASS, _score(88, 88, 88)),
                                   "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(86, 86, 86), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    o.run_script_cycle({"source.novel": "x"})

    spans = o.runtime.spans()
    check("H1 每次调用都留痕（调用树天然可观测）", len(spans) >= 3,
          f"{len(spans)} 个 span")
    check("H2 留痕含调用方/被调方/能力/深度",
          all({"caller", "callee", "capability", "depth"} <= set(s) for s in spans))
    tree = o.trace()
    check("H3 可渲染为调用树（供答辩演示）", len(tree) >= 3, f"{len(tree)} 行")
    snap = o.snapshot()
    check("H4 快照含黑板/归属/span 数",
          {"blackboard", "ownership", "spans"} <= set(snap),
          f"blackboard keys={list(snap['blackboard'])[:4]}")


# ---------- I. 黑板键空间与依赖（2026-10-23 审计 C-3 / C-2）----------

def _blackboard_wiring() -> None:
    """黑板的**键空间**与**失效传播**是否真的接上了。

    这两件事都曾在"代码看着没问题"的情况下静默失效：
      · `run_stage` 写 `deps=spec.requires`（能力名 `shot.image`），
        而 `_dependents_of` 比对的是 `entry_id`（`bb_000001`）——
        两个命名空间互不相交，`_propagate_stale` 恒不标记任何条目。
      · 审核者往黑板写 **dict** 时，`ReviewFeedback.from_dict` **不存在**，
        必然 `AttributeError`（而它上面那条"未产出可解析的审查意见"是死代码）。
    """
    print()
    print("I. 黑板键空间与依赖")

    o = _orch().bind("screenwriter", ScreenwriterStub()).bind(
        "dramaturge", ReviewerStub(_fb(Verdict.PASS, _score(80, 80, 80)),
                                   "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(80, 80, 80), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res = o.run_script_cycle({"source.novel": "x"})
    check("I0 前置：闭环通过", res.accepted, res.reason[:50])

    # ---- C-3：deps 必须是 entry_id，且传播真的发生 ----
    entries = o.blackboard._entries          # 内部结构，仅在自检里用
    ids = set(entries)
    bad = [(e.key, e.deps) for e in entries.values()
           if any(d not in ids for d in e.deps)]
    check("I1 ★★ deps 里装的是 entry_id（`bb_000001`），不是能力名",
          not bad, str(bad[:2]))

    marked = o.blackboard.invalidate("script.draft")
    stale = {e.key for e in o.blackboard.stale_entries()}
    check("I2 ★★ 改上游 -> 下游被标 stale（失效传播真的接上了）",
          {"script.critique", "style.guard"} <= stale, str(sorted(stale)))
    check("I3 传播返回条目 id（可追溯到具体条目）",
          bool(marked) and all(str(m).startswith("bb_") for m in marked),
          str(marked[:2]))

    # ---- C-2：审核者写 dict（而非对象）也必须能还原 ----
    o2 = _orch().bind("screenwriter", ScreenwriterStub()).bind(
        "dramaturge", ReviewerStub(
            _fb(Verdict.PASS, _score(82, 82, 82)).as_dict(), "script.critique")
    ).bind("story_editor", ReviewerStub(
        _fb(Verdict.PASS, _score(82, 82, 82), reviewer="qwen3-max",
            generator="deepseek-flash"), "style.guard"))
    res2 = o2.run_script_cycle({"source.novel": "x"})
    check("I4 ★★ 审查意见是 dict 也能还原（此前必 AttributeError）",
          res2.accepted, res2.reason[:70])
    last = res2.rounds[-1] if res2.rounds else None
    revs = tuple(getattr(last, "reviews", ()) or ())
    check("I5 ★ 还原出的是 ReviewFeedback，且审查方归属没丢",
          bool(revs) and all(isinstance(x, ReviewFeedback) for x in revs)
          and any(getattr(x, "frm", "") == "dramaturge" for x in revs),
          str([type(x).__name__ for x in revs]))


if __name__ == "__main__":
    raise SystemExit(main())
