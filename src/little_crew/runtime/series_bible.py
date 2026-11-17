"""系列资产台账（2026-11-17）：跨集场景/角色/档位的唯一事实来源。

## 为什么需要

单集能跑，但长篇短剧是"多集 + 复用 + 更新 + 升档"：

- 同一地点在第 1 集叫"站台"、第 8 集叫"空站台" → 必须有**稳定 sceneKey**
- 同一场景第 1 集 Tier1、第 5 集成题眼 → 需要**档位历史与升档**
- 角色跨集出现、换装 → 需要**角色 bible 与造型版本**
- 已经生成过的资产 → **复用（0 成本）**，而不是每集重生成

## 存储

```
series/
  series.json     # 本文件：剧集清单 / scenes / cast / usage / budget
  overrides.json  # 人工干预（可选）
```

## 纪律

- **稳定 key 优先用剧本地点**（标题第一段去时段），别名表可配
- 场景卡内容哈希变化 → 新版本；版本只增不减
- 档位 history 记录每次决策来源（llm/manual/heuristic/series）
- 复用判定：同 key + 同卡哈希 + 现有档位 ≥ 目标档位 → 可复用
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "SCHEMA", "load", "save", "stable_scene_key", "card_hash",
    "episodes_of", "upsert_episode", "upsert_scene", "upsert_character",
    "scene_entry", "character_entry", "decide_reuse", "record_usage",
    "summary", "find_scene",
]

SCHEMA = "little_crew.series/v1"
_TIME_WORDS = {"夜", "日", "晨", "黄昏", "清晨", "深夜", "午后", "傍晚",
               "凌晨", "雨", "白天", "晚上"}


def _loc_norm(text: Any) -> str:
    """地点归一：取标题第一段（去时段），去掉前后空白/标点。"""
    s = str(text or "").strip()
    if not s:
        return ""
    parts = [p.strip() for p in re.split(r"[·•|/]", s) if p.strip()]
    for p in parts:
        if p not in _TIME_WORDS:
            return re.sub(r"[（）()【】\[\]\s]", "", p)
    return parts[0] if parts else ""


def stable_scene_key(heading: Any, *, aliases: Mapping[str, str] | None = None) -> str:
    """剧本标题/场景卡 → 稳定 sceneKey。别名可把"空站台/站台"归到同一个。"""
    loc = _loc_norm(heading)
    if aliases:
        for k, v in aliases.items():
            _k = _loc_norm(k)
            if _k and (loc == _k or loc.startswith(_k) or loc.endswith(_k)
                       or _k in loc):
                return str(v)
    return loc.lower()


def card_hash(card: Mapping[str, Any] | None) -> str:
    try:
        from little_crew.ir.scene_card import canonical_scene_card

        canon = canonical_scene_card(dict(card or {}))
    except Exception:                                        # noqa: BLE001
        canon = dict(card or {})
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def load(base: str | Path) -> dict[str, Any]:
    """读 series.json；不存在返回空台账。"""
    p = Path(str(base)) / "series.json"
    if not p.is_file():
        return {"schema": SCHEMA, "seriesId": Path(str(base)).name,
                "episodes": [], "scenes": {}, "cast": {},
                "usage": [], "createdAt": ""}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {"schema": SCHEMA, "episodes": [], "scenes": {}, "cast": {},
                "usage": []}


def save(base: str | Path, data: Mapping[str, Any]) -> Path:
    p = Path(str(base)) / "series.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def episodes_of(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(x) for x in (data.get("episodes") or ()) if isinstance(x, Mapping)]


def upsert_episode(data: dict[str, Any], *, episode_id: str, title: str = "",
                   scenes: Sequence[str] = (), shots: int = 0,
                   status: str = "done") -> dict[str, Any]:
    for ep in data.setdefault("episodes", []):
        if isinstance(ep, dict) and ep.get("episodeId") == episode_id:
            ep.update({"title": title or ep.get("title", ""),
                       "scenes": sorted(set(scenes) or ep.get("scenes") or []),
                       "shots": shots or ep.get("shots", 0),
                       "status": status})
            return ep
    ep = {"episodeId": episode_id, "title": title,
          "scenes": sorted(set(scenes)), "shots": int(shots), "status": status}
    data["episodes"].append(ep)
    return ep


def scene_entry(data: Mapping[str, Any], scene_key: str) -> dict[str, Any] | None:
    scenes = data.get("scenes") or {}
    got = scenes.get(scene_key) if isinstance(scenes, Mapping) else None
    return dict(got) if isinstance(got, Mapping) else None


def find_scene(data: Mapping[str, Any], scene_key: str) -> dict[str, Any] | None:
    return scene_entry(data, scene_key)


def upsert_scene(data: dict[str, Any], *, scene_key: str, heading: str = "",
                 card: Mapping[str, Any] | None = None, tier: int | None = None,
                 episode_id: str = "", assets: Mapping[str, Any] | None = None,
                 decision_source: str = "", reason: str = "") -> dict[str, Any]:
    """登记/更新场景；卡哈希变化 → 版本 +1；档位只升不降（除非 force）。"""
    scenes = data.setdefault("scenes", {})
    prev = scenes.get(scene_key) if isinstance(scenes, dict) else None
    ch = card_hash(card) if card else ""
    try:
        from little_crew.ir.scene_card import canonical_scene_card as _csc

        _card_canon = _csc(dict(card or {})) if card else {}
    except Exception:                                        # noqa: BLE001
        _card_canon = dict(card or {})
    now = {"sceneKey": scene_key, "heading": heading,
           "cardHash": ch, "card": _card_canon, "updatedIn": episode_id,
           "lastDecisionSource": decision_source,
           "lastReason": str(reason)[:200]}
    if not isinstance(prev, dict):
        now.update({"version": 1, "tier": int(tier or 1), "episodes": [episode_id] if episode_id else [],
                    "usage": 0, "assets": dict(assets or {}), "tierHistory": []})
        if tier:
            now["tierHistory"].append({"episodeId": episode_id, "tier": int(tier),
                                       "source": decision_source})
        scenes[scene_key] = now
        return now
    # 已存在：版本/档位/使用
    prev_update = dict(prev)
    if ch and ch != str(prev.get("cardHash") or ""):
        prev_update["version"] = int(prev.get("version") or 1) + 1
        prev_update["cardHash"] = ch
    prev_update["heading"] = heading or prev.get("heading", "")
    if _card_canon:
        prev_update["card"] = _card_canon
    prev_update["updatedIn"] = episode_id or prev.get("updatedIn", "")
    prev_update["lastDecisionSource"] = decision_source or prev.get("lastDecisionSource", "")
    prev_update["lastReason"] = str(reason)[:200] if reason else prev.get("lastReason", "")
    if episode_id and episode_id not in (prev_update.get("episodes") or []):
        prev_update["episodes"] = list(prev_update.get("episodes") or []) + [episode_id]
    if tier is not None:
        old_tier = int(prev_update.get("tier") or 1)
        new_tier = max(old_tier, int(tier))     # 档位只升不降（人工可另外改）
        if new_tier != old_tier:
            prev_update["tier"] = new_tier
            prev_update.setdefault("tierHistory", []).append(
                {"episodeId": episode_id, "tier": new_tier,
                 "source": decision_source or "series"})
        else:
            prev_update["tier"] = old_tier
    if assets:
        # 按视图合并：新产物覆盖同名视图，但**不丢旧视图**（9 视图复用缺 2 张的根因）
        prev_update["assets"] = {**dict(prev_update.get("assets") or {}),
                                 **dict(assets)}
    scenes[scene_key] = prev_update
    return prev_update


def character_entry(data: Mapping[str, Any], character_id: str) -> dict[str, Any] | None:
    got = (data.get("cast") or {}).get(character_id) if isinstance(data.get("cast"), Mapping) else None
    return dict(got) if isinstance(got, Mapping) else None


def upsert_character(data: dict[str, Any], *, character_id: str, name: str = "",
                     appearance: str = "", outfit: str = "",
                     episode_id: str = "", anchor: str = "") -> dict[str, Any]:
    """登记/更新角色；外观变化 → 版本 +1；造型单独记版本集合。"""
    cast = data.setdefault("cast", {})
    prev = cast.get(character_id) if isinstance(cast, dict) else None
    app_hash = hashlib.sha256(str(appearance or "").encode("utf-8")).hexdigest()[:10]
    if not isinstance(prev, dict):
        cast[character_id] = {"characterId": character_id, "name": name or character_id,
                              "appearance": appearance, "appearanceHash": app_hash,
                              "version": 1, "outfits": [outfit] if outfit else [],
                              "episodes": [episode_id] if episode_id else [],
                              "anchor": anchor}
        return cast[character_id]
    upd = dict(prev)
    if app_hash != str(prev.get("appearanceHash") or ""):
        upd["version"] = int(prev.get("version") or 1) + 1
        upd["appearanceHash"] = app_hash
        upd["appearance"] = appearance
    upd["name"] = name or prev.get("name", character_id)
    if outfit and outfit not in (upd.get("outfits") or []):
        upd["outfits"] = list(upd.get("outfits") or []) + [outfit]
    if episode_id and episode_id not in (upd.get("episodes") or []):
        upd["episodes"] = list(upd.get("episodes") or []) + [episode_id]
    if anchor:
        upd["anchor"] = anchor
    cast[character_id] = upd
    return upd


def decide_reuse(data: Mapping[str, Any], *, scene_key: str,
                 card: Mapping[str, Any] | None, desired_tier: int) -> dict[str, Any]:
    """复用判定：同 key + 同卡哈希 + 现有档位 ≥ 目标 → 复用；否则重生成/升档。"""
    got = scene_entry(data, scene_key)
    if not got:
        return {"reuse": False, "why": "台账无此场景（首次）", "version": 0}
    ch = card_hash(card) if card else ""
    if ch and ch != str(got.get("cardHash") or ""):
        return {"reuse": False, "why": f"场景卡内容变化（v{got.get('version', 1)} → 新版本）",
                "version": int(got.get("version") or 1)}
    have = int(got.get("tier") or 1)
    want = int(desired_tier or 1)
    if have >= want:
        return {"reuse": True, "why": f"已有 Tier{have} ≥ 目标 Tier{want}",
                "version": int(got.get("version") or 1),
                "assets": dict(got.get("assets") or {})}
    return {"reuse": False, "upgrade": True,
            "why": f"档位升级 Tier{have} → Tier{want}",
            "version": int(got.get("version") or 1)}


def record_usage(data: dict[str, Any], *, episode_id: str, scene_key: str,
                 tier: int, action: str, source: str = "",
                 assets: Mapping[str, Any] | None = None) -> dict[str, Any]:
    entry = {"episodeId": episode_id, "sceneKey": scene_key, "tier": int(tier),
             "action": action, "source": source, "assets": dict(assets or {})}
    data.setdefault("usage", []).append(entry)
    sc = (data.get("scenes") or {}).get(scene_key)
    if isinstance(sc, dict):
        sc["usage"] = int(sc.get("usage") or 0) + 1
    return entry


def summary(data: Mapping[str, Any]) -> dict[str, Any]:
    scenes = data.get("scenes") or {}
    cast = data.get("cast") or {}
    tiers = {}
    for v in scenes.values():
        if isinstance(v, Mapping):
            tiers[int(v.get("tier") or 1)] = tiers.get(int(v.get("tier") or 1), 0) + 1
    return {"seriesId": data.get("seriesId"), "episodes": len(data.get("episodes") or ()),
            "scenes": len(scenes), "cast": len(cast), "tiers": tiers,
            "usage": len(data.get("usage") or ())}
