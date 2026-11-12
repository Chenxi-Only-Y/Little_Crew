#!/usr/bin/env python3
"""渲染 A/B 测量：**串行 vs 并发**（2026-11-12）。

## 为什么要有这个工具

"渲染能不能并行"这件事，我们已经凭印象改过一次，也踩过一次：
分镜并发实测**更慢**（83.5s 串行 vs 95.5s 4 线程）。
所以"要不要并行渲染"必须**量出来**，不能拍。

本工具做一件事：拿同一批真实 IR，分别用串行 / 并发各渲一遍，
比较**墙钟**与**每张平均耗时**。缓存目录分开且用后即弃，
保证第二遍**真的重算**（否则会比出"并行快 10 倍"这种假结论）。

## 用法

```bash
python tools/render_ab.py --workdir outputs/_EF39 --shots 4 --workers 2
python tools/render_ab.py --workdir outputs/_EF39 --shots 4 --workers 2 --apply-cache
```

- `--shots N`：取前 N 镜（默认 4；一镜约 21 秒，4 镜 ≈ 90 秒/轮）
- `--workers N`：并发度（默认 2）
- `--apply-cache`：共享 `--workdir/cache`（**只用于"再次运行"的复算验证**，
  默认会开临时缓存，确保两轮都真算）

## 三条纪律

1 **两轮用同一批 IR**：不同镜头的耗时差异远大于并发收益，混用会得出随机结论。
2 **缓存隔离**：两轮各用独立临时缓存目录，跑完删掉。
3 **报告要含"没有收益"这个结果**：如果并发不快，就如实写"不建议开"。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _load_irs(workdir: Path, shots: int) -> list[dict]:
    ck = workdir / "checkpoint" / "stages.json"
    if not ck.is_file():
        raise SystemExit(f"找不到 checkpoint：{ck}")
    data = json.loads(ck.read_text(encoding="utf-8"))
    irs = ((data.get("stages") or {}).get("render") or {}).get("data", {}).get("irJson") or {}
    picked = list(irs.values())[:max(1, int(shots))]
    if not picked:
        raise SystemExit("checkpoint 里没有 irJson（这一集可能没跑过渲染）")
    return picked


def _build(workdir: Path, cache_dir: Path, *, timeout_s: float = 1800):
    """按真实装配方式造渲染器（ComfyUI 通道 + 真编译器 + 内容缓存）。"""
    from little_crew.models.spec import BackendKind
    from little_crew.runtime.backend_select import make_channel, pick_backend
    from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
    from little_crew.runtime.cache import ContentCache
    from little_crew.runtime.episode_demo import _comfy_compiler  # type: ignore
    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.render import RenderSession

    spec = build_default_registry().get("z-image-turbo")
    if spec is None:
        raise SystemExit("注册表里没有 z-image-turbo")
    be = pick_backend(spec, "comfyui")
    ch = make_channel(be, timeout_s=timeout_s)
    ok, why = ch.available()
    if not ok:
        raise SystemExit(f"ComfyUI 不可用：{why}")
    comp = (_comfy_compiler(ch, be.endpoint)
            if be.kind is BackendKind.COMFYUI else None)
    renderer = CachingRenderer(RenderSession(ch, comp),
                               ContentCache(cache_dir),
                               fetcher=artifact_fetcher_for(ch))
    return renderer


def _time_serial(renderer, irs: list[dict]) -> list[float]:
    out = []
    for ir in irs:
        t0 = time.perf_counter()
        renderer.render(ir)
        out.append(time.perf_counter() - t0)
    return out


def _time_parallel(renderer, irs: list[dict], workers: int) -> list[float]:
    def _one(ir):
        t0 = time.perf_counter()
        renderer.render(ir)
        return time.perf_counter() - t0

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        return list(ex.map(_one, irs))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="渲染 A/B：串行 vs 并发")
    ap.add_argument("--workdir", required=True, help="某一集的工作目录（要含 checkpoint）")
    ap.add_argument("--shots", type=int, default=4, help="取前 N 镜（默认 4）")
    ap.add_argument("--workers", type=int, default=2, help="并发度（默认 2）")
    ap.add_argument("--apply-cache", action="store_true",
                    help="共享 workdir/cache（默认各轮独立临时缓存，确保真算）")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")                # type: ignore[attr-defined]

    wd = Path(args.workdir)
    irs = _load_irs(wd, args.shots)
    print(f"  A/B：{len(irs)} 镜 · 并发度 {args.workers} · 缓存"
          f"{'共享' if args.apply_cache else '隔离（确保真算）'}")

    tmp_a = tmp_b = None
    if args.apply_cache:
        ca = cb = wd / "cache"
    else:
        tmp_a = Path(tempfile.mkdtemp(prefix="ab_a_"))
        tmp_b = Path(tempfile.mkdtemp(prefix="ab_b_"))
        ca, cb = tmp_a, tmp_b

    try:
        print("  [A] 串行…")
        ra = _build(wd, ca)
        t0 = time.perf_counter()
        sa = _time_serial(ra, irs)
        wall_a = time.perf_counter() - t0
        print(f"      墙钟 {wall_a:.1f}s · 单张均值 {sum(sa)/len(sa):.1f}s")

        print(f"  [B] 并发 {args.workers}…")
        rb = _build(wd, cb)
        t0 = time.perf_counter()
        sb = _time_parallel(rb, irs, args.workers)
        wall_b = time.perf_counter() - t0
        print(f"      墙钟 {wall_b:.1f}s · 单张均值 {sum(sb)/len(sb):.1f}s")

        speed = wall_a / wall_b if wall_b else 0.0
        print()
        print(f"  结论：并发 {args.workers} -> {speed:.2f}x "
              f"（{'更快' if speed > 1.05 else '无收益/更慢' if speed < 0.95 else '几乎相同'}）")
        print(f"  建议：{'可以开' if speed > 1.15 else '**不要开**（默认串行）'}")
        return 0
    finally:
        for t in (tmp_a, tmp_b):
            if t is not None:
                shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
