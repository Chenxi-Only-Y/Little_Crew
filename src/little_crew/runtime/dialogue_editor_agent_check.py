"""对白编辑职位自检（确定性，不调模型）。"""
from __future__ import annotations

import sys

from little_crew.agent.dialogue_editor_agent import DialogueEditorAgent
from little_crew.agent.protocol import Budget, CallEnvelope
from little_crew.governance.dialogue_editor import speech_plan

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _Draft(scenes, eid="E1"):
    """必须是真 `ScriptDraft`（`_as_draft` 只认它或 Mapping）。

    ⚠️ 用鸭子类型会静默失败：`res.output` 变 None，看着像"职位没产出"。
    """
    from little_crew.agent.messages import ScriptDraft

    return ScriptDraft(frm="screenwriter", to="*", episode_id=eid,
                       title="T", scenes=tuple(scenes))


def _env(draft):
    return CallEnvelope.root("dialogue_editor", "shot.voice",
                            {"script.final": draft}, Budget.unlimited(),
                            callee_kind="agent")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("对白编辑（script.final -> shot.voice）—— 自检")
    print("=" * 66)

    scenes = [
        {"scene_id": "S01", "dialogue": [
            {"character": "少女", "line": "你不能死在这里。", "emotion": "咬牙"},
            {"character": "旅人", "line": "……别碰我。", "emotion": "昏沉"},
        ]},
        {"scene_id": "S02", "dialogue": []},
    ]
    ag = DialogueEditorAgent()
    res = ag(envelope=_env(_Draft(scenes)), runtime=None)
    check("A1 ★★★ 产出成功", bool(getattr(res, "ok", False)), "")
    plan = res.output.get("shot.voice") or {}
    check("A2 ★★★ 产物名是 shot.voice", plan.get("kind") == "shot.voice", plan.get("kind"))
    check("A3 ★★★ 句数正确（空场不产句）", plan.get("lineCount") == 2,
          str(plan.get("lineCount")))
    check("A4 ★★ 保留角色与情绪",
          plan["lines"][0]["character"] == "少女" and plan["lines"][0]["emotion"] == "咬牙",
          str(plan["lines"][0])[:60])
    check("A5 ★★ 保留场号（下游要按镜头摆时间轴）",
          plan["lines"][0]["sceneId"] == "S01", plan["lines"][0]["sceneId"])

    print(); print("B. 与规则模块一致（不是另算一套）")
    ref = speech_plan("你不能死在这里。")
    check("B1 ★★★ 估时与 speech_plan 一致",
          abs(plan["lines"][0]["estimateSeconds"] - float(ref["estimate"])) < 0.01,
          f"{plan['lines'][0]['estimateSeconds']} vs {ref['estimate']}")
    check("B2 ★★ 句尾呼吸也被带出来", plan["lines"][0]["tailPause"] > 0,
          str(plan["lines"][0]["tailPause"]))
    check("B3 ★★ 停顿带数字", "pauseSeconds" in plan["lines"][0], "")

    print(); print("C. 边界")
    ag2 = DialogueEditorAgent()
    res2 = ag2(envelope=_env(_Draft([{"scene_id": "S01", "dialogue": []}])), runtime=None)
    plan2 = res2.output.get("shot.voice") or {}
    check("C1 ★★★ 没有台词也产出（lines=[]，不返回 None）",
          bool(getattr(res2, "ok", False)) and plan2.get("lines") == [], str(plan2)[:60])
    res3 = ag2(envelope=CallEnvelope.root("dialogue_editor", "shot.voice", {},
                                         Budget.unlimited()), runtime=None)
    check("C2 ★★★ 没收到剧本 -> 明确失败", getattr(res3, "ok", True) is False, "")
    check("C3 ★★ 说明「没做什么」",
          any("口型" in x for x in (plan.get("notDone") or ())), str(plan.get("notDone")))
    check("C4 ★★ 总时长是各句之和",
          abs(plan["totalSeconds"] - sum(x["estimateSeconds"]
                                        for x in plan["lines"])) < 0.05,
          str(plan["totalSeconds"]))

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
