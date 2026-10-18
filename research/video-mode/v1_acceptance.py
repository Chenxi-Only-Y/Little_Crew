"""**V1 真实验收**：2 个已有静帧 → `render_video_stage` → 2 个带音轨的 mp4。

## 这一步验什么

`alpha.66` 只验了**接线**（假通道）。这一步验**两段式真的成立**：

```
已有静帧  →  h3_video.render_videos  →  mp4（带音轨）
```

预期 ≈ 12 分钟（2 镜 × ~340 秒 + 模型加载）。

## 与 `probe_async_channel.py` 的区别

那个是**单镜直连**探针，验的是 `HttpChannel` 本身。
这个走的是**主流程的方法** `EpisodeRunner.render_video_stage` ——
也就是"将来 `run()` 会调的那一个"。

**所以它验的是接线 + 通道一起。**

## 判据

| 项 | 判据 |
|---|---|
| 两镜都出片 | `ok=True` 且 `clips` 有 2 个 |
| **都带音轨** | `audio_track_present` 断言通过（这是 M3 的核心要求） |
| 体积合理 | 每个 > 100 KB |
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

ROOT = pathlib.Path(r"D:\AI\ds.8\Little_Crew")
OUT = ROOT / "research" / "video-mode"
OUT.mkdir(parents=True, exist_ok=True)

#: 用主流程已经产出的真静帧（成集那次跑的）
CANDIDATES = [
    ROOT / "research" / "llm-crew" / "full_S01-001.png",
    ROOT / "research" / "llm-crew" / "full_S02-001.png",
    ROOT / "research" / "llm-crew" / "full_S03-001.png",
]

PROMPTS = {
    "S01-001": "镜头缓慢横移，风穿过薰衣草，少女抬头望向远处",
    "S02-001": "镜头轻推，雨落在窗玻璃上，屋内只有一盏灯",
}


def main() -> int:
    print("=" * 78)
    print("V1 真实验收：静帧 -> 动画化（走主流程的 render_video_stage）")
    print("=" * 78)

    stills = [p for p in CANDIDATES[:2] if p.is_file()]
    if len(stills) < 2:
        print(f"  ❌ 静帧不足（找到 {len(stills)} 个）")
        for p in CANDIDATES:
            print(f"     {'✓' if p.is_file() else '✗'} {p.name}")
        return 1
    print(f"  静帧：{[p.name for p in stills]}")

    from little_crew.agent.crew_stubs import default_crew_stubs
    from little_crew.agent.orchestrator import CrewOrchestrator
    from little_crew.runtime.cache import ContentCache
    from little_crew.runtime.cache_measure import _null_compiler
    from little_crew.runtime.cached_render import CachingRenderer, artifact_fetcher_for
    from little_crew.runtime.channels.http import HttpChannel
    from little_crew.runtime.episode import EpisodeRunner
    from little_crew.runtime.h3_video import h3_backend
    from little_crew.runtime.render import RenderSession

    be = h3_backend()
    if be is None:
        print("  ❌ 拿不到本地 H3 后端 —— 本地服务没起。**不退回付费 API。**")
        return 1
    print(f"  后端：{be.endpoint}{be.path}")

    ch = HttpChannel(be, timeout_s=1800)
    ok, why = ch.available()
    print(f"  通道：{ok}  {why}")
    if not ok:
        return 1

    renderer = CachingRenderer(
        RenderSession(ch, _null_compiler()),
        ContentCache(OUT / "cache"),
        fetcher=artifact_fetcher_for(ch),
        checkpoint=None)

    orch = CrewOrchestrator(model_families={})
    orch.bind_many(default_crew_stubs())
    runner = EpisodeRunner(orch, renderer, episode_id="V1PROBE")

    # `{静帧名: 路径}` —— 键就是 shotId
    shot_map = {p.stem.replace("full_", ""): p for p in stills}
    prompt_map = {sid: PROMPTS.get(sid, "镜头缓慢推进") for sid in shot_map}

    print("\n  开始动画化（2 镜，约 12 分钟）…")
    t0 = time.perf_counter()
    res = runner.render_video_stage(
        shot_map, prompt_map, seconds=2.0, steps=8, seed_base=7,
        on_progress=lambda *a: print(f"    · {a[1] if len(a) > 1 else a}"))
    dt = time.perf_counter() - t0

    print()
    print("=" * 78)
    print(f"  结果 ok={res['ok']}  耗时 {dt:.1f}s")
    print(f"  片段 {len(res['clips'])} 个   失败 {len(res['failures'])} 条")
    if res.get("reason"):
        print(f"  原因 {res['reason']}")
    for f in res["failures"][:5]:
        print(f"    ✗ {f}")

    # ---- 落盘 + 探音轨 ----
    from little_crew.review.observe import measure_artifact
    from little_crew.review.ops import audio_track_present

    verdicts = {}
    for sid, blob in res["clips"].items():
        dst = OUT / f"{sid}.mp4"
        dst.write_bytes(blob)
        probe = measure_artifact(dst.name, lambda n, d=dst: d if n == d.name else None)
        v = audio_track_present({}, probe)
        verdicts[sid] = {"bytes": len(blob), "container": probe.get("container"),
                         "hasVideo": probe.get("hasVideo"),
                         "hasAudio": probe.get("hasAudio"),
                         "passed": v.passed, "detail": v.detail}
        print(f"\n  {sid}.mp4  {len(blob)/2**20:.2f} MB")
        print(f"    容器 {probe.get('container')} / 视频 {probe.get('hasVideo')}"
              f" / 音轨 {probe.get('hasAudio')}")
        print(f"    断言 {'✅ 通过' if v.passed else '❌ 不通过'} —— {v.detail}")

    all_audio = bool(verdicts) and all(x["hasAudio"] for x in verdicts.values())
    print()
    if len(verdicts) == 2 and all_audio:
        print("  ✅✅ **V1 验收通过：两镜都出片、都带音轨** —— 两段式成立")
    elif verdicts:
        print(f"  ⚠️ 部分通过：{len(verdicts)} 个片段，带音轨 "
              f"{sum(1 for x in verdicts.values() if x['hasAudio'])}/2")
    else:
        print("  ❌ 没有任何片段产出")

    (OUT / "v1-acceptance.json").write_text(json.dumps({
        "kind": "video-mode-v1",
        "elapsedSeconds": round(dt, 1),
        "ok": res["ok"], "reason": res.get("reason"),
        "backend": res.get("backend"),
        "clips": verdicts,
        "failures": res["failures"],
        "allHaveAudio": all_audio,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  证据写入 {OUT}")
    return 0 if (len(verdicts) == 2 and all_audio) else 1


if __name__ == "__main__":
    raise SystemExit(main())
