"""`review/interface_defects` 自检 —— 纯数据结构，不联网。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.review.interface_defects import (
    BUDGET, DURATION, SCENE, collect, from_budget, from_durations,
    from_scene_conflicts, write,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, sid, scene, dur):
        self.shot_id, self.scene_id, self.duration_sec = sid, scene, dur


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("接口缺陷清单 —— 自检")
    print("=" * 66)

    print(); print("A. 越权 -> 缺陷")
    conf = [
        {"shotId": "S1", "severity": "error", "intruded": ["花田"],
         "script": ["山坡"], "reason": "引入花田"},
        {"shotId": "S2", "severity": "warn", "intruded": [], "script": ["山坡"]},
    ]
    d = from_scene_conflicts(conf)
    check("A1 只有 error 进清单（warn 不进）", len(d) == 1, str(len(d)))
    check("A2 ★ 带单元（shotId）", d and d[0]["unit"]["shotId"] == "S1", "")
    check("A3 ★ 带授权列表与越权主体",
          d and d[0]["detail"]["authorized"] == ["山坡"]
          and d[0]["detail"]["intruded"] == ["花田"], str(d[0]["detail"]) if d else "")
    check("A4 接口名正确", d and d[0]["interface"] == SCENE, "")

    print(); print("B. 超时 -> 缺陷")
    shots = [_Shot("A", "S01", 6.0), _Shot("B", "S01", 12.0), _Shot("C", "S02", 10.0)]
    d = from_durations(shots, 10.0)
    check("B1 ★ 只有超过上限的进清单", len(d) == 1, str(len(d)))
    check("B2 带秒数与上限", d and d[0]["detail"]["seconds"] == 12.0, "")
    check("B3 等于上限不算超", not any(x["detail"]["seconds"] == 10.0 for x in d), "")

    print(); print("C. 预算偏差 -> 缺陷")
    plan = {"scenes": [{"sceneId": "S01", "shotBudget": 3},
                       {"sceneId": "S02", "shotBudget": 5}]}
    sh = [_Shot("1", "S01", 4.0), _Shot("2", "S01", 4.0), _Shot("3", "S01", 4.0),
          _Shot("4", "S02", 4.0), _Shot("5", "S02", 4.0)]
    d = from_budget(plan, sh)
    check("C1 ★ S01 命中（3 镜 vs 预算 3 镜不算偏差）", not any(
        x["unit"]["sceneId"] == "S01" for x in d), str([x["unit"] for x in d]))
    check("C2 ★★ S02 命中（预算 5 实给 2）", any(
        x["unit"]["sceneId"] == "S02" for x in d), str([x["unit"] for x in d]))
    check("C3 偏差是 warn（不阻断）", all(x["severity"] == "warn" for x in d), "")
    check("C4 无预算 -> 空清单", from_budget({}, sh) == [], "")

    print(); print("D. 汇总与落盘")
    p = collect(scene_conflicts=conf, shots=shots, limit_s=10.0, plan=plan,
                episode_id="E1")
    check("D1 汇总三类都进去", p["total"] >= 3, str(p["byInterface"]))
    check("D2 ★ 有 byInterface 统计", set(p["byInterface"]) >= {SCENE, DURATION}, "")
    with tempfile.TemporaryDirectory() as td:
        dst = write(Path(td), p)
        check("D3 ★ 落到 delivery/interface_defects.json",
              dst.name == "interface_defects.json" and dst.is_file(), str(dst))
        check("D4 可读回且 kind 正确",
              '"interface.defects"' in dst.read_text(encoding="utf-8"), "")
    check("D5 空输入不崩", collect()["total"] == 0, "")

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
