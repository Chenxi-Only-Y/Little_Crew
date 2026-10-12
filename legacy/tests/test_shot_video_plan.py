"""逐镜视频生成的规划：时长 + 首尾帧衔接（用户 2026-10-08 定的语义）。

## 语义（都是用户逐条拍板的，别再改）

| 问题 | 定论 |
|---|---|
| 时长从哪来 | 台词实测（TTS 实长）+ LLM 估动作复杂度 |
| 短镜 | **逐镜生成**，不合并（实测服务收任意 num_frames） |
| 总长 | 不卡 |
| **衔接依据** | **按分镜**：只有 `transition == "连续"` 才衔接 |
| **连续镜首帧** | **用上一镜尾帧** |
| **不连续镜首帧** | **用本镜分镜图** |
| 上一镜尾帧拿不到 | 降级成链头（用分镜图）+ 标注 |

## 这个文件守两类东西

**一、"按分镜决定"必须真的可解析。**
原先分镜提示词写 `"transition": "转场方式"`（自由文本、无取值约束），
LLM 可能输出任何字。拿它判衔接等于没判 —— 所以要有受控取值 + 归一 + 别名兜底。

**二、"不衔接"是常态，不是异常。**
换场景/换构图/淡入淡出本来就该断开。所以链头不需要特殊处理；
要处理的是**什么时候衔接**，以及**接不上时怎么降级**。
"""

from __future__ import annotations

import pytest

from app.services.shot_video_plan import (
    CONTINUOUS,
    DEFAULT_TRANSITION,
    TRANSITION_VALUES,
    normalize_transition,
    plan_video_segments,
    summarize_plan,
    wants_chaining,
)


# ----------------------------------------------------------------------
# transition 归一
# ----------------------------------------------------------------------

@pytest.mark.parametrize("raw,expect", [
    ("连续", CONTINUOUS),
    ("切", "切"),
    ("淡入", "淡入"),
    ("淡出", "淡出"),
    ("叠化", "叠化"),
    # 别名/同义写法要救回来
    ("接上一镜", CONTINUOUS),
    ("延续", CONTINUOUS),
    ("同机位", CONTINUOUS),
    ("hard cut", "切"),
    ("硬切", "切"),
    ("dissolve", "叠化"),
    # 认不出 → 不衔接（衔接会篡改首帧，拿不准就别做）
    ("随便什么", DEFAULT_TRANSITION),
    ("", DEFAULT_TRANSITION),
    (None, DEFAULT_TRANSITION),
    # LLM 可能把字段说明原样吐出来 —— 必须当成"不认识"
    ("转场方式", DEFAULT_TRANSITION),
])
def test_normalize_transition(raw, expect):
    assert normalize_transition(raw) == expect


def test_only_continuous_triggers_chaining():
    """**核心**：受控取值里只有「连续」触发衔接。"""
    for v in TRANSITION_VALUES:
        shot = {"shot_id": "S1", "transition": v}
        assert wants_chaining(shot) is (v == CONTINUOUS), f"{v} 的判定不对"


def test_unknown_value_does_not_chain():
    """猜不出意图时**不衔接** —— 宁可多一个链头，也不要错接。"""
    assert wants_chaining({"transition": "转场方式"}) is False
    assert wants_chaining({}) is False


# ----------------------------------------------------------------------
# 规划：链头 / 衔接 / 降级
# ----------------------------------------------------------------------

def _shots(*specs):
    """specs: (shot_id, transition, duration)"""
    return [
        {"shot_id": s, "scene_id": s.split("-")[0], "transition": t,
         "duration_s": d, "description": ""}
        for s, t, d in specs
    ]


def _images(*ids):
    return [{"shot_id": i, "local_path": f"/tmp/{i}.png"} for i in ids]


def test_first_shot_is_always_a_chain_head():
    """第一镜没有上一镜 → 必定链头（用分镜图）。"""
    plan = plan_video_segments(_shots(("S1-01", "连续", 4.0)), _images("S1-01"))
    assert plan[0]["chained"] is False
    assert plan[0]["first_frame_src"] == "storyboard"
    assert "第一镜" in plan[0]["note"]


def test_continuous_shot_chains_from_previous():
    """`连续` → 首帧取上一镜尾帧。"""
    shots = _shots(("S1-01", "切", 4.0), ("S1-02", "连续", 3.0))
    plan = plan_video_segments(shots, _images("S1-01", "S1-02"))
    assert plan[0]["chained"] is False
    assert plan[1]["chained"] is True
    assert plan[1]["first_frame_src"] == "prev_last"
    assert plan[1]["chain_from"] == "S1-01"


@pytest.mark.parametrize("trans", ["切", "淡入", "淡出", "叠化"])
def test_non_continuous_shot_uses_its_own_image(trans):
    """**核心**：不连续 → 用本镜自己的分镜图（不是上一镜尾帧）。"""
    shots = _shots(("S1-01", "切", 4.0), ("S1-02", trans, 3.0))
    plan = plan_video_segments(shots, _images("S1-01", "S1-02"))
    assert plan[1]["chained"] is False
    assert plan[1]["first_frame_src"] == "storyboard"
    assert plan[1]["chain_from"] == ""


