"""`composer` 音乐床自检：**素材优先、生成兜底、来源必须标注**。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from little_crew.runtime.music_bed import (
    EXTS, find, generate_bed, music_dir, pick, resolve,
)

_r = []
FF = r"D:\AI\FFmpeg\bin\ffmpeg.exe"


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("音乐床（composer）—— 自检")
    print("=" * 66)

    print(); print("A. 素材发现（确定性）")
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        check("A1 空目录 -> 空列表", find(d) == [], "")
        for name in ("b_rain.wav", "a_calm.wav", "note.txt", "c.mp3"):
            (d / name).write_bytes(b"x")
        got = [Path(x).name for x in find(d)]
        check("A2 ★★ 按文件名排序（确定性）", got == ["a_calm.wav", "b_rain.wav", "c.mp3"], str(got))
        check("A3 ★ 只认音频后缀", "note.txt" not in got and ".txt" not in EXTS, str(got))
        check("A4 ★★ 按情绪关键词挑", Path(pick(find(d), "rain")).name == "b_rain.wav",
              pick(find(d), "rain"))
        check("A5 ★ 关键词没有就取第一首",
              Path(pick(find(d), "zzz")).name == "a_calm.wav", "")
        check("A6 空列表 -> 空串", pick([], "x") == "", "")

    print(); print("B. 生成兜底")
    if not Path(FF).is_file():
        print("  跳过 B/C：本机没有 ffmpeg")
    else:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            r = generate_bed(3.0, d / "bed.wav", ffmpeg=FF)
            check("B1 ★★ 能生成", r["ok"], str(r.get("error"))[:80])
            check("B2 ★★★ 来源标注为 generated（不是「配乐」）",
                  r["source"] == "generated:ffmpeg-sine", r["source"])
            check("B3 ★ 文件非空", (d / "bed.wav").stat().st_size > 1000, "")
            check("B4 ★★ 时长 0 或负 -> 明确失败",
                  generate_bed(0, d / "x.wav", ffmpeg=FF)["ok"] is False, "")

            print(); print("C. resolve：素材优先于生成")
            r2 = resolve(3.0, d, ffmpeg=FF)
            check("C1 ★★★ 没有素材 -> 生成并标注 source",
                  r2["ok"] and r2["source"].startswith("generated:"), r2["source"])
            check("C2 ★★ 说明里写清「这不是配乐」", "不是配乐" in (r2.get("note") or ""),
                  (r2.get("note") or "")[:40])
            # 放一首"素材"再解析
            subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "sine=frequency=220:duration=2",
                            str(d / "a_calm.wav")], check=False)
            r3 = resolve(3.0, d / "out", directory=d, ffmpeg=FF)
            check("C3 ★★★ 有素材时优先用素材（不再生成）",
                  r3["source"].startswith("asset:"), r3["source"])
            check("C4 ★★ 素材路径就是那首",
                  Path(r3["path"]).name == "a_calm.wav", r3["path"])

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
