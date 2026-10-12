"""逐镜视频生成的**规划器**：时长量化 + 首尾帧衔接。

## 决策（用户 2026-10-08 逐条拍板）

| 问题 | 定论 |
|---|---|
| 时长从哪来 | **台词实测（TTS 实长）+ LLM 估动作复杂度** |
| 短镜怎么处理 | **逐镜生成**（不再合并 —— 实测服务能收任意 num_frames） |
| 全片总长 | **不卡** |
| 衔接依据 | **按分镜**：`transition == "连续"` 才衔接 |
| 连续镜头首帧 | **用上一镜的尾帧** |
| 不连续镜头首帧 | **用本镜自己的分镜图** |
| 上一镜尾帧拿不到 | **降级成链头**（用分镜图），并**标注"未衔接"** |

## 为什么"按分镜决定"必须先修分镜

原先分镜提示词写的是 `"transition": "转场方式"` —— **自由文本、没有取值约束**，
LLM 可能输出任何东西（mock 数据里全是 `"切"`）。
拿这种值去判"要不要衔接"，等于没判。

所以 `TRANSITION_VALUES` 把它约束成**固定取值**，其中只有 `连续` 触发衔接。

## 时长为什么要"量化"

本地服务实测**不接受任意秒数**，会量化到自己的帧数档位：

    请求 seconds=3  → 107 帧 = 4.46s
    请求 seconds=4  → 107 帧 = 4.46s   ← 与 3s 同一个档位
    请求 seconds=6  → 158 帧 = 6.58s
    请求 num_frames=72  → 73 帧  = 3.04s
    请求 num_frames=120 → 124 帧 = 5.17s

**两个参数都作数，但都会被量化。** 所以规划器只输出**目标时长**，
由调用方按档位表量化；成片时再按目标时长剪辑到准确长度。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger("little_crew")

#: `transition` 的**受控取值**。只有 `连续` 触发首尾帧衔接。
#:
#: 分镜提示词必须按这个集合约束 LLM；否则值不可解析，"按分镜决定"就是空话。
TRANSITION_VALUES: tuple[str, ...] = (
    "连续",   # 顺着上一镜往下（同机位/同场景连续动作）→ 衔接
    "切",     # 硬切，换构图/换场景                   → 不衔接
    "淡入",   # 从黑/白淡入                             → 不衔接
    "淡出",   # 淡出到黑/白                             → 不衔接
    "叠化",   # 与上一镜叠化过渡                         → 不衔接
)

#: 触发衔接的那个取值
CONTINUOUS = "连续"

#: 无法解析时的兜底（当作不衔接 —— 衔接是会"篡改首帧"的强动作，
#: 拿不准时宁可不做）
DEFAULT_TRANSITION = "切"

#: 常见同义写法 → 受控取值（LLM 不一定听话，尽量救回来）
_TRANSITION_ALIASES: dict[str, str] = {
    "接上一镜": CONTINUOUS, "延续": CONTINUOUS, "连贯": CONTINUOUS,
    "连续镜头": CONTINUOUS, "同机位": CONTINUOUS, "跟着": CONTINUOUS,
    "硬切": "切", "直接切": "切", "cut": "切",
    "fade in": "淡入", "fadein": "淡入",
    "fade out": "淡出", "fadeout": "淡出",
    "dissolve": "叠化", "溶": "叠化", "溶接": "叠化",
}


def normalize_transition(raw: Any) -> str:
    """把分镜给的 `transition` 归一到 `TRANSITION_VALUES`；认不出→`切`（不衔接）。

    归一而不是原样透传，是因为这个值直接决定"要不要拿上一镜尾帧当首帧"——
    一个拼错的值若被当成"不认识"，行为就不可预期。
    """
    s = str(raw or "").strip()
    if not s:
        return DEFAULT_TRANSITION
    if s in TRANSITION_VALUES:
        return s
    low = s.lower()
    if low in _TRANSITION_ALIASES:
        return _TRANSITION_ALIASES[low]
    for k, v in _TRANSITION_ALIASES.items():
        if k and k in s:
            return v
    # 含中文"连续/延续"字样也认
    if "连续" in s or "延续" in s or "连贯" in s:
        return CONTINUOUS
    return DEFAULT_TRANSITION


def wants_chaining(shot: dict) -> bool:
    """该镜头是否需要与**上一镜**首尾帧衔接（按分镜的 `transition` 决定）。"""
    return normalize_transition((shot or {}).get("transition")) == CONTINUOUS


# ----------------------------------------------------------------------
# 规划
# ----------------------------------------------------------------------

def plan_video_segments(
    storyboard: Iterable[dict],
    images: Iterable[dict],
    *,
    fps: int = 24,
) -> list[dict]:
    """把分镜 + 分镜图规划成**逐镜生成计划**。

    每项输出：

        {
          "shot_id":       "S1-02",
          "target_seconds": 4.0,          # 来自分镜的 duration_s（语义定时长的产物）
          "target_frames":  96,           # = round(target_seconds * fps)
          "first_frame_src": "storyboard" | "prev_last",
          "chain_from":     "S1-01" | "", # 衔接自哪个镜头（空=链头）
          "chained":        True | False, # 最终是否真的衔接（拿不到尾帧会降级）
          "note":           "",           # 给用户看的原因（如"未衔接：上一镜无尾帧"）
        }

    ## 为什么"链头"是常态

    `transition != "连续"` 的镜头**本来就不该衔接** —— 换场景、换构图、
    淡入淡出都是"断开"。所以"不需要衔接的镜头"不需要特殊处理，它就是链头。

    ## 降级

    需要衔接但**上一镜拿不到尾帧**（生成失败 / 文件缺失 / 上一镜自己就是链头
    且没产出尾帧）→ 本镜降级为链头（用自己的分镜图）并在 `note` 里说明。
    绝不拿一个不存在或失败的结果去当首帧。
    """
    shots = [s for s in (storyboard or []) if isinstance(s, dict)]
    if not shots:
        return []

    # shot_id -> 分镜图（取该镜的产出图）
    by_shot: dict[str, dict] = {}
    for im in (images or []):
        if not isinstance(im, dict):
            continue
        sid = str(im.get("shot_id") or "").strip()
        if sid:
            by_shot[sid] = im

    plan: list[dict] = []
    prev_sid = ""
    prev_chained = False          # 上一镜是否真的做了衔接（决定它有没有产出尾帧）
    prev_ok = False

    for shot in shots:
        sid = str(shot.get("shot_id") or "").strip()
        if not sid:
            continue
        target = _target_seconds(shot)
        im = by_shot.get(sid) or {}
        has_own_image = bool(im.get("local_path") or im.get("image_url"))

        want = wants_chaining(shot)
        chained = False
        note = ""
        first_src = "storyboard"
        chain_from = ""

        if want:
            if not prev_sid:
                note = "未衔接：这是第一镜"
            elif not prev_ok:
                note = f"未衔接：上一镜 {prev_sid} 生成失败"
            else:
                # 上一镜要有尾帧才接得上。逐镜生成时，上一镜的**末帧**就是它的尾帧；
                # 这里只做规划，实际取帧由调用方完成（见 video.py）。
                first_src = "prev_last"
                chain_from = prev_sid
                chained = True

        if not has_own_image and not chained:
            # 既没有自己的图、也接不上 → 无法生成（调用方应跳过并标注）
            note = (note + "；" if note else "") + "未生成：缺分镜图"

        plan.append({
            "shot_id": sid,
            "target_seconds": target,
            "target_frames": max(1, int(round(target * fps))),
            "first_frame_src": first_src,
            "chain_from": chain_from,
            "chained": chained,
            "has_own_image": has_own_image,
            "note": note,
        })

        prev_sid = sid
        prev_chained = chained
        prev_ok = bool(has_own_image or chained)

    return plan


def _target_seconds(shot: dict) -> float:
    """取该镜的目标时长（秒）。

    优先 `duration_s`（节奏总监按语义算好的），其次 `duration`。
    都没有 → 用 `DEFAULT_SHOT_SECONDS`（宁可有个值也不给 0，
    因为 0 会让生成请求变成非法）。
    """
    for key in ("duration_s", "duration"):
        v = shot.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return round(f, 2)
    return DEFAULT_SHOT_SECONDS


#: 分镜没给时长时的兜底（秒）
DEFAULT_SHOT_SECONDS = 4.0


def summarize_plan(plan: list[dict]) -> dict:
    """给用户看的一行摘要：几镜、几镜衔接、几镜降级。"""
    total = len(plan)
    chained = sum(1 for p in plan if p["chained"])
    degraded = sum(1 for p in plan if p["note"].startswith("未衔接"))
    missing = sum(1 for p in plan if "缺分镜图" in p["note"])
    return {
        "shots": total,
        "chained": chained,
        "degraded": degraded,
        "missing_image": missing,
        "total_seconds": round(sum(p["target_seconds"] for p in plan), 2),
    }
