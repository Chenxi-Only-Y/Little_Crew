"""云端使用提醒的回归测试（只提醒，不拦截）。

覆盖：本地/云端的判定、价格估算、订阅门槛与底模绑定的提示、
以及 `.env` 默认回退（避免"配置为空就误报全部免费"）。
"""

from __future__ import annotations

import pytest

from app.services.cloud_cost import (
    MODEL_COST,
    audit_settings,
    cost_of,
    estimate,
    is_cloud,
    reminder,
    resolve_effective,
)


# ------------------------------------------------------------------
# 本地 / 云端判定
# ------------------------------------------------------------------

@pytest.mark.parametrize("provider,expected", [
    ("local", False),
    ("ffmpeg", False),
    ("edge", False),
    ("ollama", False),
    ("vllm", False),
    ("(自动)", False),
    ("", False),
    ("dashscope", True),
    ("minimax", True),
    ("replicate", True),
    ("openai", True),
])
def test_is_cloud(provider, expected):
    assert is_cloud(provider) is expected


# ------------------------------------------------------------------
# 估算
# ------------------------------------------------------------------

def test_local_is_free():
    est = estimate("local", "z-image-turbo", "image_gen", 8)
    assert est.free and not est.unknown
    assert est.low_usd == 0.0


def test_per_second_video_cost():
    """60 秒 Hailuo ≈ $0.60（与调研数据一致）。"""
    est = estimate("minimax", "MiniMax-H3", "video_gen", 60)
    assert not est.free and not est.unknown
    assert est.unit_price == pytest.approx(0.01)
    assert est.low_usd == pytest.approx(0.60)


def test_expensive_video_flagged_with_subscription():
    r = reminder("replicate", "sora-2-pro", "video_gen", 60)
    assert r["level"] == "cloud_subscription"
    assert any("订阅门槛" in b for b in r["badges"])
    assert any("订阅门槛" in n for n in r["estimate"]["notes"])


def test_proxy_badge():
    r = reminder("replicate", "openai/gpt-image-1.5", "image_gen", 1)
    assert "需海外网络" in r["badges"]


def test_region_ok_badge():
    r = reminder("dashscope", "wan2.7-image-pro", "image_gen", 1)
    assert "中国可直连" in r["badges"]


def test_lora_binding_warning():
    """底模绑定的警告必须出现（否则用户会白花钱）。

    ⚠️ 这条原先传 `capability="lora_train"` —— 而「LoRA 训练」已按用户要求
    从本项目整体删除（那是本地部署模型自己的事），那个能力不再存在。

    但**这条警告本身仍要守**：`lora_bound` 是**模型属性**，图像模型上依然挂着
    它（如百炼的模型"训练产物绑定底模，只能在同平台推理"）。所以改成对
    **图像能力**断言 —— 测的是"警告逻辑对不对"，而不是"某个已删能力配得对不对"。
    """
    r = reminder("dashscope", "wan2.7-image-pro", "image_gen", 1)
    assert any("绑定底模" in n for n in r["estimate"]["notes"]), r


def test_unknown_price_keeps_metadata():
    """价格未知时仍要保留地域/可训练等元数据（曾因提前 return 丢失）。"""
    est = estimate("dashscope", "wan2.7-image-pro", "image_gen", 1)
    assert est.unknown
    assert any("中国大陆可直连" in n for n in est.notes)
    assert any("按官方刊例" in n for n in est.notes)


def test_cost_of_fuzzy_match():
    assert cost_of("sora-2-pro") is not None
    assert cost_of("MiniMax-H3") is not None
    assert cost_of("不存在的模型xyz") is None


def test_all_declared_models_have_label():
    for k, v in MODEL_COST.items():
        assert v.label, f"{k} 缺 label"
        assert v.capability, f"{k} 缺 capability"


# ------------------------------------------------------------------
# 配置审计（含默认回退）
# ------------------------------------------------------------------

def test_resolve_effective_falls_back_to_defaults():
    """用户没配置时，必须回退到 .env 默认值，而不是当作"没有"。"""
    eff = resolve_effective({}, {"image_provider": "minimax", "video_provider": "local"})
    assert eff["image_provider"] == "minimax"
    assert eff["video_provider"] == "local"


def test_resolve_effective_user_overrides_default():
    eff = resolve_effective({"image_provider": "local"}, {"image_provider": "minimax"})
    assert eff["image_provider"] == "local"
    assert "image_provider" in eff["_explicit"]


def test_audit_reports_cloud_when_only_defaults():
    """配置为空但默认是云端时，必须报"会走云端"（曾误报全部免费）。"""
    a = audit_settings({}, {"image_provider": "minimax", "video_provider": "minimax",
                            "llm_provider": "deepseek"})
    assert a["has_cloud"], "默认走云端却没报云端，属于误导性提醒"
    assert len(a["cloud_items"]) >= 3


def test_audit_all_local():
    a = audit_settings({}, {"image_provider": "local", "video_provider": "local",
                            "llm_provider": "ollama", "tts_provider": "edge"})
    assert not a["has_cloud"]
    assert "不产生 API 费用" in a["summary"]


def test_audit_marks_user_setting_vs_default():
    a = audit_settings({"image_provider": "local"},
                       {"image_provider": "minimax", "video_provider": "minimax"})
    by_key = {it["key"]: it for it in (*a["cloud_items"], *a["free_items"])}
    assert by_key["image_provider"]["from_user_setting"] is True
    assert by_key["video_provider"]["from_user_setting"] is False
