"""白模参考视频导出链：帧序列 → ffmpeg 编码 mp4 → 归档路径。

## 这条链是什么（澄清一个常见误解）

用户问过"白模视频不是 LLM 做的吗为啥会用显卡"—— **三个环节各用各的**：

    ①  LLM 出「白模规格 JSON」      用 LLM（DeepSeek）
            ↓  体块 / 关节 / 比例
    ②  前端 Three.js 逐帧渲染成 PNG  用浏览器 WebGL（**不用显卡**，软件渲染也行）
            ↓  一帧一张图
    ③  后端 ffmpeg 编码成 mp4        用 ffmpeg（**CPU**，代码里不碰显卡）

那个"本地 8GB 建议 832×480"说的是**下游的 H3 视频模型**（MiniMax-H3），
不是渲染白模：白模视频最终当**参考视频**喂给 H3 复刻运镜，H3 才吃显存。

## 实测踩的 bug（2026-10-08）

导出白模视频得到：

    outputs/_global/未命名工程/白模/previs_artist/no-session-20261008-14/

**账号丢成 `_global`、会话丢成 `no-session-*`**。
根因：`asset_paths.asset_dir` 读 thread-local 的
`current_user()/current_session()/current_project()`，而 `/api/video/whitebox`
虽然由 `auth_guard` 做了认证，**却从没把 uid 写进 thread-local**。

**认证 ≠ 上下文** —— 这和 `character_designer` 那个 `_global` 是同一类 bug。
项目里其它需要归档的端点都用 `rs.set_user(uid)` + `finally: rs.set_user(None)`。
"""

from __future__ import annotations

import inspect

import pytest


# ----------------------------------------------------------------------
# 归档归属：账号 / 会话
# ----------------------------------------------------------------------

def _endpoint_source() -> str:
    """把两个相关函数（端点 + 干活的）源码拼起来检查。"""
    import app.main as m

    parts = []
    for name in ("upload_whitebox_video", "_encode_whitebox_video"):
        fn = getattr(m, name, None)
        if fn is not None:
            parts.append(inspect.getsource(fn))
    assert parts, "找不到白模视频端点"
    return "\n".join(parts)


def _call_names(fn) -> set[str]:
    """函数体里**真实调用**过的函数名（含 `a.b(...)` 记作 `a.b`）。

    ⚠️ 必须走 AST，不能字符串匹配 —— 我第一版用 `"set_user" in src`，
    结果**命中的是注释里的字**（注释里就写着 "`rs.set_user(uid)` + finally"），
    于是撤掉真实调用后测试仍然全绿，等于没测。
    """
    import ast

    tree = ast.parse(inspect.getsource(fn))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                names.add(f"{f.value.id}.{f.attr}")
    return names


def test_endpoint_sets_user_context_before_archiving():
    """**核心回归**：必须在归档前把 uid 写进 thread-local。

    不做的话 `asset_dir` 会取到 `_global` —— 产物认不出属于谁。
    """
    import app.main as m

    calls = _call_names(m.upload_whitebox_video)
    assert "_rs.set_user" in calls, (
        f"端点没有真实调用 set_user → 白模视频会落进 outputs/_global/。"
        f"实际调用：{sorted(calls)}")
    # 必须有还原，避免污染同线程后续请求
    assert "_rs.set_user" in calls and "finally" in inspect.getsource(
        m.upload_whitebox_video), "设了 uid 但没在 finally 里还原 —— 会污染同一线程"


def test_endpoint_does_not_archive_before_setting_context():
    """顺序也要对：`set_user` 必须出现在 `_encode_whitebox_video` 之前。

    在**源码行号**上比较（比 AST 顺序检查简单且够用），
    并且先把注释行剔除，免得又被注释里的字骗到。
    """
    import app.main as m

    raw = inspect.getsource(m.upload_whitebox_video)
    lines = []
    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("#") or s.startswith("`"):
            continue
        lines.append(ln)
    code = "\n".join(lines)

    i_set = code.find("_rs.set_user")
    i_enc = code.find("_encode_whitebox_video")
    assert i_set != -1, "没有调用 set_user"
    assert i_enc != -1, "没有调用 _encode_whitebox_video"
    assert i_set < i_enc, "set_user 必须在归档（编码）之前调用"


def test_endpoint_archives_under_asset_paths():
    """必须走统一归档规则（而不是 `outputs/whitebox/{uuid}/` 那种裸路径）。"""
    src = _endpoint_source()
    assert "asset_paths" in src and "asset_dir" in src


