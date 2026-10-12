"""配音（TTS）—— 文字 → 一段音频文件。

## 为什么用**子进程**调外部 venv，而不是 import

配音引擎（`edge-tts`）装在**项目外面**的工具目录里
（`D:\\AI\\little-crew-tools\\venv`），因为本项目有一条边界：
**项目目录只放代码，不放模型与工具**。

那我为什么不用 `PYTHONPATH` 指过去、直接 `import edge_tts`？

    `pip install --target` + `PYTHONPATH` 会让**外部包的依赖**出现在
    本项目解释器的 `sys.path` 上。`edge-tts` 依赖 `httpx` / `aiohttp`
    这类项目里也有的包，一旦版本不同，就会出现"项目跑着跑着 import 到
    了另一个版本"的怪问题 —— 而这种问题极难排查。

    子进程是**进程级隔离**：两边各用各的解释器与依赖，永远不会互相污染。
    代价只是每次多几十毫秒的启动开销，而配音本身就要几秒，可以忽略。

## 不装 / 没配好时的行为

`available()` 返回 False，`synthesize()` 抛 `TTSUnavailable`，错误信息带
**可执行的修复步骤**（跑哪个脚本），而不是一句"未安装"。
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

logger = logging.getLogger("little_crew")

#: 默认音色。挑它是因为它标着 `Warm`（温暖）且支持 `Novel`（小说/叙述），
#: 适合漫剧旁白；`YunxiNeural`（男声、Lively）适合年轻主角。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

#: 常用中文音色（供界面下拉；`edge-tts --list-voices` 是完整的，这里是精选）。
KNOWN_VOICES: tuple[tuple[str, str], ...] = (
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声 温暖（默认）"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声 活泼"),
    ("zh-CN-YunxiNeural", "云希 · 男声 阳光"),
    ("zh-CN-YunyangNeural", "云扬 · 男声 专业"),
    ("zh-CN-YunjianNeural", "云健 · 男声 激情"),
    ("zh-CN-YunxiaNeural", "云夏 · 男声 可爱"),
)


class TTSUnavailable(RuntimeError):
    """配音引擎不可用（没装 / 没配路径）。错误信息里带修复步骤。"""


@dataclass(frozen=True)
class TTSResult:
    """一次配音的产物。"""

    audio_path: str
    voice: str
    #: 秒。**估算值**（由 MP3 字节数推算），用于把静音视频撑到配音长度。
    #: 需要精确时长时应当用 ffprobe —— 但本项目只拿它做"大致对齐"，
    #: 为此再引一个依赖或解析工具不值得。
    duration_s: float


def tools_python() -> str | None:
    """外部工具目录里的 python（配音引擎所在解释器）。找不到返回 None。"""
    from app.services.whitebox_video import tools_dir

    d = tools_dir()
    if d is None:
        return None
    exe = d / "venv" / "Scripts" / "python.exe"
    if exe.is_file():
        return str(exe)
    # 非 Windows 布局（理论上用不上，但别写死）
    exe2 = d / "venv" / "bin" / "python"
    return str(exe2) if exe2.is_file() else None


def available() -> bool:
    """配音引擎是否可用（外部 venv 里装了 edge-tts）。"""
    py = tools_python()
    if not py:
        return False
    try:
        r = subprocess.run(
            [py, "-c", "import edge_tts"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _hint() -> str:
    return (
        "配音需要外部工具目录里的 edge-tts。修复：\n"
        "  1) 运行 `powershell -ExecutionPolicy Bypass -File scripts\\setup_tools.ps1`；\n"
        "  2) 或在「设置」里把「声音来源」改成「不要声音」/「视频模型原生」；\n"
        "  3) 若外部目录换了位置，改 start.bat 里的 `LITTLE_CREW_TOOLS`。"
    )


def synthesize(
    text: str,
    out_path: str | pathlib.Path,
    *,
    voice: str = "",
    speed: float = 1.0,
    timeout: float = 180.0,
) -> TTSResult:
    """把一段文字合成到 `out_path`（MP3）。

    Args:
        text:     台词。空文本直接报错（静默产出空文件会让下游更难查）。
        out_path: 输出路径（`.mp3`）。父目录会自动创建。
        voice:    音色名；空 = `DEFAULT_VOICE`。
        speed:    语速倍率（1.0 = 原速）。edge-tts 用**百分比**表达，
                  所以这里换算成 `+N%`：1.25 → `+25%`，0.8 → `-20%`。
        timeout:  子进程超时（秒）。默认 180 —— 长台词 + 网络慢时的余量。

    Raises:
        TTSUnavailable: 引擎不可用或调用失败（信息里带修复步骤）。
    """
    t = (text or "").strip()
    if not t:
        raise TTSUnavailable("台词为空，无法配音。" + _hint())

    py = tools_python()
    if not py or not available():
        raise TTSUnavailable("未找到可用的 TTS 引擎（外部 venv 里没有 edge-tts）。" + _hint())

    dst = pathlib.Path(out_path)
    dst.parent.mkdir(parents=True, exist_ok=True)

    v = (voice or "").strip() or DEFAULT_VOICE
    # 倍率 → 百分比。限制在 ±100% 内（edge-tts 对极端值会报错或语速失真）
    pct = int(round((float(speed) - 1.0) * 100))
    pct = max(-90, min(100, pct))
    rate = f"{pct:+d}%"

    # 长台词走**临时文件**而不是命令行参数：命令行有长度上限，
    # 一整段剧本很容易撞上（Windows 约 32K），而且台词里的引号/换行
    # 在命令行里需要转义，用文件就完全避免这两类问题。
    with tempfile.TemporaryDirectory(prefix="lc-tts-") as tmp:
        txt = pathlib.Path(tmp) / "line.txt"
        txt.write_text(t, encoding="utf-8")
        cmd = [py, "-m", "edge_tts", "--file", str(txt), "--voice", v,
               "--rate", rate, "--write-media", str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise TTSUnavailable(
                f"配音超时（{timeout:.0f} 秒）。台词可能过长，或网络不通。" + _hint()
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise TTSUnavailable(f"调用配音引擎失败：{exc}\n" + _hint()) from exc

    if r.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        detail = (r.stderr or r.stdout or "").strip()[-500:]
        if dst.exists() and dst.stat().st_size == 0:
            dst.unlink(missing_ok=True)   # 别留下 0 字节文件让下游以为成功
        raise TTSUnavailable(f"配音失败（引擎返回 {r.returncode}）：\n{detail}\n" + _hint())

    # 估算时长：edge-tts 默认输出 24 kHz 单声道 MP3，约 48 kbps。
    # 用它只为"把静音视频撑到与配音等长"，不需要精确。
    size = dst.stat().st_size
    est = round(size * 8 / 48_000, 2)
    logger.info("配音完成 voice=%s bytes=%d 约%.1fs -> %s", v, size, est, dst)
    return TTSResult(audio_path=str(dst), voice=v, duration_s=est)


def find_ffmpeg_or_none() -> str | None:
    """拿到 ffmpeg 路径（找不到返回 None，**不抛异常**）。

    混流/拼接需要它；调用方据此给出可读提示，而不是让异常冒到用户面前。
    """
    from app.services.whitebox_video import FFmpegNotFound, find_ffmpeg

    try:
        return find_ffmpeg()
    except FFmpegNotFound:
        return None
    except Exception:  # noqa: BLE001
        return None


def mux_audio(
    video: str | pathlib.Path,
    audio: str | pathlib.Path,
    out: str | pathlib.Path,
    *,
    mix_with_original: bool = False,
    original_volume: float = 0.25,
) -> str:
    """把音频**合进**视频。

    Args:
        video:  输入视频。
        audio:  输入音频（配音）。
        out:    输出视频。
        mix_with_original: True = 与视频**自带的声音混音**
                （TTS 为主，原声降到 `original_volume`）；False = 用配音
                **替换**原声。
        original_volume: 混音时原声的音量系数（0~1）。

    Returns:
        输出路径。

    Raises:
        TTSUnavailable: 缺 ffmpeg，或 ffmpeg 返回非零。信息里带修复步骤。

    为什么默认是"替换"而不是"混音"：
        用户选「TTS 配音」的意图就是"我要听到这句台词"，此时模型生成的
        对白往往与台词**不一致**，混在一起会变成两个人同时说话。
        「两者混音」是另一个独立选项，由调用方显式传入。
    """
    ff = find_ffmpeg_or_none()
    if not ff:
        raise TTSUnavailable(
            "找不到 ffmpeg，无法把配音合进视频。修复：\n"
            "  1) 运行 `powershell -ExecutionPolicy Bypass -File scripts\\setup_tools.ps1`；\n"
            "  2) 或在「设置」里填 `ffmpeg_path`；\n"
            "  3) 或把 ffmpeg 放进 PATH。"
        )

    src_v, src_a, dst = pathlib.Path(video), pathlib.Path(audio), pathlib.Path(out)
    dst.parent.mkdir(parents=True, exist_ok=True)

    # ⚠️ 混音前**必须**先确认视频里真有音轨。
    #
    # 实测踩到：`amix` 要两路输入，而 `[0:a]` 在无声视频上不存在 ——
    # ffmpeg 直接报错退出（"Stream specifier ':a' ... matches no streams"）。
    # 而"选了两者混音、但视频模型没产出声音"是**很常见**的组合
    # （Seedance / 本机 H3 都没有原生音频，用户仍可能把声音来源设成混音）。
    #
    # 此时**降级为替换**（而不是报错）：混音拿不到原声，等价于只有配音，
    # 替换正是这个语义。用户要的结果（能听到台词）也达成了。
    if mix_with_original and not _has_audio_stream(ff, src_v):
        logger.info("视频没有音轨，混音降级为替换：%s", src_v)
        mix_with_original = False

    if mix_with_original:
        # `amix` 把两路混成一轨。`duration=first` 表示以第一路（配音）长度为准，
        # 这样视频不会因为原声更长而被拉长。
        filt = (f"[0:a]volume={original_volume:.2f}[bg];"
                f"[1:a]volume=1.0[fg];"
                f"[bg][fg]amix=inputs=2:duration=first:dropout_transition=0[a]")
        cmd = [ff, "-y", "-i", str(src_v), "-i", str(src_a),
               "-filter_complex", filt, "-map", "0:v", "-map", "[a]",
               "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst)]
    else:
        # 替换原声：`-map 0:v` 只取画面，音轨全用配音。
        # `-shortest` 让输出不短于两者中较短的那个，避免尾部拖一段静音。
        cmd = [ff, "-y", "-i", str(src_v), "-i", str(src_a),
               "-map", "0:v", "-map", "1:a",
               "-c:v", "copy", "-c:a", "aac", "-shortest", str(dst)]

    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    if r.returncode != 0 or not dst.is_file():
        raise TTSUnavailable(
            f"混流出错（ffmpeg 返回 {r.returncode}）：\n{(r.stderr or '')[-600:]}"
        )
    logger.info("配音混流完成 mix=%s -> %s", mix_with_original, dst)
    return str(dst)


def ffmpeg_available() -> bool:
    """能否找到 ffmpeg（供界面标注"本机未就绪"）。"""
    return (find_ffmpeg_or_none() is not None) or (shutil.which("ffmpeg") is not None)


def _has_audio_stream(ffmpeg: str, video: pathlib.Path) -> bool:
    """视频里有没有音轨。

    为什么不直接用 `ffprobe`：装的是 `ffmpeg` 单文件，**不一定**带着
    `ffprobe`（本机这份就没有）。而"有没有音轨"用 `ffmpeg -i` 的
    stderr 就能判断 —— 那里会列出全部 `Stream #0:x`。

    这样也顺带避免了"为了判断一个布尔值再引一个依赖"。
    """
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except Exception:  # noqa: BLE001  探测失败时**保守**当作没有音轨
        return False
    # `-i` 只做探测、不产出，所以返回非零是正常的；看 stderr 内容即可
    return "Audio:" in (r.stderr or "")
