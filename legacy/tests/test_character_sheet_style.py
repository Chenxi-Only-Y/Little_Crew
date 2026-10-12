"""风格一致性护栏。

## 用户问过的问题：「为什么风格不一致」

做了两组对照试验：

**实验一（同一 seed、同一角色描述，只换风格段）** → 风格**跟着风格段变**。
**实验二（同一提示词、只换 seed，4 张）** → 风格**完全稳定**，seed 只影响人物细节。

结论：**风格漂移的主因是"每次换了风格词"**，不是模型随机性。

而且查出**旧默认值自相矛盾**：

    "3D 写实"                        ← 要写实
    "85mm 人像镜头 / 8K / 大片质感"    ← 摄影词 → 拉向真人照片
    "类似 BJD 人偶的精致五官"          ← BJD 是**塑料娃娃**
    → 三个方向互相拉扯 → 模型每次随机挑一个

三版对照结果：旧默认**塑料感、腿被拉长**；"纯 CG、无摄影词"那版
皮肤有次表面散射的通透感、比例正常。

所以本文件守两件事：
  1. 风格三段（prefix / style / tail）**必须是同一套风格词**；
  2. **不许混入互斥词**（CG 词 + 摄影词 + 塑料娃娃词）。
"""

from __future__ import annotations

import pytest

from app.agents.stage2.character_sheet_template import (
    SHEET_RENDER_TAIL,
    SHEET_STYLE,
    SHEET_STYLE_PREFIX,
    build_sheet_prompt,
)

#: 一套风格只能走一个方向。这三组词**互斥**，混用会让模型每次挑不同的。
MUTUALLY_EXCLUSIVE = {
    "CG/游戏": ("3D CG", "虚幻引擎", "次表面散射", "游戏角色", "3D 渲染"),
    "摄影/真人": ("85mm", "人像镜头", "8K", "大片质感", "摄影"),
    "塑料娃娃": ("BJD", "人偶", "手办", "黏土"),
}


def _style_text() -> str:
    return f"{SHEET_STYLE_PREFIX} {SHEET_STYLE} {SHEET_RENDER_TAIL}"


def test_style_does_not_mix_exclusive_directions():
    """**核心**：风格词不能跨方向混用。

    旧默认值同时含"3D 写实"+"85mm 人像镜头"+"BJD 人偶" → 三个方向打架，
    实测出图塑料感、比例失调（腿被拉长）。
    """
    text = _style_text()
    hit = [d for d, words in MUTUALLY_EXCLUSIVE.items() if any(w in text for w in words)]
    assert len(hit) == 1, (
        f"风格词跨了 {len(hit)} 个方向：{hit}。"
        f"只能选一个方向 —— 混用会让模型每次挑不同的，表现为「风格不一致」。"
    )


def test_style_direction_is_cg():
    """当前选定方向：半写实 3D CG（实测皮肤质感与比例最好）。"""
    text = _style_text()
    assert "3D CG" in text or "虚幻引擎" in text


def test_no_photography_words():
    """摄影词会把画面拉向真人照片，与 CG 打架 —— 实测混用导致风格漂移。"""
    text = _style_text()
    for w in ("85mm", "人像镜头", "大片质感", "8K"):
        assert w not in text, f"混入了摄影词 {w!r}（会与 CG 风格打架）"


def test_no_plastic_doll_words():
    """`BJD 人偶` 会拉向塑料娃娃 —— 与"次表面散射皮肤"冲突。"""
    text = _style_text()
    for w in ("BJD", "人偶", "手办"):
        assert w not in text, f"混入了 {w!r}（会拉向塑料娃娃）"


def test_prefix_and_tail_share_the_same_direction():
    """标题前缀与尾段**必须是同一套风格词**（否则一开头一结尾互相拉扯）。"""
    assert ("3D CG" in SHEET_STYLE_PREFIX) == ("3D CG" in SHEET_RENDER_TAIL)
    assert ("半写实" in SHEET_STYLE_PREFIX) == ("半写实" in SHEET_RENDER_TAIL)


def test_no_duplicated_phrases_across_style_segments():
    """风格词不要在三段里重复 —— 重复会把权重放大、压掉别的描述。

    实测踩过：`SHEET_STYLE_PREFIX` 与 `SHEET_RENDER_TAIL` 都写"3D CG 游戏角色"。

    ⚠️ 不断言"次表面散射" —— 它是**定义该风格的核心术语**，在风格段与尾段
    各出现一次是合理的（风格段给技法名、尾段说主观感受）。
    这里只查**纯冗余的整段重复**。
    """
    segs = [SHEET_STYLE_PREFIX, SHEET_STYLE, SHEET_RENDER_TAIL]
    for phrase in ("游戏角色", "半写实 3D CG"):
        n = sum(1 for s in segs if phrase in s)
        # prefix 与 tail 都提到风格名是允许的；但"游戏角色"这种**主体词**
        # 重复会让画面变成"游戏角色角色…"，必须只出现一次。
        assert n <= 1 or phrase == "半写实 3D CG", (
            f"{phrase!r} 在三段里出现了 {n} 次（会过度加权）")


def test_style_prefix_does_not_stutter_with_template():
    """`{style_prefix}角色设定图` 拼出来不能是"…游戏角色**角色**设定图"。

    实测踩过：prefix 写成"半写实风格的 3D CG 游戏角色"。
    """
    p = build_sheet_prompt("测试角色", "描述")
    assert "游戏角色角色" not in p
    assert "角色角色" not in p


def test_built_prompt_uses_the_canonical_style():
    """填出来的提示词必须用这套风格，不许被调用方意外换掉。"""
    p = build_sheet_prompt("测试角色", "描述")
    assert SHEET_STYLE_PREFIX in p
    assert SHEET_STYLE in p
    assert SHEET_RENDER_TAIL in p
