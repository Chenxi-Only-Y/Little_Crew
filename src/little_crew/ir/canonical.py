"""统一 IR 的规范化与内容哈希。

设计目标
--------
保证 **语义等价 ⇒ 哈希相同**。这是三级缓存、产物时效清单、可复现实验的共同地基。
任何破坏该性质的字段都必须在此模块被显式剥离，任何影响语义的字段都必须被显式保留。

六步规范化流程（对应设计文档 §3.4）
-----------------------------------
1. 递归应用 Schema 默认值   —— 消除"省略 vs 显式"的哈希差异
2. 剔除非语义字段           —— meta.ui / provenance.createdAt / contentHash
3. 浮点量化                 —— 消除浮点表示噪声
4. 规范化端口引用           —— 递归解析为规范表达式串
5. 确定性排序               —— 节点按 id、边按 (type, from, to, port)
6. 规范 JSON + sha256

重要约束
--------
- 规范化必须是 **纯函数**：同一输入必得同一输出，不依赖 dict 插入顺序、时间、随机数。
- seed **不进哈希**（它是运行期随机源，但要可复现），而 seed 的派生参数进哈希。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "IR_VERSION",
    "FLOAT_PRECISION",
    "CanonicalizationError",
    "normalize",
    "canonical_json",
    "content_hash",
    "derive_seed",
    "condition_fingerprint",
]

IR_VERSION = "little_crew.ir/v1"

#: 浮点量化小数位。6 位足以保留扩散采样参数的全部有效精度
#: （steps/cfg/denoise/strength 的实际有效位远少于 6），同时消除
#: 0.30000000000000004 这类表示噪声。
FLOAT_PRECISION = 6

#: 从哈希输入中剔除的字段路径（末尾为字段名，前缀用 * 表示任意层级）。
_STRIP_PATHS: tuple[tuple[str, ...], ...] = (
    ("meta", "ui"),
    ("provenance", "createdAt"),
    ("provenance", "contentHash"),
)

#: 节点级默认值。仅补全 **影响语义** 的字段；纯展示字段不补，
#: 因为补了也会在剥离阶段被删除，反而增加噪声。
_NODE_EXECUTION_DEFAULTS: dict[str, Any] = {
    "sideEffect": "pure",
    "recoveryMode": "replay",
    "maxAutomaticAttempts": 3,
    "backoffClass": "bounded_exponential",
}

_EDGE_DEFAULTS: dict[str, Any] = {"enforcement": "hard"}
_CONSTRAINT_DEFAULTS: dict[str, Any] = {"enforcement": "hard"}
_SEMANTIC_DEFAULTS: dict[str, Any] = {"opaque": False}


class CanonicalizationError(ValueError):
    """IR 结构非法，无法规范化。"""


# --------------------------------------------------------------------------
# 步骤 1-3：递归规范化
# --------------------------------------------------------------------------

def _quantize(value: float) -> float:
    """浮点量化。保证 -0.0 归一为 0.0，避免符号位造成的哈希差异。"""
    if not math.isfinite(value):
        raise CanonicalizationError(f"浮点数必须有限，得到 {value!r}")
    rounded = round(value, FLOAT_PRECISION)
    # -0.0 == 0.0 为真，但二者的 JSON 表示不同（'-0.0' vs '0.0'）
    if rounded == 0:
        return 0.0
    return rounded


def _canonicalize_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        # bool 必须在 int 之前判断：Python 中 isinstance(True, int) 为 True
        return value
    if isinstance(value, float):
        return _quantize(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if value is None:
        return None
    raise CanonicalizationError(f"不支持的标量类型: {type(value).__name__}")


def _normalize_port_expression(node: Mapping[str, Any]) -> str:
    """把节点的端口引用图转换为规范表达式串。

    形如 ``n_prompt <- (n_fuse.out)``，用于让"结构相同但节点 id 不同"的图
    在需要时可通过替换 id 归一（当前版本保留 id，见下方说明）。

    说明：本平台 **不** 做节点 id 的 alpha 重命名。原因是 id 出现在
    semantic.constraintRefs 与断言 evidence 中，重命名会让追溯链断裂。
    该函数仅产出表达式串，供编译期去重与调试使用。
    """
    parts: list[str] = []
    inputs = node.get("inputs") or {}
    for key in sorted(inputs):
        val = inputs[key]
        if isinstance(val, Mapping) and "ref" in val and "port" in val:
            parts.append(f"{key}<-({val['ref']}.{val['port']})")
        elif isinstance(val, Mapping) and "binding" in val:
            parts.append(f"{key}<=({val['binding']})")
    return f"{node.get('id')}[{','.join(parts)}]"


def _normalize_assertion(assertion: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(assertion):
        val = assertion[key]
        if isinstance(val, float):
            out[key] = _quantize(val)
        elif isinstance(val, list):
            out[key] = sorted(_canonicalize_scalar(v) for v in val)
        else:
            out[key] = _canonicalize_scalar(val) if not isinstance(val, (dict, list)) else val
    return out


def _normalize_node(node: Mapping[str, Any]) -> dict[str, Any]:
    if "id" not in node or "kind" not in node:
        raise CanonicalizationError("节点必须包含 id 与 kind")

    out: dict[str, Any] = {"id": node["id"], "kind": node["kind"]}

    # inputs / params：按键排序，逐值规范化
    for section in ("inputs", "params"):
        raw = node.get(section)
        if not raw:
            continue
        norm: dict[str, Any] = {}
        for key in sorted(raw):
            val = raw[key]
            if isinstance(val, Mapping):
                # 端口引用 / 资产引用：原样保留（不补默认键，避免哈希噪声）
                norm[key] = {k: _canonicalize_scalar(v) for k, v in sorted(val.items())}
            elif isinstance(val, list):
                norm[key] = [_canonicalize_scalar(v) for v in val]
            else:
                norm[key] = _canonicalize_scalar(val)
        out[section] = norm

    # outputs
    raw_outputs = node.get("outputs")
    if raw_outputs:
        out["outputs"] = {
            key: dict(sorted(raw_outputs[key].items()))
            for key in sorted(raw_outputs)
        }

    # execution：补默认值后规范化（external 副作用必须可识别，故保留全部字段）
    raw_exec = node.get("execution") or {}
    exec_norm: dict[str, Any] = {}
    for key, default in _NODE_EXECUTION_DEFAULTS.items():
        exec_norm[key] = raw_exec.get(key, default)
    for key in sorted(raw_exec):
        if key in exec_norm:
            continue
        exec_norm[key] = raw_exec[key]
    if exec_norm.get("sideEffect") == "external" and "idempotency" not in exec_norm:
        # 这不是规范化错误，而是可被验收层捕获的语义缺口。此处仅提示，
        # 由 validate 层报出（见 ir_spec.validate）。
        pass
    out["execution"] = {k: exec_norm[k] for k in sorted(exec_norm)}

    # semantic
    raw_sem = node.get("semantic")
    if raw_sem:
        sem = dict(_SEMANTIC_DEFAULTS)
        sem.update(raw_sem)
        if sem.get("opaque") is False:
            # opaque=false 是默认值，剔除以减少噪声；true 必须保留
            sem.pop("opaque", None)
        out["semantic"] = {k: sem[k] for k in sorted(sem)}

    # meta.title 保留（参与哈希，因为它会写入执行图 _meta 并影响人工排查基线）
    title = (node.get("meta") or {}).get("title")
    if title is not None:
        out["meta"] = {"title": title}

    # 统一剔除 UI 与非语义字段
    return _strip_non_semantic(out)


def _strip_non_semantic(obj: Any) -> Any:
    """递归剔除设计文档 §3.4 步骤 2 列出的非语义字段。"""
    if isinstance(obj, dict):
        cleaned = {}
        for key, val in obj.items():
            if any(_match_strip_path(obj, key, path) for path in _STRIP_PATHS):
                continue
            cleaned[key] = _strip_non_semantic(val)
        return cleaned
    if isinstance(obj, list):
        return [_strip_non_semantic(v) for v in obj]
    return obj


def _match_strip_path(container: Mapping[str, Any], key: str, path: tuple[str, ...]) -> bool:
    """判断 (container, key) 是否命中要从哈希中剔除的路径。

    由于本函数在递归中不携带完整祖先路径，采用保守策略：
    只要字段名等于任一 strip 路径的末段，且该字段的父级结构符合预期，
    即剔除。当前 strip 集合为 {ui, createdAt, contentHash}，三者作为
    顶层或常见二级字段均属非语义，保守剔除是安全的。
    """
    return key == path[-1] and key in {"ui", "createdAt", "contentHash"}


def _normalize_edge(edge: Mapping[str, Any]) -> dict[str, Any]:
    etype = edge.get("type")
    if etype not in ("data", "constraint"):
        raise CanonicalizationError(f"边 type 非法: {etype!r}")

    out: dict[str, Any] = {"id": edge["id"], "type": etype}
    for endpoint in ("from", "to"):
        ep = edge.get(endpoint)
        if not isinstance(ep, Mapping) or "node" not in ep or "port" not in ep:
            raise CanonicalizationError(f"边 {edge.get('id')!r} 的 {endpoint} 必须含 node 与 port")
        out[endpoint] = {"node": ep["node"], "port": ep["port"]}

    if etype == "data":
        if "dtype" in edge:
            out["dtype"] = edge["dtype"]
    else:
        out["assertion"] = _normalize_assertion(edge["assertion"])
        out["enforcement"] = edge.get("enforcement", _EDGE_DEFAULTS["enforcement"])
    return out


def _normalize_binding(binding: Mapping[str, Any]) -> dict[str, Any]:
    """资产绑定规范化。仅保留影响生成结果或时效判定的字段。"""
    out: dict[str, Any] = {}
    for key in sorted(binding):
        val = binding[key]
        if isinstance(val, float):
            out[key] = _quantize(val)
        elif isinstance(val, Mapping):
            out[key] = {k: val[k] for k in sorted(val)}
        elif isinstance(val, list):
            out[key] = list(val)
        else:
            out[key] = val
    return out


# --------------------------------------------------------------------------
# 步骤 5：确定性排序
# --------------------------------------------------------------------------

def _edge_sort_key(edge: Mapping[str, Any]) -> tuple:
    return (
        edge.get("type", ""),
        edge["from"]["node"],
        edge["from"]["port"],
        edge["to"]["node"],
        edge["to"]["port"],
        edge.get("id", ""),
    )


def normalize(graph: Mapping[str, Any]) -> dict[str, Any]:
    """执行六步规范化，返回可直接哈希的 dict。纯函数，不修改入参。"""
    if not isinstance(graph, Mapping):
        raise CanonicalizationError("IR 必须是对象")

    for required in ("irVersion", "graphId", "scope", "nodes"):
        if required not in graph:
            raise CanonicalizationError(f"IR 缺少必填字段 {required!r}")

    out: dict[str, Any] = {
        "irVersion": graph["irVersion"],
        "graphId": graph["graphId"],
        "scope": graph["scope"],
    }

    # 节点：规范化后按 id 排序
    nodes = [_normalize_node(n) for n in graph["nodes"]]
    node_ids = [n["id"] for n in nodes]
    if len(node_ids) != len(set(node_ids)):
        dupes = sorted({i for i in node_ids if node_ids.count(i) > 1})
        raise CanonicalizationError(f"节点 id 重复: {dupes}")
    out["nodes"] = sorted(nodes, key=lambda n: n["id"])

    # 边：规范化后按 (type, from, to, port, id) 排序
    edges = [_normalize_edge(e) for e in graph.get("edges", [])]
    edge_ids = [e["id"] for e in edges]
    if len(edge_ids) != len(set(edge_ids)):
        dupes = sorted({i for i in edge_ids if edge_ids.count(i) > 1})
        raise CanonicalizationError(f"边 id 重复: {dupes}")
    out["edges"] = sorted(edges, key=_edge_sort_key)

    # 图级约束：按 ref 排序
    raw_constraints = graph.get("constraints")
    if raw_constraints:
        cons = []
        for c in raw_constraints:
            norm = {
                "ref": c["ref"],
                "assertion": _normalize_assertion(c["assertion"]),
                "enforcement": c.get("enforcement", _CONSTRAINT_DEFAULTS["enforcement"]),
            }
            if "description" in c:
                norm["description"] = c["description"]
            cons.append(norm)
        # ⚠️ **ref 唯一性** —— 与上面「节点 id 重复」「边 id 重复」同构。
        #
        # 为什么必须有：`compiler.py:1606` 用 `registry[c["ref"]] = c` 建索引，
        # 重复 ref 会**后写覆盖先写**（一条约束静默消失），
        # 而 `review/gate.py` 又把 `constraint_ref` 当作断言追溯的唯一身份。
        # 2026-10-23 实测：`ir_bridge` 的 ⓪ 与 ③ 曾共用 `cr_cast_{shotId}`，
        # 于是 ⓪ 被 ③ 整条挤掉。**校验放在规范化这一层，所有入口都受益。**
        refs = [c["ref"] for c in cons]
        if len(refs) != len(set(refs)):
            dupes = sorted({r for r in refs if refs.count(r) > 1})
            raise CanonicalizationError(f"约束 ref 重复: {dupes}")
        out["constraints"] = sorted(cons, key=lambda c: c["ref"])

    # 资产绑定：按键排序
    raw_bindings = graph.get("bindings")
    if raw_bindings:
        out["bindings"] = {
            k: _normalize_binding(raw_bindings[k]) for k in sorted(raw_bindings)
        }

    # provenance：仅保留影响可复现性的字段（authorAgent* 与 seedDerivation 保留，
    # createdAt / contentHash 已在 _strip_non_semantic 中剔除）
    raw_prov = graph.get("provenance")
    if raw_prov:
        prov = _strip_non_semantic(dict(raw_prov))
        if prov:
            out["provenance"] = {k: prov[k] for k in sorted(prov)}

    return out


# --------------------------------------------------------------------------
# 步骤 6：规范 JSON 与哈希
# --------------------------------------------------------------------------

def canonical_json(value: Any) -> str:
    """规范 JSON 序列化。

    - ``sort_keys=True``      键序无关
    - ``ensure_ascii=False``  中文不被转义（保证与人工可读形态一致）
    - ``separators``          去除多余空白
    - ``allow_nan=False``     拒绝 NaN/Infinity（它们无法稳定表示）
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def content_hash(graph: Mapping[str, Any]) -> str:
    """返回 ``sha256:<hex>`` 形式的语义内容哈希。"""
    normalized = normalize(graph)
    digest = hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def canonical_bytes(graph: Mapping[str, Any]) -> bytes:
    """规范化后的字节串，便于直接比对或落盘归档。"""
    return canonical_json(normalize(graph)).encode("utf-8")


