"""`runtime/series_bible.py` 自检（2026-11-17）。

系列资产台账：稳定 sceneKey / 场景卡版本 / 档位只升不降 /
角色版本与造型 / 复用判定 / 使用记录 / 汇总。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("系列资产台账 —— 自检")
    print("=" * 66)

    from little_crew.runtime import series_bible as SB

    print(); print("A. 稳定 sceneKey 与卡哈希")
    check("A1 ★★★ 标题第一段去时段",
          SB.stable_scene_key("地铁站台·夜") == "地铁站台", SB.stable_scene_key("地铁站台·夜"))
    check("A2 ★★★ 空站台/站台 可经别名归一",
          SB.stable_scene_key("空站台·夜", aliases={"站台": "地铁站台"}) == "地铁站台",
          SB.stable_scene_key("空站台·夜", aliases={"站台": "地铁站台"}))
    check("A3 ★★ 同一卡 → 同一卡哈希（内容指纹）",
          SB.card_hash({"space": "站台", "props": ["长椅"]})
          == SB.card_hash({"space": "站台", "props": ["长椅"]}), "")
    check("A4 ★★ 卡内容变化 → 卡哈希变化",
          SB.card_hash({"space": "站台"}) != SB.card_hash({"space": "站厅"}), "")

    print(); print("B. 场景登记：版本/档位/使用")
    with tempfile.TemporaryDirectory() as td:
        data = SB.load(td)
        check("B1 ★★★ 空目录 -> 空台账", not data.get("scenes") and not data.get("cast"), "")
        e1 = SB.upsert_scene(data, scene_key="地铁站台", heading="地铁站台·夜",
                             card={"space": "站台"}, tier=1, episode_id="E01",
                             decision_source="llm", assets={"base": "/tmp/a.png"})
        check("B2 ★★★ 首次登记 version=1 / tier=1 / episodes=[E01]",
              e1["version"] == 1 and e1["tier"] == 1 and e1["episodes"] == ["E01"], str(e1))
        e2 = SB.upsert_scene(data, scene_key="地铁站台", heading="地铁站台·夜",
                             card={"space": "站台"}, tier=2, episode_id="E02",
                             decision_source="llm-series",
                             assets={"front_wide": "/tmp/b.png"})
        check("B3 ★★★ 档位只升不降（1→2）", e2["tier"] == 2, str(e2["tier"]))
        e3 = SB.upsert_scene(data, scene_key="地铁站台", card={"space": "站台"},
                             tier=1, episode_id="E03")
        check("B4 ★★★ 低档位不会把台账拉低", e3["tier"] == 2, str(e3["tier"]))
        check("B5 ★★★ 同一卡不升级版本", e3["version"] == 1, str(e3["version"]))
        e4 = SB.upsert_scene(data, scene_key="地铁站台",
                             card={"space": "站台（改）"}, tier=2, episode_id="E04")
        check("B6 ★★★ 卡内容变化 -> version+1", e4["version"] == 2, str(e4["version"]))
        check("B7 ★★ 资产路径可累积更新",
              "base" in (e4.get("assets") or {}) and "front_wide" in (e4.get("assets") or {}), "")
        check("B8 ★★ episodes 去重累积",
              e4["episodes"] == ["E01", "E02", "E03", "E04"], str(e4["episodes"]))
        check("B9 ★★ tierHistory 记录升档",
              any(h.get("tier") == 2 for h in (e4.get("tierHistory") or [])), "")

        print(); print("C. 复用判定")
        same = SB.decide_reuse(data, scene_key="地铁站台",
                               card={"space": "站台（改）"}, desired_tier=2)
        check("C1 ★★★ 同卡+同档 -> 可复用", same["reuse"] is True, str(same))
        up = SB.decide_reuse(data, scene_key="地铁站台",
                             card={"space": "站台（改）"}, desired_tier=3)
        check("C2 ★★★ 目标档更高 -> 需升档", up["reuse"] is False and up.get("upgrade") is True, str(up))
        first = SB.decide_reuse(data, scene_key="新地点", card={"space": "x"}, desired_tier=2)
        check("C3 ★★ 台账没有 -> 首次生成", first["reuse"] is False, str(first))
        changed = SB.decide_reuse(data, scene_key="地铁站台",
                                  card={"space": "完全不同"}, desired_tier=1)
        check("C4 ★★★ 卡变了 -> 版本更新（不复用旧图）",
              changed["reuse"] is False and "变化" in str(changed.get("why")), str(changed))

        print(); print("D. 角色台账")
        c1 = SB.upsert_character(data, character_id="少女", name="少女",
                                 appearance="银灰高马尾", outfit="素色长袍",
                                 episode_id="E01", anchor="/tmp/anchor.png")
        check("D1 ★★★ 角色首次登记 version=1",
              c1["version"] == 1 and c1["episodes"] == ["E01"], str(c1))
        c2 = SB.upsert_character(data, character_id="少女", appearance="银灰高马尾",
                                 outfit="深灰卫衣", episode_id="E02")
        check("D2 ★★★ 外观不变 -> 版本不变", c2["version"] == 1, str(c2["version"]))
        check("D3 ★★ 新造型进 outfits 集合",
              set(c2["outfits"]) == {"素色长袍", "深灰卫衣"}, str(c2["outfits"]))
        c3 = SB.upsert_character(data, character_id="少女", appearance="黑长直",
                                 episode_id="E03")
        check("D4 ★★★ 外观变化 -> 版本+1", c3["version"] == 2, str(c3["version"]))

        print(); print("E. 使用记录 / 汇总 / 持久化")
        SB.record_usage(data, episode_id="E02", scene_key="地铁站台", tier=2,
                        action="reused", source="llm-series")
        check("E1 ★★ usage 记录追加，场景计数 +1",
              len(data["usage"]) == 1 and e4["usage"] == 1, str(data["usage"]))
        ep = SB.upsert_episode(data, episode_id="E01", title="第一集",
                               scenes=["地铁站台"], shots=6)
        SB.upsert_episode(data, episode_id="E01", shots=8)
        check("E2 ★★ 集信息 upsert（不重复）",
              len(data["episodes"]) == 1 and data["episodes"][0]["shots"] == 8, str(data["episodes"]))
        s = SB.summary(data)
        check("E3 ★★★ 汇总含集数/场景/角色/档位分布",
              s["episodes"] == 1 and s["scenes"] == 1 and s["cast"] == 1
              and s["tiers"].get(2) == 1, str(s))
        path = SB.save(td, data)
        loaded = SB.load(td)
        check("E4 ★★★ 落盘/读回一致", path.is_file()
              and loaded["scenes"]["地铁站台"]["tier"] == 2
              and loaded["cast"]["少女"]["version"] == 2, str(path))
        check("E5 ★★ 台账 schema 版本写清", loaded.get("schema") == SB.SCHEMA, "")

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
