"""场景卡（Scene Card）→ 提示词（确定性，2026-11-15）。

## 为什么需要

同一场戏会**跨多个镜头重复出现**（有时跨集复用同一场景）。如果每镜的场景描述
由 LLM 各自自由发挥，就会出现"同一站台，这镜是金属柱、下镜变成水泥柱"——
**场景前后不一致**。

美术指导（`production_designer`）本来就会产出每场的 `space / materials / props /
palette / era`（`art.bible.scenes`）。但在这之前它只作为**分镜师提示词的参考**，
由 LLM 转述 —— 转述必然不一致。

所以这里把它做成 **确定性注入**：
**同一张场景卡 → 永远得到逐字相同的场景片段 → 进 IR 哈希 → 跨镜/跨集可复现。**

## 字段（来自 `art.bible.scenes[]`）

| 字段 | 含义 | 例 |
|---|---|---|
`space` | 空间结构与尺度 | 地下站台，金属立柱，纵深透视 |
`materials` | 主要材质（2~3） | ["拉丝不锈钢", "湿沥青"] |
`props` | 剧情用得到的道具 | ["长椅", "列车车灯"] |
`palette` | 主色（2~3） | ["冷青", "品红霓虹", "炭黑"] |
`era` | 年代档（封闭集） | 当代 / 未来 … |

## 约束

1 **确定性**：字段顺序固定（space → materials → props → palette → era）
2 **严格**：`era` 必须在封闭集内，非法值报错（不静默）
3 **可哈希**：`scene_fingerprint()` 给证据用；IR 顶层带 `scene` 字段 → 换卡必重渲
4 **双语**：与 `ir_bridge` 的 `prompt_language` 一致（中文原生模型吃中文）
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

#: 年代档（与 `agent.production_designer_llm.ERA_LEVELS` 同一套封闭集）
ERA_LEVELS: tuple[str, ...] = ("古代", "近代", "民国", "当代", "未来", "架空")

#: 场景卡允许的字段（顺序即注入顺序）
SCENE_FIELDS = ("space", "materials", "props", "palette", "era")

_LIST_FIELDS = ("materials", "props", "palette")


def _as_list(v: Any) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [s.strip() for s in v.replace("、", ",").split(",") if s.strip()]
    return []


def canonical_scene_card(card: Mapping[str, Any] | None) -> dict[str, Any]:
    """归一化 + 校验。未知字段**忽略**（美术 bible 可能带更多信息），
    但已知字段的类型/取值**严格**。空卡返回 `{}`。
    """
    if not card:
        return {}
    out: dict[str, Any] = {}
    space = str(card.get("space") or "").strip()
    if space:
        out["space"] = space[:200]
    for k in _LIST_FIELDS:
        vals = _as_list(card.get(k))
        if vals:
            out[k] = vals[:8]
    era = str(card.get("era") or "").strip()
    if era:
        if era not in ERA_LEVELS:
            raise ValueError(
                f"场景卡 era={era!r} 不在封闭集内；可用：{list(ERA_LEVELS)}")
        out["era"] = era
    return {k: out[k] for k in SCENE_FIELDS if k in out}


def scene_terms(card: Mapping[str, Any] | None, language: str = "en") -> list[str]:
    """按固定顺序返回场景片段列表（中文/英文）。"""
    canon = canonical_scene_card(card)
    if not canon:
        return []
    zh = str(language).lower().startswith("zh")
    terms: list[str] = []
    if canon.get("space"):
        terms.append(("场景：" if zh else "scene: ") + str(canon["space"]))
    if canon.get("materials"):
        terms.append(("材质：" if zh else "materials: ")
                     + ("、".join(canon["materials"]) if zh
                        else ", ".join(canon["materials"])))
    if canon.get("props"):
        terms.append(("道具：" if zh else "props: ")
                     + ("、".join(canon["props"]) if zh
                        else ", ".join(canon["props"])))
    if canon.get("palette"):
        terms.append(("色板：" if zh else "color palette: ")
                     + ("、".join(canon["palette"]) if zh
                        else ", ".join(canon["palette"])))
    if canon.get("era"):
        terms.append(("年代：" if zh else "era: ") + str(canon["era"]))
    return terms


def scene_prompt(card: Mapping[str, Any] | None, language: str = "en") -> str:
    """拼成一段场景片段；空卡返回空串。"""
    terms = scene_terms(card, language)
    if not terms:
        return ""
    return ("，" if str(language).lower().startswith("zh") else ", ").join(terms)


def scene_fingerprint(card: Mapping[str, Any] | None) -> str:
    """归一化场景卡的短哈希（证据里核对"这镜用的是哪张卡"）。"""
    canon = canonical_scene_card(card)
    if not canon:
        return ""
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
