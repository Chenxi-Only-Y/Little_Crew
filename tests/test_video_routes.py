"""视频生成的三条路线（F5 扩展）的回归测试。

用户 2026-10-02 定的三条路线：
    路线一  文生图 → 图生白模 → 白模生视频   （接 API）
    路线二  文生图 → 图生视频                 （本地）
    路线三  文生视频                          （本地）

本地走 MiniMax H3（`local-gen` 服务）。关键契约：
    - 有图 → 发 `image_base64`（首帧）
    - 给了尾帧 → 再发 `last_image_base64`（终点布局，白模产物）
    - **无图 → 什么都不发**，即纯文生视频（H3 的 `image_base64` 是可选参数）

本文件重点守住第三条：**无图时绝不能发出 `image_base64`**，
否则 H3 会把它当图生视频处理，路线三就静默退化成路线二。
"""

from __future__ import annotations

import base64
import pathlib

import pytest

from app.core.config import settings
from app.services import video as video_mod


class _FakeResponse:
    """够用的 httpx.Response 替身。

    真实代码用到 `status_code` / `text` / `json()` / `raise_for_status()` / `content`，
    少一个就会在**下载结果那一步**炸掉（第一版漏了后两个，5 条测试全红）。
    """

    def __init__(self, payload, status=200, content=b""):
        self._payload = payload
        self.status_code = status
        self.text = "{}"
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """拦截提交给 local-gen 的请求体，供断言。"""

    captured: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, json=None, **kwargs):  # noqa: A002  模仿 httpx 签名
        type(self).captured.append({"url": url, "body": json})
        return _FakeResponse({"job_id": "job_test_1"})

    def get(self, url, **kwargs):
        return _FakeResponse({})


@pytest.fixture
def local_h3(monkeypatch, tmp_path):
    """把视频服务切到本地 H3，并拦下 HTTP。"""
    import httpx

    _FakeClient.captured = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)

    from app.services import runtime_settings as rs

    rs.reset("_global")
    rs.set_user("_global")
    rs.set_many("_global", {"video_provider_mode": "local"})

    monkeypatch.setattr(settings, "mock_video", False)
    monkeypatch.setattr(settings, "local_gen_url", "http://127.0.0.1:8100")
    monkeypatch.setattr(settings, "video_max_duration", 7)

    # 轮询直接返回成功，避免睡眠
    monkeypatch.setattr(video_mod.VideoService, "_poll_local",
                        lambda self, *a, **k: "http://127.0.0.1:8100/files/v.mp4", raising=False)
    monkeypatch.setattr(video_mod.VideoService, "_download",
                        lambda self, *a, **k: None, raising=False)

    yield tmp_path
    rs.reset("_global")


def _png(tmp_path: pathlib.Path, name: str = "a.png", color=(128, 128, 128)) -> str:
    """写一个最小可读的 PNG 文件（真的能被 Pillow 打开）。

    `color` 可指定：测"首尾帧是不同的图"时必须给不同颜色，
    否则两张纯色图 PNG 编码后**字节完全相同**，断言形同虚设。
    """
    from PIL import Image

    p = tmp_path / name
    Image.new("RGB", (64, 64), color).save(p)
    return str(p)


def test_route3_text_to_video_sends_no_image(local_h3):
    """路线三：没有图片时必须走纯文生视频——**不发任何图像字段**。"""
    from app.services.video import get_video_service

    out, produced = get_video_service().synthesize(
        images=[], script={}, out_dir=str(local_h3), session_id="s_route3",
        video_count=1, text_prompt="一只猫在雨夜走进侦探事务所",
    )

    assert _FakeClient.captured, "没有发出任何请求"
    body = _FakeClient.captured[0]["body"]
    assert "image_base64" not in body, "路线三却发了首帧图，会静默退化成图生视频"
    assert "last_image_base64" not in body
    # 提示词必须用上用户创意（画面内容全靠它描述）
    assert "雨夜" in body["prompt"], f"未使用用户创意作提示词：{body['prompt']!r}"
    assert body["seconds"] == 7


