"""定向返工：把「缺陷清单」变成「只重做哪几处」。

## 位置

策划书 §3.10.2 第二层。第一层（`review/interface_defects.py`）解决**说清楚哪里坏了**；
这一层解决**只改那一处** —— 否则验证做得再准，下游还是整篇重抽。

## 三个纯函数（都可单测，不碰 LLM）

| 函数 | 作用 |
|---|---|
| `select_targets(defects)` | 缺陷清单 -> 要重做的场次/镜头集合 |
| `merge_reworked(shots, new_shots, targets)` | 用重做结果**替换**目标镜头，其余原样保留 |
| `should_rework(defects)` | 有没有值得重做的（error 级才算） |

## 为什么先做"选目标"而不直接调模型

选目标是**确定的**，可以单测；调模型是有副作用的，且今天已证明
「先改对判据，再动生成」。**把确定的这一步做扎实，再接生成。**
"""
from __future__ import annotations

#: 需要重做的缺陷类型 -> 重做粒度
_SCENE_LEVEL = {"scene.intrusion", "budget.deviation"}
_SHOT_LEVEL = {"duration.over"}


def should_rework(defects) -> bool:
    """有 `error` 级缺陷才值得重做。`warn` 只记录，不触发重做。"""
    return any(isinstance(d, dict) and d.get("severity") == "error"
               for d in (defects or ()))


def select_targets(defects) -> dict:
    """缺陷清单 -> `{"scenes": [...], "shots": [...]}`（**只收 error 级**）。

    场级缺陷（越权）落到 `scenes`，镜头级（超时）落到 `shots`；
    能落到场也**同时**把该镜记进 `shots` —— 上游改动必然影响该镜。
    """
    scenes: set[str] = set()
    shots: set[str] = set()
    for d in defects or ():
        if not isinstance(d, dict) or d.get("severity") != "error":
            continue
        unit = d.get("unit") or {}
        sid_shot = str(unit.get("shotId") or "")
        sid_scene = str(unit.get("sceneId") or "")
        kind = str(d.get("kind") or "")
        if kind in _SCENE_LEVEL:
            if sid_scene:
                scenes.add(sid_scene)
            if sid_shot:
                shots.add(sid_shot)
        elif kind in _SHOT_LEVEL:
            if sid_shot:
                shots.add(sid_shot)
        else:
            if sid_scene:
                scenes.add(sid_scene)
            if sid_shot:
                shots.add(sid_shot)
    return {"scenes": sorted(scenes), "shots": sorted(shots)}


def merge_reworked(shots, new_shots, targets) -> tuple:
    """用重做结果替换目标镜头；**未命中的原镜头一律保留**。

    - `targets["shots"]` 里的 shotId：用 `new_shots` 中同 id 的替换
    - `targets["scenes"]` 里的场：该场所有镜头用 `new_shots` 里同场的替换
    - 新结果里没有的，保留旧的（**不因重做而丢镜头**）
    """
    tgt_shots = set((targets or {}).get("shots") or ())
    tgt_scenes = set((targets or {}).get("scenes") or ())
    new_by_id = {str(getattr(s, "shot_id", "")): s for s in (new_shots or ())}
    out = []
    for s in shots or ():
        sid = str(getattr(s, "shot_id", ""))
        scene = str(getattr(s, "scene_id", ""))
        if sid in tgt_shots or scene in tgt_scenes:
            out.append(new_by_id.get(sid, s))
        else:
            out.append(s)
    return tuple(out)


def plan(defects) -> dict:
    """给报告用的一段说明：要重做几处、为什么。"""
    t = select_targets(defects)
    n = len(t["scenes"]) + len(t["shots"])
    return {
        "shouldRework": should_rework(defects),
        "targets": t,
        "count": n,
        "summary": (f"定向重做 {len(t['scenes'])} 场 / {len(t['shots'])} 镜"
                    if n else "无需重做"),
    }
