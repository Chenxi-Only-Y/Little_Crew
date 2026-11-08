"""模型路由 + 跨族审查自检。"""
from __future__ import annotations

import os
import sys

from little_crew.governance.model_routing import (
    DEFAULT_MODEL, ROUTES, explain, route, table,
)
from little_crew.runtime.model_family import cross_family, family_of, pick_judge

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("模型路由（任务类型 + 跨族审查）—— 自检")
    print("=" * 66)

    print(); print("A. 任务路由")
    check("A1 ★★ 已知任务给表里的模型",
          route("strict_json") == ROUTES["strict_json"]["model"], route("strict_json"))
    check("A2 ★★★ 未知任务回默认（不报错、不猜）",
          route("不存在的任务") == DEFAULT_MODEL, route("不存在的任务"))
    os.environ["LITTLE_CREW_MODEL_JSON"] = "mimo-v2.6-flash"
    check("A3 ★★ 环境变量覆盖", route("strict_json") == "mimo-v2.6-flash", route("strict_json"))
    check("A4 ★★ explain 标出 routedBy=env",
          explain("strict_json")["routedBy"] == "env", str(explain("strict_json")))
    os.environ.pop("LITTLE_CREW_MODEL_JSON", None)
    check("A5 ★ 路由表覆盖所有登记任务",
          {r["task"] for r in table()} == set(ROUTES), "")
    check("A6 ★★ 每个任务都写了「为什么」", all(r["why"] for r in table()), "")

    print(); print("B. 族判定（按端点域名，不按名字）")
    check("B1 ★★★ deepseek 族", family_of("deepseek-flash") == "deepseek", family_of("deepseek-flash"))
    check("B2 ★★★ GLM 族是 bigmodel（不是 glm）",
          family_of("GLM-5.3-flash") == "bigmodel", family_of("GLM-5.3-flash"))
    check("B3 ★★ mimo 族", family_of("mimo-v2.6-flash") == "xiaomimimo", family_of("mimo-v2.6-flash"))
    check("B4 ★★★ 未知模型 -> unknown（**不假定跨族**）",
          family_of("不存在的模型") == "unknown", family_of("不存在的模型"))

    print(); print("C. 跨族判定")
    same = cross_family("deepseek-flash", "deepseek-flash")
    check("C1 ★★★ 同族 -> independent=False", same["independent"] is False, str(same))
    diff = cross_family("deepseek-flash", "GLM-5.3-flash")
    check("C2 ★★★ 跨族 -> independent=True", diff["independent"] is True, diff["reason"])
    unk = cross_family("deepseek-flash", "不存在的模型")
    check("C3 ★★★ 族未知 -> 判**不独立**（宁严不松）",
          unk["independent"] is False and "未知" in unk["reason"], unk["reason"])

    print(); print("D. 自动挑审查模型")
    p1 = pick_judge("deepseek-flash")
    check("D1 ★★★ 为 deepseek 挑到跨族的",
          p1["ok"] and p1["judgeFamily"] != p1["genFamily"], str(p1))
    p2 = pick_judge("GLM-5.3-flash")
    check("D2 ★★★ 为 GLM 挑到另一族（不会挑自己）",
          p2["ok"] and p2["model"] != "GLM-5.3-flash", str(p2))
    p3 = pick_judge("deepseek-flash", candidates=["deepseek-flash"])
    check("D3 ★★★ 候选里只有同族 -> ok=False（宁可不审）",
          p3["ok"] is False and "没有可用" in p3["reason"], p3["reason"])

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
