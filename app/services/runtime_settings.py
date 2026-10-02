"""运行时 API 配置（持久化到 SQLite + 按用户独立）。

- 存储表：api_settings(user_id PRIMARY KEY, payload JSON)。"_global" 为全局默认。
- 每个用户可覆盖自己的 LLM/图像设置；读优先级：当前用户覆盖 > _global 覆盖 > config 默认。
- 生成时（task_runner）set_user(user_id) 设置「当前线程用户」，llm/image 按该用户读取。
- 持久化在 SQLite，重启保留。
"""

import json
import logging
import os
import sqlite3
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings as cfg

logger = logging.getLogger("little_crew")

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
_fernet_lock = threading.Lock()
_fernet_cache: Optional[Fernet] = None


class DecryptError(RuntimeError):
    """密文无法解开（密钥换了 / 丢了 / 数据损坏）。

    单独开一个异常类型，是为了**不要让它混进 `json.loads`**——
    原先解密失败会原样返回密文，接着 `json.loads(密文)` 抛
    `JSONDecodeError`，而这个调用发生在模块导入期，于是
    **整个服务起不来**，报错还完全指不到"密钥不对"这个真因。
    """


def _fernet() -> Fernet:
    """取（并缓存）Fernet 实例。

    两处原来的问题：
      - 每次调用都读一遍密钥文件；
      - 密钥不存在时**并发写**（`write_bytes` 不是原子的），
        两个进程可能各自生成一把、后者覆盖前者，先写的那些密文随后就解不开了。
    这里加锁 + 存在性复查，并用 0o600 落盘（原先用默认权限，
    Linux 下同机其他用户可读走它、进而解开库里所有 API key）。
    """
    global _fernet_cache
    if _fernet_cache is not None:
        return _fernet_cache
    with _fernet_lock:
        if _fernet_cache is not None:      # 双检：可能已被别的线程建好
            return _fernet_cache
        if _SECRET_PATH.exists():
            key = _SECRET_PATH.read_bytes().strip()
            try:
                _fernet_cache = Fernet(key)
            except Exception as exc:  # noqa: BLE001
                raise DecryptError(
                    f"密钥文件不可用：{_SECRET_PATH}（{type(exc).__name__}）。"
                    "请确认它是 Fernet 生成的 44 字节 base64 密钥。"
                ) from exc
            return _fernet_cache

        key = Fernet.generate_key()
        _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 先原子创建（O_EXCL）再写内容：避免"检查后写入"的竞态。
        # 若已被别人抢先创建，就改用已有那把，绝不覆盖。
        try:
            fd = os.open(str(_SECRET_PATH), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
        except FileExistsError:
            key = _SECRET_PATH.read_bytes().strip()
        _fernet_cache = Fernet(key)
        return _fernet_cache


def _encrypt(s: str) -> str:
    """加密。**失败必须抛**——绝不能静默退回明文。

    原先 `except Exception: return s` 会在加密器不可用（如密钥文件损坏）时
    把 API key **以明文写进数据库**，而且是静默的：调用方以为已经加密。
    宁可让这次保存失败（用户看到错误），也不要把凭据明文落盘。
    """
    if not s:
        return s
    try:
        return _fernet().encrypt(s.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise DecryptError(
            f"加密失败，已中止保存（不写明文）：{type(exc).__name__}: {exc}"
        ) from exc


def _decode_payload_checked(stored: Optional[str], *, who: str) -> tuple[dict, bool]:
    """同 `_decode_payload`，但额外返回"是否**成功读到**了原数据"。

    `readable=False` 表示：库里**有**数据，但解不开/不是合法 JSON。
    调用方（`set_many`）据此决定**不要覆盖**——那份数据修好密钥后还能救回来。
    缺少这个区分就会出现"打开一次设置页，就把读不懂的历史数据永久清掉"。
    """
    if not stored:
        return {}, True          # 本来就没有数据 → 不是"读不懂"
    try:
        text = _decrypt(stored)
    except DecryptError as exc:
        logger.error(
            "[settings] %s 的设置解不开（密钥与数据不匹配？）：%s。"
            "本次按空设置运行；**不会覆盖原数据**，修复密钥后即可恢复。",
            who, exc,
        )
        return {}, False
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        logger.error("[settings] %s 的设置不是合法 JSON：%s", who, exc)
        return {}, False
    if not isinstance(data, dict):
        logger.error("[settings] %s 的设置不是对象（%s），按空处理", who, type(data).__name__)
        return {}, False
    return data, True


def _decode_payload(stored: Optional[str], *, who: str) -> dict:
    """把库里存的 payload 解成 dict；解不开时返回 `{}`（**降级但不崩**）。

    ⚠️ 这里的取舍很关键：密文解不开时，
      - 不能抛——它在导入期被调用，抛了就是**服务起不来**；
      - 不能返回密文——`json.loads(密文)` 同样抛；
      - 返回 `{}` 但**必须留下可诊断的日志**，否则就成了"设置莫名全没了"。
    再配上 `set_many` 的"读不懂就不写"，数据不会因为一次读取失败而丢失。
    """
    return _decode_payload_checked(stored, who=who)[0]


def _decrypt(s: str) -> str:
    """解密；失败抛 `DecryptError`（不再静默返回密文）。"""
    if not s or not s.strip():
        return s
    if not s.startswith("gAAAA"):
        return s  # 兼容旧的明文存储
    try:
        return _fernet().decrypt(s.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise DecryptError(f"{type(exc).__name__}: {exc}") from exc


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(cfg.db_path_value))
    c.execute("CREATE TABLE IF NOT EXISTS api_settings(user_id TEXT PRIMARY KEY, payload TEXT)")
    return c


def _load_global() -> None:
    global _global_override
    c = _conn()
    row = c.execute("SELECT payload FROM api_settings WHERE user_id=?", (_GLOBAL,)).fetchone()
    c.close()
    _global_override = _decode_payload(row[0] if row else None, who="_global")


def _user_override(user_id: str) -> dict:
    c = _conn()
    row = c.execute("SELECT payload FROM api_settings WHERE user_id=?", (user_id,)).fetchone()
    c.close()
    return _decode_payload(row[0] if row else None, who=user_id)


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

    c = _conn()
    try:
        row = c.execute("SELECT payload FROM api_settings WHERE user_id=?", (uid,)).fetchone()
        if merge:
            data, readable = _decode_payload_checked(row[0] if row else None, who=uid)
            if not readable:
                # 原有数据还在但**解不开**（密钥不匹配/损坏）。
                # 此时若照常"合并写回"，就等于用只含本次改动的内容
                # **永久覆盖**那份可能还能救回来的数据（修好密钥即可恢复）。
                # 所以拒绝写入，让用户先去修密钥。
                raise DecryptError(
                    f"{uid} 的已有设置无法解密，为避免覆盖原数据，本次保存已中止。"
                    "请先修复密钥文件（.little_crew.key）——"
                    "数据仍在库中，密钥恢复后即可读回。"
                )
            data.update(changes)
        else:
            data = changes

        c.execute(
            "INSERT INTO api_settings(user_id,payload) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
            (uid, _encrypt(json.dumps(data, ensure_ascii=False))),
        )
        c.commit()
    finally:
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
