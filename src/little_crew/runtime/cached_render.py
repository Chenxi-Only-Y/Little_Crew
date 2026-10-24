"""缓存感知的渲染 —— 兑现「改一句只重跑受影响的镜头」。

这一层解决什么
--------------
一集 60 镜、每镜 14~500 秒 → 全量重跑是**小时级**的代价。
"改一个参数就重跑一集"在工程上不可接受，所以缓存**不是优化项，是可作性前提**。

它如何做到"只重跑受影响的镜头"
-----------------------------
缓存键 = ``sha256(irHash ‖ catalogHash ‖ ruleVersion)``。而 ``irHash``
是**内容哈希** —— 它只随**语义变化**而变。于是：

```
改第 3 镜的服装  →  第 3 镜的 irHash 变  →  只有它未命中
                    其余 59 镜 irHash 不变 →  全部命中，零 GPU
```

**不需要"增量分析"或"依赖图推导"** —— 内容寻址本身就是增量的。

与 `RenderSession` 的分工
------------------------
| 层 | 职责 |
|---|---|
| :class:`CachingRenderer` | **查缓存 → 命中就返回，未命中才渲染 → 回填缓存** |
| ``RenderSession`` | 编译、提交、轮询、回收、失败分类（**不关心缓存**） |

这样分层的好处：缓存可以整个关掉（``enabled=False``）而渲染逻辑一行不变，
便于对照实验 —— 而"对照"正是"成本可控"这一论点需要的数据。

⚠️ 命中统计必须分得清
--------------------
**我们的三级缓存**与 **ComfyUI 自身的节点级缓存**是两回事：
前者**零 GPU**，后者仍要走一次提交。混记会让节省比例虚高且无法解释。
本模块只记前者；后者由 ``RenderSession`` 经通道回传。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from little_crew.runtime.cache import (
    CacheStats,
    CacheTier,
    ContentCache,
    LookupResult,
)
from little_crew.runtime.render import (
    BatchOutcome,
    FailureKind,
    RenderOutcome,
    RenderSession,
)

__all__ = [
    "CacheOutcome",
    "CachedBatch",
    "CachingRenderer",
    "artifact_fetcher_for",
]


# ==========================================================================
# 结果类型
# ==========================================================================

@dataclass
class CacheOutcome:
    """一次"查缓存 → （可能）渲染 → 回填"的结果。"""

    graph_id: str
    ok: bool
    #: 是否命中缓存（命中则**零 GPU**）
    from_cache: bool = False
    #: 命中级别（未命中为 None）
    tier: CacheTier | None = None
    #: 命中时省下的渲染时间（秒）；未命中为 0
    seconds_saved: float = 0.0
    #: 实际耗时
    seconds: float = 0.0
    #: 产物文件名 -> 字节（命中时来自缓存，未命中时来自本次渲染）
    artifacts: Mapping[str, bytes] = field(default_factory=dict)
    #: 底层渲染结果（命中时为 None —— 因为根本没渲染）
    render: RenderOutcome | None = None
    #: 失败分类（命中时为 NONE）
    failure: str = FailureKind.NONE
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "graphId": self.graph_id,
            "ok": self.ok,
            "fromCache": self.from_cache,
            "tier": self.tier.value if self.tier else None,
            "secondsSaved": round(self.seconds_saved, 3),
            "seconds": round(self.seconds, 3),
            "artifactCount": len(self.artifacts),
            "failure": self.failure,
        }


@dataclass
class CachedBatch:
    """一批镜头的结果。**部分成功与部分命中都是常态。**"""

    outcomes: tuple[CacheOutcome, ...] = ()

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def succeeded(self) -> tuple[CacheOutcome, ...]:
        return tuple(o for o in self.outcomes if o.ok)

    @property
    def failed(self) -> tuple[CacheOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.ok)

    @property
    def cache_hits(self) -> tuple[CacheOutcome, ...]:
        return tuple(o for o in self.outcomes if o.from_cache)

    @property
    def rendered(self) -> tuple[CacheOutcome, ...]:
        return tuple(o for o in self.outcomes if o.ok and not o.from_cache)

    @property
    def hit_rate(self) -> float:
        return (len(self.cache_hits) / self.total) if self.total else 0.0

    @property
    def seconds_saved(self) -> float:
        return sum(o.seconds_saved for o in self.outcomes)

    @property
    def seconds_spent(self) -> float:
        return sum(o.seconds for o in self.outcomes)

    @property
    def speedup(self) -> float:
        """有缓存 vs 无缓存的时间比。

        **这是"成本可控"这一论点的核心指标** ——
        没有它，"只重跑受影响的镜头"就只是一句设计意图。
        """
        spent = self.seconds_spent
        if spent <= 0:
            return float("inf") if self.seconds_saved > 0 else 1.0
        return (self.seconds_saved + spent) / spent

    def as_dict(self) -> dict[str, Any]:
        sp = self.speedup
        return {
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "cacheHits": len(self.cache_hits),
            "rendered": len(self.rendered),
            "hitRate": round(self.hit_rate, 4),
            "secondsSaved": round(self.seconds_saved, 2),
            "secondsSpent": round(self.seconds_spent, 2),
            "speedup": None if sp == float("inf") else round(sp, 3),
            "detail": [o.as_dict() for o in self.outcomes],
        }


# ==========================================================================
# 产物抓取
# ==========================================================================

def artifact_fetcher_for(channel: Any, timeout_s: int = 120) -> Callable[[Any], bytes]:
    """按通道类型构造一个「引用 → 字节」的抓取器。

    不同通道的 ``ArtifactRef.uri`` 形态不同：

    | 通道 | uri 形态 | 抓法 |
    |---|---|---|
    | HTTP（local-gen） | 完整 URL | 直接取 |
    | ComfyUI | ``/view?filename=…``（**相对路径**） | 拼上 endpoint 再取 |

    ⚠️ 这个差异必须在这里处理掉。早期若直接对 ComfyUI 的相对路径发请求，
    会得到 ``unknown url type`` —— 而错误信息与"产物丢失"很像，
    容易误判成缓存问题。
    """
    import urllib.request

    endpoint = str(getattr(channel, "endpoint", "") or "").rstrip("/")

    def fetch(ref: Any) -> bytes:
        uri = str(getattr(ref, "uri", "") or "")
        if not uri:
            raise ValueError("产物引用缺少 uri")
        url = uri if uri.startswith(("http://", "https://")) else endpoint + uri
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.read()

    return fetch


# ==========================================================================
# 缓存感知渲染器
# ==========================================================================

class CachingRenderer:
    """在 :class:`RenderSession` 外面包一层内容缓存。

    **关闭缓存只需 ``enabled=False``** —— 渲染逻辑一行不变。
    这样"开/关缓存的对照实验"是同一份代码跑两遍，
    而不是两份实现对比（后者无法排除实现差异）。
    """

    def __init__(
        self,
        session: RenderSession,
        cache: ContentCache,
        *,
        enabled: bool = True,
        tier2_enabled: bool = True,
        fetcher: Callable[[Any], bytes] | None = None,
        checkpoint: Any = None,
    ) -> None:
        self.session = session
        self.cache = cache
        self.enabled = bool(enabled)
        self.tier2_enabled = bool(tier2_enabled)
        self.fetcher = fetcher or artifact_fetcher_for(session.channel)
        #: 可选的持久检查点（:class:`RenderCheckpoint`）。
        #:
        #: 给了它就在**提交前**记意图、完成后记产物 —— 长跑中断可续。
        #: 不给则退化为纯缓存（短任务够用）。
        #:
        #: 两者的分工：缓存让"没变的镜头"不用跑，检查点让"跑到一半崩了"
        #: 不用从头跑。**互补，不替代。**
        self.checkpoint = checkpoint
        #: 内容哈希 -> (图哈希, 产物, 渲染耗时)。
        #:
        #: ⚠️ **必须记耗时**。早期版本只存了图哈希与产物，于是进程内命中
        #: 报 `seconds_saved=0` —— 提速比永远是 1.0，而"节省了多少"恰恰是
        #: 这个模块存在的理由。一个不记录收益的缓存，等于没有度量。
        self._by_ir: dict[str, tuple[str, Mapping[str, bytes], float]] = {}

    # ---- 单镜 ----------------------------------------------------------

    def render(self, ir: Mapping[str, Any], **kw: Any) -> CacheOutcome:
        """查缓存 → 命中即返回；未命中才渲染并回填。"""
        t0 = time.perf_counter()
        gid = str(ir.get("graphId", ""))

        if not self.enabled:
            return self._render_and_store(ir, gid, t0, store=False, **kw)

        # ---- 1) 内容哈希（语义指纹）----
        from little_crew.ir.canonical import content_hash, cache_key

        try:
            ir_hash = content_hash(ir)
        except Exception as exc:                              # noqa: BLE001
            return CacheOutcome(
                graph_id=gid, ok=False, failure=FailureKind.COMPILE,
                reason=f"内容哈希失败：{type(exc).__name__}: {exc}",
                seconds=time.perf_counter() - t0)

        # ---- 2) 进程内索引：省掉一次编译 ----
        memo = self._by_ir.get(ir_hash)
        if memo is not None:
            _, cached_arts, cached_secs = memo
            self.cache.stats.record_hit(CacheTier.T1, cached_secs)
            return CacheOutcome(
                graph_id=gid, ok=True, from_cache=True, tier=CacheTier.T1,
                seconds_saved=cached_secs,
                artifacts=cached_arts, reason="进程内索引命中",
                seconds=time.perf_counter() - t0)

        # ---- 3) 磁盘缓存 ----
        #
        # T1 键需要 catalogHash 与 ruleVersion，而它们只有**编译后**才知道。
        # 因此这里先用 T2 键（只含 irHash）探测 —— 若命中，
        # 就完全跳过编译与提交。
        probe = self.cache.lookup(ir_hash=ir_hash, allow_t2=False
                                  if not self.tier2_enabled else True)
        # 注意：allow_t2=False 时 lookup 只会查 T1，而这里没有 T1 键 ——
        # 所以关闭 T2 等于"只信进程内索引"，这是刻意的语义。
        if probe.hit and probe.entry is not None:
            try:
                arts = self.cache.read_artifacts(probe)
            except Exception:                                 # noqa: BLE001
                arts = {}
            if arts:
                secs = float(probe.entry.render_seconds)
                self._by_ir[ir_hash] = (
                    str(probe.entry.provenance.get("graphHash", "")), arts, secs)
                return CacheOutcome(
                    graph_id=gid, ok=True, from_cache=True,
                    tier=probe.tier,
                    seconds_saved=secs,
                    artifacts=arts,
                    reason=f"磁盘缓存命中 {probe.tier.value if probe.tier else '?'}",
                    seconds=time.perf_counter() - t0)

        # ---- 4) 未命中：真渲染并回填 ----
        return self._render_and_store(ir, gid, t0, store=True,
                                      ir_hash=ir_hash, **kw)

    def _render_and_store(self, ir: Mapping[str, Any], gid: str, t0: float,
                          *, store: bool, ir_hash: str = "",
                          **kw: Any) -> CacheOutcome:
        # ---- 提交前记意图（审计 M-22：IR 路径此前**完全不写检查点**）----
        #
        # ComfyUI 是本机的**主渲染路径**，而"跑到一半崩了不用从头跑"
        # 这个能力在这条路上原来根本不存在：进程被 kill 后每一镜都要重新烧 GPU。
        self._ck_begin(gid, idempotency_key=ir_hash or gid,
                       fingerprint=ir_hash or gid,
                       engine=str(getattr(self.session.channel, "endpoint", "")))

        out = self.session.render(ir, **kw)
        dt = time.perf_counter() - t0
        self._ck_settle(gid, out)
        # 记**实耗**与 ComfyUI 自身的命中（审计 M-12 / M-13）——
        # 两处都是"计数器存在但没人喂"的接线缺失。
        self._record_metrics(out, dt)

        if not out.ok:
            return CacheOutcome(
                graph_id=gid, ok=False, failure=out.failure, reason=out.reason,
                render=out, seconds=dt)

        # ---- 抓产物字节 ----
        #
        # **为什么必须抓下来**：缓存的价值是"下次不用跑"，
        # 而只存一个 URL 是没用的 —— 服务重启后那个 URL 就是死链。
        # 抓到本地是"缓存"与"记录"的分界。
        blobs: dict[str, bytes] = {}
        for ref in out.artifacts:
            try:
                nm = _name_of(ref)
                blob = self.fetcher(ref)
            except Exception as exc:                          # noqa: BLE001
                return CacheOutcome(
                    graph_id=gid, ok=False,
                    failure=FailureKind.CHANNEL_ERROR,
                    reason=f"产物抓取失败（{_name_of(ref)}）："
                           f"{type(exc).__name__}: {exc}",
                    render=out, seconds=dt)
            # **撞名即报错。**
            #
            # 两个不同产物映射到同一文件名，意味着**后一个会覆盖前一个** ——
            # 缓存看起来"有东西"，实际少了一张图，而且不报任何错。
            # 这类静默损坏必须在写入前拦住。
            if nm in blobs:
                return CacheOutcome(
                    graph_id=gid, ok=False, failure=FailureKind.CHANNEL_ERROR,
                    reason=f"产物文件名冲突：{nm}（来自 {ref.uri}）—— "
                           f"命名规则不唯一，继续下去会静默覆盖",
                    render=out, seconds=dt)
            blobs[nm] = blob

        if not blobs:
            return CacheOutcome(
                graph_id=gid, ok=False, failure=FailureKind.JOB_FAILED,
                reason="渲染成功但没抓到任何产物字节", render=out, seconds=dt)

        if store and self.enabled and ir_hash:
            try:
                self.cache.store(
                    ir_hash=ir_hash, artifacts=blobs,
                    catalog_hash=(out.compile_result.catalog_hash
                                  if out.compile_result else ""),
                    rule_version=(out.compile_result.rule_version
                                  if out.compile_result else ""),
                    graph_hash=(out.compile_result.graph_hash
                                if out.compile_result else ""),
                    render_seconds=dt,
                    extra_provenance={"graphId": gid},
                )
            except Exception:                                 # noqa: BLE001
                pass          # 回填失败不影响本次结果 —— 只是下次还要重跑
            self._by_ir[ir_hash] = (
                out.compile_result.graph_hash if out.compile_result else "",
                blobs, dt)

        # 成功落账（产物名必须与缓存层命名一致，否则续跑对不上）
        self._ck_succeed(gid, sorted(blobs))

        return CacheOutcome(graph_id=gid, ok=True, artifacts=blobs,
                            render=out, seconds=dt)

    # ---- 批量 ----------------------------------------------------------

    # ---- 检查点记账（两条路径共用）-------------------------------------
    #
    # 2026-10-23 审计 H-11 / M-22：记账原来只写在 HTTP 路径里，
    # 而且把"超时/状态未知"也一并写成 FAILED —— 而 FAILED 在续跑计划里
    # 等于「可安全重提」。**两条路都错**：IR 路径（ComfyUI，本机主路）压根不记，
    # HTTP 路径把在途任务记成可重提。

    def _ck_begin(self, job_id: str, *, idempotency_key: str,
                  fingerprint: str, engine: str = "") -> None:
        """提交**之前**记意图。

        为什么必须在提交前：若只在拿到 channel_id 之后才写，那么在
        「提交成功、还没来得及写」这一瞬间崩溃，重启后就不知道有个任务在跑 ——
        只能重提，而服务端其实已经在跑了。**双份 GPU。**
        """
        ck = self.checkpoint
        if ck is None or not job_id:
            return
        try:
            ck.begin(job_id, idempotency_key=idempotency_key,
                     fingerprint=fingerprint, extra={"engine": engine})
        except Exception:                                     # noqa: BLE001
            pass          # 记账失败不该阻断渲染 —— 但下次续跑会不知道它

    def _ck_settle(self, job_id: str, out: RenderOutcome) -> None:
        """按结果把检查点落到**最保守**的状态。

        | 情形 | 记成 | 续跑时 |
        |---|---|---|
        | 成功 | 交给 `_ck_succeed`（要等产物名） | 跳过 |
        | **超时 / 状态未知**（句柄还在） | `UNKNOWN` | 进 reconcile，**不重提** |
        | 无 channel_id（确定没被受理） | `FAILED` | 可安全重提 |
        | 有 channel_id 且服务端明确报错 | `FAILED` | 可重试 |
        """
        ck = self.checkpoint
        if ck is None or not job_id:
            return
        try:
            h = out.handle
            if out.ok:
                if h is not None and h.channel_id:
                    ck.mark_submitted(job_id, h.channel_id)
                return
            if h is not None and h.channel_id:
                # 先记 id：`reconcile` 只能按服务端任务 id 查
                ck.mark_submitted(job_id, h.channel_id)
            if out.maybe_running:
                # ⚠️ **绝不能记 FAILED**：那在 `plan()` 里等于"可安全重提"，
                # 而任务很可能还在服务端跑（超时只是我们等够了）。
                ck.mark_unknown(job_id, out.reason or "任务可能仍在服务端运行")
                return
            ck.mark_failed(job_id, out.reason or "提交未成功")
        except Exception:                                     # noqa: BLE001
            pass

    def _ck_succeed(self, job_id: str, names: Sequence[str]) -> None:
        ck = self.checkpoint
        if ck is None or not job_id:
            return
        try:
            ck.mark_succeeded(job_id, tuple(names))
        except Exception:                                     # noqa: BLE001
            pass

    def _record_metrics(self, out: Any, dt: float) -> None:
        """把这次**实算**的耗时与 ComfyUI 自身的命中记进统计。

        ## 两处都是"计数器存在但没人喂"（2026-10-23 审计 M-12 / M-13）

        | 指标 | 症状 | 根因 |
        |---|---|---|
        | `secondsSpent` -> `speedup` | 恒为 0，提速比永远是 1.0 / None | 唯一调用点 `record_miss()` 不传时间 |
        | `comfyNodeCacheEvents` | 恒为 0 | `record_comfy_cache()` **生产零调用** |

        两个指标都是"成本可控 / 缓存收益"这条论点的**核心证据**，
        而它们一直是空数 —— 报告上看不出任何异常，因为 0 看起来像个正常值。
        """
        stats = getattr(self.cache, "stats", None)
        if stats is not None:
            try:
                stats.record_spent(dt)
            except Exception:                                 # noqa: BLE001
                pass
        ch = getattr(self.session, "channel", None)
        fn = getattr(ch, "cached_nodes_for", None)
        handle = getattr(out, "handle", None)
        if not callable(fn) or handle is None or stats is None:
            return
        try:
            n = int(fn(handle) or 0)
        except Exception:                                     # noqa: BLE001
            return
        if n > 0:
            try:
                stats.record_comfy_cache(n)
            except Exception:                                 # noqa: BLE001
                pass

    def render_batch(self, irs: Sequence[Mapping[str, Any]],
                     **kw: Any) -> CachedBatch:
        return CachedBatch(outcomes=tuple(self.render(ir, **kw) for ir in irs))

    # ---- HTTP 路径的缓存（不编译，直接走请求）--------------------------

    def render_http(
        self,
        values: Mapping[str, Any],
        backend: Any,
        *,
        job_id: str,
        provenance: Mapping[str, Any] | None = None,
    ) -> CacheOutcome:
        """缓存化的 HTTP 渲染。**键取自全部入参的内容哈希。**

        与 :meth:`render` 的分工一致：命中就返回，未命中才提交。
        区别只是"作业从哪来" —— 一个来自编译器，一个来自模板填充。
        """
        t0 = time.perf_counter()

        if not self.enabled:
            return self._http_and_store(values, backend, job_id, t0,
                                        store=False, key="",
                                        provenance=provenance)

        # 幂等/缓存键：后端标识 + 全部入参。
        # **必须含全部入参** —— 少一个就会"改了参数却复用了旧图"。
        blob = json.dumps({"endpoint": getattr(backend, "endpoint", ""),
                           "path": getattr(backend, "path", ""),
                           "values": dict(values)},
                          sort_keys=True, ensure_ascii=False)
        key = hashlib.sha256(blob.encode("utf-8")).hexdigest()

        memo = self._by_ir.get(key)
        if memo is not None:
            _, arts, secs = memo
            self.cache.stats.record_hit(CacheTier.T1, secs)
            return CacheOutcome(graph_id=job_id, ok=True, from_cache=True,
                                tier=CacheTier.T1, seconds_saved=secs,
                                artifacts=arts, reason="进程内索引命中",
                                seconds=time.perf_counter() - t0)

        probe = self.cache.lookup(ir_hash=key, allow_t2=self.tier2_enabled)
        if probe.hit and probe.entry is not None:
            try:
                arts = self.cache.read_artifacts(probe)
            except Exception:                                 # noqa: BLE001
                arts = {}
            if arts:
                secs = float(probe.entry.render_seconds)
                self._by_ir[key] = ("", arts, secs)
                return CacheOutcome(
                    graph_id=job_id, ok=True, from_cache=True, tier=probe.tier,
                    seconds_saved=secs, artifacts=arts,
                    reason=f"磁盘缓存命中 {probe.tier.value if probe.tier else '?'}",
                    seconds=time.perf_counter() - t0)

        return self._http_and_store(values, backend, job_id, t0, store=True,
                                    key=key, provenance=provenance)

    def _http_and_store(self, values: Mapping[str, Any], backend: Any,
                        job_id: str, t0: float, *, store: bool, key: str,
                        provenance: Mapping[str, Any] | None) -> CacheOutcome:
        # ---- 提交前记意图（续跑的地基）----
        self._ck_begin(job_id, idempotency_key=key, fingerprint=key,
                       engine=getattr(backend, "endpoint", ""))

        # ⚠️ **换成配这个后端的会话**（2026-10-18 真机事故）。
        # `HttpChannel` 构造时锁死了 `backend`，不换会话就会出现
        # "用图像通道请求视频接口" —— 报 `response_path='data.0.url'`。
        # 用 `with_backend` 整份换，避免"提交换对了、轮询没换"的半对状态。
        out = self.session.with_backend(backend).render_http(
            values, backend=backend, job_id=job_id, provenance=provenance)
        dt = time.perf_counter() - t0

        # ---- 落账：**先判"是否可能还在跑"，再决定记什么**（审计 H-11）----
        #
        # 原来的写法是「`if not out.ok: mark_failed`」—— 而超时/状态未知
        # 也走这条路，于是 SUBMITTED 被改写成 FAILED，续跑时被当成
        # "可安全重提" -> 重复提交 -> 重复烧 GPU / 重复计费。
        self._ck_settle(job_id, out)
        self._record_metrics(out, dt)

        if not out.ok:
            return CacheOutcome(graph_id=job_id, ok=False, failure=out.failure,
                                reason=out.reason, render=out, seconds=dt)

        blobs: dict[str, bytes] = {}
        for ref in out.artifacts:
            try:
                nm = _name_of(ref)
                blob = self.fetcher(ref)
            except Exception as exc:                          # noqa: BLE001
                return CacheOutcome(
                    graph_id=job_id, ok=False,
                    failure=FailureKind.CHANNEL_ERROR,
                    reason=f"产物抓取失败：{type(exc).__name__}: {exc}",
                    render=out, seconds=dt)
            if nm in blobs:
                return CacheOutcome(
                    graph_id=job_id, ok=False, failure=FailureKind.CHANNEL_ERROR,
                    reason=f"产物文件名冲突：{nm} —— 命名规则不唯一",
                    render=out, seconds=dt)
            blobs[nm] = blob

        if not blobs:
            return CacheOutcome(graph_id=job_id, ok=False,
                                failure=FailureKind.JOB_FAILED,
                                reason="渲染成功但没抓到任何产物字节",
                                render=out, seconds=dt)

        if store and self.enabled and key:
            try:
                self.cache.store(ir_hash=key, artifacts=blobs,
                                 render_seconds=dt,
                                 extra_provenance={"jobId": job_id,
                                                   "channel": "http"})
            except Exception:                                 # noqa: BLE001
                pass
            self._by_ir[key] = ("", blobs, dt)

        # ---- 记完成 + 产物名 ----
        #
        # 产物名必须与缓存层的命名一致，否则续跑时对账回来的名字
        # 在缓存里找不到。
        self._ck_succeed(job_id, sorted(blobs))

        return CacheOutcome(graph_id=job_id, ok=True, artifacts=blobs,
                            render=out, seconds=dt)

    def render_http_batch(self, items: Sequence[tuple[Mapping[str, Any], str]],
                          backend: Any,
                          **kw: Any) -> CachedBatch:
        """``items`` 是 ``[(values, job_id), ...]``。"""
        return CachedBatch(outcomes=tuple(
            self.render_http(v, backend, job_id=j, **kw) for v, j in items))

    # ---- 失效 ----------------------------------------------------------

    def invalidate(self, ir: Mapping[str, Any]) -> int:
        """让**某个镜头**的缓存失效（改了它之后调用）。

        这是"改一句只重跑受影响镜头"的**另一半**：
        命中负责"不受影响的照旧"，失效率责"受影响的重来"。
        """
        from little_crew.ir.canonical import content_hash

        try:
            ir_hash = content_hash(ir)
        except Exception:                                     # noqa: BLE001
            return 0
        self._by_ir.pop(ir_hash, None)
        return self.cache.invalidate_by_ir(ir_hash)

    def stats(self) -> dict[str, Any]:
        """合并「内容缓存」与「进程内索引」的统计。

        ⚠️ 仍然**不含** ComfyUI 自身的节点级缓存 —— 那是另一个来源，
        混进来会让节省比例虚高且无法解释。
        """
        base = self.cache.stats.as_dict()
        base["memoIndexSize"] = len(self._by_ir)
        base["enabled"] = self.enabled
        return base


def _name_of(ref: Any) -> str:
    """从产物引用推一个**稳定且唯一**的本地文件名。

    两条要求缺一不可：

    | 要求 | 为什么 |
    |---|---|
    | **稳定** | 同一产物两次渲染要得到同一名字，否则回读时找不到 |
    | **唯一** | 不同产物**绝不能**同名，否则互相覆盖 |

    ⚠️ 这里曾犯过一个典型错误：只按 `/` 取路径末段算名字，
    于是 ComfyUI 的 ``/view?filename=SH_001.png`` 得到的是 **``view``** ——
    所有镜头的产物都存成 ``view``，**后一个覆盖前一个**。
    缓存看起来"有东西"，实际只剩一张图。

    这类缺陷极隐蔽：不报错、目录里有文件、命中率也正常，
    只有去比对"命中的产物是不是当初那一张"才会暴露。

    规则：优先取查询串里的 ``filename``，否则取路径末段；
    两者都拿不到时用 URI 的哈希兜底。最后**统一加 URI 哈希前缀**，
    保证任何情况下都唯一。
    """
    uri = str(getattr(ref, "uri", "") or "")
    if not uri:
        raise ValueError("产物引用缺少 uri")

    name = ""
    # 1) 优先取查询串里的 filename（ComfyUI 的 /view 就是这种形态）
    if "?" in uri:
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(uri).query)
        cand = (q.get("filename") or q.get("file") or q.get("name") or [""])[0]
        if cand:
            name = str(cand).strip()

    # 2) 退回路径末段
    if not name:
        tail = uri.split("?")[0].rstrip("/").split("/")[-1]
        # `view` 这类是**路由名而非文件名** —— 用它做名字必然撞车
        if tail and tail not in _ROUTE_WORDS:
            name = tail

    # 3) 兜底
    if not name:
        name = "artifact"

    # 4) **无条件**加 URI 哈希前缀，保证唯一
    #
    # 即便上游给了同名文件（不同子目录下的同名产物是常见情况），
    # 也不会互相覆盖。多这 8 个字符，换掉一类静默损坏。
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:8]
    return f"{digest}_{name}"


#: 会被误当文件名的**路由词**。用它们做键必然撞车。
_ROUTE_WORDS = frozenset({
    "view", "files", "file", "image", "images", "output", "download",
    "api", "v1", "result", "results", "get", "media", "static",
})
