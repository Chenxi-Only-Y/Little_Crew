"""环境自检（`little-crew doctor`）：把"能不能开跑"一次问清（2026-11-12）。

## 为什么要它

现在开跑前要自己记：ComfyUI 起没起、TTS 起没起、LLM key 在不在、模型登记没有。
任何一个不对，表现都是"跑到一半失败"，而原因散在四五处日志里。
`doctor` 把它们**一次列出来**，并给退出码：0 全通过 / 2 有阻塞项。

## 判据（都不调模型，只探活/查配置）

| 项 | 判据 | 阻塞? |
|---|---|---|
| ComfyUI | `GET /system_stats` 200 | 要渲染才阻塞 |
| TTS | `GET /health` 200 | 要配音才阻塞 |
| LLM key | `DEEPSEEK_API_KEY` 等是否在环境 | 要真 LLM 才阻塞 |
| 模型注册表 | 图片/音频/视频三类都各有本地与云端 | 不阻塞（提示） |
| 输出目录 | `outputs/` 可写、剩余空间 | 阻塞 |
| FFmpeg | `ffmpeg -version` 能跑 | 要合成成片才阻塞 |
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

#: 探测超时（秒）—— 本机服务，短一点
TIMEOUT = 2.5


def _http_ok(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return (200 <= int(r.status) < 300), f"HTTP {r.status}"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"


def _ffmpeg_ok() -> tuple[bool, str]:
    exe = os.environ.get("LITTLE_CREW_FFMPEG") or r"D:\AI\FFmpeg\bin\ffmpeg.exe"
    cand = [exe, "ffmpeg"]
    for c in cand:
        try:
            p = subprocess.run([c, "-version"], capture_output=True, text=True,
                               timeout=5)
            if p.returncode == 0:
                head = (p.stdout or "").splitlines()[0][:50]
                return True, head
        except Exception:                                     # noqa: BLE001
            continue
    return False, f"找不到可执行的 ffmpeg（试过 {cand}）"


def _disk_ok(root: Path) -> tuple[bool, str]:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:                                  # noqa: BLE001
        return False, f"不可写：{type(exc).__name__}: {exc}"
    try:
        free = shutil.disk_usage(str(root)).free / 1024 ** 3
    except Exception:                                         # noqa: BLE001
        free = 0.0
    return (free > 2.0), f"剩余 {free:.1f} GB（建议 > 2 GB）"


def _registry_matrix() -> tuple[bool, str]:
    try:
        from little_crew.models.registry import build_default_registry
        from little_crew.models.spec import BackendKind, TaskKind

        reg = build_default_registry()
        groups = {"图片": [TaskKind.IMAGE_T2I], "音频": [TaskKind.AUDIO_TTS],
                  "视频": [TaskKind.VIDEO_I2V, TaskKind.VIDEO_T2V]}
        lines = []
        ok = True
        for label, tasks in groups.items():
            local = cloud = 0
            for s in reg.all():
                if s.task not in tasks:
                    continue
                for b in s.backends:
                    if b.kind is BackendKind.COMFYUI or getattr(b, "is_local_by_default", False):
                        local += 1
                    elif b.kind is BackendKind.HTTP:
                        cloud += 1
            lines.append(f"{label} 本地{local}/云{cloud}")
            ok = ok and local > 0 and cloud > 0
        return ok, " · ".join(lines)
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _cfg(name: str, default: str = "") -> str:
    """读配置：**先查项目配置（config/.env），再查环境变量**。

    ⚠️ 只查 `os.environ` 是错的：本项目的 key 放在 `config/.env`，
    由 `config.env_value()` 读进来 —— 第一版 doctor 就因此把
    "明明能跑" 报成 "一个 key 都没有"（真踩过）。
    """
    try:
        from little_crew.config import env_value

        v = str(env_value(name, "") or "").strip()
        if v:
            return v
    except Exception:                                         # noqa: BLE001
        pass
    return str(os.environ.get(name) or default).strip()


def _llm_keys() -> tuple[bool, str]:
    keys = ("DEEPSEEK_API_KEY", "MIMO_API_KEY", "GLM_API_KEY", "QWEN_API_KEY",
            "DASHSCOPE_API_KEY")
    have = [k for k in keys if _cfg(k)]
    return bool(have), ("有：" + ", ".join(have)) if have else "一个都没有（--llm 跑不了）"


def run_checks() -> list[dict[str, Any]]:
    root = Path(os.environ.get("LITTLE_CREW_OUTPUTS") or "outputs")
    comfy = (_cfg("COMFYUI_ENDPOINT", "http://127.0.0.1:8188")
             or "http://127.0.0.1:8188").rstrip("/")
    tts = (_cfg("BREEZE_TTS_ENDPOINT", "http://127.0.0.1:8110")
           or "http://127.0.0.1:8110").rstrip("/")

    checks: list[dict[str, Any]] = []

    ok, why = _http_ok(comfy + "/system_stats")
    checks.append({"name": "ComfyUI", "ok": ok, "blocking": ok is False,
                   "detail": f"{comfy} · {why}"})

    ok2, why2 = _http_ok(tts + "/health")
    checks.append({"name": "TTS", "ok": ok2, "blocking": ok2 is False,
                   "detail": f"{tts} · {why2}"})

    ok3, why3 = _llm_keys()
    checks.append({"name": "LLM key", "ok": ok3, "blocking": ok3 is False,
                   "detail": why3})

    ok4, why4 = _registry_matrix()
    checks.append({"name": "模型矩阵", "ok": ok4, "blocking": False,
                   "detail": why4})

    ok5, why5 = _disk_ok(root)
    checks.append({"name": "输出目录", "ok": ok5, "blocking": ok5 is False,
                   "detail": f"{root} · {why5}"})

    ok6, why6 = _ffmpeg_ok()
    checks.append({"name": "FFmpeg", "ok": ok6, "blocking": ok6 is False,
                   "detail": why6})
    return checks


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")               # type: ignore[attr-defined]
    except Exception:                                         # noqa: BLE001
        pass
    as_json = "--json" in (argv if argv is not None else sys.argv[1:])
    checks = run_checks()
    blocking = [c for c in checks if c["blocking"]]

    if as_json:
        print(json.dumps({"checks": checks,
                          "blocking": len(blocking)}, ensure_ascii=False, indent=2))
        return 0 if not blocking else 2

    print("=" * 66)
    print("环境自检（doctor）")
    print("=" * 66)
    for c in checks:
        mark = "✅" if c["ok"] else ("⛔" if c["blocking"] else "⚠️")
        print(f"  {mark} {c['name']:<10} {c['detail']}")
    print("=" * 66)
    if blocking:
        print(f"  阻塞项 {len(blocking)} 个：{', '.join(c['name'] for c in blocking)}")
        print("  起服务：见 docs/16「开工检查」；端口不对用环境变量改")
        return 2
    print("  全部就绪 —— 可以开跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
