"""运行时 API 配置（持久化到 SQLite + 按用户独立）。

- 存储表：api_settings(user_id PRIMARY KEY, payload JSON)。"_global" 为全局默认。
- 每个用户可覆盖自己的 LLM/图像设置；读优先级：当前用户覆盖 > _global 覆盖 > config 默认。
- 生成时（task_runner）set_user(user_id) 设置「当前线程用户」，llm/image 按该用户读取。
- 持久化在 SQLite，重启保留。
"""

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings as cfg

_lock = threading.Lock()
_local = threading.local()
_global_override: dict = {}
_GLOBAL = "_global"


# ---- 落库加密：密钥文件放 data 目录(gitignored)，增量保护供应商密钥 ----
_SECRET_PATH = Path(str(cfg.db_path_value)).parent / ".little_crew.key"


def _fernet() -> Fernet:
    if _SECRET_PATH.exists():
        key = _SECRET_PATH.read_bytes()
    else:
        key = Fernet.generate_key()
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_PATH.write_bytes(key)
    return Fernet(key)


def _encrypt(s: str) -> str:
    if not s:
        return s
    try:
        return _fernet().encrypt(s.encode("utf-8")).decode("utf-8")
    except Exception:
        return s


def _decrypt(s: str) -> str:
    if not s or not s.strip() or not s.startswith("gAAAA"):
        return s  # 兼容旧的明文存储
    try:
        return _fernet().decrypt(s.encode("utf-8")).decode("utf-8")
    except (InvalidToken, Exception):
        return s


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(cfg.db_path_value))
    c.execute("CREATE TABLE IF NOT EXISTS api_settings(user_id TEXT PRIMARY KEY, payload TEXT)")
    return c


def _load_global() -> None:
    global _global_override
    c = _conn()
    row = c.execute("SELECT payload FROM api_settings WHERE user_id=?", (_GLOBAL,)).fetchone()
    c.close()
    _global_override = json.loads(_decrypt(row[0])) if row else {}


def _user_override(user_id: str) -> dict:
    c = _conn()
    row = c.execute("SELECT payload FROM api_settings WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return json.loads(_decrypt(row[0])) if row else {}


def _merged(user_id: str | None = None) -> dict:
    uid = user_id or user()
    if uid == _GLOBAL:
        return dict(_global_override)
    return dict(_global_override) | _user_override(uid)


def set_user(user_id: str | None) -> None:
    """设置当前线程的用户（供 llm/image 按该用户读取）。"""
    _local.user = user_id or _GLOBAL


def set_context(user_id: str | None, session_id: str | None = None) -> None:
    """设置当前线程的运行上下文：用户 + 会话 ID（供生成/视频按此归类输出目录）。"""
    set_user(user_id)
    _local.session_id = session_id or ""


def run_session() -> str:
    """当前线程正在运行的会话 ID（空串表示无）。"""
    return getattr(_local, "session_id", "")


def user() -> str:
    """当前线程的用户。"""
    return getattr(_local, "user", _GLOBAL)


def get(name: str, default=None):
    """读取（合并后）设置：当前线程用户 > _global > default。"""
    return _merged().get(name, default)


def snapshot(user_id: str | None = None) -> dict:
    """返回某用户（或当前线程）的合并设置。"""
    return _merged(user_id)


def set_many(user_id: str, mapping: dict) -> None:
    """写入某用户的覆盖设置（None 忽略；空串存为覆盖，读取时回退默认）。"""
    data = {k: v for k, v in mapping.items() if v is not None}
    uid = user_id or _GLOBAL
    c = _conn()
    c.execute(
        "INSERT INTO api_settings(user_id,payload) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
        (uid, _encrypt(json.dumps(data, ensure_ascii=False))),
    )
    c.commit()
    c.close()
    if uid == _GLOBAL:
        global _global_override
        _global_override = data


def reset(user_id: str | None = None) -> None:
    uid = user_id or _GLOBAL
    c = _conn()
    c.execute("DELETE FROM api_settings WHERE user_id=?", (uid,))
    c.commit()
    c.close()
    if uid == _GLOBAL:
        global _global_override
        _global_override = {}


_load_global()
