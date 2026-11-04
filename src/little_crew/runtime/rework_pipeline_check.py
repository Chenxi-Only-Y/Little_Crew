"""`runtime/rework_pipeline` 自检 —— 注入假的 run_stage，不调模型。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from little_crew.runtime.rework_pipeline import rework_targets

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, sid, scene):
        self.shot_id, self.scene_id = sid, scene


def _draft(scenes):
    # 只要能被 getattr(draft,"scenes") 拿到、且可 dataclasses.replace 就行
    return SimpleNamespace(episode_id="E", scenes=tuple(
        {"scene_id": s, "heading": s, "text": "x", "characters": []} for s in scenes))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("定向返工（执行层）—— 自检")
    print("=" * 66)

    intr = {"kind": "scene.intrusion", "severity": "error",
            "unit": {"shotId": "SH-2", "sceneId": "S02"}}
    warn = {"kind": "budget.deviation", "severity": "warn", "unit": {"sceneId": "S03"}}

    print(); print("A. 不该重做的情形")
    calls = []

    def fake_stage(agent, cap, payload):
        calls.append(SimpleNamespace(n=len(payload.get("script.final").scenes)))
        return SimpleNamespace(ok=True, error="")

    r = rework_targets(run_stage=fake_stage, draft=_draft(["S01", "S02"]),
                       defects=[warn], shots=(_Shot("SH-1", "S01"),))
    check("A1 ★ 只有 warn -> 跳过", r["ok"] and r["skipped"], str(r))
    check("A2 跳过时不调模型", calls == [], str(calls))

    print(); print("B. 重做目标场")
    calls.clear()
    store = {"sl": None}

    def stage_ok(agent, cap, payload):
        scenes = payload["script.final"].scenes
        calls.append(len(scenes))
        sid = scenes[0]["scene_id"]
        store["sl"] = SimpleNamespace(
            shots=(_Shot(f"{sid}-NEW-1", sid), _Shot(f"{sid}-NEW-2", sid)))
        return SimpleNamespace(ok=True, error="")

    r = rework_targets(run_stage=stage_ok, draft=_draft(["S01", "S02"]),
                       defects=[intr],
                       shots=(_Shot("SH-1", "S01"), _Shot("SH-2", "S02")),
                       get_shots=lambda: store["sl"])
    check("B1 ★★ 只调了 1 次（只重做 S02）", len(calls) == 1, str(calls))
    check("B2 ★★★ 每次只喂一场（不是整本剧本）", calls == [1], str(calls))
    check("B3 ★★ 合并后 S02 用新镜头", any(
        "S02-NEW" in getattr(s, "shot_id", "") for s in r["shots"]), "")
    check("B4 ★★★ S01 的旧镜头保留（不丢）", any(
        getattr(s, "shot_id", "") == "SH-1" for s in r["shots"]), str(r["shots"]))
    check("B5 报告里写明重做几场", "重做 1 场" in r["reason"], r["reason"])

    print(); print("C. 失败与上限")
    def stage_bad(agent, cap, payload):
        return SimpleNamespace(ok=False, error="模型挂了")

    r = rework_targets(run_stage=stage_bad, draft=_draft(["S01", "S02"]),
                       defects=[intr], shots=(_Shot("SH-2", "S02"),))
    check("C1 ★ 单场失败不抛，返回 ok=False", r["ok"] is False, str(r))
    check("C2 镜头表原样返回（不丢）",
          getattr(r["shots"][0], "shot_id") == "SH-2", str(r["shots"]))

    many = [{"kind": "scene.intrusion", "severity": "error",
             "unit": {"sceneId": f"S{i:02d}"}} for i in range(1, 7)]
    calls.clear()
    r = rework_targets(run_stage=stage_ok, draft=_draft([f"S{i:02d}" for i in range(1, 7)]),
                       defects=many, shots=(), get_shots=lambda: store["sl"],
                       max_scenes=3)
    check("C3 ★★ 最多只重做 max_scenes 场（默认 3）", len(calls) == 3, str(len(calls)))

    r2 = rework_targets(run_stage=stage_ok, draft=_draft(["S01"]),
                        defects=[{"kind": "scene.intrusion", "severity": "error",
                                  "unit": {"sceneId": "S99"}}], shots=())
    check("C4 剧本里没有该场 -> 记失败但不崩",
          r2["ok"] is False and r2["failed"], str(r2.get("failed")))

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
