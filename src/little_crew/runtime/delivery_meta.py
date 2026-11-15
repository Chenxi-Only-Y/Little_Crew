"""交付元数据（2026-11-15）：**AI 标识 / 署名 / 素材登记**。

## 为什么要有这个模块

合规官（`compliance_officer`）的交付腿会核对三样东西：
`aiLabels`（显式+隐式标识）· `credits`（授权要求的署名）· `sourceRegistry`（素材来源）。
在 A2 之前**没有任何代码写它们** —— 于是合规报告每次都只能列"缺失"。

本模块负责**真的写出来**（不是只置个布尔值）：

| 产物 | 内容 | 位置 |
|---|---|---|
`ai_label_endcard.png` | **显式标识**：片尾卡「本片由 AI 生成」+ 署名 | `delivery/` |
`cover_labeled.*` | **显式标识**：封面角标 `AI-GENERATED` | `delivery/`（原封面不动） |
mp4 容器元数据 | **隐式标识**：`comment` 写入 AI 标识 + 署名 + 素材清单路径 | 现有 master mp4 原位 remux |
`credits.txt` | 署名文本（来自 LicenseGate 的 attribution 要求） | `delivery/` |
`sources.json` | 素材登记：模型/字体/音轨/参考图的**来源 + 许可** | `delivery/` |
`ai_labels.json` | 以上各项是否**实际落地**（供合规官核对） | `delivery/` |

## 诚实原则

- `explicit/implicit` 只在**文件真的生成/写入成功**时为 `True`；
  失败会记 `errors`，**绝不因"我们打算加"就报 True**。
- `sourceRegistryOk` 只在每条素材都有 `source` 与 `license` 时才为 `True`；
  未知来源记 `"UNKNOWN"` 并让整体为 `False`。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 显式标识文本（中文 + ASCII 兜底：缺少中文字体时也能读）
LABEL_TEXT_ZH = "本片由 AI 生成"
LABEL_TEXT_EN = "AI-GENERATED"


def _ffmpeg() -> str:
    try:
        from little_crew.config import env_value

        return str(env_value("FFMPEG", "") or "") or "ffmpeg"
    except Exception:                                         # noqa: BLE001
        return "ffmpeg"


def _run(cmd: list[str], *, timeout: int = 120) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode == 0, (p.stderr or p.stdout or "")[-400:]
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _font() -> str:
    """找一个能画中文的字体；找不到返回空串（用 ASCII 兜底）。"""
    try:
        from little_crew.review.cover_designer import find_title_font

        f = find_title_font("banner")
        return str(f or "")
    except Exception:                                         # noqa: BLE001
        return ""


def write_endcard(out_path: Path, *, credits: str = "",
                  size: tuple[int, int] = (1280, 720)) -> bool:
    """生成**显式 AI 标识片尾卡**（PNG）。有 Pillow 且写成功才返回 True。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:                                         # noqa: BLE001
        return False
    try:
        w, h = int(size[0]), int(size[1])
        img = Image.new("RGB", (w, h), (12, 12, 14))
        d = ImageDraw.Draw(img)
        font_path = _font()

        def _f(sz: int):
            try:
                if font_path and Path(font_path).is_file():
                    return ImageFont.truetype(font_path, sz)
            except Exception:                                 # noqa: BLE001
                pass
            try:
                return ImageFont.truetype("arial.ttf", sz)
            except Exception:                                 # noqa: BLE001
                return ImageFont.load_default()

        lines = [(LABEL_TEXT_ZH, 64), (LABEL_TEXT_EN, 40)]
        if credits:
            lines.append((str(credits)[:80], 24))
        y = int(h * 0.34)
        for text, sz in lines:
            fnt = _f(sz)
            try:
                tw = d.textlength(text, font=fnt)
            except Exception:                                 # noqa: BLE001
                tw = len(text) * sz * 0.6
            d.text(((w - tw) / 2, y), text, font=fnt,
                   fill=(238, 236, 230))
            y += int(sz * 1.7)
        out_path = Path(str(out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), format="PNG")
        return True
    except Exception:                                         # noqa: BLE001
        return False


