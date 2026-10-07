"""角色设定表拼接（`app/services/character_sheet.py`）。

## 为什么是"拼接"而不是"一次生成 16:9"

用户问"16:9 设定表不能是 4 张图拼接吗"—— **能，而且这才是对的**：

| 做法 | 每个视角在成品里 |
|---|---|
| 一次生成 16:9 | 模型**直接画小**了 → 天生 ~200px，蕾丝/配饰细节**根本不存在** |
| N 张独立生成 → 拼接 | 每张**先按 768×1024 画足**，再缩放进格子 → 每个 ~455×607 |

一次生成是把信息**压缩**掉；拼接是把已经画足的高清图**排在一起**。

## 而且拼接**不影响下游**

    生成 N 张独立高清图  ──┬──→ 存成 N 个文件      ← 给 IP-Adapter（纯净、高清）
                           └──→ 拼成 16:9 sheet   ← 给人看（好看、一眼看全）

拼图是**给人看的展示层**，不参与锚定 —— 所以不存在"多视角污染 CLIP"的问题
（那条只对"一次生成设定表"成立：CLIP 把整图编成**一个**向量，4 个视角会混）。
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.services.character_sheet import (
    SHEET_ASPECT,
    compose_character_sheet,
)


def _mk(path, size, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture()
def views(tmp_path):
    """三个竖幅视角（模拟新规范的 768×1024）。"""
    d = tmp_path / "views"
    return {
        "front": _mk(d / "front.png", (768, 1024), (200, 120, 90)),
        "side": _mk(d / "side.png", (768, 1024), (150, 150, 170)),
        "back": _mk(d / "back.png", (768, 1024), (110, 110, 130)),
    }


def test_sheet_is_16_9(views, tmp_path):
    out = compose_character_sheet(views, tmp_path / "sheet.png")
    with Image.open(out) as im:
        w, h = im.size
    assert abs(w / h - SHEET_ASPECT) < 0.02, f"不是 16:9：{w}x{h}"
    assert w == 1820, "默认宽度应为 1820"
    assert h == 1024


def test_sheet_contains_all_views(views, tmp_path):
    """每个视角都要真的贴上去 —— 抽查各栏目中心点**不是背景色**。"""
    from app.services.character_sheet import BG_COLOR

    out = compose_character_sheet(views, tmp_path / "sheet.png")
    with Image.open(out) as im:
        img = im.convert("RGB")
        w, h = img.size
        # 脸栏中心 + 三个身栏中心
        xs = [int(w * 0.17), int(w * 0.475), int(w * 0.70), int(w * 0.925)]
        for i, x in enumerate(xs):
            px = img.getpixel((x, h // 2))
            # 允许接近背景（灰 238,238,240）但不应完全等于
            assert px != BG_COLOR, f"第 {i} 栏是空的（中心点仍是背景色）"


def test_sheet_skips_missing_views(views, tmp_path):
    """少给一个视角也能出图（部分失败时别整个挂掉）。"""
    partial = {k: v for k, v in views.items() if k != "back"}
    out = compose_character_sheet(partial, tmp_path / "partial.png")
    with Image.open(out) as im:
        assert im.size[0] == 1820, "宽度仍应固定，只是少一格"


def test_sheet_skips_nonexistent_files(views, tmp_path):
    """路径不存在就跳过，不抛异常。"""
    mixed = dict(views)
    mixed["back"] = tmp_path / "nope.png"
    out = compose_character_sheet(mixed, tmp_path / "mixed.png")
    assert out.exists()


def test_sheet_raises_when_nothing_usable(tmp_path):
    with pytest.raises(ValueError):
        compose_character_sheet({"front": tmp_path / "nope.png"}, tmp_path / "x.png")


def test_face_crop_is_from_the_same_character(views, tmp_path):
    """脸特写必须**从已有全身图裁**（不另生成）→ 保证与全身是同一人。

    验证方式：正面图涂成纯色 A，另两个视角纯色 B；
    脸栏应该出现 A 而不是 B。
    """
    out = compose_character_sheet(views, tmp_path / "sheet.png", face_from="front")
    with Image.open(out) as im:
        img = im.convert("RGB")
        w, h = img.size
        face_px = img.getpixel((int(w * 0.17), h // 2))
    # 正面色 (200,120,90) 附近，而不是侧/背的灰蓝
    assert face_px[0] > face_px[2], f"脸栏不是正面图裁的：{face_px}"


def test_custom_width_keeps_aspect(views, tmp_path):
    out = compose_character_sheet(views, tmp_path / "small.png", width=910)
    with Image.open(out) as im:
        w, h = im.size
    assert w == 910
    assert abs(w / h - SHEET_ASPECT) < 0.03


def test_output_is_readable_png(views, tmp_path):
    out = compose_character_sheet(views, tmp_path / "s.png")
    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.mode in ("RGB", "RGBA")
