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
