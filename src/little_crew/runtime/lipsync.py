"""口型视频通道（DashScope `wan2.2-s2v`，2026-11-15）。

## 这是什么

**视频通道能力**：把「一镜静帧 + 该镜对白音轨」变成"说话视频"，
再让成片合成用这些片段**替换对应镜头的静帧 Ken Burns**。

## 为什么放在 runtime/

- 它是**执行能力**（调远端模型、落盘产物），不是判定 —— 与 `cloud_render` 同类
- 判定仍归门禁/审片（本模块**不做质量判断**，只如实记录 ok/失败/费用）

## 实测口径（2026-11-15）

| 项 | 结果 |
|---|---|
模型 | `wan2.2-s2v`（Apache-2.0 开放权重；百炼托管） |
输入 | 正脸半身图 + 中文对白 4.96s |
输出 | 512×640 · 16fps · 4.75s（含音轨） |
计费 | **480P ¥0.5/秒**，720P ¥0.9/秒；只按成功生成的秒数计 |
耗时 | 约 60s/镜 |
局限 | 动作幅度小、面部略磨平、16fps 偏低 |

## 关键实现约束（踩过的坑）

1 **文件与模型绑定**：百炼临时存储要求上传时指定模型，且与调用模型一致
   → 检测图用 `wan2.2-s2v-detect` 上传，生成素材用 `wan2.2-s2v` 上传，**分别传**
2 **`oss://` URL 必须带头** `X-DashScope-OssResourceResolve: enable`，否则解析失败
3 **音频 <20s、<15MB**；超长要切段生成再拼（本模块**不自动切**，超限直接报错）
4 **`client` 可注入**：测试用假客户端，绝不联网（`tools/` 与单测都靠这个）
"""

from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import sys
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
DETECT_MODEL = "wan2.2-s2v-detect"
GEN_MODEL = "wan2.2-s2v"
#: 官方价（元/秒）：480P 0.5，720P 0.9；只按成功生成的视频秒数计
PRICE_CNY_PER_SEC: dict[str, float] = {"480P": 0.5, "720P": 0.9}
RESOLUTIONS = tuple(PRICE_CNY_PER_SEC)
#: 官方限制：音频 <20s、<15MB
MAX_AUDIO_SECONDS = 20.0
MAX_AUDIO_BYTES = 15 * 1024 * 1024


def _key() -> str:
    # ⚠️ 不能只读 os.environ：密钥已迁到项目外秘密文件（D:\AI\ds.8\api key）。
    # 统一走 config.env_value —— 它按"进程环境 > config/.env > 秘密文件"解析。
    from little_crew.config import env_value
    k = str(env_value("DASHSCOPE_API_KEY", "") or "").strip()
    if k:
        return k
    # 项目内 config/.env（gitignored）
    try:
        root = pathlib.Path(__file__).resolve().parents[3]
        envf = root / "config" / ".env"
        if envf.is_file():
            for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s.startswith("DASHSCOPE_API_KEY="):
                    return s.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:                                         # noqa: BLE001
        pass
    return ""


def available() -> tuple[bool, str]:
    """口型通道是否可用（key 是否配置）。"""
    if _key():
        return True, "DASHSCOPE_API_KEY 已配置"
    return False, "缺少 DASHSCOPE_API_KEY（环境变量或 config/.env）"


def estimate_cost_cny(seconds: float, resolution: str = "480P") -> float:
    unit = PRICE_CNY_PER_SEC.get(str(resolution).upper(), 0.0)
    return round(max(0.0, float(seconds or 0.0)) * unit, 3)


def wav_seconds(path: str | pathlib.Path) -> float:
    """wav 时长（秒）；非 wav 或读取失败返回 0。"""
    try:
        import wave

        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() / float(w.getframerate() or 1), 2)
    except Exception:                                         # noqa: BLE001
        return 0.0


def _req(url: str, *, key: str, method: str = "GET", body: bytes | None = None,
         content_type: str = "application/json", extra: dict | None = None,
         timeout: float = 120) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "little-crew-lipsync/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:                                     # noqa: BLE001
            return e.code, {"error": raw[:600]}


