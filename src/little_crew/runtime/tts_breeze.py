"""breeze-tts-2 配音后端（本机 GGUF + Vulkan）—— §3.11 的正式后端。

## 为什么用它而不是 SAPI

| | SAPI | breeze-tts-2 |
|---|---|---|
| 音质 | 机械 | 自然 |
| 角色区分 | 靠不同预设音色（本机子进程只认到 2 个） | `--voice` / `--ref-audio` / **`--instruction`** |
| 确定性 | 是 | **是**（`--seed` 默认 42，实测两次输出字节相同） |
| 成本 | 0 | 0（本地 3.4GB 模型，24kHz 单声道） |
| 耗时 | 0.6 s | **约 3.8 s/句**（首音 3.9 s 是模型加载） |

SAPI 保留为**降级后端**：模型坏/显存不足时还能出声。

## 三种区分角色的手段

1 `--voice <已存音色>` —— 需要事先 `--save-voice`
2 `--ref-audio <wav> --ref-text <转写>` —— 参考音克隆
3 **`--instruction <文字描述>`** —— **不需要样本**，用文字描述声音
   （例："沙哑的老妇人，说话很慢"）

`--instruction` 对我们最实用：6 个角色不可能都录样本。
"""
from __future__ import annotations

import hashlib
import subprocess
import wave
from pathlib import Path

#: 默认安装位置（可用环境变量覆盖，便于换机器）
DEFAULT_ROOT = r"D:\AI\local-ai\tts"
#: 合成超时（秒）。首句要加载 3.4GB 模型，给足余量。
TIMEOUT_S = 180.0
#: 默认种子 —— 同种子同文本**输出字节相同**（实测）
DEFAULT_SEED = 42


def root_dir() -> Path:
    import os

    return Path(os.environ.get("BREEZE_TTS_ROOT") or DEFAULT_ROOT)


def exe_path() -> Path:
    d = root_dir()
    return d / "bin" / "breeze-tts-2-v0.1.6-windows-x64-vulkan" / "breeze-cli.exe"


def model_path() -> Path:
    return root_dir() / "models" / "breeze-tts-2-q8_0.gguf"


def voices_dir() -> Path:
    return root_dir() / "voices"


def available() -> dict:
    """后端是否可用（不跑合成，只看文件在不在）。"""
    return {"exe": exe_path().is_file(), "model": model_path().is_file(),
            "voicesDir": voices_dir().is_dir(),
            "voices": list_voices()}


def list_voices() -> list[str]:
    """已存音色名（`voices/*.breeze` 的文件名主干）。"""
    d = voices_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.breeze"))


def synth(text: str, out_wav, *, voice: str = "", instruction: str = "",
          ref_audio: str = "", ref_text: str = "", seed: int | None = DEFAULT_SEED,
          timeout: float = TIMEOUT_S) -> dict:
    """合成一句台词。返回 `{ok, seconds, voice, seed, error, bytes, sha256}`。

    **失败不静默**：`ok=False` + `error`，调用方必须决定降级方式并记录。
    """
    text = str(text or "").strip()
    if not text:
        return _fail("empty-text", voice)
    if not exe_path().is_file() or not model_path().is_file():
        return _fail("backend-missing", voice)
    out = Path(str(out_wav))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    cmd = [str(exe_path()), str(model_path()), "--text", text,
           "--output", str(out), "--seed", str(int(seed)),
           "--voices-dir", str(voices_dir())]
    # ⚠️ **两者要同时给**（2026-11-08 用户反馈"音色一直在变"）：
    # 原来写成 `if instruction: ... elif voice:` —— 只要给了语气描述，
    # `--voice`（音色锚点）就被跳过，于是**每句音色都跟着提示词漂**。
    # 实测 CLI 支持 `--voice` + `--instruction` 并用：
    #   voice 锁音色，instruction 只调语气/节奏。
    if voice:
        cmd += ["--voice", str(voice)]
    if instruction:
        cmd += ["--instruction", str(instruction)]
    if ref_audio:
        cmd += ["--ref-audio", str(ref_audio)]
        if ref_text:
            cmd += ["--ref-text", str(ref_text)]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return _fail(f"timeout({timeout:.0f}s)", voice)
    except Exception as e:                                    # noqa: BLE001
        return _fail(f"{type(e).__name__}: {e}", voice)

    if p.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        tail = [x for x in (p.stderr or p.stdout or "").splitlines() if x.strip()]
        return _fail((tail[-1] if tail else f"exit {p.returncode}")[:200], voice)

    data = out.read_bytes()
    return {"ok": True, "seconds": wav_seconds(out), "voice": voice,
            "seed": int(seed), "error": "", "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()[:16]}


def wav_seconds(path) -> float:
    """读 wav 时长（标准库 `wave`）。**不猜时长**。"""
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
    except Exception:                                         # noqa: BLE001
        return 0.0
    return round(frames / float(rate), 3) if rate > 0 else 0.0


def _fail(reason: str, voice: str) -> dict:
    return {"ok": False, "seconds": 0.0, "voice": voice, "seed": 0,
            "error": reason, "bytes": 0, "sha256": ""}
