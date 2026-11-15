"""音频来源自检（2026-11-16）：**视频模型原生音频优先，TTS 是兜底**。

锁住四件事：
1 `minimax-h3` 声明原生音轨、`wan22-5b-ti2v` 不声明；未知模型**不猜**（按需要 TTS）
2 EpisodeRunner `_video_native_audio()`：视频模式下默认 H3；显式模型优先；env 兜底
3 默认负向提示词禁止"对话气泡/字幕"（B2 实测模型会把台词画进画面）
4 原生音频时配音阶段跳过 TTS 的判断依据可复核（不静默）
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

_r = []


def check(name, cond, detail=""):
    _r.append((name, bool(cond), detail))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 68)
    print("音频来源（视频原生音频 / TTS 兜底）—— 自检")
    print("=" * 68)

    from little_crew.models.registry import native_audio_for

    print(); print("A. 模型能力表")
    h3 = native_audio_for("minimax-h3")
    check("A1 ★★★ minimax-h3 声明原生音轨",
          h3.get("nativeAudio") is True and h3.get("checked") is True, str(h3))
    wan = native_audio_for("wan22-5b-ti2v")
    check("A2 ★★★ wan22 未声明原生音轨（需要 TTS）",
          wan.get("nativeAudio") is False and wan.get("checked") is True, str(wan))
    ghost = native_audio_for("ghost-video")
    check("A3 ★★★ 未知模型 -> 不猜（checked=False，按需要 TTS）",
          ghost.get("nativeAudio") is False and ghost.get("checked") is False, str(ghost))

    print(); print("B. EpisodeRunner 判断")
    from little_crew.runtime.episode import EpisodeRunner

    r = EpisodeRunner.__new__(EpisodeRunner)
    r.video_mode = True
    r.video_model = ""
    b1 = r._video_native_audio()
    check("B1 ★★★ 视频模式 + 未显式声明 -> 默认 H3（原生音频，不起 TTS）",
          b1.get("modelId") == "minimax-h3" and b1.get("nativeAudio") is True, str(b1))
    r.video_model = "wan22-5b-ti2v"
    b2 = r._video_native_audio()
    check("B2 ★★★ 显式声明 wan22 -> 需要 TTS",
          b2.get("nativeAudio") is False and b2.get("modelId") == "wan22-5b-ti2v", str(b2))
    r.video_mode = False
    r.video_model = ""
    b3 = r._video_native_audio()
    check("B3 ★★ 非视频模式且未声明 -> 未知（需要 TTS 兜底）",
          b3.get("checked") is False and b3.get("nativeAudio") is False, str(b3))

    print(); print("C. 默认负向提示词")
    from little_crew.agent.ir_bridge import default_negative_prompt

    neg = default_negative_prompt()
    check("C1 ★★★ 默认负向禁止“字幕”", "字幕" in neg, neg[:80])
    check("C2 ★★★ 默认负向禁止“对话气泡”（B2 实测会画进画面）",
          "对话气泡" in neg or "气泡框" in neg, neg[:100])
    check("C3 ★★ 原有负向项仍保留（多手/水印）",
          "多手" in neg and "水印" in neg, "")

    n = sum(1 for _, ok, _ in _r if ok)
    print()
    for name, ok, detail in _r:
        print(f" [{'v' if ok else 'X'}] {name}")
        if detail and not ok:
            print(f"       {detail}")
    print("=" * 68)
    print(f"结果：{n}/{len(_r)} 通过")
    return 0 if n == len(_r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