def label_cover(src: Path, out_path: Path,
                text: str = LABEL_TEXT_EN) -> bool:
    """在封面**副本**上加显式标识角标（原封面不动）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:                                         # noqa: BLE001
        return False
    try:
        src, out_path = Path(str(src)), Path(str(out_path))
        im = Image.open(src).convert("RGB")
        fnt = None
        for sz in (max(14, im.width // 46), 18, 14):
            try:
                fnt = ImageFont.truetype("arial.ttf", sz)
                break
            except Exception:                                 # noqa: BLE001
                fnt = None
        if fnt is None:
            try:
                fnt = ImageFont.load_default()
            except Exception:                                 # noqa: BLE001
                return False
        d = ImageDraw.Draw(im, "RGBA")
        pad = max(6, im.width // 120)
        try:
            tw = d.textlength(text, font=fnt)
        except Exception:                                     # noqa: BLE001
            tw = len(text) * 10
        x, y = im.width - tw - pad * 2, im.height - pad * 3
        d.rectangle([x - pad, y - pad // 2, x + tw + pad, y + pad * 3],
                    fill=(0, 0, 0, 150))
        d.text((x, y), text, font=fnt, fill=(255, 255, 255))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fmt = "PNG" if out_path.suffix.lower() == ".png" else "JPEG"
        im.save(str(out_path), format=fmt, quality=92)
        return True
    except Exception:                                         # noqa: BLE001
        return False


def inject_mp4_metadata(video: Path, *, comment: str) -> bool:
    """把隐式标识写进 mp4 容器（原位 remux，不动画面/音轨）。"""
    video = Path(str(video))
    if not video.is_file():
        return False
    tmp = video.with_suffix(".meta.tmp.mp4")
    ok, err = _run([_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(video), "-c", "copy", "-map", "0",
                    "-metadata", f"comment={comment}",
                    "-metadata", "ai_generated=1", str(tmp)])
    if not ok or not tmp.is_file():
        try:
            tmp.unlink(missing_ok=True)
        except Exception:                                     # noqa: BLE001
            pass
        return False
    try:
        tmp.replace(video)
        return True
    except Exception:                                         # noqa: BLE001
        return False


def collect_sources(*, models: Iterable[str] = (),
                    anchors: Mapping[str, str] | None = None,
                    fonts: Iterable[str] = (),
                    audio: Iterable[str] = (),
                    covers: Iterable[str] = ()) -> list[dict[str, Any]]:
    """素材登记：每条都要有 `source` 与 `license`（缺一个就记为 UNKNOWN）。"""
    out: list[dict[str, Any]] = []

    # ① 模型（从 LicenseGate 目录取许可）
    try:
        from little_crew.governance.license import LICENSE_CATALOG

        cat = dict(LICENSE_CATALOG)
    except Exception:                                         # noqa: BLE001
        cat = {}
    for m in dict.fromkeys(str(x) for x in models if str(x).strip()):
        info = cat.get(m)
        if info is not None:
            out.append({"kind": "model", "name": m,
                        "source": "models 授权目录（governance/license.py）",
                        "license": str(info.license_id)
                        + ("（未核实）" if not info.verified else ""),
                        "licenseVerified": bool(info.verified)})
        else:
            out.append({"kind": "model", "name": m,
                        "source": "运行时使用", "license": "UNKNOWN",
                        "licenseVerified": False})

    # ② 角色参考图（项目自产）
    for cid, path in (anchors or {}).items():
        out.append({"kind": "reference", "name": str(cid),
                    "path": str(path),
                    "source": "项目自产（角色设定板/锚点）",
                    "license": "project-owned"})

    # ③ 字体
    for f in dict.fromkeys(str(x) for x in fonts if str(x).strip()):
        out.append({"kind": "font", "name": Path(f).name, "path": f,
                    "source": "系统字体（本机已安装）",
                    "license": "system-font（随系统许可，不再分发）"})

    # ④ 音轨
    for a in dict.fromkeys(str(x) for x in audio if str(x).strip()):
        out.append({"kind": "audio", "name": Path(a).name, "path": a,
                    "source": "程序生成（ffmpeg 合成/垫音）或项目素材",
                    "license": "project-owned / see file"})

    # ⑤ 封面主视觉
    for c in dict.fromkeys(str(x) for x in covers if str(x).strip()):
        out.append({"kind": "cover", "name": Path(c).name, "path": c,
                    "source": "平台生成（选帧或生图）",
                    "license": "platform-generated"})

    return out


def build_delivery_meta(*, workdir: Path, episode_id: str,
                        models: Iterable[str] = (),
                        attributions: Iterable[str] = (),
                        anchors: Mapping[str, str] | None = None,
                        fonts: Iterable[str] = (),
                        audio: Iterable[str] = (),
                        covers: Iterable[str] = (),
                        masters: Iterable[str] = ()) -> dict[str, Any]:
    """生成并写入全套交付元数据，返回**实际落地情况**。

    返回结构直接喂给合规官（键名与 `compliance_audit._delivery_audit` 对齐）：
    `aiLabels{explicit,implicit,where}` / `credits` / `sources` / `sourceRegistryOk`。
    """
    wd = Path(str(workdir))
    d = wd / "delivery"
    d.mkdir(parents=True, exist_ok=True)
    models = list(models or ())
    attrs = [str(a) for a in (attributions or ()) if str(a).strip()]
    credits = "；".join(attrs) if attrs else "本片由 AI 生成合成"

    sources = collect_sources(models=models, anchors=anchors, fonts=fonts,
                              audio=audio, covers=covers)
    for s in sources:
        if s.get("kind") == "cover" and s.get("name", "").startswith("cover"):
            pass
    source_ok = bool(sources) and all(
        str(s.get("source") or "").strip() and str(s.get("license") or "").strip()
        and str(s.get("license")) != "UNKNOWN" for s in sources)

    errors: list[str] = []
    where: list[str] = []

    # ---- ① 显式：片尾卡 ----
    endcard = d / "ai_label_endcard.png"
    if write_endcard(endcard, credits=credits):
        where.append(str(endcard.relative_to(wd)))
    else:
        errors.append("endcard 未生成")

    # ---- ① 显式：封面角标（副本） ----
    labeled: list[str] = []
    for c in dict.fromkeys(str(x) for x in (covers or ())):
        src = Path(c)
        if not src.is_file():
            continue
        dst = d / f"cover_labeled{src.suffix or '.jpg'}"
        if label_cover(src, dst):
            labeled.append(str(dst.relative_to(wd)))
            where.append(str(dst.relative_to(wd)))
        else:
            errors.append(f"封面角标失败：{src.name}")

    # ---- ② 署名 / 素材清单 ----
    (d / "credits.txt").write_text(
        f"AI 标识：{LABEL_TEXT_ZH} / {LABEL_TEXT_EN}\n署名：{credits}\n"
        f"素材清单：delivery/sources.json\n", encoding="utf-8")
    (d / "sources.json").write_text(
        json.dumps({"kind": "sources", "episodeId": episode_id,
                    "registryOk": source_ok, "items": sources},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- ③ 隐式：mp4 容器元数据（原位 remux） ----
    comment = (f"AI-generated; credits={credits}; "
               f"sources=delivery/sources.json")
    injected: list[str] = []
    for m in dict.fromkeys(str(x) for x in (masters or ())):
        p = Path(m)
        if p.is_file() and inject_mp4_metadata(p, comment=comment):
            injected.append(str(p.relative_to(wd)) if wd in p.parents else p.name)
            where.append(str(p.relative_to(wd)) if wd in p.parents else p.name)
        elif p.is_file():
            errors.append(f"mp4 元数据注入失败：{p.name}")

    meta = {
        "aiLabels": {"explicit": bool(endcard.is_file() or labeled),
                     "implicit": bool(injected),
                     "where": where},
        "credits": credits,
        "attributions": attrs,
        "sources": sources,
        "sourceRegistryOk": source_ok,
        "endcard": str(endcard.relative_to(wd)) if endcard.is_file() else "",
        "labeledCovers": labeled,
        "metadataInjected": injected,
        "errors": errors,
    }
    (d / "ai_labels.json").write_text(
        json.dumps({"kind": "delivery.meta", "episodeId": episode_id, **meta},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return meta
