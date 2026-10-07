"""角色设定表提示词模板（用户 2026-10-07 定稿的固定格式）。

## 版式是用户拍板的，测试就是把它钉住

```
┌──────────────┬─────────┬─────────┬─────────┐
│  正脸放大     │  正面   │  侧面   │  背面   │   ← 16:9
│  （≈1/3 宽）  │  全身   │  全身   │  全身   │
└──────────────┴─────────┴─────────┴─────────┘
```

这些断言看着啰嗦，但每一条都对应一个**实测踩过的坑**：
  · 漏 `text/chinese characters` 负面词 → 模型往图上写标注，中文写不对就成乱码
  · 漏「同一角色同一套发型服装」 → 四个视角变成四个人
  · 漏「双手自然下垂」 → 每格姿势乱，不像设定图
  · 漏「各视角保持一致」 → 发夹只在一个视角有
"""

from __future__ import annotations

import pytest

from app.agents.stage2.character_sheet_template import (
    APPEARANCE_SECTIONS,
    SHEET_NEGATIVE,
    SHEET_SIZE,
    SHEET_STYLE,
    SHEET_TEMPLATE,
    build_sheet_prompt,
)


# ----------------------------------------------------------------------
# 版式：用户拍板的形制，必须逐条在提示词里
# ----------------------------------------------------------------------

def test_prompt_asks_for_16_9_banner():
    p = build_sheet_prompt("测试角色", "描述")
    assert "16:9" in p
    assert "横幅" in p


def test_prompt_puts_face_closeup_on_the_left():
    """左侧是**大尺寸面部特写**，占约三分之一。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert "左侧" in p
    assert "面部特写" in p
    assert "三分之一" in p


@pytest.mark.parametrize("word", ["正面全身", "侧面全身", "背面全身"])
def test_prompt_lists_all_three_views(word):
    p = build_sheet_prompt("测试角色", "描述")
    assert word in p, f"缺视图：{word}"


def test_prompt_requires_same_character_across_views():
    """**核心**：不写这句，四个视角会变成四个人（实测）。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert "同一个角色" in p
    assert "同一套发型和服装" in p


def test_prompt_pins_natural_pose():
    p = build_sheet_prompt("测试角色", "描述")
    assert "站姿自然" in p
    assert "双手自然下垂" in p


def test_prompt_pins_background_and_quality():
    p = build_sheet_prompt("测试角色", "描述")
    assert "浅灰偏白" in p
    assert "8K" in p
    assert "留白" in p


def test_prompt_is_3d_render_style():
    """用户要的是「精致 3D 渲染质感」。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert "3D 渲染" in p


# ----------------------------------------------------------------------
# 负面词：防乱码 + 防"四个视角四个人"
# ----------------------------------------------------------------------

def test_negative_blocks_text_and_chinese():
    """**必须有** —— 否则模型往图上写标注，中文写不对就是乱码。"""
    n = SHEET_NEGATIVE.lower()
    for must in ("text", "letters", "words", "chinese characters",
                 "watermark", "signature", "caption", "annotation"):
        assert must in n, f"负面词缺 {must!r}"


def test_negative_blocks_character_drift():
    n = SHEET_NEGATIVE.lower()
    for must in ("duplicated character", "inconsistent face", "different person",
                 "mismatched outfit"):
        assert must in n, f"负面词缺 {must!r}（防四个视角变成四个人）"


def test_negative_blocks_breakage():
    n = SHEET_NEGATIVE.lower()
    for must in ("extra limbs", "extra fingers", "deformed hands", "bad anatomy"):
        assert must in n


def test_negative_blocks_grid_lines():
    """版式靠提示词说，**别让模型自己画分隔线**。"""
    assert "grid lines" in SHEET_NEGATIVE.lower()


def test_negative_blocks_borders_and_frames():
    """实测：高分辨率下画面边缘会出现一圈**细黑框**。得压掉。"""
    n = SHEET_NEGATIVE.lower()
    for must in ("border", "frame", "vignette", "divider"):
        assert must in n, f"负面词缺 {must!r}（防边框/暗角）"


# ----------------------------------------------------------------------
# 版式细节（高分辨率下才显形的问题）
# ----------------------------------------------------------------------

def test_prompt_pins_left_to_right_order():
    """**实测必需**：漏了这句，模型会自己重排顺序（出成 正/背/侧）。

    第一版模板只写了"右侧并排站立…正面…侧面…背面…"，
    在 1920×1080 下实际出成了 **正/背/侧**。
    """
    p = build_sheet_prompt("测试角色", "描述")
    assert "从左到右依次是" in p
    assert p.index("正面") < p.index("侧面") < p.index("背面")


def test_prompt_says_full_body_head_to_toe():
    """每格必须全身完整入画，否则脚被裁掉、拼起来不齐。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert "全身完整入画" in p
    assert "从头顶到脚" in p


