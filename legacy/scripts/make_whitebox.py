"""白模「外轮廓」生成原型 —— 纯 numpy + PIL。

背景（ControlNet README 实测 + 本机实测）：
    H3 的 video VAE 下采样 16x 再 2x2 patch，1280x704 只有 40x22 个 token。
    实测现有 pose 骨架脚本的帧：非黑像素仅 1.97%，下采样到 26x15 后峰值只剩 71/255。
    也就是说**细骨架线在模型能看到的分辨率下几乎被抹平**。

    → 外轮廓（有面积的粗形状）比细骨架更能穿透下采样。

本脚本产出同一动作的三种控制图，便于与 ControlNet-Union 的
pose / canny / hed 三种模式直接比对：
    silhouette_*.png   实心剪影（外轮廓填充）
    edge_*.png         轮廓线（从剪影提边缘，ISO 线风格）
    thin_*.png         原始细骨架（对照组）

只依赖 numpy + PIL。
"""

from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, r"D:\AI\local-gen")
import make_pose_seq as mp  # noqa: E402  复用其骨架定义与运动学

OUT = pathlib.Path(r"D:\AI\local-gen\outputs\whitebox")
W, H = mp.W, mp.H
FPS = mp.FPS
SECONDS = mp.SECONDS

#: 肢体半宽（像素）—— 决定剪影的"胖瘦"。
#: 太细会退回成骨架；太粗会把关节糊成一团、失去轮廓信息。
LIMB_RADIUS = {
    "torso": 26.0,   # 躯干
    "head": 30.0,    # 头
    "arm": 13.0,     # 手臂
    "leg": 16.0,     # 腿
}


def _radius_for(part: str) -> float:
    if part == "head":
        return LIMB_RADIUS["head"]
    if part == "torso":
        return LIMB_RADIUS["torso"]
    if part.startswith(("arm", "sho", "elb", "wri")):
        return LIMB_RADIUS["arm"]
    return LIMB_RADIUS["leg"]


# 骨骼名 -> 粗细类别（与 make_pose_seq 的命名对应）
_PART_OF = {
    "pelvis": "torso", "spine": "torso", "chest": "torso", "neck": "torso",
    "head": "head",
    "sho_l": "arm", "elb_l": "arm", "wri_l": "arm",
    "sho_r": "arm", "elb_r": "arm", "wri_r": "arm",
    "hip_l": "leg", "kne_l": "leg", "ank_l": "leg",
    "hip_r": "leg", "kne_r": "leg", "ank_r": "leg",
}


def draw_silhouette(pts: dict) -> Image.Image:
    """实心剪影：每根骨按粗细画粗线 + 关节画圆，形成连贯的人形块。"""
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)

    # 先画粗肢体（用圆头线：PIL 的 line 无圆头，故两端补圆）
    for child, parent in mp.BONES:
        r = LIMB_RADIUS[_PART_OF[child]]
        x1, y1 = pts[parent]
        x2, y2 = pts[child]
        d.line([x1, y1, x2, y2], fill=255, width=int(r * 2))
        for (x, y) in ((x1, y1), (x2, y2)):
            d.ellipse([x - r, y - r, x + r, y + r], fill=255)

    # 头单独加一个更大的球，让人形比例更像人
    hx, hy = pts["head"]
    hr = LIMB_RADIUS["head"]
    d.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=255)
    return img


def to_edge(sil: Image.Image, width: int = 3) -> Image.Image:
    """从剪影提外轮廓（形态学梯度：膨胀减腐蚀）。

    为什么不用 Canny：Canny 依赖梯度阈值，对纯黑白剪影只会返回边界像素，
    细且断；而形态学梯度给出**闭合、连续**的粗轮廓线，更像 canny/hed 控制图。
    """
    a = np.asarray(sil, dtype=np.uint8)
    m = a > 127
    # 用可分离的滑动最大值/最小值近似膨胀腐蚀（矩形结构元），足够用
    def dilate(x: np.ndarray, w: int) -> np.ndarray:
        out = x.copy()
        for s in range(1, w + 1):
            out[:, s:] |= x[:, :-s]
            out[:, :-s] |= x[:, s:]
            out[s:, :] |= x[:-s, :]
            out[:-s, :] |= x[s:, :]
        return out

    def erode(x: np.ndarray, w: int) -> np.ndarray:
        out = x.copy()
        for s in range(1, w + 1):
            out[:, s:] &= x[:, :-s]
            out[:, :-s] &= x[:, s:]
            out[s:, :] &= x[:-s, :]
            out[:-s, :] &= x[s:, :]
        return out

    band = dilate(m, width) & ~erode(m, width)
    return Image.fromarray((band * 255).astype(np.uint8), mode="L")


def draw_thin(pts: dict) -> Image.Image:
    """对照组：原始细骨架（与 make_pose_seq 的画法一致）。"""
    img = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(img)
    for child, parent in mp.BONES:
        d.line([*pts[parent], *pts[child]], fill=255, width=8)
    for k, (x, y) in pts.items():
        r = 10 if k in ("head", "pelvis") else 6
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return img


def coverage_after_downsample(im: Image.Image, factor: int = 32) -> tuple[float, int]:
    """下采样到 H3 的等效 token 分辨率后，还剩多少有效信号。"""
    small = im.resize((max(1, W // factor), max(1, H // factor)), Image.BILINEAR)
    a = np.asarray(small)
    return float((a > 20).mean() * 100), int(a.max())


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    total = FPS * SECONDS
    print(f"生成 {total} 帧的 剪影 / 轮廓 / 细骨架（对照）到 {OUT}")

    for i in range(total):
        t = i / total
        pos, _ = mp.forward_kinematics(mp.pose_at(t))
        pts = mp.project(pos, yaw=0.5 * math.sin(t * 2 * math.pi))
        sil = draw_silhouette(pts)
        sil.save(OUT / f"silhouette_{i:04d}.png")
        to_edge(sil, width=3).save(OUT / f"edge_{i:04d}.png")
        draw_thin(pts).save(OUT / f"thin_{i:04d}.png")

    # 拼图对比
    picks = [0, total // 6, total // 3, total // 2]
    rows = {"thin": "细骨架(对照)", "silhouette": "实心剪影", "edge": "外轮廓线"}
    cw, ch = W // 3, H // 3
    sheet = Image.new("RGB", (cw * len(picks), ch * len(rows)), (18, 18, 18))
    for r, key in enumerate(rows):
        for c, idx in enumerate(picks):
            im = Image.open(OUT / f"{key}_{idx:04d}.png").convert("RGB").resize((cw, ch))
            sheet.paste(im, (c * cw, r * ch))
    sheet_path = pathlib.Path(r"D:\AI\local-gen\outputs\whitebox_compare.png")
    sheet.save(sheet_path)

    print(f"\n对比图: {sheet_path}")
    print(f"  （3 行 = {' / '.join(rows.values())}；{len(picks)} 列 = 不同帧）")
    print("\n下采样到 H3 等效 token 分辨率（32x）后的信号强度：")
    print(f"  {'类型':<12}{'有效占比':>10}{'峰值':>8}")
    for key, label in rows.items():
        pct, peak = coverage_after_downsample(Image.open(OUT / f"{key}_0000.png"))
        print(f"  {label:<12}{pct:>9.2f}%{peak:>8}")
    print("\n  峰值越接近 255，说明该控制图越能穿透下采样被模型看到。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
