"""系列级档位决策（2026-11-17）：**看全剧台账**决定本集每场档位与升档。

与单集 `scene_tier_planner` 的区别：
- 输入多一份**系列台账**：每个场景出现在哪些集、历史档位、使用次数
- 输出多两项：**升档建议**（promotion）和稳定 `sceneKey`
- 策略：已有档位是**下限**（不降档）；跨集高频/复用 → 建议升档；Tier3 仍受每集上限
- LLM 不可用 → 系列感知启发式（单集频率 + 台账历史），来源写清
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

__all__ = ["plan_episode", "SERIES_SYSTEM"]

SERIES_SYSTEM = (
    "你是《小剧组》平台的美术统筹（系列级）。你只输出 JSON。"
    "你要参考**系列台账**（每个场景出现在哪些集、历史档位、使用次数），"
    "给当前这一集的每个场景选档位，并指出哪些场景应该升档。"
)


def _scenes_of(script: Any) -> list[dict[str, str]]:
    scenes: list[Any] = []
    if isinstance(script, Mapping):
        scenes = list(script.get("scenes") or ())
    elif script is not None:
        scenes = list(getattr(script, "scenes", ()) or ())
    out = []
    for s in scenes:
        if isinstance(s, Mapping):
            sid = str(s.get("id") or s.get("sceneId") or s.get("scene_id") or "")
            head = str(s.get("heading") or s.get("location") or "")
            text = str(s.get("text") or s.get("body") or "")
        else:
            sid = str(getattr(s, "scene_id", "") or getattr(s, "id", "") or "")
            head = str(getattr(s, "heading", "") or "")
            text = str(getattr(s, "text", "") or "")
        if sid:
            out.append({"sceneId": sid, "heading": head, "text": text})
    return out


def _shots_per_scene(shots: Sequence[Any]) -> dict[str, int]:
    per: dict[str, int] = {}
    for s in shots or ():
        sid = (str(s.get("sceneId") or s.get("scene_id") or "")
               if isinstance(s, Mapping) else str(getattr(s, "scene_id", "") or ""))
        if sid:
            per[sid] = per.get(sid, 0) + 1
    return per


def build_prompt(*, episode_id: str, scenes: list[dict[str, str]],
                 shots_per_scene: Mapping[str, int],
                 scene_cards: Mapping[str, Mapping[str, Any]] | None,
                 data: Mapping[str, Any], hero_max: int,
                 keys: Mapping[str, str]) -> str:
    lines = [
        f"## 当前集：{episode_id}（共 {len(scenes)} 场）",
        f"Tier3 每集最多 {hero_max} 场；**已有档位是下限，不降档**；"
        "如果某场景在系列里已经出现多次/承载题眼，可以建议升档。",
        "",
        "## 系列台账（已出现过的场景）",
    ]
    scenes_tab = (data.get("scenes") or {})
    if not scenes_tab:
        lines.append("（空：这是第一集）")
    for key, ent in list(scenes_tab.items())[:40]:
        if not isinstance(ent, Mapping):
            continue
        _eps = ",".join(str(x) for x in (ent.get("episodes") or [])[:6])
        lines.append(
            f"- {key}｜档位 T{ent.get('tier')}｜出现 {len(ent.get('episodes') or [])} 集"
            f"（{_eps}）｜次数 {ent.get('usage', 0)}"
            f"｜标题 {str(ent.get('heading') or '')[:40]}")
    lines += ["", "## 本集场次"]
    for sc in scenes:
        sid = sc["sceneId"]
        n = int(shots_per_scene.get(sid, 0))
        key = keys.get(sid, "")
        ent = scenes_tab.get(key) if key else None
        card = (scene_cards or {}).get(sid) or {}
        card_txt = "；".join(str(card.get(k) or "") for k in ("space", "materials", "props")
                             if str(card.get(k) or "").strip())[:120]
        prev = f"｜台账：T{ent.get('tier')}、出现 {len(ent.get('episodes') or [])} 集" \
            if isinstance(ent, Mapping) else "｜台账：新场景"
        lines.append(f"- {sid}｜{sc['heading']}｜{n} 镜{prev}"
                     + (f"｜场景卡：{card_txt}" if card_txt else ""))
    lines += [
        "",
        "## 输出（严格 JSON）",
        '{"scenes":[{"sceneId":"S01","tier":1|2|3,"sceneKey":"稳定地点键",'
        '"reason":"一句话","promotion": true|false}],"seriesNote":"系列级判断"}',
        f"规则：必须覆盖本集每一场；Tier3 数量 ≤ {hero_max}；"
        "已有档位不得低于台账（除非明确人工要求）。",
    ]
    return "\n".join(lines)


def _fallback(*, script: Any, shots: Sequence[Any], data: Mapping[str, Any],
              keys: Mapping[str, str], hero_max: int) -> dict[str, dict[str, Any]]:
    from little_crew.runtime.scene_tier_planner import heuristic_tiers

    base = heuristic_tiers(script, shots)
    out: dict[str, dict[str, Any]] = {}
    for sid, row in base.items():
        key = keys.get(sid, sid)
        ent = (data.get("scenes") or {}).get(key) if key else None
        floor = int((ent or {}).get("tier") or 1) if isinstance(ent, Mapping) else 1
        tier = max(int(row.get("tier") or 1), floor)
        out[sid] = {"tier": tier, "source": "series-heuristic",
                    "reason": (f"系列启发式：频率分 {row.get('score')}；"
                               f"台账下限 T{floor}" if floor > 1 else
                               f"系列启发式：频率分 {row.get('score')}"),
                    "sceneKey": key, "promotion": tier > floor}
    return out


def plan_episode(*, episode_id: str, script: Any, shots: Sequence[Any],
                 scene_cards: Mapping[str, Mapping[str, Any]] | None = None,
                 data: Mapping[str, Any] | None = None, client: Any = None,
                 hero_max: int = 2,
                 overrides: Mapping[str, Any] | None = None,
                 keys: Mapping[str, str] | None = None,
                 force_heuristic: bool = False) -> dict[str, Any]:
    """返回 `{scenes:{sceneId:{tier,source,reason,sceneKey,promotion}}, source, ...}`。"""
    from little_crew.runtime.series_bible import stable_scene_key

    data = dict(data or {})
    scenes = _scenes_of(script)
    per = _shots_per_scene(shots)
    if keys is None:
        keys = {sc["sceneId"]: stable_scene_key(sc["heading"]) for sc in scenes}
    base: dict[str, dict[str, Any]] = {}
    source, err, series_note = "series-heuristic", "", ""
    if not force_heuristic and client is not None and scenes:
        try:
            prompt = build_prompt(episode_id=episode_id, scenes=scenes,
                                  shots_per_scene=per, scene_cards=scene_cards,
                                  data=data, hero_max=hero_max, keys=keys)
            obj = client.chat_json(prompt, system=SERIES_SYSTEM,
                                   what="系列档位")
            known = {sc["sceneId"] for sc in scenes}
            from little_crew.runtime.scene_tier_planner import _normalize_llm_out

            rows = _normalize_llm_out(obj)
            for sid, row in rows.items():
                if sid not in known:
                    continue
                key = str(keys.get(sid) or "")
                # 从原始输出里找该场的 sceneKey/promotion
                for raw in (obj or {}).get("scenes") or ():
                    if isinstance(raw, Mapping) and str(raw.get("sceneId")) == sid:
                        key = (keys.get(sid)
                               or stable_scene_key(str(raw.get("sceneKey") or key)))
                        promo = bool(raw.get("promotion", False))
                        break
                else:
                    promo = False
                base[sid] = {"tier": int(row["tier"]), "source": "llm-series",
                             "reason": row.get("reason", ""), "sceneKey": key,
                             "promotion": promo}
            if base:
                source = "llm-series"
                series_note = str((obj or {}).get("seriesNote") or "")[:200]
            else:
                err = "LLM 输出缺少合法场景档位"
        except Exception as e:                               # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
    if not base:
        base = _fallback(script=script, shots=shots, data=data,
                         keys=keys, hero_max=hero_max)
    fb = _fallback(script=script, shots=shots, data=data, keys=keys,
                   hero_max=hero_max)
    for sid in per:
        if sid not in base:
            base[sid] = fb.get(sid) or {"tier": 1, "source": "series-heuristic",
                                        "reason": "单集决策漏场", "sceneKey": keys.get(sid, ""),
                                        "promotion": False}
        # 台账下限（不降档）
        key = base[sid].get("sceneKey") or keys.get(sid, "")
        ent = (data.get("scenes") or {}).get(key) if key else None
        floor = int((ent or {}).get("tier") or 1) if isinstance(ent, Mapping) else 1
        if int(base[sid]["tier"]) < floor:
            base[sid]["tier"] = floor
            base[sid]["reason"] = (str(base[sid].get("reason") or "")
                                   + f"（台账下限 T{floor}）")
    # Tier3 上限
    t3 = [sid for sid, row in base.items() if int(row["tier"]) >= 3]
    if len(t3) > max(0, int(hero_max)):
        def _rank(sid: str) -> int:
            ent = (data.get("scenes") or {}).get(base[sid].get("sceneKey") or "") or {}
            return len(ent.get("episodes") or []) * 10 + per.get(sid, 0)
        keep = set(sorted(t3, key=lambda s: (-_rank(s), s))[:max(0, int(hero_max))])
        for sid in t3:
            if sid not in keep:
                base[sid]["tier"] = 2
                base[sid]["reason"] = (str(base[sid].get("reason") or "")
                                       + f"（超每集 Tier3 上限 {hero_max}，降为 Tier2）")
                base[sid]["promotion"] = False
    # 人工覆盖
    for raw_key, tier in (overrides or {}).items():
        try:
            t = int(tier)
        except (TypeError, ValueError):
            continue
        if t not in (1, 2, 3):
            continue
        for sid, row in base.items():
            if raw_key in (sid, row.get("sceneKey")):
                row.update({"tier": t, "source": "manual", "reason": "人工指定",
                            "promotion": False})
    from little_crew.runtime.scene_tier_planner import TIER_COST_CNY

    cost = round(sum(TIER_COST_CNY.get(int(r["tier"]), 0) for r in base.values()), 2)
    return {"episodes": [episode_id], "scenes": base, "source": source,
            "seriesNote": series_note, "error": err, "costCny": cost,
            "keys": dict(keys)}