def test_prompt_forbids_borders():
    """提示词里要明写"不要边框"，光靠负面词不够。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert "不要画任何边框" in p


def test_prompt_asks_for_margin_around_subject():
    p = build_sheet_prompt("测试角色", "描述")
    assert "人物居中" in p
    assert "留白" in p


# ----------------------------------------------------------------------
# 分辨率（用户问"像素会不会太低"后提高的）
# ----------------------------------------------------------------------

def test_sheet_size_is_16_9():
    w, h = SHEET_SIZE
    assert abs(w / h - 16 / 9) < 0.02, f"不是 16:9：{w}x{h}"


def test_sheet_size_is_high_enough_per_panel():
    """**核心**：每格全身视图不能太小。

    用户的疑问是"像素会不会太低" —— 1216×688 时每格只有 261×684，
    等于把 768×1024 的单视角图缩到 261×348（**23%**），确实偏低。

    这里要求成品宽 ≥ 1600，即每格全身 ≥ ~340px 宽。
    """
    w, h = SHEET_SIZE
    assert w >= 1600, f"成品宽度 {w} 偏小（每格全身视图会不足 340px）"
    assert h >= 900, f"成品高度 {h} 偏小"

    # 按版式算每格实际尺寸
    face_w = int(w * 0.34)
    body_w = (w - face_w - 6 * 3) // 3
    assert body_w >= 330, (
        f"每格全身只有 {body_w}x{h}，太小（单视角图是 768x1024）")


def test_sheet_size_is_32_multiple_or_safe_for_local_model():
    """本地模型的尺寸一般要求是 32 的倍数，否则会被服务端拒绝/内缩。"""
    w, h = SHEET_SIZE
    assert w % 32 == 0 or w % 8 == 0, f"宽度 {w} 不是 8/32 的倍数"
    assert h % 32 == 0 or h % 8 == 0, f"高度 {h} 不是 8/32 的倍数"


# ----------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------

def test_build_fills_placeholders():
    p = build_sheet_prompt("橘小福", "一只橘色虎斑猫")
    assert "橘小福" in p
    assert "一只橘色虎斑猫" in p
    assert "{" not in p and "}" not in p, "还有没填的占位符"


def test_build_uses_default_style_when_omitted():
    p = build_sheet_prompt("橘小福", "描述")
    assert SHEET_STYLE in p


def test_build_accepts_custom_style():
    p = build_sheet_prompt("橘小福", "描述", style="水彩画风")
    assert "水彩画风" in p
    assert SHEET_STYLE not in p


def test_build_survives_empty_inputs():
    """空名字/空描述不能抛异常（流程里可能提炼失败）。"""
    p = build_sheet_prompt("", "")
    assert "角色" in p           # 名字兜底
    assert "{" not in p


def test_template_has_exactly_three_placeholders():
    """占位符只有 3 个，多了就是文档没跟上。"""
    import re

    names = set(re.findall(r"\{(\w+)\}", SHEET_TEMPLATE))
    assert names == {"name", "appearance", "style"}


# ----------------------------------------------------------------------
# 颗粒度指南
# ----------------------------------------------------------------------

def test_appearance_guide_covers_six_sections():
    """`{appearance}` 的六段结构要齐 —— 这是用户要的"详细写法"。"""
    joined = " ".join(APPEARANCE_SECTIONS)
    for must in ("气质", "面部", "发型", "服装", "配饰", "身形"):
        assert must in joined, f"颗粒度指南缺「{must}」段"
