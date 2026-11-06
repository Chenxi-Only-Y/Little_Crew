"""`camera_operator` 的接口层：**把"一镜"变成一段可播放的 video**（2026-11-06）。

## 为什么先做接口

名册里 `camera_operator` 的产物是 `shot.video`，而我们现在整集是**静帧 + 缓推**，
`shot.video` 根本不存在 —— 这个职位**没有输入可接**。

先定接口的好处：等云视频 API（Seedance 等）接上时，**链路不用改**，
只需在 `render()` 里换一个 backend。

## 现在能跑的后端

| backend | 说明 | 依赖 |
|---|---|---|
| **`kenburns`** | 静帧缓推成 mp4（**就是现在成片的做法，但按镜头切好**） | FFmpeg |
| `cloud` | 预留给云视频 API（Seedance / 可灵 / 即梦） | 待接 |

## 接口约定（换后端必须守住）

1 **输出时长 = 镜头时长**（`durationSec`），误差 < 0.1s
2 **分辨率与静帧一致**（不做二次缩放）
3 **失败要报**：返回 `ok=False` + `error`，不许产出 0 字节文件当成功
4 **产物可哈希**：返回 sha256，进证据链
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

#: 目前可用的后端（顺序即优先）
BACKENDS = ("kenburns", "cloud")

#: 云后端需要的环境变量名（留空 = 未配置）
CLOUD_ENV = "LITTLE_CREW_VIDEO_ENDPOINT"


def available_backends() -> list[str]:
    """真正能用的后端。`cloud` 只有在配置了端点时才算可用。"""
    import os

    out = ["kenburns"]
    if os.environ.get(CLOUD_ENV):
        out.append("cloud")
    return out


def render(image, out_path, *, duration: float, size=(768, 1024), fps: int = 24,
           backend: str = "kenburns", ffmpeg: str = "ffmpeg",
           zoom_max: float = 1.12, timeout: float = 300.0) -> dict:
    """把一张静帧渲成一段 mp4。返回 `{ok, seconds, bytes, sha256, backend, error}`。"""
    if backend == "cloud":
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": "cloud",
                "error": f"cloud 后端未接入（等视频 API）；配置 {CLOUD_ENV} 后可扩展"}
    if backend != "kenburns":
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": str(backend), "error": f"未知后端 {backend!r}"}
    img = Path(str(image))
    if not img.is_file():
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": backend, "error": f"静帧不存在：{img.name}"}
    dur = float(duration or 0)
    if dur <= 0:
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": backend, "error": "durationSec 必须 > 0"}

    out = Path(str(out_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    W, H = int(size[0]), int(size[1])
    frames = max(1, int(round(dur * fps)))
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
          f"zoompan=z='min(zoom+0.0009,{zoom_max})':d={frames}:"
          f"s={W}x{H}:fps={fps},format=yuv420p")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-loop", "1", "-i", str(img), "-t", f"{dur:.3f}", "-vf", vf,
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           str(out)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": backend, "error": f"{type(e).__name__}: {e}"}
    if p.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = [x for x in (p.stderr or "").splitlines() if x.strip()]
        return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
                "backend": backend,
                "error": (tail[-1] if tail else f"exit {p.returncode}")[:200]}
    data = out.read_bytes()
    return {"ok": True, "seconds": _seconds(out, ffmpeg), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "backend": backend, "error": ""}


def render_all(shots, images, out_dir, **kw) -> dict:
    """逐镜出片。`shots`: 有 `shot_id` / `duration_sec` 的对象序列。

    某一镜失败**不中断整批** —— 返回里逐条记，上层决定是"补渲"还是"降级"。
    """
    import tempfile

    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    clips, failed = [], []
    for i, s in enumerate(shots or ()):
        sid = str(getattr(s, "shot_id", f"shot{i}"))
        dur = float(getattr(s, "duration_sec", 0) or 0)
        img = (images or [])
        src = img[i] if i < len(img) else (img[0] if img else "")
        path = out / f"{sid}.mp4"
        r = render(src, path, duration=dur, **kw)
        row = {"shotId": sid, **r, "path": str(path) if r["ok"] else ""}
        if r["ok"]:
            clips.append(row)
        else:
            failed.append(row)
    total = round(sum(c["seconds"] for c in clips), 2)
    return {"kind": "shot.video", "clips": clips, "failed": failed,
            "totalSeconds": total, "count": len(clips),
            "failureRate": round(len(failed) / len(shots), 3) if shots else 0.0}


def _seconds(path: Path, ffmpeg: str) -> float:
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
    except Exception:                                         # noqa: BLE001
        return 0.0
    for line in (p.stderr or "").splitlines():
        if "Duration:" in line:
            try:
                h, m, sec = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return round(int(h) * 3600 + int(m) * 60 + float(sec), 3)
            except (IndexError, ValueError):
                return 0.0
    return 0.0
