"""角色设定表的**拼接**与**切片**。

## 两条相反的路线，各有一个致命短板（都实测过）

**路线 1：一次生成 16:9**（`SHEET_TEMPLATE`）
  · 优点：四个视角在**同一张图**里生成 → 一致性天然保证。
  · 短板：一张图塞不下"大脸特写 + 三套精细服装" → 模型**随机丢配饰**。
    实例：Y2K 少女的"灰百褶裙粉内衬、银色珠链、蕾丝及膝袜、粉白厚底鞋"
    在 16:9 单图里全被简化成"白蕾丝短裙 + 光腿高跟鞋"。

**路线 2：N 格独立生成 → 拼接**（本模块）
  · 优点：每格吃满 768×1024 + 完整提示词 → **配饰全保住**。
  · 短板：四张各自生成 → **脸/服装会漂**（实测第 3 格与左边大图明显不是同一张脸）。

## 所以正解是**混合**（用户 2026-10-07 选定）

    ① 一次生成 16:9 的四格设定表     ← 一致性天然保证（同一张图）
    ② 按固定版式切出 4 格             ← 版式是我们定的，切割位置可精确计算
    ③ 每格单独放大                    ← 补回细节分辨率

`slice_sheet_panels()` 做第 ② 步。

## 下游不受影响（重点）

    生成 N 格高清图  ──┬──→ 存成 N 个文件      ← 给 IP-Adapter（纯净、高清）
                       └──→ 拼成 16:9 sheet   ← 给人看（好看、一眼看全）

拼图是**给人看的展示层**，不参与锚定，所以不存在"多视角污染 CLIP 特征"的问题。
（"污染"那条只对**一次生成设定表直接当参考图**成立：CLIP 把整图编成**一个**向量。）

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
    face_path: str | Path | None = None,
    width: int = 1820,
    height: int | None = None,
    bg: tuple[int, int, int] = BG_COLOR,
) -> Path:
    """把各视角图拼成一张 16:9 设定表。

    参数：
        view_paths: {"front": 路径, "side": ..., "back": ...}
                    **顺序即出图顺序**；缺哪个就少一格（不报错）。
        out_path:   输出 PNG 路径
        face_from:  从哪个视角**裁**面部特写（默认正面）。
                    仅当没给 `face_path` 时用。
        face_path:  专用的面部特写图（如 `B_face.png`）。
                    **给了就用它**，不再从全身图裁 —— 专用图取景更准。
        width:      成品宽度，默认 1820（16:9 → 高 1024）

    返回：生成的文件路径

    设计取舍：
      · 脸特写优先用**专用图**；没有才从全身图裁。
        两者都保证"与全身是同一角色"是**生成端**的责任（同一 appearance 描述）——
        所以模板里必须明写"三个视图必须是完全同一角色"。
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
    # 优先用**专用脸图**（取景更准）；没有才从全身图裁。
    if face_path and Path(face_path).exists():
        with Image.open(face_path) as im:
            face = im.convert("RGB")
    else:
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


SHEET_PANEL_KEYS = ("face", "front", "side", "back")


def slice_sheet_panels(
    sheet_path: str | Path,
    out_dir: str | Path,
    *,
    face_ratio: float = FACE_WIDTH_RATIO,
    upscale: float = 1.0,
    prefix: str = "panel",
    trim: float = 0.0,
) -> dict[str, Path]:
    """把一张**一次生成**的 16:9 设定表按固定版式切成 4 格。

    这是"混合路线"的第 ② 步：

        ① 一次生成 16:9（一致性由"同一张图"保证）
        ② **本函数**：按版式切出 脸 / 正 / 侧 / 背 四格
        ③ 各格单独放大（`upscale`）

    参数：
        sheet_path: 设定表 PNG
        out_dir:    切片输出目录
        face_ratio: 脸栏占宽比例（须与生成时提示词里说的一致）
        upscale:    放大倍数（1.0 = 不放大）。放大是插值，**不会长出新细节**，
                    但便于单独查看/交付。
        prefix:     输出文件名前缀
        trim:       每边额外内缩的比例（0~0.1）。模型实测**不完全遵守**版式，
                    偶尔人物会压到格边，内缩一点可以避开相邻格的串入。

    返回：`{"face": Path, "front": Path, "side": Path, "back": Path}`

    注意：切割位置是**按我们给的版式算的**，不是检测出来的。所以生成时必须用
    `SHEET_TEMPLATE`（它把脸栏写成"约四分之一宽"）。
    """
    sheet_path = Path(sheet_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with Image.open(sheet_path) as im:
        sheet = im.convert("RGB")
    W, H = sheet.size

    face_w = int(W * face_ratio)
    body_w = (W - face_w) // 3

    def _trim(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if trim <= 0:
            return box
        x0, y0, x1, y1 = box
        dx = int((x1 - x0) * trim)
        dy = int((y1 - y0) * trim)
        return (x0 + dx, y0 + dy, x1 - dx, y1 - dy)

    boxes = {
        "face": _trim((0, 0, face_w, H)),
        "front": _trim((face_w, 0, face_w + body_w, H)),
        "side": _trim((face_w + body_w, 0, face_w + body_w * 2, H)),
        "back": _trim((face_w + body_w * 2, 0, W, H)),
    }

    out: dict[str, Path] = {}
    for key, box in boxes.items():
        panel = sheet.crop(box)
        if upscale and upscale != 1.0:
            nw = max(1, int(panel.width * upscale))
            nh = max(1, int(panel.height * upscale))
            panel = panel.resize((nw, nh), Image.LANCZOS)
        p = out_dir / f"{prefix}_{key}.png"
        panel.save(p, "PNG", optimize=True)
        out[key] = p
    return out


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
