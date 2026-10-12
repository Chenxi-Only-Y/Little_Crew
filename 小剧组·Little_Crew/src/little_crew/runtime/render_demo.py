"""端到端演示 —— 剧本 → 剧组协作 → 镜头表 → 编译 → **真实出图**。

这是整条链路的第一次完整贯通：
```
剧本素材
  ↓ 编剧（出稿，首稿留结构缺陷）
剧本顾问 ‖ 文学策划（并行双维审查）
  ↓ 编排器合并 → 返工 → 修订 → 通过
script.final
  ↓ 分镜师
shot.list（每镜含景别/运镜/动作/角色/时长）
  ↓ ir_bridge
统一 IR（可编译、可校验、可缓存）
  ↓ compiler
ComfyUI 图
  ↓ ComfyChannel
**真实图片**
```

用法
----
    # 现探测环境并用真实 ComfyUI 出图（默认）
    python -m little_crew.runtime.render_demo

    # 只跑前 2 镜（省时间）
    python -m little_crew.runtime.render_demo --limit 2

    # 不出图，只验证到编译（无需 ComfyUI 在跑）
    python -m little_crew.runtime.render_demo --dry-run

**为什么要单独一个 demo 而不是塞进测试**：真机出图慢且有外部依赖，
不适合放进每次都要跑的测试套件；但它是**唯一能证明"链路真的通了"**的东西 ——
编译过了不等于出了图。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from little_crew.agent.crew_demo import DEMO_SOURCE
from little_crew.agent.crew_stubs import default_crew_stubs
from little_crew.agent.ir_bridge import shotlist_to_ir
from little_crew.agent.messages import ShotList
from little_crew.agent.orchestrator import CrewOrchestrator
from little_crew.capability.capability import probe_from_object_info
from little_crew.compiler.compiler import ComfyCompiler
from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.channels.comfy import ComfyChannel
from little_crew.runtime.channels.http import HttpChannel
from little_crew.runtime.render import BatchOutcome, FailureKind, RenderSession

class _NoopCompiler:
    """HTTP 路径的占位编译器。

    :class:RenderSession 的签名要求编译器，因为 ComfyUI 路径必需。
    但 HTTP 路径**不需要编译** —— 接口自己就是语义接口。
    用一个显式报错的占位符，而不是传 `None`：
    真被误用时立刻暴露，而不是在深处报一个难懂的 `AttributeError`。
    """

    def compile(self, ir: object) -> object:      # noqa: D102
        raise NotImplementedError(
            "HTTP 通道不做节点图编译；请用 RenderSession.render_http()")


DEFAULT_ENDPOINT = "http://127.0.0.1:8188"
CKPT = "sd_xl_base_1.0.safetensors"
LOCAL_GEN = "http://127.0.0.1:8100"

#: 漫剧风格后缀 —— 让出图风格统一，而不是每镜各异
STYLE_SUFFIX = ("2D anime style, cel shading, cinematic composition, "
                "detailed background, dramatic lighting")

#: Z-Image 用的中文风格后缀。
#:
#: **为什么中文与英文各一份**：Z-Image 是中文原生模型，中文提示词它更懂；
#: 而 SDXL 系只认英文。让引擎决定用哪份，比强行统一成一种语言更好。
STYLE_SUFFIX_ZH = "2D 动画风格，赛璐璐上色，电影感构图，细致背景，戏剧性光影"


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="小剧组端到端演示")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--engine", choices=("sdxl", "zimage"), default="zimage",
                    help="出图引擎：sdxl（ComfyUI 通道）或 zimage（local-gen HTTP 通道）")
    ap.add_argument("--local-gen", default=LOCAL_GEN)
    ap.add_argument("--limit", type=int, default=1,
                    help="渲染前 N 镜（默认 1，全量很慢）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只编译不出图（无需服务）")
    ap.add_argument("--steps", type=int, default=8,
                    help="采样步数（默认 8，草稿档）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    _hr("小剧组 · 端到端演示：剧本 → 协作 → 分镜 → 编译 → 出图")

    # ---------- 1. 剧组协作 ----------
    stubs = default_crew_stubs()
    orch = CrewOrchestrator(
        max_rounds=3,
        model_families={"screenwriter": "deepseek", "storyboard_artist": "deepseek",
                        "dramaturge": "qwen", "story_editor": "qwen",
                        "review_board": "qwen"})
    orch.bind_many(stubs)

    print("\n【1】剧本闭环")
    print("-" * 78)
    cycle = orch.run_script_cycle({"source.novel": DEMO_SOURCE,
                                   "episodeId": "E001", "title": "雨夜信"})
    for r in cycle.rounds:
        print(f"  第 {r.round_index} 轮  裁定 {r.verdict.value:9} "
              f"权威总分 {r.total:6.1f}  意见 {len(r.issues)} 条")
        for i in r.issues[:2]:
            print(f"      · [{i.severity.value:7}] {i.problem[:56]}")
    if not cycle.accepted:
        print(f"  ❌ 剧本未通过：{cycle.reason}")
        return 1
    print(f"  ✅ {cycle.reason}")

    # ---------- 2. 分镜 ----------
    print("\n【2】分镜")
    print("-" * 78)
    out = orch.run_stage("storyboard_artist", "shot.list",
                         {"script.final": cycle.final})
    if not out.ok:
        print(f"  ❌ {out.error}")
        return 1
    sl: ShotList = orch.blackboard.value("shot.list")
    print(f"  ✅ {sl.shot_count} 个镜头，总时长 {sl.total_seconds:.1f} 秒")

    # ---------- 3. 桥接 ----------
    print("\n【3】镜头 → IR")
    print("-" * 78)
    # 片段语言**由目标模型决定**：Z-Image 中文原生，SDXL 只认英文。
    # 混着写会让两边都拿不到最好的结果。
    pairs = shotlist_to_ir(
        sl, limit=args.limit,
        checkpoint=CKPT if args.engine == "sdxl" else "z-image-turbo",
        steps=args.steps,
        style_suffix=(STYLE_SUFFIX if args.engine == "sdxl" else STYLE_SUFFIX_ZH),
        prompt_language=("en" if args.engine == "sdxl" else "zh"))
    irs = [g for _, g in pairs]
    print(f"  转出 {len(pairs)} 张 IR（片段语言 "
          f"{'英文' if args.engine == 'sdxl' else '中文'}）")
    for s, g in pairs:
        print(f"    {s.shot_id}  {s.shot_size:13} {s.camera_movement:11} "
              f"{len(g['nodes'])} 节点")
        ir = next(n for n in g["nodes"] if n["id"] == "n_prompt")
        print(f"        提示词：{ir['params']['text'][:70]}…")

    # ---------- 4. 引擎准备 ----------
    print("\n【4】引擎准备")
    print("-" * 78)

    if args.engine == "sdxl":
        # ComfyUI 通道：IR → 节点图 → 提交
        if args.dry_run:
            from little_crew.compiler.test_compiler import env_minimal
            matrix = probe_from_object_info(env_minimal(), endpoint="fixture:8188",
                                            comfy_version="fixture")
            print("  （dry-run）使用代码内环境夹具")
        else:
            import json as _json
            import urllib.request
            url = args.endpoint.rstrip("/") + "/object_info"
            print(f"  探测 {url} …")
            with urllib.request.urlopen(url, timeout=90) as resp:
                raw = _json.loads(resp.read().decode("utf-8"))
            matrix = probe_from_object_info(raw, endpoint=args.endpoint,
                                            comfy_version="0.39.0")
            print(f"  ✅ 探测到 {len(matrix.nodes)} 个节点，"
                  f"catalogHash={matrix.catalog_hash()[:26]}…")
        compiler = ComfyCompiler(matrix)
        for g in irs:
            try:
                r = compiler.compile(g)
                print(f"  ✅ {g['graphId']}  编译通过  {len(r.graph)} 节点")
            except Exception as exc:                          # noqa: BLE001
                print(f"  ❌ {g['graphId']}  编译失败：{type(exc).__name__}: {exc}")
                return 1
    else:
        # local-gen HTTP 通道：IR 只用来取提示词与尺寸，不编译成图
        from little_crew.models.registry import build_default_registry
        from little_crew.models.spec import BackendKind
        _spec = build_default_registry().get("z-image-turbo")
        zbe = next(b for b in _spec.backends if b.kind is BackendKind.HTTP)
        print(f"  引擎：Z-Image-Turbo（中文原生）")
        print(f"  通道：HTTP {zbe.endpoint}{zbe.path}")
        print(f"  说明：HTTP 接口自己就是语义接口（提示词进、图片出），"
              f"**不需要编译成节点图**")
        for g in irs:
            txt = next(n for n in g["nodes"] if n["id"] == "n_prompt")["params"]["text"]
            print(f"  ✅ {g['graphId']}")
            print(f"      提示词：{txt[:64]}…")
        compiler = None

    if args.dry_run:
        _hr("dry-run 结束：全部就绪，未出图")
        print("  去掉 --dry-run 即可真实出图")
        return 0

    # ---------- 5. 真实出图 ----------
    print("\n【5】提交并出图")
    print("-" * 78)

    if args.engine == "sdxl":
        channel = ComfyChannel(
            Backend(kind=BackendKind.COMFYUI, endpoint=args.endpoint),
            endpoint=args.endpoint, timeout_s=1800)
    else:
        from little_crew.models.spec import Backend as _B
        channel = HttpChannel(zbe, timeout_s=1800)

    ok_avail, why = channel.available()
    if not ok_avail:
        print(f"  ❌ 通道不可用：{why}")
        return 1
    print(f"  ✅ 通道可用：{getattr(channel, 'endpoint', '?')}")

    session = RenderSession(channel, compiler) if compiler else None
    if session is None:
        # HTTP 路径不需要编译器 —— 会话只负责提交/幂等/回收
        session = RenderSession(channel, _NoopCompiler())

    t0 = time.time()
    if args.engine == "sdxl":
        batch = session.render_batch(irs)
    else:
        outs = []
        for g in irs:
            txt = next(n for n in g["nodes"] if n["id"] == "n_prompt")["params"]["text"]
            neg = next(n for n in g["nodes"] if n["id"] == "n_neg")["params"]["text"]
            lat = next(n for n in g["nodes"] if n["id"] == "n_latent")["params"]
            outs.append(session.render_http(
                {"prompt": txt, "negative_prompt": neg,
                 "width": lat["width"], "height": lat["height"],
                 "steps": args.steps, "seed": args.seed},
                backend=zbe, job_id=g["graphId"],
                provenance={"graphId": g["graphId"], "engine": "z-image-turbo"}))
        batch = BatchOutcome(outcomes=tuple(outs))
    dt = time.time() - t0

    print(f"\n  渲染完成：{len(batch.succeeded)}/{batch.total} 成功，"
          f"耗时 {dt:.1f} 秒")
    for o in batch.outcomes:
        if o.ok:
            for a in o.images:
                print(f"    ✅ {o.graph_id}")
                print(f"        {a.uri}")
        else:
            print(f"    ❌ {o.graph_id}  [{o.failure.value}] {o.reason[:70]}")
    if batch.failed:
        print(f"  失败分类：{batch.failures_by_kind()}")

    # ---------- 6. 摘要 ----------
    _hr("结果")
    print(f"  剧本：{cycle.round_count} 轮通过（{cycle.reason}）")
    print(f"  分镜：{sl.shot_count} 镜 / {sl.total_seconds:.1f} 秒")
    print(f"  渲染：{len(batch.succeeded)}/{batch.total} 成功")
    print(f"  产物：")
    for o in batch.succeeded:
        for a in o.images:
            print(f"    {a.uri}")
    print()
    print("  在 ComfyUI 的 output 目录下按文件名即可找到图片。")

    return 0 if batch.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