class DashScopeClient:
    """百炼 HTTP 客户端（上传/检测/建任务/轮询/下载）。**可被测试替换。**"""

    def __init__(self, key: str = "", base: str = DEFAULT_BASE,
                 *, timeout_s: float = 600) -> None:
        self.key = key or _key()
        self.base = str(base or DEFAULT_BASE).rstrip("/")
        self.timeout_s = float(timeout_s)

    # ---- 上传 ---------------------------------------------------------
    def _policy(self, model: str) -> dict:
        url = f"{self.base}/uploads?" + urllib.parse.urlencode(
            {"action": "getPolicy", "model": model})
        st, obj = _req(url, key=self.key, timeout=60)
        data = obj.get("data") or {}
        if st != 200 or not data.get("upload_host"):
            raise RuntimeError(f"取上传凭证失败 HTTP {st}: "
                               f"{json.dumps(obj, ensure_ascii=False)[:300]}")
        return data

    def upload_file(self, model: str, path: str | pathlib.Path) -> str:
        """上传到百炼临时存储 → `oss://...`（文件与模型绑定，48h 有效）。"""
        p = pathlib.Path(str(path))
        if not p.is_file():
            raise FileNotFoundError(p)
        if p.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError(f"文件过大（>{MAX_AUDIO_BYTES//1024//1024}MB）：{p.name}")
        pol = self._policy(model)
        name = p.name
        key_path = f"{pol['upload_dir']}/{name}"
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        fields = {
            "OSSAccessKeyId": pol["oss_access_key_id"],
            "Signature": pol["signature"],
            "policy": pol["policy"],
            "x-oss-object-acl": pol["x_oss_object_acl"],
            "x-oss-forbid-overwrite": pol["x_oss_forbid_overwrite"],
            "key": key_path,
            "success_action_status": "200",
        }
        boundary = "----lc" + uuid.uuid4().hex
        body = b""
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                     f'name="{k}"\r\n\r\n{v}\r\n').encode("utf-8")
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                 f'filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8")
        body += p.read_bytes() + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            pol["upload_host"], data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": "little-crew-lipsync/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                if r.status not in (200, 201, 204):
                    raise RuntimeError(f"上传失败 HTTP {r.status}")
        except urllib.error.HTTPError as e:
            raw = (e.read() or b"").decode("utf-8", errors="replace")
            raise RuntimeError(f"上传失败 HTTP {e.code}: {raw[:300]}") from e
        return f"oss://{key_path}"

    # ---- 检测 / 建任务 / 轮询 ------------------------------------------
    _OSS = {"X-DashScope-OssResourceResolve": "enable"}

    def detect(self, image_url: str) -> dict:
        body = json.dumps({"model": DETECT_MODEL,
                           "input": {"image_url": image_url}}).encode("utf-8")
        st, obj = _req(f"{self.base}/services/aigc/image2video/face-detect",
                       key=self.key, method="POST", body=body, extra=dict(self._OSS))
        obj["_http"] = st
        return obj

    def create_task(self, image_url: str, audio_url: str, *,
                    resolution: str = "480P", style: str = "") -> dict:
        params: dict[str, Any] = {"resolution": str(resolution).upper()}
        if style:
            params["style"] = style
        payload = {"model": GEN_MODEL,
                   "input": {"image_url": image_url, "audio_url": audio_url},
                   "parameters": params}
        st, obj = _req(f"{self.base}/services/aigc/image2video/video-synthesis",
                       key=self.key, method="POST",
                       body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       extra={"X-DashScope-Async": "enable", **self._OSS})
        obj["_http"] = st
        return obj

    def poll(self, task_id: str, *, timeout_s: float = 1200,
             interval_s: float = 15.0, on_progress: Any = None) -> dict:
        t0 = time.time()
        last: dict = {}
        while time.time() - t0 < timeout_s:
            time.sleep(max(2.0, float(interval_s)))
            st, obj = _req(f"{self.base}/tasks/{task_id}", key=self.key, timeout=60)
            last = obj
            status = str((obj.get("output") or {}).get("task_status") or "").upper()
            if on_progress:
                try:
                    on_progress(st, status)
                except Exception:                             # noqa: BLE001
                    pass
            if status in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
                return obj
        return last

    def download(self, url: str, dst: str | pathlib.Path) -> dict:
        out = pathlib.Path(str(dst))
        out.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={
            "User-Agent": "little-crew-lipsync/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r, out.open("wb") as f:
            f.write(r.read())
        return {"ok": out.is_file(), "out": str(out),
                "bytes": out.stat().st_size if out.is_file() else 0}


def synth(image: str | pathlib.Path, audio: str | pathlib.Path, *,
          out_path: str | pathlib.Path | None = None,
          resolution: str = "480P", style: str = "",
          client: Any = None, do_detect: bool = True,
          timeout_s: float = 1200, poll_s: float = 15.0,
          on_progress: Any = None) -> dict:
    """生成一镜"说话视频"。**不抛**：失败一律返回 `{ok: False, error, detail}`。

    `client` 可注入（测试用），需实现：
    `upload_file(model, path) -> str` / `detect(url) -> dict` /
    `create_task(img, aud, resolution=, style=) -> dict` /
    `poll(task_id, timeout_s=, interval_s=, on_progress=) -> dict` /
    `download(url, dst) -> dict`
    """
    res = str(resolution or "480P").upper()
    rep: dict = {"kind": "lipsync.clip", "model": GEN_MODEL,
                 "provider": "dashscope", "resolution": res,
                 "image": str(image), "audio": str(audio),
                 "style": str(style or "")}
    if res not in RESOLUTIONS:
        return {**rep, "ok": False, "error": f"resolution 只能是 {RESOLUTIONS}"}
    img, aud = pathlib.Path(str(image)), pathlib.Path(str(audio))
    for p, what in ((img, "图片"), (aud, "音频")):
        if not p.is_file():
            return {**rep, "ok": False, "error": f"{what}不存在：{p}"}
    if aud.stat().st_size > MAX_AUDIO_BYTES:
        return {**rep, "ok": False,
                "error": f"音频超过 {MAX_AUDIO_BYTES//1024//1024}MB"}
    audio_sec = wav_seconds(aud)
    if audio_sec and audio_sec >= MAX_AUDIO_SECONDS:
        return {**rep, "ok": False, "audioSeconds": audio_sec,
                "error": f"音频 {audio_sec:.1f}s ≥ 官方上限 {MAX_AUDIO_SECONDS:.0f}s（需切段）"}
    rep["audioSeconds"] = audio_sec
    rep["estCostCny"] = estimate_cost_cny(audio_sec or 0.0, res)

    cli = client or DashScopeClient()
    dst = pathlib.Path(str(out_path)) if out_path else (
        img.parent / f"{img.stem}_lipsync.mp4")
    t0 = time.time()
    try:
        # 1) 检测图（模型绑定检测模型）
        if do_detect:
            det_url = cli.upload_file(DETECT_MODEL, img)
            det = cli.detect(det_url)
            rep["detect"] = {"_http": det.get("_http"),
                             "output": det.get("output")}
            if not bool((det.get("output") or {}).get("check_pass")):
                return {**rep, "ok": False,
                        "error": "图片未通过 wan2.2-s2v 规范检测", "detail": det}
        # 2) 上传生成素材（模型绑定生成模型）
        img_url = cli.upload_file(GEN_MODEL, img)
        aud_url = cli.upload_file(GEN_MODEL, aud)
        rep["imageUrl"], rep["audioUrl"] = img_url, aud_url
        # 3) 建任务
        task = cli.create_task(img_url, aud_url, resolution=res, style=style)
        rep["createTaskHttp"] = task.get("_http")
        tid = str(((task.get("output") or {}).get("task_id")) or "")
        rep["taskId"] = tid
        if not tid:
            return {**rep, "ok": False, "error": "建任务失败（无 task_id）",
                    "detail": task}
        # 4) 轮询
        done = cli.poll(tid, timeout_s=timeout_s, interval_s=poll_s,
                        on_progress=on_progress)
        status = str((done.get("output") or {}).get("task_status") or "").upper()
        rep["taskStatus"] = status
        if status != "SUCCEEDED":
            return {**rep, "ok": False,
                    "error": f"任务未成功：{status or '未知'}",
                    "detail": done}
        video_url = str(((done.get("output") or {}).get("results") or {})
                        .get("video_url") or "")
        if not video_url:
            return {**rep, "ok": False, "error": "结果里没有 video_url",
                    "detail": done}
        # 5) 下载
        dl = cli.download(video_url, dst)
        if not dl.get("ok"):
            return {**rep, "ok": False, "error": "下载失败", "detail": dl}
        usage = done.get("usage") or {}
        secs = float(usage.get("duration") or audio_sec or 0.0)
        rep.update({"ok": True, "out": str(dst), "bytes": dl.get("bytes"),
                    "usage": usage, "videoUrl": video_url,
                    "seconds": round(time.time() - t0, 1),
                    "costCny": estimate_cost_cny(secs, res)})
        return rep
    except Exception as exc:                                  # noqa: BLE001
        return {**rep, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}

# ==========================================================================
# 时长对齐（2026-11-15 实测抓到）
# ==========================================================================
#
# 口型片段时长由**音频**决定（如 1.5s），而镜头时长是**分镜计划**（如 6s）。
# 直接拿去合成会让整集变短：B2 实测 4×6s 的成片变成 19.5s，
# 之后的镜头全部提前、音轨还按 24s 计划 -> **音画错位**。
#
# 对齐规则（确定性）：
#   · 片段短于镜头 -> **尾帧冻结**补足（tpad clone），嘴停住但时间轴不动
#   · 片段长于镜头 -> 截断到镜头时长（宁可切掉尾巴，也不让整集错位）
#   · 差值 < 0.08s -> 不动（避免无意义重编码）

FIT_EPSILON = 0.08


def _ffmpeg_path() -> str:
    try:
        from little_crew.config import env_value

        return str(env_value("FFMPEG", "") or "") or "ffmpeg"
    except Exception:                                         # noqa: BLE001
        return "ffmpeg"


def probe_seconds(path) -> float:
    """用 ffprobe 读时长（读不到返回 0）。"""
    import shutil
    import subprocess

    ff = _ffmpeg_path()
    probe = str(Path(ff).with_name("ffprobe" + Path(ff).suffix)) if Path(ff).is_file() else "ffprobe"
    if not (Path(probe).is_file() or shutil.which(probe)):
        probe = "ffprobe"
    try:
        p = subprocess.run([probe, "-v", "error", "-show_entries",
                            "format=duration", "-of",
                            "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float((p.stdout or "0").strip() or 0.0)
    except Exception:                                         # noqa: BLE001
        return 0.0


def fit_duration(video, seconds: float, *, out: str = "") -> dict:
    """把视频对齐到 `seconds`。短补尾帧、长截尾；返回实际时长与方式。"""
    import subprocess

    src = Path(str(video))
    dst = Path(str(out)) if out else src
    target = max(0.1, float(seconds or 0))
    before = probe_seconds(src)
    if not src.is_file():
        return {"ok": False, "before": 0.0, "after": 0.0,
                "error": "文件不存在"}
    if abs(before - target) < FIT_EPSILON:
        return {"ok": True, "before": before, "after": before,
                "method": "noop"}
    ff = _ffmpeg_path()
    tmp = dst.with_name(dst.stem + ".fit.mp4")
    if before < target:
        pad = target - before + 0.2
        vf = f"tpad=stop_mode=clone:stop_duration={pad:.3f}"
        method = "pad_freeze"
    else:
        vf = None
        method = "trim"
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-t", f"{target:.3f}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=300, encoding="utf-8", errors="replace")
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "before": before, "after": before,
                "error": f"{type(e).__name__}: {e}"}
    if r.returncode != 0 or not tmp.is_file():
        try:
            tmp.unlink(missing_ok=True)
        except Exception:                                     # noqa: BLE001
            pass
        return {"ok": False, "before": before, "after": before,
                "method": method, "error": (r.stderr or "")[-200:]}
    try:
        if str(tmp) != str(dst):
            tmp.replace(dst)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "before": before, "after": before,
                "method": method, "error": f"替换失败:{e}"}
    after = probe_seconds(dst)
    return {"ok": True, "before": before, "after": after, "method": method}
