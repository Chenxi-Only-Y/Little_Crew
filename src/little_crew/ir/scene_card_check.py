"""场景卡 → 提示词 自检（2026-11-15）。

锁住五件事：
1 **归一化**：`materials` 支持列表/顿号/逗号；字段顺序固定
2 **严格校验**：`era` 必须在封闭集内，非法值报错（不静默）
3 **确定性**：同一张卡 → 逐字相同的片段（跨镜一致的前提）
4 **双语**：中文原生模型得中文片段
5 **空卡语义**：无卡 → 空串（不凭空造场景）

> IR 桥接的检查放在 `agent/ir_bridge_check.py`（ir 层不能依赖 agent 层）。
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("场景卡 → 提示词（scene_card）—— 自检")
    print("=" * 66)

    from little_crew.ir import scene_card as SC

    card = {"space": "地下站台，金属立柱，纵深透视",
            "materials": ["拉丝不锈钢", "湿沥青"],
            "props": ["长椅", "列车车灯"],
            "palette": ["冷青", "品红霓虹"],
            "era": "当代"}

    print(); print("A. 归一化")
    c = SC.canonical_scene_card(card)
    check("A1 ★★★ 字段顺序固定（space→materials→props→palette→era）",
          list(c.keys()) == ["space", "materials", "props", "palette", "era"],
          str(list(c.keys())))
    c2 = SC.canonical_scene_card({"materials": "钢、湿沥青", "props": "长椅,车灯"})
    check("A2 ★★★ 字符串材质/道具可转列表（顿号/逗号都认）",
          c2.get("materials") == ["钢", "湿沥青"] and c2.get("props") == ["长椅", "车灯"],
          str(c2))
    check("A3 ★★ 未知字段被忽略（美术 bible 可能带更多信息）",
          SC.canonical_scene_card({"space": "站台", "light": "顶灯"}) == {"space": "站台"},
          "")
    check("A4 ★★ 空白卡 -> {}（不塞空壳）",
          SC.canonical_scene_card({"space": "", "materials": []}) == {}, "")

    print(); print("B. 严格校验")
    try:
        SC.canonical_scene_card({"era": "赛博"})
        bad = False
    except ValueError as e:
        bad = "赛博" in str(e) and "可用" in str(e)
    check("B1 ★★★ 非法 era -> ValueError 并列出封闭集", bad, "")

    print(); print("C. 确定性与双语")
    p_zh1 = SC.scene_prompt(card, "zh")
    p_zh2 = SC.scene_prompt(dict(reversed(list(card.items()))), "zh")
    check("C1 ★★★ 同卡不同键序 -> 逐字相同（跨镜一致）", p_zh1 == p_zh2, p_zh1[:80])
    check("C2 ★★★ 中文片段含场景/材质/道具/色板/年代",
          all(k in p_zh1 for k in ("场景：", "材质：", "道具：", "色板：", "年代：")),
          p_zh1[:120])
    p_en = SC.scene_prompt(card, "en")
    check("C3 ★★ 英文片段用英文标签",
          "scene:" in p_en and "materials:" in p_en and "道具：" not in p_en,
          p_en[:90])
    check("C4 ★★ 片段条数一致（中英同一张卡）",
          len(SC.scene_terms(card, "zh")) == len(SC.scene_terms(card, "en")) == 5, "")

    print(); print("D. 指纹与空值")
    f1 = SC.scene_fingerprint(card)
    f2 = SC.scene_fingerprint(dict(card, props=["列车车灯", "长椅"]))
    check("D1 ★★★ 指纹稳定且可进证据", len(f1) == 12 and f1 == SC.scene_fingerprint(card), f1)
    check("D2 ★★★ 道具顺序变了 -> 指纹变（顺序是内容）", f1 != f2, f"{f1} vs {f2}")
    check("D3 ★★★ 空卡 -> 空片段/空指纹", SC.scene_prompt(None) == ""
          and SC.scene_fingerprint(None) == "", "")

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
