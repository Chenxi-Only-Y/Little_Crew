"""跑通一集 —— 从剧本素材到成片清单与交付证据。

这是 **M3「一集成片」** 的落点。

用法
----
    # 真实出图（Z-Image / local-gen，需服务在 8100）
    python -m little_crew.runtime.episode_demo

    # 只跑前 3 镜（省时间）
    python -m little_crew.runtime.episode_demo --limit 3

    # 不出图，只验证到分镜（快）
    python -m little_crew.runtime.episode_demo --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from little_crew.agent.crew_demo import DEMO_SOURCE_LAVENDER
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.orchestrator import FAILURE_ACTIONS, CrewOrchestrator
from little_crew.models.spec import BackendKind
from little_crew.models.registry import build_default_registry
from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cache_measure import _null_compiler
from little_crew.runtime.channels.base import ArtifactRef, JobHandle, JobStatus
from little_crew.runtime.channels.http import HttpChannel
from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
from little_crew.runtime.checkpoint import RenderCheckpoint
from little_crew.runtime.episode import EpisodeRunner
from little_crew.runtime.render import RenderSession

# ==========================================================================
# 剧本素材 —— 普罗旺斯薰衣草传说
# ==========================================================================
#
# 结构化规则（与 `crew_stubs._parse_scenes` 一致）：
#   · `[场N]` 切分场次
#   · `@角色名` 标记出场人物（**不是画面内容**，桥接时会清掉）
#   · 每场最好含「冲突 / 代价 / 转折」三类信号，否则剧本顾问会打回
#
# 剧本夹具已移到 `agent/crew_demo.py`（与 DEMO_SOURCE 同类）。
# 别名保留，避免改动本文件其余部分与既有调用方。
LAVENDER_LEGEND = DEMO_SOURCE_LAVENDER


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑通一集")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 镜（默认全跑）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true",
                    help="不出图，只跑到分镜与桥接")
    ap.add_argument("--llm", action="store_true",
                    help="编剧与剧评人改用**真实 LLM**（其余职位仍用规则桩）——"
                         "需要 API key；产出不是确定性的")
    ap.add_argument("--llm-model", default="deepseek-v4",
                    help="`--llm` 时**生成方**（编剧）用哪个注册表模型")
    ap.add_argument("--llm-reviewer", default="mimo-v2.6-flash",
                    help="`--llm` 时**审核方**（剧评人）用哪个模型 —— "
                         "⚠️ 必须与 --llm-model 不同族（跨模型族盲读的硬要求）")
    ap.add_argument("--llm-critics", action="store_true",
                    help="把 `story_editor` 换成**真实 LLM 批评家**"
                         "（A 类审查职位的通用实现）。"
                         "它是 `script.draft` 的**门禁审核者**之一，"
                         "所以意见直接影响裁定。需要 --llm 的 key")
    ap.add_argument("--accept-script", action="store_true",
                    help="⚠️ **调试用**：无视剧本门禁裁定，强行进入分镜与渲染。"
                         "只用于验证**下游链路**（桥接/渲染/合成）——"
                         "正式跑不要加，它会让不合格剧本也出片")
    ap.add_argument("--workdir", default="outputs/episode_E100")
    ap.add_argument("--fresh", action="store_true", help="清空工作目录")
    ap.add_argument("--no-video", action="store_true",
                    help="只出图，不合成 MP4")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parents[3]
    work = root / args.workdir
    if args.fresh:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("跑通一集 —— 薰衣草传说")
    print("=" * 78)
    print(f"  工作目录 {work}")
    print(f"  引擎 {'dry-run（不出图）' if args.dry_run else 'Z-Image / local-gen'}")
    print()

    # ---------- 装配剧组 ----------
    #
    # `--llm` 时**只换编剧**，其余职位仍用规则桩。
    # 为什么一次只换一个：接真模型的问题几乎都出在提示词与输出结构上，
    # 而"五个一起换"会让归因变成猜 —— 出了问题不知道是谁的。
    stubs = default_crew_stubs()
    if args.llm:
        from little_crew.agent.crew_llm import DramaturgeLlm, ScreenwriterLlm
        from little_crew.agent.llm import LlmClient

        writer = LlmClient(args.llm_model)
        reviewer = LlmClient(args.llm_reviewer)
        for label, cl in (("编剧", writer), ("剧评人", reviewer)):
            d = cl.describe()
            print(f"  {label}：**真实 LLM**（{d['displayName']} / "
                  f"{d['apiKeyMasked']}）")
            if not d["configured"]:
                print(f"  ❌ {label}未配置密钥（{d['apiKeyEnv']}）—— "
                      f"三处都取不到。见 STRUCTURE.md §2.0")
                return 1
        # ⚠️ 两者必须不同族 —— 同族会让门禁退化成自我签收（且不报错）
        if writer.model_id == reviewer.model_id:
            print(f"  ❌ 编剧与剧评人用了同一个模型（{writer.model_id}）—— "
                  f"跨模型族盲读要求两者不同族")
            return 1
        stubs["screenwriter"] = ScreenwriterLlm(client=writer)
        stubs["dramaturge"] = DramaturgeLlm(client=reviewer)
        # ⚠️ **换实现要成对换**：分镜师按规则桩的产出形态（正则解析
        # `[场N]`/`@角色`）设计的，而 LLM 编剧产出的是 `scenes` 结构化
        # 数组 —— 桩读不懂，结果是"跑满 3 轮但分镜 0 个"。
        from little_crew.agent.storyboard_llm import StoryboardArtistLlm

        # 分镜师是**创作**职位，用生成方同族即可（它不是审核方）
        stubs["storyboard_artist"] = StoryboardArtistLlm(client=writer)
    # ---- A 类审查职位：通用批评家（2026-10-18）----
    #
    # ⚠️ **`story_editor` 是 `script.draft` 的门禁审核者之一**
    # （`_REVIEWERS_BY_ARTIFACT["script.draft"] = (dramaturge, story_editor)`），
    # 所以换它**真的会影响裁定** —— 不是摆设。
    #
    # 另外 4 个批评家（`continuity_supervisor` / `compliance_officer` /
    # `pacing_editor` / `review_board`）**已实现但还没有对应的审查阶段**：
    # `shot.image` 的审片流程目前不在这一集的流水线里。所以这里只换
    # `story_editor`，其余保持未绑定 —— **不假装它们在工作**。
    if args.llm_critics:
        if not args.llm:
            print("  ⚠️ --llm-critics 需要 --llm（否则没有客户端），已忽略")
        else:
            from little_crew.agent.critic_llm import CriticLlm

            critic = CriticLlm("story_editor", client=reviewer)
            stubs["story_editor"] = critic
            print(f"  文风编辑：**真实 LLM 批评家**"
                  f"（{reviewer.model_id} / 盯文风一致性）")

    if not args.llm:
        print("  编剧：规则桩（确定性）")
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek", "storyboard_artist": "deepseek",
                        "dramaturge": "qwen", "story_editor": "qwen",
                        "review_board": "qwen"})
    orch.bind_many(stubs)

    # ---------- 装配渲染 ----------
    ck = RenderCheckpoint(work / "checkpoint", episode_id="E100")
    if args.dry_run:
        renderer = CachingRenderer(
            RenderSession(_DryChannel(), _null_compiler()),
            ContentCache(work / "cache"),
            fetcher=lambda r: f"PNG:{r.uri}".encode(), checkpoint=ck)
        backend = None
    else:
        spec = build_default_registry().get("z-image-turbo")
        be = next(b for b in spec.backends if b.kind is BackendKind.HTTP)
        ch = HttpChannel(be, timeout_s=1800)
        okv, why = ch.available()
        if not okv:
            print(f"  ❌ local-gen 不可用：{why}")
            print("     先启动服务，或加 --dry-run 只验证到分镜。")
            return 1
        renderer = CachingRenderer(RenderSession(ch, _null_compiler()),
                                   ContentCache(work / "cache"),
                                   fetcher=artifact_fetcher_for(ch),
                                   checkpoint=ck)
        backend = be

    # ---------- 跑 ----------
    runner = _Runner(orch, renderer, episode_id="E100",
                     title="薰衣草", backend=backend)
    runner.engine_seed = args.seed
    runner.engine_steps = args.steps
    runner.workdir = work
    # ⚠️ 调试开关：只为验证下游链路，**正式跑不要开**。
    # 开了之后 `scriptVerdict` 仍会如实显示 REVISE —— 不掩盖剧本没过这一事实。
    runner.force_accept_script = bool(args.accept_script)
    if args.accept_script:
        print("  ⚠️ --accept-script：无视剧本门禁裁定（仅用于验证下游）")

    # 产物散落在缓存的**分片目录**里，所以要能按文件名找回来。
    # 视频层要求调用方提供解析方式 —— 它不该去猜目录结构。
    #
    # 渲染发生在 run() 内部，所以索引必须在**调用时**才建立，
    # 用一个惰性解析器：先查索引，未命中就现搜一次。
    _idx: dict[str, Path] = {}

    def _resolve(name: str) -> Path | None:
        if name in _idx:
            return _idx[name]
        for q in work.rglob(name):
            _idx[name] = q
            return q
        return None

    runner.resolve_artifact = None if args.no_video else _resolve
    if not args.no_video:
        print(f"  视频合成已启用（产物按名解析于 {work.name}/）")

    print("【1–2】剧本闭环 + 分镜")
    print("-" * 78)
    t0 = time.perf_counter()
    res = runner.run(LAVENDER_LEGEND, limit=args.limit)
    wall = time.perf_counter() - t0

    # 剧本与分镜的中间结果
    black = orch.snapshot()["blackboard"]
    cycle_rounds = res.script_rounds
    print(f"  剧本：{cycle_rounds} 轮 → {res.script_verdict}")
    print(f"  分镜：{res.planned_shots} 个镜头"
          f"（全集 {res.shot_count}）")

    if args.dry_run:
        print()
        print("=" * 78)
        print(f"dry-run 结束：剧本与分镜已跑通（{wall:.1f}s），未出图")
        print("=" * 78)
        return 0

    print()
    print("【3–4】桥接 + 渲染")
    print("-" * 78)
    print(f"  实算 {res.rendered}  命中 {res.cache_hits}  失败 {res.failed}")
    print(f"  耗时 {res.wall_seconds:.1f}s")
    for f in res.failures[:5]:
        print(f"    ❌ {f['graphId']}  [{f['failure']}] {f['reason'][:60]}")

    print()
    print("【5】成片清单与交付证据")
    print("-" * 78)
    if res.manifest is None:
        # ⚠️ **这条提前退出路径也必须显示分类与处置** ——
        # 剧本没过是最常见的失败，而它根本走不到最后那行报告。
        # （第一版只加在最后一行，导致最常见的失败反而看不到分类。）
        print(f"  ❌ {res.reason}")
        if res.failure_kind:
            print(f"  失败分类 {res.failure_kind}")
            print(f"  处置     {FAILURE_ACTIONS.get(res.failure_kind, '（未登记）')}")
        return 1
    m = res.manifest
    print(f"  成片：{m.shot_count} 镜 / {m.total_seconds:.1f} 秒"
          f"（阶段 {m.stage}，待补 {'/'.join(m.pending_stages)}）")
    print()
    print(f"  {'镜头':<24} {'景别':<14} {'运镜':<12} {'时长':>6}  产物")
    print("  " + "-" * 74)
    for e in m.shots:
        mark = "✅" if e["ready"] else "❌"
        print(f"  {e['shotId']:<24} {e['shotSize']:<14}"
              f" {e['cameraMovement']:<12} {e['durationSec']:>5.1f}s"
              f"  {mark} {len(e['artifacts'])}")

    # ---------- 视频 ----------
    if res.video is not None:
        print()
        print("【6】视频合成")
        print("-" * 78)
        v = res.video
        if hasattr(v, "ok"):
            print(f"  {'✅ 成片' if v.ok else '⚠️ 部分成功'}  "
                  f"{v.clips_built}/{v.clips_total} 镜  "
                  f"{v.seconds:.1f} 秒  {v.bytes/1024/1024:.2f} MB  "
                  f"墙钟 {v.wall_seconds:.1f}s")
            if v.reason and not v.ok:
                print(f"  说明：{v.reason}")
            if v.stderr_tail:
                print(f"  首条错误：{v.stderr_tail[:140]}")
        else:
            print(f"  ⚠️ 未合成：{v}")

    ev = res.evidence
    print()
    print("  交付证据链：")
    print(f"    产物 {len(ev.artifacts)} 个   内容哈希 {len(ev.ir_hashes)} 条")
    print(f"    缓存：命中 {ev.cache_hits} / 实算 {ev.rendered}，"
          f"节省 {ev.seconds_saved:.1f}s")
    print("    硬规则校验：")
    for k, v in ev.checks.items():
        print(f"      {'✅' if v else '❌'} {k}")

    # ---------- 落盘 ----------
    out_dir = work / "delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "episode.master.json").write_text(
        json.dumps(m.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "delivery.evidence.json").write_text(
        json.dumps(ev.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "run.report.json").write_text(
        json.dumps(res.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"结果：{'✅ 跑通一集' if res.ok else '⚠️ 未完全跑通'} —— {res.reason}")
    # **失败分类 + 处置建议**（2026-10-17）：
    # 只说"未通过"会让读者把三种完全不同的情况混成一个 ——
    #   评审服务抽风 / 判定作废 / 产物真的不够好
    # 它们的处置分别 是"重跑 / 换族重审 / 返工"，差别很大。
    if res.failure_kind:
        print(f"  失败分类 {res.failure_kind}")
        print(f"  处置     {FAILURE_ACTIONS.get(res.failure_kind, '（未登记）')}")
    print(f"  覆盖率 {res.coverage:.0%}（{res.rendered + res.cache_hits}"
          f"/{res.planned_shots} 镜有产物）")
    print(f"  墙钟 {wall:.1f}s   成片时长 {res.total_seconds:.1f}s")
    print(f"  交付物写入 {out_dir}")
    print("=" * 78)
    return 0 if res.ok else 1


class _Runner(EpisodeRunner):
    """把后端交给流水线 —— HTTP 与编译两条路的差异只在这里。"""

    def __init__(self, orch: Any, renderer: Any, *, episode_id: str,
                 title: str, backend: Any) -> None:
        super().__init__(orch, renderer, episode_id=episode_id, title=title)
        self._backend = backend

    def engine_backend(self) -> Any:
        return self._backend


class _DryChannel:
    """dry-run 用的假通道。"""

    def __init__(self) -> None:
        self.endpoint = "http://dry:1"
        self.channel_id = "dry"
        self._n = 0
        self._by: dict[str, Any] = {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def submit(self, job: Any) -> Any:
        if job.idempotency_key in self._by:
            return self._by[job.idempotency_key]
        self._n += 1
        h = JobHandle(job_id=job.idempotency_key, channel_id=f"d{self._n}",
                      status=JobStatus.PENDING)
        self._by[job.idempotency_key] = h
        return h

    def poll(self, h: Any) -> Any:
        h.status = JobStatus.SUCCEEDED
        return h

    def collect(self, h: Any) -> list[Any]:
        return [ArtifactRef(kind="image", uri=f"/view?filename={h.channel_id}.png")]

    def reconcile(self, ids: Any) -> dict:
        return {}

    def cancel(self, h: Any) -> None:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
