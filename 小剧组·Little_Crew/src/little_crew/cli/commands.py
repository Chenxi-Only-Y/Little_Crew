"""CLI 子命令的实现。

设计原则
--------
**命令是"薄编排"，不是"第二套实现"。**
每个命令只做三件事：① 解析参数与配置 ② 组装已有的层 ③ 呈现结果。
判定逻辑在 `review/`，执行在 `runtime/`，编译在 `compiler/` ——
本模块**不复制**它们中的任何一条规则。

这样做的理由很实际：**若命令里藏了业务规则，那些规则就永远不会被测到**
（测试测的是层，不是 CLI）。而 CLI 一旦成为事实上的第二实现，
两边迟早漂移，且漂移无法察觉。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from little_crew.cli import ExitCode

__all__ = ["cmd_probe", "cmd_compile", "cmd_shot", "cmd_episode"]


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _endpoints(args: Any) -> tuple[str, str]:
    """解析两个端点。**参数 > .env > 默认**。"""
    from little_crew.config import env_value

    comfy = (getattr(args, "comfyui", "") or ""
             or env_value("COMFYUI_ENDPOINT", "")
             or "http://127.0.0.1:8188")
    local = (getattr(args, "local_gen", "") or ""
             or env_value("LOCAL_GEN_ENDPOINT", "")
             or "http://127.0.0.1:8100")
    return comfy.rstrip("/"), local.rstrip("/")


def _emit(args: Any, human: str, machine: dict[str, Any]) -> None:
    """统一输出：`--json` 给机器，默认给人。"""
    if getattr(args, "json", False):
        print(json.dumps(machine, ensure_ascii=False, indent=2))
    else:
        print(human)


def _repo_root() -> Path:
    """项目根（`src/little_crew/cli/` 往上三层）。"""
    return Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args: Any) -> int:
    """探测环境：两个通道是否可用、能力矩阵有多少节点。"""
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.models.spec import Backend, BackendKind
    from little_crew.runtime.channels.comfy import ComfyChannel
    from little_crew.runtime.channels.http import HttpChannel

    comfy_ep, local_ep = _endpoints(args)
    report: dict[str, Any] = {
        "comfyui": {"endpoint": comfy_ep},
        "localGen": {"endpoint": local_ep},
    }
    lines = ["环境探测", "=" * 60]

    # ---- ComfyUI ----
    ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=comfy_ep,
                              is_local_by_default=True),
                      endpoint=comfy_ep, timeout_s=60)
    ok, why = ch.available()
    report["comfyui"].update({"available": ok, "detail": why})
    lines.append(f"  ComfyUI   {comfy_ep}")
    lines.append(f"    {'✅ 可用' if ok else '❌ 不可用'}  {why}")

    if ok:
        try:
            raw = ch.probe_catalog()
            m = probe_from_object_info(raw, endpoint=comfy_ep,
                                       comfy_version="unknown")
            report["comfyui"].update({
                "nodeCount": m.node_count(),
                "catalogHash": m.catalog_hash(),
            })
            lines.append(f"    节点 {m.node_count()} 个   "
                         f"catalogHash={m.catalog_hash()[:24]}…")

            # 关键节点是否在位（决定哪些能力可用）
            needed = ("ZImageFunControlnet", "ModelPatchLoader", "ImageBlur",
                      "UNETLoader", "CLIPLoader", "VAELoader",
                      "CheckpointLoaderSimple", "KSampler")
            have = {n: m.spec(n) is not None for n in needed}
            report["comfyui"]["nodes"] = have
            missing = [n for n, v in have.items() if not v]
            lines.append(f"    关键节点 {'全部在位' if not missing else '缺 ' + ', '.join(missing)}")

            if getattr(args, "save_snapshot", ""):
                p = Path(args.save_snapshot)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(m.to_snapshot(), ensure_ascii=False,
                                        indent=2), encoding="utf-8")
                lines.append(f"    快照已存 {p}")
                report["comfyui"]["snapshot"] = str(p)
        except Exception as e:                          # noqa: BLE001
            report["comfyui"]["error"] = f"{type(e).__name__}: {e}"
            lines.append(f"    ⚠️ 能力探测失败：{type(e).__name__}: {e}")

    # ---- local-gen ----
    hch = HttpChannel(Backend(kind=BackendKind.HTTP, endpoint=local_ep,
                              path="/v1/images/generations",
                              is_local_by_default=True),
                      timeout_s=60)
    ok2, why2 = hch.available()
    report["localGen"].update({"available": ok2, "detail": why2})
    lines.append(f"  local-gen {local_ep}")
    lines.append(f"    {'✅ 可用' if ok2 else '❌ 不可用'}  {why2}")

    # ---- 配置里声明的后端 ----
    try:
        from little_crew.models.registry import build_default_registry
        reg = build_default_registry()
        report["models"] = reg.summary()
        lines.append(f"  模型注册表：{len(reg.all())} 个模型")
    except Exception as e:                              # noqa: BLE001
        report["models"] = {"error": str(e)}

    both = ok and ok2
    report["ok"] = both
    lines.append("=" * 60)
    lines.append("结论：" + ("两个通道都可用" if both else
                             "至少一个通道不可用（见上）"))

    _emit(args, "\n".join(lines), report)
    # **环境不可用 -> 专用退出码**（不是通用失败）
    return ExitCode.OK if both else ExitCode.ENVIRONMENT


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

def cmd_compile(args: Any) -> int:
    """只编译 IR，不提交。（排查"为什么这张图不对"的第一站。）"""
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler, CompileError
    from little_crew.models.spec import Backend, BackendKind
    from little_crew.runtime.channels.comfy import ComfyChannel

    src = Path(args.ir)
    if not src.is_file():
        raise FileNotFoundError(src)
    ir = json.loads(src.read_text(encoding="utf-8"))

    comfy_ep, _ = _endpoints(args)
    ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=comfy_ep,
                              is_local_by_default=True),
                      endpoint=comfy_ep, timeout_s=120)
    ok, why = ch.available()
    if not ok:
        print(f"ComfyUI 不可用：{why}", file=sys.stderr)
        return ExitCode.ENVIRONMENT

    m = probe_from_object_info(ch.probe_catalog(), endpoint=comfy_ep,
                               comfy_version="unknown")
    c = ComfyCompiler(m)
    try:
        res = c.compile(ir)
    except CompileError as e:
        print(f"编译失败：{e}", file=sys.stderr)
        return ExitCode.FAILED

    payload = {
        "graphId": res.graph_hash,
        "irHash": res.ir_hash,
        "nodeCount": len(res.graph),
        "notices": [n.as_dict() for n in res.notices],
        "drifts": [d.as_dict() if hasattr(d, "as_dict") else str(d)
                   for d in res.drifts],
        "graph": res.graph,
    }
    lines = [
        "编译结果",
        "=" * 60,
        f"  图哈希   {res.graph_hash}",
        f"  IR 哈希  {res.ir_hash}",
        f"  节点数   {len(res.graph)}",
        f"  通知     {len(res.notices)} 条",
        f"  漂移     {len(res.drifts)} 条",
    ]
    for n in res.notices[:6]:
        d = n.as_dict()
        lines.append(f"    [{d['kind']}] {d['from']} -> {d['to']}")
    if len(res.notices) > 6:
        lines.append(f"    … 另 {len(res.notices) - 6} 条")

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        lines.append(f"  已写入 {args.out}")

    _emit(args, "\n".join(lines), payload)
    return ExitCode.OK


# ---------------------------------------------------------------------------
# shot
# ---------------------------------------------------------------------------

def _load_shot(text_or_path: str):
    """从文件或一段直接描述构造 `Shot`。"""
    from little_crew.agent.messages import Shot

    p = Path(text_or_path)
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "shotId" in data:
            # 允许省略信封字段 —— CLI 的输入不该要求用户填消息元数据
            for k, v in (("frm", "cli"), ("to", "cli")):
                data.setdefault(k, v)
            return Shot(**{k: v for k, v in data.items()
                           if k in Shot.__dataclass_fields__})
        raise ValueError(f"{p} 不是合法的镜头 JSON（缺 shotId）")

    return Shot(frm="cli", to="cli", shot_id="SH-CLI-001", scene_id="SC-CLI-001",
                description=text_or_path, shot_size="full",
                camera_movement="static", action="stand",
                characters=(), duration_sec=5.0)


def cmd_shot(args: Any) -> int:
    """跑单个镜头 —— 排查"这一镜为什么不对"的最小单元。"""
    from little_crew.agent.ir_bridge import shot_to_ir
    from little_crew.capability.capability import probe_from_object_info
    from little_crew.compiler.compiler import ComfyCompiler
    from little_crew.models.spec import Backend, BackendKind
    from little_crew.runtime.channels.comfy import ComfyChannel
    from little_crew.runtime.channels.http import HttpChannel
    from little_crew.runtime.render import RenderSession

    shot = _load_shot(args.shot)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    ir = shot_to_ir(shot, episode_id="CLI", steps=args.steps)
    comfy_ep, local_ep = _endpoints(args)

    if args.channel == "comfyui":
        ch = ComfyChannel(Backend(kind=BackendKind.COMFYUI, endpoint=comfy_ep,
                                  is_local_by_default=True),
                          endpoint=comfy_ep, timeout_s=1800)
        ok, why = ch.available()
        if not ok:
            print(f"ComfyUI 不可用：{why}", file=sys.stderr)
            return ExitCode.ENVIRONMENT
        m = probe_from_object_info(ch.probe_catalog(), endpoint=comfy_ep,
                                   comfy_version="unknown")
        sess = RenderSession(ch, ComfyCompiler(m))
        outcome = sess.render(ir, job_id=shot.shot_id)
    else:
        be = Backend(kind=BackendKind.HTTP, endpoint=local_ep,
                     path="/v1/images/generations",
                     body_template=('{"prompt": "{prompt}", "width": {width},'
                                    ' "height": {height},'
                                    ' "num_inference_steps": {steps},'
                                    ' "seed": {seed}}'),
                     response_path="data.0.url",
                     is_local_by_default=True)
        ch2 = HttpChannel(be, timeout_s=1800)
        ok, why = ch2.available()
        if not ok:
            print(f"local-gen 不可用：{why}", file=sys.stderr)
            return ExitCode.ENVIRONMENT
        sess = RenderSession(ch2, None)
        outcome = sess.render_http(
            {"prompt": shot.description, "width": 832, "height": 480,
             "steps": args.steps, "seed": args.seed},
            backend=be, job_id=shot.shot_id)

    payload = {
        "shotId": shot.shot_id,
        "ok": outcome.ok,
        "failure": str(outcome.failure),
        "reason": outcome.reason,
        "seconds": round(outcome.seconds, 2),
        "artifacts": [a.uri for a in outcome.artifacts],
    }
    lines = [
        "单镜渲染",
        "=" * 60,
        f"  镜头     {shot.shot_id}",
        f"  通道     {args.channel}",
        f"  {'✅ 成功' if outcome.ok else '❌ 失败'}   {outcome.seconds:.1f}s",
    ]
    if outcome.ok:
        for a in outcome.artifacts:
            lines.append(f"    产物 {a.uri}")
    else:
        lines.append(f"    原因 [{outcome.failure}] {outcome.reason}")

    _emit(args, "\n".join(lines), payload)
    return ExitCode.OK if outcome.ok else ExitCode.FAILED


# ---------------------------------------------------------------------------
# episode
# ---------------------------------------------------------------------------

#: 未提供 `--script` 时用的内置剧本。
#:
#: **为什么内置一份**：CLI 的"跑一集"若必须自带文件，第一次用的人
#: 就卡在"我该写什么格式"。给一份可跑的样例，命令能直接成功，
#: 用户再照它的格式替换。这与 `--help` 里有示例是同一个道理。
_SAMPLE_SCRIPT = """[场1] 山中·午后
少女背着竹篮走在山路上，篮里已有半篮紫色花穗。
她看见路边躺着一个人，蹲下探他的额头，烫得吓人。