def test_scene_change_with_continuous_is_still_honored():
    """衔接**只看 transition**（用户："按照分镜决定"）——
    分镜说连续就连续，不拿 scene_id 二次否决。"""
    shots = _shots(("S1-01", "切", 4.0), ("S2-01", "连续", 3.0))
    plan = plan_video_segments(shots, _images("S1-01", "S2-01"))
    assert plan[1]["chained"] is True


def test_degrades_when_previous_shot_failed():
    """上一镜生成失败（没有图）→ 本镜降级成链头，并**标注原因**。"""
    shots = _shots(("S1-01", "切", 4.0), ("S1-02", "连续", 3.0))
    # S1-01 没有产出图（失败）
    plan = plan_video_segments(shots, _images("S1-02"))
    assert plan[1]["chained"] is False
    assert plan[1]["first_frame_src"] == "storyboard"
    assert "生成失败" in plan[1]["note"]
    assert plan[1]["chain_from"] == ""


def test_degrades_when_previous_shot_is_itself_a_head():
    """上一镜自己就是链头且没产出图 → 接不上 → 降级（不能臆造尾帧）。"""
    shots = _shots(("S1-01", "连续", 4.0), ("S1-02", "连续", 3.0))
    # 只有 S1-02 有图；S1-01 既接不上也没图
    plan = plan_video_segments(shots, _images("S1-02"))
    assert plan[0]["chained"] is False
    assert plan[1]["chained"] is False
    assert "未生成" in plan[0]["note"] or "缺分镜图" in plan[0]["note"]


def test_missing_own_image_is_flagged():
    """本镜没有分镜图且接不上 → 标注"缺分镜图"（调用方据此跳过）。"""
    shots = _shots(("S1-01", "切", 4.0))
    plan = plan_video_segments(shots, [])
    assert plan[0]["has_own_image"] is False
    assert "缺分镜图" in plan[0]["note"]


# ----------------------------------------------------------------------
# 时长
# ----------------------------------------------------------------------

def test_target_frames_from_duration():
    """目标帧数 = 时长 × fps（由调用方再量化到档位）。"""
    shots = _shots(("S1-01", "切", 4.0), ("S1-02", "切", 2.5))
    plan = plan_video_segments(shots, _images("S1-01", "S1-02"), fps=24)
    assert plan[0]["target_seconds"] == 4.0
    assert plan[0]["target_frames"] == 96
    assert plan[1]["target_seconds"] == 2.5
    assert plan[1]["target_frames"] == 60


def test_duration_falls_back_to_duration_key():
    """`duration` 也能认（不同来源字段名不一致）。"""
    shots = [{"shot_id": "S1-01", "transition": "切", "duration": 3.5}]
    plan = plan_video_segments(shots, _images("S1-01"))
    assert plan[0]["target_seconds"] == 3.5


def test_missing_duration_gets_a_sane_default():
    """没有时长也不给 0（0 会让生成请求非法）。"""
    from app.services.shot_video_plan import DEFAULT_SHOT_SECONDS

    shots = [{"shot_id": "S1-01", "transition": "切"}]
    plan = plan_video_segments(shots, _images("S1-01"))
    assert plan[0]["target_seconds"] == DEFAULT_SHOT_SECONDS
    assert plan[0]["target_frames"] > 0


def test_bad_duration_value_is_ignored():
    for bad in ("", "abc", 0, -3, None):
        shots = [{"shot_id": "S1-01", "transition": "切", "duration_s": bad}]
        plan = plan_video_segments(shots, _images("S1-01"))
        assert plan[0]["target_seconds"] > 0, f"{bad!r} 没被兜底"


# ----------------------------------------------------------------------
# 边界与摘要
# ----------------------------------------------------------------------

def test_empty_storyboard_is_safe():
    assert plan_video_segments([], []) == []
    assert plan_video_segments(None, None) == []


def test_storyboard_entries_without_shot_id_are_skipped():
    shots = [{"transition": "切", "duration_s": 4.0}, {"shot_id": "S1-01"}]
    plan = plan_video_segments(shots, _images("S1-01"))
    assert len(plan) == 1 and plan[0]["shot_id"] == "S1-01"


def test_summary_counts():
    shots = _shots(
        ("S1-01", "切", 4.0),      # 链头
        ("S1-02", "连续", 3.0),    # 衔接
        ("S2-01", "切", 4.0),      # 链头
        ("S2-02", "连续", 3.0),    # 衔接
    )
    plan = plan_video_segments(shots, _images("S1-01", "S1-02", "S2-01", "S2-02"))
    s = summarize_plan(plan)
    assert s["shots"] == 4
    assert s["chained"] == 2
    assert s["degraded"] == 0
    assert s["total_seconds"] == 14.0


def test_summary_reports_degradation():
    shots = _shots(("S1-01", "切", 4.0), ("S1-02", "连续", 3.0))
    plan = plan_video_segments(shots, _images("S1-02"))   # S1-01 没图
    s = summarize_plan(plan)
    assert s["degraded"] >= 1
