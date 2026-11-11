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
                    "shotSeconds": float(getattr(shot, "duration_sec", 0) or 0),
                    "source": str(ln.get("source") or "")})
    return out, dropped


def append_silence(path, seconds: float) -> dict:
    """在 wav 末尾补一段静音（**句尾呼吸**）。原地改写，只用标准库。"""
    import wave

    sec = float(seconds or 0)
    if sec <= 0:
        return {"ok": True, "appended": 0.0}
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
        n = int(round(sec * params.framerate))
        pad = b"\x00" * (params.sampwidth * params.nchannels) * n
        with wave.open(str(path), "wb") as out:
            out.setparams(params)
            out.writeframes(frames)
            out.writeframes(pad)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "appended": 0.0, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "appended": round(sec, 3)}


def trim_silence(path, out_path, *, threshold: int = 300,
                 keep: float = 0.10) -> dict:
    """裁掉 wav 头尾的静音（16-bit PCM）。**只用标准库**。

    为什么需要：breeze 每段合成都自带头尾静音；分句合成后如果不裁，
    我的"设计停顿"会和它的"自带静音"叠在一起 ——
    实测整句 3.76s 变 4.43s，越拆越长，对齐反而更差。
    """
    import wave

    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            frames = w.readframes(w.getnframes())
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "trimmed": 0.0}
    sw, ch = params.sampwidth, params.nchannels
    if sw != 2 or ch != 1:
        # 只处理 16-bit 单声道（breeze 的输出就是这个），其它原样返回
        return {"ok": True, "trimmed": 0.0, "note": "unsupported-format"}

    import array

    a = array.array("h")
    a.frombytes(frames[: len(frames) - (len(frames) % 2)])
    n = len(a)
    lo = 0
    while lo < n and abs(a[lo]) < threshold:
        lo += 1
    hi = n
    while hi > lo and abs(a[hi - 1]) < threshold:
        hi -= 1
    if lo >= hi:
        return {"ok": True, "trimmed": 0.0, "note": "all-silent"}
    pad = int(round(keep * params.framerate))
    lo = max(0, lo - pad)
    hi = min(n, hi + pad)
    with wave.open(str(out_path), "wb") as out:
        out.setparams(params)
        out.writeframes(a[lo:hi].tobytes())
    return {"ok": True, "trimmed": round((n - (hi - lo)) / params.framerate, 3),
            "kept": round((hi - lo) / params.framerate, 3)}


def join_wavs(paths, out_path, gaps) -> dict:
    """把多段 wav 按**指定停顿**拼成一条。只用标准库 `wave`。

    为什么不用 FFmpeg：这是**逐句合成后的拼接**，发生在 TTS 内部，
    不该让核心层依赖外部命令（而且比调进程快得多）。
    """
    import wave

    paths = [str(x) for x in (paths or ()) if x]
    if not paths:
        return {"ok": False, "seconds": 0.0, "error": "no-segments"}
    try:
        with wave.open(paths[0], "rb") as w0:
            params = w0.getparams()
        out = wave.open(str(out_path), "wb")
        out.setparams(params)
        silent_frame = b"\x00" * (params.sampwidth * params.nchannels)
        total = 0
        for i, path in enumerate(paths):
            with wave.open(path, "rb") as w:
                if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (
                        params.framerate, params.nchannels, params.sampwidth):
                    out.close()
                    return {"ok": False, "seconds": 0.0,
                            "error": "segment-params-mismatch"}
                frames = w.readframes(w.getnframes())
                out.writeframes(frames)
                total += w.getnframes()
            gap = float((gaps or [])[i] if i < len(gaps or []) else 0.0)
            if gap > 0:
                n = int(round(gap * params.framerate))
                out.writeframes(silent_frame * n)
                total += n
        out.close()
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "seconds": 0.0, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "seconds": round(total / float(params.framerate), 3),
            "error": "", "segments": len(paths)}


