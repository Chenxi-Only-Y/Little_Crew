"""守住本次审查发现的几类"静默失效"。

这些 bug 的共同特征：**不报错、界面还提示成功**，所以只有测试能挡住回归。
每条都对应一次实测确认过的真实缺陷。

1. 密钥框失焦抹掉已存密钥（前端，见 `test_*` 说明；此处用后端语义固化契约）
2. 用户可配置项"能填能存、运行时读 config"（`runtime_settings.resolve`）
3. 死端点 `/api/asset/upload` 必须不存在（曾匿名可无限写盘）
4. 免鉴权路径必须**精确列出**，不能靠前缀匹配顺带放行
5. 会话 ID 不能被他人占用（跨用户接管）
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, _PUBLIC_PATHS, _claim_session


def _client() -> TestClient:
    return TestClient(app)


def _register(c: TestClient, name: str) -> dict:
    """注册一个**唯一**用户名并取 token。

    加随机后缀：固定名在第二次运行（或与别的测试撞名）时会返回 409，
    于是 `r.json()['token']` 抛 KeyError，报错信息还指向断言而不是真实原因。
    """
    import uuid as _uuid

    r = c.post("/api/auth/register",
               json={"username": f"{name}_{_uuid.uuid4().hex[:8]}",
                     "password": "test1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ------------------------------------------------------------------
# 1. 可配置项必须真的生效
# ------------------------------------------------------------------

def test_resolve_prefers_runtime_over_config():
    """`resolve` 必须让运行时设置覆盖 config.py。

    否则就是"设置页能改、提示已保存、运行时读的却是 .env"——
    实测过：库里 video_max_duration=15，运行时仍是 7，
    本地 8GB 的「>10s 必 OOM」保护因此完全失效。

    注意 `resolve()` 读的是**当前线程用户**（与 `get()` 同语义），
    所以必须先 `set_user`，否则它看到的是 `_global`。
    """
    from app.core.config import settings
    from app.services import runtime_settings as rs

    uid = "_t_resolve"
    rs.reset(uid)
    rs.set_many(uid, {
        "video_max_duration": 15,
        "max_images": 3,
        "quality_threshold": 0.9,
        "video_resolution": "2K",
    })
    rs.set_user(uid)
    try:
        assert rs.resolve("video_max_duration", settings.video_max_duration) == 15
        assert rs.resolve("max_images", settings.max_images) == 3
        assert rs.resolve("quality_threshold", settings.quality_threshold) == 0.9
        assert rs.resolve("video_resolution", settings.video_resolution) == "2K"
    finally:
        rs.set_user(None)
        rs.reset(uid)


def test_resolve_falls_back_on_empty_string():
    """空串＝清除覆盖，应回退到 config 默认（与 `set_many` 的语义一致）。"""
    from app.core.config import settings
    from app.services import runtime_settings as rs

    uid = "_t_resolve_empty"
    rs.reset(uid)
    rs.set_many(uid, {"video_max_duration": ""})
    rs.set_user(uid)
    try:
        assert rs.resolve("video_max_duration", settings.video_max_duration) == \
            settings.video_max_duration
    finally:
        rs.set_user(None)
        rs.reset(uid)


def test_user_configurable_keys_have_a_runtime_reader():
    """声明为"用户可配置"的键，必须至少有一个读 `resolve`/`get` 的地方。

    这条是防止"注册表声明了、界面能填、但没有任何代码读它"——
    `max_images` 就曾如此（设 3 张仍出 8 张，全仓 0 处读取）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    src = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in root.rglob("*.py")
    )
    # 这些键是用户可见的生成参数，必须能影响运行
    for key in ("max_images", "video_max_duration", "video_count",
                "video_resolution", "quality_threshold"):
        pattern = (
            rf'resolve\(\s*["\']{re.escape(key)}["\']'
            rf'|rs\.get\(\s*["\']{re.escape(key)}["\']'
            rf'|get\(\s*["\']{re.escape(key)}["\']'
        )
        assert re.search(pattern, src), (
            f"{key} 被声明为可配置，但没有任何运行时读取点——"
            "用户改了不会生效，且不会报错"
        )


# ------------------------------------------------------------------
# 2. 死端点与匿名写盘
# ------------------------------------------------------------------

def test_asset_upload_endpoint_is_gone():
    """`/api/asset/upload` 必须不存在。

    它原本只服务于已删除的 Streamlit 前端；前端删掉后就成了
    **匿名可无限写盘**的入口（实测无 token 提交即写入成功、并回显绝对路径）。
    """
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/api/asset/upload" not in paths, "匿名写盘端点又回来了"


