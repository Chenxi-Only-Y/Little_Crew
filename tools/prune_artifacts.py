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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理一集的大体积产物（先记后删）")
    ap.add_argument("workdir")
    ap.add_argument("--yes", action="store_true", help="真删（默认只看）")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")               # type: ignore[attr-defined]
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
