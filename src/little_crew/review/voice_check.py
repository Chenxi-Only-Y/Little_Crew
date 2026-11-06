"""配音的三项可数指标（§3.11.3）—— **与画面质量无关，全可数**。

| 指标 | 定义 | 本模块怎么算 |
|---|---|---|
| **音画对齐误差** | 台词结束时刻与镜尾的差（秒） | `abs(音频秒 - 镜头秒)`，逐句统计均值/最大/超容差条数 |
| **音色一致性** | 同角色跨集/跨句是否同一音色 | 同角色出现 >1 种 `voice`/`instruction` 即为不一致 |
| **TTS 失败率** | 与渲染失败率同口径 | `失败句数 / 总句数` |

## 为什么单独一个模块

语音这块最容易被"听起来还行"糊过去。
这三项不依赖人耳，**任何人拿到 `voice_used.json` 都能复算**。
"""
from __future__ import annotations

#: 对齐容差（秒）——与 `voice_cast.ALIGN_TOLERANCE` 同源
ALIGN_TOLERANCE = 0.5


def check(utterances, *, tolerance: float = ALIGN_TOLERANCE) -> dict:
    """`utterances`: `[{shotId, character, text, ok, audioSeconds, shotSeconds, voice, instruction}]`"""
    rows = []
    errors = []
    for u in utterances or ():
        ok = bool(u.get("ok", True))
        a = float(u.get("audioSeconds") or 0)
        s = float(u.get("shotSeconds") or 0)
        err = round(a - s, 3) if ok else 0.0
        row = {
            "shotId": str(u.get("shotId") or ""),
            "character": str(u.get("character") or ""),
            "ok": ok,
            "audioSeconds": round(a, 3),
            "shotSeconds": round(s, 3),
            "error": err,
            "overTolerance": bool(ok and abs(err) > tolerance),
            "voice": str(u.get("voice") or ""),
            "instruction": str(u.get("instruction") or ""),
            "error_msg": str(u.get("error") or ""),
        }
        rows.append(row)
        if ok:
            errors.append(abs(err))

    total = len(rows)
    failed = [r for r in rows if not r["ok"]]
    over = [r for r in rows if r["overTolerance"]]

    # 音色一致性：同角色必须用同一个声音
    seen: dict[str, set] = {}
    for r in rows:
        if not r["ok"]:
            continue
        key = r["character"] or "?"
        seen.setdefault(key, set()).add((r["voice"], r["instruction"]))
    inconsistent = {k: sorted(map(str, v)) for k, v in seen.items() if len(v) > 1}

    findings = []
    for r in over:
        findings.append({"shotId": r["shotId"], "kind": "align.over-tolerance",
                         "severity": "warn",
                         "reason": f"音频 {r['audioSeconds']}s vs 镜头 "
                                   f"{r['shotSeconds']}s，差 {r['error']:+.2f}s"})
    for k, v in inconsistent.items():
        findings.append({"shotId": "", "kind": "voice.inconsistent",
                         "severity": "error",
                         "reason": f"{k} 用了 {len(v)} 种声音：{v}"})
    for r in failed:
        findings.append({"shotId": r["shotId"], "kind": "tts.failed",
                         "severity": "error",
                         "reason": f"合成失败：{r['error_msg'] or '未知'}"})

    return {
        "kind": "voice.check",
        "total": total,
        "failed": len(failed),
        "failureRate": round(len(failed) / total, 3) if total else 0.0,
        "align": {
            "meanAbs": round(sum(errors) / len(errors), 3) if errors else 0.0,
            "maxAbs": round(max(errors), 3) if errors else 0.0,
            "overTolerance": len(over),
            "tolerance": tolerance,
        },
        "voiceConsistency": {"characters": len(seen),
                             "inconsistent": len(inconsistent)},
        "rows": rows,
        "findings": findings,
        "notChecked": ["口型同步（lip-sync，明确不做）",
                       "情感贴切度（需要人耳/模型判断）",
                       "背景音与 BGM（尚未接入）"],
    }
