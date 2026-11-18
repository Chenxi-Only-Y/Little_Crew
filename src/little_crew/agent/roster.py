"""Agent 名册 —— **按剧组职业组织**的 19 个 Agent 声明（注册表驱动）。

命名即架构
----------
本平台叫「**小剧组**」，所以 Agent 不是抽象的 "agent_1/2/3"，
而是**剧组的真实职位**：导演、编剧、美术指导、摄影指导、灯光师、
剪辑师、录音师、场记、制片主任、审片…

这样组织的三个实际好处（不是命名偏好）：

  1. **职责边界天然清晰**。真实剧组里"谁管什么"已经过几百年打磨，
     照搬比自创更不容易出现职责重叠或真空。
  2. **产出物名可直接对应**。摄影指导交镜头、剪辑师交成片、
     场记交场记单 —— 不需要额外发明一套产物分类。
  3. **人机协作时的心智模型一致**。用户说"让灯光师重打一下光"，
     比"调用 gaffer_agent 重跑 lighting_pass"自然得多。

一个关键的架构修正
------------------
**确定性职责不再是 Agent，而是工具。**

技术质检（查分辨率/时长/帧率/编码）与合规核对（查归属标注是否注入）
本质是**规则判定**，不需要模型决策。把它们做成 Agent 的后果是：
每次质检都要花 token、引入不确定性、且**永远不如一条断言可靠**。

它们的正确位置是 **L3 执行工具**（`gate.assert` / `qc.tech_check`），
由**剪辑师**或**审片**在需要时调用。

这也让 Agent 名册回归它该有的语义：**Agent = 需要判断的角色**。

与 `protocol.py` 的关系
-----------------------
``protocol.py`` 提供管道（三原语 + R1–R4 层边界），本模块提供内容。
`AgentSpec` 是 `AgentManifest` 的**外层包装**：

  · `AgentManifest`  给运行时（层、预算、推理步数上限、能力标签）
  · `AgentSpec`      给人（职位名、部门、执行方式、任务档案、提示词模板）

⚠️ `AgentManifest.consumes` 的语义是「**允许调用的下游能力**」，
**不是**「需要的输入数据」。后者由 `AgentSpec.requires` 表达。
审计规则会检查 **L1 不得直接 consumes 任何 L3 能力**（R2 禁止跳层）。

注册表驱动（2026-11-18，B1 全职业插件化）
----------------------------------------
19 个核心职位已从本模块**迁出**到 `plugins/core_crew.py`，通过插件
`PluginManifest.positions` 声明；本模块不再硬编码任何 `AgentSpec`：

  · `DEFAULT_ROSTER`：默认单集名册快照（19 条）—— 由**注册表中
    `default_roster=True` 的插件职位**构建，保留给既有 17 处导入（向后兼容）
  · `positions()`：**运行时注册表全量职位**（含已加载插件职位，如 showrunner）
    —— 无限画布/依赖图用它，加载插件后自动多出节点
  · `build_registry` / `roster_by_*` / `validate_roster` / `roster_warnings`：
    名册查询与校验（默认参数仍指向 `DEFAULT_ROSTER`）

`Department` / `Stage` / `ExecutionKind` / `STAGE_ORDER` / `AgentSpec` 已原样
迁到 `agent/crew_spec.py`，本模块 re-export 它们以保持向后兼容。
"""

from __future__ import annotations

from typing import Any, Sequence

from little_crew.agent.crew_spec import (
    AgentSpec,
    Department,
    ExecutionKind,
    STAGE_ORDER,
    Stage,
)
from little_crew.agent.protocol import (
    CapabilityRegistry,
    SideEffect,
    ToolSpec,
)
from little_crew.plugin import ensure_default_plugins, get_registry

__all__ = [
    "Department",
    "Stage",
    "ExecutionKind",
    "AgentSpec",
    "DEFAULT_ROSTER",
    "STAGE_ORDER",
    "positions",
    "build_registry",
    "roster_by_department",
    "roster_by_stage",
    "execution_kind_counts",
    "validate_roster",
    "roster_warnings",
]

