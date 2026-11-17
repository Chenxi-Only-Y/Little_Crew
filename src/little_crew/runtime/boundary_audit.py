"""边界审（2026-11-17，0.6.0 系列层）。

R1：章与章之间两条线都要查
1. **契约线（机器）**：上一章 exit vs 本章 entry（`chapter_plan.contract_diff`）
2. **画面线（VLM）**：上一章最后帧 vs 本章第一帧 —— 同一空间/造型/光线？
   - VLM 判 no → 红项 → **只重拍本章边界镜**
   - 拿不准/无帧 → 黄项 → 人工

诚实边界：VLM 只判"连续性"，**不是人脸同一性度量**。
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = ["audit_contract", "audit_visual", "audit_chapter", "VLM_PROMPT"]

VLM_PROMPT = (
    "这是一组相邻两章的边界画面：左边是上一章最后一帧，右边是本章第一帧。"
    "请只判断三项连续性：1) 是否同一空间/场景；2) 主要人物造型/服装是否一致；"
    "3) 光线/时段是否连续。"
    "输出 JSON：{\"sameSpace\": true|false, \"sameCostume\": true|false, "
    "\"sameLighting\": true|false, \"diff\": \"一句话差异\"}"
)


def audit_contract(prev_exit: Any, cur_entry: Any) -> dict[str, Any]:
    """契约线：机器比对；不一致即红项。"""
    from little_crew.runtime.chapter_plan import contract_diff

    diffs = contract_diff(prev_exit, cur_entry)
    return {"kind": "boundary.contract", "ok": not diffs,
            "level": "green" if not diffs else "red",
            "diffs": diffs, "evidence": "chapter_plan.contract_diff"}


def _side_by_side(prev: Path, cur: Path, dst: Path) -> Path:
    from PIL import Image

    a = Image.open(prev).convert("RGB")
    b = Image.open(cur).convert("RGB")
    h = 512
    a = a.resize((int(h * a.width / max(a.height, 1)), h))
    b = b.resize((int(h * b.width / max(b.height, 1)), h))
    out = Image.new("RGB", (a.width + b.width + 8, h), (18, 18, 22))
    out.paste(a, (0, 0))
    out.paste(b, (a.width + 8, 0))
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, quality=90)
    return dst


def _default_vlm(png: Path) -> str:
    from little_crew.agent.llm import LlmClient

    b64 = base64.b64encode(Path(png).read_bytes()).decode("ascii")
    c = LlmClient("vlm-judge-cloud", temperature=0.2, timeout_s=90)
    return str(c.chat_image(VLM_PROMPT, b64, mime="image/png",
                            what="boundary.visual") or "")


def audit_visual(prev_frames: Sequence[str | Path] | None,
                 cur_frames: Sequence[str | Path] | None,
                 *, judge: Callable[[Path], str] | None = None,
                 composite: str | Path | None = None) -> dict[str, Any]:
    """画面线：上一章最后帧 vs 本章第一帧；无帧则黄项。"""
    p = Path(str(prev_frames[0])) if prev_frames else None
    c = Path(str(cur_frames[0])) if cur_frames else None
    if not p or not c or not p.is_file() or not c.is_file():
        return {"kind": "boundary.visual", "ok": None, "level": "yellow",
                "diffs": ["边界帧缺失，无法自动判"], "judge": "",
                "evidence": "frames missing"}
    comp = Path(str(composite)) if composite else (p.parent / "boundary_compare.jpg")
    _side_by_side(p, c, comp)
    j = judge or _default_vlm
    try:
        out = str(j(comp) or "")
    except Exception as e:                                   # noqa: BLE001
        return {"kind": "boundary.visual", "ok": None, "level": "yellow",
                "diffs": [f"VLM 失败：{type(e).__name__}: {str(e)[:100]}"],
                "judge": "", "evidence": str(comp)}
    import json as _json
    import re as _re

    verdict: dict[str, Any] = {}
    try:
        m = _re.search(r"\{.*\}", out, _re.S)
        verdict = _json.loads(m.group(0)) if m else {}
    except Exception:                                        # noqa: BLE001
        verdict = {}
    bad: list[str] = []
    for k, label in (("sameSpace", "空间不一致"),
                     ("sameCostume", "人物造型不一致"),
                     ("sameLighting", "光线/时段不连续")):
        if verdict.get(k) is False:
            bad.append(label)
    if not verdict:
        return {"kind": "boundary.visual", "ok": None, "level": "yellow",
                "diffs": ["VLM 返回不可解析，转人工"], "judge": out[:300],
                "evidence": str(comp)}
    return {"kind": "boundary.visual", "ok": not bad,
            "level": "green" if not bad else "red",
            "diffs": bad, "judge": str(verdict.get("diff") or "")[:200],
            "verdict": verdict, "evidence": str(comp)}


def audit_chapter(prev_exit: Any, cur_entry: Any,
                  prev_frames: Sequence[str | Path] | None = None,
                  cur_frames: Sequence[str | Path] | None = None,
                  *, judge: Callable[[Path], str] | None = None,
                  composite: str | Path | None = None) -> dict[str, Any]:
    """合并契约线 + 画面线；红项优先，黄项次之。"""
    c = audit_contract(prev_exit, cur_entry)
    v = audit_visual(prev_frames, cur_frames, judge=judge, composite=composite)
    level = "red" if "red" in (c["level"], v["level"]) else (
        "yellow" if "yellow" in (c["level"], v["level"]) else "green")
    return {"kind": "boundary.audit", "level": level,
            "ok": level == "green",
            "contract": c, "visual": v,
            "retakeScope": "boundary_shots_only" if level == "red" else ""}
