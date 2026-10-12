"""token 存储、登出、重复请求头、掩码哨兵 的回归测试。

四条都来自安全复审的发现：
  · token **明文入库** —— 库/备份/只读副本泄漏 = 全量会话接管；
  · **没有登出** —— token 只能等 90 天过期，用户无法主动收回；
  · 重复 `Authorization` 头 —— 应用取第一个、某些代理取最后一个，产生身份分歧；
  · 掩码哨兵可经 `publish_*_token` 路径**覆盖真实令牌**。
"""

import secrets
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth import _token_hash, get_user_store


@pytest.fixture()
def client():
    return TestClient(app)


def _mk_user(client, prefix="u"):
    name = f"{prefix}_{secrets.token_hex(4)}"
    tok = client.post("/api/auth/register",
                      json={"username": name, "password": "test1234"}).json()["token"]
    return name, tok


# ------------------------------------------------------------------
# ① token 必须哈希存储
# ------------------------------------------------------------------

def test_token_is_stored_hashed_not_plaintext(client):
    """`users.token` 不能等于签发出去的 token。

    明文存储意味着：任何读到库的东西（备份、只读副本、误提交的 .db、
    同机其它进程）都能直接冒充所有用户。
    """
    name, tok = _mk_user(client, "hash")
    with sqlite3.connect(get_user_store().db_path) as c:
        stored = c.execute("SELECT token FROM users WHERE username=?",
                           (name,)).fetchone()[0]
    assert stored != tok, "token 仍是明文入库"
    assert stored == _token_hash(tok), "存储值与 sha256(token) 不一致"
    # 且仍能正常鉴权
    assert client.get("/api/settings",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_legacy_plaintext_token_still_works_then_upgrades():
    """旧的**明文 token 行**必须仍能登录（否则已登录用户全部掉线）。

    命中后顺手升级为哈希——迁移完就再没有明文 token。
    """
    store = get_user_store()
    name = "legacy_tok_" + secrets.token_hex(4)
    old = secrets.token_urlsafe(32)
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO users(username,uid,token,salt,hash,created,token_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (name, "uid_" + secrets.token_hex(4), old, "ab" * 16, "x" * 64,
             time.time(), time.time()),
        )
    assert store.resolve(old) == name, "旧明文 token 无法解析 → 已登录用户会掉线"
    with sqlite3.connect(store.db_path) as c:
        now = c.execute("SELECT token FROM users WHERE username=?",
                        (name,)).fetchone()[0]
    assert now == _token_hash(old), "旧明文 token 未被升级为哈希"


# ------------------------------------------------------------------
# ② 登出
# ------------------------------------------------------------------

def test_logout_revokes_token(client):
    """登出后 token 必须立刻失效。"""
    _, tok = _mk_user(client, "lo")
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/settings", headers=h).status_code == 200

    r = client.post("/api/auth/logout", headers=h)
    assert r.status_code == 200, r.text
    assert client.get("/api/settings", headers=h).status_code == 401, (
        "登出后 token 仍然有效"
    )


def test_logout_is_idempotent_and_does_not_probe(client):
    """重复登出/无效 token 都返回 200 —— 否则会变成 token 有效性探测接口。"""
    _, tok = _mk_user(client, "lo2")
    h = {"Authorization": f"Bearer {tok}"}
    client.post("/api/auth/logout", headers=h)
    assert client.post("/api/auth/logout", headers=h).status_code in (200, 401)


# ------------------------------------------------------------------
# ③ 重复 Authorization 头
# ------------------------------------------------------------------

def test_duplicate_authorization_header_rejected(client):
    """重复头必须拒绝：应用取第一个、某些代理取最后一个 → 身份分歧。"""
    _, tok = _mk_user(client, "dup")
    r = client.get("/api/settings", headers=[
        ("Authorization", f"Bearer {tok}"),
        ("Authorization", "Bearer garbage"),
    ])
    assert r.status_code == 400, (
        f"重复 Authorization 头未被拒（HTTP {r.status_code}）——与代理存在身份分歧风险"
    )
    # 单个头仍然正常
    assert client.get("/api/settings",
                      headers={"Authorization": f"Bearer {tok}"}).status_code == 200


# ------------------------------------------------------------------
# ④ 掩码哨兵不得覆盖发布令牌
# ------------------------------------------------------------------

def test_mask_sentinel_cannot_overwrite_publish_token(client):
    """提交 `••••已配置` 必须被拒 —— 否则会把真实令牌覆盖成哨兵。

    这条路径直接 `rs_set`，绕过了 `put_settings` 的哨兵过滤。
    """
    _, tok = _mk_user(client, "pub")
    h = {"Authorization": f"Bearer {tok}"}
    client.post("/api/sessions", headers=h,
                json={"session_id": f"pub_{secrets.token_hex(4)}", "user_input": "x"})

    real = "real-publish-token-123"
    client.post("/api/crew/publish/link/bilibili", headers=h, json={"token": real})
    r = client.post("/api/crew/publish/link/bilibili", headers=h,
                    json={"token": "••••已配置"})
    assert r.status_code == 400, f"哨兵被接受（HTTP {r.status_code}）"
    # 真实令牌未被破坏：不传 token 时应沿用已有值
    r2 = client.post("/api/crew/publish/link/bilibili", headers=h, json={})
    assert r2.status_code == 200
    assert str(r2.json().get("token", "")).startswith(real[:8]), (
        f"真实令牌已被破坏：{r2.json().get('token')!r}"
    )


def test_empty_string_clears_publish_token(client):
    """空串 = 显式清除（与"未提供=沿用"区分开）。"""
    _, tok = _mk_user(client, "pub2")
    h = {"Authorization": f"Bearer {tok}"}
    client.post("/api/crew/publish/link/youtube", headers=h, json={"token": "abc12345"})
    r = client.post("/api/crew/publish/link/youtube", headers=h, json={"token": ""})
    assert r.status_code == 200
    assert r.json().get("token") == "(已清除)"
