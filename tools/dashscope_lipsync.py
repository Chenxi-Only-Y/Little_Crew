#!/usr/bin/env python
"""DashScope（阿里云百炼）单镜口型验证：wan2.2-s2v（2026-11-15，B 项）。

用途：把「一张正脸图 + 一段中文对白」变成说话视频，验证口型能不能看。
只做**一次**调用，用来给"口型值不值得做"一个实测答案。

用法（Windows PowerShell）：
    $env:DASHSCOPE_API_KEY = "sk-..."      # 或写进 config/.env
    python tools/dashscope_lipsync.py `
        --image research/episodes/最后一班/lipsync-test/face.png `
        --audio research/episodes/最后一班/lipsync-test/line.wav `
        --out   research/episodes/最后一班/lipsync-test/out-aliyun

流程（与官方文档一致，2026-11 版）：
    1 GET  /api/v1/uploads?action=getPolicy&model=...   拿上传凭证（**文件与模型绑定**！）
    2 POST <upload_host>（OSS 表单）                      本地文件 -> oss:// URL（48h）
    3 POST /services/aigc/image2video/face-detect        wan2.2-s2v-detect 图片合规检测
    4 POST /services/aigc/image2video/video-synthesis    异步建任务（X-DashScope-Async: enable）
    5 GET  /api/v1/tasks/{task_id}                       轮询（官方约 5-10 分钟）
    6 下载 mp4；写 report.json（usage/费用；480P ¥0.5/秒、720P ¥0.9/秒）

约定：
- **只用标准库**（tools/ 的规矩：不 import 本包、不引第三方）
- key 只从环境/`config/.env` 读，不打印明文
- 用 oss:// 临时 URL 时，请求头必须带 `X-DashScope-OssResourceResolve: enable`（官方要求，漏了会解析失败）
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
import urllib.parse
import urllib.request
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_BASE = "https://dashscope.aliyuncs.com/api/v1"
DETECT_MODEL = "wan2.2-s2v-detect"
GEN_MODEL = "wan2.2-s2v"
PRICE_CNY_PER_SEC = {"480P": 0.5, "720P": 0.9}


def _key() -> str:
    k = str(os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if k:
        return k
    envf = ROOT / "config" / ".env"
    if envf.is_file():
        for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("DASHSCOPE_API_KEY="):
                return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _req(url: str, *, key: str, method: str = "GET", body: bytes | None = None,
         content_type: str = "application/json", extra: dict | None = None,
         timeout: float = 120) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json",
               "User-Agent": "little-crew-lipsync-test/1.0"}
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


def get_policy(base: str, key: str, model: str) -> dict:
    url = f"{base}/uploads?" + urllib.parse.urlencode({"action": "getPolicy",
                                                       "model": model})
    st, obj = _req(url, key=key)
    if st != 200 or not (obj.get("data") or {}).get("upload_host"):
        raise RuntimeError(f"取上传凭证失败 HTTP {st}: {json.dumps(obj, ensure_ascii=False)[:300]}")
    return obj["data"]


def upload_file(base: str, key: str, model: str, path: pathlib.Path) -> str:
    """上传到百炼临时存储（文件与模型绑定）→ 返回 oss:// URL。"""
    pol = get_policy(base, key, model)
    boundary = "----lc" + uuid.uuid4().hex
    name = path.name
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
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode("utf-8")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
             f"filename=\"{name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8")
    body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        pol["upload_host"], data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "little-crew-lipsync-test/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError(f"上传失败 HTTP {r.status}")
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"上传失败 HTTP {e.code}: {raw[:300]}") from e
    return f"oss://{key_path}"


def detect(base: str, key: str, image_url: str) -> dict:
    body = json.dumps({"model": DETECT_MODEL,
                       "input": {"image_url": image_url}}).encode("utf-8")
    st, obj = _req(f"{base}/services/aigc/image2video/face-detect",
                   key=key, method="POST", body=body,
                   extra={"X-DashScope-OssResourceResolve": "enable"})
    obj["_http"] = st
    return obj


def create_task(base: str, key: str, image_url: str, audio_url: str,
                resolution: str, style: str = "") -> dict:
    payload: dict = {"model": GEN_MODEL,
                     "input": {"image_url": image_url, "audio_url": audio_url},
                     "parameters": {"resolution": resolution}}
    if style:
        payload["parameters"]["style"] = style
    st, obj = _req(f"{base}/services/aigc/image2video/video-synthesis",
                   key=key, method="POST",
                   body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   extra={"X-DashScope-Async": "enable",
                          "X-DashScope-OssResourceResolve": "enable"})
    obj["_http"] = st
    return obj


def poll(base: str, key: str, task_id: str, *, timeout_s: float = 1200,
         interval_s: float = 15.0) -> dict:
    t0 = time.time()
    last: dict = {}
    while time.time() - t0 < timeout_s:
        time.sleep(max(3.0, interval_s))
        st, obj = _req(f"{base}/tasks/{task_id}", key=key, timeout=60)
        last = obj
        status = str((obj.get("output") or {}).get("task_status") or "").upper()
        print(f"  轮询 {int(time.time()-t0):4d}s · HTTP {st} · {status or '?'}")
        if status in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
            return obj
    return last