def test_public_paths_are_exact_not_prefix():
    """免鉴权必须是"逐个精确列出"，不能靠前缀顺带放行。

    原先用 `path.startswith(_HEALTH_PUBLIC)`，于是白名单里的 `/api/asset`
    顺带公开了 `/api/asset/upload`（匿名可无限写盘），
    `/api/registry` 同理放行了 `/api/registry/validate`。

    这里换个可证伪的判据：**拿一个"前缀 + 尾巴"的路径去请求**，
    它必须**不被**当成公开路径（即中间件不因前缀而放行）。
    单纯查字符串表会被"某个路径恰好既是具体端点又是别人的前缀"绕过。
    """
    assert isinstance(_PUBLIC_PATHS, frozenset), "白名单应是精确路径集合"

    c = _client()
    # 这些路径都不存在；若中间件用前缀匹配放行，它们会走到路由层得到 404；
    # 用精确匹配则会被鉴权拦下得到 401。
    for probe in ("/api/registryZZZ", "/api/providersZZZ", "/api/authZZZ",
                  "/healthZZZ", "/api/registry/validateZZZ"):
        r = c.get(probe)
        assert r.status_code == 401, (
            f"{probe} 得到 HTTP {r.status_code}——说明中间件按前缀放行了它，"
            "任何以白名单路径开头的路由都会意外公开"
        )

    # 该公开的仍在
    for need in ("/health", "/api/auth/login", "/api/registry", "/api/registry/validate",
                 "/api/providers"):
        assert need in _PUBLIC_PATHS, f"{need} 应免鉴权（前端登录前要用）"


def test_every_public_path_is_a_real_route():
    """白名单里的路径必须真实存在，否则是"僵尸公开项"。

    僵尸项不会报错，却会让人以为某个端点是公开的（或反之）。
    """
    paths = {getattr(r, "path", "") for r in app.routes}
    zombie = sorted(p for p in _PUBLIC_PATHS if p not in paths)
    assert not zombie, f"白名单里有不存在的路径：{zombie}"


def test_anonymous_cannot_reach_protected_endpoints():
    """未登录访问受保护端点必须 401（抽查几条关键的）。"""
    c = _client()
    for method, path in (("GET", "/api/settings"),
                         ("GET", "/api/projects"),
                         ("GET", "/api/registry"),
                         ("POST", "/api/registry/validate")):
        r = c.request(method, path, json={} if method == "POST" else None)
        expect_public = path in _PUBLIC_PATHS
        if expect_public:
            assert r.status_code != 401, f"{path} 应免鉴权，却返回 401"
        else:
            assert r.status_code == 401, f"{path} 未登录竟可访问（HTTP {r.status_code}）"


# ------------------------------------------------------------------
# 3. 会话归属
# ------------------------------------------------------------------

def test_claim_session_rejects_foreign_session(tmp_path, monkeypatch):
    """他人会话 ID 必须被拒（否则可跨用户接管）。

    实测过：B 把 A 的 session_id 放进请求体，`SessionStore.save` 是 upsert
    会把 user_id 改写成 B，之后 `_require_owner` 反而认为 B 是主人。
    """
    from app.services.session_store import get_session_store

    sid = "test_claim_foreign_1"
    store = get_session_store()
    store.save(sid, "owner_alice", "A 的创意", "review", "review", None)
    try:
        resp = _claim_session(sid, "intruder_bob")
        assert resp is not None, "他人会话竟然被放行"
        assert resp.status_code == 403

        # 属主自己可以用
        assert _claim_session(sid, "owner_alice") is None
        # 空 id（新建）与不存在的 id 都放行
        assert _claim_session("", "someone") is None
        assert _claim_session("never-existed-xyz", "someone") is None
    finally:
        store.delete(sid)


def test_session_endpoint_rejects_foreign_id():
    """走 HTTP：B 用 A 的 session_id 建会话必须 403。"""
    import uuid as _uuid

    c = _client()
    ha = _register(c, "iso_claim_a")
    hb = _register(c, "iso_claim_b")

    # 唯一 id：固定 id 会与上一次运行残留的会话撞上（那个会话属主是旧用户），
    # 于是 A 自己第一次请求就被正确拒绝 → 断言失败在读起来毫不相关的地方。
    sid = f"test_claim_http_{_uuid.uuid4().hex[:8]}"
    r = c.post("/api/sessions", headers=ha,
               json={"session_id": sid, "user_input": "A 的创意"})
    assert r.status_code == 200, r.text

    r2 = c.post("/api/sessions", headers=hb,
                json={"session_id": sid, "user_input": "B 想接管"})
    assert r2.status_code == 403, (
        f"B 用 A 的 session_id 建会话得到 HTTP {r2.status_code}（应 403）——跨用户接管"
    )
    # 且属主没被改写：A 仍然拥有它
    r3 = c.get(f"/api/crew/status/{sid}", headers=ha)
    assert r3.status_code == 200, f"A 反而访问不了自己的会话：{r3.text}"