[场2] 山中·傍晚
旅人醒来，两人对坐。远处是成片的薰衣草田。
少女指向山下，那里有灯火。
"""


def cmd_episode(args: Any) -> int:
    """跑一集 —— 剧本 → 分镜 → 角色定妆 → 渲染 → 门禁 → 合成。"""
    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator
    from little_crew.models.spec import BackendKind
    from little_crew.runtime.cache import ContentCache
    from little_crew.runtime.cached_render import (CachingRenderer,
                                                   artifact_fetcher_for)
    from little_crew.runtime.checkpoint import RenderCheckpoint
    from little_crew.runtime.episode import EpisodeRunner
    from little_crew.runtime.render import RenderSession

    work = Path(args.out)
    work.mkdir(parents=True, exist_ok=True)

    if args.script:
        sp = Path(args.script)
        if not sp.is_file():
            raise FileNotFoundError(sp)
        source = sp.read_text(encoding="utf-8")
    else:
        source = _SAMPLE_SCRIPT

    comfy_ep, local_ep = _endpoints(args)

    # ---- 组装渲染器（通道差异只在这一段）----
    if args.channel == "comfyui":
        from little_crew.capability.capability import probe_from_object_info
        from little_crew.compiler.compiler import ComfyCompiler
        from little_crew.models.spec import Backend
        from little_crew.runtime.channels.comfy import ComfyChannel

        be = Backend(kind=BackendKind.COMFYUI, endpoint=comfy_ep,
                     is_local_by_default=True)
        ch = ComfyChannel(be, endpoint=comfy_ep, timeout_s=1800)
        ok, why = ch.available()
        if not ok:
            print(f"ComfyUI 不可用：{why}", file=sys.stderr)
            return ExitCode.ENVIRONMENT
        m = probe_from_object_info(ch.probe_catalog(), endpoint=comfy_ep,
                                   comfy_version="unknown")
        compiler = ComfyCompiler(m, model_source_kind="model.source_zimage")
        renderer = CachingRenderer(RenderSession(ch, compiler),
                                   ContentCache(work / "cache"),
                                   fetcher=artifact_fetcher_for(ch),
                                   checkpoint=RenderCheckpoint(
                                       work / "checkpoint", episode_id="CLI"))
        backend = None
    else:
        from little_crew.models.registry import build_default_registry
        from little_crew.runtime.channels.http import HttpChannel

        # ⚠️ **从注册表取后端声明，不要在这里另写一份。**
        #
        # 早期版本手写 `path` / `body_template` / `response_path`，
        # 而模板里只列了 4 个字段 —— 但 `EpisodeRunner._values()` 会发 6 个
        # （还有 `negative_prompt`），于是 local-gen 以 **HTTP 422** 拒绝。
        #
        # 那类缺陷的教训很直接：**同一份契约在两处维护，必然漂移**，
        # 而漂移的表现是"服务端说参数不对，可参数看着都对"。
        # 注册表已经是那份契约的单一来源。
        spec = build_default_registry().get("z-image-turbo")
        if spec is None:
            print("模型注册表里没有 z-image-turbo", file=sys.stderr)
            return ExitCode.ENVIRONMENT
        be = next((b for b in spec.backends if b.kind is BackendKind.HTTP),
                  None)
        if be is None:
            print("z-image-turbo 未声明 HTTP 后端", file=sys.stderr)
            return ExitCode.ENVIRONMENT

        ch = HttpChannel(be, timeout_s=1800)
        ok, why = ch.available()
        if not ok:
            print(f"local-gen 不可用：{why}", file=sys.stderr)
            return ExitCode.ENVIRONMENT
        renderer = CachingRenderer(RenderSession(ch, None),
                                   ContentCache(work / "cache"),
                                   fetcher=artifact_fetcher_for(ch),
                                   checkpoint=RenderCheckpoint(
                                       work / "checkpoint", episode_id="CLI"))
        backend = be

    # ---- 组装剧组 ----
    orch = CrewOrchestrator(max_rounds=3, model_families={})
    orch.bind_many(default_crew_stubs())

    class _R(EpisodeRunner):
        def engine_backend(self) -> Any:
            return backend

    runner = _R(orch, renderer, episode_id="CLI", title="CLI 一集")
    runner.workdir = work
    runner.engine_seed = args.seed
    runner.engine_steps = args.steps
    runner.verify_artifacts = not args.no_gate
    runner.assemble_video = not args.no_video
    if args.white_model:
        runner.use_white_model = True
        runner.use_zimage_source = True
        runner.previs_root = Path(args.white_model)

    res = runner.run(source, limit=(args.limit or None))

    payload = res.as_dict()
    lines = [
        "一集流水线",
        "=" * 60,
        f"  剧本   {res.script_rounds} 轮 → {res.script_verdict}",
        f"  镜头   {res.planned_shots} 镜   出图 {res.rendered + res.cache_hits}"
        f"   失败 {res.failed}",
        f"  覆盖率 {res.coverage:.0%}   墙钟 {res.wall_seconds:.1f}s",
    ]
    ev = res.evidence
    if ev is not None and ev.gate:
        c = (ev.gate.get("counts") or {})
        lines.append(f"  门禁   KEEP {c.get('kept', 0)} / RETRY {c.get('retry', 0)}"
                     f" / ESCALATE {c.get('escalate', 0)}"
                     f" / 跳过 {c.get('skipped', 0)}")
        for x in (ev.gate.get("results") or []):
            if x.get("verdict") != "KEEP":
                for rep in (x.get("repairs") or []):
                    lines.append(f"    ⚠️ {rep[:96]}")
    lines.append(f"  结果   {'✅ ' if res.ok else '⚠️ '}{res.reason}")

    # 报告落盘（`--json` 只是把它也打到 stdout）
    (work / "run.report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    _emit(args, "\n".join(lines), payload)
    return ExitCode.OK if res.ok else ExitCode.FAILED