# ==========================================================================
# 注册表驱动（2026-11-18，B1）：名册不再手写，从插件 positions 构建
# ==========================================================================
#
# 导入期 `ensure_default_plugins()` 注册 `core_crew`（19 职位，默认名册）。
# 注意循环导入：本模块 -> `plugin`（机制层，root-level 豁免）-> importlib
# 加载 `plugins/core_crew` -> 它只 import `agent/crew_spec`（不 import 本模块）
# -> 无环。冒烟：`python -c "import little_crew.agent.roster"`。
ensure_default_plugins()

#: 默认单集名册（19 条）：注册表中 `default_roster=True` 的插件职位。
#: **模块级快照** —— 插件后续卸载不会改变它（保证默认流水线稳定）。
DEFAULT_ROSTER: tuple[AgentSpec, ...] = tuple(get_registry().default_roster())


def positions() -> tuple[AgentSpec, ...]:
    """运行时注册表的**全量职位**（默认 19；加载 showrunner 后 20）。

    与 `DEFAULT_ROSTER` 的区别：
      · `DEFAULT_ROSTER` 是默认单集名册的**快照**（19，不含插件专属职位）
      · `positions()` 是**当前注册表**的全量声明（含插件专属职位），
        无限画布/依赖图用它 —— 加载插件后画布自动多节点。

    幂等：先 `ensure_default_plugins()`（缺默认插件时补注册，如被卸载过）。
    """
    ensure_default_plugins()
    return tuple(get_registry().positions())


#: **代码产出**的产物（不是任何职位交付的）：`shot.image` 由渲染通道产出。
#: 2026-11-12 删掉「摄影指导」后，这条是"出图不是职位判断"的显式记录。
CODE_OUTPUTS: frozenset = frozenset({
    "shot.image",          # 渲染通道出图
    "episode.master",      # 成片：由 runtime 的成片阶段（ffmpeg）产出
    "delivery.evidence",   # 交付证据链：由 runtime 的交付阶段产出
})

#: 由**代码**消费的产物：渲染通道取 `previs.layouts` 当控制图。
CODE_CONSUMED: frozenset = frozenset({"previs.layouts", "post.plan"})


# ==========================================================================
# L3 执行工具（确定性职责都在这里，不在 Agent 里）
# ==========================================================================

