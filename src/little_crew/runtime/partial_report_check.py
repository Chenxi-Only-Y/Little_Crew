"""`runtime/partial_report.py` 自检 —— 纯文件读写，不联网。

## 守什么

| 段 | 守什么 |
|---|---|
| A 写入 | 文件落在 `delivery/run.report.partial.json`，**不覆盖** `run.report.json` |
| B 已完成/还差 | **按实际 trace 算，不按静态顺序** —— 顺序算会把跑过的算成没跑 |
| C 磁盘事实 | 只写**数得出来**的（白模镜数 / 缓存文件 / 交付有无）；数不出的不猜 |
| D 摘要 | `summary_line` 能一行说清"停在哪、做到多少、还差几个阶段" |

## 为什么 B 段是重点

2026-10-20 实测：停在「分镜」时，报告把**已经跑过的「导演」列进了"还差"** ——
因为 `STAGES` 里 `storyboard` 写在 `director` 前面。
**静态顺序算不准**（真实执行顺序是 script → director → art → lighting → storyboard），
所以"已完成"必须按 `trace` 的实际键来。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.partial_report import (
    PARTIAL_NAME,
    STAGES,
    summary_line,
    write_partial,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


def _read(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _write() -> None:
    print()
    print("A. 写入")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        p = write_partial(wd, episode_id="E200", title="测试集",
                          stage="storyboard", reason="测试",
                          trace={"script": {"t": 0}, "director": {"t": 5},
                                 "storyboard": {"t": 60}})
        check("A1 文件在 delivery/ 下", p.parent.name == "delivery", str(p.parent))
        check("A2 文件名固定", p.name == PARTIAL_NAME, p.name)
        d2 = _read(p)
        check("A3 ★ 明确标 interrupted=True", d2.get("interrupted") is True,
              str(d2.get("interrupted")))
        check("A4 集号/标题写进去了",
              d2.get("episodeId") == "E200" and d2.get("title") == "测试集",
              f"{d2.get('episodeId')}/{d2.get('title')}")
        check("A5 停在哪一步（键 + 人话）",
              d2.get("stoppedAt") == "storyboard"
              and d2.get("stoppedAtName") == "分镜",
              f"{d2.get('stoppedAt')}/{d2.get('stoppedAtName')}")
        # 不覆盖正常报告
        (p.parent / "run.report.json").write_text('{"ok": true}', encoding="utf-8")
        write_partial(wd, stage="render", trace={"script": {}})
        check("A6 ★★ 不覆盖 run.report.json（两份都留着）",
              (p.parent / "run.report.json").read_text(encoding="utf-8") == '{"ok": true}',
              "被覆盖了")


def _trace_based() -> None:
    print()
    print("B. 已完成/还差 按 trace 算")
    with tempfile.TemporaryDirectory() as d:
        p = write_partial(Path(d), stage="storyboard",
                          trace={"script": {}, "director": {},
                                 "art": {}, "lighting": {}, "storyboard": {}})
        d2 = _read(p)
        done = " ".join(d2["finishedStages"])
        pending = " ".join(d2["pendingStages"])
        check("B1 ★★ 跑过的「导演」在已完成的里",
              "director" in done, done[:80])
        check("B2 ★★ 跑过的「美术」「灯光」也在已完成里",
              "art" in done and "lighting" in done, done[:80])
        check("B3 ★★ 「导演」**不在**还差里（第一次实测就是错在这）",
              "director" not in pending, pending[:80])
        check("B4 还差里只有真没跑的（白模/出图/合成）",
              ("previs" in pending and "render" in pending
               and "assemble" in pending), pending[:80])
        names = [k for k, _ in STAGES]
        check("B5 阶段表顺序符合真实执行（导演在分镜之前）",
              names.index("director") < names.index("storyboard"), str(names))


def _disk() -> None:
    print()
    print("C. 磁盘事实：只写数得出来的")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        (wd / "previs" / "SH-1").mkdir(parents=True)
        (wd / "previs" / "SH-1" / "previs_control.png").write_bytes(b"x")
        (wd / "previs" / "SH-2").mkdir(parents=True)
        (wd / "cache" / "t1" / "aa").mkdir(parents=True)
        (wd / "cache" / "t1" / "aa" / "f.png").write_bytes(b"y")
        p = write_partial(wd, stage="render", trace={"script": {}})
        disk = _read(p)["disk"]
        check("C1 ★ 白模镜数 = 2（数目录）", disk.get("previsShots") == 2,
              str(disk.get("previsShots")))
        check("C2 ★ 有控制图的只有 1 个（不把目录数当控制图数）",
              disk.get("previsControl") == 1, str(disk.get("previsControl")))
        check("C3 缓存文件数 = 1", disk.get("cacheFiles") == 1,
              str(disk.get("cacheFiles")))
        check("C4 交付目录空时 hasMaster=False",
              disk.get("hasMaster") is False, str(disk.get("hasMaster")))
        check("C5 ★ 数不出来的字段就不写（不猜「剧本为什么没过」）",
              "scriptVerdict" not in _read(p), "混进了猜的值")


def _summary() -> None:
    print()
    print("D. 一行摘要")
    with tempfile.TemporaryDirectory() as d:
        wd = Path(d)
        (wd / "previs" / "SH-1").mkdir(parents=True)
        p = write_partial(wd, stage="previs", trace={"script": {}})
        s = summary_line(p)
        check("D1 ★ 摘要说清停在哪", "白模预演" in s, s)
        check("D2 ★ 说清白模做了几镜", "白模 1 镜" in s, s)
        check("D3 ★ 说清还差几个阶段", "还差" in s, s)
        bad = Path(d) / "不存在.json"
        check("D4 摘要读不到文件时不崩（返回路径）",
              isinstance(summary_line(bad), str), "崩了")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")            # type: ignore[attr-defined]
    except Exception:                                        # noqa: BLE001
        pass
    print("=" * 74)
    print("半成品报告 —— 自检（纯文件）")
    print("=" * 74)
    _write()
    _trace_based()
    _disk()
    _summary()
    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 74)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
