"""配音（TTS）服务的测试。

## 为什么不测真实合成

真实合成要连微软的朗读服务 —— 那会让测试**依赖网络**（CI 上必挂、
离线必挂），而且每次几秒。所以这里测的是**我们自己的逻辑**：

  · 不可用时给的是**可执行的修复步骤**，不是一句"未安装"
  · 空台词要**明确报错**（静默产出 0 字节文件会让下游更难查）
  · 语速换算（倍率 → edge-tts 的百分比）
  · **无声视频 + 混音要降级成替换**，而不是让 ffmpeg 报错

真实合成我在开发时手工验过（一条中文台词 → 26928 字节、4.49 秒的
24 kHz 单声道 MP3，`ffmpeg -i` 能看到音轨），那是"能不能用"的验证；
这里守的是"出问题时会不会骗人"。
"""

from __future__ import annotations

import pathlib

import pytest

from app.services import tts


# ------------------------------------------------------------------
# 不可用时的报错质量
# ------------------------------------------------------------------

def test_unavailable_error_lists_actionable_steps(monkeypatch):
    """引擎不可用时，错误信息必须告诉用户**怎么做**。

    用户看到"未安装"只会困惑：装什么？装哪？而这里要给出跑哪个脚本、
    或者"改用另一个选项"这种当下就能做的动作。
    """
    monkeypatch.setattr(tts, "tools_python", lambda: None)
    monkeypatch.setattr(tts, "available", lambda: False)

    with pytest.raises(tts.TTSUnavailable) as ei:
        tts.synthesize("随便一句话", "x.mp3")

    msg = str(ei.value)
    assert "setup_tools.ps1" in msg, "要给出可执行的安装命令"
    assert "声音来源" in msg or "不要声音" in msg, "要给出当下可用的替代动作"
    assert "LITTLE_CREW_TOOLS" in msg, "要说明换了位置该改哪里"


def test_empty_text_is_rejected_not_silently_written(monkeypatch):
    """空台词要直接报错。

    若放任它去调引擎，会得到一个 0 字节文件 —— 下游拿到"成功"却是
    空的，排查起来比当场报错难得多。
    """
    monkeypatch.setattr(tts, "tools_python", lambda: "/usr/bin/python")
    monkeypatch.setattr(tts, "available", lambda: True)

    for bad in ("", "   ", "\n\t"):
        with pytest.raises(tts.TTSUnavailable) as ei:
            tts.synthesize(bad, "x.mp3")
        assert "台词为空" in str(ei.value)


def test_tools_python_is_none_without_tools_dir(monkeypatch, tmp_path):
    """没有外部工具目录时，`tools_python()` 返回 None（而不是抛异常）。

    目录不存在是**正常情况**（只想用云端的用户就不需要它），
    不该让任何调用方崩掉。
    """
    import app.services.whitebox_video as wv

    monkeypatch.setattr(wv, "tools_dir", lambda: None)
    assert tts.tools_python() is None
    assert tts.available() is False


# ------------------------------------------------------------------
# 语速换算（倍率 → edge-tts 的百分比）
# ------------------------------------------------------------------

