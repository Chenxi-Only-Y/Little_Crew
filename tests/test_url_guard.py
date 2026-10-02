"""URL 安全（SSRF）的回归测试。

这里的两条都来自复审发现的真问题，而不是"顺手加的覆盖"：

1. `safe_get` 声称"逐跳校验"，但 `assert_public_url` 默认**放行回环**，
   于是"公网服务 302 → 127.0.0.1"一路放行 —— 而这恰恰是 `safe_get`
   自己 docstring 举的例子。实测能读到本机服务的响应体。
2. 正常公网地址不能被误伤（校验过严会让本地推理/云端 API 全部用不了，
   这个坑本项目已经踩过一次：`198.18/15` 那次）。
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.url_guard import assert_public_url, safe_get


class _Secret(BaseHTTPRequestHandler):
    """模拟本机上的内部服务（Docker API / 管理后台之类）。"""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"INTERNAL-ADMIN-SECRET")

    def log_message(self, *a):
        pass


class _Bouncer(BaseHTTPRequestHandler):
    """模拟攻击者的公网服务：302 跳到任意目标。"""

    target = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.target)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def local_servers():
    secret = HTTPServer(("127.0.0.1", 0), _Secret)
    bouncer = HTTPServer(("127.0.0.1", 0), _Bouncer)
    for s in (secret, bouncer):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield secret.server_address[1], bouncer.server_address[1]
    bouncer.shutdown()
    secret.shutdown()


def test_allow_local_flag_controls_loopback():
    """`allow_local` 必须真的区分两种场景。

    默认 True 是给"用户显式配置的本地端点"（ComfyUI / local-gen）用的；
    下载类调用传 False，避免被重定向骗去读本机。
    """
    for host in ("127.0.0.1", "localhost"):
        # 默认允许（用户自己填的本地服务）
        assert_public_url(f"http://{host}:8899/x")
        assert_public_url(f"http://{host}:8899/x", allow_local=True)
        # 显式禁止（远端内容决定的 URL）
        with pytest.raises(ValueError):
            assert_public_url(f"http://{host}:8899/x", allow_local=False)


def test_safe_get_blocks_redirect_to_loopback(local_servers):
    """302 跳到回环必须被拦住 —— 这是 `safe_get` 存在的理由本身。

    修复前实测：`safe_get` 跟到了回环目标并拿到 `INTERNAL-ADMIN-SECRET`。
    """
    secret_port, bouncer_port = local_servers
    _Bouncer.target = f"http://127.0.0.1:{secret_port}/admin"
    with pytest.raises(ValueError):
        safe_get(f"http://localhost:{bouncer_port}/hop", context="测试", timeout=5)


def test_safe_get_does_not_leak_internal_body(local_servers):
    """再确认一次：即使拦住，也**不能**把内部响应体带出来。"""
    secret_port, bouncer_port = local_servers
    _Bouncer.target = f"http://localhost:{secret_port}/admin"
    try:
        resp = safe_get(f"http://localhost:{bouncer_port}/hop", context="测试", timeout=5)
    except ValueError:
        return  # 正确行为
    raise AssertionError(f"safe_get 未拦截，响应体={resp.text[:60]!r}")


def test_public_addresses_still_pass():
    """校验不能过严——否则本地推理与云端 API 全部不可用。

    （本项目踩过一次：`198.18.0.0/15` 是 Clash/Surge 的 fake-ip 段，
      不放行会让**每一个域名**都被拒，表现为"DeepSeek/百炼全连不上"。）
    """
    for ok in ("https://1.1.1.1/v1", "http://8.8.8.8/", "https://93.184.216.34/",
               "https://198.18.0.16/v1"):     # 代理 fake-ip 段
        assert_public_url(ok, allow_local=False)

    for bad in ("http://10.0.0.5/", "http://192.168.1.1/", "http://172.16.0.1/",
                "http://169.254.169.254/", "http://0.0.0.0/", "http://[fd00::1]/",
                "http://[::ffff:10.0.0.1]/", "file:///etc/passwd",
                "gopher://x/", "http:///nohost"):
        with pytest.raises(ValueError):
            assert_public_url(bad, allow_local=False)
