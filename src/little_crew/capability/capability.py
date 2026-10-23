"""ComfyUI 能力矩阵探测与三态漂移检测。

对应设计文档 §7.2 / §7.3
------------------------
本模块解决一个具体问题：**目标 ComfyUI 环境的节点目录会随自定义节点版本漂移，
而模板填充范式在此处静默产出错图。**

本模块提供三件事：

1. **能力矩阵**：从 ``GET /object_info`` 抓取节点目录，构建可查询的能力视图。
2. **环境哈希**（``catalog_hash``）：把"环境"变成一个可哈希实体，参与缓存键。
   环境一变，所有依赖该环境的缓存自动失效 —— 根治"换了节点版本后旧产物静默错误"。
3. **三态漂移检测**：节点缺失 / 端口变更 / 枚举值缩减，逐一给出处置建议。

设计原则
--------
- **探测是纯函数**：给定 object_info 快照，产出确定的能力矩阵与哈希。
  网络抓取与解析分离，便于离线测试与回归。
- **漂移必须显式**：任何不一致都产出结构化 ``Drift`` 对象，绝不安静降级。
- **文件名型输入单独校验**：``lora_name`` / ``ckpt_name`` 等文件型 widget 的
  候选列表来自目标环境，缺失即代表模型文件不在位 —— 这是模板填充的经典故障点。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from little_crew.capability.comfy_interfaces import (
    IMAGELIKE_INPUT_DTYPES,
    KNOWN_FINGERPRINT_HOOKS,
    DeterminismClass,
    FingerprintHook,
    classify_determinism,
    lookup_fingerprint_hook,
)

__all__ = [
    "CATALOG_PROTOCOL_VERSION",
    "DriftKind",
    "Severity",
    "Drift",
    "NodeSpec",
    "CapabilityMatrix",
    "probe_from_object_info",
    "parse_object_info",
]

CATALOG_PROTOCOL_VERSION = "little_crew.catalog/v1"

#: 认定为"文件型输入"的 widget 名后缀。这些输入的值必须是目标环境上真实存在的文件。
_FILE_INPUT_SUFFIXES: tuple[str, ...] = (
    "name", "file", "path", "ckpt_name", "lora_name", "vae_name",
    "control_net_name", "ipadapter_file", "unet_name", "clip_name",
    "style_model_name", "embedding_name", "gligen_name", "model_name",
)

#: ComfyUI 在 object_info 中表示"下拉候选"的形状为 [ [候选...], {配置} ]，
#: 而类型声明的形状为 [ "TYPE", {配置} ]。据此区分枚举输入与连接输入。
_ENUM_TYPE_TOKENS = {"COMBO", "ENUM"}


class DriftKind(str, Enum):
    NODE_MISSING = "NODE_MISSING"            # 节点不存在
    PORT_MISSING = "PORT_MISSING"            # 端口不存在
    PORT_ADDED = "PORT_ADDED"                # 端口新增（通常无害，记录即可）
    PORT_TYPE_CHANGED = "PORT_TYPE_CHANGED"  # 端口类型变化
    #: 声明的端口类型与实际不符 —— **编译前就能拦住"把文件路径喂给 IMAGE 端口"**
    PORT_DTYPE_MISMATCH = "PORT_DTYPE_MISMATCH"
    ENUM_VALUE_MISSING = "ENUM_VALUE_MISSING"  # 枚举候选缺失（如 sampler 被移除）
    FILE_MISSING = "FILE_MISSING"            # 文件型输入的文件不在位
    OUTPUT_COUNT_CHANGED = "OUTPUT_COUNT_CHANGED"
    NODE_DEPRECATED = "NODE_DEPRECATED"
    #: 缓存失效钩子发生变化 —— 改变缓存键计算方式，属 ABI 变化
    FINGERPRINT_HOOK_CHANGED = "FINGERPRINT_HOOK_CHANGED"
    #: 确定性等级变化（多因已知钩子表更新）
    DETERMINISM_CHANGED = "DETERMINISM_CHANGED"


class Severity(str, Enum):
    INFO = "info"          # 记录，不影响生成
    WARNING = "warning"    # 可降级继续，质量可能受影响
    BLOCKER = "blocker"    # 必须阻断编译


@dataclass(frozen=True)
class Drift:
    """一条漂移记录。**必须携带处置建议**，否则运维无法自助修复。"""

    kind: DriftKind
    severity: Severity
    class_type: str
    port: str | None = None
    detail: str = ""
    expected: Any = None
    observed: Any = None
    remedy: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "classType": self.class_type,
            "port": self.port,
            "detail": self.detail,
            "expected": self.expected,
            "observed": self.observed,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class PortSpec:
    name: str
    dtype: str                                   # "MODEL" / "COMBO" / ...
    required: bool = True
    enum_values: tuple[str, ...] | None = None    # 仅 COMBO 有
    default: Any = None
    is_file_input: bool = False

    @property
    def is_link(self) -> bool:
        """该端口是否为**连线端口**（需要上游节点供给）。

        ComfyUI 的输入分两类，**性质完全不同**：

        | 类别 | 判据 | 缺输入时 |
        |---|---|---|
        | **连线端口** | 类型是张量型（MODEL / CLIP / LATENT / CONDITIONING / …） | ComfyUI **直接拒绝**（`required_input_missing`） |
        | **widget 端口** | 类型是字面量型（INT / FLOAT / STRING / BOOLEAN / COMBO） | 用节点自带的默认值，**不会失败** |

        这个区分是"产出完整性终检"的判据：只有**连线端口**缺失才算图不完整。
        widget 缺值不构成缺陷 —— 把它当缺陷会让"只声明部分参数的 IR"被误判为错误。
        """
        return self.dtype.upper() in _LINK_PORT_DTYPES

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "dtype": self.dtype,
                             "required": self.required}
        if self.enum_values is not None:
            d["enumValues"] = list(self.enum_values)
        if self.default is not None:
            d["default"] = self.default
        if self.is_file_input:
            d["isFileInput"] = True
        return d


#: **连线型**端口的数据类型集合。
#: 其余（INT / FLOAT / STRING / BOOLEAN / COMBO 及各种自定义标量）都是 widget 端口。
#:
#: ⚠️ **这张表是唯一的真源**：`compiler._KNOWN_PORT_TYPES`（重接时的类型校验）
#: 直接由它派生，不再各写一份。
#:
#: 2026-10-23 审计 H-14：两张手工维护的表曾经漂移 ——
#: `MODEL_PATCH` 只有编译器认、capability 不认（于是 3.5) 终检**漏检**它），
#: `PHOTOMAKER` 反过来只有 capability 认（于是重接时**跳过**类型校验）。
#: 同一个类型在两个方向上各缺一次 = **两条防线同时失效**。
LINK_PORT_DTYPES: frozenset[str] = frozenset({
    "MODEL", "CLIP", "VAE", "CONDITIONING", "LATENT", "IMAGE", "MASK",
    "AUDIO", "VIDEO", "CONTROL_NET", "CLIP_VISION", "CLIP_VISION_OUTPUT",
    "STYLE_MODEL", "GLIGEN", "UPSCALE_MODEL", "SAMPLER", "SIGMAS",
    "GUIDER", "NOISE", "WEBCAM", "HOOKS", "IPADAPTER", "PHOTOMAKER",
    # ComfyUI 的自定义张量类型：加载器注入、由应用器消费。
    # 它参与 `composition.model_patch` 的连线，**必须**算连线端口。
    "MODEL_PATCH",
})

#: 兼容旧名（外部可能引用过私有名）
_LINK_PORT_DTYPES = LINK_PORT_DTYPES


@dataclass(frozen=True)
class NodeSpec:
    class_type: str
    category: str
    inputs_required: Mapping[str, PortSpec]
    inputs_optional: Mapping[str, PortSpec]
    outputs: tuple[str, ...]
    output_node: bool
    deprecated: bool = False
    description: str = ""
    #: 缓存失效钩子。**不从 /object_info 来** —— 它无法通过 HTTP 探测，
    #: 由 comfy_interfaces 的已知表 + 显式声明提供。
    fingerprint_hook: str | None = None
    #: 该钩子计算的是什么（用于影响评估）
    fingerprint_of: str = ""
    #: 钩子来源："known_table" | "prefix_match" | "declared" | 空
    fingerprint_source: str = ""
    #: 确定性等级。见 comfy_interfaces.DeterminismClass
    determinism: str = DeterminismClass.UNKNOWN.value

    def port(self, name: str) -> PortSpec | None:
        return self.inputs_required.get(name) or self.inputs_optional.get(name)

    def has_port(self, name: str) -> bool:
        return name in self.inputs_required or name in self.inputs_optional

    @property
    def has_fingerprint_hook(self) -> bool:
        return self.fingerprint_hook is not None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "classType": self.class_type,
            "category": self.category,
            "required": {k: v.as_dict() for k, v in sorted(self.inputs_required.items())},
            "optional": {k: v.as_dict() for k, v in sorted(self.inputs_optional.items())},
            "outputs": list(self.outputs),
            "outputNode": self.output_node,
            "deprecated": self.deprecated,
            "description": self.description,
            "determinism": self.determinism,
        }
        if self.fingerprint_hook:
            d["fingerprintHook"] = self.fingerprint_hook
            d["fingerprintOf"] = self.fingerprint_of
            d["fingerprintSource"] = self.fingerprint_source
        return d


# ==========================================================================
# 解析 object_info
# ==========================================================================

def _is_enum_decl(type_decl: Any) -> tuple[bool, tuple[str, ...] | None]:
    """判断一个 input 声明是枚举候选还是类型名。

    ComfyUI 的两种形状：
        连接输入： ["MODEL", {"tooltip": "..."}]
        枚举输入： [["euler", "dpmpp_2m", ...], {"default": "euler"}]
    少数自定义节点会写成 [["a","b"], {...}, "extra"] 这类超长数组，此处容错。
    """
    if not isinstance(type_decl, (list, tuple)) or not type_decl:
        return False, None
    head = type_decl[0]
    if isinstance(head, (list, tuple)):
        return True, tuple(str(v) for v in head)
    if isinstance(head, str) and head.upper() in _ENUM_TYPE_TOKENS:
        return True, None
    return False, None


def _extract_default(type_decl: Any) -> Any:
    if not isinstance(type_decl, (list, tuple)) or len(type_decl) < 2:
        return None
    opts = type_decl[1]
    if isinstance(opts, Mapping) and "default" in opts:
        return opts["default"]
    return None


def _looks_like_file_input(name: str, dtype: str, enum_values: tuple[str, ...] | None) -> bool:
    """文件型输入的判定：候选由环境文件系统决定，且名字带文件语义。

    保守策略：只有当它是枚举输入、且名字命中文件后缀、且候选里出现过
    带扩展名的条目时，才认定为文件型。避免把 sampler_name 误判成文件。
    """
    if enum_values is None:
        return False
    if any(ext in name for ext in (".",)):
        pass
    name_l = name.lower()
    hit = any(name_l == s or name_l.endswith("_" + s) or name_l.endswith(s)
              for s in _FILE_INPUT_SUFFIXES)
    if not hit:
        return False
    # 候选里存在带扩展名的条目 -> 更像文件列表
    return any("." in v for v in enum_values if isinstance(v, str))


def _parse_port(name: str, type_decl: Any, *, required: bool) -> PortSpec:
    is_enum, enum_values = _is_enum_decl(type_decl)
    if is_enum:
        dtype = "COMBO"
    else:
        dtype = str(type_decl[0]) if isinstance(type_decl, (list, tuple)) and type_decl else "ANY"
    return PortSpec(
        name=name,
        dtype=dtype,
        required=required,
        enum_values=enum_values,
        default=_extract_default(type_decl),
        is_file_input=_looks_like_file_input(name, dtype, enum_values),
    )


def parse_object_info(
    raw: Mapping[str, Any],
    *,
    declared_hooks: Mapping[str, bool] | None = None,
) -> dict[str, NodeSpec]:
    """把 ``/object_info`` 的原始响应解析为 ``{class_type: NodeSpec}``。纯函数。

    **关键限制**：缓存失效钩子（``IS_CHANGED`` / ``fingerprint_inputs``）是
    ComfyUI 节点类的**类方法**，不出现在 ``/object_info`` 的 schema 里，
    因此**无法通过 HTTP 探测**。本函数按三级来源补全它：

      1. ``declared_hooks``（调用方显式声明，优先级最高 —— 自定义节点走这条）
      2. ``comfy_interfaces.KNOWN_FINGERPRINT_HOOKS``（已从官方源码逐条确认）
      3. 都没有 → ``DeterminismClass.UNKNOWN``，由编译器产出通知交人工确认

    第 3 条是刻意的保守选择：**"不知道"必须显式化为 UNKNOWN 并告知用户，
    而不是默认当作 PURE** —— 后者会静默复用可能失效的缓存。
    """
    declared_hooks = dict(declared_hooks or {})
    nodes: dict[str, NodeSpec] = {}
    for class_type, body in raw.items():
        if not isinstance(body, Mapping):
            continue
        inp = body.get("input") or {}
        required_raw = inp.get("required") or {}
        optional_raw = inp.get("optional") or {}

        required = {
            k: _parse_port(k, v, required=True)
            for k, v in required_raw.items()
        }
        optional = {
            k: _parse_port(k, v, required=False)
            for k, v in optional_raw.items()
        }

        out_raw = body.get("output") or []
        outputs = tuple(str(o) for o in (out_raw if isinstance(out_raw, (list, tuple)) else [out_raw]))

        ct = str(class_type)
        hook_name: str | None = None
        hook_of = ""
        hook_source = ""
        # 三态：True=显式声明有钩子 / False=显式声明无钩子 / None=未声明（查已知表）
        # 必须区分 False 与 None —— 前者是"确认没有"，后者是"不知道"。
        declared: bool | None = (declared_hooks[ct] if ct in declared_hooks else None)

        if declared is True:
            hook_name = FingerprintHook.IS_CHANGED.value
            hook_of = "由声明提供（内容未知）"
            hook_source = "declared"
        elif declared is False:
            hook_name = None            # 显式确认无钩子，**不得**回退到已知表
            hook_source = "declared_none"
        else:
            rec = lookup_fingerprint_hook(ct)
            if rec is not None:
                hook_name = rec.hook.value
                hook_of = rec.fingerprint_of
                hook_source = ("known_table"
                               if ct in KNOWN_FINGERPRINT_HOOKS else "prefix_match")

        determinism = classify_determinism(ct, declared_hook=declared)

        nodes[ct] = NodeSpec(
            class_type=ct,
            category=str(body.get("category") or ""),
            inputs_required=required,
            inputs_optional=optional,
            outputs=outputs,
            output_node=bool(body.get("output_node", False)),
            deprecated=bool(body.get("deprecated", False)),
            description=str(body.get("description") or ""),
            fingerprint_hook=hook_name,
            fingerprint_of=hook_of,
            fingerprint_source=hook_source,
            determinism=determinism.value,
        )
    return nodes


def probe_from_object_info(
    raw: Mapping[str, Any],
    *,
    endpoint: str = "unknown",
    comfy_version: str = "unknown",
    probed_at: str | None = None,
    extra_packs: Sequence[str] = (),
    declared_hooks: Mapping[str, bool] | None = None,
) -> "CapabilityMatrix":
    """从 object_info 快照构建能力矩阵。

    ``declared_hooks``：``{class_type: 是否有缓存失效钩子}``。
    用于两类场景：
      1. **自定义节点**：其钩子无法从 /object_info 探测，需人工声明
      2. **测试与回归**：显式构造"钩子有/无"的环境，验证 ABI 变化被检出
    未声明的节点按已知表查，查不到则为 UNKNOWN（不默认当作 PURE）。
    """
    return CapabilityMatrix(
        nodes=parse_object_info(raw, declared_hooks=declared_hooks),
        endpoint=endpoint,
        comfy_version=comfy_version,
        probed_at=probed_at,
        custom_node_packs=tuple(extra_packs),
    )


# ==========================================================================
# 能力矩阵
# ==========================================================================

@dataclass
class CapabilityMatrix:
    """目标 ComfyUI 环境的能力视图。可哈希、可查询、可做漂移检测。"""

    nodes: Mapping[str, NodeSpec]
    endpoint: str = "unknown"
    comfy_version: str = "unknown"
    probed_at: str | None = None
    custom_node_packs: tuple[str, ...] = ()
    _hash_cache: str | None = field(default=None, repr=False, compare=False)

    # ---- 查询 ----------------------------------------------------------

    def has(self, class_type: str) -> bool:
        return class_type in self.nodes

    def spec(self, class_type: str) -> NodeSpec | None:
        return self.nodes.get(class_type)

    def first_available(self, candidates: Iterable[str]) -> str | None:
        """按优先级返回第一个存在的 class_type。降级链选型的核心。"""
        for c in candidates:
            if c in self.nodes:
                return c
        return None

    def enum_values(self, class_type: str, port: str) -> tuple[str, ...] | None:
        spec = self.spec(class_type)
        if not spec:
            return None
        p = spec.port(port)
        return p.enum_values if p else None

    def node_count(self) -> int:
        return len(self.nodes)

    # ---- 环境哈希 ------------------------------------------------------

    def catalog_hash(self) -> str:
        """环境的规范哈希。参与 Tier-1 缓存键。

        对**每个节点的结构签名**做哈希，而非对原始 object_info 做哈希。
        原因：object_info 含 tooltip / description 等纯展示字段，它们变化
        不应使缓存失效（否则装个翻译插件就全量重跑）。

        纳入签名的量：
          - class_type、输入端口名、端口类型、是否必填
          - 枚举候选（排序后）、输出类型、是否废弃
          - **缓存失效钩子是否存在及其种类**（`determinism`）

        最后一项的必要性：`IS_CHANGED` / `fingerprint_inputs` 的存在与否
        **直接改变缓存键的计算方式**（见 ComfyUI `caching.py`
        `get_immediate_node_signature`，签名第二项就是 `is_changed`）。
        因此"某节点从无钩子变为有钩子"属于 **ABI 变化**，
        必须使环境哈希改变，否则会复用按旧缓存语义产出的结果。

        来源差异（known_table / declared / prefix_match）**不纳入哈希** ——
        同一事实的不同来源不应改变环境身份。但 `determinism` 纳入，
        因为 UNKNOWN 与 HOOKED 意味着不同的缓存行为保证。
        """
        if self._hash_cache:
            return self._hash_cache
        signature: list[dict[str, Any]] = []
        for ct in sorted(self.nodes):
            spec = self.nodes[ct]
            ports = []
            for section, ports_map in (("req", spec.inputs_required),
                                       ("opt", spec.inputs_optional)):
                for pname in sorted(ports_map):
                    p = ports_map[pname]
                    entry: dict[str, Any] = {
                        "s": section, "n": pname, "t": p.dtype,
                        "ev": sorted(p.enum_values) if p.enum_values is not None else None,
                    }
                    ports.append(entry)
            signature.append({
                "ct": ct,
                "ports": ports,
                "out": list(spec.outputs),
                "dep": spec.deprecated,
                # 缓存语义：钩子种类 + 确定性等级
                "fh": spec.fingerprint_hook,
                "det": spec.determinism,
            })
        material = json.dumps(
            {"protocol": CATALOG_PROTOCOL_VERSION, "nodes": signature},
            sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        )
        self._hash_cache = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        return self._hash_cache

    # ---- 漂移检测 ------------------------------------------------------

    def detect_drift(
        self,
        requirements: Mapping[str, "PortRequirement"],
        *,
        baseline: "CapabilityMatrix | None" = None,
    ) -> list[Drift]:
        """检测"编译所需的节点/端口/取值"与"环境实际提供"之间的差异。

        requirements 由编译规则表生成（见 compiler.py），形如：
            {"KSampler": PortRequirement(ports={"positive": "CONDITIONING"},
                                          enums={"sampler_name": ("dpmpp_2m",)},
                                          files={"lora_name": ("linwan_v3.safetensors",)})}

        baseline 非空时同时检测环境自身的变化（端口新增/类型变更/节点废弃）。
        """
        drifts: list[Drift] = []

        for ct in sorted(requirements):
            req = requirements[ct]
            spec = self.spec(ct)

            if spec is None:
                drifts.append(Drift(
                    kind=DriftKind.NODE_MISSING,
                    severity=Severity.BLOCKER if req.required else Severity.WARNING,
                    class_type=ct,
                    detail=f"目标环境缺少节点 {ct}",
                    remedy=req.install_hint or f"安装提供 {ct} 的自定义节点包",
                ))
                continue

            if spec.deprecated:
                drifts.append(Drift(
                    kind=DriftKind.NODE_DEPRECATED, severity=Severity.WARNING,
                    class_type=ct, detail=f"节点 {ct} 已被标记废弃",
                    remedy="迁移到官方推荐的替代节点",
                ))

            # 端口存在性与类型
            for pname in sorted(req.ports):
                want_type = req.ports[pname]
                p = spec.port(pname)
                if p is None:
                    drifts.append(Drift(
                        kind=DriftKind.PORT_MISSING,
                        severity=Severity.BLOCKER if req.required else Severity.WARNING,
                        class_type=ct, port=pname,
                        detail=f"节点 {ct} 上不存在输入端口 {pname}",
                        expected=want_type,
                        remedy=req.port_aliases.get(pname, "") or "检查节点版本是否匹配",
                    ))
                    continue
                if want_type and p.dtype != want_type and p.dtype != "COMBO":
                    drifts.append(Drift(
                        kind=DriftKind.PORT_TYPE_CHANGED, severity=Severity.BLOCKER,
                        class_type=ct, port=pname,
                        detail=f"端口 {ct}.{pname} 类型不符",
                        expected=want_type, observed=p.dtype,
                        remedy="升级或降级该自定义节点到规则表目标的版本",
                    ))

            # 枚举取值
            for pname, wanted in sorted(req.enums.items()):
                actual = self.enum_values(ct, pname)
                if actual is None:
                    drifts.append(Drift(
                        kind=DriftKind.ENUM_VALUE_MISSING,
                        severity=Severity.WARNING,
                        class_type=ct, port=pname,
                        detail=f"端口 {ct}.{pname} 不是枚举输入，无法校验取值",
                        remedy="确认节点版本；必要时在规则表中改用类型名",
                    ))
                    continue
                for val in wanted:
                    if val not in actual:
                        drifts.append(Drift(
                            kind=DriftKind.ENUM_VALUE_MISSING,
                            severity=Severity.WARNING,
                            class_type=ct, port=pname,
                            detail=f"端口 {ct}.{pname} 不支持期望取值 {val!r}",
                            expected=val, observed=list(actual)[:8],
                            remedy=f"改用环境支持的值，或安装提供 {val!r} 的节点包",
                        ))

            # 文件型输入：模型/LoRA 是否真的在位
            #
            # 注意 PortRequirement.files 的键是**端口名**（承载文件值的输入名），
            # 不是 class_type —— 把 class_type 当端口名去查会静默跳过全部校验，
            # 后果是"底模/LoRA 不在目标环境"这一最致命故障在编译期漏检。
            for pname, wanted in sorted(req.files.items()):
                actual = self.enum_values(ct, pname)
                if actual is None:
                    drifts.append(Drift(
                        kind=DriftKind.PORT_MISSING, severity=Severity.BLOCKER,
                        class_type=ct, port=pname,
                        detail=f"节点 {ct} 上不存在文件型输入 {pname}，无法校验模型文件是否在位",
                        remedy="检查该自定义节点版本；文件型输入名可能已变更",
                    ))
                    continue
                for val in wanted:
                    if val not in actual:
                        drifts.append(Drift(
                            kind=DriftKind.FILE_MISSING, severity=Severity.BLOCKER,
                            class_type=ct, port=pname,
                            detail=f"模型文件不在目标环境：{ct}.{pname} = {val!r}",
                            expected=val, observed=list(actual)[:8],
                            remedy=f"把 {val!r} 放入 ComfyUI 对应 models 子目录并重启",
                        ))

        if baseline is not None:
            drifts.extend(self._diff_against(baseline))
        return drifts

    def _diff_against(self, baseline: "CapabilityMatrix") -> list[Drift]:
        """与环境基线对比，检出环境自身的结构变化。"""
        drifts: list[Drift] = []
        for ct in sorted(baseline.nodes):
            old = baseline.nodes[ct]
            new = self.spec(ct)
            if new is None:
                drifts.append(Drift(
                    kind=DriftKind.NODE_MISSING, severity=Severity.WARNING,
                    class_type=ct, detail=f"节点 {ct} 在基线中存在，现已消失",
                    remedy="确认自定义节点包是否被卸载",
                ))
                continue
            for pname in sorted(set(old.inputs_required) | set(old.inputs_optional)):
                op = old.port(pname)
                np_ = new.port(pname)
                if np_ is None:
                    drifts.append(Drift(
                        kind=DriftKind.PORT_MISSING, severity=Severity.WARNING,
                        class_type=ct, port=pname,
                        detail=f"端口 {ct}.{pname} 在基线中存在，现已消失",
                        remedy="检查该自定义节点是否降级",
                    ))
                elif op and np_ and op.dtype != np_.dtype:
                    drifts.append(Drift(
                        kind=DriftKind.PORT_TYPE_CHANGED, severity=Severity.WARNING,
                        class_type=ct, port=pname,
                        detail=f"端口 {ct}.{pname} 类型由 {op.dtype} 变为 {np_.dtype}",
                        expected=op.dtype, observed=np_.dtype,
                        remedy="端口类型变化意味着工作流语义可能已变，需复核规则表",
                    ))
            for pname in sorted(set(new.inputs_required) | set(new.inputs_optional)):
                if not old.has_port(pname):
                    drifts.append(Drift(
                        kind=DriftKind.PORT_ADDED, severity=Severity.INFO,
                        class_type=ct, port=pname,
                        detail=f"端口 {ct}.{pname} 为新增",
                    ))
            if len(old.outputs) != len(new.outputs):
                drifts.append(Drift(
                    kind=DriftKind.OUTPUT_COUNT_CHANGED, severity=Severity.BLOCKER,
                    class_type=ct,
                    detail=f"输出数量由 {len(old.outputs)} 变为 {len(new.outputs)}",
                    expected=len(old.outputs), observed=len(new.outputs),
                    remedy="输出索引可能已变化，编译规则中的 outputIndex 需重新映射",
                ))

            # 缓存失效钩子的变化：直接改变缓存键的计算方式，属于 ABI 变化
            if old.fingerprint_hook != new.fingerprint_hook:
                drifts.append(Drift(
                    kind=DriftKind.FINGERPRINT_HOOK_CHANGED, severity=Severity.WARNING,
                    class_type=ct,
                    detail=(f"缓存失效钩子由 {old.fingerprint_hook or '（无）'} "
                            f"变为 {new.fingerprint_hook or '（无）'}"),
                    expected=old.fingerprint_hook, observed=new.fingerprint_hook,
                    remedy=("该变化改变缓存键计算方式，缓存语义随之改变；"
                            "请复核产物时效清单并考虑重跑受影响镜头"),
                ))
            elif old.determinism != new.determinism:
                drifts.append(Drift(
                    kind=DriftKind.DETERMINISM_CHANGED, severity=Severity.INFO,
                    class_type=ct,
                    detail=f"确定性等级由 {old.determinism} 变为 {new.determinism}",
                    expected=old.determinism, observed=new.determinism,
                    remedy="可能因已知钩子表更新导致；确认无误后更新基线快照",
                ))
        return drifts

    # ---- 序列化 --------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "protocol": CATALOG_PROTOCOL_VERSION,
            "endpoint": self.endpoint,
            "comfyVersion": self.comfy_version,
            "probedAt": self.probed_at,
            "nodeCount": self.node_count(),
            "catalogHash": self.catalog_hash(),
            "customNodePacks": list(self.custom_node_packs),
            "categories": sorted({s.category for s in self.nodes.values() if s.category}),
        }

    def to_snapshot(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "nodes": {ct: self.nodes[ct].as_dict() for ct in sorted(self.nodes)},
        }

    @staticmethod
    def from_snapshot(snap: Mapping[str, Any]) -> "CapabilityMatrix":
        """从快照还原，用于离线回归与跨机器比对（不丢失任何结构信息）。"""
        nodes: dict[str, NodeSpec] = {}
        for ct, body in (snap.get("nodes") or {}).items():
            req = {
                k: PortSpec(name=k, dtype=v["dtype"], required=True,
                            enum_values=tuple(v["enumValues"]) if v.get("enumValues") is not None else None,
                            default=v.get("default"), is_file_input=bool(v.get("isFileInput")))
                for k, v in (body.get("required") or {}).items()
            }
            opt = {
                k: PortSpec(name=k, dtype=v["dtype"], required=False,
                            enum_values=tuple(v["enumValues"]) if v.get("enumValues") is not None else None,
                            default=v.get("default"), is_file_input=bool(v.get("isFileInput")))
                for k, v in (body.get("optional") or {}).items()
            }
            nodes[ct] = NodeSpec(
                class_type=ct, category=body.get("category", ""),
                inputs_required=req, inputs_optional=opt,
                outputs=tuple(body.get("outputs") or ()),
                output_node=bool(body.get("outputNode", False)),
                deprecated=bool(body.get("deprecated", False)),
                description=body.get("description", ""),
                fingerprint_hook=body.get("fingerprintHook"),
                fingerprint_of=body.get("fingerprintOf", ""),
                fingerprint_source=body.get("fingerprintSource", ""),
                determinism=body.get("determinism", DeterminismClass.UNKNOWN.value),
            )
        return CapabilityMatrix(
            nodes=nodes,
            endpoint=snap.get("endpoint", "unknown"),
            comfy_version=snap.get("comfyVersion", "unknown"),
            probed_at=snap.get("probedAt"),
            custom_node_packs=tuple(snap.get("customNodePacks") or ()),
        )


# ==========================================================================
# 编译需求声明（由 compiler.py 的规则表生成）
# ==========================================================================

@dataclass(frozen=True)
class PortRequirement:
    """某个 class_type 被编译时对环境的要求。"""

    ports: Mapping[str, str] = field(default_factory=dict)        # port -> dtype
    enums: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    files: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    required: bool = True
    install_hint: str = ""
    port_aliases: Mapping[str, str] = field(default_factory=dict)  # 缺失端口 -> 替代端口

    def as_dict(self) -> dict[str, Any]:
        return {
            "ports": dict(self.ports),
            "enums": {k: list(v) for k, v in self.enums.items()},
            "files": {k: list(v) for k, v in self.files.items()},
            "required": self.required,
            "installHint": self.install_hint,
            "portAliases": dict(self.port_aliases),
        }
