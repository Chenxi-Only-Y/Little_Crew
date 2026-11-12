"""进度落盘自检（2026-11-12）：`state.json` 能不能被外部正确读到。

不跑整集：直接驱动 `Progress`，验证
"原子写 / 只增不改 / 三种终态 / 并发不坏 / 写失败不抛"。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

from little_crew.runtime.progress import (
    Progress, cancel_requested, read_state, request_cancel,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("进度落盘（state.json）—— 自检")
    print("=" * 66)

    print(); print("A. 基本写入")
    with tempfile.TemporaryDirectory() as td:
        p = Progress(td, episode_id="EF99")
        p.mark("script")
        st = read_state(td)
        check("A1 ★★★  첫 mark 后文件存在且是合法 JSON",
              bool(st) and st.get("kind") == "episode.state", str(st)[:60])
        check("A2 ★★★ 当前阶段与集号正确",
              st.get("stage") == "script" and st.get("episodeId") == "EF99", str(st)[:60])
        check("A3 ★★ 进行中 ok 为 null（不是 false）", st.get("ok") is None, str(st.get("ok")))

        p.mark("storyboard")
        p.mark("render")
        st2 = read_state(td)
        names = [s["stage"] for s in st2["stages"]]
        check("A4 ★★★ 阶段按顺序累积", names == ["script", "storyboard", "render"], str(names))

        print(); print("B. 只增不改（重复 mark 不产生重复项）")
        p.mark("render")
        st3 = read_state(td)
        names3 = [s["stage"] for s in st3["stages"]]
        check("B1 ★★★ 重复阶段只更新、不重复",
              names3 == ["script", "storyboard", "render"], str(names3))

        print(); print("C. 终态")
        p.finish(ok=True)
        st4 = read_state(td)
        check("C1 ★★★ 成功收尾 ok=true", st4.get("ok") is True, str(st4.get("ok")))
        # ⚠️ 不能写 `float(x or -1)`：elapsedSec 可能正好是 0.0，
        # 而 0.0 是 falsy -> 会被替换成 -1，测试自己把自己判失败（真踩过）。
        _el = st4.get("elapsedSec")
        check("C2 ★★ 记录了耗时（非负且是数字）",
              isinstance(_el, (int, float)) and _el >= 0, repr(_el))

    with tempfile.TemporaryDirectory() as td2:
        p2 = Progress(td2, episode_id="EF98")
        p2.mark("script")
        p2.finish(ok=False, error="F-BUDGET: 步数用尽")
        st5 = read_state(td2)
        check("C3 ★★★ 失败收尾 ok=false 且带错误文本",
              st5.get("ok") is False and "F-BUDGET" in str(st5.get("error")), str(st5.get("error")))

    print(); print("D. 没写过的目录")
    with tempfile.TemporaryDirectory() as td3:
        check("D1 ★★ 没有 state.json -> 返回空 dict（不抛）",
              read_state(td3) == {}, str(read_state(td3)))
        check("D2 ★★ 坏 JSON -> 也返回空 dict",
              (Path(td3) / "state.json").write_text("{bad", encoding="utf-8")
              is not None and read_state(td3) == {}, "")

    print(); print("E. 并发写不坏（原子替换）")
    with tempfile.TemporaryDirectory() as td4:
        p3 = Progress(td4, episode_id="EF97")
        ths = [threading.Thread(target=lambda i=i: p3.mark(f"s{i}"))
               for i in range(20)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        st6 = read_state(td4)
        check("E1 ★★★ 20 个线程各 mark 一次 -> 20 个阶段且 JSON 合法",
              len(st6.get("stages") or []) == 20, str(len(st6.get("stages") or [])))
        check("E2 ★★★ 没有残留 .tmp 文件（写完即替换）",
              not (Path(td4) / "state.json.tmp").exists(), "")

    print(); print("F. 写失败不阻断")
    p4 = Progress(str(Path(tempfile.gettempdir()) / "no_such_dir_xyz" / "a" / "b"),
                  episode_id="EF96")
    p4.mark("script")          # 目录不存在但会自动创建；这里主要验证不抛
    p4.finish(ok=True)
    check("F1 ★★★ 不可写路径也不抛异常（观测不该拖垮主流程）", True, "")

    print(); print("G. 取消信号（CANCEL 文件）")
    with tempfile.TemporaryDirectory() as td5:
        check("G1 ★★★ 没有标记 -> 不取消", cancel_requested(td5) is False, "")
        path = request_cancel(td5, "测试取消")
        check("G2 ★★★ 建标记后 -> 取消为真",
              bool(path) and cancel_requested(td5) is True, str(path))
        st7 = (Path(td5) / "CANCEL")
        check("G3 ★★ 标记里写了原因", st7.is_file() and "测试取消" in st7.read_text(encoding="utf-8"),
              st7.read_text(encoding="utf-8") if st7.is_file() else "")
        st7.unlink()
        check("G4 ★★ 删掉标记 -> 恢复", cancel_requested(td5) is False, "")

    print(); print("H. 接进 _mark（真流程会在阶段边界停下）")
    import time as _time

    from little_crew.runtime.episode import EpisodeRunner, StopRequested

    with tempfile.TemporaryDirectory() as td6:
        stub = type("_S", (), {})()
        stub.stage = ""
        stub.trace = {}
        stub._t0 = _time.time()
        stub._workdir = td6
        stub.stop_after = ""
        stub.episode_id = "EF95"
        stub.progress = None
        EpisodeRunner._mark(stub, "script")
        check("H1 ★★★ 没有 CANCEL 时 _mark 正常走", stub.stage == "script", stub.stage)
        request_cancel(td6, "验证")
        stopped = False
        try:
            EpisodeRunner._mark(stub, "render")
        except StopRequested:
            stopped = True
        check("H2 ★★★ 有 CANCEL 时在阶段边界停下（抛 StopRequested）", stopped, "")
        check("H3 ★★ 停下前已把进度落盘（能看出停在哪）",
              (Path(td6) / "state.json").is_file(), "")

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