def _exec_tools() -> list[ToolSpec]:
    """执行工具。**确定性、可复现、零模型成本。**

    这里同时容纳了两类东西：
      · 原本就该是工具的（编译、提交、合成）
      · **从 Agent 降级下来的**（技术质检、合规核对）—— 它们本质是规则判定，
        做成 Agent 只会花钱、引入不确定性、且永远不如一条断言可靠。
    """
    return [
        ToolSpec(
            name="compile.ir",
            input_schema={"type": "object", "required": ["ir"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.PURE,
            idempotent=True,
            est_ms=50,
        ),
        ToolSpec(
            name="comfy.submit",
            input_schema={"type": "object", "required": ["graph"]},
            output_schema={"type": "object"},
            # 调外部服务（本地 ComfyUI 也是独立进程），重试需幂等键
            side_effect=SideEffect.EXTERNAL,
            idempotent=True,
            idempotency_key_field="idempotencyKey",
            est_ms=30_000,
            requires_gpu=True,
        ),
        ToolSpec(
            name="gate.assert",
            input_schema={"type": "object",
                          "required": ["artifacts", "constraints"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.READ,
            idempotent=True,
            est_ms=200,
        ),
        ToolSpec(
            name="qc.tech_check",
            input_schema={"type": "object", "required": ["artifacts"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.READ,
            idempotent=True,
            est_ms=500,
            # 由 Agent 降级而来：查分辨率/时长/帧率/编码/音画同步
        ),
        ToolSpec(
            name="license.audit",
            input_schema={"type": "object", "required": ["modelIds"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.PURE,
            idempotent=True,
            est_ms=10,
            # 由 Agent 降级而来：核对归属标注与部署地区授权
        ),
        ToolSpec(
            name="shot.image",
            input_schema={"type": "object", "required": ["ir"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.EXTERNAL,
            idempotent=True,
            idempotency_key_field="idempotencyKey",
            est_ms=21_000,
            requires_gpu=True,
            # **2026-11-12 从「摄影指导」降级而来**：出图是通道执行
            # （`runtime/render.py`），不是某个职位的判断。
            # 删掉那个职位后，`shot.image` 作为**代码能力**留在这里，
            # 这样 `director.consumes` 仍能解析，而名册里不再有"摄影指导"。
        ),
        ToolSpec(
            name="ffmpeg.assemble",
            input_schema={"type": "object", "required": ["clips"]},
            output_schema={"type": "object"},
            side_effect=SideEffect.WRITE,
            idempotent=True,
            est_ms=5_000,
        ),
    ]


# ==========================================================================
# 查询与校验
# ==========================================================================

def build_registry(roster: Sequence[AgentSpec] = DEFAULT_ROSTER,
                   *, register_tools: bool = True) -> CapabilityRegistry:
    """把名册灌入 `CapabilityRegistry`。

    ``register_tools=True`` 时同时注册 L3 工具 —— 否则审计会报
    "声明消费的能力无实现者"，因为那些能力其实由工具提供。
    """
    reg = CapabilityRegistry()
    for spec in roster:
        reg.register_agent(spec.manifest())
    if register_tools:
        for tool in _exec_tools():
            reg.register_tool(tool)
    return reg


def roster_by_department(roster: Sequence[AgentSpec] = DEFAULT_ROSTER
                         ) -> dict[str, list[AgentSpec]]:
    out: dict[str, list[AgentSpec]] = {d.value: [] for d in Department}
    for s in roster:
        out[s.department.value].append(s)
    return out


def roster_by_stage(roster: Sequence[AgentSpec] = DEFAULT_ROSTER
                    ) -> dict[str, list[AgentSpec]]:
    out: dict[str, list[AgentSpec]] = {s.value: [] for s in Stage}
    for spec in roster:
        out[spec.stage.value].append(spec)
    return out


def execution_kind_counts(roster: Sequence[AgentSpec] = DEFAULT_ROSTER
                          ) -> dict[str, int]:
    out: dict[str, int] = {k.value: 0 for k in ExecutionKind}
    for s in roster:
        out[s.execution.value] += 1
    return out


def validate_roster(roster: Sequence[AgentSpec] = DEFAULT_ROSTER) -> list[str]:
    """静态校验名册自身的一致性。返回问题列表（空 = 健康）。

    检查项：
      1. agent_id 唯一
      2. **有且仅有一个导演**（L1 编排中枢不可多）
      3. `requires` 闭合：每个依赖都有职位产出它
      4. `produces` 无孤儿：每个产物都有人消费，或属**已知终态/档案类交付物**
      5. `consumes` 与 `tools` 可解析
      6. **审核方不得审自己的产出**（"不自我签收"的正确表述）
      7. GPU 职位数提示（本机同时只能加载一个模型）

    ⚠️ **第 6 条曾经写错**：早期判据是"生成了内容又声明审核能力"，
    于是把**剧本顾问**与**文学策划**判为违规 —— 而那两位**只**审别人的稿，
    审核正是它们唯一的职责。错在把"有审核能力"当成了"自我签收"。

    正确判据是**产出归属**：某职位交付的产物，不得由它自己审核。
    """
    problems: list[str] = []

    # 1) id 唯一
    seen: dict[str, int] = {}
    for s in roster:
        seen[s.agent_id] = seen.get(s.agent_id, 0) + 1
    for aid, n in seen.items():
        if n > 1:
            problems.append(f"agent_id 重复 {aid!r}（出现 {n} 次）")

    # 2) 唯一导演
    directors = [s for s in roster if s.department is Department.DIRECTOR]
    if len(directors) != 1:
        problems.append(
            f"应有且仅有一个「导演」（Department.DIRECTOR），实得 {len(directors)} 个"
        )

    # 3) 依赖闭合
    produced: set[str] = set()
    for s in roster:
        produced.update(s.produces)
    required: set[str] = set()
    for s in roster:
        required.update(s.requires)

    #: 外部输入（来自用户/上游系统，不必由任何职位产出）
    external_inputs = {"source.novel"}
    #: 代码产出/消费的产物（模块级常量，见 `CODE_OUTPUTS` / `CODE_CONSUMED`）
    code_outputs = set(CODE_OUTPUTS)

    for s in roster:
        for r in s.requires:
            if r not in produced and r not in external_inputs and r not in code_outputs:
                problems.append(
                    f"「{s.crew_title}」需要 {r!r}，但无任何职位交付它")

    #: 终态交付物与**档案类产物**无需下游消费者。
    #: 前者是项目产出，后者是留档（美术 bible、角色设定表、场记单、交付证据链）——
    #: 真实剧组里这些本来就是给人查的，不是流水线的中间件。
    #: 由**代码**消费的产物 —— 孤儿检查放行（消费者不是职位）
    code_consumed = set(CODE_CONSUMED)

    terminal_outputs = {
        "episode.master",       # 成片
        "delivery.evidence",    # 交付证据链
        "compliance.report",    # 合规报告（留档）
        "continuity.report",    # 场记报告（留档）
        "qc.report",            # 质检报告：由 run() 读取并执行重画（代码消费）
        "cover.image",          # 封面：交付物（报告页/答辩使用）
        "continuity.sheet",     # 场记单（真实剧组的现场记录，本就是档案）
        "art.bible",            # 美术 bible（档案）
        "character.sheet",      # 角色设定表（档案，供人查阅）
        "shot.breakdown",       # 镜头分解表（档案）
        "pacing.report",        # 节奏报告（留档）
        "review.verdict",       # 审片结论（最终裁定，留档）
        "episode.plan",         # 导演的编排计划 —— 它是**控制信号**不是数据依赖，
                                # 由编排器消费，不进产物依赖图
    }
    # 导演消费审查意见（据此决定返工）—— 显式补入，避免误报孤儿
    for s in roster:
        if s.department is Department.DIRECTOR:
            required.update(s.consumes)

    for p in sorted(produced):
        if p not in required and p not in terminal_outputs and p not in code_consumed:
            problems.append(f"产物 {p!r} 无人消费（孤儿产出，白算）")

    # 5) consumes / tools 可解析
    try:
        reg = build_registry(roster)
        for s in roster:
            for cap in s.consumes:
                try:
                    reg.resolve(cap)
                except KeyError as e:
                    problems.append(f"「{s.crew_title}」的 consumes 无法解析: {e}")
            for t in s.tools:
                try:
                    reg.tool(t)
                except KeyError as e:
                    problems.append(f"「{s.crew_title}」的 tools 无法解析: {e}")
    except Exception as e:  # noqa: BLE001
        problems.append(f"名册无法灌入注册表: {type(e).__name__}: {e}")

    # 6) 不自我签收 —— 按**产出归属**判定，而非按"是否具备审核能力"
    artifact_owner: dict[str, str] = {}
    for s in roster:
        for p in s.produces:
            artifact_owner.setdefault(p, s.agent_id)

    #: 审核类能力 -> 它审查的产物类型
    review_targets: dict[str, tuple[str, ...]] = {
        "script.critique": ("script.draft",),
        "style.guard": ("script.draft",),
        "continuity.check": ("shot.image", "character.anchor_set"),
        "review.board": ("shot.image", "shot.video"),
    }
    for s in roster:
        for cap in s.capabilities:
            for target in review_targets.get(cap, ()):
                if artifact_owner.get(target) == s.agent_id:
                    problems.append(
                        f"违反「不自我签收」：「{s.crew_title}」的 {cap!r} "
                        f"要审查 {target!r}，而该产物正是它自己交付的"
                    )

    # ---- 7) GPU 调度约束见 roster_warnings() ----
    #
    # 刻意**不放进 problems**：它不是错误而是约束（本机 8 GB 显存同时只能加载
    # 一个模型），修不好也不需要修好。若混进 validate_roster 的返回值，
    # "名册健康"将永远为假 —— 人就会开始忽略这个信号（告警疲劳）。
    return problems


def roster_warnings(roster: Sequence[AgentSpec] = DEFAULT_ROSTER) -> list[str]:
    """名册的**约束提示**（非错误）。与 `validate_roster` 分开，避免告警疲劳。"""
    warns: list[str] = []
    gpu_roles = [s.crew_title for s in roster if s.execution.needs_gpu]
    if len(gpu_roles) > 1:
        warns.append(
            f"有 {len(gpu_roles)} 个职位需要 GPU（{'、'.join(gpu_roles)}）。"
            f"本机 8 GB 显存同时只能加载一个模型 —— 制片主任必须"
            f"**按模型分批调度**（把用同一底模的镜头排在一起），而非按镜头顺序逐个跑。"
        )
    no_profile = [s.crew_title for s in roster
                  if s.execution is not ExecutionKind.ASSEMBLY and not s.task_profile]
    if no_profile:
        warns.append(f"以下职位未声明任务档案（无法求解模型）：{'、'.join(no_profile)}")
    return warns
