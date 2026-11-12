"""进度落盘：`state.json`（2026-11-12）。

## 为什么需要

长跑一集 20~50 分钟，此前**只能盯 stdout**：
换一台机器看、或者进程卡住时，外部完全不知道"跑到哪了、卡在哪"。
`_mark()` 早就在记 `self.stage` / `self.trace`，但那只在**内存里**，进程一停就没了。

本模块把"到了哪一步"写成一个**可被外部读取**的小文件：

```
<workdir>/state.json
{
  "kind": "episode.state",
  "episodeId": "EF40",
  "stage": "render",
  "ok": null,                      // null=进行中, true/false=已结束
  "startedAt": "...", "updatedAt": "...",
  "elapsedSec": 613.2,
  "stages": [{"stage": "script", "t": 1.2, "at": "..."}],
  "error": ""
}
```

## 三条纪律

1 **原子写**：先写 `.tmp` 再 `replace` —— 外部读到的永远是完整 JSON，
   不会读到"写了一半"的文件（长跑里被读到半截是很常见的）。
2 **只增不改**：`stages` 是追加列表；重复 `mark` 同名阶段只更新时间，不产生重复项。
3 **写失败不阻断**：落盘是观测手段，不该把一集跑挂（异常吞掉但要打印一次）。
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
class Progress:
    """把阶段进度写成 `<workdir>/state.json`。线程安全，写失败不抛。"""

    workdir: Any
    episode_id: str = ""
    _lock: Any = field(default=None, repr=False, compare=False)
    _t0: float = field(default_factory=time.time, repr=False)
    _stages: list[dict] = field(default_factory=list, repr=False)
    _warned: bool = field(default=False, repr=False)
    _finished: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self._lock is None:
            self._lock = threading.Lock()

    # ---- 路径 ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return Path(str(self.workdir)) / "state.json"

    # ---- 写入 ----------------------------------------------------------

    def mark(self, stage: str, **extra: Any) -> None:
        """记一个阶段（同名阶段只更新时间，不重复追加）。"""
        with self._lock:
            now = time.time()
            entry = {"stage": str(stage), "t": round(now - self._t0, 1),
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now))}
            entry.update({k: v for k, v in extra.items()
                          if isinstance(v, (str, int, float, bool))})
            for i, e in enumerate(self._stages):
                if e.get("stage") == entry["stage"]:
                    self._stages[i] = entry
                    break
            else:
                self._stages.append(entry)
            self._write(ok=None, error="")

    def finish(self, *, ok: bool, error: str = "") -> None:
        with self._lock:
            self._finished = True
            self._write(ok=bool(ok), error=str(error)[:500])

    # ---- 内部 ----------------------------------------------------------

    def _payload(self, *, ok: bool | None, error: str) -> dict:
        now = time.time()
        return {
            "kind": "episode.state",
            "episodeId": self.episode_id,
            "stage": (self._stages[-1]["stage"] if self._stages else ""),
            "ok": ok,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(self._t0)),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S",
                                       time.localtime(now)),
            "elapsedSec": round(now - self._t0, 1),
            "stages": list(self._stages),
            "error": error,
        }

    def _write(self, *, ok: bool | None, error: str) -> None:
        try:
            p = self.path
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._payload(ok=ok, error=error),
                                      ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:                              # noqa: BLE001
            # 观测手段不该让一集跑挂；但也不能完全静默（只提示一次）
            if not self._warned:
                self._warned = True
                print(f"  ⚠️ 进度落盘失败（不影响出片）：{type(exc).__name__}: {exc}")


def read_state(workdir: Any) -> dict:
    """读 `<workdir>/state.json`；没有或坏了返回空 dict（不抛）。"""
    p = Path(str(workdir)) / "state.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {}

# ---------------------------------------------------------------------------
# 取消信号（2026-11-12）：**优雅停止**，不是 kill
# ---------------------------------------------------------------------------

#: 取消标记文件名：在 `<workdir>/` 下建这个文件 = 请求停止
CANCEL_FILE = "CANCEL"


def cancel_requested(workdir: Any) -> bool:
    """工作目录里有没有 `CANCEL` 文件。

    为什么用文件而不是信号：一集跑 20~50 分钟，可能换了终端、甚至换了机器；
    信号发不到那个进程，但**文件系统是共享的**。
    语义：看到标记 -> 在当前检查点停下（写半成品报告），不是立刻杀进程。
    """
    try:
        return (Path(str(workdir)) / CANCEL_FILE).is_file()
    except Exception:                                         # noqa: BLE001
        return False


def request_cancel(workdir: Any, reason: str = "") -> str:
    """建取消标记（幂等）。返回标记路径；失败返回空串。"""
    try:
        p = Path(str(workdir)) / CANCEL_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(reason or "manual", encoding="utf-8")
        return str(p)
    except Exception:                                         # noqa: BLE001
        return ""
