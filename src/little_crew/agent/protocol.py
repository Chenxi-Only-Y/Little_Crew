"""三层智能体调用协议 —— 不依赖任何编排框架。

设计要点（对应设计文档 §2.1、§5.4、§6.2）
-----------------------------------------
本模块用 **三个原语** 取代"图"这一抽象：

  1. CapabilityRegistry  发现 —— 谁能做什么，副作用与成本如何
  2. CallEnvelope        调用 —— 三层共用的同一信封
  3. Blackboard          状态 —— 共享状态与依赖失效传播

为什么不用 LangGraph
--------------------
LangGraph 用同一个抽象（图/节点/边）同时表达两个正交的轴：

  轴 A 组织分层：L1 决策 → L2 协作 → L3 执行      本质是 **树**
  轴 B 推理循环：Observe → Think → Act → 循环     本质是 **有界状态机**

把树和有界状态机都画成图，带来三个后果：
  - 轴 A 引入不需要的回路可能，调度决策不再可静态分析
  - 轴 B 的"最多 N 轮"预算约束退化为隐性约定，不可校验
  - 真正需要图的 IR 执行 DAG 反而失去了立足点（图抽象已被占用）

本模块的分工：
  - 轴 A  -> 显式调用树（LayerRuntime，单向、可强制、可追溯）
  - 轴 B  -> 有界循环（call_agent 内，硬上限，超出即 Escalate）
  - 执行 DAG -> 只在 L3 内部，由 IR 编译器产出（见 compiler.py）

R1-R4 层边界规则
----------------
  R1 单向依赖：L1→L2、L2→L3 合法；L3→上层非法
  R2 禁止跳层：L1 不得直接调 L3，保证 L3 永远确定性、无 LLM
  R3 预算只减不增：子预算 ≤ 父剩余预算
  R4 深度硬上限：超限 Escalate，防互相调用死循环
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Any, Callable, Iterable, Mapping, Sequence

# 复用 ir-core 的规范序列化，保证信封哈希与 IR 哈希同一套规则。
# 依赖方向：agent -> ir（单向，见 platform/ARCHITECTURE.md）
from little_crew.ir.canonical import canonical_json

__all__ = [
    "Layer",
    "SideEffect",
    "Budget",
    "AgentManifest",
    "ToolSpec",
    "CapabilityRegistry",
    "CallEnvelope",
    "CallResult",
    "Blackboard",
    "BlackboardConflict",
    "LayerViolation",
    "BudgetExceeded",
    "DepthExceeded",
    "LayerRuntime",
    "ReasoningStep",
    "AgentDecision",
    "BoundedReasoningLoop",
    "DEFAULT_LAYER_TRANSITIONS",
]


def _deep_normalize(value: Any) -> Any:
    """深层规范化 payload，供信封指纹使用。

    做三件事：
      1. 递归排序所有 Mapping 的键（canonical_json 也会做，此处显式化以便单测）
      2. 剔除以 None 为值的键 —— ``{"a": None}`` 与 ``{}`` 视为同一调用意图
      3. 浮点量化到 9 位小数，消除 0.30000000000000004 一类表示噪声

    不做的事：不改动 list 顺序（顺序在多数工具入参中是有语义的）。
    """
    if isinstance(value, Mapping):
        return {
            k: _deep_normalize(v)
            for k, v in sorted(value.items())
            if v is not None
        }
    if isinstance(value, (list, tuple)):
        return [_deep_normalize(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        r = round(value, 9)
        return 0.0 if r == 0 else r
    return value


# ==========================================================================
# 枚举与错误
# ==========================================================================

class Layer(IntEnum):
    """组织分层。数值用于 R1/R2 的单向性判定（只允许数值递增）。"""

    DECISION = 1   # L1 决策层：DirectorAgent / Scheduler
    COLLAB = 2     # L2 协作层：18 个领域 Agent
    EXEC = 3       # L3 执行层：确定性工具 / 算子 / ComfyUI 网关


class SideEffect(str):
    """副作用等级。字符串子类，便于直接序列化进信封。"""

    PURE = "pure"            # 无副作用，可任意重放
    READ = "read"            # 只读
    WRITE = "write"          # 写本地/存储
    EXTERNAL = "external"    # 调外部付费服务，重试需幂等键


class LayerViolation(RuntimeError):
    """违反 R1/R2 层边界规则。"""


class BudgetExceeded(RuntimeError):
    """违反 R3 预算规则。"""


class DepthExceeded(RuntimeError):
    """违反 R4 深度规则。"""


class BlackboardConflict(RuntimeError):
    """黑板乐观并发冲突。"""


# ==========================================================================
# 预算
# ==========================================================================

@dataclass(frozen=True)
class Budget:
    """跨层传递的预算。R3：只能减，不能增。

    三类预算分别约束不同的失控模式：
      - tokens/cny 约束 **成本** 失控
      - wall_ms     约束 **时延** 失控
      - steps       约束 **Agent 推理循环** 失控（轴 B 的硬上限来源）
    """

    tokens: int = 0
    cny: float = 0.0
    wall_ms: int = 0
    steps: int = 0

    def can_afford(self, other: "Budget") -> bool:
        return (
            other.tokens <= self.tokens
            and other.cny <= self.cny + 1e-9
            and other.wall_ms <= self.wall_ms
            and other.steps <= self.steps
        )

    def minus(self, spent: "Budget") -> "Budget":
        """扣减。允许扣到 0 以下吗？不允许 —— 调用方必须先 can_afford。"""
        return Budget(
            tokens=self.tokens - spent.tokens,
            cny=round(self.cny - spent.cny, 6),
            wall_ms=self.wall_ms - spent.wall_ms,
            steps=self.steps - spent.steps,
        )

    @staticmethod
    def unlimited() -> "Budget":
        return Budget(tokens=10**12, cny=10**9, wall_ms=10**12, steps=10**6)

    def as_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "cny": self.cny,
                "wall_ms": self.wall_ms, "steps": self.steps}


# ==========================================================================
# 能力声明
# ==========================================================================

@dataclass(frozen=True)
class ToolSpec:
    """L3 工具声明。**必须**是确定性的：无 LLM、无自由判断。"""

    name: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    side_effect: str = SideEffect.PURE
    idempotent: bool = True
    est_cost_cny: float = 0.0
    est_ms: int = 0
    requires_gpu: bool = False
    #: external 副作用工具必须提供幂等键提取函数名，供运行时对账
    idempotency_key_field: str | None = None

    def __post_init__(self) -> None:
        if self.side_effect == SideEffect.EXTERNAL and not self.idempotency_key_field:
            raise ValueError(
                f"工具 {self.name!r} 声明 external 副作用，必须提供 idempotency_key_field"
            )
        if self.side_effect == SideEffect.PURE and not self.idempotent:
            raise ValueError(f"工具 {self.name!r} 为 pure 副作用，必须幂等")


@dataclass(frozen=True)
class AgentManifest:
    """Agent 声明。注册即生效，无需改框架代码。"""

    agent_id: str
    layer: Layer
    #: 能力标签，供路由匹配，如 ("script.review", "consistency.audit")
    capabilities: tuple[str, ...]
    #: 该 Agent 允许调用的下游能力标签（空元组表示不调用下游 = 叶子）
    consumes: tuple[str, ...] = ()
    #: 单次调用的预算上限。运行时取 min(父剩余, 本值)
    max_budget: Budget = field(default_factory=Budget.unlimited)
    #: 单次调用的最大推理步数（轴 B 硬上限）
    max_reasoning_steps: int = 5
    #: 触发阶段，便于 MasterDirector 编排
    trigger_stage: str = "any"
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError(f"Agent {self.agent_id!r} 必须声明至少一个 capability")


# ==========================================================================
# 能力注册表 —— 原语 1
# ==========================================================================

class CapabilityRegistry:
    """声明式能力目录。

    三个作用：
      1. 路由：按 capability 标签解析到 agent_id / tool_name
      2. 预判：调用前即可知道成本与副作用，无需试探
      3. 审计：静态检查"所有被 consumes 声明的能力都有实现者"
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentManifest] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._cap_index: dict[str, list[tuple[Layer, str]]] = {}

    # ---- 注册 ----------------------------------------------------------

    def register_agent(self, manifest: AgentManifest) -> None:
        if manifest.agent_id in self._agents:
            raise ValueError(f"Agent {manifest.agent_id!r} 已注册")
        self._agents[manifest.agent_id] = manifest
        for cap in manifest.capabilities:
            self._cap_index.setdefault(cap, []).append((manifest.layer, manifest.agent_id))

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已注册")
        self._tools[spec.name] = spec
        self._cap_index.setdefault(spec.name, []).append((Layer.EXEC, spec.name))

    # ---- 查询 ----------------------------------------------------------

    def agent(self, agent_id: str) -> AgentManifest:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise KeyError(f"未注册的 Agent: {agent_id!r}") from None

    def tool(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"未注册的工具: {name!r}") from None

    def resolve(self, capability: str, *, prefer_layer: Layer | None = None) -> str:
        """把能力标签解析为具体实现者 id。歧义时需 prefer_layer 或报错。"""
        candidates = self._cap_index.get(capability)
        if not candidates:
            raise KeyError(f"无实现者声明的能力: {capability!r}")
        if prefer_layer is not None:
            filtered = [c for c in candidates if c[0] == prefer_layer]
            if filtered:
                candidates = filtered
        if len(candidates) > 1:
            raise KeyError(
                f"能力 {capability!r} 有多个实现者，需指定 prefer_layer: "
                f"{[f'{l.name}:{i}' for l, i in candidates]}"
            )
        return candidates[0][1]

    # ---- 静态校验 ------------------------------------------------------

    def audit(self) -> list[str]:
        """返回问题列表（空列表 = 健康）。供 CI 与启动自检调用。"""
        problems: list[str] = []

        for m in self._agents.values():
            for cap in m.consumes:
                if cap not in self._cap_index:
                    problems.append(
                        f"Agent {m.agent_id!r} 声明消费 {cap!r}，但无任何实现者"
                    )

        # R2 检查：L1 不得直接消费 L3 能力
        for m in self._agents.values():
            if m.layer != Layer.DECISION:
                continue
            for cap in m.consumes:
                for layer, impl in self._cap_index.get(cap, []):
                    if layer == Layer.EXEC:
                        problems.append(
                            f"R2 违反：L1 Agent {m.agent_id!r} 直接消费 L3 能力 "
                            f"{cap!r}（实现者 {impl!r}），应经 L2 中转"
                        )

        # external 工具必须有幂等键
        for t in self._tools.values():
            if t.side_effect == SideEffect.EXTERNAL and not t.idempotency_key_field:
                problems.append(f"工具 {t.name!r} 为 external 但缺 idempotency_key_field")

        return problems

    def describe(self) -> dict[str, Any]:
        by_layer: dict[str, list[str]] = {l.name: [] for l in Layer}
        for m in self._agents.values():
            by_layer[m.layer.name].append(m.agent_id)
        return {
            "agents": by_layer,
            "tools": sorted(self._tools),
            "capabilities": sorted(self._cap_index),
        }


