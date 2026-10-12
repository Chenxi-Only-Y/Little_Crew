"""HTTP 服务实现（标准库 `http.server`，零第三方依赖）。

核心设计：**长任务异步 + 进度可订阅**
------------------------------------
```
POST /v1/episode            -> 立刻返回 {runId}，后台线程开跑
GET  /v1/runs/{id}          -> 轮询状态（给简单客户端）
GET  /v1/runs/{id}/events   -> SSE 流（给要实时进度的客户端）
```

**为什么不做成同步**：跑一集是分钟级的。同步接口的三种结局都很糟：
  · 客户端等超时 -> 重试 -> **同一个作业被提交两次，烧两份 GPU**
  · 客户端等到了，但服务端中途崩了 -> 客户端无法区分"还在跑"与"挂了"
  · 网关有超时上限 -> 长任务必然失败

异步 + 可查询解决了这三条，且**复用平台已有的检查点机制**
（作业状态落盘，服务重启后仍可对账）。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

__all__ = ["RunState", "RunRegistry", "ApiServer", "serve"]


class RunState:
    """任务状态。**五态，不是三态** —— 理由与作业状态机一致。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: 服务重启后**无法确定**是否跑过 -> 必须对账，不可重试
    UNKNOWN = "unknown"

    TERMINAL = frozenset({SUCCEEDED, FAILED, UNKNOWN})


@dataclass
class Run:
    """一次后台任务。"""

    run_id: str
    kind: str
    state: str = RunState.PENDING
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    #: 人类可读的阶段（"分镜"/"渲染 3/8"/"门禁"）
    stage: str = ""
    #: 结构化结果（成功时是 `EpisodeResult.as_dict()`）
    result: Mapping[str, Any] | None = None
    error: str = ""
    #: 进度事件（SSE 从这里读）
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, stage: str, **extra: Any) -> None:
        """记一条进度。**同时更新 `stage`** —— 轮询与 SSE 看到的是同一份数据。"""
        self.stage = stage
        self.events.append({"at": time.time(), "stage": stage, **extra})

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "kind": self.kind,
            "state": self.state,
            "stage": self.stage,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
            "seconds": (round(self.finished_at - self.created_at, 2)
                        if self.finished_at else None),
            "error": self.error,
            "result": self.result,
        }


