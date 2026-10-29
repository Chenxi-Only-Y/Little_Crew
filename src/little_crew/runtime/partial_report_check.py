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


def _source_order() -> None:
    """**源码顺序守卫**：白模预演必须排在出图之前。

    2026-10-20 实测发现：`_run_previs()` 曾被放在「合成」之后，
    于是白模控制图**永远来不及用** —— 渲染早就跑完了。
    那种错**不会报错**，只会让"有白模"这件事永远不生效。

    这里直接查源码里两个 `_mark` 的行号顺序 —— 比跑一遍整集快，也不会漏。
    """
    print()
    print("E. 源码顺序守卫")
    src = (Path(__file__).parent / "episode.py").read_text(encoding="utf-8")
    lines = src.splitlines()

    def where(needle: str) -> int:
        for n, line in enumerate(lines, 1):
            if needle in line:
                return n
        return -1

    previs = where('self._mark("previs")')
    render = where('self._mark("render_start")')
    check("E1 ★★ 源码里有 previs 标记", previs > 0, str(previs))
    check("E2 ★★ 源码里有 render_start 标记", render > 0, str(render))
    check("E3 ★★★ 白模预演在出图**之前**（否则控制图永远来不及用）",
          previs > 0 and render > 0 and previs < render,
          f"previs 在 {previs} 行，render 在 {render} 行")
    lp = where("self._run_line_producer()")
    check("E4 ★ 制片主任排期也在出图之前（它是前置预估）",
          lp > 0 and lp < render, f"排期在 {lp} 行，render 在 {render} 行")
    check("E5 排期在白模之前（先算钱，再渲白模）",
          lp > 0 and previs > 0 and lp < previs, f"{lp} vs {previs}")

    # ⚠️ 2026-10-21 实测两个 bug，都是「看着生效、实际没生效」：
    #   ① `--episode-id E300` 报告写 E300，但 shotId 仍是 `SH-E100-...`
    #      —— 因为 demo 里两处 `episode_id="E100"` 的 replace **静默失败**。
    #   ② `--limit 3` 只限制了渲染，白模仍跑 39 镜（多花约 10 分钟）。
    # 这两个都只能在源码里钉住 —— 跑一遍整集要 10 分钟，而且不容易察觉。
    src_demo = (Path(__file__).parent / "episode_demo.py").read_text(encoding="utf-8")
    check("E6 ★★★ story 集号不再写死 E100（--episode-id 才真的生效）",
          'episode_id="E100"' not in src_demo, "还有写死的 E100")
    check("E7 ★★ 集标题不再写死「薰衣草」",
          'title="薰衣草", backend=backend' not in src_demo, "标题还写死")
    check("E8 ★★★ 白模只预演将要渲染的镜头（--limit 必须同时管两处）",
          "self._run_previs(picked)" in src, "白模没用 picked")

    # 2026-10-29：`_run_previs` 曾在「没绑上实现」时 `return {}`。
    # 空 dict 与「白模跑了但没产出」在报告里无法区分 ——
    # 于是「没绑实现」被误读成「白模失败」，而白模失败不阻断，
    # 最终一集「跑通」了、控制图一张没进画面。
    src_ep = (Path(__file__).parent / "episode.py").read_text(encoding="utf-8")
    i_pre = src_ep.find("def _run_previs(")
    seg = src_ep[i_pre:i_pre + 1200]
    check("E9 ★★★ 无 previs 实现时给 reason，不返回空 dict",
          "没有绑定 previs 实现" in seg, "仍是 return {}（静默）")


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
    _source_order()
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
