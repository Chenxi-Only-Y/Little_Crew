"""B 类最后两个职位（美术指导 / 制片主任）—— 假客户端自检。

## 守什么

| 段 | 守什么 |
|---|---|
| A 契约 | 产物键是名册声明的 `produces`，不是 capability 名 |
| B 提示词 | 越界禁令（不打光/不写分镜/不改剧情）；**基准数字进提示词** |
| C 规整 | 美术：`era` 封闭集、无 sceneId 丢弃；制片：**总量由代码算**、编造的 shotId 丢弃 |
| D 调用路径 | 缺输入 / 空正文 / 可重试 / 不可重试 |
| E 编排器 | 两个职位都能被真驱动；产物落对键；归属正确 |

## 最要紧的两条

1. **`era` 是封闭集**（同 `gaffer` 的 `contrast`）—— 自由文本下游无法对齐
2. **`totalSeconds` 由代码算，不取模型自报** ——
   有实测单价却让模型估总量，等于把经验值换成猜测（与 `§3.4.3` 同一原则）
"""
from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.line_producer_llm import (
    DEFAULT_SECONDS_PER_SHOT,
    DEFAULT_SECONDS_PER_STILL,
    LineProducerLlm,
    normalize_schedule,
)
from little_crew.agent.production_designer_llm import (
    ERA_LEVELS,
    ProductionDesignerLlm,
    normalize_art,
)
from little_crew.agent.protocol import Budget, CallEnvelope

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _FakeClient:
    def __init__(self, reply: Any, model_id: str = "fake-b") -> None:
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


def _env(cap: str, payload: dict[str, Any]) -> CallEnvelope:
    return CallEnvelope.root("x", cap, payload, Budget.unlimited(),
                             callee_kind="agent")


TEXT = "第一场：内景。木屋，窗很小。\n第二场：外景。田埂，远处有山。"


def _contract() -> None:
    print()
    print("A. 契约")
    d = ProductionDesignerLlm(client=_FakeClient({}))
    check("A1 美术指导 agent_id 与名册一致",
          d.agent_id == "production_designer", d.agent_id)
    check("A2 ★ 产物键是 art.bible（不是 capability 名 scene.design）",
          d.produces_key == "art.bible", d.produces_key)
    check("A3 消费剧本", d.consumes_key == "script.draft")
    lp = LineProducerLlm(client=_FakeClient({}))
    check("A4 制片主任产物键是 production.schedule",
          lp.produces_key == "production.schedule", lp.produces_key)
    check("A5 ★ 制片主任消费**镜头表**（不是剧本）",
          lp.consumes_key == "shot.list", lp.consumes_key)
    check("A6 era 封闭集六档", len(ERA_LEVELS) == 6, str(ERA_LEVELS))


def _prompt() -> None:
    print()
    print("B. 提示词")
    d = ProductionDesignerLlm(client=_FakeClient({}))
    p, s1 = d._render({"script.draft": TEXT})
    check("B1 剧本进了提示词", "木屋" in p)
    check("B2 ★ 明确禁止越界（不打光/不写分镜/不改剧情）",
          "不要打光" in p and "不要写分镜" in p and "不要改剧情" in p)
    check("B3 要求 space 写结构与尺度", "结构与尺度" in p)
    check("B4 era 六个值列在提示词里",
          all(e in p for e in ERA_LEVELS))

    lp = LineProducerLlm(client=_FakeClient({}), seconds_per_shot=100.0)
    rows = [{"shotId": "S1", "sceneId": "01", "shotSize": "full",
             "durationSec": 6.0}]
    p2, s2 = lp._render(rows)
    check("B5 ★★ 实测基准进了提示词（不让模型自己估速度）",
          "100.0" in p2, "没找到 100.0")
    check("B6 明确禁止模型另估总量", "不要另估" in p2 or "必须用上面给的基准" in p2)
    check("B7 要求 advice 可执行", "能执行" in p2)