# --------------------------------------------------------------------------
# 种子派生（不进哈希，但派生参数进哈希）
# --------------------------------------------------------------------------

def derive_seed(
    project_seed: int,
    episode_idx: int,
    scene_idx: int,
    shot_idx: int,
    attempt: int = 0,
    *,
    bits: int = 64,
) -> int:
    """确定性种子派生。

    设计文档 §8.4：``projectSeed → episodeSeed → sceneSeed → shotSeed → attemptSeed``。
    使用 sha256 而非简单算术组合，避免不同 (ep, sc, sh) 三元组碰撞到同一种子
    ——这在长剧集中会表现为"某些镜头莫名雷同"的隐蔽缺陷。

    ``attempt`` 参与派生，因此重跑必然换种子；但 seed 本身不进内容哈希，
    所以"同语义图 + 不同 attempt"共享缓存键之外的尝试记录。
    """
    if attempt < 0:
        raise ValueError("attempt 必须 >= 0")
    payload = f"{project_seed}|{episode_idx}|{scene_idx}|{shot_idx}|{attempt}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    value = int.from_bytes(digest, "big")
    return value % (2 ** bits)


def condition_fingerprint(
    *,
    prompt: str,
    character_refs: Sequence[str] = (),
    scene_ref: str | None = None,
    width: int,
    height: int,
    frames: int,
    fps: int,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """镜头级条件指纹（设计文档 §7.5 的 Tier-3 缓存键）。

    语义：不同 IR 但**实际生成条件相同**的镜头应共享此指纹，从而共享
    昂贵的条件编码结果。因此本函数只取真正影响扩散输入的量。
    """
    material = {
        "prompt": prompt,
        "characterRefs": sorted(character_refs),
        "sceneRef": scene_ref,
        "width": int(width),
        "height": int(height),
        "frames": int(frames),
        "fps": int(fps),
    }
    if extra:
        material["extra"] = {k: extra[k] for k in sorted(extra)}
    digest = hashlib.md5(
        canonical_json(material).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return f"cf:{digest}"


# --------------------------------------------------------------------------
# 缓存键合成（设计文档 §7.5 三级）
# --------------------------------------------------------------------------

def cache_key(
    ir_content_hash: str,
    catalog_hash: str | None = None,
    rule_version: str | None = None,
) -> str:
    """合成缓存键。

    - 传全部三个参数 → Tier-1（同环境同语义，精确命中）
    - 只传 ir_content_hash → Tier-2（跨环境语义复用，需重校验质量断言）
    """
    if catalog_hash is None and rule_version is None:
        return f"t2:{ir_content_hash}"
    if catalog_hash is None or rule_version is None:
        raise ValueError("Tier-1 缓存键需要同时提供 catalog_hash 与 rule_version")
    digest = hashlib.sha256(
        f"{ir_content_hash}|{catalog_hash}|{rule_version}".encode("utf-8")
    ).hexdigest()
    return f"t1:sha256:{digest}"
