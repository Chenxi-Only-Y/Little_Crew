"""产物清理工具自检：**只删缓存尝试，交付物与证据一个不动**。"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def _load(root: Path):
    spec = importlib.util.spec_from_file_location(
        "prune_artifacts", root / "tools" / "prune_artifacts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                      # type: ignore[union-attr]
    return mod


PA = _load(Path(__file__).resolve().parents[3])


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("产物清理（先记后删）—— 自检")
    print("=" * 66)
    root = Path(__file__).resolve().parents[3]
    mod = _load(root)

    with tempfile.TemporaryDirectory() as td:
        wd = Path(td)
        (wd / "delivery").mkdir()
        (wd / "cache" / "t1").mkdir(parents=True)
        # 一个交付物 + 三个缓存尝试
        keep = wd / "cache" / "keep_me.png"
        keep.write_bytes(b"K" * 100)
        for i in range(3):
            (wd / "cache" / "t1" / f"try{i}.png").write_bytes(b"x" * (100 + i))
        (wd / "delivery" / "episode.master.json").write_text(json.dumps(
            {"shots": [{"shotId": "S1", "artifacts": ["keep_me.png"]}]}),
            encoding="utf-8")
        (wd / "delivery" / "delivery.evidence.json").write_text(json.dumps(
            {"artifactHashes": {"keep_me.png": "sha256:" + "a" * 64}}),
            encoding="utf-8")
        (wd / "delivery" / "run.report.json").write_text("{}", encoding="utf-8")

        print(); print("A. 计划")
        plan = mod.build_plan(wd)
        check("A1 ★★★ 交付物被豁免（不进删除计划）",
              all("keep_me" not in e["file"] for e in plan["entries"]),
              str([e["file"] for e in plan["entries"]]))
        check("A2 ★★★ 只删缓存尝试", plan["count"] == 3, str(plan["count"]))
        check("A3 ★★ 记下交付物个数", plan["deliveredCount"] == 1, str(plan["deliveredCount"]))
        check("A4 ★★ 认得出证据里声明过的哈希", plan["evidenceDeclared"] == 1,
              str(plan["evidenceDeclared"]))

        print(); print("B. 执行（先记后删）")
        res = mod.apply(wd, plan)
        check("B1 ★★★ 删了计划内的文件", res["removed"] == 3 and res["failed"] == 0, str(res))
        check("B2 ★★★ 交付物还在", keep.is_file(), "")
        check("B3 ★★★ 证据 json 一个都没动",
              (wd / "delivery" / "run.report.json").is_file()
              and (wd / "delivery" / "delivery.evidence.json").is_file(), "")
        rec = json.loads((wd / "delivery" / "pruned_artifacts.json")
                         .read_text(encoding="utf-8"))
        check("B4 ★★★ 有删除记录且写明后果",
              rec["count"] == 3 and "missing" in rec["consequence"], rec["consequence"][:40])
        check("B5 ★★ 记录里保留每个产物的 sha256 字段（没哈希就留空）",
              all("sha256" in e for e in rec["entries"]), "")
        check("B6 ★★ 记录写明删除原因", "体积" in rec["reason"], rec["reason"])

        print(); print("C. 边界")
        empty = Path(td) / "empty"
        empty.mkdir()
        p2 = mod.build_plan(empty)
        check("C1 ★★ 空目录 -> 0 个、不崩", p2["count"] == 0, str(p2["count"]))
        r2 = mod.main([str(empty)])
        check("C2 ★★ dry-run 返回 0（不删）", r2 == 0, str(r2))


    print(); print("G. 按集深度清理（2026-11-11）")
    import pathlib as _pathlib, tempfile as _tf, os as _os, time as _time

    def _mk(base, ep, age_days):
        d = base / ep
        (d / "delivery").mkdir(parents=True, exist_ok=True)
        (d / "cache").mkdir(parents=True, exist_ok=True)
        (d / "build").mkdir(parents=True, exist_ok=True)
        (d / "checkpoint").mkdir(parents=True, exist_ok=True)
        (d / "delivery" / "episode.mp4").write_bytes(b"x" * 1000)
        (d / "delivery" / "run.report.json").write_text("{}", encoding="utf-8")
        (d / "delivery" / "mix.wav").write_bytes(b"x" * 5000)
        (d / "cache" / "a.png").write_bytes(b"x" * 3000)
        (d / "build" / "clip.mp4").write_bytes(b"x" * 2000)
        (d / "checkpoint" / "stages.json").write_text("{}", encoding="utf-8")
        old = _time.time() - age_days * 86400
        for f in d.rglob("*"):
            if f.is_file():
                _os.utime(f, (old, old))
        return d

    with _tf.TemporaryDirectory() as td0:
        base = _pathlib.Path(td0) / "outputs"
        base.mkdir()
        for i, age in enumerate((0.1, 0.2, 0.3, 5.0, 6.0)):
            _mk(base, f"E{i}", age)
        deep = PA.deep_plan(base / "E3")
        names = {e["file"] for e in deep["entries"]}
        check("G1 ★★★ 深度清理会删 wav 与 cache/build",
              any(n.endswith("mix.wav") for n in names)
              and any("cache/" in n for n in names)
              and any("build/" in n for n in names), str(sorted(names))[:110])
        check("G2 ★★★ 深度清理保留成片 / json / checkpoint",
              not any(n.endswith("episode.mp4") for n in names)
              and not any(n.endswith(".json") for n in names),
              str([n for n in names if n.endswith(('.mp4', '.json'))]))
        fp = PA.fleet_plan(base, keep_recent=2, keep_names={"E3"}, min_age_days=0)
        eps = {r["episode"] for r in fp["episodes"]}
        check("G3 ★★★ 最近 N 集与指定集跳过",
              "E0" not in eps and "E1" not in eps and "E3" not in eps and "E4" in eps,
              str(sorted(eps)))
        fp2 = PA.fleet_plan(base, keep_recent=0, min_age_days=3)
        check("G4 ★★ 太新的目录不动（min-age-days）",
              "E2" not in {r["episode"] for r in fp2["episodes"]},
              str(sorted(r["episode"] for r in fp2["episodes"])))
        _before = {e["file"] for e in
                   next(r["plan"] for r in fp["episodes"] if r["episode"] == "E4")["entries"]}
        res = PA.fleet_apply(base, fp)
        _gone = all(not (base / "E4" / rel).is_file() for rel in _before)
        check("G5 ★★★ 清理后成片还在、计划过的文件都删了",
              (base / "E4" / "delivery" / "episode.mp4").is_file() and _gone,
              f"plan={sorted(_before)} res={res}")
        check("G6 ★★ 清理记录落盘（可追溯）",
              (base / "E4" / "delivery" / "pruned_artifacts.json").is_file(), "")

    print(); print("F. 阈值触发（--auto：没超就什么都不做）")
    with _tf.TemporaryDirectory() as td4:
        base2 = _pathlib.Path(td4) / "outputs"
        base2.mkdir()
        _mk(base2, "A1", 5.0)
        _mk(base2, "A2", 5.0)
        # 阈值调到很大 -> 跳过
        info = PA.auto_prune(base2, max_gb=99.0, keep_recent=0, min_age_days=0)
        check("F1 ★★★ 没超阈值 -> skipped 且不动文件",
              info.get("skipped") is True
              and (base2 / "A1" / "delivery" / "mix.wav").is_file(), str(info)[:80])
        # 阈值调到 0 -> 触发
        info2 = PA.auto_prune(base2, max_gb=0.0, keep_recent=0, min_age_days=0,
                              apply_deletes=False)
        check("F2 ★★★ 超阈值 -> 出计划（dry-run 不删）",
              info2.get("skipped") is False
              and (info2.get("fleet") or {}).get("episodes", 0) >= 1
              and (base2 / "A1" / "delivery" / "mix.wav").is_file(), str(info2)[:80])
        info3 = PA.auto_prune(base2, max_gb=0.0, keep_recent=0, min_age_days=0,
                              apply_deletes=True)
        check("F3 ★★★ apply_deletes 才真删，且成片保留",
              not (base2 / "A1" / "delivery" / "mix.wav").is_file()
              and (base2 / "A1" / "delivery" / "episode.mp4").is_file(),
              str(info3.get("result"))[:80])

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
