"""集库（`little-crew library`）：把 `outputs/` 下的每一集列成一张表（2026-11-12）。

## 为什么要它

一集跑完会留下 `run.report.json`、`episode.master.json`、成片、`state.json`、
`pruned_artifacts.json`…… 但这些是**按集散在目录里**的。
想回答"我跑过哪些集、哪些交付了、各花了多久"，以前只能手工翻 130 多个目录。

本模块只做**读**：扫 `outputs/*/delivery/run.report.json`，按时间倒序列表；
`--json` 给机器读；`--failed` 只看未交付。

## 纪律

- **不猜**：字段缺失就留空/None，不拿默认值填成"看起来有数据"。
- **注明来源**：每行的数据来自哪个文件（`source`），便于核对。
- 不修改任何产物。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}


def collect(outputs: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for d in sorted(outputs.iterdir()):
        if not d.is_dir():
            continue
        rep = d / "delivery" / "run.report.json"
        if not rep.is_file():
            continue
        j = _read_json(rep)
        video = j.get("video") or {}
        state = _read_json(d / "state.json")
        rows.append({
            "episode": str(j.get("episodeId") or d.name.lstrip("_")),
            "dir": str(d),
            "ok": bool(j.get("ok")),
            "shots": j.get("plannedShots"),
            "coverage": j.get("coverage"),
            "seconds": video.get("seconds"),
            "wallMinutes": (round(float(j.get("wallSeconds") or 0) / 60, 1)
                            if j.get("wallSeconds") else None),
            "scriptVerdict": j.get("scriptVerdict"),
            "failureKind": j.get("failureKind"),
            "state": state.get("stage") or "",
            "stateOk": state.get("ok"),
            "film": str((d / "delivery" / "episode.mp4")) if
                    (d / "delivery" / "episode.mp4").is_file() else "",
            "mtime": rep.stat().st_mtime,
            "source": str(rep),
        })
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _fmt(r: dict[str, Any]) -> str:
    mark = "✅" if r["ok"] else "❌"
    shots = r["shots"] if r["shots"] is not None else "-"
    secs = f"{r['seconds']:.0f}s" if isinstance(r["seconds"], (int, float)) else "-"
    wall = f"{r['wallMinutes']:.0f}分" if r["wallMinutes"] else "-"
    cov = f"{r['coverage']:.2f}" if isinstance(r["coverage"], (int, float)) else "-"
    return (f"  {mark} {r['episode']:<8} {str(shots):>3} 镜 · 覆盖 {cov} · "
            f"成片 {secs:>6} · 墙钟 {wall:>5} · 脚本 {str(r['scriptVerdict'] or '-'):<8}"
            f" · {r['state'] or '-'}")


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")               # type: ignore[attr-defined]
    except Exception:                                         # noqa: BLE001
        pass
    args = list(argv if argv is not None else sys.argv[1:])
    as_json = "--json" in args
    only_failed = "--failed" in args
    root = Path("outputs")
    for i, a in enumerate(args):
        if a == "--outputs" and i + 1 < len(args):
            root = Path(args[i + 1])
    if not root.is_dir():
        print(f"  目录不存在：{root}")
        return 2
    rows = collect(root)
    if only_failed:
        rows = [r for r in rows if not r["ok"]]
    if as_json:
        print(json.dumps({"count": len(rows), "episodes": rows},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    if not rows:
        print("  还没有任何一集（跑一集后这里会出现记录）")
        return 0
    ok = sum(1 for r in rows if r["ok"])
    print("=" * 92)
    print(f"集库 · {root} · {len(rows)} 集 · 交付 {ok}（{ok / len(rows) * 100:.0f}%）")
    print("=" * 92)
    for r in rows:
        print(_fmt(r))
    print("=" * 92)
    print("  提示：`--json` 给机器读 · `--failed` 只看未交付 · "
          "`little-crew report <dir>` 生成单集报告页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
