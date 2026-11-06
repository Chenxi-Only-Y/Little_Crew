"""成片：静帧 + 缓推 + 音轨 + 字幕 -> mp4（§3.11.1 的最后一步）。

## 这一步解决什么

在此之前，我们手里有：**渲染好的静帧**（`cache/*.png`）、**混好的音轨**（`mix.wav`）、
**字幕**（`subtitles.srt`）—— **但没有一个能播放的东西**。

## 做法（全部 ffmpeg，无可选依赖）

```
每张静帧 --zoompan--> 一段带缓推的视频
所有段   --concat--> 一条视频轨
音轨     --amix/shift--> 贴上去
字幕     --subtitles--> 烧进画面（失败就外挂，不静默）
```

## 两条纪律

1 **时长要报**：视频长度 vs 音轨长度的差必须打出来（`drift`），
   差太多说明"画面和声音不是同一集"。
2 **字幕烧不进去要说**：某些 ffmpeg 构建没有 libass，此时**外挂 srt 并标记**，
   不许假装烧好了。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

#: 缓推的最大放大倍数（1.12 ≈ 轻微推进，不晕）
ZOOM_MAX = 1.12
DEFAULT_FPS = 24


def build(images, shots, out_mp4, *, audio: str = "", srt: str = "",
          ffmpeg: str = "ffmpeg", fps: int = DEFAULT_FPS,
          size=(768, 1024), workdir=None, timeout: float = 900.0) -> dict:
    """把静帧 + 音轨合成成片。返回 `{ok, videoSeconds, audioSeconds, drift,
    subtitles: "burned"|"sidecar"|"none", error, command}`。

    `images`: 图片路径列表（按镜头顺序；少于镜头数就**循环复用**）
    `shots`: 镜头对象（读 `duration_sec`）
    """
    imgs = [str(x) for x in (images or []) if x and Path(str(x)).is_file()]
    if not imgs:
        return _fail("no-images", "没有可用的静帧 —— 成片无从谈起")
    durs = [float(getattr(s, "duration_sec", 0) or 0) for s in (shots or ())]
    durs = [d for d in durs if d > 0]
    if not durs:
        return _fail("no-shots", "没有镜头时长 —— 不知道每段多长")

    planned = round(sum(durs), 3)      # 画面总长（后面 apad 与报告都要用）
    wd = Path(str(workdir)) if workdir else Path(str(out_mp4)).parent
    wd.mkdir(parents=True, exist_ok=True)
    W, H = int(size[0]), int(size[1])

    # ① 每段：静帧 -> 指定时长（zoompan 做缓推）
    segs = []
    for i, d in enumerate(durs):
        img = imgs[i % len(imgs)]
        frames = max(1, int(round(d * fps)))
        seg = wd / f"seg_{i:03d}.mp4"
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
              f"zoompan=z='min(zoom+0.0009,{ZOOM_MAX})':d={frames}:"
              f"s={W}x{H}:fps={fps},format=yuv420p")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-loop", "1", "-i", img, "-t", f"{d:.3f}",
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", str(seg)]
        r = _run(cmd, timeout)
        if r["code"] != 0:
            return _fail("segment-failed", r["err"] or f"第 {i} 段失败")
        segs.append(seg)

    # ② 拼接
    lst = wd / "concat.txt"
    lst.write_text("".join(f"file '{Path(s).as_posix()}'\n" for s in segs),
                   encoding="utf-8")
    silent = wd / "video_silent.mp4"
    r = _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
              "-f", "concat", "-safe", "0", "-i", str(lst),
              "-c", "copy", str(silent)], timeout)
    if r["code"] != 0:
        return _fail("concat-failed", r["err"] or "拼接失败")

    # ③ 音轨 + 字幕
    has_audio = bool(audio and Path(str(audio)).is_file())
    sub_mode = "none"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(silent)]
    if has_audio:
        cmd += ["-i", str(audio)]
    vf = ""
    if srt and Path(str(srt)).is_file():
        # 先尝试烧进画面；失败再降级外挂（并如实标记）
        esc = str(Path(str(srt)).as_posix()).replace(":", r"\:")
        vf = f"subtitles='{esc}'"
        sub_mode = "burned"
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-map", "0:v:0"]
    if has_audio:
        # **不用 `-shortest`**：那会把画面砍到音轨长度（第一次合成踩到：
        # 3 镜 19s 被砍成 15.4s，报告还写着 drift +0.00）。
        # 改成"音轨不够就补静音"—— **画面是故事，不能被声音截断**。
        cmd += ["-map", "1:a:0", "-af", f"apad=whole_dur={planned:.3f}",
                "-t", f"{planned:.3f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(out_mp4)]
    r = _run(cmd, timeout)
    if r["code"] != 0 and sub_mode == "burned":
        # 烧字幕失败 -> 重试一次不烧，外挂
        sub_mode = "sidecar"
        cmd2 = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(silent)]
        if has_audio:
            cmd2 += ["-i", str(audio)]
        cmd2 += ["-map", "0:v:0"]
        if has_audio:
            cmd2 += ["-map", "1:a:0", "-af", f"apad=whole_dur={planned:.3f}",
                     "-t", f"{planned:.3f}"]
        cmd2 += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", str(out_mp4)]
        r = _run(cmd2, timeout)
    if r["code"] != 0:
        return _fail("mux-failed", r["err"] or "合成失败")

    from little_crew.runtime.tts_breeze import wav_seconds

    vs = _media_seconds(str(out_mp4), ffmpeg)
    as_ = wav_seconds(audio) if has_audio else 0.0
    # ⚠️ `-shortest` 会把画面截到音轨长度 —— 此时 `drift` 恒为 0，
    # **看起来"完美对齐"，其实画面被砍掉了**（2026-11-06 第一次合成踩到：
    # 3 镜 19s、音轨 15.4s，报告写着 drift +0.00）。
    # 所以必须同时报"原本该多长"和"被砍了多少"。
    trimmed = round(planned - vs, 3)
    audio_padded = round(max(0.0, planned - as_), 3) if has_audio else 0.0
    return {"ok": True, "error": "", "videoSeconds": vs, "audioSeconds": as_,
            "plannedSeconds": planned,
            "trimmedByAudio": bool(has_audio and trimmed > 0.05),
            "trimmedSeconds": max(0.0, trimmed),
            "audioPadded": audio_padded,
            "drift": round(vs - as_, 3) if has_audio else 0.0,
            "hasAudio": has_audio, "subtitles": sub_mode,
            "segments": len(segs), "size": Path(str(out_mp4)).stat().st_size,
            "noImagesReused": len(durs) > len(imgs)}


def _media_seconds(path: str, ffmpeg: str) -> float:
    """读媒体时长（ffmpeg -i 的 Duration 行）。读不到返回 0。"""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except Exception:                                         # noqa: BLE001
        return 0.0
    for line in (p.stderr or "").splitlines():
        if "Duration:" in line:
            try:
                hms = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = hms.split(":")
                return round(int(h) * 3600 + int(m) * 60 + float(s), 3)
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _run(cmd, timeout) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return {"code": -1, "err": f"{type(e).__name__}: {e}"}
    tail = [x for x in (p.stderr or "").splitlines() if x.strip()]
    return {"code": p.returncode, "err": (tail[-1] if tail else "")[:200]}


def _fail(kind: str, why: str) -> dict:
    return {"ok": False, "error": f"{kind}: {why}", "videoSeconds": 0.0,
            "audioSeconds": 0.0, "plannedSeconds": 0.0,
            "trimmedByAudio": False, "trimmedSeconds": 0.0,
            "drift": 0.0, "hasAudio": False,
            "subtitles": "none", "segments": 0, "size": 0}
