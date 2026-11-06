"""配音纯函数层自检：音色绑定 · 对齐规则 · 三项指标（§3.11）。"""
from __future__ import annotations

import sys

from little_crew.governance.voice_cast import (
    ALIGN_TOLERANCE, MAX_SPEEDUP, align, cast, describe, pick_voice,
)
from little_crew.review.voice_check import check

_r = []


def check_(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


AV = ["Microsoft Huihui Desktop", "Microsoft Huihui", "Microsoft Yaoyao",
      "Microsoft Kangkang"]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("配音：音色绑定 / 对齐规则 / 三项指标 —— 自检")
    print("=" * 66)

    print(); print("A. 音色绑定角色（确定性）")
    sheets = {"少女": {"gender": "女性", "ageBand": "少女"},
              "母亲": {"gender": "女性", "ageBand": "中年"},
              "父亲": {"gender": "男性", "ageBand": "中年"},
              "老奶奶": {"gender": "女性", "ageBand": "年长"}}
    c1 = cast(["少女", "母亲", "父亲", "老奶奶"], sheets, AV)
    c2 = cast(["老奶奶", "父亲", "母亲", "少女"], sheets, AV)   # 换顺序
    check_("A1 ★★★ 同角色同音色（与传入顺序无关）",
           c1["少女"]["voice"] == c2["少女"]["voice"]
           and c1["父亲"]["voice"] == c2["父亲"]["voice"], "")
    check_("A2 ★★ 两个女性角色分到不同音色",
           c1["少女"]["voice"] != c1["母亲"]["voice"],
           f"{c1['少女']['voice']} / {c1['母亲']['voice']}")
    check_("A3 ★★ 男性角色拿到男声",
           "Kangkang" in c1["父亲"]["voice"], c1["父亲"]["voice"])
    check_("A4 ★★★ 声音描述按年龄区分",
           c1["老奶奶"]["instruction"] != c1["少女"]["instruction"],
           f"{c1['老奶奶']['instruction'][:12]} / {c1['少女']['instruction'][:12]}")
    ov = cast(["甲"], {"甲": {"gender": "男性", "voiceDescription": "人工指定"}})
    check_("A5 ★★ 定妆表里的人工指定优先",
           ov["甲"]["instruction"] == "人工指定" and ov["甲"]["source"] == "sheet", "")
    check_("A6 描述不崩（无年龄段）",
           describe("女性", "") != "" and describe("男性", "") != "", "")

    print(); print("B. 对齐规则")
    check_("B1 容差内不动", align(5.0, 5.2)["action"] == "ok", "")
    r = align(6.0, 5.0)
    check_("B2 ★★ 小幅超出 -> 调语速（不是拉镜头）",
          r["action"] == "rate" and r["newShotSeconds"] == 5.0, str(r))
    r = align(8.0, 5.0)
    check_("B3 ★★★ 大幅超出 -> 拉镜头，不硬压语速",
          r["action"] == "extend" and r["newShotSeconds"] > 8.0, str(r))
    check_("B4 ★ 拉长的镜头覆盖音频（含 0.3s 余量）",
          r["newShotSeconds"] >= 8.0 + 0.3 - 1e-6, str(r["newShotSeconds"]))
    r = align(3.0, 5.0)
    check_("B5 音频偏短 -> 收紧，但不低于 1 秒",
          r["action"] == "trim" and r["newShotSeconds"] >= 1.0, str(r))
    check_("B6 ★★ 语速调整有上限（不许超过 MAX_SPEEDUP）",
          abs(align(5.0 * (1 + MAX_SPEEDUP * 0.5), 5.0)["rate"]) <= 10, "")
    check_("B7 零时长不崩", align(3.0, 0.0)["action"] in ("rate", "extend", "trim"),
          str(align(3.0, 0.0)))

    print(); print("C. 三项指标")
    U = [{"shotId": "a", "character": "少女", "ok": True,
          "audioSeconds": 5.2, "shotSeconds": 5.0, "instruction": "少女音"},
         {"shotId": "b", "character": "少女", "ok": True,
          "audioSeconds": 3.0, "shotSeconds": 5.0, "instruction": "少女音"},
         {"shotId": "c", "character": "父亲", "ok": False, "error": "timeout"}]
    rep = check(U)
    check_("C1 ★★ 失败率口径 = 失败句/总句",
          rep["failed"] == 1 and abs(rep["failureRate"] - 1 / 3) < 1e-3,
          str(rep["failureRate"]))
    check_("C2 ★★ 对齐误差按均值/最大统计",
          rep["align"]["maxAbs"] == 2.0 and rep["align"]["overTolerance"] == 1,
          str(rep["align"]))
    check_("C3 ★★★ 同角色换声音 -> 报不一致",
          check([{"shotId": "x", "character": "少女", "ok": True,
                  "audioSeconds": 1, "shotSeconds": 1, "instruction": "A"},
                 {"shotId": "y", "character": "少女", "ok": True,
                  "audioSeconds": 1, "shotSeconds": 1, "instruction": "B"}]
                )["voiceConsistency"]["inconsistent"] == 1, "")
    check_("C4 ★★★ 明确写出「没检查什么」",
          any("口型" in x for x in rep["notChecked"]), str(rep["notChecked"])[:40])
    check_("C5 空输入不崩", check([])["total"] == 0, "")
    check_("C6 容差与对齐规则同源", ALIGN_TOLERANCE == check([])["align"]["tolerance"], "")

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
