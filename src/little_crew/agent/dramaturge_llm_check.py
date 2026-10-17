"""给 `DramaturgeLlm.__call__` 补单测 —— 用假客户端，不吃 API。

## 为什么必须有这一组

2026-10-17 踩了一个**测试盲区**：给剧评人换了完整性公式，
但忘了在 `crew_llm.py` 里导入 `COMPLETENESS_PENALTY_PER_SCENE`。

**当时的 1205 项测试全过** —— 因为：

| 测试 | 覆盖 |
|---|---|
| `crew_stubs_check` I 段（长度无关性） | **规则桩**的剧评人 |
| `crew_llm_check` G 段 | 只碰 `DramaturgeLlm().client`，**从不调用 `__call__`** |

于是 **LLM 剧评人的执行路径一次都没被跑过**，`NameError` 一直藏着 ——
直到跑真实一集时才暴露（3 次全部 ESCALATE）。

**教训：一个类有单测，不等于它的 `__call__` 有单测。**
"能构造"与"能工作"是两件事。
"""
from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.crew_llm import CREW_PROMPTS, DramaturgeLlm
from little_crew.agent.messages import (
    COMPLETENESS_PENALTY_PER_SCENE,
    Dimension,
    ScriptDraft,
    Severity,
    Verdict,
)
from little_crew.agent.llm import LLMError
from little_crew.agent.protocol import Budget, CallEnvelope


class _FakeClient:
    """假客户端：返回预设 JSON，记录收到的提示词。"""

    def __init__(self, reply: Any, model_id: str = "fake-reviewer") -> None:
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


RICH = "她拒绝了，但那要付出代价。忽然她转身离开。"
FLAT = "屋里很安静，光线柔和，她坐着。"


def _draft(n: int, bad: int) -> ScriptDraft:
    scenes = [{"scene_id": f"S{i+1:02d}", "heading": "内景",
               "characters": ["甲"],
               "text": (FLAT if i < bad else RICH)} for i in range(n)]
    return ScriptDraft(frm="screenwriter", to="dramaturge", episode_id="E900",
                       title="T", scenes=tuple(scenes), revision=1)


def _call(reply: Any, draft: ScriptDraft):
    c = _FakeClient(reply)
    d = DramaturgeLlm(client=c)                       # type: ignore[arg-type]
    env = CallEnvelope.root("dramaturge", "script.critique",
                            {"script.draft": draft}, Budget.unlimited(),
                            callee_kind="agent")
    return d(envelope=env, runtime=None), c, d        # type: ignore[arg-type]


