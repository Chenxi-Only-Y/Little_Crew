"""小剧组 (Little Crew) — Provider 注册表（能力 → 提供者 → 参数）。

解决的问题：provider 清单曾散在 **3 处**
    - `app/core/config.py` 的默认值
    - 旧 Streamlit 设置页的 PROVIDER_OPTIONS / PROVIDER_LABEL / PROVIDER_DEFAULT_MODEL
      （那个前端与 `frontend/pages/9_设置.py` 已删除；现由本表 + `/api/providers` 下发）
    - `app/services/image.py` / `video.py` 里的 `if provider == ...` 分派
并且"并发度"是写死在调用方的常量（`ImageGenerator.CONCURRENCY = 4`），
接了本地推理后会与服务端的串行锁打架（见 `docs/recon/07-risks-predictions.md §2`）。

本模块只做三件事：
    1. 声明每个 provider 的元数据（类别/可用性/默认模型/并发上限/已知模型）
    2. 给调用方查"并发度"等能力参数
    3. 供接口下发给前端（`/api/providers`，含 `credential_spec`），消除重复清单

视频类提供者由 `app/services/video_adapters.py` 声明、在文件末尾自动并入本表，
避免两处手写清单漂移。

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
    "llm", "image_gen", "image_edit", "video_gen", "tts", "music", "render_2d",
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
    #: 产出里**有没有声音**（仅 video_gen 有意义）。
    #:
    #: ⚠️ 与 `conditioning` 里的 `reference_audio` **不是一回事**：
    #:    那个是"能不能**喂**音频当参考"（输入），这个是"产出**有没有**声音"
    #:    （输出）。用户问的"配音不是某些视频模型自带吗"说的就是这个，
    #:    设置页据它决定"声音来源"里哪些选项可选。
    has_native_audio: bool = False
    note: str = ""
    #: **协议风格** —— 决定这个提供者能不能被现有客户端直接调用。
    #:
    #: 本项目 LLM 侧用 langchain `ChatOpenAI`，它固定请求
    #: `{base_url}/chat/completions`。所以只有 `"openai"` 风格能直接跑。
    #:
    #: ⚠️ 为什么要把它变成**数据**而不是写在 note 里：
    #:    清单里混进过协议不同的条目却标着可用（如 xAI 主线已转 Responses、
    #:    Perplexity 的 chat.completions 只在私有预览里），用户选中后必然失败，
    #:    而界面上只看得到一句风险提示。做成字段后"能不能用"是可查询的。
    #:
    #:   "openai"     —— OpenAI Chat Completions 兼容，**可直接用**
    #:   "responses"  —— 只有 OpenAI Responses API（/v1/responses）
    #:   "messages"   —— Anthropic Messages 协议（/v1/messages）
    #:   "unverified" —— 无法验证（网络不可达 / 官方文档未说明）
    api_style: str = "openai"


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
    #
    # 模型名于 2026-10 逐家核对。**证据分两档**，照实标注：
    #
    #   A 档：直接读到官方文档原文 —— DeepSeek / 智谱 / 千问 / Kimi / 豆包 / 文心
    #   B 档：官网在本机网络下不可达（Cloudflare 403 / 区域封锁 / 超时），
    #         改用**替代的官方来源**交叉验证 —— OpenAI / Claude / Gemini
    #         （OpenAI 官方 OpenAPI spec、Microsoft Foundry 与 AWS Bedrock
    #          官方模型卡、Google 官方 gemini-skills 仓库）
    #
    # ⚠️ 旧默认值都是**已下线或即将下线**的（gpt-4o-mini / claude-3-5-sonnet-latest /
    #    gemini-1.5-pro / moonshot-v1-8k / doubao-pro-32k / ernie-4.0-8k），
    #    照旧填会直接报错。
    ProviderSpec("deepseek", "DeepSeek", "llm", "cn_cloud",
                 default_model="deepseek-flash",
                 known_models=("deepseek-flash", "deepseek-v4-pro"),
                 note="默认供应商，OpenAI 兼容。"
                      "https://api-docs.deepseek.com/quick_start/pricing "
                      "（官方原文：deepseek-v4-flash 等 legacy names 已 retired）"),
    ProviderSpec("zhipu", "智谱 GLM", "llm", "cn_cloud",
                 default_model="glm-5.3",
                 known_models=("glm-5.3", "glm-5.3-flash", "glm-5.3-flashx", "glm-5.2"),
                 note="https://docs.bigmodel.cn/cn/guide/start/model-overview"),
    ProviderSpec("qwen", "阿里通义千问", "llm", "cn_cloud",
                 default_model="qwen3.7-plus",
                 known_models=("qwen3.8-max", "qwen3.7-plus", "qwen3.8-flash", "qwen3.7-flash"),
                 note="官方明确推荐 qwen3.7-plus；qwen-plus/max 等为旧版。"
                      "https://help.aliyun.com/zh/model-studio/text-generation-model"),
    ProviderSpec("moonshot", "月之暗面 Kimi", "llm", "cn_cloud",
                 default_model="kimi-k3",
                 known_models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"),
                 note="moonshot-v1 全系列与 kimi-k2 已下线。"
                      "https://platform.kimi.com/docs/models"),
    ProviderSpec("doubao", "字节豆包（火山方舟）", "llm", "cn_cloud",
                 default_model="doubao-seed-evolving",
                 known_models=("doubao-seed-evolving", "doubao-seed-2-1-pro-260915",
                               "doubao-seed-2-1-lite-260915", "doubao-seed-2-1-turbo-260628"),
                 note="doubao-pro-32k 等旧名已不在模型列表。"
                      "https://docs.volcengine.com/docs/ark/model-list"),
    # 小米 MiMo —— 官方文档原文核对（2026-10）：
    #   https://mimo.mi.com/docs/zh-CN/api/chat/openai-api   （请求地址与认证）
    #   https://mimo.mi.com/docs/zh-CN/api/model/list-models （GET /v1/models 响应原文）
    #   https://mimo.mi.com/docs/zh-CN/updates/deprecate     （下线公告）
    ProviderSpec("mimo", "小米 MiMo", "llm", "cn_cloud",
                 default_model="mimo-v2.6-pro",
                 # ⚠️ **只列 v2.6 三款**：v2.5 / v2.5-pro 官方公告将于
                 # 2026-10-21 10:00 直接下线，且"无系统替换模型"——
                 # 列出来等于给用户一个几天后必失效的选项。
                 known_models=("mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed", "mimo-v2.6-flash"),
                 note="OpenAI 兼容（https://api.xiaomimimo.com/v1），认证头 api-key 或 Bearer。"
                      "v2.5 系列将于 2026-10-21 下线，故未列出"),
    ProviderSpec("ernie", "百度文心（千帆）", "llm", "cn_cloud",
                 default_model="ernie-5.1",
                 known_models=("ernie-5.1", "ernie-5.0", "ernie-5.0-thinking-latest",
                               "ernie-4.5-turbo-128k"),
                 note="https://cloud.baidu.com/doc/qianfan/s/rmh4stp0j"),
    # 腾讯混元 —— ⚠️ **必须用 TokenHub**：旧平台（api.hunyuan.cloud.tencent.com）
    # 官方公告于 2026-09-30 00:00 全面停服下线，控制台与 API 同步关停。
    #   公告 https://cloud.tencent.com/announce/detail/2287
    #   模型列表 https://cloud.tencent.cn/document/product/1823/130051
    # ⚠️ 官方**未标注**哪个是默认；`hy3` 是正式版（256k 上下文 / 128k 输出），
    #    `hy4-preview` 是更新的旗舰但仍为 preview。取正式版作默认更稳。
    ProviderSpec("hunyuan", "腾讯混元（TokenHub）", "llm", "cn_cloud",
                 default_model="hy3",
                 known_models=("hy3", "hy4-preview", "hy-mt2-pro", "hunyuan-role-latest"),
                 note="旧混元域名已 2026-09-30 停服，此处用的是新平台 "
                      "https://tokenhub.tencentmaas.com/v1；"
                      "hunyuan-* 老模型名（t1/a13b/turbos/lite 等）TokenHub 已不支持"),
    # 阶跃星辰 StepFun
    #   https://platform.stepfun.com/docs/zh/guides/developer/openai.md
    ProviderSpec("stepfun", "阶跃星辰 StepFun", "llm", "cn_cloud",
                 default_model="step-5-preview",
                 known_models=("step-5-preview", "step-3.7-flash", "step-3.5-flash",
                               "step-3.5-flash-2603"),
                 note="OpenAI 兼容（https://api.stepfun.com/v1）"),
    # 百川智能 Baichuan
    #   https://platform.baichuan-ai.com/docs-v2/api  （明写"兼容 OpenAI 接口格式"）
    # ⚠️ 官方两个文档的模型清单**不一致**：通用对话页的 api.yaml 只枚举
    #    Baichuan4/3/2 系，而 quickstart 示例与医疗页用 Baichuan-M3 系。
    #    这里两组都列，默认取通用线文档标 default 的 Baichuan4-Turbo。
    ProviderSpec("baichuan", "百川智能 Baichuan", "llm", "cn_cloud",
                 default_model="Baichuan4-Turbo",
                 known_models=("Baichuan4-Turbo", "Baichuan4-Air", "Baichuan4",
                               "Baichuan-M3-Plus", "Baichuan-M3"),
                 note="OpenAI 兼容（https://api.baichuan-ai.com/v1）。"
                      "通用线为 Baichuan4 系；Baichuan-M3 系见医疗文档"),
    # MiniMax 文本对话 —— 与已有的 image_gen / video_gen / tts 同 key、不同能力。
    # ⚠️ 官方文档里 **abab 系列已完全消失**（Models 总览、model 枚举、
    #    Release Notes 均无），但**未找到下线公告**，所以不写"已下线"，
    #    只列当前在枚举里的型号。
    #   https://platform.minimax.io/docs/api-reference/text-openai-api
    ProviderSpec("minimax", "MiniMax 文本", "llm", "cn_cloud",
                 default_model="MiniMax-M3",
                 known_models=("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"),
                 note="OpenAI 兼容（https://api.minimax.io/v1），官方同时提供 "
                      "Anthropic 兼容端点。abab 系列已不在官方文档中。"
                      "M3.1-Flash-Preview 目前公开 API 调不到，故未列"),
    # Mistral AI
    #   https://github.com/mistralai/platform-docs-public/blob/main/openapi.yaml
    ProviderSpec("mistral", "Mistral AI", "llm", "overseas_cloud",
                 default_model="mistral-large-latest", needs_proxy=True,
                 known_models=("mistral-large-latest", "mistral-medium-latest",
                               "mistral-small-latest"),
                 note="OpenAI 风格 /v1/chat/completions；另有区域端点 "
                      "api.eu.mistral.ai / api.us.mistral.ai"),
    # ---- 下面几家：官网在本机网络下不可达，证据来自替代的官方来源（见上）----
    ProviderSpec("openai", "OpenAI", "llm", "overseas_cloud",
                 default_model="gpt-6.1-sol", needs_proxy=True,
                 known_models=("gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna",
                               "gpt-5.6-sol", "gpt-5.5"),
                 note="⚠️ 官网不可达，模型名经 OpenAI 官方 OpenAPI spec + "
                      "Microsoft Foundry / AWS Bedrock 官方文档交叉验证；"
                      "gpt-6.1-sol 作为默认是**推断**（未见官方『默认』声明）"),
    # 注：**Anthropic Claude 已删除**（用户 2026-10-05 决定）。
    #   原因：清单里配的 `https://api.anthropic.com/v1` 是 Anthropic **原生**
    #   风格地址，而客户端（langchain `ChatOpenAI`）请求的是
    #   `{base_url}/chat/completions` —— 协议很可能对不上。
    #   且本机对 api.anthropic.com 一律 403（`/v1/chat/completions` 与
    #   `/v1/messages` 返回完全一样），**无法验证**它是否有兼容端点。
    #   要调 Claude 请用 `openrouter`（那个网关能调）。
    ProviderSpec("gemini", "Google Gemini", "llm", "overseas_cloud",
                 default_model="gemini-3.8-flash", needs_proxy=True,
                 known_models=("gemini-3.8-flash", "gemini-3.1-pro-preview",
                               "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"),
                 note="⚠️ Google 全域不可达；证据来自官方 gemini-skills 仓库的 "
                      "『Current Models』。非 preview 的 gemini-3.1-pro "
                      "是否存在**未能验证**"),
    ProviderSpec("ollama", "本地 Ollama", "llm", "local",
                 default_model="qwen2.5", needs_api_key=False,
                 note="跑在你自己的机器上，不产生 API 费用；需先启动 ollama"),
    ProviderSpec("vllm", "本地 vLLM", "llm", "local",
                 default_model="Qwen/Qwen2.5-7B-Instruct", needs_api_key=False,
                 note="跑在你自己的机器上，不产生 API 费用；需先启动 vLLM"),
    # ---- 聚合网关：一个 key 通多家模型（OpenAI 兼容）----
    #
    # 为什么单列这几家：它们不是模型厂，而是**路由器**——用同一个 OpenAI 兼容
    # 端点去调很多家的模型（OpenRouter 甚至能调 Claude/Gemini）。
    # 对本项目这种"想都试一遍再决定"的场景最省事：换模型不用换 key。
    #
    # ⚠️ 它们的 base_url 由 `llm._PRESETS` 提供（与上面各厂同一机制），
    #    key 用**共同的** `llm_api_key`——换网关时只需改 key 与 base_url。
    ProviderSpec("openrouter", "OpenRouter（聚合 300+ 模型）", "llm", "gateway",
                 default_model="", needs_proxy=True,
                 note="一个 key 调多家（含 Claude / Gemini）；模型名形如 anthropic/claude-…"),
    ProviderSpec("groq", "Groq（超快推理）", "llm", "gateway",
                 default_model="", needs_proxy=True,
                 note="主打低延迟；跑开源权重模型"),
    ProviderSpec("together", "Together AI", "llm", "gateway",
                 default_model="", needs_proxy=True, note="开源权重模型托管"),
    ProviderSpec("fireworks", "Fireworks AI", "llm", "gateway",
                 default_model="", needs_proxy=True, note="开源权重模型托管"),
    ProviderSpec("deepinfra", "DeepInfra", "llm", "gateway",
                 default_model="", needs_proxy=True, note="便宜的开源权重推理"),
    ProviderSpec("cerebras", "Cerebras（极速）", "llm", "gateway",
                 default_model="", needs_proxy=True, note="晶圆级芯片，吞吐极高"),
    ProviderSpec("siliconflow", "硅基流动 SiliconFlow", "llm", "gateway",
                 default_model="", note="国内聚合，一个 key 调多家开源模型"),
    ProviderSpec("custom", "自定义（OpenAI 兼容）", "llm", "gateway", note="自行填写 base_url 与模型"),

    # ================= 文生图 =================
    ProviderSpec("local", "本地 · Z-Image", "image_gen", "local",
                 default_model="z-image-turbo", needs_api_key=False, max_concurrency=1,
                 known_models=("z-image-turbo",),
                 note="跑在你自己机器上的本地推理，不产生 API 费用。"
                      "8GB 显存实测约 32 秒/张；串行处理，一次一张"),
    ProviderSpec("dashscope", "阿里百炼（通义万相 / 千问）", "image_gen", "cn_cloud",
                 default_model="wan2.7-image-pro",
                 # 型号名已按官方模型列表与 API 参考核对（2026-10）：
                 #   https://www.alibabacloud.com/help/en/model-studio/model-list-image-generation
                 #   https://www.alibabacloud.com/help/en/model-studio/text-to-image-v2-api-reference
                 # ⚠️ 通义万相与千问图像是**两个模型族**：万相管文生图，千问管生成+编辑。
                 #    这里两侧都给，让用户按需选；`z-image*` 是本地权重名（走同步接口）。
                 known_models=("z-image-turbo",
                               "wan2.7-image-pro", "wan2.7-image", "wan2.6-t2i", "wan2.6-image",
                               "qwen-image-3.0-pro", "qwen-image-3.0", "qwen-image-2.1-pro"),
                 note="z-image* 走同步接口；其余走异步任务（wan2.5 及更早是旧异步协议）"),
    ProviderSpec("minimax", "MiniMax", "image_gen", "cn_cloud",
                 default_model="image-01", known_models=("image-01",)),
    ProviderSpec("replicate", "Replicate", "image_gen", "gateway",
                 needs_proxy=True,
                 # 取自官方文生图集合页的推荐位（2026-10）：
                 #   https://replicate.com/collections/text-to-image
                 default_model="google/nano-banana-2",
                 known_models=("google/nano-banana-2", "openai/gpt-image-2",
                               "black-forest-labs/flux-2-max", "black-forest-labs/flux-2-pro",
                               "bytedance/seedream-5.0", "google/imagen-4-ultra"),
                 note="需填具体 model version（IMAGE_BASE_URL）"),
    ProviderSpec("wavespeed", "WaveSpeedAI（聚合）", "image_gen", "gateway",
                 available=False, needs_proxy=True,
                 known_models=("openai/gpt-image-1.5", "google/gemini-3-pro-image",
                               "black-forest-labs/flux-2-pro", "bytedance/seedream-4.5",
                               "tencent/hunyuan-image-3.0", "ideogram/ideogram-2.0"),
                 note="一个 key 覆盖数十个模型（v2 待接入）"),

    # ================= 图像编辑 / 崩坏修复 =================
    ProviderSpec("local", "本地 · Z-Image 修复", "image_edit", "local",
                 available=False, needs_api_key=False, max_concurrency=1,
                 known_models=("z-image-inpaint",),
                 note="与本地出图共用权重；该路由尚未接入"),
    ProviderSpec("dashscope", "阿里百炼（千问图像编辑）", "image_edit", "cn_cloud",
                 default_model="qwen-image-3.0-pro",
                 known_models=("qwen-image-3.0-pro", "qwen-image-3.0", "qwen-image-2.1-pro",
                               "qwen-image-edit-max", "qwen-image-edit-plus"),
                 note="指令式编辑：改文字/增删物体/改动作/细节增强，支持 1–3 张多图输入"),

    # ================= 图生视频 =================
    ProviderSpec("local", "本地 · MiniMax H3", "video_gen", "local",
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
    ProviderSpec("ffmpeg", "本地 · 图片拼接", "video_gen", "free",
                 default_model="ffmpeg", needs_api_key=False,
                 presets={"duration": DURATION_BY_PROVIDER["ffmpeg"],
                          "resolution": RESOLUTION_BY_PROVIDER["ffmpeg"]},
                 # 图片拼接：能给首帧，但没有任何运动控制
                 conditioning=("first_frame",),
                 note="图片幻灯片拼接；时长固定、无真实运镜（转场/字幕需自行实现）"),

    # ================= 配音 / 音乐 =================
    ProviderSpec("edge", "本地 · 系统语音", "tts", "free",
                 needs_api_key=False, known_models=("edge-tts",),
                 note="不产生 API 费用；音色较少，适合先把链路跑通"),
    # 模型名按官方 API 参考核对（2026-10）：
    #   https://help.aliyun.com/zh/model-studio/cosyvoice-tts-http-api
    #   ⚠️ CosyVoice 的 HTTP 接口**仅华北2（北京）**可用，跨区调用会失败。
    #   cosyvoice-v1 已不在取值范围。
    ProviderSpec("dashscope", "阿里 CosyVoice", "tts", "cn_cloud",
                 default_model="cosyvoice-v3.5-plus",
                 known_models=("cosyvoice-v3.5-plus", "cosyvoice-v3.5-flash",
                               "cosyvoice-v3-plus", "cosyvoice-v3-flash", "cosyvoice-v2"),
                 note="中文效果强；HTTP 接口仅华北2（北京）可用"),
    # https://platform.minimax.io/docs/api-reference/speech-t2a-http
    ProviderSpec("minimax", "MiniMax 语音", "tts", "cn_cloud",
                 default_model="speech-2.8-hd",
                 known_models=("speech-2.8-hd", "speech-2.8-turbo"),
                 note="可与现有 MiniMax Key 共用；speech-2.6 / 02 / 01 系列官方列为 Legacy"),
    # https://elevenlabs.io/docs/api-reference/text-to-speech/convert
    #   ⚠️ ElevenLabs 的参数名是 `model_id`，不是 `model`。
    #   eleven_v4 / eleven_v4_turbo 挂在 Text to Dialogue 接口下，
    #   官方**未写明** /v1/text-to-speech 是否接受它们，所以不放进来。
    ProviderSpec("elevenlabs", "ElevenLabs", "tts", "overseas_cloud",
                 needs_proxy=True,
                 default_model="eleven_multilingual_v2",
                 known_models=("eleven_multilingual_v2", "eleven_v3",
                               "eleven_flash_v2_5", "eleven_flash_v2"),
                 note="情感与音色最丰富；参数名是 model_id 不是 model"),

    # ================= 白模预演 =================
    #
    # ⚠️ 标注从"2D 俯视示意图"改成 3D —— 因为**实际实现就是 3D**：
    # 前端 `web/src/previs/` 用 Three.js 渲染体块场景（Scene3D / bodySpecMesh），
    # 后端 `white_model.py` 出体块 JSON 规格、`whitebox_video.py` 把逐帧导出编码成 mp4。
    # 后端**已无** PIL 画俯视图那条路。
    #
    # 能力名与提供者 key 仍是 `render_2d` / `local`（改名会牵动前端类型与测试）；
    # 这里只把**给用户看的文字**改对。
    ProviderSpec("local", "本地 · 程序绘制", "render_2d", "local",
                 available=True, needs_api_key=False, max_concurrency=1,
                 note="3D 体块预演（前端 Three.js 渲染 + 逐帧导出）；零新依赖、不产生 API 费用"),
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
                has_native_audio=vp.has_native_audio,
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


#: 属于"本地 / 零成本"的类别。
#:
#: 为什么把 `free` 也算本地：`edge`（Edge TTS）与 `ffmpeg`（本地拼接）
#: 都跑在本机或免费服务上、**不需要 API Key**，与用户理解的"本地"一致；
#: 而 `local`（local-gen）才是自建推理服务。三者都是"零成本、不用填 Key"。
LOCAL_CATEGORIES: frozenset[str] = frozenset({"local", "free"})


def is_local_provider(capability: str, provider_key: str) -> bool:
    """该提供者是否属于"本地 / 零成本"那一档。

    未知提供者返回 False（当作云端）——**宁可多提示一次密钥，
    也不要让用户以为不用填**。
    """
    spec = get(capability, provider_key)
    return bool(spec and spec.category in LOCAL_CATEGORIES)


#: 提供者字段里表示"**不指定、跟随模式**"的占位值。
#:
#: 注册表里用过两种写法，含义**完全相同**：
#:   · `(自动)`   —— LLM / 图像 / TTS 的提供者字段
#:   · `(按模式)` —— 视频的 `video_provider`
#:
#: ⚠️ 这必须是**唯一**的一份定义。原先 `resolve_provider` 用 `if v:`
#:    判断"有没有设过覆盖"，而 `"(自动)"` 是**非空字符串**，于是占位符被
#:    当成了一个**真正的提供者名**送去解析。实测：
#:
#:        resolve_provider("llm", "writer")
#:          没设覆盖   → ('deepseek', '来自「云端 API」模式')      ✅
#:          设了 (自动) → ('(自动)',  '来自「writer」的职业级设置') ❌
#:
#:    也就是说「(自动)」这个选项**一直是坏的** —— 选了它界面看着没问题，
#:    真跑起来会拿 `(自动)` 当模型提供者去调，必然失败。
PLACEHOLDER_PROVIDERS: frozenset[str] = frozenset({"(自动)", "(按模式)"})


def is_placeholder(provider_key: object) -> bool:
    """该值是不是"不指定、跟随模式"的占位符（而不是一个真提供者）。"""
    return str(provider_key or "").strip() in PLACEHOLDER_PROVIDERS


def providers_for_mode(capability: str, mode: str) -> list[ProviderSpec]:
    """按「云端 / 本地」模式筛选该能力的提供者。

    ## 为什么需要这道筛选（用户报的问题）

    用户原话："把下滑时的本地模型和云端模型选项分开，不要杂在一起，
    不然会出现上面选的本地，然后下面填 api 情况"。

    原先 `llm_provider_*` 的下拉把 `ollama` / `vllm`（本地）与
    `deepseek` / `openai`（云端）**混在一个列表里**，而模式字段
    （`llm_provider_mode`）就在它上方 —— 选「本地」却挑到云端提供者，
    再去填 API Key，是个**自相矛盾的组合**。

    `mode` 取值（见 `MODE_KEY_BY_CAPABILITY`）：
        "local"  → 只返回本地 / 零成本（`LOCAL_CATEGORIES`）
        "cloud"  → 只返回云端与网关
        其它（"unset" / 空）→ **返回全部**，因为用户还没选，
                              这时不该限制他先看什么
    """
    items = for_capability(capability)
    m = (mode or "").strip().lower()
    if m == "local":
        return [p for p in items if p.category in LOCAL_CATEGORIES]
    if m == "cloud":
        return [p for p in items if p.category not in LOCAL_CATEGORIES]
    return items


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


def provider_payload(p: "ProviderSpec", *, credential_keys: list[str] | None = None) -> dict:
    """**唯一的** ProviderSpec → dict 序列化实现。

    ## 为什么必须只有一份（这里修过一次真漂移）

    原先有**两处**各自把 `ProviderSpec` 摊成字典：

      · `providers.to_payload()`               —— 我给设置页加字段时改的是这里
      · `app/main.py` 的 `/api/providers` 端点 —— 前端实际读的是**这个**

    于是我给 `to_payload` 加了 `is_local` / `api_style`，
    但前端的 `/api/providers` 里**根本没有这两个字段**，前端读到的
    `undefined`，按模式分组的逻辑完全不生效。实测才发现。

    这正是本项目屡次吃亏的"两份手写清单漂移"。现在两处都调本函数，
    字段集只有一处可改。

    Args:
        p: 提供者规格。
        credential_keys: 该提供者需要在设置页填的凭据键。
            视频的密钥是**提供者级**的（`minimax_api_key` 等），
            与 LLM/图像的 `*_api_key_{角色}` 命名不同，所以由调用方传入
            （只有 video_adapters 知道自己的 key 名）。
    """
    return {
        "key": p.key, "label": p.label, "capability": p.capability,
        "category": p.category, "available": p.available,
        # 前端据此把"本地 / 零成本"与"云端"**分开**渲染。
        # 用户 2026-10-05："把下滑时的本地模型和云端模型选项分开，
        # 不要杂在一起，不然会出现上面选的本地，然后下面填 api 情况"。
        "is_local": p.category in LOCAL_CATEGORIES,
        # 协议风格：非 openai 的客户端（langchain ChatOpenAI，请求
        # {base_url}/chat/completions）调不了，前端不该列出来。
        "api_style": p.api_style,
        "default_model": p.default_model, "known_models": list(p.known_models),
        "needs_api_key": p.needs_api_key, "needs_proxy": p.needs_proxy,
        "max_concurrency": p.max_concurrency,
        "presets": {k: list(v) for k, v in p.presets.items()},
        "note": p.note,
        # 前端据此过滤「声音来源」的可选项（没原生音频就不该让人选它）
        "has_native_audio": p.has_native_audio,
        # **这台机器上现在能不能用**（本机能力探测的结果）。
        #
        # ⚠️ 与上面的 `available` 完全不同：`available` 是"**代码里接没接**"
        # （WaveSpeedAI 还没接 → False），这个是"**本机跑不跑得起来**"。
        # 没有本地模型的用户不该在下拉里看到「本地 Ollama」——
        # 选了要跑到那一步才失败，报的还是技术性错误。用户报过这个：
        #     "直接不列，如果有你觉得怎么识别"
        "runtime_ready": _runtime_ready(p),
        "credential_keys": list(credential_keys or []),
    }


def _runtime_ready(p: "ProviderSpec") -> bool:
    """本机能力探测（带缓存）。探测模块不可用时**保守返回 False**。

    保守的理由：探测模块本身出问题（比如导入失败）时，宁可少列几个
    本地选项，也不要把一堆用不了的选项摆给用户。
    """
    try:
        from app.services import local_probe

        return bool(local_probe.is_ready(p.capability, p.key, category=p.category))
    except Exception:  # noqa: BLE001
        return p.category not in ("local", "free")


def to_payload() -> list[dict]:
    """供接口下发（前端据此渲染下拉，不再自带清单）。

    ⚠️ 只是 `provider_payload()` 的薄封装——**不要再在这里另写字段**，
    否则又会与 `/api/providers` 漂移（见 `provider_payload` 的说明）。
    这里不传 `credential_keys`：视频的密钥名只有 video_adapters 知道，
    端点会自己补上。
    """
    return [provider_payload(p) for p in PROVIDERS]



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
#   2. 默认云端 → 用户没意识到会产生费用（视频按量计费金额可观）；
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
    "tts": {"cloud": "dashscope", "local": "edge"},
}

#: 能力 -> 对应的"模式"设置键（运行时设置里的键名）。
#: 命名规则统一为 `{具体提供者的键}_mode`，便于记忆与自动派生。
MODE_KEY_BY_CAPABILITY: dict[str, str] = {
    "llm": "llm_provider_mode",
    "image_gen": "image_provider_mode",
    "image_edit": "image_provider_mode",          # 编辑与生成共用同一个模式
    "video_gen": "video_provider_mode",
    "tts": "tts_provider_mode",
}

#: 能力 -> 对应的"具体提供者"设置键
PROVIDER_KEY_BY_CAPABILITY: dict[str, str] = {
    "llm": "llm_provider",
    "image_gen": "image_provider",
    "image_edit": "image_provider",
    "video_gen": "video_provider",
    "tts": "tts_provider",
}

MODE_LABELS: dict[str, str] = {
    "cloud": "云端 API",
    "local": "本地推理",
    "unset": "未选择",
}

#: 目前**真正被调用**的能力。
#:
#: ⚠️ 为什么需要这个集合：`MODE_KEY_BY_CAPABILITY` 里的能力，并非每个都在代码里
#: 有调用点 —— 有的只是注册表声明。
#: 前端却会把它们和 llm/image/video 一起标成"（必选）"、并警告"未选择会报错"，
#: 这**是假的**：没选也不会报错，因为根本没人用。
#: 结果是用户被推着去配置一个不影响任何行为的模式（还可能要填密钥）。
#: 这里如实下发"是否已接入"，让前端只对**真的会用到的**提示必选。
#: 将来接入某个能力时把这个集合补上即可（前端会自动跟着变）。
#:
#: 注：`lora_train` 已按用户要求整体删除（LoRA 训练属于"本地部署模型自己的事"，
#: 不在本项目范围内），所以这个集合里也不会再有它。
IMPLEMENTED_CAPABILITIES: frozenset[str] = frozenset({
    "llm", "image_gen", "image_edit", "video_gen",
})


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

    mode = str(rs_get(mkey) or "unset").strip().lower()

    # ⚠️ 占位符（`(自动)` / `(按模式)`）表示"**不指定、跟随模式**"，
    #    必须**跳过**、继续往下走落到模式默认。
    #
    #    原先写的是 `if v:` —— 而 `"(自动)"` 是非空字符串，于是它被当成
    #    一个真提供者名返回。实测：
    #        resolve_provider("llm", "writer")
    #          没设覆盖   → ('deepseek', '来自「云端 API」模式')      ✅
    #          设了 (自动) → ('(自动)',  '来自「writer」的职业级设置') ❌
    #    选了「(自动)」的用户，实际会拿 `(自动)` 当 provider 去调，必然失败。
    if role:
        v = rs_get(f"{pkey}_{role}")
        if v and not is_placeholder(v):
            return _check_mode_consistency(
                capability, str(v), mode, f"来自「{role}」的职业级设置"
            )

    v = rs_get(pkey)
    if v and not is_placeholder(v):
        return _check_mode_consistency(capability, str(v), mode, "来自全局设置")

    default = mode_default_provider(capability, mode)
    if default:
        return default, f"来自「{MODE_LABELS.get(mode, mode)}」模式"

    return None, (
        f"未选择「{capability}」的提供者。请先在设置里选择 **云端 API** 或 **本地推理**"
        f"（对应设置项：{mkey}），这样系统才知道该走哪条路，避免猜错产生意外费用或直接失败。"
    )


def _check_mode_consistency(
    capability: str, provider: str, mode: str, why: str
) -> tuple[str | None, str]:
    """校验「模式」与「提供者」是否自相矛盾。

    ## 为什么要在后端也挡一层（前端已经按模式筛选了下拉）

    用户报的问题是"上面选的本地，然后下面填 api"。前端把两个下拉按模式分开
    能防住大多数情况，但**存过的旧值**、**直接调接口**、**以后新加的入口**
    都可能绕过前端。配置矛盾时最糟的后果不是报错，而是**静默按另一条路跑**：
    用户以为在用本地（零成本），实际发了云端请求（花钱）。

    所以这里做兜底：模式与实际提供者冲突时**明确报错并说清怎么改**，
    而不是默默按提供者跑。

    只在 `mode` 明确为 "local" / "cloud" 时校验；"unset" 不校验
    （用户没表达意图，此时以显式提供者为准）。
    """
    # 占位符不是提供者，交给上层继续解析（见 resolve_provider）
    if is_placeholder(provider):
        return None, f"「{provider}」不是提供者（它表示跟随模式）"

    m = (mode or "").strip().lower()
    if m not in ("local", "cloud"):
        return provider, why

    spec = get(capability, provider)
    if spec is None:
        # 未知提供者不在这里拦（可能是 custom 之类），交给下游解析
        return provider, why

    is_local = spec.category in LOCAL_CATEGORIES
    if m == "local" and not is_local:
        return None, (
            f"配置矛盾：模式选了「本地推理」，但提供者是「{spec.label}」（云端）。"
            f"本地模式只能用不产生 API 费用、免密钥的提供者。"
            f"请把「{MODE_KEY_BY_CAPABILITY.get(capability)}」改成「云端 API」，"
            f"或把提供者改成 {_local_names(capability)}。"
            f"（不替你猜：猜本地会失败，猜云端会产生你没预期的费用）"
        )
    if m == "cloud" and is_local:
        return None, (
            f"配置矛盾：模式选了「云端 API」，但提供者是「{spec.label}」（本地）。"
            f"云端模式下不会调用本地服务。"
            f"请把「{MODE_KEY_BY_CAPABILITY.get(capability)}」改成「本地推理」，"
            f"或把提供者改成云端提供者。"
        )
    return provider, why


def _local_names(capability: str) -> str:
    """该能力下本地提供者的可读名，用于报错引导。"""
    names = [p.label for p in for_capability(capability) if p.category in LOCAL_CATEGORIES]
    return " / ".join(names) if names else "本地提供者"


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

    UI 应把它们放在最显眼位置：对**已接入的能力**来说这是必须先做的选择。

    每项带一个 `implemented` 字段：告知该能力**当前是否真的有调用点**。
    前端据此只对已接入的提示"必选"——没有调用点的能力若也标成必选，
    会让用户白填一堆配置（见 `IMPLEMENTED_CAPABILITIES` 的说明）。
    """
    out = []
    for cap in ("llm", "image_gen", "video_gen", "tts"):
        mkey = MODE_KEY_BY_CAPABILITY[cap]
        modes = _MODE_DEFAULT.get(cap, {})
        out.append({
            "capability": cap,
            "mode_key": mkey,
            "provider_key": PROVIDER_KEY_BY_CAPABILITY[cap],
            "mode_options": ["unset", *modes.keys()],
            "mode_labels": MODE_LABELS,
            "mode_defaults": {m: p for m, p in modes.items()},
            "implemented": cap in IMPLEMENTED_CAPABILITIES,
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
