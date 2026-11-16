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


def _platform_ok() -> tuple[bool, str]:
    """平台本体：导入 + 版本（**这是唯一必装的**）。"""
    try:
        from little_crew.version import PEP440_VERSION, VERSION

        return True, f"little_crew {VERSION}（PEP440 {PEP440_VERSION}）"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _roster_ok() -> tuple[bool, str]:
    """名册契约：19 位 + 依赖闭合（平台本体的一部分）。"""
    try:
        from little_crew.agent.roster import DEFAULT_ROSTER, validate_roster

        errs = validate_roster(DEFAULT_ROSTER)
        return (not errs), (f"{len(DEFAULT_ROSTER)} 位" if not errs
                            else f"校验失败：{'; '.join(errs[:2])}")
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _license_ok() -> tuple[bool, str]:
    """授权目录可读（平台内置；不联网）。"""
    try:
        from little_crew.governance.license import LICENSE_CATALOG

        n = len(LICENSE_CATALOG)
        unv = sum(1 for v in LICENSE_CATALOG.values() if not v.verified)
        return True, f"{n} 条（未核实 {unv} 条；未核实需显式接受风险）"
    except Exception as exc:                                  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _need(env: str, default: bool = False) -> bool:
    """按需阻塞开关：`LITTLE_CREW_NEED_LLM=1` / `LITTLE_CREW_NEED_VOICE=1`。

    外置服务（TTS/local-gen/ComfyUI）默认**不阻塞**：它们不计入平台交付物，
    只在"这次真的要用"时才要求可达（否则容器里永远 doctor 不过）。
    """
    v = str(_cfg(env, "") or os.environ.get(env, "") or "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def _external(name: str, url: str, path: str, *,
              blocking: bool = False, hint: str = "") -> dict[str, Any]:
    ok, why = _http_ok(url)
    detail = f"{url} · {why}"
    if hint:
        detail += f" · {hint}"
    return {"name": name, "ok": ok, "blocking": bool(blocking and not ok),
            "external": True, "path": path, "detail": detail}


