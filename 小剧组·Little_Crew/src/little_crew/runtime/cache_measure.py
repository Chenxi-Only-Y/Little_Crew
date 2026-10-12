"""缓存效果实测 —— 采集「只重跑受影响镜头」的真实数据。

这是项目里**唯一为"成本可控"这一论点采集一手数据**的工具。
它的输出直接进论文与答辩材料。

三次运行的设计
--------------
| 运行 | 场景 | 期望 |
|---|---|---|
| **R1** | 首跑 3 镜 | 全部实算，建立缓存 |
| **R2** | 什么都不改，重跑 | **零实算** —— 这是"重跑一集"的收益 |
| **R3** | **只改第 2 镜**，重跑 | **1 镜实算 + 2 镜命中** —— 这是"改一句"的收益 |

R3 是核心命题：改一个镜头，只有它重跑。

⚠️ 每次运行都开**新会话**（新通道、新 RenderSession）—— 
那才是真实的"重跑一集"。同一个会话内重跑会被会话自身的 R1 幂等掩盖，
测不出缓存的收益。

用法
----
    # 默认用 Z-Image（需 local-gen 在 8100 端口）
    python -m little_crew.runtime.cache_measure --shots 3

    # 不真出图，用假通道测机制（快，用于回归）
    python -m little_crew.runtime.cache_measure --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from little_crew.agent.ir_bridge import shot_to_ir
from little_crew.agent.messages import Shot
from little_crew.models.spec import Backend, BackendKind
from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cached_render import (
    CachingRenderer, artifact_fetcher_for)
from little_crew.runtime.render import RenderSession

DEFAULT_CACHE_DIR = "outputs/cache/measure"
LOCAL_GEN = "http://127.0.0.1:8100"

#: 与演示一致的中文风格后缀
STYLE_ZH = "2D 动画风格，赛璐璐上色，电影感构图，细致背景，戏剧性光影"

#: 演示用的三个镜头（同一场戏的连续镜头）
SHOTS: tuple[dict[str, Any], ...] = (
    {"id": "SH-E001-001-001", "size": "full", "move": "static",
     "action": "walk", "desc": "站在天台边缘，手里攥着那封旧信"},
    {"id": "SH-E001-001-002", "size": "medium", "move": "dolly_in",
     "action": "stand", "desc": "从楼梯口走上来，脚步声很轻"},
    {"id": "SH-E001-001-003", "size": "medium_close", "move": "truck_left",
     "action": "stand", "desc": "没有回头，只是把信攥得更紧"},
)


def _ir(spec: dict[str, Any], *, variant: str = "") -> dict[str, Any]:
    shot = Shot(
        frm="storyboard_artist", to="cinematographer",
        shot_id=spec["id"], scene_id="SC-E001-001",
        shot_size=spec["size"], camera_movement=spec["move"],
        action=spec["action"],
        description=spec["desc"] + (f"（{variant}）" if variant else ""),
        duration_sec=6.0)
    return shot_to_ir(shot, episode_id="E001",
                      checkpoint="z-image-turbo", steps=8,
                      style_suffix=STYLE_ZH, prompt_language="zh")


def _values(ir: dict[str, Any], seed: int) -> dict[str, Any]:
    """从 IR 里取出 HTTP 通道需要的入参。"""
    def param(nid: str, key: str) -> Any:
        return next(n for n in ir["nodes"] if n["id"] == nid)["params"][key]
    return {"prompt": param("n_prompt", "text"),
            "negative_prompt": param("n_neg", "text"),
            "width": param("n_latent", "width"),
            "height": param("n_latent", "height"),
            "steps": ir["nodes"][0].get("params", {}).get("steps", 8) or 8,
            "seed": seed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="缓存效果实测")
    ap.add_argument("--shots", type=int, default=3)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    ap.add_argument("--dry-run", action="store_true",
                    help="用假通道测机制，不真出图")
    ap.add_argument("--fresh", action="store_true",
                    help="清空缓存目录再测")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    root = Path(__file__).resolve().parents[3]
    cache_dir = root / args.cache_dir
    if args.fresh and cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

    specs = list(SHOTS[: max(1, args.shots)])
    irs = [_ir(s) for s in specs]

    print("=" * 78)
    print("缓存效果实测 —— 只重跑受影响的镜头")
    print("=" * 78)
    print(f"  镜头数 {len(irs)}   缓存目录 {cache_dir}")
    print(f"  通道 {'假通道（dry-run）' if args.dry_run else 'Z-Image / local-gen'}")
    print()

    runs: list[dict[str, Any]] = []

    # ---------- R1：首跑 ----------
    r1 = _run(cache_dir, "R1", irs, args, changed=None)
    runs.append(r1)

    # ---------- R2：无改动重跑 ----------
    r2 = _run(cache_dir, "R2", irs, args, changed=None)
    runs.append(r2)

    # ---------- R3：只改第 2 镜 ----------
    irs3 = list(irs)
    if len(irs3) >= 2:
        irs3[1] = _ir(specs[1], variant="服装改为深灰")
    r3 = _run(cache_dir, "R3", irs3, args, changed=1)
    runs.append(r3)

    # ---------- 汇总 ----------
    print("=" * 78)
    print("实测汇总")
    print("=" * 78)
    print(f"  {'运行':<6} {'场景':<22} {'实算':>4} {'命中':>4} "
          f"{'命中率':>7} {'耗时':>9} {'节省':>9}")
    print("  " + "-" * 72)
    labels = ("首跑（建立缓存）", "无改动重跑", "只改第 2 镜后重跑")
    for run, lab in zip(runs, labels):
        print(f"  {run['tag']:<6} {lab:<22} {run['rendered']:>4} "
              f"{run['hits']:>4} {run['hit_rate']:>7.1%} "
              f"{run['seconds']:>8.1f}s {run['saved']:>8.1f}s")

    print()
    base = runs[0]["seconds"]
    if base > 0:
        for run, lab in zip(runs[1:], labels[1:]):
            ratio = (run["saved"] + run["seconds"]) / max(run["seconds"], 1e-9)
            print(f"  {run['tag']}（{lab}）：相比首跑，"
                  f"实算 {runs[0]['rendered']} → {run['rendered']} 镜，"
                  f"提速 {ratio:.2f}×")

    out = root / "outputs" / "cache" / "measure_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "shots": len(irs), "seed": args.seed, "dryRun": args.dry_run,
        "runs": runs,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告已写入 {out}")

    # 核心命题的判定
    print()
    ok = (runs[0]["rendered"] == len(irs)
          and runs[1]["rendered"] == 0
          and (len(irs) < 2 or runs[2]["rendered"] == 1))
    print("=" * 78)
    print(f"核心命题：{'✅ 成立' if ok else '❌ 未成立'} —— "
          f"改一个镜头，只有它被重跑")
    print("=" * 78)
    return 0 if ok else 1


def _run(cache_dir: Path, tag: str, irs: list[dict[str, Any]],
         args: argparse.Namespace, *, changed: int | None) -> dict[str, Any]:
    """跑一次（**新会话 = 新一次运行**）。"""
    label = {0: "", 1: "（改第 2 镜）", 2: "（改第 3 镜）"}.get(
        changed if changed is not None else -1, "")
    print(f"  【{tag}】{'重跑' if tag == 'R1' else '重跑'} "
          f"{len(irs)} 镜{label} …")

    if args.dry_run:
        # 假通道 + 假后端（**走 HTTP 路径**，与真实测量同一条代码路径）
        #
        # 刻意不在这里走 `render()`（编译路径）：本工具测的是 HTTP 引擎，
        # 走错路径会得到"编译异常 NotImplementedError"这种与缓存无关的失败，
        # 掩盖真正要测的东西。
        ch = _FakeChannel()
        be = Backend(kind=BackendKind.HTTP, endpoint="http://fake:8100",
                     path="/v1/images/generations",
                     body_template=('{"prompt": "{prompt}", "width": {width},'
                                    ' "height": {height},'
                                    ' "num_inference_steps": {steps},'
                                    ' "seed": {seed}}'),
                     response_path="data.0.url")
        session = RenderSession(ch, _null_compiler())
        renderer = CachingRenderer(session, ContentCache(cache_dir),
                                   fetcher=_fake_fetcher)
    else:
        from little_crew.models.registry import build_default_registry
        from little_crew.runtime.channels.http import HttpChannel

        spec = build_default_registry().get("z-image-turbo")
        be = next(b for b in spec.backends if b.kind is BackendKind.HTTP)
        ch = HttpChannel(be, timeout_s=1800)
        okv, why = ch.available()
        if not okv:
            print(f"    ❌ 通道不可用：{why}")
            return {"tag": tag, "rendered": 0, "hits": 0, "hit_rate": 0.0,
                    "seconds": 0.0, "saved": 0.0, "error": why}
        session = RenderSession(ch, _null_compiler())
        renderer = CachingRenderer(session, ContentCache(cache_dir),
                                   fetcher=artifact_fetcher_for(ch))

    items = [(_values(ir, args.seed), ir["graphId"]) for ir in irs]
    t0 = time.perf_counter()
    batch = renderer.render_http_batch(items, be)
    dt = time.perf_counter() - t0
    rendered = len(batch.rendered)

    r = {"tag": tag, "rendered": rendered, "hits": len(batch.cache_hits),
         "hit_rate": batch.hit_rate, "seconds": round(dt, 2),
         "saved": round(batch.seconds_saved, 2),
         "failed": len(batch.failed)}
    print(f"    实算 {r['rendered']}  命中 {r['hits']}  "
          f"耗时 {r['seconds']:.1f}s  节省 {r['saved']:.1f}s"
          + (f"  失败 {r['failed']}" if r["failed"] else ""))
    return r


# ==========================================================================
# dry-run 用的替身
# ==========================================================================

class _FakeChannel:
    def __init__(self) -> None:
        self.endpoint = "http://fake:8188"
        self.channel_id = "fake"
        self._n = 0
        self._by_key: dict[str, Any] = {}

    def available(self) -> tuple[bool, str]:
        return True, ""

    def submit(self, job: Any) -> Any:
        from little_crew.runtime.channels.base import JobHandle, JobStatus
        prior = self._by_key.get(job.idempotency_key)
        if prior:
            return prior
        self._n += 1
        h = JobHandle(job_id=job.idempotency_key, channel_id=f"p{self._n}",
                      status=JobStatus.PENDING)
        self._by_key[job.idempotency_key] = h
        return h

    def poll(self, h: Any) -> Any:
        from little_crew.runtime.channels.base import JobStatus
        h.status = JobStatus.SUCCEEDED
        return h

    def collect(self, h: Any) -> list[Any]:
        from little_crew.runtime.channels.base import ArtifactRef
        return [ArtifactRef(kind="image",
                            uri=f"/view?filename={h.channel_id}.png")]

    def reconcile(self, ids: Any) -> dict:
        return {}

    def cancel(self, h: Any) -> None:
        pass


def _fake_fetcher(ref: Any) -> bytes:
    return f"PNG:{ref.uri}".encode()


def _null_compiler() -> Any:
    class C:
        def compile(self, ir: Any) -> Any:
            raise NotImplementedError("HTTP 路径不编译")
    return C()


if __name__ == "__main__":
    raise SystemExit(main())
