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


def test_video_download_helper_is_mockable_and_guarded():
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
