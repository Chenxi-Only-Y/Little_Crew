"""`agent/gaffer_llm.py` 自检 —— 假客户端，不吃 API。

## 守什么

| 断言 | 守什么 |
|---|---|
| A | 契约与名册一致；**产物键是 `lighting.plan` 不是 capability 名** |
| B | 提示词要求**可执行的光**（位置+性质），禁止形容词与越界 |
| C | 规整：`contrast` **封闭集**、无 `sceneId` 丢掉、猜方向的兜底 |
| D | 调用路径：缺输入 / 空正文 / 可重试 / 不可重试 |
| E | 真经编排器跑一轮；产物落 `lighting.plan`、归属 `gaffer` |

## 为什么 `contrast` 必须是封闭集

它是**下游要用来写提示词的词**。若允许"较强"/"很硬"这类自由文本，
分镜师写出来的画面描述就会不一致 —— 而**不一致不会报错**，
只会让"同一场的光"在不同镜头里长得不一样。

这与 `LEGAL_VOCAB`（景别/运镜的封闭集）是同一个道理。
"""
from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.gaffer_llm import (
    CONTRAST_LEVELS,
    GafferLlm,
    normalize_lighting,
)
from little_crew.agent.protocol import Budget, CallEnvelope

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _FakeClient:
    def __init__(self, reply: Any, model_id: str = "fake-gaffer") -> None:
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
                              "usage": {"prompt_tokens": 1,
                                        "completion_tokens": 1}})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _env(payload: dict[str, Any]) -> CallEnvelope:
    return CallEnvelope.root("gaffer", "lighting.plan", payload,
                             Budget.unlimited(), callee_kind="agent")


TEXT = "第一场：内景。少女坐着。\n第二场：外景。天亮了。"


def _contract() -> None:
    print()
    print("A. 契约")
    g = GafferLlm(client=_FakeClient({}))
    check("A1 agent_id 与名册一致", g.agent_id == "gaffer", g.agent_id)
    check("A2 capability 是 lighting.plan",
          g.capability == "lighting.plan", g.capability)
    check("A3 ★ 产物键是 lighting.plan（不是 capability 名）",
          g.produces_key == "lighting.plan", g.produces_key)
    check("A4 消费的是剧本", g.consumes_key == "script.draft", g.consumes_key)
    check("A5 影调封闭集是三档", CONTRAST_LEVELS == ("高", "中", "低"),
          str(CONTRAST_LEVELS))


def _prompt() -> None:
    print()
    print("B. 提示词")
    g = GafferLlm(client=_FakeClient({}),
                  plan={"tone": "潮湿而克制", "motifs": ["雨", "门"]})
    p, system = g._render({"script.draft": TEXT})
    check("B1 剧本进了提示词", "少女坐着" in p)
    check("B2 导演基调被参考（有 plan 时）", "潮湿而克制" in p)
    check("B3 视觉动机被参考", "雨" in p and "门" in p)
    check("B4 ★ 要求 `keyLight` 写位置与性质",
          "位置与性质" in p or "窗在左侧" in p)
    check("B5 ★ 明确禁止形容词式空话",
          "营造氛围" in p or "不要写" in p)
    check("B6 明确禁止越界（不写分镜/景别/剧情）",
          "不要写分镜" in p and "不要改剧情" in p)
    check("B7 system 点明职责边界", "不写" in system, system[:50])


