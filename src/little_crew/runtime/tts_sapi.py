"""SAPI 配音（离线、免费、无第三方依赖）—— §3.11 的第一个可用后端。

## 为什么用 SAPI

- **离线**：不花钱、不怕网络、可复现
- **系统自带**：核心层仍然 `dependencies = []`
- **够用来验证规则**：音色绑定、时长对齐、失败兜底这三件事与音质无关

## 调用方式

通过 PowerShell 调 `System.Speech.Synthesis` 写 wav：
**子进程 + 文件输出**，不在 Python 里持有 COM 对象（避免线程/生命周期问题）。

## 失败语义（§3.11.2 问题 4）

**不许静默**：失败返回 `ok=False` + `error`，由调用方决定
"降级字幕"还是"整段跳过"，并**记进交付件**。
"""
from __future__ import annotations

import subprocess
import tempfile
import wave
from pathlib import Path

#: 单次合成的超时（秒）。SAPI 很快，10 秒足够；卡住就是坏了。
TIMEOUT_S = 10.0

_PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$v = '{voice}'
if ($v) { try { $s.SelectVoice($v) } catch { Write-Error "voice-missing:$v"; exit 3 } }
$s.Rate = {rate}
$s.SetOutputToWaveFile('{out}')
$s.Speak([Console]::In.ReadToEnd())
$s.Dispose()
"""


def synth(text: str, out_wav, *, voice: str = "", rate: int = 0,
          timeout: float = TIMEOUT_S) -> dict:
    """合成一段台词到 `out_wav`。返回 `{ok, seconds, voice, error, bytes}`。

    `seconds` 用 `wave` 读帧数算出来 —— **不信任任何"应该有多长"的猜测**。
    """
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "seconds": 0.0, "voice": voice,
                "error": "empty-text", "bytes": 0}
    out = Path(str(out_wav))
    out.parent.mkdir(parents=True, exist_ok=True)
    r = max(-10, min(10, int(rate or 0)))
    script = _PS.replace("{voice}", str(voice or "").replace("'", "''")) \
                .replace("{rate}", str(r)) \
                .replace("{out}", str(out).replace("'", "''"))
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            input=text, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": 0.0, "voice": voice,
                "error": f"timeout({timeout}s)", "bytes": 0}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "seconds": 0.0, "voice": voice,
                "error": f"{type(e).__name__}: {e}", "bytes": 0}

    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip().splitlines()
        err = msg[-1] if msg else f"exit {p.returncode}"
        return {"ok": False, "seconds": 0.0, "voice": voice,
                "error": err[:200], "bytes": 0}
    if not out.is_file() or out.stat().st_size == 0:
        return {"ok": False, "seconds": 0.0, "voice": voice,
                "error": "no-output-file", "bytes": 0}
    return {"ok": True, "seconds": wav_seconds(out), "voice": voice,
            "error": "", "bytes": out.stat().st_size}


def wav_seconds(path) -> float:
    """读 wav 时长（只用标准库 `wave`）。读不了返回 0.0。"""
    try:
        with wave.open(str(path), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
    except Exception:                                         # noqa: BLE001
        return 0.0
    if rate <= 0:
        return 0.0
    return round(frames / float(rate), 3)


def available_voices() -> list[str]:
    """本机装了哪些音色（失败返回空列表，不抛）。"""
    script = ("Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "$s.GetInstalledVoices()|%{$_.VoiceInfo.Name};$s.Dispose()")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT_S)
    except Exception:                                         # noqa: BLE001
        return []
    return [x.strip() for x in (p.stdout or "").splitlines() if x.strip()]
