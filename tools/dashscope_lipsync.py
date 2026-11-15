#!/usr/bin/env python
"""DashScope `wan2.2-s2v` 单镜口型验证（命令行入口）。

⚠️ **实现只有一份**：`src/little_crew/runtime/lipsync.py`。
本脚本只是**命令行入口**（方便单独跑一镜验证），不重复实现上传/轮询逻辑。

用法（Windows PowerShell）：
    $env:DASHSCOPE_API_KEY = "sk-..."      # 或写进 config/.env
    python tools/dashscope_lipsync.py `
        --image research/episodes/最后一班/lipsync-test/face.png `
        --audio research/episodes/最后一班/lipsync-test/line.wav `
        --out   research/episodes/最后一班/lipsync-test/out-aliyun

平台内用法：`little-crew episode --lipsync ...` 或
`python -m little_crew.runtime.episode_demo --lipsync ...`
（会把片段替换进成片，并写 `delivery/lipsync.json`）

计费：480P ¥0.5/秒、720P ¥0.9/秒；只按成功生成的秒数计。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    ap = argparse.ArgumentParser(description="DashScope wan2.2-s2v 单镜口型验证")
    ap.add_argument("--image", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default=str(ROOT / "outputs" / "lipsync-test"))
    ap.add_argument("--resolution", default="480P", choices=("480P", "720P"))
    ap.add_argument("--style", default="", choices=("", "speech", "sing", "perform"))
    ap.add_argument("--skip-detect", action="store_true",
                    help="跳过图片合规检测（不推荐；官方流程要求先检测）")
    ap.add_argument("--timeout", type=float, default=1200)
    ap.add_argument("--poll", type=float, default=15.0)
    args = ap.parse_args()

    from little_crew.runtime import lipsync as LS

    ok, why = LS.available()
    if not ok:
        print(f"[X] {why}", file=sys.stderr)
        return 2
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    img, aud = pathlib.Path(args.image), pathlib.Path(args.audio)
    print(f"  图片 {img} · 音频 {aud}")
    print(f"  档位 {args.resolution} · 单价 ¥{LS.PRICE_CNY_PER_SEC[args.resolution]}/秒")
    rep = LS.synth(img, aud, out_path=out_dir / "lipsync.mp4",
                   resolution=args.resolution, style=args.style,
                   do_detect=not args.skip_detect,
                   timeout_s=args.timeout, poll_s=args.poll,
                   on_progress=lambda st, status: print(f"  轮询 HTTP {st} · {status}"))
    (out_dir / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not rep.get("ok"):
        print(f"[X] 失败：{rep.get('error')}（原始响应见 report.json）", file=sys.stderr)
        return 1
    print(f"[v] 完成：{rep.get('out')}（{int(rep.get('bytes') or 0)//1024} KB）")
    print(f"    时长 {rep.get('usage', {}).get('duration')}s · "
          f"费用 ¥{float(rep.get('costCny') or 0):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
