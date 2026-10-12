"""提供者凭据必须在设置页可填。

背景（本项目实际发生过的问题）：
    设置页原先只渲染以 `llm_` / `image_` / `video_` / `tts_` / `lora_` 开头的字段。
    但视频的密钥是**提供者级**的——`minimax_api_key`、`dashscope_api_key`、
    `ark_api_key`——完全不匹配那些前缀。

    后果：这些键在 `/api/settings` 的白名单里（**能存**），却**没有输入框**，
    用户根本没法在界面上配视频 API key。表现是"填不了"，
    而不是"填了不生效"——后者我们之前已经修过一次。

所以这里钉住两条：
  1. 每个需要密钥的视频提供者，都要能被设置页发现；
  2. 发现的键必须真的在写白名单里（否则又变成"能填不能存"）。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.registry import api as registry
from app.services.video_adapters import VIDEO_PROVIDERS, provider_credential_spec


def test_providers_api_exposes_credential_spec():
    """/api/providers 必须下发凭据清单（设置页据此渲染输入框）。"""
    c = TestClient(app)
    r = c.get("/api/providers", params={"capability": "video_gen"})
    assert r.status_code == 200
    body = r.json()
    assert "credential_spec" in body, "设置页拿不到凭据清单就只能硬编码（会漂移）"
    spec = body["credential_spec"]
    assert spec.get("video", {}).get("keys"), "视频凭据清单为空"


def test_every_video_provider_with_key_is_discoverable():
    """需要密钥的视频提供者，其凭据键必须出现在清单里。"""
    spec = provider_credential_spec()
    listed = {f["key"] for f in spec["video"]["keys"]}
    for p in VIDEO_PROVIDERS:
        if p.api_key_setting:
            assert p.api_key_setting in listed, (
                f"{p.key} 需要 {p.api_key_setting}，但设置页清单里没有它——用户填不了"
            )


def test_discoverable_credentials_are_writable():
    """清单里给出的键必须能真正写入（否则是"能填不能存"）。"""
    allowed = registry.allowed_settings()
    spec = provider_credential_spec()
    for f in spec["video"]["keys"] + spec["video"]["endpoints"]:
        assert f["key"] in allowed, f"{f['key']} 在设置页可填，但写白名单里没有——会被静默丢弃"


def test_provider_items_carry_credential_keys():
    """单个提供者条目也要带上它需要的凭据键（供按提供者定制 UI）。"""
    c = TestClient(app)
    items = c.get("/api/providers", params={"capability": "video_gen"}).json()["providers"]
    by_key = {p["key"]: p for p in items}
    assert by_key["minimax"]["credential_keys"] == ["minimax_api_key"]
    assert by_key["dashscope_video"]["credential_keys"] == ["dashscope_api_key"]
    assert by_key["seedance"]["credential_keys"] == ["ark_api_key"]
    # 本地与免费提供者不需要密钥
    assert by_key["local"]["credential_keys"] == []
    assert by_key["ffmpeg"]["credential_keys"] == []


def test_minimax_is_in_adapters_not_only_tiered_registry():
    """minimax 必须存在于适配器里。

    它早先只在 `providers.py` 且走 `video.py` 自己的直连实现，
    于是同一提供者有两份实现——适配器声明与真实发送会漂移。
    """
    keys = {p.key for p in VIDEO_PROVIDERS}
    assert "minimax" in keys, "minimax 没并入适配器，会出现两份实现"


def test_adapter_and_tiered_registry_agree_on_minimax():
    from app.services.providers import get

    spec = get("video_gen", "minimax")
    adapter = next(p for p in VIDEO_PROVIDERS if p.key == "minimax")
    assert spec is not None
    assert tuple(spec.conditioning) == adapter.conditioning, "两处条件声明漂移"
    assert spec.default_model == adapter.model, "两处默认模型漂移"
