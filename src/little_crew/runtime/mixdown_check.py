"""混流自检：时间轴 · SRT 格式 · 缺素材的处置（不跑真 ffmpeg 也能验大半）。"""
from __future__ import annotations

import sys

from little_crew.runtime.mixdown import (  # noqa: PLC0415
    loudnorm_apply, loudnorm_measure,
    _loudness_filter, _ts, build, loudness_db, plan, srt,
)

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


U = [{"ok": True, "audioSeconds": 1.2, "character": "少女", "text": "你别走。"},
     {"ok": True, "audioSeconds": 6.4, "character": "父亲", "text": "他是外乡人。"},
     {"ok": False, "audioSeconds": 0.0, "character": "旅人", "text": "合成的失败句"}]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 66)
    print("混流（时间轴 / SRT / 缺素材处置）—— 自检")
    print("=" * 66)

    print(); print("A. 时间轴")
    rows = plan(U)
    check("A1 ★★ 失败句不进时间轴", len(rows) == 2, str(len(rows)))
    check("A2 ★ 首句从 0 开始", rows[0]["start"] == 0.0, str(rows[0]))
    check("A3 ★★ 第二句 = 首句时长 + 间隙",
          abs(rows[1]["start"] - (1.2 + 0.25)) < 1e-6, str(rows[1]["start"]))
    check("A4 ★ 每句 end - start = 音频时长",
          all(abs((r["end"] - r["start"]) - d) < 1e-6
              for r, d in zip(rows, (1.2, 6.4))), str(rows))
    check("A5 空输入不崩", plan([]) == [], "")

    print(); print("B. SRT 格式")
    text = srt(rows)
    check("B1 ★★★ 时间格式 HH:MM:SS,mmm",
          "00:00:00,000 --> 00:00:01,200" in text, text.splitlines()[1] if len(text.splitlines()) > 1 else "")
    check("B2 ★★ 带说话人", "少女：你别走。" in text, "")
    check("B3 ★ 条目编号从 1 开始", text.startswith("1\n"), text[:8])
    check("B4 ★★ 毫秒补零（不是 1200 这种）",
          _ts(1.2) == "00:00:01,200" and _ts(61.05) == "00:01:01,050",
          f"{_ts(1.2)} / {_ts(61.05)}")
    check("B5 空列表返回空串", srt([]) == "", "")

    print(); print("C. 缺素材的处置（不许编）")
    r = build(rows, "/tmp/_never.wav")            # rows 里的 wav 都是空 -> 直接拒绝
    check("C1 ★★★ 没有对白文件 -> 明确失败（不是混个静音糊过去）",
          r["ok"] is False and r["error"] == "no-voice-files", str(r)[:80])
    check("C2 失败时给出原因说明", "无从谈起" in r.get("note", ""), r.get("note", ""))

    print(); print("D. 响度归一（EBU R128）")
    check("D1 ★★★ 开启时用 loudnorm 到 -16 LUFS",
          "loudnorm=I=-16.0" in _loudness_filter(True, -16.0, -1.5)
          and "TP=-1.5" in _loudness_filter(True, -16.0, -1.5),
          _loudness_filter(True, -16.0, -1.5))
    check("D2 ★★ 关闭时退化为 anull（不偷改音量）",
          _loudness_filter(False, -16.0, -1.5) == "[mixed2]anull[out]",
          _loudness_filter(False, -16.0, -1.5))
    check("D3 ★★★ 目标值可配置（不是写死 -16）",
          "I=-14.0" in _loudness_filter(True, -14.0, -2.0),
          _loudness_filter(True, -14.0, -2.0))
    check("D4 ★★ 测不出的文件返回 0.0（不抛）",
          loudness_db("/tmp/_never_exists.wav") == 0.0, "")


    print(); print("E. 两遍响度（先扫后归）")
    check("E1 ★★★ 没有测量结果时不硬归（返回失败，不是假装成功）",
          loudnorm_apply("/tmp/_a.wav", "/tmp/_b.wav", {})["ok"] is False, "")
    check("E2 ★★ 测不出的文件返回空 dict（不抛）",
          loudnorm_measure("/tmp/_never_exists.wav") == {}, "")
    # 用真实 ffmpeg 时（本机有）验一次端到端：-18.9 -> 应该靠近 -16
    import shutil as _sh
    _ff = _sh.which("ffmpeg") or r"D:\AI\FFmpeg\bin\ffmpeg.exe"
    import pathlib as _p
    _src = _p.Path(r"D:\AI\ds.8\Little_Crew\outputs\_EF22\delivery\mix.wav")
    if _p.Path(_ff).is_file() and _src.is_file():
        _m = loudnorm_measure(str(_src), ffmpeg=_ff)
        _dst = _p.Path(r"D:\AI\ds.8\Little_Crew\outputs\_2pass_check.wav")
        # ⚠️ 变量名不能叫 `_r`：模块里 `_r` 是**结果列表**，
        # 覆盖它会让最后的统计 `for _, ok, _ in _r` 直接炸（今晚踩到）。
        _ap2 = loudnorm_apply(str(_src), str(_dst), _m, ffmpeg=_ff)
        _after = loudness_db(str(_dst), ffmpeg=_ff) if _ap2.get("ok") else 0.0
        check("E3 ★★★ 两遍后落在 -16 +/- 0.8 LU",
              bool(_ap2.get("ok")) and abs(_after + 16.0) <= 0.8,
              f"after={_after}")
        _dst.unlink(missing_ok=True)
    else:
        check("E3 ★★★ 两遍后落在 -16 +/- 0.8 LU（跳过：无 ffmpeg/无样本）", True, "skipped")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 66)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
