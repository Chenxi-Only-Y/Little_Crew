"""渲染并发策略：**本地串行，云端才允许并发**（2026-11-12）。

## 依据（真机实测，不是拍的）

同一批 4 个真实 IR、两轮各用独立缓存目录（确保都真算）：

| 方式 | 墙钟 | 单张均值 |
|---|---|---|
| 串行 | **40.7 s** | **10.2 s** |
| 并发 2 路 | 35.3 s（1.15×） | 15.5 s（+52%，GPU 争用） |

结论：本地并发只有 **1.15×**，而且单张成本明显上升 —— 这个量级**不值得**
增加复杂度和不确定性。所以：

- **本地（ComfyUI / 本机 HTTP）一律串行**（不提供开关，避免"不小心开了"）
- **云端 HTTP** 才允许 `LITTLE_CREW_RENDER_PARALLEL=N`（默认 1 = 串行）

## 为什么云端可以试

云端渲染是**远端排队**，本地只是发请求 + 等结果（I/O 密集）。
provider 侧对短请求的并发实测有 **2.31×**（4 并发 1.9s vs 串行 1.1s/次），
与本地"抢同一块 GPU"是两种情形。

⚠️ **诚实标注**：云端并发这条路**还没有真跑过**（没有云 key），
策略与开关已实现并有单测；首次接云时必须先量一轮再决定默认值。
"""

from __future__ import annotations

import os
from typing import Any, Mapping

#: 云端并发度环境变量（本地忽略）
ENV_PARALLEL = "LITTLE_CREW_RENDER_PARALLEL"


def is_local(backend: Any) -> bool:
    """这个后端是不是"本机"。

    判据取两条里任一成立即算本地：
    1 声明 `is_local_by_default=True`（模型注册表里的显式标注）
    2 端点指向本机回环地址（`127.0.0.1` / `localhost` / `::1`）
    """
    if backend is None:
        return True
    if bool(getattr(backend, "is_local_by_default", False)):
        return True
    ep = str(getattr(backend, "endpoint", "") or "").lower()
    if not ep:
        return True
    return ("127.0.0.1" in ep or "localhost" in ep or "[::1]" in ep)


def render_workers(backend: Any, env: Mapping[str, str] | None = None) -> int:
    """该后端允许的并发度（1 = 串行）。

    · 本地后端 -> 恒为 1（**不接受环境变量覆盖**）
    · 云端后端 -> 读 `LITTLE_CREW_RENDER_PARALLEL`，默认 1，上限 4
    """
    if is_local(backend):
        return 1
    e = dict(env if env is not None else os.environ)
    try:
        n = int(str(e.get(ENV_PARALLEL) or 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, min(4, n))


def explain(backend: Any, env: Mapping[str, str] | None = None) -> str:
    """一句话说明当前策略（用于启动日志与报告）。"""
    w = render_workers(backend, env)
    if is_local(backend):
        return "渲染并发 1（本地后端强制串行：实测 1.15×，不值得）"
    if w <= 1:
        return f"渲染并发 1（云端后端，可用 {ENV_PARALLEL} 开 2~4；**未实测**）"
    return (f"渲染并发 {w}（云端后端，由 {ENV_PARALLEL} 指定；**未实测**）")
