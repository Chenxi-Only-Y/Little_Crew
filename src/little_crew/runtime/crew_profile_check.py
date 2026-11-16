"""剧组编制自检（2026-11-16）：**职业可选，但不许偷偷降质**。

锁住六件事：
1 **谁能选/谁不能选**：必需核心（编剧/分镜）不可移除；商用必须带合规官
2 **依赖闭包**：选了职位但缺前置产物 -> 自动补齐并记录 `autoAdded`
3 **三预设 + 自定义**：minimal/standard/full/custom 的成员与验证级别
4 **跳过 ≠ 通过**：缺席审片/质检/合规 -> `unverified` + `level != full`
5 **应用**：`apply_to_stubs` 真的裁掉未启用职位；功能开关联动
6 稳定指纹：同编制同 hash，不同编制不同 hash
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("剧组编制（crew profile）—— 自检")
    print("=" * 68)

    from little_crew.runtime import crew_profile as CP
    from little_crew.agent.roster import DEFAULT_ROSTER

    all_ids = {a.agent_id for a in DEFAULT_ROSTER}

    print(); print("A. 谁能选 / 谁不能选")
    m = CP.resolve(profile="minimal")
    check("A1 ★★★ minimal 保留必需核心（编剧+分镜）",
          set(CP.MANDATORY) <= set(m["active"]), str(m["active"]))
    try:
        bad = CP.resolve(profile="custom", add=["gaffer"],
                         remove=["screenwriter"])
        removed_core = (not bad["ok"]) and any("必需核心" in e for e in bad["errors"])
    except Exception:                                         # noqa: BLE001
        removed_core = False
    check("A2 ★★★ 移除必需核心 -> 拒绝并说明", removed_core, "")
    r_com = CP.resolve(profile="minimal", commercial=True)
    check("A3 ★★★ 商用模式自动带上合规官",
          "compliance_officer" in r_com["active"]
          and "compliance_officer" in r_com["autoAdded"], str(r_com["autoAdded"]))
    r_bad = CP.resolve(profile="custom", add=["compliance_officer"],
                       remove=["compliance_officer"], commercial=True)
    check("A4 ★★★ 商用模式手删合规官 -> 拒绝",
          (not r_bad["ok"]) and any("合规官" in e for e in r_bad["errors"]), "")

    print(); print("B. 依赖闭包")
    r_g = CP.resolve(profile="custom", add=["gaffer"])
    check("B1 ★★★ 选灯光师 -> 自动补齐场景设计师",
          "scene_designer" in r_g["autoAdded"]
          and "scene_designer" in r_g["active"], str(r_g["autoAdded"]))
    r_lp = CP.resolve(profile="custom", add=["line_producer"])
    check("B2 ★★★ 选制片主任 -> 自动补齐分镜师（已有）",
          "storyboard_artist" in r_lp["active"], "")
    r_pv = CP.resolve(profile="custom", add=["previs"])
    check("B3 ★★ 选白模预演师 -> 自动补齐编剧/分镜",
          {"screenwriter", "storyboard_artist"} <= set(r_pv["active"]), "")
    r_min = CP.resolve(profile="minimal")
    check("B4 ★★★ minimal 的闭包补齐（角色设计/场景设计）也记录在 autoAdded",
          {"character_designer", "scene_designer"} <= set(r_min["autoAdded"]),
          str(r_min["autoAdded"]))

    print(); print("C. 三预设 + 自定义")
    f = CP.resolve(profile="full")
    check("C1 ★★★ full = 全部 19 位且 level=full",
          set(f["active"]) == all_ids and f["level"] == "full"
          and not f["unverified"], f"{len(f['active'])}/{f['level']}")
    s = CP.resolve(profile="standard")
    check("C2 ★★★ standard 带审片与场记（验证级别 reduced，因为缺合规）",
          {"review_board", "continuity_supervisor"} <= set(s["active"])
          and s["level"] == "reduced", s["level"])
    check("C3 ★★★ minimal 明确是 draft 级别（未审/未质检/未合规）",
          m["level"] == "draft"
          and {"review_board", "continuity_supervisor",
               "compliance_officer"} <= set(m["unverified"]), str(m["unverified"]))
    c1 = CP.resolve(profile="custom", add=["director"])
    check("C4 ★★ custom = 核心 + 指定职位 + 闭包",
          "director" in c1["active"] and set(CP.MANDATORY) <= set(c1["active"]),
          str(c1["active"]))

    print(); print("D. 跳过 ≠ 通过（验证口径）")
    check("D1 ★★★ 未验证项逐一列出（审片/质检/合规）",
          set(m["unverified"]) == {"review_board", "continuity_supervisor",
                                   "compliance_officer"}, str(m["unverified"]))
    check("D2 ★★★ 缺席质量位 -> verification 全 False",
          all(v is False for v in m["verification"].values()), str(m["verification"]))
    sm = CP.summary(m)
    check("D3 ★★★ 报告摘要带 level / unverified / hash",
          sm.get("level") == "draft" and sm.get("unverified")
          and len(str(sm.get("hash") or "")) == 12, str(sm))

    print(); print("E. 应用（裁掉未启用）")
    stubs = {"screenwriter": object(), "storyboard_artist": object(),
             "compliance_officer": object(), "gaffer": object()}
    rm = CP.apply_to_stubs(stubs, m)
    check("E1 ★★★ apply_to_stubs 真的移除未启用职位",
          "gaffer" in rm["removed"]
          and "compliance_officer" in rm["removed"]
          and "gaffer" not in stubs, str(rm["removed"]))
    check("E2 ★★★ 功能开关与编制联动（未选场记 -> vision 功能关）",
          CP.feature_allowed(m, "continuity_supervisor") is False
          and CP.feature_allowed(f, "continuity_supervisor") is True, "")

    print(); print("F. 稳定性")
    check("F1 ★★★ 同编制同 hash",
          CP.resolve(profile="standard")["hash"] == s["hash"], "")
    check("F2 ★★★ 不同编制不同 hash",
          m["hash"] != s["hash"] != f["hash"], "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
