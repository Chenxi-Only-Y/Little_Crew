"""把一次运行的"调用树 + 阶段 + 预算"落成 `trace.json`（2026-11-12）。

## 为什么需要

`LayerRuntime._spans` 里有完整的调用记录（谁调谁、什么能力、预算、耗时），
但它**只在内存**：进程一退、崩溃、被 kill，这份审计就没了。
报告里只剩"结果"，回答不了"这 40 分钟里到底调了多少次、谁调的、花了多少"。

本模块做一件事：把三样东西写成一个文件，**先写 `<name>.tmp` 再替换**：

```
<workdir>/trace.json
{
  "kind": "episode.trace",
  "episodeId": "EF40",
  "ok": true,
  "writtenAt": "...",
  "stages": {"script": {"t": 1.2}, ...},        // episode.trace
  "spans": [{"kind": "agent", "caller": ..., "callee": ..., "capability": ...,
             "traceId": ..., "budget": {...}}, ...],
  "spanCount": 57,
  "budget": {"limits": {...}, "spent": {...}, "calls": 57, "refused": 0},
  "truncated": false
}
```

## 三条纪律

1 **原子写**（tmp + replace）：读到一半的 trace 比没有更糟 —— 会让人误判。
2 **有上限**：span 数量超过 `max_spans` 时**保留头尾**并标 `truncated`，
   不静默截断（否则"看起来只有 200 次调用"是假象）。
3 **写失败不阻断**：它是观测手段，不是产出。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 默认最多保留多少条 span（超出保留头尾）
DEFAULT_MAX_SPANS = 500


def _trim(spans: Sequence[Mapping[str, Any]], limit: int) -> tuple[list, bool]:
    spans = list(spans or ())
    if limit <= 0 or len(spans) <= limit:
        return spans, False
    head = limit // 2
    tail = limit - head
    return list(spans[:head]) + list(spans[-tail:]), True


def dump(workdir: Any, *, episode_id: str = "", ok: bool | None = None,
         stages: Mapping[str, Any] | None = None,
         spans: Sequence[Mapping[str, Any]] | None = None,
         budget: Mapping[str, Any] | None = None,
         max_spans: int = DEFAULT_MAX_SPANS,
         reason: str = "") -> str:
    """写 `trace.json`，返回路径；失败返回空串（不抛）。"""
    try:
        root = Path(str(workdir))
        root.mkdir(parents=True, exist_ok=True)
        trimmed, truncated = _trim(spans or (), int(max_spans))
        payload = {
            "kind": "episode.trace",
            "episodeId": str(episode_id),
            "ok": ok,
            "reason": str(reason)[:400],
            "writtenAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": dict(stages or {}),
            "spans": trimmed,
            "spanCount": len(list(spans or ())),
            "truncated": truncated,
            "budget": dict(budget or {}),
        }
        p = root / "trace.json"
        tmp = root / "trace.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                  default=str), encoding="utf-8")
        os.replace(tmp, p)
        return str(p)
    except Exception as exc:                                  # noqa: BLE001
        print(f"  ⚠️ trace 落盘失败（不影响出片）：{type(exc).__name__}: {exc}")
        return ""


def read_trace(workdir: Any) -> dict:
    """读 `trace.json`；没有或坏了返回空 dict（不抛）。"""
    p = Path(str(workdir)) / "trace.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}
