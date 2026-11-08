"""对白编辑（职位实现）：`script.final` -> `shot.voice`（2026-11-08）。

## 为什么这一层值得接

配音现在是在 `episode.py` 里**内联算**的：抽台词 → 断句 → 估时 → 排停顿。
它能跑，但有两个问题：

1 **不可审**：产物是"渲染时顺手算的"，证据里看不到"配音计划"本身
2 **不可替换**：想换断句策略/换情绪标注，得改主流程

名册里 `dialogue_editor` 早就声明了 `requires=script.final` / `produces=shot.voice` ——
**一直没接**，所以这项能力只存在于 runtime 代码里，不在链路里。

## 这一版做什么（**确定性的，不调模型**）

```
script.final
  -> 逐场抽台词（保留角色与情绪）
  -> governance.dialogue_editor.speech_plan（断句 / 停顿 / 句尾呼吸 / 估时）
  -> shot.voice：{lines: [{sceneId, character, text, segments, pauses,
                          estimateSeconds, tailPause}], totalSeconds}
```

**为什么不用 LLM**：断句与停顿是**可数**的事，规则已经标定过
（`ONSET_SECONDS=1.08`、`PER_CHAR_SECONDS=0.094`，MAE 0.15s）。
让模型重写一遍只会引入不可复现的抖动 —— 情绪标注已经在剧本里，不需要它猜。

## 纪律

- **没有台词也要产出**（`lines=[]`），不能返回 None
- 估时与停顿**全部带数字**，下游（voice_track）直接照用
"""
from __future__ import annotations

from typing import Any

from little_crew.agent.protocol import CallResult


class DialogueEditorAgent:
    """名册里的 `dialogue_editor`。"""

    agent_id = "dialogue_editor"

    def __init__(self, *, max_line: int = 60) -> None:
        self.max_line = int(max_line)
        self.calls = 0

    def __call__(self, *, envelope, runtime=None) -> CallResult:
        from little_crew.agent.crew_llm import _as_draft
        from little_crew.governance.dialogue_editor import (
            estimate_segment, speech_plan,
        )

        self.calls += 1
        p = dict(envelope.payload or {})
        draft = _as_draft(p.get("script.final") or p.get("script.draft"))
        if draft is None:
            return CallResult.fail("F-INPUT", "对白编辑未收到定稿剧本")

        lines: list[dict[str, Any]] = []
        for sc in (getattr(draft, "scenes", ()) or ()):
            sid = str((sc or {}).get("scene_id") or (sc or {}).get("id") or "")
            for d in ((sc or {}).get("dialogue") or ()):
                text = str((d or {}).get("line") or "").strip()
                if not text:
                    continue
                who = str((d or {}).get("character") or "").strip()
                plan = speech_plan(text[: self.max_line])
                segs = list(plan.get("segments") or ())
                lines.append({
                    "sceneId": sid,
                    "character": who,
                    "text": text,
                    "emotion": str((d or {}).get("emotion") or ""),
                    "segments": [{"text": s["text"], "pause": s["pause"]}
                                 for s in segs],
                    "segmentCount": len(segs),
                    "pauseSeconds": round(sum(float(s["pause"]) for s in segs), 3),
                    "tailPause": float(plan.get("tailPause") or 0.0),
                    "estimateSeconds": float(
                        plan.get("estimate")
                        or sum(estimate_segment(s["text"]) for s in segs)),
                })

        total = round(sum(x["estimateSeconds"] for x in lines), 2)
        payload = {
            "kind": "shot.voice",
            "episodeId": str(getattr(draft, "episode_id", "") or ""),
            "lines": lines,
            "lineCount": len(lines),
            "totalSeconds": total,
            # 明确列出这一层**没做什么** —— 免得被当成"已做口型/情绪"
            "notDone": ["口型同步（画面没有会动的嘴）",
                        "逐句情绪由剧本给定，不由本职位推断"],
        }
        return CallResult.success({"shot.voice": payload},
                                  evidence=[{"kind": "voice.plan",
                                             "lines": len(lines),
                                             "seconds": total}])
