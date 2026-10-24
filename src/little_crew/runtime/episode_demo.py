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
from little_crew.runtime.episode import (
    EpisodeRunner,
    StopRequested,
    artifact_resolver,
    check_workdir_episode,
    write_delivery,
)
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


def _print_advisory(res: Any) -> None:
    """打印**顾问意见**（不进裁定）。dry-run 与完整跑都要看到。"""
    adv = getattr(res, "advisory", ()) or ()
    if not adv:
        return
    print()
    print("  顾问意见（**不进裁定**，仅供人判断）：")
    for a in adv:
        mark = {"PASS": "·", "REVISE": "!"}.get(str(a.get("verdict")), "X")
        print(f"    [{mark}] {a.get('position')}: {a.get('verdict')}"
              f"  总分 {a.get('total')}"
              + (f"  ({a.get('reason')})" if a.get("reason") else ""))
        for i in (a.get("issues") or [])[:3]:
            print(f"         [{i.get('severity')}] {str(i.get('problem'))[:88]}")



def _print_clips(runner: Any, enabled: bool) -> None:
    """打印**真片段第二段**的结果。

    ⚠️ 为什么必须有这个打印：`alpha.68` 第一次真机跑时，第二段因为
    "静帧路径没解析出来"而**立刻失败**，但它只是返回了一个 `ok=False` 的
    dict —— **屏幕上什么都没显示**。表现是"视频模式开了，但成片还是 Ken Burns"，
    而人会以为"视频模式没生效"。

    **失败不说话，等于没失败** —— 只是把问题推给下一次。
    """
    if not enabled:
        return
    info = getattr(runner, "_clip_info", None) or {}
    print()
    if info.get("ok"):
        print(f"  真片段成片：{info.get('path')}")
        print(f"    片段 {len(info.get('clips') or [])} 个")
        return
    print(f"  ⚠️ 真片段没出来：{info.get('reason') or '（未进入第二段）'}")
    for f in (info.get("failures") or [])[:4]:
        print(f"      ✗ {f}")
    return


def _print_audio(runner: Any, enabled: bool) -> None:
    """打印**片段音轨检查**（V3）。缺音轨 = M3 没达成，必须看得见。"""
    if not enabled:
        return
    rep = getattr(runner, "_clip_audio", None) or {}
    if not rep:
        return
    print()
    print(f"  音轨检查：{'✅ 全部通过' if rep.get('ok') else '❌ 有片段缺音轨'}"
          f"  ({rep.get('passed')}/{rep.get('passed', 0) + rep.get('failed', 0)})")
    for r in (rep.get("clips") or []):
        mark = "✓" if r.get("passed") else "✗"
        print(f"      [{mark}] {r.get('clip')}  容器 {r.get('container')}"
              f"  音轨 {r.get('hasAudio')}")
    if not rep.get("ok"):
        print(f"      {rep.get('reason')}")


#: Z-Image 通路的模型名 + 控制网 patch。
#: ⚠️ `default_model_patch` **必须给** —— `ZImageFunControlnet` 的 required 输入是
#: `model / model_patch / vae / strength`，缺 `model_patch` 会在提交前被拦下。
ZIMAGE_UNET = "z_image_turbo_int8_convrot.safetensors"
ZIMAGE_PATCH = ("Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps"
                ".safetensors")


