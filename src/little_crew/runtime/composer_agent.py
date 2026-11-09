"""配乐（职位实现）：`shot.list` -> `shot.music`（2026-11-08）。

## 名册声明

```
composer: requires=shot.list  produces=shot.music
```

## 它做什么

```
shot.list（总时长/每镜情绪线索）
  -> music_bed.resolve（素材优先，没有就程序生成垫音）
  -> shot.music {path, seconds, source, mood, requestedSeconds}
```

**不调模型**：这一层是"按总时长铺一条床"，素材选不选是文件系统的事。
**不冒充配乐**：`source` 如实写 `asset:<文件>` 或 `generated:ffmpeg-sine` ——
后者是**垫音**，不是作曲。

## 纪律

- **时长必须够**：少于成片时长就是"后半段静音"（真跑踩过：68 秒的床盖 137 秒的片）
- **缺素材要说明**：`skipped: no asset` 而不是假装有音乐
"""
from __future__ import annotations

from typing import Any

from little_crew.agent.protocol import CallResult


class ComposerAgent:
    """名册里的 `composer`。"""

    agent_id = "composer"

    def __init__(self, *, ffmpeg: str = "ffmpeg", mood: str = "") -> None:
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.mood = str(mood or "")
        self.calls = 0

    def __call__(self, *, envelope, runtime=None) -> CallResult:
        from little_crew.runtime import music_bed as MB

        self.calls += 1
        p = dict(envelope.payload or {})
        shots = list(p.get("shot.list") or ())
        # 总时长：优先用镜头表声明的，没有就按每镜时长求和
        total = float(p.get("totalSeconds") or 0)
        if total <= 0:
            total = sum(float(getattr(s, "duration_sec", 0) or 0) for s in shots)
        if total <= 0:
            return CallResult.fail("F-INPUT", "配乐未收到可用时长（镜头表为空）")

        out_dir = str(p.get("outDir") or "outputs/_music")
        mood = str(p.get("mood") or self.mood or "")
        # 调用方给的 ffmpeg 优先（默认 "ffmpeg" 在很多机器上不在 PATH）
        ff = str(p.get("ffmpeg") or self.ffmpeg or "ffmpeg")
        r = MB.resolve(total, out_dir, mood=mood, ffmpeg=ff)
        payload: dict[str, Any] = {
            "kind": "shot.music",
            "path": str(r.get("path") or ""),
            "seconds": float(r.get("seconds") or 0),
            "requestedSeconds": round(total, 2),
            "source": str(r.get("source") or ""),
            "mood": mood,
            "ok": bool(r.get("ok")),
            "error": str(r.get("error") or ""),
            # 没做/做不到的，写清楚
            "notDone": ["作曲/编曲（只有素材或程序垫音）",
                        "情绪曲线跟随（现在是一条恒定床）"],
        }
        if not r.get("ok"):
            payload["skipped"] = "no asset"
        return CallResult.success({"shot.music": payload},
                                  evidence=[{"kind": "music.bed",
                                             "seconds": payload["seconds"],
                                             "source": payload["source"]}])
