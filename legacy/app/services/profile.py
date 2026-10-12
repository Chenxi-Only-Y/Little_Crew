"""用户个人资料 —— 显示名(ID) + 头像，持久化到 SQLite。

表：profiles(username PRIMARY KEY, display_name TEXT, avatar TEXT, avatar_color TEXT, updated REAL)
avatar：base64 data URI（前端上传图片转成）；avatar_color：头像底色（可选，用于字母头像）。
"""

import base64
import sqlite3
import threading
import time
from typing import Optional

from app.core.config import settings


class ProfileStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS profiles (
                    username TEXT PRIMARY KEY,
                    display_name TEXT,
                    avatar TEXT,
                    avatar_color TEXT,
                    updated REAL
                )"""
            )

    def get(self, username: str) -> dict:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT display_name,avatar,avatar_color FROM profiles WHERE username=?", (username,)
            ).fetchone()
        if not row:
            return {"username": username, "display_name": username, "avatar": None, "avatar_color": None}
        return {"username": username, "display_name": row[0] or username, "avatar": row[1], "avatar_color": row[2]}

    def set(self, username: str, display_name: Optional[str] = None,
            avatar: Optional[str] = None, avatar_color: Optional[str] = None) -> dict:
        cur = self.get(username)
        display_name = display_name if display_name is not None else cur["display_name"]
        avatar = avatar if avatar is not None else cur["avatar"]
        avatar_color = avatar_color if avatar_color is not None else cur["avatar_color"]
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO profiles(username,display_name,avatar,avatar_color,updated) VALUES(?,?,?,?,?)
                   ON CONFLICT(username) DO UPDATE SET display_name=excluded.display_name,
                     avatar=excluded.avatar, avatar_color=excluded.avatar_color, updated=excluded.updated""",
                (username, display_name, avatar, avatar_color, time.time()),
            )
        return {"username": username, "display_name": display_name, "avatar": avatar, "avatar_color": avatar_color}


_store: Optional[ProfileStore] = None


def get_profile_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore(str(settings.db_path_value))
    return _store


def make_data_uri(data: bytes, mime: str) -> str:
    """把字节流转成 base64 data URI（用于存头像）。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
