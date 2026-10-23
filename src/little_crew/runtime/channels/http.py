"""HTTP 通道 —— 直连 HTTP 接口的执行通道。

**这是"本地 / API 双通道"的统一实现。**
``D:\\AI\\local-gen`` 暴露的是与云端等价的 OpenAI 兼容接口，
所以本地服务与云端 API 走**同一段代码**，只是 endpoint 与凭据不同。

零第三方依赖
------------
用标准库 ``urllib.request`` 而非 requests/httpx。
理由：本平台核心层刻意保持零依赖（见 ARCHITECTURE.md「依赖铁律」），
而 HTTP 通道的请求都是**短请求 + JSON**，标准库完全够用。
将来若需连接池/HTTP2，可在 ``runtime`` 层引入 httpx 并替换本类，
**Channel 协议不变**。

实现的契约条款
--------------
R1 提交幂等    ``idempotency_key`` 命中既有记录时直接返回，**不重复请求** ——
               云端通道重复请求就是重复计费。
R2 可中断      同步请求无法中断（返回 False + 原因），异步任务式可发取消请求。
R4 进度可观测  同步请求只能报"开始/完成"；异步任务式按轮询次数报进度。

R3（对账）需要持久化，本类只做进程内记录；持久化由 ``runtime.checkpoint`` 负责。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Sequence

from little_crew.models.spec import Backend, BackendKind
#: 模板渲染与响应取值**已下移到 `models` 层**（契约的实现在契约旁边）——
#: 因为 `agent` 层也要用它们，而 `agent` 不能 import `runtime`。
#: 这里**再导出**，既有调用方（`render.py`、测试）无需改动。
from little_crew.models.template import (
    TemplateError,
    extract_path,
    render_body_template as _render_body_template,
)
from little_crew.runtime.channels.base import (
    ArtifactRef,
    ChannelCapabilities,
    ChannelError,
    CompiledJob,
    JobHandle,
    JobStatus,
    ProgressEvent,
)

__all__ = ["HttpChannel", "render_body_template", "extract_path"]


# ==========================================================================
# 模板与取值工具 —— **已下移到 models.template**
# ==========================================================================
#
# ⚠️ 这里**不再定义**它们，只把 `TemplateError` 翻译成通道层的
# `ChannelError`，让既有调用方看到的行为与从前一致。
#
# 为什么下移：`agent` 层接 LLM 时也要渲染请求体、按路径取响应，
# 而 `agent`（层 6）**不能** import `runtime`（层 8）。
# **正确的反应不是放宽守卫，而是把东西放到对的地方** ——
# **正确的反应不是放宽守卫，而是把东西放到对的地方** ——
# 它们实现的是 `Backend.body_template` / `response_path` 的语义，
# 而 `Backend` 定义在 `models`：**契约的实现在契约旁边**。
# 而 Backend 定义在 models：**契约的实现在契约旁边**。


def render_body_template(template: str,
                         values: Mapping[str, Any]) -> dict[str, Any]:
    """见 :func:little_crew.models.template.render_body_template。

    本包装只做一件事：把 `TemplateError` 转成 `ChannelError`，
    保持通道层的错误体系单一（调用方只认 `ChannelError`）。
    """
    try:
        return _render_body_template(template, values)
    except TemplateError as e:
        raise ChannelError(e.code, e.message, detail=e.detail) from e


# 通道
# ==========================================================================

#: 通用失败状态取值（小写）。后端可用 `Backend.status_failure_values` 追加。
#: `fail` 单独列出来是**有代价的记忆**：MiniMax 官方用的是 `Fail`，
#: 缺了它，一个明确失败的任务会被当成"还在跑"空转到超时（审计 H-12）。
_DEFAULT_FAILURE_VALUES: frozenset[str] = frozenset({
    "failed", "fail", "error", "canceled", "cancelled", "timeout", "expired",
})

@dataclass
class _Record:
    """一次提交的落账。用于 R1 幂等。"""

    idempotency_key: str
    handle: JobHandle
    submitted_at: float
    attempts: int = 0
    #: 请求**确实发出去过**。为 False 表示"登记了但没发请求"（如服务不可用）——
    #: 那种情况下幂等键不能被毒化，否则一次「服务没起」就让这一镜永久提交不了
    #: （审计 M-14）。
    submitted: bool = False


class HttpChannel:
    """直连 HTTP 通道。本地服务与云端 API 共用。"""

    def __init__(
        self,
        backend: Backend,
        *,
        api_key: str = "",
        timeout_s: int = 300,
        poll_interval_s: float = 2.0,
        max_polls: int = 900,
        channel_id: str = "",
    ) -> None:
        if backend.kind is not BackendKind.HTTP:
            raise ValueError(f"HttpChannel 只接受 HTTP 通道，收到 {backend.kind.value!r}")
        self.backend = backend
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self.channel_id = channel_id or f"http:{backend.endpoint}{backend.path}"
        self._records: dict[str, _Record] = {}
        #: 服务端任务 id -> 幂等键。`reconcile` 的键约定是**服务端任务 id**
        #: （审计 H-9：协议说幂等键、comfy 实现用 prompt_id、checkpoint 传
        #: channel_id —— 三套约定互不兼容，于是 HTTP 通道的对账恒失败）。
        self._by_channel: dict[str, str] = {}

    # ---- 能力自述 ------------------------------------------------------

    @property
    def capabilities(self) -> ChannelCapabilities:
        metered = not _is_local(self.backend.endpoint)
        return ChannelCapabilities(
            kind=BackendKind.HTTP,
            progress=self.backend.async_task,     # 同步请求无中间进度
            cancellable=self.backend.async_task,  # 同步请求无法中断
            reconcilable=False,                   # 需持久化，由 checkpoint 层做
            needs_gpu=False,                      # 算力在服务端
            metered=metered,
            notes=("远端 API（按量计费，重复请求会重复计费）" if metered
                   else "本地 HTTP 服务（零成本）"),
        )

    # ---- 可用性 --------------------------------------------------------

    def for_backend(self, backend: Any) -> "HttpChannel":
        """返回**同一套设置、换一个后端**的通道视图。

        ## 为什么需要它（2026-10-18 真机事故）

        `HttpChannel` 在构造时把 `backend` 存进 `self`，而
        `submit/poll/collect/fetch` 全都读 `self.backend.response_path`
        —— **`render_http(values, backend)` 传进来的那个 backend 被忽略了**。

        于是同一条通道只能服务一个后端。而真实管线里一条通道要服务两种：
        **出图**（zimage，`data.0.url`）与**出视频**（H3，`result.url`）。

        后果很隐蔽：第二段拿图像通道去请求视频接口，报
        `响应中取不到 response_path='data.0.url' 指定的值` ——
        **看起来像服务端返回不对，实际是我们问错了地方**。

        所以 `RenderSession.render_http` 在发现 `backend is not self.channel.backend`
        时，用这个方法换一个视图，而不是复用。
        """
        if backend is self.backend:
            return self
        view = type(self)(backend,
                          timeout_s=getattr(self, "timeout_s", 300),
                          poll_interval_s=getattr(self, "poll_interval_s", 0.2))
        # 其余与后端无关的设置照抄 —— 漏抄会让"换了后端就丢了 key/上限"
        for name in ("api_key", "max_polls", "channel_id"):
            if hasattr(self, name) and hasattr(view, name):
                try:
                    setattr(view, name, getattr(self, name))
                except Exception:                             # noqa: BLE001
                    pass
        return view

    def available(self) -> tuple[bool, str]:
        """探测可用性。**不抛异常** —— 失败本身就是要返回的信息。"""
        if not self.backend.path:
            return False, "通道未声明 path"
        if not self.backend.endpoint:
            return False, "通道未声明 endpoint（可在 config/.env 中提供默认值）"
        base = self.backend.endpoint.rstrip("/")
        # 探测根路径或 /health；两者都不响应才算不可用
        for probe in ("/health", "/"):
            try:
                req = urllib.request.Request(base + probe, method="GET")
                with urllib.request.urlopen(req, timeout=5):
                    return True, ""
            except urllib.error.HTTPError:
                # 有 HTTP 响应即说明服务在（4xx/5xx 也算「活着」）
                return True, ""
            except Exception:  # noqa: BLE001 - 探测失败不是错误
                continue
        return False, f"无法连接 {base}（服务未启动或地址不可达）"

    # ---- R1 提交 -------------------------------------------------------

    def submit(self, job: CompiledJob) -> JobHandle:
        """提交。``idempotency_key`` 命中**已受理**的任务时直接返回既有句柄。"""
        if job.kind is not BackendKind.HTTP:
            raise ChannelError("F-SUBMIT-API",
                               f"HttpChannel 不能提交 {job.kind.value!r} 形态的任务")
        if not job.idempotency_key:
            raise ChannelError(
                "F-SUBMIT-VALID",
                "HTTP 通道提交必须携带 idempotency_key —— "
                "远端 API 无幂等键会造成重复计费",
            )
        if job.request is None:
            raise ChannelError("F-SUBMIT-VALID", "HTTP 任务缺少 request 字段")

        # ---- R1：幂等去重（只认**可能已被受理**的记录）----
        #
        # ⚠️ 判据不是"有没有这条记录"，而是"上次那条记录**可能已经在服务端跑起来**了吗"：
        #   · 有 channel_id（服务端给了任务 id）-> 已受理 -> **绝不能重发**
        #   · 状态不是 FAILED -> 已受理 -> 不能重发
        #   · 没有 channel_id 且状态是 FAILED -> **确定没被受理** -> 允许重发
        #
        # 早期版本只看"有没有记录"，而记录是在**发请求之前**就建的 ——
        # 于是「服务没起」这种最常见的可恢复故障会把幂等键**永久毒化**：
        # 之后每次用同一个键重试都直接返回那个 FAILED 句柄，请求根本不再发
        # （审计 M-14）。
        rec = self._records.get(job.idempotency_key)
        if rec is not None and (bool(rec.handle.channel_id)
                                or rec.handle.status is not JobStatus.FAILED):
            rec.attempts += 1
            return rec.handle

        # ---- 环境检查放在建账**之前**：不可用就别落账 ----
        ok, why = self.available()
        if not ok:
            handle = JobHandle(job_id=job.job_id, channel_id="",
                               status=JobStatus.FAILED)
            handle.error = ChannelError("F-SUBMIT-API", why, retryable=True)
            return handle

        handle = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.PENDING)
        rec = _Record(idempotency_key=job.idempotency_key, handle=handle,
                      submitted_at=time.time(), attempts=1)
        self._records[job.idempotency_key] = rec

        # 从这一刻起"请求已经发出"，网络超时也可能**已被受理** -> 记下来
        rec.submitted = True
        try:
            raw = self._request(job, job.request)
        except ChannelError as e:
            handle.status = JobStatus.FAILED
            handle.error = e
            return handle

        handle.raw = raw
        if self.backend.async_task:
        # ⚠️ 也要认 `job_id` —— local-gen 的异步视频接口用的就是它
            # （`{"job_id": "...", "status": "queued", "poll": "..."}`）。
            # 只认 `id`/`task_id` 的话，任务会被判"服务端没给任务 id"
            # 而**每次提交都失败**，看起来像服务不可用。
            task_id = (extract_path(raw, "id")
                       or extract_path(raw, "task_id")
                       or extract_path(raw, "job_id"))
            if not task_id:
                handle.status = JobStatus.FAILED
                handle.error = ChannelError(
                    "F-SUBMIT-API", "异步接口未返回任务 id", detail={"raw": raw})
                return handle
            handle.channel_id = str(task_id)
            handle.status = JobStatus.RUNNING
            self._by_channel[handle.channel_id] = job.idempotency_key
        else:
            handle.channel_id = str(extract_path(raw, "id") or job.job_id)
            self._by_channel[handle.channel_id] = job.idempotency_key
            self._absorb_sync_result(handle, raw)
        return handle

    # ---- R4 轮询 -------------------------------------------------------

    def poll(self, handle: JobHandle) -> JobHandle:
        if handle.status.terminal:
            return handle
        if not self.backend.async_task:
            return handle          # 同步通道在 submit 时已终结

        path = self.backend.status_path.replace("{task_id}", handle.channel_id)
        try:
            raw = self._request_raw(
                method="GET",
                url=self.backend.endpoint.rstrip("/") + path,
                body=None,
            )
        except ChannelError as e:
            # ⚠️ **查询失败不等于任务失败**（2026-10-23 审计 M-16）。
            #
            # 与 ComfyUI 通道对齐：一次网络抖动不该把一个正在跑（甚至已完成）
            # 的**计费**任务判死 —— 判成 FAILED 后 checkpoint 会把它归入
            # "可安全重提"，于是重新提交 -> **重复扣费**。
            # 标 UNKNOWN 交给对账，由服务端回答"到底跑完没有"。
            handle.status = JobStatus.UNKNOWN
            handle.error = e
            return handle

        handle.raw = raw
        state = str(extract_path(raw, "status") or extract_path(raw, "state") or "")
        success = self.backend.status_success_value
        failures = _failure_values(self.backend)
        if state == success:
            self._absorb_sync_result(handle, raw)
        elif state.strip().lower() in failures:
            handle.status = JobStatus.FAILED
            handle.error = ChannelError(
                "F-SUBMIT-API",
                f"任务 {handle.channel_id} 在服务端失败: {state}",
                detail={"raw": raw},
            )
        else:
            handle.status = JobStatus.RUNNING
        return handle

    def watch(self, handle: JobHandle) -> Iterator[ProgressEvent]:
        """轮询式进度。异步任务只能按轮询次数报进度（服务端不提供细粒度时）。"""
        if not self.backend.async_task:
            yield ProgressEvent(0, 1, "item", "同步请求，无中间进度")
            if handle.status.terminal:
                yield ProgressEvent(1, 1, "item", "已完成")
            return
        for i in range(self.max_polls):
            handle = self.poll(handle)
            yield ProgressEvent(i + 1, self.max_polls, "poll",
                                f"轮询第 {i+1} 次，状态 {handle.status.value}")
            if handle.status.terminal:
                return
            time.sleep(self.poll_interval_s)

    # ---- 回收 ----------------------------------------------------------

    def collect(self, handle: JobHandle) -> list[ArtifactRef]:
        if not handle.status.terminal:
            raise ChannelError("F-SUBMIT-API",
                               f"任务 {handle.job_id} 尚未终结（{handle.status.value}），"
                               f"不能回收产物")
        return list(handle.artifacts)

    # ---- R2 中断 -------------------------------------------------------

    def cancel(self, handle: JobHandle) -> None:
        """同步通道**不可中断** —— 抛错而非静默忽略。

        静默忽略会让上层以为已取消，实际服务端还在跑并计费。
        """
        if not self.backend.async_task:
            raise ChannelError(
                "F-SUBMIT-API",
                "同步 HTTP 请求无法中断（服务端已在执行且可能已计费）",
                retryable=False,
            )
        if not self.backend.status_path:
            raise ChannelError("F-SUBMIT-API", "通道未声明 status_path，无法构造取消请求")
        # 约定的取消端点：与状态查询同路径，DELETE
        path = self.backend.status_path.replace("{task_id}", handle.channel_id)
        try:
            self._request_raw("DELETE",
                              self.backend.endpoint.rstrip("/") + path, None)
            handle.status = JobStatus.CANCELLED
        except ChannelError as e:
            raise ChannelError("F-SUBMIT-API",
                               f"取消请求失败: {e.message}", retryable=True) from e

    # ---- R3 对账 -------------------------------------------------------

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        """对账。**键是服务端任务 id**（``channel_id``）。

        ## 两条路径，缺一不可
        1. **进程内**：``_records`` 里查得到 -> 顺带 poll 一次（快、不耗往返）
        2. **跨进程**：查不到就**按任务 id 问服务端**（``status_path``）——
           这才是"崩溃重启后回收已完成任务"真正需要的那条。

        ⚠️ 只做第 1 条是不够的（2026-10-23 审计 H-9）：重启后 ``_records``
        是空的，于是 HTTP 通道的对账**恒返回空**，调用方把它解释成
        「服务端无此任务」-> 判 FAILED -> **重复提交 -> 重复计费**。
        而 ComfyUI 之所以没这个问题，是因为它本来就查服务端的 ``/history``。

        查询失败**抛出**（契约见 ``base.Channel.reconcile``）：不能猜。
        唯一例外是服务端明确回 404 —— 那才真的是"没这个任务"。
        """
        out: dict[str, JobHandle] = {}
        for key in job_ids:
            rec = self._records.get(key)
            if rec is None:
                ik = self._by_channel.get(str(key))
                if ik:
                    rec = self._records.get(ik)
            if rec is not None:
                if self.backend.async_task and not rec.handle.status.terminal:
                    rec.handle = self.poll(rec.handle)
                out[key] = rec.handle
                continue
            remote = self._reconcile_remote(str(key))
            if remote is not None:
                out[key] = remote
        return out

    def _reconcile_remote(self, task_id: str) -> JobHandle | None:
        """按服务端任务 id 直接查状态（跨进程对账）。

        - 查不到（404）-> ``None``：**确实没这个任务**，可安全重提
        - 查询失败（网络/5xx）-> 抛出：不能猜
        - 成功但按 ``response_path`` 取不到产物 -> 标 UNKNOWN 并说明
          （**不谎报成功、也不重提**：那种情况只可能是取件逻辑还没实现）
        """
        if not (self.backend.async_task and self.backend.status_path and task_id):
            return None
        path = self.backend.status_path.replace("{task_id}", task_id)
        try:
            raw = self._request_raw(method="GET",
                                    url=self.backend.endpoint.rstrip("/") + path,
                                    body=None)
        except ChannelError as e:
            if int(e.detail.get("status") or 0) == 404:
                return None
            raise

        handle = JobHandle(job_id=task_id, channel_id=task_id,
                           status=JobStatus.RUNNING)
        handle.raw = raw
        state = str(extract_path(raw, "status") or extract_path(raw, "state") or "")
        if state == self.backend.status_success_value:
            self._absorb_sync_result(handle, raw)
            good = [a for a in handle.artifacts if _looks_like_artifact(a.uri)]
            if not good:
                # 服务端说成功，但按 `response_path` 取不到**可取的**产物 ——
                # 不谎报成功（那会让续跑把它当"已完成"而永远缺这张图），
                # 也不重提（那会重复计费）。
                #
                # ⚠️ 这里必须校验"像不像产物引用"：MiniMax 的
                # `response_path` 是 `task_id`，查询成功的响应里也有 task_id ——
                # 直接 absorb 会把**任务 id 当成产物 URI**，得到一个假引用。
                handle.artifacts.clear()
                handle.status = JobStatus.UNKNOWN
                handle.error = ChannelError(
                    "F-SUBMIT-API",
                    f"服务端说已完成，但按 response_path="
                    f"{self.backend.response_path!r} 取不到可取的产物 —— "
                    f"需要额外取件步骤（如 MiniMax 的 file_id 下载）",
                    detail={"raw": raw},
                )
        elif state.strip().lower() in _failure_values(self.backend):
            handle.status = JobStatus.FAILED
            handle.error = ChannelError(
                "F-SUBMIT-API", f"任务 {task_id} 在服务端失败: {state}",
                detail={"raw": raw})
        return handle

    # ---- 内部 ----------------------------------------------------------

    def _request(self, job: CompiledJob,
                 request: Mapping[str, Any]) -> dict[str, Any]:
        method = str(request.get("method") or self.backend.method or "POST")
        path = str(request.get("path") or self.backend.path)
        body = request.get("body")
        url = self.backend.endpoint.rstrip("/") + path
        return self._request_raw(method, url, body, timeout=job.timeout_s or self.timeout_s)

    def _request_raw(self, method: str, url: str, body: Any,
                     timeout: int | None = None) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            payload = ""
            try:
                payload = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise ChannelError(
                "F-SUBMIT-API",
                f"HTTP {e.code} {e.reason}",
                # 429/5xx 可重试；4xx（除 429）是请求本身的问题，重试无意义
                retryable=(e.code == 429 or 500 <= e.code < 600),
                detail={"url": url, "status": e.code, "body": payload[:600]},
            ) from e
        except Exception as e:  # noqa: BLE001 - 网络层异常统一转结构化错误
            raise ChannelError(
                "F-TIMEOUT" if "timed out" in str(e).lower() else "F-SUBMIT-API",
                f"请求失败: {type(e).__name__}: {e}",
                retryable=True, detail={"url": url},
            ) from e

        if not payload.strip():
            return {}
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as e:
            raise ChannelError(
                "F-SUBMIT-API",
                f"响应不是合法 JSON: {e}",
                detail={"url": url, "body": payload[:400]},
            ) from e
        if not isinstance(obj, dict):
            return {"_value": obj}
        return obj

    def _absorb_sync_result(self, handle: JobHandle, raw: Mapping[str, Any]) -> None:
        """从响应里抽出产物引用。"""
        val = extract_path(raw, self.backend.response_path)
        if val is None:
            handle.status = JobStatus.FAILED
            handle.error = ChannelError(
                "F-SUBMIT-API",
                f"响应中取不到 response_path={self.backend.response_path!r} 指定的值",
                detail={"raw": raw},
            )
            return
        uri = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        # ⚠️ 产物类型**由后端声明**，不写死（2026-10-23 审计 H-10）。
        #
        # 这里原来读 `handle.raw.get("_kind", "image")` —— 而 `_kind` 这个键
        # **全代码库没有任何一处写入**，于是 kind 恒为 `"image"`：
        # H3 的 `.mp4` 被算进 `RenderOutcome.images`，门禁拿 mp4 当图片量尺寸。
        handle.artifacts.append(
            ArtifactRef(kind=_artifact_kind(self.backend, uri), uri=uri))
        handle.status = JobStatus.SUCCEEDED


def _failure_values(backend: Any) -> frozenset[str]:
    """失败状态取值集合（**小写**，用于大小写不敏感的比对）。

    默认清单兜住各家常见写法；后端可以用 ``Backend.status_failure_values``
    追加自己的枚举（MiniMax 官方就是 ``Fail``，不在默认清单里）。
    """
    vals = {str(v).strip().lower()
            for v in (getattr(backend, "status_failure_values", ()) or ())}
    vals.update(_DEFAULT_FAILURE_VALUES)
    return frozenset(vals)


def _looks_like_artifact(uri: str) -> bool:
    """这个值**像不像**一个能取回的产物引用？

    ⚠️ 存在的理由（2026-10-23 自查）：对账时按 `response_path` 取值，
    而有些后端的 `response_path` 是 `task_id`（MiniMax 提交用它）——
    查询成功的响应里也有这个字段，于是会把**任务 id 当成产物 URI**，
    得到一个"看着有产物、其实取不回来"的假引用。那种假成功比没有更糟：
    续跑会认为这一镜已完成，然后永远缺这张图。
    """
    s = str(uri or "").strip()
    if not s:
        return False
    if "://" in s:
        return True
    if s.startswith(("/", "\\", "./", "../")) or (len(s) > 1 and s[1] == ":"):
        return True
    low = s.lower()
    return any(low.endswith(ext) for ext in (
        ".png", ".jpg", ".jpeg", ".webp", ".bmp",
        ".mp4", ".webm", ".mov", ".mkv",
        ".wav", ".mp3", ".m4a", ".flac"))


def _artifact_kind(backend: Any, uri: str) -> str:
    """产物类型：**以声明为准**，声明为默认值时按扩展名兜底。

    为什么保留扩展名兜底：本地服务有时返回不带扩展名的 URL，
    而"声明写错了"和"返回值没扩展名"是两件事 —— 前者该改声明，
    后者只能猜。猜的时候**只在声明是默认值 `image` 时**才覆盖，
    免得把显式声明压掉。
    """
    declared = str(getattr(backend, "artifact_kind", "") or "").strip().lower()
    if declared and declared != "image":
        return declared
    low = str(uri or "").lower().split("?")[0]
    for suffix, kind in ((".mp4", "video"), (".webm", "video"), (".mov", "video"),
                         (".wav", "audio"), (".mp3", "audio"), (".m4a", "audio")):
        if low.endswith(suffix):
            return kind
    return declared or "image"


def _is_local(endpoint: str) -> bool:
    e = (endpoint or "").lower()
    return any(h in e for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1",
                                "host.docker.internal"))
