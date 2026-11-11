"""逐场分镜（可单测）：一场一次调用，再合并（2026-11-09）。

## 为什么不用现成的 `storyboard_per_scene`

它写好了但**从未被调用**；今天接上后整集只出 3 镜、且全部来自**最后一场**
（`{S08: 3}`）—— 合并行为与预期不符。与其猜它内部的约定，
不如在 runtime 写一个**行为可见、可单测**的版本。

## 做法

```
for 每一场:
    sub = copy(draft); sub.scenes = (那一场,)
    out = orch.run_stage("storyboard_artist", "shot.list", {"script.final": sub})
    若失败或 0 镜 -> 重试一次
    收下这场的镜头（并**按场号过滤**，防止混进别的场）
合并 -> ShotList 塞回黑板
```

## 纪律

- **任何一场最终为 0 镜 -> 整体失败**（不能悄悄少一场）
- 合并后**校验场号覆盖**：`scenesCovered` 与 `scenes` 一起返回，少一场就说少一场
"""
from __future__ import annotations

import copy
from typing import Any


def _sub_draft(draft, scene):
    """只留一场的稿子（dataclass 用 replace，其它用浅拷贝）。"""
    import dataclasses

    try:
        return dataclasses.replace(draft, scenes=(scene,))
    except Exception:                                         # noqa: BLE001
        sub = copy.copy(draft)
        try:
            sub.scenes = (scene,)
        except Exception:                                     # noqa: BLE001
            pass
        return sub


def _scene_id(scene) -> str:
    if isinstance(scene, dict):
        return str(scene.get("scene_id") or scene.get("id") or "")
    return str(getattr(scene, "scene_id", "") or "")


def per_scene(orch, draft, *, retries: int = 1, progress=None,
              parallel: bool = False, subjects=()) -> dict:
    """逐场生成并合并。返回 `{ok, shots, shotsByScene, scenes, scenesCovered, failed, reason}`。"""
    from little_crew.agent.messages import Shot, ShotList

    scenes = list(getattr(draft, "scenes", ()) or ())
    if not scenes:
        return {"ok": False, "shots": [], "shotsByScene": {},
                "scenes": 0, "scenesCovered": 0, "failed": [],
                "reason": "剧本没有场次"}

    def _one(sc):
        """取单场镜头。返回 (sceneId, shots, error)。"""
        sid = _scene_id(sc)
        got: list[Any] = []
        err = ""
        for attempt in range(1 + max(0, int(retries))):
            sub = _sub_draft(draft, sc)
            # 并发路径：走**无黑板副作用**的直调（黑板同一个键会互相覆盖）
            if parallel and hasattr(orch, "invoke_payload"):
                res, _dt = orch.invoke_payload(
                    "storyboard_artist", "shot.list",
                    {"script.final": sub, "scene.subjects": tuple(subjects or ())})
                if res is None or not getattr(res, "ok", False):
                    err = str(getattr(res, "error_message", "")
                              or getattr(res, "error_code", "") or "分镜失败")[:120]
                    continue
                _out = getattr(res, "output", {}) or {}
                val = _out.get("shot.list")
                shots = list(getattr(val, "shots", ()) or ())
            else:
                out = orch.run_stage("storyboard_artist", "shot.list",
                                     {"script.final": sub,
                                      "scene.subjects": tuple(subjects or ())})
                if not getattr(out, "ok", False):
                    err = str(getattr(out, "error", "")
                              or getattr(out, "error_message", "") or "分镜失败")[:120]
                    continue
                val = orch.blackboard.value("shot.list")
                shots = list(getattr(val, "shots", ()) or ())
            mine = [s for s in shots if _scene_id_of_shot(s) == sid]
            got = mine or shots
            if got:
                break
            err = "0 镜"
        return sid, got, err

    merged: list[Any] = []
    by_scene: dict[str, int] = {}
    failed: list[dict] = []
    # 并发（2026-11-11）：8 场分镜是 8 次**独立**的云端调用，串行纯属浪费。
    # 用线程池（I/O 密集），结果按场次顺序合并；任一场 0 镜 -> 整体失败（语义不变）。
    if parallel and len(scenes) > 1 and hasattr(orch, "invoke_payload"):
        from concurrent.futures import ThreadPoolExecutor
        import os as _os

        _workers = max(1, min(4, int(_os.environ.get("LITTLE_CREW_SB_WORKERS") or 4),
                              len(scenes)))
        try:
            with ThreadPoolExecutor(max_workers=_workers) as ex:
                results = list(ex.map(_one, scenes))
        except Exception:                                     # noqa: BLE001
            results = [_one(sc) for sc in scenes]
        for sid, got, err in results:
            if not got:
                failed.append({"sceneId": sid, "error": err or "0 镜"})
                if progress:
                    progress(sid, 0)
                continue
            merged.extend(got)
            by_scene[sid] = len(got)
            if progress:
                progress(sid, len(got))
    else:
        for sc in scenes:
            sid = _scene_id(sc)
            got: list[Any] = []
            err = ""
            for attempt in range(1 + max(0, int(retries))):
                out = orch.run_stage("storyboard_artist", "shot.list",
                                     {"script.final": _sub_draft(draft, sc)})
                if not getattr(out, "ok", False):
                    err = str(getattr(out, "error", "")
                              or getattr(out, "error_message", "") or "分镜失败")[:120]
                    continue
                val = orch.blackboard.value("shot.list")
                shots = list(getattr(val, "shots", ()) or ())
                # 只收这场自己的（agent 可能整稿返回）
                mine = [s for s in shots if _scene_id_of_shot(s) == sid]
                got = mine or shots
                if got:
                    break
                err = "0 镜"
            if not got:
                failed.append({"sceneId": sid, "error": err or "0 镜"})
                if progress:
                    progress(sid, 0)
                continue
            merged.extend(got)
            by_scene[sid] = len(got)
            if progress:
                progress(sid, len(got))

    if failed:
        return {"ok": False, "shots": merged, "shotsByScene": by_scene,
                "scenes": len(scenes), "scenesCovered": len(by_scene),
                "failed": failed,
                "reason": f"{len(failed)}/{len(scenes)} 场没有镜头：" +
                          "；".join(f"{f['sceneId']}({f['error']})"
                                   for f in failed[:5])}

    frm = getattr(draft, "frm", "") or "screenwriter"
    sl = ShotList(frm="storyboard_artist", to="*", stage="storyboard",
                  episode_id=str(getattr(draft, "episode_id", "") or ""),
                  shots=tuple(merged),
                  total_seconds=round(sum(float(getattr(s, "duration_sec", 0) or 0)
                                          for s in merged), 2))
    orch.blackboard.put("shot.list", sl, owner="storyboard_artist")
    return {"ok": True, "shots": list(merged), "shotsByScene": by_scene,
            "scenes": len(scenes), "scenesCovered": len(by_scene),
            "failed": [], "reason": "", "from": frm}


def _scene_id_of_shot(s) -> str:
    return str(getattr(s, "scene_id", "") or "")
