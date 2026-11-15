"""封面设计师（Cover Designer）：**选帧** 或 **生成**，二选一（2026-11-13）。

两种模式：

| 模式 | 做什么 | 成本 | 何时用 |
|---|---|---|---|
| `select` | 从成片镜头里挑一帧当封面 | 1 次 VLM（拼图判断） | 已有满意镜头 |
| **`generate`** | 以**角色设定图 + 美术/剧情设定**为输入，**生成**一张新封面 | 1 次生图（图生图 edits） | 想要"海报感" |

为什么两种都要：选帧快而忠实（就是成片里的画面），生成更好看但会引入新内容。
**默认选帧**（可追溯），生成需显式开。

## 为什么值得做成职位

平台此前**没有任何封面** —— 交付物、报告页、答辩材料都缺这块。
而"哪张能当封面"是纯视觉判断：主体完不完整、有没有切头、表情到不到位。
文本模型做不了，只有看图才行。

## 成本设计：**一张拼图，一次判断**

逐张判要 N 次 VLM（每次 20~40s），N=8 时不可接受。做法：

```
候选 8 张 ──ffmpeg──> 一张 2×4 拼图 ──VLM 一次──> 选第几格
                                                        ↓
                                              把那一格的**原图**复制成 cover.png
```

**关键纪律**：判的是拼图，选出来的是**原分辨率那张**（不是拼图裁片）。

## 判什么（只判可数项）

| 项 | 说明 |
|---|---|
| 主体完整 | 人物没被裁切（尤其头部/手） |
| 未切头 | 头顶到下巴完整入画 |
| 无崩坏 | 没有糊脸、多手、暗块 |
| 无文字水印 | 画面干净 |
| 门面感 | 能一眼看出"这是谁、什么故事" |

## 失败语义

- VLM 不给合法 JSON / 返回越界索引 / 全部候选不达标 → **不出封面**（`ok=False` 并写明原因），
  绝不"随便挑一张当封面"（那等于没选）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 一次最多看几个候选（拼图格数 = 列 × 行）
DEFAULT_MAX_CANDIDATES = 8
#: 拼图列数（2 列 × 4 行 = 8 格，竖排；模型按 row-major 报序号）
GRID_COLS = 2
#: 每格缩略图宽度 / 高度（拼图整体不宜过大，否则视觉 token 爆）
THUMB_W = 640
THUMB_H = 360

_PROMPT = """这是一张候选镜头拼图：{cols} 列 × {rows} 行，共 {n} 格，**按行优先编号**：
第 1 行左起为 {first_cell} …

请挑出**最适合当封面（Key Art）**的那一格。只输出一行 JSON，不要推理：
{"pick": <从 1 开始的序号>, "reason": "<20字内>",
 "bad": [<明显不合格的序号>]}

