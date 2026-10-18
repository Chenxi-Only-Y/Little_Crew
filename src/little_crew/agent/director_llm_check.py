"""`agent/director_llm.py` 自检 —— 假客户端，不吃 API。

## 守什么

| 断言 | 守什么 |
|---|---|
| A | 契约（`agent_id` / `capability` / 消费键）与名册一致 |
| B | 提示词点明**越界禁令**（不做场次表、不做分镜）+ 预算数字进提示词 |
| C | 规整三条硬规矩：**单场 ≥2**、总和回收、**保留 sceneId** |
| D | 调用路径：缺输入 / 空正文 / 可重试 / 不可重试 |
| E | 真经编排器跑一轮 |

## 为什么"单场 ≥2"是硬规矩

模型在**场次多、预算少**时会给某些场 `shotBudget: 1`。而一场只给一个镜头，
等于这场没拍 —— 节奏、情绪、空间全都交代不出来。所以规整里提上来。

这条是从"分镜师现在自己定每场几个镜头，于是出现'一场只给一个镜头'"来的。
导演职位的**实际理由**就在这：**先把预算定死，分镜在预算内分配。**
"""
from __future__ import annotations

import sys
from typing import Any

from little_crew.agent.director_llm import (
    DEFAULT_TOTAL_SHOTS,
    DirectorLlm,
    normalize_plan,
)
from little_crew.agent.protocol import Budget, CallEnvelope

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


class _FakeClient:
    def __init__(self, reply: Any, model_id: str = "fake-director") -> None:
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
    return CallEnvelope.root("director", "episode.direct", payload,
                             Budget.unlimited(), callee_kind="agent")


TEXT = "第一场：内景。少女坐着。\n第二场：外景。天亮了。"


def _contract() -> None:
    print()
    print("A. 契约")
    d = DirectorLlm(client=_FakeClient({}))
    check("A1 agent_id 与名册一致", d.agent_id == "director", d.agent_id)
    check("A2 capability 是 episode.direct",
          d.capability == "episode.direct", d.capability)
    check("A3 消费的是剧本（不是自己的产物）",
          d.consumes_key == "script.draft", d.consumes_key)
    check("A4 默认预算与实测镜数一致（20 镜）",
          10 <= DEFAULT_TOTAL_SHOTS <= 30, str(DEFAULT_TOTAL_SHOTS))


def _prompt() -> None:
    print()
    print("B. 提示词")
    d = DirectorLlm(client=_FakeClient({"scenes": []}), total_shots=18)
    p, system = d._render({"script.draft": TEXT})
    check("B1 剧本进了提示词", "少女坐着" in p)
    check("B2 ★ 预算数字进了提示词", "18" in p, "没找到 18")
    check("B3 ★ 明确禁止越界（不做分镜/不改剧情）",
          "不要写分镜" in p or "不要写景别" in p)
    check("B4 system 里点明职责边界",
          "不做" in system and "导演" in system, system[:60])
    check("B5 要求覆盖每一场（不能漏场）", "每一场" in p)
    check("B6 ★ 明确「单场不少于 2」", "不少于 2" in p or "不低于 2" in p)


