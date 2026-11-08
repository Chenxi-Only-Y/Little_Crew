"""摄影（职位实现）：`shot.image` -> `shot.video`（2026-11-08）。

## 名册声明

```
camera_operator: requires=shot.image  produces=shot.video
```

## 它做什么

```
{shotId: 图片路径} + 每镜时长
  -> video_operator.render（kenburns 静帧缓推 / h3 真运动 / cloud）
  -> shot.video {clips:[{shotId, path, seconds}], backend, ok, failed}
```

**抽成职位的好处**：视频阶段从 `episode.py` 里的一段代码，变成
**有契约、可替换、可审计**的产物 —— 换后端（本地/云端）不改主流程。

## 纪律

- **一镜失败不影响其它**（记录 `failed`，不整体抛）
- **时长必须与镜头表一致**（片段比镜头短 = 后面黑屏）
- `h3` 有硬上限（`H3_MAX_SECONDS`），超了如实报，不偷偷截断
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from little_crew.agent.protocol import CallResult


class CameraOperatorAgent:
    """名册里的 `camera_operator`。"""

    agent_id = "camera_operator"

    def __init__(self, *, backend: str = "kenburns", ffmpeg: str = "ffmpeg",
                 size: tuple[int, int] = (768, 1024), fps: int = 24) -> None:
        self.backend = str(backend or "kenburns")
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.size = size
        self.fps = int(fps)
        self.calls = 0

    def __call__(self, *, envelope, runtime=None) -> CallResult:
        from little_crew.runtime import video_operator as VO

        self.calls += 1
        p = dict(envelope.payload or {})
        shots = list(p.get("shot.list") or ())
        images = dict(p.get("images") or {})
        out_dir = Path(str(p.get("outDir") or "outputs/_clips"))
        backend = str(p.get("backend") or self.backend)
        if not shots:
            return CallResult.fail("F-INPUT", "摄影未收到镜头表")

        clips: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for s in shots:
            sid = str(getattr(s, "shot_id", "") or "")
            img = images.get(sid, "")
            secs = float(getattr(s, "duration_sec", 0) or 0)
            if not img or not Path(str(img)).is_file():
                failed.append({"shotId": sid, "error": "没有图片"})
                continue
            out = out_dir / f"{sid}.mp4"
            r = VO.render(img, out, duration=secs, size=self.size,
                          fps=self.fps, backend=backend,
                          ffmpeg=self.ffmpeg)
            if r.get("ok"):
                clips.append({"shotId": sid, "path": str(out),
                              "seconds": float(r.get("seconds") or secs),
                              "backend": backend})
            else:
                failed.append({"shotId": sid, "error": str(r.get("error") or "")[:120]})

        payload = {
            "kind": "shot.video",
            "backend": backend,
            "clips": clips,
            "failed": failed,
            "clipCount": len(clips),
            "totalSeconds": round(sum(c["seconds"] for c in clips), 2),
            "notDone": ["口型同步", "镜头语言的语义正确性（只保证时长与构图）"],
        }
        return CallResult.success({"shot.video": payload},
                                  evidence=[{"kind": "clips",
                                             "count": len(clips),
                                             "backend": backend}])
