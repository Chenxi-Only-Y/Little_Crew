"""视频服务里「与声音/路径有关」的回归测试。

## 这里守的是什么

不是"能不能出视频"（那要真调云端，或至少几秒的 ffmpeg 编码），
而是**几个静默的错误**——它们不会崩，只会让产物不对、或者让用户
拿到一句毫无线索的报错：

  1. **中文路径**：项目名默认是「未命名工程」，而 ffmpeg 的相对路径
     会解析失败。它曾经让整个合成直接报 "No such file or directory"。
  2. **`subprocess` 的编码**：中文 Windows 上 `text=True` 默认按 GBK
     解码，而 ffmpeg 输出 UTF-8 → `UnicodeDecodeError`（整个调用抛异常，
     连错误信息都看不到）。
  3. **ffmpeg 的错误被吞掉**：原先只捕超时，`CalledProcessError` 直接
     冒出去，而真正的原因全在 stderr 里被丢了。
  4. **声音策略的解析**：`auto` 该按"提供者有没有原生音频"决定，
     不是无条件配音、也不是无条件不配。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from app.services import video as video_mod


# ------------------------------------------------------------------
# 声音策略解析
# ------------------------------------------------------------------

class _FakeSpec:
    def __init__(self, native: bool) -> None:
        self.has_native_audio = native


def _apply(monkeypatch, tmp_path, *, mode: str, has_native: bool, dialogue: str = "台词一句。"):
    """跑 `_apply_audio_plan`，把外部动作全部替换成可观测的假实现。"""
    calls: dict = {}

    monkeypatch.setattr(
        "app.services.runtime_settings.get",
        lambda k, default=None: {"video_audio": mode, "tts_voice": "", "tts_speed": 1.0}.get(k, default),
    )
    monkeypatch.setattr(
        "app.services.providers.get",
        lambda cap, key: _FakeSpec(has_native) if cap == "video_gen" else None,
    )

    def fake_synth(text, out, **kw):
        calls["synthesized"] = text
        pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(out).write_bytes(b"x" * 100)

        class R:
            audio_path = str(out)
            voice = "v"
            duration_s = 1.0
        return R()

    def fake_mux(v, a, out, **kw):
        calls["mux"] = kw
        pathlib.Path(out).write_bytes(b"y" * 100)
        return str(out)

    monkeypatch.setattr("app.services.tts.synthesize", fake_synth)
    monkeypatch.setattr("app.services.tts.mux_audio", fake_mux)

    svc = video_mod.VideoService()
    svc.mock = False
    out = svc._apply_audio_plan(
        "ffmpeg", "in.mp4", dialogue, str(tmp_path), "sess",
    )
    return out, calls


def test_auto_uses_tts_when_provider_has_no_native_audio(monkeypatch, tmp_path):
    """`auto` + 提供者**没有**原生音频 + 有台词 → 应该配音。

    这是 Seedance / 图片拼接 / 本机 H3 的默认路径：模型不产出声音，
    用户也没显式指定，那当然要补上配音 —— 否则成片是哑的。
    """
    out, calls = _apply(monkeypatch, tmp_path, mode="auto", has_native=False)
    assert "synthesized" in calls, "auto 应当回落到配音"
    assert "out_tts" in pathlib.Path(out).name


def test_auto_keeps_native_when_provider_has_audio(monkeypatch, tmp_path):
    """`auto` + 提供者**有**原生音频 → 什么都不做（保留模型自己的声音）。

    万相 wan3.0 就是这种：它的产出里已经有对白/环境音，再叠一层 TTS
    要么盖掉、要么糊在一起。
    """
    out, calls = _apply(monkeypatch, tmp_path, mode="auto", has_native=True)
    assert "synthesized" not in calls
    assert "mux" not in calls
    assert out == "in.mp4", "有原生音频时该原样返回"


def test_tts_mode_overrides_native(monkeypatch, tmp_path):
    """显式选 TTS 时，即使模型自带音频也要用配音（用户要固定音色念台词）。"""
    out, calls = _apply(monkeypatch, tmp_path, mode="tts", has_native=True)
    assert "synthesized" in calls
    assert calls["mux"].get("mix_with_original") is False, "TTS 模式是替换，不是混音"


def test_mix_mode_asks_for_mixing(monkeypatch, tmp_path):
    """显式选混音时要传 `mix_with_original=True`。"""
    out, calls = _apply(monkeypatch, tmp_path, mode="mix", has_native=True)
    assert calls["mux"].get("mix_with_original") is True
    assert "out_mixed" in pathlib.Path(out).name


def test_native_mode_without_audio_warns_but_keeps_video(monkeypatch, caplog, tmp_path):
    """选了「视频模型原生」但提供者没有 → **警告但保留画面**。

    画面已经生成好了（云端可能已经花了钱），因为音频设置不自洽就丢弃
    整个产物，对用户是净损失。
    """
    out, calls = _apply(monkeypatch, tmp_path, mode="native", has_native=False)
    assert out == "in.mp4"
    assert "synthesized" not in calls


def test_tts_mode_without_dialogue_skips_instead_of_failing(monkeypatch, tmp_path):
    """选了 TTS 但剧本没台词 → 跳过，不报错。

    这不是配置错误（可能就是个无对白的镜头），报错会打断整条流水线。
    """
    out, calls = _apply(monkeypatch, tmp_path, mode="tts", has_native=False, dialogue="")
    assert "synthesized" not in calls
    assert out == "in.mp4"


def test_tts_failure_keeps_the_video(monkeypatch, tmp_path):
    """配音失败**不能让画面作废** —— 返回原视频并记 warning。"""
    monkeypatch.setattr(
        "app.services.runtime_settings.get",
        lambda k, default=None: {"video_audio": "tts"}.get(k, default),
    )
    monkeypatch.setattr(
        "app.services.providers.get",
        lambda cap, key: _FakeSpec(False) if cap == "video_gen" else None,
    )

    def boom(*a, **kw):
        raise RuntimeError("网络断了")

    monkeypatch.setattr("app.services.tts.synthesize", boom)
    svc = video_mod.VideoService()
    svc.mock = False
    out = svc._apply_audio_plan("seedance", "in.mp4", "台词", str(tmp_path), "sess")
    assert out == "in.mp4", "配音失败应原样返回画面"


# ------------------------------------------------------------------
# 路径与编码（两个静默错误）
# ------------------------------------------------------------------

def test_concat_list_uses_absolute_paths(tmp_path):
    """concat 清单里必须是**绝对路径**，且转义正确。

    ⚠️ 相对路径会让 ffmpeg 报 "Error opening input file ...:
    No such file or directory" —— concat demuxer 按 **ffmpeg 的工作目录**
    解析相对路径，而 Python 的 cwd 与调用处预期不一定一致。
    这个坑很隐蔽：**写清单时不报错**，要等 ffmpeg 跑起来才知道。
    """
    # 用中文 + 含单引号的目录名：这两样都真实出现过
    # （项目名默认「未命名工程」；而单引号会破坏 concat 的引号包裹）
    weird = tmp_path / "未命名工程" / "it's here"
    weird.mkdir(parents=True)
    a, b = weird / "s0.png", weird / "s1.png"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    listfile = weird / "list.txt"
    video_mod._write_concat(listfile, [a, b])
    content = listfile.read_text(encoding="utf-8")

    entries = [ln for ln in content.splitlines() if ln.startswith("file '")]
    assert entries, "清单里应当有 file 记录"
    for ln in entries:
        raw = ln[len("file '"):-1]
        # 反转义后应当是一个**绝对路径**
        unescaped = raw.replace("'\\''", "'").replace("\\\\", "\\")
        assert pathlib.Path(unescaped).is_absolute(), f"应是绝对路径：{raw}"

    # 末尾重复一次最后一帧，否则 ffmpeg 会截断最后一张的时长
    assert content.count("s1.png") == 2, "末帧要被重复一次"
    # 单引号必须被转义，否则 concat 的引号包裹会被提前结束
    assert "it'\\''s here" in content, "单引号没转义，清单会被解析错"


def test_subprocess_calls_specify_utf8_encoding(tmp_path):
    """所有 `text=True` 的 subprocess 调用都必须显式指定 UTF-8。

    ⚠️ 中文 Windows 上 `text=True` 默认按 **GBK** 解码，而 ffmpeg 输出
    UTF-8 → 抛 `UnicodeDecodeError`，连真正的错误信息都看不到。
    这个坑在 ffmpeg / pip 有非 ASCII 输出时必现，所以拿测试守住。
    """
    import ast

    root = pathlib.Path(__file__).resolve().parent.parent
    bad: list[str] = []
    for f in list(root.glob("app/**/*.py")) + list(root.glob("scripts/*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name != "run":
                continue
            kws = {k.arg for k in node.keywords}
            if "text" in kws and "encoding" not in kws:
                bad.append(f"{f.relative_to(root)}:{node.lineno}")

    assert not bad, (
        "这些 subprocess.run(text=True) 没指定 encoding，"
        "中文 Windows 上会按 GBK 解码而崩：\n  " + "\n  ".join(bad)
    )