def _normalize() -> None:
    print()
    print("C. 规整")

    ok = normalize_lighting({"scenes": [
        {"sceneId": "S01", "timeOfDay": "夜", "keyLight": "窗在左侧，硬光斜切",
         "colorTemp": "冷白", "contrast": "高", "note": "只照亮手里的信"}],
        "palette": ["靛蓝", "灰白"]})
    s = ok["scenes"][0]
    check("C1 正常项全部保留",
          s["timeOfDay"] == "夜" and s["contrast"] == "高"
          and "窗在左侧" in s["keyLight"] and "信" in s["note"],
          str(s))
    check("C2 色板保留", ok["palette"] == ["靛蓝", "灰白"], str(ok["palette"]))

    # ---- 封闭集 ----
    bad = normalize_lighting({"scenes": [
        {"sceneId": "S01", "contrast": "较强"},
        {"sceneId": "S02", "contrast": "很柔和"},
        {"sceneId": "S03", "contrast": "中"}]})
    got = [x["contrast"] for x in bad["scenes"]]
    check("C3 ★ 自由文本影调被归到封闭集（较强->高 / 很柔和->低 / 中->中）",
          got == ["高", "低", "中"], str(got))

    # 猜不到方向 -> 默认"中"
    d = normalize_lighting({"scenes": [{"sceneId": "S01", "contrast": "???"}]})
    check("C4 猜不到方向 -> 默认「中」（不编造极端）",
          d["scenes"][0]["contrast"] == "中", d["scenes"][0]["contrast"])

    # ---- 无 sceneId 丢掉 ----
    e = normalize_lighting({"scenes": [
        {"timeOfDay": "夜"},
        {"sceneId": "S02", "timeOfDay": "晨"}]})
    check("C5 ★ 没有 sceneId 的场次被丢掉（无场次号无法对账）",
          [x["sceneId"] for x in e["scenes"]] == ["S02"],
          str([x["sceneId"] for x in e["scenes"]]))

    # ---- 脏输入 ----
    check("C6 空输入 -> 空结构",
          normalize_lighting({})["scenes"] == [])
    check("C7 scenes 不是列表 -> 空列表，不崩",
          normalize_lighting({"scenes": "x"})["scenes"] == [])
    check("C8 色板超过 4 个被截断",
          len(normalize_lighting({"palette": list("abcdefg")})["palette"]) == 4)


def _call_path() -> None:
    print()
    print("D. 调用路径")
    reply = {"scenes": [{"sceneId": "S01", "timeOfDay": "夜",
                         "keyLight": "窗左硬光", "colorTemp": "冷白",
                         "contrast": "高", "note": "压住犹豫"}],
             "palette": ["靛蓝"]}
    fake = _FakeClient(reply)
    g = GafferLlm(client=fake)
    out = g(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D1 __call__ 成功", out.ok, str(getattr(out, "error_message", ""))[:90])
    if out.ok:
        check("D2 ★ 产物键 = lighting.plan（不是 capability 名）",
              "lighting.plan" in out.output, str(list(out.output)))
        check("D3 有 scenes", len(out.output["lighting.plan"]["scenes"]) == 1)

    g2 = GafferLlm(client=_FakeClient({}))
    o2 = g2(envelope=_env({}), runtime=None)
    check("D4 缺剧本 -> 失败（不崩）",
          not o2.ok and o2.error_code == "F-INPUT", str(o2.error_code))
    o3 = g2(envelope=_env({"script.draft": "  "}), runtime=None)
    check("D5 ★ 空正文 -> 失败（不是产一份空方案）",
          not o3.ok and o3.error_code == "F-INPUT", str(o3.error_code))

    from little_crew.agent.llm import LLMError
    g4 = GafferLlm(client=_FakeClient(LLMError("timeout", "超时",
                                               retryable=True)))
    o4 = g4(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D6 可重试错误 -> 失败但不升级",
          not o4.ok and not o4.escalate, str(o4.error_code))
    g5 = GafferLlm(client=_FakeClient(LLMError("no-api-key", "没 key",
                                               retryable=False)))
    o5 = g5(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D7 不可重试错误 -> 失败且升级",
          not o5.ok and o5.escalate, str(o5.error_code))


def _integration() -> None:
    print()
    print("E. 编排器集成")
    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator

    o = CrewOrchestrator(model_families={})
    o.bind_many(default_crew_stubs())
    o.bind("gaffer", GafferLlm(client=_FakeClient({
        "scenes": [{"sceneId": "S01", "contrast": "高"}], "palette": []})))
    out = o.run_stage("gaffer", "lighting.plan", {"script.draft": TEXT})
    check("E1 ★ 灯光师能被编排器驱动（契约接错会在这里暴露）",
          out.ok, str(getattr(out, "error", ""))[:100])
    check("E2 产物落到 lighting.plan 键",
          o.blackboard.value("lighting.plan") is not None)
    check("E3 归属记在 gaffer 名下",
          o.owner_of("lighting.plan") == "gaffer",
          str(o.owner_of("lighting.plan")))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("灯光师（lighting.plan）—— 自检（假客户端）")
    print("=" * 74)

    _contract()
    _prompt()
    _normalize()
    _call_path()
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