class RunRegistry:
    """任务表。**线程安全** —— 后台线程写，HTTP 线程读。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}

    def create(self, kind: str) -> Run:
        rid = uuid.uuid4().hex[:12]
        r = Run(run_id=rid, kind=kind)
        with self._lock:
            self._runs[rid] = r
        return r

    def get(self, rid: str) -> Run | None:
        with self._lock:
            return self._runs.get(rid)

    def all(self) -> list[Run]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda r: r.created_at,
                          reverse=True)

    def start(self, run: Run, fn: Callable[[Run], None]) -> None:
        """在**后台线程**里跑 `fn`，把异常也变成状态。

        ⚠️ 异常必须被捕获并写进 `error` ——
        后台线程里未捕获的异常**不会让 HTTP 请求失败**（它已经在另一个线程），
        于是客户端会永远看到 `running`。那是最糟的一种失败：
        **服务端知道出事了，客户端不知道。**
        """
        def _wrap() -> None:
            run.state = RunState.RUNNING
            run.emit("开始")
            try:
                # ⚠️ **把返回值当作结果** —— 而不是要求调用方在 `fn` 里
                # 自己 `run.result = ...`。
                #
                # 早期版本要求后者，于是路由层写了
                # `lambda r: r.__setattr__("result", handler(...))` ——
                # 那种写法**绕过了本方法的机制**，且一旦 handler 抛异常，
                # `__setattr__` 根本不会执行，结果永远是 None。
                out = fn(run)
                if out is not None:
                    run.result = out
                if run.state is RunState.RUNNING:
                    run.state = RunState.SUCCEEDED
                run.emit("完成", state=run.state)
            except Exception as e:                          # noqa: BLE001
                run.state = RunState.FAILED
                run.error = f"{type(e).__name__}: {e}"
                run.emit("失败", error=run.error)
                # 栈留在服务端日志，**不进响应**（可能含路径等内部信息）
                traceback.print_exc()
            finally:
                run.finished_at = time.time()

        threading.Thread(target=_wrap, daemon=True,
                         name=f"run-{run.run_id}").start()


# ---------------------------------------------------------------------------
# 业务处理
# ---------------------------------------------------------------------------

class _Handlers:
    """把 HTTP 请求映射到各层的调用。

    **本类不含业务规则** —— 与 CLI 同理：
    规则若写在这里，就永远不会被测到（测试测的是层）。
    """

    @staticmethod
    def healthz() -> dict[str, Any]:
        """存活检查。**刻意不碰外部服务** ——
        它回答的是"这个进程还活着吗"，不是"依赖都好吗"。
        把依赖检查混进来，会让"服务挂了"与"ComfyUI 没起"无法区分。
        """
        from little_crew.version import VERSION

        return {"ok": True, "version": VERSION}

    @staticmethod
    def env(body: Mapping[str, Any]) -> dict[str, Any]:
        from little_crew.config import describe, env_value

        keys = ("COMFYUI_ENDPOINT", "LOCAL_GEN_ENDPOINT", "LLM_API_KEY",
                "VLM_API_KEY")
        return {"config": describe(keys),
                "comfyui": env_value("COMFYUI_ENDPOINT", "") or "(默认)",
                "localGen": env_value("LOCAL_GEN_ENDPOINT", "") or "(默认)"}

    @staticmethod
    def compile(body: Mapping[str, Any]) -> dict[str, Any]:
        from little_crew.capability.capability import probe_from_object_info
        from little_crew.compiler.compiler import ComfyCompiler, CompileError
        from little_crew.config import env_value
        from little_crew.models.spec import Backend, BackendKind
        from little_crew.runtime.channels.comfy import ComfyChannel

        ir = body.get("ir")
        if not isinstance(ir, Mapping):
            # 与编译失败同样**对称地**返回 ok:false（而不是抛异常变成 500）
            return {"ok": False, "error": "请求体缺少 `ir`（对象）"}

        ep = (env_value("COMFYUI_ENDPOINT", "")
              or "http://127.0.0.1:8188").rstrip("/")
        ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=ep,
                                  is_local_by_default=True),
                          endpoint=ep, timeout_s=120)
        ok, why = ch.available()
        if not ok:
            raise RuntimeError(f"ComfyUI 不可用：{why}")

        m = probe_from_object_info(ch.probe_catalog(), endpoint=ep,
                                   comfy_version="unknown")
        try:
            res = ComfyCompiler(m).compile(ir)
        except (CompileError, ValueError, KeyError, TypeError) as e:
            # **编译失败是业务结果，不是服务器错误** ——
            # 调用方给了一个编不过的 IR，那是 4xx 语义。
            #
            # 捕获范围刻意宽（`CompileError` / `ValueError` / `KeyError` /
            # `TypeError`）：IR 不合法的表现有好几种 ——
            # 缺必填字段走 `canonicalize` 抛 `ValueError`，
            # 结构问题走编译器抛 `CompileError`。
            # **它们对调用方是同一件事**（"你给的 IR 有问题"），
            # 所以必须统一呈现；漏掉一种就会变成 500，
            # 让客户端把"我的输入错了"读成"服务端挂了"。
            #
            # ⚠️ 失败响应也带 `ok: False` —— 与成功响应**对称**。
            # 不对称的代价：客户端要写 `if resp.get("ok", True) and "error" not in resp`
            # 这种"猜"的逻辑，而少写一个分支就变成"把失败当成功"。
            return {"ok": False, "error": str(e),
                    "hint": "检查 IR 的必填字段与节点类型；"
                            "`POST /v1/compile` 只编译不提交，可安全反复试"}
        return {"ok": True, "graphHash": res.graph_hash,
                "irHash": res.ir_hash, "nodeCount": len(res.graph),
                "notices": [n.as_dict() for n in res.notices],
                "graph": res.graph}

    @staticmethod
    def episode(body: Mapping[str, Any], run: Run) -> dict[str, Any]:
        """跑一集。**同步执行，由 `RunRegistry.start` 放进后台线程。**"""
        from little_crew.agent.crew_stubs import default_crew_stubs
        from little_crew.agent.orchestrator import CrewOrchestrator
        from little_crew.config import env_value
        from little_crew.models.spec import Backend, BackendKind
        from little_crew.runtime.cache import ContentCache
        from little_crew.runtime.cached_render import (CachingRenderer,
                                                       artifact_fetcher_for)
        from little_crew.runtime.checkpoint import RenderCheckpoint
        from little_crew.runtime.channels.http import HttpChannel
        from little_crew.runtime.episode import EpisodeRunner
        from little_crew.runtime.render import RenderSession
        from pathlib import Path

        source = str(body.get("script") or "").strip()
        if not source:
            raise ValueError("请求体缺少 `script`")
        limit = int(body.get("limit") or 0) or None
        steps = int(body.get("steps") or 8)
        seed = int(body.get("seed") or 7)
        gate = bool(body.get("gate", True))
        out = Path(str(body.get("out") or "outputs/api"))
        out.mkdir(parents=True, exist_ok=True)

        # ⚠️ **从注册表取后端声明，不要在这里另写一份。**
        #
        # 手写 `body_template` 会与 `EpisodeRunner._values()` 实际发的字段
        # 漂移（实测：模板少写 `negative_prompt` -> local-gen 回 **HTTP 422**）。
        # 那类缺陷的表现是"服务端说参数不对，可参数看着都对" ——
        # 因为不对的那个参数根本不在你写的那份模板里。
        from little_crew.models.registry import build_default_registry

        spec = build_default_registry().get("z-image-turbo")
        be = (next((b for b in spec.backends
                    if b.kind is BackendKind.HTTP), None)
              if spec is not None else None)
        if be is None:
            raise RuntimeError("模型注册表里没有 z-image-turbo 的 HTTP 后端")

        ch = HttpChannel(be, timeout_s=1800)
        ok, why = ch.available()
        if not ok:
            raise RuntimeError(f"local-gen 不可用：{why}")

        renderer = CachingRenderer(
            RenderSession(ch, None), ContentCache(out / "cache"),
            fetcher=artifact_fetcher_for(ch),
            checkpoint=RenderCheckpoint(out / "checkpoint",
                                        episode_id="API"))

        orch = CrewOrchestrator(max_rounds=3, model_families={})
        orch.bind_many(default_crew_stubs())

        class _R(EpisodeRunner):
            def engine_backend(self) -> Any:
                return be

        runner = _R(orch, renderer, episode_id="API", title="API 一集")
        runner.workdir = out
        runner.engine_steps = steps
        runner.engine_seed = seed
        runner.verify_artifacts = gate
        runner.assemble_video = False       # API 默认只出图（视频慢）

        run.emit("剧本与分镜")
        res = runner.run(source, limit=limit,
                         on_progress=lambda *a, **k: run.emit("渲染中"))
        run.emit("完成", ok=res.ok)
        return res.as_dict()


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------

class ApiServer:
    """把 `RunRegistry` 与 `_Handlers` 组装成 HTTP 服务。"""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8787) -> None:
        self.host = host
        self.port = port
        self.runs = RunRegistry()
        self.handlers = _Handlers()

    # ---- 路由 ----

    def route(self, method: str, path: str,
              body: Mapping[str, Any]) -> tuple[int, Any]:
        """返回 `(status, payload)`。

        ⚠️ **默认绑 `127.0.0.1`**：本服务没有鉴权（见 README 的已知限制），
        绑到 `0.0.0.0` 等于把无人看守的 GPU 提交口暴露到局域网。
        """
        parts = [p for p in path.split("/") if p]

        if method == "GET" and parts == ["healthz"]:
            return 200, self.handlers.healthz()

        if method == "GET" and parts == ["v1", "env"]:
            return 200, self.handlers.env(body)

        if method == "POST" and parts == ["v1", "compile"]:
            out = self.handlers.compile(body)
            # 编译失败 -> 400（**客户端给了编不过的 IR**，不是服务器故障）
            return (200 if out.get("ok") else 400), out

        if method == "POST" and parts == ["v1", "episode"]:
            run = self.runs.create("episode")
            # 直接传函数（返回值会被 `start` 当作结果）——
            # 不要用 lambda 包一层去赋值 `run.result`，那会绕过异常处理机制。
            self.runs.start(run, lambda r: self.handlers.episode(body, r))
            return 202, {"runId": run.run_id, "state": run.state}

        if method == "GET" and parts == ["v1", "runs"]:
            return 200, {"runs": [r.as_dict() for r in self.runs.all()]}

        if (method == "GET" and len(parts) == 3 and parts[:2] == ["v1", "runs"]):
            r = self.runs.get(parts[2])
            if r is None:
                return 404, {"error": f"未知 runId {parts[2]!r}"}
            return 200, r.as_dict()

        return 404, {"error": f"未知路径 {method} {path}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "LittleCrew/1"
    protocol_version = "HTTP/1.1"

    #: 由 `serve()` 注入
    api: ApiServer

    def log_message(self, fmt: str, *args: Any) -> None:      # noqa: A003
        # 默认实现往 stderr 写，格式与平台日志不一致。压掉，交调用方决定。
        pass

    # ---- 工具 ----

    def _read_body(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"请求体不是合法 JSON：{e}") from e
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return obj

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- 方法 ----

    def do_GET(self) -> None:                                 # noqa: N802
        path = urlparse(self.path).path
        # SSE 单独处理（它是长连接，不是一问一答）
        parts = [p for p in path.split("/") if p]
        if (len(parts) == 4 and parts[:2] == ["v1", "runs"]
                and parts[3] == "events"):
            return self._sse(parts[2])
        try:
            status, payload = self.api.route("GET", path, {})
        except Exception as e:                                # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(status, payload)

    def do_POST(self) -> None:                                # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        try:
            status, payload = self.api.route("POST", path, body)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:                                # noqa: BLE001
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        self._send(status, payload)

    def _sse(self, run_id: str) -> None:
        """Server-Sent Events 进度流。

        **标准库实现**：`text/event-stream` 就是"长连接 + 逐条写文本"。
        每条消息形如 `data: {...}\\n\\n`。

        终止条件有三个，缺一不可：
          · 任务进入终态 -> 推完最后一条就结束
          · 客户端断开 -> 写失败即退出（**必须捕获**，否则线程泄漏）
          · 兜底超时 -> 防止任务卡死导致连接永久占用
        """
        r = self.api.runs.get(run_id)
        if r is None:
            return self._send(404, {"error": f"未知 runId {run_id!r}"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        sent = 0
        deadline = time.time() + 3600
        try:
            while time.time() < deadline:
                while sent < len(r.events):
                    msg = json.dumps(r.events[sent], ensure_ascii=False)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    sent += 1
                if r.state in RunState.TERMINAL:
                    self.wfile.write(
                        f"data: {json.dumps({'state': r.state}, ensure_ascii=False)}\n\n"
                        .encode("utf-8"))
                    self.wfile.flush()
                    return
                time.sleep(0.3)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端走了 —— **正常情况，不是错误**。静默退出即可。
            return


def serve(*, host: str = "127.0.0.1", port: int = 8787,
          api: ApiServer | None = None) -> ThreadingHTTPServer:
    """起服务并返回 server 对象（**已在后台线程里 serve**）。

    调用方负责 `server.shutdown()`。
    """
    a = api or ApiServer(host=host, port=port)

    class _H(_Handler):
        pass

    _H.api = a
    httpd = ThreadingHTTPServer((host, port), _H)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="api-serve").start()
    return httpd
