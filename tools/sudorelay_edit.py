"""Sudorelay `/images/edits` 的多部分上传（零第三方依赖）。

为什么单独写：`HttpChannel` 只发 JSON。图生图要 `multipart/form-data` 上传参考图，
而本项目不允许引入 requests —— 所以手写 multipart。字段按模板文档：
`model / prompt / size / n / image`。

⚠️ **历史记录已过期**（2026-11-13 实测）：早期文档说"2.6 的 edits 返回 502"，因此一律用 2.5。
现在 **2.6 的 edits 可用**（8 张实测 32~44s，且细节保持更好：百褶裙褶、菱格纹、鞋子结构）。
所以默认模型改为 2.6；2.5 保留为对照/回退。
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path

from little_crew import net
from little_crew.config import env_value


def _fields(model: str, prompt: str, size: str, n: int = 1) -> list[tuple[str, str]]:
    return [("model", model), ("prompt", prompt), ("size", size), ("n", str(n))]


def edit_image(ref_path: str | Path, prompt: str, *, model: str = "gpt-image-2.5-sunburst",
               size: str = "1536x1024", out_path: str | Path | None = None,
               timeout_s: float = 600) -> dict:
    """调 `/images/edits`，返回 `{ok, out, bytes, seconds, error}`（**不抛**）。"""
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
    for k, v in _fields(model, prompt, size):
        body += part(k, v)
    ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
             f'filename="ref.{ctype.split("/")[-1]}"\r\nContent-Type: {ctype}\r\n\r\n'
             ).encode("utf-8")
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = net.urllib.request.Request(
        "https://api.sudorelay.com/v1/images/edits", data=body, method="POST",
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
