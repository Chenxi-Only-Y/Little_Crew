"""视觉闸：**真的看图**，逐镜判「崩没崩」（2026-11-08）。

## 为什么这道闸最要紧

实测：一集 23 镜里会出现「没有脸、只剩暗块」的图（`docs/13` 图里那张 75 秒的），
而当时唯一的图像审查是**可数项**（存在/尺寸/串镜）—— **它抓不到"画崩了"**。

2026-11-08 验证：`deepseek-flash` **支持图像输入**
（纯红图→"红色"、纯蓝图→"蓝色"，判别正确）。所以视觉闸不必等云 API。

## 判什么（三项，都能进返工）

| 字段 | 含义 | 处理 |
|---|---|---|
| `blank` | 几乎空白/纯色 | error → 重渲 |
| `faces` | `ok` / `broken` / `none` | `broken` → error → 重渲 |
| `matches` | 0~5，与镜头描述是否一致 | ≤2 → warn（记，不自动重渲） |

## 纪律

- **失败不静默**：API 错/解析错 → `reviewed=False`，**不算通过**
- **不评"好不好看"**：只判"崩没崩、像不像描述" —— 美丑仍需人看
- **成本可控**：默认最多看 N 镜（`max_shots`），其余标 `skipped`
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

#: 单次最多看几镜（控制成本；0 = 全部）
DEFAULT_MAX_SHOTS = 12

_PROMPT = """看图，**只输出一行 JSON，不要推理过程**：
{"blank": false, "faces": "ok", "matches": 3, "problems": []}

字段说明：
- blank：画面是否几乎空白/纯色（true/false）
- faces：人物脸部 ok / broken（无脸或糊成一团）/ none（本镜没有人）
- matches：与下面描述的**一致度 0~5**（5=完全一致，0=完全无关）
- problems：简短中文，最多 4 条

镜头描述：{desc}"""


#: 视觉审查的 max_tokens。**实测 400 不够**：推理模型会把额度全花在思考上，
#: JSON 被截断 → 表现为"模型没给 JSON"（2026-11-08 踩到，6 镜 5 镜失败）。
VISION_MAX_TOKENS = 1200


def _ask(client, image_path: Path, desc: str) -> dict:
    """一次视觉调用。返回 `{ok, blank, faces, matches, problems, error}`。"""
    try:
        data = base64.b64encode(image_path.read_bytes()).decode()
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"读图失败:{type(e).__name__}"}
    try:
        from little_crew.agent.llm import LlmClient  # noqa: F401  (类型提示用)

        reply = client.chat_image(_PROMPT.replace("{desc}", desc[:300]), data,
                                  what="画面审查",
                                  max_tokens=VISION_MAX_TOKENS)
        text = reply if isinstance(reply, str) else getattr(reply, "text", "")
        obj = _extract_json(text)
        if obj is None:
            return {"ok": False, "error": "模型没给 JSON"}
        return {"ok": True, "blank": bool(obj.get("blank")),
                "faces": str(obj.get("faces") or "?"),
                "matches": int(obj.get("matches") or 0),
                "problems": [str(x)[:80] for x in (obj.get("problems") or ())][:4],
                "error": ""}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}


def _extract_json(text: str):
    s = str(text or "")
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except Exception:                                         # noqa: BLE001
        return None


def _desc_of(s) -> str:
    """从镜头对象/字典里取"这一镜要画什么"。

    ⚠️ 实测：`episode.master.json` 里这个字段叫 **`prompt`**，
    不是 `description` —— 取错字段会让模型判「与描述无关（0/5）」，
    而其实图是对的（2026-11-08 真跑踩到）。
    """
    if isinstance(s, dict):
        for k in ("prompt", "description", "action", "focus"):
            v = s.get(k)
            if v:
                return str(v)[:400]
        return ""
    for k in ("prompt", "description", "action"):
        v = getattr(s, k, "")
        if v:
            return str(v)[:400]
    return ""


def review(shots, images, *, client, max_shots: int = DEFAULT_MAX_SHOTS) -> dict:
    """逐镜看。`images`: `{shotId: path}`。返回可落盘的结论。"""
    rows, findings = [], []
    todo = list(shots or ())
    seen = 0
    for s in todo:
        sid = str(getattr(s, "shot_id", s.get("shotId") if isinstance(s, dict) else "?"))
        desc = _desc_of(s)
        if max_shots and seen >= max_shots:
            rows.append({"shotId": sid, "reviewed": False, "reason": "超出本次上限"})
            continue
        path = images.get(sid)
        if not path or not Path(str(path)).is_file():
            rows.append({"shotId": sid, "reviewed": False, "reason": "没有图"})
            continue
        seen += 1
        r = _ask(client, Path(str(path)), desc)
        row = {"shotId": sid, "reviewed": bool(r.get("ok")),
               "blank": r.get("blank"), "faces": r.get("faces"),
               "matches": r.get("matches"), "problems": r.get("problems") or [],
               "error": r.get("error") or ""}
        rows.append(row)
        if not r.get("ok"):
            continue
        if r.get("blank"):
            findings.append({"shotId": sid, "kind": "vision.blank",
                             "severity": "error", "reason": "画面几乎空白/纯色"})
        if str(r.get("faces")) == "broken":
            findings.append({"shotId": sid, "kind": "vision.faces-broken",
                             "severity": "error",
                             "reason": "人物脸部崩坏：" + "；".join(r.get("problems") or [])[:80]})
        if int(r.get("matches") or 0) <= 2:
            findings.append({"shotId": sid, "kind": "vision.low-match",
                             "severity": "warn",
                             "reason": f"与镜头描述不一致（{r.get('matches')}/5）"})
    reviewed = [r for r in rows if r["reviewed"]]
    return {"kind": "vision.review", "rows": rows, "findings": findings,
            "reviewedCount": len(reviewed), "total": len(todo),
            "skipped": len(rows) - len(reviewed),
            "errors": sum(1 for r in rows if r.get("error")),
            "notJudged": ["美不美", "角色像不像同一个人（需相似度模型）",
                          "构图是否有意图"],
            "kindCounts": _counts(findings)}


def _counts(findings) -> dict:
    out: dict[str, int] = {}
    for f in findings or ():
        out[f["kind"]] = out.get(f["kind"], 0) + 1
    return out
