"""角色设定表拼接：把**已经生成好的**独立视角图拼成 16:9 展示图。

## 为什么是"拼接"而不是"一次生成 16:9"

| 做法 | 每个视角在成品里 |
|---|---|
| 一次生成 16:9 | 模型**直接画小**了 → 天生 ~200px，蕾丝/配饰细节**根本不存在** |
| N 张独立生成 → 拼接 | 每张**先按 768×1024 画足**，再缩放进格子 → 每个 ~455×607 |

关键区别：一次生成是把信息**压缩**掉；拼接是把已经画足的高清图**排在一起**。

## 下游不受影响（这是重点）

    生成 N 张独立高清图  ──┬──→ 存成 N 个文件      ← 给 IP-Adapter（纯净、高清）
                           └──→ 拼成 16:9 sheet   ← 给人看（好看、一眼看全）

拼图是**给人看的展示层**，不参与锚定，所以不存在"多视角污染 CLIP 特征"的问题。

## 布局（用户给的参考图就是这个形制）

    ┌──────────┬────────┬────────┬────────┐
    │          │        │        │        │
    │  面部特写 │  正面  │  侧面  │  背面  │
    │（自动裁） │ 全身   │ 全身   │ 全身   │
    │          │        │        │        │
    └──────────┴────────┴────────┴────────┘
      约 1/3 宽         各约 2/9 宽

脸特写是**从正面全身图裁出来的**（不是另生成一张）——
这样脸一定和全身是**同一个人**，不会出现"特写与全身长得不一样"。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image

#: 设定表目标宽高比（16:9 横幅）
SHEET_ASPECT = 16 / 9

#: 面部特写占整幅宽度的比例（用户参考图约 1/3）
FACE_WIDTH_RATIO = 0.34

#: 面板之间的留白（像素，按最终宽度比例算）
GAP_RATIO = 0.008

#: 背景色（浅灰偏白影棚背景，与出图背景呼应）
BG_COLOR = (238, 238, 240)

#: 每个全身视角的宽度相等，故整幅宽 = 脸宽 + N×身宽 + 间隙


def _face_crop(img: Image.Image) -> Image.Image:
    """从全身图裁出「面部特写」。

    取上部的正方形区域（头肩），而不是居中裁 —— 角色立绘的头在顶部，
    居中裁会把肚子放大成"脸"。

    竖直取景：头顶留一点余量，往下取到头肩（约图高的 42%）。
    """
    w, h = img.size
    # 头肩区域：从 2% 高度开始，取 42% 高
    top = int(h * 0.02)
    box_h = int(h * 0.42)
    # 宽度取与高度同宽（正方形），水平居中
    side = min(box_h, w)
    left = (w - side) // 2
    return img.crop((left, top, left + side, top + box_h if box_h <= side else top + side))


def _fit(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩放使图完整装进 box，不裁剪（letterbox）。"""
    iw, ih = img.size
    scale = min(box_w / iw, box_h / ih)
    nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
    return img.resize((nw, nh), Image.LANCZOS)


def _contain(img: Image.Image, box_w: int, box_h: int) -> tuple[Image.Image, int, int]:
    """等比缩放并返回居中粘贴的偏移。"""
    r = _fit(img, box_w, box_h)
    return r, (box_w - r.width) // 2, (box_h - r.height) // 2


def compose_character_sheet(
    view_paths: dict[str, str | Path],
    out_path: str | Path,
    *,
    face_from: str = "front",
    width: int = 1820,
    height: int | None = None,
    bg: tuple[int, int, int] = BG_COLOR,
) -> Path:
    """把各视角图拼成一张 16:9 设定表。

    参数：
        view_paths: {"front": 路径, "side": ..., "back": ...}
                    **顺序即出图顺序**；缺哪个就少一格（不报错）。
        out_path:   输出 PNG 路径
        face_from:  用哪个视角裁面部特写（默认正面）
        width:      成品宽度，默认 1820（16:9 → 高 1024）

    返回：生成的文件路径

    设计取舍：
      · 脸特写**从已有全身图裁**（不另生成）→ 保证与全身是同一人；
      · 其余视角等比缩放居中（letterbox）→ 不裁剪人物，不拉伸变形；
      · 找不到的视角直接跳过 → 部分失败时仍能出图。
    """
    out_path = Path(out_path)
    h = height or int(round(width / SHEET_ASPECT))

    # 只保留真实存在的文件，且保持调用方给的顺序
    items = [(k, Path(v)) for k, v in view_paths.items() if v and Path(v).exists()]
    if not items:
        raise ValueError("没有任何可用的视角图，无法拼接")

    gap = max(2, int(width * GAP_RATIO))
    face_w = int(width * FACE_WIDTH_RATIO)
    rest_w = width - face_w - gap * (len(items) + 1)
    body_w = rest_w // len(items)

    sheet = Image.new("RGB", (width, h), bg)

    # --- 左侧：面部特写 ---
    face_src = dict(items).get(face_from) or items[0][1]
    with Image.open(face_src) as im:
        face = _face_crop(im.convert("RGB"))
    fr, fx, fy = _contain(face, face_w, h - gap * 2)
    sheet.paste(fr, (gap + fx, gap + fy))

    # --- 右侧：各全身视角 ---
    x = gap + face_w + gap
    for _key, p in items:
        with Image.open(p) as im:
            body = im.convert("RGB")
        br, bx, by = _contain(body, body_w, h - gap * 2)
        sheet.paste(br, (x + bx, gap + by))
        x += body_w + gap

    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, "PNG", optimize=True)
    return out_path


if __name__ == "__main__":  # pragma: no cover - 手动试用
    import sys

    base = Path(
        r"data\outputs\54589943bc69\流程试跑\资产\character_designer\real2-1791365801"
    )
    name = sys.argv[1] if len(sys.argv) > 1 else "橘小福"
    compose_character_sheet(
        {
            "front": base / f"char_{name}_front.png",
            "side": base / f"char_{name}_three_quarter.png",
            "back": base / f"char_{name}_half_body_expression.png",
        },
        Path("data/_verify") / f"sheet_{name}.png",
    )
    print(f"  已生成 data/_verify/sheet_{name}.png")
