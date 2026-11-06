"""`camera_operator` 接口自检：契约（时长/失败语义/可哈希）+ 后端可换。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.video_operator import (
    available_backends, render, render_all,
)

_r = []
FF = r"D:\AI\FFmpeg\bin\ffmpeg.exe"


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


class _Shot:
    def __init__(self, sid, dur):
        self.shot_id, self.duration_sec = sid, dur


def _img(path: Path, color="teal") -> str:
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={color}:s=768x1024:d=1",
                    "-frames:v", "1", str(path)], check=False)
    return str(path)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("camera_operator（shot.video）—— 接口自检")
    print("=" * 66)
    if not Path(FF).is_file():
        print("  跳过：本机没有 ffmpeg"); return 0

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        img = _img(d / "a.png")

        print(); print("A. 契约")
        r = render(img, d / "c1.mp4", duration=2.0, ffmpeg=FF)
        check("A1 ★★ 出一条视频", r["ok"], str(r["error"])[:60])
        check("A2 ★★★ 时长 = 镜头时长（误差 <0.1s）",
              abs(r["seconds"] - 2.0) < 0.1, str(r["seconds"]))
        check("A3 ★★ 产物非空且有 sha256", r["bytes"] > 0 and len(r["sha256"]) == 16, "")
        check("A4 ★ 报出用的是哪个后端", r["backend"] == "kenburns", r["backend"])

        print(); print("B. 失败语义（不许产出 0 字节当成功）")
        r2 = render(d / "nope.png", d / "c2.mp4", duration=2.0, ffmpeg=FF)
        check("B1 ★★★ 静帧不存在 -> ok=False", r2["ok"] is False and "不存在" in r2["error"], r2["error"])
        r3 = render(img, d / "c3.mp4", duration=0, ffmpeg=FF)
        check("B2 ★★ 时长 0 -> ok=False", r3["ok"] is False, r3["error"])
        r4 = render(img, d / "c4.mp4", duration=2.0, backend="cloud", ffmpeg=FF)
        check("B3 ★★★ cloud 未接入 -> 明确说「未接入」，不是静默失败",
              r4["ok"] is False and "未接入" in r4["error"], r4["error"])
        check("B4 ★★ 未接入时不会留下 0 字节文件", not (d / "c4.mp4").exists(), "")

        print(); print("C. 逐镜批量")
        shots = [_Shot("S1", 1.0), _Shot("S2", 2.0)]
        imgs = [_img(d / "b1.png", "navy"), _img(d / "b2.png", "olive")]
        rep = render_all(shots, imgs, d / "clips", ffmpeg=FF)
        check("C1 ★★ 两镜都出片", rep["count"] == 2 and not rep["failed"], str(rep["failed"])[:80])
        check("C2 ★★ 总时长 = 各镜之和（3s）", abs(rep["totalSeconds"] - 3.0) < 0.2, str(rep["totalSeconds"]))
        # 第 1 镜好、第 2 镜静帧缺失 -> 应出 1 条、失败 1 条（不整批垮）
        _mixed = render_all(shots, [imgs[0], str(d / "missing.png")],
                            d / "clips2", ffmpeg=FF)
        check("C3 ★★★ 失败不中断整批（一镜坏、另一镜仍出）",
              _mixed["count"] == 1 and len(_mixed["failed"]) == 1,
              f"count={_mixed['count']} failed={len(_mixed['failed'])}")
        check("C4 ★ 后端列表里有 kenburns", "kenburns" in available_backends(), str(available_backends()))

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
