"""`runtime/chapter_plan.py` 自检（2026-11-17，0.6.0 系列层）。

章卡三字段 / 边界契约 / 依赖拓扑 / 总导演上下文守门（不读正文）/ brief / F-CONT 差异。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _contract(place="站台", chars=("少女",), props=("手机",), threads=("等谁",)):
    return {"placeKey": place, "time": "夜", "characters": list(chars),
            "props": list(props), "openThreads": list(threads)}


def _plan():
    return {
        "schema": "little_crew.chapter_plan/v1",
        "logline": "少女在末班地铁等一个不会来的人",
        "volumes": [{"name": "第一卷", "chapters": ["CH01", "CH02", "CH03"]}],
        "chapters": [
            {"chapterId": "CH01", "name": "末班车",
             "card": {"goal": "等车", "conflict": "车快没了", "hook": "有人从柱子后出现"},
             "entry": _contract(chars=["少女"]),
             "exit": _contract(chars=["少女", "旅人"], threads=("他是谁",)),
             "dependsOn": []},
            {"chapterId": "CH02", "name": "同行",
             "card": {"goal": "同行", "conflict": "她不信他", "hook": "他记得她小时候"},
             "entry": _contract(chars=["少女", "旅人"], threads=("他是谁",)),
             "exit": _contract(chars=["少女", "旅人"], threads=("记忆真相",)),
             "dependsOn": ["CH01"]},
            {"chapterId": "CH03", "name": "终点",
             "card": {"goal": "到终点", "conflict": "真相伤人", "hook": "她选择留下"},
             "entry": _contract(chars=["少女", "旅人"], threads=("记忆真相",)),
             "exit": _contract(chars=["少女"], threads=()),
             "dependsOn": ["CH02"]},
        ],
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("章节契约 / 总导演上下文 —— 自检")
    print("=" * 66)

    from little_crew.runtime import chapter_plan as CP

    print(); print("A. 章卡与契约校验")
    plan = _plan()
    check("A1 ★★★ 合法 plan 通过", CP.validate_plan(plan) == [], str(CP.validate_plan(plan)))
    bad = _plan()
    bad["chapters"][0]["card"]["hook"] = ""
    check("A2 ★★★ 章卡缺 hook -> F-CARD",
          any("缺 hook" in e for e in CP.validate_plan(bad)), str(CP.validate_plan(bad)))
    bad2 = _plan()
    del bad2["chapters"][1]["entry"]
    check("A3 ★★★ 缺 entry 契约 -> 报错",
          any("缺 entry" in e for e in CP.validate_plan(bad2)), "")
    bad3 = _plan()
    bad3["chapters"][1]["dependsOn"] = ["CH99"]
    check("A4 ★★ 依赖不存在 -> 报错",
          any("CH99" in e for e in CP.validate_plan(bad3)), "")
    bad4 = _plan()
    bad4["chapters"][0]["dependsOn"] = ["CH01"]
    check("A5 ★★ 依赖自己 -> 报错",
          any("依赖自己" in e for e in CP.validate_plan(bad4)), "")

    print(); print("B. 依赖拓扑")
    check("B1 ★★★ 拓扑序 = CH01→CH02→CH03",
          CP.topo_order(plan) == ["CH01", "CH02", "CH03"], str(CP.topo_order(plan)))
    cyc = _plan()
    cyc["chapters"][0]["dependsOn"] = ["CH03"]
    try:
        CP.topo_order(cyc)
        ok = False
    except ValueError:
        ok = True
    check("B2 ★★★ 依赖环 -> 抛错", ok, "")

    print(); print("C. 总导演上下文：只读章名/章卡/契约，不读正文")
    ctx = CP.showrunner_context(plan)
    raw = str(ctx)
    check("C1 ★★★ 上下文含章名/章卡/契约",
          "末班车" in raw and "等车" in raw and "station" not in raw and "openThreads" in raw, "")
    check("C2 ★★★ 上下文不含正文字段（text/script/body）",
          all(k not in raw for k in ("'text'", '"text"', "'script'", "body")), "")
    try:
        CP.assert_no_script({"logline": "x", "chapters": [{"name": "y", "text": "整章正文"}]})
        blocked = False
    except ValueError:
        blocked = True
    check("C3 ★★★ assert_no_script 拦住正文注入", blocked, "")
    ctx2 = CP.showrunner_context(plan, chapter_ids=["CH02"])
    check("C4 ★★ 可按章切片（只给需要审的章）",
          len(ctx2["chapters"]) == 1 and ctx2["chapters"][0]["chapterId"] == "CH02", "")

    print(); print("D. 子导演 brief / 边界 F-CONT")
    brief = CP.build_brief(plan, "CH02")
    check("D1 ★★★ brief 含 entry/exit/章卡/prevRecap",
          brief["chapterId"] == "CH02" and brief["entry"]["placeKey"] == "站台"
          and brief["prevRecap"]["chapterId"] == "CH01", str(brief)[:120])
    diff_ok = CP.contract_diff(plan["chapters"][0]["exit"], plan["chapters"][1]["entry"])
    check("D2 ★★ 正常衔接 diff 为空", diff_ok == [], str(diff_ok))
    cur = _plan()
    cur["chapters"][1]["entry"]["characters"] = ["少女"]   # 旅人消失
    diff = CP.contract_diff(cur["chapters"][0]["exit"], cur["chapters"][1]["entry"])
    check("D3 ★★★ F-CONT：上一章 exit 的人物在下一章 entry 消失 -> 检出",
          any("旅人" in x for x in diff), str(diff))
    cur2 = _plan()
    cur2["chapters"][1]["entry"]["placeKey"] = "医院"
    diff2 = CP.contract_diff(cur2["chapters"][0]["exit"], cur2["chapters"][1]["entry"])
    check("D4 ★★★ placeKey 不一致 -> 检出",
          any("placeKey" in x for x in diff2), str(diff2))

    print(); print("E. 汇总")
    s = CP.plan_summary(plan)
    check("E1 ★★ summary 含章数/顺序", s["chapters"] == 3
          and s["order"] == ["CH01", "CH02", "CH03"], str(s))

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