def synth_with_pauses(text, out_path, *, synth, voice="", instruction="",
                      plan=None, max_seconds: float = 0.0,
                      min_pause: float = 0.08, seed=None) -> dict:
    """**按语段分别合成，再按停顿拼接** —— 对白编辑的落地。

    与"整句念"的区别：句子之间有**真实静音**（0.18~0.35 秒），
    听感是"人在说话"而不是"播报"；而且每段时长可分别对齐。
    """
    import tempfile
    from pathlib import Path

    from little_crew.governance.dialogue_editor import speech_plan

    pl = plan or speech_plan(text)
    segs = pl.get("segments") or []
    # 若给了镜头时长：**先估一遍，超了就按比例压缩停顿**（不压到 min_pause 以下）。
    # 这是"分句合成"真正的收益 —— 停顿是参数，可以拿来对齐。
    if max_seconds > 0 and len(segs) > 1:
        from little_crew.governance.dialogue_editor import estimate_segment as _es

        _speech = sum(_es(s["text"]) for s in segs)
        _est = _speech + sum(s["pause"] for s in segs)
        if _est > max_seconds:
            _scale = max(0.0, (max_seconds - _speech)
                         / max(1e-6, sum(s["pause"] for s in segs)))
            for s in segs:
                s["pause"] = round(max(min_pause, s["pause"] * _scale), 2)
            pl["segments"] = segs
            pl["pauseScaledTo"] = round(max_seconds, 2)
    _kw = {"voice": voice, "instruction": instruction}
    if seed:
        _kw["seed"] = seed
    _tail = float(pl.get("tailPause") or 0.0)
    if len(segs) <= 1:
        r = synth(text, out_path, **_kw)
        if r.get("ok") and not Path(str(out_path)).is_file():
            r = {"ok": False, "seconds": 0.0,
                "error": "synth-说成功但没产出文件"}
        r.setdefault("segments", 1)
        if r.get("ok") and _tail > 0:
            append_silence(out_path, _tail)
            if not Path(str(out_path)).is_file():
                return {"ok": False, "seconds": 0.0,
                    "error": "join-没有产出文件", "segments": len(parts),
                    "sha256": ""}
            r["seconds"] = round(float(r.get("seconds") or 0) + _tail, 3)
            r["tailPauseSeconds"] = _tail
        return r

    tmpdir = Path(tempfile.mkdtemp(prefix="dlg_"))
    parts, gaps, total = [], [], 0.0
    _trimmed_total = 0.0
    try:
        for i, seg in enumerate(segs):
            sp = tmpdir / f"seg{i:02d}.wav"
            r = synth(seg["text"], sp, **_kw)
            if not r.get("ok"):
                return {"ok": False, "seconds": 0.0, "error": r.get("error", ""),
                        "segments": i, "sha256": ""}
            trimmed = sp
            tr = trim_silence(sp, tmpdir / f"trim{i:02d}.wav")
            if tr.get("ok"):
                trimmed = tmpdir / f"trim{i:02d}.wav"
                _trimmed_total += float(tr.get("trimmed") or 0)
            parts.append(str(trimmed))
            gaps.append(float(seg.get("pause") or 0.0))
            total += float(r.get("seconds") or 0)
        j = join_wavs(parts, out_path, gaps)
        if j.get("ok") and not Path(str(out_path)).is_file():
            return {"ok": False, "seconds": 0.0,
                "error": "join-说成功但没产出文件",
                "segments": len(parts), "sha256": ""}
        if not j.get("ok"):
            return {"ok": False, "seconds": 0.0, "error": j.get("error", ""),
                    "segments": len(parts), "sha256": ""}
        if _tail > 0:
            append_silence(out_path, _tail)
        return {"ok": True,
                "seconds": round(j["seconds"] + _tail, 3),
                "voiceseconds": round(total, 3),
                "pauseSeconds": round(sum(gaps), 3),
                "tailPauseSeconds": _tail,
                "trimmedSeconds": round(_trimmed_total, 3),
                "segments": len(parts), "error": "", "sha256": ""}
    finally:
        for p in tmpdir.glob("*.wav"):
            p.unlink(missing_ok=True)
        try:
            tmpdir.rmdir()
        except OSError:
            pass