def _issues(n: int, bad: int) -> dict[str, Any]:
    """造一个"前 bad 场有问题"的模型回复。"""
    return {"issues": [
        {"sceneId": f"S{i+1:02d}", "dimension": "conflict",
         "problem": "缺少冲突", "suggestion": "加一个对抗"}
        for i in range(bad)
    ], "overall": "总体还行"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # type: ignore[attr-defined]
    except Exception:                                 # noqa: BLE001
        pass
    print("=" * 74)
    print("DramaturgeLlm.__call__ 自检（假客户端，不消耗 API）")
    print("=" * 74)

    fails: list[str] = []
    n = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal n
        n += 1
        print(f"  {'OK ' if ok else 'X  '} {name}"
              + (f"   {detail}" if detail and not ok else ""))
        if not ok:
            fails.append(name)

    # ---- A 基本路径 ----
    print()
    print("A. 基本路径")
    out, c, d = _call(_issues(2, 1), _draft(2, 1))
    check("A1 __call__ 能跑通（**这一条就是当初缺的**）", out.ok,
          str(out.error_message)[:120])
    if out.ok:
        fb = out.output["script.critique"]
        check("A2 产出 script.critique", fb is not None)
        check("A3 有维度评分", bool(fb.score and fb.score.dimensions))
        check("A4 verdict 为 REVISE（有问题）", fb.verdict is Verdict.REVISE,
              fb.verdict.value)
        check("A5 问题严重度是 MAJOR（不是 BLOCKER —— 由阈值裁决）",
              all(i.severity is Severity.MAJOR for i in fb.issues),
              str([i.severity.value for i in fb.issues]))
        check("A6 用了剧评人的 system 提示",
              c.systems[0] == CREW_PROMPTS["dramaturge"]["system"])
        check("A7 剧本正文进了提示词", RICH[:10] in c.prompts[0])

    # ---- B ★ 完整性：**长度无关**（与规则桩同一口径）----
    print()
    print("B. 完整性公式（长度无关）")
    o2, _, _ = _call(_issues(2, 1), _draft(2, 1))
    o8, _, _ = _call(_issues(8, 1), _draft(8, 1))
    if o2.ok and o8.ok:
        c2 = o2.output["script.critique"].score.dimensions[Dimension.COMPLETENESS]
        c8 = o8.output["script.critique"].score.dimensions[Dimension.COMPLETENESS]
        exp2 = 100.0 - COMPLETENESS_PENALTY_PER_SCENE
        check("B1 ★ 1 个缺陷场次：2 场与 8 场得到相同完整性",
              abs(c2 - c8) < 0.01, f"2 场={c2} 8 场={c8}")
        check("B2 等于 100 - 惩罚", abs(c2 - exp2) < 0.01, f"{c2} vs {exp2}")
    else:
        check("B1 ★ 长度无关性", False, "前置调用失败")

    # 无缺陷 -> 满分
    o0, _, _ = _call(_issues(2, 0), _draft(2, 0))
    if o0.ok:
        c0 = o0.output["script.critique"].score.dimensions[Dimension.COMPLETENESS]
        check("B3 无缺陷 -> 100", abs(c0 - 100.0) < 0.01, str(c0))
        check("B4 无缺陷 -> PASS",
              o0.output["script.critique"].verdict is Verdict.PASS)
        check("B5 无缺陷 -> 无返工指令",
              len(o0.output["script.critique"].repair_hints) == 0)

    # ---- C 输入与失败处理 ----
    print()
    print("C. 输入与失败")
    out_bad, _, _ = _call(_issues(1, 1), _draft(1, 0))  # 占位
    # 未收到 ScriptDraft
    c2c = _FakeClient(_issues(1, 1))
    dd = DramaturgeLlm(client=c2c)                      # type: ignore[arg-type]
    r_none = dd(envelope=CallEnvelope.root(
        "dramaturge", "script.critique", {"script.draft": None},
        Budget.unlimited(), callee_kind="agent"), runtime=None)  # type: ignore[arg-type]
    check("C1 未收到 ScriptDraft -> 失败（不崩）",
          not r_none.ok and r_none.error_code == "F-INPUT",
          str(r_none.error_code))

    out_err, _, _ = _call(LLMError("timeout", "超时", retryable=True),
                          _draft(2, 0))
    check("C2 可重试错误 -> 失败但不升级",
          not out_err.ok and not out_err.escalate,
          f"{out_err.error_code} esc={out_err.escalate}")

    out_err2, _, _ = _call(LLMError("no-api-key", "没 key", retryable=False),
                           _draft(2, 0))
    check("C3 不可重试错误 -> 失败且升级",
          not out_err2.ok and out_err2.escalate,
          f"{out_err2.error_code} esc={out_err2.escalate}")

    # ---- D 输出规整的底线 ----
    print()
    print("D. 输出规整")
    # 模型报了不在剧本里的场次 -> 丢掉（不硬塞假位置）
    out_x, _, _ = _call({"issues": [
        {"sceneId": "S99", "dimension": "conflict", "problem": "问题"}]},
        _draft(2, 0))
    check("D1 场次不在剧本里的意见被丢掉",
          out_x.ok and len(out_x.output["script.critique"].issues) == 0,
          str(len(out_x.output["script.critique"].issues)) if out_x.ok else "fail")

    # 缺 problem -> 丢掉
    out_y, _, _ = _call({"issues": [{"sceneId": "S01", "dimension": "cost"}]},
                        _draft(2, 0))
    check("D2 缺 problem 的意见被丢掉",
          out_y.ok and len(out_y.output["script.critique"].issues) == 0,
          str(len(out_y.output["script.critique"].issues)) if out_y.ok else "fail")

    print()
    print("=" * 74)
    if fails:
        print(f"结果：FAIL（{n - len(fails)}/{n}）")
        for f in fails:
            print(f"  X {f}")
        print("=" * 74)
        return 1
    print(f"结果：{n}/{n} 通过")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
