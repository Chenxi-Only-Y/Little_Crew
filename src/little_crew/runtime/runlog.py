"""结构化运行日志：`<workdir>/run.log.jsonl`（2026-11-12）。

## 为什么不是 `print`

现在跑一集的全部信息都在 stdout 里：重定向丢了就没、想按阶段筛只能 grep 中文、
多个进程同时跑还会交叠。日志应当是**机器可读、可按阶段过滤、可轮转**的。

本模块写 NDJSON（每行一个 JSON 对象），字段固定：

```
{"t":"2026-11-12T20:40:01","elapsed":12.3,"level":"info",
 "stage":"render","event":"stage","episodeId":"EF40","msg":"","extra":{}}
```

## 三条纪律

1 **永不抛出**：日志失败不能把一集跑挂（只提示一次）。
2 **按大小轮转**：超过 `max_bytes`（默认 8 MB）就改名 `.1`，旧 `.1` 覆盖 ——
   一集跑 50 分钟、40 镜各若干条，不轮转会越滚越大。
3 **`elapsed` 是相对开始的秒数**：跨进程对齐时间戳很难，相对时间够用且稳定。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunLog:
    """NDJSON 运行日志。线程安全，写失败不抛。"""

    workdir: Any
    episode_id: str = ""
    max_bytes: int = 8 * 1024 * 1024
    _lock: Any = field(default=None, repr=False, compare=False)
    _t0: float = field(default_factory=time.time, repr=False)
    _warned: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._lock is None:
            self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return Path(str(self.workdir)) / "run.log.jsonl"

    def write(self, event: str, *, level: str = "info", stage: str = "",
              msg: str = "", **extra: Any) -> None:
        with self._lock:
            try:
                self._rotate()
                rec = {
                    "t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                    "elapsed": round(time.time() - self._t0, 2),
                    "level": str(level),
                    "stage": str(stage),
                    "event": str(event),
                    "episodeId": str(self.episode_id),
                    "msg": str(msg)[:500],
                    "extra": {k: v for k, v in extra.items()
                              if isinstance(v, (str, int, float, bool))},
                }
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as exc:                          # noqa: BLE001
                if not self._warned:
                    self._warned = True
                    print(f"  ⚠️ 日志写入失败（不影响出片）：{type(exc).__name__}: {exc}")

    def _rotate(self) -> None:
        try:
            if self.max_bytes <= 0 or not self.path.is_file():
                return
            if self.path.stat().st_size < self.max_bytes:
                return
            os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        except Exception:                                     # noqa: BLE001
            pass


def read_events(workdir: Any, *, stage: str = "", limit: int = 0) -> list[dict]:
    """读日志（可按 stage 过滤）。坏行跳过 —— 读日志不该因为一行坏而失败。"""
    p = Path(str(workdir)) / "run.log.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:                                 # noqa: BLE001
                continue
            if stage and rec.get("stage") != stage:
                continue
            out.append(rec)
            if limit and len(out) >= limit:
                break
    except Exception:                                         # noqa: BLE001
        return out
    return out
