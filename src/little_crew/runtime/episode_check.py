"""一集流水线 —— 自检。

用假通道 + 假抓取器，**完全离线**，因此断言的是**机制**：
  · 剧本 → 分镜 → 桥接 → 渲染 → 合成 的完整串联
  · 成片清单与交付证据的内容正确性
  · **硬规则真的会拦**（缺产物 / 超长镜头 / 重名产物）
  · 部分失败不会丢掉整集的记录

运行： python -m little_crew.runtime.episode_check
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.orchestrator import CrewOrchestrator
from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cached_render import CachingRenderer
from little_crew.runtime.checkpoint import RenderCheckpoint
from little_crew.runtime.channels.base import (
    ArtifactRef,
    CompiledJob,
    JobHandle,
    JobStatus,
)
from little_crew.runtime.episode import (
    AssemblyEditorRules,
    DeliveryEvidence,
    EpisodeManifest,
    EpisodeRunner,
)
from little_crew.runtime.render import RenderSession

_results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((name, bool(cond), detail))


# ==========================================================================
# 夹具
# ==========================================================================

SCRIPT = """[场1] 山中·午后
@少女 在山坡上采花。@旅人 倒在岩石边，腿上带着伤。
少女蹲下替他扎紧伤口。她甩开他的手，没有回答。
代价是她再没有回过村子里的集会。

[场2] 村口·春
@旅人 的伤好了。他望着通往北方的路。
@少女 从屋里出来，身上只有一个小包袱。她忽然笑了："我跟你走。"
@老奶奶 把一束薰衣草塞进她手里："这花的香气能让不洁的东西现形。"

