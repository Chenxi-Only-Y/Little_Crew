"""云/本地模型成本提示 —— **只提醒，不拦截**。

设计定位（用户 2026-10-02 确认）：
    不做预算拦截、不做熔断、不改 `quota.py`；只把"这次用了云端、大概多少钱、
    有什么门槛"清楚地告诉用户，由用户自己决定。

三件事：
  1. `MODEL_COST`：模型 → 价格/地域/门槛 的元数据（数据源见 `docs/recon/06-cloud-models.md`）
  2. `estimate(...)`：给一次操作算出粗略花费区间
  3. `reminder(...)`：生成人话提醒（供设置页与导演台直接展示）

⚠️ 价格为 2026 年公开标价，**仅作方向性参考**；接入前须以厂商页面为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ------------------------------------------------------------------
# 类型
# ------------------------------------------------------------------

#: 计费方式
BillingUnit = Literal["per_image", "per_second", "per_step", "free", "unknown", "subscription"]

Capability = Literal["llm", "image_gen", "image_edit", "video_gen", "lora_train", "tts", "music"]


@dataclass(frozen=True)
class ModelCost:
    """一个模型的成本/可用性元数据。

    Attributes:
        key:        模型标识（与注册表/设置页使用的 key 一致）
        label:      中文展示名
        capability: 能力类别
        unit:       计费单位
        unit_price: 单价（美元）；free/unknown/subscription 时为 None
        price_hint: 人话价格描述（优先展示这个）
        region_ok:  中国大陆是否可直连
        needs_proxy:是否需要海外网络
        subscription_floor: 订阅门槛（美元/月）；0 表示按量付费无门槛
        trainable:  是否支持在该模型上训练 LoRA
        lora_bound: 训练出的 LoRA 是否绑定该平台（只能在同平台推理）
        note:       补充说明
    """

    key: str
    label: str
    capability: Capability
    unit: BillingUnit
    unit_price: float | None = None
    price_hint: str = ""
    region_ok: bool = False
    needs_proxy: bool = False
    subscription_floor: float = 0.0
    trainable: bool = False
    lora_bound: bool = False
    note: str = ""


@dataclass(frozen=True)
class CostEstimate:
    """一次操作的粗略花费估算。"""

    provider: str
    model: str
    units: float
    unit_price: float | None
    low_usd: float | None
    high_usd: float | None
    free: bool = False
    unknown: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider, "model": self.model, "units": self.units,
            "unit_price": self.unit_price,
            "low_usd": self.low_usd, "high_usd": self.high_usd,
            "free": self.free, "unknown": self.unknown, "notes": self.notes,
        }


# ------------------------------------------------------------------
# 本地 / 免费
# ------------------------------------------------------------------

_LOCAL = ModelCost(
    key="local", label="本地推理 local-gen", capability="image_gen", unit="free",
    price_hint="零成本（用你自己的 GPU 与电费）",
    region_ok=True, needs_proxy=False,
    note="本机 8GB 显存：出图约 32 秒/张；视频 8 步约 8.6 分钟、时长上限 10 秒",
)


# ------------------------------------------------------------------
# 模型成本表
# ------------------------------------------------------------------

MODEL_COST: dict[str, ModelCost] = {
    # ============ 文生图 ============
    "z-image-turbo": ModelCost(
        "z-image-turbo", "Z-Image-Turbo", "image_gen", "per_image", None,
        "本地免费；走百炼按官方价",
        region_ok=True, note="本机默认出图模型（8 步 / cfg=1.0）",
    ),
    "wan2.7-image-pro": ModelCost(
        "wan2.7-image-pro", "万相 2.7 Pro（百炼）", "image_gen", "per_image", None,
        "按官方刊例计费；支持在其上训练 LoRA",
        region_ok=True, trainable=True, lora_bound=True,
        note="仅在华北2(北京)可用，须用该地域 API Key",
    ),
    "qwen-image-2.0-pro": ModelCost(
        "qwen-image-2.0-pro", "千问图像 2.0 Pro（百炼）", "image_gen", "per_image", None,
        "按官方刊例计费；支持在其上训练 LoRA",
        region_ok=True, trainable=True, lora_bound=True,
        note="仅在华北2(北京)可用",
    ),
    "image-01": ModelCost(
        "image-01", "MiniMax image-01", "image_gen", "per_image", None,
        "按 MiniMax 刊例计费",
        region_ok=True,
    ),
    "openai/gpt-image-1.5": ModelCost(
        "openai/gpt-image-1.5", "GPT Image 1.5", "image_gen", "per_image", 0.040,
        "$0.040/张（1024²）；HD $0.080",
        needs_proxy=True, note="综合质量最强；需海外网络",
    ),
    "black-forest-labs/flux-2-pro": ModelCost(
        "black-forest-labs/flux-2-pro", "Flux 2 Pro", "image_gen", "per_image", 0.055,
        "$0.045–0.055/张",
        needs_proxy=True, note="专业级写实",
    ),
    "black-forest-labs/flux-2-dev": ModelCost(
        "black-forest-labs/flux-2-dev", "Flux 2 Dev", "image_gen", "per_image", 0.025,
        "$0.025/张（开源权重可自托管免费）",
        needs_proxy=True,
    ),
    "tencent/hunyuan-image-3.0": ModelCost(
        "tencent/hunyuan-image-3.0", "混元图像 3.0", "image_gen", "per_image", 0.030,
        "$0.030/张",
        needs_proxy=True, note="中文/亚洲语境最优，性价比高",
    ),
    "bytedance/seedream-4.5": ModelCost(
        "bytedance/seedream-4.5", "Seedream 4.5", "image_gen", "per_image", 0.035,
        "$0.035/张",
        needs_proxy=True, note="创意/艺术感强",
    ),
    "ideogram/ideogram-2.0": ModelCost(
        "ideogram/ideogram-2.0", "Ideogram 2.0", "image_gen", "per_image", 0.040,
        "$0.040/张",
        needs_proxy=True, note="文字渲染最强（招牌/海报）",
    ),
    "openai/dall-e-3": ModelCost(
        "openai/dall-e-3", "DALL·E 3", "image_gen", "per_image", 0.040,
        "$0.040/张；HD $0.080",
        needs_proxy=True,
    ),
    "google/gemini-3-pro-image": ModelCost(
        "google/gemini-3-pro-image", "Gemini 3 Pro Image", "image_gen", "per_image", 0.035,
        "$0.035/张；HD $0.070",
        needs_proxy=True,
    ),
    # ============ 图像编辑 / 崩坏修复 ============
    "qwen-image-edit-max": ModelCost(
        "qwen-image-edit-max", "千问图像编辑 Max（百炼）", "image_edit", "per_image", None,
        "按官方刊例计费；支持多图输入",
        region_ok=True, note="角色一致性更强；用于修复手/脸/闪烁",
    ),
    "qwen-image-edit-plus": ModelCost(
        "qwen-image-edit-plus", "千问图像编辑 Plus（百炼）", "image_edit", "per_image", None,
        "按官方刊例计费",
        region_ok=True,
    ),
    "openai/gpt-image-2/edit": ModelCost(
        "openai/gpt-image-2/edit", "GPT Image 2 Edit", "image_edit", "per_image", None,
        "按 OpenAI 刊例",
        needs_proxy=True,
    ),
    "z-image-inpaint": ModelCost(
        "z-image-inpaint", "本地 inpaint（diffusers）", "image_edit", "free",
        price_hint="零成本（需在 local-gen 增加 inpaint 路由）",
        region_ok=True, note="ZImageInpaintPipeline 与现有权重共用，无需再下模型",
    ),
    # ============ 图生视频（价格差 10 倍，务必提醒）============
    "MiniMax-H3": ModelCost(
        "MiniMax-H3", "MiniMax H3（Hailuo）", "video_gen", "per_second", 0.01,
        "约 $0.01/秒（最便宜档）；1 分钟成片约 $0.60",
        region_ok=True, note="渲染 <2 分钟/段；你已在用",
    ),
    "minimax-h3": ModelCost(
        "minimax-h3", "本地 MiniMax H3", "video_gen", "free",
        price_hint="零成本；8GB 实测 8 步约 8.6 分钟/段，时长上限 10 秒",
        region_ok=True, note="超过 10 秒必 OOM，降分辨率无效",
    ),
    "kling-3.0": ModelCost(
        "kling-3.0", "Kling 3.0", "video_gen", "per_second", 0.03,
        "约 $0.03/秒（批量可至 ~$0.015）",
        needs_proxy=True, note="多镜头 + 原生音频",
    ),
    "veo-3.1": ModelCost(
        "veo-3.1", "Veo 3.1", "video_gen", "per_second", 0.06,
        "约 $0.06/秒（$0.50/8秒）；**每条自带同步音频**",
        needs_proxy=True, subscription_floor=19.99,
        note="订阅门槛：Gemini Advanced $19.99/月",
    ),
    "sora-2-pro": ModelCost(
        "sora-2-pro", "Sora 2 Pro", "video_gen", "per_second", 0.08,
        "约 $0.06–0.10/秒（$0.30–0.50/5秒）",
        needs_proxy=True, subscription_floor=200.0,
        note="订阅门槛：ChatGPT Pro $200/月；渲染 15–30 分钟/条",
    ),
    "seedance": ModelCost(
        "seedance", "Seedance（字节）", "video_gen", "per_second", 0.015,
        "约 $0.01–0.02/秒",
        needs_proxy=True, note="性价比最好",
    ),
    "ffmpeg": ModelCost(
        "ffmpeg", "FFmpeg 本地合成", "video_gen", "free",
        price_hint="零成本（图片幻灯片拼接）",
        region_ok=True, note="时长固定、无真实运镜；转场/字幕需自己实现",
    ),
    # ============ LoRA 训练 ============
    "diffsynth-qlora": ModelCost(
        "diffsynth-qlora", "本地 NF4 QLoRA（DiffSynth）", "lora_train", "free",
        price_hint="零成本（本机 GPU）；NF4 + peft 已就绪",
        region_ok=True, trainable=True, lora_bound=True,
        note="只能训 Z-Image 底模；8GB 显存较慢",
    ),
    "flux-lora": ModelCost(
        "flux-lora", "Flux LoRA 训练（Replicate/fal）", "lora_train", "per_step", None,
        "按训练计费；仅支持 Flux/SDXL 底模",
        needs_proxy=True, trainable=True, lora_bound=True,
        note="⚠️ 训出的 LoRA 不能给 Z-Image 用（底模绑定）",
    ),
    "sdxl-lora": ModelCost(
        "sdxl-lora", "SDXL LoRA 训练", "lora_train", "per_step", None,
        "按训练计费；仅支持 SDXL 底模",
        needs_proxy=True, trainable=True, lora_bound=True,
        note="⚠️ 底模绑定",
    ),
    # ============ 配音 / 音乐 ============
    "edge-tts": ModelCost(
        "edge-tts", "Edge TTS", "tts", "free",
        price_hint="零成本（免费兜底）",
        region_ok=True, note="音色较少；适合先跑通链路",
    ),
    "cosyvoice": ModelCost(
        "cosyvoice", "CosyVoice（百炼）", "tts", "unknown",
        price_hint="按官方刊例计费",
        region_ok=True, note="中文效果强",
    ),
    "minimax-speech": ModelCost(
        "minimax-speech", "MiniMax 语音", "tts", "unknown",
        price_hint="按 MiniMax 刊例计费",
        region_ok=True, note="可与现有 MiniMax Key 共用",
    ),
    "elevenlabs": ModelCost(
        "elevenlabs", "ElevenLabs", "tts", "unknown",
        price_hint="按字符/订阅计费",
        needs_proxy=True, note="情感与音色最丰富",
    ),
}


#: provider → 是否属于"云端计费"（用于提醒措辞）
CLOUD_PROVIDERS = {
    "dashscope", "minimax", "replicate", "wavespeed", "fal", "mobbi", "openai",
    "gemini", "elevenlabs", "deepseek", "zhipu", "qwen", "moonshot", "claude",
    "doubao", "ernie", "custom",
}
LOCAL_PROVIDERS = {"local", "ffmpeg", "edge", "ollama", "vllm", "mock", "diffsynth-qlora"}


def is_cloud(provider: str) -> bool:
    """该 provider 是否会产生云端费用。"""
    p = (provider or "").strip().lower()
    if not p or p == "(自动)":
        return False
    if p in LOCAL_PROVIDERS:
        return False
    return p in CLOUD_PROVIDERS or p not in LOCAL_PROVIDERS


def cost_of(model_key: str) -> ModelCost | None:
    """按模型 key 取成本元数据（支持模糊匹配供应商前缀）。"""
    if not model_key:
        return None
    if model_key in MODEL_COST:
        return MODEL_COST[model_key]
    low = model_key.lower()
    for k, v in MODEL_COST.items():
        if k.lower() == low or low.endswith(k.lower()) or k.lower().endswith(low):
            return v
    return None


# ------------------------------------------------------------------
# 估算
# ------------------------------------------------------------------

def estimate(provider: str, model: str, capability: Capability, units: float = 1.0) -> CostEstimate:
    """估算一次操作的粗略花费。

    Args:
        provider:   提供者标识（local / dashscope / minimax / replicate …）
        model:      模型名（可为空，此时按 provider 给通用提示）
        capability: 能力类别
        units:      数量（图片张数 / 视频秒数 / 训练步数）
    """
    notes: list[str] = []
    p = (provider or "").strip().lower()

    if not is_cloud(p):
        return CostEstimate(provider=p or "local", model=model, units=units,
                            unit_price=0.0, low_usd=0.0, high_usd=0.0, free=True,
                            notes=["本地/免费通道，不产生 API 费用"])

    # 云端：查模型元数据
    mc = cost_of(model) or cost_of(p)
    if mc is None or mc.unit_price is None:
        # 价格未知，但若该模型有元数据（地域/可训练/价格提示），仍要保留给用户看
        notes = []
        if mc is not None:
            if mc.price_hint:
                notes.append(mc.price_hint)
            if mc.needs_proxy:
                notes.append("需海外网络")
            elif mc.region_ok:
                notes.append("中国大陆可直连")
            if mc.lora_bound:
                notes.append("⚠️ 训练产物绑定底模：只能在同一平台/同底模上推理")
            if mc.note:
                notes.append(mc.note)
        if not notes:
            notes.append(
                f"走云端「{p}」，具体费用取决于所选模型与厂商刊例，请到厂商控制台确认"
            )
        return CostEstimate(
            provider=p, model=model or (mc.key if mc else ""), units=units,
            unit_price=None, low_usd=None, high_usd=None, unknown=True,
            notes=notes,
        )

    note = mc.price_hint or f"约 ${mc.unit_price}/单位"
    if mc.needs_proxy:
        notes.append("需海外网络")
    if not mc.region_ok and not mc.needs_proxy:
        notes.append("中国大陆可直连")
    if mc.subscription_floor:
        notes.append(f"⚠️ 该模型有订阅门槛：约 ${mc.subscription_floor:.2f}/月"
                     f"（低用量时按量付费通常更划算）")
    if mc.lora_bound:
        notes.append("⚠️ 训练产物绑定底模：只能在同一平台/同底模上推理")
    if mc.note:
        notes.append(mc.note)

    low = high = mc.unit_price * units
    return CostEstimate(provider=p, model=model or mc.key, units=units,
                        unit_price=mc.unit_price, low_usd=low, high_usd=high,
                        notes=[note, *notes])


def reminder(provider: str, model: str, capability: Capability, units: float = 1.0) -> dict:
    """生成"提醒"字典（供前端直接展示；不阻断任何操作）。

    Returns:
        {
          "level": "free" | "cloud" | "cloud_subscription" | "unknown",
          "is_cloud": bool,
          "title": str,
          "detail": str,
          "estimate": {...},
          "badges": [str],   # 供 UI 渲染的小标签
        }
    """
    est = estimate(provider, model, capability, units)
    badges: list[str] = []

    if est.free:
        level = "free"
        title = "本地 / 免费"
        detail = "不会产生 API 费用。" + ("；".join(est.notes) if est.notes else "")
    elif est.unknown:
        level = "unknown"
        title = "将调用云端 API（费用未知）"
        detail = "这次操作会走云端，会产生费用；具体金额请以厂商控制台为准。"
    else:
        level = "cloud_subscription" if any("订阅门槛" in n for n in est.notes) else "cloud"
        title = "将调用云端 API（会产生费用）"
        cost = (f"约 ${est.low_usd:.2f}"
                if est.low_usd is not None and est.low_usd < 100 else "金额待厂商确认")
        detail = f"预估 {cost}（{est.units:g} 单位 × ${est.unit_price}）。"

    mc = cost_of(model)
    if mc:
        if mc.region_ok:
            badges.append("中国可直连")
        if mc.needs_proxy:
            badges.append("需海外网络")
        if mc.subscription_floor:
            badges.append(f"订阅门槛 ${mc.subscription_floor:.0f}/月")
        if mc.trainable:
            badges.append("可训练 LoRA")
        if mc.price_hint:
            badges.append(mc.price_hint)

    return {
        "level": level,
        "is_cloud": not est.free,
        "title": title,
        "detail": detail,
        "estimate": est.as_dict(),
        "badges": badges,
    }


# ------------------------------------------------------------------
# 整份配置的提醒（设置页用）
# ------------------------------------------------------------------

def resolve_effective(snapshot_settings: dict, defaults: dict | None = None) -> dict:
    """把用户设置补全为"实际会生效的 provider/model"。

    用户没显式配置时，真正生效的是 `.env` / `config.py` 的默认值（含 per-role 覆盖）。
    若只扫描用户设置，会得出"全部本地免费"的错误结论——方向恰好相反。
    """
    s = dict(snapshot_settings or {})
    d = dict(defaults or {})

    def pick(*keys, fallback=""):
        for k in keys:
            v = s.get(k)
            if v:
                return v
        for k in keys:
            v = d.get(k)
            if v:
                return v
        return fallback

    return {
        "llm_provider": pick("llm_provider", fallback="deepseek"),
        "llm_model": pick("llm_model", "deepseek_model"),
        "image_provider": pick("image_provider", fallback="minimax"),
        "image_model": pick("image_model", fallback="image-01"),
        "video_provider": pick("video_provider", fallback="minimax"),
        "video_model": pick("video_model", fallback="MiniMax-H3"),
        "tts_provider": pick("tts_provider", fallback="edge"),
        "lora_train_provider": pick("lora_train_provider", fallback="local"),
        # 用户显式设过的键（用于区分"默认回退"与"用户主动选择"）
        "_explicit": sorted(k for k, v in s.items() if v),
    }


def audit_settings(snapshot_settings: dict, defaults: dict | None = None) -> dict:
    """扫描配置，列出所有"会走云端"的项与整体提示。

    Args:
        snapshot_settings: `runtime_settings.snapshot()` 的内容
        defaults:          回退用的默认值（通常是 `config.settings` 的相关字段）

    Returns:
        {"cloud_items": [...], "free_items": [...], "summary": str, "has_cloud": bool,
         "effective": {...}}
    """
    eff = resolve_effective(snapshot_settings, defaults)
    explicit = set(eff.pop("_explicit", []))

    CAP_OF = {
        "llm_provider": "llm",
        "image_provider": "image_gen",
        "video_provider": "video_gen",
        "tts_provider": "tts",
        "lora_train_provider": "lora_train",
    }
    MODEL_OF = {
        "llm_provider": "llm_model",
        "image_provider": "image_model",
        "video_provider": "video_model",
        "lora_train_provider": "lora_train_provider",
    }

    cloud_items, free_items = [], []
    for pkey, provider in eff.items():
        cap = CAP_OF.get(pkey)
        if not cap or not provider:
            continue
        model = eff.get(MODEL_OF.get(pkey, ""), "") or ""
        item = {
            "key": pkey,
            "provider": provider,
            "model": model,
            "from_user_setting": pkey in explicit,
            **reminder(provider, model, cap),   # type: ignore[arg-type]
        }
        (cloud_items if item["is_cloud"] else free_items).append(item)

    has_cloud = bool(cloud_items)
    if has_cloud:
        summary = (f"当前有 {len(cloud_items)} 项会走云端、产生 API 费用"
                   f"（{len(free_items)} 项为本地/免费）。")
    else:
        summary = "当前全部为本地/免费通道，不产生 API 费用。"
    return {"cloud_items": cloud_items, "free_items": free_items,
            "summary": summary, "has_cloud": has_cloud, "effective": eff}
