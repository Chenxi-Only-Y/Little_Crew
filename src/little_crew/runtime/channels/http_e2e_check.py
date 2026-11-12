"""`HttpChannel` **端到端**自检（2026-11-12）：起一个本地假服务，真发 HTTP。

## 为什么要有它

`HttpChannel` 的同步/异步两条路径此前只验了"响应键解析"——
真正的 `submit -> poll -> collect` 从来没在**真 HTTP** 上跑过一轮。
而这两条路径正是云通道的全部：键名错一个，表现是"服务端说成功但取不到产物"。

本模块起一个 `http.server`（127.0.0.1，随机端口），实现三个端点：

| 端点 | 行为 |
|---|---|
| `GET /health` | 200（探活） |
| `POST /v1/generate` | 同步：直接返回产物 URL |
| `POST /v1/tasks` | 异步：返回 `task_id`，状态先 `running` 再 `succeeded` |
| `GET /v1/tasks/{id}` | 状态查询 |
| `GET /out*.png` | 产物字节 |

**不碰外网、不要 key**：这样别人 clone 下来也能跑（与"契约测试不该依赖
ComfyUI 是否在跑"是同一条纪律）。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.channels.http import HttpChannel
from little_crew.runtime.render import CompiledJob

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Stub:
    """假服务状态：控制异步任务的查询次数（第一次 running、第二次 succeeded）。"""

    def __init__(self) -> None:
        self.polls = 0
        self.posts = 0


def _handler(state: _Stub):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):                            # noqa: ANN002
            pass

        def _json(self, obj, code=200):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):                                     # noqa: N802
            if self.path == "/health":
                return self._json({"ok": True})
            if self.path.startswith("/out"):
                data = b"\x89PNG\r\n\x1a\n" + b"stub"
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/v1/tasks/"):
                state.polls += 1
                if state.polls < 2:
                    return self._json({"status": "running"})
                base = f"http://127.0.0.1:{self.server.server_address[1]}"
                return self._json({"status": "succeeded",
                                   "output": {"url": base + "/out2.png"}})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):                                    # noqa: N802
            state.posts += 1
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            base = f"http://127.0.0.1:{self.server.server_address[1]}"
            if self.path == "/v1/generate":
                return self._json({"data": [{"url": base + "/out1.png"}]})
            if self.path == "/v1/tasks":
                return self._json({"task_id": "T-1", "status": "queued"})
            return self._json({"error": "not found"}, 404)

    return H


def _job(body: dict, *, key: str = "k1") -> CompiledJob:
    return CompiledJob(job_id="job-1", kind=BackendKind.HTTP,
                       backend=None, request=body,  # type: ignore[arg-type]
                       idempotency_key=key)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("HttpChannel 端到端（本地假服务，真发 HTTP）—— 自检")
    print("=" * 66)

    state = _Stub()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    try:
        print(); print("A. 同步通道（一次请求就出产物）")
        be_sync = Backend(kind=BackendKind.HTTP, endpoint=base,
                          path="/v1/generate",
                          response_path="data.0.url", artifact_kind="image")
        ch = HttpChannel(be_sync, timeout_s=5)
        ok, why = ch.available()
        check("A1 ★★★ 探活通过", ok, why)
        h = ch.submit(_job({"prompt": "x"}))
        check("A2 ★★★ 提交后即终结（同步通道）",
              h.status.value in ("succeeded", "failed"), str(h.status.value))
        check("A3 ★★★ 拿到产物 URL（response_path 生效）",
              bool(h.artifacts) and h.artifacts[0].uri.endswith("/out1.png"),
              str([a.uri for a in h.artifacts])[:80])
        arts = ch.collect(h)
        check("A4 ★★ collect 返回同一批产物", len(arts) == 1, str(len(arts)))

        print(); print("B. 异步通道（submit -> poll -> 终结）")
        be_async = Backend(kind=BackendKind.HTTP, endpoint=base,
                           path="/v1/tasks", status_path="/v1/tasks/{task_id}",
                           async_task=True, response_path="output.url",
                           artifact_kind="image")
        ch2 = HttpChannel(be_async, timeout_s=5)
        h2 = ch2.submit(_job({"prompt": "y"}, key="k2"))
        check("B1 ★★★ 提交后进入 RUNNING 且带 task_id",
              h2.status.value == "running" and h2.channel_id == "T-1",
              f"{h2.status.value} / {h2.channel_id}")
        h3 = ch2.poll(h2)
        check("B2 ★★★ 第一次 poll 仍未完成", h3.status.value == "running",
              h3.status.value)
        h4 = ch2.poll(h3)
        check("B3 ★★★ 第二次 poll 成功并带产物",
              h4.status.value == "succeeded"
              and any(a.uri.endswith("/out2.png") for a in h4.artifacts),
              f"{h4.status.value} / {[a.uri for a in h4.artifacts][:1]}")
        check("B4 ★★ 状态查询真的发了两次（不是本地瞎猜）", state.polls == 2,
              str(state.polls))

        print(); print("C. 幂等（R1：不能重复提交）")
        before = state.posts
        h5 = ch.submit(_job({"prompt": "x"}))
        check("C1 ★★★ 同幂等键再提交 -> 不再发请求",
              state.posts == before and h5.artifacts, f"posts={state.posts}")

        print(); print("D. 不可用时的语义")
        be_bad = Backend(kind=BackendKind.HTTP, endpoint="http://127.0.0.1:1",
                         path="/v1/generate", response_path="data.0.url")
        ch3 = HttpChannel(be_bad, timeout_s=1)
        okb, whyb = ch3.available()
        check("D1 ★★★ 端口不通 -> 探活失败且给原因（不抛）",
              not okb and bool(whyb), whyb[:60])
        hb = ch3.submit(_job({"p": 1}, key="k3"))
        check("D2 ★★★ 提交时通道不可用 -> FAILED 而不是异常",
              hb.status.value == "failed", hb.status.value)
    finally:
        srv.shutdown()

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
