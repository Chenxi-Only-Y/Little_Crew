"""对外接口自检（2026-11-11）：能力清单 / 产物列表 / 下载 / 路径穿越。

CLI 子命令的检查在 `cli/check.py` 里 —— 否则会形成 `api -> cli` 反向依赖
（架构守卫会拦，实测抓过一次）。

不占端口、不调模型：直接调 `ApiServer.route(...)` 与 `build_parser()`。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from little_crew.api.server import ApiServer

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("对外接口 —— 自检")
    print("=" * 66)

    srv = ApiServer()

    print(); print("A. 能力清单")
    st, cap = srv.route("GET", "/v1/capabilities", {})
    check("A1 ★★★ 200 且带名册规模", st == 200 and cap.get("roster") == 19,
          f"{st} {cap.get('roster')}")
    check("A2 ★★★ 给出「在链路」数量（不许只给名册数）",
          cap.get("inChain") == 10, str(cap.get("inChain")))
    check("A3 ★★ 列出任务→模型（4 类）",
          len(cap.get("modelsByTask") or {}) == 4, str(cap.get("modelsByTask")))
    check("A4 ★★ 列出端点与 API 清单",
          "comfyui" in (cap.get("endpoints") or {}) and cap.get("api"),
          str(cap.get("api"))[:60])

    print(); print("B. 产物列表 / 下载")
    run = srv.runs.create("episode")
    run.result = {}
    st2, empty = srv.route("GET", f"/v1/runs/{run.run_id}/artifacts", {})
    check("B1 ★★★ 还没交付 -> 200 + 说明（不是 500）",
          st2 == 200 and empty.get("count") == 0 and empty.get("note"), str(empty)[:60])

    td = Path(tempfile.mkdtemp()) / "delivery"
    td.mkdir(parents=True)
    (td / "episode.mp4").write_bytes(b"MP4DATA")
    (td / "run.report.json").write_text("{}", encoding="utf-8")
    run.result = {"deliveryDir": str(td)}
    st3, listing = srv.route("GET", f"/v1/runs/{run.run_id}/artifacts", {})
    names = [f["name"] for f in listing.get("files") or []]
    check("B2 ★★★ 列出交付文件与大小",
          st3 == 200 and "episode.mp4" in names and listing.get("count") == 2, str(names))
    st4, data = srv.route("GET", f"/v1/runs/{run.run_id}/artifacts/episode.mp4", {})
    check("B3 ★★★ 下载返回原始字节", st4 == 200 and data == b"MP4DATA",
          f"{st4} {type(data).__name__}")
    st5, miss = srv.route("GET", f"/v1/runs/{run.run_id}/artifacts/nope.mp4", {})
    check("B4 ★★ 不存在的文件 -> 404", st5 == 404, str(miss)[:50])

    print(); print("C. 安全：路径穿越")
    for bad in ("..", "../secret", "a/b", "..%2Fx"):
        stx, payload = srv.route("GET", f"/v1/runs/{run.run_id}/artifacts/{bad}", {})
        check(f"C1 ★★★ 拒绝非法名 {bad!r}", stx in (400, 404), f"{stx} {payload}")
    st6, unr = srv.route("GET", "/v1/runs/不存在/artifacts", {})
    check("C2 ★★ 未知 runId -> 404（不是 500）", st6 == 404, str(unr)[:50])

    print(); print("F. 单镜接口 /v1/shot（2026-11-12）")
    st, shot = srv.route("POST", "/v1/shot",
                         {"prompt": "午后山坡，风吹花穗", "width": 832,
                          "height": 480, "steps": 8})
    check("F1 ★★★ 只编译不渲（dryRun=true）且给内容哈希",
          st == 200 and shot.get("dryRun") is True and bool(shot.get("contentHash")),
          str(shot)[:70])
    check("F2 ★★ 编译出的请求体带提示词与尺寸",
          "午后山坡" in str((shot.get("body") or {}).get("prompt"))
          and (shot.get("body") or {}).get("width") == 832,
          str(shot.get("body"))[:70])
    st2, bad = srv.route("POST", "/v1/shot", {})
    check("F3 ★★★ 缺 prompt -> 400（不是 500）", st2 == 400, str(bad)[:60])

    print(); print("G. 任务表落盘（重启不丢 runId）")
    import tempfile as _tf

    from little_crew.api.server import RunRegistry, RunState

    with _tf.TemporaryDirectory() as td:
        reg = RunRegistry(td)
        run = reg.create("episode")
        run.state = RunState.SUCCEEDED
        reg.touch(run)
        reg2 = RunRegistry(td)
        check("G1 ★★★ 重启后仍能查到该 runId",
              reg2.get(run.run_id) is not None, str(run.run_id))
        run2 = reg.create("episode")
        run2.state = RunState.RUNNING
        reg.touch(run2)
        reg3 = RunRegistry(td)
        got = reg3.get(run2.run_id)
        check("G2 ★★★ 重启前在跑的任务 -> unknown（不假装成功）",
              got is not None and got.state == RunState.UNKNOWN,
              str(getattr(got, "state", None)))

    print(); print("E. 路由 404")
    st7, nf = srv.route("GET", "/v1/nope", {})
    check("E1 ★★ 未知路径 -> 404 + 明确错误", st7 == 404 and "error" in nf, str(nf)[:60])

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
