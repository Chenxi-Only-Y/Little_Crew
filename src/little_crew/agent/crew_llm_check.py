"""`crew_llm` 的自检 —— 用**假客户端**，不依赖 API。

## 为什么必须有假客户端

真调 LLM 的测试有两个致命问题：**要钱**、**不确定**。
而本模块最要紧的恰恰是几条**机制性**保证（返工意见真的传到了吗？
模型空转会不会被当成成功？），它们必须能被**确定性地**断言。

所以这里注入一个 `_FakeClient`：它按脚本返回预设 JSON，
并**记录收到的提示词** —— 于是"意见传到了吗"变成可断言的事实，
而不是"我看模型像是改了"。

⚠️ 命名以 ``_check`` 结尾（与 `roster_check` / `embed_check` 同例），
不会被 pytest 收集；入口是本文件的 `main()`。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from little_crew.agent.crew_llm import CREW_PROMPTS, ScreenwriterLlm
from little_crew.agent.llm import LLMError
from little_crew.agent.protocol import Blackboard, Budget, CallEnvelope, LayerRuntime
from little_crew.agent.roster import DEFAULT_ROSTER, build_registry

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILED.append(f"{name} —— {detail}")
    print(f"  {'OK ' if ok else 'X  '} {name}" + (f"   {detail}" if detail and not ok else ""))


class _FakeClient:
    """假 LLM 客户端：按队列返回预设内容，并记录收到的提示词。"""

    def __init__(self, replies: list[Any]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.input_tokens = 0
        self.call_log: list[dict[str, Any]] = []
        self.calls = 0

    def chat_json(self, prompt: str, *, system: str = "",
                  temperature: float | None = None,
                  what: str = "输出") -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        self.systems.append(system)
        self.input_tokens += len(prompt)
        self.call_log.append({"servedModel": "fake", "elapsedS": 0.01,
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        if not self.replies:
            raise LLMError("empty-output", "假客户端没有更多回复了")
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _runtime() -> LayerRuntime:
    return LayerRuntime(build_registry(DEFAULT_ROSTER), Blackboard())


def _env(payload: Mapping[str, Any]) -> CallEnvelope:
    return CallEnvelope.root("screenwriter", "script.draft", dict(payload),
                             Budget.unlimited(), callee_kind="agent")


def _draft(*, n: int = 1, body: str = "正文内容", title: str = "标题",
           **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "title": title,
        "scenes": [{"scene_id": f"S{i:02d}", "heading": "内景·室内·日",
                    "characters": ["甲"], "text": f"{body}{i}",
                    "summary": "摘要"} for i in range(1, n + 1)],
    }
    d.update(extra)
    return d


# ==========================================================================
def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # type: ignore[attr-defined]
    except Exception:                                # noqa: BLE001
        pass
    print("=" * 74)
    print("crew_llm 自检（假客户端，不消耗 API）")
    print("=" * 74)

    rt = _runtime()

    # ---- A 首稿基本路径 ----
    print()
    print("A. 首稿")
    c = _FakeClient([_draft(n=3)])
    w = ScreenwriterLlm(client=c)                    # type: ignore[arg-type]
    r = w(envelope=_env({"source.novel": "小说素材", "episodeId": "E009",
                         "title": "雨夜信"}), runtime=rt)
    check("A1 首稿成功", r.ok, str(r.error_code))
    d = (r.output or {}).get("script.draft")
    check("A2 产出 script.draft 且场数正确",
          d is not None and len(d.scenes) == 3, str(getattr(d, "scenes", None)))
    check("A3 revision = 1", d is not None and d.revision == 1,
          str(getattr(d, "revision", None)))
    check("A4 episodeId 透传", d is not None and d.episode_id == "E009",
          str(getattr(d, "episode_id", None)))
    check("A5 素材进了提示词", "小说素材" in c.prompts[0], c.prompts[0][:60])
    check("A6 用了编剧的 system 提示",
          c.systems[0] == CREW_PROMPTS["screenwriter"]["system"], "")
    check("A7 证据链带 llm-call",
          bool(r.evidence) and r.evidence[0].get("kind") == "llm-call",
          str(r.evidence)[:80])

    # ---- B 空素材必须失败（不能产出空剧本当成功）----
    print()
    print("B. 空素材")
    c_b = _FakeClient([_draft()])
    r_b = ScreenwriterLlm(client=c_b)(                    # type: ignore[arg-type]
        envelope=_env({"source.novel": "   "}), runtime=rt)
    check("B1 空素材且无意见 -> 失败", not r_b.ok, str(r_b.error_code))
    check("B2 失败码是 F-INPUT-EMPTY",
          r_b.error_code == "F-INPUT-EMPTY", str(r_b.error_code))
    check("B3 空素材不该真的调用模型", c_b.calls == 0, f"calls={c_b.calls}")

    # ---- C ★ 返工意见必须真的传到模型 ----
    #
    # 这一条是整套协作机制的地基：如果意见没传过去，
    # 模型"改了"也只是随机重采样，而返工闭环就成了摆设。
    print()
    print("C. ★ 返工意见的传递")
    crit = {"issues": [{"sceneId": "S01", "dimension": "cost",
                        "problem": "独特的问题描述XYZ",
                        "suggestion": "独特建议ABC"}]}
    c_c = _FakeClient([_draft(body="首稿正文"),
                       _draft(body="修订正文", changes=["补了代价"])])
    w_c = ScreenwriterLlm(client=c_c)                     # type: ignore[arg-type]
    w_c(envelope=_env({"source.novel": "素材"}), runtime=rt)
    w_c(envelope=_env({"source.novel": "素材", "script.critique": crit}),
        runtime=rt)
    check("C1 第二次调用收到了意见原文",
          "独特的问题描述XYZ" in c_c.prompts[1] and "独特建议ABC" in c_c.prompts[1],
          c_c.prompts[1][-220:])
    check("C2 意见**原样**给出，未被总结",
          json.dumps(crit, ensure_ascii=False)[:20] in c_c.prompts[1]
          or "cost" in c_c.prompts[1], "")
    check("C3 提示词要求模型说明 changes",
          "changes" in c_c.prompts[1], "")
    check("C4 实现记录了收到的意见",
          len(w_c.received_critiques) == 1, str(w_c.received_critiques))

    # ---- D ★ 空转防护 ----
    print()
    print("D. ★ 空转防护（本模块存在的理由）")

    # D1 有意见却没声明改动 -> 失败
    c_d1 = _FakeClient([_draft(body="A"), _draft(body="B")])
    w_d1 = ScreenwriterLlm(client=c_d1)                   # type: ignore[arg-type]
    w_d1(envelope=_env({"source.novel": "素材"}), runtime=rt)
    r_d1 = w_d1(envelope=_env({"source.novel": "素材",
                               "script.critique": crit}), runtime=rt)
    check("D1 有意见但未声明 changes -> 失败", not r_d1.ok, str(r_d1.error_code))
    check("D2 失败码是 F-REVISION-NOT-DECLARED",
          r_d1.error_code == "F-REVISION-NOT-DECLARED", str(r_d1.error_code))
    check("D3 该失败要求升级人工", r_d1.escalate, str(r_d1.escalate))

    # D4 声明了改动但正文一字未变 -> 失败（真正的空转）
    same = _draft(body="完全不变的正文")
    c_d4 = _FakeClient([same, dict(same, changes=["声称改了"])])
    w_d4 = ScreenwriterLlm(client=c_d4)                   # type: ignore[arg-type]
    w_d4(envelope=_env({"source.novel": "素材"}), runtime=rt)
    r_d4 = w_d4(envelope=_env({"source.novel": "素材",
                               "script.critique": crit}), runtime=rt)
    check("D4 声明改了但正文没变 -> 失败", not r_d4.ok, str(r_d4.error_code))
    check("D5 失败码是 F-REVISION-NO-OP",
          r_d4.error_code == "F-REVISION-NO-OP", str(r_d4.error_code))

    # D6 真的改了 -> 成功，且自述进了证据
    c_d6 = _FakeClient([_draft(body="旧"), _draft(body="新", changes=["改了X"])])
    w_d6 = ScreenwriterLlm(client=c_d6)                   # type: ignore[arg-type]
    w_d6(envelope=_env({"source.novel": "素材"}), runtime=rt)
    r_d6 = w_d6(envelope=_env({"source.novel": "素材",
                               "script.critique": crit}), runtime=rt)
    check("D6 真改了 -> 成功", r_d6.ok, str(r_d6.error_code))
    check("D7 自述改动进入证据链",
          r_d6.ok and r_d6.evidence[0].get("declaredChanges") == ["改了X"],
          str(r_d6.evidence)[:90])

    # ---- E 输出规整的宽容与底线 ----
    print()
    print("E. 输出规整")
    c_e = _FakeClient([{"title": "T", "scenes": [
        {"id": "X1", "location": "外景", "cast": "甲，乙",
         "content": "用别名写的正文"},
        {"scene_id": "X2", "text": "   "},          # 空正文 -> 丢掉
        "不是字典",                                   # 形状不对 -> 丢掉
    ]}])
    r_e = ScreenwriterLlm(client=c_e)(                    # type: ignore[arg-type]
        envelope=_env({"source.novel": "素材"}), runtime=rt)
    check("E1 别名字段被认（id/location/cast/content）", r_e.ok, str(r_e.error_code))
    if r_e.ok:
        sc = r_e.output["script.draft"].scenes
        check("E2 空正文的场次被丢掉（只剩 1 场）", len(sc) == 1, f"{len(sc)} 场")
        check("E3 逗号字符串的 cast 被切成列表",
              list(sc[0].get("characters", [])) == ["甲", "乙"],
              str(sc[0].get("characters")))
    c_e2 = _FakeClient([{"title": "T", "scenes": [{"text": "  "}]}])
    r_e2 = ScreenwriterLlm(client=c_e2)(                  # type: ignore[arg-type]
        envelope=_env({"source.novel": "素材"}), runtime=rt)
    check("E4 全是空场次 -> 失败（不产出空剧本）", not r_e2.ok,
          str(r_e2.error_code))

    # ---- F LLM 失败的上报 ----
    print()
    print("F. 失败上报")
    c_f1 = _FakeClient([LLMError("timeout", "超时", retryable=True)])
    r_f1 = ScreenwriterLlm(client=c_f1)(                  # type: ignore[arg-type]
        envelope=_env({"source.novel": "素材"}), runtime=rt)
    check("F1 可重试错误 -> 失败但不升级",
          not r_f1.ok and not r_f1.escalate, f"{r_f1.error_code} esc={r_f1.escalate}")
    c_f2 = _FakeClient([LLMError("no-api-key", "没配密钥", retryable=False)])
    r_f2 = ScreenwriterLlm(client=c_f2)(                  # type: ignore[arg-type]
        envelope=_env({"source.novel": "素材"}), runtime=rt)
    check("F2 不可重试错误 -> 失败且升级人工",
          not r_f2.ok and r_f2.escalate, f"{r_f2.error_code} esc={r_f2.escalate}")
    check("F3 错误码带原始 kind",
          (r_f1.error_code or "").endswith("TIMEOUT"), str(r_f1.error_code))

    # ---- G 组装 ----
    print()
    print("G. 组装")
    from little_crew.agent.crew_llm import build_llm_crew

    crew = build_llm_crew(client=_FakeClient([_draft()]))  # type: ignore[arg-type]
    check("G1 默认只做编剧（先跑通一个再铺开）",
          list(crew) == ["screenwriter"], str(list(crew)))
    check("G2 有 screenwriter 的提示词",
          "screenwriter" in CREW_PROMPTS, str(list(CREW_PROMPTS)))

    # ---- 汇总 ----
    print()
    print("=" * 74)
    total = PASSED + len(FAILED)
    if FAILED:
        print(f"结果：FAIL（{PASSED}/{total}）")
        for f in FAILED:
            print(f"  X {f}")
        print("=" * 74)
        return 1
    print(f"结果：{PASSED}/{total} 通过")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