def _comfy_compiler(ch, endpoint: str):
    """按 ComfyUI 的**真实能力**构造编译器（探测 `/object_info`）。

    为什么必须探测而不是写死规则表：**这台机器上有没有某个节点，决定了哪些算子能编译**。
    写死会在缺节点时「编译通过、提交失败」，而那时已经离故障点很远了。
    """
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler

    mat = probe_from_object_info(ch.probe_catalog(), endpoint=endpoint,
                                 comfy_version="0.39.0")
    return ComfyCompiler(mat, default_checkpoint=ZIMAGE_UNET,
                         default_model_patch=ZIMAGE_PATCH,
                         model_source_kind="model.source_zimage")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑通一集")
    ap.add_argument("--limit", type=int, default=None,
                    help="只跑前 N 镜（默认全跑）")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true",
                    help="不出图，只跑到分镜与桥接")
    ap.add_argument("--backend", default="localgen",
                    choices=["localgen", "comfyui"],
                    help="出图后端（默认 localgen）。comfyui 才能编译控制图节点")
    ap.add_argument("--llm", action="store_true",
                    help="编剧与剧评人改用**真实 LLM**（其余职位仍用规则桩）——"
                         "需要 API key；产出不是确定性的")
    ap.add_argument("--llm-model", default="deepseek-flash",
                    help="`--llm` 时**生成方**（编剧）用哪个注册表模型")
    ap.add_argument("--llm-reviewer", default="mimo-v2.6-flash",
                    help="`--llm` 时**审核方**（剧评人）用哪个模型 —— "
                         "⚠️ 必须与 --llm-model 不同族（跨模型族盲读的硬要求）")
    ap.add_argument("--llm-critic-model", default="GLM-5.3-flash",
                    help="文风编辑用哪个模型（默认 GLM —— **第三个族**，"
                         "让两个审核职位不再同源）")
    ap.add_argument("--llm-critics", action="store_true",
                    help="把 `story_editor` 换成**真实 LLM 批评家**"
                         "（A 类审查职位的通用实现）。"
                         "它是 `script.draft` 的**门禁审核者**之一，"
                         "所以意见直接影响裁定。需要 --llm 的 key")
    ap.add_argument("--video-mode", action="store_true",
                    help="**真片段**模式：静帧之后把每镜动画化再拼成片。"
                         "⚠️ 实测 331s/镜 —— 2 镜 ≈11 分钟，24 镜 ≈2.3 小时")
    ap.add_argument("--video-mode-seconds", type=float, default=2.0,
                    help="每镜片段秒数（H3 上限 10 秒，2 秒最稳）")
    ap.add_argument("--llm-director", action="store_true",
                    help="先跑**导演**产出 `episode.plan`，"
                         "并按它的 shotBudget 给分镜师派活。"
                         "需要 --llm")
    ap.add_argument("--llm-previs", action="store_true",
                    help="跑**白模预演师**：每镜判断布局 -> Blender 出白模"
                         "（圆柱给人看 / 人形喂 ControlNet）。需要 --llm")
    ap.add_argument("--blender", default="",
                    help="Blender 可执行文件（配 --llm-previs）")
    ap.add_argument("--use-white-model", action="store_true",
                    help="把白模控制图注入出图（需要 ComfyUI + ControlNet）")
    ap.add_argument("--comfy-input", default="",
                    help="ComfyUI 的 input/ 目录 —— 控制图必须投放进去")
    ap.add_argument("--llm-art", action="store_true",
                    help="跑**美术指导**产出 `art.bible`，把每场的空间/材质/道具"
                         "写进分镜提示词。需要 --llm")
    ap.add_argument("--llm-schedule", action="store_true",
                    help="跑**制片主任**产出 `production.schedule`"
                         "（排期 + 风险镜）。需要 --llm")
    ap.add_argument("--llm-gaffer", action="store_true",
                    help="跑**灯光师**产出 `lighting.plan`，"
                         "把每场的光写进分镜提示词。需要 --llm")
    ap.add_argument("--llm-advisory", action="store_true",
                    help="额外跑 3 个**顾问批评家**"
                         "（连贯性 / 合规 / 节奏）。"
                         "它们**不进裁定**，只把意见写进报告 —— "
                         "先量命中率，再决定要不要升门禁。需要 --llm")
    ap.add_argument("--accept-script", action="store_true",
                    help="⚠️ **调试用**：无视剧本门禁裁定，强行进入分镜与渲染。"
                         "只用于验证**下游链路**（桥接/渲染/合成）——"
                         "正式跑不要加，它会让不合格剧本也出片")
    ap.add_argument("--workdir", default="outputs/episode_E100")
    ap.add_argument("--fresh", action="store_true", help="清空工作目录")
    ap.add_argument("--episode-id", default="E100",
                    help="集号，写进 shotId 与报告（默认 E100）。"
                         "**开新一集必须换它**，否则镜头号会重名")
    ap.add_argument("--title", default="薰衣草",
                    help="集标题（默认 薰衣草）")
    ap.add_argument("--allow-reuse", action="store_true",
                    help="允许往已有**别的集**的工作目录里写（默认拒绝）")
    ap.add_argument("--stop-after", default="",
                    choices=["", "script", "director", "storyboard",
                             "previs", "render_start", "assemble"],
                    help="跑到这个阶段就停下，并写半成品报告（默认不停）")
    ap.add_argument("--no-video", action="store_true",
                    help="只出图，不合成 MP4")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parents[3]
    work = root / args.workdir

    # ---- 防串集（2026-10-20 加；判据抽到 `runtime.episode`，与 CLI 共用）----
    #
    # 最常见的出错方式不是"写错代码"，是**把新的一集写进老目录** ——
    # 然后两集的 shotId 会重名（`SH-E100-S01-001`），缓存也会串，
    # 而报告看起来一切正常。
    old_id, conflict = check_workdir_episode(work, args.episode_id)
    if conflict and not args.fresh and not args.allow_reuse:
        print(f"  [X] {conflict}")
        print("      换一个 --workdir（推荐），或加 --fresh 清空，"
              "或加 --allow-reuse 强行覆盖")
        return 2
    print(f"  集号 {args.episode_id} · 标题 {args.title}")
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
        #
        # ---- 导演（若启用）----
        #
        # ⚠️ **导演先定预算，分镜在预算内分配** —— 这是"导演"这个职位
        # 真正进入协作链的方式。没这一步，`episode.plan` 只是一份没人看的文档。
        #
        # 注意：导演要看**剧本**，而剧本此刻还没产出。
        # 所以这里只绑定实现；由 `EpisodeRunner._run_director()` 在剧本定稿后
        # 调它、并把拿到的 plan 交给分镜师。
        if args.llm_director:
            from little_crew.agent.director_llm import DirectorLlm

            stubs["director"] = DirectorLlm(client=writer)
            print("  导演：**真实 LLM**（定基调与每场镜头预算）")
        if args.llm_previs:
            from little_crew.agent.previs_agent_llm import PrevisAgentLlm

            stubs["previs"] = PrevisAgentLlm(client=writer)
            print("  白模预演师：**真实 LLM**（判断每镜机位与站位，交 Blender 出白模）")
        if args.llm_art:
            from little_crew.agent.production_designer_llm import (
                ProductionDesignerLlm,
            )

            stubs["production_designer"] = ProductionDesignerLlm(client=writer)
            print("  美术指导：**真实 LLM**（定每场的空间/材质/道具）")
        if args.llm_schedule:
            from little_crew.agent.line_producer_llm import LineProducerLlm

            stubs["line_producer"] = LineProducerLlm(client=writer)
            print("  制片主任：**真实 LLM**（按实测单价排期 + 标风险镜）")
        if args.llm_gaffer:
            from little_crew.agent.gaffer_llm import GafferLlm

            stubs["gaffer"] = GafferLlm(client=writer)
            print("  灯光师：**真实 LLM**（定每场的光与影调）")

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

            # ⚠️ **文风编辑用第三个模型族**（2026-10-19 加入 GLM）。
            #
            # 在此之前 `dramaturge` 与 `story_editor` 都是 MiMo ——
            # 所谓「双维审查」实际是同一模型的两个提示词，
            # 独立性弱于跨族（docs/00 §5.10.16 ①）。
            # 现在：生成方 DeepSeek / 剧评人 MiMo / 文风编辑 GLM，**三个职位三个族**。
            critic_client = LlmClient(args.llm_critic_model)
            critic = CriticLlm("story_editor", client=critic_client)
            stubs["story_editor"] = critic
            print(f"  文风编辑：**真实 LLM 批评家**"
                  f"（{args.llm_critic_model} / 第三个族 / 盯文风一致性）")

    if not args.llm:
        print("  编剧：规则桩（确定性）")
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek", "storyboard_artist": "deepseek",
                        "dramaturge": "mimo", "story_editor": "glm",
                        "review_board": "qwen"})
    orch.bind_many(stubs)

    # ---------- 装配渲染 ----------
    ck = RenderCheckpoint(work / "checkpoint", episode_id=args.episode_id)
    if args.dry_run:
        renderer = CachingRenderer(
            RenderSession(_DryChannel(), _null_compiler()),
            ContentCache(work / "cache"),
            fetcher=lambda r: f"PNG:{r.uri}".encode(), checkpoint=ck)
        backend = None
    else:
        spec = build_default_registry().get("z-image-turbo")
        # 后端由 `--backend` 决定（默认 localgen，与改造前一致）。
        #
        # 改造前这里写死 `BackendKind.HTTP` —— 于是整集永远走 local-gen，
        # 而 `composition.zimage_controlnet` 是 **ComfyUI 专用**，
        # 结果白模控制图生成了也永远编译不进去。
        from little_crew.runtime.backend_select import (
            describe as _describe_backend,
            make_channel as _make_channel,
            pick_backend as _pick_backend,
        )

        be = _pick_backend(spec, args.backend)
        print(f"  后端 {args.backend} -> {_describe_backend(be)}")
        ch = _make_channel(be, timeout_s=1800)
        okv, why = ch.available()
        if not okv:
            print(f"  ❌ {args.backend} 不可用：{why}")
            print("     先启动服务，或加 --dry-run 只验证到分镜。")
            return 1
        # **编译器按后端选**（2026-10-22）：`_null_compiler()` 意味着 IR 从不被编译
        # —— 一直是「一份 http 形态的任务」，ComfyUI 收不了，而 local-gen 正好要它。
        _comp = (_comfy_compiler(ch, be.endpoint)
                 if be.kind is BackendKind.COMFYUI else _null_compiler())
        renderer = CachingRenderer(RenderSession(ch, _comp),
                                   ContentCache(work / "cache"),
                                   fetcher=artifact_fetcher_for(ch),
                                   checkpoint=ck)
        backend = be

    # ---------- 跑 ----------
    runner = _Runner(orch, renderer, episode_id=args.episode_id,
                     title=args.title, backend=backend)
    runner.engine_seed = args.seed
    runner.engine_steps = args.steps
    runner.workdir = work
    # ⚠️ 调试开关：只为验证下游链路，**正式跑不要开**。
    # 开了之后 `scriptVerdict` 仍会如实显示 REVISE —— 不掩盖剧本没过这一事实。
    runner.force_accept_script = bool(args.accept_script)
    runner.video_mode = bool(args.video_mode)
    runner.video_seconds = float(args.video_mode_seconds)
    runner._workdir = work
    # ---- 白模（2026-10-19）----
    runner.stop_after = str(args.stop_after or "")
    if runner.stop_after:
        print(f"  ⏸ 按要求停在「{runner.stop_after}」后")
    runner.blender_exe = str(args.blender or "")
    runner.use_white_model = bool(args.use_white_model)
    runner.comfy_input_dir = (Path(args.comfy_input) if args.comfy_input else None)
    if args.use_white_model and not args.blender:
        print("  ⚠️ --use-white-model 需要 --blender（否则渲不出控制图）")
    if args.use_white_model and not args.comfy_input:
        print("  ⚠️ --use-white-model 需要 --comfy-input（控制图必须投放进 ComfyUI 的 input/）")
    if args.video_mode:
        print(f"  视频模式：**真片段**（{args.video_mode_seconds}s/镜，"
              f"约 331s/镜 —— 请估算总耗时）")
    # 顾问批评家：**不进裁定**，只产意见（见 EpisodeRunner.advisory_critics）
    if args.llm_advisory and args.llm:
        runner.advisory_critics = ("continuity_supervisor",
                                   "compliance_officer", "pacing_editor")
        print("  顾问批评家：连贯性 / 合规 / 节奏（**不进裁定**）")
    if args.accept_script:
        print("  ⚠️ --accept-script：无视剧本门禁裁定（仅用于验证下游）")

    # 产物散落在缓存的**分片目录**里，所以要能按文件名找回来。
    # 视频层要求调用方提供解析方式 —— 它不该去猜目录结构。
    #
    # 解析器抽在 `runtime.episode.artifact_resolver`（2026-10-23 审计 H-21）：
    # 产品 CLI 也需要同一个东西，两处各写一份必然漂移 ——
    # 而漂移的表现是"demo 能合视频、CLI 永远合不出来"。
    runner.resolve_artifact = (None if args.no_video
                               else artifact_resolver(work))
    if not args.no_video:
        print(f"  视频合成已启用（产物按名解析于 {work.name}/）")

    print("【1–2】剧本闭环 + 分镜")
    print("-" * 78)
    t0 = time.perf_counter()
    # ---- 中断也要留记录（2026-10-20）----
    #
    # 正常结束时 `run.report.json` 会写全。但**中途停下时原来什么都没有** ——
    # 跑了 26 分钟、第 30 镜发现不对、停下，然后没有任何东西说明
    # "前 29 镜是怎么判的、卡在哪"。
    #
    # 所以这里捕获三类收尾：Ctrl+C、异常、主动停（`--stop-after`）。
    try:
        res = runner.run(LAVENDER_LEGEND, limit=args.limit)
    except (KeyboardInterrupt, StopRequested) as exc:
        from little_crew.runtime.episode import StopRequested as _SR  # noqa: F401
        from little_crew.runtime.partial_report import (
            summary_line,
            write_partial,
        )

        pth = write_partial(
            work,
            episode_id=args.episode_id,
            title=args.title,
            stage=getattr(runner, "stage", ""),
            reason=str(exc) or "用户中断",
            trace=getattr(runner, "trace", {}),
            started_at=getattr(runner, "_t0", None),
        )
        print()
        print("  ⏸ 已停下 —— 半成品报告已写：")
        print(f"    {pth}")
        print(f"    {summary_line(pth)}")
        print("    续跑：用同一个 --workdir 再跑（已算的不重算）")
        return 0
    except Exception as exc:                                # noqa: BLE001
        from little_crew.runtime.partial_report import write_partial

        pth = write_partial(
            work,
            episode_id=args.episode_id,
            title=args.title,
            stage=getattr(runner, "stage", ""),
            reason=f"{type(exc).__name__}: {exc}",
            trace=getattr(runner, "trace", {}),
            started_at=getattr(runner, "_t0", None),
        )
        print(f"  ❌ 出错（{type(exc).__name__}），半成品报告：{pth}")
        raise
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
        _print_advisory(res)
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

    _print_advisory(res)

    # ---------- 落盘 ----------
    # 与产品 CLI 共用同一份实现（`runtime.episode.write_delivery`）——
    # 报告页按 `<work>/delivery/` 读三份文件，两边必须一致（审计 M-28）。
    out_dir = write_delivery(work, result=res, manifest=m, evidence=ev)

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
    # ---- 资产落盘（2026-10-21）----
    #
    # 角色定妆表原来只活在内存与那次报告里 —— 换一集就从头再生成，
    # 无法「还是这个少女」。这里把它落成 `assets/characters/<角色>/sheet.json`，
    # 并记下来源（哪一集、什么 seed），供下一集 `--inputs` 复用。
    try:
        from little_crew.runtime.assets import write_characters

        _chars = getattr(res, "characters", None) or {}
        _sheets = _chars.get("sheets") if isinstance(_chars, dict) else None
        if _sheets:
            _man = write_characters(work, _sheets,
                                    episode_id=args.episode_id,
                                    seed=str(_chars.get("seed") or ""))
            print(f"  资产：{len(_man['characters'])} 个角色 -> "
                  f"{work / 'assets' / 'characters'}")
    except Exception as _e:                                    # noqa: BLE001
        print(f"  ⚠️ 资产落盘失败（不影响交付）：{type(_e).__name__}: {_e}")

    print(f"  交付物写入 {out_dir}")
    print("=" * 78)
    _print_clips(runner, args.video_mode)
    _print_audio(runner, args.video_mode)

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
