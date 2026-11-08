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
BACKENDS = ("kenburns", "h3", "cloud")

#: 本机 H3 图生视频服务的默认地址（`local-ai/media`，端口 8100）
H3_URL_ENV = "LITTLE_CREW_H3_URL"
H3_DEFAULT = "http://127.0.0.1:8100"
#: H3 单镜最长秒数（**实测 12s 必 OOM**，见 local-gen 技能文档）
H3_MAX_SECONDS = 10

#: 云后端需要的环境变量名（留空 = 未配置）
CLOUD_ENV = "LITTLE_CREW_VIDEO_ENDPOINT"


def available_backends() -> list[str]:
    """真正能用的后端。`cloud` 只有在配置了端点时才算可用。"""
    import os

    out = ["kenburns"]
    if _h3_ready():
        out.append("h3")
    if os.environ.get(CLOUD_ENV):
        out.append("cloud")
    return out


def _h3_url() -> str:
    import os

    return (os.environ.get(H3_URL_ENV) or H3_DEFAULT).rstrip("/")


def _h3_ready(timeout: float = 1.5) -> bool:
    """服务在不在（GET /health）。**不抛异常**。"""
    import json as _json
    import urllib.request

    try:
        with urllib.request.urlopen(f"{_h3_url()}/health", timeout=timeout) as r:
            _json.loads(r.read().decode("utf-8", "replace"))
        return True
    except Exception:                                         # noqa: BLE001
        return False


def render(image, out_path, *, duration: float, size=(768, 1024), fps: int = 24,
           backend: str = "kenburns", ffmpeg: str = "ffmpeg", prompt: str = "",
           h3_steps: int = 8,
           zoom_max: float = 1.12, timeout: float = 300.0) -> dict:
    """把一张静帧渲成一段 mp4。返回 `{ok, seconds, bytes, sha256, backend, error}`。"""
    if backend == "h3":
        return _render_h3(image, out_path, duration=duration, fps=fps,
                          prompt=prompt)
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


def _render_h3(image, out_path, *, duration: float, fps: int = 24,
               prompt: str = "", steps: int = 8, timeout: float = 1800.0) -> dict:
    """本机 H3 图生视频（**真运动**，不是缓推）。

    为什么值得接：缓推只让「镜头」动，H3 让**人物动**（呼吸、抬手、转头）。

    三条实测约束（来自 local-gen 技能文档，别再试）：
      1 单镜 **≤10 秒**，12s/15s 在任何分辨率都 OOM
      2 尺寸必须是 **32 的倍数**（这里固定 480×640 = 3:4，与我们静帧同比例）
      3 每步约 54 秒 —— 8 步 ≈ 7 分钟/镜，**不要用在整集**
    """
    import base64
    import json as _json
    import time
    import urllib.request
    from pathlib import Path

    img = Path(str(image))
    if not img.is_file():
        return _err(f"静帧不存在：{img.name}")
    dur = float(duration or 0)
    if dur <= 0:
        return _err("duration 必须 > 0")
    if dur > H3_MAX_SECONDS:
        return _err(f"H3 单镜最多 {H3_MAX_SECONDS}s（实测 12s 必 OOM），收到 {dur}s")
    if not _h3_ready():
        return _err(f"H3 服务不可达（{_h3_url()}）；先启动 local-ai/media 服务")

    body = _json.dumps({
        "prompt": str(prompt or "镜头缓慢推近，人物自然呼吸"),
        "image_base64": base64.b64encode(img.read_bytes()).decode("ascii"),
        "width": 480, "height": 640, "seconds": dur,
        "num_inference_steps": int(steps), "fps": int(fps),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_h3_url()}/v1/videos/generations", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            job = _json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:                                    # noqa: BLE001
        return _err(f"提交失败：{type(e).__name__}: {e}")

    jid = str(job.get("id") or job.get("job_id") or "")
    if not jid:
        return _err(f"提交后拿不到任务 id：{str(job)[:120]}")
    deadline = time.time() + timeout
    url = ""
    while time.time() < deadline:
        time.sleep(5)
        try:
            with urllib.request.urlopen(
                    f"{_h3_url()}/v1/videos/generations/{jid}", timeout=60) as r:
                st = _json.loads(r.read().decode("utf-8", "replace"))
        except Exception:                                     # noqa: BLE001
            continue
        status = str(st.get("status") or "").lower()
        if status in ("succeeded", "success", "done", "completed"):
            data = st.get("data") or st.get("output") or []
            url = (data[0].get("url") if isinstance(data, list) and data else "") \
                or str(st.get("url") or "")
            break
        if status in ("failed", "error", "cancelled"):
            return _err(f"任务失败：{str(st.get('error') or st)[:160]}")
    if not url:
        return _err(f"H3 超时（{timeout:.0f}s）仍有没结果")

    out = Path(str(out_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            out.write_bytes(r.read())
    except Exception as e:                                    # noqa: BLE001
        return _err(f"下载产物失败：{type(e).__name__}: {e}")
    if not out.is_file() or out.stat().st_size == 0:
        return _err("下载下来是空文件")
    data = out.read_bytes()
    return {"ok": True, "seconds": round(dur, 3), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16],
            "backend": "h3", "error": "", "jobId": jid}


def _err(msg: str) -> dict:
    return {"ok": False, "seconds": 0.0, "bytes": 0, "sha256": "",
            "backend": "h3", "error": str(msg)[:220]}
