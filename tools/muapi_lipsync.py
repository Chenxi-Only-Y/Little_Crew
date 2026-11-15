#!/usr/bin/env python
"""MuAPI 单镜口型验证（2026-11-15，B 项）。

用途：把「一张正脸图 + 一段中文对白」变成说话视频，验证口型能不能看。
只做**一次**调用，用来给"口型值不值得做"一个实测答案。

用法：
    $env:MUAPI_API_KEY = "..."        # 或写进 config/.env
    python tools/muapi_lipsync.py `
        --image research/episodes/最后一班/lipsync-test/face.png `
        --audio research/episodes/最后一班/lipsync-test/line.wav `
        --out   research/episodes/最后一班/lipsync-test/out

流程（MuAPI 两条式）：
    1 POST /api/v1/upload_file        上传图/音频 -> 公网 URL
    2 POST /api/v1/{model}            提交任务 -> request_id
    3 GET  /api/v1/predictions/{id}/result   轮询 -> 产物 URL
    4 下载视频 + 写 report.json（含耗时/预估费用）

约定：
- **只用标准库**（tools/ 的规矩：不 import 本包、不引第三方）
- key 只从环境/`config/.env` 读，不打印明文
- 任何一步失败都打印**原始响应**：接口字段按 README 推断，首跑可能要按真实返回微调
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://api.muapi.ai"
ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "wan2.2-speech-to-video"
#: 官方报价（USD）：$0.04/s @480p，$0.08/s @720p
PRICE_PER_SEC = {"480p": 0.04, "720p": 0.08}


def _key() -> str:
    k = str(os.environ.get("MUAPI_API_KEY") or "").strip()
    if k:
        return k
    envf = ROOT / "config" / ".env"
    if envf.is_file():
        for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("MUAPI_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _post_json(url: str, payload: dict, key: str, timeout: float = 120) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"x-api-key": key, "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        return e.code, {"error": raw[:600]}


def _post_file(url: str, path: pathlib.Path, key: str, timeout: float = 300) -> tuple[int, dict]:
    boundary = "----lc" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = b""
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="file"; filename="{path.name}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"x-api-key": key,
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        return e.code, {"error": raw[:600]}


def _get(url: str, key: str, timeout: float = 120) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"x-api-key": key,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return r.status, json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        return e.code, {"error": raw[:600]}


def _pick_url(obj: dict) -> str:
    """从各种可能的返回形态里取 URL。"""
    for k in ("url", "file_url", "fileUrl", "uploaded_url", "path"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    for parent in ("data", "result", "output", "file"):
        sub = obj.get(parent)
        if isinstance(sub, dict):
            u = _pick_url(sub)
            if u:
                return u
    return ""


def _pick_id(obj: dict) -> str:
    for k in ("request_id", "requestId", "id", "prediction_id"):
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    for parent in ("data", "result"):
        sub = obj.get(parent)
        if isinstance(sub, dict):
            v = _pick_id(sub)
            if v:
                return v
    return ""


def _pick_video_url(obj: dict) -> str:
    """产物可能是 video/output/url 字段，或 data 里的列表。"""
    for k in ("video", "video_url", "videoUrl", "output", "url", "file_url"):
        v = obj.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]
        if isinstance(v, dict):
            u = _pick_video_url(v)
            if u:
                return u
    for parent in ("data", "result", "output", "prediction"):
        sub = obj.get(parent)
        if isinstance(sub, (dict, list)):
            if isinstance(sub, list):
                for it in sub:
                    if isinstance(it, dict):
                        u = _pick_video_url(it)
                        if u:
                            return u
            else:
                u = _pick_video_url(sub)
                if u:
                    return u
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="MuAPI 单镜口型验证")
    ap.add_argument("--image", required=True, help="正脸图（png/jpg）")
    ap.add_argument("--audio", required=True, help="对白音频（wav/mp3）")
    ap.add_argument("--out", default="outputs/lipsync-test", help="输出目录")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"MuAPI 模型 endpoint（默认 {DEFAULT_MODEL}）")
    ap.add_argument("--resolution", default="480p", choices=("480p", "720p"))
    ap.add_argument("--prompt", default="", help="可选的运镜/表情提示")
    ap.add_argument("--timeout", type=int, default=900, help="轮询最长秒数")
    ap.add_argument("--poll", type=float, default=5.0, help="轮询间隔秒")
    args = ap.parse_args()

    key = _key()
    img = pathlib.Path(args.image)
    aud = pathlib.Path(args.audio)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"kind": "lipsync.test", "model": args.model,
                    "resolution": args.resolution,
                    "image": str(img), "audio": str(aud),
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not key:
        print("[X] 没有 MUAPI_API_KEY（环境变量或 config/.env）", file=sys.stderr)
        print("    获取：https://muapi.ai/access-keys  → 写进 config/.env", file=sys.stderr)
        return 2
    for p in (img, aud):
        if not p.is_file():
            print(f"[X] 文件不存在：{p}", file=sys.stderr)
            return 2

    # 时长（用于费用估算）
    dur = 0.0
    try:
        import wave
        if aud.suffix.lower() == ".wav":
            with wave.open(str(aud), "rb") as w:
                dur = w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        dur = 0.0
    report["audioSeconds"] = round(dur, 2)
    report["estCostUsd"] = round(dur * PRICE_PER_SEC.get(args.resolution, 0.04), 4)
    print(f"  对白 {dur:.2f}s · 预估 ${report['estCostUsd']:.3f} "
          f"(≈¥{report['estCostUsd'] * 7.2:.2f})")

    # 1) 上传
    st, up_img = _post_file(f"{BASE}/api/v1/upload_file", img, key)
    print(f"  上传图片 HTTP {st}: {json.dumps(up_img, ensure_ascii=False)[:200]}")
    st2, up_aud = _post_file(f"{BASE}/api/v1/upload_file", aud, key)
    print(f"  上传音频 HTTP {st2}: {json.dumps(up_aud, ensure_ascii=False)[:200]}")
    img_url = _pick_url(up_img)
    aud_url = _pick_url(up_aud)
    if not (img_url and aud_url):
        report["error"] = "上传后没拿到 URL（见 upload 原始响应）"
        report["upload"] = {"image": up_img, "audio": up_aud}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print("[X] 上传返回里取不到 URL，已写 report.json；请对照原始响应调字段名",
              file=sys.stderr)
        return 1

    # 2) 提交
    payload = {"image_url": img_url, "audio_url": aud_url,
               "resolution": args.resolution}
    if args.prompt:
        payload["prompt"] = args.prompt
    st3, sub = _post_json(f"{BASE}/api/v1/{args.model}", payload, key)
    print(f"  提交 HTTP {st3}: {json.dumps(sub, ensure_ascii=False)[:300]}")
    rid = _pick_id(sub)
    if not rid:
        report["error"] = "提交后没拿到 request_id"
        report["submit"] = sub
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print("[X] 提交返回里取不到 request_id，已写 report.json", file=sys.stderr)
        return 1

    # 3) 轮询
    t0 = time.time()
    last: dict = {}
    video_url = ""
    while time.time() - t0 < args.timeout:
        time.sleep(max(1.0, args.poll))
        st4, last = _get(f"{BASE}/api/v1/predictions/{rid}/result", key)
        status = str(last.get("status") or last.get("state") or "").lower()
        print(f"  轮询 {int(time.time() - t0):3d}s · HTTP {st4} · status={status or '?'}")
        if status in ("completed", "succeeded", "success", "done"):
            video_url = _pick_video_url(last)
            break
        if status in ("failed", "error", "cancelled"):
            break
    report["pollSeconds"] = round(time.time() - t0, 1)
    report["requestId"] = rid
    report["lastPoll"] = last

    if not video_url:
        report["error"] = "没拿到视频 URL（见 lastPoll 原始响应）"
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print("[X] 未拿到视频；原始响应已写 report.json", file=sys.stderr)
        return 1

    # 4) 下载
    dst = out / "lipsync.mp4"
    req = urllib.request.Request(video_url, headers={"User-Agent": "OpenAI/Python 1.0"})
    with urllib.request.urlopen(req, timeout=600) as r, dst.open("wb") as f:
        f.write(r.read())
    report.update({"ok": True, "videoUrl": video_url, "out": str(dst),
                   "bytes": dst.stat().st_size,
                   "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"[v] 完成：{dst}（{dst.stat().st_size // 1024} KB）· report.json 已写")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
