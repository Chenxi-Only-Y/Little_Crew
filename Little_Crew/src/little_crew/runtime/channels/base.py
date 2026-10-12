"""执行通道契约 —— 双通道（API / 本地）的统一接口。

核心抽象
--------
平台的执行路径只有一条：

    编译器产出 CompiledJob  ──►  Channel.submit(job)  ──►  JobHandle
                                     │
                    ┌────────────────┴────────────────┐
                    ▼                                 ▼
            ComfyChannel                       HttpChannel
        （本地节点图执行）                  （直连 HTTP：本地服务 or 云端 API）

**提交之后的一切（轮询、进度、产物回收）对上层完全同构。**
上层（编排层、验收层）不需要知道这次跑的是本地还是云端。

为什么本地服务与云端 API 归为同一种通道
--------------------------------------
``D:\\AI\\local-gen`` 暴露的是与云端**等价的** OpenAI 兼容接口，
所以它和 DeepSeek / 通义走同一段代码 —— 换 endpoint 即换"本地/云端"，
不需要两套实现。只有 ComfyUI 因为要"编译成节点图"才是真正不同的通道。

契约的稳定性要求
----------------
Channel 的实现**必须**满足：

  R1 提交幂等    ``idempotency_key`` 相同的重复提交不得产生两次执行
                （本地跑两次浪费 GPU 时间；云端跑两次**重复计费**）
  R2 可中断      ``cancel`` 后不得再有产物落地
  R3 可对账      ``reconcile`` 能在进程崩溃重启后回收已完成的任务，
                绝不重复提交 —— 这是"4 小时长跑必须能续"的基础
  R4 进度可观测  必须能上报细粒度进度（帧/步），供前端与超时判定

尚未实现的部分**显式抛 NotImplementedError**，而非静默返回空值 ——
静默的空值会让上游以为成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from little_crew.models.spec import Backend, BackendKind

__all__ = [
    "JobStatus",
    "ArtifactRef",
    "JobHandle",
    "ProgressEvent",
    "Artifact",
    "ChannelError",
    "ChannelCapabilities",
    "Channel",
    "CompiledJob",
]


# ==========================================================================
# 状态与错误
# ==========================================================================

class JobStatus(str, Enum):
    PENDING = "pending"          # 已提交，未开始
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 进程崩溃或连接中断，**状态未知** —— 必须走 reconcile 对账，不可假定失败
    UNKNOWN = "unknown"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class ChannelError(RuntimeError):
    """通道层错误。``code`` 使用项目统一失败码体系（见设计文档 §9.7）。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        #: **可重试**与**不可重试**必须分清：
        #: 超时/限流可重试；参数非法重试一万次也一样失败。
        self.retryable = retryable
        self.detail = dict(detail or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "detail": self.detail}


# ==========================================================================
# 提交物
# ==========================================================================

@dataclass(frozen=True)
class CompiledJob:
    """编译器产出的可提交物。**通道形态决定它装什么**。

    - ``kind is COMFYUI``  -> ``graph`` 非空（节点图），``request`` 为 None
    - ``kind is HTTP``     -> ``request`` 非空（method/path/body），``graph`` 为 None

    这样"双通道"不需要两个编译器，只需要编译器知道目标形态。
    """

    job_id: str
    kind: BackendKind
    backend: Backend
    #: ComfyUI 节点图（API 格式）
    graph: Mapping[str, Any] | None = None
    #: HTTP 请求（method / path / body / headers）
    request: Mapping[str, Any] | None = None
    #: 幂等键（R1）。**必须稳定** —— 同语义任务必得同键。
    idempotency_key: str = ""
    #: 期望产物类型，供通道校验返回是否合理
    expect: str = ""                 # "image" | "video" | "audio" | "text"
    #: 超时（秒）。0 = 用通道默认。
    timeout_s: int = 0
    #: 溯源信息（IR 哈希 / 图哈希 / 规则版本），由通道原样回传以便落库
    provenance: Mapping[str, Any] = field(default_factory=dict)