def _normalize() -> None:
    print()
    print("C. 规整")

    # ---- 单场 <2 被提上来 ----
    plan = normalize_plan({"tone": "克制", "pacing": "前松后紧",
                           "scenes": [{"sceneId": "S01", "shotBudget": 1,
                                       "focus": "犹豫"},
                                      {"sceneId": "S02", "shotBudget": 5,
                                       "focus": "决断"}],
                           "motifs": ["雨", "门"]}, total_shots=20)
    budgets = [s["shotBudget"] for s in plan["scenes"]]
    check("C1 ★★ 单场 shotBudget=1 -> 提到 2（一场一个镜头等于没拍）",
          budgets == [2, 5], str(budgets))
    check("C2 sceneId 保留（下游要对账）",
          [s["sceneId"] for s in plan["scenes"]] == ["S01", "S02"])
    check("C3 focus 保留", plan["scenes"][0]["focus"] == "犹豫")
    check("C4 基调/节奏/动机都保留",
          plan["tone"] == "克制" and plan["pacing"] == "前松后紧"
          and plan["motifs"] == ["雨", "门"])
    check("C5 totalShotBudget 被算出来",
          plan["totalShotBudget"] == sum(budgets), str(plan["totalShotBudget"]))

    # ---- 总和超预算 -> 回收，但不压到 1 ----
    many = {"scenes": [{"sceneId": f"S{i:02d}", "shotBudget": 10}
                       for i in range(1, 11)]}       # 10 场 × 10 = 100
    p2 = normalize_plan(many, total_shots=20)
    bs2 = [s["shotBudget"] for s in p2["scenes"]]
    check("C6 ★ 总和超预算 -> 按比例回收",
          sum(bs2) < 100, f"sum={sum(bs2)}")
    check("C7 ★ 回收后没有任何一场低于 2",
          all(b >= 2 for b in bs2), str(bs2))

    # ---- 缺 sceneId 的预算丢掉（无法对账） ----
    p3 = normalize_plan({"scenes": [{"shotBudget": 5},
                                    {"sceneId": "S02", "shotBudget": 3}]},
                        total_shots=20)
    check("C8 缺 sceneId 的预算被丢掉（无法对账）",
          [s["sceneId"] for s in p3["scenes"]] == ["S02"],
          str([s["sceneId"] for s in p3["scenes"]]))

    # ---- 空/脏输入 ----
    p4 = normalize_plan({}, total_shots=20)
    check("C9 空输入 -> 空结构，不编场次",
          p4["scenes"] == [] and p4["totalShotBudget"] == 0)
    p5 = normalize_plan({"scenes": "不是列表"}, total_shots=20)
    check("C10 scenes 类型不对 -> 空列表，不崩", p5["scenes"] == [])
    p6 = normalize_plan({"scenes": [{"sceneId": "S01",
                                     "shotBudget": "很多"}]}, total_shots=20)
    check("C11 shotBudget 非数字 -> 取 2（不低于下限）",
          p6["scenes"][0]["shotBudget"] == 2,
          str(p6["scenes"][0]["shotBudget"]))


def _call_path() -> None:
    print()
    print("D. 调用路径")
    reply = {"tone": "温暖", "pacing": "匀速",
             "scenes": [{"sceneId": "S01", "shotBudget": 3,
                         "focus": "等候"}], "motifs": ["薰衣草"]}
    fake = _FakeClient(reply)
    d = DirectorLlm(client=fake, total_shots=20)
    out = d(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D1 __call__ 成功", out.ok, str(getattr(out, "error", ""))[:90])
    if out.ok:
        plan = out.output["episode.plan"]
        check("D2 ★ 产出键 = 名册声明的 episode.plan（不是 capability 名）",
          plan is not None, str(list(out.output)))
        check("D3 plan 里有 scenes", len(plan["scenes"]) == 1)
        check("D4 记了调用（1 次）", fake.calls == 1, str(fake.calls))

    # 缺剧本
    d2 = DirectorLlm(client=_FakeClient({}))
    out2 = d2(envelope=_env({}), runtime=None)
    check("D5 缺 script.draft -> 失败（不崩）",
          not out2.ok and out2.error_code == "F-INPUT", str(out2.error_code))

    # 空正文
    out3 = d2(envelope=_env({"script.draft": "   "}), runtime=None)
    check("D6 ★ 空正文 -> 失败（不是产一份空方案）",
          not out3.ok and out3.error_code == "F-INPUT", str(out3.error_code))

    # 错误分类
    from little_crew.agent.llm import LLMError
    d4 = DirectorLlm(client=_FakeClient(LLMError("timeout", "超时",
                                                 retryable=True)))
    o4 = d4(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D7 可重试错误 -> 失败但不升级",
          not o4.ok and not o4.escalate, str(o4.error_code))
    d5 = DirectorLlm(client=_FakeClient(LLMError("no-api-key", "没 key",
                                                 retryable=False)))
    o5 = d5(envelope=_env({"script.draft": TEXT}), runtime=None)
    check("D8 不可重试错误 -> 失败且升级",
          not o5.ok and o5.escalate, str(o5.error_code))


def _integration() -> None:
    print()
    print("E. 编排器集成")
    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator

    stubs = default_crew_stubs()
    fake = _FakeClient({"tone": "克制", "pacing": "匀速",
                        "scenes": [{"sceneId": "S01", "shotBudget": 3,
                                    "focus": "犹豫"}], "motifs": []})
    o = CrewOrchestrator(model_families={})
    o.bind_many(stubs)
    o.bind("director", DirectorLlm(client=fake))
    out = o.run_stage("director", "episode.direct",
                      {"script.draft": TEXT})
    check("E1 ★ 导演能被编排器驱动（契约接错会在这里暴露）",
          out.ok, str(getattr(out, "error", ""))[:100])
    plan = o.blackboard.value("episode.plan")
    check("E2 产物落到 episode.plan 键", plan is not None,
          str(type(plan).__name__))
    check("E3 归属记在 director 名下", o.owner_of("episode.plan") == "director",
          str(o.owner_of("episode.plan")))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("导演职位（episode.plan）—— 自检（假客户端）")
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
