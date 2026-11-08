"""`composer` 的接口层：给成片准备一条**音乐床**（2026-11-06）。

## 两条路，都必须标注来源

| 来源 | 条件 | 标注 |
|---|---|---|
| **素材**（`assets/music/*`） | 目录里有音频文件 | `source: "asset:<文件名>"` |
| **程序生成**（FFmpeg 正弦垫音） | 没有素材 | `source: "generated:ffmpeg-sine"` |

## 为什么允许"生成"

实测成片 154 秒里 **68 秒只有房间底噪**（无对白、无音乐），听感很"空"。
先用**程序生成的和弦垫音**把这段填上，能立刻改善观感；
但它**不是配乐**，所以：

- 文件名固定为 `music_bed.wav`（不叫 bgm）
- 交付件里写清 `source: "generated:ffmpeg-sine"`
- **绝不宣称"我们有配乐"**

有素材时**优先用素材** —— 生成只是兜底，不是替代。

## 接口（换音源不改调用方）

`resolve(seconds=..., out_dir=...) -> {"ok", "path", "source", "seconds"}`
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: 认作音乐素材的后缀
EXTS = (".wav", ".mp3", ".ogg", ".m4a", ".flac")
#: 素材目录（可用环境变量覆盖）
MUSIC_ENV = "LITTLE_CREW_MUSIC_DIR"


def music_dir() -> Path:
    import os

    return Path(os.environ.get(MUSIC_ENV) or "assets/music")


def find(directory=None) -> list[str]:
    """列出可用音乐素材（按文件名排序，**排序是确定性的**）。"""
    d = Path(str(directory)) if directory else music_dir()
    if not d.is_dir():
        return []
    return sorted(str(p) for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in EXTS)


def pick(files, mood: str = "") -> str:
    """按情绪关键词挑一首；挑不到就取第一首（**确定性，不随机**）。"""
    items = list(files or ())
    if not items:
        return ""
    key = str(mood or "").strip()
    if key:
        for f in items:
            if key in Path(f).stem:
                return f
    return items[0]


def generate_bed(seconds: float, out_wav, *, ffmpeg: str = "ffmpeg",
                 root_hz: float = 110.0, timeout: float = 120.0) -> dict:
    """生成一条**和弦垫音**（root + 五度 + 八度，慢颤音，头尾淡入淡出）。

    这不是音乐，是"别让耳朵听见真空"的最低成本方案。
    """
    dur = float(seconds or 0)
    if dur <= 0:
        return {"ok": False, "path": "", "error": "seconds 必须 > 0",
                "source": "generated:ffmpeg-sine"}
    out = Path(str(out_wav))
    out.parent.mkdir(parents=True, exist_ok=True)
    f = root_hz
    # 三个正弦叠加：基音 + 五度(1.5f) + 八度(2f)；再乘一个 0.15Hz 的慢颤
    expr = (f"0.28*sin(2*PI*{f}*t)+0.18*sin(2*PI*{f * 1.5:.2f}*t)"
            f"+0.12*sin(2*PI*{f * 2:.2f}*t)")
    # ⚠️ 单声道输入只能给**一个**表达式：写 `exprs='a|a'` 会报
    #   "Mismatch between the specified number of channel expressions '2'
    #    and the number of expected output channels '1'"（真踩过）。
    af = (f"aeval=exprs='{expr}':c=same,"
          f"tremolo=f=0.15:d=0.25,"
          f"afade=t=in:st=0:d=2.0,"
          f"afade=t=out:st={max(0.0, dur - 2.5):.2f}:d=2.5,"
          f"volume=0.35")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-t", f"{dur:.3f}",
           "-i", "anullsrc=r=48000:cl=mono",
           "-af", af, "-ac", "1", "-ar", "48000", str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "path": "", "error": f"{type(e).__name__}: {e}",
                "source": "generated:ffmpeg-sine"}
    if p.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = [x for x in (p.stderr or "").splitlines() if x.strip()]
        return {"ok": False, "path": "",
                "error": (tail[-1] if tail else f"exit {p.returncode}")[:200],
                "source": "generated:ffmpeg-sine"}
    return {"ok": True, "path": str(out), "error": "", "seconds": round(dur, 2),
            "source": "generated:ffmpeg-sine", "rootHz": f}


def resolve(seconds: float, out_dir, *, mood: str = "",
            directory=None, ffmpeg: str = "ffmpeg") -> dict:
    """**先找素材，找不到才生成。** 返回可直接喂给混流的结构。"""
    files = find(directory)
    chosen = pick(files, mood)
    if chosen:
        return {"ok": True, "path": chosen, "source": f"asset:{Path(chosen).name}",
                "seconds": 0.0, "error": "",
                "note": "用素材；时长由混流层按成片长度裁切"}
    out = Path(str(out_dir)) / "music_bed.wav"
    r = generate_bed(seconds, out, ffmpeg=ffmpeg)
    r["note"] = ("没有音乐素材，用程序生成的和弦垫音 —— **这不是配乐**，"
                 "只是填掉无对白的空白")
    return r