def wav_seconds(p: pathlib.Path) -> float:
    try:
        import wave
        with wave.open(str(p), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:                                         # noqa: BLE001
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="DashScope wan2.2-s2v 单镜口型验证")
    ap.add_argument("--image", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default="outputs/lipsync-test-aliyun")
    ap.add_argument("--resolution", default="480P", choices=("480P", "720P"))
    ap.add_argument("--style", default="", choices=("", "speech", "sing", "perform"),
                    help="官方 parameters.style（可选）")
    ap.add_argument("--base", default=os.environ.get("DASHSCOPE_BASE", DEFAULT_BASE),
                    help="API base（默认北京经典域名；工作空间域名可用 DASHSCOPE_BASE 覆盖）")
    ap.add_argument("--skip-detect", action="store_true",
                    help="跳过图片合规检测（不推荐；官方流程要求先检测）")
    ap.add_argument("--timeout", type=int, default=1200)
    args = ap.parse_args()

    key = _key()
    img, aud = pathlib.Path(args.image), pathlib.Path(args.audio)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"kind": "lipsync.test", "provider": "dashscope",
                 "model": GEN_MODEL, "resolution": args.resolution,
                 "image": str(img), "audio": str(aud),
                 "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if not key:
        print("[X] 没有 DASHSCOPE_API_KEY（环境变量或 config/.env）", file=sys.stderr)
        return 2
    for p in (img, aud):
        if not p.is_file():
            print(f"[X] 文件不存在：{p}", file=sys.stderr)
            return 2
    dur = wav_seconds(aud)
    unit = PRICE_CNY_PER_SEC.get(args.resolution, 0.5)
    rep["audioSeconds"] = round(dur, 2)
    rep["estCostCny"] = round(dur * unit, 2)
    print(f"  对白 {dur:.2f}s · 单价 ¥{unit}/秒 · 预估 ¥{rep['estCostCny']:.2f}")

    try:
        # 1) 检测图（模型绑定：wan2.2-s2v-detect）
        if not args.skip_detect:
            det_url = upload_file(args.base, key, DETECT_MODEL, img)
            print(f"  检测图已上传：{det_url[:60]}…")
            det = detect(args.base, key, det_url)
            rep["detect"] = det
            ok = bool((det.get("output") or {}).get("check_pass"))
            print(f"  图片检测 HTTP {det.get('_http')} · check_pass={ok}")
            if not ok:
                rep["error"] = "图片未通过 wan2.2-s2v 规范检测"
                (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                                 encoding="utf-8")
                print("[X] 图片不合规，已写 report.json", file=sys.stderr)
                return 1
        # 2) 上传生成用素材（模型绑定：wan2.2-s2v）
        img_url = upload_file(args.base, key, GEN_MODEL, img)
        aud_url = upload_file(args.base, key, GEN_MODEL, aud)
        print(f"  生成素材已上传：图 {img_url[-40:]} / 音 {aud_url[-40:]}")
        rep["imageUrl"] = img_url
        rep["audioUrl"] = aud_url
        # 3) 建任务
        task = create_task(args.base, key, img_url, aud_url, args.resolution, args.style)
        rep["createTask"] = task
        tid = str(((task.get("output") or {}).get("task_id")) or "")
        print(f"  建任务 HTTP {task.get('_http')} · task_id={tid or '（无）'}")
        if not tid:
            rep["error"] = "建任务失败"
            (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
            return 1
        # 4) 轮询
        done = poll(args.base, key, tid, timeout_s=args.timeout)
        rep["taskResult"] = done
        status = str((done.get("output") or {}).get("task_status") or "").upper()
        if status != "SUCCEEDED":
            rep["error"] = f"任务未成功：{status}"
            (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
            print(f"[X] 任务失败：{status}（原始响应见 report.json）", file=sys.stderr)
            return 1
        url = str(((done.get("output") or {}).get("results") or {}).get("video_url") or "")
        if not url:
            rep["error"] = "结果里没有 video_url"
            (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
            return 1
        # 5) 下载
        dst = out / "lipsync.mp4"
        req = urllib.request.Request(url, headers={"User-Agent": "little-crew-lipsync-test/1.0"})
        with urllib.request.urlopen(req, timeout=600) as r, dst.open("wb") as f:
            f.write(r.read())
        usage = done.get("usage") or {}
        secs = float(usage.get("duration") or dur)
        rep.update({"ok": True, "videoUrl": url, "out": str(dst),
                    "bytes": dst.stat().st_size, "usage": usage,
                    "costCny": round(secs * unit, 2),
                    "finishedAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print(f"[v] 完成：{dst}（{dst.stat().st_size//1024} KB）")
        print(f"    实际计费时长 {secs:.2f}s · 约 ¥{rep['costCny']:.2f}")
        return 0
    except Exception as exc:                                  # noqa: BLE001
        rep["error"] = f"{type(exc).__name__}: {exc}"
        (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        print(f"[X] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