def run_checks() -> list[dict[str, Any]]:
    """环境自检。

    ## 分层（2026-11-16 按部署边界重做）

    | 类别 | 阻塞？ | 例子 |
    |---|---|---|
    **平台本体** | ✅ 必须 | 导入/版本、名册契约、输出目录、FFmpeg、授权目录 |
    **外置服务** | ❌ 默认不阻塞 | local-gen（出图）、ComfyUI、Breeze TTS —— **不计入平台交付物** |
    **按需能力** | 仅"这次要用"时 | 云端渲染 key、LLM key、配音（`_NEED_*` 开关） |

    外置服务挂了只影响对应路径；平台本体应该能独立部署与升级。
    """
    from pathlib import Path

    root = Path(os.environ.get("LITTLE_CREW_OUTPUTS_DIR")
                or os.environ.get("OUTPUTS_DIR") or "outputs")
    comfy = (_cfg("COMFYUI_ENDPOINT", "http://127.0.0.1:8188")
             or "http://127.0.0.1:8188").rstrip("/")
    tts = (_cfg("BREEZE_TTS_ENDPOINT", "http://127.0.0.1:8110")
           or "http://127.0.0.1:8110").rstrip("/")
    localgen = (_cfg("LITTLE_CREW_IMAGE_ENDPOINT", "")
                or _cfg("IMAGE_API_ENDPOINT", "")
                or "http://127.0.0.1:8100").rstrip("/")

    checks: list[dict[str, Any]] = []

    # ---- 平台本体（阻塞项）----
    ok, why = _platform_ok()
    checks.append({"name": "平台导入", "ok": ok, "blocking": not ok,
                   "external": False, "detail": why})
    ok, why = _roster_ok()
    checks.append({"name": "名册契约", "ok": ok, "blocking": not ok,
                   "external": False, "detail": why})
    ok, why = _license_ok()
    checks.append({"name": "授权目录", "ok": ok, "blocking": not ok,
                   "external": False, "detail": why})
    ok, why = _disk_ok(root)
    checks.append({"name": "输出目录", "ok": ok, "blocking": not ok,
                   "external": False, "detail": f"{root} · {why}"})
    ok, why = _ffmpeg_ok()
    checks.append({"name": "FFmpeg", "ok": ok, "blocking": not ok,
                   "external": False, "detail": why})

    # ---- 外置服务（默认不阻塞，不计入平台交付物）----
    checks.append(_external(
        "local-gen", localgen, "render.image",
        hint="外置出图服务（不计入平台）；--render-path local 时需要"))
    checks.append(_external(
        "ComfyUI", comfy + "/system_stats", "render.comfyui",
        hint="外置可选（不计入平台）"))

    # ---- 按需能力 ----
    _vmid = (_cfg("LITTLE_CREW_VIDEO_MODEL", "") or "")
    _na = False
    try:
        from little_crew.models.registry import native_audio_for

        _na = bool(native_audio_for(str(_vmid)).get("nativeAudio"))
    except Exception:                                         # noqa: BLE001
        _na = False
    _need_voice = _need("LITTLE_CREW_NEED_VOICE") and not _na
    checks.append(_external(
        "TTS", tts + "/health", "audio.tts", blocking=_need_voice,
        hint=("视频模型原生音轨 → 不需要 TTS"
              if _na else
              ("需要外部配音（LITTLE_CREW_NEED_VOICE=1）" if _need_voice
               else "外置可选（不计入平台）；需要外部配音时设 LITTLE_CREW_NEED_VOICE=1"))))

    ok3, why3 = _llm_keys()
    checks.append({"name": "LLM key", "ok": ok3,
                   "blocking": (_need("LITTLE_CREW_NEED_LLM") and not ok3),
                   "external": False, "detail": why3})

    # 云端渲染：用户选定云端路径时才要求 key（否则只是提示）
    _rp = str(_cfg("LITTLE_CREW_RENDER_PATH", "") or "").strip().lower()
    _cloud_key = bool(_cfg("DASHSCOPE_API_KEY") or _cfg("SUDORELAY_API_KEY"))
    checks.append({"name": "云端渲染 key", "ok": _cloud_key,
                   "blocking": (_rp == "cloud" and not _cloud_key),
                   "external": False,
                   "detail": (("已配置" if _cloud_key else
                               "未配置（--render-path cloud 需要 DASHSCOPE/SUDORELAY key）")
                              + (f" · 当前路径={_rp}" if _rp else " · 未固定渲染路径"))})

    ok4, why4 = _registry_matrix()
    checks.append({"name": "模型矩阵", "ok": ok4, "blocking": False,
                   "external": False, "detail": why4})

    # 自定义供应商（2026-11-13）：不阻塞，但必须列出来
    try:
        from little_crew.models.custom_providers import describe as _desc

        info = _desc()
        if not info["exists"]:
            checks.append({"name": "自定义供应商", "ok": True, "blocking": False,
                           "external": False,
                           "detail": f"未配置（{info['path']}）"})
        else:
            parts = []
            for r in info["providers"]:
                key = "key √" if r["keyPresent"] else f"缺 {r['keyEnv']}"
                scope = ("限定 " + ",".join(r["allowedAgents"])) if r["allowedAgents"] else "不限"
                parts.append(f"{r['id']}（{r['task']} · {key} · {scope}）")
            bad = info["problems"]
            detail = " · ".join(parts) or "配置为空"
            if bad:
                detail += f" · ⚠️ {len(bad)} 条问题"
            checks.append({"name": "自定义供应商", "ok": not bad, "blocking": False,
                           "external": False, "detail": detail[:160]})
    except Exception as exc:                                  # noqa: BLE001
        checks.append({"name": "自定义供应商", "ok": False, "blocking": False,
                       "external": False,
                       "detail": f"读取失败：{type(exc).__name__}: {exc}"})
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
        tag = " [外置]" if c.get("external") else ""
        print(f"  {mark} {c['name']:<10}{tag} {c['detail']}")
    print("=" * 66)
    _ext = [c for c in checks if c.get("external")]
    if _ext:
        print(f"  外置服务 {len(_ext)} 个（不计入平台交付物）："
              + ", ".join(f"{c['name']}{'✓' if c['ok'] else '✗'}" for c in _ext))
    if blocking:
        print(f"  阻塞项 {len(blocking)} 个：{', '.join(c['name'] for c in blocking)}")
        print("  起服务：见 docs/16「开工检查」；端口不对用环境变量改")
        return 2
    print("  全部就绪 —— 可以开跑")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
