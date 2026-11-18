"""边界重拍计划（2026-11-17，B2）。

边界审判红后，**只重拍边界镜**是关键：
- 边界镜 = 本章前 1~3 镜（接戏镜），不是整章
- 本模块只负责**定位/计划/证据**；真正的局部重渲由渲染阶段按 shotId 执行
- 重试上限：默认 2 次；仍红 -> 升级人工（不无限烧钱）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

__all__ = ["plan", "write", "run_retake", "MAX_RETRIES"]

MAX_RETRIES = 2


def plan(boundary: Mapping[str, Any], workdir: str | Path,
         *, max_shots: int = 3) -> dict[str, Any]:
    """返回重拍计划：只包含边界镜（本章前 N 镜）。"""
    wd = Path(str(workdir))
    shots_dir = wd / "assets" / "shots"
    shots: list[str] = []
    if shots_dir.is_dir():
        for sub in sorted(shots_dir.iterdir()):
            if sub.is_dir() and sub.name.startswith("SH-"):
                shots.append(sub.name)
    boundary_shots = shots[:max(1, int(max_shots))]
    level = str(boundary.get("level") or "")
    out = {"kind": "boundary.retake", "level": level,
           "needRetake": level == "red",
           "scope": "boundary_shots_only",
           "retakeShots": boundary_shots,
           "maxRetries": MAX_RETRIES,
           "reason": ("边界审红：只重拍边界镜" if level == "red"
                      else "边界审非红，无需重拍"),
           "workdir": str(wd)}
    return out


def write(workdir: str | Path, retake: Mapping[str, Any]) -> Path:
    p = Path(str(workdir)) / "delivery" / "retake-plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(dict(retake), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p



def run_retake(workdir: str | Path, shots: list[str], hint: str,
               *, renderer: Any = None) -> dict[str, Any]:
    """B2 执行层：给边界镜加 hint → 只重渲这些镜 → 更新 shot manifest。

    `renderer` 可注入（测试用）；默认走云端 `cloud_render.render_one`。
    """
    import json as _json
    from types import SimpleNamespace

    from little_crew.runtime import shot_manifest as _sm

    wd = Path(str(workdir))
    results: list[dict[str, Any]] = []
    for sid in shots:
        rec = _sm.load(wd, sid)
        if not rec:
            results.append({"shotId": sid, "ok": False,
                            "error": "没有 shot.json（先跑一次普通渲染）"})
            continue
        before = int(rec.get("version") or 0)
        new = _sm.bump_hint(wd, sid, hint)
        if renderer is None:
            from little_crew.runtime import cloud_render as _cr

            fake = SimpleNamespace(shot_id=sid,
                                   description=str(new.get("prompt") or ""),
                                   characters="")
            res = _cr.render_one(fake, str(new.get("anchor") or ""),
                                 wd / "assets" / "cloud_render",
                                 model=str(new.get("model") or _cr.DEFAULT_MODEL),
                                 extra="")
        else:
            res = renderer(sid, str(new.get("prompt") or ""))
        if not res.get("ok"):
            _sm.upsert(wd, shot_id=sid, prompt=str(new.get("prompt") or ""),
                       status="failed", meta={"error": str(res.get("error"))[:160]})
            results.append({"shotId": sid, "ok": False,
                            "error": str(res.get("error"))[:160]})
            continue
        upd = _sm.upsert(wd, shot_id=sid, prompt=str(new.get("prompt") or ""),
                         model=str(res.get("model") or new.get("model") or ""),
                         anchor=str(res.get("anchor") or new.get("anchor") or ""),
                         out=str(res.get("out") or ""), status="ok",
                         cost_cny=0.0 if res.get("reused") else 0.05,
                         meta={"retake": True, "hint": hint,
                               "reused": bool(res.get("reused"))})
        results.append({"shotId": sid, "ok": True,
                        "versionBefore": before, "versionAfter": upd.get("version"),
                        "out": upd.get("out"),
                        "reused": bool(res.get("reused")),
                        "costCny": 0.0 if res.get("reused") else 0.05})
    out = {"kind": "boundary.retake.result", "hint": hint,
           "shots": results, "ok": all(r.get("ok") for r in results) if results else False,
           "costCny": round(sum(float(r.get("costCny") or 0) for r in results), 4)}
    ev = wd / "delivery" / "retake-result.json"
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
