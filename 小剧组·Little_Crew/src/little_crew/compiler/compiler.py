"""IR → ComfyUI 执行图的编译器。

对应设计文档 §3.5 / §7.1 / §7.4
--------------------------------
本模块是"双层同构 DAG"的落地处：把语义完整的 Unified IR 编译为 ComfyUI 可执行的
API 格式图，并在环境能力不足时沿降级链回退，产出 **显式降级通知**。

编译契约
--------
  输入：UnifiedIR + CapabilityMatrix + COMPILE_RULE_VERSION
  输出：CompileResult { graph, notices[], warnings[], graphHash, sourceRefs }
  纯度：编译是**纯函数** —— 同一 (IR, Matrix, RuleVersion) 必得同一 graph 与 hash
  失败：四类结构化错误 UNKNOWN_KIND / MISSING_REQUIRED_NODE / DANGLING_REF / PORT_MISMATCH

与"模板填充"范式的根本差异
--------------------------
模板填充通过**固定节点 ID** 注入参数：目标环境节点 ID 一变即静默失效或静默产错图。
本编译器按 **kind → classType 映射 + 能力矩阵校验 + 降级链** 工作，因此：
  1. 节点缺失时可自动降级（如 IPAdapter -> LoRA 并提高权重）
  2. 降级必然产出 DegradationNotice，携带影响评估与修复建议
  3. 阈值随降级自动调整（如身份相似度 0.82 -> 0.74），使门禁与新能力匹配
  4. 文件型输入（LoRA/模型）在编译期即校验是否在目标环境
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

# 依赖方向：compiler -> {capability, ir}（单向，见 platform/ARCHITECTURE.md）
from little_crew.capability.capability import (
    CapabilityMatrix,
    Drift,
    DriftKind,
    NodeSpec,
    PortRequirement,
    Severity,
)
from little_crew.capability.comfy_interfaces import (
    IMAGELIKE_INPUT_DTYPES,
    THIRD_PARTY_NODES_IN_USE,
    DeterminismClass,
)
from little_crew.ir.canonical import canonical_json, content_hash, derive_seed, normalize

__all__ = [
    "COMPILE_RULE_VERSION",
    "CompileErrorCode",
    "CompileError",
    "NoticeKind",
    "DegradationNotice",
    "CompileRule",
    "CompileRuleTable",
    "CompileResult",
    "ComfyCompiler",
    "DEFAULT_RULES",
]


COMPILE_RULE_VERSION = "little_crew.compile/v1"


# ==========================================================================
# 错误与通知
# ==========================================================================

class CompileErrorCode(str):
    UNKNOWN_KIND = "UNKNOWN_KIND"
    MISSING_REQUIRED_NODE = "MISSING_REQUIRED_NODE"
    DANGLING_REF = "DANGLING_REF"
    PORT_MISMATCH = "PORT_MISMATCH"
    MISSING_BINDING = "MISSING_BINDING"
    CYCLE = "CYCLE"
    PORT_TYPE_CONFLICT = "PORT_TYPE_CONFLICT"
    #: 图能构造出来但**结构不完整**：必需输入未被填充。
    #:
    #: 单独成一类，因为这类失败**本地校验发现不了** —— 只有真实 ComfyUI
    #: 会以 `required_input_missing` 拒绝。端到端验证抓出的缺陷正是这一类，
    #: 所以必须在编译期就拦下（见 compile() 的 3.5) 产出完整性终检）。
    INCOMPLETE_GRAPH = "INCOMPLETE_GRAPH"


@dataclass
class CompileError(Exception):
    code: str
    message: str
    ir_node_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        loc = f" @{self.ir_node_id}" if self.ir_node_id else ""
        return f"[{self.code}]{loc} {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message,
                "irNodeId": self.ir_node_id, "detail": dict(self.detail)}


class NoticeKind(str):
    DEGRADED = "DEGRADED"                        # 沿降级链替换了实现
    SKIPPED_OPTIONAL = "SKIPPED_OPTIONAL"        # 可选节点无法实现，已跳过
    SUBSTITUTED = "SUBSTITUTED"                  # 端口/取值被替换
    THRESHOLD_ADJUSTED = "THRESHOLD_ADJUSTED"    # 约束阈值随能力变化自动调整
    AUTO_INJECTED = "AUTO_INJECTED"              # 自动补齐了隐含前置节点
    DRIFT_RECORDED = "DRIFT_RECORDED"            # 记录了环境漂移但不阻断
    #: 缓存键不完整 —— 节点可能有未声明的外部依赖，无法判断"重跑是否真的重跑"
    CACHE_KEY_INCOMPLETE = "CACHE_KEY_INCOMPLETE"


@dataclass
class DegradationNotice:
    """降级通知。**必须携带影响评估与修复建议** —— 这是与模板填充的分水岭。"""

    kind: str
    ir_node_id: str | None
    severity: str = Severity.WARNING.value
    frm: str = ""
    to: str = ""
    reason: str = ""
    affected_constraints: tuple[str, ...] = ()
    threshold_adjusted: Mapping[str, Any] | None = None
    impact: str = ""
    ui_hint: str = ""
    reversible: bool = True

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "irNodeId": self.ir_node_id,
            "severity": self.severity,
            "from": self.frm,
            "to": self.to,
            "reason": self.reason,
            "impact": self.impact,
            "uiHint": self.ui_hint,
            "reversible": self.reversible,
        }
        if self.affected_constraints:
            d["affectedConstraints"] = list(self.affected_constraints)
        if self.threshold_adjusted:
            d["thresholdAdjusted"] = dict(self.threshold_adjusted)
        return d


# ==========================================================================
# 端口类型
# ==========================================================================

#: ComfyUI 的端口类型名。IR 的输出端口名**直接沿用这套命名**
#: （``MODEL`` / ``CLIP`` / ``IMAGE`` / ``CONDITIONING`` / ``LATENT`` / ``VAE`` / …），
#: 因此可以用它反查"某个 IR 输出端口产出什么类型"。
#:
#: 这个反查存在的理由：**上游被跳过而需要重接时，必须校验类型是否吻合**。
#: 错接会产出语义错误的图（静默错误），留空至少会被 ComfyUI 的必需输入校验拦住
#: （显式失败）—— 两害相权，宁可选后者。
_KNOWN_PORT_TYPES: frozenset[str] = frozenset({
    "MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "IMAGE", "MASK",
    "AUDIO", "VIDEO", "CONTROL_NET", "CLIP_VISION", "CLIP_VISION_OUTPUT",
    "STYLE_MODEL", "GLIGEN", "UPSCALE_MODEL", "SAMPLER", "SIGMAS",
    "GUIDER", "NOISE", "WEBCAM", "HOOKS", "IPADAPTER",
})


# ==========================================================================
# 编译规则表
# ==========================================================================

@dataclass(frozen=True)
class CompileRule:
    """一条 kind → ComfyUI 节点 的编译规则。

    字段说明
    --------
    class_types     候选 class_type，按优先级排列（首选在前）
    degrade_to      全部候选都不可用时的降级目标 kind
    port_map        IR 输入端口 -> ComfyUI 输入名
    param_map       IR params 键 -> ComfyUI 输入名
    defaults        编译时补全的默认 widget 值
    output_ports    ComfyUI 输出端口名序列（用于解析 ref 的 outputIndex）
    required        缺失且无降级路径时是否阻断编译
    requires_model_source
                    该节点需要一个上游 MODEL/CLIP 源；缺失时自动注入
                    CheckpointLoaderSimple（解决"用户觉得理所当然而省略"的漏洞）
    adjust_on_degrade
                    被降级替换时，对 params 做的调整（如提高 LoRA 权重补偿身份锁定）
    """

    kind: str
    class_types: tuple[str, ...]
    port_map: Mapping[str, str] = field(default_factory=dict)
    param_map: Mapping[str, str] = field(default_factory=dict)
    param_templates: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)
    output_ports: tuple[str, ...] = ()
    required: bool = True
    requires_model_source: bool = False
    #: **前置加载器需求**：``{kind: 该加载器提供的输出类型}``。
    #:
    #: 存在的理由：早期规则模型假设"一条规则 = 一个节点"，但真实节点图里存在
    #: **两节点模式** —— 加载器（产出模型/适配器对象）+ 应用器（消费它）。
    #: 典型：``IPAdapterAdvanced`` 需要 ``ipadapter``(IPADAPTER) 输入，
    #: 而该输入**不是模型加载器能提供的**（模型加载器只产出 MODEL/CLIP/VAE）。
    #:
    #: 声明后，编译器会**按需自动注入**该加载器节点并连线 —— 与模型源注入同一机制。
    requires_loader: Mapping[str, str] = field(default_factory=dict)
    degrade_to: str | None = None
    install_hint: str = ""
    adjust_on_degrade: Mapping[str, Any] = field(default_factory=dict)
    #: 该 kind 的约束若依赖此节点，降级时阈值放宽量
    constraint_threshold_relax: Mapping[str, float] = field(default_factory=dict)
    seeded: bool = False


class CompileRuleTable:
    """规则表。以声明式映射实现，与具体 ComfyUI 版本解耦。"""

    def __init__(self, rules: Sequence[CompileRule], *, version: str = COMPILE_RULE_VERSION) -> None:
        self.version = version
        self._rules: dict[str, CompileRule] = {r.kind: r for r in rules}

    def get(self, kind: str) -> CompileRule | None:
        return self._rules.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._rules)

    # ---- 需求生成 ------------------------------------------------------

    #: IR 语义输出端口名 -> ComfyUI 端口类型。用于在编译前精确校验端口类型。
    IR_OUTPUT_DTYPES: Mapping[str, str] = {
        "MODEL": "MODEL", "CLIP": "CLIP", "VAE": "VAE",
        "CONDITIONING": "CONDITIONING", "LATENT": "LATENT", "IMAGE": "IMAGE",
        "AUDIO": "AUDIO", "MASK": "MASK",
    }

    def requirements(
        self,
        ir: Mapping[str, Any],
        *,
        matrix: CapabilityMatrix | None = None,
        files_by_port: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, PortRequirement]:
        """由规则表 + IR 推导出"本次编译对环境的要求"，交给能力矩阵做漂移检测。

        这里不实际编译，只声明需求 —— 因此可以在**编译前**就把环境问题报出来。

        关键设计：``class_types`` 是**备选**而非并列需求。若把每个备选都当作独立
        需求，则"任一备选缺失"都会误判为阻断 —— 这会让降级链完全失效。
        因此本方法按 kind 分组，并在提供 ``matrix`` 时**解析出实际会被选中的那一个**
        class_type，只对它声明需求；若全部备选都不可用，则把该 kind 的首选报告为缺失，
        由编译器的降级链接手。

        ``files_by_port``：端口名 -> 该端口需要的文件值（如 LoRA 文件名）。
        只挂到**确实拥有该端口**的 class_type 上，避免在无关节点上误报文件缺失。
        """
        files_by_port: dict[str, tuple[str, ...]] = dict(files_by_port or {})

        # kind -> {rule, nodes}
        grouped: dict[str, dict[str, Any]] = {}
        for node in ir.get("nodes", []):
            rule = self.get(node["kind"])
            if rule is None:
                continue
            slot = grouped.setdefault(rule.kind, {"rule": rule, "nodes": []})
            slot["nodes"].append(node)

        reqs: dict[str, PortRequirement] = {}

        for kind in sorted(grouped):
            slot = grouped[kind]
            rule: CompileRule = slot["rule"]
            nodes: list[Mapping[str, Any]] = slot["nodes"]
            candidates: tuple[str, ...] = rule.class_types

            # 解析实际选中的实现（沿降级链预演）与**生效规则**
            if matrix is not None:
                chosen, _degraded, eff_rule = self._resolve_target_kind(rule, matrix, self)
            else:
                chosen, eff_rule = (candidates[0] if candidates else None), rule

            ports: dict[str, str] = {}
            enums: dict[str, tuple[str, ...]] = {}
            # 注意：eff_rule 为 None 表示"全部候选与降级链都不可用"。
            # 此时仍必须算出需求，否则能力矩阵无法报告"这个可选能力缺失"。
            # 因此用源规则兜底 —— 需求的用途是**描述期望**，不是描述现实。
            for node in nodes:
                dyn_ports, dyn_enums = self._dynamic_requirements(eff_rule or rule, node)
                ports.update(dyn_ports)
                for k, v in dyn_enums.items():
                    enums[k] = tuple(sorted(set(enums.get(k, ())) | set(v)))

            # 只对选中的 class_type 声明需求；无可用实现时报告首选（触发降级/阻断判定）
            targets: tuple[str, ...] = (chosen,) if chosen else (candidates[:1] or (kind,))

            for ct in targets:
                # 文件型需求只挂到真正拥有该端口的节点上
                ct_files = {p: v for p, v in files_by_port.items() if p in ports}
                if ct in reqs:
                    prev = reqs[ct]
                    merged_files = dict(prev.files)
                    for p, v in ct_files.items():
                        merged_files[p] = tuple(sorted(set(merged_files.get(p, ())) | set(v)))
                    reqs[ct] = PortRequirement(
                        ports={**prev.ports, **ports},
                        enums={**prev.enums, **enums},
                        files=merged_files,
                        required=prev.required or rule.required,
                        install_hint=prev.install_hint or rule.install_hint,
                    )
                else:
                    reqs[ct] = PortRequirement(
                        ports=ports, enums=enums, files=ct_files,
                        required=rule.required, install_hint=rule.install_hint,
                    )
        return reqs

    #: kind -> {ComfyUI 输入名: 期望端口类型}。连接端口由 IR 上游输出类型决定，
    #: 此表用于在编译前就校验端口类型是否匹配（不必等到编译）。
    OUTPUT_DTYPE_BY_PORT: Mapping[str, Mapping[str, str]] = {
        "prompt.text": {"clip": "CLIP", "text": "STRING"},
        "prompt.negative": {"clip": "CLIP", "text": "STRING"},
        "style.bible": {"clip": "CLIP", "text": "STRING"},
        "identity.lora": {"model": "MODEL", "clip": "CLIP", "lora_name": ""},
        "identity.reference": {"model": "MODEL", "image": "IMAGE"},
        "composition.controlnet": {"positive": "CONDITIONING", "negative": "CONDITIONING",
                                   "control_net": "CONTROL_NET", "image": "IMAGE"},
        "latent.from_image": {"pixels": "IMAGE", "vae": "VAE"},
        "sample.ksampler": {"model": "MODEL", "positive": "CONDITIONING",
                            "negative": "CONDITIONING", "latent_image": "LATENT"},
        "decode.vae": {"samples": "LATENT", "vae": "VAE"},
        "io.save_image": {"images": "IMAGE"},
        "video.i2v": {"image": "IMAGE", "model": "MODEL"},
        "video.ref2v": {"image": "IMAGE", "model": "MODEL"},
        "perf.sage_attention": {"model": "MODEL"},
        "prompt.fuse": {"conditioning_to": "CONDITIONING", "conditioning_from": "CONDITIONING"},
        "model.checkpoint": {"ckpt_name": ""},
        "latent.empty": {"width": "INT", "height": "INT", "batch_size": "INT"},
        "audio.tts": {"text": "STRING", "voice": ""},
    }

    def _dynamic_requirements(
        self, rule: CompileRule, node: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """从单个节点的实际取值推导需求（端口 + 枚举取值）。

        注意：``param_map`` 的**键是 IR 参数名，值是 ComfyUI 输入名**。
        需求必须用**值**（ComfyUI 输入名）声明 —— 用键会得到
        "节点上不存在输入端口 sampler" 这类假漂移。
        """
        ports: dict[str, str] = {}
        enums: dict[str, tuple[str, ...]] = {}

        # 连接端口：类型由 OUTPUT_DTYPE_BY_PORT 声明
        dtype_by_port = self.OUTPUT_DTYPE_BY_PORT.get(rule.kind, {})
        for cname in rule.port_map.values():
            ports.setdefault(cname, dtype_by_port.get(cname, ""))
        # widget 端口：默认值字段名即 ComfyUI 输入名
        for pname in rule.defaults:
            ports.setdefault(pname, dtype_by_port.get(pname, ""))
        # 参数映射值（ComfyUI 输入名）
        for cname in rule.param_map.values():
            ports.setdefault(cname, dtype_by_port.get(cname, ""))
        for cname in rule.param_templates.values():
            ports.setdefault(cname, dtype_by_port.get(cname, ""))

        params = node.get("params") or {}
        # 枚举取值：参数值映射到 ComfyUI 输入名后声明
        for pkey, cname in rule.param_map.items():
            val = params.get(pkey)
            if isinstance(val, str) and cname.endswith("_name"):
                enums[cname] = tuple(sorted(set(enums.get(cname, ())) | {val}))

        return ports, enums

    def _resolve_target_kind(
        self,
        rule: CompileRule,
        matrix: CapabilityMatrix,
        rules: "CompileRuleTable",
    ) -> tuple[str | None, str | None, CompileRule | None]:
        """在不实际编译的前提下，预演"会选中哪个 class_type、是否会降级、生效规则是谁"。

        与 ComfyCompiler._select_class_type 保持同一逻辑，但不产生通知对象。
        """
        chosen = matrix.first_available(rule.class_types)
        if chosen is not None:
            return chosen, None, rule
        target = rule.degrade_to
        seen: set[str] = set()
        while target and target not in seen:
            seen.add(target)
            t_rule = rules.get(target)
            if t_rule is None:
                break
            alt = matrix.first_available(t_rule.class_types)
            if alt is not None:
                return alt, rule.kind, t_rule
            target = t_rule.degrade_to
        return None, None, None


#: 平台内置规则表。新增模型/节点只需在此追加规则，不改编译器代码。
DEFAULT_RULES: tuple[CompileRule, ...] = (
    # ---- 前置：模型源 ----------------------------------------------------
    CompileRule(
        kind="model.checkpoint",
        class_types=("CheckpointLoaderSimple", "CheckpointLoader"),
        port_map={},
        param_map={"ckpt_name": "ckpt_name"},
        # ⚠️ 文件名必须与**官方发布名逐字一致**。
        #
        # 这里曾写成 `sdxl_base_1.0.safetensors`（社区简写，去掉了 `sd_xl_` 前缀）——
        # 那是错的：ComfyUI 的 `ckpt_name` 是**枚举**，按磁盘上的真实文件名匹配，
        # 不做任何模糊匹配。错一个字符就是 `FILE_MISSING` 硬阻断。
        #
        # 官方名（HuggingFace 与 ModelScope 一致，文件内嵌 `modelspec.title`
        # 亦为 "Stable Diffusion XL 1.0 Base"）：
        #     sd_xl_base_1.0.safetensors
        defaults={"ckpt_name": "sd_xl_base_1.0.safetensors"},
        output_ports=("MODEL", "CLIP", "VAE"),
        required=True,
        install_hint="CheckpointLoaderSimple 为 ComfyUI 内置节点，缺失说明安装异常",
    ),
    # ---- 模型源（分离加载）：Z-Image --------------------------------------
    #
    # ⚠️ **这是一条独立 kind，不是 `model.checkpoint` 的候选补充。**
    #
    # 原因：Z-Image 在 ComfyUI 里是**分离加载** —— 它没有"一个 checkpoint
    # 包含全部"的形态，而是三个节点各管一段：
    #
    #     UNETLoader  -> MODEL
    #     CLIPLoader  -> CLIP      （type="qwen_image"，Z-Image 归在 Qwen-Image 架构）
    #     VAELoader   -> VAE
    #
    # 而 `model.checkpoint` 的候选只能是**都能被同一个 IR 节点参数化**的加载器
    # （它们读 `ckpt_name`）。把 UNETLoader 塞进那条规则的候选列表里，
    # 会得到一个"能选中但参数名对不上"的假支持。
    #
    # **这是"一条规则 = 一个节点"这一假设第三次成为阻碍**
    # （前两次：IPAdapter 与 ModelPatchLoader 的"加载器 + 应用器"模式）。
    # 解法与那两次一致：把多节点模式显式声明出来（这里是 `requires_loader`），
    # 由编译器负责注入与接线 —— 而不是让一条规则硬扛三个节点。
    CompileRule(
        kind="model.source_zimage",
        class_types=("UNETLoader",),
        port_map={},
        param_map={"unet_name": "unet_name"},
        defaults={"unet_name": "", "weight_dtype": "default"},
        output_ports=("MODEL",),
        required=False,
        #: 分离加载：CLIP 与 VAE 各需一个独立加载器
        requires_loader={
            "model.zimage_clip": "CLIP",
            "model.zimage_vae": "VAE",
        },
        install_hint="UNETLoader 为 ComfyUI 内置节点",
    ),
    CompileRule(
        kind="model.zimage_clip",
        class_types=("CLIPLoader",),
        port_map={},
        param_map={"clip_name": "clip_name", "type": "type"},
        #: Z-Image 的文本编码器是 Qwen3-4B，ComfyUI 里对应 `qwen_image` 类型。
        #  实测确认：`ZImageFunControlnet` 继承自 `QwenImageDiffsynthControlnet`。
        defaults={"clip_name": "", "type": "qwen_image"},
        output_ports=("CLIP",),
        required=False,
        install_hint="CLIPLoader 为 ComfyUI 内置节点",
    ),
    CompileRule(
        kind="model.zimage_vae",
        class_types=("VAELoader",),
        port_map={},
        param_map={"vae_name": "vae_name"},
        defaults={"vae_name": ""},
        output_ports=("VAE",),
        required=False,
        install_hint="VAELoader 为 ComfyUI 内置节点",
    ),
    # ---- 提示词 ---------------------------------------------------------
    CompileRule(
        kind="prompt.text",
        class_types=("CLIPTextEncode", "BNK_CLIPTextEncodeAdvanced"),
        port_map={"clip": "clip"},
        param_map={"text": "text"},
        defaults={"text": ""},
        output_ports=("CONDITIONING",),
        required=True,
    ),
    CompileRule(
        kind="prompt.negative",
        class_types=("CLIPTextEncode",),
        port_map={"clip": "clip"},
        param_map={"text": "text"},
        defaults={"text": ""},
        output_ports=("CONDITIONING",),
        required=True,
    ),
    # ---- 身份锁定：LoRA（MODEL+CLIP 双输出版） ---------------------------
    # ---- 身份锁定：LoRA（MODEL+CLIP 双输出版） ---------------------------
    #
    # ⚠️ `required=False` 是**刻意的**，理由与硬件实测约束直接相关：
    #
    #   角色 LoRA 需要训练。本机 Z-Image 的 fp32 底模需 **22.93 GB 内存**，
    #   而本机只有 **15.8 GB** —— 因此**无法训练角色 LoRA**。
    #   若把 `identity.lora` 标为必需，就等于"平台在这台机器上永远无法出片"，
    #   哪怕参考图（identity.reference）路径完全可用。
    #
    #   所以它必须是**可降级**的：
    #     有 LoRA  -> 用它（身份锁定最强）
    #     无 LoRA  -> 沿 identity.reference（参考图）路径降级，并发显式降级通知
    #
    #   对应地，文件缺失的判定也区分了必需/可选（见 compile() 的 soft_blockers）：
    #   可选规则缺文件是**降级信号**，不是错误。
    CompileRule(
        kind="identity.lora",
        class_types=("LoraLoader",),
        port_map={"model": "model", "clip": "clip"},
        param_map={"lora_name": "lora_name", "strength": "strength_model"},
        defaults={"strength_model": 0.75, "strength_clip": 0.75},
        output_ports=("MODEL", "CLIP"),
        required=False,
        requires_model_source=True,
        degrade_to="identity.lora_model_only",
        install_hint=("LoraLoader 为 ComfyUI 内置节点。若本机无法训练角色 LoRA"
                      "（显存/内存不足），可只用参考图路径，身份锁定强度下降"),
        constraint_threshold_relax={"identity_similarity": 0.10},
    ),
    # 仅改 MODEL 的 LoRA 变体。**契约与上一档不同**：无 clip 输入、无 CLIP 输出，
    # 故必须是独立 kind —— 混在同一 kind 会编译出类型错位的图。
    CompileRule(
        kind="identity.lora_model_only",
        class_types=("LoraLoaderModelOnly",),
        port_map={"model": "model"},
        param_map={"lora_name": "lora_name", "strength": "strength_model"},
        defaults={"strength_model": 0.75},
        output_ports=("MODEL",),
        required=False,
        requires_model_source=True,
        install_hint="LoraLoaderModelOnly 为 ComfyUI 内置节点",
    ),
    # ---- 文件加载：把资产路径变成张量 ------------------------------------
    #
    # 为什么必须有这条规则：
    #   IPAdapterAdvanced.image 的端口类型是 **IMAGE**（张量），不是文件路径。
    #   IR 的 bindings 里存的是路径字符串，**必须经 LoadImage 转换**。
    #   早期版本把路径字符串直接塞进 IMAGE 端口 —— 编译"成功"但真实执行必报错。
    #   这是能力矩阵（端口类型）本应拦住、却因守卫缺失而漏掉的缺陷。
    #
    # 附带收益：LoadImage 自带 IS_CHANGED（对文件内容做 sha256），
    #   因此锚点图换版时 ComfyUI 会**自动重算**下游 —— 与我们的产物时效清单天然互补。
    CompileRule(
        kind="image.file",
        class_types=("LoadImage", "LoadImageOutput"),
        port_map={},
        param_map={"image": "image"},
        defaults={"image": ""},
        output_ports=("IMAGE", "MASK"),
        required=True,
        install_hint="LoadImage 为 ComfyUI 内置节点，缺失说明安装异常",
    ),
    # ---- 控制信号预处理：模糊 --------------------------------------------
    #
    # **为什么模糊能改善白模控制**（实测，见 CHANGELOG alpha.21）：
    # 白模的实心剪影**带内部明暗**，模型会去**描摹骨架的内部细节**
    # （手臂、腿的精确位置），人物因此被驯化成骨架形 —— 就是"完全不像人"。
    # 高斯模糊抹掉内部结构，只留"**这一团是中景偏左的一个人，这么大**"，
    # 形状细节交还给图像模型。**定位信息保住，外观自由度还给模型。**
    #
    # **为什么做成图节点而不是平台侧预处理**：
    #   · 图节点 -> `blur_radius` **自动进入 IR 哈希**，
    #     改模糊半径必然重渲（平台侧预处理则需要额外维护一份参数哈希，
    #     漏了就是"改了参数却命中旧缓存"，而那是无法察觉的）
    #   · 图里看得见做了什么，可追溯
    CompileRule(
        kind="composition.image_blur",
        class_types=("ImageBlur",),
        port_map={"image": "image"},
        param_map={"blur_radius": "blur_radius", "sigma": "sigma"},
        #: 半径 18px @832×480 是实测的平衡点（见 p3_shape_compare.png）；
        #: `sigma` 是高斯标准差，ComfyUI 用它控制衰减。
        defaults={"blur_radius": 18, "sigma": 5.0},
        output_ports=("IMAGE",),
        required=False,
        install_hint="ImageBlur 为 ComfyUI 内置节点，缺失说明安装异常",
    ),
    # ---- 空间控制：Z-Image Fun ControlNet --------------------------------
    #
    # ⚠️ **契约与上面的 `composition.controlnet` 完全不同，必须是独立 kind。**
    #
    #   composition.controlnet        : {positive, negative, control_net, image}
    #                                   -> (CONDITIONING, CONDITIONING)   条件补丁
    #   composition.zimage_controlnet : {model, model_patch, vae, image}
    #                                   -> MODEL                          模型补丁
    #
    # 前者**补条件**，后者**补模型**。混在一个 kind 里会编译出类型错位的图
    # —— 与 `identity.lora` / `identity.lora_model_only` 分家是同一理由。
    #
    # 它还是"加载器 + 应用器"两节点模式的又一例：
    # `ZImageFunControlnet` 需要 `model_patch`(MODEL_PATCH)，
    # 而该类型**不是模型加载器能提供的** —— 由 `composition.model_patch` 提供。
    #
    # 实测依据（2026-10-12，真机跑通）：
    #   白模 Gray 序列 → 本节点 → 机位与姿态跟随白模；
    #   同提示词同种子只换白模，相对基线平均绝对差 35.6~37.8（阈值 8）
    CompileRule(
        kind="composition.zimage_controlnet",
        class_types=("ZImageFunControlnet",),
        # ⚠️ 端口取舍依据**真机抓取的签名**（ComfyUI 0.39.0 的 /object_info）：
        #     required : model(MODEL) model_patch(MODEL_PATCH) vae(VAE) strength(FLOAT)
        #     optional : image(IMAGE) inpaint_image(IMAGE) mask(MASK)
        #                start_percent(FLOAT) end_percent(FLOAT)
        #
        # ⚠️⚠️ **`port_map` 不是"必需端口表"，而是"IR 端口 -> ComfyUI 端口的完整映射表"。**
        #
        # 这里踩过一个代价很高的坑：既然 `image` 在真机里是**可选**的，
        # 就把它从 `port_map` 里删掉了 —— 想表达"它不是必需的"。
        # 但 `_resolve_inputs` **只遍历 `port_map`**：
        #   · 不在 port_map 里 -> IR 声明的连线**被静默丢弃**
        # 后果是产出的图里 `ZImageFunControlnet` **根本没有 image 输入**，
        # 于是 ControlNet 什么都不做 —— 生成结果与"完全不加控制"**逐像素相同**。
        # 而本地编译不报错、图看起来完整、通知也说"已接线"。
        #
        # **"可选"的表达方式是 `required=False` 与 defaults，不是从 port_map 里删。**
        port_map={"model": "model", "vae": "vae",
                  "image": "image",
                  "start_percent": "start_percent",
                  "end_percent": "end_percent"},
        param_map={"strength": "strength"},
        defaults={"strength": 1.0},
        output_ports=("MODEL",),
        required=False,
        requires_model_source=True,
        #: 本规则的实现还需要一个前置加载器节点（kind -> 该加载器的输出类型）
        requires_loader={"composition.model_patch": "MODEL_PATCH"},
        #: **不设降级目标** —— 空间控制没有"退一步还能用"的替代品。
        #  没有 ControlNet 就退化成纯文生图，那是**功能缺失**而不是降级，
        #  如实报缺比假装降级更诚实（`required=False` 已允许整体不放它）。
        degrade_to=None,
        constraint_threshold_relax={"camera_consistency": 0.10},
        install_hint=("ZImageFunControlnet 为 ComfyUI 内置节点。"
                      "缺失说明安装异常"),
    ),
    # Z-Image ControlNet 的权重加载器。
    #
    # ⚠️ 权重放 **`models/model_patches/`**，不是 `controlnet/`
    #   —— 它的类型是 MODEL_PATCH，且 ComfyUI 按张量键
    #   `control_all_x_embedder.2-1.weight` 识别为
    #   "alipai z image fun controlnet"（见 nodes_model_patch.py:271）。
    #
    # `name` 走 **defaults 而非 port** —— 与 `identity.ipadapter` 的 `preset` 同型：
    # 权重是否存在由部署方保证，**不参与本地文件校验**。
    # 这样也避免了 `FILE_MISSING` 在没装权重的机器上硬阻断整条链。
    CompileRule(
        kind="composition.model_patch",
        class_types=("ModelPatchLoader",),
        port_map={},
        param_map={"name": "name"},
        #: 空串会被 `_inject_required_loaders` 用 `default_model_patch`
        #: 填上（见 ComfyCompiler 构造参数）。
        defaults={"name": ""},
        output_ports=("MODEL_PATCH",),
        required=False,
        install_hint="ModelPatchLoader 为 ComfyUI 内置节点",
    ),
    # ---- 身份锁定：参考图注入（可降级为 LoRA） ---------------------------
    #
    # ⚠️ 候选的**契约不变量**：本规则的 port_map 假设候选都接受
    #    `model`(MODEL) + `image`(IMAGE) 并输出 **MODEL**。
    #
    #    早期版本把 `ReferenceLatent` 也列为候选 —— 那是**错误的**：
    #      ReferenceLatent 输出 CONDITIONING（不是 MODEL），
    #      且只有 `conditioning`(CONDITIONING) + `latent`(LATENT) 两个输入，
    #      **既没有 model 也没有 image 端口**。
    #
    #    为什么测试没发现：夹具里 IPAdapter 存在，规则总命中第一候选，
    #    `ReferenceLatent` 从未被选中。**真实环境才暴露**（本地未装 IPAdapter，
    #    于是降级到第三候选，产出一张类型完全错位的图）。
    #
    #    由此新增两条防线（见 `_validate_rule_invariants` 与
    #    `capability.CapabilityMatrix.detect_rule_signature_conflicts`）。
    CompileRule(
        kind="identity.reference",
        class_types=("IPAdapterAdvanced", "IPAdapterUnifiedLoader"),
        # `ipadapter` 是 IPAdapterAdvanced 的**必需**输入，但它由另一个节点提供
        # （见 `identity.ipadapter` 规则）。这里声明映射关系，
        # 由编译器**按需自动注入加载器**——与模型源注入同一机制。
        port_map={"model": "model", "image": "image"},
        param_map={"weight": "weight", "weight_type": "weight_type"},
        defaults={"weight": 0.65, "weight_type": "linear"},
        output_ports=("MODEL",),
        required=False,
        requires_model_source=True,
        #: 本规则的实现还需要一个前置加载器节点（kind -> 该加载器的输出类型）
        requires_loader={"identity.ipadapter": "IPADAPTER"},
        degrade_to="identity.lora",
        install_hint="安装 ComfyUI_IPAdapter_plus 以恢复完整身份锁定强度",
        adjust_on_degrade={"strength_model": 0.90, "strength_clip": 0.90},
        constraint_threshold_relax={"identity_similarity": 0.08},
    ),
    # IPAdapter 模型加载器。存在的理由：`IPAdapterAdvanced` 需要 `ipadapter`
    # (IPADAPTER) 输入，而该输入**不是模型加载器能提供的**（它产出 MODEL/CLIP/VAE）。
    #
    # 这暴露了早期规则模型的一个结构性局限：**"一条规则 = 一个节点"** 的假设
    # 对"加载器 + 应用器"这类两节点模式不成立。此规则即该模式的补全。
    CompileRule(
        kind="identity.ipadapter",
        class_types=("IPAdapterUnifiedLoader", "IPAdapterModelLoader"),
        port_map={"model": "model"},
        param_map={"preset": "preset"},
        defaults={"preset": "PLUS (high strength)"},
        output_ports=("MODEL", "IPADAPTER"),
        required=False,
        requires_model_source=True,
        install_hint="安装 ComfyUI_IPAdapter_plus 及其 IPAdapter 权重",
    ),
    # ---- 构图锁定（Sketch-Lock） ----------------------------------------
    #
    # 两档的**元数不同**：Advanced 输出 (positive, negative) 两个 CONDITIONING，
    # 基础版输出单个 CONDITIONING 且只接受一个 conditioning 输入。
    # 这是真实语义差异，不能混在一个 kind 里。
    CompileRule(
        kind="composition.controlnet",
        class_types=("ControlNetApplyAdvanced",),
        port_map={"positive": "positive", "negative": "negative",
                  "control_net": "control_net", "image": "image"},
        param_map={"strength": "strength"},
        defaults={"strength": 0.8},
        output_ports=("CONDITIONING", "CONDITIONING"),
        required=False,
        degrade_to="composition.controlnet_basic",
        install_hint="ControlNetApplyAdvanced 为 ComfyUI 内置节点",
    ),
    CompileRule(
        kind="composition.controlnet_basic",
        class_types=("ControlNetApply",),
        port_map={"positive": "conditioning",
                  "control_net": "control_net", "image": "image"},
        param_map={"strength": "strength"},
        defaults={"strength": 0.8},
        output_ports=("CONDITIONING",),
        required=False,
        install_hint="ControlNetApply 为 ComfyUI 内置节点",
    ),
    # ---- 潜空间 ---------------------------------------------------------
    CompileRule(
        kind="latent.empty",
        class_types=("EmptyLatentImage", "EmptySD3LatentImage"),
        param_map={"width": "width", "height": "height", "batch_size": "batch_size"},
        defaults={"width": 768, "height": 1344, "batch_size": 1},
        output_ports=("LATENT",),
        required=True,
    ),
    CompileRule(
        kind="latent.from_image",
        class_types=("VAEEncode",),
        port_map={"pixels": "pixels", "vae": "vae"},
        output_ports=("LATENT",),
        required=True,
    ),
    # ---- 采样（核心，不可降级） ------------------------------------------
    #
    # KSampler 与 KSamplerAdvanced **参数契约不同**：
    #   KSampler          : seed, denoise
    #   KSamplerAdvanced  : noise_seed, add_noise, start_at_step, end_at_step,
    #                       return_with_leftover_noise（**没有 denoise**）
    # 混为一个 kind 会让 seed/denoise 映射到不存在的端口。故拆开并用 degrade_to 串联。
    CompileRule(
        kind="sample.ksampler",
        class_types=("KSampler",),
        port_map={"model": "model", "positive": "positive",
                  "negative": "negative", "latent_image": "latent_image"},
        param_map={"steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
                   "scheduler": "scheduler", "denoise": "denoise"},
        defaults={"steps": 20, "cfg": 5.5, "sampler_name": "dpmpp_2m",
                  "scheduler": "karras", "denoise": 1.0, "seed": 0},
        output_ports=("LATENT",),
        required=True,
        seeded=True,
        degrade_to="sample.ksampler_advanced",
        install_hint="KSampler 为 ComfyUI 内置节点，缺失说明安装异常",
    ),
    CompileRule(
        kind="sample.ksampler_advanced",
        class_types=("KSamplerAdvanced",),
        port_map={"model": "model", "positive": "positive",
                  "negative": "negative", "latent_image": "latent_image"},
        # 参数名不同：种子走 noise_seed，且无 denoise
        param_map={"steps": "steps", "cfg": "cfg", "sampler": "sampler_name",
                   "scheduler": "scheduler", "seed": "noise_seed"},
        defaults={"steps": 20, "cfg": 5.5, "sampler_name": "dpmpp_2m",
                  "scheduler": "karras",
                  "add_noise": "enable", "start_at_step": 0, "end_at_step": 10000,
                  "return_with_leftover_noise": "disable"},
        output_ports=("LATENT",),
        required=False,
        seeded=False,   # 种子由 param_map 直接映射，无需派生注入
        install_hint="KSamplerAdvanced 为 ComfyUI 内置节点",
    ),
    # ---- 解码 -----------------------------------------------------------
    CompileRule(
        kind="decode.vae",
        class_types=("VAEDecode",),
        port_map={"samples": "samples", "vae": "vae"},
        output_ports=("IMAGE",),
        required=True,
    ),
    # ---- 保存 -----------------------------------------------------------
    CompileRule(
        kind="io.save_image",
        class_types=("SaveImage",),
        port_map={"images": "images"},
        param_map={"filename_prefix": "filename_prefix"},
        defaults={"filename_prefix": "Little Crew"},
        output_ports=(),
        required=True,
    ),
    # ---- 视频 -----------------------------------------------------------
    #
    # 这里刻意**分成三个 kind**，而不是把三个节点塞进一个候选列表。
    #
    # 原因（真实环境核实所得）：这三种节点是**三种不同的架构**，端口契约毫无共同点：
    #
    #   WanImageToVideo         两阶段：吃 positive/negative CONDITIONING + vae，
    #                           吐 (positive, negative, latent) 三个输出
    #   MiniMaxH3ImageToVideo   一体化：吃 clip + vae + prompt 字符串，
    #                           吐 (positive, latent) 两个输出，图像输入走 optional
    #   SVD_img2vid_Conditioning 吃 clip_vision + init_image + motion_bucket_id，
    #                           吐三个输出
    #
    # 把它们混在一个 kind 里，任何"最小公倍数"式的 port_map 都会在真机上错位。
    # 正确做法：**每个架构一个 kind**，用 `degrade_to` 串联成降级链。
    #
    # 注：本机（未装任何视频权重）三者**节点都在**，因此编译能过、
    #     但执行期会因缺权重而失败 —— 这是预期的，由 runtime 层的检查点与
    #     失败码处理，不属于编译层的职责。
    CompileRule(
        kind="video.i2v",
        class_types=("WanImageToVideo",),
        port_map={"positive": "positive", "negative": "negative", "vae": "vae",
                  "image": "start_image"},
        param_map={"frames": "length", "width": "width", "height": "height",
                   "batch_size": "batch_size"},
        defaults={"length": 81, "width": 832, "height": 480, "batch_size": 1},
        output_ports=("CONDITIONING", "CONDITIONING", "LATENT"),
        required=False,
        degrade_to="video.i2v_h3",
        install_hint="安装 Wan 视频节点包（WanImageToVideo 为 ComfyUI 内置）",
    ),
    CompileRule(
        kind="video.i2v_h3",
        class_types=("MiniMaxH3ImageToVideo",),
        port_map={"clip": "clip", "vae": "vae", "image": "first_frame"},
        param_map={"prompt": "prompt", "frames": "length",
                   "width": "width", "height": "height"},
        defaults={"length": 124, "width": 768, "height": 1344, "prompt": ""},
        output_ports=("CONDITIONING", "LATENT"),
        required=False,
        degrade_to="video.i2v_svd",
        install_hint="安装 Comfy-Org/MiniMax-H3 节点与权重",
    ),
    CompileRule(
        kind="video.i2v_svd",
        class_types=("SVD_img2vid_Conditioning",),
        port_map={"clip_vision": "clip_vision", "image": "init_image", "vae": "vae"},
        param_map={"frames": "video_frames", "width": "width", "height": "height",
                   "fps": "fps"},
        defaults={"video_frames": 25, "width": 1024, "height": 576, "fps": 6,
                  "motion_bucket_id": 127, "augmentation_level": 0.0},
        output_ports=("CONDITIONING", "CONDITIONING", "LATENT"),
        required=False,
        install_hint="SVD_img2vid_Conditioning 为 ComfyUI 内置节点，需 SVD 权重",
    ),
    # ⚠️ 已知能力缺口（真实环境核实所得，**尚未打通**）
    #
    # MiniMaxH3ReferenceToVideo 的参考图输入是 `ref_images`，类型为
    # **COMFY_AUTOGROW_V3** —— 一种**动态端口**，可挂任意数量的参考图，
    # 另有 `ref_videos` / `ref_audios` / `ref_video_audios` 三类动态输入。
    #
    # 而本平台 IR 目前的 `bindings.CHAR-xxxx.anchorSet.front` 表达的是
    # **单一参考图**。二者不匹配，因此本规则**暂时不能声明 image 映射** ——
    # 否则会编译出一张把单图塞进动态端口的错图。
    #
    # 打通它需要 IR 侧先支持"多参考资产"（见 docs/01 §3.3 binding 定义），
    # 属独立工作项。在那之前，本 kind 在环境缺 ref2v 时会沿 degrade_to 降级，
    # 而在环境有 ref2v 时会因缺少必需输入而**在编译期被端口校验拦下**（而非静默出错）。
    CompileRule(
        kind="video.ref2v",
        class_types=("MiniMaxH3ReferenceToVideo",),
        port_map={"clip": "clip", "vae": "vae"},
        param_map={"prompt": "prompt", "frames": "length",
                   "width": "width", "height": "height"},
        defaults={"length": 124, "width": 768, "height": 1344, "prompt": "",
                  "ref_image_size": "match"},
        output_ports=("CONDITIONING", "LATENT"),
        required=False,
        degrade_to="video.i2v_h3",
        install_hint="需要 MiniMax H3 Ref2VA 权重与节点；缺失时身份锁定强度下降",
        constraint_threshold_relax={"identity_similarity": 0.10},
    ),
    # ---- 音频 -----------------------------------------------------------
    CompileRule(
        kind="audio.tts",
        class_types=("QwenTTSNode", "GPTSoVITSNode", "EdgeTTSNode"),
        param_map={"text": "text", "voice": "voice", "speed": "speed"},
        defaults={"speed": 1.0},
        output_ports=("AUDIO",),
        required=False,
        install_hint="安装 TTS 节点包（Qwen-TTS / GPT-SoVITS / Edge-TTS）",
    ),
    # ---- 性能 -----------------------------------------------------------
    CompileRule(
        kind="perf.sage_attention",
        class_types=("MiniMaxH3MemoryEfficientSageAttentionPatch", "SageAttentionPatch"),
        port_map={"model": "model"},
        output_ports=("MODEL",),
        required=False,
        install_hint="安装 SageAttention 加速节点（可选，仅影响速度）",
    ),
    CompileRule(
        kind="style.bible",
        class_types=("CLIPTextEncode",),
        port_map={"clip": "clip"},
        param_map={"text": "text"},
        defaults={"text": ""},
        output_ports=("CONDITIONING",),
        required=False,
    ),
    CompileRule(
        kind="script.beat",
        class_types=("CLIPTextEncode",),      # 占位：语义节点，实际由上游 Agent 展开
        param_map={"text": "text"},
        defaults={"text": ""},
        output_ports=("CONDITIONING",),
        required=False,
    ),
    CompileRule(
        kind="prompt.fuse",
        class_types=("ConditioningConcat",),
        port_map={"conditioning_1": "conditioning_to",
                  "conditioning_2": "conditioning_from"},
        output_ports=("CONDITIONING",),
        required=False,
        degrade_to="prompt.fuse_combine",
        install_hint="ConditioningConcat 为 ComfyUI 内置节点",
    ),
    # 语义相同（合并两路条件）但**端口名不同**：
    #   ConditioningConcat 用 conditioning_to / conditioning_from（拼接，语义"叠加"）
    #   ConditioningCombine 用 conditioning_1 / conditioning_2（平均，语义"混合"）
    # 二者输出都是单个 CONDITIONING，可互为降级，但不能共用一套 port_map。
    CompileRule(
        kind="prompt.fuse_combine",
        class_types=("ConditioningCombine",),
        port_map={"conditioning_1": "conditioning_1",
                  "conditioning_2": "conditioning_2"},
        output_ports=("CONDITIONING",),
        required=False,
        install_hint="ConditioningCombine 为 ComfyUI 内置节点",
    ),
)


# ==========================================================================
# 编译结果
# ==========================================================================

@dataclass
class CompileResult:
    graph: dict[str, Any]                    # ComfyUI API 格式
    notices: list[DegradationNotice] = field(default_factory=list)
    drifts: list[Drift] = field(default_factory=list)
    graph_hash: str = ""
    ir_hash: str = ""
    catalog_hash: str = ""
    rule_version: str = COMPILE_RULE_VERSION
    threshold_adjustments: dict[str, float] = field(default_factory=dict)
    auto_injected: list[str] = field(default_factory=list)
    #: 为满足端口类型而自动注入的图片加载节点：{node_id: 文件路径}
    load_image_injections: dict[str, str] = field(default_factory=dict)
    #: 需要新增 LoadImage 的（class_type, port）清单，供需求声明在**编译前**校验
    required_loaders: list[tuple[str, str]] = field(default_factory=list)
    #: 确定性存疑的节点：{ir_node_id: (class_type, determinism)}
    #: 供 runtime 决定是否需要核对缓存，避免"以为重跑其实没跑"
    determinism_concerns: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return any(d.severity == Severity.BLOCKER.value for d in self.drifts)

    def warnings(self) -> list[DegradationNotice]:
        return [n for n in self.notices if n.severity == Severity.WARNING.value]

    def summary(self) -> dict[str, Any]:
        return {
            "graphHash": self.graph_hash,
            "irHash": self.ir_hash,
            "catalogHash": self.catalog_hash,
            "ruleVersion": self.rule_version,
            "nodeCount": len(self.graph),
            "noticeCount": len(self.notices),
            "blockerCount": sum(1 for d in self.drifts if d.severity == Severity.BLOCKER.value),
            "autoInjected": list(self.auto_injected),
            "thresholdAdjustments": dict(self.threshold_adjustments),
        }


# ==========================================================================
# 编译器
# ==========================================================================

class ComfyCompiler:
    """IR → ComfyUI API 格式图的编译器。纯函数式：无 IO、无随机。"""

    #: 自动注入的模型源节点 ID。固定值以保证编译确定性。
    MODEL_SOURCE_ID = "n_ckpt"

    @staticmethod
    def _model_source_id_for(graph: Mapping[str, Any]) -> str:
        """推导本轮的模型源节点 id。

        **不存实例状态**：``ComfyCompiler`` 声称"纯函数式、无 IO、无随机"，
        若把"上一轮注入了哪个 id"记在 self 上，连续两次 ``compile()``
        就会互相影响 —— 那不再是纯函数，也会让同一 IR 在不同调用顺序下
        产出不同的图哈希。

        推导规则：优先取 IR 显式声明的模型节点（编译产物中 class_type 为
        模型加载器的那个），否则用固定的 ``MODEL_SOURCE_ID``。
        """
        loader_types = {"CheckpointLoaderSimple", "CheckpointLoader",
                        "UNETLoader", "CheckpointLoaderSimpleAdv"}
        for gid, gnode in graph.items():
            if str(gnode.get("class_type", "")) in loader_types:
                return gid
        return ComfyCompiler.MODEL_SOURCE_ID

    def __init__(
        self,
        matrix: CapabilityMatrix,
        rules: CompileRuleTable | None = None,
        *,
        default_checkpoint: str = "sd_xl_base_1.0.safetensors",
        default_model_patch: str = "",
        model_source_kind: str = "model.checkpoint",
    ) -> None:
        self.matrix = matrix
        self.rules = rules or CompileRuleTable(DEFAULT_RULES)
        self.default_checkpoint = default_checkpoint
        self.default_model_patch = default_model_patch
        #: 当前环境用哪条规则作**模型源**。
        #:
        #: ``model.checkpoint`` 是"单一 checkpoint 加载器"（SDXL 走这条）；
        #: ``model.source_zimage`` 是"分离加载"（UNET + CLIP + VAE 三个节点）。
        #: 二者不能合并成一条规则 —— **"一条规则 = 一个节点"的假设对分离加载不成立**，
        #: 而那个假设在本项目里已经是第三次成为阻碍（前两次是 IPAdapter 与
        #: ModelPatchLoader 的"加载器 + 应用器"模式）。
        self.model_source_kind = model_source_kind

        # 把 `default_model_patch` **灌进规则本身**，而不是在每个注入点各处理一次。
        #
        # 理由：`composition.model_patch` 的 `name` 是个"由部署决定的文件名"，
        # 而规则表里只能写空串占位。若在每个用到它的地方分别兜底，
        # 早晚会漏一处 —— 而漏掉的表现是**产出一张没有控制效果的图**，
        # 看起来像"ControlNet 没用"，不像配置错误。
        #
        # 与 `default_checkpoint` 的差别：那个有"IR 里显式声明优先"的语义，
        # 这个没有 —— 权重文件是纯粹的部署参数，不来自 IR。
        if default_model_patch:
            patched = dict(self.rules._rules)
            r = patched.get("composition.model_patch")
            if r is not None:
                patched["composition.model_patch"] = replace(
                    r, defaults={**r.defaults,
                                 "name": r.defaults.get("name")
                                 or default_model_patch})
                self.rules = CompileRuleTable(tuple(patched.values()))

    # ---- 公开入口 ------------------------------------------------------

    def compile(self, ir: Mapping[str, Any]) -> CompileResult:
        """编译主流程。任何结构性问题抛 CompileError；能力问题产出 drift/notice。"""
        normalized = normalize(ir)
        ir_hash = content_hash(ir)
        notices: list[DegradationNotice] = []
        adjustments: dict[str, float] = {}
        auto_injected: list[str] = []
        load_image_injections: dict[str, str] = {}
        determinism_concerns: dict[str, tuple[str, str]] = {}
        #: class_type -> 受影响的 ir_node_id 列表（用于按类去重告警）
        determinism_nodes_by_class: dict[str, list[str]] = {}

        nodes = {n["id"]: n for n in normalized["nodes"]}
        order = self._topo_order(normalized)

        # 0) 约束注册表：收集图级约束 + 节点引用的约束，并校验引用完整性。
        #    健全的约束注册表是"降级时能正确放宽阈值"的前提：
        #    否则降级只影响节点自身声明的约束，图级约束会被漏掉。
        constraint_registry = self._build_constraint_registry(normalized)

        # 1) 需求声明 + 漂移检测（编译前即可发现环境问题）
        files_by_port = self._file_requirements(normalized)
        reqs = self.rules.requirements(
            normalized,
            matrix=self.matrix,
            files_by_port={p: tuple(v for _, v in pairs)
                           for p, pairs in files_by_port.items()},
        )
        drifts = self.matrix.detect_drift(reqs)

        # 2) 预判：**必需**的 class_type 或文件缺失 -> 直接阻断
        #
        # ⚠️ 关键区分：`FILE_MISSING` 漂移对象本身**不带**规则信息，因此
        #    "这个缺失文件来自必需规则还是可选规则"必须回查 `files_by_port`。
        #
        #    若不区分，缺一个**可选**的身份 LoRA 就会把整条管线硬阻断 ——
        #    而本机**无法训练角色 LoRA**（Z-Image fp32 底模需 22.93 GB
        #    > 本机 15.8 GB 内存），于是"必须提供角色 LoRA"这条约束
        #    等于让平台在这台机器上永远无法出片。
        #
        #    可选规则缺文件不是错误，是**降级信号**：应当继续编译、
        #    改走降级链，并发出显式降级通知（含影响评估与修复建议）。
        optional_pairs: set[tuple[str, str]] = set()
        #: (端口, 文件名) -> 来源 kind。用于在降级通知里说明"缺的是谁要的东西"
        pair_kind: dict[tuple[str, str], str] = {}
        for port, pairs in files_by_port.items():
            for kind, value in pairs:
                pair_kind[(port, value)] = kind
                rule = self.rules.get(kind)
                if rule is None or not rule.required:
                    optional_pairs.add((port, value))

        blockers = [d for d in drifts if d.severity == Severity.BLOCKER.value]
        hard_blockers: list[Drift] = []
        soft_blockers: list[Drift] = []
        for d in blockers:
            if d.kind == DriftKind.NODE_MISSING.value:
                hard_blockers.append(d)
            elif d.kind == DriftKind.FILE_MISSING.value:
                if d.port and (d.port, str(d.expected)) in optional_pairs:
                    soft_blockers.append(d)
                else:
                    hard_blockers.append(d)

        if soft_blockers:
            for d in soft_blockers:
                kind = pair_kind.get((d.port or "", str(d.expected)), "?")
                notices.append(DegradationNotice(
                    kind=NoticeKind.SKIPPED_OPTIONAL,
                    ir_node_id=None,
                    severity=Severity.WARNING.value,
                    frm=str(d.class_type),
                    to="(沿该 kind 的降级链继续)",
                    reason=(f"可选文件 {d.expected!r}（端口 {d.port}，来自 kind {kind!r}）"
                            f"不在目标环境中；不阻断编译，改走降级链。"
                            f"典型成因：本机无法训练角色 LoRA（内存不足），"
                            f"故只能走参考图路径。"),
                    impact="身份锁定强度下降；一致性约束阈值将按降级补偿表自动放宽",
                    ui_hint=d.remedy or "安装对应权重，或接受参考图路径的强度",
                    reversible=True,
                ))

        if hard_blockers:
            raise CompileError(
                CompileErrorCode.MISSING_REQUIRED_NODE,
                "必需节点或模型文件缺失，编译被阻断："
                + "; ".join(f"{d.class_type}.{d.port or '-'} ({d.kind})" for d in hard_blockers[:5]),
                detail={"drifts": [d.as_dict() for d in hard_blockers]},
            )

        # 2.5) 计算**必须跳过的节点**与其**下游重接表**
        #
        # 为什么必须有这一步：漂移检测报出"某节点的文件缺失"之后，编译器若照常
        # 生成该节点，就会产出**引用不存在文件的图** —— 本地看着没问题，
        # 提交时被 ComfyUI 以 `value_not_in_list` 拒绝。
        #
        # 这是端到端验证抓出的真实缺陷：漂移检测**知道** `LoraLoader.lora_name`
        # 不在环境里，但编译器没有据此行事。而这恰恰是本项目最反对的
        # "静默产出必错图"。
        #
        # 跳过之后必须**重接下游**，否则下游节点的必需端口会留空 ——
        # 那是同一类缺陷的另一副面孔。
        missing_by_node: dict[str, set[str]] = {}

        # 先做一次**预选型**：确定每个 IR 节点将被编译成哪个 class_type。
        #
        # 为什么必须预选型而不能按 IR 的 kind 匹配：降级会改变归属关系。
        # 例：`identity.reference` 在缺 IPAdapter 时降级到 `LoraLoader`，
        # 于是漂移报的是 `LoraLoader.lora_name`，而 IR 里那个节点的 kind
        # 却是 `identity.reference` —— 按 kind 匹配会漏掉它，
        # 结果就是"漂移检测报了缺失，编译器却照样生成引用该文件的节点"。
        # 这正是缺陷 2 的原貌。
        preselected: dict[str, str] = {}
        for nid2, n2 in nodes.items():
            r2 = self.rules.get(n2.get("kind"))
            if r2 is None:
                continue
            ct2 = self._select_class_type(r2, nid2, [])[0]
            if ct2:
                preselected[nid2] = ct2

        file_drifts = [
            d for d in soft_blockers
            if d.kind == DriftKind.FILE_MISSING.value and d.port
        ]
        for nid2, ct2 in preselected.items():
            spec2 = self.matrix.spec(ct2)
            if spec2 is None:
                continue
            for d in file_drifts:
                if d.class_type != ct2:
                    continue
                # 该节点选中的实现上，这个端口确实承载着这个缺失文件
                p2 = spec2.port(d.port) if hasattr(spec2, "port") else None
                if p2 is None:
                    continue
                declared = self._declared_file_for(nodes[nid2], normalized, d.port)
                if declared and declared == str(d.expected):
                    missing_by_node.setdefault(nid2, set()).add(str(d.expected))

        # 依赖图（被跳过节点的输入来源），供重接时上溯
        deps: dict[str, set[str]] = {
            nid2: {str(v["ref"]) for v in (n2.get("inputs") or {}).values()
                   if isinstance(v, Mapping) and "ref" in v}
            for nid2, n2 in nodes.items()
        }

        #: 因能力缺失而失去执行者的约束（编译末尾会据此把阈值归零并告警）
        skipped_constraints: set[str] = set()
        skip_set: set[str] = set(missing_by_node)
        remap = self._remap_table(skip_set, deps, nodes)

        for nid in sorted(skip_set):
            files_missing = sorted(missing_by_node[nid])
            kind = str(nodes[nid].get("kind"))
            rule = self.rules.get(kind)
            if rule is not None and rule.required:
                # 必需规则的权重缺失 = 真的跑不了，不假装能跑
                raise CompileError(
                    CompileErrorCode.MISSING_REQUIRED_NODE,
                    f"必需节点 {nid!r}({kind}) 依赖的权重文件不在目标环境："
                    + ", ".join(files_missing),
                    ir_node_id=nid,
                    detail={"missingFiles": files_missing,
                            "installHint": rule.install_hint},
                )
            skipped_constraints.update(
                (nodes[nid].get("semantic") or {}).get("constraintRefs", ())
            )
            tgt, tgt_port = remap.get(nid, ("?", "?"))
            notices.append(DegradationNotice(
                kind=NoticeKind.SKIPPED_OPTIONAL, ir_node_id=nid,
                severity=Severity.WARNING.value,
                frm=f"{kind} @ {nodes[nid].get('kind')}",
                to=f"(已跳过，下游重接到 {tgt}.{tgt_port})",
                reason=(f"权重文件 {files_missing} 不在目标环境。"
                        f"**移除该节点而非生成一个引用不存在文件的节点** —— "
                        f"后者会产出「编译通过但提交必失败」的图。"),
                impact=("该能力未生效，其约束已从注册表移除；"
                        "一致性约束阈值将按降级补偿表放宽"),
                ui_hint=(rule.install_hint if rule else "")
                        or f"把 {files_missing} 放入 ComfyUI 对应 models 子目录并重启",
                reversible=True,
            ))

        # 3) 逐节点编译
        graph: dict[str, Any] = {}
        model_source_injected = False
        #: 模型加载器的「端口类型 -> outputIndex」，供按类型自动补齐必需端口
        #: 端口类型 -> (提供者节点 id, 输出索引)
        model_outputs: dict[str, tuple[str, int]] = {}
        chosen_by_node: dict[str, str] = {}
        for nid in order:
            node = nodes[nid]
            kind = node["kind"]
            rule = self.rules.get(kind)
            if rule is None:
                raise CompileError(
                    CompileErrorCode.UNKNOWN_KIND,
                    f"未登记的节点类型 {kind!r}（规则表版本 {self.rules.version}）",
                    ir_node_id=nid,
                    detail={"availableKinds": self.rules.kinds()},
                )

            if nid in skip_set:
                continue        # 已在 2.5) 处理并发出通知

            # 本节点**已经由 IR 安排**的 ComfyUI 端口名（还没解析成连线，只看声明）。
            # 用途：判断"模型加载器能否补上本节点缺的必需端口"时，
            # 只能看**真正缺的**那些，不能把 IR 已声明的算进去。
            inputs_planned: set[str] = {
                cname
                for ir_port, cname in rule.port_map.items()
                if (node.get("inputs") or {}).get(ir_port) is not None
            }

            # 选型：端口/参数映射用**目标规则**，降级补偿用**源规则**。
            chosen, degrade_from, target_rule, source_rule = self._select_class_type(
                rule, nid, notices)
            if chosen is None:
                # 无可用实现且无降级链
                if rule.required:
                    raise CompileError(
                        CompileErrorCode.MISSING_REQUIRED_NODE,
                        f"必需节点 {kind!r} 在目标环境无可用实现（候选 {rule.class_types}）",
                        ir_node_id=nid,
                        detail={"installHint": rule.install_hint},
                    )
                skipped_constraints.update(
                    (node.get("semantic") or {}).get("constraintRefs", ())
                )
                notices.append(DegradationNotice(
                    kind=NoticeKind.SKIPPED_OPTIONAL, ir_node_id=nid,
                    severity=Severity.INFO.value, frm=kind, to="(skipped)",
                    reason=f"候选 {list(rule.class_types)} 在目标环境均不存在",
                    impact="该可选能力未生效，不影响主流程产出",
                    ui_hint=rule.install_hint,
                ))
                continue

            eff_rule = target_rule or rule

            # 自动注入模型源。
            #
            # 触发条件有两个，缺一不可：
            #   ① 规则声明 `requires_model_source`（如 LoRA / 采样器）
            #   ② **本节点有必需端口，且其类型正好由模型加载器提供**
            #      —— 典型是 `CLIPTextEncode.clip`(CLIP)。
            #      它的规则没声明 requires_model_source（文本编码本身不加载模型），
            #      但缺了 CLIP 就编不出完整的图。
            #
            # 只靠 ① 会漏掉 prompt.* 这类节点，产出的图必然被 ComfyUI 以
            # `required_input_missing` 拒绝 —— 端到端验证抓出的缺陷 1。
            need_source = eff_rule.requires_model_source
            if not need_source and not model_source_injected:
                node_spec0 = self.matrix.spec(chosen)
                if node_spec0 is not None:
                    want = self._model_provided_types()
                    if any(p.dtype in want
                           for pn, p in node_spec0.inputs_required.items()
                           if pn not in inputs_planned):
                        need_source = True

            if need_source and not model_source_injected:
                model_outputs = self._inject_model_source(
                    graph, normalized, notices, auto_injected)
                model_source_injected = True

            # 注入**前置加载器**（如 IPAdapter 的权重加载器），并把它的输出类型
            # 合入 model_outputs —— 这样"应用器"缺的 `ipadapter` 端口就能被补上。
            if eff_rule.requires_loader:
                loader_outputs = self._inject_loader(
                    graph, eff_rule, node, chosen, notices,
                    auto_injected, model_outputs,
                )
                if loader_outputs:
                    merged_outputs = dict(model_outputs)
                    merged_outputs.update(loader_outputs)
                    node_outputs = merged_outputs
                else:
                    node_outputs = model_outputs
            else:
                node_outputs = model_outputs

            # 解析输入（使用被选中规则的端口映射）
            inputs = self._resolve_inputs(
                node, eff_rule, chosen, graph, nodes, normalized, nid,
                notices, load_image_injections,
                skip_set=skip_set, remap=remap, chosen_by_node=chosen_by_node,
                model_outputs=node_outputs,
            )

            # 参数（widget 值）
            self._apply_params(node, eff_rule, chosen, inputs, ir_hash, normalized)

            # 降级后的参数补偿：补偿表定义在**源规则**上（"能力变弱了要怎么补"）
            if degrade_from is not None and source_rule.adjust_on_degrade:
                for pkey, val in source_rule.adjust_on_degrade.items():
                    inputs[pkey] = val

            # 默认值补全（不覆盖已设置值）
            for pname, dflt in eff_rule.defaults.items():
                if pname not in inputs:
                    inputs[pname] = dflt

            # 端口存在性最终校验（防止规则表与实际环境不一致）
            spec = self.matrix.spec(chosen)
            if spec is not None:
                unknown = [p for p in inputs if not spec.has_port(p)]
                if unknown:
                    # 端口不存在则剔除并记录（多为可选 widget 在旧版本不存在）
                    for p in unknown:
                        inputs.pop(p, None)
                    notices.append(DegradationNotice(
                        kind=NoticeKind.SUBSTITUTED, ir_node_id=nid,
                        severity=Severity.WARNING.value, frm=chosen, to=chosen,
                        reason=f"目标环境节点 {chosen} 上不存在输入 {unknown}，已剔除",
                        impact="对应参数未生效",
                        ui_hint=f"检查 {chosen} 的节点版本",
                    ))

            # 约束阈值随降级自动调整（放宽量取自**源规则**）
            # 关键：阈值必须按**约束注册表**调整，而非只按本节点声明的约束 ——
            # 否则图级约束（graph.constraints）在降级时会被漏掉，
            # 表现为"门禁仍用原阈值，于是降级后必然失败"，降级链形同虚设。
            if degrade_from is not None and source_rule.constraint_threshold_relax:
                self._adjust_thresholds(
                    source_rule, degrade_from, node, notices,
                    adjustments, constraint_registry,
                )

            # 确定性检查：**"重跑是否真的重跑"取决于 ComfyUI 的缓存键是否完整**
            #
            # 事实依据（research/comfyui-internals/caching.py:117）：
            #   节点签名 = [class_type, is_changed] + 全部 inputs + 祖先签名
            # 因此：
            #   · 种子已写入 inputs（我们做确定性派生）⇒ 换种子会自动重算 ✅
            #   · 但节点若读外部状态（文件/时间/网络）且**未声明钩子**，
            #     同名 inputs 会产生不同输出 —— 缓存键不完整
            # 而我们**无法从 /object_info 探测钩子存在性**（那是类方法）。
            #
            # 告警粒度：**按 class_type 去重**，每类只报一条。
            # 理由：一个 60 镜的剧集会有数百个节点，逐节点告警会淹没真正的问题。
            # 同一类节点的风险是同一个，报一次即可，节点清单列在通知内容里。
            node_spec = self.matrix.spec(chosen)
            if node_spec is not None and node_spec.determinism == DeterminismClass.UNKNOWN.value:
                determinism_concerns[nid] = (chosen, node_spec.determinism)
                determinism_nodes_by_class.setdefault(chosen, []).append(nid)

            graph[nid] = {
                "class_type": chosen,
                "inputs": inputs,
                "_meta": {
                    "title": (node.get("meta") or {}).get("title", kind),
                    "irNodeId": nid,
                    **({"irKind": kind} if kind != chosen else {}),
                },
            }
            chosen_by_node[nid] = chosen

        # 3.5) **补接线 + 产出完整性终检**
        #
        # 先补一遍接线（second pass），再终检。为什么需要两遍：
        # 第一遍按拓扑序逐节点解析输入，此时**排在后面的提供者还没编译出来**。
        # 一个只声明了自身、没有上游声明的节点，就会在第一遍里找不到 IMAGE /
        # CONDITIONING 之类的提供者而留空。等全图构建完再补，提供者必然已在。
        #
        # 补接线之后仍缺的**连线端口**才算真缺陷 —— 那意味着这张图提交给
        # ComfyUI 必然被 `required_input_missing` 拒绝。这是端到端验证
        # 抓出的真实缺陷类别（`CLIPTextEncode.clip` 缺失）。
        #
        # ⚠️ **只查连线端口，不查 widget 端口**：`KSampler.steps` 这类
        # widget 输入缺值时 ComfyUI 会用节点自带默认值，不会失败。
        # 若把它们也算缺陷，"只声明部分参数的 IR"就会被误判为错误 ——
        # 那会逼着每个 IR 写全所有参数，与"IR 只表达语义"的设计相悖。
        for nid in list(graph):
            gnode = graph[nid]
            gspec = self.matrix.spec(gnode["class_type"])
            if gspec is None:
                continue
            self._autowire_required(
                nodes.get(nid, {"id": nid}), gnode["class_type"], gspec,
                gnode["inputs"], graph, nid, notices,
                model_outputs, chosen_by_node,
            )

        incomplete: list[str] = []
        for gid, gnode in graph.items():
            gspec = self.matrix.spec(gnode["class_type"])
            if gspec is None:
                continue
            provided = set(gnode["inputs"])
            for pname, pspec in gspec.inputs_required.items():
                if pname in provided or not pspec.is_link:
                    continue
                incomplete.append(
                    f"{gid}({gnode['class_type']}).{pname} [{pspec.dtype}]"
                )

        if incomplete:
            raise CompileError(
                CompileErrorCode.INCOMPLETE_GRAPH,
                "编译产出的图不完整：以下必需**连线**输入未被填充 —— "
                + "; ".join(incomplete[:8])
                + ("…" if len(incomplete) > 8 else ""),
                detail={
                    "incomplete": incomplete,
                    "reason": ("这些端口在本地校验中不会被发现，"
                               "但提交到真实 ComfyUI 时会被 required_input_missing 拒绝。"
                               "典型成因：上游节点被跳过且未能重接，"
                               "或 IR 未声明该输入而编译器也未能自动注入。"
                               "（widget 型端口缺值不在此列 —— ComfyUI 会用默认值。）"),
                    "remedy": ("补齐缺失的权重文件以恢复上游节点；"
                               "或在 IR 中显式声明该输入；"
                               "或检查依赖链是否完整"),
                },
            )

        if not graph:
            raise CompileError(
                CompileErrorCode.MISSING_REQUIRED_NODE,
                "编译结果为空：所有节点均无可用的目标实现",
            )

        graph_hash = "sha256:" + hashlib.sha256(
            canonical_json(graph).encode("utf-8")
        ).hexdigest()

        # 被跳过的可选节点所关联的约束：其阈值放宽量需要单独说明，
        # 因为该约束已完全失去执行者（不是"能力变弱"，而是"能力消失"）。
        for cref in sorted(skipped_constraints):
            if cref in adjustments:
                continue
            adjustments[cref] = 0.0
            notices.append(DegradationNotice(
                kind=NoticeKind.THRESHOLD_ADJUSTED, ir_node_id=None,
                severity=Severity.WARNING.value,
                frm="约束有执行者", to="约束失去执行者",
                reason=f"约束 {cref} 依赖的可选能力在目标环境缺失，已无法执行",
                affected_constraints=(cref,),
                impact="该约束不再被校验，产出质量可能下降且不会被门禁拦截",
                ui_hint="安装对应节点包以恢复该约束的执行",
                reversible=True,
            ))

        # 确定性存疑告警：**按 class_type 去重**，每类一条。
        # 逐节点告警会让 60 镜的剧集产出数百条同义通知，淹没真正的问题节点。
        for ctype in sorted(determinism_nodes_by_class):
            affected = sorted(determinism_nodes_by_class[ctype])
            tp = THIRD_PARTY_NODES_IN_USE.get(ctype)
            if tp and tp.get("unreviewed"):
                hint = (f"该节点来自第三方节点包 {tp['pack']}，其失效钩子尚未核实。"
                        f"核实方法：{tp['review_hint']}")
                reason = f"{ctype} 的缓存失效钩子未核实（第三方节点包 {tp['pack']}）"
            else:
                hint = ("① 若确认该节点输出仅由输入决定，可在环境配置中声明 "
                        "determinism=pure 消除本提示；"
                        "② 若它读文件/时间，请确认其声明了 IS_CHANGED")
                reason = ("该节点的缓存失效钩子无法从 /object_info 探测"
                          "（IS_CHANGED / fingerprint_inputs 是类方法，不在 schema 里）")
            notices.append(DegradationNotice(
                kind=NoticeKind.CACHE_KEY_INCOMPLETE, ir_node_id=affected[0],
                severity=Severity.WARNING.value,
                frm=f"{ctype}（确定性未核实）",
                to=f"已标记，涉及 {len(affected)} 个节点",
                reason=reason,
                impact=("若该节点实际读取外部状态而未声明钩子，相同输入可能复用失效缓存 —— "
                        "表现为「以为重跑了其实没重跑」。该风险仅影响本类节点，"
                        "同一 class_type 只报一次"),
                ui_hint=hint,
                reversible=True,
            ))

        return CompileResult(
            graph=graph, notices=_dedupe_notices(notices), drifts=drifts,
            graph_hash=graph_hash, ir_hash=ir_hash,
            catalog_hash=self.matrix.catalog_hash(),
            rule_version=self.rules.version,
            threshold_adjustments={k: v for k, v in sorted(adjustments.items())},
            auto_injected=auto_injected,
            load_image_injections=dict(sorted(load_image_injections.items())),
            required_loaders=sorted(set(self._collect_required_loaders(normalized))),
            determinism_concerns=dict(sorted(determinism_concerns.items())),
        )

    def _collect_required_loaders(self, ir: Mapping[str, Any]) -> list[tuple[str, str]]:
        """编译**前**就能算出的 (class_type, port) 清单 —— 这些端口会收到文件路径。

        用途：`requirements()` 据此提前声明"需要 LoadImage"，
        使"环境缺 LoadImage"在编译前的漂移检测阶段就被发现，
        而不是等到逐节点编译时才报错。
        """
        out: list[tuple[str, str]] = []
        for node in ir.get("nodes", ()):
            rule = self.rules.get(node.get("kind", ""))
            if rule is None:
                continue
            for ir_port, cname in rule.port_map.items():
                src = (node.get("inputs") or {}).get(ir_port)
                has_path = (
                    isinstance(src, Mapping) and "binding" in src
                ) or (isinstance(src, str) and src and "." in src)
                if not has_path:
                    continue
                for ct in rule.class_types:
                    p = self.matrix.spec(ct)
                    if p is not None and (port := p.port(cname)) is not None:
                        if port.dtype in IMAGELIKE_INPUT_DTYPES:
                            out.append((ct, cname))
        return out

    # ---- 约束注册表 ----------------------------------------------------

    def _build_constraint_registry(
        self, ir: Mapping[str, Any],
    ) -> dict[str, Mapping[str, Any]]:
        """收集全部约束声明（图级 + 节点级），并校验引用完整性。

        为什么需要独立注册表：约束可以声明在 ``graph.constraints``，
        也可以由节点通过 ``semantic.constraintRefs`` 引用。降级时需要按约束
        （而非按节点）汇总阈值放宽量，否则图级约束会被漏掉 ——
        表现为"门禁仍用原阈值，降级后必然失败"，降级链形同虚设。

        同时校验悬空引用：``constraintRefs`` 指向不存在的约束是典型的
        IR 书写错误（多为拼写错误），必须在编译期报出而非静默忽略。
        """
        registry: dict[str, Mapping[str, Any]] = {}
        for c in ir.get("constraints", ()) or ():
            registry[c["ref"]] = c

        for node in ir.get("nodes", ()):
            for cref in (node.get("semantic") or {}).get("constraintRefs", ()) or ():
                if cref in registry:
                    continue
                # 节点引用但图级未声明 -> 悬空引用
                raise CompileError(
                    CompileErrorCode.DANGLING_REF,
                    f"节点 {node['id']!r} 引用了未声明的约束 {cref!r}；"
                    f"请在 graph.constraints 中声明该约束，或修正拼写",
                    ir_node_id=node["id"],
                    detail={
                        "declaredConstraints": sorted(registry),
                        "referenced": cref,
                    },
                )
        return registry

    def _adjust_thresholds(
        self,
        eff_rule: CompileRule,
        degraded_kind: str,
        node: Mapping[str, Any],
        notices: list[DegradationNotice],
        adjustments: dict[str, float],
        constraint_registry: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """按约束注册表汇总阈值放宽量，并为每条受影响约束产生通知。"""
        # 该节点自身声明的约束 + 图级约束中断言与此能力相关的约束
        node_refs = list((node.get("semantic") or {}).get("constraintRefs", ()) or ())

        affected: list[str] = list(node_refs)
        # 图级约束：若断言算子与该降级能力相关（如身份→identity_similarity），一并放宽
        relax_ops = set(eff_rule.constraint_threshold_relax)
        for cref, cons in constraint_registry.items():
            if cref in affected:
                continue
            op = (cons.get("assertion") or {}).get("op")
            if op in relax_ops:
                affected.append(cref)

        for cref in affected:
            cons = constraint_registry.get(cref)
            if cons is None:
                continue

            # 同一约束可能被多个节点触发（例如镜头里两个节点都声明了 cr_costume_007），
            # 但**阈值只需放宽一次**。若累加多次，约束会被过度放宽而失去把关作用 ——
            # 这是比"重复通知"严重得多的功能性缺陷。
            already = cref in adjustments
            if not already:
                for _op, delta in eff_rule.constraint_threshold_relax.items():
                    prev = adjustments.get(cref)
                    adjustments[cref] = round((prev if prev is not None else 0.0) + delta, 6)
            else:
                continue

            assertion = cons.get("assertion") or {}
            old_thr = assertion.get("threshold")
            new_thr = (
                round(float(old_thr) - adjustments[cref], 6)
                if isinstance(old_thr, (int, float)) else None
            )
            notices.append(DegradationNotice(
                kind=NoticeKind.THRESHOLD_ADJUSTED, ir_node_id=node["id"],
                severity=Severity.WARNING.value,
                frm=f"{degraded_kind} 完整能力",
                to=f"阈值 {old_thr} -> {new_thr}" if new_thr is not None
                    else f"放宽 +{adjustments[cref]}",
                reason="身份锁定能力下降后原阈值已不可达；不放宽则门禁必然失败",
                affected_constraints=(cref,),
                threshold_adjusted={
                    "constraintRef": cref,
                    "from": old_thr,
                    "to": new_thr,
                    "delta": adjustments[cref],
                },
                impact="该约束的实际严格度下降，审片报告会单列标记",
                ui_hint="恢复完整节点后阈值将自动回到原值",
                reversible=True,
            ))

    # ---- 拓扑排序 ------------------------------------------------------

    def _topo_order(self, ir: Mapping[str, Any]) -> list[str]:
        """按数据边做拓扑排序。检测环与悬空引用。

        确定性：同层节点按 id 字典序，保证编译结果可复现。
        """
        nodes = {n["id"]: n for n in ir["nodes"]}
        deps: dict[str, set[str]] = {nid: set() for nid in nodes}
        for e in ir.get("edges", ()):
            if e.get("type") != "data":
                continue
            src, dst = e["from"]["node"], e["to"]["node"]
            if src not in nodes:
                raise CompileError(CompileErrorCode.DANGLING_REF,
                                   f"数据边 {e['id']!r} 的源节点 {src!r} 不存在",
                                   ir_node_id=dst)
            if dst not in nodes:
                raise CompileError(CompileErrorCode.DANGLING_REF,
                                   f"数据边 {e['id']!r} 的目标节点 {dst!r} 不存在",
                                   ir_node_id=src)
            deps[dst].add(src)

        # 同时把 inputs 里的端口引用视为依赖（有些引用未显式建边）
        for nid, node in nodes.items():
            for val in (node.get("inputs") or {}).values():
                if isinstance(val, Mapping) and "ref" in val:
                    ref = val["ref"]
                    if ref not in nodes:
                        raise CompileError(
                            CompileErrorCode.DANGLING_REF,
                            f"节点 {nid!r} 的输入引用了不存在的节点 {ref!r}",
                            ir_node_id=nid,
                        )
                    deps[nid].add(ref)

        ready = sorted(nid for nid, d in deps.items() if not d)
        order: list[str] = []
        remaining = {nid: set(d) for nid, d in deps.items()}
        while ready:
            nid = ready.pop(0)
            order.append(nid)
            for other in sorted(remaining):
                if nid in remaining[other]:
                    remaining[other].discard(nid)
                    if not remaining[other] and other not in order and other not in ready:
                        ready.append(other)
            ready.sort()
        if len(order) != len(nodes):
            cyclic = sorted(set(nodes) - set(order))
            raise CompileError(CompileErrorCode.CYCLE,
                               f"IR 含环，无法编译：{cyclic}",
                               detail={"cycleNodes": cyclic})
        return order

    # ---- 实现选型与降级 -------------------------------------------------

    def _select_class_type(
        self, rule: CompileRule, nid: str, notices: list[DegradationNotice],
    ) -> tuple[str | None, str | None, CompileRule | None, CompileRule]:
        """按优先级选择可用 class_type；全不可用时走降级链。

        返回 ``(选中的 class_type, 被降级前的 kind 或 None, 目标规则, 源规则)``。

        **为什么需要同时返回两个规则**（这是本方法最容易做错的地方）：
          - 端口映射（port_map）与参数映射（param_map）必须用**目标规则** ——
            否则会把源规则的端口名（IPAdapter 的 image/weight）写进目标节点（LoRA），
            造成假漂移与执行期报错。
          - 降级补偿（adjust_on_degrade）与阈值放宽量（constraint_threshold_relax）
            必须用**源规则** —— 因为"能力变弱了、需要怎么补偿"这件事的定义在源能力上，
            目标规则对此一无所知（它的这两张表是空的）。
        """
        chosen = self.matrix.first_available(rule.class_types)
        if chosen is not None:
            return chosen, None, rule, rule

        # 沿降级链
        target_kind = rule.degrade_to
        seen: set[str] = set()
        while target_kind and target_kind not in seen:
            seen.add(target_kind)
            target_rule = self.rules.get(target_kind)
            if target_rule is None:
                break
            alt = self.matrix.first_available(target_rule.class_types)
            if alt is not None:
                notices.append(DegradationNotice(
                    kind=NoticeKind.DEGRADED, ir_node_id=nid,
                    severity=Severity.WARNING.value,
                    frm=f"{rule.kind} @ {rule.class_types[0]}",
                    to=f"{target_kind} @ {alt}",
                    reason=f"目标环境缺少节点 {list(rule.class_types)}",
                    impact="身份锁定能力下降；相关一致性约束阈值已自动放宽",
                    ui_hint=rule.install_hint,
                    reversible=True,
                ))
                return alt, rule.kind, target_rule, rule
            target_kind = target_rule.degrade_to
        return None, None, None, rule

    # ---- 输入解析 ------------------------------------------------------

    def _resolve_inputs(
        self,
        node: Mapping[str, Any],
        rule: CompileRule,
        chosen: str,
        graph: dict[str, Any],
        nodes: Mapping[str, Any],
        ir: Mapping[str, Any],
        nid: str,
        notices: list[DegradationNotice],
        load_image_injections: dict[str, str],
        skip_set: set[str] | None = None,
        remap: Mapping[str, tuple[str, str]] | None = None,
        chosen_by_node: Mapping[str, str] | None = None,
        model_outputs: Mapping[str, tuple[str, int]] | None = None,
    ) -> dict[str, Any]:
        """解析一个节点的输入。

        **两种"上游不可用"的情形必须区别对待**：

        | 情形 | 正确处理 |
        |---|---|
        | 上游是**可选**能力且不可用，而当前端口**可选** | 留空即可（继续） |
        | 上游是**可选**能力且不可用，而当前端口**必需** | **沿依赖图重接到最近的存活祖先** |

        早期版本对两者一视同仁地 ``continue`` —— 结果图里出现
        "必需端口缺输入"的节点，**本地校验看不出来**，只有真实 ComfyUI
        会以 ``required_input_missing`` 拒绝。这是端到端验证抓出的真实缺陷。

        Args:
            skip_set: 已被跳过的 IR 节点 id 集合。
            remap: ``被跳过节点 -> (最近存活祖先, 该祖先的输出端口)``。
            chosen_by_node: ``IR 节点 id -> 选中的 class_type``（用于**类型校验**：
                重接后的祖先输出类型必须与端口要求一致，否则宁可留空也不能接错）。
            model_outputs: ``端口类型 -> outputIndex``，来自自动注入的模型源。
                用于**按类型自动补齐必需端口**（见 `_autowire_required`）。
        """
        inputs: dict[str, Any] = {}
        spec = self.matrix.spec(chosen)
        skip_set = skip_set or set()
        remap = remap or {}
        chosen_by_node = chosen_by_node or {}
        model_outputs = model_outputs or {}

        for ir_port, cname in rule.port_map.items():
            src = (node.get("inputs") or {}).get(ir_port)
            if src is None and ir_port in ("model", "clip") and rule.requires_model_source:
                # 未显式连接 model/clip -> 指向自动注入的模型源
                out_idx = 0 if ir_port == "model" else 1
                if spec is not None and not spec.has_port(cname):
                    continue
                inputs[cname] = [self.MODEL_SOURCE_ID, out_idx]
                continue
            if src is None:
                continue

            if isinstance(src, Mapping) and "ref" in src:
                src_node_id = src["ref"]
                src_ir = nodes.get(src_node_id)
                if src_ir is None:
                    raise CompileError(
                        CompileErrorCode.DANGLING_REF,
                        f"输入 {ir_port!r} 引用了不存在的节点 {src_node_id!r}",
                        ir_node_id=nid,
                    )

                if src_node_id in skip_set and src_node_id not in graph:
                    # 上游被跳过 -> 重接。这一步是必需的，不是优化。
                    resolved = self._resolve_remap(src_node_id, nodes, remap)
                    if resolved is None:
                        continue
                    tgt, tgt_port = resolved
                    if not self._output_matches(
                        nodes.get(tgt), tgt_port, chosen, cname, spec,
                        chosen_by_node,
                    ):
                        notices.append(DegradationNotice(
                            kind=NoticeKind.SKIPPED_OPTIONAL, ir_node_id=nid,
                            severity=Severity.WARNING.value,
                            frm=f"{src_node_id}.{src['port']}",
                            to=f"{tgt}.{tgt_port}（类型不符，未接线）",
                            reason=(f"上游被跳过，最近的存活祖先 {tgt} 输出 "
                                    f"{tgt_port!r} 与端口 {chosen}.{cname} 的类型要求不符"),
                            impact=f"{cname} 将缺输入，提交时会被 ComfyUI 拒绝",
                            ui_hint="请补齐缺失的权重文件，或在 IR 中改用其他能力路径",
                        ))
                        continue
                    inputs[cname] = [tgt, self._output_index(nodes[tgt], tgt_port)]
                    continue

                if src_node_id not in graph:
                    # 上游被跳过但无存活祖先可重接 —— 如实报告，不静默留空
                    notices.append(DegradationNotice(
                        kind=NoticeKind.SKIPPED_OPTIONAL, ir_node_id=nid,
                        severity=Severity.WARNING.value,
                        frm=f"{src_node_id}.{src['port']}", to="(无存活祖先)",
                        reason=f"输入 {ir_port!r} 依赖的节点 {src_node_id!r} 已被跳过，"
                               f"且沿依赖图找不到可替代的存活祖先",
                        impact=f"必需端口 {cname} 将缺输入，提交时可能被 ComfyUI 拒绝",
                        ui_hint="检查 IR 的依赖链是否完整",
                    ))
                    continue

                out_idx = self._output_index(src_ir, src["port"])
                inputs[cname] = [src_node_id, out_idx]
            elif isinstance(src, Mapping) and "binding" in src:
                value = self._resolve_binding(src["binding"], ir, nid, ir_port, cname)
                inputs[cname] = self._coerce_for_port(
                    value, ir_port, cname, chosen, spec, graph, nid,
                    notices, load_image_injections,
                )
            else:
                inputs[cname] = self._coerce_for_port(
                    src, ir_port, cname, chosen, spec, graph, nid,
                    notices, load_image_injections,
                )

        # **按类型自动补齐**仍然空着的必需端口。
        #
        # 两类来源，按优先级：
        #   ① 模型加载器（MODEL / CLIP / VAE）—— 数据来源是确定的
        #   ② **已编译图中任何提供该类型的节点** —— 取最近编译的一个
        #
        # 场景②的必要性：ComfyUI 升级可能**改名**端口（如 `KSampler.latent_image`
        # → `latent`），此时 IR 声明的连线会被"端口不存在则剔除"的逻辑丢掉，
        # 于是必需端口留空。图里明明有 LATENT 的提供者（就是采样器的上游），
        # 不接上就是白白产出一张不完整的图。
        #
        # 不补的后果：图里出现必需端口缺输入的节点，**本地校验看不出来**，
        # 提交时被 ComfyUI 以 `required_input_missing` 拒绝（缺陷 1 的原貌）。
        if spec is not None:
            self._autowire_required(
                node, chosen, spec, inputs, graph, nid, notices,
                model_outputs, chosen_by_node,
            )

        return inputs

    def _autowire_required(
        self,
        node: Mapping[str, Any],
        chosen: str,
        spec: NodeSpec,
        inputs: dict[str, Any],
        graph: dict[str, Any],
        nid: str,
        notices: list[DegradationNotice],
        model_outputs: Mapping[str, tuple[str, int]],
        chosen_by_node: Mapping[str, str],
    ) -> None:
        """把仍空着的**必需**端口按类型接上提供者。

        优先级：模型加载器 / 已注入的前置加载器 > 图中最近的一个同类型提供者。
        都找不到则留空 —— 由编译末尾的**产出完整性终检**兜底报错。
        """
        for pname, pspec in spec.inputs_required.items():
            if pname in inputs:
                continue
            dtype = pspec.dtype

            # ① 已知来源（模型加载器 **或** 自动注入的前置加载器）
            #
            # ⚠️ 这里曾经写死 `msid = self._model_source_id_for(graph)`，
            #   即"提供者一定是模型源节点"。**那是错的**：
            #   `composition.model_patch` 注入的 `ModelPatchLoader` 输出 MODEL_PATCH，
            #   而模型源（CheckpointLoaderSimple）根本不产出该类型。
            #   写死的后果是 `model_patch` 被接到模型源的 output[0]（MODEL）——
            #   图**看起来完整**、编译**不报错**，但语义完全错位。
            #
            #   所以来源必须随类型一起记录，而不是靠"类型 -> 假设的节点"推。
            src = model_outputs.get(dtype)
            if graph and src is not None:
                msid, idx = src
                inputs[pname] = [msid, idx]
                notices.append(DegradationNotice(
                    kind=NoticeKind.AUTO_INJECTED, ir_node_id=nid,
                    severity=Severity.INFO.value,
                    frm=f"{chosen}.{pname} 未声明",
                    to=f"{msid}[{idx}] ({dtype})",
                    reason=(f"必需端口 {pname} 在 IR 中未声明，已按端口类型 "
                            f"{dtype} 接到其提供者节点"),
                    impact="该输入的值来自加载器节点，与 IR 意图一致",
                    ui_hint=f"如需精确控制 {pname}，请在 IR 的 inputs 中显式声明",
                ))
                continue

            # ② 图中最近的一个同类型提供者
            provider = self._find_provider(graph, dtype)
            if provider is None:
                continue
            pid, pidx = provider
            inputs[pname] = [pid, pidx]
            notices.append(DegradationNotice(
                kind=NoticeKind.AUTO_INJECTED, ir_node_id=nid,
                severity=Severity.WARNING.value,
                frm=f"{chosen}.{pname} 缺输入",
                to=f"{pid}[{pidx}] ({dtype})",
                reason=(f"必需端口 {pname} 未获得输入（常见成因：目标环境的端口被改名，"
                        f"IR 声明的连线因此失效）。已按类型 {dtype} 接到图中最近的提供者"),
                impact=("图结构恢复完整；但该连线是**按类型推断**的，"
                        "若语义不符请检查 IR 的端口声明"),
                ui_hint=f"核对 IR 中 {pname!r} 的连线是否仍指向正确的上游节点",
                reversible=True,
            ))

    def _find_provider(
        self,
        graph: Mapping[str, Any],
        dtype: str,
    ) -> tuple[str, int] | None:
        """在已编译图中找**最近一个**输出该类型的节点。

        "最近"按图的插入顺序取最后一个 —— 编译按拓扑序进行，
        故最后插入的节点在数据流上离当前节点最近。
        """
        best: tuple[str, int] | None = None
        for gid, gnode in graph.items():
            gspec = self.matrix.spec(str(gnode.get("class_type", "")))
            if gspec is None:
                continue
            outs = tuple(str(d) for d in gspec.outputs)
            if dtype in outs:
                best = (gid, outs.index(dtype))
        return best


    def _declared_file_for(
        self,
        node: Mapping[str, Any],
        ir: Mapping[str, Any],
        port: str,
    ) -> str | None:
        """取出某 IR 节点在指定 ComfyUI 端口上**声明的文件名**（无则 None）。

        目前只有两类文件端口：
          ``lora_name`` <- 来自 ``bindings[CHAR-xxx].loraFile``
          ``ckpt_name`` <- 来自节点 ``params.ckpt_name``（或规则表默认值）

        存在的理由：判断"某漂移是否属于这个节点"时必须比对**文件名**，
        只比端口名会把同一端口上的不同文件混为一谈。
        """
        if port == "lora_name":
            cref = (node.get("semantic") or {}).get("characterRef")
            b = (ir.get("bindings") or {}).get(cref or "")
            if isinstance(b, Mapping) and b.get("loraFile"):
                return str(b["loraFile"])
            return None
        if port == "ckpt_name":
            v = (node.get("params") or {}).get("ckpt_name")
            return str(v) if v else None
        return None

    # ---- 被跳过节点的下游重接 -------------------------------------------

    @staticmethod
    def _remap_table(
        skip_set: set[str],
        deps: Mapping[str, set[str]],
        nodes: Mapping[str, Any],
    ) -> dict[str, tuple[str, str]]:
        """为每个被跳过的节点求"最近存活祖先 + 输出端口"。

        为什么需要它：一个可选节点被跳过后，**下游引用它的必需端口不能留空** ——
        否则图里会出现"结构不完整但本地校验看不出"的节点（端到端验证抓出的缺陷）。

        重接语义：把"经过被跳过节点的数据流"接回该数据的**来源**。
        例：``n_ckpt -> n_lora(缺文件) -> n_ks``
            跳掉 n_lora 后，n_ks.model 应重接到 n_ckpt.MODEL。

        沿**输入依赖**方向（即数据来源方向）BFS，取第一个非跳过节点。
        """
        out: dict[str, tuple[str, str]] = {}
        for sid in skip_set:
            src_node = nodes.get(sid) or {}
            seen: set[str] = {sid}
            frontier: list[str] = [
                str(v["ref"])
                for v in (src_node.get("inputs") or {}).values()
                if isinstance(v, Mapping) and "ref" in v
            ]
            # 广度优先：先近后远
            anchor: str | None = None
            while frontier and anchor is None:
                nxt: list[str] = []
                for cand in frontier:
                    if cand in seen:
                        continue
                    seen.add(cand)
                    if cand not in skip_set and cand in nodes:
                        anchor = cand
                        break
                    nxt.extend(deps.get(cand, ()))
                frontier = nxt

            # 兜底：图上找不到就从自动注入的模型源取（模型/条件类端口的主要来源）
            out[sid] = (anchor or ComfyCompiler.MODEL_SOURCE_ID, "MODEL")
        return out

    @staticmethod
    def _resolve_remap(
        node_id: str,
        nodes: Mapping[str, Any],
        remap: Mapping[str, tuple[str, str]],
    ) -> tuple[str, str] | None:
        """取重接目标；目标自身也被跳过时继续沿链上溯（带环保护）。"""
        seen: set[str] = set()
        cur = node_id
        while cur not in seen:
            seen.add(cur)
            entry = remap.get(cur)
            if entry is None:
                return None
            tgt, port = entry
            if tgt == cur:
                return None
            if tgt not in remap:
                return (tgt, port)
            cur = tgt
        return None

    def _output_matches(
        self,
        src_ir_node: Mapping[str, Any] | None,
        src_port: str,
        chosen: str,
        cname: str,
        spec: NodeSpec | None,
        chosen_by_node: Mapping[str, str],
    ) -> bool:
        """校验"重接后的祖先输出类型"与"当前端口要求的类型"是否一致。

        **类型不符时宁可不接线**：错接会产出语义错误的图，而留空至少会被
        ComfyUI 的必需输入校验拦住 —— 前者是静默错误，后者是显式失败。
        """
        if spec is None or src_ir_node is None:
            return True
        target = spec.port(cname)
        if target is None:
            return True
        dtype = self._ir_port_dtype(src_ir_node, src_port, chosen_by_node)
        if dtype is None:
            return True  # 无从判断则不阻拦
        return dtype == target.dtype

    @staticmethod
    def _ir_port_dtype(
        ir_node: Mapping[str, Any],
        port: str,
        chosen_by_node: Mapping[str, str],
    ) -> str | None:
        """推断 IR 某个输出端口的 ComfyUI 类型。

        约定：IR 的输出端口名**直接用 ComfyUI 的类型名**（``MODEL`` / ``CLIP`` /
        ``IMAGE`` / ``CONDITIONING`` / ``LATENT`` / ``VAE`` / ``MASK``）。
        这也是 ``_output_index`` 的既有约定，故此处保持一致。
        """
        kn = str(port).upper()
        if kn in _KNOWN_PORT_TYPES:
            return kn
        return None


    # ---- 端口类型强制 --------------------------------------------------

    def _coerce_for_port(
        self,
        value: Any,
        ir_port: str,
        port_name: str,
        chosen: str,
        spec: NodeSpec | None,
        graph: dict[str, Any],
        nid: str,
        notices: list[DegradationNotice],
        injections: dict[str, str],
    ) -> Any:
        """把 IR 值强制转换为目标端口要求的类型。

        目前处理一类关键情形：**端口要求 IMAGE，但值是文件路径字符串**。
        ComfyUI 的 IMAGE 是张量，文件路径必须先经 ``LoadImage`` 转换。

        不做这一步的后果：编译"成功"，但提交到真实 ComfyUI 必然报类型错误。
        这是本项目早期版本的真实缺陷 —— 端口类型守卫存在，但因未做
        widget 值的类型校验而漏过。

        修复方式：自动注入 ``LoadImage``，并产出 ``AUTO_INJECTED`` 通知
        （与模型源注入同一机制，保持行为一致、可观测）。
        """
        if spec is None:
            return value
        p = spec.port(port_name)
        if p is None or p.dtype not in IMAGELIKE_INPUT_DTYPES:
            return value
        if not isinstance(value, str) or not value:
            return value

        loader_cls = self._first_available_kind("image.file")
        if loader_cls is None:
            raise CompileError(
                CompileErrorCode.MISSING_REQUIRED_NODE,
                f"端口 {chosen}.{port_name} 要求 {p.dtype}，"
                f"但输入是文件路径 {value!r}，而环境中没有可用的 LoadImage 节点来转换它",
                ir_node_id=nid,
                detail={"requiredDtype": p.dtype, "value": value,
                        "triedKinds": ["image.file"]},
            )

        loader_id = self._load_image_node_id(value)
        if loader_id not in graph:
            graph[loader_id] = {
                "class_type": loader_cls,
                "inputs": {"image": value},
                "_meta": {"title": f"载入图片：{value.split('/')[-1]}",
                          "irNodeId": nid, "autoInjectedFor": f"{chosen}.{port_name}"},
            }
            injections[loader_id] = value
            notices.append(DegradationNotice(
                kind=NoticeKind.AUTO_INJECTED, ir_node_id=nid,
                severity=Severity.INFO.value,
                frm=f"{chosen}.{port_name} 收到文件路径",
                to=f"{loader_cls}({value}) -> {p.dtype}",
                reason=f"端口 {port_name} 要求 {p.dtype}（张量），而 IR 提供的是文件路径字符串",
                impact=("已自动插入图片加载节点。附带收益：LoadImage 自带 IS_CHANGED，"
                        "锚点图换版时 ComfyUI 会自动重算下游"),
                ui_hint="如需控制加载行为（如遮罩/裁剪），请在 IR 中显式添加 image.file 节点",
            ))
        return [loader_id, 0]

    def _load_image_node_id(self, path: str) -> str:
        """由文件路径**确定性派生**的节点 id。

        必须确定性：编译是纯函数，同一 IR 必得同一图与同一哈希。
        用路径哈希而非自增计数器，避免"节点顺序变化导致哈希漂移"。
        """
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
        return f"n_loadimg_{digest}"

    def _first_available_kind(self, kind: str) -> str | None:
        """在能力矩阵中找出该 kind 的可用 class_type（不产生通知）。"""
        rule = self.rules.get(kind)
        if rule is None:
            return None
        return self.matrix.first_available(rule.class_types)

    # ---- 规则表不变量检查 ------------------------------------------------

    def validate_rule_invariants(self) -> list[dict[str, Any]]:
        """校验每条规则的**候选项之间契约一致**。

        为什么必须有这个检查
        --------------------
        规则的 ``port_map`` / ``output_ports`` 是对**所有候选**共同成立的契约假设。
        若某个候选的真实端口签名与假设不符，就会出现：
          · 规则"选中"了它（因为它在能力矩阵里存在）
          · 但编译出的图**类型完全错位**
          · 而漂移检测不会报错（因为它校验的是"端口是否存在"，不是"候选之间是否一致"）

        **真实案例**：`identity.reference` 曾把 `ReferenceLatent` 列为第三候选。
        `ReferenceLatent` 输出 CONDITIONING（不是 MODEL），且只有
        `conditioning` + `latent` 两个输入 —— 没有 `model`、没有 `image`。
        本地未装 IPAdapter 时它被选中，产出一张类型错位的图。

        **为什么单元测试没发现**：夹具环境里 IPAdapter 存在，规则总命中第一候选，
        该候选从未被选中。只有真实环境（缺 IPAdapter）才暴露。
        → 因此本检查必须在**接入真实环境时**主动调用。

        返回问题清单（空列表 = 健康）。每项含 kind / class_type / 具体不符点。
        """
        problems: list[dict[str, Any]] = []

        for rule in self.rules._rules.values():  # noqa: SLF001 - 同模块内访问
            present = [ct for ct in rule.class_types if self.matrix.has(ct)]
            if len(present) < 2:
                continue  # 候选不足两个，无一致性问题

            # 以**第一个存在的候选**为基准
            base_ct = present[0]
            base = self.matrix.spec(base_ct)
            if base is None:
                continue

            for ct in present[1:]:
                spec = self.matrix.spec(ct)
                if spec is None:
                    continue
                diffs: list[str] = []

                # ① 输出类型必须一致（否则下游接线全错）
                if tuple(spec.outputs) != tuple(base.outputs):
                    diffs.append(
                        f"输出类型不一致: {base_ct}={list(base.outputs)} "
                        f"vs {ct}={list(spec.outputs)}"
                    )

                # ② port_map 声明的每个 ComfyUI 端口都必须存在
                for ir_port, cname in rule.port_map.items():
                    if not spec.has_port(cname):
                        diffs.append(f"缺少 port_map 要求的输入 {cname!r}(对应 IR {ir_port!r})")

                # ③ 文件型/必需 widget 端口也应在场
                for pname in rule.defaults:
                    if not spec.has_port(pname):
                        diffs.append(f"缺少 defaults 要求的输入 {pname!r}")

                if diffs:
                    problems.append({
                        "kind": rule.kind,
                        "baseClassType": base_ct,
                        "conflictingClassType": ct,
                        "diffs": diffs,
                        "impact": ("该候选若被选中（例如前面的候选在目标环境缺失），"
                                   "会编译出类型错位的图"),
                        "remedy": (f"从 kind {rule.kind!r} 的 class_types 中移除 {ct!r}，"
                                   f"或为它单独定义一个新的 kind"),
                    })
        return problems

    def validate_rule_declarations(self) -> list[dict[str, Any]]:
        """对照**真实 spec** 校验每条规则的声明本身是否成立。

        与 `validate_rule_invariants` 的分工：

        | 检查 | 回答的问题 | 适用 |
        |---|---|---|
        | `validate_rule_invariants` | 同 kind 内多个候选**彼此**是否一致 | 多候选规则 |
        | `validate_rule_declarations` | 规则声明与**目标实现**是否吻合 | 所有规则 |

        为什么需要后者：修掉候选冲突后，多数规则变成**单候选**，
        而单候选规则没有"彼此差异"可查 —— 于是"声明本身写错"就无人发现。
        典型错误：`port_map` 里的 ComfyUI 端口名拼错、`param_map` 指向不存在的 widget、
        `output_ports` 声明的类型/数量与实现不符。

        返回问题清单（空列表 = 健康）。
        """
        problems: list[dict[str, Any]] = []

        for rule in self.rules._rules.values():  # noqa: SLF001
            for ct in rule.class_types:
                spec = self.matrix.spec(ct)
                if spec is None:
                    continue  # 该候选不在本环境，无从校验（属正常）
                diffs: list[str] = []

                # ① port_map 的两侧：IR 端口要有来源，ComfyUI 端口要存在
                for ir_port, cname in rule.port_map.items():
                    if not spec.has_port(cname):
                        diffs.append(
                            f"port_map 声明的输入 {cname!r}(对应 IR {ir_port!r}) "
                            f"在 {ct} 上不存在"
                        )

                # ② param_map / defaults 的目标端口要存在
                for pkey, cname in rule.param_map.items():
                    if not spec.has_port(cname):
                        diffs.append(f"param_map 目标 {cname!r}(来自 IR {pkey!r}) 不存在")
                for pname in rule.defaults:
                    if not spec.has_port(pname):
                        diffs.append(f"defaults 目标 {pname!r} 不存在")

                # ③ output_ports 校验（空声明 = 不关心，显式跳过）
                if rule.output_ports:
                    declared = tuple(str(x) for x in rule.output_ports)
                    actual = tuple(str(x) for x in spec.outputs)
                    if len(declared) != len(actual):
                        diffs.append(
                            f"output_ports 声明 {len(declared)} 个，实际 {len(actual)} 个 "
                            f"(声明={list(declared)} 实际={list(actual)})"
                        )
                    elif declared != actual:
                        # 数量相同但类型不同：可能是"语义名 vs 类型名"的差异，记为提示
                        type_names = {"CONDITIONING", "LATENT", "IMAGE", "MASK",
                                      "MODEL", "CLIP", "VAE", "AUDIO", "VIDEO"}
                        if all(d in type_names for d in declared):
                            diffs.append(
                                f"output_ports 类型序列不符：声明={list(declared)} "
                                f"实际={list(actual)}（按位置解析 outputIndex 会错位）"
                            )

                if diffs:
                    problems.append({
                        "kind": rule.kind,
                        "classType": ct,
                        "diffs": diffs,
                        "remedy": f"修正 kind {rule.kind!r} 对 {ct} 的声明，或改用其他实现",
                    })
        return problems

    def _output_index(self, src_ir: Mapping[str, Any], port_name: str) -> int:
        """把 IR 输出端口名解析为 ComfyUI 的 outputIndex。

        规则：先查 IR 声明的 outputs 顺序；否则按编译规则的 output_ports；
        再否则按 ComfyUI 输出的类型名匹配；最后回退到 0（并依赖后续校验）。
        """
        declared = list((src_ir.get("outputs") or {}))
        if port_name in declared:
            return declared.index(port_name)

        rule = self.rules.get(src_ir["kind"])
        if rule is not None:
            if port_name in rule.output_ports:
                return rule.output_ports.index(port_name)
            # 允许用小写/类型名引用
            for idx, op in enumerate(rule.output_ports):
                if op.lower() == port_name.lower():
                    return idx

        # 端口名本身是数字（如 "0"）
        try:
            return int(port_name)
        except (TypeError, ValueError):
            pass
        return 0

    def _resolve_binding(
        self, binding_path: str, ir: Mapping[str, Any],
        nid: str, ir_port: str, cname: str,
    ) -> Any:
        """解析 graph.bindings 引用，如 CHAR-0007.anchorSet.front 或 CHAR-0007.loraFile。"""
        parts = binding_path.split(".")
        root = parts[0]
        bindings = ir.get("bindings") or {}
        if root not in bindings:
            raise CompileError(
                CompileErrorCode.MISSING_BINDING,
                f"资产绑定 {root!r} 不存在于 graph.bindings",
                ir_node_id=nid,
                detail={"available": sorted(bindings)},
            )
        cur: Any = bindings[root]
        for p in parts[1:]:
            if isinstance(cur, Mapping) and p in cur:
                cur = cur[p]
            else:
                raise CompileError(
                    CompileErrorCode.MISSING_BINDING,
                    f"资产绑定路径 {binding_path!r} 在 {p!r} 处中断",
                    ir_node_id=nid,
                    detail={"resolvedSoFar": cur if isinstance(cur, (str, int, float)) else "..."},
                )
        return cur

    # ---- 参数映射 ------------------------------------------------------

    def _apply_params(
        self,
        node: Mapping[str, Any],
        rule: CompileRule,
        chosen: str,
        inputs: dict[str, Any],
        ir_hash: str,
        ir: Mapping[str, Any],
    ) -> None:
        params = node.get("params") or {}

        for pkey, cname in rule.param_map.items():
            if pkey in params:
                inputs[cname] = params[pkey]
            elif pkey == "lora_name":
                # 从 bindings 取 LoRA 文件名
                sem = node.get("semantic") or {}
                cref = sem.get("characterRef")
                bind = (ir.get("bindings") or {}).get(cref or "")
                if isinstance(bind, Mapping) and bind.get("loraFile"):
                    inputs[cname] = bind["loraFile"]
                else:
                    raise CompileError(
                        CompileErrorCode.MISSING_BINDING,
                        f"节点 {node['id']!r} 需要 LoRA 文件，但 binding {cref!r} 未提供 loraFile",
                        ir_node_id=node["id"],
                    )

        # 端口引用的参数（如 prompt.text 来自上游）
        for ir_port, cname in rule.param_map.items():
            src = (node.get("inputs") or {}).get(ir_port)
            if isinstance(src, Mapping) and "ref" in src:
                inputs[cname] = [src["ref"], self._output_index(
                    next(n for n in ir["nodes"] if n["id"] == src["ref"]), src["port"]
                )]
            elif isinstance(src, Mapping) and "binding" in src:
                inputs[cname] = self._resolve_binding(src["binding"], ir, node["id"], ir_port, cname)

        # 种子：由 provenance.seedDerivation 确定性派生
        if rule.seeded and "seed" not in inputs:
            inputs["seed"] = self._derive_seed_from_ir(ir, node)


    def _derive_seed_from_ir(self, ir: Mapping[str, Any], node: Mapping[str, Any]) -> int:
        prov = ir.get("provenance") or {}
        sd = prov.get("seedDerivation") or {}
        if all(k in sd for k in ("projectSeed", "episodeIdx", "sceneIdx", "shotIdx")):
            return derive_seed(
                int(sd["projectSeed"]), int(sd["episodeIdx"]), int(sd["sceneIdx"]),
                int(sd["shotIdx"]), int(sd.get("attempt", 0)),
            )
        # 无种子派生信息时，从 graphId 稳定派生（保证可复现，但不可跨项目对齐）
        return int(hashlib.sha256(str(ir.get("graphId", "")).encode("utf-8")).hexdigest()[:15], 16)

    # ---- 自动注入：前置加载器 -------------------------------------------

    def _inject_loader(
        self,
        graph: dict[str, Any],
        rule: CompileRule,
        node: Mapping[str, Any],
        chosen: str,
        notices: list[DegradationNotice],
        auto_injected: list[str],
        model_outputs: Mapping[str, tuple[str, int]],
    ) -> dict[str, int]:
        """按 ``rule.requires_loader`` 注入前置加载器，返回其「类型 -> (节点 id, 索引)」。

        **为什么需要这个机制**：真实 ComfyUI 节点图存在"加载器 + 应用器"两节点模式。
        若只声明应用器，产出的图会缺一个必需输入（如 IPAdapter 的 `ipadapter`），
        本地校验看不出来，提交时被拒绝 —— 与缺陷 1 同类。

        注入是**幂等**的，且有两层含义：

        ① 同一 kind 一轮编译只注入一次（用确定性 id ``n_auto_<kind>`` 去重）
        ② **IR 里已显式声明同 kind 的节点时，不注入** —— 复用用户声明的那一个

        第 ② 条曾经缺失：只用 ① 去重时，IR 显式声明的节点用的是**它自己的 id**，
        与 ``n_auto_...`` 不同名，于是去重失效 —— 图里出现**两个** `ModelPatchLoader`，
        两个都产出 MODEL_PATCH。图能编译、能被接受，但**到底用哪一个是不确定的**
        （取决于端口解析时谁先被扫到）。
        """
        merged: dict[str, tuple[str, int]] = {}
        nid = str(node.get("id") or "")

        # 先扫一遍图，收集"IR 已声明的同类加载器"。
        #
        # 判据用 **kind 所属的 class_types**，而不是节点 id ——
        # 因为用户给节点起什么 id 由他决定，我们不能靠名字认它。
        declared: dict[str, tuple[str, int]] = {}
        for other_id, other in graph.items():
            if other_id == nid:
                continue
            lct = str(other.get("class_type") or "")
            for lkind2, _dt in rule.requires_loader.items():
                lr2 = self.rules.get(lkind2)
                if lr2 and lct in lr2.class_types:
                    spec2 = self.matrix.spec(lct)
                    if spec2 is not None:
                        for i, dt2 in enumerate(spec2.outputs):
                            declared.setdefault(str(dt2), (other_id, i))

        for lkind, dtype in rule.requires_loader.items():
            # ② IR 已声明 → 复用它，不注入
            if dtype in declared:
                merged[dtype] = declared[dtype]
                continue

            lrule = self.rules.get(lkind)
            if lrule is None:
                continue
            lchosen = self._select_class_type(lrule, nid, [])[0]
            if lchosen is None:
                continue
            lid = f"n_auto_{lkind.replace('.', '_')}"
            if lid not in graph:
                linputs: dict[str, Any] = {}
                # 加载器自己通常也需要模型源
                if lrule.requires_model_source and model_outputs:
                    linputs["model"] = [self._model_source_id_for(graph),
                                        model_outputs.get("MODEL", 0)]
                for pname, dflt in lrule.defaults.items():
                    linputs[pname] = dflt
                lspec0 = self.matrix.spec(lchosen)
                if lspec0 is not None:
                    linputs = {k: v for k, v in linputs.items()
                               if lspec0.has_port(k)}
                graph[lid] = {
                    "class_type": lchosen,
                    "inputs": linputs,
                    "_meta": {"title": f"{lkind}（自动注入）",
                              "irNodeId": nid,
                              "autoInjectedFor": f"{chosen}.{dtype}"},
                }
                auto_injected.append(lid)
                notices.append(DegradationNotice(
                    kind=NoticeKind.AUTO_INJECTED, ir_node_id=nid,
                    severity=Severity.INFO.value,
                    frm=f"{chosen} 需要 {dtype} 输入",
                    to=f"{lchosen} -> {dtype}",
                    reason=(f"{chosen} 的必需输入 {dtype} 由独立的加载器节点提供，"
                            f"而 IR 未声明它，已自动注入 {lchosen}"),
                    impact="身份锁定能力完整可用",
                    ui_hint=f"如需指定具体权重，请在 IR 中添加 {lkind} 节点",
                ))
            lspec = self.matrix.spec(lchosen)
            if lspec is not None:
                for i, dt in enumerate(lspec.outputs):
                    # 值是 (提供者 id, 索引) —— 见 _autowire_required 的说明
                    merged.setdefault(str(dt), (lid, i))
        return merged

    # ---- 自动注入 ------------------------------------------------------

    def _model_provided_types(self) -> frozenset[str]:
        """模型源能提供的输出端口类型集合。

        用于判断"某节点的必需端口能否由模型源按类型补上"。

        ⚠️ **必须遍历模型源规则里的全部候选，而不是只看第一个可用的。**
        取第一个的写法在"单一加载器"下没问题（谁提供 MODEL/CLIP/VAE 都一样），
        但对**分离加载**是错的：Z-Image 的候选只有 `UNETLoader`（输出 MODEL），
        而 CLIP 与 VAE 来自它声明的辅助加载器。

        历史上这里写死 `model.checkpoint` + `first_available` ——
        于是 Z-Image 环境下 `CLIPTextEncode.clip` 会被判成"模型源提供不了"，
        编译器转而去找图中"最近的同类型提供者"，接出一条**语义错位**的边。
        """
        src_rule = self.rules.get(self.model_source_kind)
        if src_rule is None:
            return frozenset()

        types: set[str] = set()
        # 主加载器：**遍历全部候选**（不同环境可能命中不同实现）
        for ct in src_rule.class_types:
            s = self.matrix.spec(ct)
            if s is not None:
                types |= {str(d) for d in s.outputs}
        # 辅助加载器（如 Z-Image 的 CLIPLoader / VAELoader）
        for lkind in src_rule.requires_loader:
            lr = self.rules.get(lkind)
            if lr is None:
                continue
            for ct in lr.class_types:
                s = self.matrix.spec(ct)
                if s is not None:
                    types |= {str(d) for d in s.outputs}
        return frozenset(types)

    def _inject_model_source(
        self,
        graph: dict[str, Any],
        ir: Mapping[str, Any],
        notices: list[DegradationNotice],
        auto_injected: list[str],
    ) -> dict[str, int]:
        """注入模型加载器，并返回 ``端口类型 -> outputIndex`` 映射。

        **为什么返回映射**：其他节点可能有一个"必需但在 IR 中未声明"的端口，
        其类型正好由模型加载器提供（最典型的是 `CLIPTextEncode.clip`）。
        有了这张映射，编译器就能**按类型**把它接上，而不是产出一张
        "必需端口缺输入"的图（那会被真实 ComfyUI 以
        `required_input_missing` 拒绝 —— 端到端验证抓出的缺陷 1）。

        为什么需要它：用户（或上游 Agent）常认为"模型源理所当然存在"而省略它，
        导致编译出的图缺少 MODEL/CLIP 上游而无法执行。自动注入把这类隐性缺口
        变成显式的图节点，并在通知里说明。
        """
        kind = self.model_source_kind
        rule = self.rules.get(kind)
        assert rule is not None
        chosen = self.matrix.first_available(rule.class_types)
        if chosen is None:
            raise CompileError(
                CompileErrorCode.MISSING_REQUIRED_NODE,
                f"无法自动注入模型源（{kind}）："
                f"候选 {list(rule.class_types)} 均不存在",
                detail={"installHint": rule.install_hint},
            )
        # 模型源的节点 id 按 kind 推导 —— 不同模型源用不同 id，
        # 避免"换模型源后 id 复用"导致 `_model_source_id_for` 找错节点。
        src_id = (self.MODEL_SOURCE_ID if kind == "model.checkpoint"
                  else f"n_src_{kind.replace('.', '_')}")

        def outputs_map(ct: str, node_id: str) -> dict[str, tuple[str, int]]:
            """端口类型 -> (提供者节点 id, 输出索引)。

            ⚠️ 值里**必须带节点 id**。早期版本只返回裸索引，
            于是调用方只能假设"提供者一定是模型源节点" ——
            而自动注入的前置加载器（如 `ModelPatchLoader`）**不是**模型源，
            结果它的输出被接到了模型源上：图看起来完整、编译不报错，
            **语义完全错位**。
            """
            s = self.matrix.spec(ct)
            if s is None:
                return {}
            return {str(dt): (node_id, i) for i, dt in enumerate(s.outputs)}

        # 优先使用 IR 中显式声明的模型源节点
        #
        # ⚠️ 这里曾经写死 `kind == "model.checkpoint"`。改成 `self.model_source_kind`
        # 是必需的：否则 Z-Image 的 IR 声明了 `model.source_zimage` 也**不被承认**，
        # 编译器会再注入一个 UNETLoader —— 图里出现**两个模型源**，
        # 它们各自带自己的权重参数，到底哪个生效取决于谁先被扫到。
        explicit = next(
            (nd for nd in ir.get("nodes", ())
             if nd.get("kind") == kind), None
        )
        # 承载文件名的参数从 `param_map` 取，不硬编码 ——
        # CheckpointLoaderSimple 用 `ckpt_name`，UNETLoader 用 `unet_name`。
        # 写死的后果是把 `ckpt_name` 塞进 UNETLoader，产生**语义错位的参数**。
        file_param = next(
            (p for p in rule.param_map.values()
             if p in {"ckpt_name", "unet_name", "model_name"}), "ckpt_name")
        ckpt = self.default_checkpoint
        if explicit:
            ckpt = ((explicit.get("params") or {}).get(file_param)
                    or (explicit.get("params") or {}).get("ckpt_name")
                    or ckpt)
            # 显式节点用其自身 id，避免重复注入
            graph[explicit["id"]] = {
                "class_type": chosen,
                # ⚠️ **必须保留该节点已有的其他参数。**
                #
                # 这里曾经写成 `inputs={file_param: ckpt}` —— 直接重建，
                # 于是丢掉 `_apply_params` 刚写进去的一切（如 UNETLoader 的
                # `weight_dtype`）。后果是 ComfyUI 以
                # `required_input_missing: weight_dtype` 拒绝整张图，
                # 而本地编译**不报任何错**、图看起来完整。
                "inputs": {**(graph.get(explicit["id"], {}).get("inputs") or {}),
                           file_param: ckpt},
                "_meta": {"title": (explicit.get("meta") or {}).get("title", "模型加载"),
                          "irNodeId": explicit["id"]},
            }
            auto_injected.append(explicit["id"])
            return self._inject_aux_loaders(
                graph, rule, chosen, explicit["id"],
                explicit.get("params") or {},
                outputs_map(chosen, explicit["id"]), notices, auto_injected)

        graph[src_id] = {
            "class_type": chosen,
            "inputs": {file_param: ckpt},
            "_meta": {"title": "模型加载（自动注入）", "irNodeId": src_id},
        }
        auto_injected.append(src_id)
        notices.append(DegradationNotice(
            kind=NoticeKind.AUTO_INJECTED, ir_node_id=src_id,
            severity=Severity.INFO.value, frm="(absent)", to=f"{chosen}({ckpt})",
            reason="IR 未声明模型源，但下游节点需要 MODEL/CLIP",
            impact="已自动补齐；若需更换底模请在 IR 中显式声明 model.checkpoint 节点",
            ui_hint=f"在 IR 中添加 {kind} 节点以显式控制底模",
        ))

        return self._inject_aux_loaders(
            graph, rule, chosen, src_id, {},
            outputs_map(chosen, src_id), notices, auto_injected)

    def _inject_aux_loaders(
        self,
        graph: dict[str, Any],
        rule: CompileRule,
        chosen: str,
        src_id: str,
        params: Mapping[str, Any],
        merged: dict[str, tuple[str, int]],
        notices: list[DegradationNotice],
        auto_injected: list[str],
    ) -> dict[str, tuple[str, int]]:
        """注入模型源声明的**辅助加载器**（分离加载用）。

        与 `_inject_loader` 的分工：
          · `_inject_loader`  —— "**某个应用器节点**缺一个输入，给它补个加载器"
          · 本方法        —— "**模型源本身**由多个节点组成"（如 Z-Image 的 CLIP/VAE）

        两者都产出 `类型 -> (节点 id, 索引)`，下游消费方式一致。

        ⚠️ **必须被"显式声明"与"自动注入"两条路径都调用。**
        早期版本只放在自动注入那条路径的末尾 —— 于是 IR 显式声明模型源时，
        辅助加载器不会被建出来，下游只好退回"按类型找最近的提供者"，
        从"模型源的一部分"退化成"补救性接线"。
        """
        if rule.requires_loader:
            for lkind, ldtype in rule.requires_loader.items():
                lrule = self.rules.get(lkind)
                if lrule is None:
                    continue
                lchosen = self.matrix.first_available(lrule.class_types)
                if lchosen is None:
                    continue
                # id 只取 lkind 的末段 —— 全名会得到 `n_src_model_source_zimage_zimage_clip`
                # 这种又长又重复的名字，读图时很难看
                # 只取 lkind 的末段 —— 否则 `model.source_zimage` 与
                # `model.zimage_clip` 会拼出 `source_zimage_zimage_clip`
                # 这种重复的怪名字
                lid = f"n_{lkind.split('.')[-1]}"
                if lid not in graph:
                    linputs: dict[str, Any] = {}
                    for pname, dflt in lrule.defaults.items():
                        # IR 的模型源节点可以携带辅助加载器的权重名 ——
                        # 一个节点写三个文件名，比逼用户建三个节点自然得多。
                        linputs[pname] = (params or {}).get(pname, dflt)
                    lspec = self.matrix.spec(lchosen)
                    if lspec is not None:
                        linputs = {k: v for k, v in linputs.items()
                                   if lspec.has_port(k)}
                        for i, dt in enumerate(lspec.outputs):
                            if str(dt) == ldtype:
                                merged[str(dt)] = (lid, i)
                    graph[lid] = {
                        "class_type": lchosen,
                        "inputs": linputs,
                        "_meta": {"title": f"{lkind}（模型源的一部分，自动注入）",
                                  "irNodeId": src_id,
                                  "partOfModelSource": rule.kind},
                    }
                    auto_injected.append(lid)
                    notices.append(DegradationNotice(
                        kind=NoticeKind.AUTO_INJECTED, ir_node_id=src_id,
                        severity=Severity.INFO.value,
                        frm=f"{rule.kind} 的 {ldtype} 需独立加载器",
                        to=f"{lchosen} -> {ldtype}",
                        reason=(f"模型源 {rule.kind} 是**分离加载**："
                                f"{ldtype} 不来自主加载器 {chosen}，"
                                f"已自动注入 {lchosen}"),
                        impact="模型源完整，下游按端口类型接线",
                        ui_hint=f"如需指定 {ldtype} 的权重，请在 IR 中显式声明 {lkind}",
                    ))
        return merged

    # ---- 文件型需求 ----------------------------------------------------

    def _file_requirements(self, ir: Mapping[str, Any]
                           ) -> dict[str, tuple[tuple[str, str], ...]]:
        """从 IR 提取文件型输入需求，**按端口名**分组，并保留**来源 kind**。

        按端口名而非 class_type 分组，因为：
          - 同一 kind 可能因降级映射到不同的 class_type（端口名才是稳定标识）
          - 能力矩阵需要 ``(class_type, port)`` 二元组才能查对象文件候选列表

        **为什么必须保留来源 kind**：``FILE_MISSING`` 漂移对象本身**不带**规则信息，
        因此"这个缺失文件来自必需规则还是可选规则"必须在源头记下来 ——
        否则缺一个**可选**的身份 LoRA 就会把整条管线硬阻断。

        重要：IR 未显式声明 model.checkpoint 时，编译器会自动注入默认底模。
        因此这里必须**把默认底模一并计入需求** —— 否则"底模文件不在目标环境"
        这一最致命的故障会被漏检，编译期照过、执行期才失败。

        返回形如 ``{"lora_name": (("identity.lora", "linwan_v3.safetensors"),)}``。
        """
        files: dict[str, set[tuple[str, str]]] = {}
        bindings = ir.get("bindings") or {}

        def add(port: str, kind: str, value: str) -> None:
            files.setdefault(port, set()).add((kind, value))

        # kind -> 承载文件值的 ComfyUI 端口名
        port_of_kind = {
            "identity.lora": "lora_name",
            "model.checkpoint": "ckpt_name",
        }

        declared_ckpt = False
        needs_model_source = False
        for node in ir.get("nodes", ()):
            kind = node.get("kind")
            rule = self.rules.get(kind) if kind else None
            if rule is not None and rule.requires_model_source:
                needs_model_source = True

            if kind == "identity.lora":
                sem = node.get("semantic") or {}
                cref = sem.get("characterRef")
                bind = bindings.get(cref or "")
                if isinstance(bind, Mapping) and bind.get("loraFile"):
                    add(port_of_kind["identity.lora"], str(kind), str(bind["loraFile"]))
            elif kind == "model.checkpoint":
                ck = (node.get("params") or {}).get("ckpt_name")
                if ck:
                    add(port_of_kind["model.checkpoint"], str(kind), str(ck))
                    declared_ckpt = True

        if needs_model_source and not declared_ckpt:
            add(port_of_kind["model.checkpoint"], "model.checkpoint",
                self.default_checkpoint)

        return {p: tuple(sorted(v)) for p, v in files.items()}


def _dedupe_notices(notices: Iterable[DegradationNotice]) -> list[DegradationNotice]:
    """按内容去重通知，并保持首次出现顺序。

    去重是必要的：同一轮编译中多个节点可能触发内容完全一致的自动注入/替换通知，
    重复项会污染 UI 与审片报告，也会让"降级计数"这类指标失真。
    """
    seen: set[tuple] = set()
    out: list[DegradationNotice] = []
    for n in notices:
        d = n.as_dict()
        key = (
            d["kind"], d["irNodeId"], d["severity"], d["from"], d["to"],
            d["reason"], tuple(d.get("affectedConstraints", ())),
            canonical_json(d.get("thresholdAdjusted")) if d.get("thresholdAdjusted") else "",
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out
