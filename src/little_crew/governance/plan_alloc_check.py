"""`runtime/plan_alloc` 自检 —— 纯算术，不调模型、不联网。"""
from __future__ import annotations

import sys

from little_crew.governance.plan_alloc import (
    DEFAULT_SCENE_SECONDS, MAX_SHOTS_PER_SCENE, allocate, preferred_avg,
    prompt_lines, scene_quota,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景预算协调器 —— 自检")
    print("=" * 66)

    print(); print("A. 由总时长反推镜数")
    q30 = scene_quota(scene_seconds=30)
    check("A1 ★ 30s -> 镜数区间不自相矛盾",
          q30["shotMin"] <= q30["shotIdeal"] <= q30["shotMax"], str(q30))
    check("A2 ★★ 镜数有常识上限（不是 30s 切 15 镜）",
          q30["shotMax"] <= MAX_SHOTS_PER_SCENE, str(q30["shotMax"]))
    check("A3 ★★★ 最少镜数乘最短时长不超过总时长（可行）",
          q30["shotMin"] * q30["singleMin"] <= q30["sceneSeconds"] + 1e-6,
          f"{q30['shotMin']}x{q30['singleMin']} vs {q30['sceneSeconds']}")

    q80 = scene_quota(scene_seconds=80)
    check("A4 ★ 80s 不会给 40 镜（封顶生效）",
          q80["shotMax"] <= MAX_SHOTS_PER_SCENE, str(q80["shotMax"]))

    print(); print("B. 边界")
    check("B1 0s / 负数 -> 至少 1 镜不崩",
          scene_quota(scene_seconds=0)["shotMax"] >= 1,
          str(scene_quota(scene_seconds=0)))
    check("B2 ★ 1s 极短 -> 只给 1 镜", scene_quota(scene_seconds=1)["shotMax"] == 1,
          str(scene_quota(scene_seconds=1)["shotMax"]))
    check("B3 600s 极长 -> 仍封顶",
          scene_quota(scene_seconds=600)["shotMax"] <= MAX_SHOTS_PER_SCENE, "")
    weird = scene_quota(scene_seconds=10, single_min=5, longest_cap=2)
    check("B4 ★★ 上限比下限还小（不合理输入）-> 标 tight 且区间不空",
          weird["tight"] is True and weird["shotMin"] <= weird["shotMax"], str(weird))

    print(); print("C. 导演的节奏权重只影响目标均值")
    short = preferred_avg(2, 10, avg_target="偏短")
    mid = preferred_avg(2, 10, avg_target="")
    long_ = preferred_avg(2, 10, avg_target="偏长", allow_long=True)
    check("C1 ★ 偏短 < 中性 < 偏长", short < mid < long_,
          f"{short}/{mid}/{long_}")
    check("C2 ★★ 允许长镜头时目标更长",
          long_ > preferred_avg(2, 10, avg_target="偏长", allow_long=False), "")
    qa = scene_quota(scene_seconds=30, avg_target="偏短")
    qb = scene_quota(scene_seconds=30, avg_target="偏长", allow_long=True)
    check("C3 ★★ 偏短 -> 目标镜数更多", qa["shotIdeal"] >= qb["shotIdeal"],
          f"{qa['shotIdeal']} vs {qb['shotIdeal']}")

    print(); print("D. 多场分配（按优先级削）")
    scenes = [
        {"sceneId": "S01", "sceneSeconds": 30, "priority": 5},
        {"sceneId": "S02", "sceneSeconds": 30, "priority": 1},
    ]
    free = allocate(scenes)
    check("D1 不封顶时不动任何场", free["trimmed"] is False, str(free["totalIdeal"]))
    capped = allocate(scenes, global_shot_cap=free["totalMin"] + 1)
    check("D2 ★★ 封顶时触发裁剪", capped["trimmed"] is True,
          f"cap={free['totalMin']+1} ideal={free['totalIdeal']}")
    s2 = next(q for q in capped["scenes"] if q["sceneId"] == "S02")
    s1 = next(q for q in capped["scenes"] if q["sceneId"] == "S01")
    check("D3 ★★★ 先削优先级低的（S02）", s2["shotIdeal"] <= s1["shotIdeal"],
          f"S01={s1['shotIdeal']} S02={s2['shotIdeal']}")
    check("D4 ★★ 任何场都不被压到 shotMin 以下",
          all(q["shotIdeal"] >= q["shotMin"] for q in capped["scenes"]),
          str([(q["sceneId"], q["shotIdeal"], q["shotMin"]) for q in capped["scenes"]]))

    print(); print("E. 确定性 / 提示词")
    check("E1 同输入同输出（无隐藏随机）",
          scene_quota(scene_seconds=30) == scene_quota(scene_seconds=30), "")
    line = prompt_lines(q30)[0]
    check("E2 ★ 提示词写清总时长/镜数/单镜范围",
          "总时长" in line and "镜数" in line and "单镜" in line, line)
    r = allocate([])
    check("E3 空场次列表不崩", r["totalIdeal"] == 0 and r["scenes"] == [], "")
    check("E4 默认总时长是 30s", DEFAULT_SCENE_SECONDS == 30.0, "")


    print(); print("F. 同优先级与软超帽（2026-11-06 补）")
    # 两场同为 priority=1，但可削量不同：S02 可削更多
    tie = [
        {"sceneId": "S01", "sceneSeconds": 10, "priority": 1},
        {"sceneId": "S02", "sceneSeconds": 60, "priority": 1},
    ]
    r0 = allocate(tie)
    slack = {q["sceneId"]: q["shotIdeal"] - q["shotMin"] for q in r0["scenes"]}
    cap = r0["totalIdeal"] - 1
    r1 = allocate(tie, global_shot_cap=cap)
    check("F1 ★★ 同优先级时先削「可削量」大的",
          r1["trimOrder"][:1] == [max(slack, key=slack.get)],
          f"slack={slack} order={r1['trimOrder']}")

    # 语义反转守卫：当前约定 priority=1 是**最低**优先级。
    # 若有人把 1 改成"最高"，这条断言会失败 —— 这正是它存在的意义。
    guard = [
        {"sceneId": "S09", "sceneSeconds": 30, "priority": 1},
        {"sceneId": "S08", "sceneSeconds": 30, "priority": 5},
    ]
    rg = allocate(guard, global_shot_cap=allocate(guard)["totalIdeal"] - 1)
    check("F2 ★★★ priority=1 是「最低」-> 第一个被削的是 S09",
          rg["trimOrder"][:1] == ["S09"], str(rg["trimOrder"]))

    # 全部压到 N_min 仍超帽 -> 软超帽，不再削
    hard = [{"sceneId": "S01", "sceneSeconds": 30, "priority": 3},
            {"sceneId": "S02", "sceneSeconds": 30, "priority": 3}]
    rmin = allocate(hard)
    r2 = allocate(hard, global_shot_cap=rmin["totalMin"] - 1)
    check("F3 ★★★ 削到 N_min 仍超帽 -> softCapExceeded",
          r2["softCapExceeded"] is True, str(r2.get("overBy")))
    check("F4 ★★ 软超帽时 overBy 记下超了多少", r2["overBy"] > 0, str(r2["overBy"]))
    check("F5 ★★★ 软超帽时任何场都不低于 N_min（不产生空场）",
          all(q["shotIdeal"] >= q["shotMin"] for q in r2["scenes"]),
          str([(q["sceneId"], q["shotIdeal"], q["shotMin"]) for q in r2["scenes"]]))


    print(); print("G. 一轮时长再分配（P4，防震荡）")
    from little_crew.governance.plan_alloc import MAX_BORROW, redistribute

    def _q(*pairs):
        return {"scenes": [{"sceneId": s, "sceneSeconds": c} for s, c in pairs]}

    def _au(*pairs):
        return {"scenes": [{"sceneId": s, "seconds": u} for s, u in pairs]}

    qq = _q(("S01", 30), ("S02", 20))
    r = redistribute(qq, _au(("S01", 12), ("S02", 26)))
    check("G1 ★★ 余量场借给超时场", r["applied"] and r["moves"], str(r["moves"]))
    check("G2 ★★ 借入方上限提高、借出方降低",
          dict((x["sceneId"], x["sceneSeconds"]) for x in qq["scenes"])["S02"] > 20,
          str([(x["sceneId"], x["sceneSeconds"]) for x in qq["scenes"]]))
    check("G3 ★ 借出方不会被掏空（仍 > 已用量）",
          dict((x["sceneId"], x["sceneSeconds"]) for x in qq["scenes"])["S01"] > 12,
          "")
    q2 = _q(("S01", 30), ("S02", 20))
    r2 = redistribute(q2, _au(("S01", 12), ("S02", 26)), round_no=2, max_rounds=1)
    check("G4 ★★★ 第二轮一律不生效（防震荡）",
          r2["applied"] is False and "上限" in r2["reason"], r2["reason"])
    q3 = _q(("S01", 30), ("S02", 20))
    r3 = redistribute(q3, _au(("S01", 12), ("S02", 12)))
    check("G5 没有超时场 -> 不动", r3["applied"] is False and r3["moves"] == [], "")
    q4 = _q(("S01", 20), ("S02", 30))
    r4 = redistribute(q4, _au(("S01", 26), ("S02", 10)))
    moved = sum(m["seconds"] for m in r4["moves"])
    check("G6 ★★ 借入不超过「缺口」且不超过 MAX_BORROW",
          moved <= min(6.0, 20 * MAX_BORROW) + 1e-6, f"moved={moved}")

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
