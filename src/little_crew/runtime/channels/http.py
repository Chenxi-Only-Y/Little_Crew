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


#: 模板占位符。只认 ``{name}`` 形式（不匹配 JSON 的花括号，
#: 因为 JSON 的花括号后面跟的是引号或换行，不是标识符直接接右花括号）。
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ==========================================================================
# 模板与取值工具
# ==========================================================================

def render_body_template(template: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``body_template`` 里的占位符替换成实际值，返回可提交的 dict。

    **关键安全点 1：引号感知**
    模板里通常写成 ``"prompt": "{prompt}"``（占位符已被引号包住）。
    若此时把 ``json.dumps(v)`` 的结果（**自带外层引号**）塞进去，
    会得到 ``""值""`` —— 坏 JSON。因此本函数检测占位符两侧是否已有引号：

      - 已有引号 -> 插入**去掉外层引号**的转义内容
      - 没有引号 -> 插入完整的 JSON 字面量（字符串自带引号，数值/布尔为裸字面量）

    这样模板作者写哪种形式都正确。

    **关键安全点 2：必须用 json.dumps 转义**
    提示词里出现引号、反斜杠、换行是常态。手工拼接字符串会在第一个引号处
    破坏 JSON 结构 —— 这是手写 JSON 模板最常见的严重缺陷。

    **关键安全点 3：替换后必须重新解析校验**
    宁可在这里报错，也不要让一个坏 JSON 发出去（云端会返回难懂的 400）。
    """
    rendered = template
    for key, val in values.items():
        token = "{" + key + "}"
        idx = rendered.find(token)
        if idx < 0:
            continue

        # 数值/布尔/空 -> 裸字面量（模板里本来就不该给它们加引号）
        if isinstance(val, bool):
            lit = "true" if val else "false"
        elif val is None:
            lit = "null"
        elif isinstance(val, (int, float)):
            lit = json.dumps(val)
        else:
            encoded = json.dumps(str(val), ensure_ascii=False)  # 自带外层引号
            before = rendered[idx - 1] if idx > 0 else ""
            after_pos = idx + len(token)
            after = rendered[after_pos] if after_pos < len(rendered) else ""
            if before == '"' and after == '"':
                lit = encoded[1:-1]      # 模板已加引号，只插转义后的内容
            else:
                lit = encoded            # 模板未加引号，插入完整字符串字面量

        rendered = rendered.replace(token, lit)

    # **未替换的占位符必须报错，不能静默发出去。**
    #
    # 早期版本直接进入下面的 json.loads —— 而 `{"prompt": "{prompt}"}`
    # 本身是**合法 JSON**，于是校验通过、请求发出、服务收到一个字面量
    # `"{prompt}"` 并返回一张与提示词毫无关系的图。
    #
    # 这类失败极难归因：服务不报错、我们有产物、图也"看起来像那么回事"。
    # 一个拼错的占位符（`{promt}`）就能触发它。
    leftover = _PLACEHOLDER_RE.findall(rendered)
    if leftover:
        raise ChannelError(
            "F-SUBMIT-TEMPLATE",
            f"请求体模板里有未替换的占位符 {sorted(set(leftover))} —— "
            f"模板要求的入参与实际提供的不一致（拼写错误？）",
            detail={"missing": sorted(set(leftover)),
                    "provided": sorted(values),
                    "rendered": rendered[:400]},
        )

    try:
        obj = json.loads(rendered)
    except json.JSONDecodeError as e:
        raise ChannelError(
            "F-SUBMIT-VALID",
            f"请求体模板渲染后不是合法 JSON: {e}",
            detail={"rendered": rendered[:400]},
        ) from e
    if not isinstance(obj, dict):
        raise ChannelError("F-SUBMIT-VALID", "请求体模板渲染后不是 JSON 对象")
    return obj


def extract_path(obj: Any, path: str) -> Any:
    """按点号路径取值。支持 ``data.0.url`` 这类列表下标。

    取不到返回 ``None``（由调用方判定是否致命），不抛异常 ——
    某些接口在失败时返回结构不同，取不到值本身就是要判断的信息。
    """
    if not path:
        return obj
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, Mapping):
            cur = cur.get(seg)
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if cur is None:
            return None
    return cur


# ==========================================================================
# 通道
# ==========================================================================

@dataclass
class _Record:
    """一次提交的落账。用于 R1 幂等。"""

    idempotency_key: str
    handle: JobHandle
    submitted_at: float
    attempts: int = 0


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
        """提交。``idempotency_key`` 命中时**直接返回既有句柄，不重复请求**。"""
        if job.kind is not BackendKind.HTTP:
            raise ChannelError("F-SUBMIT-API",
                               f"HttpChannel 不能提交 {job.kind.value!r} 形态的任务")
        if not job.idempotency_key:
            raise ChannelError(
                "F-SUBMIT-VALID",
                "HTTP 通道提交必须携带 idempotency_key —— "
                "远端 API 无幂等键会造成重复计费",
            )

        # R1：幂等去重
        if job.idempotency_key in self._records:
            rec = self._records[job.idempotency_key]
            rec.attempts += 1
            return rec.handle

        if job.request is None:
            raise ChannelError("F-SUBMIT-VALID", "HTTP 任务缺少 request 字段")

        handle = JobHandle(job_id=job.job_id, channel_id="", status=JobStatus.PENDING)
        self._records[job.idempotency_key] = _Record(
            idempotency_key=job.idempotency_key, handle=handle,
            submitted_at=time.time(),
        )

        ok, why = self.available()
        if not ok:
            handle.status = JobStatus.FAILED
            handle.error = ChannelError("F-SUBMIT-API", why, retryable=True)
            return handle

        try:
            raw = self._request(job, job.request)
        except ChannelError as e:
            handle.status = JobStatus.FAILED
            handle.error = e
            return handle

        handle.raw = raw
        if self.backend.async_task:
            task_id = extract_path(raw, "id") or extract_path(raw, "task_id")
            if not task_id:
                handle.status = JobStatus.FAILED
                handle.error = ChannelError(
                    "F-SUBMIT-API", "异步接口未返回任务 id", detail={"raw": raw})
                return handle
            handle.channel_id = str(task_id)
            handle.status = JobStatus.RUNNING
        else:
            handle.channel_id = str(extract_path(raw, "id") or job.job_id)
            self._absorb_sync_result(handle, raw)
        return handle

    # ---- R4 轮询 -------------------------------------------------------

    def poll(self, handle: JobHandle) -> JobHandle:
        if handle.status.terminal:
            return handle
        if not self.backend.async_task:
            return handle          # 同步通道在 submit 时已终结

        path = self.backend.status_path.replace("{task_id}", handle.channel_id)
        req = {"method": "GET", "path": path, "body": None}
        try:
            raw = self._request_raw(
                method="GET",
                url=self.backend.endpoint.rstrip("/") + path,
                body=None,
            )
        except ChannelError as e:
            handle.status = JobStatus.FAILED
            handle.error = e
            return handle

        handle.raw = raw
        state = str(extract_path(raw, "status") or extract_path(raw, "state") or "")
        success = self.backend.status_success_value
        if state == success:
            self._absorb_sync_result(handle, raw)
        elif state in ("failed", "error", "canceled", "cancelled"):
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
        """进程内对账。

        ⚠️ **真正的对账需要持久化** —— 本类只能在**同进程内**回收已提交的任务。
        崩溃重启后的对账由 ``runtime.checkpoint`` 读持久记录后调用本方法完成。
        只返回**确实查得到**的任务；查不到的表示"没执行过"，可安全重提。
        """
        out: dict[str, JobHandle] = {}
        for key in job_ids:
            rec = self._records.get(key)
            if rec is None:
                continue
            if self.backend.async_task and not rec.handle.status.terminal:
                rec.handle = self.poll(rec.handle)
            out[key] = rec.handle
        return out

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
        handle.artifacts.append(ArtifactRef(kind=handle.raw.get("_kind", "image"), uri=uri))
        handle.status = JobStatus.SUCCEEDED


def _is_local(endpoint: str) -> bool:
    e = (endpoint or "").lower()
    return any(h in e for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1",
                                "host.docker.internal"))
