"""身份相关的越权回归测试。

两条都来自安全复审的**动态复现**，都是真实可利用的：

1. **用户名撞全局哨兵** —— `_GLOBAL = "_global"` 是"全员默认设置"那行的键，
   而注册对用户名不设限。注册一个叫 `_global` 的用户后，`PUT /api/settings`
   会写进全局行，**另一个无辜用户读到的就是攻击者的 `llm_base_url`** ——
   受害者会带着自己的 API Key 把请求发到攻击者服务器。
2. **`seed` 直通 LangGraph State** —— `state.update(seed)` 能覆盖服务端刚设好的
   `run_context`，于是攻击者可传 `{"run_context": {"user_id": "受害者"}}`，
   以他人身份读设置、用他人 Key、把产物写进他人目录。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _register(c, username: str):
    return c.post("/api/auth/register", json={"username": username, "password": "test1234"})


# ------------------------------------------------------------------
# ① 用户名不得撞内部哨兵 / 不得是路径穿越形态
# ------------------------------------------------------------------

@pytest.mark.parametrize("name", ["_global", "GLOBAL", "local", "admin", "system", "root"])
def test_reserved_usernames_rejected(client, name):
    """保留名必须拒绝 —— `_global` 尤其关键（它是全局设置的存储键）。"""
    r = _register(client, name)
    body = r.json()
    assert not (r.status_code == 200 and body.get("status") == "ok"), (
        f"保留名 {name!r} 竟然注册成功 —— 可劫持全局设置"
    )


@pytest.mark.parametrize("name", ["../evil", "a/b", "a\\b", "a b", "x", "a" * 300])
def test_invalid_usernames_rejected(client, name):
    """用户名会被用作用户标识与目录名，必须限制字符集与长度。"""
    r = _register(client, name)
    body = r.json()
    assert not (r.status_code == 200 and body.get("status") == "ok"), (
        f"非法用户名 {name[:20]!r} 竟然注册成功"
    )


def test_normal_usernames_still_accepted(client):
    """校验不能过严 —— 普通用户名（含中文）必须能注册。

    用**唯一**名字：固定名在第二次运行时会因"已存在"而失败，
    那是残留数据造成的假红（这类时红时绿的测试比没有更糟）。
    """
    import uuid

    uniq = uuid.uuid4().hex[:8]
    for name in (f"user_{uniq}", f"中文用户{uniq}"):
        r = _register(client, name)
        assert r.status_code == 200 and r.json().get("status") == "ok", r.text


def test_user_settings_do_not_leak_to_others(client):
    """一个用户写的设置不能影响另一个用户（全局哨兵劫持的下游表现）。"""
    import uuid

    a = f"atk_{uuid.uuid4().hex[:8]}"
    b = f"vic_{uuid.uuid4().hex[:8]}"
    _register(client, a)
    _register(client, b)
    ta = client.post("/api/auth/login",
                     json={"username": a, "password": "test1234"}).json()["token"]
    tb = client.post("/api/auth/login",
                     json={"username": b, "password": "test1234"}).json()["token"]

    client.put("/api/settings", headers={"Authorization": f"Bearer {ta}"},
               json={"llm_base_url": "http://atk.evil/v1"})
    got = client.get("/api/settings",
                     headers={"Authorization": f"Bearer {tb}"}).json()["settings"]
    assert got.get("llm_base_url") in (None, ""), (
        f"B 读到了 A 的设置：{got.get('llm_base_url')!r}"
    )


# ------------------------------------------------------------------
# ② seed 不得改写身份 / 不得注入任意 State 键
# ------------------------------------------------------------------

def test_seed_whitelist_drops_identity_and_state_keys():
    """`run_context` 与 `images` 等必须被白名单剔除。"""
    from app.services.task_runner import _ALLOWED_SEED_KEYS, _sanitize_seed

    assert "run_context" not in _ALLOWED_SEED_KEYS, (
        "seed 白名单里又出现了 run_context —— 身份可被请求体改写"
    )
    clean = _sanitize_seed({
        "run_context": {"user_id": "victim"},
        "images": [{"x": 1}],
        "character_reference_images": {"a": "b"},
        "user_input": "hi",
    })
    assert "run_context" not in clean
    assert "images" not in clean
    assert "character_reference_images" not in clean
    assert clean.get("user_input") == "hi"


def test_seed_non_dict_is_ignored():
    from app.services.task_runner import _sanitize_seed

    assert _sanitize_seed("oops") == {}
    assert _sanitize_seed(None) == {}


def test_start_run_sets_identity_after_seed():
    """`run_context` 必须在 seed 合并**之后**由服务端写入。

    读源码断言顺序：原先 `state["run_context"] = ...` 在前、
    `state.update(seed)` 在后，seed 因此能覆盖身份。
    """
    import inspect

    from app.services import task_runner

    src = inspect.getsource(task_runner.start_run)
    i_seed = src.find("_sanitize_seed(seed)")
    i_ctx = src.find('state["run_context"] = RunContext(')
    assert i_seed != -1 and i_ctx != -1, "找不到 seed 合并或 run_context 赋值"
    assert i_ctx > i_seed, (
        "run_context 必须在 seed 合并之后赋值，否则 seed 能覆盖身份"
    )
