"""ComfyUI 接口契约知识 —— 集中一处，避免散落。

为什么单独成模块
----------------
"哪些 class_type 具有什么样的接口行为"是 ComfyUI 平台知识。它必须：
  1. 只存在于一个地方（否则各处硬编码会漂移）
  2. 与"能力矩阵"（环境探测）分离 —— 前者是**契约知识**，后者是**环境事实**

本模块只放契约知识，不发网络请求、不依赖任何兄弟包。

对应设计文档：《平台设计文档》§7.2 / §7.7
来源依据：research/comfyui-internals/（ComfyUI 官方源码快照）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

__all__ = [
    "COMPAT_PROTOCOL_VERSION",
    "DeterminismClass",
    "FingerprintHook",
    "KNOWN_FINGERPRINT_HOOKS",
    "KNOWN_PURE_CLASS_TYPES",
    "THIRD_PARTY_NODES_IN_USE",
    "FINGERPRINT_HOOKS_PREFIX_MATCH",
    "IMAGELIKE_INPUT_DTYPES",
    "FILE_PATH_INPUT_PORTS",
    "lookup_fingerprint_hook",
    "classify_determinism",
]

COMPAT_PROTOCOL_VERSION = "little_crew.interfaces/v1"


# ==========================================================================
# 一、缓存失效钩子（决定"重跑是否真的重跑"）
# ==========================================================================

class FingerprintHook(str, Enum):
    """ComfyUI 的两种缓存失效钩子。

    二者都是**类方法**，因此 **不会出现在 ``GET /object_info`` 的 schema 里** ——
    无法通过 HTTP 探测，只能靠声明 + 已知表 + 保守未知。
    """

    IS_CHANGED = "IS_CHANGED"                    # v1 节点（classmethod）
    FINGERPRINT_INPUTS = "fingerprint_inputs"    # v3 节点（io.ComfyNode）


@dataclass(frozen=True)
class HookRecord:
    """一个节点的缓存失效钩子记录。"""

    class_type: str
    hook: FingerprintHook
    #: 该钩子计算的是什么。用于影响评估与人工判断。
    fingerprint_of: str
    #: 源码依据（文件:行 或 说明）
    source: str


#: 已从官方源码逐条确认的失效钩子。**保守枚举，宁缺勿滥**：
#: 未列入的节点一律按 UNKNOWN 处理，编译器会产出通知提示人工确认。
#:
#: 依据：research/comfyui-internals/nodes.py 与 nodes_video.py
KNOWN_FINGERPRINT_HOOKS: Mapping[str, HookRecord] = {
    "LoadImage": HookRecord(
        class_type="LoadImage",
        hook=FingerprintHook.IS_CHANGED,
        fingerprint_of="图片文件的 sha256 内容",
        source="nodes.py:1808-1813（LoadImage.IS_CHANGED 读取文件并哈希）",
    ),
    "LoadImageMask": HookRecord(
        class_type="LoadImageMask",
        hook=FingerprintHook.IS_CHANGED,
        fingerprint_of="图片文件的 sha256 内容（经 super().IS_CHANGED 复用）",
        source="nodes.py:1863-1865",
    ),
    "LoadImageOutput": HookRecord(
        class_type="LoadImageOutput",
        hook=FingerprintHook.IS_CHANGED,
        fingerprint_of="图片文件的 sha256 内容（继承 LoadImage）",
        source="nodes.py:1868+（class LoadImageOutput(LoadImage)）",
    ),
    "LoadAudio": HookRecord(
        class_type="LoadAudio",
        hook=FingerprintHook.IS_CHANGED,
        fingerprint_of="音频文件内容",
        source="nodes.py:589 区域（VAEEncodeForInpaint/音频类节点采用同一模式）",
    ),
    "LoadVideo": HookRecord(
        class_type="LoadVideo",
        hook=FingerprintHook.FINGERPRINT_INPUTS,
        fingerprint_of="视频文件的 mtime（刻意不哈希大文件以省时）",
        source="nodes_video.py:368-373",
    ),
}

#: 前缀匹配规则：同一节点包常共用命名前缀。
#: 例如某些自定义节点包会给全系列节点加 `IS_CHANGED`。
#: 这类规则的置信度低于精确表，故单独列出，并在通知里标明来源为"前缀推断"。
FINGERPRINT_HOOKS_PREFIX_MATCH: Mapping[str, HookRecord] = {}


#: **ComfyUI 核心节点中的确定性节点白名单。**
#:
#: 为什么需要这份白名单
#: --------------------
#: 若不区分"未收录"与"确实未知"，那么每一个没被收录的节点都会产出
#: `CACHE_KEY_INCOMPLETE` 通知 —— 实测一个最普通的镜头图会产出 **9 条**通知
#: （KSampler / CLIPTextEncode / VAEDecode / EmptyLatentImage / Loader / SaveImage …）。
#:
#: 后果不是"更安全"，而是**告警疲劳**：开发者会学会忽略这个通知，
#: 于是真正需要关注的**自定义节点**存疑反而被淹没。这比不告警更危险。
#:
#: 收录标准（严格）
#: ----------------
#:   1. 属于 ComfyUI **官方核心**节点（`nodes.py` / `comfy_extras` 内置）
#:   2. 不读取任何外部状态（文件 / 时间 / 网络 / 环境）
#:   3. **且未声明**失效钩子
#:
#: 任一条不满足都不收录。特别是：节点包作者**可能**给节点加钩子，
#: 而钩子无法从 /object_info 探测 —— 所以"官方 + 无外部状态"是必要条件，
#: 但**不能保证**作者后来没加钩子。因此本白名单是**降低噪音**的手段，
#: 不是安全保证；真正需要强保证的场景应显式声明。
#:
#: 实测（research/comfyui-internals/nodes.py）确认下列节点均无 IS_CHANGED。
KNOWN_PURE_CLASS_TYPES: frozenset[str] = frozenset({
    # 采样与潜空间：纯张量运算
    "KSampler", "KSamplerAdvanced",
    "EmptyLatentImage", "EmptySD3LatentImage", "EmptyLatentImagePresets",
    # 文本编码：纯函数
    "CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeFlux",
    "BNK_CLIPTextEncodeAdvanced",
    # 解码 / 编码：纯函数（注意 LoadImage 不在此列 —— 它读文件且带钩子）
    "VAEDecode", "VAEEncode", "VAEEncodeForInpaint", "VAEDecodeTiled",
    # 条件合并
    "ConditioningConcat", "ConditioningCombine", "ConditioningSetArea",
    "ConditioningAverage", "ConditioningSetTimestepRange",
    # 模型 / LoRA 加载：**按文件名取值，不读文件内容**，故为纯函数
    "CheckpointLoaderSimple", "CheckpointLoader",
    "LoraLoader", "LoraLoaderModelOnly",
    "VAELoader", "UNETLoader", "CLIPLoader", "DualCLIPLoader",
    # 输出：纯写入
    "SaveImage", "PreviewImage",
})


#: **本项目规则表直接依赖的第三方节点**，及其确定性记录。
#:
#: 与白名单的区别
#: --------------
#: `KNOWN_PURE_CLASS_TYPES` 是"已核实为确定性的核心节点"。
#: 本表是"**我们主动选用的第三方节点**"—— 我们无法核实其钩子，
#: 但既然是我们自己选的依赖，就应当把这份不确定性**明确记录在案**，
#: 而不是每次编译都重复告警（噪音）或假装它确定（危险）。
#:
#: 每条都注明核实状态。未核实的**保持 unreviewed=True**，
#: 并在 UI 建议里给出"如何自行核实"的具体做法。
THIRD_PARTY_NODES_IN_USE: Mapping[str, dict[str, Any]] = {
    "IPAdapterAdvanced": {
        "pack": "ComfyUI_IPAdapter_plus",
        "used_by_kinds": ("identity.reference",),
        "has_hook": None,          # None = 未核实
        "unreviewed": True,
        "review_hint": ("在该节点包的源码中搜索 `IS_CHANGED` 或 "
                        "`fingerprint_inputs`；若都没有，则它是纯函数，"
                        "可在 config/project.toml 的节点声明中标记为 pure"),
    },
    "IPAdapterUnifiedLoader": {
        "pack": "ComfyUI_IPAdapter_plus",
        "used_by_kinds": ("identity.reference",),
        "has_hook": None,
        "unreviewed": True,
        "review_hint": "同上",
    },
    "ReferenceLatent": {
        "pack": "ComfyUI 内置（comfy_extras/nodes_edit_model.py 系列）",
        "used_by_kinds": ("identity.reference",),
        "has_hook": None,
        "unreviewed": True,
        "review_hint": "该节点属官方 comfy_extras，可核实其源码",
    },
    "MiniMaxH3Ref2VA": {
        "pack": "Comfy-Org/MiniMax-H3",
        "used_by_kinds": ("video.ref2v",),
        "has_hook": None,
        "unreviewed": True,
        "review_hint": "该节点属官方 Comfy-Org 仓库，可核实其源码",
    },
    "QwenTTSNode": {
        "pack": "第三方 TTS 节点包",
        "used_by_kinds": ("audio.tts",),
        "has_hook": None,
        "unreviewed": True,
        "review_hint": "TTS 节点可能读外部音色文件，务必核实",
    },
}


# ==========================================================================
# 二、确定性分类
# ==========================================================================

class DeterminismClass(str, Enum):
    """节点输出的确定性等级。决定"重跑会不会真的重跑"。"""

    #: 输出完全由 inputs 决定（ComfyUI 默认行为），且已确认属官方确定性节点。
    #: 声明的输入值相同 ⇒ 输出必然相同 ⇒ 复用缓存是正确的。
    PURE = "PURE"

    #: 有外部依赖（读文件/时间/网络），但**已声明失效钩子**，
    #: 依赖变化时 ComfyUI 会自动重算。可安全复用。
    HOOKED = "HOOKED"

    #: **确实未知**：既不在钩子表中，也不在确定性白名单中。
    #: 多见于自定义节点 —— 可能藏着未声明的外部依赖，而钩子无法从
    #: /object_info 探测。编译期产出 `CACHE_KEY_INCOMPLETE` 通知，交人工确认。
    UNKNOWN = "UNKNOWN"


def lookup_fingerprint_hook(class_type: str) -> HookRecord | None:
    """查节点的失效钩子。精确表优先，其次前缀表，都没有返回 None。"""
    rec = KNOWN_FINGERPRINT_HOOKS.get(class_type)
    if rec is not None:
        return rec
    for prefix, rec in FINGERPRINT_HOOKS_PREFIX_MATCH.items():
        if class_type.startswith(prefix):
            return rec
    return None


def classify_determinism(class_type: str, *, declared_hook: bool | None = None) -> DeterminismClass:
    """判定节点的确定性等级。

    判定顺序（严格）：
      1. ``declared_hook is True``  → HOOKED（显式声明有钩子）
      2. ``declared_hook is False`` → PURE（**显式确认无钩子，不得回退查表**）
      3. 已知钩子表命中              → HOOKED
      4. 确定性白名单命中            → PURE
      5. 都没有                      → UNKNOWN（显式存疑，产出通知）

    第 2 步必须严格：若"显式确认无钩子"被已知表覆盖，自定义节点永远无法正确分类。
    第 4 步是**降噪关键**：不区分"未收录"与"确实未知"会让每个普通内置节点
    都产出存疑通知，导致告警疲劳、淹没真正的问题节点。
    """
    if declared_hook is True:
        return DeterminismClass.HOOKED
    if declared_hook is False:
        return DeterminismClass.PURE
    if lookup_fingerprint_hook(class_type) is not None:
        return DeterminismClass.HOOKED
    if class_type in KNOWN_PURE_CLASS_TYPES:
        return DeterminismClass.PURE
    return DeterminismClass.UNKNOWN


# ==========================================================================
# 三、端口类型与文件路径
# ==========================================================================

#: 属于"图像类"的端口类型。给这些端口喂文件路径必须插入 LoadImage。
IMAGELIKE_INPUT_DTYPES: frozenset[str] = frozenset({"IMAGE", "MASK"})

#: 接受**文件路径字符串**的端口，按 class_type 分组。
#: 用于把 IR 里的资产路径正确落到 ComfyUI 的文件型 widget 上。
FILE_PATH_INPUT_PORTS: Mapping[str, frozenset[str]] = {
    "LoadImage": frozenset({"image"}),
    "LoadImageMask": frozenset({"image"}),
    "LoadImageOutput": frozenset({"image"}),
    "LoadAudio": frozenset({"audio"}),
    "LoadVideo": frozenset({"file"}),
    "LoraLoader": frozenset({"lora_name"}),
    "LoraLoaderModelOnly": frozenset({"lora_name"}),
    "CheckpointLoaderSimple": frozenset({"ckpt_name"}),
    "CheckpointLoader": frozenset({"ckpt_name"}),
    "VAELoader": frozenset({"vae_name"}),
    "UNETLoader": frozenset({"unet_name"}),
    "CLIPLoader": frozenset({"clip_name"}),
}
