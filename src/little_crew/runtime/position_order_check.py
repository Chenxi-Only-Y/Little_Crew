"""`runtime/position_order.py` 自检（2026-11-17）：必需性 + 顺序依赖。"""
from __future__ import annotations

import sys
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Spec:
    def __init__(self, agent_id, produces=(), requires=(), after=()):
        self.agent_id = agent_id
        self.produces = tuple(produces)
        self.requires = tuple(requires)
        self.after = tuple(after)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("职业依赖与顺序 —— 自检")
    print("=" * 66)

    from little_crew.agent.roster import DEFAULT_ROSTER
    from little_crew.runtime import position_order as PO

    print(); print("A. 产物依赖 → 顺序边")
    edges = PO.dependency_edges()
    check("A1 ★★★ 导演依赖编剧（产物 script.final）",
          "screenwriter" in edges["director"], str(edges["director"]))
    check("A2 ★★★ 分镜依赖导演（显式 after）",
          "director" in edges["storyboard_artist"], str(edges["storyboard_artist"]))
    check("A3 ★★ 分镜依赖角色/场景设计师",
          {"character_designer", "scene_designer"} <= edges["storyboard_artist"],
          str(edges["storyboard_artist"]))
    check("A4 ★★ 摄影依赖分镜/灯光",
          {"storyboard_artist", "gaffer"} <= edges["camera_operator"],
          str(edges["camera_operator"]))
    check("A5 ★★ 审片依赖摄影", "camera_operator" in edges["review_board"], "")
    check("A6 ★★★ 边数非零且无自环",
          sum(len(v) for v in edges.values()) >= 8
          and all(k not in v for k, v in edges.items()), str(sum(len(v) for v in edges.values())))

    print(); print("B. 拓扑顺序")
    order = PO.order_positions()
    rank = {p: i for i, p in enumerate(order)}
    check("B1 ★★★ 顺序覆盖 19 职位", len(order) == 19 and len(set(order)) == 19, str(len(order)))
    check("B2 ★★★ 编剧在导演前", rank["screenwriter"] < rank["director"], "")
    check("B3 ★★★ 导演在分镜前", rank["director"] < rank["storyboard_artist"], "")
    check("B4 ★★★ 分镜在摄影前", rank["storyboard_artist"] < rank["camera_operator"], "")
    check("B5 ★★★ 摄影在审片前", rank["camera_operator"] < rank["review_board"], "")

    print(); print("C. 必需性校验")
    all_ids = [s.agent_id for s in DEFAULT_ROSTER]
    full = PO.validate_dependencies(all_ids)
    check("C1 ★★★ 全职业通过", full["ok"] and not full["errors"], str(full["errors"]))
    missing = [x for x in all_ids if x != "screenwriter"]
    bad = PO.validate_dependencies(missing)
    check("C2 ★★★ 缺必需编剧 -> F-POSITION-MISSING",
          not bad["ok"] and any(PO.F_POSITION_MISSING in e for e in bad["errors"]),
          str(bad["errors"]))
    optional = [x for x in all_ids if x != "gaffer"]
    ok_opt = PO.validate_dependencies(optional)
    check("C3 ★★ 去可选灯光 -> 通过", ok_opt["ok"], str(ok_opt["errors"]))
    bad_unknown = PO.validate_dependencies(all_ids + ["不存在的职业"])
    check("C4 ★★ 未知职业 -> 报错",
          any("未知职业" in e for e in bad_unknown["errors"]), str(bad_unknown["errors"]))
    # 2026-11-18（B1 全职业插件化）：插件专属职位不进默认调度口径
    from little_crew.plugin import get_registry, load_plugin
    from little_crew.agent.roster import positions as _positions
    load_plugin("little_crew.plugins.series_showrunner")
    try:
        plugin_counts = (len(_positions()), len(PO.order_positions()))
    finally:
        get_registry().unload("series_showrunner")
    check("C5 ★★★ 插件职位不挤占默认顺序（positions=20 时 order 仍 19）",
          plugin_counts == (20, 19), str(plugin_counts))

    print(); print("D. 环 / 描述")
    cyc = [_Spec("A", produces=["x"], requires=["y"]),
           _Spec("B", produces=["y"], requires=["x"])]
    try:
        PO.order_positions(cyc)
        cyc_ok = False
    except ValueError as e:
        cyc_ok = PO.F_ORDER in str(e)
    check("D1 ★★★ 产物互相依赖成环 -> F-ORDER-CYCLE", cyc_ok, "")
    d = PO.describe()
    check("D2 ★★ describe 含职位数/边数/必需职业",
          d["positions"] == 19 and d["edges"] >= 8
          and "screenwriter" in d["mandatory"], str(d))
    check("D3 ★★ describe.order 是拓扑序",
          len(d["order"]) == 19 and d["order"].index("screenwriter")
          < d["order"].index("director"), "")
    check("D4 ★★★ describe 标 source=registry（默认名册来自插件注册表）",
          d.get("source") == "registry", str(d.get("source")))
    d2 = PO.describe(list(DEFAULT_ROSTER))
    check("D5 ★★ 显式传 roster -> source=explicit（既有键全部保留）",
          d2.get("source") == "explicit" and d2["positions"] == 19
          and {"positions", "edges", "mandatory", "order"} <= set(d2), str(d2.get("source")))
    from little_crew.runtime.crew_profile import MANDATORY as _MAND
    check("D6 ★★ 必需性口径与编制同源（describe.mandatory = crew_profile.MANDATORY）",
          d["mandatory"] == list(_MAND), str(d["mandatory"]))

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
