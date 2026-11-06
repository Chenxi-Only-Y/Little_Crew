"""配音轨：从剧本正文里抠出台词 -> 逐句合成 -> 与镜头对齐（§3.11.1）。

## 台词从哪来

剧本的 `scenes[].text` 是**正文（对白 + 动作混写）**，没有独立对白字段。
所以这里做**容忍式抽取**，抽不到就当没有对白（**不编台词**）：

    少女：「她把信折了三折。」
    父亲：你走吧。
    母亲说道：“别去了。”

## 这一层只做三件事

1 抽台词（正则，多写法）
2 逐句合成（后端可换：breeze / sapi）
3 与镜头时长对齐（用 `voice_cast.align` 的规则），产出 `voice_used.json`

**混流（对白 + 环境音 + BGM）与口型同步都不在这层** —— 见 `notDone`。
"""
from __future__ import annotations

import re

#: 说话人 + 引号台词
_PAT_QUOTED = re.compile(
    r"([\u4e00-\u9fa5A-Za-z]{1,6}?)\s*(?:说道|说|问|喊|道)?\s*[：:，,]?\s*[「“\"]([^」”\"]{1,80})[」”\"]")
#: 说话人 + 冒号 + 裸台词（到行尾/句号）
_PAT_BARE = re.compile(
    r"^[\s　]*([\u4e00-\u9fa5A-Za-z]{1,6})\s*[：:]\s*([^\n「」“”\"]{2,60})", re.M)


def parse_dialogue(text: str, known=None) -> list[dict]:
    """从一段正文里抽台词。**宁缺勿滥**。

    `known`: 本集角色名集合。**给了就只认名单里的名字** ——
    否则会把"汤溅出来「…」"这种**动作描述**当成说话人
    （2026-11-06 真跑踩到：6 句里 6 个"说话人"全是动作）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    allow = {str(x) for x in known} if known else None

    def _ok(who: str) -> bool:
        if not who or _looks_like_action(who):
            return False
        return True if allow is None else who in allow
    raw = str(text or "")
    for m in _PAT_QUOTED.finditer(raw):
        who, line = _clean_name(m.group(1)), m.group(2).strip()
        key = (who, line)
        if line and _ok(who) and key not in seen:
            seen.add(key)
            out.append({"character": who, "text": line})
    for m in _PAT_BARE.finditer(raw):
        who, line = _clean_name(m.group(1)), m.group(2).strip(" ，,。.")
        key = (who, line)
        if line and _ok(who) and key not in seen:
            seen.add(key)
            out.append({"character": who, "text": line})
    return out


#: 名字后面可能粘着的言说动词（"母亲说道" -> "母亲"）
_SAY_VERBS = ("说道", "说", "问道", "问", "喊道", "喊", "道")


def _clean_name(word: str) -> str:
    """把粘在名字后的言说动词去掉。**名字必须干净** —— 音色是按名字查的。"""
    w = str(word or "").strip()
    changed = True
    while changed and len(w) > 1:
        changed = False
        for v in _SAY_VERBS:
            if w.endswith(v) and len(w) - len(v) >= 1:
                w = w[: -len(v)]
                changed = True
                break
    return w


def _looks_like_action(word: str) -> bool:
    """把"内景/外景/字幕"这类非人名的前缀排除掉。"""
    bad = ("内景", "外景", "字幕", "画面", "镜头", "旁白", "注", "场景")
    return any(word.startswith(b) for b in bad)


def assign_to_shots(lines, shots) -> list[dict]:
    """把台词按**场次**挂到该场第 1 个镜头上（同一场多句则按顺序往后挂）。

    挂不上（该场没有镜头）的台词**丢弃并记录** —— 不许悄悄消失。
    """
    by_scene: dict[str, list] = {}
    for s in shots or ():
        sid = str(getattr(s, "scene_id", "") or "")
        by_scene.setdefault(sid, []).append(s)
    used: dict[str, int] = {}
    out, dropped = [], []
    for ln in lines or ():
        sid = str(ln.get("sceneId") or "")
        pool = by_scene.get(sid) or []
        i = used.get(sid, 0)
        if not pool:
            dropped.append({**ln, "reason": "该场没有镜头"})
            continue
        shot = pool[min(i, len(pool) - 1)]
        used[sid] = i + 1
        out.append({**ln, "shotId": str(getattr(shot, "shot_id", "")),
                    "shotSeconds": float(getattr(shot, "duration_sec", 0) or 0)})
    return out, dropped


def build(lines, shots, *, synth, cast_map, max_lines: int = 6,
          out_dir=None) -> dict:
    """逐句合成 + 对齐，返回可直接落盘的证据结构。

    `synth(text, path, voice=..., instruction=...)` —— 后端由调用方注入
    （breeze / sapi 都可以，这里不绑定具体引擎）。
    """
    from pathlib import Path

    from little_crew.governance.voice_cast import align
    from little_crew.review.voice_check import check

    rows: list[dict] = []
    dropped: list[dict] = []
    ordered, dropped = assign_to_shots(lines, shots)
    for n, ln in enumerate(ordered[:max_lines], 1):
        who = ln["character"]
        spec = (cast_map or {}).get(who) or {}
        wav = ""
        r = {"ok": False, "seconds": 0.0, "error": "skipped", "voice": "",
             "sha256": ""}
        if out_dir:
            wav = str(Path(str(out_dir)) / f"voice_{n:02d}_{who}.wav")
            r = synth(ln["text"], wav,
                      voice=str(spec.get("voice") or ""),
                      instruction=str(spec.get("instruction") or ""))
        a = align(float(r.get("seconds") or 0),
                  float(ln.get("shotSeconds") or 0))
        rows.append({
            "shotId": ln["shotId"], "character": who, "text": ln["text"],
            "ok": bool(r.get("ok", True)), "audioSeconds": float(r.get("seconds") or 0),
            "shotSeconds": float(ln.get("shotSeconds") or 0),
            "voice": str(spec.get("voice") or ""),
            "instruction": str(spec.get("instruction") or ""),
            "wav": wav, "sha256": r.get("sha256", ""),
            "error": r.get("error", ""),
            "align": a,
        })
    ok_rows = [{"shotId": x["shotId"], "character": x["character"],
                "ok": x["ok"], "audioSeconds": x["audioSeconds"],
                "shotSeconds": x["shotSeconds"], "voice": x["voice"],
                "instruction": x["instruction"], "error": x["error"]}
               for x in rows]
    rep = check(ok_rows)
    return {
        "kind": "voice.track",
        "utterances": rows,
        "dropped": dropped,
        "report": rep,
        "notDone": ["混流（对白 + 环境音 + BGM）", "口型同步（明确不做）",
                    "跨集音色一致性（需多集样本，目前只有映射规则）"],
    }