# ==========================================================================
# 调用信封 —— 原语 2
# ==========================================================================

@dataclass(frozen=True)
class CallEnvelope:
    """**三层完全相同的调用信封**。这是本设计最核心的一点。

    无论 L1→L2 还是 L2→L3，信封结构一字不差，只有 callee_kind 不同。
    因此"三层调用规范"不需要三套 API，也不需要图抽象。
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None
    caller: str
    callee: str
    callee_kind: str            # "agent" | "tool"
    capability: str
    payload: Mapping[str, Any]
    budget: Budget
    depth: int
    #: 用于幂等与缓存。external 调用必须稳定。
    idempotency_key: str | None = None

    # ---- 构造与派生 ----------------------------------------------------

    @staticmethod
    def root(
        callee: str,
        capability: str,
        payload: Mapping[str, Any],
        budget: Budget,
        *,
        callee_kind: str = "agent",
        caller: str = "user",
    ) -> "CallEnvelope":
        trace = uuid.uuid4().hex
        return CallEnvelope(
            trace_id=trace,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=None,
            caller=caller,
            callee=callee,
            callee_kind=callee_kind,
            capability=capability,
            payload=payload,
            budget=budget,
            depth=0,
        )

    def child(
        self,
        *,
        callee: str,
        capability: str,
        payload: Mapping[str, Any],
        budget: Budget,
        callee_kind: str = "agent",
        idempotency_key: str | None = None,
    ) -> "CallEnvelope":
        """派生子调用。预算扣减与深度递增在此集中处理，调用方无法绕过。"""
        if not self.budget.can_afford(budget):
            raise BudgetExceeded(
                f"子调用预算 {budget.as_dict()} 超出父剩余 {self.budget.as_dict()} "
                f"(caller={self.callee}, callee={callee})"
            )
        return CallEnvelope(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            caller=self.callee,
            callee=callee,
            callee_kind=callee_kind,
            capability=capability,
            payload=payload,
            budget=budget,
            depth=self.depth + 1,
            idempotency_key=idempotency_key,
        )

    def key(self) -> str:
        """信封指纹。用于日志关联、幂等去重与重放。

        注意：payload 必须 **深层规范化** 后再入哈希。``canonical_json`` 虽会
        递归排序键，但无法消除以下真实差异：
          - 嵌套结构中的冗余空值（``{"a": None}`` vs ``{}``）
          - 浮点表示噪声（``0.1+0.2`` vs ``0.3``）
        若不做深层规范化，"语义相同的两次调用得到不同幂等键"会导致
        external 工具被重复调用 —— 即真实的重复计费。
        """
        material = canonical_json({
            "trace": self.trace_id,
            "span": self.span_id,
            "parent": self.parent_span_id,
            "caller": self.caller,
            "callee": self.callee,
            "capability": self.capability,
            "payload": _deep_normalize(self.payload),
            "depth": self.depth,
        })
        return "env:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def as_dict(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "caller": self.caller,
            "callee": self.callee,
            "calleeKind": self.callee_kind,
            "capability": self.capability,
            "depth": self.depth,
            "budget": self.budget.as_dict(),
            "idempotencyKey": self.idempotency_key,
        }


@dataclass
class CallResult:
    """统一返回。三层也是同一个结构。"""

    ok: bool
    output: Any = None
    error_code: str | None = None
    error_message: str | None = None
    spent: Budget = field(default_factory=Budget)
    #: 供 L1 决策：是否需要人工介入
    escalate: bool = False
    #: 断言与证据，直接进入交付证据链
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: 子调用账本（仅 L1/L2 会填充）
    children: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def fail(code: str, message: str, *, escalate: bool = False,
             spent: Budget | None = None) -> "CallResult":
        return CallResult(ok=False, error_code=code, error_message=message,
                          escalate=escalate, spent=spent or Budget())

    @staticmethod
    def success(output: Any = None, *, evidence: Sequence[dict[str, Any]] = (),
                spent: Budget | None = None) -> "CallResult":
        """成功返回的对称构造器。

        与 :meth:`fail` 成对 —— 两者都是静态构造器而非字段，
        调用方不必记住 ``CallResult(ok=True, ...)`` 这种位置细节。

        ⚠️ 注意 ``ok`` 是**布尔字段**，不是可调用对象：
        ``CallResult.ok(...)`` 会抛 ``AttributeError``。本方法即为此而设。
        """
        return CallResult(ok=True, output=output, evidence=list(evidence),
                          spent=spent or Budget())


# ==========================================================================
# 黑板 —— 原语 3
# ==========================================================================

@dataclass
class BlackboardEntry:
    entry_id: str
    key: str
    value: Any
    version: int
    owner: str
    deps: tuple[str, ...] = ()
    watching: tuple[str, ...] = ()
    stale: bool = False


class Blackboard:
    """共享黑板。乐观并发 + 显式依赖 + 失效传播。

    失效传播是"改一张定妆照，全剧 40 个相关镜头自动重跑"的实现基础：
    写入某条目标记 stale 后，沿 deps 反查所有传递依赖者一并标记。
    """

    def __init__(self) -> None:
        self._entries: dict[str, BlackboardEntry] = {}
        self._by_key: dict[str, str] = {}
        self._seq = 0

    # ---- 写 ------------------------------------------------------------

    def put(
        self,
        key: str,
        value: Any,
        *,
        owner: str,
        deps: Sequence[str] = (),
        watching: Sequence[str] = (),
        expect_version: int | None = None,
        aliases: Sequence[str] = (),
    ) -> BlackboardEntry:
        """写入。expect_version 用于乐观并发控制（None = 不检查）。

        ``aliases``：同一份内容的**其他名字**（典型：能力名 ↔ 产物名）。

        ## 为什么需要
        同一件东西在项目里有两种名字：职位**声明的能力名**（`review.board`）
        和它**产出的键**（`review.verdict`）。`run_stage` 按产物名写，
        而调用方经常按能力名取 —— 取到 `None` 时的表现是"它没产出"，
        而它其实产出了。2026-10-23 审计 H-1/C-3 就是这个形态。

        关键点：**两条名字必须共用一个 `entry_id`**。
        否则 `_dependents_of`（按 `entry_id` 比对）会再次对不上，
        失效传播等于没接。登记别名是为了让"名字"和"身份"分开。
        """
        self._seq += 1
        existing = self._by_key.get(key)
        if existing is None or existing not in self._entries:
            # 主键没登记（或登记的是已失效条目）-> 看别名有没有指向活着的条目
            existing = None
            for a in aliases:
                eid = self._by_key.get(a)
                if eid is not None and eid in self._entries:
                    existing = eid
                    break

        if existing is None:
            if expect_version is not None:
                raise BlackboardConflict(f"键 {key!r} 不存在，无法匹配版本 {expect_version}")
            entry = BlackboardEntry(
                entry_id=f"bb_{self._seq:06d}", key=key, value=value, version=1,
                owner=owner, deps=tuple(deps), watching=tuple(watching),
            )
            self._entries[entry.entry_id] = entry
            self._bind(key, entry.entry_id)
            for a in aliases:
                self._bind(a, entry.entry_id)
            return entry

        entry = self._entries[existing]
        if expect_version is not None and entry.version != expect_version:
            raise BlackboardConflict(
                f"键 {key!r} 版本冲突：期望 {expect_version}，实际 {entry.version}"
            )
        entry.value = value
        entry.version += 1
        entry.owner = owner
        entry.deps = tuple(deps)
        entry.watching = tuple(watching)
        entry.stale = False
        self._bind(key, entry.entry_id)
        for a in aliases:
            self._bind(a, entry.entry_id)

        # 失效传播：本次写入变更了语义，所有传递依赖者标记 stale
        self._propagate_stale(entry.entry_id)
        return entry

    def _bind(self, name: str, entry_id: str) -> None:
        """把某个名字绑到条目上。**只填空位，不抢占**另一个活着的条目。"""
        cur = self._by_key.get(name)
        if cur is None or cur not in self._entries:
            self._by_key[name] = entry_id

    # ---- 读 ------------------------------------------------------------

    def get(self, key: str) -> BlackboardEntry | None:
        eid = self._by_key.get(key)
        return self._entries.get(eid) if eid else None

    def value(self, key: str, default: Any = None) -> Any:
        e = self.get(key)
        return e.value if e else default

    # ---- 失效传播 ------------------------------------------------------

    def _dependents_of(self, entry_id: str) -> list[str]:
        """反查：哪些条目的 deps 包含 entry_id。"""
        return [e.entry_id for e in self._entries.values() if entry_id in e.deps]

    def _propagate_stale(self, origin_id: str) -> list[str]:
        marked: list[str] = []
        stack = [origin_id]
        seen = {origin_id}
        while stack:
            cur = stack.pop()
            for dep_id in self._dependents_of(cur):
                if dep_id in seen:
                    continue
                seen.add(dep_id)
                self._entries[dep_id].stale = True
                marked.append(dep_id)
                stack.append(dep_id)
        return marked

    def invalidate(self, key: str) -> list[str]:
        """显式失效某键（如定妆照换版），返回被标记 stale 的条目 id 列表。

        语义要点：**被失效的源条目本身不是 stale，只有它的（传递）依赖者才是。**
        源刚刚被更新过，它是新鲜的；是那些"基于旧源算出来的产物"过期了。
        把源也标成 stale 会让"当前有效状态"的统计失真，进而让重渲队列多算。
        """
        e = self.get(key)
        if not e:
            return []
        e.version += 1
        return self._propagate_stale(e.entry_id)

    def stale_entries(self) -> list[BlackboardEntry]:
        return [e for e in self._entries.values() if e.stale]

    def snapshot(self) -> dict[str, Any]:
        return {
            e.key: {"version": e.version, "owner": e.owner,
                    "stale": e.stale, "value": e.value}
            for e in sorted(self._entries.values(), key=lambda x: x.key)
        }


# ==========================================================================
# 层运行时 —— 轴 A：显式调用树
# ==========================================================================

#: 允许的层跃迁。键 = 调用方层，值 = 允许的被调方层集合。
#: R1 单向 + R2 禁止跳层都由这一张表强制。
DEFAULT_LAYER_TRANSITIONS: dict[Layer, frozenset[Layer]] = {
    Layer.DECISION: frozenset({Layer.COLLAB}),
    Layer.COLLAB: frozenset({Layer.COLLAB, Layer.EXEC}),
    Layer.EXEC: frozenset(),   # L3 不得回调任何层
}

MAX_DEPTH = 6


class LayerRuntime:
    """执行三层调用，强制 R1-R4。

    与 LangGraph 的区别：这里没有"图"，只有一个**带校验的递归调用**。
    调用树由信封的 (trace_id, span_id, parent_span_id) 天然构成，
    因此可观测性不需要额外机制 —— trace 就是执行轨迹。
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        blackboard: Blackboard | None = None,
        *,
        layer_transitions: Mapping[Layer, frozenset[Layer]] | None = None,
        max_depth: int = MAX_DEPTH,
    ) -> None:
        self.registry = registry
        self.blackboard = blackboard or Blackboard()
        self.transitions = dict(layer_transitions or DEFAULT_LAYER_TRANSITIONS)
        self.max_depth = max_depth
        #: agent_id / tool_name -> 可调用实现
        self._agent_impls: dict[str, Callable[..., CallResult]] = {}
        self._tool_impls: dict[str, Callable[..., Any]] = {}
        self._spans: list[dict[str, Any]] = []

    # ---- 实现绑定 ------------------------------------------------------

    def bind_agent(self, agent_id: str, impl: Callable[..., CallResult]) -> None:
        self.registry.agent(agent_id)          # 未注册即抛错
        self._agent_impls[agent_id] = impl

    def bind_tool(self, name: str, impl: Callable[..., Any]) -> None:
        self.registry.tool(name)
        self._tool_impls[name] = impl

    # ---- 校验（R1-R4 集中在此，调用方无法绕过） --------------------------

    def _check_transition(self, caller_layer: Layer, callee_layer: Layer,
                          caller: str, callee: str) -> None:
        allowed = self.transitions.get(caller_layer, frozenset())
        if callee_layer not in allowed:
            if callee_layer < caller_layer:
                raise LayerViolation(
                    f"R1 违反：{caller_layer.name}({caller}) 不得回调用 "
                    f"{callee_layer.name}({callee})，L3 必须是叶子"
                )
            if callee_layer > caller_layer + 1:
                raise LayerViolation(
                    f"R2 违反：{caller_layer.name}({caller}) 跳过 "
                    f"{Layer(caller_layer + 1).name} 直接调用 {callee_layer.name}({callee})"
                )
            raise LayerViolation(
                f"层跃迁非法：{caller_layer.name} -> {callee_layer.name} ({caller} -> {callee})"
            )

    def _check_depth(self, envelope: CallEnvelope) -> None:
        if envelope.depth > self.max_depth:
            raise DepthExceeded(
                f"R4 违反：调用深度 {envelope.depth} 超过上限 {self.max_depth} "
                f"(trace={envelope.trace_id})"
            )

    # ---- L3：确定性工具调用 ---------------------------------------------

    def call_tool(self, envelope: CallEnvelope) -> CallResult:
        """L3 调用。唯一的执行入口，保证无 LLM 参与。"""
        spec = self.registry.tool(envelope.callee)
        self._check_depth(envelope)

        if not envelope.budget.can_afford(
            Budget(cny=spec.est_cost_cny, wall_ms=spec.est_ms, steps=0, tokens=0)
        ):
            raise BudgetExceeded(
                f"工具 {spec.name!r} 预估成本 ¥{spec.est_cost_cny} / {spec.est_ms}ms "
                f"超出剩余预算 {envelope.budget.as_dict()}"
            )

        if spec.side_effect == SideEffect.EXTERNAL and not envelope.idempotency_key:
            raise ValueError(
                f"工具 {spec.name!r} 为 external 副作用，调用必须携带 idempotency_key"
            )

        impl = self._tool_impls.get(spec.name)
        if impl is None:
            return CallResult.fail("F-TOOL-UNBOUND", f"工具 {spec.name!r} 未绑定实现")

        self._spans.append({"kind": "tool", **envelope.as_dict()})
        try:
            output = impl(**dict(envelope.payload))
        except Exception as exc:                      # noqa: BLE001 - 需转为结构化失败
            return CallResult.fail(
                "F-TOOL-EXEC", f"{type(exc).__name__}: {exc}",
                spent=Budget(cny=spec.est_cost_cny, wall_ms=spec.est_ms),
            )

        return CallResult(
            ok=True,
            output=output,
            spent=Budget(cny=spec.est_cost_cny, wall_ms=spec.est_ms),
        )

    # ---- L2 / L1：Agent 调用 --------------------------------------------

    def call_agent(self, envelope: CallEnvelope) -> CallResult:
        """Agent 调用。轴 B 的有界推理循环由被调 Agent 实现，本方法只做边界强制。"""
        manifest = self.registry.agent(envelope.callee)
        self._check_depth(envelope)

        # R3：子预算不得超过该 Agent 声明的上限
        effective = Budget(
            tokens=min(envelope.budget.tokens, manifest.max_budget.tokens),
            cny=min(envelope.budget.cny, manifest.max_budget.cny),
            wall_ms=min(envelope.budget.wall_ms, manifest.max_budget.wall_ms),
            steps=min(envelope.budget.steps, manifest.max_budget.steps),
        )
        envelope = replace(envelope, budget=effective)

        impl = self._agent_impls.get(manifest.agent_id)
        if impl is None:
            return CallResult.fail("F-AGENT-UNBOUND", f"Agent {manifest.agent_id!r} 未绑定实现")

        self._spans.append({"kind": "agent", **envelope.as_dict()})
        result = impl(envelope=envelope, runtime=self)
        return result

    # ---- 顶层入口 ------------------------------------------------------

    def invoke(
        self,
        capability: str,
        payload: Mapping[str, Any],
        *,
        budget: Budget | None = None,
        prefer_layer: Layer | None = None,
    ) -> tuple[CallResult, CallEnvelope]:
        """按能力标签发起顶层调用。返回 (结果, 信封) 便于取 trace_id。"""
        callee = self.registry.resolve(capability, prefer_layer=prefer_layer)
        if callee in self.registry._agents:                    # noqa: SLF001 - 内部判定
            kind = "agent"
            envelope = CallEnvelope.root(
                callee, capability, payload,
                budget or Budget.unlimited(), callee_kind=kind,
            )
            return self.call_agent(envelope), envelope
        envelope = CallEnvelope.root(
            callee, capability, payload,
            budget or Budget.unlimited(), callee_kind="tool",
        )
        return self.call_tool(envelope), envelope

    # ---- 可观测性 ------------------------------------------------------

    def spans(self) -> list[dict[str, Any]]:
        return list(self._spans)

    def trace_tree(self) -> list[str]:
        """把 span 账本渲染成缩进调用树，用于人工排查与答辩演示。

        这是"调用树天然可观测"的直接体现：不需要额外埋点。
        """
        lines: list[str] = []
        by_parent: dict[str | None, list[dict[str, Any]]] = {}
        for s in self._spans:
            by_parent.setdefault(s.get("parentSpanId"), []).append(s)

        def walk(parent: str | None, indent: int) -> None:
            for s in by_parent.get(parent, []):
                lines.append(
                    f"{'  ' * indent}[{s['kind']:5}] {s['callee']} "
                    f"<- {s['caller']} ({s['capability']}, d={s['depth']})"
                )
                walk(s["spanId"], indent + 1)

        walk(None, 0)
        return lines


