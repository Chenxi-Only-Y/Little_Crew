"""渲染编排 —— 自检。

**用可编排的假通道**，把每种失败路径都测到 —— 真机测试做不到这一点：
真实 ComfyUI 不会按你的意思超时、返回空产物、或进入未知状态。

覆盖：
  · 编译 → 提交 → 回收 的正常路径
  · **五种失败分类**各自被正确识别（这是本层最核心的价值）
  · **R1 幂等**：同 IR 二次渲染不重复提交
  · **R3 对账**：状态未知时要求对账而非重提
  · 成功但零产物 → 判为失败（不能当成功传下去）
  · 批量**部分成功**不丢其余；通道挂掉时提前退出

运行： python -m little_crew.runtime.render_check
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Mapping, Sequence

from little_crew.runtime.channels.base import (
    ArtifactRef,
    ChannelError,
    CompiledJob,
    JobHandle,
    JobStatus,
    ProgressEvent,
)
from little_crew.runtime.render import (
    BatchOutcome,
    FailureKind,
    RenderOutcome,
    RenderSession,
    render_ir,
)

# ==========================================================================
# 编译器夹具 —— 用**代码内的环境夹具**，不依赖磁盘状态或服务
# ==========================================================================


def _build_compiler() -> Any:
    """构造一个确定性的编译器。

    用 ``compiler.test_compiler.env_minimal()`` 这个**代码内环境夹具**，
    而不是现场探测或入库快照：

    | 方式 | 问题 |
    |---|---|
    | 现场探测 | 需要 ComfyUI 在跑 —— 但本测试验证的是**编排逻辑**，与服务在不在无关 |
    | 入库快照 | 快照是某次探测的**时间切片**（如加入 SDXL 权重之前），会随环境演进而失效 |

    代码内夹具随代码一起演进，**永远与编译器同步**。
    真机链路由 ``tests/test_e2e_comfy.py`` 覆盖。
    """
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler
    from little_crew.compiler.test_compiler import env_minimal

    matrix = probe_from_object_info(
        env_minimal(), endpoint="fixture:8188", comfy_version="fixture")
    return ComfyCompiler(matrix)


_COMPILER = _build_compiler()


_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ==========================================================================
# 假通道：可编排每一种失败
# ==========================================================================

class FakeChannel:
    """可编排的通道替身。

    **为什么必须能做这种事**：超时、未知状态、空产物这些路径
    在真实服务上几乎复现不了，但它们恰恰是最需要防护的 ——
    真出问题时（跑了 4 小时后进程崩了）才发现没处理，代价太高。
    """

    def __init__(
        self,
        *,
        available: bool = True,
        why: str = "",
        #: 轮询序列：每次 poll 取一个状态，用完后停在最后一个
        statuses: Sequence[JobStatus] = (JobStatus.SUCCEEDED,),
        artifacts: Sequence[ArtifactRef] | None = None,
        #: 提交时抛的异常
        submit_raises: BaseException | None = None,
        poll_raises: BaseException | None = None,
        #: 失败时带的通道错误
        error: ChannelError | None = None,
        progress: bool = True,
    ) -> None:
        self.endpoint = "http://fake:8188"
        self.channel_id = "fake:ch"
        self._available = available
        self._why = why
        self._statuses = list(statuses) or [JobStatus.SUCCEEDED]
        self._artifacts = list(artifacts) if artifacts is not None else [
            ArtifactRef(kind="image", uri="fake://out_00001_.png",
                        mime="image/png", width=832, height=480, bytes=12345)]
        self._submit_raises = submit_raises
        self._poll_raises = poll_raises
        self._error = error
        self._progress = progress
        #: 记录**每一次** submit 调用（含重复）—— 反映调用方是否做了本地去重
        self.submitted_keys: list[str] = []
        self.submitted_jobs: list[CompiledJob] = []
        #: 幂等键 -> 句柄。**R1 的落点。**
        self._by_key: dict[str, JobHandle] = {}
        self._handles: dict[str, JobHandle] = {}
        self._poll_count = 0
        #: **真正执行**了几次（去重后）
        self.executions = 0

    def available(self) -> tuple[bool, str]:
        return self._available, self._why

    def submit(self, job: CompiledJob) -> JobHandle:
        if self._submit_raises is not None:
            raise self._submit_raises

        # **按幂等键去重，而不是按 job_id。**
        #
        # ⚠️ 这里曾写错：早期版本按 ``job_id`` 判重，于是"不同 graphId
        # 但图相同"的两个任务被当成两件事各提交一次 ——
        # 而真实 ComfyUI 的 R1 是**按幂等键**保证不重复执行的。
        # **替身与真实实现语义不一致时，它验证的是错的东西。**
        self.submitted_keys.append(job.idempotency_key)
        self.submitted_jobs.append(job)

        prior = self._by_key.get(job.idempotency_key)
        if prior is not None:
            return prior

        cid = f"prompt-{len(self._by_key) + 1:03d}"
        h = JobHandle(job_id=job.idempotency_key, channel_id=cid,
                      status=JobStatus.PENDING)
        self._by_key[job.idempotency_key] = h
        self._handles[cid] = h
        self.executions += 1
        return h

    @property
    def unique_keys(self) -> list[str]:
        """**真正执行**过的幂等键（去重后）。"""
        seen: dict[str, None] = {}
        for k in self.submitted_keys:
            seen.setdefault(k, None)
        return list(seen)

    def poll(self, handle: JobHandle) -> JobHandle:
        if self._poll_raises is not None:
            raise self._poll_raises
        idx = min(self._poll_count, len(self._statuses) - 1)
        self._poll_count += 1
        st = self._statuses[idx]
        handle.status = st
        if self._progress:
            handle.progress = ProgressEvent(current=idx + 1, total=len(self._statuses),
                                            unit="step")
        if st is JobStatus.FAILED:
            handle.error = self._error or ChannelError(
                code="F-JOB", message="fake failure")
        return handle

    def collect(self, handle: JobHandle) -> list[ArtifactRef]:
        handle.artifacts = list(self._artifacts)
        return list(self._artifacts)

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        return {j: h for j, h in self._handles.items() if j in set(job_ids)}

    def cancel(self, handle: JobHandle) -> None:
        handle.status = JobStatus.CANCELLED


def _ok_ir(graph_id: str = "G-E001-SH-001") -> dict[str, Any]:
    """一个能过编译的最小 IR（与端到端测试用的同构）。"""
    return {
        "irVersion": "little_crew.ir/v1",
        "graphId": graph_id,
        "scope": "shot",
        "bindings": {},
        "nodes": [
            {"id": "n_ckpt", "kind": "model.checkpoint",
             "params": {"ckpt_name": "sd_xl_base_1.0.safetensors"}},
            {"id": "n_prompt", "kind": "prompt.text",
             "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"text": "rainy rooftop, close-up"}},
            {"id": "n_neg", "kind": "prompt.negative",
             "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"text": "blurry"}},
            {"id": "n_latent", "kind": "latent.empty",
             "params": {"width": 832, "height": 480, "batch_size": 1}},
            {"id": "n_ks", "kind": "sample.ksampler",
             "inputs": {"model": {"ref": "n_ckpt", "port": "MODEL"},
                        "positive": {"ref": "n_prompt", "port": "CONDITIONING"},
                        "negative": {"ref": "n_neg", "port": "CONDITIONING"},
                        "latent_image": {"ref": "n_latent", "port": "LATENT"}},
             "params": {"steps": 8, "cfg": 5.5, "sampler": "dpmpp_2m",
                        "scheduler": "karras", "denoise": 1.0}},
            {"id": "n_decode", "kind": "decode.vae",
             "inputs": {"samples": {"ref": "n_ks", "port": "LATENT"},
                        "vae": {"ref": "n_ckpt", "port": "VAE"}}},
            {"id": "n_save", "kind": "io.save_image",
             "inputs": {"images": {"ref": "n_decode", "port": "IMAGE"}},
             "params": {"filename_prefix": "T"}},
        ],
        "edges": [],
        "constraints": [],
    }


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("渲染编排 —— 自检")
    print("=" * 78)

    _happy()
    _compile_failure()
    _channel_unavailable()
    _channel_exceptions()
    _job_failed()
    _timeout()
    _unknown_status()
    _empty_artifacts()
    _idempotency()
    _batch()
    _cancel()
    _http_path()

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


# ---------- A. 正常路径 ----------

def _happy() -> None:
    ch = FakeChannel(statuses=[JobStatus.RUNNING, JobStatus.SUCCEEDED])
    o = render_ir(_ok_ir(), ch, _COMPILER)
    check("A1 正常路径返回成功", o.ok, f"failure={o.failure}")
    check("A2 拿到产物引用", len(o.artifacts) == 1 and o.images,
          f"{len(o.artifacts)} 个产物")
    check("A3 成功时 failure 为 NONE", o.failure == FailureKind.NONE)
    check("A4 带回编译产物与图哈希",
          o.compile_result is not None and bool(o.compile_result.graph_hash),
          (o.compile_result.graph_hash[:26] + "…") if o.compile_result else "-")
    check("A5 记了耗时", o.seconds >= 0)

    # 进度回调被调用
    # 注意：**同一个会话只能渲染一次同一张图** —— 第二次会命中本地去重
    # 而不再实际执行，因而不会产生进度事件。要测回调就得用新会话。
    seen: list[tuple[str, int]] = []
    ch2 = FakeChannel(statuses=[JobStatus.RUNNING, JobStatus.RUNNING,
                                JobStatus.SUCCEEDED])
    s = RenderSession(ch2, _COMPILER)
    o2 = s.render(_ok_ir(), on_progress=lambda g, p: seen.append((g, p.current)))
    check("A6 进度回调被逐次触发", len(seen) >= 2 and o2.ok,
          f"{len(seen)} 次，渲染 ok={o2.ok}")

    # 进度回调抛异常不得影响渲染
    def boom(g: str, p: Any) -> None:
        raise RuntimeError("callback exploded")

    o3 = RenderSession(FakeChannel(), _COMPILER).render(_ok_ir(), on_progress=boom)
    check("A7 进度回调抛异常不影响渲染结果", o3.ok)


# ---------- B. 编译失败 ----------

def _compile_failure() -> None:
    bad = _ok_ir()
    # 引用一个不存在的节点 —— 编译器应拒绝
    bad["nodes"][1]["inputs"] = {"clip": {"ref": "n_nonexistent", "port": "CLIP"}}
    ch = FakeChannel()
    o = render_ir(bad, ch, _COMPILER)
    check("B1 编译失败被识别为 COMPILE", o.failure == FailureKind.COMPILE,
          f"{o.failure.value}: {o.reason[:60]}")
    check("B2 编译失败时**没有提交**（不浪费 GPU）",
          ch.submitted_keys == [], f"提交 {len(ch.submitted_keys)} 次")
    check("B3 编译失败带可读原因", bool(o.reason), o.reason[:70])

    # 完全非法的 IR
    o2 = render_ir({"nonsense": True}, FakeChannel(), _COMPILER)
    check("B4 结构非法的 IR 也归入 COMPILE",
          o2.failure == FailureKind.COMPILE, o2.reason[:60])


# ---------- C. 通道不可用 ----------

def _channel_unavailable() -> None:
    ch = FakeChannel(available=False, why="connection refused")
    o = render_ir(_ok_ir(), ch, _COMPILER)
    check("C1 通道不可用被单独识别（不是执行失败）",
          o.failure == FailureKind.CHANNEL_UNAVAILABLE, o.reason[:60])
    check("C2 通道不可用时**没有提交**", ch.submitted_keys == [])
    check("C3 仍保留编译结果（IR 本身是对的）",
          o.compile_result is not None)


# ---------- D. 通道异常 ----------

def _channel_exceptions() -> None:
    ch = FakeChannel(submit_raises=ConnectionResetError("reset by peer"))
    o = render_ir(_ok_ir(), ch, _COMPILER)
    check("D1 提交抛异常归为 CHANNEL_ERROR",
          o.failure == FailureKind.CHANNEL_ERROR, o.reason[:60])
    check("D2 异常类型出现在原因里（便于排障）",
          "ConnectionResetError" in o.reason, o.reason[:60])

    ch2 = FakeChannel(statuses=[JobStatus.RUNNING],
                      poll_raises=TimeoutError("poll timed out"))
    o2 = render_ir(_ok_ir(), ch2, _COMPILER)
    check("D3 轮询抛异常归为 CHANNEL_ERROR",
          o2.failure == FailureKind.CHANNEL_ERROR, o2.reason[:60])


# ---------- E. 执行失败 ----------

def _job_failed() -> None:
    ch = FakeChannel(statuses=[JobStatus.RUNNING, JobStatus.FAILED],
                     error=ChannelError(code="F-VALIDATION",
                                        message="required_input_missing: clip"))
    o = render_ir(_ok_ir(), ch, _COMPILER)
    check("E1 执行失败归为 JOB_FAILED（与编译失败区分）",
          o.failure == FailureKind.JOB_FAILED, o.reason[:60])
    check("E2 通道错误信息被带上来",
          "required_input_missing" in o.reason, o.reason[:60])
    check("E3 执行失败时已提交过", len(ch.submitted_keys) == 1)

    ch2 = FakeChannel(statuses=[JobStatus.CANCELLED])
    o2 = render_ir(_ok_ir(), ch2, _COMPILER)
    check("E4 被取消也归为 JOB_FAILED", o2.failure == FailureKind.JOB_FAILED,
          o2.failure.value)


# ---------- F. 超时 ----------

def _timeout() -> None:
    ch = FakeChannel(statuses=[JobStatus.RUNNING])       # 永远在跑
    sess = RenderSession(ch, _COMPILER, timeout_s=1, poll_interval_s=0.01)
    o = sess.render(_ok_ir())
    check("F1 超时被单独识别为 TIMEOUT",
          o.failure == FailureKind.TIMEOUT, o.reason[:60])
    check("F2 超时保留了句柄（可继续对账或取消）", o.handle is not None)
    # ★ 超时 = **可能还在跑**（2026-10-23 审计 H-11）。
    # 上层据此必须记 UNKNOWN 而不是 FAILED —— 记 FAILED 在续跑计划里
    # 等于「可安全重提」，而服务端不会因为我们等够了就停下。
    check("F3 ★★ 超时标 `maybe_running`（不得被当成「可以重提」）",
          o.maybe_running, f"maybe_running={o.maybe_running}")


# ---------- G. 未知状态 ----------

def _unknown_status() -> None:
    ch = FakeChannel(statuses=[JobStatus.UNKNOWN])
    o = render_ir(_ok_ir(), ch, _COMPILER)
    # ⚠️ 断言改过（2026-10-23 审计 M-20）：原来断言的是 CHANNEL_ERROR ——
    # 那等于把「状态未知」和「通道出错」压成同一个分类，而上层对这两件事的
    # 处置**完全相反**（一个该对账、一个该重试）。旧断言把 bug 锁成了规范。
    check("G1 ★★ 状态未知归为 NEEDS_RECONCILE（不是 JOB_FAILED，也不是 CHANNEL_ERROR）",
          o.failure == FailureKind.NEEDS_RECONCILE,
          f"{o.failure} / {o.reason[:50]}")
    check("G1b ★ 且 `maybe_running=True`（上层据此记 UNKNOWN 而不是 FAILED）",
          o.maybe_running, f"maybe_running={o.maybe_running}")
    check("G2 原因明确要求对账而非重提",
          "reconcile" in o.reason and "不可重提" in o.reason, o.reason[:70])

    # ★ 会话级幂等表**不缓存 FAILED 句柄**（2026-10-23 审计 M-21）。
    # 否则一次确定性失败会让这一镜在本次会话内永远失败、且没有重试机会，
    # 而 `reused_submission=True` 还会把失败显示成"复用了提交"。
    ch_fail = FakeChannel(statuses=[JobStatus.FAILED])
    sess = RenderSession(ch_fail, _COMPILER)
    o1 = sess.render(_ok_ir())
    submits_after_first = len(ch_fail.submitted_keys)
    o2 = sess.render(_ok_ir())
    check("G4 ★★ 失败的句柄不进会话幂等表（下次会真的重试）",
          not o1.ok and not o2.reused_submission
          and len(ch_fail.submitted_keys) == submits_after_first + 1,
          f"submits={len(ch_fail.submitted_keys)} reused={o2.reused_submission}")


# ---------- H. 空产物 ----------

def _empty_artifacts() -> None:
    ch = FakeChannel(artifacts=[])
    o = render_ir(_ok_ir(), ch, _COMPILER)
    check("H1 通道报成功但零产物 → 判为失败",
          not o.ok and o.failure == FailureKind.JOB_FAILED, o.reason[:60])
    check("H2 不会把空产物当成功传给上层", o.images == ())


# ---------- I. 幂等（R1） ----------

def _idempotency() -> None:
    ch = FakeChannel()
    sess = RenderSession(ch, _COMPILER)
    ir = _ok_ir()

    o1 = sess.render(ir)
    o2 = sess.render(ir)
    check("I1 同一 IR 渲染两次都成功", o1.ok and o2.ok)
    check("I2 第二次复用已提交记录", o2.reused_submission is True)
    check("I3 会话层本地去重：同 IR 只向通道提交一次",
          len(ch.submitted_keys) == 1, f"提交 {len(ch.submitted_keys)} 次")
    check("I4 会话记录了该次提交", sess.submission_count == 1)

    # 不同镜头 → 不同图 → 不同键 → 各自提交
    o3 = sess.render(_ok_ir("G-E001-SH-002"))
    check("I5 不同镜头得到不同幂等键并各自提交",
          len(ch.unique_keys) == 2, f"{len(ch.unique_keys)} 个唯一键")

    k = ch.submitted_keys[0]
    check("I6 幂等键含规则版本（换规则不复用旧图）",
          "v1" in k and k.startswith("lc-"), k[:62])

    # ⚠️ **graphId 是语义字段**，参与内容哈希 —— 所以"图相同但 graphId 不同"
    # 并不成立。两个不同 graphId 的 IR 得到不同的图，各自提交是**正确行为**。
    # 这条断言记录这个事实，避免后来者误以为"应该只提交一次"。
    ch2 = FakeChannel()
    s2 = RenderSession(ch2, _COMPILER)
    s2.render(_ok_ir("G-A"))
    s2.render(_ok_ir("G-B"))
    check("I7 不同 graphId → 不同图 → 不同键（graphId 是语义字段）",
          len(ch2.unique_keys) == 2, f"{len(ch2.unique_keys)} 个唯一键")

    # 真正该验证的是：**同一张图**（同 IR）在**两个不同会话**里
    # 也应当只执行一次 —— 这是 R1 跨会话的意义所在。
    ch3 = FakeChannel()
    s3a = RenderSession(ch3, _COMPILER)
    s3b = RenderSession(ch3, _COMPILER)      # 新会话，无本地记忆
    s3a.render(_ok_ir("G-SAME"))
    s3b.render(_ok_ir("G-SAME"))
    check("I8 通道 R1：跨会话同图仍只**执行**一次（不重复占用 GPU）",
          ch3.executions == 1,
          f"执行 {ch3.executions} 次，提交调用 {len(ch3.submitted_keys)} 次")


# ---------- J. 批量 ----------

def _batch() -> None:
    ch = FakeChannel()
    sess = RenderSession(ch, _COMPILER)
    irs = [_ok_ir(f"G-E001-SH-{i:03d}") for i in range(1, 6)]
    b = sess.render_batch(irs)
    check("J1 批量全部成功", b.total == 5 and len(b.succeeded) == 5,
          f"{len(b.succeeded)}/{b.total}")
    check("J2 成功率可算", abs(b.success_rate - 1.0) < 1e-9,
          f"{b.success_rate}")
    check("J3 汇总含耗时", b.total_seconds >= 0)

    # 通道中途挂掉 → 提前退出，不空跑
    class FlakyChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def available(self) -> tuple[bool, str]:
            self.calls += 1
            return (self.calls <= 2, "" if self.calls <= 2 else "gone")

    fc = FlakyChannel()
    b2 = RenderSession(fc, _COMPILER).render_batch(irs)
    check("J4 通道挂掉后提前退出（不空跑剩下的镜头）",
          b2.total < 5 and b2.total >= 2, f"跑了 {b2.total} 镜")

    # 部分失败不丢其余
    ch3 = FakeChannel(statuses=[JobStatus.FAILED])
    b3 = RenderSession(ch3, _COMPILER).render_batch(irs[:3])
    check("J5 部分失败时仍返回全部结果（不因一镜失败丢其余）",
          b3.total == 3 and len(b3.failed) == 3,
          f"{len(b3.succeeded)}/{b3.total}")
    check("J6 失败可按分类汇总",
          b3.failures_by_kind().get(FailureKind.JOB_FAILED.value, 0) == 3,
          str(b3.failures_by_kind()))


# ---------- K. 中断 ----------

def _cancel() -> None:
    ch = FakeChannel(statuses=[JobStatus.RUNNING])
    sess = RenderSession(ch, _COMPILER)
    h = ch.submit(CompiledJob(job_id="j", kind=ch_kind(), backend=ch_backend(),
                              graph={}, idempotency_key="k1"))
    sess._submitted["k1"] = h                              # noqa: SLF001
    sess.cancel(h)
    check("K1 中断后状态为 cancelled", h.status is JobStatus.CANCELLED)
    check("K2 中断后清掉本地在途记录（否则误判为已提交）",
          sess.submission_count == 0, f"{sess.submission_count} 条")


def ch_kind():
    from little_crew.models.spec import BackendKind
    return BackendKind.COMFYUI


def ch_backend():
    from little_crew.models.spec import Backend, BackendKind
    return Backend(kind=BackendKind.COMFYUI, endpoint="http://fake:8188")


# ---------- L. HTTP 路径（不编译，直接构造请求）----------

def _zimage_backend():
    """从**真实注册表**取 Z-Image 的 HTTP 后端 —— 而不是在测试里重写一份。

    这样测试与生产用的是同一个 `body_template`：
    模板写错了测试就会红，不会出现"测试里那份是对的、注册表里那份是错的"。
    """
    from little_crew.models.registry import build_default_registry
    from little_crew.models.spec import BackendKind

    spec = build_default_registry().get("z-image-turbo")
    for b in spec.backends:
        if b.kind is BackendKind.HTTP:
            return b
    raise AssertionError("注册表里找不到 z-image-turbo 的 HTTP 后端")


def _http_path() -> None:
    be = _zimage_backend()
    check("L1 注册表里的 Z-Image HTTP 后端指向 local-gen",
          "8100" in be.endpoint and be.path == "/v1/images/generations",
          f"{be.endpoint}{be.path}")

    ch = FakeChannel()
    sess = RenderSession(ch, _COMPILER)
    values = {"prompt": "雨夜天台，米色风衣，回头", "negative_prompt": "多手",
              "width": 832, "height": 480, "steps": 8, "seed": 7}

    o = sess.render_http(values, backend=be, job_id="shot-001")
    check("L2 HTTP 路径渲染成功", o.ok, f"failure={o.failure} {o.reason[:50]}")
    check("L3 HTTP 路径产出图像产物", bool(o.images), f"{len(o.images)} 个")
    check("L4 HTTP 路径**不经过编译**（无 compile_result）",
          o.compile_result is None)
    check("L5 HTTP 路径也能报降级通知数（此处为 0）", o.notice_count == 0)

    # 提交的请求体应是合法 JSON 且中文未损坏
    job = ch.submitted_jobs[-1]
    body = job.request["body"]
    check("L6 请求方法/路径来自后端声明",
          job.request["method"] == "POST" and job.request["path"] == be.path,
          f"{job.request['method']} {job.request['path']}")
    check("L7 请求体是 dict（已被模板解析校验过）", isinstance(body, dict),
          type(body).__name__)
    check("L8 **中文提示词未被破坏**（引号感知 + json 转义）",
          body.get("prompt") == values["prompt"],
          str(body.get("prompt"))[:40])
    check("L9 数值参数按数值类型进入（不带引号）",
          body.get("width") == 832 and body.get("num_inference_steps") == 8,
          f"width={body.get('width')!r} num_inference_steps={body.get('num_inference_steps')!r}")

    # 提示词含引号/反斜杠/换行 —— 这是手拼 JSON 最容易炸的地方
    nasty = '她说"别走"\\然后\n转身'
    o2 = sess.render_http({**values, "prompt": nasty}, backend=be,
                          job_id="shot-002")
    b2 = ch.submitted_jobs[-1].request["body"]
    check("L10 提示词含引号/反斜杠/换行仍能正确编码",
          o2.ok and b2.get("prompt") == nasty, repr(b2.get("prompt"))[:46])

    # 幂等：同参数第二次复用，不重复请求
    before = len(ch.submitted_keys)
    o3 = sess.render_http(values, backend=be, job_id="shot-001")
    check("L11 HTTP 路径同样有 R1 幂等（同参数不再提交）",
          o3.reused_submission is True
          and len(ch.submitted_keys) == before,
          f"提交数 {before} -> {len(ch.submitted_keys)}")

    # 改了参数 → 不同幂等键 → 必须重新提交
    o4 = sess.render_http({**values, "seed": 99}, backend=be, job_id="shot-003")
    check("L12 改了任一入参即换幂等键（避免复用旧图）",
          o4.reused_submission is False
          and len(ch.submitted_keys) == before + 1,
          f"提交数 {len(ch.submitted_keys)}")

    # 失败分类同样适用
    ch2 = FakeChannel(available=False, why="local-gen 未启动")
    o5 = RenderSession(ch2, _COMPILER).render_http(values, backend=be,
                                                   job_id="s")
    check("L13 HTTP 路径同样区分「通道不可用」",
          o5.failure == FailureKind.CHANNEL_UNAVAILABLE, o5.reason[:50])

    # 模板渲染失败（缺必需占位符）应归为 COMPILE 而非 CHANNEL_ERROR
    ch3 = FakeChannel()
    from little_crew.models.spec import Backend, BackendKind
    broken = Backend(kind=BackendKind.HTTP, endpoint="http://x:1",
                     path="/v1/images/generations",
                     body_template='{"prompt": "{prompt}"}')
    o6 = RenderSession(ch3, _COMPILER).render_http({"nope": 1}, backend=broken,
                                                   job_id="s")
    check("L14 请求构造失败归为 COMPILE（改 IR/参数，重试无用）",
          o6.failure == FailureKind.COMPILE, o6.reason[:60])


if __name__ == "__main__":
    raise SystemExit(main())
