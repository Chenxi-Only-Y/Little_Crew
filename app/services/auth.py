"""用户认证 —— 轻量 token 制（开发用，无第三方依赖）。

- 注册：username + password（pbkdf2 加盐哈希）→ 返回 token。
- 登录：校验密码 → 返回既有 token。
- 解析：`Authorization: Bearer <token>` → username。

生产建议：接入真实用户体系（OAuth / 手机号）或 JWT + 刷新；这里是可运行的 MVP 版。
"""

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from typing import Optional

from app.core.config import settings

logger = logging.getLogger("little_crew")

# token 有效期：90 天（到期后需重新登录）
_TOKEN_TTL = 90 * 24 * 3600

#: 允许的用户名：字母/数字/下划线/连字符 + 中日韩字符；2~32 位。
#:
#: ⚠️ 为什么必须限制：用户名是**身份字符串**，而身份又被直接当作
#: `runtime_settings` 的存储键（`api_settings.user_id`）。原先不限制，
#: 于是可以注册一个叫 `_global` 的用户 —— 而 `_GLOBAL = "_global"` 是
#: "全员默认设置"那行的哨兵键。实测：注册 `_global` 后 `PUT /api/settings`
#: 会写到全局行，**另一个无辜用户读到的就是攻击者的 `llm_base_url`**
#: ——受害者会带着自己的 API Key 把请求发到攻击者的服务器。
#: 同理要挡住路径穿越类名字（`../evil`），它们会被用作输出目录名。
_USERNAME_RE = re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fff]{2,32}")

#: 保留用户名：与内部哨兵/目录名冲突，绝不能注册。
_RESERVED_USERNAMES = frozenset({
    "_global", "global", "local", "anonymous", "system", "root", "admin",
    "none", "null", "undefined",
})


