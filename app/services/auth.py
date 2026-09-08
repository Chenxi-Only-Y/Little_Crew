"""用户认证 —— 轻量 token 制（开发用，无第三方依赖）。

- 注册：username + password（pbkdf2 加盐哈希）→ 返回 token。
- 登录：校验密码 → 返回既有 token。
- 解析：`Authorization: Bearer <token>` → username。

生产建议：接入真实用户体系（OAuth / 手机号）或 JWT + 刷新；这里是可运行的 MVP 版。
"""

import hashlib
import secrets
import sqlite3
import threading
import time
from typing import Optional

from app.core.config import settings

# token 有效期：90 天（到期后需重新登录）
_TOKEN_TTL = 90 * 24 * 3600


class UserStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    uid TEXT UNIQUE,
                    token TEXT UNIQUE,
                    salt TEXT,
                    hash TEXT,
                    created REAL
                )"""
            )
            # 迁移：老库补 uid 列；对无 uid 的老用户用确定性值回填
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
            if "uid" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN uid TEXT")
            for row in conn.execute("SELECT username FROM users WHERE uid IS NULL"):
                conn.execute(
                    "UPDATE users SET uid=? WHERE username=?",
                    (hashlib.sha256(row[0].encode()).hexdigest()[:12], row[0]),
                )
            # 迁移：补 token_at（最近一次签发 token 的时间），用于过期判定
            if "token_at" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN token_at REAL")
            conn.execute(
                "UPDATE users SET token_at=? WHERE token_at IS NULL",
                (time.time(),),
            )

    @staticmethod
    def _uid() -> str:
        return secrets.token_hex(6)  # 12 位十六进制，足够唯一

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()

    def register(self, username: str, password: str) -> tuple[Optional[str], str]:
        """成功返回 (token, ok)；失败返回 (None, 原因)。"""
        username = (username or "").strip()
        if not username or len(password) < 4:
            return None, "用户名不能为空，密码至少 4 位"
        if self._by_username(username):
            return None, "用户名已存在"
        salt = secrets.token_hex(8)
        token = secrets.token_urlsafe(32)
        uid = self._uid()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO users(username,uid,token,salt,hash,created,token_at) VALUES(?,?,?,?,?,?,?)",
                (username, uid, token, salt, self._hash(password, salt), time.time(), time.time()),
            )
        return token, "ok"

    def login(self, username: str, password: str) -> tuple[Optional[str], str]:
        """成功返回 (token, ok)；失败返回 (None, 原因)。

        每次登录轮换 token 并刷新签发时间（旧 token 失效），配合过期避免长期泄漏。
        """
        user = self._by_username((username or "").strip())
        if not user:
            return None, "用户不存在"
        if self._hash(password, user["salt"]) != user["hash"]:
            return None, "密码错误"
        token = secrets.token_urlsafe(32)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET token=?, token_at=? WHERE username=?",
                (token, time.time(), user["username"]),
            )
        return token, "ok"

    def resolve(self, token: str) -> Optional[str]:
        """token -> username；过期或无效返回 None。"""
        if not token:
            return None
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT username, token_at FROM users WHERE token=?", (token,)
            ).fetchone()
        if not row:
            return None
        if time.time() - (row[1] or 0) > _TOKEN_TTL:
            return None
        return row[0]

    def resolve_uid(self, username: str) -> str:
        """username -> 稳定 UID（用于输出目录等）；匿名/未找到就回退 username。"""
        user = self._by_username(username)
        if user and user.get("uid"):
            return user["uid"]
        return username

    def _by_username(self, username: str) -> Optional[dict]:
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT username,uid,token,salt,hash,created FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return None
        return {"username": row[0], "uid": row[1], "token": row[2], "salt": row[3], "hash": row[4], "created": row[5]}


_store: Optional[UserStore] = None


def get_user_store() -> UserStore:
    """获取（单例）用户存储。"""
    global _store
    if _store is None:
        _store = UserStore(str(settings.db_path_value))
    return _store
