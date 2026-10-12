"""断点续跑演示 —— 崩溃后重启，已完成的不重跑。

**这是"长跑可作性"的证据。** 一集按实测是分钟到小时级，
在这段时间里崩溃、重启、误关窗口**不是异常，是必然**。
而"从头再来"的代价不只是时间 —— 云端按次计费，重跑就是重复付费。

演示什么
--------
```
第一次运行：跑 5 镜，跑到第 3 镜后**硬中断**
   ↓（模拟崩溃：进程退出，只留下检查点账本）
重启 + 对账
   ↓
第二次运行：**只跑剩下的 2 镜**
```

用法
----
    # 真实 Z-Image（需 local-gen 在 8100）
    python -m little_crew.runtime.resume_demo

    # 假通道，只演示机制（快）
    python -m little_crew.runtime.resume_demo --dry-run
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from little_crew.agent.ir_bridge import shot_to_ir
from little_crew.agent.messages import Shot
from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
from little_crew.runtime.checkpoint import JobState, RenderCheckpoint
from little_crew.runtime.render import RenderSession

LOCAL_GEN = "http://127.0.0.1:8100"
STYLE_ZH = "2D 动画风格，赛璐璐上色，电影感构图，细致背景，戏剧性光影"

SHOTS: tuple[dict[str, Any], ...] = (
    {"id": "SH-E002-001-001", "size": "full", "move": "static",
     "desc": "站在天台边缘，手里攥着那封旧信"},
    {"id": "SH-E002-001-002", "size": "medium", "move": "dolly_in",
     "desc": "从楼梯口走上来，脚步声很轻"},
    {"id": "SH-E002-001-003", "size": "medium_close", "move": "truck_left",
     "desc": "没有回头，只是把信攥得更紧"},
    {"id": "SH-E002-001-004", "size": "close", "move": "static",
     "desc": "雨水顺着脸颊流下来"},
    {"id": "SH-E002-001-005", "size": "wide", "move": "dolly_out",
     "desc": "独自走进雨里，街灯把影子拉长"},
)


def _ir(spec: dict[str, Any]) -> dict[str, Any]:
    return shot_to_ir(
        Shot(frm="storyboard_artist", to="cinematographer",
             shot_id=spec["id"], scene_id="SC-E002-001",
             shot_size=spec["size"], camera_movement=spec["move"],
             action="walk", description=spec["desc"], duration_sec=6.0),
        episode_id="E002", checkpoint="z-image-turbo", steps=8,
        style_suffix=STYLE_ZH, prompt_language="zh")


def _values(ir: dict[str, Any], seed: int) -> dict[str, Any]:
    def param(nid: str, key: str) -> Any:
        return next(n for n in ir["nodes"] if n["id"] == nid)["params"][key]
    return {"prompt": param("n_prompt", "text"),
            "negative_prompt": param("n_neg", "text"),
            "width": param("n_latent", "width"),
            "height": param("n_latent", "height"),
            "steps": 8, "seed": seed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="断点续跑演示")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workdir", default="outputs/resume_demo")
    ap.add_argument("--fresh", action="store_true", help="清空工作目录再演示")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parents[3]
    work = root / args.workdir
    if args.fresh and work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    irs = [_ir(s) for s in SHOTS]
    backend = _backend(args)

    print("=" * 78)
    print("断点续跑演示 —— 崩溃后重启，已完成的不重跑")
    print("=" * 78)
    print(f"  镜头数 {len(irs)}   工作目录 {work}")
    print(f"  通道 {'假通道（dry-run）' if args.dry_run else 'Z-Image / local-gen'}")
    print()

    # ================= 第一次运行：跑到第 3 镜后硬中断 =================
    print("【第一次运行】模拟崩溃：跑到第 3 镜后硬中断")
    print("-" * 78)
    ck = RenderCheckpoint(work / "checkpoint", episode_id="E002")
    renderer, channel = _make(work, ck, args)
    done_before, t1 = _run_some(renderer, irs, backend, args, limit=3)
    print(f"  完成 {done_before} 镜，耗时 {t1:.1f}s")
    print("  ⚡ 模拟崩溃：进程退出（检查点账本已落盘）")
    print(f"     账本状态：{ck.counts()}")
    print()

    # ================= 重启 =================
    print("【重启】加载账本 + 向服务端对账")
    print("-" * 78)
    ck2 = RenderCheckpoint(work / "checkpoint")
    print(f"  账本记录数 {ck2.record_count}   状态 {ck2.counts()}")
    if ck2.load_error:
        print(f"  ⚠️ {ck2.load_error}")

    plan = ck2.plan(expected=[ir["graphId"] for ir in irs])
    print(f"  续跑计划：已完成 {len(plan.done)}  需对账 {len(plan.reconcile)}  "
          f"待重跑 {len(plan.todo)}  全新 {len(plan.fresh)}")

    if plan.needs_service:
        print(f"  向服务端对账 {len(plan.reconcile)} 个在途任务 …")
        _, ch_probe = _make(work, ck2, args)
        out = ck2.reconcile_now(ch_probe)
        print(f"    结果：{ {k: v.value for k, v in out.items()} }")
    else:
        print("  无在途任务，无需访问服务端")

    plan2 = ck2.plan(expected=[ir["graphId"] for ir in irs])
    need_run = [r.job_id for r in plan2.todo] + list(plan2.fresh)
    print(f"  对账后：已完成 {len(plan2.done)}  "
          f"仍需实跑 {len(need_run)} → {sorted(need_run)}")
    print()

    # ================= 第二次运行：只跑剩下的 =================
    print("【第二次运行】只跑剩下的镜头")
    print("-" * 78)
    renderer2, _ = _make(work, ck2, args)
    todo_irs = [ir for ir in irs
                if ir["graphId"] in set(need_run)
                or ck2.get(ir["graphId"]) is None]
    # 已完成的那几镜也会走一遍缓存 —— 它们应当**命中而非实算**
    t2 = time.perf_counter()
    batch = renderer2.render_http_batch(
        [(_values(ir, args.seed), ir["graphId"]) for ir in irs], backend)
    dt2 = time.perf_counter() - t2

    print(f"  本遍耗时 {dt2:.1f}s")
    print(f"    实算 {len(batch.rendered)}  命中 {len(batch.cache_hits)}  "
          f"失败 {len(batch.failed)}")
    print(f"  最终账本状态：{ck2.counts()}")
    print()

    # ================= 汇总 =================
    print("=" * 78)
    print("续跑效果")
    print("=" * 78)
    print(f"  第一次运行完成      {done_before} 镜（{t1:.1f}s）")
    print(f"  若不续跑、从头再来   需重跑 {len(irs)} 镜")
    print(f"  实际续跑           实算 {len(batch.rendered)} 镜"
          f"，其余 {len(batch.cache_hits)} 镜直接命中")
    saved = t1 - dt2
    print(f"  省下的时间         约 {saved:.1f}s"
          if saved > 0 else f"  本遍耗时 {dt2:.1f}s")

    ok = (len(batch.rendered) == len(need_run)
          and ck2.counts().get(JobState.SUCCEEDED.value, 0) == len(irs))
    print()
    print("=" * 78)
    print(f"结论：{'✅ 续跑正确 —— 只跑了缺的那几镜' if ok else '❌ 续跑未达预期'}")
    print("=" * 78)
    return 0 if ok else 1


def _backend(args: argparse.Namespace) -> Backend:
    if args.dry_run:
        return Backend(kind=BackendKind.HTTP, endpoint="http://fake:8100",
                       path="/v1/images/generations",
                       body_template=('{"prompt": "{prompt}", "width": {width},'
                                      ' "height": {height},'
                                      ' "num_inference_steps": {steps},'
                                      ' "seed": {seed}}'),
                       response_path="data.0.url")
    from little_crew.models.registry import build_default_registry

    spec = build_default_registry().get("z-image-turbo")
    return next(b for b in spec.backends if b.kind is BackendKind.HTTP)


def _make(work: Path, ck: RenderCheckpoint, args: argparse.Namespace):
    if args.dry_run:
        # ⚠️ 模块内的名字**不能**在这里导入 —— Python 会把整个函数作用域内
        # 出现的名字都视为局部变量，于是 ``else`` 分支取不到它
        # （`UnboundLocalError: cannot access local variable`）。
        from little_crew.runtime.cache_measure import _FakeChannel

        ch = _FakeChannel()
        session = RenderSession(ch, _null_compiler())
        fetcher = _fake_fetcher
    else:
        from little_crew.runtime.channels.http import HttpChannel

        be = _backend(args)
        ch = HttpChannel(be, timeout_s=1800)
        session = RenderSession(ch, _null_compiler())
        fetcher = artifact_fetcher_for(ch)
    r = CachingRenderer(session, ContentCache(work / "cache"),
                        fetcher=fetcher, checkpoint=ck)
    return r, ch


def _fake_fetcher(ref: Any) -> bytes:
    return f"PNG:{ref.uri}".encode()


def _run_some(renderer: CachingRenderer, irs: list[dict[str, Any]],
              backend: Backend, args: argparse.Namespace, *, limit: int
              ) -> tuple[int, float]:
    """只跑前 ``limit`` 镜，然后"崩溃"。"""
    picked = irs[:limit]
    t0 = time.perf_counter()
    for ir in picked:
        renderer.render_http(_values(ir, args.seed), backend,
                             job_id=ir["graphId"])
    return len(picked), time.perf_counter() - t0


def _null_compiler() -> Any:
    class C:
        def compile(self, ir: Any) -> Any:
            raise NotImplementedError("HTTP 路径不编译")
    return C()


if __name__ == "__main__":
    raise SystemExit(main())
