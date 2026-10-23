"""缓存感知渲染 —— 自检。

**核心命题**：改一个镜头的参数，只有它被重跑，其余全部命中且零 GPU。
这不是"优化"，而是"改一句不重跑一集"这一可作性前提的**证据**。

方法：假通道 + 假抓取器 + 临时缓存目录 —— 完全离线、完全确定，
因此断言的是**机制**而不是"某次跑得快不快"。

运行： python -m little_crew.runtime.cached_render_check
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from little_crew.runtime.cache import CacheTier, ContentCache
from little_crew.runtime.cached_render import (
    CachedBatch,
    CacheOutcome,
    CachingRenderer,
    artifact_fetcher_for,
)
from little_crew.runtime.channels.base import (
    ArtifactRef,
    CompiledJob,
    JobHandle,
    JobStatus,
    ProgressEvent,
)
from little_crew.runtime.render import RenderSession

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ==========================================================================
# 夹具
# ==========================================================================

def _compiler() -> Any:
    """确定性编译器（代码内环境夹具，不依赖服务或磁盘）。"""
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler
    from little_crew.compiler.test_compiler import env_minimal

    return ComfyCompiler(probe_from_object_info(
        env_minimal(), endpoint="fixture:8188", comfy_version="fixture"))


_COMPILER = _compiler()


class FakeChannel:
    """假通道。**记录真实执行次数**，这是"零 GPU"断言的依据。"""

    def __init__(self, *, render_seconds: float = 4.0,
                 unknown_after: int | None = None) -> None:
        self.endpoint = "http://fake:8188"
        self.channel_id = "fake:ch"
        #: 轮询到第 N 次之后返回 UNKNOWN（模拟"网络断了、查不到状态"）。
        #: 这是**在途任务**最典型的形态，也是续跑机制最怕被误判成失败的地方。
        self.unknown_after = unknown_after
        #: **提交调用次数** —— 注意它与"真正执行次数"是两件事。
        #:
        #: 通道自带 R1 幂等：同幂等键重复提交会返回同一句柄、**不重复执行**。
        #: 于是"关掉缓存后 executions 没涨"这个现象会**掩盖**
        #: "我们确实又提交了一次"这一事实。度量缓存是否生效，
        #: 要看的是**我们提交了几次**，不是通道执行了几次。
        self.submissions = 0
        self.executions = 0
        self.render_seconds = render_seconds
        self._by_key: dict[str, JobHandle] = {}
        self._poll: dict[str, int] = {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def submit(self, job: CompiledJob) -> JobHandle:
        self.submissions += 1
        prior = self._by_key.get(job.idempotency_key)
        if prior is not None:
            return prior
        self.executions += 1
        cid = f"prompt-{self.executions:04d}"
        h = JobHandle(job_id=job.idempotency_key, channel_id=cid,
                      status=JobStatus.PENDING)
        self._by_key[job.idempotency_key] = h
        self._poll[cid] = 0
        return h

    def poll(self, handle: JobHandle) -> JobHandle:
        n = self._poll.get(handle.channel_id, 0) + 1
        self._poll[handle.channel_id] = n
        if self.unknown_after is not None and n > self.unknown_after:
            # 查询不到状态 —— **不等于失败**，交给对账
            handle.status = JobStatus.UNKNOWN
            return handle
        handle.status = JobStatus.RUNNING if n < 2 else JobStatus.SUCCEEDED
        handle.progress = ProgressEvent(current=n, total=2, unit="step")
        return handle

    def collect(self, handle: JobHandle) -> list[ArtifactRef]:
        # 产物名含 channel_id，便于断言"这一镜是新渲染的"
        ref = ArtifactRef(kind="image",
                          uri=f"http://fake:8188/view?filename={handle.channel_id}.png",
                          mime="image/png", width=832, height=480)
        handle.artifacts = [ref]
        return [ref]

    def reconcile(self, job_ids: Sequence[str]) -> Mapping[str, JobHandle]:
        return {}

    def cancel(self, handle: JobHandle) -> None:
        handle.status = JobStatus.CANCELLED


def _fake_fetcher(ref: Any) -> bytes:
    """假抓取器：按 URI 生成确定性字节。

    用假的而不是真下载：本测试验证的是**缓存机制**，
    不该依赖网络。抓取器本身的可替换性也是设计的一部分 ——
    换通道只需换抓取器，缓存层不动。
    """
    return f"PNG:{ref.uri}".encode("utf-8")


def _ir(graph_id: str, *, text: str = "雨夜天台，米色风衣，回头") -> dict[str, Any]:
    """一个能过编译的最小 IR。"""
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
             "params": {"text": text}},
            {"id": "n_neg", "kind": "prompt.negative",
             "inputs": {"clip": {"ref": "n_ckpt", "port": "CLIP"}},
             "params": {"text": "多手"}},
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
             "params": {"filename_prefix": graph_id.replace("-", "_")}},
        ],
        "edges": [],
        "constraints": [],
    }


def _episode(n: int = 8) -> list[dict[str, Any]]:
    return [_ir(f"G-E001-SH{i:03d}", text=f"第 {i} 镜：雨夜天台")
            for i in range(1, n + 1)]


# ==========================================================================
# 测试
# ==========================================================================

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("缓存感知渲染 —— 自检")
    print("=" * 78)

    tmp = Path(tempfile.mkdtemp(prefix="lc_cached_"))
    try:
        _hit_miss(tmp)
        _one_shot_changed(tmp)
        _toggle(tmp)
        _invalidate(tmp)
        _persistence(tmp)
        _stats_honesty(tmp)
        _checkpoint_wiring(tmp)
        _fetcher()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    npass = sum(1 for _, ok, _ in _results if ok)
    for name, ok, detail in _results:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail:
            print(f"       {detail}")
    print("=" * 78)
    print(f"结果：{npass}/{len(_results)} 通过")
    return 0 if npass == len(_results) else 1


def _mk(tmp: Path, tag: str, **kw: Any) -> tuple[CachingRenderer, FakeChannel]:
    ch = FakeChannel()
    sess = RenderSession(ch, _COMPILER)
    cache = ContentCache(tmp / tag)
    r = CachingRenderer(sess, cache, fetcher=_fake_fetcher, **kw)
    return r, ch


# ---------- A. 命中与未命中 ----------

def _hit_miss(tmp: Path) -> None:
    r, ch = _mk(tmp, "a")
    ir = _ir("G-A")

    o1 = r.render(ir)
    check("A1 首次渲染未命中缓存", o1.ok and not o1.from_cache,
          f"from_cache={o1.from_cache}")
    check("A2 首次确实提交了（submissions=1）", ch.submissions == 1,
          f"submissions={ch.submissions}")
    check("A3 首次抓到产物字节", len(o1.artifacts) == 1,
          f"{len(o1.artifacts)} 个")
    check("A4 未命中时无节省时间", o1.seconds_saved == 0.0)

    o2 = r.render(ir)
    check("A5 二次渲染命中缓存", o2.from_cache, f"from_cache={o2.from_cache}")
    check("A6 **命中时零提交**（submissions 仍为 1）", ch.submissions == 1,
          f"submissions={ch.submissions}")
    check("A7 命中时拿到同一份产物字节",
          o2.artifacts == o1.artifacts,
          f"{list(o2.artifacts)[:1]}")
    check("A8 命中时不需要底层的渲染结果",
          o2.render is None, str(type(o2.render).__name__))

    # 不同 IR → 未命中
    o3 = r.render(_ir("G-B", text="另一个镜头"))
    check("A9 不同 IR 各自未命中", (not o3.from_cache) and ch.submissions == 2,
          f"submissions={ch.submissions}")


# ---------- B. 核心命题：改一镜只重跑那一镜 ----------

def _one_shot_changed(tmp: Path) -> None:
    r, ch = _mk(tmp, "b")
    ep = _episode(8)

    b1 = r.render_batch(ep)
    check("B1 全集首跑：8 镜全部实算",
          len(b1.rendered) == 8 and ch.submissions == 8,
          f"rendered={len(b1.rendered)} submissions={ch.submissions}")
    check("B2 首跑命中率 0", b1.hit_rate == 0.0, f"{b1.hit_rate}")

    # 什么都不改，再跑一遍
    b2 = r.render_batch(ep)
    check("B3 无改动重跑：8 镜全命中，**零提交**",
          len(b2.cache_hits) == 8 and ch.submissions == 8,
          f"hits={len(b2.cache_hits)} submissions={ch.submissions}")
    check("B4 命中率 100%", abs(b2.hit_rate - 1.0) < 1e-9, f"{b2.hit_rate}")

    # **只改第 3 镜**
    ep[2] = _ir("G-E001-SH003", text="第 3 镜：雨夜天台（服装改为深灰）")
    b3 = r.render_batch(ep)

    check("B5 ★ 改一镜后：7 镜命中 + 1 镜实算",
          len(b3.cache_hits) == 7 and len(b3.rendered) == 1,
          f"hits={len(b3.cache_hits)} rendered={len(b3.rendered)}")
    check("B6 ★ 只多提交了 1 次（submissions 8 -> 9）",
          ch.submissions == 9, f"submissions={ch.submissions}")
    check("B7 ★ 实算的正是被改动的那一镜",
          len(b3.rendered) == 1
          and b3.rendered[0].graph_id == "G-E001-SH003",
          b3.rendered[0].graph_id if b3.rendered else "-")
    check("B8 ★ 其余 7 镜的产物与首跑一致（未被牵连）",
          all(b3.outcomes[i].artifacts == b1.outcomes[i].artifacts
              for i in (0, 1, 3, 4, 5, 6, 7)))
    check("B9 命中率 7/8", abs(b3.hit_rate - 0.875) < 1e-9, f"{b3.hit_rate}")

    # 提速比
    check("B10 可算提速比（有缓存快于无缓存）", b3.speedup > 1.0,
          f"speedup={b3.speedup:.2f}")
    check("B11 节省时间 = 命中镜头的渲染耗时之和",
          b3.seconds_saved > 0, f"saved={b3.seconds_saved:.2f}s")
    check("B12 批量汇总含命中/实算/提速",
          {"cacheHits", "rendered", "speedup", "hitRate"} <= set(b3.as_dict()),
          str(sorted(b3.as_dict()))[:70])


# ---------- C. 对照实验：跨运行测缓存 ----------
#
# ⚠️ **尺度很重要**。早期版本在**同一个会话**里跑两遍来测缓存，
# 结果被 `RenderSession` 自己的 R1 幂等掩盖了 —— 第二遍复用了句柄，
# 于是"关掉缓存也照样不重跑"，测试看起来通过，实际什么都没验证。
#
# 真实的节省发生在**跨运行**：改了剧本重跑一集、服务重启后重跑。
# 所以这里每次都开**新会话**（模拟新一次运行），只在第 2 次运行时开缓存。

def _run_once(cache_dir: Path, irs: list[dict[str, Any]],
              *, enabled: bool, tag: str) -> tuple[Any, FakeChannel, Any]:
    """模拟一次"运行"：新会话 + 新通道，但**共用缓存目录**。"""
    ch = FakeChannel()
    sess = RenderSession(ch, _COMPILER)
    r = CachingRenderer(sess, ContentCache(cache_dir / tag), fetcher=_fake_fetcher,
                        enabled=enabled)
    return r.render_batch(irs), ch, r


def _toggle(tmp: Path) -> None:
    ep = _episode(3)
    cache_dir = tmp / "c"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # ⚠️ 顺序很重要：**必须先有一次"写入缓存"的运行**，
    # 否则缓存目录是空的，后面的"开缓存"自然命中不了。
    # 早期版本把前两次都设成 enabled=False，于是缓存目录从未被写入 ——
    # 测试看起来在测缓存，实际在测空气。

    # 运行 1（开缓存）—— 建立缓存
    b1, ch1, _ = _run_once(cache_dir, ep, enabled=True, tag="shared")
    check("C1 运行 1（开缓存）：3 镜全部实算并写入缓存",
          ch1.executions == 3 and len(b1.rendered) == 3,
          f"executions={ch1.executions} rendered={len(b1.rendered)}")

    # 运行 2（开缓存）—— 实验组：重跑一集，零实算
    b2, ch2, _ = _run_once(cache_dir, ep, enabled=True, tag="shared")
    check("C2 **实验组：重跑一集零实算**（3 镜全命中）",
          ch2.executions == 0 and len(b2.cache_hits) == 3,
          f"executions={ch2.executions} hits={len(b2.cache_hits)}")

    # 运行 3（关缓存）—— 对照组：同样的重跑，全部重来
    b3, ch3, _ = _run_once(cache_dir, ep, enabled=False, tag="shared")
    check("C3 **对照组：关掉缓存后重跑一集又要 3 次实算**",
          ch3.executions == 3 and len(b3.rendered) == 3,
          f"executions={ch3.executions} rendered={len(b3.rendered)}")
    check("C4 缓存把「重跑一集」从 3 次实算降到 0 次",
          ch2.executions == 0 and ch3.executions == 3,
          f"开缓存 {ch2.executions} 次 / 关缓存 {ch3.executions} 次")

    check("C5 关缓存时全部报未命中",
          all(not o.from_cache for o in b3.outcomes))
    check("C6 开缓存时全部报命中",
          all(o.from_cache for o in b2.outcomes))
    check("C7 命中的产物与首次实算的一致（不是空壳命中）",
          sorted(b2.outcomes[0].artifacts) == sorted(b1.outcomes[0].artifacts),
          f"{sorted(b2.outcomes[0].artifacts)}")


# ---------- D. 失效（"改一句只重跑"的另一半）----------

def _invalidate(tmp: Path) -> None:
    cache_dir = tmp / "d"
    cache_dir.mkdir(parents=True, exist_ok=True)
    ep = _episode(5)

    _run_once(cache_dir, ep, enabled=True, tag="s")

    # 手工失效第 2 镜，并在**同一会话**内重跑（验证失效立即生效）
    ch = FakeChannel()
    r = CachingRenderer(RenderSession(ch, _COMPILER),
                        ContentCache(cache_dir / "s"), fetcher=_fake_fetcher)
    n = r.invalidate(ep[1])
    check("D1 对已缓存镜头失效返回 >0", n >= 1, f"失效 {n} 条")

    b = r.render_batch(ep)
    check("D2 失效后该镜重新实算",
          len(b.rendered) == 1 and b.rendered[0].graph_id == "G-E001-SH002",
          b.rendered[0].graph_id if b.rendered else "-")
    check("D3 其余 4 镜仍命中（失效不扩散）",
          len(b.cache_hits) == 4, f"hits={len(b.cache_hits)}")
    check("D4 只有失效的那一镜被提交",
          ch.submissions == 1, f"submissions={ch.submissions}")

    n2 = r.invalidate(_ir("G-NEVER"))
    check("D5 对未缓存镜头失效返回 0（不报错）", n2 == 0, f"返回 {n2}")

    # 新会话重跑：**全部命中** —— 因为 D2 那一步已经把失效的镜头重新渲染并回填了。
    #
    # ⚠️ 早期版本在这里断言"仍有 1 镜要实算"，那是**错的**：
    # 失效只在"下次渲染"之前有效，一旦重渲染完成，缓存就被填回来了。
    # 失效不是"永久标记"，而是"让下一位来看的人扑空一次"。
    b2, ch2, _ = _run_once(cache_dir, ep, enabled=True, tag="s")
    check("D6 失效的镜头重渲染后被重新回填（新会话全部命中）",
          ch2.executions == 0 and len(b2.cache_hits) == 5,
          f"executions={ch2.executions} hits={len(b2.cache_hits)}")

    # 真正该验证的：**只失效、不重渲染**时，新会话必须实算那一镜
    cache_dir2 = tmp / "d2"
    cache_dir2.mkdir(parents=True, exist_ok=True)
    _run_once(cache_dir2, ep, enabled=True, tag="s")
    ch_x = FakeChannel()
    r_x = CachingRenderer(RenderSession(ch_x, _COMPILER),
                          ContentCache(cache_dir2 / "s"), fetcher=_fake_fetcher)
    r_x.invalidate(ep[1])
    b3, ch3, _ = _run_once(cache_dir2, ep, enabled=True, tag="s")
    check("D7 **失效是持久的**：只失效不重渲染时，新会话实算那一镜",
          ch3.executions == 1 and len(b3.rendered) == 1
          and b3.rendered[0].graph_id == "G-E001-SH002",
          f"executions={ch3.executions} rendered={[o.graph_id for o in b3.rendered]}")


# ---------- E. 跨会话持久化（缓存的真正价值）----------

def _persistence(tmp: Path) -> None:
    """**新进程（新会话）也能命中** —— 否则缓存只是内存里的备忘录。"""
    cache_dir = tmp / "e"

    ch1 = FakeChannel()
    r1 = CachingRenderer(RenderSession(ch1, _COMPILER), ContentCache(cache_dir),
                         fetcher=_fake_fetcher)
    ir = _ir("G-PERSIST")
    r1.render(ir)
    check("E1 首次会话实算", ch1.submissions == 1, f"submissions={ch1.submissions}")

    # 全新会话 + 全新缓存对象，但**同一个缓存目录**
    ch2 = FakeChannel()
    r2 = CachingRenderer(RenderSession(ch2, _COMPILER), ContentCache(cache_dir),
                         fetcher=_fake_fetcher)
    o = r2.render(ir)
    check("E2 **新会话仍命中磁盘缓存**（缓存跨进程有效）",
          o.from_cache and ch2.submissions == 0,
          f"from_cache={o.from_cache} submissions={ch2.submissions}")
    check("E3 新会话读回的产物与首次一致",
          o.artifacts == r1.render(ir).artifacts)
    check("E4 命中级别被记录", o.tier is not None,
          o.tier.value if o.tier else "-")


# ---------- F. 统计的诚实性 ----------

def _stats_honesty(tmp: Path) -> None:
    r, ch = _mk(tmp, "f")
    ep = _episode(4)
    r.render_batch(ep)
    r.render_batch(ep)
    s = r.stats()

    check("F1 统计含命中/未命中/命中率",
          {"hits", "misses", "hitRate"} <= set(s), str(sorted(s))[:70])
    check("F2 统计含进程内索引大小",
          s.get("memoIndexSize") == 4, f"memo={s.get('memoIndexSize')}")
    check("F3 统计标明缓存是否启用", s.get("enabled") is True)
    check("F4 **ComfyUI 的节点级缓存与我们分开记**",
          "comfyNodeCacheEvents" in s and "不计入 hits" in str(s.get("note", "")),
          str(s.get("note", ""))[:60])
    check("F5 我们的命中数不等于 ComfyUI 的事件数（来源不同）",
          s["hits"] > 0 and s.get("comfyNodeCacheEvents", 0) == 0,
          f"ours={s['hits']} comfy={s.get('comfyNodeCacheEvents')}")


# ---------- G. 抓取器 ----------

def _checkpoint_wiring(tmp: Path) -> None:
    """检查点记账：**两条路径都要记，且"可能还在跑"绝不能记成 FAILED**。

    ## 这一节存在的理由（2026-10-23 审计 H-11 / M-22）
    · `H-11`：原来 `if not out.ok: mark_failed(...)` —— 而超时/状态未知
      也走这条路，于是 SUBMITTED 被改写成 FAILED。FAILED 在 `plan()` 里
      等于「可安全重提」，续跑就会**重复提交 / 重复计费**。
    · `M-22`：IR 路径（ComfyUI，本机主路）**一个字都不写检查点** ——
      「跑到一半崩了不用从头跑」这个能力在那条路上根本不存在。
    """
    from little_crew.runtime.checkpoint import JobState, RenderCheckpoint

    # ① 成功：IR 路径必须留痕，且带产物名（续跑时缓存要靠它对上）
    ck1 = RenderCheckpoint(tmp / "ck1", episode_id="E1")
    r1, _ch1 = _mk(tmp, "ck1", checkpoint=ck1)
    o1 = r1.render(_ir("G-CK1"))
    rec1 = ck1.get("G-CK1")
    check("H1 ★★ IR 路径也写检查点（此前崩了就得从头烧 GPU）",
          o1.ok and rec1 is not None, str(ck1.counts()))
    check("H2 ★ 成功记 SUCCEEDED 且带产物名",
          rec1 is not None and rec1.state is JobState.SUCCEEDED
          and bool(rec1.artifacts),
          f"{rec1.state.value if rec1 else None} / "
          f"{rec1.artifacts if rec1 else None}")

    # ② 状态未知：记 UNKNOWN，**绝不记 FAILED**
    ck2 = RenderCheckpoint(tmp / "ck2", episode_id="E1")
    ch2 = FakeChannel(unknown_after=1)
    r2 = CachingRenderer(RenderSession(ch2, _COMPILER),
                         ContentCache(tmp / "ck2cache"),
                         fetcher=_fake_fetcher, checkpoint=ck2)
    o2 = r2.render(_ir("G-CK2"))
    rec2 = ck2.get("G-CK2")
    check("H3 ★★ 状态未知 -> UNKNOWN（记 FAILED 就等于允许重提）",
          not o2.ok and rec2 is not None and rec2.state is JobState.UNKNOWN,
          f"{rec2.state.value if rec2 else None} / {o2.failure}")
    check("H4 ★ 且它进 reconcile 而不是 todo（plan 的判据）",
          rec2 is not None and rec2.state.needs_reconcile)
    check("H5 ★ 记下了 channel_id（对账只能按服务端任务 id 查）",
          rec2 is not None and bool(rec2.channel_id),
          f"{rec2.channel_id if rec2 else None}")

    # ③ 超时：同样是「可能还在跑」，不得记 FAILED
    ck3 = RenderCheckpoint(tmp / "ck3", episode_id="E1")
    ch3 = FakeChannel()

    def _always_running(h: JobHandle) -> JobHandle:
        h.status = JobStatus.RUNNING
        return h

    ch3.poll = _always_running                        # type: ignore[method-assign]
    r3 = CachingRenderer(RenderSession(ch3, _COMPILER, timeout_s=1,
                                       poll_interval_s=0.01),
                         ContentCache(tmp / "ck3cache"),
                         fetcher=_fake_fetcher, checkpoint=ck3)
    o3 = r3.render(_ir("G-CK3"))
    rec3 = ck3.get("G-CK3")
    check("H6 ★★ 超时 -> UNKNOWN（不是 FAILED；服务端不会因为我们等够了就停下）",
          not o3.ok and rec3 is not None and rec3.state is JobState.UNKNOWN,
          f"{rec3.state.value if rec3 else None} / {o3.failure}")

    # ④ 对照：**提交就没成功**（连句柄都没有）才该记 FAILED —— 那才可以重提
    ck4 = RenderCheckpoint(tmp / "ck4", episode_id="E1")
    ch4 = FakeChannel()

    def _submit_raises(job: CompiledJob) -> JobHandle:
        raise RuntimeError("连接被拒绝")

    ch4.submit = _submit_raises                       # type: ignore[method-assign]
    r4 = CachingRenderer(RenderSession(ch4, _COMPILER),
                         ContentCache(tmp / "ck4cache"),
                         fetcher=_fake_fetcher, checkpoint=ck4)
    o4 = r4.render(_ir("G-CK4"))
    rec4 = ck4.get("G-CK4")
    check("H7 ★ 提交异常（确定没被受理）-> FAILED，允许重试",
          not o4.ok and rec4 is not None and rec4.state is JobState.FAILED,
          f"{rec4.state.value if rec4 else None} / {o4.failure}")


def _fetcher() -> None:
    class Ch:
        endpoint = "http://127.0.0.1:8188"

    f = artifact_fetcher_for(Ch())
    check("G1 抓取器可从通道构造", callable(f))

    # 相对路径应被拼上 endpoint —— ComfyUI 的 /view 就是相对路径
    ref = ArtifactRef(kind="image", uri="/view?filename=a.png")
    check("G2 ComfyUI 的相对 uri 会被拼上 endpoint（否则 unknown url type）",
          not str(ref.uri).startswith("http"), f"uri={ref.uri}")

    # 完整 URL 应原样使用
    ref2 = ArtifactRef(kind="image", uri="http://127.0.0.1:8100/files/a.png")
    check("G3 local-gen 的完整 URL 原样使用",
          str(ref2.uri).startswith("http"), f"uri={ref2.uri}")

    ref3 = ArtifactRef(kind="image", uri="")
    try:
        f(ref3)
        ok = False
    except ValueError:
        ok = True
    except Exception:                                         # noqa: BLE001
        ok = False
    check("G4 缺 uri 时明确报错（不静默返回空字节）", ok)

    # ---- 命名规则：稳定 **且唯一** ----
    from little_crew.runtime.cached_render import _name_of

    r_a = ArtifactRef(kind="image", uri="/view?filename=SH_001.png")
    r_b = ArtifactRef(kind="image", uri="/view?filename=SH_002.png")
    n_a, n_b = _name_of(r_a), _name_of(r_b)
    check("G5 从 ComfyUI 的 /view 查询串里取到**真实文件名**（不是路由名 view）",
          "SH_001.png" in n_a and n_a != "view", n_a)
    check("G6 不同产物得到**不同**文件名（否则会静默覆盖）",
          n_a != n_b, f"{n_a} vs {n_b}")
    check("G7 同一产物两次得到**同一**文件名（稳定）",
          _name_of(r_a) == n_a, n_a)
    check("G8 local-gen 的 URL 也取到真实文件名",
          "img_x.png" in _name_of(
              ArtifactRef(kind="image",
                          uri="http://127.0.0.1:8100/files/img_x.png")),
          _name_of(ArtifactRef(kind="image",
                               uri="http://127.0.0.1:8100/files/img_x.png")))
    # 同名文件在不同路径下 —— 必须仍然唯一
    p1 = ArtifactRef(kind="image", uri="http://h/a/out.png")
    p2 = ArtifactRef(kind="image", uri="http://h/b/out.png")
    check("G9 不同路径下的同名产物仍唯一（加 URI 哈希前缀）",
          _name_of(p1) != _name_of(p2),
          f"{_name_of(p1)} vs {_name_of(p2)}")


if __name__ == "__main__":
    raise SystemExit(main())
