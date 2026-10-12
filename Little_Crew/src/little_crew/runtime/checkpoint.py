"""渲染检查点 —— 断点续跑，**绝不重复烧 GPU**。

为什么必须有这一层
------------------
一集 60 镜按实测 13~76 秒/镜算是 **13 分钟到 1 小时以上**，
加上视频阶段会是**小时级**。在这个时长上：

- 进程崩溃、机器重启、手动中止 —— **不是异常，是必然**
- 而"从头再来"的代价不只是时间：**云端按次计费，重跑就是重复付费**

所以续跑不是便利功能，是**长跑可作性的前提**。

两阶段提交：为什么"提交前"就要落盘
---------------------------------
```
① 写意图（pending，含幂等键与请求指纹）   ← 崩溃点在提交之前 → 无副作用
② 提交，拿到 prompt_id 立刻落盘            ← 崩溃点在提交之后 → 必须能找回
③ 完成后记产物引用（succeeded）
```

**关键在于①必须在②之前落盘**。若只在拿到 prompt_id 之后才写，
那么在"提交成功、还没来得及写"这一瞬间崩溃，重启后就**不知道有个任务在跑** ——
只能重新提交，而服务端其实已经在跑了。结果：**双份 GPU**。

（严格说这一瞬间仍无法完全消除：提交成功与落盘之间物理上有一个窗口。
但把窗口从"整个提交往返（可能几十秒）"压到"一次本地写（毫秒级）"，
风险降低几个数量级。这是工程上能做到的最好情况。）

⚠️ 与内容缓存的分工
------------------
| | 检查点（本模块） | 内容缓存（`cache.py`） |
|---|---|---|
| 记什么 | **过程中的状态**（谁在跑、跑到哪） | **结果**（跑出来的产物） |
| 键 | job_id（幂等键） | 内容哈希 |
| 解决的问题 | 崩溃后续跑 | 改一句只重跑受影响的 |

两者互补：缓存让"没变的镜头"不用跑，检查点让"跑到一半崩了"不用从头跑。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "JobState",
    "CheckpointRecord",
    "RenderCheckpoint",
    "ResumePlan",
]


# ==========================================================================
# 状态
# ==========================================================================

class JobState(str, Enum):
    """一个镜头的渲染状态。

    刻意区分 ``SUBMITTED`` 与 ``SUCCEEDED``：前者表示"可能已经烧了 GPU"，
    是**绝不能重提**的状态。把它与"还没开始"混为一谈，就是重复烧 GPU 的根源。
    """

    #: 记了意图但还没提交成功 —— 重提是安全的
    PENDING = "pending"
    #: **已提交**（服务端可能已经在跑）—— 必须先对账，绝不直接重提
    SUBMITTED = "submitted"
    #: 完成且有产物
    SUCCEEDED = "succeeded"
    #: 明确失败（可重试）
    FAILED = "failed"
    #: 状态未知（崩溃留下的）—— 必须对账
    UNKNOWN = "unknown"

    @property
    def needs_reconcile(self) -> bool:
        """是否必须向服务端对账后才能决定。"""
        return self in (JobState.SUBMITTED, JobState.UNKNOWN)

    @property
    def terminal(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED)


# ==========================================================================
# 记录
# ==========================================================================

@dataclass
class CheckpointRecord:
    """一个镜头的一条持久记录。"""

    job_id: str
    idempotency_key: str
    state: JobState = JobState.PENDING
    #: 服务端的任务标识（ComfyUI 的 prompt_id）
    channel_id: str = ""
    #: 产物文件名清单（相对缓存/产物目录）
    artifacts: tuple[str, ...] = ()
    attempts: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    #: 最近一次失败原因
    error: str = ""
    #: 请求指纹（入参摘要），用于识别"参数变了"而要求重跑
    fingerprint: str = ""
    #: 任意附加信息（引擎、模型等）
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "key": self.idempotency_key,
            "state": self.state.value,
            "channelId": self.channel_id,
            "artifacts": list(self.artifacts),
            "attempts": self.attempts,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "error": self.error,
            "fingerprint": self.fingerprint,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "CheckpointRecord":
        return cls(
            job_id=str(d.get("jobId", "")),
            idempotency_key=str(d.get("key", "")),
            state=JobState(str(d.get("state", JobState.PENDING.value))),
            channel_id=str(d.get("channelId", "")),
            artifacts=tuple(d.get("artifacts", ())),
            attempts=int(d.get("attempts", 0)),
            created_at=float(d.get("createdAt", 0.0)),
            updated_at=float(d.get("updatedAt", 0.0)),
            error=str(d.get("error", "")),
            fingerprint=str(d.get("fingerprint", "")),
            extra=dict(d.get("extra", {})),
        )


@dataclass
class ResumePlan:
    """重启后该做什么。**这是本模块最重要的输出。**"""

    #: 已完成，直接跳过（零 GPU）
    done: tuple[CheckpointRecord, ...] = ()
    #: 需要向服务端对账后才能定（**绝不能直接重提**）
    reconcile: tuple[CheckpointRecord, ...] = ()
    #: 可以安全重提（记了意图但没提交成功，或明确失败）
    todo: tuple[CheckpointRecord, ...] = ()
    #: 从未见过的镜头
    fresh: tuple[str, ...] = ()

    @property
    def total_known(self) -> int:
        return (len(self.done) + len(self.reconcile)
                + len(self.todo) + len(self.fresh))

    @property
    def needs_service(self) -> bool:
        """是否需要向服务端查询 —— 有在途任务就必须先对账。"""
        return bool(self.reconcile)

    def as_dict(self) -> dict[str, Any]:
        return {
            "done": [r.job_id for r in self.done],
            "reconcile": [r.job_id for r in self.reconcile],
            "todo": [r.job_id for r in self.todo],
            "fresh": list(self.fresh),
            "needsService": self.needs_service,
        }


# ==========================================================================
# 检查点
# ==========================================================================

class RenderCheckpoint:
    """一次拍摄的**持久**状态账本。

    刻意做成"一个小 JSON 文件 + 原子写"，而不是数据库：
    账本很小（一集 60~120 条），而**依赖越少，崩溃时越不容易一起坏掉**。
    """

    #: 账本文件名
    LEDGER = "checkpoint.json"

    #: 加载时发现的问题（供上层上报）。
    #: 声明在类顶部 —— 放在使用点之后虽能跑，但读代码的人会以为它是实例属性。
    load_error: str = ""

    def __init__(self, root: str | os.PathLike[str],
                 *, episode_id: str = "") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / self.LEDGER
        self.episode_id = episode_id
        self._records: dict[str, CheckpointRecord] = {}
        self.load_error = ""
        self._load()

    # ---- 持久化 --------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 账本损坏 → **不静默当空账本**。
            #
            # 当空账本的后果是"所有在途任务都变成 fresh"，也就是**全部重提** ——
            # 恰恰是检查点要防止的事。所以保留现场、改名隔离，让人能看到。
            broken = self.path.with_suffix(f".broken.{int(time.time())}")
            try:
                self.path.replace(broken)
            except OSError:
                pass
            self.load_error = f"账本损坏，已隔离到 {broken.name}"
            return
        for item in (blob.get("records") or []):
            try:
                r = CheckpointRecord.from_dict(item)
            except Exception:                                 # noqa: BLE001
                continue
            if r.job_id:
                self._records[r.job_id] = r
        self.episode_id = str(blob.get("episodeId", self.episode_id))

    #: 加载时发现的问题（供上层上报）

    def _flush(self) -> None:
        """**原子写**：先写临时文件再改名。

        否则写到一半崩溃会留下半个 JSON —— 而半个 JSON 比没有更糟：
        它会触发上面那条"损坏隔离"路径，把在途任务全部变成 fresh。
        """
        payload = {
            "version": 1,
            "episodeId": self.episode_id,
            "updatedAt": time.time(),
            "records": [r.as_dict() for r in self._records.values()],
        }
        tmp = self.path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ---- 写入 ----------------------------------------------------------

    def begin(self, job_id: str, idempotency_key: str, *,
              fingerprint: str = "", extra: Mapping[str, Any] | None = None
              ) -> CheckpointRecord:
        """**提交前**记录意图。这一步是整个续跑机制的地基。"""
        prior = self._records.get(job_id)
        if prior is not None:
            # 幂等键变了 → 参数变了 → 之前的完成状态不再有效
            if prior.idempotency_key != idempotency_key:
                prior.state = JobState.PENDING
                prior.channel_id = ""
                prior.artifacts = ()
            prior.idempotency_key = idempotency_key
            prior.fingerprint = fingerprint or prior.fingerprint
            prior.updated_at = time.time()
            if extra:
                prior.extra = {**prior.extra, **dict(extra)}
            self._flush()
            return prior

        r = CheckpointRecord(job_id=job_id, idempotency_key=idempotency_key,
                             state=JobState.PENDING, fingerprint=fingerprint,
                             extra=dict(extra or {}))
        self._records[job_id] = r
        self._flush()
        return r

    def mark_submitted(self, job_id: str, channel_id: str) -> CheckpointRecord:
        """提交成功后立刻落盘 —— **紧接着提交，不能等到轮询**。"""
        r = self._records.get(job_id)
        if r is None:
            raise KeyError(f"未记录的任务 {job_id!r} —— 必须先调 begin()")
        r.channel_id = str(channel_id)
        r.state = JobState.SUBMITTED
        r.attempts += 1
        r.updated_at = time.time()
        self._flush()
        return r

    def mark_succeeded(self, job_id: str, artifacts: Sequence[str] = ()) -> None:
        r = self._records.get(job_id)
        if r is None:
            return
        r.state = JobState.SUCCEEDED
        r.artifacts = tuple(str(a) for a in artifacts)
        r.error = ""
        r.updated_at = time.time()
        self._flush()

    def mark_failed(self, job_id: str, error: str = "") -> None:
        r = self._records.get(job_id)
        if r is None:
            return
        r.state = JobState.FAILED
        r.error = str(error)[:400]
        r.updated_at = time.time()
        self._flush()

    def mark_unknown(self, job_id: str, reason: str = "") -> None:
        r = self._records.get(job_id)
        if r is None:
            return
        r.state = JobState.UNKNOWN
        r.error = str(reason)[:400]
        r.updated_at = time.time()
        self._flush()

    # ---- 读取 ----------------------------------------------------------

    def get(self, job_id: str) -> CheckpointRecord | None:
        return self._records.get(job_id)

    def all_records(self) -> list[CheckpointRecord]:
        return sorted(self._records.values(), key=lambda r: r.created_at)

    @property
    def record_count(self) -> int:
        return len(self._records)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self._records.values():
            out[r.state.value] = out.get(r.state.value, 0) + 1
        return out

    # ---- 续跑计划 ------------------------------------------------------

    def plan(self, expected: Sequence[str] = ()) -> ResumePlan:
        """算出重启后该做什么。**绝不把在途任务放进 todo。**"""
        done: list[CheckpointRecord] = []
        rec: list[CheckpointRecord] = []
        todo: list[CheckpointRecord] = []
        for r in self._records.values():
            if r.state is JobState.SUCCEEDED:
                done.append(r)
            elif r.state.needs_reconcile:
                rec.append(r)
            else:
                todo.append(r)
        known = set(self._records)
        fresh = tuple(j for j in expected if j not in known)
        return ResumePlan(done=tuple(done), reconcile=tuple(rec),
                          todo=tuple(todo), fresh=fresh)

    def reconcile_now(self, channel: Any) -> dict[str, JobState]:
        """向服务端对账在途任务，并把结果落盘。

        返回 ``{job_id: 新状态}``。

        **对账不上的在途任务判为 FAILED（可重提），不判 SUCCEEDED。**
        理由：查不到就是"服务端没有这个任务"，重提是安全的；
        若错判为已完成，会**静默丢掉一个镜头** —— 那比多跑一次严重得多。
        """
        plan = self.plan()
        by_channel: dict[str, CheckpointRecord] = {
            r.channel_id: r for r in plan.reconcile if r.channel_id}
        out: dict[str, JobState] = {}

        if not by_channel:
            # 没有 channel_id 的在途任务 —— 提交没成功，可以安全重提
            for r in plan.reconcile:
                self.mark_failed(r.job_id, "无 channel_id，视为未提交")
                out[r.job_id] = JobState.FAILED
            return out

        try:
            found = channel.reconcile(list(by_channel))
        except Exception as exc:                              # noqa: BLE001
            # 对账本身失败 —— **不能猜**。保持 SUBMITTED，等下次再对。
            for r in plan.reconcile:
                r.error = f"对账失败：{type(exc).__name__}: {exc}"
            self._flush()
            return out

        for cid, r in by_channel.items():
            h = found.get(cid)
            if h is None:
                self.mark_failed(r.job_id, "服务端无此任务，视为未提交")
                out[r.job_id] = JobState.FAILED
                continue
            status = str(getattr(getattr(h, "status", None), "value", ""))
            if status == "succeeded":
                arts = tuple(_name_of(a) for a in getattr(h, "artifacts", ()))
                self.mark_succeeded(r.job_id, arts)
                out[r.job_id] = JobState.SUCCEEDED
            elif status == "failed":
                self.mark_failed(r.job_id, "服务端报告失败")
                out[r.job_id] = JobState.FAILED
            else:
                # 还在跑 —— 保持 SUBMITTED，下次再对
                r.error = f"仍在运行（{status or '未知'}）"
                self._flush()
                out[r.job_id] = JobState.SUBMITTED

        for r in plan.reconcile:
            if r.job_id not in out and not r.channel_id:
                self.mark_failed(r.job_id, "无 channel_id，视为未提交")
                out[r.job_id] = JobState.FAILED
        return out

    # ---- 可观测性 ------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "episodeId": self.episode_id,
            "records": self.record_count,
            "byState": self.counts(),
            "loadError": self.load_error,
            "path": str(self.path),
        }


def _name_of(ref: Any) -> str:
    """沿用缓存层的命名规则 —— **必须一致**，否则对账回来的产物名与缓存对不上。"""
    from little_crew.runtime.cached_render import _name_of as impl

    return impl(ref)
