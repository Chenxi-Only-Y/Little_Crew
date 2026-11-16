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

    from little_crew.runtime import scene_views as SV

    print(); print("A. 上游决策（谁该用四视图）")
    t1 = SV.choose_tier(shots_in_scene=1)
    check("A1 ★★★ 单镜普通场 -> Tier1", t1["tier"] == 1, str(t1))
    t2 = SV.choose_tier(shots_in_scene=2)
    check("A2 ★★ 两镜（仅多机位）仍 Tier1", t2["tier"] == 1, str(t2))
    t3 = SV.choose_tier(shots_in_scene=2, director_scene={"role": "高潮"})
    check("A3 ★★★ 导演标高亮角色 -> Tier2", t3["tier"] == 2, str(t3))
    t4 = SV.choose_tier(shots_in_scene=1, director_scene={"priority": 5})
    check("A4 ★★ 单镜+仅高优先级 -> 仍 Tier1（不为一镜铺四视图）",
          t4["tier"] == 1, str(t4))
    t4b = SV.choose_tier(shots_in_scene=2, director_scene={"priority": 5})
    check("A4b ★★★ 多机位+高优先级 -> Tier2", t4b["tier"] == 2, str(t4b))
    t5 = SV.choose_tier(shots_in_scene=2, recurring=True)
    check("A5 ★★★ 跨场复用 -> Tier2", t5["tier"] == 2, str(t5))
    check("A6 ★★★ 决策依据可复核（factors 非空）",
          bool(t5["factors"]) and any("复用" in f for f in t5["factors"]), str(t5["factors"]))

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
                           t2i=t2i, edit=edit)
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
