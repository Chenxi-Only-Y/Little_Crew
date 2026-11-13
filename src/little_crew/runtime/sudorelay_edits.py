"""图生图（`/images/edits`）通道：保图微调 / 参考图重绘（2026-11-13）。

## 为什么需要它

文生图每张脸都是重新"抽"，精致度靠运气；**图生图能锁住人物与构图**。
实测（8 张设定板）：

| 路线 | 结果 |
|---|---|
| 纯文生图 | 一致性尚可，但面部精致度靠运气 |
| 图生图（参考图 + 长提示词） | 长提示词会**压过参考图**，输出几乎不随参考图变化 |
| **图生图（参考图 + 短精修提示词）** | **保图微调**：人物/构图不变，细节（手/鞋/发丝/布料）变好 |

## 两条硬约束

1 **2.6 可用**：早期文档记录"2.6 的 edits 返回 502"，2026-11-13 实测已恢复，
   且细节保持优于 2.5。默认 2.6，2.5 保留回退。
2 **零第三方依赖**：`multipart/form-data` 手写（`requests` 不在允许清单里）。

## 微调的语义边界（`REFINE_PROMPT`）

保留：人物 / 脸型五官 / 发型 / 妆容 / 服饰结构与配色 / 四视图布局 / 背景
只修：手部与手指数量 · 鞋型结构 · 发丝走向 · 布料纹理 · 四视图一致性
禁止：换装 · 改脸 · 加减视图 · 文字水印

提示词**刻意写短** —— 长提示词压过参考图是实测得出的教训。
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path

from little_crew import net
from little_crew.config import env_value

#: 图生图默认模型（2.6 的 edits 已可用；2.5 保留作回退）
DEFAULT_EDIT_MODEL = "gpt-image-2.6-sunburst"

#: 保图微调提示词（短提示词：把"人物与构图"交给参考图）
REFINE_PROMPT = (
    "保持与参考图完全相同的人物身份、脸型五官、发型、妆容、服饰结构与配色、"
    "四视图布局与浅灰影棚背景，不改变任何设计与长相，只做细节精修："
    "①手部结构与手指数量正确；②鞋型与鞋底结构清晰、左右对称；"
    "③发丝走向顺滑、发束层次自然；④布料纹理与缝线更清晰；"
    "⑤四个视图的脸、发型、服装、配饰完全一致。"
    "高精度 3D 半写实国漫 CG 质感，柔和棚光，无文字无水印。"
)

_BASE = "https://api.sudorelay.com"


def available() -> tuple[bool, str]:
    """是否具备调用条件（有 key）。**不发网络请求**。"""
    key = str(env_value("SUDORELAY_API_KEY", "") or "")
    if not key:
        return False, "缺少 SUDORELAY_API_KEY（config/.env）"
    return True, ""


def edit_image(ref_path: str | Path, prompt: str, *,
               model: str = DEFAULT_EDIT_MODEL, size: str = "1536x1024",
               out_path: str | Path | None = None,
               timeout_s: float = 900) -> dict:
    """调 `/images/edits`。返回 `{ok, out, bytes, seconds, model}` 或 `{ok: False, error, detail}`。

    **不抛异常**：调用方（角色设计师）要能把失败记进报告而不是拖垮整集。
    """
    key = str(env_value("SUDORELAY_API_KEY", "") or "")
    if not key:
        return {"ok": False, "error": "缺少 SUDORELAY_API_KEY"}
    ref = Path(str(ref_path))
    if not ref.is_file():
        return {"ok": False, "error": f"参考图不存在：{ref}"}
    data = ref.read_bytes()
    boundary = "----dsh" + uuid.uuid4().hex

    def part(name: str, value: str) -> bytes:
        return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f'{value}\r\n').encode("utf-8")

    body = b""
    for k, v in (("model", model), ("prompt", prompt), ("size", size), ("n", "1")):
        body += part(k, v)
    ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    ext = ctype.split("/")[-1]
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
             f'filename="ref.{ext}"\r\nContent-Type: {ctype}\r\n\r\n').encode("utf-8")
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = net.urllib.request.Request(
        f"{_BASE}/v1/images/edits", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": "OpenAI/Python 1.0"})
    t0 = time.time()
    try:
        with net.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
        dt = time.time() - t0
    except Exception as exc:                                  # noqa: BLE001
        detail = ""
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:                                 # noqa: BLE001
                pass
        return {"ok": False, "seconds": round(time.time() - t0, 1),
                "error": f"{type(exc).__name__}: {exc}", "detail": detail}

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
        return {"ok": False, "seconds": round(dt, 1), "error": "响应里没有图片",
                "detail": str(obj)[:300]}

    out = Path(str(out_path or "edited.png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(img)
    return {"ok": True, "out": str(out), "bytes": len(img),
            "seconds": round(dt, 1), "model": model}


def refine_sheet(sheet_path: str | Path, *, out_path: str | Path | None = None,
                 model: str = DEFAULT_EDIT_MODEL) -> dict:
    """对一张已生成的设定板做**保图微调**（薄封装，便于角色设计师调用）。"""
    src = Path(str(sheet_path))
    dst = Path(str(out_path)) if out_path else src.with_name(
        f"{src.stem}_refined{src.suffix}")
    res = edit_image(src, REFINE_PROMPT, model=model, out_path=dst)
    if res.get("ok"):
        res["source"] = str(src)
        res["out"] = str(dst)
    return res
