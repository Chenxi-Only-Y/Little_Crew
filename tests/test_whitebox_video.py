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

    直接复用产品代码的 `find_ffmpeg`（设置项 → 环境变量 → PATH → 内置包），
    而不是在测试里另写一套探测逻辑——否则加了解析途径就得记得两处都改。
    """
    from app.services.whitebox_video import FFmpegNotFound, find_ffmpeg

    try:
        return find_ffmpeg("")
    except FFmpegNotFound:
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


# ------------------------------------------------------------------
# ffmpeg 解析：显式配置错了**不许静默回退**
# ------------------------------------------------------------------

def test_bad_explicit_ffmpeg_path_raises(monkeypatch, tmp_path):
    """设置项指向不存在的路径时必须报错。

    静默回退到 PATH 或内置版本会让用户看到"一切正常"，实际用的是
    另一个版本的 ffmpeg——配置错了却没人告诉你是最难排查的一类问题。
    """
    from app.services.whitebox_video import FFmpegNotFound, find_ffmpeg

    monkeypatch.delenv("LITTLE_CREW_FFMPEG", raising=False)
    with pytest.raises(FFmpegNotFound) as ei:
        find_ffmpeg(str(tmp_path / "nope" / "ffmpeg.exe"))
    msg = str(ei.value)
    assert "不存在" in msg
    # 报错要告诉用户怎么改
    assert "清空" in msg


def test_bad_env_ffmpeg_path_raises(monkeypatch):
    from app.services.whitebox_video import FFmpegNotFound, find_ffmpeg

    monkeypatch.setenv("LITTLE_CREW_FFMPEG", r"Z:\definitely\not\here\ffmpeg.exe")
    with pytest.raises(FFmpegNotFound) as ei:
        find_ffmpeg("")
    assert "LITTLE_CREW_FFMPEG" in str(ei.value)


def test_ffmpeg_dir_lookup_handles_version_suffix(monkeypatch, tmp_path):
    """给目录时要在其中找到 ffmpeg，且要容忍版本后缀。

    imageio-ffmpeg 里的文件名是 `ffmpeg-win-x86_64-v7.1.exe`——
    只匹配 `ffmpeg.exe` 这种死名字会找不到。
    """
    from app.services import whitebox_video

    monkeypatch.delenv("LITTLE_CREW_FFMPEG", raising=False)
    fake = tmp_path / "ffmpeg-win-x86_64-v7.1.exe"
    fake.write_bytes(b"stub")
    assert whitebox_video.find_ffmpeg(str(tmp_path)) == str(fake)


def test_no_ffmpeg_error_lists_actionable_steps(monkeypatch, tmp_path):
    """完全找不到时，报错要给出可执行的步骤（而不是只说"未安装"）。"""
    from app.services import whitebox_video

    monkeypatch.delenv("LITTLE_CREW_FFMPEG", raising=False)
    monkeypatch.setattr(whitebox_video.shutil, "which", lambda _: None)
    # 外部工具目录也当成"没配置"（否则本机恰好装了就测不到报错分支）
    monkeypatch.setattr(whitebox_video, "tools_bin_ffmpeg", lambda: None)
    with pytest.raises(whitebox_video.FFmpegNotFound) as ei:
        whitebox_video.find_ffmpeg("")
    msg = str(ei.value)
    # 错误信息必须给出**可执行的修复步骤**，而不是只说"未安装"。
    # 兜底方案从 `pip install imageio-ffmpeg`（装在项目里）改成了
    # `scripts\setup_tools.ps1`（装到**项目外面**）—— 因为项目目录
    # 只放代码，84 MB 的 ffmpeg 不该进仓库。
    assert "setup_tools.ps1" in msg
    assert "ffmpeg_path" in msg
    assert "LITTLE_CREW_FFMPEG" in msg
    # 报错里要列出**试过哪些地方**，否则用户不知道该往哪查
    assert "已尝试" in msg


# ------------------------------------------------------------------
# 设置必须能被**服务内部**读到（同步端点跨线程问题）
# ------------------------------------------------------------------

def test_settings_readable_inside_sync_endpoint(client: TestClient):
    """在端点内部（同步线程）必须读到**当前登录用户**的设置。

    背景：`threading.local()` 不跨线程传播。中间件在事件循环线程设置的用户，
    同步端点（跑在 AnyIO worker 线程）看不到，于是 `runtime_settings.get()`
    返回 `_global` 的值 → 用户"填了设置却不生效"。
    这类问题不会报错，只会静默用错值，所以必须用测试钉住。

    这里用一个不存在的 `ffmpeg_path` 来"逼出"读取结果：
      读到了用户设置 → 报"路径不存在"；没读到 → 静默用内置 ffmpeg 并成功。

    ⚠️ 这个测试**故意不用 `with_ffmpeg` fixture**：那个 fixture 会把
    `rs.get("ffmpeg_path")` 打桩成真实路径，于是端点永远读到桩值、
    读不到用户设置，测试就成了"测桩"而不是"测机制"（我第一版正是这么错的）。
    """
    import io

    from PIL import Image

    h = _auth(client)
    client.put("/api/settings", headers=h,
               json={"ffmpeg_path": r"Z:\definitely\not\here\ffmpeg.exe"})

    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (1, 2, 3)).save(buf, format="PNG")
    r = client.post("/api/video/whitebox", headers=h,
                    files=[("frames", ("a.png", buf.getvalue(), "image/png"))],
                    data={"fps": "12"})
    assert r.status_code == 400, (
        "端点没读到用户的 ffmpeg_path（静默用了内置 ffmpeg）——"
        f"说明请求作用域的用户没传到同步端点。实际响应：{r.text[:200]}"
    )
    assert "ffmpeg_path" in r.json()["detail"]


# ------------------------------------------------------------------
# 设置必须能被**服务内部**读到（同步端点跨线程问题）
# ------------------------------------------------------------------

def test_request_user_propagates_to_sync_endpoint():
    """请求作用域的用户必须能传到**同步端点**（跑在 AnyIO worker 线程）。

    为什么单独测这条机制、而不是只测白模端点：
      `threading.local()` 不跨线程传播——中间件在事件循环线程设的用户，
      同步端点看不到，`runtime_settings.get()` 会退回 `_global`，
      表现为"用户填了设置却不生效"，**且不报任何错**。

      用一个最小 app 直接验证机制，不依赖 ffmpeg、也不被任何 fixture 打桩——
      否则测试可能因为桩值而"假通过/假失败"。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient as _TC

    from app.services import runtime_settings as rs

    app = FastAPI()

    @app.middleware("http")
    async def mw(request, call_next):  # noqa: ANN001
        rs.set_request_user(request.headers.get("x-user"))
        return await call_next(request)

    @app.get("/sync")
    def sync_probe():  # 同步端点 → 走 AnyIO worker 线程
        return {"user": rs.user(), "value": rs.get("probe_setting")}

    @app.get("/async")
    async def async_probe():
        return {"user": rs.user(), "value": rs.get("probe_setting")}

    rs.set_many("probe_alice", {"probe_setting": "ALICE"})
    c = _TC(app)
    h = {"x-user": "probe_alice"}

    a = c.get("/async", headers=h).json()
    s = c.get("/sync", headers=h).json()
    assert a["value"] == "ALICE", f"异步端点读不到：{a}"
    assert s["value"] == "ALICE", (
        f"同步端点读不到用户设置（thread-local 没跨线程）——实际 {s}"
    )
    # 注意：**不要**断言 `rs.user()` 等于请求用户。
    # `user()` 的语义是"当前线程用户"，只认 `set_user()`；
    # 请求作用域用户通过 `_request_user` 生效，`user()` 仍回退 `_global`。
    # 断言它只会测出错误的东西（我第一版就是这么写错的）。


def test_global_still_wins_when_no_request_user():
    """没有请求用户时仍应回退到 _global（不要因为加了请求作用域就破坏原有回退）。"""
    from app.services import runtime_settings as rs

    rs.set_many("_global", {"probe_global_only": "G"})
    rs.set_request_user(None)
    rs.set_user(None)
    assert rs.get("probe_global_only") == "G"


def test_clearing_setting_restores_autodiscovery(client: TestClient, with_ffmpeg):
    """清空设置后应恢复正常（走自动查找）。"""
    import io

    from PIL import Image

    h = _auth(client)
    client.put("/api/settings", headers=h, json={"ffmpeg_path": ""})

    buf = io.BytesIO()
    Image.new("RGB", (32, 18), (1, 2, 3)).save(buf, format="PNG")
    r = client.post("/api/video/whitebox", headers=h,
                    files=[("frames", ("a.png", buf.getvalue(), "image/png"))],
                    data={"fps": "12"})
    assert r.status_code == 200, r.text