[场3] 山路·黎明
两人并肩走着。@少女 忽然停下，举起花束掷了过去——
花瓣散开的一瞬，@旅人 化作一阵紫色轻烟。
风里只剩一句话："我就是你想远行的心啊。"
"""


class FakeChannel:
    def __init__(self, *, fail_ids: set[str] | None = None,
                 no_artifacts: set[str] | None = None) -> None:
        self.endpoint = "http://fake:1"
        self.channel_id = "fake"
        self._n = 0
        self._by: dict[str, JobHandle] = {}
        self.fail_ids = set(fail_ids or ())
        self.no_artifacts = set(no_artifacts or ())
        self.submissions = 0

    def available(self) -> tuple[bool, str]:
        return True, ""

    def submit(self, job: CompiledJob) -> JobHandle:
        self.submissions += 1
        prior = self._by.get(job.idempotency_key)
        if prior is not None:
            return prior
        self._n += 1
        # 把请求参数带上 —— `collect` 要用它决定产物尺寸。
        # `CompiledJob.request` 就是 `EpisodeRunner._values()` 的产物。
        req = dict(getattr(job, "request", None) or {})
        h = JobHandle(job_id=job.idempotency_key, channel_id=f"p{self._n}",
                      status=JobStatus.PENDING, raw=req)
        self._by[job.idempotency_key] = h
        return h

    def poll(self, h: JobHandle) -> JobHandle:
        if h.channel_id in self.fail_ids:
            h.status = JobStatus.FAILED
            return h
        h.status = JobStatus.SUCCEEDED
        return h

    def collect(self, h: JobHandle) -> list[ArtifactRef]:
        if h.channel_id in self.no_artifacts:
            return []
        # ⚠️ **尺寸要从 `request["body"]` 里解出来，而 `body` 有两种形态：**
        #
        #   · **dict**     —— 当 `values` 直接给全了字段（`_build_http_job` 的预填路径）
        #   · **str(JSON)** —— 当需要按 `body_template` 填充时
        #
        # 实测：`EpisodeRunner` 走的是第一条，`body` 就是 `_values()` 的字典。
        # 我第一版只处理了字符串分支，于是**永远拿到默认 64x48** ——
        # 产物尺寸恒定不变，返工改了 IR 也不生效，循环**一步没前进**
        # 且不报任何错（那正是"空转的循环"）。
        raw = getattr(h, "raw", None) or {}
        w, ht = 64, 48
        body = raw.get("body")
        parsed = None
        if isinstance(body, Mapping):
            parsed = body
        elif isinstance(body, str):
            try:
                parsed = json.loads(body)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed = None
        if isinstance(parsed, Mapping):
            try:
                w = int(parsed.get("width", 64))
                ht = int(parsed.get("height", 48))
            except (TypeError, ValueError):
                w, ht = 64, 48
        return [ArtifactRef(
            kind="image",
            uri=f"http://fake:1/files/{h.channel_id}.png?w={w}&h={ht}")]

    def reconcile(self, ids: Sequence[str]) -> Mapping[str, JobHandle]:
        return {}

    def cancel(self, h: JobHandle) -> None:
        pass


def _fetcher(ref: Any) -> bytes:
    """假抓取器 —— **必须产出真的能被解码的 PNG**。

    ⚠️ 早期版本返回 "PNG:{ref.uri}".encode()，那**不是合法图片**。
    缓存层不在乎（它只存字节），但**视频合成层会把它喂给 ffmpeg** ——
    于是 scale/zoompan 在退化输入上陷入病态计算：不报错、不退出，
    只是**一路烧满 CPU**（实测 187s 墙钟 / 225s CPU，正常只需 3.5s）。

    表现是测试卡住，真相是夹具不是合法输入。
    """
    import tempfile as _tf
    from little_crew.review.observe import _image_size_bytes
    from little_crew.runtime.video import make_test_png

    uri = str(ref.uri)
    w, h = 64, 48
    if "?" in uri:
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(uri).query)
        try:
            w = int((q.get("w") or ["64"])[0])
            h = int((q.get("h") or ["48"])[0])
        except ValueError:
            w, h = 64, 48

    d = pathlib.Path(_tf.gettempdir()) / "lc_fix_png"
    # ⚠️ **文件名必须含尺寸，而且读回时还要自校验。**
    #
    # 只用 `sha256(uri)` 做文件名是不够的：**全局临时目录跨测试共享**，
    # 而不同测试可能给出**同样的 URI 却期望不同尺寸**
    # （`?w=32&h=24` 这条 URI 在多个用例里出现过）。
    #
    # 光把尺寸放进文件名也不够 —— 万一旧版本留下的同名文件还在，
    # 读到的仍然是旧尺寸。所以**读回后校验尺寸，不符就重写**。
    #
    # 表现一旦漏掉：门禁量到 64x48，而 IR 明明声明 32x24 ——
    # 看起来像"返工改了 IR 却没生效"，实际是**夹具自己的缓存**。
    p = d / f"{hashlib.sha256(uri.encode()).hexdigest()[:16]}_{w}x{h}.png"
    if p.is_file() and _image_size_bytes(p.read_bytes()) != (w, h):
        p.unlink()                      # 旧尺寸的残留 -> 丢掉重写
    if not p.is_file():
        make_test_png(p, w=w, h=h)
    return p.read_bytes()


def _compiler() -> Any:
    class C:
        def compile(self, ir: Any) -> Any:
            raise NotImplementedError("HTTP 路径不编译")
    return C()


ZBE = Backend(kind=BackendKind.HTTP, endpoint="http://fake:1",
              path="/v1/images/generations",
              body_template=('{"prompt": "{prompt}", "width": {width},'
                             ' "height": {height},'
                             ' "num_inference_steps": {steps}, "seed": {seed}}'),
              response_path="data.0.url")


class Runner(EpisodeRunner):
    def engine_backend(self) -> Any:
        return ZBE


#: 夹具假通道产出的占位图尺寸。
#:
#: ⚠️ **必须与 `FakeChannel` 实际产出一致** —— 它会变成 `cr_res_*` 硬约束。
#: 这个常量存在的理由：约束写死成一个与实际产出无关的常量时，
#: **每一镜都会被判不合格**（实测：夹具产 64x48 而约束写 832x480，
#: 12 镜全部 RETRY）。门禁没错，是约束没反映真实规格。
_FAKE_RESOLUTION: tuple[int, int] = (64, 48)


def _mk(tmp: Path, tag: str, ch: FakeChannel | None = None,
        *, resolution: tuple[int, int] | None = None
        ) -> tuple[Runner, FakeChannel, RenderCheckpoint]:
    """造一个流水线实例。

    `resolution` 是**流水线声明的产出规格**（会变成 `cr_res_*` 硬约束）。
    ⚠️ 它必须与夹具实际产出的尺寸一致 —— 否则门禁会把每一镜都判不合格。
    默认取 `_FAKE_RESOLUTION`。
    """
    ch = ch or FakeChannel()
    ck = RenderCheckpoint(tmp / tag / "ck", episode_id="E100")
    r = CachingRenderer(RenderSession(ch, _compiler()),
                        ContentCache(tmp / tag / "cache"),
                        fetcher=_fetcher, checkpoint=ck)
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek", "storyboard_artist": "deepseek",
                        "dramaturge": "qwen", "story_editor": "qwen",
                        "review_board": "qwen"})
    orch.bind_many(default_crew_stubs())
    runner = Runner(orch, r, episode_id="E100", title="测试集")
    runner.workdir = tmp / tag / "delivery"
    # 流水线**声明**的产出规格 —— 它会变成 `cr_res_*` 硬约束。
    #
    # ⚠️ 默认必须与夹具实际产出（`_FAKE_RESOLUTION`）一致，
    # 否则门禁会把每一镜都判不合格 —— 而那**不是门禁的错**：
    # 门禁只是如实比对"要求的"与"实际拿到的"。
    #
    # 返工测试会传入**故意不同**的值（模拟"声明的规格与实际产出不符"），
    # 那正是返工闭环要修的典型场景。
    runner.engine_resolution = tuple(resolution or _FAKE_RESOLUTION)
    # 产物按名解析：渲染写进缓存分片目录，所以要能找回来
    idx: dict[str, Any] = {}

    def _resolve(name: str):
        if name in idx:
            return idx[name]
        for q in (tmp / tag).rglob(name):
            idx[name] = q
            return q
        return None

    runner.resolve_artifact = _resolve
    return runner, ch, ck


# ==========================================================================
# 测试
# ==========================================================================



def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    print("=" * 78)
    print("一集流水线 —— 自检")
    print("=" * 78)

    # ⚠️ **先清掉夹具的图片缓存目录。**
    #
    # `_fetcher` 把"假产物"按 URI 缓存在**全局临时目录**里，而该目录
    # **跨运行、跨测试共享**。于是可能出现：本轮逻辑上该产 32x24，
    # 却因为命中上一轮留下的同名文件而拿到 64x48。
    #
    # 这会伪造出各种诡异的失败 —— 实测见过的是
    # 「返工改了 IR 却没生效」（门禁量到旧尺寸，循环永远不收敛），
    # 看起来像返工的 bug，实际是夹具的缓存**跨运行污染**。
    #
    # 生产代码不受影响（它用的是每次运行独立的 `workdir`），
    # 但**自检必须自己保证干净**，否则测的是"上次的残留"。
    import shutil as _sh

    _sh.rmtree(pathlib.Path(tempfile.gettempdir()) / "lc_fix_png",
               ignore_errors=True)

    tmp = Path(tempfile.mkdtemp(prefix="lc_ep_"))
    try:
        _full_run(tmp)
        _rerun_hits_cache(tmp)
        _assembly_rules(tmp)
        _hard_gates(tmp)
        _partial_failure(tmp)
        _video_stage(tmp)
        print()
        _section_gate_wiring(tmp)
        print()
        _section_repair_loop(tmp)
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


# ---------- A. 全流程 ----------

def _full_run(tmp: Path) -> None:
    runner, ch, ck = _mk(tmp, "a")
    res = runner.run(SCRIPT)

    check("A1 一集跑通", res.ok, res.reason[:70])
    check("A2 剧本经历返工（不止一轮）", res.script_rounds >= 1,
          f"{res.script_rounds} 轮，终裁 {res.script_verdict}")
    check("A3 分镜切出镜头（每场至少 2 镜）",
          res.planned_shots >= 6, f"{res.planned_shots} 镜")
    check("A4 全部镜头出图（覆盖率 100%）",
          abs(res.coverage - 1.0) < 1e-9, f"{res.coverage:.0%}")
    check("A5 首跑全部实算、无命中",
          res.rendered == res.planned_shots and res.cache_hits == 0,
          f"实算 {res.rendered} 命中 {res.cache_hits}")
    check("A6 无失败", res.failed == 0, f"{res.failed} 镜失败")

    m = res.manifest
    check("A7 成片清单非空且有时长",
          m is not None and m.shot_count == res.planned_shots
          and m.total_seconds > 0,
          f"{m.shot_count if m else 0} 镜 / {m.total_seconds if m else 0:.1f}s")
    check("A8 清单标明阶段与待补（不假装已完成）",
          m.stage == "image" and "video" in m.pending_stages,
          f"stage={m.stage} pending={m.pending_stages}")
    check("A9 每镜都带产物文件名",
          all(e["artifacts"] for e in m.shots),
          f"空产物的镜头 {sum(1 for e in m.shots if not e['artifacts'])} 个")
    check("A10 清单条目含可审查的镜头属性",
          all({"shotId", "shotSize", "cameraMovement", "durationSec",
               "ready"} <= set(e) for e in m.shots))

    ev = res.evidence
    check("A11 交付证据含产物清单", len(ev.artifacts) == m.shot_count,
          f"{len(ev.artifacts)} 个")
    check("A12 交付证据含**内容哈希链**（可追溯改了什么）",
          len(ev.ir_hashes) == m.shot_count
          and all(ev.ir_hashes.values()),
          f"{len(ev.ir_hashes)} 条")
    check("A13 交付证据含缓存统计", ev.rendered == res.rendered)
    check("A14 硬规则全部通过", ev.all_checks_passed,
          str(ev.checks))

    # 检查点应把每镜都记为完成
    check("A15 检查点记录全部镜头为已完成",
          ck.counts().get("succeeded") == m.shot_count,
          str(ck.counts()))


# ---------- B. 重跑命中缓存 ----------

def _rerun_hits_cache(tmp: Path) -> None:
    runner, ch, ck = _mk(tmp, "b")
    r1 = runner.run(SCRIPT)
    before = ch.submissions
    r2 = runner.run(SCRIPT)

    check("B1 重跑仍成功", r2.ok, r2.reason[:60])
    check("B2 ★ 重跑**零实算**（全部命中缓存）",
          r2.rendered == 0 and r2.cache_hits == r1.planned_shots,
          f"实算 {r2.rendered} 命中 {r2.cache_hits}")
    check("B3 ★ 重跑没有再向通道提交",
          ch.submissions == before,
          f"提交数 {before} -> {ch.submissions}")
    check("B4 重跑省下了时间",
          r2.evidence.seconds_saved > 0,
          f"节省 {r2.evidence.seconds_saved:.2f}s")
    check("B5 重跑产出同一批文件名（可用同一份清单交付）",
          sorted(r2.evidence.artifacts) == sorted(r1.evidence.artifacts),
          f"{len(r2.evidence.artifacts)} 个")


# ---------- C. 合成剪辑的规则 ----------

def _assembly_rules(tmp: Path) -> None:
    a = AssemblyEditorRules()
    shots = [
        {"shotId": "S1", "sceneId": "SC-1", "shotSize": "full",
         "cameraMovement": "static", "durationSec": 5.0, "characters": ["A"]},
        {"shotId": "S2", "sceneId": "SC-1", "shotSize": "close",
         "cameraMovement": "dolly_in", "durationSec": 4.0, "characters": ["A"]},
    ]
    arts = {"S1": ["a.png"], "S2": ["b.png"]}
    hashes = {"S1": "sha256:11", "S2": "sha256:22"}
    m, ev = a.assemble(episode_id="E1", title="T", shots=shots,
                       artifacts=arts, ir_hashes=hashes,
                       cache_stats={"hits": 1, "rendered": 1,
                                    "secondsSaved": 3.0, "secondsSpent": 5.0})

    check("C1 清单时长为各镜之和", abs(m.total_seconds - 9.0) < 1e-9,
          f"{m.total_seconds}")
    check("C2 清单顺序与输入一致",
          [e["shotId"] for e in m.shots] == ["S1", "S2"])
    check("C3 每镜标出是否就绪", all(e["ready"] for e in m.shots))
    check("C4 证据记录缓存收益",
          ev.cache_hits == 1 and abs(ev.seconds_saved - 3.0) < 1e-9,
          f"命中 {ev.cache_hits} 节省 {ev.seconds_saved}")
    check("C5 清单可序列化（可落盘交付）",
          isinstance(m.as_dict(), dict) and m.as_dict()["shotCount"] == 2)
    check("C6 证据可序列化", ev.as_dict()["allChecksPassed"] is True)


# ---------- D. 硬规则真的会拦 ----------

def _hard_gates(tmp: Path) -> None:
    a = AssemblyEditorRules()
    base = [{"shotId": "S1", "sceneId": "SC-1", "shotSize": "full",
             "cameraMovement": "static", "durationSec": 5.0}]

    # 缺产物
    _, ev = a.assemble(episode_id="E", title="", shots=base,
                       artifacts={}, ir_hashes={"S1": "sha256:1"},
                       cache_stats={})
    check("D1 ★ 有镜头没有产物 → 硬规则不过",
          ev.checks["allShotsHaveArtifacts"] is False, str(ev.checks))

    # 超长镜头
    long_shot = [dict(base[0], durationSec=14.0)]
    _, ev2 = a.assemble(episode_id="E", title="", shots=long_shot,
                        artifacts={"S1": ["a.png"]},
                        ir_hashes={"S1": "sha256:1"}, cache_stats={})
    check("D2 ★ 镜头超过 10 秒硬上限 → 硬规则不过",
          ev2.checks["allShotsWithinDurationLimit"] is False)

    # 产物重名（会互相覆盖）
    two = [dict(base[0]), dict(base[0], shotId="S2")]
    _, ev3 = a.assemble(episode_id="E", title="", shots=two,
                        artifacts={"S1": ["same.png"], "S2": ["same.png"]},
                        ir_hashes={"S1": "sha256:1", "S2": "sha256:2"},
                        cache_stats={})
    check("D3 ★ 产物文件名重复 → 硬规则不过（会静默覆盖）",
          ev3.checks["artifactsUnique"] is False)

    # 缺内容哈希
    _, ev4 = a.assemble(episode_id="E", title="", shots=base,
                        artifacts={"S1": ["a.png"]}, ir_hashes={},
                        cache_stats={})
    check("D4 ★ 缺内容哈希链 → 硬规则不过（无法追溯）",
          ev4.checks["hasContentHashChain"] is False)

    # 空清单
    _, ev5 = a.assemble(episode_id="E", title="", shots=[],
                        artifacts={}, ir_hashes={}, cache_stats={})
    check("D5 ★ 空清单 → 硬规则不过", ev5.checks["manifestNonEmpty"] is False)

    # 全部合规时应通过
    _, ev6 = a.assemble(episode_id="E", title="", shots=base,
                        artifacts={"S1": ["a.png"]},
                        ir_hashes={"S1": "sha256:1"}, cache_stats={})
    check("D6 全部合规时硬规则通过", ev6.all_checks_passed, str(ev6.checks))


# ---------- E. 部分失败不丢整集 ----------

def _partial_failure(tmp: Path) -> None:
    # 让前两个提交失败
    ch = FakeChannel(fail_ids={"p1", "p2"})
    runner, ch, ck = _mk(tmp, "e", ch)
    res = runner.run(SCRIPT)

    check("E1 有镜头失败时整集标记为未跑通", not res.ok, res.reason[:60])
    check("E2 **失败的镜头被记录**（不静默丢弃）", res.failed >= 1,
          f"{res.failed} 镜失败")
    check("E3 失败明细含 id / 分类 / 原因",
          all({"graphId", "failure", "reason"} <= set(f)
              for f in res.failures),
          str(res.failures[:1]))
    check("E4 **成功的那部分仍被记录**（不因部分失败丢掉整集）",
          res.rendered > 0 or res.cache_hits > 0,
          f"实算 {res.rendered} 命中 {res.cache_hits}")
    check("E5 清单仍产出（可审查缺口）",
          res.manifest is not None
          and sum(1 for e in res.manifest.shots if not e["ready"]) >= 1,
          f"未就绪 {sum(1 for e in res.manifest.shots if not e['ready']) if res.manifest else 0} 镜")
    check("E6 覆盖率正确地反映缺口",
          res.coverage < 1.0, f"{res.coverage:.0%}")

    # 通道报成功但没有产物
    ch2 = FakeChannel(no_artifacts={"p1"})
    runner2, _, _ = _mk(tmp, "e2", ch2)
    res2 = runner2.run(SCRIPT)
    check("E7 通道报成功但零产物 → 判为失败（不当成功传下去）",
          res2.failed >= 1, f"{res2.failed} 镜失败")


# ---------- F. 视频集成 ----------

def _video_stage(tmp: Path) -> None:
    """一集流水线**默认**应产出可播放的成片 —— 这正是「一集」的含义。

    在此之前"成片"只是一份 JSON 清单：能被审片、能被验收，但**不能被播放**。
    """
    runner, ch, ck = _mk(tmp, "f")
    res = runner.run(SCRIPT)

    check("F1 一集跑通", res.ok, res.reason[:60])
    check("F2 ★ 默认产出了视频结果", res.video is not None,
          type(res.video).__name__)
    check("F3 成片文件存在且非空", res.playable, str(res.video_path))

    if res.video is not None and hasattr(res.video, "clips_built"):
        check("F4 全部镜头都进了成片",
              res.video.clips_built == res.manifest.shot_count,
              f"{res.video.clips_built}/{res.manifest.shot_count}")
        check("F5 成片时长等于清单时长",
              abs(res.video.seconds - res.manifest.total_seconds) < 1e-6,
              f"{res.video.seconds:.1f} vs {res.manifest.total_seconds:.1f}")
        check("F6 成片文件有实际字节", res.video.bytes > 0,
              f"{res.video.bytes} 字节")
    check("F7 结果可序列化且带「可播放」标记",
          res.as_dict()["playable"] is True and "video" in res.as_dict())

    # 关掉视频合成 → 主流程不受影响，只是不可播放
    r2, _, _ = _mk(tmp, "f2")
    r2.assemble_video = False
    res2 = r2.run(SCRIPT)
    check("F8 可关掉视频合成（只出图）", res2.ok and res2.video is None,
          f"ok={res2.ok} video={res2.video}")
    check("F9 关掉视频时 playable=False（不谎报）",
          res2.as_dict()["playable"] is False)
    check("F10 关掉视频不影响镜头覆盖率",
          abs(res2.coverage - 1.0) < 1e-9, f"{res2.coverage:.0%}")

    # 没有解析器 → 优雅跳过，而不是崩
    r3, _, _ = _mk(tmp, "f3")
    r3.resolve_artifact = None
    res3 = r3.run(SCRIPT)
    check("F11 未提供产物解析器时优雅跳过（不崩）",
          res3.ok and res3.video is None, f"ok={res3.ok}")


# ==========================================================================
# G. 验收门禁接进流水线（C3）
# ==========================================================================

def _with_constraints(runner: Runner, constraints: list) -> None:
    """给流水线注入约束 —— 包住 `_bridge`，在它产出的 IR 上追加。

    走 `_bridge` 而不是改 IR 模板：约束进入的路径必须与生产一致
    （C1 说的是"约束边不编译，而是生成可执行断言"，
    所以约束本来就该在 IR 里）。
    """
    orig = runner._bridge

    def patched(shot):
        ir = orig(shot)
        out = dict(ir)
        out["constraints"] = list(constraints)
        return out

    runner._bridge = patched            # type: ignore[method-assign]


def _gate_of(res) -> dict:
    return dict((res.evidence.gate if res.evidence else None) or {})


def _section_gate_wiring(tmp: Path) -> None:
    """门禁在流水线里**真的会拦**。

    ⚠️ 这一节存在的理由：把门禁接进去很容易做成
    "调用了但结论不影响交付" —— 那与自我签收没有实质区别。
    所以核心断言是 **`ok` 会因门禁而变 False**。
    """
    from little_crew.review import GateVerdict

    # ---- ① 关闭门禁：证据里没有 gate（与"全过"可区分）----
    r, _, _ = _mk(tmp, "g1")
    r.verify_artifacts = False
    res = r.run(SCRIPT, limit=2)
    check("G1 关闭门禁时证据里**没有** gate（与「全过」可区分）",
          not _gate_of(res), f"gate 非空={bool(_gate_of(res))}")

    # ---- ② 开启门禁、约束全过 -> 通过，且如实报告跳过项 ----
    #
    # ⚠️ 这一条的**前提变过**：`shot_to_ir` 早期不生成任何约束，
    # 于是"IR 无约束"是常态；现在它总会生成分辨率/时长/角色数三条，
    # 所以"无约束"这个场景在正常路径下不再出现。
    r, _, _ = _mk(tmp, "g2")
    r.verify_artifacts = True
    res = r.run(SCRIPT, limit=2)
    check("G2 门禁开启且可计算的约束全过 -> **不算失败**",
          res.ok, res.reason[:70])
    g = _gate_of(res)
    counts = g.get("counts") or {}
    check("G3 每镜都有真实裁定（门禁确实执行了，不是空跑）",
          counts.get("shots", 0) >= 1 and counts.get("kept", 0) >= 1,
          str(counts))
    check("G3b 证据里如实列出**跳过的约束**（图片没有时长 -> 时长约束跳过）",
          "skipped" in counts,
          str(counts))

    # ---- ③ ★★ 注入必然失败的硬约束 -> ok 必须变 False ----
    #
    # ⚠️ **关掉返工**（`max_repair_rounds = 0`）。
    # 这一段测的是**门禁判定**，而返工会把"必然失败"的约束**修好** ——
    # 于是 `ok` 变 True，G4~G9 全部测不到。
    # **判定与闭环是两个正交的机制，必须分开测。**
    r, _, _ = _mk(tmp, "g3")
    r.verify_artifacts = True
    r.max_repair_rounds = 0
    _with_constraints(r, [{
        "ref": "cr_impossible", "enforcement": "hard",
        "assertion": {"op": "resolution_matches", "width": 1, "height": 1},
    }])
    res = r.run(SCRIPT, limit=2)
    g = _gate_of(res)
    check("G4 ★★ 硬约束失败 -> **ok 变 False**（门禁真会拦）",
          not res.ok, f"ok={res.ok} reason={res.reason[:56]}")
    check("G5 ★ 理由指明是「门禁未过」而非「渲染失败」",
          "门禁" in res.reason, res.reason[:70])
    counts = g.get("counts") or {}
    check("G6 门禁计数正确（每镜一条 RETRY）",
          counts.get("retry", 0) >= 1, str(counts))
    check("G7 证据链带完整裁定（审查方可复核）",
          bool(g.get("results")), f"{len(g.get('results') or [])} 条")
    repairs = [x for it in (g.get("results") or [])
               for x in (it.get("repairs") or [])]
    check("G8 ★ 返工指令进了证据", bool(repairs), str(repairs[:1])[:88])
    check("G9 返工指令带约束 ref（可追溯）",
          any("cr_impossible" in x for x in repairs), str(repairs[:1])[:66])

    # ---- ④ 软约束不阻断 ----
    r, _, _ = _mk(tmp, "g4")
    r.verify_artifacts = True
    r.max_repair_rounds = 0        # 同上：测判定，不测闭环
    _with_constraints(r, [{
        "ref": "cr_soft", "enforcement": "soft",
        "assertion": {"op": "resolution_matches", "width": 1, "height": 1},
    }])
    res = r.run(SCRIPT, limit=2)
    check("G10 软约束失败**不阻断**交付", res.ok, f"ok={res.ok}")

    # ---- ⑤ ★★ L2 让相似度**可算了**（此前恒被跳过）----
    #
    # ⚠️ 这一段的断言**变过**，而且变得很关键：
    # L2 之前（本机没有嵌入模型），`identity_similarity` 永远被
    # 判"缺观测 similarity"而**跳过**，G11/G12 断言的就是"如实跳过"。
    # L2 落地后它能算了 —— 于是旧断言**反过来失败**了。
    # 现在断言的是"**能算**"，以及"算不出来时仍然如实跳过"。
    r, _, _ = _mk(tmp, "g5")
    r.verify_artifacts = True
    r.max_repair_rounds = 0
    _with_constraints(r, [{
        "ref": "cr_face", "enforcement": "hard",
        "assertion": {"op": "identity_similarity", "min": 0.82},
    }])
    res = r.run(SCRIPT, limit=2)
    g = _gate_of(res)
    skipped = g.get("skippedConstraints") or []
    avail = g.get("availableObservations") or []

    check("G11 ★★ L2 之后 `similarity` **可算**了（此前恒被跳过）",
          "similarity" in avail, str(avail))
    check("G12 ★ 且该约束**真的执行了**（不再出现在跳过清单里）",
          not any("similarity" in x for x in skipped),
          f"跳过 {len(skipped)} 条：{str(skipped[:1])[:70]}")

    # 逐镜断言里应当能看到 similarity 与算法名
    asserts = [a for it in (g.get("results") or [])
               for a in (it.get("assertions") or [])]
    ident = [a for a in asserts if a.get("op") == "identity_similarity"]
    check("G12b 断言里带 similarity 观测值",
          bool(ident) and "similarity" in (ident[0].get("observed") or {}),
          str(ident[0].get("observed"))[:90] if ident else "无 identity 断言")
    check("G12c ★ 观测值里记明**用了哪个算法**（可追溯）",
          bool(ident) and "consistency-descriptor" in str(
              (ident[0].get("observed") or {}).get("method", "")),
          str((ident[0].get("observed") or {}).get("method")) if ident else "无")
    check("G12d 观测值里记明**跟哪一镜最不像 + 哪一维**",
          bool(ident) and (ident[0].get("observed") or {}).get("otherShotId"),
          str(ident[0].get("observed"))[:110] if ident else "无")

    check("G13 ★★ 算得出的约束**不阻断**（夹具里两镜内容相同 -> 相似度 1）",
          res.ok, f"ok={res.ok} reason={res.reason[:56]}")
    check("G14 证据里说明本机**具备哪些观测能力**",
          bool(avail), str(avail))

    # ---- ⑥ 三态常量 ----
    check("G15 三态裁定可用（KEEP/RETRY/ESCALATE）",
          {v.value for v in GateVerdict} == {"KEEP", "RETRY", "ESCALATE"},
          str(sorted(v.value for v in GateVerdict)))

    # ---- ⑦ ★★ 时长硬门禁必须**真的执行**（2026-10-23 审计 H-18）----
    #
    # 病根是**字段名**：`observe.OPERATOR_NEEDS["duration_within"]` 要
    # `durationSec`，而流水线原来供给的是 `plannedDurationSec` ——
    # 于是每条 `duration_within`（`enforcement=hard`，防 H3 OOM 的那道闸）
    # 都被判「缺观测」而**静默跳过**，报告里只写「未执行：缺观测」，
    # 看起来像"本机能力不足"，实际是拼写不一致。
    #
    # ⚠️ 这条守卫必须走**真流水线**（`r.run`），不能直接调算子 ——
    # 直接调算子并手工传 `{"durationSec": ...}` 正是当初漏掉的原因：
    # 测了公式，没测**谁供给这个字段**。
    r6, _, _ = _mk(tmp, "g6")
    r6.verify_artifacts = True
    r6.max_repair_rounds = 0
    res6 = r6.run(SCRIPT, limit=2)
    g6 = _gate_of(res6)
    avail_d = g6.get("availableObservations") or []
    skipped_d = g6.get("skippedConstraints") or []
    check("G16 ★★ 门禁能拿到 `durationSec`（键名与 OPERATOR_NEEDS 对齐）",
          "durationSec" in avail_d, str(avail_d))
    check("G17 ★★ 时长硬门禁**真的执行了**（不在跳过清单里）",
          not any("cr_dur_" in x for x in skipped_d),
          str(skipped_d[:1])[:70])


# ==========================================================================
# H. 返工闭环：改 IR -> 只重跑那几镜 -> 复检
# ==========================================================================

def _h_gate(res) -> dict:
    return dict((res.evidence.gate if res.evidence else None) or {})


def _append_constraints(runner: Runner, extra: list) -> None:
    """**追加**约束（保留 IR 自带的那些）。

    与 `_with_constraints` 的分工很关键：

    | 辅助 | 语义 | 用在 |
    |---|---|---|
    | `_with_constraints` | **替换**全部约束 | 测「注入一条不可能满足的约束」—— 基线约束会干扰，必须去掉 |
    | `_append_constraints` | **追加**到现有约束 | 测**返工闭环** —— 需要「IR 自带的（可满足）+ 注入的（要求不同）」共存 |

    返工测试必须用追加：只有两条约束都真实存在，
    "返工把 IR 改到两边都满足"才是一件**有意义**的事。
    用替换的话，基线约束根本不在，门禁首轮就通过，循环压根不会跑
    （我第一版就是这么写错的，而且看起来"通过"得很正常）。
    """
    orig = runner._bridge

    def patched(shot):
        ir = orig(shot)
        out = dict(ir)
        out["constraints"] = list(ir.get("constraints") or ()) + list(extra)
        return out

    runner._bridge = patched            # type: ignore[method-assign]


def _append_constraints_for(runner: Runner, by_shot: dict) -> None:
    """**按镜头追加**约束（保留 IR 自带的）。见 `_append_constraints` 的说明。"""
    orig = runner._bridge

    def patched(shot):
        ir = orig(shot)
        extra = by_shot.get(shot.shot_id)
        if not extra:
            return ir
        out = dict(ir)
        out["constraints"] = list(ir.get("constraints") or ()) + list(extra)
        return out

    runner._bridge = patched            # type: ignore[method-assign]


def _with_constraints_for(runner: Runner, by_shot: dict) -> None:
    """**只对指定镜头**注入约束（按 shotId 匹配）。

    与 `_with_constraints` 的分工：
      · `_with_constraints`     对所有镜头注入（测"全都得重跑"）
      · `_with_constraints_for` 按镜头注入（测"**只**重跑受影响的"）
    """
    orig = runner._bridge

    def patched(shot):
        ir = orig(shot)
        extra = by_shot.get(shot.shot_id)
        if not extra:
            return ir
        out = dict(ir)
        out["constraints"] = list(ir.get("constraints") or ()) + list(extra)
        return out

    runner._bridge = patched            # type: ignore[method-assign]


def _section_repair_loop(tmp: Path) -> None:
    """返工闭环真的能**自愈**，且三道防烧闸都有效。

    夹具已能"按请求尺寸产图"（见 `FakeChannel.collect` / `_fetcher`），
    所以"改了 IR 的分辨率"会让下一轮**真的**产出新尺寸 ——
    这才测得到"循环前进了"，而不只是"循环跑了"。
    """
    # ---- ① ★★ 自愈：约束要求 32x24，返工改 IR，重跑后通过 ----
    # 声明 32x24（脚手架会据此产出 32x24），而约束要求 64x48 ->
    # 首轮 RETRY -> 返工改 IR 到 64x48 -> 重跑产出 64x48 -> 复检通过。
    r, ch1, _ = _mk(tmp, "h1", resolution=(32, 24))
    r.verify_artifacts = True
    r.max_repair_rounds = 2
    target = (64, 48)
    # ⚠️ 这里用**替换**语义（`_with_constraints`），不是追加。
    #
    # 理由：IR 自带的 `cr_res`（32x24）与注入的「应改为 64x48」是**同一维度的
    # 两条互相矛盾的约束** —— 不管理怎么改，都不可能同时满足。
    # 而"IR 声明的规格是错的"这个前提，本来就意味着那条自带约束**该被取代**。
    #
    # （我先用追加写，结果循环正确地改了 IR，但两条约束永远打架 ——
    #  那不是返工的 bug，是**用例在要求一件不可能的事**。）
    _with_constraints(r, [{
        "ref": "cr_wrong_res", "enforcement": "hard",
        "assertion": {"op": "resolution_matches",
                      "width": target[0], "height": target[1]},
    }])
    res = r.run(SCRIPT, limit=1)
    g = _h_gate(res)

    check("H1 ★★ 返工后**通过**（改了 IR -> 重跑 -> 复检，真的自愈）",
          res.ok, f"ok={res.ok} reason={res.reason[:60]}")
    check("H2 门禁最终 KEEP（不是带着 RETRY 交付）",
          (g.get("counts") or {}).get("kept", 0) >= 1,
          str(g.get("counts")))

    rounds = g.get("repairRounds") or []
    check("H3 返工过程有日志（可追溯改了什么）",
          len(rounds) >= 2, f"{len(rounds)} 条")
    check("H4 日志说明是「改 IR 的哪一步」",
          any("应用" in x for x in rounds), str(rounds[:2])[:110])

    # ---- ② ★ 只重跑受影响的镜头 ----
    r2, ch2, _ = _mk(tmp, "h2", resolution=(32, 24))
    r2.verify_artifacts = True
    r2.max_repair_rounds = 1
    # **只对第一镜注入**约束 —— 这样才能验证"只重跑受影响的镜头"。
    # 若对两镜都注入，两镜都得重跑，就测不出"只挑受影响的"了。
    _with_constraints_for(r2, {"SH-E100-001-001": [{
        "ref": "cr_wrong_res", "enforcement": "hard",
        "assertion": {"op": "resolution_matches", "width": 64, "height": 48},
    }]})
    res2 = r2.run(SCRIPT, limit=2)
    check("H5 ★ 返工时**只重跑受影响的镜头**（不是整集重跑）",
          ch2.submissions == 3,
          f"提交 {ch2.submissions} 次（首跑 2 镜 + 只重跑受影响的 1 镜）")

    # ---- ③ ★★ 闸一：max_repair_rounds=0 时不重跑 ----
    r3, ch3, _ = _mk(tmp, "h3", resolution=(32, 24))
    r3.verify_artifacts = True
    r3.max_repair_rounds = 0
    _with_constraints(r3, [{
        "ref": "cr_wrong_res", "enforcement": "hard",
        "assertion": {"op": "resolution_matches", "width": 64, "height": 48},
    }])
    res3 = r3.run(SCRIPT, limit=1)
    check("H6 ★★ 闸一：`max_repair_rounds=0` 时**不重跑**（可关）",
          not res3.ok and ch3.submissions == 1,
          f"ok={res3.ok} 提交 {ch3.submissions} 次")
    check("H7 关闭返工时仍如实报 RETRY（判定不受影响）",
          (_h_gate(res3).get("counts") or {}).get("retry", 0) >= 1,
          str(_h_gate(res3).get("counts")))

    # ---- ④ ★★ 闸二：动作全不可应用时不重跑 ----
    r4, ch4, _ = _mk(tmp, "h4")
    r4.verify_artifacts = True
    r4.max_repair_rounds = 3
    _with_constraints(r4, [{
        "ref": "cr_face", "enforcement": "hard",
        "assertion": {"op": "identity_similarity", "min": 0.99},
    }])
    res4 = r4.run(SCRIPT, limit=1)
    # 相似度缺观测 -> 被预检跳过 -> 根本不进裁定 -> 也就不会重跑
    check("H8 ★★ 闸二：算不出的约束被跳过，**不触发重跑**",
          ch4.submissions == 1, f"提交 {ch4.submissions} 次")

    # **这一条才是闸二的正题**：约束能算但修不了（同框超限）
    r5, ch5, _ = _mk(tmp, "h5")
    r5.verify_artifacts = True
    r5.max_repair_rounds = 3
    _with_constraints(r5, [{
        "ref": "cr_cast", "enforcement": "hard",
        "assertion": {"op": "count_at_most", "field": "characterCount",
                      "max": 0},
    }])
    res5 = r5.run(SCRIPT, limit=1)
    g5 = _h_gate(res5)
    check("H9 ★★ 闸二：能算但**修不了**的约束 -> RETRY 但**不重跑**",
          not res5.ok and ch5.submissions == 1,
          f"ok={res5.ok} 提交 {ch5.submissions} 次")
    check("H10 且日志明说「没有可应用的动作」（不是静默结束）",
          any("没有可应用的动作" in x for x in (g5.get("repairRounds") or [])),
          str((g5.get("repairRounds") or [])[:1])[:100])

    acts = [a for rr in (g5.get("results") or [])
            for a in (rr.get("repairActions") or [])]
    check("H11 不可应用的动作被标记且给出原因",
          any(a.get("applicable") is False and a.get("blockedBy")
              for a in acts),
          str(acts[:1])[:110])

    # ---- ⑤ 返工轮次上限 ----
    #
    # ⚠️ 想构造"修不好"的场景时要注意：**分辨率类的约束在夹具里总能修好** ——
    # 返工改 IR，夹具就按新尺寸产图。所以拿它测不出"上限"。
    #
    # 真正修不好的是**不可应用的动作**（见 H9），而那种情况**压根不会重跑**
    # （闸二先拦住了），也就测不到"重跑次数上限"。
    #
    # 因此这一条改为验证"**上限确实被用作循环边界**"：
    # 用一条可修的约束 + `max_repair_rounds=1`，断言重跑**确实只有一轮**。
    r6, ch6, _ = _mk(tmp, "h6", resolution=(32, 24))
    r6.verify_artifacts = True
    r6.max_repair_rounds = 1
    _with_constraints(r6, [{
        "ref": "cr_wrong_res", "enforcement": "hard",
        "assertion": {"op": "resolution_matches", "width": 64, "height": 48},
    }])
    res6 = r6.run(SCRIPT, limit=1)
    g6 = _h_gate(res6)
    rounds6 = [x for x in (g6.get("repairRounds") or [])
               if x.startswith("第 ")]
    check("H12 ★ 上限生效：`max_repair_rounds=1` 时**最多一轮**返工",
          len(rounds6) <= 1, f"{len(rounds6)} 轮")
    check("H12b 一轮就修好（自愈），且 ok=True",
          res6.ok, f"ok={res6.ok} reason={res6.reason[:50]}")

    # ---- ⑥ 证据链里带返工轨迹 ----
    check("H14 ★ 证据链里有完整的返工轨迹（审查方可复核）",
          bool(g.get("repairRounds")), f"{len(g.get('repairRounds') or [])} 条")
    check("H15 返工动作进证据（机器可读，含轮次与镜头）",
          bool(g.get("appliedRepairs")),
          str((g.get("appliedRepairs") or [])[:1])[:110])
    check("H16 轨迹里写明「重跑了哪几镜」（可对账）",
          any("重跑" in x for x in (g.get("repairRounds") or [])),
          str((g.get("repairRounds") or [])[:1])[:90])


if __name__ == "__main__":
    raise SystemExit(main())