判断标准（按优先级）：
1. 主体完整、**没有切头**（头顶到下巴都在画面里）
2. 人物正面或 3/4 侧、表情清楚，能一眼看出"这是谁"
3. 画面干净：无文字、无水印、无暗块、无糊脸、无多余肢体
4. 若多格都合格，选**主体占比更大、信息更集中**的那格
5. 宁可不选（pick 填 0），也不要选一张有硬伤的
"""

#: 视觉调用上限。**900 不够**（2026-11-13 实测）：mimo 在 900 时会把额度用在
#: 描述画面上、JSON 被挤掉；1200 才稳定"只回 JSON"。
MAX_TOKENS = 1200


def _ffmpeg() -> str:
    """ffmpeg 路径（沿用项目的固定位置/环境变量）。"""
    return str(os.environ.get("LITTLE_CREW_FFMPEG")
               or r"D:\AI\FFmpeg\bin\ffmpeg.exe")


def _img_ok(path: Path, *, min_bytes: int = 8_000, min_side: int = 320) -> bool:
    """粗筛：存在、够大、且有图片魔数（不解析像素，避免第三方依赖）。"""
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
        head = path.read_bytes()[:16]
    except Exception:                                         # noqa: BLE001
        return False
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if head[:2] == b"\xff\xd8":
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return False


def prefilter(images: Mapping[str, str], *, limit: int) -> tuple[list[tuple[str, Path]], list[dict]]:
    """按给定顺序筛出可用的候选（**等距抽样**，保证覆盖全片）。

    返回 `(候选[(shotId, path)], 被剔除的[(shotId, reason)])`。
    """
    valid: list[tuple[str, Path]] = []
    dropped: list[dict] = []
    for sid, p in images.items():
        path = Path(str(p))
        if _img_ok(path):
            valid.append((str(sid), path))
        else:
            dropped.append({"shotId": str(sid), "reason": "文件缺失/过小/非图片"})
    if limit > 0 and len(valid) > limit:
        # 等距抽样：保留首、尾与中间，覆盖整集而不是只挑前几个
        step = len(valid) / float(limit)
        picked = [valid[int(i * step)] for i in range(limit)]
        keep = {sid for sid, _ in picked}
        dropped.extend({"shotId": sid, "reason": "未抽样（超出候选上限）"}
                       for sid, _ in valid if sid not in keep)
        valid = picked
    return valid, dropped


def build_contact_sheet(candidates: Iterable[tuple[str, Path]], out_path: str | Path,
                        *, cols: int = GRID_COLS) -> dict:
    """把候选拼成一张网格图（ffmpeg）。返回 `{ok, out, cols, rows, count}`。

    每个格子固定 ``THUMB_W × THUMB_H``（等比缩放 + 黑边补齐）；
    不足一整行的格子用 **lavfi 黑图当输入**补齐（不用 split —— 实测 `split=1` 会报错），
    保证网格整齐：模型按"行优先编号"报序号，格子错位会让选择全错。
    """
    items = list(candidates)
    n = len(items)
    if n == 0:
        return {"ok": False, "error": "没有候选"}
    cols = max(1, int(cols))
    rows = (n + cols - 1) // cols
    blanks = rows * cols - n
    out = Path(str(out_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error"]
    for _sid, p in items:
        cmd += ["-i", str(p)]
    for _ in range(blanks):
        cmd += ["-f", "lavfi", "-i",
                f"color=c=black:s={THUMB_W}x{THUMB_H}:d=1"]
    total = rows * cols
    parts: list[str] = []
    for i in range(total):
        parts.append(f"[{i}:v]scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=decrease,"
                     f"pad={THUMB_W}:{THUMB_H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]")
    layout = "|".join(f"{(i % cols) * THUMB_W}_{(i // cols) * THUMB_H}"
                      for i in range(total))
    parts.append("".join(f"[v{i}]" for i in range(total))
                 + f"xstack=inputs={total}:layout={layout}")
    cmd += ["-filter_complex", ";".join(parts), "-frames:v", "1", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as exc:              # noqa: PERF203
        err = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"拼图失败：{err}"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"拼图失败：{type(exc).__name__}: {exc}"}
    if not out.is_file():
        return {"ok": False, "error": "拼图未生成"}
    return {"ok": True, "out": str(out), "cols": cols, "rows": rows, "count": n}


def _extract_obj(text: str) -> dict | None:
    """从回复里找**含 pick 的 JSON 对象**（容忍前后有描述文字）。"""
    if not isinstance(text, str):
        return None
    import re as _re

    for m in _re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except Exception:                                     # noqa: BLE001
            continue
        if isinstance(obj, dict) and ("pick" in obj):
            return obj
    # 退化：整体解析
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            obj = json.loads(text[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:                                     # noqa: BLE001
            return None
    return None


def judge_grid(sheet_path: str | Path, *, n: int, cols: int = GRID_COLS,
               client: Any = None) -> dict:
    """让 VLM 看拼图选一格。返回 `{ok, pick, reason, bad, error}`。**不抛**。"""
    import base64

    path = Path(str(sheet_path))
    if not path.is_file():
        return {"ok": False, "error": f"拼图不存在：{path}"}
    if client is None:
        try:
            from little_crew.agent.llm import LlmClient

            # 判官：mimo（非推理 + 跨族；deepseek-flash 是推理模型，JSON 容易被思考挤掉）
            client = LlmClient("mimo-v2.6-flash", timeout_s=60)
        except Exception as exc:                              # noqa: BLE001
            return {"ok": False, "error": f"没有可用的视觉客户端：{exc}"}
    rows = (n + cols - 1) // cols
    prompt = _PROMPT.replace("{cols}", str(cols)).replace("{rows}", str(rows)) \
                    .replace("{n}", str(n)).replace("{first_cell}", "1、2、3")
    data = base64.b64encode(path.read_bytes()).decode()
    text = ""
    obj = None
    for attempt in (1, 2):
        try:
            # 第 2 次把"只输出 JSON"提到最前面（实测推理型回复会在第 1 次跑偏）
            p2 = prompt if attempt == 1 else (
                "只输出一行 JSON，不要任何描述：\n" + prompt)
            reply = client.chat_image(p2, data, what="封面选帧", max_tokens=MAX_TOKENS)
        except Exception as exc:                              # noqa: BLE001
            return {"ok": False, "error": f"调用失败：{type(exc).__name__}: {exc}"}
        text = reply if isinstance(reply, str) else getattr(reply, "text", "")
        obj = _extract_obj(text)
        if obj is not None:
            break
    if obj is None:
        return {"ok": False, "error": "模型没给合法 JSON（不能当作已选）",
                "replyHead": str(text)[:120]}
    try:
        pick = int(obj.get("pick") or 0)
    except Exception:                                         # noqa: BLE001
        pick = 0
    if pick <= 0:
        return {"ok": False, "error": "模型选择「不选」（宁缺毋滥）",
                "reason": str(obj.get("reason") or "")[:60]}
    if pick > n:
        return {"ok": False, "error": f"选择越界：pick={pick} > 候选数 {n}"}
    bad = obj.get("bad")
    return {"ok": True, "pick": pick, "reason": str(obj.get("reason") or "")[:60],
            "bad": [int(x) for x in bad if isinstance(x, (int, float))] if isinstance(bad, list) else []}


def select_cover(images: Mapping[str, str], out_dir: str | Path, *,
                 client: Any = None, max_candidates: int = DEFAULT_MAX_CANDIDATES,
                 title: str = "") -> dict:
    """选封面：粗筛 → 拼图 → 一次判断 → 复制原图。返回报告（`ok/failed`）。"""
    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"kind": "cover.select", "episodeTitle": title,
                              "candidates": [], "dropped": [], "ok": False}
    valid, dropped = prefilter(images, limit=max_candidates)
    report["dropped"] = dropped
    if not valid:
        report["error"] = "没有可用候选（全部被剔除）"
        return report
    sheet = build_contact_sheet(valid, out / "cover_candidates.jpg")
    if not sheet.get("ok"):
        report["error"] = sheet.get("error", "拼图失败")
        return report
    report["contactSheet"] = sheet["out"]
    picked = judge_grid(sheet["out"], n=len(valid),
                        cols=int(sheet["cols"]), client=client)
    report["judge"] = picked
    if not picked.get("ok"):
        report["error"] = picked.get("error", "未选出封面")
        return report
    idx = int(picked["pick"]) - 1
    if idx >= len(valid):
        report["error"] = f"选择越界：{picked['pick']} > {len(valid)}"
        return report
    sid, src = valid[idx]
    cover = out / "cover.png"
    try:
        shutil.copy2(src, cover)
    except Exception as exc:                                  # noqa: BLE001
        report["error"] = f"复制封面失败：{type(exc).__name__}: {exc}"
        return report
    report.update({
        "ok": True, "shotId": sid, "source": str(src),
        "cover": str(cover), "reason": picked.get("reason", ""),
        "candidates": [s for s, _ in valid],
    })
    return report


def summarize(report: Mapping[str, Any]) -> str:
    if report.get("ok"):
        mode = report.get("mode", "select")
        what = ("封面已生成" if mode == "generate"
                else "封面已选：" + str(report.get("shotId")))
        t = report.get("title") or {}
        extra = []
        if report.get("reason"):
            extra.append(str(report["reason"]))
        if isinstance(t, Mapping) and t.get("ok"):
            extra.append(f"标题「{t.get('title')}」已叠加（{t.get('size')}px）")
        return what + ("：" + "；".join(extra) if extra else "")
    return f"未出封面：{report.get('error', '未知原因')}"

# ==========================================================================
# 模式二：**生成**封面（角色设定图 + 美术/剧情设定 -> 图生图）
# ==========================================================================

#: 封面生成默认模型（Sudorelay 图生图 2.6；早期"2.6 edits 502"记录已过期）
DEFAULT_GEN_MODEL = "gpt-image-2.6-sunburst"

#: 封面主题模板：把"角色/故事/风格"三块拼成海报感题面
_COVER_PROMPT = (
    "为一部漫剧设计**封面主视觉**（Key Art），横版海报构图，浅暖灰背景。"
    "画面主体是参考图中的角色，**严格保持其脸型五官、发型、妆容与服饰结构不变**。"
    "{character}"
    "{story}"
    "{style}"
    "构图：角色位于画面右侧三分之二处，半身或七分身，视线看向镜头略偏；"
    "左侧留出干净留白用于放标题。{title_directive}"
    "氛围：柔和主光 + 轻轮廓光，背景可加极淡的环境层次，整体低饱和、高级感。"
    "画质：高精度 3D 半写实国漫 CG 质感，清晰锐利，无水印、无边框。"
    "禁止：改变角色长相、增加其他人物、裁切头部、日期戳与多余小字。"
)


#: 各平台常见中文字体文件名（按优先级）
_FONT_NAMES = (
    "msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simkai.ttf", "msyhbd.ttf",
    "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "NotoSansCJK-Regular.ttc",
    "NotoSansSC-Bold.otf", "PingFang.ttc", "Hiragino Sans GB.ttc",
    "SourceHanSansSC-Bold.otf", "wqy-zenhei.ttc", "DroidSansFallbackFull.ttf",
)

#: 标题模式
TITLE_MODES = ("overlay", "draw", "none")


def font_dirs() -> list[Path]:
    """系统字体目录（**跨平台**，每个人机器上都有其中之一）。

    为什么要探测而不是写死路径：写死 `C:/Windows/Fonts/...` 只在 Windows 上活，
    而本项目要求"换机器不改代码"。顺序：显式环境变量 > 用户字体目录 > 系统目录。
    """
    import os as _os

    out: list[Path] = []
    env = str(_os.environ.get("LITTLE_CREW_COVER_FONT_DIR") or "").strip()
    if env:
        out.append(Path(env))
    home = Path.home()
    out += [
        home / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",  # Windows 用户字体
        Path("C:/Windows/Fonts"),
        home / "Library" / "Fonts",                                      # macOS
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),        # Linux
    ]
    seen, uniq = set(), []
    for d in out:
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            uniq.append(d)
    return uniq


def find_font() -> str:
    """找一个可用的中文字体文件；都找不到返回空串（调用方降级）。

    也支持显式指定：`LITTLE_CREW_COVER_FONT=<字体文件路径>`。
    """
    import os as _os

    env = str(_os.environ.get("LITTLE_CREW_COVER_FONT") or "").strip()
    if env and Path(env).is_file():
        return str(Path(env))
    names = [n.lower() for n in _FONT_NAMES]
    # 先按"已知文件名"找
    for d in font_dirs():
        try:
            for f in d.rglob("*"):
                if f.is_file() and f.name.lower() in names:
                    return str(f)
        except Exception:                                     # noqa: BLE001
            continue
    # 再按"名字里含中文字体关键字"兜底
    keys = ("msyh", "simhei", "simkai", "notosanscjk", "notosanssc",
            "pingfang", "sourcehansans", "wqy", "droid")
    for d in font_dirs():
        try:
            for f in d.rglob("*"):
                if f.is_file() and any(k in f.name.lower() for k in keys):
                    return str(f)
        except Exception:                                     # noqa: BLE001
            continue
    return ""


def _escape_drawtext(s: str) -> str:
    """drawtext 的文本转义（冒号/单引号/反斜杠/百分号）。"""
    return (str(s).replace("\\", "\\\\").replace("'", "\u2019")
            .replace(":", "\\:").replace("%", "\\%").replace(",", "\\,"))


# ==========================================================================
# 标题艺术字（2026-11-15）：参考漫剧封面的"粗体 + 金描边 + 缎带 + 角标"
# ==========================================================================

#: 标题风格 -> 候选字体（按优先级）。找不到就降级到通用中文字体。
#:
#: `banner` 用华文琥珀（圆胖粗体，最接近漫剧封面常用字）；
#: `brush` 用行楷（笔锋）；`seal` 用隶书/新魏；`display` 用粗黑宋。
TITLE_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "banner": ("STHUPO.TTF", "STXINWEI.TTF", "msyhbd.ttc"),
    "brush": ("STXINGKA.TTF", "FZSTK.TTF", "STXINWEI.TTF", "STLITI.TTF"),
    "seal": ("STLITI.TTF", "STXINWEI.TTF", "STHUPO.TTF", "STXINGKA.TTF"),
    "display": ("STXINWEI.TTF", "msyhbd.ttc", "STHUPO.TTF"),
}

#: 支持的艺术字风格（`classic` = 2026-11 之前的纯 drawtext，可回退）
TITLE_STYLES = ("banner", "brush", "seal", "display", "classic")


def _ffprobe() -> str:
    return str(os.environ.get("LITTLE_CREW_FFPROBE")
               or r"D:\AI\FFmpeg\bin\ffprobe.exe")


def find_title_font(style: str = "banner") -> str:
    """按风格找艺术字体；找不到退回 `find_font()`。"""
    env = str(os.environ.get("LITTLE_CREW_COVER_TITLE_FONT") or "").strip()
    if env and Path(env).is_file():
        return str(Path(env))
    key = str(style or "banner").lower()
    if key not in TITLE_FONT_CANDIDATES:
        key = "banner"
    names = [n.lower() for n in TITLE_FONT_CANDIDATES[key]]
    # ⚠️ 必须**按候选优先级**外层循环：先按目录扫会把同目录里更早出现的
    # 备用字体（msyhbd）先命中，导致"指定了琥珀却用了雅黑"（2026-11-15 实测）。
    dirs = list(font_dirs())
    for name in names:
        for d in dirs:
            try:
                for f in d.rglob("*"):
                    if f.is_file() and f.name.lower() == name:
                        return str(f)
            except Exception:                                 # noqa: BLE001
                continue
    return find_font()


def _ff_font(font_file: str) -> str:
    return str(font_file).replace("\\", "/").replace(":", "\\:")


def _ff_esc(s: str) -> str:
    return (str(s).replace("\\", "\\\\").replace("'", "\u2019")
            .replace(":", "\\:").replace("%", "\\%").replace(",", "\\,"))


def _probe_size(p: str | Path) -> tuple[int, int]:
    r = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
        capture_output=True, text=True, check=False)
    try:
        w, h = (r.stdout or "1280,720").strip().splitlines()[0].split(",")[:2]
        return int(w), int(h)
    except Exception:                                         # noqa: BLE001
        return 1280, 720


def _run_vf(out: Path, W: int, H: int, parts: list[str]) -> dict:
    vf = ",".join(parts) + ",colorkey=0x000000:0.16:0.04"
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}",
           "-vf", vf, "-frames:v", "1", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as exc:              # noqa: PERF203
        err = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"标题卡生成失败：{err}"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"标题卡生成失败：{type(exc).__name__}: {exc}"}
    return {"ok": out.is_file(), "card": str(out)}


def _banner_parts(chars: list[str], W: int, H: int, A: str,
                  subtitle: str, badge: str) -> tuple[list[str], int, list[str]]:
    if len(chars) > 6:
        half = (len(chars) + 1) // 2
        lines = ["".join(chars[:half]), "".join(chars[half:])]
    elif len(chars) > 4:
        lines = ["".join(chars)]
    else:
        lines = ["".join(chars)]
    fs = int(min(W * 0.115, H * 0.19))
    if len(lines) > 1:
        fs = int(fs * 0.86)
    step = int(fs * 1.22)
    block_h = step * len(lines)
    cy0 = int(H * 0.60) - block_h // 2
    pad = int(W * 0.055)
    band_y = cy0 - int(fs * 0.34)
    band_h = block_h + int(fs * 0.68)
    parts = [
        f"drawbox=x={pad}:y={band_y}:w={W - pad * 2}:h={band_h}:color=#120C10@0.52:t=fill",
        f"drawbox=x={pad}:y={band_y}:w={W - pad * 2}:h=2:color=#D9B25F@0.95:t=fill",
        f"drawbox=x={pad}:y={band_y + band_h - 2}:w={W - pad * 2}:h=2:"
        f"color=#D9B25F@0.95:t=fill",
    ]
    for i, line in enumerate(lines):
        y = cy0 + i * step
        text_w = len(line) * fs
        x = max(4, (W - text_w) // 2)
        base = f"fontfile='{A}':text='{_ff_esc(line)}':fontsize={fs}:x={x}:y={y}"
        parts += [
            f"drawtext={base}:fontcolor=#FFE9B0@0.20:borderw=18:"
            f"bordercolor=#FFD98A@0.20",
            f"drawtext={base}:fontcolor=#2B1608:borderw=12:bordercolor=#2B1608",
            f"drawtext={base}:fontcolor=#C99733:x={x + 4}:y={y + 5}",
            f"drawtext={base}:fontcolor=#F7E7B0",
        ]
    if subtitle:
        sub = " ".join(str(subtitle).strip().upper())
        sfs = max(14, int(fs * 0.20))
        parts.append(f"drawtext=fontfile='{A}':text='{_ff_esc(sub)}':"
                     f"fontsize={sfs}:fontcolor=#E8DBC0@0.95:"
                     f"x={max(4, (W - len(sub) * sfs // 2) // 2)}:"
                     f"y={band_y + band_h + int(fs * 0.18)}:"
                     f"shadowcolor=#2B2118@0.6:shadowx=1:shadowy=1")
    if badge:
        btxt = str(badge)
        bfs = max(12, int(fs * 0.22))
        # 角标宽度按**文字实际长度**算（否则「60 秒短片」会被裁掉 — 实测）
        bw = max(int(fs * 1.15), bfs * len(btxt) + int(fs * 0.42))
        bh = int(fs * 0.40)
        bx, by = pad, band_y - bh
        parts += [
            f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=#8E2A23@0.95:t=fill",
            f"drawbox=x={bx}:y={by}:w={bw}:h={bh}:color=#E8C87A@0.9:t=2",
            f"drawtext=fontfile='{A}':text='{_ff_esc(btxt)}':fontsize={bfs}:"
            f"fontcolor=#F7F0E3:x={bx + (bw - len(btxt) * bfs) // 2}:"
            f"y={by + (bh - bfs) // 2}",
        ]
    return parts, fs, lines


def _brush_parts(chars: list[str], W: int, H: int, A: str,
                 subtitle: str) -> tuple[list[str], int]:
    n = len(chars)
    char_size = int(min(W * 0.135, H * 0.17))
    step = int(char_size * 1.14)
    block_h = step * n
    if block_h > H * 0.74:
        char_size = max(24, int(H * 0.74 / (n * 1.14)))
        step = int(char_size * 1.14)
        block_h = step * n
    cx = int(W * 0.16)
    x0 = cx - char_size // 2
    y0 = max(int(H * 0.06), int(H * 0.82) - block_h)
    parts = [
        f"drawbox=x={x0 - int(char_size * 0.62)}:y={y0 - int(char_size * 0.18)}:"
        f"w=2:h={block_h + int(char_size * 0.30)}:color=#C9A227@0.80:t=fill",
        f"drawbox=x={x0 - int(char_size * 0.62) - 7}:y={y0 - int(char_size * 0.12)}:"
        f"w=1:h={block_h + int(char_size * 0.20)}:color=#C9A227@0.45:t=fill",
    ]
    for i, ch in enumerate(chars):
        y = y0 + i * step
        base = f"fontfile='{A}':text='{_ff_esc(ch)}':fontsize={char_size}:x={x0}:y={y}"
        parts += [
            f"drawtext={base}:fontcolor=#FFD98A@0.32:borderw=8:"
            f"bordercolor=#FFD98A@0.28",
            f"drawtext={base}:fontcolor=#C9A227:x={x0 + 3}:y={y + 3}",
            f"drawtext={base}:fontcolor=#F7F0E3:borderw=3:bordercolor=#3B2A20",
        ]
    sw = int(char_size * 0.82)
    sh = int(sw * 1.42)
    sx = W - sw - int(W * 0.045)
    sy = H - sh - int(H * 0.075)
    parts.append(f"drawbox=x={sx}:y={sy}:w={sw}:h={sh}:color=#8E2A23@0.96:t=fill")
    fs = int(sw * 0.42)
    for j, ch in enumerate("小剧组"):
        cy = sy + int(sh * (j + 0.5) / 3) - fs // 2
        parts.append(f"drawtext=fontfile='{A}':text='{_ff_esc(ch)}':fontsize={fs}:"
                     f"fontcolor=#F7F0E3:x={sx + (sw - fs) // 2}:y={cy}")
    if subtitle:
        sub = " ".join(str(subtitle).strip().upper())
        sfs = max(13, int(char_size * 0.20))
        parts.append(f"drawtext=fontfile='{A}':text='{_ff_esc(sub)}':fontsize={sfs}:"
                     f"fontcolor=#E8DBC0@0.92:x={x0}:"
                     f"y={min(H - sfs - 8, y0 + block_h + int(char_size * 0.26))}:"
                     f"shadowcolor=#2B2118@0.6:shadowx=1:shadowy=1")
    return parts, char_size


def build_title_card(title: str, out_dir: str | Path, *, width: int, height: int,
                     style: str = "banner", subtitle: str = "", badge: str = "",
                     font: str = "") -> dict:
    """生成透明标题卡。`style=banner`（横排缎带）/`brush`（竖排行楷）。"""
    chars = [c for c in str(title or "").strip() if not c.isspace()]
    if not chars:
        return {"ok": False, "error": "标题为空"}
    font_file = font or find_title_font(style)
    if not font_file:
        return {"ok": False, "error": "找不到中文字体（可用 LITTLE_CREW_COVER_TITLE_FONT 指定）"}
    W, H = int(width), int(height)
    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    card = out / "title_card.png"
    A = _ff_font(font_file)
    if str(style).lower() == "brush":
        parts, size = _brush_parts(chars, W, H, A, subtitle)
        lines = ["".join(chars)]
    else:
        parts, size, lines = _banner_parts(chars, W, H, A, subtitle, badge)
    rep = _run_vf(card, W, H, parts)
    if not rep.get("ok"):
        return rep
    return {"ok": True, "card": str(card), "font": Path(font_file).name,
            "size": size, "chars": len(chars), "lines": lines,
            "style": str(style).lower()}


def compose_poster(cover: str | Path, card: str | Path, out: str | Path,
                   *, width: int, height: int) -> dict:
    """标题卡合成：左下压暗遮罩 + 胶片颗粒。"""
    W, H = int(width), int(height)
    scrim = (f"color=c=black:s={W}x{H},format=rgba,"
             f"geq=r=0:g=0:b=0:a='clip(255*(0.62*pow(Y/H\\,2.2)+0.28*(1-X/(W*0.9)))\\,0\\,255)'")
    fc = ("[0:v][1:v]overlay=0:0[b];[b][2:v]overlay=0:0,"
          "noise=alls=6:allf=t+u[v]")
    cmd = [_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(cover), "-f", "lavfi", "-i", scrim,
           "-i", str(card), "-filter_complex", fc, "-map", "[v]",
           "-frames:v", "1", str(out)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except subprocess.CalledProcessError as exc:              # noqa: PERF203
        err = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"合成失败：{err}"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"合成失败：{type(exc).__name__}: {exc}"}
    return {"ok": Path(str(out)).is_file(), "out": str(out)}


def add_title(cover_path: str | Path, title: str, out_path: str | Path | None = None,
              *, font: str = "", size: int = 0, color: str = "white",
              position: str = "left", style: str = "banner",
              subtitle: str = "", badge: str = "") -> dict:
    """在封面上**叠加标题**（ffmpeg drawtext）。返回 `{ok, out, font, size}`。

    为什么后合成而不是让模型写：**中文文字渲染不稳定**（尤其长标题会糊/错字）；
    ffmpeg 叠字是确定性的，位置/字号/描边都可控、可复现，且不污染 AI 出的画面。
    """
    src = Path(str(cover_path))
    if not src.is_file():
        return {"ok": False, "error": f"封面不存在：{src}"}
    text = str(title or "").strip()
    if not text:
        return {"ok": False, "error": "标题为空"}
    # ---- 艺术字（2026-11-15）：banner/brush 等 -> 生成透明卡再合成 ----
    if str(style or "").lower() in TITLE_STYLES and \
            str(style or "").lower() != "classic":
        dst0 = Path(str(out_path)) if out_path else src.with_name("cover_titled.png")
        w0, h0 = _probe_size(src)
        card = build_title_card(text, dst0.parent, width=w0, height=h0,
                                style=str(style).lower(), subtitle=subtitle,
                                badge=badge, font=font)
        if not card.get("ok"):
            return card
        comp = compose_poster(src, card["card"], dst0, width=w0, height=h0)
        if not comp.get("ok"):
            return comp
        return {"ok": dst0.is_file(), "out": str(dst0), "shape": "poster",
                "style": str(style).lower(), "font": card.get("font"),
                "size": card.get("size"), "title": text,
                "card": card.get("card"), "subtitle": str(subtitle or ""),
                "badge": str(badge or "")}
    font_file = font or find_font()
    if not font_file:
        return {"ok": False, "error": "找不到中文字体（可用 LITTLE_CREW_COVER_FONT 指定）"}
    dst = Path(str(out_path)) if out_path else src.with_name("cover_titled.png")
    # 字号：默认按画面宽度 1/14（短标题大、长标题自动缩一点）
    if size <= 0:
        size = 0
    probe = subprocess.run(
        [r"D:\AI\FFmpeg\bin\ffprobe.exe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "csv=p=0", str(src)],
        capture_output=True, text=True, check=False)
    try:
        width = int((probe.stdout or "0").strip().splitlines()[0])
    except Exception:                                         # noqa: BLE001
        width = 1280
    if size <= 0:
        base = max(28, width // 14)
        size = base if len(text) <= 8 else max(28, int(base * 8 / len(text)))
    if position == "center":
        x, y = "(w-text_w)/2", "(h-text_h)/2"
    elif position == "bottom":
        x, y = "(w-text_w)/2", "h-text_h-h/10"
    else:  # left
        x, y = "w*0.06", "h*0.45"
    esc = _escape_drawtext(text)
    # ⚠️ ffmpeg 的 drawtext 里 **fontfile 用正斜杠**：Windows 反斜杠会被当成转义，
    # 实测报 "No option name near '\Windows\Fonts\...'"。
    # 还要把盘符后的冒号转义（`C\:/Windows/...`）—— 冒号在 filtergraph 里是参数分隔符
    font_av = str(font_file).replace("\\", "/").replace(":", "\\:")
    vf = (f"drawtext=fontfile='{font_av}':text='{esc}':fontsize={size}:"
          f"fontcolor={color}:x={x}:y={y}:"
          f"shadowcolor=black@0.6:shadowx=2:shadowy=2:borderw=2:bordercolor=black@0.35")
    cmd = [r"D:\AI\FFmpeg\bin\ffmpeg.exe", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-vf", vf, "-frames:v", "1", str(dst)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except subprocess.CalledProcessError as exc:              # noqa: PERF203
        err = (exc.stderr or b"").decode("utf-8", errors="replace")[:200]
        return {"ok": False, "error": f"叠标题失败：{err}"}
    except Exception as exc:                                  # noqa: BLE001
        return {"ok": False, "error": f"叠标题失败：{type(exc).__name__}: {exc}"}
    return {"ok": dst.is_file(), "out": str(dst), "font": font_file,
            "size": size, "title": text}


def generate_cover(ref_images: Iterable[str | Path], out_dir: str | Path, *,
                   character: str = "", story: str = "", style: str = "",
                   title: str = "", title_mode: str = "overlay",
                   model: str = DEFAULT_GEN_MODEL,
                   size: str = "1536x1024") -> dict:
    """用**图生图**生成一张封面。返回报告（`ok/out/model/seconds/error`）。**不抛**。"""
    import base64
    import time

    from little_crew import net
    from little_crew.config import env_value

    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"kind": "cover.generate", "model": model, "ok": False}
    refs = [Path(str(r)) for r in ref_images or () if Path(str(r)).is_file()]
    if not refs:
        report["error"] = "没有可用的参考图（角色设定图缺失）"
        return report
    ref = refs[0]
    key = str(env_value("SUDORELAY_API_KEY", "") or "")
    if not key:
        report["error"] = "缺少 SUDORELAY_API_KEY"
        return report
    prompt = (_COVER_PROMPT
              .replace("{character}", (f"角色设定：{character}。" if character else ""))
              .replace("{story}", (f"故事氛围：{story}。" if story else ""))
              .replace("{style}", (f"视觉风格：{style}。" if style else ""))
              .replace("{title_directive}",
                       (f"画面左侧留白处**直接写上标题「{title}」**（白色粗体、加黑色描边），"
                        f"文字必须清晰正确、不糊不改字。" if str(title_mode) == "draw" and title
                        else "左侧留白处**不要写任何文字**（标题稍后由后期叠加，保证字形正确）。")))
    data = ref.read_bytes()
    ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    fields = {"model": model, "prompt": prompt, "size": size, "n": "1"}
    files = {"image": (f"ref.{ctype.split('/')[-1]}", data)}
    t0 = time.time()
    try:
        raw = net.post_multipart(
            "https://api.sudorelay.com/v1/images/edits", fields, files,
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "OpenAI/Python 1.0"}, timeout=600)
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        item = (obj.get("data") or [{}])[0]
        img = None
        if item.get("b64_json"):
            img = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            with net.urlopen(net.urllib.request.Request(
                    item["url"], headers={"User-Agent": "OpenAI/Python 1.0"}),
                    timeout=600) as rr:
                img = rr.read()
        if not img:
            report["error"] = "响应里没有图片"
            return report
        dst = out / "cover_generated.png"
        dst.write_bytes(img)
        report.update({"ok": True, "out": str(dst), "ref": str(ref),
                       "seconds": round(time.time() - t0, 1),
                       "bytes": len(img), "prompt": prompt})
        return report
    except Exception as exc:                                  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["seconds"] = round(time.time() - t0, 1)
        return report


# ==========================================================================
# 生成式海报（2026-11-15）：让模型**直接把标题画进封面**
# ==========================================================================
#
# 为什么换这条路：ffmpeg 叠字虽然字形 100% 正确，但"艺术字感"不够
# （用户反馈"太正规呆板"）。实测 gpt-image-2.6 直接画的标题有金属渐变、
# 厚描边、外发光、缎带、角标，观感接近平台热门封面。
#
# 代价与纪律：
# - 每张 ¥0.05；默认 2 个变体（¥0.10），失败可回退到 ffmpeg 叠字
# - 标题文字必须**逐字**写进 prompt 并强调"字形正确、不糊不改字"
# - 生成后**不裁切不留字**：画面自带完整构图

_COVER_POSTER_PROMPT = """\
以参考帧为基础做电影海报。**严格保持参考图的画风、渲染质感、光线、色调与人物形象
完全不变**：同一个角色、同一套服装、同样的场景氛围与光影；不要重新设计人物，
不要改变脸型五官，不要改变服装，不要加塑料高光/磨皮/发光特效。
{character}{story}{style}
{layout}
除此之外不要添加任何图形、缎带、角标、水印，不要金色渐变、不要彩色描边光效。
整体安静、克制、低饱和电影感，保留胶片颗粒。
"""

#: 海报变体 -> (版式说明)
POSTER_VARIANTS: dict[str, str] = {
    "cine": (
        "版式：保持原画面构图，在画面下方约 1/5 处、左对齐写简体中文标题「{title}」，"
        "字体用**细衬线宋体**（不是黑体、不要金色、不要渐变、不要描边光效），"
        "颜色为柔和的象牙白，字号克制（约占画面宽度的 1/4），带极淡的暗色投影。"
        "{subtitle}"
    ),
    "cine_vertical": (
        "版式：保持原画面构图，在画面右侧留白处**竖排**写简体中文标题「{title}」，"
        "细衬线宋体、象牙白、字号克制、字距略宽。{subtitle}"
    ),
}


def poster_prompt(*, title: str, subtitle: str = "", badge: str = "",
                  character: str = "", story: str = "", style: str = "",
                  variant: str = "cine") -> str:
    """拼生成式海报 prompt（克制电影风格；标题逐字写清）。"""
    v = str(variant or "cine")
    layout = POSTER_VARIANTS.get(v, POSTER_VARIANTS["cine"])
    sub_line = (f"标题下方一行极小的英文「{str(subtitle).upper()}」，字距略拉开。"
                if subtitle else "")
    if v == "cine_vertical" and subtitle:
        sub_line = (f"标题下方**竖排**英文小字「{str(subtitle).upper()}」。")
    layout = layout.replace("{title}", str(title or "标题")).replace(
        "{subtitle}", sub_line)
    return _COVER_POSTER_PROMPT.format(
        character=(f"角色设定：{character}。" if character else ""),
        story=(f"故事氛围：{story}。" if story else ""),
        style=(f"视觉风格：{style}。" if style else ""),
        layout=layout,
    )


def _edit_image_call(ref_path: str | Path, prompt: str, *,
                     size: str, out_path: str | Path,
                     model: str = DEFAULT_GEN_MODEL,
                     timeout_s: float = 900) -> dict:
    """调 `/images/edits` 生成一张图（review 层本地实现，**不 import runtime**）。

    ⚠️ 架构约束（R1）：review 层不能 import runtime —— 否则反向依赖被守卫拦下
    （2026-11-15 实测：封面设计师直接借 runtime.sudorelay_edits 当场违规）。
    所以这里只用 `net` 与 `config`，与 `generate_cover` 同一套调用形态。
    """
    import base64
    import time

    from little_crew import net
    from little_crew.config import env_value

    rep: dict = {"ok": False}
    key = str(env_value("SUDORELAY_API_KEY", "") or "")
    if not key:
        rep["error"] = "缺少 SUDORELAY_API_KEY"
        return rep
    ref = Path(str(ref_path))
    if not ref.is_file():
        rep["error"] = f"参考图不存在：{ref}"
        return rep
    data = ref.read_bytes()
    ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    t0 = time.time()
    try:
        raw = net.post_multipart(
            "https://api.sudorelay.com/v1/images/edits",
            {"model": model, "prompt": prompt, "size": size, "n": "1"},
            {"image": (f"ref.{ctype.split('/')[-1]}", data)},
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": "OpenAI/Python 1.0"}, timeout=timeout_s)
        obj = json.loads(raw.decode("utf-8", errors="replace"))
        item = (obj.get("data") or [{}])[0]
        img = None
        if item.get("b64_json"):
            img = base64.b64decode(item["b64_json"])
        elif item.get("url"):
            with net.urlopen(net.urllib.request.Request(
                    item["url"], headers={"User-Agent": "OpenAI/Python 1.0"}),
                    timeout=timeout_s) as rr:
                img = rr.read()
        if not img:
            rep["error"] = "响应里没有图片"
            return rep
        dst = Path(str(out_path))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(img)
        rep.update({"ok": True, "out": str(dst), "seconds": round(time.time() - t0, 1),
                    "bytes": len(img), "model": model})
        return rep
    except Exception as exc:                                  # noqa: BLE001
        rep["error"] = f"{type(exc).__name__}: {exc}"
        rep["seconds"] = round(time.time() - t0, 1)
        return rep


def generate_poster(ref_path: str | Path, out_dir: str | Path, *,
                    title: str, subtitle: str = "", badge: str = "",
                    character: str = "", story: str = "", style: str = "",
                    model: str = DEFAULT_GEN_MODEL, size: str = "1536x1024",
                    variants: int = 2, editor: Any = None) -> dict:
    """以参考帧生成成品海报（模型直接写标题）。返回报告，**不抛**。

    `editor` 可注入（测试用）：`(ref, prompt, *, size, out_path) -> dict`。
    """
    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    rep: dict = {"kind": "cover.poster", "ok": False, "mode": "poster",
                 "model": model, "size": size, "variants": [],
                 "style": str(style or "")}
    ref = Path(str(ref_path))
    if not ref.is_file():
        rep["error"] = f"参考帧不存在：{ref}"
        return rep
    edit = editor or (lambda r, p, *, size, out_path:
                      _edit_image_call(r, p, size=size, out_path=out_path,
                                       model=model))
    n = max(1, min(4, int(variants or 1)))
    names = list(POSTER_VARIANTS.keys())
    first_ok: Path | None = None
    for i in range(n):
        variant = names[i % len(names)]
        prompt = poster_prompt(title=title, subtitle=subtitle, badge=badge,
                               character=character, story=story, style=style,
                               variant=variant)
        dst = out / f"cover_poster_{i + 1}_{variant}.png"
        try:
            res = edit(ref, prompt, size=size, out_path=dst)
        except Exception as exc:                              # noqa: BLE001
            res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        item = {"variant": variant, "prompt": prompt,
                "ok": bool(res.get("ok")), "out": str(res.get("out") or dst),
                "seconds": res.get("seconds"), "bytes": res.get("bytes"),
                "error": res.get("error")}
        rep["variants"].append(item)
        if item["ok"] and first_ok is None:
            first_ok = Path(str(item["out"]))
    if first_ok and first_ok.is_file():
        final = out / "cover.png"
        try:
            final.write_bytes(first_ok.read_bytes())
        except Exception as exc:                              # noqa: BLE001
            rep["error"] = f"复制封面失败：{exc}"
            return rep
        rep.update({"ok": True, "out": str(final), "cover": str(final),
                    "source": str(first_ok),
                    "okVariants": sum(1 for v in rep["variants"] if v["ok"]),
                    "title": str(title), "titleStyle": "model-cine",
                    "subtitle": str(subtitle or "")})
    else:
        rep["error"] = "所有变体都生成失败"
    return rep


def build_cover(ref_images: Iterable[str | Path], shot_images: Mapping[str, str],
                out_dir: str | Path, *, mode: str = "select", client: Any = None,
                title: str = "", character: str = "", story: str = "",
                style: str = "", title_mode: str = "overlay",
                title_style: str = "banner", subtitle: str = "",
                badge: str = "", variants: int = 2,
                poster_editor: Any = None,
                max_candidates: int = DEFAULT_MAX_CANDIDATES) -> dict:
    """统一入口：`mode="select"` 选帧 / `mode="generate"` 生成。"""
    tmode = str(title_mode or "overlay").lower()
    if tmode not in TITLE_MODES:
        tmode = "overlay"
    # ---- 生成式海报（2026-11-15）：模型直接写标题 ----
    if str(mode).lower() == "poster":
        # 1) 先选一帧作为参考（VLM 选帧；没 client 就取第一张可用图）
        picked_frame = ""
        shot_imgs = {str(k): str(v) for k, v in (shot_images or {}).items()
                     if Path(str(v)).is_file()}
        if shot_imgs:
            if client is not None:
                sel = select_cover(shot_imgs, out_dir, client=client, title="")
                picked_frame = str(sel.get("cover") or sel.get("out") or "")                     if sel.get("ok") else ""
            if not picked_frame:
                picked_frame = next(iter(shot_imgs.values()))
        refs2 = [str(r) for r in (ref_images or ()) if Path(str(r)).is_file()]
        poster_ref = picked_frame or (refs2[0] if refs2 else "")
        rep = generate_poster(
            poster_ref, out_dir, title=title, subtitle=subtitle, badge=badge,
            character=character, story=story, style=style,
            variants=max(1, int(variants or 2)), editor=poster_editor)
        rep["mode"] = "poster"
        rep["referenceFrame"] = poster_ref
        if rep.get("ok"):
            raw = Path(str(out_dir)) / "cover_raw.png"
            try:
                if poster_ref and Path(poster_ref).is_file():
                    raw.write_bytes(Path(poster_ref).read_bytes())
                    rep["coverRaw"] = str(raw)
            except Exception:                                 # noqa: BLE001
                pass
            return rep
        # 2) 失败回退：选帧 + ffmpeg 叠字（保证交付不断）
        rep["fallback"] = "ffmpeg-overlay"
        if picked_frame:
            fb = add_title(picked_frame, title, out_path=Path(str(out_dir)) / "cover.png",
                           style="banner", subtitle=subtitle, badge=badge)
            if fb.get("ok"):
                rep.update({"ok": True, "out": fb.get("out"),
                            "cover": fb.get("out"), "fallback": "ffmpeg-overlay",
                            "titleStyle": "poster-fallback"})
        return rep
    if str(mode).lower() == "generate":
        rep = generate_cover(ref_images, out_dir, character=character,
                             story=story, style=style, title=title,
                             title_mode=tmode)
    else:
        rep = select_cover(shot_images, out_dir, client=client, title=title,
                           max_candidates=max_candidates)
    rep["mode"] = str(mode).lower()
    rep["titleMode"] = tmode
    # ⚠️ 2026-11-13 真机：封面失败时 `delivery/cover.png` 还是**上一轮**的旧图，
    # 光看文件会以为"这次也出了封面"。失败就把旧图改名成 cover.stale.png。
    final_cover = Path(str(out_dir)) / "cover.png"
    if not rep.get("ok"):
        if final_cover.is_file():
            try:
                stale = final_cover.with_name("cover.stale.png")
                if stale.is_file():
                    stale.unlink()
                final_cover.replace(stale)
                rep["stalePreviousCover"] = str(stale)
            except Exception:                                 # noqa: BLE001
                pass
    else:
        stale = final_cover.with_name("cover.stale.png")
        if stale.is_file():
            try:
                stale.unlink()                                # 成功时清掉旧的 stale
            except Exception:                                 # noqa: BLE001
                pass
    # 后合成标题：默认覆盖 cover.png（留 cover_raw.png 备查），draw 模式不重复叠字
    if rep.get("ok") and title and tmode == "overlay":
        base = Path(str(rep.get("cover") or rep.get("out") or ""))
        final = Path(str(out_dir)) / "cover.png"
        if base.is_file():
            # ⚠️ 选帧模式下 cover.png 就是源文件：ffmpeg 不允许输出与输入同路径。
            # 先把它改名为 cover_raw.png，叠字结果再写回 cover.png（失败则还原）。
            raw = base
            if base.resolve() == final.resolve():
                raw = Path(str(out_dir)) / "cover_raw.png"
                try:
                    base.replace(raw)
                except Exception:                             # noqa: BLE001
                    raw = base
            titled = add_title(raw, title, out_path=final,
                               style=str(title_style or "banner"),
                               subtitle=str(subtitle or ""),
                               badge=str(badge or ""))
            rep["title"] = titled          # 叠字报告（F6 依赖：ok/out/font）
            rep["titleText"] = title
            rep["titleStyle"] = str(title_style or "banner")
            if subtitle:
                rep["subtitle"] = str(subtitle)
            if badge:
                rep["badge"] = str(badge)
            if titled.get("ok"):
                rep["cover"] = str(final)
                rep["coverRaw"] = str(raw)
            else:
                # 叠字失败：保留原封面（不因为标题丢了封面）
                if raw.resolve() != final.resolve() and not final.is_file():
                    try:
                        raw.replace(final)
                    except Exception:                         # noqa: BLE001
                        pass
    return rep
