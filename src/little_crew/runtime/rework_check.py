"""`runtime/rework` 自检 —— 纯函数，不调模型。"""
from __future__ import annotations

import sys

from little_crew.runtime.rework import (
    merge_reworked, plan, select_targets, should_rework,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _S:
    def __init__(self, sid, scene, tag=""):
        self.shot_id, self.scene_id, self.tag = sid, scene, tag

    def __repr__(self):
        return f"<{self.shot_id}>"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("定向返工 —— 自检")
    print("=" * 66)

    intr = {"interface": "script->storyboard", "kind": "scene.intrusion",
            "severity": "error", "unit": {"shotId": "SH-2"}}
    over = {"interface": "storyboard->render", "kind": "duration.over",
            "severity": "error", "unit": {"shotId": "SH-5"}}
    warn = {"interface": "director->storyboard", "kind": "budget.deviation",
            "severity": "warn", "unit": {"sceneId": "S02"}}

    print(); print("A. 该不该重做")
    check("A1 ★ 只有 warn 不重做", should_rework([warn]) is False, "")
    check("A2 ★★ 有 error 才重做", should_rework([warn, intr]) is True, "")
    check("A3 空清单不重做", should_rework([]) is False, "")

    print(); print("B. 选目标")
    t = select_targets([warn, intr, over])
    check("B1 ★ warn 不进目标", "S02" not in t["scenes"], str(t))
    check("B2 ★★ error 的镜头进 shots", t["shots"] == ["SH-2", "SH-5"], str(t))

    sc = {"interface": "script->storyboard", "kind": "scene.intrusion",
          "severity": "error", "unit": {"shotId": "SH-1", "sceneId": "S03"}}
    t2 = select_targets([sc])
    check("B3 ★★ 场级缺陷 -> scenes 且关联镜头也进", 
          t2["scenes"] == ["S03"] and t2["shots"] == ["SH-1"], str(t2))
    check("B4 未知类型也收（保守）",
          select_targets([{"kind": "???", "severity": "error",
                           "unit": {"sceneId": "S09"}}])["scenes"] == ["S09"], "")

    print(); print("C. 合并（关键：不丢镜头）")
    old = [_S("SH-1", "S01"), _S("SH-2", "S02", "old"), _S("SH-3", "S02", "old")]
    new = [_S("SH-2", "S02", "new")]
    out = merge_reworked(old, new, {"shots": [], "scenes": ["S02"]})
    check("C1 ★ 目标场用新结果", [x.tag for x in out][1:] == ["new", "old"], 
          str([x.tag for x in out]))
    check("C2 ★★ 非目标镜头原样保留", out[0].tag == "" and len(out) == 3, str(out))
    out2 = merge_reworked(old, [], {"shots": ["SH-2"], "scenes": []})
    check("C3 ★★★ 重做没返回时**保留旧的**（不丢镜头）",
          len(out2) == 3 and out2[1].tag == "old", str(out2))
    out3 = merge_reworked(old, new, {"shots": ["SH-9"], "scenes": []})
    check("C4 目标不存在时不动任何镜头", [x.tag for x in out3] == ["", "old", "old"],
          str([x.tag for x in out3]))

    print(); print("D. 报告摘要")
    p = plan([warn, intr, over])
    check("D1 ★ 摘要里写明重做几处", p["shouldRework"] and p["count"] == 2, str(p))
    p2 = plan([warn])
    check("D2 无需重做时说清", p2["shouldRework"] is False and "无需" in p2["summary"],
          p2["summary"])

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
