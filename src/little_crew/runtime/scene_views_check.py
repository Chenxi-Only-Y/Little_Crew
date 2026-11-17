"""`runtime/scene_views.py` 自检（2026-11-17）。

两档 + 上游决策：
- Tier1：1 张空景锚点；Tier2：4 视图（基准+反向+45°+正俯）
- 档位由导演计划（role/priority/allowLong）+ 该场镜数 + 复用信号决定
- 生成失败不阻断；正俯只作空间草图（不作为后续 img2img 参考）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _png(path: Path, color=(80, 80, 80)) -> dict:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (128, 80), color).save(path)
    return {"ok": True, "out": str(path), "seconds": 0.1}


class _Shot:
    def __init__(self, sid: str, scene: str, size: str = "medium"):
        self.shot_id = sid
        self.scene_id = scene
        self.shot_size = size


CARD = {"space": "地下站台，金属立柱", "materials": ["拉丝不锈钢"],
        "props": ["长椅", "车灯"], "palette": ["冷青"], "era": "当代"}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景空间视图（Tier1/Tier2 + 上游决策）—— 自检")
    print("=" * 66)

    from PIL import Image

    from little_crew.runtime import scene_views as SV

    print(); print("A. 剧本场景出现频率 → 档位")
    _script = {"scenes": [
        {"id": "SC1", "heading": "地铁站台·夜"},
        {"id": "SC2", "heading": "走廊·夜"},
    ]}

    def _freq(script, scene, n):
        return SV.scene_frequency(script, [_Shot(f"{scene}-{i}", scene)
                                          for i in range(1, n + 1)])

    f1 = _freq(_script, "SC1", 1)["SC1"]
    check("A1 ★★★ 低频（1 次出现 + 1 镜）-> Tier1", f1["tier"] == 1, str(f1))
    f2 = _freq(_script, "SC1", 2)["SC1"]
    check("A2 ★★★ 1 次出现 + 2 镜 -> Tier2", f2["tier"] == 2, str(f2["score"]))
    f3 = _freq(_script, "SC1", 3)["SC1"]
    check("A3 ★★★ 1 次出现 + 3 镜 -> Tier2", f3["tier"] == 2, str(f3["score"]))
    f4 = _freq(_script, "SC1", 4)["SC1"]
    check("A4 ★★★ 高频（4 镜）-> Tier3", f4["tier"] == 3, str(f4["score"]))
    _rep = {"scenes": [{"id": "SC1", "heading": "站台·列车门前·夜"},
                       {"id": "SC2", "heading": "站台·空站台·夜"}]}
    f5 = _freq(_rep, "SC1", 3)["SC1"]
    check("A5 ★★★ 同一地点在剧本出现 2 次 + 3 镜 -> Tier3",
          f5["occurrences"] == 2 and f5["tier"] == 3, str(f5))
    check("A6 ★★★ 决策因素写明出现次数/镜数",
          any("出现 2 次" in x for x in f5["factors"])
          and any("3 镜" in x for x in f5["factors"]), str(f5["factors"]))
    check("A7 ★★ 地点键去掉时段（站台·列车门前·夜 -> 站台）",
          SV._location_key("站台·列车门前·夜") == "站台", "")

    print(); print("B. 提示词（确定性/无人物/正俯约束）")
    p1 = SV.build_prompts("SC1", CARD, 1)
    p2 = SV.build_prompts("SC1", CARD, 2)
    check("B1 ★★★ Tier1 只有空景锚点", set(p1) == {"base"}, str(list(p1)))
    check("B2 ★★★ Tier2 四视图齐全",
          set(p2) == {"base", "reverse", "oblique45", "topdown"}, str(list(p2)))
    check("B3 ★★★ 空景提示词明确无人物/无手",
          "没有任何人物" in p2["base"] and "没有手" in p2["base"], "")
    check("B4 ★★★ 正俯图要求正交/无透视",
          "正交" in p2["topdown"] and "无透视" in p2["topdown"], "")
    check("B5 ★★★ 同输入同提示词（确定性）",
          SV.build_prompts("SC1", CARD, 2) == p2, "")

    print(); print("C. 锚点事实")
    facts = SV.anchor_facts(CARD)
    check("C1 ★★★ 从场景卡抽 3~5 条锚点", 3 <= len(facts) <= 5, str(facts))
    check("C2 ★★ 锚点含空间/材质/道具事实",
          "地下站台，金属立柱" in facts and "拉丝不锈钢" in facts and "长椅" in facts, str(facts))

    print(); print("D. 生成（假渲染，Tier1/Tier2 张数与引用链）")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        shots = [_Shot("S1-01", "SC1"), _Shot("S1-02", "SC1")]
        plan = {"scenes": [{"sceneId": "SC1", "role": "高潮", "priority": 5}]}

        calls = {"t2i": 0, "edit": 0}

        def t2i(prompt, dst):
            calls["t2i"] += 1
            return _png(dst, (10, 30, 60))

        def edit(ref, prompt, dst):
            calls["edit"] += 1
            assert Path(ref).is_file(), "参考图必须存在"
            return _png(dst, (60, 30, 10))

        rep1 = SV.generate(scene_cards={"SC1": CARD}, shots=shots[:1],
                           director_plan={}, workdir=wd, enabled=True,
                           t2i=t2i, edit=edit)
        sc1 = rep1["scenes"]["SC1"]
        check("D1 ★★★ 单镜普通场生成 Tier1（1 张）",
              sc1["tier"] == 1 and len(sc1["images"]) == 1 and calls["t2i"] == 1, str(calls))
        check("D2 ★★ Tier1 成本 ¥0.05", abs(rep1["costCny"] - 0.05) < 1e-6, str(rep1["costCny"]))
        check("D3 ★★★ 证据含 sceneHash/anchors/决策",
              bool(sc1.get("sceneHash")) and bool(sc1.get("anchors"))
              and bool(sc1["decision"].get("factors")), "")

        calls.update(t2i=0, edit=0)
        rep2 = SV.generate(scene_cards={"SC1": CARD}, shots=shots,
                           director_plan=plan, workdir=wd, enabled=True,
                           t2i=t2i, edit=edit, tier3_enabled=False)
        sc2 = rep2["scenes"]["SC1"]
        check("D4 ★★★ 高亮场生成 Tier2（4 张：1 文生 + 3 派生）",
              sc2["tier"] == 2 and len(sc2["images"]) == 4
              and calls["t2i"] == 1 and calls["edit"] == 3, str(calls))
        check("D5 ★★ Tier2 成本 ¥0.20", abs(rep2["costCny"] - 0.20) < 1e-6, str(rep2["costCny"]))
        _derived = [r for r in sc2["images"] if r["view"] != "base"]
        check("D6 ★★★ 派生视图都以基准图为参考（含正俯）",
              all(r.get("reference") and "base" in r["reference"] for r in _derived),
              str([r.get("reference") for r in _derived]))
        check("D7 ★★ 正俯图标注只作草图（note 写明）",
              "正俯" in sc2.get("note", "") and "不回灌" in sc2.get("note", ""), "")

        print(); print("D2. 外部档位 / 人工覆盖（优先于启发式）")
        rep_ex = SV.generate(scene_cards={"SC1": CARD}, shots=shots[:1],
                             director_plan={}, workdir=wd, enabled=True,
                             t2i=t2i, edit=edit,
                             tiers={"SC1": {"tier": 2, "source": "manual",
                                            "reason": "人工指定"}})
        sc_ex = rep_ex["scenes"]["SC1"]
        check("D8 ★★★ 外部档位生效（人工 Tier2）+ 来源可核",
              sc_ex["tier"] == 2 and len(sc_ex["images"]) == 4
              and rep_ex.get("decisionSource") == "manual", str(rep_ex.get("decisionSource")))
        check("D9 ★★ 外部档位的理由进证据",
              any("人工指定" in str(x) for x in sc_ex["decision"].get("factors") or []), "")

        print(); print("D3. 系列复用（0 成本跳过生成）")
        calls.update(t2i=0, edit=0)
        reuse_assets = {}
        for _v in ("base", "reverse", "oblique45", "topdown"):
            _dst = Path(td) / "series_assets" / "SC1" / f"{_v}.png"
            _png(_dst)
            reuse_assets[_v] = str(_dst)
        rep_re = SV.generate(scene_cards={"SC1": CARD}, shots=shots[:1],
                             director_plan={}, workdir=wd, enabled=True,
                             t2i=t2i, edit=edit,
                             tiers={"SC1": {"tier": 2, "source": "series-reuse"}},
                             reuse_map={"SC1": {"reuse": True,
                                                "assets": reuse_assets,
                                                "from": "E01"}})
        sc_re = rep_re["scenes"]["SC1"]
        check("D10 ★★★ 复用命中：4 张全部复用，0 次生成调用",
              sc_re["reused"] and all(r.get("reused") for r in sc_re["images"])
              and calls["t2i"] == 0 and calls["edit"] == 0, str(calls))
        check("D11 ★★★ 复用成本 ¥0", rep_re["costCny"] == 0.0, str(rep_re["costCny"]))
        check("D12 ★★ 复用来源写进证据", sc_re.get("reusedFrom") == "E01", str(sc_re.get("reusedFrom")))

        print(); print("E. 空景 QC：检出人物 -> 重试一次（更硬约束）")
        _seq = {"v": ["yes", "no"]}

        def _check(path):
            return _seq["v"].pop(0) if _seq["v"] else "no"

        calls.update(t2i=0, edit=0)
        rep_q = SV.generate(scene_cards={"SC1": CARD}, shots=shots[:1],
                            director_plan={}, workdir=wd, enabled=True,
                            t2i=t2i, edit=edit, verify=True, checker=_check)
        rowq = rep_q["scenes"]["SC1"]["images"][0]
        check("E0a ★★★ 检出人物后重试一次（tries=2）",
              rowq.get("tries") == 2 and rowq.get("peopleDetected") == "no", str(rowq))
        check("E0b ★★ 重试图也落盘（不覆盖原图）",
              (Path(td) / "assets/scene_views/SC1/base__retry.png").is_file(), "")
        check("E0c ★★ 成本按实际尝试张数计（2 张 = ¥0.10）",
              abs(rep_q["costCny"] - 0.10) < 1e-6, str(rep_q["costCny"]))

        _seq2 = {"v": ["yes", "yes"]}
        rep_q2 = SV.generate(scene_cards={"SC1": CARD}, shots=shots[:1],
                             director_plan={}, workdir=wd, enabled=True,
                             t2i=t2i, edit=edit, verify=True,
                             checker=lambda _p: _seq2["v"].pop(0) if _seq2["v"] else "yes")
        check("E0d ★★★ 重试后仍有人 -> 记警告（peopleWarnings≥1）",
              rep_q2["peopleWarnings"] >= 1, str(rep_q2["peopleWarnings"]))

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        print(); print("F. 失败不阻断 / 文件落盘")
        def bad_t2i(prompt, dst):
            return {"ok": False, "error": "模拟云端失败"}

        rep3 = SV.generate(scene_cards={"SC1": CARD}, shots=shots,
                           director_plan=plan, workdir=wd, enabled=True,
                           t2i=bad_t2i, edit=edit)
        sc3 = rep3["scenes"]["SC1"]
        check("E1 ★★★ 基准图失败 -> 派生全部跳过但不抛",
              not sc3["ok"] and all(not r["ok"] or r["view"] != "base"
                                    for r in sc3["images"]), str(sc3.get("ok")))
        rep4 = SV.generate(scene_cards={"SC1": CARD}, shots=shots,
                           director_plan=plan, workdir=wd, enabled=False)
        check("E2 ★★★ 未开启 -> 空结果（不花一分钱）",
              rep4.get("enabled") is False and not rep4.get("scenes"), "")
        outp = SV.write_report(wd, rep2)
        check("E3 ★★★ 证据落 delivery/scene_views.json",
              outp.is_file() and json.loads(outp.read_text(encoding="utf-8"))["kind"]
              == "scene.views", str(outp))

    print(); print("G. Tier3 九宫格（1~2 场上限；9 张分开生成 + 程序拼板）")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        # 3 场都是"英雄场"（都要 Tier2+），看上限只提升 2 场为 Tier3
        scenes = ["SC1", "SC2", "SC3"]
        shots = []
        for sc in scenes:
            shots += [_Shot(f"{sc}-1", sc), _Shot(f"{sc}-2", sc)]
        cards = {sc: CARD for sc in scenes}
        plan = {"scenes": [{"sceneId": sc, "role": "高潮", "priority": 5}
                           for sc in scenes]}
        calls = {"t2i": 0, "edit": 0}

        def t2i(prompt, dst):
            calls["t2i"] += 1
            return _png(dst, (20, 40, 60))

        def edit(ref, prompt, dst):
            calls["edit"] += 1
            return _png(dst, (60, 40, 20))

        rep = SV.generate(scene_cards=cards, shots=shots, director_plan=plan,
                          workdir=wd, enabled=True, t2i=t2i, edit=edit,
                          tier3_enabled=True, tier3_max=2)
        tiers = {sid: rep["scenes"][sid]["tier"] for sid in scenes}
        check("G1 ★★★ Tier3 每集上限 = 2（3 场英雄场只提升 2 场）",
              sorted(tiers.values()) == [2, 3, 3], str(tiers))
        t3 = [sid for sid, v in tiers.items() if v == 3]
        g3 = rep["scenes"][t3[0]]
        check("G2 ★★★ Tier3 每场 9 张图（front_wide 即基准，不额外多一张）",
              g3["tier"] == 3 and len(g3["images"]) == 9 and g3["ok"], str(len(g3["images"])))
        check("G3 ★★★ Tier3 成本 9 张 ¥0.45（本测 2 场 Tier3 + 1 场 Tier2 = ¥1.10）",
              abs(rep["costCny"] - (9 * 0.05 * 2 + 4 * 0.05)) < 1e-6, str(rep["costCny"]))
        check("G4 ★★★ 9 张里 1 张文生（front_wide）+ 8 张派生（都引用它）",
              sum(1 for r in g3["images"] if r["view"] == "front_wide") == 1
              and all(r.get("reference") for r in g3["images"]
                      if r["view"] != "front_wide"), "")
        bd = Path(str(g3.get("board") or ""))
        check("G5 ★★★ 展示板是程序拼的（board.jpg 存在、可读）",
              bd.is_file() and Image.open(bd).size[0] > 1000, str(bd))
        check("G6 ★★ Tier3 matrix 记录 9 个机位（行/列/标签）",
              len(g3.get("matrix") or []) == 9
              and all({"view", "row", "col", "label"} <= set(x) for x in g3["matrix"]), "")
        check("G7 ★★★ 普通场（非英雄）不提升 Tier3",
              all(rep["scenes"][sid]["tier"] != 3 for sid in ("SC1", "SC2", "SC3")
                  if sid not in t3), "")

    print(); print("H. Tier3 提示词矩阵（9 机位 + 锁定 + 50mm）")
    p3 = SV.build_prompts("SC1", CARD, 3)
    check("H1 ★★★ Tier3 提示词键 = 九宫格 9 个机位",
          set(p3) == {k for k, _r, _c, _l, _d in SV.TIER3_VIEWS}, str(sorted(p3)))
    check("H2 ★★★ 每个机位都写'唯一许可：只移动机位'",
          all("只允许摄影机机位移动" in v for v in p3.values()), "")
    check("H3 ★★ 统一 50mm / 16:9",
          all("50mm" in v and "16:9" in v for v in p3.values()), "")
    check("H4 ★★★ 同输入同提示词（Tier3 确定性）",
          SV.build_prompts("SC1", CARD, 3) == p3, "")
    check("H5 ★★★ Tier3 含正交平面图（混合版），且不再有 top_wide",
          "topdown" in p3 and "top_wide" not in p3, str(sorted(p3)))
    check("H6 ★★ 平面图要求无透视/标注出入口",
          "无透视" in p3["topdown"] and "出入口" in p3["topdown"], "")
    check("H7 ★★★ Tier3 每个视图都带场景卡内容（空间/材质/道具）",
          all("地下站台" in v and "拉丝不锈钢" in v and "长椅" in v
              for v in p3.values()), "")

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
