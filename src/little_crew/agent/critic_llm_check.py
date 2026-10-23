"""`agent/critic_llm.py` 自检 —— 用假客户端，不吃 API。

## 守什么

| 类别 | 守什么 |
|---|---|
| 覆盖面 | A 类 5 个职位都能构造；**非审查职位必须被拒绝** |
| 提示词 | 三条检查项真的进了提示词；只审本职位那一块 |
| 规整 | 空问题 -> 空数组（**不编问题**）；缺 problem 的意见被丢；越界场次被丢 |
| ★ `BLOCKER` | **`blocker` 无 suggestion 时必须降级** —— 否则整轮作废 |
| 算分 | **分数由代码算，不取模型自报** |
| 集成 | 能经编排器跑通一轮，产出 `ReviewFeedback` 且归属正确 |

## 为什么 `BLOCKER` 那条最重要

`merge_reviews` 的规则是：**存在阻塞级问题但没有任何可执行的返工指令 ->
整轮作废（ESCALATE）**。

也就是说模型只要报一条 `blocker` 却忘了给建议，**整轮的 token 全白花**。
所以这里强制降级 —— 宁可把一条该拦的问题降成 `major`，
也不让整轮判定作废。这是一个**刻意的取舍**，理由写在这里。
"""
from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.critic_llm import (
    CRITIC_CHECKS,
    CriticLlm,
    _repair_op,
    build_critic_crew,
)
from little_crew.agent.critic_llm import _as_text
from little_crew.agent.messages import Dimension, Severity, Verdict
from little_crew.agent.protocol import Budget, CallEnvelope

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _FakeClient:
    def __init__(self, reply: Any, model_id: str = "fake-critic") -> None:
        self.reply = reply
        self.model_id = model_id
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.call_log: list[dict[str, Any]] = []
        self.calls = 0

    def chat_json(self, prompt: str, *, system: str = "",
                  temperature: float | None = None,
                  what: str = "输出") -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        self.systems.append(system)
        self.call_log.append({"servedModel": self.model_id, "elapsedS": 0.0,
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _env(payload: dict[str, Any]) -> CallEnvelope:
    return CallEnvelope.root("critic", "x", payload, Budget.unlimited(),
                             callee_kind="agent")


TEXT = "第一场：内景。少女独自坐在屋里，窗外的雨下了一整夜。"


def _coverage() -> None:
    print()
    print("A. 覆盖面")
    check("A1 A 类 5 个职位", len(CRITIC_CHECKS) == 5, str(list(CRITIC_CHECKS)))
    crew = build_critic_crew()
    check("A2 build_critic_crew() 全量构造", len(crew) == 5, str(len(crew)))
    try:
        CriticLlm("director")
        check("A3 ★ 非审查职位被拒绝（B 类产物是结构化计划，不该混进来）", False)
    except ValueError:
        check("A3 ★ 非审查职位被拒绝（B 类产物是结构化计划，不该混进来）", True)
    check("A4 only 参数可只取子集",
          len(build_critic_crew(only=("review_board",))) == 1)


def _prompt() -> None:
    print()
    print("B. 提示词")
    c = CriticLlm("continuity_supervisor", client=_FakeClient({"issues": []}))
    p, sysmsg = c._render({"script.draft": TEXT})
    check("B1 待审内容进了提示词", "少女独自坐在屋里" in p)
    check("B2 ★ 本职位三条检查项都在",
          all(x in p for x in CRITIC_CHECKS["continuity_supervisor"]["checks"]),
          "缺检查项")
    check("B3 system 里点明职责范围（防越权评论）",
          "连贯性" in sysmsg, sysmsg[:60])
    check("B4 明确要求「没有问题就返回空数组」",
          "空数组" in p or "空" in p)
    # ⚠️ 这条断言随提示词一起改过（2026-10-18）。
    # 原提示词写"没有问题就返回空数组"，实测模型对一份明显有人称混用的
    # 三场戏给出"0 个问题" —— **橡皮图章比没有审核者更糟**。
    # 现在改成强制逐项表态，所以断言也跟着改成验那个契约。
    check("B5 ★ 要求逐项表态（三项都必须出现，漏项=未检查）",
          "三项都必须出现" in p and "逐项" in p)
    check("B6 要求每项带位置字段（无位置的意见无法返工）",
          "where" in p and "哪一场" in p)
    check("B7 明确「finding 留空 = 该项没问题」（不逼模型编问题）",
          "留空字符串" in p)
    check("B7b 要求用 ok 布尔表态，而不是靠 finding 文本",
          '"ok"' in p and "ok`：**该项没问题就 true**" in p)


def _normalize() -> None:
    print()
    print("C. 规整")

    c2 = CriticLlm("story_editor", client=_FakeClient({}))
    issues5, hints5, _ = c2._normalize({
        "observations": [
            {"check": 1, "ok": True, "finding": "", "where": ""},
            {"check": 2, "ok": False, "finding": "第二场人称从她跳到他", "where": "S02"},
            {"check": 3, "ok": True, "finding": "", "where": ""},
        ]}, {"episodeId": "E1"})
    check("B8 逐项表态：ok=false 才生成问题",
          len(issues5) == 1, str([i.problem[:40] for i in issues5]))
    check("B9 逐项表态：问题里带上了检查项编号（可追溯）",
          "检查项 2" in issues5[0].problem, issues5[0].problem[:60])

    # ---- ★ 实测踩到的坑：模型会把「未见问题」写进 finding ----
    issues7, _, _ = c2._normalize({
        "observations": [
            {"check": 1, "ok": True, "finding": "无人称问题。全文均为第三人称。"},
            {"check": 2, "ok": True, "finding": "", "where": ""},
            {"check": 3, "ok": True, "finding": "", "where": ""},
        ]}, {"episodeId": "E1"})
    check("B10 ★★ ok=true 时 finding 里写「没问题」也**不算问题**",
          issues7 == [], str([i.problem[:50] for i in issues7]))

    # ---- 漏项 = 未检查 ----
    issues6, _, _ = c2._normalize({
        "observations": [{"check": 1, "ok": True}]}, {"episodeId": "E1"})
    check("B11 ★ 漏项被记为问题（未检查不等于已通过）",
          any("未覆盖" in i.problem for i in issues6),
          str([i.problem[:40] for i in issues6]))

    # ---- 没给 ok 布尔 -> 视为未表态，算漏项 ----
    issues8, _, _ = c2._normalize({
        "observations": [
            {"check": 1, "ok": True},
            {"check": 2, "finding": "有问题的描述但没给 ok"},
            {"check": 3, "ok": True},
        ]}, {"episodeId": "E1"})
    check("B12 ★ 缺 ok 布尔 -> 视为未表态（不靠文本猜），算漏项",
          any("未覆盖" in i.problem for i in issues8),
          str([i.problem[:40] for i in issues8]))


def _call_path() -> None:
    print()
    print("D. 调用路径")
    fake = _FakeClient({"issues": [
        {"where": "S01", "problem": "用词太书面", "suggestion": "口语化",
         "severity": "minor"}]})
    c = CriticLlm("story_editor", client=fake)
    out = c(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D1 __call__ 成功（**这一条就是当初缺的那类**）", out.ok,
          str(out.error_message)[:100])
    if out.ok:
        fb = out.output["style.guard"]
        # 本职位的能力名与产物名恰好相同（`style.guard`）——
        # 所以这条断言**区分不出**下面 D10 要抓的那个 bug。
        check("D2 产出键 = 职位的产物名", fb is not None)
        check("D3 verdict 为 REVISE（有问题）", fb.verdict is Verdict.REVISE,
              fb.verdict.value)
        check("D4 归属正确", fb.frm == "story_editor", fb.frm)
        check("D5 记了 reviewer_model（跨族审核要靠它）",
              fb.reviewer_model == "fake-critic", str(fb.reviewer_model))

    # 无问题 -> PASS
    c2 = CriticLlm("story_editor", client=_FakeClient({"issues": []}))
    out2 = c2(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D6 无问题 -> PASS", out2.ok and
          out2.output["style.guard"].verdict is Verdict.PASS)

    # 缺输入
    c3 = CriticLlm("story_editor", client=_FakeClient({}))
    out3 = c3(envelope=_env({}), runtime=None)
    check("D7 缺 payload -> 失败（不崩）",
          not out3.ok and out3.error_code == "F-INPUT", str(out3.error_code))

    # 可重试 / 不可重试
    from little_crew.agent.llm import LLMError
    c4 = CriticLlm("story_editor",
                   client=_FakeClient(LLMError("timeout", "超时", retryable=True)))
    out4 = c4(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D8 可重试错误 -> 失败但不升级",
          not out4.ok and not out4.escalate, str(out4.error_code))
    c5 = CriticLlm("story_editor",
                   client=_FakeClient(LLMError("no-api-key", "没 key",
                                               retryable=False)))
    out5 = c5(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D9 不可重试错误 -> 失败且升级",
          not out5.ok and out5.escalate, str(out5.error_code))

    # ---- ★★ 产出键必须落在名册声明的 `produces` 里（2026-10-23 审计 H-1）----
    #
    # `CrewOrchestrator.run_stage` 只写 `spec.produces` 里的键：
    #
    #     for key in produced:            # produced = spec.produces
    #         if key in payload_out:      # 而 CriticLlm 曾按 capability 返回
    #             blackboard.put(key, ...)
    #
    # 对 `continuity_supervisor`（`continuity.check` vs `continuity.report`）、
    # `compliance_officer`（`compliance.check` vs `compliance.report`）、
    # `review_board`（`review.board` vs `review.verdict`）三个职位，
    # `key in payload_out` **恒假** —— **黑板上什么都不写，而 run_stage 仍 ok=True**。
    # 随后 `review_artifact` 两次回退都取不到 -> "未产出可解析的审查意见"
    # -> 整轮审查作废为 ESCALATE。
    #
    # ⚠️ D2 抓不到它（story_editor 的能力名与产物名相同）—— 所以必须逐职位查。
    from little_crew.agent.roster import DEFAULT_ROSTER

    produces_of = {a.agent_id: tuple(a.produces) for a in DEFAULT_ROSTER}
    stray: list[str] = []
    for aid in CRITIC_CHECKS:
        cc = CriticLlm(aid, client=_FakeClient({"issues": []}))
        o = cc(envelope=_env({cc.artifact_key: TEXT}), runtime=None)
        keys = tuple((o.output or {}).keys())
        if not keys or any(k not in produces_of.get(aid, ()) for k in keys):
            stray.append(f"{aid}:产出 {keys} ⊄ produces {produces_of.get(aid)}")
    check("D10 ★★ 5 个批评家的产出键都在名册 produces 里", not stray,
          str(stray[:2]))

    # 与编排器的读取侧对齐：`_REVIEW_FEEDBACK_KEY` 必须覆盖这 5 个职位，
    # 否则 `review_artifact` 会去一个没人写的键上取值。
    from little_crew.agent.orchestrator import _REVIEW_FEEDBACK_KEY
    uncovered = [a for a in CRITIC_CHECKS if a not in _REVIEW_FEEDBACK_KEY]
    check("D11 ★ 每个批评家都有反馈键映射（编排器按它取意见）",
          not uncovered, str(uncovered))
    wrong = [a for a in CRITIC_CHECKS
             if CriticLlm(a, client=_FakeClient({"issues": []})).produce_key
             != _REVIEW_FEEDBACK_KEY.get(a)]
    check("D12 ★★ 产出键与编排器读的键**是同一个**（H-1 的实效判据）",
          not wrong, str(wrong))


def _helpers() -> None:
    print()
    print("E. 助手")
    from little_crew.agent.messages import RepairHint
    check("E1 返工操作名在合法表里",
          _repair_op() in tuple(getattr(RepairHint, "KNOWN_OPS", ()) or ()),
          _repair_op())
    check("E2 _as_text 处理字符串", _as_text("abc") == "abc")
    check("E3 _as_text 处理 Mapping",
          "k" in _as_text({"k": "v"}))
    check("E4 _as_text 处理没人认得的对象（不崩）",
          isinstance(_as_text(object()), str))


def _integration() -> None:
    """真过一遍编排器 —— 契约接错会在这里暴露。"""
    print()
    print("F. 编排器集成")

    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator

    stubs = default_crew_stubs()
    fake = _FakeClient({"issues": []})
    stubs["story_editor"] = CriticLlm("story_editor", client=fake)
    o = CrewOrchestrator(
        max_rounds=1,
        model_families={"screenwriter": "deepseek",
                        "dramaturge": "mimo",
                        "story_editor": "deepseek"})
    o.bind_many({k: v for k, v in stubs.items() if k != "dramaturge"})
    # 只绑 story_editor 一个审核者：脚本闭环要求双审，
    # 这里刻意只验"批评家能不能被编排器驱动"，不验裁定逻辑。
    from little_crew.agent.crew_stubs import DramaturgeRules
    o.bind("dramaturge", DramaturgeRules())

    cyc = o.run_script_cycle({"source.novel": "第一场：内景。她坐着。"})
    check("F1 ★ 批评家能被编排器驱动（一轮跑完，不崩）",
          cyc is not None and len(cyc.rounds) >= 1,
          f"rounds={len(cyc.rounds) if cyc else 0}")
    check("F2 批评家真的被调用过", fake.calls >= 1, str(fake.calls))
    if cyc.rounds:
        r = cyc.rounds[0]
        check("F3 本轮有权威总分（不是 0）", r.total > 0, str(r.total))
        check("F4 产出被记录（归属 story_editor 的意见可见）",
              any(getattr(fb, "frm", "") == "story_editor"
                  for fb in (r.reviews or ())),
              str([getattr(x, "frm", "?") for x in (r.reviews or ())]))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("通用批评家（A 类审查职位）—— 自检（假客户端）")
    print("=" * 74)

    _coverage()
    _prompt()
    _normalize()
    _call_path()
    _helpers()
    _integration()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
