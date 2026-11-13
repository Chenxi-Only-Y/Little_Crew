"""云端主链路：串行 vs 并行实测（2026-11-13，方案 C 的并行依据）。

## 为什么单独做这个

`render_policy` 里写着"云端并发可开，但**还没有真跑过**（没有云 key）"。
现在有 key 了，必须把这句话变成实测数字 —— 否则"云端可以并行"就只是配置。

## 做法

同一批 **8 个互不相同的题面**（避免缓存命中），分两组：
- A 组 4 张：`workers=1`（串行）
- B 组 4 张：`workers=4`（并行）

每组用**独立缓存目录**，确保都是真算。输出墙钟、单张均值、加速比。

用法：

```powershell
$env:PYTHONPATH="<项目>\\src"; $env:LITTLE_CREW_ALLOW_UNVERIFIED="1"
python tools/bench_cloud_parallel.py [--model gpt-image-2.6-sunburst] [--n 4]
```
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_PROMPTS = [
    "少女站在落地窗前看夜景，高精度 3D 半写实国漫 CG",
    "少年在雨中的街道奔跑，霓虹倒影，高精度 3D 半写实国漫 CG",
    "老人在旧书店里翻书，暖黄台灯，高精度 3D 半写实国漫 CG",
    "女孩坐在操场看台上吃冰棍，午后逆光，高精度 3D 半写实国漫 CG",
    "男孩在车站月台上等车，玻璃反光，高精度 3D 半写实国漫 CG",
    "两位少女在天台上说话，城市天际线，高精度 3D 半写实国漫 CG",
    "少年在便利店门口打电话，冷白灯，高精度 3D 半写实国漫 CG",
    "少女抱着一束花走过小巷，斑驳墙面，高精度 3D 半写实国漫 CG",
]


def _renderer(be, workdir: Path):
    from little_crew.runtime.cache import ContentCache
    from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
    from little_crew.runtime.cache_measure import _null_compiler
    from little_crew.runtime.render import RenderSession

    return CachingRenderer(RenderSession(_make_channel(be), _null_compiler()),
                           ContentCache(workdir / "cache"),
                           fetcher=artifact_fetcher_for(_make_channel(be)))


def _make_channel(be):
    from little_crew.runtime.backend_select import make_channel

    return make_channel(be, timeout_s=600)


def _values(prompt: str, seed: int) -> dict:
    return {"prompt": prompt, "negative_prompt": "", "width": 1536,
            "height": 1024, "steps": 4, "seed": seed}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-image-2.6-sunburst")
    ap.add_argument("--n", type=int, default=4, help="每组几张（默认 4）")
    ap.add_argument("--out", default="outputs/_cloudbench")
    args = ap.parse_args()

    os.environ.setdefault("LITTLE_CREW_ALLOW_UNVERIFIED", "1")
    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.backend_select import pick_backend

    spec = build_default_registry().get(args.model)
    if spec is None:
        print(f"❌ 模型未注册：{args.model}")
        return 2
    be = pick_backend(spec, "localgen")
    print(f"  模型 {args.model} · 后端 {be.kind} · {be.endpoint}")

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    n = max(2, min(int(args.n), len(_PROMPTS) // 2))

    groups = {"serial": (_PROMPTS[:n], list(range(1, n + 1))),
              "parallel": (_PROMPTS[n:2 * n], list(range(101, 101 + n)))}
    result = {}
    for mode, (prompts, seeds) in groups.items():
        wd = out / mode
        r = _renderer(be, wd)
        jobs = [(_values(p, s), f"bench-{mode}-{i}") for i, (p, s) in enumerate(zip(prompts, seeds))]
        t0 = time.time()
        if mode == "serial":
            outs = [r.render_http(v, be, job_id=j) for v, j in jobs]
        else:
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                outs = list(ex.map(lambda j: r.render_http(j[0], be, job_id=j[1]), jobs))
        wall = time.time() - t0
        ok = sum(1 for o in outs if getattr(o, "ok", False))
        per = [float(getattr(getattr(o, "render", None), "seconds", 0) or 0) for o in outs]
        result[mode] = {"wall": round(wall, 1), "ok": ok, "n": len(jobs),
                        "sumSingle": round(sum(per), 1)}
        print(f"  {mode:8s}: {ok}/{len(jobs)} 张 · 墙钟 {wall:.1f}s"
              f" · 单张合计 {sum(per):.1f}s")

    s, p = result["serial"], result["parallel"]
    speed = (s["wall"] / p["wall"]) if p["wall"] else 0
    print()
    print(f"  加速比：{speed:.2f}×（串行 {s['wall']}s -> 并行 4 路 {p['wall']}s）")
    (out / "bench.json").write_text(json.dumps({"model": args.model, **result,
                                                "speedup": round(speed, 2)},
                                               ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if (s["ok"] == s["n"] and p["ok"] == p["n"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
