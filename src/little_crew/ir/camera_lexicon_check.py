"""相机参数 → 提示词 自检（2026-11-15）。

锁住六件事：
1 **归一化**：`85`/`85mm`、`F1.4`/`f/1.4`、`70`/`70mm_film` 都归一
2 **严格校验**：未知字段/取值 → `ValueError`（不静默忽略）
3 **确定性**：字段顺序固定、字典键序无关、zh/en 各自稳定
4 **双语**：中文模型得到中文片段，英文模型得到英文片段（不混）
5 **空值语义**：无参数 → 空串（不凭空加词）
6（IR 桥接的检查放在 `agent/ir_bridge_check.py` —— ir 层不能依赖 agent 层）
"""
from __future__ import annotations

import sys

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("相机参数 → 提示词（camera_lexicon）—— 自检")
    print("=" * 66)

    from little_crew.ir import camera_lexicon as CL

    print(); print("A. 归一化与别名")
    c1 = CL.canonical_camera({"focal": "85", "aperture": "F1.4",
                              "body": "70mm_film"})
    check("A1 ★★★ 数值/大小写/别名都归一",
          c1 == {"focal": "85mm", "aperture": "f1.4", "body": "70mm"}, str(c1))
    c2 = CL.canonical_camera({"focal": "35mm", "angle": "low_angle"})
    check("A2 ★★★ 常规取值原样保留", c2 == {"focal": "35mm", "angle": "low"}, str(c2))
    check("A3 ★★★ 空值被跳过（不报错也不加词）",
          CL.canonical_camera({"focal": "", "aperture": None}) == {}, "")

    print(); print("B. 严格校验（不静默）")
    try:
        CL.canonical_camera({"focal": "500mm"})
        bad_val = False
    except ValueError as e:
        bad_val = "500mm" in str(e) and "可用" in str(e)
    check("B1 ★★★ 未知取值 -> ValueError 且列出可用值", bad_val, "")
    try:
        CL.canonical_camera({"shutter": "1/48"})
        bad_field = False
    except ValueError as e:
        bad_field = "shutter" in str(e)
    check("B2 ★★★ 未知字段 -> ValueError", bad_field, "")

    print(); print("C. 确定性与顺序")
    cam_a = {"body": "70mm", "focal": "85mm", "aperture": "f1.4"}
    cam_b = {"focal": "85mm", "aperture": "f1.4", "body": "70mm"}
    check("C1 ★★★ 字典键序无关 -> 同一串",
          CL.camera_prompt(cam_a) == CL.camera_prompt(cam_b),
          CL.camera_prompt(cam_a)[:80])
    p_en = CL.camera_prompt(cam_a)
    check("C2 ★★★ 字段顺序固定（focal -> aperture -> body）",
          p_en.index("85mm") < p_en.index("f/1.4") < p_en.index("70mm film"),
          p_en[:100])
    check("C3 ★★ 指纹稳定（可进证据）",
          CL.camera_fingerprint(cam_a) == CL.camera_fingerprint(cam_b)
          and "85mm" in CL.camera_fingerprint(cam_a), CL.camera_fingerprint(cam_a))

    print(); print("D. 双语")
    p_zh = CL.camera_prompt(cam_a, "zh")
    check("D1 ★★★ 中文片段是中文（不混英文句子）",
          "85mm 人像镜头" in p_zh and "portrait lens" not in p_zh, p_zh[:80])
    check("D2 ★★★ 英文片段是英文",
          "85mm portrait lens" in p_en and "人像" not in p_en, p_en[:80])
    check("D3 ★★ 中英片段条数一致",
          len(CL.camera_terms(cam_a, "zh")) == len(CL.camera_terms(cam_a, "en")) == 3, "")
    check("D4 ★★ 无效语言按英文处理（不抛）",
          CL.camera_prompt(cam_a, "fr").startswith("85mm"), "")

    print(); print("E. 空值语义")
    check("E1 ★★★ 无相机参数 -> 空串", CL.camera_prompt(None) == ""
          and CL.camera_prompt({}) == "", "")
    check("E2 ★★ 空参数指纹为空串（不是 '{}'）",
          CL.camera_fingerprint(None) == "" and CL.camera_fingerprint({}) == "", "")

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
