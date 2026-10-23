"""模型规格 —— 双通道（API / 本地）的统一抽象。

设计前提（回应"支持用户选择模型 API / 本地双通道"）
---------------------------------------------------
**同一个模型，可以走不同的通道；同一个通道，可以承载不同的模型。**
二者必须正交，否则每加一个模型就要写一套代码。

因此本模块把"模型"拆成两个独立维度：

    模型（干什么、擅长什么）
      ├── TaskKind       任务域：文生图 / 图生视频 / 文本 …
      ├── Capability     能力画像：中文文字渲染、原生音轨、最大分辨率 …
      └── ResourceProfile 资源画像：显存峰值、是否需 offload、磁盘占用
            ▲
            │  与"在哪跑"完全解耦
            ▼
    通道（在哪跑、怎么调）
      └── Backend
            ├── COMFYUI     本地节点图执行：平台编译出图 → POST /prompt
            ├── HTTP        直连 HTTP：平台组装请求体 → POST <endpoint>
            │                （本地服务与云端 API **同属这一类**，只是 endpoint 不同）
            └── PROCESS     本地进程内调用（预留）

**关键设计：本地与云端 API 不是两种通道，是同一种通道的两个 endpoint。**
`D:\\AI\\local-gen` 暴露的是与云端等价的 OpenAI 兼容 HTTP 接口，
所以它和 DeepSeek / 通义 走的是同一段代码路径 —— 换端点即换通道形态。
只有 ComfyUI 因为需要"编译成节点图"才是真正不同的通道。

依赖方向
--------
``models`` 只依赖 ``ir`` 与 ``governance``。**不依赖** ``capability`` 与 ``compiler``。
`capability` 是"环境事实"（有哪些 class_type），`models` 是"模型声明"（想用哪些模型）；
把二者绑在一起会让"换模型"必须改环境探测逻辑。
二者的**交汇点在路由层**（``compiler/router.py``），那里同时看见两者。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

__all__ = [
    "TaskKind",
    "BackendKind",
    "Maturity",
    "Capability",
    "ResourceProfile",
    "CostProfile",
    "Backend",
    "PerformanceRecord",
    "ModelSpec",
    "TaskProfile",
    "ModelRegistry",
]


# ==========================================================================
# 枚举
# ==========================================================================

class TaskKind(str, Enum):
    """任务域。路由的第一层筛选。"""

    TEXT_COMPLETE = "text.complete"            # 通用文本
    TEXT_STRUCTURED = "text.structured"        # 要求严格结构化输出（分镜 JSON 等）
    TEXT_LONG_CONTEXT = "text.long_context"    # 超长上下文（整本小说）
    TEXT_REASONING = "text.reasoning"          # 推理型（剧评、连续性检查）
    VLM_JUDGE = "vlm.judge"                    # 视觉打分（审片官）

    IMAGE_T2I = "image.text_to_image"          # 文生图
    IMAGE_EDIT = "image.edit"                  # 图像编辑 / 局部重绘
    IMAGE_UPSCALE = "image.upscale"             # 超分

    VIDEO_I2V = "video.image_to_video"          # 图生视频
    VIDEO_T2V = "video.text_to_video"           # 文生视频
    VIDEO_REF2V = "video.reference_to_video"    # 参考图生视频（锁角色）

    AUDIO_TTS = "audio.tts"                     # 语音合成
    AUDIO_MUSIC = "audio.music"                 # 音乐生成

    @property
    def domain(self) -> str:
        return self.value.split(".")[0]


class BackendKind(str, Enum):
    """执行通道。这是"双通道"的落点。"""

    #: 本地节点图执行。平台把 IR 编译成节点图，POST /prompt 提交。
    COMFYUI = "comfyui"

    #: 直连 HTTP 接口。**本地服务与云端 API 同属此类**，仅 endpoint 不同。
    HTTP = "http"

    #: 本地进程内调用（预留；如直接 import diffusers 管线）。
    PROCESS = "process"


class Maturity(str, Enum):
    """成熟度。用于路由时的风险偏好。"""

    PRODUCTION = "production"
    BETA = "beta"
    EXPERIMENTAL = "experimental"
    DEPRECATED = "deprecated"


# ==========================================================================
# 画像
# ==========================================================================

@dataclass(frozen=True)
class Capability:
    """能力画像。路由的硬性筛选依据。

    **只放"能否做到"这类布尔/数值事实，不放"做得好不好"** ——
    后者属 ``PerformanceRecord``，因为它依赖测量环境。
    """

    max_resolution: tuple[int, int] | None = None
    max_duration_s: float | None = None
    #: 中文文字渲染能力（0~1）。某些模型渲染中文招牌/信件会糊成乱码。
    chinese_text_score: float = 0.0
    supports_negative_prompt: bool = False
    supports_reference_image: bool = False
    #: 支持**多张**参考图。H3 的 ref2v 走动态端口可挂任意数量。
    supports_multi_reference: bool = False
    native_audio: bool = False          # 原生输出音轨
    supports_lora: bool = False
    supports_controlnet: bool = False
    #: 上下文窗口（token）。仅文本模型相关。
    context_tokens: int | None = None
    #: 结构化输出可靠性（0~1）。JSON 模式实测稳定性。
    structured_output_score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "maxResolution": list(self.max_resolution) if self.max_resolution else None,
            "maxDurationS": self.max_duration_s,
            "chineseTextScore": self.chinese_text_score,
            "supportsNegativePrompt": self.supports_negative_prompt,
            "supportsReferenceImage": self.supports_reference_image,
            "supportsMultiReference": self.supports_multi_reference,
            "nativeAudio": self.native_audio,
            "supportsLora": self.supports_lora,
            "supportsControlnet": self.supports_controlnet,
            "contextTokens": self.context_tokens,
            "structuredOutputScore": self.structured_output_score,
        }


@dataclass(frozen=True)
class ResourceProfile:
    """资源画像。**本地通道的硬约束**。

    本机实测（RTX 4060 Laptop / 8 GB 显存 / 15.8 GB 内存）：
    瓶颈是**内存不是显存**，且同一时刻只能加载一个模型。
    """

    vram_peak_mb: int = 0
    #: 必须常驻显存的部分（如 ControlNet 不支持 offload）。0 = 全部可 offload。
    vram_resident_mb: int = 0
    ram_peak_mb: int = 0
    disk_gb: float = 0.0
    #: 是否需要 offload（主模型+编码器之和 > 显存）
    needs_offload: bool = False
    #: 是否要求独占 GPU（本机所有模型都要求：同时只能加载一个）
    exclusive_gpu: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "vramPeakMb": self.vram_peak_mb,
            "vramResidentMb": self.vram_resident_mb,
            "ramPeakMb": self.ram_peak_mb,
            "diskGb": self.disk_gb,
            "needsOffload": self.needs_offload,
            "exclusiveGpu": self.exclusive_gpu,
        }


@dataclass(frozen=True)
class CostProfile:
    """成本画像。**API 通道的硬约束**。"""

    #: "local" = 仅电费；"metered" = 按量计费
    kind: str = "local"
    #: 每次调用的现金成本（元）。local 为 0。
    cny_per_call: float = 0.0
    #: 按秒计费时的单价（元/秒）。视频类常见。
    cny_per_second: float = 0.0
    #: 按 token 计费时的单价（元/千 token）。
    cny_per_1k_tokens: float = 0.0
    #: 本地运行的功耗成本（元/小时），用于统一口径比较。
    power_cny_per_hour: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "cnyPerCall": self.cny_per_call,
            "cnyPerSecond": self.cny_per_second,
            "cnyPer1kTokens": self.cny_per_1k_tokens,
            "powerCnyPerHour": self.power_cny_per_hour,
        }


@dataclass(frozen=True)
class Backend:
    """执行通道声明。

    **同一个模型可以有多条通道**：例如 `z-image-turbo` 既能走本地 ComfyUI
    （``kind=COMFYUI``），也能走 ``local-gen`` 的 HTTP 服务（``kind=HTTP``，
    endpoint 指向 ``http://127.0.0.1:8100``）。用户选本地还是 API，
    改变的是**走哪条通道**，不是改变模型。
    """

    kind: BackendKind
    #: 端点。COMFYUI 填 ``http://127.0.0.1:8188``；HTTP 填服务地址。
    #: **留空表示"用 config 里的默认值"** —— 便于换机器不改代码。
    endpoint: str = ""
    #: 该通道需要的环境变量名（凭据从 config/.env 读，不写进代码）
    api_key_env: str = ""

    # ---- COMFYUI 专有 ----
    #: 编译目标：本模型需要哪些 class_type（值来自规则表，不是硬编码节点名）
    #: 留空 = 由编译规则表按 kind 推导
    required_kinds: tuple[str, ...] = ()
    #: 权重文件（用于编译期校验"文件是否在位"）
    weight_files: tuple[str, ...] = ()
    lora_files: tuple[str, ...] = ()
    #: 编码器加载类型。**同一编码器在不同模型下 type 不同**
    #: （Z-Image 是 lumina2、FLUX.2 是 flux2），搞混就报错。
    clip_type: str = ""

    # ---- HTTP 专有 ----
    #: 请求路径，如 ``/v1/images/generations``
    path: str = ""
    method: str = "POST"
    #: 请求体模板（JSON 字符串，含 ``{占位符}``）。
    #: ⚠️ 必须用 json.dumps 生成再替换，**不要手写 JSON 字符串** ——
    #: 否则提示词里的引号会破坏 JSON 结构。
    body_template: str = ""
    #: **要求严格 JSON 输出**时用的模板（可选）。
    #:
    #: 为什么需要第二个模板：某些厂商（实测 DeepSeek）在启用
    #: ``response_format={"type":"json_object"}`` 时**会强制要求提示词里
    #: 出现 "json" 字样**，否则直接 400。而"要纯文本"和"要 JSON"
    #: 是**两种不同的请求**，一个模板表达不了 ——
    #: 早先只有一个模板且硬编码了 json_object，结果是
    #: **纯文本调用必然 400**（踩过）。
    #:
    #: 留空表示该模型不支持/不需要强 JSON 模式，此时退回 `body_template`。
    body_template_json: str = ""
    #: 响应取值路径，如 ``data.0.url``、``output.0``
    response_path: str = ""
    #: 该接口是异步任务式（提交后需轮询）还是同步返回
    async_task: bool = False
    #: 异步时的状态查询路径模板，如 ``/v1/tasks/{task_id}``
    status_path: str = ""
    #: 异步时的完成状态取值
    status_success_value: str = "succeeded"
    #: 异步时的**失败**状态取值（与 `status_success_value` 对称）。
    #:
    #: 为什么必须声明而不是写死在通道里：各家枚举差别很大 ——
    #: MiniMax 官方用的是 ``Fail``（首字母大写、无 d），而通道里曾硬编码
    #: ``("failed","error","canceled")`` —— 于是 ``Fail`` 落到「还在跑」的分支，
    #: 一个**明确失败**的任务被当成在跑，空转到超时（审计 H-12，30 分钟）。
    #: 匹配一律**大小写不敏感**。
    status_failure_values: tuple[str, ...] = ()
    #: 该通道产物的类型（``image`` / ``video`` / ``audio``）。
    #:
    #: 为什么是声明而不是推断：视频后端（H3）的产物是 `.mp4`，
    #: 而通道曾硬编码 ``kind="image"`` —— 于是 mp4 被算进
    #: `RenderOutcome.images`，门禁拿它当图片去量尺寸（审计 H-10）。
    artifact_kind: str = "image"

    #: **endpoint 留空时的本地性默认判断。**
    #: 语义：这条 HTTP 通道"按设计"是本地服务还是远端 API。
    #: 例：``local-gen`` 的本地服务 -> True（endpoint 由 config 提供）；
    #:     MiniMax 官方 API -> False。
    #: 仅当 ``endpoint`` 为空时生效；endpoint 一旦给出就以它为准。
    is_local_by_default: bool = False

    #: 人类可读备注（用于"选择通道"界面展示）
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "endpoint": self.endpoint,
            "apiKeyEnv": self.api_key_env,
            "requiredKinds": list(self.required_kinds),
            "weightFiles": list(self.weight_files),
            "loraFiles": list(self.lora_files),
            "clipType": self.clip_type,
            "path": self.path,
            "method": self.method,
            "bodyTemplate": self.body_template,
            "responsePath": self.response_path,
            "asyncTask": self.async_task,
            "statusPath": self.status_path,
            "statusSuccessValue": self.status_success_value,
            "statusFailureValues": list(self.status_failure_values),
            "artifactKind": self.artifact_kind,
            "isLocalByDefault": self.is_local_by_default,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PerformanceRecord:
    """实测性能记录。

    ⚠️ **必须标注测量环境**。本机 4060 与第三方 5090 基准差 5~10 倍，
    不标环境就拿别人的数字做预算，实际会超支一个数量级。
    """

    measured_ms: Mapping[str, int] = field(default_factory=dict)   # "1024x1024@8" -> ms
    measured_on: str = ""            # "RTX 4060 Laptop 8GB" / "RTX 5090 Laptop 24GB"
    source: str = ""                 # "local_measured" / "third_party_controlled_ab" / "vendor_claim"

    def as_dict(self) -> dict[str, Any]:
        return {
            "measuredMs": dict(self.measured_ms),
            "measuredOn": self.measured_on,
            "source": self.source,
        }


# ==========================================================================
# 模型规格
# ==========================================================================

@dataclass(frozen=True)
class ModelSpec:
    """一个模型的完整声明。**声明式** —— 新增模型只需加一条注册。"""

    model_id: str
    display_name: str
    task: TaskKind
    #: 可用的通道。**多个通道 = 用户可在本地/API 之间选**
    backends: tuple[Backend, ...]
    capability: Capability = field(default_factory=Capability)
    resource: ResourceProfile = field(default_factory=ResourceProfile)
    cost: CostProfile = field(default_factory=CostProfile)
    performance: PerformanceRecord = field(default_factory=PerformanceRecord)
    #: 提示词纪律档案名（见 docs/02 §5.3）。与 IR 解耦的关键：
    #: IR 只存结构化语义槽位，编译时按档案组装成该模型期望的文本。
    prompt_profile: str = ""
    #: 授权标识，用于 governance.LicenseGate 判定
    license_id: str = ""
    maturity: Maturity = Maturity.BETA
    notes: str = ""

    # ---- 通道查询 ------------------------------------------------------

    def backend(self, kind: BackendKind) -> Backend | None:
        """取指定类型的通道。一个模型可能有多个同类通道（不同 endpoint）。"""
        for b in self.backends:
            if b.kind is kind:
                return b
        return None

    def backend_kinds(self) -> tuple[BackendKind, ...]:
        return tuple(b.kind for b in self.backends)

    @property
    def supports_local(self) -> bool:
        """是否具备本地执行通道。

        判定顺序：
          1. 有 ``COMFYUI`` / ``PROCESS`` 通道 -> 本地（这些按定义就是本地）
          2. 有 ``HTTP`` 通道且 endpoint **显式**指向本机 -> 本地
          3. 有 ``HTTP`` 通道但 endpoint **留空** -> 由 ``is_local_by_default`` 决定

        ⚠️ 第 3 条是必需的：``endpoint`` 留空的语义是"用 config 提供的默认值"。
        若把留空一律当成"非本地"，就会把一个部署在 ``127.0.0.1:8100`` 的
        本地服务误判为云端 API —— 于是"用户选本地"时它被错误排除。
        """
        return self._has_http_local() or any(
            b.kind in (BackendKind.COMFYUI, BackendKind.PROCESS)
            for b in self.backends
        )

    @property
    def supports_api(self) -> bool:
        """是否具备远端 API 通道。"""
        for b in self.backends:
            if b.kind is not BackendKind.HTTP:
                continue
            if _looks_local(b.endpoint):
                continue
            # endpoint 留空且默认本地 -> 不算远端 API
            if not b.endpoint and b.is_local_by_default:
                continue
            return True
        return False

    def _has_http_local(self) -> bool:
        for b in self.backends:
            if b.kind is not BackendKind.HTTP:
                continue
            if b.endpoint:
                if _looks_local(b.endpoint):
                    return True
            elif b.is_local_by_default:
                return True
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "modelId": self.model_id,
            "displayName": self.display_name,
            "task": self.task.value,
            "backends": [b.as_dict() for b in self.backends],
            "capability": self.capability.as_dict(),
            "resource": self.resource.as_dict(),
            "cost": self.cost.as_dict(),
            "performance": self.performance.as_dict(),
            "promptProfile": self.prompt_profile,
            "licenseId": self.license_id,
            "maturity": self.maturity.value,
            "notes": self.notes,
        }


def _looks_local(endpoint: str) -> bool:
    """判定 endpoint 是否指向本机。用于区分"本地 HTTP 服务"与"云端 API"。"""
    if not endpoint:
        return False
    e = endpoint.lower()
    return any(h in e for h in ("127.0.0.1", "localhost", "0.0.0.0", "::1",
                                "192.168.", "10.", "host.docker.internal"))


# ==========================================================================
# 任务档案（路由的需求侧）
# ==========================================================================

@dataclass(frozen=True)
class TaskProfile:
    """任务需求。路由算法据此在模型集合中求解。

    ``must`` 是硬约束（不满足直接出局），``prefer`` 是软偏好（用于排序）。
    """

    task: TaskKind
    #: 硬约束：能力画像的字段名 -> 要求值。用 ``{"native_audio": True}`` 这类断言表达。
    must: Mapping[str, Any] = field(default_factory=dict)
    #: 软偏好：字段名 -> (权重, 方向)。方向 "min" 表示越小越好。
    prefer: Mapping[str, tuple[float, str]] = field(default_factory=dict)
    #: 是否要求本地执行（True = 只用本地通道；False = 只用 API；None = 不限）
    channel: str | None = None          # "local" | "api" | None
    #: 成本上限（元）。0 表示不限。
    budget_cny: float = 0.0
    #: 渲染阶段。草稿档可放宽质量要求换速度。
    phase: str = "final"                # "draft" | "final"

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.value,
            "must": dict(self.must),
            "prefer": {k: list(v) for k, v in self.prefer.items()},
            "channel": self.channel,
            "budgetCny": self.budget_cny,
            "phase": self.phase,
        }


# ==========================================================================
# 注册表
# ==========================================================================

class ModelRegistry:
    """模型目录。**新增模型 = 加一条注册，不改平台代码。**

    与 ``CapabilityMatrix`` 的分工：
      - ``CapabilityMatrix`` 是**环境事实**（目标 ComfyUI 有哪些 class_type）
      - ``ModelRegistry`` 是**模型声明**（我们想用哪些模型、怎么调它们）
    """

    def __init__(self, specs: Sequence[ModelSpec] = ()) -> None:
        self._specs: dict[str, ModelSpec] = {}
        for s in specs:
            self.register(s)

    # ---- 注册与查询 ----------------------------------------------------

    def register(self, spec: ModelSpec) -> None:
        if spec.model_id in self._specs:
            raise ValueError(f"模型 {spec.model_id!r} 已注册")
        if not spec.backends:
            raise ValueError(f"模型 {spec.model_id!r} 未声明任何通道（backends 为空）")
        self._specs[spec.model_id] = spec

    def get(self, model_id: str) -> ModelSpec | None:
        return self._specs.get(model_id)

    def all(self) -> list[ModelSpec]:
        return sorted(self._specs.values(), key=lambda s: s.model_id)

    def by_task(self, task: TaskKind) -> list[ModelSpec]:
        return [s for s in self.all() if s.task is task]

    def by_channel(self, channel: str) -> list[ModelSpec]:
        """按通道形态筛选。"local" / "api"。"""
        if channel == "local":
            return [s for s in self.all() if s.supports_local]
        if channel == "api":
            return [s for s in self.all() if s.supports_api]
        raise ValueError(f"未知通道形态 {channel!r}，应为 'local' 或 'api'")

    def tasks(self) -> list[TaskKind]:
        return sorted({s.task for s in self._specs.values()}, key=lambda t: t.value)

    def summary(self) -> dict[str, Any]:
        """一份可直接展示给用户的模型目录摘要（用于"选择模型"界面）。"""
        return {
            "modelCount": len(self._specs),
            "tasks": {
                t.value: [
                    {
                        "modelId": s.model_id,
                        "displayName": s.display_name,
                        "channels": [b.kind.value for b in s.backends],
                        "local": s.supports_local,
                        "api": s.supports_api,
                        "licenseId": s.license_id,
                        "maturity": s.maturity.value,
                    }
                    for s in self.by_task(t)
                ]
                for t in self.tasks()
            },
        }