def _token_hash(token: str) -> str:
    """token 的存储形态：sha256 十六进制。

    ⚠️ 为什么不能明文存 token：token 就是**长期凭据**（90 天有效），
    而 `users.token` 是明文列。任何能读到库的东西——备份、只读副本、
    误提交的 .db 文件、同机的其它进程——都能直接拿去冒充所有用户。
    存哈希后，读到库也**无法还原出可用 token**。

    为什么用 sha256 而不是再上 PBKDF2：token 本身是
    `secrets.token_urlsafe(32)`（256 bit 真随机），不存在"弱口令"可猜，
    所以不需要慢哈希；用快哈希还能让每次请求的鉴权不走昂贵计算。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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

    #: 当前 PBKDF2 迭代次数。2026 年的合理下界；提高它只影响注册/登录一次。
    _PBKDF2_ITERATIONS = 600_000

    #: 历史迭代次数（按从新到旧排列）。
    #:
    #: ⚠️ 为什么必须保留：改了迭代次数后，**旧的哈希用新参数是算不出来的**
    #: ——已部署的库上每个用户都会突然"密码错误"，而且没有任何报错提示原因。
    #: 登录时按这几个值依次验证，命中旧值时顺手把哈希升级到当前参数
    #: （用户无感，只多花一次哈希的时间）。这样"加固"不会变成"把所有人锁在门外"。
    _LEGACY_ITERATIONS = (100_000,)

    @classmethod
    def _hash(cls, password: str, salt: str,
              iterations: Optional[int] = None) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt),
            iterations or cls._PBKDF2_ITERATIONS,
        ).hex()

    def register(self, username: str, password: str) -> tuple[Optional[str], str]:
        """成功返回 (token, ok)；失败返回 (None, 原因)。"""
        username = (username or "").strip()
        if not username or len(password) < 4:
            return None, "用户名不能为空，密码至少 4 位"
        # 用户名格式与保留名（见 `_USERNAME_RE` / `_RESERVED_USERNAMES` 的说明）
        if not _USERNAME_RE.fullmatch(username):
            return None, ("用户名只能包含字母、数字、下划线、连字符或中文，"
                          "长度 2~32 位")
        if username.lower() in _RESERVED_USERNAMES:
            return None, "该用户名为系统保留，请换一个"
        if self._by_username(username):
            return None, "用户名已存在"
        salt = secrets.token_hex(16)      # 16 字节（原为 8）
        token = secrets.token_urlsafe(32)
        uid = self._uid()
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO users(username,uid,token,salt,hash,created,token_at) VALUES(?,?,?,?,?,?,?)",
                (username, uid, _token_hash(token), salt,
                 self._hash(password, salt), time.time(), time.time()),
            )
        return token, "ok"

    def login(self, username: str, password: str) -> tuple[Optional[str], str]:
        """成功返回 (token, ok)；失败返回 (None, 原因)。

        每次登录轮换 token 并刷新签发时间（旧 token 失效），配合过期避免长期泄漏。

        ⚠️ 两种失败**必须返回同一句话**：区分"用户不存在"与"密码错误"
        等于白送一个账号枚举 oracle，而枚举又是后续越权攻击的前置条件。
        同理，密码比较用 `hmac.compare_digest`（常量时间），
        避免按字节比较泄漏"前几位对不对"的时序信息。
        """
        #: 统一的失败文案（故意不区分原因）
        fail = "用户名或密码错误"
        user = self._by_username((username or "").strip())
        if not user:
            # 仍然做一次哈希：否则"不存在的用户"会明显更快，同样是 oracle
            self._hash(password, secrets.token_hex(16))
            return None, fail

        salt, stored = user["salt"], user["hash"]
        # 先按当前参数验证；不匹配再依次试历史参数（兼容已部署的库）。
        # 命中旧参数时顺手把哈希升级——用户无感，只多花一次哈希。
        matched_legacy = False
        ok = hmac.compare_digest(self._hash(password, salt), stored)
        if not ok:
            for iters in self._LEGACY_ITERATIONS:
                if hmac.compare_digest(self._hash(password, salt, iters), stored):
                    ok = True
                    matched_legacy = True
                    break
        if not ok:
            return None, fail

        token = secrets.token_urlsafe(32)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET token=?, token_at=? WHERE username=?",
                (_token_hash(token), time.time(), user["username"]),
            )
            if matched_legacy:
                # 顺带升级到当前参数（下次登录即走新路径）
                conn.execute(
                    "UPDATE users SET hash=? WHERE username=?",
                    (self._hash(password, salt), user["username"]),
                )
                logger.info("[auth] 已把用户 %s 的密码哈希升级到当前参数",
                            user["username"])
        return token, "ok"

    def logout(self, token: str) -> bool:
        """吊销一个 token（登出）。返回是否确实清掉了一行。

        原先**没有登出**：token 一旦签发就只能等 90 天过期，
        用户没有任何主动收回的手段。
        """
        if not token:
            return False
        th = _token_hash(token)
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE users SET token=NULL, token_at=NULL "
                "WHERE token IN (?, ?)", (th, token),
            )
            return cur.rowcount > 0

    def resolve(self, token: str) -> Optional[str]:
        """token -> username；过期或无效返回 None。

        同时兼容**旧的明文 token 行**：老库里的 token 是明文，
        直接改成哈希会让所有已登录用户突然掉线。命中明文行时顺手把它
        升级成哈希（用户无感），迁移完就再也没有明文 token 了。
        """
        if not token:
            return None
        th = _token_hash(token)
        row = None
        legacy = False
        with self._lock, sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT username, token_at FROM users WHERE token=?", (th,)
            ).fetchone()
            if not row:
                # 兼容旧明文行
                row = conn.execute(
                    "SELECT username, token_at FROM users WHERE token=?", (token,)
                ).fetchone()
                if row:
                    legacy = True
                    conn.execute(
                        "UPDATE users SET token=? WHERE token=?", (th, token)
                    )
        if not row:
            return None
        if time.time() - (row[1] or 0) > _TOKEN_TTL:
            return None
        if legacy:
            logger.info("[auth] 已把用户 %s 的 token 升级为哈希存储", row[0])
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
