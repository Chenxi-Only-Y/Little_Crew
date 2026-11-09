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
    # ---- 后端选择（2026-11-09 实测后改）----
    #
    # 实测（RTX 4060 Laptop / Q8_0 / Vulkan）：
    #   CLI    : 7.5s/句，其中**约 6s 是每句重新加载 2GB 模型**，生成只要 1.1s
    #            争用严重时 20~180s（还超时过一次）
    #   server : **0.89s/句（1.52x 实时）**，模型只加载一次
    #
    # 所以默认：**server 活着就用 server**；CLI 仅作兜底。
    # `BREEZE_TTS_PREFER=cli` 可强制回到 CLI（例如没有常驻服务时）。
    import os as _os_tts

    _prefer = (_os_tts.environ.get("BREEZE_TTS_PREFER") or "auto").strip().lower()
    if _prefer != "cli" and server_alive():
        r = synth_server(text, out_wav, voice=voice,
                         instruction=instruction, seed=seed)
        if r.get("ok"):
            r.setdefault("backend", "server")
            r.setdefault("voice", voice)
            return r
        # 服务端失败 -> 落到 CLI（并保留原因在 error 里，不静默）

    if not exe_path().is_file() or not model_path().is_file():
        # ⚠️ 2026-11-09：本机发行版把 `breeze-cli.exe` 换成了 `breeze-server.exe`，
        # 于是 CLI 分支一律 `backend-missing` —— 而**上层只看到"配音失败"**，
        # 真实原因（缺后端）被埋在错误字符串里，整片配音静默没了。
        # 这里改成：CLI 不在就试服务端（`/v1/audio/speech`，OpenAI 兼容）。
        if server_alive():
            r = synth_server(text, out_wav, voice=voice,
                             instruction=instruction, seed=seed)
            r.setdefault("backend", "server")
            r.setdefault("voice", voice)
            return r
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


#: 服务端地址（`breeze-server` 的 OpenAI 兼容接口）
SERVER_ENV = "BREEZE_TTS_ENDPOINT"
DEFAULT_SERVER = "http://127.0.0.1:8110"


def server_endpoint() -> str:
    import os

    return (os.environ.get(SERVER_ENV) or DEFAULT_SERVER).rstrip("/")


def server_alive(timeout_s: float = 2.0) -> bool:
    """服务端在不在（`/health`）。**失败不抛**：这是探测。"""
    try:
        import json as _j
        import urllib.request as _u

        with _u.urlopen(f"{server_endpoint()}/health", timeout=timeout_s) as r:
            return bool(_j.loads(r.read().decode("utf-8", "replace")).get("status") == "ok")
    except Exception:  # noqa: BLE001
        return False


def duration_of(path) -> float:
    """wav 时长（秒）。读不出来返回 0 —— 调用方按失败处理。"""
    import wave
    from pathlib import Path as _P

    f = _P(str(path))
    try:
        with wave.open(str(f), "rb") as w:
            rate = float(w.getframerate() or 0)
            width, ch = int(w.getsampwidth() or 2), int(w.getnchannels() or 1)
            frames = int(w.getnframes())
        # ⚠️ `breeze-server` 返回的是**流式 wav**：头里 nframes 写成 0x7FFFFFFF，
        # 直接算会得到 89478 秒（实测）。按字节反推才准。
        if rate <= 0:
            return 0.0
        if frames <= 0 or frames > 10_000_000:
            frames = max(0, (f.stat().st_size - 44) // max(1, width * ch))
        return frames / rate
    except Exception:  # noqa: BLE001
        return 0.0


def synth_server(text: str, out_wav, *, voice: str = "", instruction: str = "",
                 seed: int | None = None) -> dict:
    """走 `breeze-server` 的 `/v1/audio/speech`。

    背景（2026-11-09）：本机 TTS 发行版把 `breeze-cli.exe` 换成了 `breeze-server.exe`，
    CLI 后端直接报 `backend-missing`，全片配音因此静默失败
    （表现是"配音 3 句全失败 → 没有台词 → 混流不跑"）。
    """
    import json as _json
    import urllib.error as _ue
    import urllib.request as _ur
    from pathlib import Path as _P

    body = {"input": str(text), "response_format": "wav"}
    if voice:
        body["voice"] = str(voice)
    if instruction:
        body["instruction"] = str(instruction)
    if seed is not None:
        body["seed"] = int(seed)
    req = _ur.Request(
        f"{server_endpoint()}/v1/audio/speech",
        data=_json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=300) as r:
            data = r.read()
    except _ue.HTTPError as e:
        return {"ok": False, "seconds": 0.0,
                "error": f"server-http-{e.code}: {e.read()[:120]!r}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": 0.0, "error": f"server-{type(e).__name__}: {e}"}
    out = _P(str(out_wav))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    # **必须归一化**：流式头会让后续 wave 写回直接 struct 溢出
    normalize_wav(out)
    secs = duration_of(out)
    return {"ok": bool(secs > 0), "seconds": round(secs, 3), "error": "",
            "backend": "server", "bytes": len(data)}


def normalize_wav(path) -> bool:
    """把 wav 头改写成**真实帧数**。

    `breeze-server` 返回的是流式 wav：`nframes = 0x7FFFFFFF`。
    `wave` 读没问题，但**写回**（去静音 / 补句尾静音）会抛
    `struct.error: 'L' format requires 0 <= number <= 4294967295`
    —— 2026-11-09 真跑整段配音就是这么挂的。
    """
    import wave
    from pathlib import Path as _P

    f = _P(str(path))
    try:
        with wave.open(str(f), "rb") as w:
            ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            data = w.readframes(w.getnframes())
        with wave.open(str(f), "wb") as o:
            o.setnchannels(ch)
            o.setsampwidth(width)
            o.setframerate(rate)
            o.writeframes(data)
        return True
    except Exception:                                         # noqa: BLE001
        return False
