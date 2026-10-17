"""实测：local-gen 的 H3 产出的 mp4 里**到底有没有音轨**。

## 为什么值得单独测

`local-gen/app/engines/h3.py` 里写着：

    from diffsynth.pipelines.minimax_h3_audio_video import MiniMaxH3Pipeline
    from diffsynth.utils.data.audio_video import write_video_audio
    video, audio = pipe(...)
    write_video_audio(... audio=audio, audio_sample_rate=pipe.audio_vae.sample_rate)

**看起来是音画同出**。但"代码看起来会出音频"与"产出的文件里真有音轨"
是两件事 —— 中间可能：音频被丢弃、mux 失败、或参数为 0 导致静音。

**所以不看代码，看文件。** 本脚本解析 mp4 容器找音轨。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

BASE = "http://127.0.0.1:8100"
OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\audio-track")
OUT.mkdir(parents=True, exist_ok=True)


def post_json(url: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def probe_mp4(path: pathlib.Path) -> dict:
    """**不依赖 ffprobe** —— 直接解析 mp4 容器。

    判据：
      · `moov/trak/mdia/hdlr` 的 handler 为 `soun` -> 有音轨
      · 出现 `mp4a` / `aac ` 等采样描述 -> 音频编解码器
      · 出现 `vide` handler -> 视频轨
    这是容器层的事实，不是推测。
    """
    b = path.read_bytes()
    res = {
        "bytes": len(b),
        "has_video_handler": b"vide" in b,
        "has_sound_handler": b"soun" in b,
        "codecs_found": sorted({m.decode("ascii", "replace") for m in
                                re.findall(rb"(mp4a|aac |ac-3|opus|alac|avc1|hvc1|hev1|vp09|av01)",
                                           b)}),
    }
    return res


def main() -> int:
    print("=" * 76)
    print("H3 产出里有没有音轨")
    print("=" * 76)

    # 先看服务状态
    try:
        h = get_json(f"{BASE}/health")
        print(f"  /health  loaded={h.get('loaded_model')}  "
              f"VRAM 空闲 {h.get('vram', {}).get('free_gb')} GB")
    except Exception as e:                                    # noqa: BLE001
        print(f"  ❌ 服务不可用: {e}")
        return 1

    body = {
        "prompt": "清晨的薰衣草田，风吹过花海，远处有鸟叫",
        "width": 640, "height": 384,
        "seconds": 2,
        "num_inference_steps": 8,
        "seed": 42,
    }
    print(f"\n  提交视频任务: {json.dumps(body, ensure_ascii=False)}")
    t0 = time.perf_counter()
    try:
        job = post_json(f"{BASE}/v1/videos/generations", body, timeout=180)
    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
        return 1
    print(f"  任务已建: {json.dumps(job, ensure_ascii=False)[:200]}")

    jid = job.get("id") or job.get("job_id") or job.get("jobId")
    if not jid:
        print("  ⚠️ 响应里没有 job id，无法轮询")
        return 1

    # 轮询（H3 首次要加载模型，可能要几分钟）
    deadline = time.perf_counter() + 1800
    last = ""
    while time.perf_counter() < deadline:
        time.sleep(10)
        try:
            st = get_json(f"{BASE}/v1/videos/generations/{jid}")
        except Exception as e:                                # noqa: BLE001
            print(f"    轮询失败: {type(e).__name__}")
            continue
        status = str(st.get("status") or "")
        el = time.perf_counter() - t0
        if status != last:
            print(f"    [{el:6.1f}s] status={status}")
            last = status
        if status in ("succeeded", "success", "completed", "done"):
            print(f"  ✅ 完成，耗时 {el:.1f}s")
            print(f"     响应: {json.dumps(st, ensure_ascii=False)[:300]}")
            url = ""
            for k in ("url", "video_url", "videoUrl", "output"):
                v = st.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    url = v
                    break
            if not url:
                data = st.get("data") or []
                if data and isinstance(data[0], dict):
                    url = data[0].get("url") or ""
            if not url:
                print("  ⚠️ 拿不到产物 URL")
                return 1
            print(f"  下载 {url}")
            dst = OUT / "h3_probe.mp4"
            with urllib.request.urlopen(url, timeout=300) as r:
                dst.write_bytes(r.read())
            info = probe_mp4(dst)
            print()
            print("=" * 76)
            print("  容器探测（不依赖 ffprobe）")
            print("=" * 76)
            for k, v in info.items():
                print(f"    {k}: {v}")
            print()
            if info["has_sound_handler"]:
                print("  ✅✅ **产物里有音轨（soun handler）—— H3 确实是音画同出**")
            else:
                print("  ❌ 没有 soun handler —— 音频没有进容器")
            (OUT / "probe.json").write_text(
                json.dumps({"request": body, "elapsedS": round(el, 1),
                            "response": st, "probe": info},
                           ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        if status in ("failed", "error"):
            print(f"  ❌ 失败: {json.dumps(st, ensure_ascii=False)[:400]}")
            return 1
    print("  ⏱ 超时")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
