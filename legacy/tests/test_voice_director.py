"""配音指导节点的测试（用户要求"1、2 先做"里的 ①）。

改之前的状态：注册表**声明**了配音指导（能力 tts、产出 voice_track），
但 `workflow_node` 为空（不在执行图里），而且 `voice_track` / `bgm_track`
这两个字段在 `CrewState` 里**根本不存在** —— 界面上像流程的一步，实际无落点。

这个节点只做**对白**（TTS）。BGM 不做（`music` 能力在提供者表里是空的）。

测试重点：
  · 没有台词 → 跳过（不是失败），也**不去碰引擎**
  · 引擎不可用 → 记可读原因，**不抛异常**
  · 单句失败 → 不拖垮整轨
  · 字段名兼容（dialogue / line / text）
"""

from __future__ import annotations

import sys
import io

import pytest


def _patch_tts(monkeypatch, *, available=True, fail_on=()):
    """替换 tts 模块：可用性 + synthesize 行为都可控。"""
    from app.services import tts as tts_mod

    calls: list[tuple[str, str]] = []

    def fake_synth(text, out_path, *, voice="", speed=1.0, timeout=180.0):
        calls.append((text, str(out_path)))
        if text in fail_on:
            raise RuntimeError(f"合成失败：{text}")
        # ⚠️ 不能在 `class R:` 里直接引用函数参数 —— 类体**看不到**外层函数作用域
        # 的局部变量（我踩过：`NameError: name 'voice' is not defined`，
        # 而且被节点的 except 吞成 `status: failed`，只看到"失败"看不到原因）。
        # 先把值绑成**类属性**再用。
        _voice = voice or "zh-CN-XiaoxiaoNeural"
        _path = str(out_path)

        class R:
            audio_path = _path
            voice = _voice
            duration_s = 1.5

        return R()

    monkeypatch.setattr(tts_mod, "available", lambda: available)
    monkeypatch.setattr(tts_mod, "synthesize", fake_synth)
    if not available:
        monkeypatch.setattr(tts_mod, "_hint", lambda: "配音需要外部工具目录里的 edge-tts。修复：…")
    return calls


def test_skips_when_no_dialogue(monkeypatch):
    """分镜里没有台词 → 跳过，**不该去碰引擎**。

    否则会报一个"引擎不可用"来吓人，而其实根本不需要配音。
    """
    from app.services import tts as tts_mod
    from app.services.voice_director import synthesize_voice_track

    def boom():
        raise AssertionError("没有台词时不该检查引擎可用性")

    monkeypatch.setattr(tts_mod, "available", boom)

    # 引擎可用性都不该被调用 —— 上面那个 boom 会在被调用时抛异常
    out = synthesize_voice_track({"storyboard": [{"shot_id": "s1", "description": "走路"}]})
    assert out["voice_track"] == []
    assert "没有台词" in out.get("voice_track_note", "")


def test_skips_when_no_storyboard():
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({})
    assert out["voice_track"] == []
    assert out["bgm_track"] == []


def test_engine_unavailable_reports_reason_not_raise(monkeypatch):
    """引擎不可用 → 记录**可读原因**，不抛异常（配音不该拖垮流程）。"""
    _patch_tts(monkeypatch, available=False)
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({
        "storyboard": [{"shot_id": "s1", "dialogue": "你好"}]})
    items = out["voice_track"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
    assert "edge-tts" in items[0]["reason"] or "引擎" in items[0]["reason"]


def test_synthesizes_each_shot(monkeypatch):
    calls = _patch_tts(monkeypatch)
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({
        "session_id": "sess1",
        "storyboard": [
            {"shot_id": "s1", "dialogue": "第一句"},
            {"shot_id": "s2", "dialogue": "第二句"},
            {"shot_id": "s3", "description": "无声镜头"},
        ],
    })
    items = out["voice_track"]
    assert len(items) == 2, "只该合成有台词的两句"
    # 把失败原因带进断言消息 —— 否则"失败了但不知道为啥"，白查一轮
    assert all(i["status"] == "ok" for i in items), [
        (i["shot_id"], i["status"], i["reason"]) for i in items]
    assert [i["shot_id"] for i in items] == ["s1", "s2"]
    assert len(calls) == 2
    # 每句写到**不同**文件（否则互相覆盖）
    assert calls[0][1] != calls[1][1]
    assert calls[0][1].endswith(".mp3")


def test_one_failure_does_not_kill_the_track(monkeypatch):
    _patch_tts(monkeypatch, fail_on={"第二句"})
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({
        "storyboard": [
            {"shot_id": "s1", "dialogue": "第一句"},
            {"shot_id": "s2", "dialogue": "第二句"},
            {"shot_id": "s3", "dialogue": "第三句"},
        ],
    })
    items = out["voice_track"]
    assert len(items) == 3, "一句失败不该让其余句子消失"
    by_id = {i["shot_id"]: i for i in items}
    assert by_id["s1"]["status"] == "ok"
    assert by_id["s2"]["status"] == "failed"
    assert "第二句" in by_id["s2"]["reason"]
    assert by_id["s3"]["status"] == "ok"


@pytest.mark.parametrize("field,value", [
    ("dialogue", "台词 A"),
    ("line", "台词 B"),
    ("text", "台词 C"),
    ("lines", ["台词 D1", "台词 D2"]),
])
def test_dialogue_field_variants(monkeypatch, field, value):
    """不同来源的台词字段名都认（`dialogue` / `line` / `lines` / `text`）。"""
    _patch_tts(monkeypatch)
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({"storyboard": [{"shot_id": "s1", field: value}]})
    items = out["voice_track"]
    assert len(items) == 1, f"{field} 没被识别成台词"
    assert items[0]["status"] == "ok"


def test_bgm_is_empty_and_documented(monkeypatch):
    """BGM 留空 —— `music` 能力是已知空缺，不能假装做了。"""
    _patch_tts(monkeypatch)
    from app.services.voice_director import synthesize_voice_track

    out = synthesize_voice_track({"storyboard": [{"shot_id": "s1", "dialogue": "x"}]})
    assert out["bgm_track"] == [], "BGM 没有实现，不该凭空产出"


def test_state_has_voice_and_bgm_fields():
    """`CrewState` 必须有这两个字段 —— 原先**没有**，这是"声明了但没落点"的典型。"""
    from app.core.state import CrewState

    assert "voice_track" in CrewState.__annotations__
    assert "bgm_track" in CrewState.__annotations__
