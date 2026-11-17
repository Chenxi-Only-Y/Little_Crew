"""场景档位决策（2026-11-17）：**LLM 看整部剧**决定 Tier1/2/3，可人工干预。

为什么不再用"该场镜头数"公式：
- 镜头数只看局部；同一地点可能在剧本里反复出现（一场戏写长、拆成多场、跨集复用），
  这些只有**读完整剧本**才看得出来。
- 重要性也不只是"镜多"：一个只出现 2 镜的门口，可能是全剧的题眼。

所以决策交给 LLM（输入：整份剧本的场景标题/摘要 + 各场镜数 + 场景卡摘要），
输出每场档位 + 理由；**人工覆盖优先**；LLM 不可用时退回频率启发式，并在证据里写明来源。

档位与成本：
    Tier1 = 1 张空景锚点           ¥0.05
    Tier2 = 4 视图（含平面图）      ¥0.20
    Tier3 = 9 张九宫格 + 平面图     ¥0.45（每集上限 hero_max 场）
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

__all__ = ["plan_tiers", "heuristic_tiers", "TIER_COST_CNY"]

TIER_COST_CNY = {1: 0.05, 2: 0.20, 3: 0.45}

_SYSTEM = (
    "你是《小剧组》平台的美术统筹。你只输出 JSON，不解释。"
    "你的任务是给剧本里的每个场景选一个'场景空间视图档位'，控制成本的同时保证关键空间被锁住。"
)


def _scenes_of(script: Any) -> list[dict[str, Any]]:
    scenes: list[Any] = []
    if isinstance(script, Mapping):
        scenes = list(script.get("scenes") or ())
    elif script is not None:
        scenes = list(getattr(script, "scenes", ()) or ())
    out: list[dict[str, Any]] = []
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


def build_prompt(*, scenes: list[dict[str, Any]], shots_per_scene: Mapping[str, int],
                 scene_cards: Mapping[str, Mapping[str, Any]] | None,
                 hero_max: int) -> str:
    """构造"整部剧"决策提示词（所有场都列出；长文本截断）。"""
    lines = [
        f"这部剧共 {len(scenes)} 场。总预算策略：Tier3 每集最多 {hero_max} 场；"
        "Tier1 最便宜、Tier2 中等、Tier3 最贵。",
        "档位定义与用途：",
        f"- Tier1（¥{TIER_COST_CNY[1]}/场）：只出 1 张空景锚点；一次性过场/不重要空间。",
        f"- Tier2（¥{TIER_COST_CNY[2]}/场）：4 视图（基准/反向/45°/正交平面图）；普通主场景。",
        f"- Tier3（¥{TIER_COST_CNY[3]}/场）：9 张九宫格 + 正交平面图；"
        "反复出现/多机位/承载题眼的关键空间（最多 " + str(hero_max) + " 场）。",
        "",
        "## 全部场次",
    ]
    for sc in scenes:
        sid = sc["sceneId"]
        n = int(shots_per_scene.get(sid, 0))
        head = sc.get("heading") or "（无标题）"
        txt = (sc.get("text") or "").replace("\n", " ")[:180]
        card = (scene_cards or {}).get(sid) or {}
        card_bits = []
        for k in ("space", "materials", "props", "palette", "era"):
            v = card.get(k)
            if isinstance(v, (list, tuple)):
                v = "、".join(str(x) for x in v if str(x).strip())
            if str(v or "").strip():
                card_bits.append(f"{k}={str(v)[:80]}")
        lines.append(
            f"- {sid}｜{head}｜本场 {n} 镜"
            + (f"｜场景卡：{'；'.join(card_bits)}" if card_bits else "｜场景卡：无")
            + f"｜摘要：{txt}"
        )
    lines += [
        "",
        "## 输出（严格 JSON，不要其它内容）",
        '{"scenes": [{"sceneId": "S01", "tier": 1|2|3, "reason": "一句话理由"}], "seriesNote": "整部剧判断"}',
        "规则：",
        f"· 必须覆盖上面列出的每一场；Tier3 数量 ≤ {hero_max}",
        "· 判断依据：整部剧里该地点是否反复出现/是否多机位/是否承载题眼/空间是否复杂；",
        "  不能只看本场镜数",
        "· 不确定时给 Tier1，并写清理由",
    ]
    return "\n".join(lines)


def heuristic_tiers(script: Any, shots: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """LLM 不可用时的退路：频率启发式（与 0.4.8 相同口径）。"""
    from little_crew.runtime.scene_views import scene_frequency

    return scene_frequency(script, shots)


def _normalize_llm_out(obj: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(obj, Mapping):
        return out
    for row in (obj.get("scenes") or ()):
        if not isinstance(row, Mapping):
            continue
        sid = str(row.get("sceneId") or row.get("scene_id") or "")
        try:
            tier = int(row.get("tier") or 0)
        except (TypeError, ValueError):
            continue
        if not sid or tier not in (1, 2, 3):
            continue
        out[sid] = {"tier": tier,
                    "reason": str(row.get("reason") or "")[:200],
                    "source": "llm"}
    return out


def plan_tiers(*, script: Any, shots: Sequence[Any],
               scene_cards: Mapping[str, Mapping[str, Any]] | None = None,
               client: Any = None, hero_max: int = 2,
               overrides: Mapping[str, Any] | None = None,
               force_heuristic: bool = False) -> dict[str, Any]:
    """返回 `{scenes: {sceneId: {tier, reason, source}}, source, costCny, error}`。

    优先级：**人工覆盖 > LLM 决策 > 频率启发式**。
    """
    scenes = _scenes_of(script)
    per = _shots_per_scene(shots)
    base: dict[str, dict[str, Any]] = {}
    source = "heuristic"
    err = ""
    series_note = ""
    if not force_heuristic and client is not None and scenes:
        try:
            prompt = build_prompt(scenes=scenes, shots_per_scene=per,
                                  scene_cards=scene_cards, hero_max=hero_max)
            obj = client.chat_json(prompt, system=_SYSTEM, what="场景档位")
            _known = {s["sceneId"] for s in scenes}
            base = {sid: row for sid, row in _normalize_llm_out(obj).items()
                    if sid in _known}
            if base:
                source = "llm"
                series_note = str((obj or {}).get("seriesNote") or "")[:200]
            else:
                err = "LLM 输出缺少合法场景档位"
        except Exception as e:                               # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"
    if not base:
        base = {sid: {**row, "source": "heuristic"}
                for sid, row in heuristic_tiers(script, shots).items()}
    # 缺省场补启发式（LLM 漏场不算失败，但来源要标清）
    for sid, row in heuristic_tiers(script, shots).items():
        if sid not in base:
            base[sid] = {**row, "source": "heuristic"}

    # Tier3 上限：超出则按"LLM 给的分/镜数"降级，并写明
    t3 = [sid for sid, row in base.items() if int(row.get("tier") or 0) >= 3]
    if len(t3) > max(0, int(hero_max)):
        def _rank(sid: str) -> int:
            return int((base[sid].get("score") or per.get(sid, 0)))
        _keep = set(sorted(t3, key=lambda s: (-_rank(s), s))[:max(0, int(hero_max))])
        for sid in t3:
            if sid not in _keep:
                base[sid]["tier"] = 2
                base[sid]["reason"] = (str(base[sid].get("reason") or "")
                                       + f"（超每集 Tier3 上限 {hero_max}，降为 Tier2）")

    # 人工覆盖
    for sid, tier in (overrides or {}).items():
        try:
            t = int(tier)
        except (TypeError, ValueError):
            continue
        if sid in base and t in (1, 2, 3):
            base[sid] = {**base[sid], "tier": t, "source": "manual",
                         "reason": "人工指定"}
    # 成本（按决策张数）
    _imgs = {1: 1, 2: 4, 3: 9}
    cost = round(sum(TIER_COST_CNY.get(int(r.get("tier") or 1), 0.05)
                     for r in base.values()), 2)
    return {"scenes": base, "source": source, "costCny": cost,
            "seriesNote": series_note, "error": err}