def _normalize() -> None:
    print()
    print("C. 规整")

    art = normalize_art({"scenes": [
        {"sceneId": "S01", "space": "木结构老屋，一间", "era": "近代",
         "materials": ["粗木", "泥"], "props": ["信"], "palette": ["赭石"]},
        {"sceneId": "", "space": "无场次号"},          # 丢
        {"sceneId": "S02", "era": "说不清"},           # era 兜底
    ], "throughLine": "木与雨"})
    check("C1 正常项保留", art["scenes"][0]["space"].startswith("木结构"))
    check("C2 ★ 无 sceneId 的丢掉", [s["sceneId"] for s in art["scenes"]] == ["S01", "S02"],
          str([s["sceneId"] for s in art["scenes"]]))
    check("C3 ★★ era 自由文本归到封闭集（说不清 -> 架空）",
          art["scenes"][1]["era"] in ERA_LEVELS, art["scenes"][1]["era"])
    check("C4 throughLine 保留", art["throughLine"] == "木与雨")

    shots = [{"shotId": f"SH{i}", "sceneId": "01", "shotSize": "full",
              "durationSec": 6.0} for i in range(1, 5)]
    sch = normalize_schedule(
        {"totalSeconds": 999, "riskShots": [{"shotId": "SH1", "why": "人物多"},
                                            {"shotId": "编的", "why": "x"}],
         "advice": ["连着跑"], "note": "ok"},
        shots=shots, seconds_per_shot=100.0, seconds_per_still=10.0)
    check("C5 ★★ totalSeconds 由代码算（4 镜 × 110 = 440），不取模型自报的 999",
          sch["totalSeconds"] == 440.0, str(sch["totalSeconds"]))
    check("C6 模型自报的总量**留档**（便于日后核对偏差）",
          sch["modelClaimedSeconds"] == 999, str(sch["modelClaimedSeconds"]))
    check("C7 ★ 编造的 shotId 被丢掉（只留镜头表里真有的）",
          [r["shotId"] for r in sch["riskShots"]] == ["SH1"],
          str([r["shotId"] for r in sch["riskShots"]]))
    check("C8 totalHours 算出来", sch["totalHours"] == 0.12, str(sch["totalHours"]))
    check("C9 空镜头表 -> 总量 0，不崩",
          normalize_schedule({}, shots=[])["totalSeconds"] == 0.0)


def _call_path() -> None:
    print()
    print("D. 调用路径")
    d = ProductionDesignerLlm(client=_FakeClient(
        {"scenes": [{"sceneId": "S01", "era": "近代"}], "throughLine": ""}))
    o = d(envelope=_env("scene.design", {"script.draft": TEXT}), runtime=None)
    check("D1 美术指导 __call__ 成功", o.ok, str(o.error_message)[:90])
    check("D2 ★ 产物键 = art.bible", "art.bible" in (o.output or {}),
          str(list(o.output or {})))

    lp = LineProducerLlm(client=_FakeClient({"advice": []}))
    o2 = lp(envelope=_env("production.schedule",
                          {"shot.list": [{"shot_id": "S1", "scene_id": "01",
                                          "shot_size": "full",
                                          "duration_sec": 6.0}]}), runtime=None)
    check("D3 制片主任 __call__ 成功（吃 as_dict 形状）", o2.ok,
          str(o2.error_message)[:90])
    check("D4 ★ 产物键 = production.schedule",
          "production.schedule" in (o2.output or {}), str(list(o2.output or {})))

    check("D5 美术指导缺剧本 -> 失败",
          not ProductionDesignerLlm(client=_FakeClient({}))
          (envelope=_env("scene.design", {}), runtime=None).ok)
    check("D6 ★ 美术指导空正文 -> 失败（不产空白术）",
          not ProductionDesignerLlm(client=_FakeClient({}))
          (envelope=_env("scene.design", {"script.draft": "  "}),
           runtime=None).ok)
    check("D7 ★ 制片主任空镜头表 -> 失败（不排空期）",
          not LineProducerLlm(client=_FakeClient({}))
          (envelope=_env("production.schedule", {"shot.list": []}),
           runtime=None).ok)


def _integration() -> None:
    print()
    print("E. 编排器")
    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator

    o = CrewOrchestrator(model_families={})
    o.bind_many(default_crew_stubs())
    o.bind("production_designer", ProductionDesignerLlm(
        client=_FakeClient({"scenes": [{"sceneId": "S01", "era": "近代"}]})))
    out = o.run_stage("production_designer", "scene.design",
                      {"script.draft": TEXT})
    check("E1 ★ 美术指导能被编排器驱动", out.ok, str(getattr(out, "error", ""))[:100])
    check("E2 产物落 art.bible", o.blackboard.value("art.bible") is not None)

    o2 = CrewOrchestrator(model_families={})
    o2.bind_many(default_crew_stubs())
    o2.bind("line_producer", LineProducerLlm(client=_FakeClient({"advice": []})))
    out2 = o2.run_stage("line_producer", "production.schedule",
                        {"shot.list": [{"shot_id": "S1", "scene_id": "01",
                                        "shot_size": "full",
                                        "duration_sec": 6.0}]})
    check("E3 ★ 制片主任能被编排器驱动", out2.ok, str(getattr(out2, "error", ""))[:100])
    check("E4 产物落 production.schedule",
          o2.blackboard.value("production.schedule") is not None)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("B 类最后两个职位（美术指导 / 制片主任）—— 自检（假客户端）")
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
