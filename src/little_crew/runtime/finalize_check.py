"""成片合成自检 —— 用 ffmpeg 现造素材，不依赖项目产物。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.finalize import build

_r = []
FF = r"D:\AI\FFmpeg\bin\ffmpeg.exe"


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Sh:
    def __init__(self, d):
        self.duration_sec = d


def _img(path: Path, color: str, size="768x1024") -> str:
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s={size}:d=1",
                    "-frames:v", "1", str(path)], check=False)
    return str(path)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("成片合成（静帧 + 缓推 + 音轨 + 字幕）—— 自检")
    print("=" * 66)
    if not Path(FF).is_file():
        print("  跳过：本机没有 ffmpeg"); return 0

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        a, b = _img(d / "a.png", "orange"), _img(d / "b.png", "navy")

        print(); print("A. 正常合成")
        r = build([a, b], [_Sh(2.0), _Sh(2.0)], str(d / "out.mp4"),
                  ffmpeg=FF, workdir=str(d / "wd"))
        check("A1 ★★ 合成成功", r["ok"], str(r)[:100])
        check("A2 ★★★ 成片时长 = 计划时长（画面不被砍）",
              abs(r["videoSeconds"] - r["plannedSeconds"]) < 0.15,
              f"{r['videoSeconds']} vs {r['plannedSeconds']}")
        check("A3 ★ 段数 = 镜头数", r["segments"] == 2, str(r["segments"]))
        check("A4 ★★ 没有音轨时 hasAudio=False", r["hasAudio"] is False, "")

        print(); print("B. 音轨短于画面 -> 补静音（不砍画面）")
        wav = d / "a.wav"
        subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                        "-ar", "48000", str(wav)], check=False)
        r2 = build([a, b], [_Sh(2.0), _Sh(2.0)], str(d / "out2.mp4"),
                   audio=str(wav), ffmpeg=FF, workdir=str(d / "wd2"))
        check("B1 ★★★ 画面仍是 4 秒（没有被 -shortest 砍）",
              abs(r2["videoSeconds"] - 4.0) < 0.15, str(r2["videoSeconds"]))
        check("B2 ★★★ 报告写明「音轨补了静音」",
              r2.get("audioPadded", 0) > 2.0, str(r2.get("audioPadded")))
        check("B3 ★★ 画面未被截短", r2.get("trimmedByAudio") is False, "")

        print(); print("C. 缺素材要说清")
        r3 = build([], [_Sh(2.0)], str(d / "x.mp4"), ffmpeg=FF, workdir=str(d / "wd3"))
        check("C1 ★★★ 没有静帧 -> 明确失败", r3["ok"] is False and "no-images" in r3["error"], r3["error"])
        r4 = build([a], [], str(d / "y.mp4"), ffmpeg=FF, workdir=str(d / "wd4"))
        check("C2 ★★ 没有镜头时长 -> 明确失败", r4["ok"] is False and "no-shots" in r4["error"], r4["error"])

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
