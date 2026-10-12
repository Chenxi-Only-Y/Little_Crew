"""端到端验证"探测器会不会随着服务起停而变"。

## 为什么这条测试值得单独写

前面的 `test_local_probe.py` 测的是**各个探针的判定逻辑**。但用户真正
关心的是一件更简单的事：

    "我把 local-gen / Ollama 启动起来，界面上会不会出现它？"

这需要真的有一个 HTTP 服务在对应端口上响应 —— 所以这里用
`http.server` 起一个**假服务**，验证探测器从"不可用"翻成"可用"，
再关掉、翻回"不可用"。

（不连真实 Ollama / local-gen：那会让测试依赖本机装了什么，
CI 上必挂，而且真实服务的行为不由我们控制。）
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import local_probe as lp
from app.services.auth import get_user_store


class _Handler(BaseHTTPRequestHandler):
    """假 Ollama：`/api/tags` 返回 `{"models": [...]}`，其余 404。"""

    payload: dict = {"models": [{"name": "qwen3:8b", "capabilities": ["completion"]}]}

    def do_GET(self):  # noqa: N802  (http.server 的命名约定)
        if self.path.rstrip("/") in ("/api/tags",):
            body = json.dumps(self.payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):  # 静音，别把测试输出弄脏
        pass


@pytest.fixture
def fake_ollama():
    """起一个假 Ollama，返回它的 base url；测试结束自动关。"""
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


# 本项目没有共享 conftest.py，`client` fixture 是各测试文件自己定义的
# （见 test_whitebox_video.py / test_auth_hardening.py）。这里也照做。
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_cache():
    lp._cache.clear()
    lp._cache_at = 0.0
    yield
    lp._cache.clear()
    lp._cache_at = 0.0


def test_probe_flips_when_service_appears_and_disappears(monkeypatch, fake_ollama):
    """服务起来 → 探测为可用；关掉 → 翻回不可用。

    这就是用户会做的操作：启动服务，点「重新检测」。
    """
    # 让探测器指向我们的假服务
    monkeypatch.setenv("OLLAMA_HOST", fake_ollama)

    assert lp.refresh(force=True)["llm:ollama"] is True, "假服务在跑，应当判可用"

    # 换到一个**没在监听**的端口 → 应当翻成不可用
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
    assert lp.refresh(force=True)["llm:ollama"] is False, "服务没了，应当判不可用"


class _HealthHandler(BaseHTTPRequestHandler):
    """假 local-gen：只认 `/health`。"""

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_local_gen():
    srv = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_local_gen_probe_flips_with_health_endpoint(monkeypatch, fake_local_gen):
    """local-gen 靠 `/health` 判断 —— 起停要能翻转。

    这条**必须真的翻转**：`local_gen_url` 指向一个在跑的服务 → 可用；
    指向没在监听的端口 → 不可用。只断言两次 False 等于没测。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "local_gen_url", fake_local_gen)
    result = lp.refresh(force=True)
    assert result["image_gen:local"] is True, "假服务在跑，出图该判可用"
    assert result["video_gen:local"] is True, "同一个服务也供视频"

    monkeypatch.setattr(settings, "local_gen_url", "http://127.0.0.1:1")
    result = lp.refresh(force=True)
    assert result["image_gen:local"] is False, "服务没了，该翻回不可用"


def test_recheck_endpoint_is_authenticated_and_forces_refresh(client):
    """`POST /api/providers/recheck`：要登录，且**强制**重探。

    为什么必须强制：探测结果缓存 60 秒，用户刚启动本地服务后需要立刻
    拿到确定结果，否则他会以为"没识别到"。
    """
    r = client.post("/api/providers/recheck")
    assert r.status_code == 401, "不该让匿名调用者触发探测"

    # ⚠️ 原先这里用 `director` / `test1234` **登录真实库里预置的账号** ——
    # 那让测试**依赖外部数据**：conftest 现在把库也隔离到临时目录
    # （为的是不污染用户的会话与设置），那个账号不存在，于是
    # `KeyError: 'token'`。改成**自己注册一个**，让测试自足。
    import time

    uname = f"recheck_{int(time.time() * 1000)}"
    tok, _msg = get_user_store().register(uname, "test1234")
    assert tok, f"注册失败：{_msg}"

    r = client.post("/api/providers/recheck", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body.get("ready"), dict)
    # 这一条是为了防"探测表空了却没人发现"
    assert body.get("total", 0) > 0, "探测表不该是空的"
    assert "detail" in body and "本地提供者" in body["detail"]
