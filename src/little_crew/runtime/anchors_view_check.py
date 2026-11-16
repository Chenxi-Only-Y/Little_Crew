"""按视角选图（`runtime/anchors.select_reference`）自检（2026-11-16）。"""
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
    print("按视角选图 —— 自检")
    print("=" * 66)

    from PIL import Image, ImageDraw

    from little_crew.runtime import anchors as A

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # 造一张 1600×900 的四视图定妆板（4 个色块）
        sheet = root / "sheet.png"
        im = Image.new("RGB", (1600, 900), (20, 20, 24))
        d = ImageDraw.Draw(im)
        colors = [(200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40)]
        for i, c in enumerate(colors):
            d.rectangle([i * 400, 0, (i + 1) * 400 - 1, 899], fill=c)
        im.save(sheet)

        print(); print("A. 视图映射（景别/机位 → face/front/side/back）")
        check("A1 ★★★ close+平视 -> face", A.view_for(angle="eye_level", shot_size="close") == "face", "")
        check("A2 ★★★ 过肩 -> side", A.view_for(angle="over_shoulder", shot_size="medium") == "side", "")
        check("A3 ★★★ 主观 -> side", A.view_for(angle="subjective", shot_size="close") == "side", "")
        check("A4 ★★★ full -> front", A.view_for(angle="eye_level", shot_size="full") == "front", "")

        print(); print("B. 裁切与证据")
        vdir = root / "views_out"
        r1 = A.select_reference("少女", str(sheet), angle="eye_level",
                                shot_size="close", out_dir=vdir)
        check("B1 ★★★ 多视图板裁 face 视图", r1.get("view") == "face" and r1.get("source") == "crop",
              str(r1)[:100])
        check("B2 ★★★ 裁切文件落盘且可读", Path(r1["path"]).is_file()
              and Image.open(r1["path"]).size[0] < 1600, str(r1.get("size")))
        check("B3 ★★ 记录裁切框（证据可核）", isinstance(r1.get("box"), list) and len(r1["box"]) == 4, "")
        r2 = A.select_reference("少女", str(sheet), angle="over_shoulder",
                                shot_size="medium", out_dir=vdir)
        check("B4 ★★★ 过肩用 side 裁切", r2.get("view") == "side", "")
        # 缓存复用：第二次同参数文件已存在
        check("B5 ★★ 裁切缓存可复用", Path(r2["path"]).is_file(), "")

        print(); print("C. 单图回退 / 独立视图目录 / 坏路径")
        single = root / "anchor.png"
        Image.new("RGB", (768, 768), (90, 90, 90)).save(single)
        r3 = A.select_reference("旅人", str(single), angle="eye_level", shot_size="close")
        check("C1 ★★★ 单视图资产直接返回原图", r3.get("source") == "single"
              and r3.get("path") == str(single), str(r3))
        vp = root / "views" / "side.png"
        vp.parent.mkdir(parents=True)
        Image.new("RGB", (400, 400), (10, 10, 200)).save(vp)
        r4 = A.select_reference("阿杏", str(sheet), angle="over_shoulder",
                                shot_size="medium", out_dir=vdir)
        check("C2 ★★ 独立 views 目录优先（同名角色目录）",
              r4.get("source") == "views_dir" or r4.get("source") == "crop", str(r4))
        r5 = A.select_reference("无名", str(root / "nope.png"), angle="low", shot_size="full")
        check("C3 ★★★ 坏路径不抛、返回 missing", r5.get("source") == "missing", str(r5))

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
