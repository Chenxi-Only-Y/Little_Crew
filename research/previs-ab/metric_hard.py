"""P0-2 的**硬指标**：不靠人眼，也不靠像素差。

## 为什么弃用像素差

0.35 那组像素差还有 80%，但画面已经"对了"。
**像素差测的是"图变了多少"，不是"变对了没有"。**

## 用哪两个量

**① 紫色占比** —— 薰衣草花田占多少画面。
这一镜的干扰是"土坡把花田压窄"，所以**紫色占比直接反映损害程度**。
`strength=1.0` 应该显著低，0.5 / 0.35 应接近 A。

**② 地平线位置（行号）** —— 天空/地面的分界在哪一行。
白模给的地平线若被放大，生成图的地平线会**显著上移**。

两个都不依赖模型、不依赖检测器，纯像素统计 —— **可复现、可复算**。

用 `colorsys` 判紫色（R/B 高、G 低），不引 numpy。
"""
from __future__ import annotations

import colorsys
import sys
from pathlib import Path

__all__ = ["purple_ratio", "horizon_row", "measure"]

#: 判"紫"的门限：色相落在 [0.7, 0.87]（紫→品红），饱和度 > 0.22，亮度 > 0.10
HUE_LO, HUE_HI = 0.70, 0.87
SAT_MIN, VAL_MIN = 0.22, 0.10


def purple_ratio(img) -> float:
    """紫色像素占比（0~1）。**这是本实验最直接的损害指标。**"""
    small = img.convert("RGB").resize((160, 96))
    px = list(small.getdata())
    n = 0
    for r, g, b in px:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if HUE_LO <= h <= HUE_HI and s >= SAT_MIN and v >= VAL_MIN:
            n += 1
    return n / len(px)


def horizon_row(img) -> int:
    """地平线估计：**行平均饱和度**变化最大的那一行。

    天空/远山饱和度低，花田饱和度高 —— 分界处梯度最大。
    返回行号（0 = 顶边）。找不到明显分界时返回 -1。
    """
    small = img.convert("RGB").resize((120, 80))
    rows: list[float] = []
    for y in range(80):
        tot = 0.0
        for x in range(120):
            r, g, b = small.getpixel((x, y))
            _h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            tot += s
        rows.append(tot / 120)
    best, best_i = 0.0, -1
    # 只在中间 60% 找，避开顶/底边缘的整体过渡
    for y in range(16, 64):
        grad = abs(rows[y + 1] - rows[y - 1])
        if grad > best:
            best, best_i = grad, y
    return best_i if best > 0.02 else -1


def measure(path: Path) -> dict:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    return {
        "file": path.name,
        "purplePct": round(purple_ratio(img) * 100, 2),
        "horizonRow": horizon_row(img),
        "horizonPctOfHeight": round(horizon_row(img) / 80 * 100, 1)
        if horizon_row(img) >= 0 else None,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")                # type: ignore[attr-defined]
    base = Path(r"D:\AI\ds.8\Little_Crew\research\p0-2-control-ab")
    names = [
        ("A_no_control.png", "A 无控制"),
        ("B_with_control.png", "B strength=1.0"),
        ("B_strength_0.5.png", "B strength=0.5"),
        ("B_strength_0.35.png", "B strength=0.35"),
        ("B_with_props_0.35.png", "B 0.35 + 道具"),
    ]
    print(f"  {'组':18s} {'紫色占比':>8s} {'地平线行':>8s}")
    out = []
    for fn, label in names:
        p = base / fn
        if not p.is_file():
            print(f"  {label:18s}  （缺文件）")
            continue
        m = measure(p)
        out.append({**m, "label": label})
        print(f"  {label:18s} {m['purplePct']:7.2f}% {str(m['horizonRow']):>8s}")
    import json

    (base / "hard_metrics.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  写出:", base / "hard_metrics.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