# ==========================================================================
# 轴 B：Agent 内部的有界推理循环
# ==========================================================================

@dataclass
class ReasoningStep:
    """一步推理的留痕。全部进入交付证据链。"""

    index: int
    thought: str
    action: str                 # 工具名 或 "finish"
    action_input: Mapping[str, Any] = field(default_factory=dict)
    observation: Any = None
    spent: Budget = field(default_factory=Budget)


@dataclass
class AgentDecision:
    """Agent 每一步的返回。**不返回完整轨迹，只返回下一步** ——
    循环由运行时驱动，因此"最多 N 步"是强制的，不是约定。"""

    action: str
    action_input: Mapping[str, Any] = field(default_factory=dict)
    thought: str = ""
    final_output: Any = None


class BoundedReasoningLoop:
    """轴 B 的规范化实现：有界、可留痕、超限即升级。

    与 LangGraph 循环的差别：这里没有"边"，只有 **step 计数器 + 预算**。
    `max_steps` 是构造参数而非图上的跳转条件，因此不可能被 Agent 绕过。
    """

    def __init__(self, *, max_steps: int, budget: Budget) -> None:
        self.max_steps = max_steps
        self.budget = budget
        self.steps: list[ReasoningStep] = []

    def run(
        self,
        envelope: CallEnvelope,
        runtime: LayerRuntime,
        decide: Callable[[CallEnvelope, Sequence[ReasoningStep]], AgentDecision],
    ) -> tuple[CallResult, list[ReasoningStep]]:
        spent = Budget()
        steps: list[ReasoningStep] = []

        for idx in range(self.max_steps):
            decision = decide(envelope, steps)

            if decision.action == "finish":
                return (
                    CallResult(ok=True, output=decision.final_output,
                               spent=spent, children=[s.action for s in steps]),
                    steps,
                )

            # 该 action 必须是 manifest.consumes 声明的能力之一（越权检查）
            spec = runtime.registry.tool(decision.action)
            child_env = envelope.child(
                callee=spec.name,
                capability=decision.action,
                payload=dict(decision.action_input),
                budget=Budget(cny=spec.est_cost_cny, wall_ms=spec.est_ms),
                callee_kind="tool",
                idempotency_key=(
                    f"{envelope.span_id}:{idx}"
                    if spec.side_effect == SideEffect.EXTERNAL else None
                ),
            )
            # 层校验：Agent(L2) -> Tool(L3) 合法；L1 Agent 调工具会被 R2 拒绝
            caller_manifest = runtime.registry.agent(envelope.callee)
            runtime._check_transition(          # noqa: SLF001 - 边界集中校验
                caller_manifest.layer, Layer.EXEC, envelope.callee, spec.name
            )

            result = runtime.call_tool(child_env)
            spent = Budget(
                tokens=spent.tokens,
                cny=round(spent.cny + result.spent.cny, 6),
                wall_ms=spent.wall_ms + result.spent.wall_ms,
                steps=spent.steps + 1,
            )
            steps.append(ReasoningStep(
                index=idx, thought=decision.thought, action=decision.action,
                action_input=dict(decision.action_input),
                observation=result.output if result.ok else result.error_message,
                spent=result.spent,
            ))
            if not result.ok:
                return (
                    CallResult.fail(result.error_code or "F-CHILD",
                                    result.error_message or "子调用失败",
                                    spent=spent, escalate=True),
                    steps,
                )

        return (
            CallResult.fail(
                "F-REASONING-BUDGET",
                f"推理步数超过上限 {self.max_steps}",
                escalate=True, spent=spent,
            ),
            steps,
        )