@pytest.mark.parametrize("speed,expect", [
    (1.0, "+0%"),
    (1.25, "+25%"),
    (0.8, "-20%"),
    (1.5, "+50%"),
])
def test_speed_is_converted_to_percent(speed, expect, monkeypatch, tmp_path):
    """`tts_speed` 是倍率（1.0 = 原速），而 edge-tts 的 `--rate` 要百分比。

    混用会让"1.25 倍速"变成"+1%"（几乎没变）—— 错了也不报错，只是语速不对。
    """
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        out = pathlib.Path(cmd[cmd.index("--write-media") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 1000)   # 假装产出了音频

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(tts, "tools_python", lambda: "py.exe")
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    tts.synthesize("台词", tmp_path / "a.mp3", speed=speed)
    cmd = captured["cmd"]
    assert cmd[cmd.index("--rate") + 1] == expect


def test_speed_is_clamped_to_sane_range(monkeypatch, tmp_path):
    """极端倍率要收敛 —— 否则引擎报错或语速失真成听不清。"""
    seen: list[str] = []

    def fake_run(cmd, **kw):
        seen.append(cmd[cmd.index("--rate") + 1])
        out = pathlib.Path(cmd[cmd.index("--write-media") + 1])
        out.write_bytes(b"\x00" * 100)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(tts, "tools_python", lambda: "py.exe")
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    tts.synthesize("x", tmp_path / "b.mp3", speed=50.0)
    tts.synthesize("x", tmp_path / "c.mp3", speed=0.0)
    assert seen == ["+100%", "-90%"], seen


def test_default_voice_is_used_when_blank(monkeypatch, tmp_path):
    """没指定音色时用默认的（而不是让引擎用它自己的英文默认值 ——
    那会给中文台词配英文发音）。"""
    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["voice"] = cmd[cmd.index("--voice") + 1]
        out = pathlib.Path(cmd[cmd.index("--write-media") + 1])
        out.write_bytes(b"\x00" * 100)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(tts, "tools_python", lambda: "py.exe")
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    tts.synthesize("中文台词", tmp_path / "d.mp3")
    assert captured["voice"] == tts.DEFAULT_VOICE
    assert tts.DEFAULT_VOICE.startswith("zh-CN"), "默认音色必须是中文的"


def test_failed_synthesis_does_not_leave_empty_file(monkeypatch, tmp_path):
    """引擎失败时要**删掉**可能留下的空文件。

    留着 0 字节文件，下游会以为配音成功了，直到合成出无声视频才发现。
    """
    out = tmp_path / "e.mp3"

    def fake_run(cmd, **kw):
        pathlib.Path(cmd[cmd.index("--write-media") + 1]).write_bytes(b"")

        class R:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return R()

    monkeypatch.setattr(tts, "tools_python", lambda: "py.exe")
    monkeypatch.setattr(tts, "available", lambda: True)
    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("x", out)
    assert not out.exists(), "失败后不该留下空文件"


# ------------------------------------------------------------------
# 混流：无声视频要降级，而不是报错
# ------------------------------------------------------------------

def test_mix_falls_back_to_replace_when_video_has_no_audio(monkeypatch, tmp_path):
    """**无声视频 + 「两者混音」→ 降级为替换**，不能让 ffmpeg 报错。

    为什么这条最重要：`amix` 需要两路输入，而无声视频里 `[0:a]` 不存在，
    ffmpeg 会直接失败。而"选了两者混音、但视频模型没产出声音"是**很常见**
    的组合（Seedance / 本机 H3 都没有原生音频）。用户要的结果（能听到台词）
    在降级后一样达成。
    """
    monkeypatch.setattr(tts, "find_ffmpeg_or_none", lambda: "ffmpeg.exe")
    monkeypatch.setattr(tts, "_has_audio_stream", lambda ff, v: False)

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

    tts.mux_audio("v.mp4", "a.mp3", tmp_path / "o.mp4", mix_with_original=True)

    cmd = captured["cmd"]
    assert "-filter_complex" not in cmd, "降级后不该再走 amix 滤镜"
    assert "0:v" in cmd and "1:a" in cmd


def test_mix_uses_amix_when_video_has_audio(monkeypatch, tmp_path):
    """视频**有**音轨时才真混音，且原声被压低（TTS 为主）。"""
    monkeypatch.setattr(tts, "find_ffmpeg_or_none", lambda: "ffmpeg.exe")
    monkeypatch.setattr(tts, "_has_audio_stream", lambda ff, v: True)

    captured: dict = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: True)

    tts.mux_audio("v.mp4", "a.mp3", tmp_path / "o.mp4",
                  mix_with_original=True, original_volume=0.3)

    cmd = captured["cmd"]
    assert "-filter_complex" in cmd
    filt = cmd[cmd.index("-filter_complex") + 1]
    assert "amix" in filt
    assert "volume=0.30" in filt, "原声要按指定音量压低，否则会盖住配音"


def test_missing_ffmpeg_error_is_actionable(monkeypatch, tmp_path):
    """缺 ffmpeg 时报错同样要给出修复步骤。"""
    monkeypatch.setattr(tts, "find_ffmpeg_or_none", lambda: None)
    with pytest.raises(tts.TTSUnavailable) as ei:
        tts.mux_audio("v.mp4", "a.mp3", tmp_path / "o.mp4")
    msg = str(ei.value)
    assert "setup_tools.ps1" in msg
    assert "ffmpeg_path" in msg
