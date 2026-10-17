"""**4 镜有声短片** —— 用 H3 的 i2v 把静帧变成"有画面、有声音"的片段。

## 为什么走 i2v 而不是 t2v

H3 是 `FL2VA`（first-last -> video + audio），支持**首帧图**。
本项目已有 Z-Image 出的静帧 —— 把它当首帧喂进去，得到的是：

    **我们控制的构图** + 模型补的运镜 + **模型自带的音轨**

纯 t2v 会丢掉构图（那就是前面 img2img 否证过的那类问题）。
**i2v 是这里唯一合理的选择。**

## 为什么只做 4 镜

单镜实测 **289 秒纯生成**（640×384 / 8 步 / 107 帧）。
24 镜要 2.6 小时 —— 那不是"演示"，那是排队。

4 镜 ≈ 20~25 分钟，足够证明"这条路通、成片有声"。
**把慢说清楚，比假装它能跑整集诚实。**

## 这一步在验证链里的位置

    Z-Image 静帧（已有 -> 复用，不重跑）
      -> H3 i2v（出 mp4，自带 AAC 音轨）
      -> ffmpeg concat
      -> **用我们自己的算子验证音轨存在**

最后一步是关键：不靠"我觉得应该有"，而是用
`review.observe.measure_artifact` + `review.ops.audio_track_present`
去**量**那个文件。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from little_crew.review.observe import measure_artifact
from little_crew.review.ops import audio_track_present

BASE = "http://127.0.0.1:8100"
FFMPEG = r"D:\AI\FFmpeg\bin\ffmpeg.exe"
OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\audio-track")
CLIPS = OUT / "clips"
CLIPS.mkdir(parents=True, exist_ok=True)

#: 4 镜：**跨场次挑**，让短片能看出场景变化。
#: 每项 (静帧文件名, 运镜提示)。提示词刻意写"镜头运动 + 环境声"，
#: 因为 H3 的音轨是跟画面一起生成的 —— 描述里点到声音，音频才有内容。
SHOTS: tuple[tuple[str, str], ...] = (
    ("04502b88_img_20261017_183702_a3d7a3.png",
     "镜头缓慢横移，风穿过薰衣草，花穗起伏，远处有鸟鸣"),
    ("d83cd330_img_20261017_183734_bdb7ed.png",
     "镜头轻轻前推，晨雾散开，露水从叶片滴落，水滴声清晰"),
    ("c3f2ae15_img_20261017_184431_eb1913.png",
     "镜头微微上摇，云影掠过屋顶，风铃轻响，人声低语"),
    ("b92bc3b7_img_20261017_184818_25251a.png",
     "镜头缓慢后拉，暮色渐沉，虫鸣渐起，脚步踩过碎石"),
)

SECONDS = 2          # 请求 2 秒（实测会出 4.46 秒，H3 的帧数是离散的）
STEPS = 8
W, H = 640, 384


def _post(url: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def make_clip(still: pathlib.Path, prompt: str, idx: int) -> tuple[dict, float]:
    """提交一个 i2v 任务并等到产物落盘。返回 (探测结果, 耗时)。"""
    body = {"prompt": prompt, "image_path": str(still),
            "width": W, "height": H, "seconds": SECONDS,
            "num_inference_steps": STEPS, "seed": 100 + idx}
    t0 = time.perf_counter()
    job = _post(f"{BASE}/v1/videos/generations", body, timeout=180)
    jid = job.get("job_id") or job.get("id") or job.get("task_id")
    if not jid:
        raise RuntimeError(f"服务端没给任务 id: {job}")

    deadline = time.perf_counter() + 1500
    state = ""
    while time.perf_counter() < deadline:
        time.sleep(10)
        st = _get(f"{BASE}/v1/videos/generations/{jid}")
        s = str(st.get("status") or "")
        if s != state:
            print(f"      [{time.perf_counter()-t0:6.1f}s] {s}")
            state = s
        if s == "succeeded":
            url = (st.get("result") or {}).get("url") or ""
            if not url:
                raise RuntimeError(f"成功但没有 url: {st}")
            dst = CLIPS / f"clip{idx:02d}.mp4"
            with urllib.request.urlopen(url, timeout=300) as r:
                dst.write_bytes(r.read())
            return measure_artifact(dst.name, lambda n: dst if n == dst.name else None), \
                time.perf_counter() - t0
        if s in ("failed", "error", "canceled"):
            raise RuntimeError(f"服务端失败: {st}")
    raise TimeoutError(f"任务 {jid} 超时")


def concat(clips: list[pathlib.Path], out: pathlib.Path) -> None:
    """ffmpeg concat（`-c copy` 保留音轨）。"""
    lst = CLIPS / "concat.txt"
    lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips),
                   encoding="utf-8")
    cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0",
           "-i", str(lst), "-c", "copy", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败: {(r.stderr or '')[-800:]}")


def main() -> int:
    print("=" * 76)
    print("4 镜有声短片 —— H3 i2v（静帧 -> 视频 + 自带音轨）")
    print("=" * 76)

    # 静帧从已有的整集产物里复用 —— **不重跑 Z-Image**
    roots = [pathlib.Path(r"D:\AI\ds.8\Little_Crew\outputs\episode_full")]
    srcs: dict[str, pathlib.Path] = {}
    for root in roots:
        for p in root.rglob("*.png"):
            srcs.setdefault(p.name, p)
    picked = []
    for name, _prompt in SHOTS:
        p = srcs.get(name)
        if p is None:
            print(f"  ⚠️ 找不到静帧 {name}")
            continue
        picked.append(p)
    if len(picked) != len(SHOTS):
        print(f"  只用找到的 {len(picked)} 镜")
    if not picked:
        return 1

    rows: list[dict] = []
    clips: list[pathlib.Path] = []
    t_all = time.perf_counter()
    for i, (still, (_n, prompt)) in enumerate(zip(picked, SHOTS), 1):
        print(f"\n  [{i}/{len(picked)}] {still.name}")
        print(f"      提示词: {prompt}")
        try:
            probe, secs = make_clip(still, prompt, i)
        except Exception as e:                                # noqa: BLE001
            print(f"      ❌ {type(e).__name__}: {e}")
            continue
        clips.append(CLIPS / f"clip{i:02d}.mp4")
        print(f"      ✅ {secs:.0f}s  容器={probe.get('container')} "
              f"视频={probe.get('hasVideo')} 音频={probe.get('hasAudio')} "
              f"{probe.get('bytes', 0)/2**20:.1f} MB")
        rows.append({"shot": i, "still": still.name, "prompt": prompt,
                     "seconds": round(secs, 1), "probe": probe})
        (OUT / "demo.partial.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if not clips:
        print("\n  没有成功片段")
        return 1

    print(f"\n  拼接 {len(clips)} 段…")
    final = OUT / "audio_demo.mp4"
    try:
        concat(clips, final)
    except Exception as e:                                    # noqa: BLE001
        print(f"  ❌ 拼接失败: {e}")
        return 1

    # ---- 用**我们自己的算子**验证音轨 ----
    probe = measure_artifact(final.name,
                             lambda n: final if n == final.name else None)
    verdict = audio_track_present({}, probe)

    print()
    print("=" * 76)
    print("  成片验证（用 review 层的算子，不靠肉眼）")
    print("=" * 76)
    print(f"  文件      {final.name}")
    print(f"  大小      {probe.get('bytes', 0)/2**20:.2f} MB")
    print(f"  容器      {probe.get('container')}")
    print(f"  视频轨    {probe.get('hasVideo')}")
    print(f"  **音轨**  **{probe.get('hasAudio')}**")
    print(f"  算子判定  {'✅ 通过' if verdict.passed else '❌ 不通过'} —— {verdict.detail}")
    print(f"  总耗时    {time.perf_counter()-t_all:.0f}s")

    summary = {
        "kind": "h3-audio-demo",
        "model": "minimax-h3 (local-gen i2v)",
        "clips": rows,
        "final": {"file": final.name,
                  "bytes": probe.get("bytes"),
                  "container": probe.get("container"),
                  "hasVideo": probe.get("hasVideo"),
                  "hasAudio": probe.get("hasAudio")},
        "operatorVerdict": {"passed": verdict.passed, "detail": verdict.detail},
        "totalSeconds": round(time.perf_counter() - t_all, 1),
        "note": ("H3 是音画同出（FL2VA），音轨与视频同一遍前向产出 —— "
                 "不需要独立 TTS。单镜实测约 290 秒纯生成，"
                 "所以只做 4 镜；整集 24 镜约 2.6 小时。"),
    }
    (OUT / "demo.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  证据写入 {OUT}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
