"""白模参考视频：帧序列 → mp4 → 上传换 `file_id`。

这是"白模 + 参考视频"这条路的最后一段。前面几步都已就位：
    预演编辑器（人形骨架 / 环境 / 多机位 / 锁定跟随 / 绘制轨道）
    → 帧序列（剪影或深度）
    → **本模块**（编码 mp4 + 上传）
    → 云端生成时用 `role=reference_video` 复刻运镜与走位

为什么走云端上传而不是"起一个静态服务器给公网看"
--------------------------------------------------
MiniMax 的 `video_url` 要么是公网可访问地址，要么是它自己的
`mm_file://{file_id}`。用户多半跑在本机（127.0.0.1），**没有公网入口**：
  - 开静态服务器 + 内网穿透：要装额外东西、要维护映射、还不稳；
  - 用官方文件接口上传：一个 HTTP 请求，拿回 file_id 就能用。
所以选后者。限制：mp4/mov ≤50MB，**有效期 7 天**（过期要重传）。

ffmpeg 从哪来
-------------
本机 PATH 里**没有** ffmpeg。系统里只在另一个项目的 `node_modules` 下
发现一个 ffmpeg-static。
**不把那个路径硬编码进来**——它属于另一个项目，对方升级/清理就会断，
而且把一个 79MB 的二进制暗中绑进来也不是用户能预期的事。
所以做成可配置查找，并在找不到时给出**可执行的**修复建议：

    1) 设置项 `ffmpeg_path`（推荐：直接指到任意一份 ffmpeg 可执行文件）
    2) 环境变量 `LITTLE_CREW_FFMPEG`
    3) PATH 里的 `ffmpeg`
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import tempfile
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

#: 帧序列编码成 mp4 的目标规格。
#:
#: 为什么固定 24fps 与 yuv420p：
#:   - 24fps 是预演导出的默认步长，改帧率会让时长与设计不符；
#:   - yuv420p 是最通用的像素格式，不少云端解码器不认 yuv444p。
VIDEO_FPS = 24
PIX_FMT = "yuv420p"


class FFmpegNotFound(RuntimeError):
    """找不到 ffmpeg 时的可读错误（带修复步骤）。"""


def find_ffmpeg(explicit: str = "") -> str:
    """按 设置项 → 环境变量 → PATH → 内置包 的顺序找 ffmpeg。

    Args:
        explicit: 设置项 `ffmpeg_path` 的值（最高优先级）。

    Returns:
        可执行文件路径。

    Raises:
        FFmpegNotFound: 都没找到。错误信息里带**可执行的修复步骤**，
            而不是只说"未安装"。

    为什么把 `imageio-ffmpeg` 放在**最后**而不是最前：
        它随 pip 安装、版本可控，所以适合当"开箱即用"的兜底；
        但如果用户明确指定了路径（或 PATH 里有自己的版本），
        那应优先用用户的——否则设了也不生效，很难排查。
    """
    tried: list[str] = []

    # 用户**显式配置**了路径却没找到 → 立即报错，不悄悄回退。
    #
    # 为什么要这样：若静默回退到 PATH 或内置版本，用户手一抖打错路径
    # （或者换了台机器、路径失效）会看到"一切正常"，实际用的是另一个
    # 版本的 ffmpeg。这类"配置错了却没人告诉你"最难排查——
    # 本项目的其他部分（提供者解析）也遵循同一条原则：不静默取默认值。
    for label, cand in (("设置项 ffmpeg_path", explicit),
                        ("环境变量 LITTLE_CREW_FFMPEG",
                         os.environ.get("LITTLE_CREW_FFMPEG", ""))):
        if not cand:
            continue
        p = pathlib.Path(cand)
        if p.is_file():
            return str(p)
        # 允许给**目录**：在其中找 ffmpeg 可执行文件。
        # 不能只匹配 `ffmpeg.exe` / `ffmpeg` 这两个死名字——
        # 不少发行包带版本后缀（例如 imageio-ffmpeg 里的
        # `ffmpeg-win-x86_64-v7.1.exe`），死名字会找不到。
        if p.is_dir():
            hits = sorted(
                q for q in p.iterdir()
                if q.is_file() and q.stem.lower().startswith("ffmpeg")
                and q.suffix.lower() in (".exe", "")
            )
            if hits:
                return str(hits[0])
        raise FFmpegNotFound(
            f"{label} 指向的路径不存在：{cand}\n"
            "请改正它，或把它清空以使用自动查找（PATH → 内置 imageio-ffmpeg）。"
        )

    which = shutil.which("ffmpeg")
    tried.append("PATH 中的 ffmpeg")
    if which:
        return which

    # 外部工具目录的 `bin\ffmpeg.exe`。
    #
    # 为什么不再用 `imageio-ffmpeg` 兜底：那是**装在项目 venv 里**的包，
    # 而 84 MB 里真正有用的只有 `ffmpeg.exe` 一个文件。用户明确要求
    # "本地模型不污染本项目" —— 所以把那个 exe 取出来放到外部目录，
    # 项目里只留"去哪儿找"的逻辑，不留二进制。
    ext = tools_bin_ffmpeg()
    tried.append(f"外部工具目录 {ext or '（未配置）'}")
    if ext:
        return ext

    raise FFmpegNotFound(
        "找不到 ffmpeg，无法把帧序列编码成 mp4。\n"
        "已尝试：" + "、".join(tried) + "\n"
        "四种解法任选其一：\n"
        "  1) 运行 `scripts\\setup_tools.ps1` 把 ffmpeg 装到外部工具目录（推荐）；\n"
        "  2) 在「设置」里填 `ffmpeg_path`，指向任意一份 ffmpeg 可执行文件；\n"
        "  3) 设环境变量 `LITTLE_CREW_FFMPEG` 指向它；\n"
        "  4) 把 ffmpeg 放进 PATH（Windows 可用 `choco install ffmpeg`，需管理员）。"
    )


def tools_dir() -> pathlib.Path | None:
    """外部工具与模型目录（`LITTLE_CREW_TOOLS`），不存在时返回 None。

    **不报错**：没有它只是少了本地配音/拼接能力，云端功能不受影响。
    启动时就因为缺一个可选目录而失败，对"只想用云端"的用户不合理。
    """
    raw = (settings.little_crew_tools or os.environ.get("LITTLE_CREW_TOOLS", "")).strip()
    if not raw:
        return None
    p = pathlib.Path(raw)
    return p if p.is_dir() else None


def tools_bin_ffmpeg() -> str | None:
    """外部工具目录里的 ffmpeg 可执行文件（找不到返回 None）。"""
    d = tools_dir()
    if d is None:
        return None
    bin_dir = d / "bin"
    if not bin_dir.is_dir():
        return None
    # 不写死 `ffmpeg.exe`：发行包常带版本后缀，死名字会找不到
    for q in sorted(bin_dir.iterdir()):
        if q.is_file() and q.stem.lower().startswith("ffmpeg") and q.suffix.lower() in (".exe", ""):
            return str(q)
    return None


def encode_frames_to_mp4(
    frames: list[pathlib.Path],
    out_path: pathlib.Path,
    *,
    fps: int = VIDEO_FPS,
    ffmpeg: str = "",
    crf: int = 18,
) -> pathlib.Path:
    """把一串图片编码成 mp4（H.264）。

    实现用 ffmpeg 的 **concat demuxer**（而不是 image2 + `%04d` 通配）：
    concat 允许每帧用完整路径、文件名单调递增，不依赖目标目录里
    恰好只有这一串帧（`%04d` 模式在混入其它文件时会静默取错帧）。

    帧序列的时长必须**精确**：参考视频过长会被云端拒绝（≤15 秒），
    所以编码后校验时长并在超限时明确报错，而不是上传后才失败。

    Args:
        frames: 图片路径，按播放顺序。
        out_path: 输出 mp4。
        fps: 帧率。
        ffmpeg: ffmpeg 路径（空则自动查找）。
        crf: H.264 质量（18 ≈ 视觉无损；越小越好越大越糊）。
    """
    if not frames:
        raise ValueError("没有帧可编码。")
    if out_path.suffix.lower() != ".mp4":
        raise ValueError(f"输出必须是 .mp4：{out_path}")

    exe = find_ffmpeg(ffmpeg)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # concat 清单里每帧一行 `file '<path>'`，时长用 1/fps 精确控制
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        list_path = pathlib.Path(fh.name)
        for p in frames:
            # ffmpeg concat 里的单引号要转义；Windows 路径的反斜杠统一成正斜杠
            safe = str(pathlib.Path(p).resolve()).replace("\\", "/").replace("'", r"'\''")
            fh.write(f"file '{safe}'\n")
            fh.write(f"duration {1.0 / fps:.6f}\n")
        # concat demuxer 需要末行重复最后一帧，否则最后一帧时长会丢
        last = str(pathlib.Path(frames[-1]).resolve()).replace("\\", "/").replace("'", r"'\''")
        fh.write(f"file '{last}'\n")

    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        # 输出**固定帧率**。每帧时长已由 concat 清单里的 duration 精确给出；
        # 这里再给 -vsync vfr 会与 -r 冲突（实测报
        # "One of -r/-fpsmax was specified together a non-CFR -vsync/-fps_mode"）。
        "-r", str(fps),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", str(crf),
        "-pix_fmt", PIX_FMT,
        # 让 mp4 的 moov 前置：某些云端解码要求可流式读取
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    finally:
        list_path.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 编码失败（exit {proc.returncode}）：{(proc.stderr or '')[:500]}"
        )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RuntimeError("ffmpeg 报告成功但没有产出文件。")

    logger.info("[whitebox] 编码完成 %s（%d 帧, %.1f KB）",
                out_path.name, len(frames), out_path.stat().st_size / 1024)
    return out_path


def probe_duration(mp4: pathlib.Path, ffmpeg: str = "") -> Optional[float]:
    """读取 mp4 时长（秒）。读不出来时返回 None（不因此让流程失败）。

    用一个极短的空输出跑 ffmpeg，从 stderr 里解析 `Duration:`——
    这样不依赖 ffprobe（ffmpeg-static 之类的发行包常常只带 ffmpeg）。
    """
    exe = find_ffmpeg(ffmpeg)
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-i", str(mp4)],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:  # noqa: BLE001
        return None
    import re

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


# ------------------------------------------------------------------
# 上传到 MiniMax，换 file_id
# ------------------------------------------------------------------

#: 官方对 reference_video 的限制
MAX_REF_VIDEO_MB = 50
#: 参考视频总时长上限（官方：每段 2~15 秒、总计 ≤15 秒）
MAX_REF_VIDEO_SECONDS = 15


def upload_for_video_generation(
    path: pathlib.Path,
    api_key: str,
    *,
    base_url: str = "https://api.minimax.io",
    timeout: int = 300,
) -> str:
    """把素材上传到 MiniMax，返回可用于生成请求的 `mm_file://{file_id}`。

    用途 `video_generation_input` 一次覆盖首帧图 / 参考图 / 参考视频 / 参考音频。
    官方会在**上传时**校验规格，不合规直接 400 且不保留文件——
    所以这里先做本地预检，省一次往返。

    Returns:
        `"mm_file://123456"` 形式的字符串，直接塞进请求的 `url` 字段。
    """
    import httpx

    from app.services.url_guard import assert_public_url

    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"素材不存在：{p}")

    size_mb = p.stat().st_size / (1024 * 1024)
    ext = p.suffix.lower()
    if ext in (".mp4", ".mov"):
        if size_mb > MAX_REF_VIDEO_MB:
            raise ValueError(
                f"参考视频 {size_mb:.1f}MB 超出上限 {MAX_REF_VIDEO_MB}MB。"
                "可降低分辨率或用更短片段。"
            )
    elif ext in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"):
        if size_mb > 30:
            raise ValueError(f"参考图 {size_mb:.1f}MB 超出上限 30MB。")
    elif ext in (".wav", ".mp3"):
        if size_mb > 15:
            raise ValueError(f"参考音频 {size_mb:.1f}MB 超出上限 15MB。")
    else:
        raise ValueError(
            f"不支持的素材类型 {ext}（图片 jpg/png/webp/heic；视频 mp4/mov；音频 wav/mp3）。"
        )

    base = (base_url or "https://api.minimax.io").rstrip("/")
    url = f"{base}/v1/files/upload"
    assert_public_url(url, context="minimax_base_url")

    with httpx.Client(timeout=timeout) as client:
        with p.open("rb") as fh:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (p.name, fh, "application/octet-stream")},
                data={"purpose": "video_generation_input"},
            )
    if resp.status_code >= 400:
        raise RuntimeError(f"上传失败({resp.status_code})：{resp.text[:300]}")

    data = resp.json()
    base_resp = data.get("base_resp") or {}
    code = base_resp.get("status_code")
    if code not in (0, None):
        raise RuntimeError(
            f"上传被拒（status_code={code}）：{base_resp.get('status_msg') or resp.text[:200]}"
        )
    file_id = (data.get("file") or {}).get("file_id")
    if not file_id:
        raise RuntimeError(f"上传成功但未返回 file_id：{str(data)[:300]}")

    logger.info("[whitebox] 已上传 %s（%.1fMB）→ file_id=%s", p.name, size_mb, file_id)
    # 官方要求以 mm_file:// 前缀引用，且 **7 天有效**（过期需重传）
    return f"mm_file://{file_id}"
