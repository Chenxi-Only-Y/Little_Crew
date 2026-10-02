"""运行时 API 配置（持久化到 SQLite + 按用户独立）。

- 存储表：api_settings(user_id PRIMARY KEY, payload JSON)。"_global" 为全局默认。
- 每个用户可覆盖自己的 LLM/图像设置；读优先级：当前用户覆盖 > _global 覆盖 > config 默认。
- 生成时（task_runner）set_user(user_id) 设置「当前线程用户」，llm/image 按该用户读取。
- 持久化在 SQLite，重启保留。
"""

import json
import sqlite3
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings as cfg

_lock = threading.Lock()
_local = threading.local()
_global_override: dict = {}
_GLOBAL = "_global"

#: 请求作用域的当前用户。
#:
#: 为什么需要它：`threading.local()` **不跨线程传播**。中间件在事件循环线程
#: 设的 `_local.user`，运行在 AnyIO worker 线程里的**同步端点**看不到——
#: 实测表现为 `rs.get("ffmpeg_path")` 在同步端点里返回 None，
#: 用户"明明填了设置却不生效"。`ContextVar` 会随 await 与
#: `run_in_threadpool` 一起传播，正好补上这个缺口。
_request_user: ContextVar[Optional[str]] = ContextVar("lc_request_user", default=None)


def set_request_user(user_id: Optional[str]) -> None:
    """中间件在每个请求开始时调用：把当前用户放进请求作用域。"""
    _request_user.set(user_id or None)


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
    """合并设置。优先级的取值顺序：

    1. 显式传入的 `user_id`
    2. 当前线程用户（`set_user`，生成流程用）
    3. 请求作用域用户（`set_request_user`，中间件设；同步端点靠它）
    4. `_global`

    第 3 条是必须的：同步端点跑在 AnyIO worker 线程里，
    `threading.local()` 传不过去，只有 `ContextVar` 能到。
    """
    uid = user_id or _local_user_or_none() or _request_user.get() or _GLOBAL
    if uid == _GLOBAL:
        return dict(_global_override)
    return dict(_global_override) | _user_override(uid)


def _local_user_or_none() -> Optional[str]:
    """读线程本地用户；**未设置时返回 None**（而不是 `_GLOBAL`）。

    为什么不能直接 `return user()`：`user()` 在未设置时返回 `_GLOBAL`，
    那是个真值，会把请求作用域的用户挡在外面——于是又回到"读不到设置"。
    """
    return getattr(_local, "user", None)


def set_user(user_id: str | None) -> None:
    """设置当前线程的用户；传 `None` 表示**清除**（不是"设为 _global"）。

    ⚠️ 传 None 时必须真的删掉属性：若存成 `_GLOBAL` 这个真值，
    `_local_user_or_none()` 就会返回它，从而**把请求作用域的用户挡在外面**，
    于是"用户填了设置却不生效"。实测踩过这个坑。
    `user()` 在未设置时本来就会回退到 `_GLOBAL`，所以删除是安全的。
    """
    if user_id:
        _local.user = user_id
    else:
        try:
            del _local.user
        except AttributeError:
            pass


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


# ------------------------------------------------------------------
# 上下文跨线程传递
# ------------------------------------------------------------------
# 背景（已实测）：`threading.local` 在 `ThreadPoolExecutor` 里**不会继承**——
# 并发出图时 worker 拿到 `user=_global`、`session=''`，于是产物落到
# `_global/no-session-<shot_id>/`（正是 01-usage §6.2 记录的现象）。
#
# 这里提供最小侵入的修法：提交任务前 `capture()`，在 worker 内 `restore()`。
# 完全替换为显式 `RunContext` 参数是更彻底的做法（08-solutions §2），
# 但会触及大量调用点；先用这一层把行为修正，接口保持不变。


def capture() -> dict:
    """抓取当前线程的上下文（供跨线程恢复）。"""
    return {"user": user(), "session_id": run_session()}


def restore(ctx: dict | None) -> None:
    """在当前线程恢复由 `capture()` 抓取的上下文（None 则不动）。"""
    if not ctx:
        return
    set_context(ctx.get("user"), ctx.get("session_id"))


def get(name: str, default=None):
    """读取（合并后）设置：当前线程用户 > _global > default。"""
    return _merged().get(name, default)


def resolve(name: str, default=None):
    """读取**可被用户覆盖**的运行参数：运行时设置优先，回退到 `config.py` 默认。

    为什么需要它（真实存在过的静默失效）：
        注册表把 `max_images` / `video_max_duration` / `video_resolution` /
        `video_count` / `quality_threshold` / `consistency_threshold` 等声明为
        用户可配置，`/api/settings` 也能存下来；但执行代码读的是
        `settings.<name>`（config.py / .env）。实测：
            PUT {"video_max_duration": 15} → 库里 15，运行时仍是 7
        界面改了、提示"已保存"、**实际完全不生效且不报错**。
        最要命的两例：本地 8GB 的「>10s 必 OOM」保护恒为 7s 而失效；
        `max_images` 设为 3 张仍出 8 张（该键此前**没有任何读取点**）。

    用法：把 `settings.<name>` 换成 `rs.resolve("<name>", settings.<name>)`。
    """
    v = _merged().get(name)
    return default if v is None or v == "" else v


def snapshot(user_id: str | None = None) -> dict:
    """返回某用户（或当前线程）的合并设置。"""
    return _merged(user_id)


def set_many(user_id: str, mapping: dict, *, merge: bool = True) -> None:
    """写入某用户的覆盖设置。

    Args:
        mapping: 要写入的键值。`None` 会被忽略；**空串会存为覆盖**，
                 读取时按 falsy 回退到默认（即"清除该覆盖"的语义）
        merge:   True（默认）与已有配置**合并**，只改动传入的键；
                 False 则整体替换。

    ⚠️ `merge` 默认必须是 True —— 这是一个**实际发生过的数据丢失**：
        属性面板只保存一个字段（如职业级 `image_provider_photographer`）时，
        整体替换会把该用户**其余所有设置清空**。
        设置页之所以没暴露该问题，是因为它一次提交全部字段。
    """
    changes = {k: v for k, v in mapping.items() if v is not None}
    uid = user_id or _GLOBAL

    if merge:
        data = dict(_user_override(uid))
        data.update(changes)
    else:
        data = changes

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
