"""一集 + 白模控制实测 —— 走 ComfyUI 的 Z-Image ControlNet 通道。

**这是"把白模接进流水线"的验证脚本。**

与 `episode_demo.py` 的分工：
  · `episode_demo`  走 local-gen 的 **HTTP 通道**（纯文生图，已跑通 20 镜）
  · 本脚本          走 ComfyUI 的**编译通道**（Z-Image + 白模 ControlNet）

两者共用同一套 `EpisodeRunner` —— 差别只在渲染器怎么装。
**这正是平台"双通道正交"设计的兑现**：换通道不改流水线。

用法
----
    python -m little_crew.runtime.p3_episode --limit 3
    python -m little_crew.runtime.p3_episode --limit 3 --no-white-model   # 对照组
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from little_crew.agent.crew_demo import DEMO_SOURCE_LAVENDER
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.orchestrator import CrewOrchestrator
from little_crew.capability.capability import probe_from_object_info
from little_crew.compiler.compiler import ComfyCompiler
from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
from little_crew.runtime.channels.comfy import ComfyChannel
from little_crew.runtime.checkpoint import RenderCheckpoint
from little_crew.runtime.episode import EpisodeRunner
from little_crew.runtime.render import RenderSession

ENDPOINT = "http://127.0.0.1:8188"
PREVIS_ROOT = Path(r"D:\AI\_previs_out")

#: ComfyUI 侧的 Z-Image 权重（分离加载）
UNET = "z_image_turbo_int8_convrot.safetensors"
CLIP = "qwen_3_4b_fp4_mixed.safetensors"
VAE = "ae.safetensors"
PATCH_LITE = "Z-Image-Turbo-Fun-Controlnet-Union-2.1-lite-2601-8steps.safetensors"



class _ComfyRunner(EpisodeRunner):
    """走编译通道 —— 不需要 HTTP 后端。"""

    def engine_backend(self) -> Any:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一集 + 白模控制（ComfyUI 通道）")
    ap.add_argument("--limit", type=int, default=3, help="只跑前 N 镜")
    ap.add_argument("--seed", type=int, default=7777)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--sampler", default="euler")
    # Z-Image 上 karras 会产出崩坏画面；simple 是官方示例用的
    ap.add_argument("--scheduler", default="simple",
                    choices=("simple", "normal", "karras", "sgm_uniform"))
    ap.add_argument("--cfg", type=float, default=4.0)
    ap.add_argument("--strength", type=float, default=0.5)
    ap.add_argument("--blur", type=int, default=18,
                    help="控制信号高斯模糊半径，0=不模糊")
    ap.add_argument("--blur-sigma", type=float, default=5.0,
                    dest="blur_sigma")
    ap.add_argument("--channel", choices=("gray", "depth"), default="gray")
    ap.add_argument("--no-white-model", action="store_true",
                    help="对照组：不加白模控制")
    ap.add_argument("--workdir", default="outputs/episode_p3")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parents[3]
    work = root / args.workdir
    if args.fresh:
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("一集 + 白模控制 —— ComfyUI / Z-Image ControlNet 通道")
    print("=" * 78)
    print(f"  通道 {ENDPOINT}   工作目录 {work.name}")
    print(f"  白模 {'**关闭（对照组）**' if args.no_white_model else f'开启（{args.channel}）'}"
          f"   强度 {args.strength}   种子 {args.seed}")
    print()

    # ---------- 通道 ----------
    be = Backend(kind=BackendKind.COMFYUI, endpoint=ENDPOINT,
                 is_local_by_default=True)
    ch = ComfyChannel(be, endpoint=ENDPOINT, timeout_s=1800)
    okv, why = ch.available()
    if not okv:
        print(f"  ❌ ComfyUI 不可用：{why}")
        return 1

    # ---------- 能力矩阵 + 编译器 ----------
    t0 = time.perf_counter()
    raw = ch.probe_catalog()
    matrix = probe_from_object_info(raw, endpoint=ENDPOINT,
                                    comfy_version="0.39.0")
    print(f"  能力矩阵：{matrix.node_count()} 个节点  "
          f"catalogHash={matrix.catalog_hash()[:24]}…  "
          f"({time.perf_counter()-t0:.1f}s)")

    compiler = ComfyCompiler(matrix, default_model_patch=PATCH_LITE,
                             model_source_kind="model.source_zimage")
    renderer = CachingRenderer(
        RenderSession(ch, compiler), ContentCache(work / "cache"),
        fetcher=artifact_fetcher_for(ch),
        checkpoint=RenderCheckpoint(work / "checkpoint", episode_id="E100"))

    # ---------- 剧组 ----------
    orch = CrewOrchestrator(max_rounds=3, model_families={})
    orch.bind_many(default_crew_stubs())

    runner = _ComfyRunner(orch, renderer, episode_id="E100", title="薰衣草")
    runner.engine_seed = args.seed
    runner.engine_steps = args.steps
    runner.engine_sampler = args.sampler
    runner.engine_cfg = args.cfg
    runner.engine_scheduler = args.scheduler
    runner.engine_style = ("2D 动画风格，赛璐璐上色，电影感构图，"
                           "细致背景，戏剧性光影，白天，山路")
    runner.workdir = work
    runner.assemble_video = False          # 本次只验证图
    runner.use_white_model = not args.no_white_model
    # 模型源与「要不要控制」独立 —— 基线组也要走 Z-Image 的分离加载
    runner.use_zimage_source = True
    runner.previs_root = PREVIS_ROOT
    runner.previs_channel = args.channel
    runner.control_strength = args.strength
    runner.previs_blur_radius = args.blur
    runner.previs_blur_sigma = args.blur_sigma
    runner.zimage_unet, runner.zimage_clip, runner.zimage_vae = UNET, CLIP, VAE
    # LoadImage 只认 ComfyUI 的 input/ 目录 —— 白模帧要先投放进去
    runner.comfy_input_dir = Path(r"D:\ComfyUI\ComfyUI\input")

    print()
    print("【跑】剧本 → 分镜 → 角色定妆 → 白模控制 → 出图")
    print("-" * 78)
    res = runner.run(DEMO_SOURCE_LAVENDER, limit=args.limit)

    print(f"  剧本 {res.script_rounds} 轮 → {res.script_verdict}")
    print(f"  镜头 {res.planned_shots}   出图 {res.rendered + res.cache_hits}"
          f"   失败 {res.failed}")
    print(f"  墙钟 {res.wall_seconds:.1f}s")
    for f in res.failures[:5]:
        print(f"    ❌ {f['graphId']}  [{f['failure']}] {f['reason'][:90]}")

    if res.manifest is not None:
        print()
        print(f"  {'镜头':<26} {'动作':<10} {'控制图'}")
        print("  " + "-" * 72)
        for e in res.manifest.shots:
            print(f"  {e['shotId']:<26} {str(e.get('action','')):<10} "
                  f"{'有' if e['ready'] else '无'}")

    # 把生成结果复制出来
    outdir = work / "frames"
    outdir.mkdir(parents=True, exist_ok=True)
    idx: dict[str, Path] = {}
    for p in work.rglob("*.png"):
        idx.setdefault(p.name, p)
    copied = 0
    if res.manifest is not None:
        for e in res.manifest.shots:
            for nm in e["artifacts"]:
                p = idx.get(nm)
                if p and p.parent != outdir:
                    shutil.copyfile(p, outdir / f"{e['shotId']}.png")
                    copied += 1
                    break
    print()
    print("=" * 78)
    print(f"  结果：{'✅ 成功' if res.ok else '⚠️ 部分成功'} —— {res.reason}")
    print(f"  覆盖率 {res.coverage:.0%}   复制出 {copied} 张到 {outdir}")
    print("=" * 78)

    (work / "run.report.json").write_text(
        json.dumps(res.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
