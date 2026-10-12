"""HTTP 接口层 —— 平台的**对外服务面**。

为什么用标准库而不是 FastAPI
----------------------------
`pyproject.toml` 声明 `dependencies = []`，而这是**刻意的架构约束**
（见 `ARCHITECTURE.md` 依赖铁律）：核心层必须能独立运行、独立测试。

`http.server` 足以支撑本平台的接口形态：

| 需求 | 标准库能否满足 |
|---|---|
| JSON 请求/响应 | ✅ |
| 长任务 + 轮询进度 | ✅（任务表 + `GET /v1/runs/{id}`） |
| Server-Sent Events 进度流 | ✅（`text/event-stream` 就是长连接写文本） |
| 并发 | ✅ `ThreadingHTTPServer` 够用 |
| WebSocket | ❌ **不用** —— 本平台进度是单向推送，SSE 已够 |

**不用 WebSocket 是刻意的**：进度是**单向**的，
SSE 能用标准库实现，而 WebSocket 需要引依赖或手写握手。
架构一致性 > 技术时髦。

接口形态
--------
| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/healthz` | 存活检查（**不碰外部服务**） |
| `GET` | `/v1/env` | 环境探测（两个通道 + 能力矩阵摘要） |
| `POST` | `/v1/compile` | 编译 IR（**不提交**） |
| `POST` | `/v1/shot` | 跑单镜 |
| `POST` | `/v1/episode` | 跑一集（**异步**，返回 runId） |
| `GET` | `/v1/runs` | 列出任务 |
| `GET` | `/v1/runs/{id}` | 查任务状态与结果 |
| `GET` | `/v1/runs/{id}/events` | **SSE 进度流** |

异步的理由
----------
跑一集的墙钟是**分钟级**（实测 20 镜约 5 分钟）。
同步接口会让客户端超时、让重试变成"重复烧 GPU"。

**长任务必须异步** —— 这是本平台最重要的一条接口设计。
"""

from __future__ import annotations

from little_crew.api.server import ApiServer, RunRegistry, RunState, serve

__all__ = ["ApiServer", "RunRegistry", "RunState", "serve"]
