"""视频时长/分辨率预设的回归测试。

需求（用户 2026-10-02）：每段时长默认 7 秒，提供 5/7/9/12/15 供选择。
硬约束：**本地 8GB 显存超过 10s 必 OOM**（降分辨率无效），故本地只能到 9s。
"""

from __future__ import annotations

import pytest

from app.services import providers as prov


# ------------------------------------------------------------------
# 预设定义
# ------------------------------------------------------------------

def test_duration_preset_definition():
    """云端提供 5/7/9/12/15，默认 7。"""
    assert prov.DURATION_PRESETS == (5, 7, 9, 12, 15)
    assert prov.DEFAULT_DURATION == 7


def test_local_durations_exclude_unsafe():
    """本地不得出现 12/15（8GB 超过 10s 必 OOM）。"""
    local = prov.allowed_durations("local")
    assert 12 not in local and 15 not in local
    assert max(local) <= 10, f"本地档位含超过 10s 的值：{local}"
    assert local == (5, 7, 9)


def test_cloud_durations_full():
    assert prov.allowed_durations("minimax") == (5, 7, 9, 12, 15)


def test_resolution_presets_no_1080p_for_known_providers():
    """1080P 实际不被支持，两个提供者的档位里都不应出现。"""
    assert "1080P" not in prov.allowed_resolutions("local")
    assert "1080P" not in prov.allowed_resolutions("minimax")
    assert prov.allowed_resolutions("minimax") == ("768P", "2K")
    assert prov.allowed_resolutions("local") == ("768P",)


def test_video_providers_declare_presets():
    """视频 provider 必须在 spec 里声明预设（供 UI 渲染）。"""
    for key in ("local", "minimax", "ffmpeg"):
        spec = prov.get("video_gen", key)
        assert spec is not None, f"缺少 video_gen provider：{key}"
        assert spec.presets.get("duration"), f"{key} 未声明时长预设"
        assert spec.presets.get("resolution"), f"{key} 未声明分辨率预设"


# ------------------------------------------------------------------
# resolve_duration
# ------------------------------------------------------------------

def test_resolve_default_is_seven():
    assert prov.resolve_duration("minimax", None) == (7, None)
    assert prov.resolve_duration("minimax", "") == (7, None)
    assert prov.resolve_duration("minimax", 0) == (7, None)


def test_resolve_keeps_valid_values():
    for v in (5, 7, 9, 12, 15):
        d, note = prov.resolve_duration("minimax", v)
        assert d == v and note is None


def test_resolve_clamps_local_overflow():
    """本地请求 12/15 必须被收敛到 9，并给出原因。"""
    d, note = prov.resolve_duration("local", 12)
    assert d == 9, f"本地 12s 未被收敛：{d}"
    assert note and "上限" in note and "OOM" in note

    d2, note2 = prov.resolve_duration("local", 15)
    assert d2 == 9 and note2


def test_resolve_rounds_down_between_presets():
    """落在档位之间时向下取（宁短勿 OOM）。"""
    d, note = prov.resolve_duration("minimax", 10)
    assert d == 9, f"10 应向下取 9，实际 {d}"
    assert note and "向下取" in note


def test_resolve_raises_below_minimum():
    d, note = prov.resolve_duration("minimax", 2)
    assert d == 5 and note


def test_resolve_handles_garbage():
    d, note = prov.resolve_duration("minimax", "abc")
    assert d == prov.DEFAULT_DURATION and note


def test_resolve_unknown_provider_uses_cloud_presets():
    d, _ = prov.resolve_duration("某个没登记的provider", 12)
    assert d == 12


# ------------------------------------------------------------------
# resolve_resolution
# ------------------------------------------------------------------

def test_resolve_resolution_keeps_valid():
    assert prov.resolve_resolution("minimax", "2K") == ("2K", None)
    assert prov.resolve_resolution("local", "768P") == ("768P", None)


def test_resolve_resolution_rejects_1080p():
    r, note = prov.resolve_resolution("minimax", "1080P")
    assert r == "768P", f"1080P 未被拒绝：{r}"
    assert note and "1080P" in note and "768P" in note


def test_resolve_resolution_local_rejects_2k():
    r, note = prov.resolve_resolution("local", "2K")
    assert r == "768P" and note


def test_resolve_resolution_default():
    assert prov.resolve_resolution("minimax", None) == ("768P", None)


# ------------------------------------------------------------------
# 载荷
# ------------------------------------------------------------------

def test_video_parameter_options_payload():
    opts = prov.video_parameter_options()
    assert opts["durations"] == [5, 7, 9, 12, 15]
    assert opts["default_duration"] == 7
    assert "1080P" not in opts["resolutions"]
    assert opts["by_provider"]["local"]["durations"] == [5, 7, 9]
    assert opts["by_provider"]["minimax"]["durations"] == [5, 7, 9, 12, 15]


def test_to_payload_includes_presets():
    payload = {p["key"]: p for p in prov.to_payload() if p["capability"] == "video_gen"}
    assert payload["local"]["presets"]["duration"] == [5, 7, 9]
    assert payload["minimax"]["presets"]["duration"] == [5, 7, 9, 12, 15]
    assert payload["minimax"]["presets"]["resolution"] == ["768P", "2K"]


# ------------------------------------------------------------------
# 配置默认值
# ------------------------------------------------------------------

def test_config_default_duration_is_seven(monkeypatch):
    """代码里的默认值应为 7。

    注意：`.env` 可以覆盖它（实测环境里就是 15），因此这里清掉环境变量后再构造 Settings，
    否则测的是本机 `.env` 而不是代码默认值。
    """
    monkeypatch.delenv("VIDEO_MAX_DURATION", raising=False)
    from app.core.config import Settings

    fresh = Settings(_env_file=None)  # 不读 .env，只看代码默认
    assert fresh.video_max_duration == 7, (
        f"代码默认时长应为 7，实际 {fresh.video_max_duration}"
    )


def test_registry_config_field_offers_presets():
    """注册表里该字段应是 select 并列出档位（供导演台渲染）。"""
    from app.registry import api as registry

    node = registry.node("editor")
    assert node is not None
    field = next((c for c in node.config if c.key == "video_max_duration"), None)
    assert field is not None, "editor 节点缺少 video_max_duration 配置"
    assert field.type == "select"
    assert [str(o) for o in field.options] == ["5", "7", "9", "12", "15"]
    assert str(field.default) == "7"
