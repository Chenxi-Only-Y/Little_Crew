"""接口缺陷清单：把验证从 `PASS/FAIL` 升级为**可定位的缺陷**。

## 为什么要有这个模块

`docs/00-项目策划书.md` §3.10.2 第一层：本项目的优势不在"生成更全"，
在"独立验证"。而验证要真有用，输出就不能只是判决 ——
**必须指向"哪个单元、哪个接口、哪类缺陷"**，否则下游只能整篇重抽。

## 三个"单元之间的接口"（都是已有信号，只差格式化）

| 接口 | 数据来源 | 缺陷类型 |
|---|---|---|
| `script->storyboard` | `episode.scene_conflicts` | `scene.intrusion` 越权新增 |
| `director->storyboard` | 导演预算 vs 实际镜头 | `budget.deviation` |
| `storyboard->render` | 单镜时长 vs 平台上限 | `duration.over` |

## 设计纪律

- **只输出有证据的缺陷**：没有信号就不编（`defects` 可以为空）
- **每条缺陷都必须能定位到单元**（`unit` 里至少有 shotId 或 sceneId）
- **不改变门禁判决**：这个清单是**附加产物**，不参与 PASS/FAIL
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SCENE, BUDGET, DURATION = "script->storyboard", "director->storyboard", "storyboard->render"


_SCENE_IN_SHOT = re.compile(r"-(S\d{1,3})-")


def _scene_of(shot_id: str) -> str:
    """从 shotId 里抠场次号（`SH-E15-S01-001` -> `S01`）。

    为什么需要：越权冲突记录里只有 shotId，**没有 sceneId** ——
    于是定向返工"找不到场级目标"，只能跳过（真跑验证时发现）。
    """
    m = _SCENE_IN_SHOT.search(str(shot_id or ""))
    return m.group(1) if m else ""


def from_scene_conflicts(conflicts) -> list[dict]:
    """越权新增 -> 缺陷。`warn`（丢掉场景）不算缺陷 —— 那是特写的常态。"""
    out = []
    for c in conflicts or ():
        if not isinstance(c, dict) or c.get("severity") != "error":
            continue
        _shot = str(c.get("shotId") or "")
        _scene = str(c.get("sceneId") or "") or _scene_of(_shot)
        out.append({
            "interface": SCENE,
            "unit": {"shotId": _shot, "sceneId": _scene},
            "kind": "scene.intrusion",
            "severity": "error",
            "detail": {"intruded": list(c.get("intruded") or ()),
                       "authorized": list(c.get("script") or ())},
            "reason": str(c.get("reason") or "")[:200],
            "fix": "退回分镜重写：不得引入剧本未授权的场景主体",
        })
    return out


def from_durations(shots, limit_s: float, *, hard_limit: float | None = None) -> list[dict]:
    """单镜时长 -> 缺陷（**软阈值 warn，硬阈值 error**）。

    ## 为什么必须有软阈值

    第一版只比「平台上限」（10 s）。但上游 `_to_shots` **已经拒绝** >10 s 的镜头，
    所以证据里永远不可能出现超限镜头 —— **这条检查是死代码**
    （2026-11-05 核 `MAX_SHOT_SECONDS=10.0` 才发现）。

    现在：> `limit_s`（默认 8 s）记 `warn`「偏长」；> `hard_limit`（默认 10 s）才记
    `error`（防御性：别的路径喂进来的镜头仍可能超限）。
    """
    soft = float(limit_s)
    hard = float(hard_limit) if hard_limit is not None else max(soft, 10.0)
    out = []
    for s in shots or ():
        try:
            dur = float(getattr(s, "duration_sec", 0) or 0)
        except (TypeError, ValueError):
            continue
        sid = str(getattr(s, "shot_id", ""))
        if dur > hard + 1e-6:
            out.append({
                "interface": DURATION, "unit": {"shotId": sid},
                "kind": "duration.over", "severity": "error",
                "detail": {"seconds": round(dur, 2), "limit": round(hard, 2)},
                "reason": f"单镜 {dur:.1f}s 超过平台上限 {hard:.1f}s",
                "fix": "切开这一镜，或缩短时长",
            })
        elif dur > soft + 1e-6:
            out.append({
                "interface": DURATION, "unit": {"shotId": sid},
                "kind": "duration.long", "severity": "warn",
                "detail": {"seconds": round(dur, 2), "softLimit": round(soft, 2)},
                "reason": f"单镜 {dur:.1f}s 偏长（软阈值 {soft:.1f}s）",
                "fix": "（仅记录）偏长镜更吃显存、节奏也拖；看分布再决定要不要收紧",
            })
    return out


def from_budget(plan, shots) -> list[dict]:
    """导演预算 vs 实际场次镜数 -> 缺陷（**只记录，不阻断**）。"""
    scenes = (plan or {}).get("scenes") or ()
    want = {}
    for s in scenes:
        sid = str(s.get("sceneId") or "")
        if sid:
            want[sid] = int(s.get("shotBudget") or 0)
    if not want:
        return []
    got: dict[str, int] = {}
    for s in shots or ():
        sid = str(getattr(s, "scene_id", "") or "")
        got[sid] = got.get(sid, 0) + 1
    out = []
    for sid, w in want.items():
        g = got.get(sid, 0)
        if w > 0 and g != w:
            out.append({
                "interface": BUDGET,
                "unit": {"sceneId": sid},
                "kind": "budget.deviation",
                "severity": "warn",
                "detail": {"budget": w, "actual": g, "delta": g - w},
                "reason": f"{sid}: 预算 {w} 镜，实给 {g} 镜",
                "fix": "（仅记录）预算偏差不阻断；看分布再决定要不要收紧",
            })
    return out


def collect(*, scene_conflicts=(), shots=(), limit_s=None, plan=None,
            episode_id="", budget_shots=None) -> dict:
    """汇总三类接口缺陷 -> 一个可落盘、可导出的清单。"""
    # **把"输入到底有没有"记下来**（2026-11-05）：
    # 空清单有两种可能 —— 「真的没问题」和「根本没喂进来」。
    # 不区分这两者，接口就会像"三个都活着"，而实际只有一个是活的。
    _scenes = (plan or {}).get("scenes") or ()
    inputs = {
        "planScenes": len(_scenes),
        "planTotalBudget": sum(int(s.get("shotBudget") or 0) for s in _scenes
                               if isinstance(s, dict)),
        "shots": len(tuple(shots or ())),
        "softLimit": limit_s,
    }
    defects = from_scene_conflicts(scene_conflicts)
    if limit_s is not None:
        defects += from_durations(shots, limit_s)
    # ⚠️ 预算要用**全量镜头表**比（2026-11-05）。
    # 用被 `--limit` 截断的那份比，未取到的场次会被当成「0 镜」，
    # 于是报出一串假偏差（真跑里一口气报了 6 条）。
    defects += from_budget(plan, budget_shots if budget_shots is not None else shots)
    counts: dict[str, int] = {}
    for d in defects:
        counts[d["interface"]] = counts.get(d["interface"], 0) + 1
    return {
        "kind": "interface.defects",
        "episodeId": episode_id,
        "total": len(defects),
        "byInterface": counts,
        "inputs": inputs,
        "defects": defects,
    }


def write(workdir, payload: dict) -> Path:
    """写到 `delivery/interface_defects.json`。**失败不抛**（附加产物）。"""
    out = Path(str(workdir)) / "delivery"
    out.mkdir(parents=True, exist_ok=True)
    dst = out / "interface_defects.json"
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst
