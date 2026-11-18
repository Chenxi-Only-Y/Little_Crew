"""全职业插件化 20/20 自检（2026-11-18，B1）。

锁住七件事：
1 **迁移源码守卫**：职位声明已从 `agent/roster.py` 迁出 ——
   roster 不再有 `AgentSpec(` 字面量；`plugins/core_crew.py` 有 19 条；
   `agent/crew_spec.py` 提供结构（枚举 + AgentSpec）；roster 仍 re-export
2 **注册表**：core_crew 自动注册、positions=19、职位归属索引、默认名册标记、
   ensure 幂等；清单校验（positions 必填 agent_id / 同插件内不重复 /
   跨插件重复声明同一职位 -> 拒绝）
3 **20/20 口径**：19 核心职位（core_crew 插件）+ 1 总导演（series_showrunner
   插件），全部经插件 `positions` 声明；默认名册仍 19（showrunner 不挤占）
4 **下游从注册表构建**：pipeline_graph 默认读注册表（加载 showrunner 后 20 节点、
   节点带 plugin/inDefaultRoster）；position_order / crew_profile / console
   同样来自注册表；显式传 `DEFAULT_ROSTER` 仍是稳定 19
5 **失败/卸载语义**：卸载 core_crew 后注册表清空但 `DEFAULT_ROSTER` 快照仍 19
   且不崩；`positions()` 会幂等补注册；缺默认插件模块 -> 清晰 PluginError
6 **插件依赖保护**：有插件 requires 某能力时，提供者不可卸载

运行（cwd=仓库根，PYTHONPATH=src）：
    python -X utf8 -m little_crew.runtime.core_crew_check
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:                                        # noqa: BLE001
        return True


def _ids(specs) -> list[str]:
    return [str(getattr(s, "agent_id", "")) for s in specs]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 70)
    print("全职业插件化 20/20（B1）—— 自检")
    print("=" * 70)

    ROOT = Path(__file__).resolve().parents[3]
    SRC = ROOT / "src" / "little_crew"
    roster_src = (SRC / "agent" / "roster.py").read_text(encoding="utf-8")
    crew_spec_src = (SRC / "agent" / "crew_spec.py").read_text(encoding="utf-8")
    core_src = (SRC / "plugins" / "core_crew.py").read_text(encoding="utf-8")

    from little_crew.agent.crew_spec import (AgentSpec, Department,
                                             ExecutionKind, STAGE_ORDER, Stage)
    from little_crew.agent.protocol import Layer
    from little_crew.agent.roster import (CODE_OUTPUTS, DEFAULT_ROSTER,
                                          build_registry, positions,
                                          validate_roster)
    from little_crew.plugin import (DEFAULT_PLUGIN_MODULES, PluginError,
                                    PluginManifest, ensure_default_plugins,
                                    get_registry, load_plugin)
    from little_crew.runtime import console as CON
    from little_crew.runtime import crew_profile as CP
    from little_crew.runtime import pipeline_graph as PG
    from little_crew.runtime import position_order as PO
    from little_crew.plugins import core_crew as CORE

    reg = get_registry()
    default_ids = _ids(DEFAULT_ROSTER)

    print(); print("A. 迁移源码守卫（19 职位不再硬编码在 roster.py）")
    check("A1 ★★★ roster.py 不再出现 `AgentSpec(` 字面量（职位已迁出）",
          "AgentSpec(" not in roster_src, "")
    check("A2 ★★★ core_crew.py 有 19 条职位声明且 SPECS=19",
          core_src.count("AgentSpec(") == 19 and len(CORE.SPECS) == 19
          and isinstance(CORE.SPECS, tuple), f"{core_src.count('AgentSpec(')}")
    check("A3 ★★★ crew_spec.py 提供结构（Department/Stage/ExecutionKind/"
          "STAGE_ORDER/AgentSpec）",
          all(x in crew_spec_src for x in (
              "class Department", "class Stage", "class ExecutionKind",
              "STAGE_ORDER", "class AgentSpec")), "")
    import little_crew.agent.crew_spec as CS
    import little_crew.agent.roster as RS
    check("A4 ★★★ roster 仍 re-export 5 个公开名字（向后兼容 17 处导入）",
          all(getattr(RS, n) is getattr(CS, n) for n in
              ("Department", "Stage", "ExecutionKind", "STAGE_ORDER",
               "AgentSpec")), "")
    director = next(s for s in DEFAULT_ROSTER if s.agent_id == "director")
    mf = director.manifest()
    check("A5 ★★ AgentSpec 行为不变（layer/is_generative/manifest/as_dict）",
          director.layer is Layer.DECISION and director.is_generative
          and mf.agent_id == "director" and mf.trigger_stage == "script"
          and director.as_dict()["agentId"] == "director"
          and director.as_dict()["needsGpu"] is False, "")
    check("A6 ★★ STAGE_ORDER 顺序不变（script→asset→storyboard→render→assemble）",
          [s.value for s in STAGE_ORDER]
          == ["script", "asset", "storyboard", "render", "assemble"], "")
    check("A7 ★★★ core_crew PLUGIN 声明正确（default_roster/provides=crew.roster）",
          CORE.PLUGIN.name == "core_crew"
          and CORE.PLUGIN.default_roster is True
          and CORE.PLUGIN.optional is False
          and len(CORE.PLUGIN.provides.get("crew.roster", lambda: ())()) == 19,
          str(CORE.PLUGIN.name))
    mand = tuple(s.agent_id for s in DEFAULT_ROSTER
                 if getattr(s, "mandatory", False))
    check("A8 ★★ 必需职位由 spec.mandatory 声明（仅编剧/分镜）",
          mand == ("screenwriter", "storyboard_artist"), str(mand))

    print(); print("B. 注册表：core_crew 自动注册 + 职位索引")
    m = reg.get("core_crew")
    check("B1 ★★★ 导入名册即自动注册 core_crew（ensure_default_plugins）",
          isinstance(m, PluginManifest) and m.name == "core_crew"
          and "core_crew" in reg.names(), str(reg.names()))
    check("B2 ★★★ 注册表 positions()=19（全部来自插件声明）",
          len(reg.positions()) == 19, str(len(reg.positions())))
    check("B3 ★★★ 职位归属可查（position_owner）",
          reg.position_owner("director") == "core_crew"
          and reg.position_owner("review_board") == "core_crew"
          and reg.position_owner("no_such_position") == "", "")
    check("B4 ★★★ default_roster()=19 且与 DEFAULT_ROSTER 完全一致",
          _ids(reg.default_roster()) == default_ids
          and len(reg.default_roster()) == 19, "")
    check("B5 ★★★ DEFAULT_ROSTER 由注册表默认名册构建（19，顺序一致）",
          len(DEFAULT_ROSTER) == 19
          and _ids(reg.default_roster()) == default_ids, "")
    first = ensure_default_plugins()
    check("B6 ★★ ensure_default_plugins 幂等（再调用不改注册表）",
          len(first) == 1 and first[0] is reg.get("core_crew")
          and len(reg.positions()) == 19 and reg.names() == ["core_crew"], "")
    check("B7 ★★★ 清单校验：provides 与 positions 都空 -> 拒绝",
          _raises(lambda: PluginManifest(name="empty", provides={})), "")
    pos_only = PluginManifest(name="pos_only_ok", provides={},
                              positions=(SimpleNamespace(agent_id="p1"),))
    check("B8 ★★★ 只有 positions 没有 provides 的清单合法（职位声明即能力）",
          len(pos_only.positions) == 1, "")
    check("B9 ★★★ positions 条目缺非空 agent_id -> 拒绝",
          _raises(lambda: PluginManifest(
              name="no_agent_id", positions=(SimpleNamespace(title="x"),))), "")
    check("B10 ★★★ 同一清单内 agent_id 重复 -> 拒绝",
          _raises(lambda: PluginManifest(
              name="dup_inside",
              positions=(SimpleNamespace(agent_id="same"),
                         SimpleNamespace(agent_id="same")))), "")
    dup_a = PluginManifest(
        name="dup_plugin_a", provides={"fake.dup.a": lambda: 1},
        positions=(SimpleNamespace(agent_id="dup_position"),))
    dup_b = PluginManifest(
        name="dup_plugin_b", provides={"fake.dup.b": lambda: 1},
        positions=(SimpleNamespace(agent_id="dup_position"),))
    reg.register(dup_a)
    try:
        cross = _raises(lambda: reg.register(dup_b))
    finally:
        reg.unload("dup_plugin_a")
    check("B11 ★★★ 跨插件重复声明同一职位 agent_id -> 注册拒绝",
          cross and reg.get("dup_plugin_b") is None
          and "dup_position" not in _ids(reg.positions()), "")
    check("B12 ★★★ 默认插件模块清单指向 core_crew（DEFAULT_PLUGIN_MODULES）",
          DEFAULT_PLUGIN_MODULES == ("little_crew.plugins.core_crew",),
          str(DEFAULT_PLUGIN_MODULES))

    print(); print("C. 20/20 口径：19 核心 + 1 总导演（全部插件 positions）")
    load_plugin("little_crew.plugins.series_showrunner")
    all_pos = reg.positions()
    check("C1 ★★★ 加载 showrunner 后注册表 positions=20", len(all_pos) == 20,
          str(len(all_pos)))
    check("C2 ★★★ 第 20 位是 showrunner 且归属 series_showrunner",
          "showrunner" in _ids(all_pos)
          and reg.position_owner("showrunner") == "series_showrunner", "")
    check("C3 ★★★ 默认名册仍 19（showrunner 不挤占默认单集名册）",
          len(reg.default_roster()) == 19 and len(DEFAULT_ROSTER) == 19
          and "showrunner" not in default_ids, str(len(reg.default_roster())))
    sr = next(s for s in all_pos if s.agent_id == "showrunner")
    check("C4 ★★★ showrunner 职位声明字段正确（总导演/DIRECTOR/SCRIPT/TEXT_LLM）",
          sr.crew_title == "总导演" and sr.crew_title_en == "Showrunner"
          and sr.department is Department.DIRECTOR
          and sr.stage is Stage.SCRIPT
          and sr.execution is ExecutionKind.TEXT_LLM
          and sr.capabilities == ("series.plan",)
          and sr.produces == ("chapter.plan",)
          and sr.mandatory is False, "")
    union = [p for nm in reg.names() for p in reg.get(nm).positions]
    check("C5 ★★★ 20/20 口径成立：全部职位都由插件 positions 声明",
          len(union) == 20 and _ids(union) == _ids(all_pos)
          and all(reg.position_owner(a) for a in _ids(all_pos)), "")
    core_positions = CORE.PLUGIN.positions
    check("C6 ★★★ 19 来自 core_crew + 1 来自 series_showrunner（可逐插件对账）",
          len(core_positions) == 19
          and len(reg.get("series_showrunner").positions) == 1, "")
    check("C7 ★★ 卸载 showrunner -> positions 回到 19、归属清空",
          reg.unload("series_showrunner") is True
          and len(reg.positions()) == 19
          and reg.position_owner("showrunner") == "", "")
    load_plugin("little_crew.plugins.series_showrunner")
    check("C8 ★★ 再加载 showrunner -> 重新是 20（注册表可增删）",
          len(reg.positions()) == 20, str(len(reg.positions())))
    check("C9 ★★★ DEFAULT_ROSTER 是模块级快照（加载插件不改变它）",
          len(DEFAULT_ROSTER) == 19 and "showrunner" not in _ids(DEFAULT_ROSTER),
          "")

    print(); print("D. 下游从注册表构建（画布/依赖图/编制）")
    g = PG.build_graph()
    gids = [n["id"] for n in g["nodes"] if n["type"] == "position"]
    check("D1 ★★★ build_graph() 不传 roster -> 读注册表，20 个职位节点",
          g["summary"]["positions"] == 20 and len(gids) == 20
          and "showrunner" in gids, str(g["summary"]["positions"]))
    sr_node = next(n for n in g["nodes"] if n["id"] == "showrunner")
    check("D2 ★★★ showrunner 节点带 plugin 来源且 inDefaultRoster=False",
          sr_node.get("plugin") == "series_showrunner"
          and sr_node.get("inDefaultRoster") is False, str(sr_node.get("plugin")))
    dir_node = next(n for n in g["nodes"] if n["id"] == "director")
    check("D3 ★★★ 核心职位节点 plugin=core_crew 且 inDefaultRoster=True",
          dir_node.get("plugin") == "core_crew"
          and dir_node.get("inDefaultRoster") is True, "")
    g19 = PG.build_graph(roster=DEFAULT_ROSTER)
    check("D4 ★★★ 显式传 DEFAULT_ROSTER -> 稳定 19 节点且无 showrunner",
          g19["summary"]["positions"] == 19
          and "showrunner" not in [n["id"] for n in g19["nodes"]], "")
    check("D5 ★★★ 图摘要带插件来源（plugins/fromRegistry），不破坏既有键",
          g["summary"]["plugins"] == ["core_crew", "series_showrunner"]
          and g["summary"]["fromRegistry"] is True
          and g19["summary"]["fromRegistry"] is False
          and {"positions", "active", "done", "pending", "skipped",
               "edges"} <= set(g["summary"]), json.dumps(g["summary"],
                                                         ensure_ascii=False))
    d = PO.describe()
    check("D6 ★★★ position_order.describe() 标 source=registry（不破坏既有键）",
          d.get("source") == "registry" and d["positions"] == 19
          and d["edges"] >= 8 and "screenwriter" in d["mandatory"], str(d))
    order = PO.order_positions()
    check("D7 ★★★ position_order 拓扑序 19 且顺序正确（编剧<导演<分镜）",
          len(order) == 19 and len(set(order)) == 19
          and order.index("screenwriter") < order.index("director")
          < order.index("storyboard_artist"), "")
    check("D8 ★★★ crew_profile.MANDATORY 由 spec.mandatory 派生",
          CP.MANDATORY == ("screenwriter", "storyboard_artist")
          and CP.MANDATORY == tuple(s.agent_id for s in DEFAULT_ROSTER
                                    if s.mandatory), str(CP.MANDATORY))
    full = CP.resolve(profile="full")
    check("D9 ★★★ crew_profile.resolve(full) 仍 19 位（默认名册口径）",
          full["ok"] and set(full["active"]) == set(default_ids)
          and len(full["active"]) == 19, str(len(full["active"])))
    vd = PO.validate_dependencies()
    check("D10 ★★★ position_order.validate_dependencies 全职业通过",
          vd["ok"] and not vd["errors"], str(vd["errors"]))
    cnodes, cedges = CON._graph()
    check("D11 ★★★ console._graph() 读注册表：20 节点且 showrunner 带插件来源",
          len(cnodes) == 20
          and any(n["id"] == "showrunner"
                  and n.get("plugin") == "series_showrunner" for n in cnodes)
          and any(n["id"] == "director"
                  and n.get("plugin") == "core_crew" for n in cnodes), "")
    with tempfile.TemporaryDirectory() as td:
        page = CON.build_console(Path(td))
        txt = page.read_text(encoding="utf-8")
        page_ok = page.is_file()
    check("D12 ★★★ 导演台页面仍可生成且如实显示 20 职位（含 showrunner）",
          page_ok and "20 个职位" in txt and "showrunner" in txt, "")

    print(); print("E. 失败/卸载语义（默认行为不崩、快照不变）")
    reg.unload("series_showrunner")
    snapshot = _ids(DEFAULT_ROSTER)
    check("E1 ★★★ 卸载 core_crew -> 注册表职位清空（原始注册表视角）",
          reg.unload("core_crew") is True and reg.positions() == []
          and reg.default_roster() == (), str(reg.names()))
    check("E2 ★★★ 此时 DEFAULT_ROSTER 快照仍 19 且校验不崩",
          len(DEFAULT_ROSTER) == 19 and _ids(DEFAULT_ROSTER) == snapshot
          and validate_roster() == [], f"{len(DEFAULT_ROSTER)}")
    check("E3 ★★★ roster.positions() 自动补注册默认插件 -> 19（幂等恢复）",
          len(positions()) == 19 and "core_crew" in reg.names(), "")
    g_restore = PG.build_graph()
    cn2, _e2 = CON._graph()
    check("E4 ★★★ 卸载后重读注册表：build_graph()/画布仍 19 且不崩",
          g_restore["summary"]["positions"] == 19 and len(cn2) == 19, "")
    check("E5 ★★ ensure_default_plugins 再调用幂等（不重复注册）",
          [x.name for x in ensure_default_plugins()] == ["core_crew"]
          and len(reg.positions()) == 19, "")
    old_modules = DEFAULT_PLUGIN_MODULES
    import little_crew.plugin as PL
    try:
        PL.DEFAULT_PLUGIN_MODULES = ("little_crew.plugins.no_such_default_plugin",)
        try:
            PL.ensure_default_plugins()
            missing_ok = False
            missing_msg = ""
        except PluginError as e:                            # noqa: PERF203
            missing_ok = True
            missing_msg = str(e)
    finally:
        PL.DEFAULT_PLUGIN_MODULES = old_modules
    check("E6 ★★★ 默认插件模块缺失 -> 清晰 PluginError（含模块名）",
          missing_ok and "no_such_default_plugin" in missing_msg,
          missing_msg[:80])
    fake = PluginManifest(
        name="fake_needs_roster", provides={"fake.need": lambda: 1},
        requires=("crew.roster",))
    fake_registered = True
    try:
        reg.register(fake)
    except PluginError:
        fake_registered = False
    try:
        # 有插件 requires 核心能力 crew.roster -> 提供者不可卸载
        protected = _raises(lambda: reg.unload("core_crew"))
    finally:
        reg.unload("fake_needs_roster")
    check("E7 ★★★ 有插件 requires crew.roster -> core_crew 卸载被拒绝（依赖保护）",
          fake_registered and protected
          and reg.get("core_crew") is not None, "")
    check("E8 ★★ 依赖保护解除后 core_crew 仍可卸载（不留死锁）",
          reg.unload("core_crew") is True, "")
    check("E9 ★★★ 最后 ensure 恢复默认 19（收尾状态干净）",
          len(ensure_default_plugins()) == 1 and len(positions()) == 19
          and len(DEFAULT_ROSTER) == 19, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 70)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
