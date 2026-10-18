"""镜头渲染编排 —— 把 IR 送进通道、拿回产物。

链路位置
--------
```
Shot（agent 层）──ir_bridge──► 统一 IR
                                 │  ★ 本模块
                     ┌───────────┴───────────┐
                     ▼                       ▼
              compiler.compile        channel.submit
                     │                       │
                     └──────► CompiledJob ◄──┘
                                 │
                                 ▼
                        产物引用 + 溯源
```

**为什么放在 runtime 而不是 agent**：本模块要 import `compiler`（L3）。
按架构守卫的分层，`runtime` 是 L4，可以向 L3 依赖；而 `agent` 也是 L3，
**同级之间没有白名单**，放进 agent 会被守卫判违规。
数据翻译（`agent/ir_bridge.py`）不含编译与执行，因此留在 agent 是合适的 ——
**分层决定了职责该放在哪，而不是反过来**。

三条契约（与通道契约对齐，但责任在这一层）
------------------------------------------
| 要求 | 本层的做法 |
|---|---|
| **R1 提交幂等** | 同 ``idempotency_key`` 复用同一句柄；**不重复提交** |
| **R3 可对账** | ``reconcile()`` 崩溃重启后凭键找回已完成任务，**绝不重提** |
| **R2 可中断** | ``cancel()`` 转发到通道，并清掉本地在途记录 |

失败分类是本层的核心价值
------------------------
一次渲染可能因**三种完全不同的原因**失败，而上层的处置方式各不相同：

| 失败类型 | 含义 | 上层该怎么办 |
|---|---|---|
| **编译失败** | IR 或环境有问题 | **改 IR 或装模型**，重试无用 |
| **通道不可用** | 服务没起来 / 端点错 | **等或修环境**，重试有意义 |
| **执行失败** | 图跑了但报错 | 看错误码，可能要**降级** |

把三者混成一个"失败"会让上游无从下手 —— 所以 `RenderOutcome` 显式区分。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

from little_crew.compiler.compiler import CompileError, CompileResult, ComfyCompiler
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
from little_crew.runtime.channels.http import render_body_template

__all__ = [
    "FailureKind",
    "RenderOutcome",
    "BatchOutcome",
    "RenderSession",
    "render_ir",
    "http_job",
]


# ==========================================================================
# 结果类型
# ==========================================================================

class FailureKind(str, Enum):
    """失败分类。**这是本层最重要的输出之一。**

    用 Enum 而非裸字符串：上层要按分类分支处置（改 IR / 等环境 / 降级），
    拼错的字符串会让分支静默走空。
    """

    #: 没失败
    NONE = "none"
    #: 编译不过 —— 改 IR 或装模型，重试无用
    COMPILE = "compile"
    #: 通道不可用 —— 服务没起来，重试有意义
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    #: 通道抛异常
    CHANNEL_ERROR = "channel_error"
    #: 图跑了但失败 —— 看错误码，可能降级
    JOB_FAILED = "job_failed"
    #: 超时
    TIMEOUT = "timeout"


@dataclass
class RenderOutcome:
    """一次渲染的结果。**成功与失败走同一个结构** —— 上层不必分支处理。"""

    graph_id: str
    ok: bool
    #: 失败分类（ok=True 时为 NONE）
    failure: str = FailureKind.NONE
    #: 可读的失败说明
    reason: str = ""
    #: 编译产物（失败于编译时可能为 None）
    compile_result: CompileResult | None = None
    #: 提交句柄
    handle: JobHandle | None = None
    #: 产物引用
    artifacts: tuple[ArtifactRef, ...] = ()
    #: 降级通知的数量（编译期产生的，**必须向上传递**）
    notice_count: int = 0
    #: 本次是否复用了已有提交（R1 幂等命中）
    reused_submission: bool = False
    #: 墙钟耗时（秒）
    seconds: float = 0.0

    @property
    def images(self) -> tuple[ArtifactRef, ...]:
        return tuple(a for a in self.artifacts if a.kind == "image")

    def as_dict(self) -> dict[str, Any]:
        return {
            "graphId": self.graph_id,
            "ok": self.ok,
            "failure": self.failure,
            "reason": self.reason,
            "artifactCount": len(self.artifacts),
            "imageCount": len(self.images),
            "noticeCount": self.notice_count,
            "reusedSubmission": self.reused_submission,
            "seconds": round(self.seconds, 3),
            "graphHash": (self.compile_result.graph_hash
                          if self.compile_result else ""),
        }


@dataclass
class BatchOutcome:
    """一批镜头的结果。**部分成功是常态** —— 不能因为一镜失败就丢掉其余。"""

    outcomes: tuple[RenderOutcome, ...] = ()

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def succeeded(self) -> tuple[RenderOutcome, ...]:
        return tuple(o for o in self.outcomes if o.ok)

    @property
    def failed(self) -> tuple[RenderOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.ok)

    @property
    def success_rate(self) -> float:
        return (len(self.succeeded) / self.total) if self.total else 0.0

    @property
    def total_seconds(self) -> float:
        return sum(o.seconds for o in self.outcomes)

    def failures_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.failed:
            out[o.failure] = out.get(o.failure, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "successRate": round(self.success_rate, 4),
            "totalSeconds": round(self.total_seconds, 2),
            "failuresByKind": self.failures_by_kind(),
            "detail": [o.as_dict() for o in self.outcomes],
        }


# ==========================================================================
# 通道协议（只声明本模块用到的部分）
# ==========================================================================

class RenderChannel(Protocol):
    """渲染需要的通道能力子集。

    **刻意只声明用到的五个方法** —— 这样测试替身不必实现整个
    ``Channel`` 协议（含 WS、取消、能力自述等）。接口越小，
    桩与真实实现的一致性越容易保证。
    """

    endpoint: str
    channel_id: str

    def available(self) -> tuple[bool, str]: ...
    def submit(self, job: CompiledJob) -> JobHandle: ...
    def poll(self, handle: JobHandle) -> JobHandle: ...
    def collect(self, handle: JobHandle) -> list[ArtifactRef]: ...
    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]: ...


# ==========================================================================
# HTTP 路径：不编译成节点图，直接构造请求
# ==========================================================================

def http_job(
    job_id: str,
    backend: Backend,
    values: Mapping[str, Any],
    *,
    idempotency_key: str,
    expect: str = "image",
    timeout_s: int = 900,
    provenance: Mapping[str, Any] | None = None,
) -> CompiledJob:
    """构造一个 **HTTP 通道**的可提交作业。

    与 ComfyUI 路径的关系
    --------------------
    | | ComfyUI | HTTP |
    |---|---|---|
    | 中间产物 | 节点图 | 请求体 |
    | 谁来产生 | :class:`ComfyCompiler` | **本函数**（模板填充） |
    | 之后 | **完全一致** —— 提交、轮询、回收、幂等、对账（这一层统一） |

    **为什么 HTTP 不需要编译器**：编译器存在的意义是"把语义 IR 翻译成
    节点图"。而 HTTP 接口自己就是一层语义接口（`prompt` 进、图片出），
    再套一层图编译是多余的。``body_template`` 承担了同样的翻译职责 ——
    只是它是**声明式**的，写在模型注册表里而不是代码里。

    ⚠️ 模板填充用 :func:`render_body_template`（**引号感知 + json 转义**），
    不要手工拼字符串：提示词里出现引号、反斜杠、换行是常态，
    手工拼接会在第一个引号处破坏 JSON。
    """
    body = render_body_template(backend.body_template or "{}", dict(values))
    return CompiledJob(
        job_id=job_id,
        kind=BackendKind.HTTP,
        backend=backend,
        request={"method": backend.method or "POST",
                 "path": backend.path,
                 "body": body},
        idempotency_key=idempotency_key,
        expect=expect,
        timeout_s=timeout_s,
        provenance=dict(provenance or {}),
    )


# ==========================================================================
# 会话
# ==========================================================================

class RenderSession:
    """一次拍摄会话 —— 持有通道、编译器与已提交记录。

    **会话的意义在于幂等与对账**：同一会话内重复渲染同一镜头
    不会二次提交；崩溃后可以凭会话记录对账找回已完成的任务。

    跨会话的持久化不在这里（那属于检查点，见 R9 的 `runtime/cache.py`）。
    """

    def __init__(
        self,
        channel: RenderChannel,
        compiler: ComfyCompiler,
        *,
        backend: Backend | None = None,
        timeout_s: int = 900,
        poll_interval_s: float = 0.2,   # 2026-10-18: 1.0 让测试套件从 45s 涨到 240s
                                    # （每个 poll 都等满 1 秒）；0.2 仍安全：
                                    # 340 秒的任务约 1700 次连接 ≈ 5/秒，
                                    # TIME_WAIT 约 1200 条 << 16384 端口池
    ) -> None:
        """``compiler`` **必填**。

        刻意不从环境隐式构造：能力矩阵决定"哪些节点可用"，
        它来自一次真实探测（或入库的快照）。隐式构造会让测试
        在"本机恰好探测成功"时通过、换台机器就失败，
        而且掩盖了"本次会话按哪个环境编译"这一关键信息。
        """
        self.channel = channel
        self.compiler = compiler
        self.backend = backend or Backend(
            kind=BackendKind.COMFYUI, endpoint=getattr(channel, "endpoint", ""))
        self.timeout_s = int(timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        #: 幂等键 -> 句柄。**R1 的落点。**
        self._submitted: dict[str, JobHandle] = {}

    # ---- 幂等 ----------------------------------------------------------

    def already_submitted(self, idempotency_key: str) -> JobHandle | None:
        return self._submitted.get(idempotency_key)

    @property
    def submission_count(self) -> int:
        return len(self._submitted)

    # ---- 单镜渲染 ------------------------------------------------------

    def render(
        self,
        ir: Mapping[str, Any],
        *,
        expect: str = "image",
        job_id: str = "",
        wait: bool = True,
        on_progress: Any = None,
    ) -> RenderOutcome:
        """编译并渲染一个 IR。

        ``wait=False`` 只提交不等待 —— 用于批量投递后再统一回收。
        """
        t0 = time.perf_counter()
        gid = str(ir.get("graphId", ""))

        # ---- 1) 编译 ----
        try:
            result = self.compiler.compile(ir)
        except CompileError as exc:
            return RenderOutcome(
                graph_id=gid, ok=False, failure=FailureKind.COMPILE,
                reason=f"编译失败：{exc}",
                seconds=time.perf_counter() - t0)
        except Exception as exc:                              # noqa: BLE001
            return RenderOutcome(
                graph_id=gid, ok=False, failure=FailureKind.COMPILE,
                reason=f"编译异常 {type(exc).__name__}：{exc}",
                seconds=time.perf_counter() - t0)

        # ---- 2) 通道可用性 ----
        ok_avail, why = self.channel.available()
        if not ok_avail:
            return RenderOutcome(
                graph_id=gid, ok=False,
                failure=FailureKind.CHANNEL_UNAVAILABLE,
                reason=f"通道不可用：{why}", compile_result=result,
                notice_count=len(result.notices),
                seconds=time.perf_counter() - t0)

        # ---- 3) 幂等键 ----
        #
        # **以图哈希为主，而非 IR 哈希。**
        #
        # 理由：真正决定"是否重复执行"的是**编译出来的图** ——
        # 执行引擎看到的就是它。而 ``irHash`` 含 ``graphId`` 这类
        # 元数据（graphId 是语义字段，参与内容哈希），
        # 于是"改个 graphId 但图不变"会得到不同幂等键、白跑一次。
        #
        # 仍然带上 ``ruleVersion``：同一张图在不同规则版本下语义可能不同，
        # 复用它等于静默产错图。``irHash`` 只作为溯源记录，不进键。
        idem = f"lc-{result.graph_hash.split(':')[-1][:32]}-" \
               f"{result.rule_version.split('/')[-1]}"

        job = CompiledJob(
            job_id=job_id or f"job-{gid}",
            kind=BackendKind.COMFYUI,
            backend=self.backend,
            graph=result.graph,
            idempotency_key=idem,
            expect=expect,
            timeout_s=self.timeout_s,
            provenance={"irHash": result.ir_hash, "graphHash": result.graph_hash,
                        "catalogHash": result.catalog_hash,
                        "ruleVersion": result.rule_version,
                        "graphId": gid},
        )

        prior = self._submitted.get(idem)
        reused = prior is not None
        if reused:
            # **复用时不再提交。**
            #
            # 早期版本即使命中本地记录也照常调 ``channel.submit()``，
            # 靠通道的 R1 兜底去重。两个坏处：
            #   1. 白跑一次网络往返（本地还好，云端就是一次计费风险）
            #   2. ``reused`` 的语义变含糊 —— 到底是"我们复用了"
            #      还是"通道帮我们复用了"，日志里分不清
            handle = prior
        else:
            try:
                handle = self.channel.submit(job)
            except Exception as exc:                          # noqa: BLE001
                return RenderOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.CHANNEL_ERROR,
                    reason=f"提交异常 {type(exc).__name__}：{exc}",
                    compile_result=result, notice_count=len(result.notices),
                    seconds=time.perf_counter() - t0)
            self._submitted[idem] = handle

        if not wait:
            return RenderOutcome(
                graph_id=gid, ok=False, failure=FailureKind.NONE,
                reason="已提交，未等待（wait=False）",
                compile_result=result, handle=handle,
                notice_count=len(result.notices), reused_submission=reused,
                seconds=time.perf_counter() - t0)

        return self._await(handle, result, gid, reused, t0, on_progress)

    # ---- HTTP 路径（不编译）--------------------------------------------

    def render_http(
        self,
        values: Mapping[str, Any],
        *,
        backend: Backend,
        job_id: str,
        expect: str = "image",
        provenance: Mapping[str, Any] | None = None,
        wait: bool = True,
    ) -> RenderOutcome:
        """走 HTTP 通道渲染一次（不经过节点图编译）。

        与 :meth:`render` 共用**提交、幂等、轮询、回收、失败分类**的
        全部逻辑 —— 唯一区别是"作业从哪来"。
        """
        t0 = time.perf_counter()

        ok_avail, why = self.channel.available()
        if not ok_avail:
            return RenderOutcome(
                graph_id=job_id, ok=False,
                failure=FailureKind.CHANNEL_UNAVAILABLE,
                reason=f"通道不可用：{why}",
                seconds=time.perf_counter() - t0)

        # 幂等键：后端标识 + 全部入参的内容哈希。
        # **必须含全部入参** —— 少一个参数就会让"改了参数但复用了旧图"。
        import hashlib
        import json as _json
        blob = _json.dumps({"endpoint": backend.endpoint, "path": backend.path,
                            "values": dict(values)},
                           sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
        idem = f"lc-http-{digest}"

        try:
            job = http_job(job_id, backend, values, idempotency_key=idem,
                           expect=expect, timeout_s=self.timeout_s,
                           provenance=provenance)
        except Exception as exc:                              # noqa: BLE001
            return RenderOutcome(
                graph_id=job_id, ok=False, failure=FailureKind.COMPILE,
                reason=f"请求构造失败：{type(exc).__name__}: {exc}",
                seconds=time.perf_counter() - t0)

        prior = self._submitted.get(idem)
        reused = prior is not None
        if reused:
            handle = prior
        else:
            try:
                handle = self.channel.submit(job)
            except Exception as exc:                          # noqa: BLE001
                return RenderOutcome(
                    graph_id=job_id, ok=False,
                    failure=FailureKind.CHANNEL_ERROR,
                    reason=f"提交异常 {type(exc).__name__}：{exc}",
                    seconds=time.perf_counter() - t0)
            self._submitted[idem] = handle

        if not wait:
            return RenderOutcome(
                graph_id=job_id, ok=False, failure=FailureKind.NONE,
                reason="已提交，未等待（wait=False）", handle=handle,
                reused_submission=reused, seconds=time.perf_counter() - t0)

        return self._await(handle, None, job_id, reused, t0, None)

    # ---- 等待与回收 ----------------------------------------------------

    def with_backend(self, backend: Any) -> "RenderSession":
        """返回一个**换了通道后端**的浅拷贝。

        ## 为什么是"整个会话换一份"，而不是"方法内替换几行"

        `HttpChannel` 构造时把 `backend` 存进 `self`，而
        `available/submit/poll/collect/reconcile` **全都读 `self.backend`** ——
        `render_http(values, backend)` 传进来的那个会被**静默忽略**。

        2026-10-18 真机上，这导致"用图像通道请求视频接口"，报
        `响应中取不到 response_path='data.0.url'`。

        我一开始只在 `render_http` 里替换了**一部分** `self.channel`，
        结果变成「提交走了对的通道、轮询走了错的」——
        **半对状态比全错更危险**：全错会当场失败，半对会跑一段才露馅。

        所以改成**整份会话换掉**：要么全用旧通道，要么全用新通道，
        **不存在中间态**。浅拷贝只替换 `channel`，其余（timeout/checkpoint 等）共享。
        """
        current = getattr(self, "channel", None)
        if current is None or getattr(current, "backend", None) is backend:
            return self
        swap = getattr(current, "for_backend", None)
        if not callable(swap):
            return self
        import copy as _copy

        clone = _copy.copy(self)
        clone.channel = swap(backend)
        return clone

    def _await(self, handle: JobHandle, result: CompileResult | None, gid: str,
               reused: bool, t0: float, on_progress: Any) -> RenderOutcome:
        # HTTP 路径没有编译产物 -> result 为 None。用局部变量统一两种情况，
        # 避免在每个返回点写一遍 if result else 0。
        notices = len(result.notices) if result is not None else 0
        deadline = time.time() + self.timeout_s
        h = handle
        while True:
            if h.status is JobStatus.SUCCEEDED:
                break
            if h.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                return RenderOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.JOB_FAILED,
                    reason=(h.error.message if h.error else h.status.value),
                    compile_result=result, handle=h,
                    notice_count=notices,
                    reused_submission=reused,
                    seconds=time.perf_counter() - t0)
            if h.status is JobStatus.UNKNOWN:
                # 状态未知**不等于失败** —— 必须对账，不可重提
                return RenderOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.CHANNEL_ERROR,
                    reason="任务状态未知，需 reconcile 对账（不可重提）",
                    compile_result=result, handle=h,
                    notice_count=notices,
                    reused_submission=reused,
                    seconds=time.perf_counter() - t0)
            if time.time() > deadline:
                return RenderOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.TIMEOUT,
                    reason=f"等待超过 {self.timeout_s}s",
                    compile_result=result, handle=h,
                    notice_count=notices,
                    reused_submission=reused,
                    seconds=time.perf_counter() - t0)
            # ⚠️ **必须节流**（2026-10-18 实测）。
            #
            # 此前这里是个**紧循环** —— `poll_interval_s` 存了却从来没用。
            # 实测后果：
            #   · 130 秒内向 local-gen 发了 **5725 次连接**（≈44/秒）
            #   · Windows 临时端口池只有 16384，TIME_WAIT 堆到 **21427**
            #   · 于是 `poll` 抛 `WinError 10048`（地址只能使用一次）
            #
            # 表现是"异步渲染随机失败"，而根因是**我们自己把端口打光了**。
            # 这类错误最容易被误判成"服务端不稳"。
            #
            # 不只影响 local-gen —— **任何异步 HTTP 后端**（含 MiniMax 官方）
            # 都会因为这里没有节流而被自己打死。
            try:
                time.sleep(self.poll_interval_s)
                h = self.channel.poll(h)
            except Exception as exc:                          # noqa: BLE001
                return RenderOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.CHANNEL_ERROR,
                    reason=f"轮询异常 {type(exc).__name__}：{exc}",
                    compile_result=result, handle=h,
                    notice_count=notices,
                    reused_submission=reused,
                    seconds=time.perf_counter() - t0)
            if on_progress is not None and h.progress is not None:
                try:
                    on_progress(gid, h.progress)
                except Exception:                             # noqa: BLE001
                    pass          # 进度回调不得影响渲染

        # ---- 回收产物 ----
        try:
            arts = self.channel.collect(h)
        except Exception as exc:                              # noqa: BLE001
            return RenderOutcome(
                graph_id=gid, ok=False, failure=FailureKind.CHANNEL_ERROR,
                reason=f"产物回收异常 {type(exc).__name__}：{exc}",
                compile_result=result, handle=h,
                notice_count=notices,
                reused_submission=reused,
                seconds=time.perf_counter() - t0)

        # **成功但零产物**视为失败：上层拿不到东西，不能当成功传下去
        if not arts:
            return RenderOutcome(
                graph_id=gid, ok=False, failure=FailureKind.JOB_FAILED,
                reason="通道报告成功但没有产物",
                compile_result=result, handle=h,
                notice_count=notices,
                reused_submission=reused,
                seconds=time.perf_counter() - t0)

        return RenderOutcome(
            graph_id=gid, ok=True, compile_result=result, handle=h,
            artifacts=tuple(arts), notice_count=notices,
            reused_submission=reused, seconds=time.perf_counter() - t0)

    # ---- 批量 ----------------------------------------------------------

    def render_batch(
        self, irs: Sequence[Mapping[str, Any]], *,
        expect: str = "image", stop_on_channel_down: bool = True,
    ) -> BatchOutcome:
        """逐镜渲染。**部分成功是常态，不因一镜失败丢掉其余。**

        ``stop_on_channel_down``：通道不可用是**环境问题**，
        继续跑只会得到同样的失败 —— 此时提前退出能省下大量无谓尝试。
        """
        outs: list[RenderOutcome] = []
        for ir in irs:
            o = self.render(ir, expect=expect)
            outs.append(o)
            if (stop_on_channel_down
                    and o.failure == FailureKind.CHANNEL_UNAVAILABLE):
                break
        return BatchOutcome(outcomes=tuple(outs))

    # ---- 对账与中断 ----------------------------------------------------

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        """凭 job_id 找回任务状态。**崩溃重启后调用，绝不重提。**"""
        return self.channel.reconcile(job_ids)

    def cancel(self, handle: JobHandle) -> None:
        """中断并清掉本地在途记录 —— 否则后续会误判为"已提交"。"""
        cancel = getattr(self.channel, "cancel", None)
        if callable(cancel):
            cancel(handle)
        for k, v in list(self._submitted.items()):
            if v.channel_id and v.channel_id == handle.channel_id:
                del self._submitted[k]


# ==========================================================================
# 便捷函数
# ==========================================================================

def render_ir(
    ir: Mapping[str, Any],
    channel: RenderChannel,
    compiler: ComfyCompiler,
    *,
    expect: str = "image",
    timeout_s: int = 900,
) -> RenderOutcome:
    """一次性渲染（无会话、无幂等记忆）。**适合单次调用与测试。**"""
    return RenderSession(channel, compiler, timeout_s=timeout_s)\
        .render(ir, expect=expect)
