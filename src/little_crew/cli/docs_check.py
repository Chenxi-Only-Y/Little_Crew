"""文档图与演示素材自检（2026-11-16）。

锁住四件事：
1 **13/14 图**真的存在且是有效 PNG（不是占位/空文件），图注写的是**当前**名册口径
2 **演示素材**齐全（报告页/流水线图/回归/doctor/交付清单 + 幻灯片 + README）
3 **版本一致**：`version.py` 与 `CHANGELOG` 第一条、`docs/README` 时效一致
4 演示说明能对上 8 分钟脚本（可复现）
"""
from __future__ import annotations

import sys
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("文档图 / 演示素材 —— 自检")
    print("=" * 68)

    root = Path(__file__).resolve().parents[3]
    docs = root / "docs"
    demo = root / "research" / "demo" / "2026-11-16"
    readme = (docs / "README.md").read_text(encoding="utf-8")
    sheet = (docs / "12-答辩一页纸.md").read_text(encoding="utf-8")

    print(); print("A. 13/14 图（当前口径）")
    f13 = docs / "13-职位流程与出图链路.png"
    f14 = docs / "14-完整形态链路.png"
    check("A1 ★★★ 13 图存在且是有效 PNG（>100KB）",
          f13.is_file() and f13.stat().st_size > 100_000
          and f13.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
          str(f13.stat().st_size if f13.is_file() else 0))
    check("A2 ★★★ 14 图存在且是有效 PNG（>100KB）",
          f14.is_file() and f14.stat().st_size > 100_000
          and f14.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n",
          str(f14.stat().st_size if f14.is_file() else 0))
    check("A3 ★★★ docs/README 索引引用两张图",
          "13-职位流程与出图链路.png" in readme
          and "14-完整形态链路.png" in readme, "")
    check("A4 ★★★ 图注是当前口径（19 职位 / 三道闸）",
          "19 职位" in readme and "三道闸" in readme, "")

    print(); print("B. 演示素材")
    need = ["README.md", "01-报告页总览.jpg", "02-流水线图.jpg",
            "03-20集回归.jpg", "04-doctor.jpg", "05-交付物清单.jpg",
            "06-容器冒烟.jpg",
            "demo_slideshow.mp4"]
    missing = [n for n in need if not (demo / n).is_file()]
    check("B1 ★★★ 素材目录齐全（6 张/段 + README）", not missing, str(missing))
    small = [n for n in need if n.endswith((".jpg", ".mp4"))
             and (demo / n).is_file() and (demo / n).stat().st_size < 20_000]
    check("B2 ★★★ 截图/幻灯片不是占位文件（>20KB）", not small, str(small))
    demo_md = (demo / "README.md").read_text(encoding="utf-8") if (demo / "README.md").is_file() else ""
    check("B3 ★★★ 素材 README 能对上 8 分钟脚本", "8 分钟" in demo_md, "")
    check("B4 ★★★ 答辩一页纸引用素材清单",
          "演示素材" in sheet and "01-报告页总览" in sheet, "")

    print(); print("C. 版本一致")
    try:
        from little_crew.version import VERSION

        head = ""
        for ln in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
            if ln.startswith("## ["):
                head = ln
                break
        check("C1 ★★★ CHANGELOG 首条与 version.py 一致",
              f"## [{VERSION}]" in head, f"{head[:60]} vs {VERSION}")
        check("C2 ★★ docs/README 时效含当前版本",
              VERSION in readme, VERSION)
    except Exception as exc:                                  # noqa: BLE001
        check("C1 ★★★ CHANGELOG 首条与 version.py 一致", False, str(exc))
        check("C2 ★★ docs/README 时效含当前版本", False, str(exc))

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
