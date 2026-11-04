"""定向返工的**执行层**：只重跑被判不合格的场次，再合并回镜头表。

## 分层

| 层 | 模块 | 职责 |
|---|---|---|
| 判定 | `review/interface_defects.py` | 说清楚哪里坏了 |
| 计划 | `runtime/rework.py` | **选哪几处重做**、怎么合并（纯函数，可单测） |
| **执行** | **本模块** | 真的逐场重跑 → 合并 → 放回黑板 |

## 为什么把"跑"和"选"分开

`rework.py` 是纯函数，任何改动都能单测；本模块有外部副作用（调模型、写黑板），
所以把依赖全部**注入**进来 —— `run_stage` 与 `put_back` 都是参数，
测试时传假的即可，不需要起模型。

## 安全边界

1 **只重做 error 级**、且**最多 `max_scenes` 场**（默认 3）—— 防止一次返工把整集重写
2 **任何一场失败都不回滚已通过的镜头**：合并用 `merge_reworked`，未命中的原样保留
3 **整体失败不抛**：返回 `{"ok": False, "reason": ...}`，由调用方决定是否继续
"""
from __future__ import annotations

import dataclasses

from little_crew.runtime import rework as _rw


def _sub_draft(draft, scene):
    """只含一场的 draft 副本。`dataclasses.replace` 不行时退化为浅拷贝。"""
    try:
        return dataclasses.replace(draft, scenes=(scene,))
    except Exception:                                       # noqa: BLE001
        import copy

        sub = copy.copy(draft)
        object.__setattr__(sub, "scenes", (scene,))
        return sub


def rework_targets(*, run_stage, draft, defects, shots, max_scenes: int = 3,
                   max_new_per_scene: int = 6,
                   get_shots=None, put_back=None) -> dict:
    """按缺陷重跑受影响场次，返回合并后的镜头表。

    参数
    ----
    run_stage: `(agent, cap, payload) -> CallResult`，注入（真实的是编排器的方法）
    draft: 剧本（要有 `.scenes`）
    defects: 缺陷清单（`interface_defects.collect()` 的 `defects`）
    shots: 当前镜头表（`Shot` 序列）
    get_shots: `() -> ShotList|None`，重跑后从黑板取回新镜头表
    put_back: `(ShotList) -> None`，把合并结果放回黑板
    """
    if not _rw.should_rework(defects):
        return {"ok": True, "skipped": True, "reason": "没有 error 级缺陷", "shots": tuple(shots)}

    targets = _rw.select_targets(defects)
    scenes = list(targets.get("scenes") or ())
    if not scenes:
        return {"ok": True, "skipped": True, "reason": "缺陷没有场级目标", "shots": tuple(shots)}
    if len(scenes) > max_scenes:
        scenes = scenes[:max_scenes]

    by_id = {}
    for sc in (getattr(draft, "scenes", ()) or ()):
        sid = str(sc.get("scene_id") or sc.get("id") or "")
        if sid:
            by_id[sid] = sc

    new_shots, reworked, failed = [], [], []
    for sid in scenes:
        scene = by_id.get(sid)
        if scene is None:
            failed.append(f"{sid}：剧本里没有这场")
            continue
        sub = _sub_draft(draft, scene)
        try:
            out = run_stage("storyboard_artist", "shot.list", {"script.final": sub})
        except Exception as exc:                             # noqa: BLE001
            failed.append(f"{sid}：{type(exc).__name__}: {exc}")
            continue
        if not getattr(out, "ok", False):
            failed.append(f"{sid}：{str(getattr(out, 'error', '') or '重跑失败')[:80]}")
            continue
        got = ()
        if get_shots is not None:
            try:
                sl = get_shots()
                got = tuple(getattr(sl, "shots", ()) or ())
            except Exception:                                # noqa: BLE001
                got = ()
        got = tuple(s for s in got if str(getattr(s, "scene_id", "")) == sid)
        if not got:
            failed.append(f"{sid}：重跑后没有镜头")
            continue
        # ⚠️ **不许借"定向返工"放大镜头数**（2026-11-05 真跑发现）：
        # 重跑一场时模型给了 11~16 镜（原表该场只有 1 镜），
        # 合并后整集镜头数暴涨 —— 那不是"修一处"，是"顺手重写一场"。
        # 上限取"该场原有镜数"；原表没有该场时取 `max_new_per_scene`。
        _orig = sum(1 for s in (shots or ())
                    if str(getattr(s, "scene_id", "")) == sid)
        _cap = _orig if _orig > 0 else int(max_new_per_scene)
        if len(got) > _cap:
            print(f"  ↻ {sid}: 重跑给了 {len(got)} 镜，按上限截到 {_cap} 镜")
            got = got[:_cap]
        new_shots.extend(got)
        reworked.append(sid)

    merged = _rw.merge_reworked(shots, new_shots, {"scenes": reworked, "shots": []})
    if put_back is not None and reworked:
        try:
            sl = get_shots() if get_shots is not None else None
            if sl is not None:
                put_back(dataclasses.replace(sl, shots=tuple(merged)))
        except Exception:                                    # noqa: BLE001
            pass

    return {"ok": bool(reworked), "skipped": False,
            "reworked": reworked, "failed": failed,
            "addedShots": len(new_shots),
            "shots": tuple(merged),
            "reason": (f"重做 {len(reworked)} 场 / 新增 {len(new_shots)} 镜"
                       + (f"；失败 {len(failed)} 场" if failed else ""))}
