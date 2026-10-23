"""ComfyUI 通道 —— 本地节点图执行的实现。

与 HTTP 通道的关系
------------------
两条通道满足**同一个** ``Channel`` 协议，因此上层（编排层/验收层）
不需要 ``isinstance`` 判断这次跑的是本地还是云端。

真正的差异只有一处：**任务形态不同**。
  HTTP 通道收 ``CompiledJob.request``（method/path/body）
  Comfy 通道收 ``CompiledJob.graph``（节点图），需要走"提交 → 排队 → 执行 → 回收"

因此本类多出 HTTP 通道没有的两个机制：

  1. **WebSocket 进度**：ComfyUI 通过 ``/ws`` 广播 ``executing`` / ``progress``
     事件。这是唯一能得到**细粒度进度**（当前第几帧/第几步）的途径。
  2. **历史对账**：``/history/{prompt_id}`` 使崩溃重启后能回收已完成的任务 ——
     这是"4 小时长跑必须能续"的物理基础（契约 R3）。

缓存语义（源码级核实，见 research/comfyui-internals/）
----------------------------------------------------
ComfyUI 自身的节点级缓存在**提交之前**就生效：命中缓存的节点不会出现在
``execution_cached`` 之后的执行流里。因此：

  **"命中"与"实算"必须分开统计**，否则我们会把 ComfyUI 的缓存收益
  误记为自己的三级缓存收益 —— 指标虚高且无法解释。

这正是 ``execution_cached`` 消息要单独处理的原因。

零第三方依赖
------------
WebSocket 用标准库 ``socket`` + 手写握手（RFC 6455 子集）。
理由同 HTTP 通道：核心层保持零依赖。ComfyUI 的 WS 只推文本帧，实现量可控。

未实现部分**显式抛 NotImplementedError** 而非静默返回空 ——
静默的空值会让上游以为成功。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlparse

from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.channels.base import (
    ArtifactRef,
    ChannelCapabilities,
    ChannelError,
    CompiledJob,
    JobHandle,
    JobStatus,
    ProgressEvent,
)

__all__ = ["ComfyChannel", "ComfyEvent", "CACHE_HIT_NODE_KINDS"]


#: ComfyUI 广播的 ``execution_cached`` 消息里列出的节点 = **命中缓存**，未实算。
#: 我们把它单独统计，**不与"实算节点"混为一谈**。
CACHE_HIT_MESSAGE = "execution_cached"


@dataclass
class ComfyEvent:
    """一条来自 ``/ws`` 的事件。"""

    type: str
    data: Mapping[str, Any] = field(default_factory=dict)
    received_at: float = 0.0

    @property
    def is_cache_hit(self) -> bool:
        return self.type == CACHE_HIT_MESSAGE


class ComfyChannel:
    """ComfyUI 本地通道。"""

    def __init__(
        self,
        backend: Backend,
        *,
        endpoint: str = "",
        client_id: str = "",
        timeout_s: int = 3600,
        poll_interval_s: float = 1.0,
        channel_id: str = "",
        #: 单次 ``/history`` 查询的超时（秒）。
        #:
        #: **不能太小**：渲染期间 ComfyUI 的 HTTP 服务会被 GPU 重活拖慢，
        #: 查询超时会被当成"任务状态未知"，而状态未知**不可重提**
        #: —— 结果是"服务端明明出了图，平台却判失败"。
        #: 默认 180s：远大于任何单镜渲染时间，又不至于让真挂死的作业无限等待。
        poll_timeout_s: int = 180,
    ) -> None:
        if backend.kind is not BackendKind.COMFYUI:
            raise ValueError(
                f"ComfyChannel 只接受 COMFYUI 通道，收到 {backend.kind.value!r}")
        self.backend = backend
        self.endpoint = (endpoint or backend.endpoint or "http://127.0.0.1:8188").rstrip("/")
        #: ComfyUI 用 client_id 把 WS 事件与提交关联。必须与提交时携带的一致。
        self.client_id = client_id or uuid.uuid4().hex
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        #: 单次 /history 查询的超时（秒）。见构造参数处的说明 ——
        #: 太小会把"服务端正忙"误判成"任务状态未知"，而未知**不可重提**。
        self.poll_timeout_s = int(poll_timeout_s)
        self.channel_id = channel_id or f"comfy:{self.endpoint}"
        #: prompt_id -> 幂等键，用于 R1 与 R3
        self._by_key: dict[str, str] = {}
        self._by_prompt: dict[str, JobHandle] = {}

    # ---- 能力自述 ------------------------------------------------------

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            kind=BackendKind.COMFYUI,
            progress=True,          # /ws 提供细粒度进度
            cancellable=True,       # /interrupt 与 /queue 删除
            reconcilable=True,      # /history 支持崩溃后对账
            needs_gpu=True,         # **必须纳入 GPU 调度**
            metered=False,          # 本地电费，不按次计费
            notes="本地节点图执行。同时只能加载一个模型，必须按模型分批调度。",
        )

    # ---- 可用性 --------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        """探测 ``/system_stats``。**不抛异常**。"""
        try:
            raw = self._get_json("/system_stats", timeout=10)
        except ChannelError as e:
            return False, f"无法连接 {self.endpoint}：{e.message}"
        ver = str((raw.get("system") or {}).get("comfyui_version") or "")
        if not ver:
            return False, f"{self.endpoint} 有响应但不是 ComfyUI（缺 comfyui_version）"
        return True, f"ComfyUI {ver}"

    def probe_catalog(self, *, declared_hooks: Mapping[str, Any] | None = None
                      ) -> dict[str, Any]:
        """拉取 ``/object_info`` 原始响应。

        这是**能力矩阵的唯一来源**。本方法只负责取，解析交给 ``capability`` 层 ——
        通道层不该含 ComfyUI 节点知识（那是 ``capability`` 的职责）。
        """
        return self._get_json("/object_info", timeout=180)

    # ---- R1 提交 -------------------------------------------------------

    def submit(self, job: CompiledJob) -> JobHandle:
        if job.kind is not BackendKind.COMFYUI:
            raise ChannelError("F-SUBMIT-COMFY",
                               f"ComfyChannel 不能提交 {job.kind.value!r} 形态的任务")
        if not job.idempotency_key:
            raise ChannelError("F-SUBMIT-VALID",
                               "ComfyUI 通道提交必须携带 idempotency_key")
        if not job.graph:
            raise ChannelError("F-SUBMIT-VALID", "ComfyUI 任务缺少 graph 字段")

        # R1：同一幂等键已提交过则返回既有句柄（避免浪费 GPU 时间）
        if job.idempotency_key in self._by_key:
            pid = self._by_key[job.idempotency_key]
            existing = self._by_prompt.get(pid)
            if existing is not None:
                return existing

        ok, why = self.available()
        if not ok:
            h = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.FAILED)
            h.error = ChannelError("F-SUBMIT-COMFY", why, retryable=True)
            return h

        payload = {
            "prompt": job.graph,
            "client_id": self.client_id,
        }
        try:
            raw = self._post_json("/prompt", payload, timeout=60)
        except ChannelError as e:
            h = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.FAILED)
            h.error = e
            return h

        # ComfyUI 的节点校验失败会以 200 + node_errors 返回，**不是 HTTP 错误**。
        # 必须显式检查，否则会把"提交被拒"当成"提交成功"。
        node_errors = raw.get("node_errors") or {}
        if node_errors:
            h = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.FAILED)
            h.error = ChannelError(
                "F-SUBMIT-COMFY",
                f"ComfyUI 拒绝该图：{len(node_errors)} 个节点有误",
                retryable=False,
                detail={"nodeErrors": node_errors},
            )
            h.raw = raw
            return h

        prompt_id = str(raw.get("prompt_id") or "")
        if not prompt_id:
            h = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.FAILED)
            h.error = ChannelError("F-SUBMIT-COMFY", "ComfyUI 未返回 prompt_id",
                                   detail={"raw": raw})
            return h

        handle = JobHandle(job_id=job.job_id, channel_id=prompt_id,
                           status=JobStatus.PENDING)
        handle.raw = {"queued": raw}
        self._by_key[job.idempotency_key] = prompt_id
        self._by_prompt[prompt_id] = handle
        return handle

    # ---- R4 进度 -------------------------------------------------------

    def poll(self, handle: JobHandle) -> JobHandle:
        """查 ``/history/{prompt_id}``。

        ⚠️ **超时必须给足。** 这里曾经用 30s —— 理由是"查历史很快"。
        但那只在 ComfyUI 空闲时成立：**渲染期间它的 HTTP 服务会被 GPU 重活拖住**，
        实测带 ControlNet 的镜头（每镜约 16s）会让 `/history` 查询超过 30s。

        后果不是"慢"，而是**误判**：查询超时 -> `F-TIMEOUT` ->
        状态标记为 `UNKNOWN` -> 平台按"不猜"原则**判该镜失败且不重提**
        （这是对的，因为可能有活着的作业）。表现为
        **"ComfyUI 历史里明明有产物，平台却报失败"**。

        `poll_timeout_s` 之所以可配：不同机器的渲染耗时差好几倍，
        一个写死的值不可能对所有环境都安全。
        """
        if handle.status.terminal:
            return handle
        try:
            hist = self._get_json(f"/history/{handle.channel_id}",
                                  timeout=self.poll_timeout_s)
        except ChannelError as e:
            # 查询失败**不等于任务失败** —— 状态标记为 UNKNOWN，交由对账处理。
            # 这是关键区分：网络抖动不该让一次已完成的渲染被判定为失败。
            handle.status = JobStatus.UNKNOWN
            handle.error = e
            return handle

        entry = hist.get(handle.channel_id)
        if not entry:
            # 不在历史里 = 仍在队列或执行中
            if handle.status is JobStatus.PENDING:
                handle.status = JobStatus.RUNNING
            return handle

        status = entry.get("status") or {}
        if status.get("status_str") == "error" or status.get("completed") is False:
            handle.status = JobStatus.FAILED
            handle.error = ChannelError(
                "F-EXEC-COMFY",
                "ComfyUI 执行失败",
                detail={"messages": status.get("messages")},
            )
            return handle

        self._absorb_outputs(handle, entry.get("outputs") or {})
        handle.status = JobStatus.SUCCEEDED
        return handle

    def watch(self, handle: JobHandle) -> Iterator[ProgressEvent]:
        """通过 ``/ws`` 流式上报进度。**保证终止**。

        ComfyUI 的关键消息：
          ``executing``        当前正在执行哪个节点（node=None 表示整图完成）
          ``progress``         采样步进度（value/max）
          ``execution_cached`` **命中缓存的节点列表 —— 与实算分开统计**

        ⚠️ **一个真实存在的竞态**（端到端验证时暴露）：
        `executing(node=None)` 是"整图完成"的唯一事件，但它**只发一次**且
        **不重放**。如果订阅生效晚于任务完成（本地小图 12 秒就能跑完），
        这个事件永远收不到 —— 早期实现会一直等到 1 小时超时。

        修复方式：**周期性用 `/history` 交叉验证**。只要历史里出现了终态，
        立刻上报并退出，不再依赖那个可能已经错过的 WS 事件。
        退出条件变成"**观察到终态**"而非"**收到某个事件**"，
        后者是不可靠的。

        另加一个**空闲上限**：连续 `_WS_IDLE_LIMIT` 次轮询都没拿到新事件且任务
        仍未终结，说明这个 WS 订阅没有价值，退出并交由调用方轮询。
        """
        try:
            ws = _WebSocket(self.endpoint, f"/ws?clientId={self.client_id}",
                            timeout=self.timeout_s)
        except ChannelError as e:
            # WS 不可用时退化为轮询，而不是让整次执行失败
            yield ProgressEvent(0, 1, "item", f"WS 不可用（{e.message}），退化为轮询")
            yield from self._watch_by_polling(handle)
            return

        deadline = time.time() + self.timeout_s
        idle = 0
        try:
            ws_msgs = ws.messages(deadline=deadline)
            while True:
                try:
                    raw = next(ws_msgs)
                except StopIteration:
                    # WS 流本身结束（对端关闭或到 deadline）
                    break

                ev = _parse_event(raw)
                if ev is not None:
                    idle = 0
                    if ev.type == CACHE_HIT_MESSAGE:
                        nodes = ev.data.get("nodes") or []
                        # 单独上报：这是 ComfyUI 的缓存，**不是我们的三级缓存收益**
                        yield ProgressEvent(0, 0, "cache_hit",
                                            f"ComfyUI 缓存命中 {len(nodes)} 个节点",
                                            node_id=",".join(map(str, nodes))[:200])
                        continue
                    if ev.type == "progress":
                        yield ProgressEvent(int(ev.data.get("value", 0)),
                                            int(ev.data.get("max", 1)), "step")
                        continue
                    if ev.type == "executing":
                        node = ev.data.get("node")
                        if node is None:
                            yield ProgressEvent(1, 1, "item", "执行完成")
                            return
                        yield ProgressEvent(0, 0, "node", f"执行节点 {node}",
                                            node_id=str(node))
                        continue
                    if ev.type in ("execution_error", "execution_interrupted"):
                        yield ProgressEvent(
                            0, 1, "item",
                            f"{ev.type}: {ev.data.get('exception_message', '')}")
                        return
                else:
                    idle += 1

                # ---- 交叉验证：不依赖可能已错过的完成事件 ----
                probed = self.poll(handle)
                if probed.status.terminal:
                    yield ProgressEvent(1, 1, "item",
                                        f"已终结（{probed.status.value}）")
                    return
                if idle >= self._WS_IDLE_LIMIT:
                    yield ProgressEvent(
                        0, 0, "poll",
                        f"WS 连续 {idle} 次无新事件，交回调用方轮询")
                    return
        finally:
            ws.close()

    #: 连续多少次轮询无新事件就放弃 WS（每次间隔 `poll_interval_s`）
    _WS_IDLE_LIMIT = 6

    def _watch_by_polling(self, handle: JobHandle) -> Iterator[ProgressEvent]:
        while True:
            handle = self.poll(handle)
            yield ProgressEvent(0, 0, "poll", f"状态 {handle.status.value}")
            if handle.status.terminal:
                return
            time.sleep(self.poll_interval_s)

    # ---- 回收 ----------------------------------------------------------

    def collect(self, handle: JobHandle) -> list[ArtifactRef]:
        if not handle.status.terminal:
            raise ChannelError("F-EXEC-COMFY",
                               f"任务 {handle.job_id} 尚未终结（{handle.status.value}）")
        return list(handle.artifacts)

    # ---- R2 中断 -------------------------------------------------------

    def cancel(self, handle: JobHandle) -> None:
        """先尝试从队列删除；若已在执行则发中断。"""
        try:
            self._post_json("/queue", {"delete": [handle.channel_id]}, timeout=15)
        except ChannelError:
            pass
        try:
            self._post_json("/interrupt", {}, timeout=15)
        except ChannelError as e:
            raise ChannelError("F-EXEC-COMFY",
                               f"中断失败: {e.message}", retryable=True) from e
        handle.status = JobStatus.CANCELLED

    # ---- R3 对账 -------------------------------------------------------

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        """按 ``prompt_id`` 从服务端 ``/history`` 回收任务。**绝不重复提交。**

        ⚠️ 这里的 ``job_ids`` 是 **prompt_id**，不是幂等键 ——
        因为 ``/history`` 里没有幂等键，只有 prompt_id。
        "幂等键 -> prompt_id" 的映射由 ``runtime.checkpoint`` 持久化。

        早期版本按幂等键查，且只看**进程内**的 ``self._by_key`` ——
        崩溃重启后那个映射是空的，于是对账**必然失败**，
        而失败的表现是"查不到 → 重新提交"，也就是**重复烧 GPU**。
        这正是对账机制要防止的事情，却由它自己引入了。

        改为查服务端 ``/history``（它跨进程持久），本方法才真正可续跑。
        单个任务查不到**不报错** —— 那正是"还没提交成功"的正常状态。

        ⚠️ **但查询本身失败必须抛出**（2026-10-23 审计 H-16）。
        早期这里 ``except ChannelError: return {}`` —— 把「查询失败」压成
        和「服务端确实没有这个任务」同一个空字典，于是调用方
        （``checkpoint.reconcile_now``）一律判"服务端无此任务，视为未提交"
        -> 在途任务被放进 todo -> **续跑时重复提交、重复烧 GPU**。
        而 checkpoint 那边其实写好了「对账本身失败 —— **不能猜**」的分支，
        只因通道选择返回空字典，那句防线**永远用不上**。
        """
        wanted = {str(j) for j in job_ids if j}
        if not wanted:
            return {}

        out: dict[str, JobHandle] = {}
        hist = self._get_json("/history")          # 查询失败 -> 抛出，交给调用方
        if not isinstance(hist, dict):
            raise ChannelError(
                "F-EXEC-COMFY",
                f"/history 返回的不是对象（{type(hist).__name__}）—— "
                f"无法判断任务在不在，不能假定它们没提交过",
                retryable=True,
            )

        for pid in wanted:
            entry = hist.get(pid)
            if not isinstance(entry, Mapping):
                continue
            out[pid] = self._handle_from_history(pid, entry)
        return out

    def _handle_from_history(self, prompt_id: str,
                             entry: Mapping[str, Any]) -> JobHandle:
        """把 ``/history`` 的一条记录还原成句柄。

        ComfyUI 的状态字段有两处，且**都要看**：
          · ``status.completed`` 是布尔
          · ``status.status_str`` 是 ``success`` / ``error``
        只看其中一个会在"完成了但报错"时判错。
        """
        status = entry.get("status")
        status = status if isinstance(status, Mapping) else {}
        completed = bool(status.get("completed"))
        sstr = str(status.get("status_str") or "")

        if completed and sstr == "success":
            st = JobStatus.SUCCEEDED
        elif completed:
            st = JobStatus.FAILED
        elif status:
            st = JobStatus.RUNNING
        else:
            st = JobStatus.UNKNOWN

        h = JobHandle(job_id=prompt_id, channel_id=prompt_id, status=st)
        h.raw = {"history": entry}
        if st is JobStatus.SUCCEEDED:
            self._absorb_outputs(h, entry.get("outputs") or {})
            self._by_prompt[prompt_id] = h
        elif st is JobStatus.FAILED:
            h.error = ChannelError(
                "F-REPLAY-FAILED",
                f"服务端历史显示该任务失败（{sstr or 'unknown'}）",
                retryable=False, detail={"promptId": prompt_id})
        return h

    # ---- 内部 ----------------------------------------------------------

    def _absorb_outputs(self, handle: JobHandle,
                        outputs: Mapping[str, Any]) -> None:
        """从 ``/history`` 的 outputs 抽产物引用。

        产物只记**文件名**（ComfyUI 的 ``/view`` 接口凭文件名取），
        真正的下载、哈希、落库由 ``runtime.cache`` 负责 —— 通道层不做存储决策。
        """
        for node_id, out in outputs.items():
            for key in ("images", "gifs", "videos", "audio"):
                for item in (out.get(key) or []):
                    if not isinstance(item, Mapping):
                        continue
                    fn = item.get("filename")
                    if not fn:
                        continue
                    kind = {"images": "image", "gifs": "video",
                            "videos": "video", "audio": "audio"}[key]
                    handle.artifacts.append(ArtifactRef(
                        kind=kind,
                        uri=f"/view?filename={fn}"
                            f"&subfolder={item.get('subfolder', '')}"
                            f"&type={item.get('type', 'output')}",
                        extra={"filename": fn,
                               "subfolder": item.get("subfolder", ""),
                               "sourceNode": node_id},
                    ))

    def _get_json(self, path: str, timeout: int = 30) -> dict[str, Any]:
        return self._request_json("GET", path, None, timeout)

    def _post_json(self, path: str, body: Any, timeout: int = 30) -> dict[str, Any]:
        return self._request_json("POST", path, body, timeout)

    def _request_json(self, method: str, path: str, body: Any,
                      timeout: int) -> dict[str, Any]:
        url = self.endpoint + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:600]
            except Exception:  # noqa: BLE001
                pass
            raise ChannelError("F-SUBMIT-COMFY", f"HTTP {e.code} {e.reason}",
                               retryable=(e.code == 429 or 500 <= e.code < 600),
                               detail={"url": url, "body": detail}) from e
        except Exception as e:  # noqa: BLE001
            raise ChannelError(
                "F-TIMEOUT" if "timed out" in str(e).lower() else "F-SUBMIT-COMFY",
                f"{type(e).__name__}: {e}", retryable=True, detail={"url": url},
            ) from e
        if not payload.strip():
            return {}
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ChannelError("F-SUBMIT-COMFY", f"响应不是合法 JSON: {e}",
                               detail={"url": url}) from e
        return obj if isinstance(obj, dict) else {"_value": obj}


# ==========================================================================
# WebSocket（RFC 6455 子集，仅文本帧）
# ==========================================================================

class _WebSocket:
    """最小 WebSocket 客户端。

    只实现 ComfyUI ``/ws`` 需要的部分：文本帧、无分片、无扩展、无压缩。
    **不实现二进制帧与大消息分片** —— 需要时显式抛错而非静默截断。
    """

    def __init__(self, endpoint: str, path: str, timeout: int = 60) -> None:
        u = urlparse(endpoint)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        self.timeout = timeout
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise ChannelError("F-SUBMIT-COMFY",
                               f"无法连接 WS {host}:{port}: {e}", retryable=True) from e
        self._handshake(host, port, path)

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ChannelError("F-SUBMIT-COMFY", "WS 握手期间连接被关闭")
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        if "101" not in head.split("\r\n")[0]:
            raise ChannelError("F-SUBMIT-COMFY",
                               f"WS 握手被拒: {head.splitlines()[0] if head else '空响应'}")
        expect = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")
                         .encode("ascii")).digest()).decode("ascii")
        if expect.lower() not in head.lower():
            raise ChannelError("F-SUBMIT-COMFY", "WS 握手响应缺少正确的 Sec-WebSocket-Accept")

    def messages(self, deadline: float | None = None) -> Iterator[str]:
        """逐条产出文本消息。超时或对端关闭即结束。"""
        while True:
            if deadline is not None:
                remain = deadline - time.time()
                if remain <= 0:
                    return
                self.sock.settimeout(min(remain, 5.0))
            try:
                frame = self._read_frame()
            except (socket.timeout, TimeoutError):
                continue          # 无消息不是错误，继续等
            except OSError:
                return
            if frame is None:
                return
            opcode, payload = frame
            if opcode == 0x8:      # close
                return
            if opcode == 0x1:      # text
                yield payload.decode("utf-8", errors="replace")
            elif opcode == 0x2:    # binary —— ComfyUI 用它推预览图，我们不需要
                continue
            elif opcode == 0x9:    # ping -> pong
                self._send_pong(payload)
            # 0x0 续帧：本实现不支持分片，忽略（ComfyUI 的事件都很短）

    def _read_frame(self) -> tuple[int, bytes] | None:
        hdr = self._recv_exact(2)
        if hdr is None:
            return None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = self._recv_exact(2)
            if ext is None:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8)
            if ext is None:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = self._recv_exact(4) if masked else None
        if masked and mask is None:
            return None
        data = self._recv_exact(length) if length else b""
        if data is None:
            return None
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data

    def _send_pong(self, payload: bytes) -> None:
        try:
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)
        except OSError:
            pass

    def _recv_exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _parse_event(raw: str) -> ComfyEvent | None:
    """ComfyUI 的 WS 消息形如 ``{"type": "...", "data": {...}}``。"""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or "type" not in obj:
        return None
    return ComfyEvent(type=str(obj["type"]),
                      data=obj.get("data") or {},
                      received_at=time.time())
