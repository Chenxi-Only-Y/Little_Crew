"""trace 落盘自检（2026-11-12）：调用树与预算能不能被外部读到。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.trace_dump import dump, read_trace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("trace 落盘（spans + stages + budget）—— 自检")
    print("=" * 66)

    print(); print("A. 基本写入")
    with tempfile.TemporaryDirectory() as td:
        spans = [{"kind": "agent", "caller": "director",
                  "callee": "storyboard_artist", "capability": "shot.list"}
                 for _ in range(5)]
        p = dump(td, episode_id="EF99", ok=True,
                 stages={"script": {"t": 1.2}, "render": {"t": 900.0}},
                 spans=spans,
                 budget={"limits": {"steps": 100}, "spent": {"steps": 5}})
        check("A1 ★★★ 写出 trace.json", bool(p) and Path(p).is_file(), str(p))
        tr = read_trace(td)
        check("A2 ★★★ 记录 span 数量（5）", tr.get("spanCount") == 5, str(tr.get("spanCount")))
        check("A3 ★★★ 预算账本一并落盘",
              (tr.get("budget") or {}).get("spent", {}).get("steps") == 5, str(tr.get("budget"))[:60])
        check("A4 ★★ 阶段耗时也在", "render" in (tr.get("stages") or {}), str(tr.get("stages"))[:50])
        check("A5 ★★ 没有残留 .tmp", not (Path(td) / "trace.json.tmp").exists(), "")

    print(); print("B. 超限保留头尾（不静默截断）")
    with tempfile.TemporaryDirectory() as td2:
        many = [{"i": i} for i in range(100)]
        dump(td2, episode_id="EF98", ok=True, spans=many, max_spans=10)
        tr2 = read_trace(td2)
        check("B1 ★★★ 超限标记 truncated=True", tr2.get("truncated") is True, str(tr2.get("truncated")))
        check("B2 ★★★ 保留头尾各一半（10 条）", len(tr2.get("spans") or []) == 10,
              str(len(tr2.get("spans") or [])))
        kept = tr2.get("spans") or []
        check("B3 ★★ 头尾确实是首尾（0 与 99 都在）",
              (kept[0].get("i") == 0 and kept[-1].get("i") == 99) if kept else False,
              str([k.get("i") for k in kept[:2]]) + "..." + str([k.get("i") for k in kept[-2:]]))
        check("B4 ★★ spanCount 仍报告真实总数", tr2.get("spanCount") == 100, str(tr2.get("spanCount")))

    print(); print("C. 没写过 / 坏文件")
    with tempfile.TemporaryDirectory() as td3:
        check("C1 ★★ 没写过 -> 空 dict", read_trace(td3) == {}, "")
        (Path(td3) / "trace.json").write_text("{bad", encoding="utf-8")
        check("C2 ★★ 坏 JSON -> 空 dict（不抛）", read_trace(td3) == {}, "")

    print(); print("D. 失败不阻断")
    with tempfile.TemporaryDirectory() as td4:
        p4 = dump(td4, episode_id="X", ok=False, spans=[], budget={}, reason="F-BUDGET")
        tr4 = read_trace(td4)
        check("D1 ★★★ 失败也写（ok=false + reason）",
              bool(p4) and tr4.get("ok") is False and "F-BUDGET" in str(tr4.get("reason")),
              str(tr4.get("reason")))

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
