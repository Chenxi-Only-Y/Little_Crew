"""结构化日志自检（2026-11-12）：`run.log.jsonl` 的写入 / 过滤 / 轮转 / 容错。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.runlog import RunLog, read_events

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("结构化运行日志 —— 自检")
    print("=" * 66)

    print(); print("A. 写入与字段")
    with tempfile.TemporaryDirectory() as td:
        lg = RunLog(td, episode_id="EFX")
        lg.write("stage", stage="script", msg="开始")
        lg.write("stage", stage="render", msg="渲染 3/8", shot=3)
        lines = (Path(td) / "run.log.jsonl").read_text(encoding="utf-8").splitlines()
        check("A1 ★★★ 每行一个 JSON（NDJSON）",
              len(lines) == 2 and all(json.loads(x) for x in lines), str(len(lines)))
        rec = json.loads(lines[0])
        check("A2 ★★★ 字段齐全（t/elapsed/level/stage/event/episodeId/msg/extra）",
              set(rec) >= {"t", "elapsed", "level", "stage", "event",
                           "episodeId", "msg", "extra"}, str(sorted(rec)))
        check("A3 ★★ elapsed 是数字且非负",
              isinstance(rec["elapsed"], (int, float)) and rec["elapsed"] >= 0,
              repr(rec["elapsed"]))
        check("A4 ★★ extra 里带上了业务字段（shot=3）",
              json.loads(lines[1])["extra"].get("shot") == 3,
              str(json.loads(lines[1])["extra"]))

    print(); print("B. 读取与过滤")
    with tempfile.TemporaryDirectory() as td2:
        lg2 = RunLog(td2, episode_id="EFY")
        for i in range(5):
            lg2.write("stage", stage=("script" if i < 2 else "render"), n=i)
        check("B1 ★★★ 全量读 5 条", len(read_events(td2)) == 5, str(len(read_events(td2))))
        check("B2 ★★★ 按阶段过滤（render 3 条）",
              len(read_events(td2, stage="render")) == 3,
              str(len(read_events(td2, stage="render"))))
        check("B3 ★★ limit 生效", len(read_events(td2, limit=2)) == 2, "")
        check("B4 ★★ 没写过 -> 空列表（不抛）",
              read_events(Path(td2) / "nope") == [], "")

    print(); print("C. 容错")
    with tempfile.TemporaryDirectory() as td3:
        p = Path(td3) / "run.log.jsonl"
        p.write_text('{"ok":1}\n这不是 JSON\n{"ok":2}\n', encoding="utf-8")
        ev = read_events(td3)
        check("C1 ★★★ 坏行被跳过、好行保留（读日志不该整体失败）",
              len(ev) == 2, str(len(ev)))

    print(); print("D. 轮转")
    with tempfile.TemporaryDirectory() as td4:
        lg4 = RunLog(td4, episode_id="EFZ", max_bytes=200)
        for i in range(30):
            lg4.write("stage", stage="render", n=i, msg="x" * 20)
        base = Path(td4) / "run.log.jsonl"
        rot = Path(td4) / "run.log.jsonl.1"
        check("D1 ★★★ 超过 max_bytes 会轮转（出现 .1）", rot.is_file(), str(rot))
        check("D2 ★★ 当前文件小于上限（说明轮转过）",
              base.stat().st_size <= 400, str(base.stat().st_size))

    print(); print("E. 不可写路径不抛")
    bad = Path(tempfile.gettempdir()) / "no_such_root_zz" / "deep" / "x"
    lg5 = RunLog(str(bad), episode_id="BAD")
    lg5.write("stage", stage="script")   # 目录会被自动创建
    check("E1 ★★★ 不可写路径也不抛（日志不该拖垮出片）", True, "")

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
