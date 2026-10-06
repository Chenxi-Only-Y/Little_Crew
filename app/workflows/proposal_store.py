"""提案的**落盘存储** —— 每次提案与审批都留痕（可追溯、可审计、可回滚）。

## 为什么要存

用户要求"每次参与执行需要用户审核"。审核要能**回看**：
  · 这份提案是谁提的、什么时候、针对什么诉求；
  · 当时预演的结果（改了什么、有没有被拒绝的原因）；
  · 谁批的、批了几次（删节点要**二次确认**，所以审批是有阶段的）；
  · 生效后图的版本号是多少（配合 `graph_manager` 可定位/回滚）。

## 存哪

`data/proposals/<id>.json`，一份一个文件。

为什么不用数据库表：提案是**低频、小体积、要能直接看/拷走**的东西
（A3 的代码补丁也要跟它放一起），文件比表更合适。
项目里 `data/` 已经用于运行期产物（`outputs/`、数据库本体），
且这些不该进 git —— 与现有做法一致。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("little_crew")


def proposals_dir() -> Path:
    """提案目录（相对项目根解析，与数据库同处 `data/` 下）。"""
    from app.core.config import settings

    root = Path(str(settings.db_path_value)).parent
    d = root / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_id() -> str:
    return f"p-{uuid.uuid4().hex[:10]}"


def _path(pid: str) -> Path:
    # 防目录穿越：id 只允许我们生成的字符
    safe = "".join(ch for ch in (pid or "") if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("提案 id 非法")
    return proposals_dir() / f"{safe}.json"


def save(record: dict) -> dict:
    """写一份提案记录（整份覆盖）。"""
    pid = record.get("id") or new_id()
    record["id"] = pid
    record.setdefault("created", time.time())
    record["updated"] = time.time()
    p = _path(pid)
    tmp = p.with_suffix(".json.tmp")
    # 先写临时文件再替换：避免进程中途挂掉留下半截 JSON
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return record


def load(pid: str) -> dict | None:
    p = _path(pid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[proposal] 读取 %s 失败：%s", pid, exc)
        return None


def list_all(limit: int = 50) -> list[dict]:
    """按时间倒序列出提案（新的在前）。"""
    out: list[dict] = []
    for f in proposals_dir().glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return out[:limit]


def append_event(pid: str, event: str, detail: dict | None = None,
                 actor: str = "") -> dict | None:
    """给提案追加一条事件（提出/预览/审批/生效/回滚…）。

    用**事件流**而不是几个布尔字段：审批是有阶段的
    （删节点要二次确认 → 至少两次「通过」），布尔字段表达不了这个过程。
    """
    rec = load(pid)
    if rec is None:
        return None
    rec.setdefault("events", []).append({
        "at": time.time(),
        "event": event,
        "actor": actor,
        "detail": detail or {},
    })
    return save(rec)


def as_public(rec: dict) -> dict:
    """给接口/界面用的形状（去掉可能很大的原始输出）。"""
    return {
        "id": rec.get("id"),
        "summary": rec.get("summary"),
        "author": rec.get("author"),
        "created": rec.get("created"),
        "updated": rec.get("updated"),
        "status": rec.get("status"),
        "needs_second_confirm": rec.get("needs_second_confirm"),
        "removes_nodes": rec.get("removes_nodes") or [],
        "ops": rec.get("ops") or [],
        "risks": rec.get("risks") or [],
        "preview": rec.get("preview") or {},
        "events": rec.get("events") or [],
        "graph_version": rec.get("graph_version"),
        "patch_path": rec.get("patch_path"),
    }


__all__ = ["proposals_dir", "new_id", "save", "load", "list_all",
           "append_event", "as_public"]
