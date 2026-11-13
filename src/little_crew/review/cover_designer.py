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
    report: dict[str, Any] = {"kind": "cover.select", "title": title,
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
        return ("封面已选：" + str(report.get("shotId")) +
                (f"（{report.get('reason')}）" if report.get("reason") else ""))
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
    "左侧留出干净留白用于放标题与字幕条（**本次不写任何文字**）。"
    "氛围：柔和主光 + 轻轮廓光，背景可加极淡的环境层次，整体低饱和、高级感。"
    "画质：高精度 3D 半写实国漫 CG 质感，清晰锐利，无文字、无水印、无边框。"
    "禁止：改变角色长相、增加其他人物、裁切头部、文字与日期戳。"
)


def generate_cover(ref_images: Iterable[str | Path], out_dir: str | Path, *,
                   character: str = "", story: str = "", style: str = "",
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
              .replace("{style}", (f"视觉风格：{style}。" if style else "")))
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


def build_cover(ref_images: Iterable[str | Path], shot_images: Mapping[str, str],
                out_dir: str | Path, *, mode: str = "select", client: Any = None,
                title: str = "", character: str = "", story: str = "",
                style: str = "", max_candidates: int = DEFAULT_MAX_CANDIDATES) -> dict:
    """统一入口：`mode="select"` 选帧 / `mode="generate"` 生成。"""
    if str(mode).lower() == "generate":
        rep = generate_cover(ref_images, out_dir, character=character,
                             story=story, style=style)
    else:
        rep = select_cover(shot_images, out_dir, client=client, title=title,
                           max_candidates=max_candidates)
    rep["mode"] = str(mode).lower()
    return rep
