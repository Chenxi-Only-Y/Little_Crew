"""剧评(文学策划)修改历史 —— 跨会话持久化到 SQLite。

表：critic_history(session_id TEXT PRIMARY KEY, user_id TEXT, history TEXT, updated REAL)
history：JSON 数组，每条为一次审查记录：
    {"time": float, "overall_score": 0-100, "logic": ..., "tension": ..., "motive": ...,
     "note": "mock 评审 | DeepSeek 评审"}

用途：让「文学策划」职业页的『修改历史』标签页跨会话保留历次审查，而非仅限当前会话。
"""

import json
import sqlite3
import threading
import time
from typing import List, Optional

from app.core.config import settings


class CriticHistoryStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS critic_history (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    history TEXT,
                    updated REAL
                )"""
            )

    def get(self, session_id: str) -> List[dict]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT history FROM critic_history WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row or not row[0]:
            return []
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return []

    def append(self, session_id: str, user_id: str, entry: dict, cap: int = 50) -> List[dict]:
        """向某会话追加一条审查历史，返回最新列表。"""
        history = self.get(session_id)
        history.append(entry)
        history = history[-cap:]
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO critic_history(session_id,user_id,history,updated) VALUES(?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET user_id=excluded.user_id,
                     history=excluded.history, updated=excluded.updated""",
                (session_id, user_id, json.dumps(history, ensure_ascii=False), time.time()),
            )
        return history

    def set(self, session_id: str, user_id: str, history: list, cap: int = 50) -> List[dict]:
        """覆盖某会话的审查历史（用于从 state 同步）。"""
        history = list(history or [])[-cap:]
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO critic_history(session_id,user_id,history,updated) VALUES(?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET user_id=excluded.user_id,
                     history=excluded.history, updated=excluded.updated""",
                (session_id, user_id, json.dumps(history, ensure_ascii=False), time.time()),
            )
        return history


_store: Optional[CriticHistoryStore] = None


def get_critic_history_store() -> CriticHistoryStore:
    global _store
    if _store is None:
        _store = CriticHistoryStore(str(settings.db_path_value))
    return _store
