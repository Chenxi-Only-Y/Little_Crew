"""视频提供者适配器的回归测试。

为什么要专门测"请求体形状"：
    适配器的职责就是把统一请求翻成各家形状，而**形状错了不会抛异常**——
    提交会成功、任务会 FAILED，或者更糟：参数被静默忽略、出来一个不对的视频。
    这类问题在真实调用时才暴露，所以用测试把形状钉住。

对齐的官方依据：
  - MiniMax v2：`content` 数组 + `role`（first_frame/last_frame/reference_*）
  - DashScope：`{model, input:{prompt, media:[{type,url}]}, parameters:{...}}`，
    且必须带 `X-DashScope-Async: enable`
  - Vidu（同端点上）：media 必须**正好 2 张 image**，顺序即首帧/尾帧
  - 万相：`reference_*` 与 `first_frame`/`last_frame` **互斥**
"""

from __future__ import annotations

import pytest

from app.services.video_adapters import (
    VIDEO_PROVIDERS,
    VideoAdapterError,
    VideoRequest,
    get_video_provider,
    validate_exclusive,
)


def _req(**kw) -> VideoRequest:
    base = dict(prompt="测试", duration=5, resolution="720P")
    base.update(kw)
    return VideoRequest(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# 互斥校验
# ------------------------------------------------------------------

def test_first_and_last_frame_together_is_ok():
    validate_exclusive(_req(first_frame="a.png", last_frame="b.png"))


def test_reference_video_alone_is_ok():
    validate_exclusive(_req(reference_videos=["ref.mp4"]))


def test_mixing_frames_and_references_is_rejected():
    """首尾帧与参考素材互斥——必须在提交前挡住，别等任务 FAILED。"""
    with pytest.raises(VideoAdapterError) as ei:
        validate_exclusive(_req(first_frame="a.png", reference_videos=["ref.mp4"]))
    msg = str(ei.value)
    assert "互斥" in msg and "二选一" in msg


def test_last_frame_without_first_is_rejected():
    with pytest.raises(VideoAdapterError) as ei:
        validate_exclusive(_req(last_frame="b.png"))
    assert "首帧" in str(ei.value)


# ------------------------------------------------------------------
# MiniMax 形状
# ------------------------------------------------------------------

def test_minimax_body_uses_content_array_with_roles():
    p = get_video_provider("dashscope_video")
    assert p is not None
    mm = p  # 占位，下面用 minimax 专属测试
    del mm

    from app.services.video_adapters import _build_minimax_body

    body = _build_minimax_body("MiniMax-H3", _req(
        first_frame="https://x/a.png",
        last_frame="https://x/b.png",
    ), {})
    assert body["model"] == "MiniMax-H3"
    assert body["duration"] == 5
    assert body["ratio"] == "adaptive"
    roles = [c.get("role") for c in body["content"]]
    assert "first_frame" in roles and "last_frame" in roles
    types = [c["type"] for c in body["content"]]
    assert types[0] == "text"


def test_minimax_reference_video_uses_video_url():
    from app.services.video_adapters import _build_minimax_body

    body = _build_minimax_body("MiniMax-H3", _req(reference_videos=["mm_file://123"]), {})
    refs = [c for c in body["content"] if c.get("role") == "reference_video"]
    assert refs and refs[0]["type"] == "video_url"
    assert refs[0]["video_url"]["url"] == "mm_file://123"


# ------------------------------------------------------------------
# DashScope 形状
# ------------------------------------------------------------------

def test_dashscope_wan_body_uses_media_types():
    from app.services.video_adapters import _build_dashscope_body

    body = _build_dashscope_body("wan3.0-video", _req(
        first_frame="https://x/a.png", last_frame="https://x/b.png",
    ), {})
    assert body["model"] == "wan3.0-video"
    assert "input" in body and "parameters" in body
    media = body["input"]["media"]
    assert [m["type"] for m in media] == ["first_frame", "last_frame"]
    assert body["parameters"]["duration"] == 5
    assert body["parameters"]["resolution"] == "720P"


def test_dashscope_wan_reference_video():
    from app.services.video_adapters import _build_dashscope_body

    body = _build_dashscope_body("wan3.0-video", _req(reference_videos=["mm_file://9"]), {})
    assert body["input"]["media"][0]["type"] == "reference_video"


def test_vidu_media_must_be_exactly_two_images():
    """Vidu 的 media 必须正好 2 张 image，顺序即首帧/尾帧。"""
    from app.services.video_adapters import _build_dashscope_body

    body = _build_dashscope_body(
        "vidu/viduq3-turbo_start-end2video",
        _req(first_frame="https://x/a.png", last_frame="https://x/b.png"),
        {},
    )
    media = body["input"]["media"]
    assert len(media) == 2
    assert all(m["type"] == "image" for m in media)
    assert media[0]["url"] == "https://x/a.png"
    assert media[1]["url"] == "https://x/b.png"


def test_vidu_without_first_frame_is_rejected():
    from app.services.video_adapters import _build_dashscope_body

    with pytest.raises(VideoAdapterError):
        _build_dashscope_body("vidu/viduq3-turbo_start-end2video", _req(), {})


def test_vidu_rejects_reference_material():
    """Vidu 只收首尾帧；给参考视频应明确报错并指出改用哪家。"""
    from app.services.video_adapters import _build_dashscope_body

    with pytest.raises(VideoAdapterError) as ei:
        _build_dashscope_body(
            "vidu/viduq3-turbo_start-end2video",
            _req(first_frame="a.png", reference_videos=["r.mp4"]),
            {},
        )
    # 报错里要给出可执行的替代方案，而不是只说"不支持"
    assert "wan3.0-video" in str(ei.value)


# ------------------------------------------------------------------
# Seedance 形状
# ------------------------------------------------------------------

def test_seedance_body_and_lowercase_resolution():
    from app.services.video_adapters import _build_seedance_body

    body = _build_seedance_body("doubao-seedance-1-0-pro", _req(
        first_frame="https://x/a.png", resolution="720P",
    ), {})
    assert body["resolution"] == "720p", "方舟用小写分辨率"
    assert body["content"][0]["type"] == "text"
    roles = [c.get("role") for c in body["content"]]
    assert "first_frame" in roles


# ------------------------------------------------------------------
# 注册表一致性
# ------------------------------------------------------------------

def test_all_adapters_have_endpoints_and_models():
    for p in VIDEO_PROVIDERS:
        assert p.submit_path, f"{p.key} 没有提交路径"
        assert p.query_path, f"{p.key} 没有查询路径"
        assert p.model, f"{p.key} 没有默认模型"
        assert p.known_models, f"{p.key} 没有可选模型"
        assert p.conditioning, f"{p.key} 没有条件声明"
        lo, hi = p.durations
        assert lo <= hi


def test_unverified_providers_are_marked():
    """没跑通过的提供者必须标出来——否则会被误认为已验证。"""
    for p in VIDEO_PROVIDERS:
        if not p.verified:
            assert "跑通" in p.note, f"{p.key} 未验证但 note 里没说明"


def test_tiered_registry_includes_adapters():
    """`providers.PROVIDERS` 应已并入适配器登记的提供者（避免两份清单漂移）。"""
    from app.services.providers import PROVIDERS

    keys = {p.key for p in PROVIDERS if p.capability == "video_gen"}
    for p in VIDEO_PROVIDERS:
        assert p.key in keys, f"{p.key} 在适配器里登记了，但注册表里没有"

    # 并且能力声明要与适配器一致
    from app.services.providers import get

    for p in VIDEO_PROVIDERS:
        spec = get("video_gen", p.key)
        assert spec is not None
        assert tuple(spec.conditioning) == p.conditioning, f"{p.key} 条件声明漂移"


def test_reference_video_is_structural():
    """参考视频必须算作"结构控制"——否则会误判为"白模只能给首尾帧"。"""
    from app.services.providers import STRUCTURAL_CONDITIONING

    assert "reference_video" in STRUCTURAL_CONDITIONING
    assert "reference_image" in STRUCTURAL_CONDITIONING


def test_provider_api_keys_are_writable():
    """提供者的 API Key 与端点必须可写入运行时设置。

    `PUT /api/settings` 的白名单是从注册表派生的。若某个提供者的 key 不在里面，
    界面能填、保存却被**静默丢弃**，表现为"明明填了 key 却报未配置"——
    本项目已经踩过同类坑（tts_provider / lora_train_provider 曾不在白名单）。
    """
    from app.registry import api as registry
    from app.core.config import settings

    allowed = registry.allowed_settings()
    missing = []
    for p in VIDEO_PROVIDERS:
        if p.api_key_setting not in allowed:
            missing.append(p.api_key_setting)
    assert not missing, f"这些提供者的 Key 设置项不在白名单里：{missing}"

    # 端点覆盖项也要能写（否则用户改不了 base_url）
    from app.services.video_adapters import get_video_provider

    for key in ("dashscope_video", "seedance"):
        p = get_video_provider(key)
        assert p is not None
        field = f"{key}_base_url"
        if hasattr(settings, field):
            assert field in allowed, f"{field} 不在白名单，用户改不了端点"
