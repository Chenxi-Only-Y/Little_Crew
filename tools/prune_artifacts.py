#!/usr/bin/env python3
"""清理某一集的大体积产物 —— **但先把"删了什么"记下来**（2026-11-08）。

## 为什么不是直接 `rm`

删掉 PNG 之后，`verify.py` 必然会报 `missing` —— 这是**事实**，不该掩盖。
但如果连"删了哪些、各自 sha256 是多少"都没留，
那这一集就从"可复核"变成"不可追溯"，性质比省空间重要得多。

本工具做两件事：

1 从 `delivery/evidence.json`（或 `episode.master.json`）读出该集**声明过的**产物哈希
2 写 `delivery/pruned_artifacts.json`：每个文件名 + sha256 + 字节数 + 删除时间 + 原因
3 然后才删文件（默认 **dry-run**，加 `--yes` 才真删）

## 用法

```bash
python tools/prune_artifacts.py outputs/ep_white            # 只看会删什么
python tools/prune_artifacts.py outputs/ep_white --yes      # 真删
```

## 纪律

- **只删产物（图片/视频）**，`delivery/` 下的 json 一个都不动
- 删除清单落盘**在删除之前** —— 断电也不至于没有记录
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

#: 会被当作"产物"删除的扩展名（json/srt 等证据文件永不删）
ARTIFACT_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def declared_hashes(delivery: Path) -> dict:
    """证据里声明过的 `文件名 -> sha256`（两处格式都认）。"""
    out: dict[str, str] = {}
    for name in ("delivery.evidence.json", "episode.master.json"):
        f = delivery / name
        if not f.is_file():
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:                                     # noqa: BLE001
            continue
        node = j.get("artifactHashes")
        if isinstance(node, dict):
            out.update({str(k): str(v) for k, v in node.items()})
    return out


def delivered_names(workdir: Path) -> set[str]:
    """`episode.master.json` 里声明交付的产物名 —— **这些一个都不删**。

    2026-11-08 实测教训：`ep_white` 下 9664 张 PNG，其中只有 **40 张**是交付物，
    其余九千多张是**缓存尝试**。按扩展名一把删会把交付图也删掉。
    """
    out: set[str] = set()
    f = workdir / "delivery" / "episode.master.json"
    if f.is_file():
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            for s in (j.get("shots") or ()):
                for a in (s.get("artifacts") or ()):
                    out.add(str(a))
        except Exception:                                     # noqa: BLE001
            pass
    return out


def collect(workdir: Path) -> list[Path]:
    """workdir 下所有产物文件（不含 delivery/ 里的 json）。"""
    out = []
    for p in sorted(workdir.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in ARTIFACT_EXT:
            continue
        if "delivery" in p.parts and p.suffix.lower() == ".json":
            continue
        out.append(p)
    return out


def build_plan(workdir: Path) -> dict:
    delivery = workdir / "delivery"
    hashes = declared_hashes(delivery) if delivery.is_dir() else {}
    delivered = delivered_names(workdir)
    files = collect(workdir)
    entries, total, kept = [], 0, []
    for p in files:
        if p.name in delivered:
            kept.append(str(p.relative_to(workdir)).replace("\\", "/"))
            continue
        size = p.stat().st_size
        total += size
        entries.append({"file": str(p.relative_to(workdir)).replace("\\", "/"),
                        "bytes": size,
                        "sha256": hashes.get(p.name, ""),
                        "inEvidence": p.name in hashes})
    return {"kind": "prune.plan", "workdir": str(workdir),
            "count": len(entries), "bytes": total, "entries": entries,
            "keptDelivered": kept, "deliveredCount": len(delivered),
            "evidenceDeclared": len(hashes)}


def apply(workdir: Path, plan: dict) -> dict:
    """先写记录，再删文件。"""
    delivery = workdir / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    record = {
        "kind": "prune.record",
        "prunedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "reason": "体积清理：保留 delivery 与成片，删除逐镜产物",
        "count": plan["count"], "bytes": plan["bytes"],
        "consequence": ("verify.py 将对该集报 missing —— 这是删除的已知后果；"
                        "本文件保留每个产物的 sha256，可追溯但不可再独立复核"),
        "entries": plan["entries"],
    }
    (delivery / "pruned_artifacts.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    removed, failed = 0, 0
    for e in plan["entries"]:
        f = workdir / e["file"]
        try:
            f.unlink()
            removed += 1
        except Exception:                                     # noqa: BLE001
            failed += 1
    return {"ok": failed == 0, "removed": removed, "failed": failed,
            "freedBytes": plan["bytes"], "record": str(delivery / "pruned_artifacts.json")}


#: 深度清理时**永不删除**的东西（审计与续跑都靠它们）
KEEP_ALWAYS = (".json", ".srt", ".md", ".txt", ".yaml", ".yml")
KEEP_NAMES = ("episode.mp4", "episode.master.mp4")
KEEP_DIRS = ("checkpoint", "delivery")          # delivery 里再按扩展名筛


def deep_plan(workdir: Path) -> dict:
    """深度清理：删 wav / 中间 mp4 / cache / build，**保住 json+srt+成片+checkpoint**。

    为什么不能只按 ARTIFACT_EXT：实测一集 106MB 里
      delivery 66MB（其中 wav 51MB）· cache 27MB · build 12MB
    而 png 只占 27MB —— 光删图回收不到两成。
    """
    delivery = workdir / "delivery"
    hashes = declared_hashes(delivery) if delivery.is_dir() else {}
    entries, total = [], 0
    kept = 0
    for p in sorted(workdir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(workdir)).replace("\\", "/")
        parts = p.relative_to(workdir).parts
        if p.suffix.lower() in KEEP_ALWAYS or p.name in KEEP_NAMES:
            kept += 1
            continue
        if any(seg in KEEP_DIRS for seg in parts[:-1]) and p.suffix.lower() in KEEP_ALWAYS:
            kept += 1
            continue
        size = p.stat().st_size
        total += size
        entries.append({"file": rel, "bytes": size,
                        "sha256": hashes.get(p.name, ""),
                        "inEvidence": p.name in hashes})
    return {"kind": "prune.deep", "workdir": str(workdir),
            "count": len(entries), "bytes": total, "entries": entries,
            "keptFiles": kept, "evidenceDeclared": len(hashes)}


def _dir_mtime(p: Path) -> float:
    """目录的"最后活动时间" —— 用**内部文件的最新 mtime**，比目录本身准。"""
    newest = 0.0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    return newest


def fleet_plan(outputs: Path, *, keep_recent: int = 3,
               keep_names: Iterable[str] = (),
               min_age_days: float = 1.0, deep: bool = False) -> dict:
    """跨集规划：保留最近 N 集与指定集，其余只清逐镜产物（delivery 不动）。

    为什么按集而不是按文件：一集的**逐镜 PNG**占 70~90%，
    而 delivery 里的成片/清单/证据才是要留的。
    """
    keep = {str(x) for x in (keep_names or ())}
    dirs = [d for d in outputs.iterdir() if d.is_dir()]
    ordered = sorted(dirs, key=_dir_mtime, reverse=True)
    recent = {d.name for d in ordered[:max(0, int(keep_recent))]}
    now = time.time()
    rows, total, skipped = [], 0, []
    for d in ordered:
        if d.name in keep or d.name in recent:
            skipped.append({"episode": d.name, "why": "保留（最近或指定）"})
            continue
        age_days = (now - _dir_mtime(d)) / 86400.0
        if age_days < float(min_age_days):
            skipped.append({"episode": d.name, "why": f"太新（{age_days:.1f} 天）"})
            continue
        plan = deep_plan(d) if deep else build_plan(d)
        if not plan["count"]:
            skipped.append({"episode": d.name, "why": "没有可删产物"})
            continue
        total += plan["bytes"]
        rows.append({"episode": d.name, "plan": plan,
                     "count": plan["count"], "bytes": plan["bytes"]})
    return {"kind": "prune.fleet", "outputs": str(outputs),
            "keepRecent": int(keep_recent), "keepNames": sorted(keep),
            "minAgeDays": float(min_age_days), "deep": bool(deep),
            "episodes": rows, "count": sum(r["count"] for r in rows),
            "bytes": total, "skipped": skipped}


def fleet_apply(outputs: Path, fleet: dict) -> dict:
    freed, removed, failed = 0, 0, 0
    for row in fleet["episodes"]:
        res = apply(outputs / row["episode"], row["plan"])
        freed += int(res.get("freedBytes") or 0)
        removed += int(res.get("removed") or 0)
        failed += int(res.get("failed") or 0)
    return {"ok": failed == 0, "removed": removed, "failed": failed,
            "freedBytes": freed}


def outputs_size_bytes(root: Path) -> int:
    """目录总大小（只算文件）。"""
    total = 0
    for f in root.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def auto_prune(outputs: Path, *, max_gb: float = 2.0, keep_recent: int = 3,
               keep_names: Iterable[str] = (), min_age_days: float = 1.0,
               apply_deletes: bool = False) -> dict:
    """超过阈值才清理；没超就什么都不做。

    为什么用"阈值"而不是每次都清：清空缓存会让下一次整集渲染全部实算
    （一集多花 6~8 分钟）。纪律是只在占盘过大时清，且保留最近 N 集。
    """
    size = outputs_size_bytes(outputs)
    limit = int(float(max_gb) * 1024 ** 3)
    if size <= limit:
        return {"kind": "prune.auto", "skipped": True,
                "bytes": size, "limit": limit,
                "reason": f"占盘 {size/1024**3:.2f} GB <= 阈值 {max_gb} GB，不清理",
                "fleet": None}
    fleet = fleet_plan(outputs, keep_recent=keep_recent,
                       keep_names=keep_names, min_age_days=min_age_days,
                       deep=True)
    out = {"kind": "prune.auto", "skipped": False, "bytes": size,
           "limit": limit,
           "reason": f"占盘 {size/1024**3:.2f} GB > 阈值 {max_gb} GB，执行深度清理",
           "fleet": {"episodes": len(fleet["episodes"]),
                     "count": fleet["count"], "bytes": fleet["bytes"]}}
    if apply_deletes:
        out["result"] = fleet_apply(outputs, fleet)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理一集的大体积产物（先记后删）")
    ap.add_argument("workdir", nargs="?", default="",
                    help="某一集的工作目录（--all 时不需要）")
    ap.add_argument("--yes", action="store_true", help="真删（默认只看）")
    ap.add_argument("--all", action="store_true",
                    help="按集清理 outputs/ 下所有旧集（保留最近 N 与指定集）")
    ap.add_argument("--outputs", default="outputs", help="--all 时的根目录")
    ap.add_argument("--keep-recent", type=int, default=3, help="保留最近几集不动")
    ap.add_argument("--keep", default="", help="逗号分隔的集名，永不清理")
    ap.add_argument("--auto", action="store_true",
                    help="按阈值自动清理（超过 --max-gb 才执行，否则什么都不做）")
    ap.add_argument("--max-gb", type=float, default=2.0,
                    help="--auto 的阈值（GB，默认 2.0）")
    ap.add_argument("--deep", action="store_true",
                    help="深度清理：删 wav/中间 mp4/cache/build，保 json+srt+成片+checkpoint")
    ap.add_argument("--min-age-days", type=float, default=1.0,
                    help="少于这么多天的目录不动（默认 1 天）")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")               # type: ignore[attr-defined]
    if args.auto:
        outputs = Path(args.outputs)
        if not outputs.is_dir():
            print(f"  目录不存在：{outputs}")
            return 2
        keep_names = [x.strip() for x in str(args.keep or "").split(",") if x.strip()]
        info = auto_prune(outputs, max_gb=args.max_gb,
                          keep_recent=args.keep_recent, keep_names=keep_names,
                          min_age_days=args.min_age_days,
                          apply_deletes=bool(args.yes))
        print(f"  {info['reason']}")
        if info.get("skipped"):
            return 0
        fl = info.get("fleet") or {}
        print(f"  计划：{fl.get('episodes')} 集 · {fl.get('count')} 个产物 · "
              f"{fl.get('bytes', 0)/1024/1024:.0f} MB")
        if not args.yes:
            print("  这是 dry-run；加 --yes 才删")
            return 0
        res = info.get("result") or {}
        print(f"  已删 {res.get('removed')} 个（失败 {res.get('failed')}）· "
              f"释放 {res.get('freedBytes', 0)/1024/1024:.0f} MB")
        return 0 if res.get("ok", True) else 1

    if args.all:
        outputs = Path(args.outputs)
        if not outputs.is_dir():
            print(f"  目录不存在：{outputs}")
            return 2
        keep_names = [x.strip() for x in str(args.keep or "").split(",") if x.strip()]
        fleet = fleet_plan(outputs, keep_recent=args.keep_recent,
                           keep_names=keep_names,
                           min_age_days=args.min_age_days,
                           deep=bool(getattr(args, "deep", False)))
        print(f"  按集清理：{len(fleet['episodes'])} 集 · "
              f"{fleet['count']} 个产物 · {fleet['bytes']/1024/1024:.0f} MB")
        for r in sorted(fleet["episodes"], key=lambda x: -x["bytes"])[:8]:
            print(f"    {r['episode']:<10} {r['count']:4d} 个  "
                  f"{r['bytes']/1024/1024:6.1f} MB")
        if len(fleet["episodes"]) > 8:
            print(f"    …共 {len(fleet['episodes'])} 集")
        print(f"  跳过 {len(fleet['skipped'])} 集（保留/太新/无可删）")
        if not args.yes:
            print("  这是 dry-run；加 --yes 才删")
            return 0
        res = fleet_apply(outputs, fleet)
        print(f"  已删 {res['removed']} 个（失败 {res['failed']}）· "
              f"释放 {res['freedBytes']/1024/1024:.0f} MB")
        return 0 if res["ok"] else 1

    wd = Path(args.workdir)
    if not wd.is_dir():
        print(f"  目录不存在：{wd}")
        return 2
    plan = build_plan(wd)
    mb = plan["bytes"] / (1024 * 1024)
    print(f"  将删 {plan['count']} 个 · {mb:.1f} MB")
    print(f"  保留交付物 {len(plan['keptDelivered'])} 个 · "
          f"证据里声明过哈希 {plan['evidenceDeclared']} 个")
    if not args.yes:
        for e in plan["entries"][:5]:
            print(f"    {e['file']}  {e['bytes']/1024:.0f} KB  "
                  f"{'（有哈希）' if e['inEvidence'] else '（无哈希）'}")
        if plan["count"] > 5:
            print(f"    …共 {plan['count']} 个")
        print("  这是 dry-run；加 --yes 才删")
        return 0
    res = apply(wd, plan)
    print(f"  已删 {res['removed']} 个（失败 {res['failed']}）· "
          f"释放 {res['freedBytes']/1024/1024:.1f} MB")
    print(f"  删除记录：{res['record']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
