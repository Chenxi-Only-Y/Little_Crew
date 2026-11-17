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

__all__ = ["plan", "write", "MAX_RETRIES"]

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
