"""启动 HTTP 服务：`python -m little_crew.api`。

**零依赖**：只用标准库 `http.server`。见 `server.py` 的模块说明。

安全默认
--------
**默认绑 `127.0.0.1`**，不是 `0.0.0.0`。

本服务**没有鉴权**（见 `README` 的已知限制）。绑到 `0.0.0.0`
等于把"无人看守的 GPU 作业提交口"暴露到局域网 ——
任何能访问该端口的人都能让这台机器开始跑图。

想对外提供必须先解决鉴权；那时用 `--host` 显式打开，
**让"暴露到网络"成为一个需要打字的决定，而不是默认行为**。
"""

from __future__ import annotations

import argparse
import sys
import time

from little_crew.api.server import ApiServer, serve

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:                                   # noqa: BLE001
        pass

    p = argparse.ArgumentParser(
        prog="python -m little_crew.api",
        description="小剧组 · Little Crew —— HTTP 接口服务",
    )
    p.add_argument("--host", default="127.0.0.1",
                   help="监听地址（默认 127.0.0.1；服务无鉴权，勿轻易改）")
    p.add_argument("--port", type=int, default=8787, help="监听端口")
    args = p.parse_args(argv)

    api = ApiServer(host=args.host, port=args.port)
    httpd = serve(host=args.host, port=args.port, api=api)
    base = f"http://{args.host}:{args.port}"

    print("小剧组 · Little Crew —— HTTP 接口服务")
    print("=" * 66)
    print(f"  监听   {base}")
    print(f"  接口   GET  /healthz              存活检查")
    print(f"         GET  /v1/env               环境与配置来源")
    print(f"         POST /v1/compile           编译 IR（不提交）")
    print(f"         POST /v1/episode           跑一集（异步，返回 runId）")
    print(f"         GET  /v1/runs              任务列表")
    print(f"         GET  /v1/runs/{{id}}         任务状态")
    print(f"         GET  /v1/runs/{{id}}/events  SSE 进度流")
    print("=" * 66)
    if args.host not in ("127.0.0.1", "localhost"):
        print(f"  ⚠️ 已绑到 {args.host} —— 本服务**没有鉴权**，"
              f"同网段任何人都能提交作业")
    print("  Ctrl-C 停止")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止…")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
