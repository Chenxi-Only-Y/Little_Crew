"""白模参考视频端点的测试。

重点：
  1. 鉴权（不在公开列表里 → 未登录必须 401，否则等于开了匿名转码+上传入口）
  2. 帧顺序按**表单顺序**而不是文件名（文件名不可信）
  3. 没有 ffmpeg 时给出**可执行**的报错，而不是 500
  4. 超长视频要给出警告（不阻断，但必须提前告知）
"""

from __future__ import annotations

import io
import pathlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app


def _png(color: tuple[int, int, int] = (200, 240, 74)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _discover_ffmpeg() -> str:
    """找一份可用的 ffmpeg；找不到返回空串。

    为什么不在测试里硬编码路径：换台机器就全红。
    这里按"环境变量 → PATH"探测，并把结果写进运行时设置（全局），
    让端点能真正走到编码那一步。
    """
    import os
    import shutil

    for cand in (os.environ.get("LITTLE_CREW_FFMPEG", ""), shutil.which("ffmpeg") or ""):
        if cand and pathlib.Path(cand).is_file():
            return cand
    return ""


@pytest.fixture()
def with_ffmpeg(monkeypatch):
    """让被测端点能找到 ffmpeg；没有就跳过该测试。"""
    exe = _discover_ffmpeg()
    if not exe:
        pytest.skip("本机没有 ffmpeg（设 LITTLE_CREW_FFMPEG 或装到 PATH 后可跑）")

    from app.services import runtime_settings as rs

    from app.core.config import settings

    def fake_get(name, default=None):
        if name == "ffmpeg_path":
            return exe
        return getattr(settings, name, default)

    monkeypatch.setattr(rs, "get", fake_get)
    # 端点内是 `from app.services import runtime_settings as rs` 后调 rs.get，
    # 所以打补丁到模块属性即可生效
    return exe


def _auth(client: TestClient) -> dict:
    """注册一个**唯一**用户名并取 token。

    用唯一名而不是固定名：固定名第二次注册会返回 409，
    而"注册失败就改登录"的写法容易在断言里给出误导性错误。
    """
    import uuid as _uuid

    r = client.post("/api/auth/register",
                    json={"username": f"wb_{_uuid.uuid4().hex[:10]}",
                          "password": "test1234"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_requires_auth(client: TestClient):
    """未带 token 必须被拒——否则任何人都能拿它转码/上传。"""
    r = client.post("/api/video/whitebox",
                    files=[("frames", ("a.png", _png(), "image/png"))])
    assert r.status_code == 401


def test_rejects_empty_form(client: TestClient):
    r = client.post("/api/video/whitebox", headers=_auth(client), files=[])
    assert r.status_code == 400
    assert "帧" in r.json()["detail"]


def test_encodes_frames_in_form_order(client: TestClient, with_ffmpeg):
    """帧顺序必须是**表单顺序**，不能靠文件名排序。"""
    h = _auth(client)

    # 故意让文件名与顺序相反：若实现按文件名排，出来的时长/内容会错
    files = [
        ("frames", ("z_last.png", _png((255, 0, 0)), "image/png")),
        ("frames", ("a_first.png", _png((0, 255, 0)), "image/png")),
    ]
    r = client.post("/api/video/whitebox", headers=h, files=files,
                    data={"fps": "24"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["frames"] == 2
    assert pathlib.Path(body["local_path"]).is_file()


def test_saved_frames_use_arrival_order(client: TestClient, with_ffmpeg):
    """按到达顺序编号：第 1 个提交的应是 00000.png。

    这条直接验证"顺序靠表单、不靠文件名"——上面那条只看时长看不出差别。
    """
    import re

    h = _auth(client)
    files = [
        ("frames", ("zzz.png", _png((255, 0, 0)), "image/png")),
        ("frames", ("aaa.png", _png((0, 255, 0)), "image/png")),
    ]
    seen: list[str] = []
    real_encode = __import__(
        "app.services.whitebox_video", fromlist=["encode_frames_to_mp4"]
    ).encode_frames_to_mp4

    def spy(frames, out, **kw):
        seen.extend(p.name for p in frames)
        return real_encode(frames, out, **kw)

    import app.services.whitebox_video as wb

    original = wb.encode_frames_to_mp4
    wb.encode_frames_to_mp4 = spy  # type: ignore[assignment]
    try:
        r = client.post("/api/video/whitebox", headers=h, files=files,
                        data={"fps": "24"})
        assert r.status_code == 200, r.text
    finally:
        wb.encode_frames_to_mp4 = original  # type: ignore[assignment]

    assert seen == ["00000.png", "00001.png"], (
        f"帧没有按到达顺序编号（拿到 {seen}）——文件名不可信"
    )


def test_missing_ffmpeg_gives_actionable_error(client: TestClient, monkeypatch):
    """没有 ffmpeg 时应回 400 + 修复步骤，而不是 500。"""
    h = _auth(client)

    from app.services import whitebox_video

    def boom(*a, **kw):
        raise whitebox_video.FFmpegNotFound(
            "找不到 ffmpeg。\n  1) 在「设置」里填 `ffmpeg_path`；"
        )

    monkeypatch.setattr(whitebox_video, "encode_frames_to_mp4", boom)
    r = client.post("/api/video/whitebox", headers=h,
                    files=[("frames", ("a.png", _png(), "image/png"))])
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "ffmpeg" in detail
    assert "ffmpeg_path" in detail, "报错里必须给出可执行的设置项名"


def test_long_video_warns_but_does_not_block(client: TestClient, with_ffmpeg):
    """超长视频给警告但不阻断——用户可能就是想先看看。"""
    h = _auth(client)
    fps = 2
    # 40 帧 @2fps = 20 秒 > 15 秒上限
    files = [("frames", (f"{i:05d}.png", _png(), "image/png")) for i in range(40)]
    r = client.post("/api/video/whitebox", headers=h, files=files,
                    data={"fps": str(fps)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seconds"] > 15
    assert "warning" in body and "上限" in body["warning"]


def test_upload_without_key_is_clear(client: TestClient, with_ffmpeg):
    """选了上传但没配 key → 明确说缺什么，而不是让它 500。"""
    h = _auth(client)
    r = client.post("/api/video/whitebox", headers=h,
                    files=[("frames", ("a.png", _png(), "image/png"))],
                    data={"upload": "1"})
    # 两条可接受路径：没配视频模型，或没配 key；都必须是人能读懂的原因
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert ("Key" in detail) or ("未选择" in detail) or ("MiniMax" in detail), detail