def test_endpoint_attributes_to_previs_artist():
    """产出者归属「白模预演师」—— 白模就是它产出的。"""
    src = _endpoint_source()
    assert '"previs_artist"' in src


def test_archiving_path_uses_account_when_context_is_set(tmp_path, monkeypatch):
    """端到端（不启服务）：设好上下文后，`asset_dir` 必须带账号。

    这是那个 bug 的最小复现：**设了 uid 就有账号目录，没设就是 `_global`**。
    """
    from app.services import asset_paths, runtime_settings as rs

    out = str(tmp_path / "outputs")

    # 不设上下文 → 落 _global（这就是 bug 现场）
    rs.set_user(None)
    d_global = asset_paths.asset_dir(out, "previs_artist", "frames")
    assert "_global" in d_global.as_posix(), "未设上下文时应当落 _global（对照）"

    # 设了上下文 → 带账号
    rs.set_user("director")
    try:
        d_user = asset_paths.asset_dir(out, "previs_artist", "frames")
    finally:
        rs.set_user(None)
    assert "director" in d_user.as_posix()
    assert "_global" not in d_user.as_posix()


def test_encode_helper_has_its_imports():
    """⚠️ 抽函数时 import 必须跟着搬。

    实测踩过：把函数体从端点搬到 `_encode_whitebox_video`，
    却把 `from app.services.whitebox_video import encode_frames_to_mp4` 留在原地
    → 导出直接 400 `name 'encode_frames_to_mp4' is not defined`。
    """
    import app.main as m

    src = inspect.getsource(m._encode_whitebox_video)
    for name in ("encode_frames_to_mp4", "probe_duration",
                 "upload_for_video_generation", "MAX_REF_VIDEO_SECONDS"):
        assert name in src, f"_encode_whitebox_video 里没引用 {name}"
    # 而这些名字必须真的能取到（import 在函数内或模块级都行）
    assert hasattr(m._encode_whitebox_video, "__code__")
    # 逐个确认它们在模块里可解析
    import app.core.config as cfg
    import app.services.whitebox_video as wv
    assert hasattr(cfg, "settings")
    for n in ("encode_frames_to_mp4", "probe_duration",
              "upload_for_video_generation", "MAX_REF_VIDEO_SECONDS"):
        assert hasattr(wv, n), f"whitebox_video 里没有 {n}"


# ----------------------------------------------------------------------
# 编码规格（不依赖前端的纯后端测试）
# ----------------------------------------------------------------------

def test_encode_frames_to_mp4_produces_playable_mp4(tmp_path):
    """真跑一次 ffmpeg：几张小 PNG → 一个能读时长的 mp4。

    跳过条件：本机没有 ffmpeg（这时给的是"可执行的修复建议"，不是崩）。
    """
    from PIL import Image

    from app.services.whitebox_video import (
        FFmpegNotFound,
        encode_frames_to_mp4,
        probe_duration,
    )

    frames = []
    for i in range(6):
        p = tmp_path / f"{i:05d}.png"
        Image.new("RGB", (64, 64), (i * 30, 100, 200)).save(p)
        frames.append(p)

    out = tmp_path / "whitebox-test.mp4"
    try:
        encode_frames_to_mp4(frames, out, fps=24)
    except FFmpegNotFound as exc:
        pytest.skip(f"本机无 ffmpeg：{exc}")

    assert out.exists() and out.stat().st_size > 0
    dur = probe_duration(out)
    assert dur is not None, "编码出的 mp4 读不到时长"
    assert 0.1 < dur < 2.0, f"时长异常：{dur}"


def test_resolution_advice_targets_the_downstream_model():
    """那句"本地 8GB 建议 832×480"是针对**下游 H3** 的，不是白模渲染。

    这条守的是"别把说明写错"——用户就是被这句话问住的
    （"白模视频不是 LLM 做的吗为啥会用显卡"）。
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    src = (root / "web" / "src" / "previs" / "PrevisPage.tsx").read_text(encoding="utf-8")
    m = re.search(r"（H3[^）]*）", src)
    assert m, "找不到尺寸说明"
    note = m.group(0)
    # 必须有 H3 字样（点明是下游模型的要求）
    assert "H3" in note
    # 不能写成"渲染/白模 需要 8GB 显存"这类误导
    assert "渲染" not in note and "白模" not in note, (
        f"这句像是说白模渲染要显存，会误导：{note}")
