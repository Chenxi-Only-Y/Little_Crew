"""认证加固的回归测试。

加固本身容易"加固成事故"：改了 PBKDF2 参数后，**旧哈希用新参数算不出来**，
已部署库上的用户会突然全部"密码错误"，而且没有任何提示。
所以这里重点锁住"加固不能把老用户锁在门外"。
"""

import hashlib
import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import UserStore


@pytest.fixture()
def client():
    return TestClient(app)


def _mk_legacy_user(store: UserStore, name: str, password: str,
                    iterations: int, salt_bytes: int) -> str:
    """直接插一个"旧参数"用户行，返回 salt。"""
    salt = secrets.token_hex(salt_bytes)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(),
                            bytes.fromhex(salt), iterations).hex()
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO users(username,uid,token,salt,hash,created,token_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, "uid_" + secrets.token_hex(4), secrets.token_urlsafe(32),
             salt, h, time.time(), time.time()),
        )
    return salt


def test_legacy_hash_still_logs_in():
    """旧迭代次数（100k）的哈希必须仍能登录。

    改动 PBKDF2 迭代数时若不做兼容，**所有既有用户会被锁在门外**。
    """
    store = UserStore.__new__(UserStore)
    from app.services.auth import get_user_store

    store = get_user_store()
    name = "legacy_" + secrets.token_hex(4)
    pw = "oldpass1234"
    _mk_legacy_user(store, name, pw, iterations=100_000, salt_bytes=8)

    token, msg = store.login(name, pw)
    assert token, f"旧哈希用户登录失败（msg={msg}）——加固把老用户锁在门外了"


def test_legacy_hash_is_upgraded_on_login():
    """登录命中旧参数时，应当顺手把哈希升级到当前参数。"""
    from app.services.auth import get_user_store

    store = get_user_store()
    name = "upg_" + secrets.token_hex(4)
    pw = "oldpass1234"
    salt = _mk_legacy_user(store, name, pw, iterations=100_000, salt_bytes=8)

    assert store.login(name, pw)[0], "首次登录应成功"
    with sqlite3.connect(store.db_path) as c:
        stored = c.execute("SELECT hash FROM users WHERE username=?",
                           (name,)).fetchone()[0]
    expect = hashlib.pbkdf2_hmac(
        "sha256", pw.encode(), bytes.fromhex(salt),
        UserStore._PBKDF2_ITERATIONS).hex()
    assert stored == expect, "旧哈希未被升级到当前参数"
    assert store.login(name, pw)[0], "升级后仍应能登录"


def test_unknown_user_and_wrong_password_give_same_message():
    """两种失败必须同一句话 —— 否则是账号枚举 oracle。"""
    from app.services.auth import get_user_store

    store = get_user_store()
    name = "enum_" + secrets.token_hex(4)
    store.register(name, "rightpass1234")

    _, wrong_pw = store.login(name, "definitely-wrong")
    _, no_user = store.login("no_such_user_" + secrets.token_hex(4), "whatever")
    assert wrong_pw == no_user, (
        f"文案可区分（错密码={wrong_pw!r} 不存在={no_user!r}）→ 账号枚举"
    )


def test_password_verification_is_nearly_constant_time():
    """密码比较必须用 `hmac.compare_digest`（常量时间）。

    这是读源码断言：`!=` 会按字节短路比较，泄漏"前几位对不对"的时序信息。
    """
    import inspect

    from app.services import auth as auth_mod

    src = inspect.getsource(auth_mod.UserStore.login)
    assert "compare_digest" in src, "登录未使用常量时间比较"


def test_pbkdf2_iterations_are_modern():
    """迭代次数不能退回低位（防止有人"为了快"改回去）。"""
    assert UserStore._PBKDF2_ITERATIONS >= 600_000, (
        f"PBKDF2 迭代次数过低：{UserStore._PBKDF2_ITERATIONS}"
    )
    assert 100_000 in UserStore._LEGACY_ITERATIONS, (
        "必须保留历史迭代次数，否则旧库用户无法登录"
    )


def test_register_uses_16_byte_salt(client):
    """新注册用户的 salt 应为 16 字节（32 个 hex 字符）。"""
    import uuid

    name = f"salt_{uuid.uuid4().hex[:8]}"
    r = client.post("/api/auth/register",
                    json={"username": name, "password": "test1234"})
    assert r.status_code == 200 and r.json().get("status") == "ok", r.text

    from app.services.auth import get_user_store

    with sqlite3.connect(get_user_store().db_path) as c:
        salt = c.execute("SELECT salt FROM users WHERE username=?",
                         (name,)).fetchone()[0]
    assert len(salt) == 32, f"salt 长度 {len(salt)} 个 hex 字符（应 32 = 16 字节）"
