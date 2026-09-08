"""会话存储 —— 基于 SQLAlchemy，支持 SQLite / PostgreSQL。

- 默认 SQLite（`db_path`）；设置 `settings.db_url`（如 postgresql://...）即切到 PostgreSQL。
- 表结构用一个 SQL 子集（TEXT/REAL/INTEGER + ON CONFLICT DO UPDATE），SQLite 与 PostgreSQL 通用。

字段：session_id / user_id / user_input / status / phase / payload(JSON) / created / updated
"""

import json
import threading
from typing import Any, Optional

from sqlalchemy import create_engine, text

from app.core.config import settings


class SessionStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = create_engine(url, future=True)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(
                text(
                    """CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        user_id TEXT,
                        user_input TEXT,
                        status TEXT,
                        phase TEXT,
                        payload TEXT,
                        created REAL,
                        updated REAL
                    )"""
                )
            )
            conn.commit()

    def save(self, session_id: str, user_id: str, user_input: str, status: str, phase: str, payload: Optional[Any]) -> None:
        import time

        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        now = time.time()
        with self._lock, self.engine.begin() as conn:
            conn.execute(
                text(
                    """INSERT INTO sessions(session_id, user_id, user_input, status, phase, payload, created, updated)
                       VALUES(:sid, :uid, :uin, :st, :ph, :pl, :created, :updated)
                       ON CONFLICT(session_id) DO UPDATE SET
                         user_id=excluded.user_id, user_input=excluded.user_input, status=excluded.status,
                         phase=excluded.phase, payload=excluded.payload, updated=excluded.updated"""
                ),
                {"sid": session_id, "uid": user_id, "uin": user_input, "st": status, "ph": phase,
                 "pl": payload_json, "created": now, "updated": now},
            )

    def get(self, session_id: str) -> Optional[dict]:
        with self._lock, self.engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM sessions WHERE session_id=:sid"), {"sid": session_id}).fetchone()
        return self._row_to_dict(row) if row else None

    def list_by_user(self, user_id: str) -> list:
        with self._lock, self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM sessions WHERE user_id=:uid ORDER BY created DESC"), {"uid": user_id}
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row) -> dict:
        return {
            "session_id": row[0],
            "user_id": row[1],
            "user_input": row[2],
            "status": row[3],
            "phase": row[4],
            "payload": json.loads(row[5]) if row[5] else None,
            "created": row[6],
            "updated": row[7],
        }

    def delete(self, session_id: str) -> None:
        """删除一条会话/记录。"""
        with self._lock, self.engine.begin() as conn:
            conn.execute(text("DELETE FROM sessions WHERE session_id=:sid"), {"sid": session_id})


_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    """获取（单例）会话存储。默认 SQLite，设置 DB_URL 则用对应数据库。"""
    global _store
    if _store is None:
        url = settings.db_url or f"sqlite:///{settings.db_path_value.as_posix()}"
        _store = SessionStore(url)
    return _store
