"""场景主体修复自检（确定性，不调模型）。"""
from __future__ import annotations

import sys

from little_crew.agent.messages import Shot
from little_crew.runtime import subject_repair as SR

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _shot(sid, sc, desc):
    return Shot(frm="sb", to="*", shot_id=sid, scene_id=sc, shot_size="full",
                camera_movement="static", duration_sec=5.0,
                characters=("少女",), description=desc)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景主体修复 —— 自检")
    print("=" * 66)

    print(); print("A. 计划")
    s1 = _shot("A", "S01", "少女在花田里采花")
    s2 = _shot("B", "S01", "少女在岩石边蹲下")
    rep = SR.plan([s1, s2], ["山坡", "岩石", "木屋"],
                  by_scene={"S01": ["山坡", "岩石"]})
    check("A1 ★★★ 检出越权词", any(r["old"] == "花田" for r in rep), str(rep)[:80])
    hit = next(r for r in rep if r["old"] == "花田")
    check("A2 ★★★ 换成本场的词（花田→山坡）",
          hit["new"] == "山坡" and hit["applied"], str(hit))
    check("A3 ★★ 授权词不动（岩石不产计划）",
          not any(r["old"] == "岩石" for r in rep), str([r["old"] for r in rep]))
    check("A4 ★★ 记录来源（scene-default）", hit.get("via") == "scene-default", hit.get("via"))

    print(); print("B. 执行")
    out, done = SR.apply([s1, s2], rep)
    check("B1 ★★★ 文本被替换", "山坡" in out[0].description, out[0].description)
    check("B2 ★★★ 原对象不被改（纯函数）", "花田" in s1.description, s1.description)
    check("B3 ★★ 记录含前后对照", bool(done and done[0].get("after")), str(done[:1])[:80])
    check("B4 ★★ 未命中的镜头原样保留", out[1].description == s2.description, out[1].description)

    print(); print("C. 边界")
    check("C1 ★★ 没有越权词 -> 计划为空",
          SR.plan([_shot("C", "S01", "少女在岩石边")], ["岩石"],
                  by_scene={"S01": ["岩石"]}) == [], "")
    check("C2 ★★ 没有授权词 -> 不瞎猜（applied=False）",
          all(not r["applied"] for r in SR.plan([_shot("D", "S01", "花田")], [])), "")
    r3 = SR.plan([_shot("E", "S99", "花田")], ["山坡"])
    # 场号未知时不瞎猜（字面相似度 0，无法确定该用哪个地点）——
    # 留给门禁否决，比"换成别场的词"更安全。
    check("C3 ★★★ 场号未知且无相似词 -> 不替换（不瞎猜）",
          bool(r3) and all(not x["applied"] for x in r3), str(r3)[:80])

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
