"""provider 指纹：**接云 API 之前必须先有的东西**（2026-11-06，§4.6.6 第 22 条）。

## 为什么

本机模型可复现（同种子字节相同）。接云以后：

```
模型会静默升级 · 厂商会用别名路由 · seed 不一定生效
-> 同样的输入，明天出不同的东西
-> **能力越强，可证明性越弱**
```

## 记什么

| 字段 | 为什么 |
|---|---|
| `provider` / `model` | 我请求的是谁 |
| **`servedModel`** | 服务端**实际**服务的模型（实测会遇到别名路由） |
| `params`（seed/temperature/max_tokens…） | 复现的关键 |
| **`requestHash`** | 证明"我当时发的就是这个 prompt" |
| `responseId` / `created` | 事后对账 |
| `artifactHashes` | 已有（`delivery.evidence` 里） |

## 纪律

- **不记录明文 prompt**（体积 + 隐私）；只记 hash
- **不静默缺失**：没拿到 `servedModel` 就写空，并在汇总里报"缺服务端模型名"
"""
from __future__ import annotations

import hashlib
import threading
import time

#: 全局记录器（进程内累积，episode 结束时落盘）
_LOCK = threading.Lock()
_RECORDS: list[dict] = []


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def make(*, kind: str, provider: str = "", model: str = "",
         served_model: str = "", params=None, request: str = "",
         response_id: str = "", created: float = 0.0) -> dict:
    """造一条指纹。**纯函数**（除了取当前时间）。"""
    return {
        "kind": str(kind or ""),
        "provider": str(provider or ""),
        "model": str(model or ""),
        "servedModel": str(served_model or ""),
        "params": dict(params or {}),
        "requestHash": _hash(request) if request else "",
        "responseId": str(response_id or ""),
        "created": float(created or time.time()),
    }


def note(record: dict) -> dict:
    """记一条（线程安全）。返回该条。"""
    with _LOCK:
        _RECORDS.append(dict(record))
    return record


def note_reply(*, kind: str, provider: str = "", model: str = "",
               served_model: str = "", params=None, request: str = "",
               response_id: str = "") -> dict:
    return note(make(kind=kind, provider=provider, model=model,
                     served_model=served_model, params=params,
                     request=request, response_id=response_id))


def taken() -> list[dict]:
    with _LOCK:
        return [dict(x) for x in _RECORDS]


def reset() -> None:
    with _LOCK:
        _RECORDS.clear()


def summary(records=None) -> dict:
    """按 provider+model 聚合，给出"这一集到底用了哪些模型"。"""
    rows = list(records if records is not None else taken())
    agg: dict[tuple[str, str], dict] = {}
    no_served = 0
    for r in rows:
        key = (r.get("provider") or "?", r.get("model") or "?")
        a = agg.setdefault(key, {"provider": key[0], "model": key[1],
                                 "calls": 0, "servedModels": {}, "kinds": {}})
        a["calls"] += 1
        sm = r.get("servedModel") or ""
        if sm:
            a["servedModels"][sm] = a["servedModels"].get(sm, 0) + 1
        else:
            no_served += 1
        k = r.get("kind") or "?"
        a["kinds"][k] = a["kinds"].get(k, 0) + 1
    out = sorted(agg.values(), key=lambda x: (-x["calls"], x["model"]))
    return {
        "kind": "provider.fingerprint",
        "total": len(rows),
        "providers": out,
        "missingServedModel": no_served,
        "records": rows,
    }
