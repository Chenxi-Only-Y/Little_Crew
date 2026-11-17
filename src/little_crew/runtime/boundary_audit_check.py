"""`runtime/boundary_audit.py` 自检（2026-11-17，0.6.0）。

R1 边界审：契约线（机器）+ 画面线（VLM）；红项只重拍边界镜；无帧/异常转黄项人工。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _contract(place="站台", chars=("少女",), threads=("等谁",)):
    return {"placeKey": place, "time": "夜", "characters": list(chars),
            "props": ["手机"], "openThreads": list(threads)}


def _png(path: Path, color=(30, 30, 30)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), color).save(path)
    return path


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("边界审（契约 + VLM）—— 自检")
    print("=" * 66)

    from little_crew.runtime import boundary_audit as BA

    print(); print("A. 契约线")
    a = BA.audit_contract(_contract(), _contract())
    check("A1 ★★★ 契约一致 -> green", a["level"] == "green" and a["ok"], str(a))
    bad = _contract(chars=["少女", "旅人"], threads=("等谁", "他是谁"))
    b = BA.audit_contract(bad, _contract(chars=["少女"], threads=()))
    check("A2 ★★★ exit 人物/悬念消失 -> red",
          b["level"] == "red" and any("旅人" in x for x in b["diffs"]), str(b))
    c = BA.audit_contract(_contract(place="站台"), _contract(place="医院"))
    check("A3 ★★★ 地点不一致 -> red", c["level"] == "red", str(c))

    print(); print("B. 画面线（fake judge）")
    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        prev = _png(wd / "prev.png", (10, 20, 30))
        cur = _png(wd / "cur.png", (10, 20, 30))
        nf = BA.audit_visual(None, [str(cur)], judge=lambda p: "{}")
        check("B1 ★★ 无上一章帧 -> yellow（转人工）", nf["level"] == "yellow", str(nf))
        okj = lambda p: json.dumps({"sameSpace": True, "sameCostume": True,
                                    "sameLighting": True, "diff": ""})
        v1 = BA.audit_visual([str(prev)], [str(cur)], judge=okj,
                             composite=wd / "cmp.jpg")
        check("B2 ★★★ VLM 三项全 true -> green", v1["level"] == "green", str(v1))
        check("B3 ★★★ 对比图真的生成了（左上一章/右本章）",
              Path(str(v1["evidence"])).is_file()
              and Path(str(v1["evidence"])).stat().st_size > 1000, str(v1.get("evidence")))
        badj = lambda p: json.dumps({"sameSpace": False, "sameCostume": True,
                                     "sameLighting": True, "diff": "换空间了"})
        v2 = BA.audit_visual([str(prev)], [str(cur)], judge=badj,
                             composite=wd / "cmp2.jpg")
        check("B4 ★★★ 空间不一致 -> red（只重拍边界镜）",
              v2["level"] == "red" and "空间不一致" in v2["diffs"], str(v2))
        j2 = lambda p: "这不是 JSON"
        v3 = BA.audit_visual([str(prev)], [str(cur)], judge=j2,
                             composite=wd / "cmp3.jpg")
        check("B5 ★★ VLM 输出不可解析 -> yellow（转人工）",
              v3["level"] == "yellow", str(v3))
        def boom(p):
            raise RuntimeError("vlm down")
        v4 = BA.audit_visual([str(prev)], [str(cur)], judge=boom,
                             composite=wd / "cmp4.jpg")
        check("B6 ★★ VLM 异常 -> yellow，不阻断", v4["level"] == "yellow", "")

        print(); print("C. 合并边界审 / 重拍范围")
        ok2 = BA.audit_chapter(_contract(), _contract(), [str(prev)], [str(cur)],
                               judge=okj, composite=wd / "cmp5.jpg")
        check("C1 ★★★ 契约+画面全绿 -> ok", ok2["level"] == "green" and ok2["ok"], str(ok2["level"]))
        red = BA.audit_chapter(bad, _contract(chars=["少女"], threads=()),
                               [str(prev)], [str(cur)], judge=okj,
                               composite=wd / "cmp6.jpg")
        check("C2 ★★★ 任一红 -> red，重拍范围=只边界镜",
              red["level"] == "red" and red["retakeScope"] == "boundary_shots_only", str(red))
        yel = BA.audit_chapter(_contract(), _contract(), None, [str(cur)],
                               judge=okj, composite=wd / "cmp7.jpg")
        check("C3 ★★ 画面缺帧 -> yellow（人来判）", yel["level"] == "yellow", str(yel["level"]))
        check("C4 ★★ 边界审证据含契约/画面两段",
              "contract" in ok2 and "visual" in ok2, "")
        check("C5 ★★ 红项给出可定位 diffs", isinstance(red["contract"]["diffs"], list)
              and isinstance(red["visual"]["diffs"], list), "")

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
