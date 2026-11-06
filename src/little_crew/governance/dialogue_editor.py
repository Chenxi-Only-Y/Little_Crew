"""对白编辑：把"一句台词"变成"能演的几句话"（2026-11-06，`dialogue_editor` 的规则层）。

## 为什么需要它

现在 TTS 是**整句念**：

```
"他是外乡人。你留他，村里人怎么看你。"  ->  一口气念完
```

后果是两件事：

1 **没有停顿** —— 观众听到的是"播报"，不是"人说话"
2 **无法对齐** —— 整句 6.4 秒，镜头 4 秒，只能整个 `extend`；
   拆成两句后，每句可以各自对齐（一句贴镜头，一句跨镜）

## 这一层做什么（纯规则，不调模型）

| 步骤 | 规则 |
|---|---|
| **断句** | 在 `。！？；…` 处切；逗号只在长句（>18 字）且离上次切点够远时切 |
| **停顿** | 句号后 0.35s · 问号/感叹 0.3s · 逗号 0.18s · 省略号 0.5s |
| **估算** | 中文约 **4.2 字/秒**（本机 TTS 实测均值，见 `research/` 的记录口径） |
| **建议** | 估算时长 vs 镜头时长 -> `ok` / `tight`（要提速）/ `split`（该拆镜） |

## 不做什么

- **不改台词内容**（润色是创作，不在这里偷偷做）
- **不做情感标注**（那需要模型判断，属"只能承认"那一类）
"""
from __future__ import annotations

#: ---- 本机 TTS 的时长模型（2026-11-06 用真合成标定，不是猜的）----
#:
#: 起初写的是"4.2 字/秒"，实测发现**短句被严重低估**：
#: "走。" 估 0.48s、实际 1.20s —— 因为 TTS 有**固定起音开销**。
#:
#: 于是用 8 句真合成做最小二乘拟合（breeze-tts-2 / 成年男声）：
#:
#:     时长 ≈ ONSET_SECONDS + PER_CHAR_SECONDS × 字数
#:
#: 实测样本（字 -> 秒）：
#:   1->1.20  2->1.20  3->1.52  4->1.60  5->1.60  7->1.20  10->2.16  14->2.48
#: 平均绝对误差 **0.15s**、最大 0.54s。
ONSET_SECONDS = 1.08
PER_CHAR_SECONDS = 0.094

#: 兼容旧名字：推算出的"字/秒"（= 1 / PER_CHAR_SECONDS ≈ 10.6）
#: ⚠️ 别拿它当除法常数用 —— 短句会被低估，必须带上 ONSET_SECONDS。
CHARS_PER_SECOND = round(1.0 / PER_CHAR_SECONDS, 2)

#: 标点 -> 停顿秒数
PAUSE = {
    "。": 0.35, "！": 0.30, "？": 0.30, "；": 0.30,
    "…": 0.50, "，": 0.18, "、": 0.12, "：": 0.20,
}

#: 允许出现停顿的字符（断句点）
_BREAKS = "。！？；…，、："

#: 超过这个长度才考虑在逗号处切
LONG_LINE = 18


def segments(line: str, *, long_line: int = LONG_LINE) -> list[dict]:
    """把一句台词切成**可分的语段**，每段带"后面停多久"。

    返回 `[{"text": "...", "pause": 0.35}, ...]` —— 最后一段 `pause=0`。
    """
    text = str(line or "").strip()
    if not text:
        return []
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _BREAKS:
            # 逗号/顿号只在"这一句已经够长"时才切，否则继续攒
            if ch in "，、：":
                if len(buf) < long_line:
                    continue
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())

    rows = []
    for i, seg in enumerate(out):
        p = 0.0
        if i < len(out) - 1:
            tail = seg[-1] if seg else ""
            p = PAUSE.get(tail, 0.25)
        rows.append({"text": seg, "pause": round(p, 2)})
    return rows


def estimate_segment(text: str) -> float:
    """单段语音时长 = 起音 + 字数 × 每字。**不含停顿。**"""
    n = len(_spoken(text or ""))
    return round(ONSET_SECONDS + PER_CHAR_SECONDS * n, 3)


def estimate(segs) -> float:
    """估总时长（秒）= 各段语音 + 各段停顿。**只用来给建议，不当真值。**"""
    total = 0.0
    for s in segs or ():
        total += estimate_segment(s.get("text") or "")
        total += float(s.get("pause") or 0)
    return round(total, 2)


def _spoken(text: str) -> str:
    """去掉标点后数"要念的字"（标点不发音）。"""
    return "".join(ch for ch in str(text) if ch not in _BREAKS + "“”「」\"'（）()《》—— ")


def advise(line: str, shot_seconds: float, *, tolerance: float = 0.5) -> dict:
    """给这条台词出一句**能直接照做**的建议。"""
    segs = segments(line)
    est = estimate(segs)
    shot = float(shot_seconds or 0)
    diff = round(est - shot, 2)
    if shot <= 0:
        adv = "没有镜头时长，无法判断"
    elif abs(diff) <= tolerance:
        adv = "ok"
    elif diff > 0 and diff / shot <= 0.2:
        adv = "tight（略长，可小幅提速）"
    elif diff > 0:
        adv = f"split（估算比镜头长 {diff:.1f}s，建议拆镜或精简）"
    else:
        adv = "短于镜头（可留白或加停顿）"
    return {"segments": len(segs), "estimatedSeconds": est,
            "shotSeconds": round(shot, 2), "delta": diff, "advice": adv}


def speech_plan(line: str, shot_seconds: float = 0.0) -> dict:
    """给合成层的完整计划：几段、每段文字、每段后停多久。"""
    segs = segments(line)
    return {"kind": "dialogue.plan", "segments": segs,
            "totalPause": round(sum(s["pause"] for s in segs), 2),
            "estimate": estimate(segs),
            "advice": advise(line, shot_seconds)}
