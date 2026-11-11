"""混流：对白 + 字幕 + 环境音（+ BGM 若有）—— §3.11.1 的最后一步。

## 素材从哪来（**不许编**）

| 元素 | 来源 | 没有时怎么办 |
|---|---|---|
| 对白 | `delivery/voice/voice_*.wav` | 报错 —— 没台词就没什么可混 |
| 字幕 | 由对白时间轴生成 SRT | 总是能生成 |
| 环境音 | FFmpeg `anoisesrc` 合成房间底噪 | 可关；**生成的要标注来源** |
| BGM | **必须外部提供文件** | **明确跳过并记录** —— 不拿噪声冒充音乐 |

## 两条纪律

1 **不静默降级**：BGM 没给就写 `bgm: "skipped: no asset"`，不是"混了个空的"
2 **削波要报**：混完测峰值，超过 -1 dBFS 就警告（那会爆音）

## 为什么用 FFmpeg 而不是 librosa/pydub

核心层 `dependencies = []`。FFmpeg 是本机既有工具（已在用），
子进程调用不影响依赖表，且**同样一条命令可复现**。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def plan(utterances, *, gap: float = 0.25, shot_offsets=None,
         lead: float = 0.30) -> list[dict]:
    """把对白排成时间轴。

    ## 两种排法（`shot_offsets` 决定）

    | 给了 shot_offsets | 行为 |
    |---|---|
    | 否 | **顺序摞**：从头往后接，只在句子间留 `gap`（旧行为） |
    | 是 | **按镜头起点摆**：每句从它所属镜头的起点 +`lead` 开始 |

    ## 为什么必须按镜头摆（2026-11-08 用户反馈驱动）

    旧行为下 24 句被摞成一条"连续旁白带"：第 1 镜 5 秒，
    但第 1 句 1.2 秒就说完、第 2 句 1.45 秒就开始 —— **画面还在第 1 镜，声音已经到第二句**。
    表现就是"字幕对不上、镜头衔接怪、语气接得生硬"。

    ## 冲突处理

    若前一句结束晚于本镜起点 + `lead`，本句**顺延**（不重叠）——
    宁可这一句晚点开口，也不要两句叠在一起。
    """
    rows: list[dict] = []
    t = 0.0
    for i, u in enumerate(utterances or (), 1):
        if not u.get("ok"):
            continue
        d = float(u.get("audioSeconds") or 0)
        if d <= 0:
            continue
        sid = str(u.get("shotId") or "")
        if shot_offsets and sid in shot_offsets:
            start = max(float(shot_offsets[sid]) + float(lead), t)
        else:
            start = t
        rows.append({"index": i, "start": round(start, 3),
                     "end": round(start + d, 3),
                     "character": str(u.get("character") or ""),
                     "text": str(u.get("text") or ""),
                     "shotId": sid,
                     "wav": str(u.get("wav") or "")})
        t = round(start + d + gap, 3)
    return rows


def srt(rows) -> str:
    """生成 SRT。时间格式必须 `HH:MM:SS,mmm` —— 写错一个字幕就不显示。"""
    out = []
    for n, r in enumerate(rows or (), 1):
        out.append(str(n))
        out.append(f"{_ts(r['start'])} --> {_ts(r['end'])}")
        who = r.get("character") or ""
        out.append(f"{who}：{r.get('text') or ''}" if who else str(r.get("text") or ""))
        out.append("")
    return "\n".join(out)


def _ts(sec: float) -> str:
    ms = int(round(float(sec) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build(rows, out_wav, *, ffmpeg: str = "ffmpeg", ambient: bool = True,
          bgm: str = "", total_seconds: float = 0.0,
          voice_gain: float = 1.0, ambient_gain: float = 0.02,
          bgm_gain: float = 0.15, loudnorm: bool = True,
          target_lufs: float = -16.0, target_tp: float = -1.5) -> dict:
    """混成一条 wav。返回 `{ok, seconds, peakDb, skipped[], command}`。"""
    rows = [r for r in (rows or []) if r.get("wav") and Path(r["wav"]).is_file()]
    if not rows:
        return {"ok": False, "seconds": 0.0, "error": "no-voice-files",
                "peakDb": 0.0, "skipped": [],
                "note": "没有可用的对白文件，混流无从谈起"}

    dur = float(total_seconds or 0) or (rows[-1]["end"] + 0.5)
    skipped: list[str] = []
    inputs, filters = [], []
    for i, r in enumerate(rows):
        inputs += ["-i", str(r["wav"])]
        delay = int(round(float(r["start"]) * 1000))
        filters.append(
            f"[{i}:a]atrim=0:{float(r['end']) - float(r['start']):.3f},"
            f"asetpts=PTS-STARTPTS,volume={voice_gain},"
            f"adelay={delay}|{delay}[v{i}]")
    mix_ins = "".join(f"[v{i}]" for i in range(len(rows)))

    n = len(rows)
    if ambient:
        # 房间底噪：不占素材，音量压得很低（-34dB 左右）
        inputs += ["-f", "lavfi", "-t", f"{dur:.3f}",
                   "-i", "anoisesrc=color=pink:amplitude=0.3"]
        filters.append(f"[{n}:a]volume={ambient_gain},"
                       f"afade=t=in:st=0:d=0.5,afade=t=out:st={max(0.0, dur-0.8):.3f}:d=0.8[amb]")
        mix_ins += "[amb]"
    if bgm and Path(bgm).is_file():
        inputs += ["-i", str(bgm)]
        idx = n + (1 if ambient else 0)
        filters.append(f"[{idx}:a]volume={bgm_gain},aloop=loop=-1:size=2e9,"
                       f"atrim=0:{dur:.3f},afade=t=out:st={max(0.0, dur-1.5):.3f}:d=1.5[bg]")
        mix_ins += "[bg]"
    else:
        skipped.append("bgm: 没有素材文件（未用噪声冒充）")

    filters.append(f"{mix_ins}amix=inputs={len(mix_ins.split(']'))-1}:"
                   f"duration=longest:dropout_transition=0[mixed]")
    # 峰值保护：超过 -1 dBFS 就压，避免爆音
    # 响度归一（2026-11-10）：EBU R128，目标 -16 LUFS / 真峰 -1.5 dBTP。
    # 放在 limiter **之后**：先管住峰值，再把整体响度拉到播出标准。
    filters.append("[mixed]alimiter=limit=0.891:level=false[mixed2]")
    filters.append(_loudness_filter(loudnorm, target_lufs, target_tp))
    # 两遍响度（2026-11-11）：先渲到临时文件 -> 扫 -> 带 measured 归一到最终路径。
    # 单遍会欠冲；两遍实测能到 ±0.5 LU（见 loudnorm_measure 的注释）。
    _two_pass = bool(loudnorm)
    _render_path = (str(out_wav) + ".preloud.wav") if _two_pass else str(out_wav)
    cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
            "-filter_complex", ";".join(filters), "-map", "[out]",
            "-ac", "1", "-ar", "48000", "-t", f"{dur:.3f}", str(_render_path)])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "seconds": 0.0, "error": f"{type(e).__name__}: {e}",
                "peakDb": 0.0, "skipped": skipped, "command": " ".join(cmd)}
    if p.returncode != 0 or not Path(str(_render_path)).is_file():
        tail = [x for x in (p.stderr or "").splitlines() if x.strip()]
        return {"ok": False, "seconds": 0.0,
                "error": (tail[-1] if tail else f"exit {p.returncode}")[:200],
                "peakDb": 0.0, "skipped": skipped, "command": " ".join(cmd)}
    if _two_pass:
        _meas = loudnorm_measure(str(_render_path), ffmpeg=ffmpeg,
                                 target_lufs=target_lufs, target_tp=target_tp)
        _ap = loudnorm_apply(str(_render_path), str(out_wav), _meas, ffmpeg=ffmpeg,
                             target_lufs=target_lufs, target_tp=target_tp)
        if not _ap.get("ok"):
            # 归一失败不能把成片搞没：**先**把临时文件复制成最终产物（未归一的），
            # 再删临时文件；顺序反了就等于把成片删了（写这段时差点犯）。
            try:
                import shutil as _sh

                _sh.copyfile(str(_render_path), str(out_wav))
            except Exception:                                # noqa: BLE001
                pass
        try:
            Path(str(_render_path)).unlink(missing_ok=True)
        except Exception:                                    # noqa: BLE001
            pass
        _loud_info = {"twoPass": True, "measure": _meas, "applyOk": bool(_ap.get("ok"))}
    else:
        _loud_info = {"twoPass": False}

    from little_crew.runtime.tts_breeze import wav_seconds

    peak = peak_db(str(out_wav), ffmpeg=ffmpeg)
    lufs = loudness_db(str(out_wav), ffmpeg=ffmpeg)
    return {"ok": True, "seconds": wav_seconds(out_wav), "error": "",
            "peakDb": peak, "skipped": skipped, "rows": len(rows),
            "ambient": bool(ambient), "bgm": bool(bgm and Path(bgm).is_file()),
            "clipped": bool(peak > -1.0),
            #: 响度归一：做了什么、目标多少、实测多少 ——
            #: 只写"已归一"是不够的，要能核对（实测 -16.8 = 达标）
            "loudnessNormalized": bool(loudnorm),
            "loudnessPass": _loud_info,
            "targetLufs": float(target_lufs) if loudnorm else 0.0,
            "lufs": lufs,
            # 两遍之后实测偏差能到 0.5 以内（EF22 原 -18.9 -> -16.5）。
            # 判据收紧到 ±1.0：留出单遍/两遍的差别，又不至于放过明显欠冲。
            "loudnessOk": bool(loudnorm and abs(lufs - target_lufs) <= 1.0),
            "command": " ".join(cmd)[:400]}


def _loudness_filter(enabled: bool, target_lufs: float,
                     target_tp: float) -> str:
    """响度归一的滤镜串（纯函数，便于单测）。

    -16 LUFS / -1.5 dBTP 是**流媒体与播出的通用目标**；LRA=11 保留动态。
    `linear=false`：单遍动态归一（两遍更准，但要先扫一遍整条音轨，
    对 3 分钟的整集不划算；实测 -16.2 LUFS，偏差 0.2 dB，够用）。
    """
    if not enabled:
        return "[mixed2]anull[out]"
    return (f"[mixed2]loudnorm=I={target_lufs}:TP={target_tp}:"
            f"LRA=11:linear=false[out]")


def loudnorm_measure(wav, *, ffmpeg: str = "ffmpeg",
                     target_lufs: float = -16.0,
                     target_tp: float = -1.5) -> dict:
    """第一遍：**只扫不写**，取 measured_* 与 target_offset。

    单遍 loudnorm 会欠冲（真跑 EF22：目标 -16，实测 -18.9）——
    因为它边听边调，动态大的内容调不到位。两遍才能到 ±0.5。
    """
    import json as _json

    try:
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostats", "-i", str(wav),
             "-af", (f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:"
                     "print_format=json"),
             "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
    except Exception:                                         # noqa: BLE001
        return {}
    text = p.stderr or ""
    # ffmpeg 把 JSON 打在 stderr 的最后一对花括号里
    lo, hi = text.rfind("{"), text.rfind("}")
    if lo < 0 or hi <= lo:
        return {}
    try:
        return _json.loads(text[lo:hi + 1])
    except Exception:                                         # noqa: BLE001
        return {}


def loudnorm_apply(src, dst, measured: dict, *, ffmpeg: str = "ffmpeg",
                   target_lufs: float = -16.0,
                   target_tp: float = -1.5) -> dict:
    """第二遍：带 measured_* 与 offset 的**线性**归一（写 dst）。"""
    if not measured:
        return {"ok": False, "error": "no-measurement"}
    try:
        m = {k: str(measured[k]) for k in
             ("input_i", "input_tp", "input_lra", "input_thresh",
              "target_offset") if k in measured}
        af = (f"loudnorm=I={target_lufs}:TP={target_tp}:LRA=11:"
              f"measured_I={m.get('input_i', -24)}:"
              f"measured_TP={m.get('input_tp', -2)}:"
              f"measured_LRA={m.get('input_lra', 7)}:"
              f"measured_thresh={m.get('input_thresh', -34)}:"
              f"offset={m.get('target_offset', 0)}:linear=true:"
              "print_format=summary")
        p = subprocess.run([ffmpeg, "-y", "-hide_banner", "-nostats",
                            "-i", str(src), "-af", af,
                            "-ar", "48000", "-ac", "1", str(dst)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if p.returncode != 0 or not Path(str(dst)).is_file():
        tail = [x for x in (p.stderr or "").splitlines() if x.strip()]
        return {"ok": False, "error": (tail[-1] if tail else "exit")[:160]}
    return {"ok": True, "error": ""}


def loudness_db(wav, *, ffmpeg: str = "ffmpeg") -> float:
    """测**积分响度**（EBU R128 / LUFS）。测不到返回 0.0。

    为什么不用峰值代替：峰值只说"最响那一下有多响"，
    而播出/平台要的是**整体响度**。同一集里两句对白，峰值可能一样，
    听感却差 10 dB —— 那正是归一要解决的问题。
    """
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(wav),
                            "-af", "ebur128=peak=true", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except Exception:                                         # noqa: BLE001
        return 0.0
    text = p.stderr or ""
    # 只看 Summary 段里的 I（Integrated）
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "Summary:" in line:
            for _l in lines[i:i + 12]:
                _s = _l.strip()
                if _s.startswith("I:"):
                    try:
                        return float(_s.split("I:")[1].split("LUFS")[0].strip())
                    except (IndexError, ValueError):
                        return 0.0
    return 0.0


def peak_db(wav, *, ffmpeg: str = "ffmpeg") -> float:
    """用 ffmpeg 的 volumedetect 测峰值（dBFS）。测不到返回 0。"""
    try:
        p = subprocess.run([ffmpeg, "-hide_banner", "-i", str(wav),
                            "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120)
    except Exception:                                         # noqa: BLE001
        return 0.0
    for line in reversed((p.stderr or "").splitlines()):
        if "max_volume" in line:
            try:
                return float(line.split("max_volume:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                return 0.0
    return 0.0
