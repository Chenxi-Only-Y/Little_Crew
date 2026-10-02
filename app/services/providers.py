"""小剧组 (Little Crew) — Provider 注册表（能力 → 提供者 → 参数）。

解决的问题：现状 provider 清单散在 **3 处**
    - `app/core/config.py` 的默认值
    - `frontend/pages/9_设置.py` 的 PROVIDER_OPTIONS / PROVIDER_LABEL / PROVIDER_DEFAULT_MODEL
    - `app/services/image.py` / `video.py` 里的 `if provider == ...` 分派
并且"并发度"是写死在调用方的常量（`ImageGenerator.CONCURRENCY = 4`），
接了本地推理后会与服务端的串行锁打架（见 `docs/recon/07-risks-predictions.md §2`）。

本模块只做三件事：
    1. 声明每个 provider 的元数据（类别/可用性/默认模型/并发上限/已知模型）
    2. 给调用方查"并发度"等能力参数
    3. 供接口下发给前端，消除前端那份重复清单

⚠️ **不重写既有的 provider 实现**：`image.py` 的 `_local()` / `video.py` 的 `_real_local()`
   等函数体保持原样，只把"分派分支"改为查本表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ------------------------------------------------------------------
# 能力类别（与注册表 types.ProviderCapability 一致）
# ------------------------------------------------------------------

Capability = Literal[
    "llm", "image_gen", "image_edit", "video_gen", "lora_train", "tts", "music", "render_2d",
]

#: 提供者类别
Category = Literal["local", "cn_cloud", "overseas_cloud", "gateway", "free"]

#: 视频模型能接受的**结构条件**维度。
#:
#: 为什么做成按维度声明：白模（预演）该产出什么，完全取决于所选的视频模型能吃什么。
#: 模型只收首尾帧，白模就该去算"起点与终点的布局"；模型支持深度序列，
#: 白模才值得去逐帧渲染深度图。**先问模型能吃什么，再决定画什么**，
#: 否则会做出视频模型根本用不上的产物（本仓库 v1 踩过类似坑：
#: 注册表声明了 `scene_whitebox`，但没有任何生产者与消费者）。
ConditioningKind = Literal[
    "text",          # 纯文生视频（无图）
    "first_frame",   # 首帧图生视频
    "last_frame",    # 尾帧引导（指定终点）
    "depth",         # 深度序列控制
    "pose",          # 骨架/姿态控制
    "canny",         # 边缘控制
    "reference",     # 参考图（角色一致性，非运动控制）
    "camera",        # 显式相机参数（推拉摇移）
]

#: 维度的中文说明（给用户看，也用于白模页面的提示文案）
CONDITIONING_LABELS: dict[str, str] = {
    "text": "文字描述",
    "first_frame": "首帧图",
    "last_frame": "尾帧图（指定终点）",
    "depth": "深度序列",
    "pose": "骨架姿态",
    "canny": "边缘控制",
    "reference": "参考图（一致性）",
    "camera": "相机参数",
    # reference_* 是"参考素材"族：给素材让它照着生成（外观/运镜/节奏）
    "reference_image": "参考图",
    "reference_video": "参考视频（复刻运镜与走位）",
    "reference_audio": "参考音频",
}

#: 哪些维度属于"结构/运动控制"——决定白模是否有活可干。
#:
#: 注意 `reference_*` 也算：参考视频能复刻运镜与走位、参考图能锚定外观，
#: 它们同样让白模的产出能影响结果。早先把它们排除在外，
#: 导致"支持参考视频"的模型被误判为"白模只能给首尾帧"。
STRUCTURAL_CONDITIONING: frozenset[str] = frozenset(
    {
        "first_frame", "last_frame", "depth", "pose", "canny", "camera",
        "reference_image", "reference_video", "reference_audio",
    }
)


#: 维度分组：同一次请求里**只能选一组**。
#:
#: 依据官方文档：首尾帧（i2v）与参考素材（r2v）互斥——
#:   MiniMax：「Image-to-video and reference-to-video are mutually exclusive」
#:   万相   ：「reference_xx/file/link 类型和 first_frame/last_frame 类型互斥，
#:             不能在同一请求中混用」
#: 与其让用户在运行时吃 API 错误，不如在这里声明出来，由 UI 提前拦住。
CONDITIONING_GROUPS: dict[str, tuple[str, ...]] = {
    "i2v": ("first_frame", "last_frame"),
    "r2v": ("reference_image", "reference_video", "reference_audio"),
}


@dataclass(frozen=True)
class ProviderSpec:
    """一个提供者的能力与约束。

    Attributes:
        key:             提供者标识（与设置项 `*_provider` 的取值一致）
        label:           中文展示名
        capability:      能力类别
        category:        类别（本地 / 国内云 / 海外云 / 聚合网关 / 免费）
        available:       v2 是否已接入（False = 仅登记，不提供选择）
        default_model:   默认模型（设置页留空时使用）
        known_models:    已知可选模型（供 UI 展示；空元组表示由用户自行填写）
        needs_api_key:   是否需要 API Key
        needs_proxy:     是否需要海外网络
        max_concurrency: 该提供者能承受的并发数（**本地=1**，云端通常 4）
        presets:         该提供者支持的**参数预设**，如时长/分辨率。
                         键为参数名，值为可选列表；UI 据此限制选项，避免给出
                         提供者根本不支持的组合（例如本地 8GB 上 12s 必 OOM）。
        conditioning:    该提供者能接受的**结构条件**（见 `ConditioningKind`）。
                         仅 `video_gen` 有意义。白模据此决定产出什么：
                         只有 `first_frame` 就只算起点布局；含 `last_frame`
                         就再算终点布局；含 `depth`/`pose` 才值得逐帧渲染。
        note:            补充说明（展示给用户）
    """

    key: str
    label: str
    capability: Capability
    category: Category
    available: bool = True
    default_model: str = ""
    known_models: tuple[str, ...] = ()
    needs_api_key: bool = True
    needs_proxy: bool = False
    max_concurrency: int = 4
    presets: dict[str, tuple] = field(default_factory=dict)
    conditioning: tuple[ConditioningKind, ...] = ()
    note: str = ""


# ------------------------------------------------------------------
# 视频参数预设（时长 / 分辨率）
# ------------------------------------------------------------------
# 需求（用户 2026-10-02）：每段时长默认 7 秒，提供 5/7/9/12/15 供选择。
# 但**本地 8GB 实测安全上限是 10 秒**（12s 起必 OOM，降分辨率无效，
# 见 local-gen README 与 docs/recon/05-roles-v2.md §6.1），
# 因此 12/15 只能给云端；本地必须被拦住——这就是"上限由 provider 声明"。
#
# 注意：本块必须定义在 `PROVIDERS` **之前**，因为 provider 条目的 presets 会引用它。

#: 全量时长档位（云端可用）
DURATION_PRESETS: tuple[int, ...] = (5, 7, 9, 12, 15)

#: 未显式设置时的默认时长（秒）
DEFAULT_DURATION: int = 7

#: 分辨率档位（1080P 已剔除：已知提供者都不支持）
RESOLUTION_PRESETS: tuple[str, ...] = ("768P", "2K")

#: 每个提供者支持的时长档位
DURATION_BY_PROVIDER: dict[str, tuple[int, ...]] = {
    "local": (5, 7, 9),            # 10s 为硬上限；9 是最后一个安全档
    "minimax": DURATION_PRESETS,
    "ffmpeg": DURATION_PRESETS,    # 本地拼接，时长不受显存限制
}

#: 每个提供者支持的分辨率档位
RESOLUTION_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "local": ("768P",),            # 本机只跑 768P
    "minimax": ("768P", "2K"),     # MiniMax-H3 官方 768P | 2K（无 1080P）
    "ffmpeg": RESOLUTION_PRESETS,  # 拼接分辨率由素材决定
}


# ------------------------------------------------------------------
# Provider 表
# ------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # ================= 文字大模型（API 与本地等价可选）=================
    ProviderSpec("deepseek", "DeepSeek", "llm", "cn_cloud",
                 default_model="deepseek-v4-flash", note="默认供应商，OpenAI 兼容"),
    ProviderSpec("zhipu", "智谱 GLM", "llm", "cn_cloud", default_model="glm-4"),
    ProviderSpec("qwen", "阿里通义千问", "llm", "cn_cloud", default_model="qwen-plus"),
    ProviderSpec("moonshot", "月之暗面 Kimi", "llm", "cn_cloud", default_model="moonshot-v1-8k"),
    ProviderSpec("doubao", "字节豆包（火山方舟）", "llm", "cn_cloud", default_model="doubao-pro-32k"),
    ProviderSpec("ernie", "百度文心（千帆）", "llm", "cn_cloud", default_model="ernie-4.0-8k"),
    ProviderSpec("openai", "OpenAI", "llm", "overseas_cloud",
                 default_model="gpt-4o-mini", needs_proxy=True),
    ProviderSpec("claude", "Anthropic Claude", "llm", "overseas_cloud",
                 default_model="claude-3-5-sonnet-latest", needs_proxy=True),
    ProviderSpec("gemini", "Google Gemini", "llm", "overseas_cloud",
                 default_model="gemini-1.5-pro", needs_proxy=True),
    ProviderSpec("ollama", "本地 Ollama", "llm", "local",
                 default_model="qwen2.5", needs_api_key=False,
                 note="本地推理，零成本；需本机已启动 ollama"),
    ProviderSpec("vllm", "本地 vLLM", "llm", "local",
                 default_model="Qwen/Qwen2.5-7B-Instruct", needs_api_key=False,
                 note="本地推理，零成本；需本机已启动 vLLM"),
    ProviderSpec("custom", "自定义（OpenAI 兼容）", "llm", "gateway", note="自行填写 base_url 与模型"),

    # ================= 文生图 =================
    ProviderSpec("local", "本地推理 local-gen（零成本）", "image_gen", "local",
                 default_model="z-image-turbo", needs_api_key=False, max_concurrency=1,
                 known_models=("z-image-turbo",),
                 note="本机 8GB 显存：出图约 32 秒/张；服务端串行推理，并发度固定为 1"),
    ProviderSpec("dashscope", "阿里百炼（通义万相 / 千问）", "image_gen", "cn_cloud",
                 default_model="z-image-turbo",
                 known_models=("z-image-turbo", "wan2.7-image-pro", "wan2.7-image",
                               "qwen-image-2.0-pro", "qwen-image-2.0"),
                 note="z-image* 走同步接口；其余走异步任务"),
    ProviderSpec("minimax", "MiniMax", "image_gen", "cn_cloud",
                 default_model="image-01", known_models=("image-01",)),
    ProviderSpec("replicate", "Replicate", "image_gen", "gateway",
                 needs_proxy=True,
                 known_models=("black-forest-labs/flux-2-dev", "stabilityai/sd3.5-large"),
                 note="需填具体 model version（IMAGE_BASE_URL）"),
    ProviderSpec("wavespeed", "WaveSpeedAI（聚合）", "image_gen", "gateway",
                 available=False, needs_proxy=True,
                 known_models=("openai/gpt-image-1.5", "google/gemini-3-pro-image",
                               "black-forest-labs/flux-2-pro", "bytedance/seedream-4.5",
                               "tencent/hunyuan-image-3.0", "ideogram/ideogram-2.0"),
                 note="一个 key 覆盖数十个模型（v2 待接入）"),

    # ================= 图像编辑 / 崩坏修复 =================
    ProviderSpec("local", "本地 inpaint（diffusers）", "image_edit", "local",
                 available=False, needs_api_key=False, max_concurrency=1,
                 known_models=("z-image-inpaint",),
                 note="ZImageInpaintPipeline 与现有权重共用；需在 local-gen 增加路由（v2 待接入）"),
    ProviderSpec("dashscope", "阿里百炼（千问图像编辑）", "image_edit", "cn_cloud",
                 default_model="qwen-image-2.0-pro",
                 known_models=("qwen-image-2.0-pro", "qwen-image-edit-max", "qwen-image-edit-plus"),
                 note="指令式编辑：改文字/增删物体/改动作/细节增强，支持 1–3 张多图输入"),

    # ================= 图生视频 =================
    ProviderSpec("local", "本地 MiniMax H3（零成本）", "video_gen", "local",
                 default_model="minimax-h3", needs_api_key=False, max_concurrency=1,
                 known_models=("minimax-h3",),
                 presets={"duration": DURATION_BY_PROVIDER["local"],
                          "resolution": RESOLUTION_BY_PROVIDER["local"]},
                 # 依据：local-gen 的 H3 `generate(keyframes=...)` 把 1 张放 index 0、
                 # 2 张放 index [0, -1]（见 local-gen/app/engines/h3.py:152），
                 # 且 API 端点已暴露 `image_base64` 与 `last_image_base64`
                 # （local-gen/app/server.py:179/188）。它是**首尾关键帧**模型，
                 # 不接受 depth/pose 等逐帧结构条件。
                 conditioning=("text", "first_frame", "last_frame"),
                 note="8GB 实测：8 步约 8.6 分钟/段；**时长上限 10 秒**，超过必 OOM（降分辨率无效）"),
    ProviderSpec("minimax", "MiniMax（Hailuo）", "video_gen", "cn_cloud",
                 default_model="MiniMax-H3", known_models=("MiniMax-H3", "MiniMax-H3-Max"),
                 presets={"duration": DURATION_BY_PROVIDER["minimax"],
                          "resolution": RESOLUTION_BY_PROVIDER["minimax"]},
                 # 依据官方 v2 文档（POST /v2/video_generation）：
                 #   content 数组支持 text / image_url / video_url / audio_url，
                 #   并用 role 区分用途：
                 #     first_frame / last_frame / reference_image /
                 #     reference_video / reference_audio
                 #   限制：参考视频 ≤3 段、每段 2~15s、总 ≤15s、单文件 ≤50MB
                 #   **首尾帧与参考视频互斥**（不能同一请求混用）
                 conditioning=("text", "first_frame", "last_frame",
                               "reference_image", "reference_video", "reference_audio"),
                 note="H3: 768P/2K，4~15s；H3-Max 更快但无 2K、5~15s。"
                      "支持白模参考视频（复刻运镜与走位）。**首尾帧与参考视频互斥**"),
    ProviderSpec("ffmpeg", "FFmpeg 本地合成（免费）", "video_gen", "free",
                 default_model="ffmpeg", needs_api_key=False,
                 presets={"duration": DURATION_BY_PROVIDER["ffmpeg"],
                          "resolution": RESOLUTION_BY_PROVIDER["ffmpeg"]},
                 # 图片拼接：能给首帧，但没有任何运动控制
                 conditioning=("first_frame",),
                 note="图片幻灯片拼接；时长固定、无真实运镜（转场/字幕需自行实现）"),

    # ================= LoRA / 自定义模型训练 =================
    ProviderSpec("local", "本地 NF4 QLoRA（DiffSynth）", "lora_train", "local",
                 default_model="diffsynth-qlora", needs_api_key=False, max_concurrency=1,
                 known_models=("diffsynth-qlora",),
                 note="peft + bitsandbytes 已就绪；只能训 Z-Image 底模；8GB 显存较慢"),
    ProviderSpec("dashscope", "阿里百炼（SFT-LoRA 微调）", "lora_train", "cn_cloud",
                 known_models=("wan2.7-image-pro", "qwen-image-2.0-pro"),
                 note="按训练步数计费；**仅华北2(北京)地域**，须用该地域 API Key；"
                      "训练产物绑定底模，只能在百炼推理"),
    ProviderSpec("replicate", "Replicate LoRA 训练", "lora_train", "gateway",
                 needs_proxy=True, known_models=("flux-lora", "sdxl-lora"),
                 note="仅支持 Flux/SDXL 底模；⚠️ 训出的 LoRA 不能给 Z-Image 用"),

    # ================= 配音 / 音乐 =================
    ProviderSpec("edge", "Edge TTS（免费兜底）", "tts", "free",
                 needs_api_key=False, known_models=("edge-tts",),
                 note="零成本；音色较少，适合先跑通链路"),
    ProviderSpec("dashscope", "阿里 CosyVoice", "tts", "cn_cloud",
                 known_models=("cosyvoice",), note="中文效果强"),
    ProviderSpec("minimax", "MiniMax 语音", "tts", "cn_cloud",
                 known_models=("minimax-speech",), note="可与现有 MiniMax Key 共用"),
    ProviderSpec("elevenlabs", "ElevenLabs", "tts", "overseas_cloud",
                 needs_proxy=True, known_models=("elevenlabs",), note="情感与音色最丰富"),

    # ================= 2D 白模预演 =================
    ProviderSpec("local", "本地 2D 绘制（PIL）", "render_2d", "local",
                 available=True, needs_api_key=False, max_concurrency=1,
                 note="俯视走位/机位示意图；零新依赖"),
)

#: (capability, key) -> spec
_BY_CAP_KEY: dict[tuple[str, str], ProviderSpec] = {
    (p.capability, p.key): p for p in PROVIDERS
}


# ------------------------------------------------------------------
# 把 `video_adapters` 里登记的云端视频提供者并入注册表
# ------------------------------------------------------------------
# 为什么要合并而不是各留一份清单：
#   `video_adapters` 是实现（怎么调），这里是**能力声明**（能干什么、让 UI 展示）。
#   两份清单一旦漂移，就会出现"UI 里能选、实际没有实现"或反之。
#   所以以 `video_adapters` 为准，在这里自动补齐。
def _sync_video_providers() -> None:
    try:
        from app.services.video_adapters import VIDEO_PROVIDERS
    except Exception:  # noqa: BLE001  适配器不可用时不应拖垮注册表
        return

    global PROVIDERS
    have = {p.key for p in PROVIDERS if p.capability == "video_gen"}
    extra: list[ProviderSpec] = []
    for vp in VIDEO_PROVIDERS:
        if vp.key in have:
            continue
        lo, hi = vp.durations
        extra.append(
            ProviderSpec(
                key=vp.key,
                label=vp.label,
                capability="video_gen",
                category=vp.category,  # type: ignore[arg-type]
                available=True,
                default_model=vp.model,
                known_models=vp.known_models,
                needs_api_key=True,
                presets={"duration": tuple(range(lo, hi + 1)),
                         "resolution": ("480P", "720P", "1080P")},
                conditioning=vp.conditioning,  # type: ignore[arg-type]
                note=vp.note + ("" if vp.verified else "（按公开文档实现，尚未用真实 key 跑通）"),
            )
        )
    if extra:
        PROVIDERS = PROVIDERS + tuple(extra)
        _BY_CAP_KEY.update({(p.capability, p.key): p for p in extra})


_sync_video_providers()


def allowed_durations(provider: str) -> tuple[int, ...]:
    """该提供者允许的时长档位（未知提供者按云端全量返回）。"""
    return DURATION_BY_PROVIDER.get((provider or "").strip().lower(), DURATION_PRESETS)


def allowed_resolutions(provider: str) -> tuple[str, ...]:
    """该提供者允许的分辨率档位。"""
    return RESOLUTION_BY_PROVIDER.get((provider or "").strip().lower(), RESOLUTION_PRESETS)


def resolve_duration(provider: str, requested) -> tuple[int, str | None]:
    """把用户请求的时长收敛到该提供者支持的档位。

    规则：
      1. 未设置 → 用 `DEFAULT_DURATION`
      2. 请求值不在档位里 → **向下取最近档位**（宁可短，不要 OOM）
      3. 请求值低于最小档位 → 用最小档位
      4. 发生调整时返回原因字符串，供上层写 warning

    Returns:
        (实际时长秒数, 调整原因或 None)
    """
    allowed = allowed_durations(provider)
    if not allowed:
        return DEFAULT_DURATION, None
    lo, hi = min(allowed), max(allowed)

    if requested in (None, "", 0):
        want = DEFAULT_DURATION
        note = None
    else:
        try:
            want = int(requested)
        except (TypeError, ValueError):
            want = DEFAULT_DURATION
            note = f"时长取值无法识别，已用默认 {DEFAULT_DURATION}s"
        else:
            note = None

    if want in allowed:
        return want, note

    if want > hi:
        return hi, (
            f"请求时长 {want}s 超过「{provider}」上限 {hi}s，已收敛为 {hi}s"
            + ("（本地 8GB 显存超过 10s 必 OOM，降分辨率无效）"
               if provider == "local" else "")
        )
    if want < lo:
        return lo, f"请求时长 {want}s 低于最小档位 {lo}s，已提升为 {lo}s"

    # 落在档位之间 → 向下取最近
    lower = max(x for x in allowed if x < want)
    return lower, f"请求时长 {want}s 不是可用档位 {list(allowed)}，已向下取 {lower}s"


def resolve_resolution(provider: str, requested: str | None) -> tuple[str, str | None]:
    """把用户请求的分辨率收敛到该提供者支持的档位。"""
    allowed = allowed_resolutions(provider)
    if not allowed:
        return "768P", None
    want = (requested or "").strip().upper()
    if not want:
        return allowed[0], None
    if want in allowed:
        return want, None
    return allowed[0], (
        f"分辨率 {want} 不被「{provider}」支持（可选：{'/'.join(allowed)}），已改用 {allowed[0]}"
    )


# ------------------------------------------------------------------
# 查询
# ------------------------------------------------------------------

def get(capability: str, provider_key: str) -> ProviderSpec | None:
    """取某能力下的 provider 规格。"""
    return _BY_CAP_KEY.get((capability, (provider_key or "").strip().lower()))


def for_capability(capability: str, *, include_unavailable: bool = False) -> list[ProviderSpec]:
    """某能力下的 provider 列表（默认只返回已接入的）。"""
    items = [p for p in PROVIDERS if p.capability == capability]
    if not include_unavailable:
        items = [p for p in items if p.available]
    return items


def all_specs() -> tuple[ProviderSpec, ...]:
    return PROVIDERS


def max_concurrency(capability: str, provider_key: str, desired: int | None = None) -> int:
    """该 provider 允许的并发度（取声明值；本地为 1）。

    Args:
        capability:   能力类别
        provider_key: 提供者标识
        desired:      调用方期望的并发（None = 用 provider 声明值）
    """
    spec = get(capability, provider_key)
    if spec is None:
        return max(1, desired or 1)
    if desired is None:
        return max(1, spec.max_concurrency)
    return max(1, min(desired, spec.max_concurrency))


def needs_proxy(capability: str, provider_key: str) -> bool:
    spec = get(capability, provider_key)
    return bool(spec and spec.needs_proxy)


def to_payload() -> list[dict]:
    """供接口下发（前端据此渲染下拉，不再自带清单）。"""
    return [
        {
            "key": p.key, "label": p.label, "capability": p.capability,
            "category": p.category, "available": p.available,
            "default_model": p.default_model, "known_models": list(p.known_models),
            "needs_api_key": p.needs_api_key, "needs_proxy": p.needs_proxy,
            "max_concurrency": p.max_concurrency,
            "presets": {k: list(v) for k, v in p.presets.items()},
            "note": p.note,
        }
        for p in PROVIDERS
    ]


def video_parameter_options() -> dict:
    """视频时长/分辨率的可选项与默认值（供设置页与导演台渲染）。

    UI 应据此限制选项；后端在执行时还会用 `resolve_duration/resolve_resolution`
    再收敛一次（前端选项不作为执行依据）。
    """
    return {
        "durations": list(DURATION_PRESETS),
        "default_duration": DEFAULT_DURATION,
        "resolutions": list(RESOLUTION_PRESETS),
        "by_provider": {
            key: {
                "durations": list(allowed_durations(key)),
                "resolutions": list(allowed_resolutions(key)),
            }
            for key in sorted(set(DURATION_BY_PROVIDER) | set(RESOLUTION_BY_PROVIDER))
        },
    }


# ------------------------------------------------------------------
# 提供者解析：显式模式（云端 / 本地）优先，**不做静默默认**
# ------------------------------------------------------------------
# 设计决定（用户 2026-10-02）：**不要默认本地、也不要默认云端**——
# 让用户显式选择"云端 or 本地"，再（可选）指定具体提供者。
#
# 为什么不能有静默默认：
#   1. 默认本地 → 用户没部署本地模型时会直接失败，且耗时长；
#   2. 默认云端 → 用户没意识到会产生费用（LoRA 训练/视频按量计费金额可观）；
#   3. 静默默认还会掩盖"其实没配"的事实，让问题在很晚才暴露。
#
# 因此：两个都没设时**明确报错**，由调用方转成可读提示，引导用户去选。

ProviderMode = Literal["cloud", "local", "unset"]

#: 各能力对应的"模式默认提供者"。
#:
#: 用户只选云端/本地、不指定具体提供者时用这里的值。
#: `video_gen` 的云端默认选 MiniMax：它支持参考视频且单价最低（约 $0.01/秒）。
_MODE_DEFAULT: dict[str, dict[str, str]] = {
    "llm": {"cloud": "deepseek", "local": "ollama"},
    "image_gen": {"cloud": "dashscope", "local": "local"},
    "image_edit": {"cloud": "dashscope", "local": "local"},
    "video_gen": {"cloud": "minimax", "local": "local"},
    "lora_train": {"cloud": "dashscope", "local": "local"},
    "tts": {"cloud": "dashscope", "local": "edge"},
}

#: 能力 -> 对应的"模式"设置键（运行时设置里的键名）。
#: 命名规则统一为 `{具体提供者的键}_mode`，便于记忆与自动派生。
MODE_KEY_BY_CAPABILITY: dict[str, str] = {
    "llm": "llm_provider_mode",
    "image_gen": "image_provider_mode",
    "image_edit": "image_provider_mode",          # 编辑与生成共用同一个模式
    "video_gen": "video_provider_mode",
    "lora_train": "lora_train_provider_mode",
    "tts": "tts_provider_mode",
}

#: 能力 -> 对应的"具体提供者"设置键
PROVIDER_KEY_BY_CAPABILITY: dict[str, str] = {
    "llm": "llm_provider",
    "image_gen": "image_provider",
    "image_edit": "image_provider",
    "video_gen": "video_provider",
    "lora_train": "lora_train_provider",
    "tts": "tts_provider",
}

MODE_LABELS: dict[str, str] = {
    "cloud": "云端 API",
    "local": "本地推理",
    "unset": "未选择",
}


def mode_default_provider(capability: str, mode: str) -> str | None:
    """某能力在某模式下的默认提供者。"""
    return _MODE_DEFAULT.get(capability, {}).get((mode or "").strip().lower())


def resolve_provider(capability: str, role: str = "") -> tuple[str | None, str]:
    """解析该能力当前应使用的提供者。

    优先级：
        1. 该 role 的显式提供者（`{key}_{role}`，如 `image_provider_photographer`）
        2. 全局显式提供者（`{key}`，如 `image_provider`）
        3. 模式（`cloud` / `local`）对应的默认提供者
        4. 都没有 → 返回 (None, 原因)，**不猜**

    Returns:
        (provider 或 None, 人话说明)。provider 为 None 时说明里含引导语。
    """
    from app.services.runtime_settings import get as rs_get

    pkey = PROVIDER_KEY_BY_CAPABILITY.get(capability)
    mkey = MODE_KEY_BY_CAPABILITY.get(capability)
    if not pkey:
        return None, f"未知能力：{capability}"

    if role:
        v = rs_get(f"{pkey}_{role}")
        if v:
            return str(v), f"来自「{role}」的职业级设置"

    v = rs_get(pkey)
    if v:
        return str(v), "来自全局设置"

    mode = str(rs_get(mkey) or "unset").strip().lower()
    default = mode_default_provider(capability, mode)
    if default:
        return default, f"来自「{MODE_LABELS.get(mode, mode)}」模式"

    return None, (
        f"未选择「{capability}」的提供者。请先在设置里选择 **云端 API** 或 **本地推理**"
        f"（对应设置项：{mkey}），这样系统才知道该走哪条路，避免猜错产生意外费用或直接失败。"
    )


def video_conditioning(provider: str | None = None) -> tuple[str, ...]:
    """某视频提供者能接受的结构条件。

    provider 为空时按当前设置解析（**不猜**：未选则返回空元组）。
    """
    if not provider:
        provider, _ = resolve_provider("video_gen")
    if not provider:
        return ()
    spec = get("video_gen", provider.lower())
    return tuple(spec.conditioning) if spec else ()


def conditioning_groups_for(provider: str | None = None) -> dict[str, list[str]]:
    """该提供者当前可用的"条件分组"（同组内互斥，组间也互斥）。

    用于界面：选了首尾帧就不能再选参考素材，反之亦然。
    """
    cond = set(video_conditioning(provider))
    out: dict[str, list[str]] = {}
    for name, members in CONDITIONING_GROUPS.items():
        avail = [m for m in members if m in cond]
        if avail:
            out[name] = avail
    return out


def check_conditioning_choice(provider: str | None, chosen: list[str]) -> str | None:
    """校验一次请求里选的条件组合是否合法。

    Returns:
        None = 合法；否则返回面向用户的错误说明。
    """
    cond = set(video_conditioning(provider))
    unknown = [c for c in chosen if c not in cond]
    if unknown:
        labels = [CONDITIONING_LABELS.get(c, c) for c in unknown]
        return f"所选的视频模型不支持这些条件：{'、'.join(labels)}"

    used_groups = {
        name for name, members in CONDITIONING_GROUPS.items()
        if any(m in chosen for m in members)
    }
    if len(used_groups) > 1:
        pretty = " 与 ".join(
            "首尾帧" if g == "i2v" else "参考素材" for g in sorted(used_groups)
        )
        return (
            f"{pretty} 互斥，不能在同一次请求里混用。"
            "请二选一：要么用首尾帧锚定起止画面，要么用参考素材（图片/视频/音频）复刻。"
        )
    return None


def previs_plan(provider: str | None = None) -> dict:
    """白模（预演）该产出什么 —— **由所选视频模型的能力决定**。

    这是用户 2026-10-02 定的方向：「白模根据用户的视频模型加入，
    如果模型支持就可以用」。所以白模不是一个固定产物，
    而是"视频模型能吃什么，就准备什么"。

    Returns:
        {
          "provider": 视频提供者（None=未选）,
          "conditioning": 该提供者接受的条件维度,
          "supported": 其中属于结构控制的维度,
          "deliverables": 白模应产出的东西（人话，供界面显示）,
          "useful": 白模是否有意义（只收 text/first_frame 时价值有限）,
          "reason": 面向用户的说明,
        }
    """
    provider = (provider or "").strip().lower() or None
    if provider is None:
        p, note = resolve_provider("video_gen")
        provider = (p or "").lower() or None
        if provider is None:
            return {
                "provider": None,
                "conditioning": (),
                "supported": (),
                "deliverables": [],
                "useful": False,
                "reason": f"尚未选择视频模型，无法确定白模要产出什么。{note}",
            }

    cond = video_conditioning(provider)
    supported = tuple(k for k in cond if k in STRUCTURAL_CONDITIONING)

    deliverables: list[str] = []
    # 参考视频通道：白模**录像**直接作为参考，复刻运镜与走位。
    # 这是当前云端最实用的一条（实测：三颗经典镜头里，白模参考版把抽卡次数
    # 从 3 次降到 2 次，纯提示词版在推门/切换/站位上均失败）。
    if "reference_video" in supported:
        deliverables.append("白模参考视频（≤15 秒，复刻运镜与走位——最推荐）")
    if "last_frame" in supported:
        deliverables.append("终点布局图（决定镜头结束时人物在哪、画面怎么收）")
    if "first_frame" in supported:
        deliverables.append("起点布局图（决定开场构图）")
    if "depth" in supported:
        deliverables.append("逐帧深度序列（限制空间结构，最严格）")
    if "pose" in supported:
        deliverables.append("逐帧骨架序列（限制人物姿态与走位）")
    if "canny" in supported:
        deliverables.append("逐帧边缘图（限制轮廓与线条）")
    if "camera" in supported:
        deliverables.append("相机轨迹参数（推拉摇移的显式数值）")

    if not supported:
        useful = False
        reason = (
            f"所选的「{provider}」只接受 {'/'.join(cond) or '文字'}，"
            "没有任何结构条件入口——白模产出无法影响视频结果，做了也用不上。"
        )
    elif "reference_video" in supported:
        useful = True
        reason = (
            f"「{provider}」支持参考视频：白模**录像**直接送进去，"
            "运镜与人物走位会被复刻。这是当前可控性最高且最省事的一条路"
            "（首尾帧与参考视频互斥，二选一）。"
        )
    elif supported == ("first_frame",):
        useful = True
        reason = (
            f"「{provider}」只接受首帧图：白模只需给出**起点构图**。"
            "没有终点约束，运动过程仍由模型自行插值，复杂位移可能不稳。"
        )
    elif "last_frame" in supported:
        useful = True
        reason = (
            f"「{provider}」支持首尾帧：白模给出**起点与终点布局**，"
            "位移方向与落点被钉死。若它也支持参考视频，优先用参考视频。"
        )
    else:
        useful = True
        reason = f"「{provider}」支持 {len(supported)} 种结构条件，白模可逐帧渲染。"

    return {
        "provider": provider,
        "conditioning": cond,
        "supported": supported,
        "deliverables": deliverables,
        "useful": useful,
        "reason": reason,
    }


def generation_settings() -> list[dict]:
    """需要用户显式选择的"模式"设置项（供设置页与导演台渲染）。

    UI 应把它们放在最显眼位置：这是**必须先做的选择**。
    """
    out = []
    for cap in ("llm", "image_gen", "video_gen", "lora_train", "tts"):
        mkey = MODE_KEY_BY_CAPABILITY[cap]
        modes = _MODE_DEFAULT.get(cap, {})
        out.append({
            "capability": cap,
            "mode_key": mkey,
            "provider_key": PROVIDER_KEY_BY_CAPABILITY[cap],
            "mode_options": ["unset", *modes.keys()],
            "mode_labels": MODE_LABELS,
            "mode_defaults": {m: p for m, p in modes.items()},
        })
    return out


# ------------------------------------------------------------------
# 能力开关（mock / 真实）的解析
# ------------------------------------------------------------------
# 背景：服务类原先在 `__init__` 里快照 `self.mock = settings.mock_xx`，
# 导致「设置页保存立即生效」是**假的**——已创建的单例永远停在旧值（已实测）。
# 这里统一为「每次读取」，优先级：
#     显式覆盖（测试用） > 运行时设置（设置页保存的） > config/.env 默认

#: 能力 -> config 里的字段名
_SETTING_KEY = {
    "llm": "mock_llm",
    "image": "mock_image",
    "video": "mock_video",
    "quality": "mock_quality",
    "critics": "mock_critics",
}

#: 显式覆盖（供测试注入；None 表示未覆盖）
_OVERRIDES: dict[str, bool] = {}


def set_override(capability: str, value: bool | None) -> None:
    """显式覆盖某能力的 mock 开关（None = 清除覆盖，恢复按配置解析）。"""
    if value is None:
        _OVERRIDES.pop(capability, None)
    else:
        _OVERRIDES[capability] = bool(value)


def clear_overrides() -> None:
    """清除全部覆盖（测试收尾用）。"""
    _OVERRIDES.clear()


def is_mock(capability: str) -> bool:
    """该能力当前是否走 mock（每次实时解析，不做快照）。"""
    key = _SETTING_KEY.get(capability)
    if key is None:
        return False
    if capability in _OVERRIDES:
        return bool(_OVERRIDES[capability])
    from app.services.runtime_settings import get as rs_get

    v = rs_get(key)
    if v is not None:
        return bool(v)
    from app.core.config import settings

    return bool(getattr(settings, key, False))
