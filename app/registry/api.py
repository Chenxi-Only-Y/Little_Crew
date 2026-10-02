"""小剧组 (Little Crew) — 注册表派生查询 API。

前端（画布 / 设置页）与后端（工作流 / 校验）都通过本模块读取注册表，
确保"唯一数据源"：不再有人在别处硬编码职业、部门、阶段顺序或确认点。

主要导出：
    snapshot()                  一次性下发完整注册表（供 `GET /api/registry`）
    execution_order()           由依赖推导出的执行顺序（替代硬编码 _STAGE_ORDER）
    validate_topology(...)      画布拓扑校验（端口兼容 + 缺失依赖）
    panels_by_role() / nodes_by_dept()  常用分组
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from app.registry.roles import (
    ALL_NODES,
    ARTIFACT_BY_KEY,
    CHECKPOINTS,
    DEPARTMENTS,
    NODE_BY_KEY,
    ROLE_BY_KEY,
    ROLES,
)
from app.registry.types import Checkpoint, Department, Node, Panel, Role

__all__ = [
    "artifact", "checkpoint", "department", "node", "role",
    "all_roles", "all_nodes", "all_departments", "all_checkpoints",
    "nodes_by_dept", "roles_by_dept", "panels_by_role", "checkpoints_for_node",
    "workflow_to_registry", "stage_to_registry", "to_registry_node",
    "execution_order", "validate_topology", "snapshot", "validation_spec",
    "allowed_settings",
]


# ------------------------------------------------------------------
# 基础查询
# ------------------------------------------------------------------

def artifact(key: str):
    """按 key 取产物定义（不存在返回 None）。"""
    return ARTIFACT_BY_KEY.get(key)


def node(key: str) -> Node | None:
    """按 key 取节点定义。"""
    return NODE_BY_KEY.get(key)


def role(key: str) -> Role | None:
    """按 key 取职业定义。支持用 `legacy_keys` 反查旧命名。"""
    r = ROLE_BY_KEY.get(key)
    if r is not None:
        return r
    for cand in ROLES:
        if key in cand.legacy_keys:
            return cand
    return None


def department(key: str) -> Department | None:
    """按 key 取部门定义。"""
    return next((d for d in DEPARTMENTS if d.key == key), None)


def checkpoint(key: str) -> Checkpoint | None:
    """按 key 取确认点定义。"""
    return next((c for c in CHECKPOINTS if c.key == key), None)


def all_roles() -> tuple[Role, ...]:
    return ROLES


def all_nodes() -> tuple[Node, ...]:
    return ALL_NODES


def all_departments() -> tuple[Department, ...]:
    """按 order 排序的部门列表。"""
    return tuple(sorted(DEPARTMENTS, key=lambda d: d.order))


def all_checkpoints() -> tuple[Checkpoint, ...]:
    return CHECKPOINTS


# ------------------------------------------------------------------
# 分组
# ------------------------------------------------------------------

def nodes_by_dept() -> dict[str, list[Node]]:
    """部门 key -> 节点列表（保持注册表声明顺序）。"""
    out: dict[str, list[Node]] = {d.key: [] for d in all_departments()}
    for n in ALL_NODES:
        out.setdefault(n.dept, []).append(n)
    return out


def roles_by_dept() -> dict[str, list[Role]]:
    """部门 key -> 职业列表。"""
    out: dict[str, list[Role]] = {d.key: [] for d in all_departments()}
    for r in ROLES:
        out.setdefault(r.dept, []).append(r)
    return out


def panels_by_role() -> dict[str, list[Panel]]:
    """职业 key -> 面板列表。"""
    return {r.key: list(r.panels) for r in ROLES}


def checkpoints_for_node(node_key: str) -> list[Checkpoint]:
    """某节点上的确认点。"""
    return [c for c in CHECKPOINTS if c.node == node_key]


# ------------------------------------------------------------------
# 命名空间映射（注册表 key ←→ 工作流节点名 / 阶段名）
# ------------------------------------------------------------------
# 系统里有三套命名（见 roles.WORKFLOW_NODE_BY_KEY 的说明）：
#   ① 注册表 key（v2 新表）  showrunner / script_editor / technical_director
#   ② 工作流节点名（v1 实现） story_planner / critics / prompt_compiler
#   ③ 阶段名（= 配置键）      writer / critics / director / photographer
# 事件广播统一用 ① 输出，界面上按 ① 渲染的节点才能亮起来。

def workflow_to_registry() -> dict[str, str]:
    """工作流节点名 → 注册表 key。"""
    return {n.workflow_node: n.key for n in ALL_NODES if n.workflow_node}


def stage_to_registry() -> dict[str, str]:
    """阶段名（= 配置键词表）→ 注册表 key。

    阶段名与工作流节点名的对应关系是固定的（见 `main_workflow._make_stage_node` 的调用）：
        节点            阶段名
        story_planner   writer
        critics         critics
        prompt_compiler director
        image_generator photographer
        其余节点        与节点名相同
    """
    wf_to_reg = workflow_to_registry()
    stage_by_workflow = {
        "story_planner": "writer",
        "critics": "critics",
        "prompt_compiler": "director",
        "image_generator": "photographer",
    }
    out: dict[str, str] = {}
    for wf_node, reg_key in wf_to_reg.items():
        out[stage_by_workflow.get(wf_node, wf_node)] = reg_key
    return out


def to_registry_node(name: str) -> str:
    """把一个可能是「工作流节点名」或「阶段名」的值，翻译成注册表 key。

    无法识别时原样返回（保证不会因为翻译失败而丢事件）。
    """
    if name in NODE_BY_KEY:
        return name
    wf = workflow_to_registry()
    if name in wf:
        return wf[name]
    return stage_to_registry().get(name, name)


# ------------------------------------------------------------------
# 执行顺序（替代硬编码 _STAGE_ORDER）
# ------------------------------------------------------------------

#: 无法从依赖推导的锚点：入口与出口（没有 consumes / 没有下游的节点）。
_ENTRY_KEYS = ("showrunner", "workflow_architect")


def execution_order() -> list[str]:
    """由注册表依赖推导节点执行顺序（Kahn 拓扑排序）。

    取代 `main_workflow._STAGE_ORDER` 的人工维护。
    约定：同层就绪时按注册表声明顺序（ROLES 顺序 + SYSTEM_NODES 顺序）出队，
    因此结果稳定可复现。若存在环，抛出 ValueError（环应由 `iter_group` 表达，而非 DAG 边）。

    Returns:
        节点 key 的有序列表。
    """
    # 稳定优先级：注册表声明顺序
    priority = {n.key: i for i, n in enumerate(ALL_NODES)}

    # 生产者映射：产物 -> 产出它的节点（多个产出者时取声明顺序最靠前者）
    producers: dict[str, list[str]] = {}
    for n in ALL_NODES:
        for a in n.produces:
            producers.setdefault(a, []).append(n.key)

    # 入边：节点 -> 它依赖的上游节点集合
    deps: dict[str, set[str]] = {n.key: set() for n in ALL_NODES}
    for n in ALL_NODES:
        for a in n.consumes:
            for p in producers.get(a, ()):
                if p != n.key:
                    deps[n.key].add(p)

    # Kahn
    remaining = dict(deps)
    order: list[str] = []
    while remaining:
        ready = sorted((k for k, v in remaining.items() if not v), key=lambda k: priority[k])
        if not ready:
            cycle = "、".join(sorted(remaining))
            raise ValueError(
                f"注册表依赖存在环，无法推导执行顺序：{cycle}。"
                f"环应改用 iter_group（有限轮次）表达，而不是 DAG 边。"
            )
        for k in ready:
            order.append(k)
            remaining.pop(k)
        for v in remaining.values():
            v.difference_update(ready)
    return order


# ------------------------------------------------------------------
# 拓扑校验（画布"能不能连"的依据）
# ------------------------------------------------------------------

def validate_topology(
    node_keys: Sequence[str],
    edges: Iterable[tuple[str, str]] | None = None,
    available_inputs: Iterable[str] | None = None,
) -> dict[str, Any]:
    """校验一组节点（及连线）能否执行。

    规则（对应 `04-frontend-rewrite.md §5.2`：前端预校验 + 后端再校验，同一份规则）：
      1. 节点必须都在注册表里
      2. 边必须满足产物依赖：source.produces ∩ target.consumes ≠ ∅
      3. 每个节点若声明了 consumes，其依赖必须由**已选节点**产出，
         或作为外部输入（`available_inputs`，如用户直接粘贴的剧本）提供
      4. 不得有环

    Args:
        node_keys:        用户选择要执行的节点
        edges:            连线 (source_key, target_key)；None = 只按 consumes 推导
        available_inputs: 外部已提供的产物（用户输入/上次产物），视为已满足

    Returns:
        {
          "ok": bool,
          "errors": [{"code","node"|"edge","message"}],
          "warnings": [...],
          "order": [node_key],          # 可行时的建议执行顺序
          "unmatched_inputs": {...},    # 节点无法满足的依赖
        }
    """
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    chosen = [k for k in node_keys]
    unknown = [k for k in chosen if k not in NODE_BY_KEY]
    for k in unknown:
        errors.append({"code": "unknown_node", "node": k, "message": f"节点不在注册表中：{k}"})
    chosen = [k for k in chosen if k in NODE_BY_KEY]

    produced: set[str] = set()
    for k in chosen:
        produced.update(NODE_BY_KEY[k].produces)
    provided = set(available_inputs or ())
    satisfiable = produced | provided

    # 规则 3：依赖是否可满足
    unmatched: dict[str, list[str]] = {}
    for k in chosen:
        missing = [a for a in NODE_BY_KEY[k].consumes if a not in satisfiable]
        if missing:
            unmatched[k] = missing
            errors.append({
                "code": "missing_dependency", "node": k,
                "message": f"「{NODE_BY_KEY[k].label}」缺少上游产物："
                           + "、".join(ARTIFACT_BY_KEY[a].label if a in ARTIFACT_BY_KEY else a
                                       for a in missing),
            })

    # 规则 2：连线必须满足产物依赖
    if edges is not None:
        for src, dst in edges:
            if src not in NODE_BY_KEY or dst not in NODE_BY_KEY:
                errors.append({"code": "unknown_edge", "edge": f"{src}->{dst}",
                               "message": f"连线引用了不存在的节点：{src} -> {dst}"})
                continue
            s, d = NODE_BY_KEY[src], NODE_BY_KEY[dst]
            shared = set(s.produces) & set(d.consumes)
            if not shared:
                errors.append({
                    "code": "incompatible_edge", "edge": f"{src}->{dst}",
                    "message": f"「{s.label}」的产出无法被「{d.label}」消费"
                               f"（它需要：{'、'.join(d.consumes) or '无'}）",
                })

    # 规则 4：环检测（在所选节点子图上）
    order: list[str] = []
    if not any(e["code"] for e in errors):
        order = _order_subset(chosen, edges)

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "order": order,
        "unmatched_inputs": unmatched,
    }


def _order_subset(chosen: Sequence[str], edges: Iterable[tuple[str, str]] | None) -> list[str]:
    """在所选节点子集上做拓扑排序（考虑显式连线 + 声明的 consumes 依赖）。"""
    keys = list(dict.fromkeys(chosen))
    keyset = set(keys)
    priority = {n.key: i for i, n in enumerate(ALL_NODES)}

    deps: dict[str, set[str]] = {k: set() for k in keys}
    if edges is None:
        # 按声明依赖推导
        for k in keys:
            producers = {a: [n.key for n in ALL_NODES if a in n.produces] for a in NODE_BY_KEY[k].consumes}
            for a, ps in producers.items():
                for p in ps:
                    if p in keyset and p != k:
                        deps[k].add(p)
    else:
        for src, dst in edges:
            if src in keyset and dst in keyset and src != dst:
                deps[dst].add(src)

    order: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted((k for k, v in remaining.items() if not v), key=lambda k: priority[k])
        if not ready:
            return []          # 有环：返回空表示无法给出顺序
        for k in ready:
            order.append(k)
            remaining.pop(k)
        for v in remaining.values():
            v.difference_update(ready)
    return order


#: 产物形态之间的兼容性：`{源形态: [可被接受的输入形态]}`
#: 用于画布连线校验。默认要求 key 完全一致；这里只声明**有意的放宽**。
COMPATIBLE_KINDS: dict[str, tuple[str, ...]] = {
    "image": ("image", "list"),      # 单图可作为图列表的一项
    "video": ("video", "list"),
    "audio": ("audio", "list"),
    "score": ("score", "json"),      # 分数可作为结构化报告的一部分
    "model": ("model", "json"),      # 模型元信息可作为 json 传递
    "timeline": ("timeline", "json"),
}


def allowed_settings() -> set[str]:
    """所有可写入运行时设置的键（由注册表派生）。

    用途：`PUT /api/settings` 的白名单。

    为什么必须派生而不是手写：后端原先把白名单写死在 `app/main.py` 的 `_SETTABLE`，
    而注册表后来新增了 `tts_provider` / `lora_train_provider` /
    `image_provider_image_edit` 等键——**两者不一致，设置会被静默丢弃**。
    这里从注册表的 config 字段派生，保证"界面上能改的一定存得下"。
    """
    keys: set[str] = set()
    for n in ALL_NODES:
        for c in n.config:
            keys.add(c.key)
    # 全局生成参数（不在任何节点的 config 里，但设置页与运行时会读）
    keys.update({
        "image_quality", "video_quality", "video_resolution",
        "llm_provider", "llm_model", "llm_api_key", "llm_base_url",
        "image_provider", "image_model", "image_api_key", "image_base_url",
        "video_provider", "video_model", "video_api_key", "video_base_url",
        "dashscope_api_key", "replicate_api_token", "minimax_api_key", "minimax_base_url",
    })
    # 兼容 v1 的 per-role 键：Agent 运行时按这些角色名读取
    # （`llm_provider_writer` / `image_provider_character_designer` …）。
    # 它们与注册表的 config 键按 `role_namespace` 对齐，这里补一轮以防遗漏。
    for n in ALL_NODES:
        ns = n.role_namespace
        keys.update({f"llm_provider_{ns}", f"llm_model_{ns}", f"llm_api_key_{ns}"})
        keys.update({f"image_provider_{ns}", f"image_model_{ns}", f"image_api_key_{ns}"})
        keys.update({f"video_provider_{ns}", f"video_model_{ns}", f"video_api_key_{ns}"})
    return keys


def validation_spec() -> dict[str, Any]:
    """导出"画布连线校验"所需的规则数据（供前端复用同一份规则）。

    为什么单独导出：
        前端要在**拖线时立刻**给出反馈，不能每次都问后端；
        但规则若由前端手写就会与后端漂移（v1 的教训）。
        故由后端导出这份精简规格，前端据此实现等价的纯函数校验算法。
    """
    return {
        "artifacts": {
            a.key: {"kind": a.kind, "label": a.label}
            for a in ARTIFACT_BY_KEY.values()
        },
        "nodes": {
            n.key: {
                "label": n.label,
                "produces": list(n.produces),
                "consumes": list(n.consumes),
                "can_skip": n.can_skip,
            }
            for n in ALL_NODES
        },
        "compatible_kinds": {k: list(v) for k, v in COMPATIBLE_KINDS.items()},
        # 需要提供的外部输入（用户输入 / 上一轮产物），校验时视为已满足
        "external_inputs": ["user_input", "art_brief"],
    }


# ------------------------------------------------------------------
# 快照（供 GET /api/registry）
# ------------------------------------------------------------------

def snapshot(*, include_state_map: bool = True) -> dict[str, Any]:
    """一次性下发完整注册表，供前端零硬编码渲染。

    Args:
        include_state_map: 是否附带 产物 key -> CrewState 字段 的映射（迁移期有用）
    """
    dep_map = {a.key: a.state_key for a in ARTIFACT_BY_KEY.values() if a.state_key}
    return {
        "schema_version": 2,
        "departments": [
            {"key": d.key, "label": d.label, "order": d.order, "desc": d.desc}
            for d in all_departments()
        ],
        "artifacts": [
            {"key": a.key, "label": a.label, "kind": a.kind, "state_key": a.state_key}
            for a in ARTIFACT_BY_KEY.values()
        ],
        "roles": [
            {
                "key": r.key,
                "label": r.label,
                "en": r.en,
                "dept": r.dept,
                "duty": r.duty,
                "real_job": r.real_job,
                "node": r.node,
                "legacy_keys": list(r.legacy_keys),
                "panels": [
                    {"key": p.key, "label": p.label, "kind": p.kind, "node": p.node}
                    for p in r.panels
                ],
            }
            for r in ROLES
        ],
        "nodes": [
            {
                "key": n.key,
                "label": n.label,
                "dept": n.dept,
                "kind": n.kind,
                "produces": list(n.produces),
                "consumes": list(n.consumes),
                "capability": list(n.capability),
                "can_skip": n.can_skip,
                "iter_group": n.iter_group,
                "legacy_keys": list(n.legacy_keys),
                "agent_class": n.agent_class,
                # 该节点在工作流图里的名字（事件广播按它对账；空 = 尚未进图）
                "workflow_node": n.workflow_node,
                "config": [
                    {
                        "key": c.key, "label": c.label, "type": c.type, "scope": c.scope,
                        "options": list(c.options), "default": c.default,
                        "secret": c.secret, "help": c.help,
                    }
                    for c in n.config
                ],
            }
            for n in ALL_NODES
        ],
        "checkpoints": [
            {"key": c.key, "label": c.label, "node": c.node, "after": c.after}
            for c in CHECKPOINTS
        ],
        "execution_order": execution_order(),
        "validation_spec": validation_spec(),
        **({"artifact_state_map": dep_map} if include_state_map else {}),
    }