def build(lines, shots, *, synth, cast_map, max_lines: int = 0,
          out_dir=None) -> dict:
    """逐句合成 + 对齐，返回可直接落盘的证据结构。

    `synth(text, path, voice=..., instruction=...)` —— 后端由调用方注入
    （breeze / sapi 都可以，这里不绑定具体引擎）。

    `max_lines`：**0 表示全部合成**。默认改成 0 是因为吃过亏：
    之前默认 6，一集 32 句只合了 6 句 —— 成片 163 秒里 **145 秒是静音**。
    """
    from pathlib import Path

    from little_crew.governance.voice_cast import align
    from little_crew.review.voice_check import check

    rows: list[dict] = []
    dropped: list[dict] = []
    ordered, dropped = assign_to_shots(lines, shots)
    _rows_in = ordered if not max_lines else ordered[:max_lines]
    for n, ln in enumerate(_rows_in, 1):
        who = ln["character"]
        spec = (cast_map or {}).get(who) or {}
        wav = ""
        r = {"ok": False, "seconds": 0.0, "error": "skipped", "voice": "",
             "sha256": ""}
        if out_dir:
            wav = str(Path(str(out_dir)) / f"voice_{n:02d}_{who}.wav")
            # 走对白编辑：分句合成 + 停顿拼接（单句时自动退化为整句合成）
            # 音色=ver（固定锚点）· 语气=instruction（角色基调 + 本句情绪）
            from little_crew.governance.voice_cast import emotion_note

            _ins = str(spec.get("instruction") or "")
            _emo = emotion_note(str(ln.get("emotion") or ""))
            if _emo:
                _ins = f"{_ins}，{_emo}" if _ins else _emo
            # 合成失败**重试一次**（2026-11-10）
            #
            # 真跑 EF14 出现过：33 句里 1 句 `FileNotFoundError`（wav 没写出来），
            # 而单独重跑同一句立刻成功 —— 是**瞬时失败**（TTS/显存争用）。
            # 一句失败 = 那一句在成片里是静音的，靠人眼看不出来，所以必须自愈。
            # 3 次尝试 + 退避（2026-11-11）：
            # 同一句话在跑挂之后**手动重调就成功** —— 说明是后端瞬时不可用，
            # 2 次不够；再加异常包裹（FileNotFound 之类也要算一次失败，
            # 不能让它直接炸掉整句）。
            import time as _time

            _attempts = 3
            for _att in range(1, _attempts + 1):
                try:
                    r = synth_with_pauses(
                        ln["text"], wav, synth=synth,
                        voice=str(spec.get("voice") or ""),
                        instruction=_ins,
                        seed=int(spec.get("seed") or 0) or None,
                        max_seconds=float(ln.get("shotSeconds") or 0))
                except Exception as _e_att:                   # noqa: BLE001
                    # 把**真实堆栈**留一份：这类"换个进程就能成功"的失败，
                    # 只看 `类型: 消息` 永远查不出是哪一行（2026-11-11）
                    import traceback as _tb

                    _tail = _tb.format_exc().strip().splitlines()[-4:]
                    print(f"  ⚠️ 配音异常（{ln.get('shotId')} · 第 {_att} 次）："
                          f"{type(_e_att).__name__}: {_e_att}")
                    for _ln_t in _tail:
                        print(f"       {_ln_t.strip()[:110]}")
                    r = {"ok": False, "seconds": 0.0,
                         "error": f"{type(_e_att).__name__}: {_e_att}"}
                if r.get("ok"):
                    if _att > 1:
                        r["retried"] = _att
                    break
                if _att < _attempts:
                    print(f"  ↻ 配音重试（第 {_att + 1} 次）：{ln.get('shotId')}"
                          f" · {str(r.get('error') or '')[:60]}")
                    _time.sleep(0.8 * _att)
                # 最终失败也要留痕：试了几次、最后一次错什么
                # （之前只在成功时写 retried，失败行永远是 0 —— 看不出重试过）
                r["retried"] = _att
                r["attempts"] = _attempts
        a = align(float(r.get("seconds") or 0),
                  float(ln.get("shotSeconds") or 0))
        rows.append({
            "shotId": ln["shotId"], "character": who, "text": ln["text"],
            "source": str(ln.get("source") or ""),
            "ok": bool(r.get("ok", True)), "audioSeconds": float(r.get("seconds") or 0),
            "shotSeconds": float(ln.get("shotSeconds") or 0),
            "voice": str(spec.get("voice") or ""),
            "instruction": str(spec.get("instruction") or ""),
            "wav": wav, "sha256": r.get("sha256", ""),
            "speechSegments": int(r.get("segments") or 1),
            "pauseSeconds": float(r.get("pauseSeconds") or 0.0),
            "error": r.get("error", ""),
            #: 第几次尝试成功的（1 = 一次过；>=2 = 重试过）——
            #: 只在行里留痕不够，要能进报告才能发现"后端在抖动"
            "retried": int(r.get("retried") or 0),
            "attempts": int(r.get("attempts") or 1),
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
