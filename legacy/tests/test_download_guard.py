"""`safe_get` 的两种模式：本地推理可用 + 远端下载不放行本机。

⚠️ 为什么要专测"本地可用"：修 SSRF 时很容易**顺手把功能弄坏**。
本项目的零成本路径是本地推理服务（`D:\\AI\\local-gen`，默认
`http://127.0.0.1:8100`），它的结果 URL 就在回环上。
如果为了安全把回环一律禁掉，本地生图/生视频会**静默失效**——
那是拿功能换安全，不是修 bug。
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.url_guard import assert_public_url, safe_get


class _LocalArtifact(BaseHTTPRequestHandler):
    """模拟本地推理服务的产物下载端点。"""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()
        self.wfile.write(b"\x89PNG-LOCAL-OK")

    def log_message(self, *a):
        pass


@pytest.fixture()
def local_service():
    srv = HTTPServer(("127.0.0.1", 0), _LocalArtifact)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


def test_local_inference_download_still_works(local_service):
    """`allow_local=True` 必须能取回本地服务的产物（否则本地推理就废了）。"""
    for host in ("127.0.0.1", "localhost"):
        r = safe_get(f"http://{host}:{local_service}/a.png",
                     context="本地下载", timeout=5, allow_local=True)
        assert r.status_code == 200
        assert b"PNG-LOCAL-OK" in r.content


def test_remote_download_rejects_loopback_by_default(local_service):
    """默认必须拒绝回环 —— 远端内容决定的 URL 不能去读本机服务。"""
    with pytest.raises(ValueError):
        safe_get(f"http://127.0.0.1:{local_service}/a.png",
                 context="远端下载", timeout=5)


def test_local_download_bypasses_system_proxy(local_service, monkeypatch):
    """连本机时**必须绕开系统代理**。

    ⚠️ 这条来自实测踩坑：httpx 默认 `trust_env=True`，会读系统/环境里的代理
    设置（本机实测：Windows 注册表把 http/https 都指向 `127.0.0.1:7890`）。
    开着 Clash 之类的工具时，**连 `127.0.0.1` 的请求也被送去代理**，
    于是本地推理服务（local-gen，默认 `127.0.0.1:8100`）的产物取不回来——
    表现为"本地生图/生视频莫名其妙失败"，而代码看着完全正常。

    这里人为把代理指向一个**必然连不上**的端口：若 safe_get 仍走代理，
    请求会失败；绕开代理则本地服务能正常响应。
    """
    import socket

    # 占一个端口再立刻关掉 → 该端口上几乎必然没有服务在听
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()

    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(var, f"http://127.0.0.1:{dead_port}")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    r = safe_get(f"http://127.0.0.1:{local_service}/a.png",
                 context="本地下载", timeout=5, allow_local=True)
    assert r.status_code == 200
    assert b"PNG-LOCAL-OK" in r.content, (
        "本地下载走了系统代理 —— 开着代理工具时本地推理会失败"
    )

    """`VideoService._download` 必须存在且带 SSRF 校验。

    它是一个**方法**而不是内联代码，理由有二：
      1. 安全逻辑集中一处，谁调都自动带校验；
      2. 测试可以 mock 它 —— `tests/test_video_routes.py` 一直这么 mock，
         但此前方法不存在，所以 mock 是空转的、真去连了本机
         （把下载内联进调用点后，那些测试立刻变红，暴露了这一点）。
    """
    import inspect

    from app.services.video import VideoService

    assert hasattr(VideoService, "_download"), "_download 应是被 mock 的那个方法"
    sig = inspect.signature(VideoService._download)
    assert "allow_local" in sig.parameters, "必须能显式控制是否允许本机"

    src = inspect.getsource(VideoService._download)
    assert "safe_get" in src, "下载必须走 safe_get（逐跳 SSRF 校验）"
