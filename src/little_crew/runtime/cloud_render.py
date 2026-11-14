"""关键镜云端渲染（方案 C 的执行层，2026-11-13）。

## 做什么

给一批**已经判定"值得上云端"**的镜头，用现有**角色库锚点图**做图生图（`/images/edits`），
得到"同一角色、更高分辨率与细节"的关键镜，替换掉本地版。

```
镜头描述 + 角色锚点图 -> /images/edits（1536×1024）-> 关键镜新图
```

## 为什么用图生图而不是文生图

"角色用现有库里"—— 库里已有 `assets/characters/<id>/sheet*.png`。
以它为参考图，云端会把**同一张脸**画进新镜头（比纯文字描述稳得多）；
没有锚点的镜头退回文生图。

## 纪律

- **只替换、不删除**：本地版留在原处（可回退、可对比）
- 每镜记录 `{shotId, anchor, model, seconds, out}`，写进渲染记录（由调用方落盘）
- 失败**不阻断**：某镜云端失败就保留本地版，逐条记录
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from little_crew import net
from little_crew.config import env_value

#: 默认云端模型（Sudorelay；2.6 的 edits 可用）
DEFAULT_MODEL = "gpt-image-2.6-sunburst"
#: 默认出图尺寸（与角色设定板一致）
DEFAULT_SIZE = "1536x1024"

#: 关键镜题面模板：**只描述这一镜**，人物身份交给参考图
_PROMPT = (
    "以参考图中的角色为唯一身份基准，绘制一镜漫剧画面（不要改脸型五官/发型/妆容）。"
    "镜头内容：{desc}"
    "{extra}"
    "画风：高精度 3D 半写实国漫 CG，柔和棚光，低饱和；"
    "禁止：改变角色长相、增加未提及的人物、裁切头部、文字水印。"
)


def _anchor_for(characters: Iterable[str], anchors: Mapping[str, str]) -> str:
    """按角色取锚点图。

    ⚠️ 镜头的 `characters` 是**角色名**（如「少女」），而库里目录名是 **character_id**
    （如 `CHAR-A1B2` / `a-heng`）—— 两套都要认，否则永远匹配不上（实测踩到）。
    """
    from little_crew.agent.character_sheet import character_id_for

    for raw in characters or ():
        name = str(raw)
        for key in (name, character_id_for(name)):
            p = str(anchors.get(key) or "")
            if p and Path(p).is_file():
                return p
    return ""


def _prompt_of(shot: Any, extra: str = "") -> str:
    desc = ""
    for key in ("description", "prompt"):
        v = getattr(shot, key, None) if not isinstance(shot, Mapping) else shot.get(key)
        if v:
            desc = str(v)
            break
    return _PROMPT.replace("{desc}", desc[:600]).replace(
        "{extra}", (f"补充：{extra}。" if extra else ""))


def render_one(shot: Any, anchor: str, out_dir: str | Path, *,
               model: str = DEFAULT_MODEL, size: str = DEFAULT_SIZE,
               extra: str = "") -> dict:
    """渲染单个关键镜（图生图；无锚点时退回文生图）。返回记录。**不抛**。"""
    out = Path(str(out_dir))
    out.mkdir(parents=True, exist_ok=True)
    sid = str(getattr(shot, "shot_id", "") if not isinstance(shot, Mapping)
              else shot.get("shot_id") or "shot")
    prompt = _prompt_of(shot, extra)
    key = str(env_value("SUDORELAY_API_KEY", "") or "")
    if not key:
        return {"shotId": sid, "ok": False, "error": "缺少 SUDORELAY_API_KEY"}
    t0 = time.time()
    try:
        if anchor and Path(anchor).is_file():
            data = Path(anchor).read_bytes()
            ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            raw = net.post_multipart(
                "https://api.sudorelay.com/v1/images/edits",
                {"model": model, "prompt": prompt, "size": size, "n": "1"},
                {"image": (f"ref.{ctype.split('/')[-1]}", data)},
                headers={"Authorization": f"Bearer {key}",
                         "User-Agent": "OpenAI/Python 1.0"}, timeout=600)
        else:
            body = json.dumps({"model": model, "prompt": prompt,
                               "size": size, "n": 1}).encode()
            req = net.urllib.request.Request(
                "https://api.sudorelay.com/v1/images/generations", data=body,
                method="POST", headers={"Authorization": f"Bearer {key}",
                                        "Content-Type": "application/json",
                                        "User-Agent": "OpenAI/Python 1.0"})
            with net.urlopen(req, timeout=600) as resp:
                raw = resp.read()
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
            return {"shotId": sid, "ok": False, "error": "响应里没有图片",
                    "seconds": round(time.time() - t0, 1)}
        dst = out / f"{sid}_cloud.png"
        dst.write_bytes(img)
        return {"shotId": sid, "ok": True, "out": str(dst), "anchor": anchor or "",
                "model": model, "seconds": round(time.time() - t0, 1),
                "bytes": len(img), "usedAnchor": bool(anchor)}
    except Exception as exc:                                  # noqa: BLE001
        return {"shotId": sid, "ok": False, "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - t0, 1)}


#: 云端并行度默认值。**云端才并行**：请求在远端排队，本地只是等 I/O；
#: 本地渲染并发实测只快 1.15x（见 render_policy），所以不在这里并行本地。
DEFAULT_WORKERS = 4


def render_plan(shots: Iterable[Any], plan_rows: Iterable[Mapping[str, Any]],
                anchors: Mapping[str, str], out_dir: str | Path, *,
                model: str = DEFAULT_MODEL, extra: str = "",
                workers: int = DEFAULT_WORKERS) -> dict:
    """按计划渲一批关键镜（**云端并行**）。返回 `{ok, rendered, failed, records}`。"""
    from concurrent.futures import ThreadPoolExecutor

    by_id = {str(getattr(s, "shot_id", "") if not isinstance(s, Mapping)
                 else s.get("shot_id") or ""): s for s in shots}
    its: list[tuple[str, Any, str, Mapping[str, Any]]] = []
    records: list[dict] = []
    for row in plan_rows:
        sid = str(row.get("shotId") or "")
        shot = by_id.get(sid)
        if shot is None:
            records.append({"shotId": sid, "ok": False, "error": "找不到该镜"})
            continue
        chars = getattr(shot, "characters", None) if not isinstance(shot, Mapping) \
            else shot.get("characters")
        its.append((sid, shot, _anchor_for(chars or (), anchors), row))

    def _one(item):
        sid, shot, anchor, row = item
        rec = render_one(shot, anchor, out_dir, model=model, extra=extra)
        rec["score"] = row.get("score")
        rec["reasons"] = row.get("reasons")
        return rec

    if workers > 1 and len(its) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(its))) as ex:
            records += list(ex.map(_one, its))
    else:
        records += [_one(it) for it in its]
    ok = [r for r in records if r.get("ok")]
    return {"ok": bool(ok), "rendered": len(ok),
            "failed": len(records) - len(ok), "records": records,
            "workers": workers,
            "seconds": round(sum(float(r.get("seconds") or 0) for r in records), 1)}


def load_anchors(workdir: str | Path) -> dict[str, str]:
    """从现有角色库读锚点：`assets/characters/<cid>/sheet*.png`。

    **角色就用现有库里的**（用户明确要求）：不重新生成设定图，直接复用。
    优先精修版，其次原版。
    """
    out: dict[str, str] = {}
    base = Path(str(workdir)) / "assets" / "characters"
    if not base.is_dir():
        return out
    for sub in sorted(base.iterdir()):
        if not sub.is_dir():
            continue
        for name in ("sheet_refined.png", "sheet.png", "sheet_refined.jpg", "sheet.jpg"):
            f = sub / name
            if f.is_file():
                out[sub.name] = str(f)
                break
    return out
