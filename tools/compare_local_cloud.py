"""B3 同镜对照（独立工具）：同一批 IR，本地渲一遍，与云端版并排。

## 为什么单独一个工具

`episode --compare-local` 把对照做在流水线里（方便，但云端整集跑一次很贵）。
本工具只做"给定 workdir 里已有的 IR + 云端产物"，**不重跑云端**：

```
checkpoint/stages.json 的 render.irJson  ->  本地渲染  ->  assets/local_shots/
assets/shots/<sid>/*.png（云端）          ->  并排拼图 ->  delivery/render_compare_*.jpg
```

## 本地渲染为什么不用 CachingRenderer

`RenderSession` 的编译路径对 HTTP 后端会 `NotImplementedError`（HTTP 路径不编译），
所以这里走**已验证可用**的直连路径：`http_job + HttpChannel.submit/collect + net.urlopen`。

用法：

```powershell
$env:PYTHONPATH="<项目>\\src"; $env:LITTLE_CREW_ALLOW_UNVERIFIED="1"
python tools/compare_local_cloud.py --workdir outputs/_EFLAST60C --episode EF-LAST60C
```
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _values(ir: dict, seed: int = 7) -> dict:
    """从 IR 取 HTTP 通道入参（与 EpisodeRunner._values 同口径，但独立不 import runtime）。"""
    def param(nid: str, key: str):
        return next(n for n in ir["nodes"] if n["id"] == nid)["params"][key]

    try:
        from little_crew.compiler.exits import compile_ir

        out = compile_ir(ir, None, kind="image", seed=seed)
        if out.ok:
            v = {k: out.params[k] for k in ("prompt", "negative_prompt", "width", "height")}
            v["steps"] = seed and 4
            v["seed"] = seed
            return v
    except Exception:                                         # noqa: BLE001
        pass
    return {"prompt": param("n_prompt", "text"),
            "negative_prompt": param("n_neg", "text"),
            "width": param("n_latent", "width"),
            "height": param("n_latent", "height"),
            "steps": 4, "seed": seed}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--episode", required=True)
    ap.add_argument("--model", default="z-image-turbo")
    ap.add_argument("--backend", default="localgen")
    ap.add_argument("--limit", type=int, default=0, help="只渲前 N 镜（0=全部）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    wd = Path(args.workdir)
    if not wd.is_absolute():
        wd = ROOT / wd
    ck_f = wd / "checkpoint" / "stages.json"
    if not ck_f.is_file():
        print(f"❌ 没有 checkpoint/stages.json：{ck_f}")
        return 2
    ck = json.loads(ck_f.read_text(encoding="utf-8"))
    ir_json = ((ck.get("stages") or {}).get("render") or {}).get("data", {}) \
        .get("irJson") or {}
    items = [(str(sid), ir) for sid, ir in ir_json.items()]
    if args.limit:
        items = items[: args.limit]
    print(f"  工作目录 {wd.name} · 集 {args.episode} · {len(items)} 镜")

    from little_crew.models.registry import build_default_registry
    from little_crew.runtime.backend_select import make_channel, pick_backend
    from little_crew.runtime.render import http_job
    from little_crew import net

    spec = build_default_registry().get(args.model)
    be = pick_backend(spec, args.backend)
    ch = make_channel(be, timeout_s=900)
    ok_ch, why = ch.available()
    if not ok_ch:
        print(f"❌ 本地后端不可用：{why}")
        return 2
    print(f"  本地后端：{be.endpoint}")

    local_dir = wd / "assets" / "local_shots"
    local_dir.mkdir(parents=True, exist_ok=True)
    cloud_root = wd / "assets" / "shots"
    rows: list[dict] = []
    t_all = time.time()
    for i, (sid, ir) in enumerate(items, 1):
        values = _values(ir, seed=args.seed)
        local_path = local_dir / f"{sid}.png"
        err = ""
        t0 = time.time()
        try:
            if not local_path.is_file():
                job = http_job(f"cmp-{args.episode}-{sid}", be, values,
                               idempotency_key=f"cmp-{sid}", expect="image",
                               timeout_s=900)
                h = ch.submit(job)
                if "SUCCEEDED" not in str(getattr(h, "status", "")):
                    err = f"提交失败：{getattr(getattr(h, 'error', None), 'message', '') or h.status}"
                else:
                    arts = ch.collect(h)
                    if not arts:
                        err = "提交成功但没有产物"
                    else:
                        uri = str(arts[0].uri)
                        if uri.startswith(("http://", "https://")):
                            with net.urlopen(net.urllib.request.Request(
                                    uri, headers={"User-Agent": "OpenAI/Python 1.0"}),
                                    timeout=300) as r:
                                blob = r.read()
                        else:
                            blob = Path(uri).read_bytes()
                        local_path.write_bytes(blob)
        except Exception as exc:                              # noqa: BLE001
            err = f"{type(exc).__name__}: {str(exc)[:120]}"
        dt = round(time.time() - t0, 1)
        hits = list((cloud_root / sid).glob("*.png")) \
            if (cloud_root / sid).is_dir() else []
        cloud = str(hits[0]) if hits else ""
        rows.append({"shotId": sid, "cloud": cloud,
                     "local": str(local_path) if local_path.is_file() else "",
                     "seconds": dt, "error": err})
        print(f"  [{i}/{len(items)}] {sid} · 本地 {dt}s"
              + (f" · ⚠️ {err}" if err else ""))

    ok = [r for r in rows if r["cloud"] and r["local"]]
    report = {"kind": "render.compare", "mode": "同镜（同一批 IR，独立工具）",
              "episode": args.episode, "model": args.model,
              "compared": len(ok), "total": len(rows), "rows": rows,
              "wallSeconds": round(time.time() - t_all, 1),
              "note": "左云端 / 右本地；本地渲染不参与主流程产物"}
    (_d := wd / "delivery").mkdir(parents=True, exist_ok=True)
    (_d / "render_compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  对照：{len(ok)}/{len(rows)} 镜两边都有图 · 墙钟 {report['wallSeconds']}s")

    # ---- 并排拼图：每张 6 镜，左列云端、右列本地 ----
    try:
        import subprocess

        exe = r"D:\AI\FFmpeg\bin\ffmpeg.exe"
        W, H = 640, 360
        for sheet_i in range(0, len(ok), 6):
            chunk = ok[sheet_i:sheet_i + 6]
            if not chunk:
                continue
            cmd = [exe, "-y", "-hide_banner", "-loglevel", "error"]
            for r in chunk:
                cmd += ["-i", r["cloud"]]
            for r in chunk:
                cmd += ["-i", r["local"]]
            n = len(chunk)
            parts = [f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                     f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
                     for i in range(n * 2)]
            layout = "|".join(
                item for i in range(n)
                for item in (f"0_{i * H}", f"{W}_{i * H}"))
            parts.append("".join(f"[v{i}]" for i in range(n * 2))
                         + f"xstack=inputs={n * 2}:layout={layout}")
            out = _d / f"render_compare_{sheet_i // 6 + 1}.jpg"
            cmd += ["-filter_complex", ";".join(parts), "-frames:v", "1", str(out)]
            subprocess.run(cmd, check=True, capture_output=True, timeout=240)
            print(f"  对照图：{out.name}")
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️ 拼图失败：{type(exc).__name__}: {exc}")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
