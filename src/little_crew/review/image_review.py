"""渲染产物的**可数审片**：不需要视觉，也能抓出真问题。

## 为什么只审这些

`docs/00-项目策划书.md` §3.10.2 第二层要求"审片接进渲染之后"。
但本项目的 LLM 客户端**没有视觉能力**（`llm.py` 里没有 image/vision 字段），
所以"画面好不好/人物像不像"**现在审不了** —— 审了就是编。

本模块只做**能证伪的检查**：

| 检查 | 抓什么 |
|---|---|
| 文件存在且可读 | 渲染报成功但文件没落盘 |
| PNG 头宽高符合预期 | 引擎按错的尺寸出图（分辨率漂移） |
| 体积过小 | 空白/失败图（往往几百字节） |
| 与其它镜头内容哈希相同 | 多镜复用同一张（串镜） |

**画面质量、角色相似度、构图 —— 明确不审**，写进产物的 `notReviewed`。

## 为什么不用 PIL

项目核心层 `dependencies = []`（§4.3 零第三方依赖）。
PNG 的宽高在文件头里，`struct` 就能读 —— 为了一个数字引入 Pillow 不值得。
"""
from __future__ import annotations

import struct
from pathlib import Path

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: 体积低于这个值几乎一定是空白/失败图
MIN_BYTES = 4096


def png_size(path) -> tuple[int, int] | None:
    """从 PNG 头读 (宽, 高)。不是 PNG 或读不到返回 None。**只用标准库。**"""
    p = Path(str(path))
    try:
        with p.open("rb") as fh:
            head = fh.read(33)
    except OSError:
        return None
    if len(head) < 33 or not head.startswith(PNG_MAGIC) or head[12:16] != b"IHDR":
        return None
    try:
        w, h = struct.unpack(">II", head[16:24])
    except struct.error:
        return None
    return int(w), int(h)


def review(shots, *, artifacts_by_shot=None, expect_size=None,
           hashes_by_shot=None, min_bytes: int = MIN_BYTES) -> dict:
    """对一批渲染产物做可数审查。

    `artifacts_by_shot`: `{shotId: [文件路径或 None]}`
    `expect_size`: `(w, h)`，None 表示不校验尺寸
    返回 `{kind, total, findings[], counts, notReviewed[]}`
    """
    findings: list[dict] = []
    seen_hash: dict[str, str] = {}
    total = 0
    for shot in shots or ():
        sid = str(getattr(shot, "shot_id", shot) if not isinstance(shot, str) else shot)
        paths = (artifacts_by_shot or {}).get(sid) or []
        if not paths:
            findings.append(_f(sid, "missing-artifact", "error",
                               "该镜没有任何产物文件"))
            continue
        for raw in paths:
            total += 1
            p = Path(str(raw))
            if not p.is_file():
                findings.append(_f(sid, "missing-artifact", "error",
                                   f"产物路径不存在：{p.name}"))
                continue
            size = p.stat().st_size
            if size < min_bytes:
                findings.append(_f(sid, "tiny-file", "error",
                                   f"文件只有 {size} 字节（疑似空白/失败图）",
                                   {"bytes": size}))
            got = png_size(p)
            if got is None:
                findings.append(_f(sid, "not-png", "warn",
                                   "文件不是合法 PNG（引擎可能换了格式）"))
            elif expect_size and tuple(got) != tuple(expect_size):
                findings.append(_f(sid, "size-mismatch", "error",
                                   f"尺寸 {got[0]}x{got[1]}，期望 "
                                   f"{expect_size[0]}x{expect_size[1]}",
                                   {"actual": list(got),
                                    "expected": list(expect_size)}))
            h = (hashes_by_shot or {}).get(sid)
            if h:
                if h in seen_hash:
                    findings.append(_f(sid, "duplicate-artifact", "error",
                                       f"与 {seen_hash[h]} 内容完全相同（串镜）",
                                       {"sameAs": seen_hash[h]}))
                else:
                    seen_hash[h] = sid

    counts: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    return {
        "kind": "image.review",
        "total": total,
        "findings": findings,
        "counts": counts,
        "notReviewed": [
            "画面质量（需要视觉模型，本机客户端不支持图像输入）",
            "角色相似度（同上；且本机无 IPAdapter/LoRA）",
            "构图/机位是否符合意图（同上）",
        ],
    }


def _f(shot_id, kind, severity, reason, detail=None) -> dict:
    return {"shotId": str(shot_id), "kind": kind, "severity": severity,
            "reason": reason, "detail": dict(detail or {})}