def test_route2_image_to_video_sends_first_frame_only(local_h3):
    """路线二：只有一张图时发首帧，不发尾帧。"""
    from app.services.video import get_video_service

    img = {"shot_id": "S1-01", "local_path": _png(local_h3)}

    get_video_service().synthesize(
        images=[img], script={}, out_dir=str(local_h3), session_id="s_route2",
        video_count=1, text_prompt="忽略我",
    )

    body = _FakeClient.captured[0]["body"]
    assert body["image_base64"].startswith("data:image/png;base64,")
    assert "last_image_base64" not in body, "没给尾帧却发了尾帧字段"
    # 有首帧时不应把用户创意当画面描述（画面已由首帧决定）
    assert "忽略我" not in body["prompt"]


def test_route1_previs_provides_last_frame(local_h3):
    """路线一：白模给出终点布局时，尾帧必须被发送。"""
    from app.services.video import get_video_service

    img = {
        "shot_id": "S1-01",
        "local_path": _png(local_h3, "first.png", (200, 30, 30)),
        "last_local_path": _png(local_h3, "last.png", (30, 30, 200)),
    }

    get_video_service().synthesize(
        images=[img], script={}, out_dir=str(local_h3), session_id="s_route1",
        video_count=1,
    )

    body = _FakeClient.captured[0]["body"]
    assert "image_base64" in body
    assert "last_image_base64" in body, "白模给了终点布局却没发送尾帧"
    assert body["last_image_base64"].startswith("data:image/png;base64,")
    # 两端应是不同的图（否则"首尾帧引导"没有意义）
    assert body["image_base64"] != body["last_image_base64"]


def test_last_frame_also_accepts_remote_url(local_h3):
    """尾帧也支持远端 URL（未来接 API 白模时用得上）。"""
    from app.services.video import get_video_service

    img = {
        "shot_id": "S1-01",
        "local_path": _png(local_h3, "f.png"),
        "last_image": "https://example.com/last.png",
    }

    get_video_service().synthesize(
        images=[img], script={}, out_dir=str(local_h3), session_id="s_url", video_count=1,
    )

    body = _FakeClient.captured[0]["body"]
    assert body["last_image_base64"] == "https://example.com/last.png"


def test_broken_image_is_skipped_not_sent_empty(local_h3):
    """图片路径损坏时应跳过该段，而不是发出一个空首帧。"""
    from app.services.video import get_video_service

    img = {"shot_id": "S1-01", "local_path": str(local_h3 / "不存在的图.png")}

    get_video_service().synthesize(
        images=[img], script={}, out_dir=str(local_h3), session_id="s_broken", video_count=1,
    )

    assert not _FakeClient.captured, "图片读不出来却仍然发出了请求"


def test_text_to_video_segment_count_follows_video_count(local_h3):
    """纯文生视频的段数：video_count>0 按其值，<=0 默认 1 段。"""
    from app.services.video import get_video_service

    get_video_service().synthesize(
        images=[], script={}, out_dir=str(local_h3), session_id="s_c3",
        video_count=3, text_prompt="x",
    )
    assert len(_FakeClient.captured) == 3

    _FakeClient.captured = []
    get_video_service().synthesize(
        images=[], script={}, out_dir=str(local_h3), session_id="s_c0",
        video_count=0, text_prompt="x",
    )
    assert len(_FakeClient.captured) == 1, "video_count=0 时应默认 1 段"


def test_image_ref_reads_requested_keys():
    """`_image_ref` 必须能按指定字段取图（首帧/尾帧字段名不同）。"""
    from app.services.video import VideoService

    svc = VideoService()
    assert svc._image_ref({"image_url": "https://a/b.png"}) == "https://a/b.png"
    assert svc._image_ref({"last_image": "https://a/last.png"},
                          url_key="last_image", path_key="last_local_path") == "https://a/last.png"
    # 字段不存在时返回空串，而不是抛异常
    assert svc._image_ref({}) == ""
    assert svc._image_ref({}, url_key="last_image", path_key="last_local_path") == ""
