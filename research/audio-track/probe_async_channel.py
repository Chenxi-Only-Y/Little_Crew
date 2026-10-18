"""真调用验证：`CachingRenderer.render_http` 能不能驱动 local-gen 的 H3。

## 为什么必须单独验这一件事

`h3_video_check` 用的是**假渲染器** —— 它验的是"参数与编排对不对"，
**完全不碰通道**。而通道里有一串我从没跑通过的东西：

    submit(异步) -> 从响应取 job_id -> 轮询 status_path -> succeeded
                 -> 用 response_path 取产物 URL -> fetcher 下载字节

其中至少两处是我刚改/刚加的：

| 改动 | 风险 |
|---|---|
| `job_id` 纳入任务 id 解析 | 只验证过 `extract_path` 本身，**没跑过真实提交** |
| `result.url` 作为 `response_path` | 同上 |
| `status_path` 带 `{task_id}` 占位 | 没跑过 |

**不知道通道通不通，后面三步接线全是空中楼阁。**

## 成本

H3 单镜实测 **约 340 秒**纯生成 + 可能的模型切换加载（当前加载的是 zimage）。
本脚本跑一次约 **6~8 分钟**。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

sys.path.insert(0, r"D:\AI\ds.8\Little_Crew\src")
sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from little_crew.runtime.cache import ContentCache
from little_crew.runtime.cache_measure import _null_compiler
from little_crew.runtime.cached_render import (
    CachingRenderer,
    artifact_fetcher_for,
)
from little_crew.runtime.channels.http import HttpChannel
from little_crew.runtime.h3_video import VideoShot, h3_backend, video_values
from little_crew.runtime.render import RenderSession

OUT = pathlib.Path(r"D:\AI\ds.8\Little_Crew\research\audio-track")
WORK = OUT / "async-probe"
WORK.mkdir(parents=True, exist_ok=True)

#: 用一张已有的静帧当首帧（i2v）—— 与主流程将来要走的路径一致
STILL = OUT.parent / "llm-crew" / "full_S01-001.png"


def main() -> int:
    print("=" * 78)
    print("真调用：CachingRenderer.render_http -> local-gen H3")
    print("=" * 78)

    be = h3_backend()
    if be is None:
        print("  ❌ 取不到本地后端（minimax-h3 的 local-gen 通道）")
        return 1
    print(f"  后端    {be.endpoint}{be.path}")
    print(f"  async   {be.async_task}")
    print(f"  status  {be.status_path}")
    print(f"  resp    {be.response_path}")

    if not STILL.is_file():
        print(f"  ❌ 找不到首帧静帧: {STILL}")
        return 1
    print(f"  首帧    {STILL.name}  {STILL.stat().st_size // 1024} KB")

    ch = HttpChannel(be, timeout_s=1800)
    ok, why = ch.available()
    print(f"  通道可用 {ok}  {why}")
    if not ok:
        return 1

    renderer = CachingRenderer(
        RenderSession(ch, _null_compiler()),
        ContentCache(WORK / "cache"),
        fetcher=artifact_fetcher_for(ch),
        checkpoint=None)

    shot = VideoShot(shot_id="PROBE-H3", prompt="镜头缓慢横移，风穿过薰衣草，远处有鸟鸣",
                     image_path=STILL, width=640, height=384)
    values = video_values(shot, seconds=2.0, steps=8, seed=7)
    print("\n  入参:")
    for k, v in values.items():
        print(f"    {k} = {str(v)[:70]}")

    print("\n  提交并等待（约 6~8 分钟，含模型切换）…")
    t0 = time.perf_counter()
    outcome = renderer.render_http(values, be, job_id=shot.shot_id)
    dt = time.perf_counter() - t0

    print()
    print("=" * 78)
    print("  结果")
    print("=" * 78)
    print(f"  ok        {outcome.ok}")
    print(f"  耗时      {dt:.1f}s")
    print(f"  fromCache {outcome.from_cache}")
    if not outcome.ok:
        print(f"  failure   {outcome.failure}")
        print(f"  reason    {outcome.reason}")
        return 1

    arts = outcome.artifacts or {}
    print(f"  产物      {len(arts)} 个: {list(arts)}")
    if not arts:
        print("  ❌ 成功但没有产物字节")
        return 1

    name, blob = next(iter(arts.items()))
    dst = WORK / name
    dst.write_bytes(blob)
    print(f"  已落盘    {dst.name}  {len(blob)/2**20:.2f} MB")

    # ---- 用项目自己的测量层判容器音轨 ----
    from little_crew.review.observe import measure_artifact
    from little_crew.review.ops import audio_track_present

    probe = measure_artifact(dst.name, lambda n: dst if n == dst.name else None)
    verdict = audio_track_present({}, probe)
    print()
    print(f"  容器      {probe.get('container')}")
    print(f"  视频轨    {probe.get('hasVideo')}")
    print(f"  音轨      {probe.get('hasAudio')}")
    print(f"  断言      {'✅ 通过' if verdict.passed else '❌ 不通过'} —— {verdict.detail}")
    print()
    if verdict.passed:
        print("  ✅✅ **异步通道端到端通了，且产物带音轨** —— 后面三步接线有地基了")
    else:
        print("  ⚠️ 通道通了但产物没有音轨 —— 查落盘/转码环节")

    (OUT / "async-probe.json").write_text(json.dumps({
        "kind": "async-channel-probe",
        "backend": {"endpoint": be.endpoint, "path": be.path,
                    "statusPath": be.status_path,
                    "responsePath": be.response_path,
                    "asyncTask": be.async_task},
        "values": {k: str(v) for k, v in values.items()},
        "ok": outcome.ok, "elapsedSeconds": round(dt, 1),
        "fromCache": outcome.from_cache,
        "artifacts": {k: len(v) for k, v in arts.items()},
        "probe": {k: v for k, v in probe.items() if k != "signature"},
        "operatorPassed": verdict.passed,
        "operatorDetail": verdict.detail,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  证据写入 {OUT / 'async-probe.json'}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
