"""角色资产设计规范（2026-10-07 用户拍板）的回归测试。

## 规范内容

| 项 | 规定 |
|---|---|
| **视图** | 正面 / 3/4 侧 / 侧面，三个都**全身**（用户选"AI 参考多角度"） |
| **图内文字** | **不要**（提示词排除 + 负面词压制） |
| **尺寸** | 768×1024 竖幅 |
| **seed** | 由**角色名 + 视图**派生（固定） |
| **负面提示词** | 必给 |

## 为什么这些测试值得写

改之前有 6 个真问题，其中两个让产出**基本没法用**：

1. 提示词写 `character design sheet` —— 那在训练数据里指"带文字标注的多格设定表"，
   所以模型往图上写中文，而它画不出中文 → 满图乱码（实测："脑强恕得蛇"）。
2. "三视图"名不副实：出的是 正面 / 3/4 / **半身表情**，而半身照画幅与另两张不同，
   往参考图集里掺噪声。
"""

from __future__ import annotations

import pytest

from app.agents.stage2.character_designer import (
    _NEGATIVE_PROMPT,
    _PROMPT_TEMPLATE,
    _VIEW_ORDER,
    _VIEWS,
    _build_prompt,
    _seed_for,
)


# ----------------------------------------------------------------------
# 视图定义
# ----------------------------------------------------------------------

def test_views_are_the_three_agreed_angles():
    """就是这三个：正面 / 3/4 侧 / 侧面。不许再出现 `half_body_expression`。"""
    assert _VIEW_ORDER == ["front", "three_quarter", "side"]
    assert set(_VIEWS) == {"front", "three_quarter", "side"}


def test_all_views_are_full_body_and_same_size():
    """三个视图**画幅必须一致**，且都是全身竖幅。

    原先 `half_body_expression` 是半身 512×512，与另两张全身不一致 ——
    拿去做一致性参考会引入噪声。
    """
    sizes = {v: (w, h) for v, (_t, w, h) in _VIEWS.items()}
    assert len(set(sizes.values())) == 1, f"三个视图画幅不一致：{sizes}"
    w, h = next(iter(sizes.values()))
    assert (w, h) == (768, 1024), f"应为 768×1024 竖幅，实际 {(w, h)}"
    assert h > w, "必须是竖幅"


@pytest.mark.parametrize("view", ["front", "three_quarter", "side"])
def test_built_prompt_pins_full_body(view):
    """**拼好的提示词**里必须有全身约束（防止退化成半身像）。

    ⚠️ 我第一版断言的是"视图短语里含 full body" —— 那是错的：全身约束写在
    `_PROMPT_TEMPLATE` 里（`standing, full body in frame head to toe`），
    视图短语只负责角度。所以要对**最终 prompt** 断言。
    """
    p = _build_prompt("橘小福", "橘色虎斑猫", view).lower()
    assert "full body" in p, f"{view} 的提示词缺全身约束"
    assert "standing" in p
    # 角度也要真的体现出来
    assert _VIEWS[view][0].split(",")[0] in p


# ----------------------------------------------------------------------
# 提示词：不许出现"设定表"语义
# ----------------------------------------------------------------------

def test_prompt_does_not_ask_for_a_sheet():
    """**核心一条**：提示词里不能有 `sheet` 这类词。

    `character design sheet` / `reference sheet` 在训练数据里是"带文字标注的
    多格设定表"，会诱使模型往图上写字 —— 而它写不对中文，产出一堆乱码。
    """
    lowered = _PROMPT_TEMPLATE.lower()
    for bad in ("sheet", "turnaround", "multiple views", "grid", "collage"):
        assert bad not in lowered, f"提示词里不该出现 {bad!r}（会诱使模型画设定表/多视图）"


def test_prompt_is_explicitly_text_free():
    """要**明说**不要文字（光靠负面词不够）。"""
    assert "no text" in _PROMPT_TEMPLATE.lower()


def test_prompt_asks_for_a_single_figure():
    lowered = _PROMPT_TEMPLATE.lower()
    assert "single figure" in lowered, "要限定单个人物，否则可能画出多格/多个人"
    assert "full body" in lowered, "要限定全身"


def test_negative_prompt_suppresses_text_and_sheets():
    lowered = _NEGATIVE_PROMPT.lower()
    for must in ("text", "watermark", "multiple views", "character sheet", "grid"):
        assert must in lowered, f"负面词里缺 {must!r}"


def test_negative_prompt_suppresses_common_breakage():
    lowered = _NEGATIVE_PROMPT.lower()
    for must in ("extra limbs", "extra fingers", "deformed hands", "blurry"):
        assert must in lowered, f"负面词里缺 {must!r}"


def test_built_prompt_contains_name_appearance_and_view():
    p = _build_prompt("橘小福", "橘色虎斑猫，戴侦探帽", "front")
    assert "橘小福" in p
    assert "橘色虎斑猫" in p
    assert "front view" in p


# ----------------------------------------------------------------------
# seed：按角色名固定
# ----------------------------------------------------------------------

def test_seed_is_stable_for_same_name_and_view():
    """同名同视图 → 同 seed（重跑得到同一个人）。"""
    assert _seed_for("橘小福", "front") == _seed_for("橘小福", "front")


def test_seed_differs_by_view():
    """同一角色的不同视图要不同 seed（否则三张图会是同一张）。"""
    seeds = {_seed_for("橘小福", v) for v in _VIEW_ORDER}
    assert len(seeds) == 3, f"三个视图的 seed 撞了：{seeds}"


def test_seed_differs_by_character():
    """不同角色要不同 seed（否则不同角色长同一张脸）。"""
    assert _seed_for("橘小福", "front") != _seed_for("白团子", "front")


def test_seed_is_a_valid_diffusers_seed():
    """是 32 位非负整数（diffusers 接受的范围）。"""
    s = _seed_for("橘小福", "front")
    assert isinstance(s, int)
    assert 0 <= s < 2**32


# ----------------------------------------------------------------------
# 生成路径：上下文与命名
# ----------------------------------------------------------------------

def test_generated_files_are_named_by_view(monkeypatch, tmp_path):
    """产物命名 `char_{角色名}_{视图}.png`，且三个视图都生成。"""
    import pathlib

    from app.core.config import settings

    monkeypatch.setattr(settings, "output_dir", str(tmp_path / "outputs"))
    monkeypatch.setattr(settings, "mock_image", True)   # 本地占位，不调外网

    from app.agents.stage2.character_designer import CharacterDesigner
    from app.core.context import RunContext
    from app.services import runtime_settings as rs

    state = {
        "session_id": "sess-spec",
        "run_context": RunContext(user_id="tester", session_id="sess-spec",
                                  project="P").as_dict(),
        "story_outline": {"characters": [{"name": "橘小福", "description": "橘猫"}]},
    }
    rs.set_context("tester", "sess-spec")
    try:
        out = CharacterDesigner().run(state)
    finally:
        rs.set_context(None, None)

    refs = out.get("character_reference_images") or {}
    assert "橘小福" in refs, "没产出参考图"

    pngs = sorted((tmp_path / "outputs").rglob("*.png"))
    names = {p.name for p in pngs}
    for v in _VIEW_ORDER:
        assert f"char_橘小福_{v}.png" in names, f"缺 {v} 视图，实际 {names}"

    # 归档归属必须按 run_context 的账号/工程
    for p in pngs:
        rel = p.as_posix()
        assert "tester" in rel, f"未按账号归档：{rel}"
        assert "/P/" in rel, f"未按工程名归档：{rel}"