# ==========================================================================
# 句柄与进度
# ==========================================================================

@dataclass
class ArtifactRef:
    """产物的**引用**（不是产物本身）。真正下载与校验在 runtime 的缓存层做。"""

    kind: str                        # image | video | audio | text | latent
    uri: str
    mime: str = ""
    width: int = 0
    height: int = 0
    duration_ms: int = 0
    fps: int = 0
    bytes: int = 0
    sha256: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", 0, None, {})}


@dataclass(frozen=True)
class ProgressEvent:
    """进度事件。``unit`` 说明 ``current/total`` 的量纲。"""

    current: int
    total: int
    unit: str = "step"               # step | frame | percent | item
    message: str = ""
    node_id: str = ""

    @property
    def ratio(self) -> float:
        return (self.current / self.total) if self.total > 0 else 0.0


@dataclass
class JobHandle:
    """一次执行的句柄。**三种通道形态返回的都是它。**"""

    job_id: str
    channel_id: str                  # 唯一标识这次提交（ComfyUI 的 prompt_id / HTTP 的 task_id）
    status: JobStatus = JobStatus.PENDING
    progress: ProgressEvent | None = None
    artifacts: list[ArtifactRef] = field(default_factory=list)
    error: ChannelError | None = None
    #: 通道回传的原始响应（排障用，不参与业务逻辑）
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is JobStatus.SUCCEEDED


# ==========================================================================
# 通道能力自述
# ==========================================================================

@dataclass(frozen=True)
class ChannelCapabilities:
    """通道自述能力。编排层据此决定是否可用，而不是靠猜。"""

    kind: BackendKind
    #: 是否支持细粒度进度上报
    progress: bool = False
    #: 是否支持中断
    cancellable: bool = False
    #: 是否支持崩溃后对账回收
    reconcilable: bool = False
    #: 是否需要 GPU（决定是否纳入 GPU 调度）
    needs_gpu: bool = False
    #: 是否按调用计费（决定是否必须幂等去重）
    metered: bool = False
    notes: str = ""


# ==========================================================================
# 通道协议
# ==========================================================================

@runtime_checkable
class Channel(Protocol):
    """执行通道契约。

    实现者只需满足本协议，**上层不需要 isinstance 判断是哪条通道**。
    """

    #: 通道实例标识（用于日志与租约归属）
    channel_id: str

    @property
    def capabilities(self) -> ChannelCapabilities:
        """自述能力。"""
        ...

    def available(self) -> tuple[bool, str]:
        """探测可用性。返回 ``(是否可用, 不可用原因)``。

        实现要点：**不得抛异常** —— 探测失败本身就是要返回的信息。
        """
        ...

    def submit(self, job: CompiledJob) -> JobHandle:
        """提交（R1 幂等 / R4 可观测）。

        ``job.idempotency_key`` 已被提交过时，**必须返回既有句柄**，
        而不是再次执行。本地重跑浪费 GPU；云端重跑重复计费。
        """
        ...

    def poll(self, handle: JobHandle) -> JobHandle:
        """查询一次状态。非阻塞。"""
        ...

    def watch(self, handle: JobHandle):
        """流式进度（R4）。返回 ``Iterator[ProgressEvent]``。

        实现要点：**必须能被上层提前 break 而不泄漏连接**。
        """
        ...

    def collect(self, handle: JobHandle) -> list[ArtifactRef]:
        """回收产物（终态后调用）。"""
        ...

    def cancel(self, handle: JobHandle) -> None:
        """中断（R2）。不可中断的通道应抛 ``ChannelError`` 而非静默忽略。"""
        ...

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        """对账（R3）。**崩溃重启后回收已完成的任务，绝不重复提交。**

        映射的键是 ``idempotency_key``。查不到的任务不出现在结果里
        （表示"确实没执行过"，可以安全重提）。
        """
        ...
