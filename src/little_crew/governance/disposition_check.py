"""`governance/disposition` 自检 —— 纯函数，不调模型。"""
from __future__ import annotations

import sys

from little_crew.governance.disposition import (
    FILL, MERGE, NOOP, SPLIT, TRIM, actions_for, instruction_for, plan,
    prompt_lines,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


QUOTA = {"scenes": [{"sceneId": "S02", "sceneSeconds": 30, "shotMin": 3,
                     "shotMax": 8, "singleMax": 8}]}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("处置闭环（缺陷 -> 怎么修）—— 自检")
    print("=" * 66)

    print(); print("A. 缺陷 -> 动作")
    check("A1 ★ 超场时长 -> 合并 + 压缩", actions_for("over-scene-seconds")
          == (MERGE, TRIM), str(actions_for("over-scene-seconds")))
    check("A2 ★ 单镜超限 -> 切分", actions_for("over-single-max") == (SPLIT,), "")
    check("A3 ★★ 串镜 -> 只报不改（NOOP）",
          actions_for("duplicate-artifact") == (NOOP,), "")
    check("A4 ★★★ 未知类型 -> 空（不乱给指令）", actions_for("???") == (), "")

    print(); print("B. 指令必须带数字")
    s1 = instruction_for({"sceneId": "S02", "kind": "over-scene-seconds",
                          "seconds": 36}, QUOTA["scenes"][0])
    check("B1 ★★ 说清超了多少、上限多少",
          "36" in s1 and "30" in s1, s1[:60])
    check("B2 ★★★ 不许提「删镜头」，要提合并/压缩",
          "删" not in s1 and ("合并" in s1 or "压缩" in s1), s1[:60])
    check("B3 ★★ 同时保住镜头数下限", "3" in s1, s1[:60])

    s2 = instruction_for({"sceneId": "S02", "kind": "over-single-max",
                          "maxShot": 11}, QUOTA["scenes"][0])
    check("B4 ★★ 单镜超限 -> 切成两镜，且不许丢内容",
          "切" in s2 and "不许丢内容" in s2, s2[:60])
    check("B5 ★ 指令里带单镜上限 8", "8" in s2, s2[:60])

    print(); print("C. 汇总与提示词")
    d = plan([{"sceneId": "S02", "kind": "over-scene-seconds", "seconds": 36},
              {"sceneId": "S02", "kind": "over-single-max", "maxShot": 11},
              {"sceneId": "S03", "kind": "duplicate-artifact"}], QUOTA)
    check("C1 ★★ 只收有动作者的场（S03 串镜被排除）",
          [x["sceneId"] for x in d["scenes"]] == ["S02"], str(d["scenes"]))
    check("C2 ★★ 动作去重合并", set(d["scenes"][0]["actions"])
          == {MERGE, TRIM, SPLIT}, str(d["scenes"][0]["actions"]))
    lines = prompt_lines(d, "S02")
    check("C3 ★★★ 提示词有明确标题", lines and "必须这样改" in lines[0], str(lines[:1]))
    check("C4 ★★ 无关场返回空", prompt_lines(d, "S99") == [], "")
    check("C5 空输入不崩", plan([])["sceneCount"] == 0, "")


    print(); print("D. 与 audit 的真实对接（数字必须带过来）")
    from little_crew.governance.plan_alloc import audit, quotas_for

    class _S:
        def __init__(self, sid, d):
            self.scene_id, self.duration_sec = sid, d

    _q = quotas_for({}, [{"scene_id": "S01"}, {"scene_id": "S02"}])
    # 人为超限：S01 塞 5 镜 × 9s = 45s，远超它的上限
    shots = [_S("S01", 9.0) for _ in range(5)] + [_S("S02", 3.0)]
    au = audit(_q, shots)
    kinds = {f["kind"] for f in au["findings"]}
    check("D1 ★★ audit 能报出超场时长", "over-scene-seconds" in kinds, str(kinds))
    _f = next(f for f in au["findings"] if f["kind"] == "over-scene-seconds")
    check("D2 ★★★ audit 的 finding 必须带数字（不是只有 reason）",
          isinstance(_f.get("seconds"), (int, float)), str(_f))

    d = plan(au["findings"], _q)
    txt = " ".join(x for s in d["scenes"] for x in s["instructions"])
    check("D3 ★★★ 处置里不许出现 None（真跑踩过）", "None" not in txt, txt[:80])
    check("D4 ★★ 处置里带真实数字 45", "45" in txt, txt[:80])


    print(); print("E. 防御：配额缺字段不许崩")
    from little_crew.governance.plan_alloc import scene_quota

    _bare = scene_quota(scene_seconds=30)          # 单场配额，**没有 sceneId**
    try:
        d2 = plan([{"sceneId": "S09", "kind": "over-scene-seconds",
                    "seconds": 99}], {"scenes": [_bare]})
        ok = True
    except Exception as e:                          # noqa: BLE001
        ok = False
        print("      ", type(e).__name__, e)
    check("E1 ★★★ 配额里没有 sceneId 也不许崩（真踩过 KeyError）", ok, "")
    if ok:
        check("E2 ★★ 没有配额数字时，处置仍给出动作（只是没数字）",
              d2["sceneCount"] == 1, str(d2))

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
