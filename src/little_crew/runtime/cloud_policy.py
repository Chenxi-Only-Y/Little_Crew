"""混合渲染策略：**哪些镜头值得走云端**（2026-11-13，方案 C）。

## 背景

本地渲染色（Z-Image-Turbo int8 / 4 步 / 832×480）在细节与一致性上是天花板；
云端（gpt-image-2.6 / 1536×1024）明显更好但按张计费。
所以不是"全本地"或"全云端"，而是**把有限的云端预算给最该给的镜头**。

## 打分规则（确定性，可解释）

| 条件 | 加分 | 理由 |
|---|---|---|
| 景别是特写/近景（或描述里含"特写/面部/眼神"） | +3 | 脸是崩坏最伤的地方 |
| 场次首镜（每场第一镜） | +2 | 开场镜决定这一场的观感 |
| 场次末镜 | +1 | 收尾镜常被用作封面候选 |
| 同镜出现 ≥2 个角色 | +1 | 多人同框更容易崩 |

按分数降序、**同分按镜序**（稳定），取前 `max_cloud` 个。
分数为 0 的镜头不走云端（远景/过场）。

## 纪律

- **纯函数**：同样的镜头表永远得到同一批云端镜（可复现、可解释）
- 上限硬约束：`max_cloud` 默认 8（一集 40 镜的 20%）
- 每镜带 `reason`，写进 `delivery/cloud_shots.json` 可复核
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 默认最多几镜走云端
DEFAULT_MAX_CLOUD = 8

#: 景别关键词（命中即算特写/近景）
_CLOSEUP_WORDS = ("特写", "近景", "半身", "面部", "眼神", "大特写", "close", "medium close")
#: 远景/过场关键词（明确排除）
_WIDE_WORDS = ("远景", "全景", "大全景", "航拍", "wide", "establishing")


def _text_of(shot: Any) -> str:
    parts = []
    for key in ("shot_size", "shotSize", "size", "description", "prompt"):
        v = getattr(shot, key, None) if not isinstance(shot, Mapping) else shot.get(key)
        if v:
            parts.append(str(v))
    return " ".join(parts)


def _scene_of(shot: Any) -> str:
    for key in ("scene_id", "sceneId", "scene"):
        v = getattr(shot, key, None) if not isinstance(shot, Mapping) else shot.get(key)
        if v:
            return str(v)
    sid = str(getattr(shot, "shot_id", "") if not isinstance(shot, Mapping)
              else shot.get("shot_id") or "")
    parts = sid.split("-")
    return parts[2] if len(parts) >= 3 else ""


def _characters_of(shot: Any) -> Sequence[str]:
    v = getattr(shot, "characters", None) if not isinstance(shot, Mapping) \
        else shot.get("characters")
    return list(v or ())


def score_shot(shot: Any, index: int, total: int,
               *, first_of_scene: bool = False,
               last_of_scene: bool = False) -> tuple[int, list[str]]:
    """单镜打分 -> `(分数, 理由列表)`。"""
    text = _text_of(shot).lower()
    score = 0
    why: list[str] = []
    if any(w in text for w in _CLOSEUP_WORDS) and not any(w in text for w in _WIDE_WORDS):
        score += 3
        why.append("特写/近景")
    if first_of_scene:
        score += 2
        why.append("场次首镜")
    if last_of_scene:
        score += 1
        why.append("场次末镜")
    if len(_characters_of(shot)) >= 2:
        score += 1
        why.append("多人同框")
    return score, why


def plan(picked: Iterable[Any], *, max_cloud: int = DEFAULT_MAX_CLOUD) -> list[dict]:
    """挑出要上云端的镜头（按分数降序、同分按镜序）。"""
    shots = list(picked)
    if max_cloud <= 0 or not shots:
        return []
    scene_first: dict[str, int] = {}
    scene_last: dict[str, int] = {}
    for i, s in enumerate(shots):
        sc = _scene_of(s)
        scene_first.setdefault(sc, i)
        scene_last[sc] = i
    rows: list[dict] = []
    for i, s in enumerate(shots):
        sc = _scene_of(s)
        score, why = score_shot(s, i, len(shots),
                                first_of_scene=(scene_first.get(sc) == i),
                                last_of_scene=(scene_last.get(sc) == i))
        if score <= 0:
            continue
        rows.append({"shotId": str(getattr(s, "shot_id", "") if not isinstance(s, Mapping)
                                   else s.get("shot_id") or ""),
                     "index": i, "scene": sc, "score": score,
                     "reasons": why})
    rows.sort(key=lambda r: (-r["score"], r["index"]))
    picked_rows = rows[:max_cloud]
    picked_rows.sort(key=lambda r: r["index"])       # 输出按镜序，方便阅读
    return picked_rows


def summarize(rows: Iterable[Mapping[str, Any]]) -> str:
    rows = list(rows)
    if not rows:
        return "无云端镜（全部本地）"
    return (f"云端 {len(rows)} 镜：" +
            "、".join(f"{r['shotId']}({r['score']})" for r in rows[:6]) +
            ("…" if len(rows) > 6 else ""))
