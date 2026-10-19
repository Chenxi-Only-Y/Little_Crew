"""Agent 名册 —— **按剧组职业组织**的 18 个 Agent 声明。

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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from little_crew.agent.protocol import (
    AgentManifest,
    Budget,
    CapabilityRegistry,
    Layer,
    SideEffect,
    ToolSpec,
)

__all__ = [
    "Department",
    "Stage",
    "ExecutionKind",
    "AgentSpec",
    "DEFAULT_ROSTER",
    "STAGE_ORDER",
    "build_registry",
    "roster_by_department",
    "roster_by_stage",
    "execution_kind_counts",
    "validate_roster",
    "roster_warnings",
]


# ==========================================================================
# 部门（剧组建制）
# ==========================================================================

class Department(str, Enum):
    """剧组部门。这是**组织维度**，与运行时层（`Layer`）正交。

    | 部门 | 现实职责 | 本平台对应 |
    |---|---|---|
    | 导演组 | 艺术决策与调度 | L1 决策 |
    | 编剧组 | 剧本创作与修订 | 剧本形态 |
    | 美术组 | 视觉资产与一致性 | 角色/场景锚点 |
    | 摄影组 | 镜头设计（分镜 + 灯光） | 镜头表与光照 |
    | 录音组 | 对白与音乐 | 音轨 |
    | 剪辑组 | 节奏与成片 | 分镜节奏、合成 |
    | 制片组 | 计划与合规 | 调度、版权 |
    | 审片组 | 审看与验收 | 门禁 |
    """

    DIRECTOR = "director"        # 导演组
    SCREENPLAY = "screenplay"    # 编剧组
    ART = "art"                  # 美术组
    CAMERA = "camera"            # 摄影组
    SOUND = "sound"              # 录音组
    EDITORIAL = "editorial"      # 剪辑组
    PRODUCTION = "production"    # 制片组
    REVIEW = "review"            # 审片组


class Stage(str, Enum):
    """生产阶段。顺序即依赖顺序。`GATE` 为横切阶段（审片可在任意出口介入）。"""

    SCRIPT = "script"          # 剧本
    ASSET = "asset"            # 资产（角色/场景锚点）
    STORYBOARD = "storyboard"  # 分镜
    RENDER = "render"          # 渲染
    ASSEMBLE = "assemble"      # 合成
    GATE = "gate"              # 门禁（横切）


#: 阶段的线性顺序。`GATE` 不参与排序。
STAGE_ORDER: tuple[Stage, ...] = (
    Stage.SCRIPT, Stage.ASSET, Stage.STORYBOARD, Stage.RENDER, Stage.ASSEMBLE,
)


class ExecutionKind(str, Enum):
    """该职位**靠什么执行**。决定成本、确定性与是否需要 GPU。

    **立这个字段的理由**：把"确定性职责"从 Agent 里剥离出去之后，
    剩下的 Agent 也不是同质的 —— 有的是纯文本推理（编剧），
    有的是纯生成（摄影），有的是视觉判定（审片）。
    混为一谈会让成本测算与调度都失准。
    """

    TEXT_LLM = "text_llm"        # 文本模型推理（编剧、剧评）
    VLM = "vlm"                  # 视觉模型判定（审片、场记核对）
    IMAGE_GEN = "image_gen"      # 图像生成（美术、摄影）
    VIDEO_GEN = "video_gen"      # 视频生成（摄影）
    AUDIO_GEN = "audio_gen"      # 音频生成（录音）
    ASSEMBLY = "assembly"        # 组装与落盘（剪辑、档案），调确定性工具

    @property
    def needs_gpu(self) -> bool:
        return self in (ExecutionKind.IMAGE_GEN, ExecutionKind.VIDEO_GEN,
                        ExecutionKind.AUDIO_GEN)

    @property
    def is_generative(self) -> bool:
        """生成型：产出新素材。**生成方不得自任审核方**（不自我签收）。"""
        return self in (ExecutionKind.IMAGE_GEN, ExecutionKind.VIDEO_GEN,
                        ExecutionKind.AUDIO_GEN, ExecutionKind.TEXT_LLM)


# ==========================================================================
# Agent（剧组职位）声明
# ==========================================================================

@dataclass(frozen=True)
class AgentSpec:
    """一个剧组职位的完整声明。**声明式**：新增职位 = 加一条，不改框架。"""

    agent_id: str
    #: 剧组职位名（中文），如 "摄影指导"
    crew_title: str
    #: 英文职位名，便于与 industry 惯例对照
    crew_title_en: str
    department: Department
    stage: Stage
    execution: ExecutionKind
    #: 能力标签，供路由匹配（与 AgentManifest.capabilities 必须一致）
    capabilities: tuple[str, ...]
    #: 该职位**交付**的产物类型
    produces: tuple[str, ...] = ()
    #: 该职位**需要**哪些产物作为输入（数据依赖，与 consumes 不同！）
    requires: tuple[str, ...] = ()
    #: 允许调用的**下游能力标签**（喂给 AgentManifest.consumes）
    consumes: tuple[str, ...] = ()
    #: 允许调用的**L3 工具名**（剪辑/档案等确定性职位用）
    tools: tuple[str, ...] = ()
    #: 任务档案名，走 `models.profile_for` 求解模型 —— **不硬编码 model_id**
    task_profile: str = ""
    #: 提示词模板的**引用名**（内容不入代码，见 STRUCTURE.md 配置分类）
    prompt_template: str = ""
    max_reasoning_steps: int = 3
    max_budget_cny: float = 0.0
    max_budget_tokens: int = 0
    retryable: bool = True
    #: 真实剧组里这个职位的职责，用于校验我们的抽象没跑偏
    real_world_duty: str = ""
    notes: str = ""

    # ---- 派生 ----------------------------------------------------------

    @property
    def layer(self) -> Layer:
        """由部门推导运行时层。

        **只有导演组是 L1**（艺术决策与调度中枢）；其余剧组职位都是 L2。
        单一来源，避免两处声明不一致。
        """
        return (Layer.DECISION if self.department is Department.DIRECTOR
                else Layer.COLLAB)

    @property
    def is_generative(self) -> bool:
        return self.execution.is_generative

    def manifest(self) -> AgentManifest:
        budget = (Budget.unlimited()
                  if not (self.max_budget_cny or self.max_budget_tokens)
                  else Budget(tokens=self.max_budget_tokens, cny=self.max_budget_cny))
        return AgentManifest(
            agent_id=self.agent_id,
            layer=self.layer,
            capabilities=self.capabilities,
            consumes=self.consumes + self.tools,
            max_budget=budget,
            max_reasoning_steps=self.max_reasoning_steps,
            trigger_stage=self.stage.value,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "crewTitle": self.crew_title,
            "crewTitleEn": self.crew_title_en,
            "department": self.department.value,
            "stage": self.stage.value,
            "layer": self.layer.name,
            "execution": self.execution.value,
            "needsGpu": self.execution.needs_gpu,
            "capabilities": list(self.capabilities),
            "produces": list(self.produces),
            "requires": list(self.requires),
            "consumes": list(self.consumes),
            "tools": list(self.tools),
            "taskProfile": self.task_profile,
            "promptTemplate": self.prompt_template,
            "maxReasoningSteps": self.max_reasoning_steps,
            "retryable": self.retryable,
            "realWorldDuty": self.real_world_duty,
            "notes": self.notes,
        }


# ==========================================================================
# 18 个剧组职位
# ==========================================================================

def _specs() -> list[AgentSpec]:
    """名册。按部门排列，**顺序即生产顺序**（审片组为横切）。"""
    return [
        # ================= 导演组（L1，1 人）=================
        AgentSpec(
            agent_id="director",
            crew_title="导演",
            crew_title_en="Director",
            department=Department.DIRECTOR,
            stage=Stage.SCRIPT,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("episode.direct",),
            produces=("episode.plan",),
            # 导演需要的是**真实产物**，不是"阶段完成标记"。
            # 早期版本写 requires=("script.final","asset.ready","board.ready") ——
            # "asset.ready" 这种标记无人产出，属于把编排概念误当数据依赖。
            requires=("script.final", "character.anchor_set", "scene.library",
                      "shot.list", "shot.image", "shot.video"),
            consumes=(
                "script.draft", "script.revise", "script.critique", "style.guard",
                "character.design", "scene.design", "continuity.check",
                "shot.list", "pacing.plan", "production.schedule",
                "shot.image", "shot.video", "shot.voice", "shot.music",
                "episode.assemble", "review.board", "compliance.check",
                "lighting.plan",
            ),
            task_profile="script.structured",
            prompt_template="crew.director.v1",
            max_reasoning_steps=8,
            real_world_duty="把握全片艺术方向，做最终创作决策，协调各部门",
            notes=("调度枢纽。持有全局预算，按阶段推进并把失败路由到返工链。"
                   "**不得直调 L3** —— 由 CapabilityRegistry.audit() 强制（R2 禁止跳层）。"),
        ),

        # ================= 编剧组（3 人）=================
        AgentSpec(
            agent_id="screenwriter",
            crew_title="编剧",
            crew_title_en="Screenwriter",
            department=Department.SCREENPLAY,
            stage=Stage.SCRIPT,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("script.draft", "script.revise"),
            produces=("script.draft", "script.final"),
            requires=("source.novel",),
            consumes=("script.critique", "style.guard"),
            task_profile="script.long_context",
            prompt_template="crew.screenwriter.v1",
            max_reasoning_steps=4,
            real_world_duty="创作与修订剧本；根据剧评意见返工",
            notes=("**同时声明 draft 与 revise 两个能力** —— 因为它既要出初稿，"
                   "也要按审查意见改稿。这两件事的提示词模板不同。"),
        ),
        AgentSpec(
            agent_id="dramaturge",
            crew_title="剧本顾问",
            crew_title_en="Dramaturge",
            department=Department.SCREENPLAY,
            stage=Stage.GATE,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("script.critique",),
            produces=("script.critique",),
            requires=("script.draft",),
            task_profile="script.structured",
            prompt_template="crew.dramaturge.v1",
            max_reasoning_steps=3,
            real_world_duty="审查剧本的戏剧结构、冲突强度、人物弧光",
            notes=("真实剧组里叫 dramaturge（戏剧顾问）。**与文学策划并行审查**，"
                   "二者互不阻塞 —— 这是「并行双维审查」的实现。"),
        ),
        AgentSpec(
            agent_id="story_editor",
            crew_title="文学策划",
            crew_title_en="Story Editor",
            department=Department.SCREENPLAY,
            stage=Stage.GATE,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("style.guard",),
            produces=("style.guard",),
            requires=("script.draft",),
            task_profile="script.structured",
            prompt_template="crew.story_editor.v1",
            max_reasoning_steps=3,
            real_world_duty="把关基调一致性、语言风格、命名规范",
            notes="与剧本顾问并行；两者意见由导演合并。",
        ),

        # ================= 美术组（3 人）=================
        AgentSpec(
            agent_id="production_designer",
            crew_title="美术指导",
            crew_title_en="Production Designer",
            department=Department.ART,
            stage=Stage.ASSET,
            execution=ExecutionKind.IMAGE_GEN,
            capabilities=("scene.design",),
            produces=("scene.library", "art.bible"),
            requires=("script.final",),
            task_profile="shot.image.final",
            prompt_template="crew.production_designer.v1",
            max_reasoning_steps=3,
            real_world_duty="总体视觉风格、场景设计、美术 bible",
            notes="产出场景库与视觉基调，供分镜与摄影复用。",
        ),
        AgentSpec(
            agent_id="character_designer",
            crew_title="角色设计师",
            crew_title_en="Character Designer",
            department=Department.ART,
            stage=Stage.ASSET,
            execution=ExecutionKind.IMAGE_GEN,
            capabilities=("character.design",),
            produces=("character.anchor_set", "character.sheet"),
            requires=("script.final",),
            consumes=("continuity.check",),
            task_profile="shot.image.identity_locked",
            prompt_template="crew.character_designer.v1",
            max_reasoning_steps=3,
            real_world_duty="角色形象设计、表情与服装设定表",
            notes=("产出锚点图集（正/侧/表情）。**一致性优先参考图** —— "
                   "本机无法训练角色 LoRA（Z-Image fp32 底模需 22.93 GB > 15.8 GB 内存）。"),
        ),
        AgentSpec(
            agent_id="continuity_supervisor",
            crew_title="场记",
            crew_title_en="Continuity Supervisor",
            department=Department.ART,
            stage=Stage.GATE,
            execution=ExecutionKind.VLM,
            capabilities=("continuity.check",),
            produces=("continuity.report", "continuity.sheet"),
            requires=("character.anchor_set", "shot.image"),
            task_profile="review.vlm_blind",
            prompt_template="crew.continuity_supervisor.v1",
            max_reasoning_steps=3,
            real_world_duty="记录并核对镜头间的连贯性（服装/道具/轴线/视线）",
            notes=("真实剧组里场记就是干这个的。**跨模型族盲读**的落点之一 —— "
                   "审核模型必须与生成模型不同族，否则等于自我签收。"),
        ),

        # ================= 摄影组（5 人）=================
        # ---- 预演师（2026-10-19 加，P2）----
        #
        # **为什么在摄影组**：它决定"机位在哪、人物站在哪、画面怎么构图" ——
        # 这是摄影组的活。放在导演组会破坏"导演有且仅有一个"这条守卫
        # （那是名册刻意的设计，不是疏漏）。
        #
        # **它不产出最终画面**：产出的是**三维空间的示意图**（白模），
        # 交给 Blender 渲染。所以 `execution=TEXT_LLM` —— 判断由 LLM 做，
        # 画面由 Blender 做。
        #
        # **`produces` 必须声明，不能省**（2026-10-19 实测）：
        # 黑板写入是**按名册的 `produces` 白名单**做的 ——
        # 省掉它，`run_stage` 会 ok=True 但**产物根本不进黑板**
        # （表现为 `previs.layouts` 取到 None，`_run_previs` 空转返回 ok=False）。
        #
        # 消费者是**摄影指导**：出图时把白模当 ControlNet 控制图
        # （`EpisodeRunner._control_frame_for` 就是这条路径）。
        # 这不是凑对子 —— 白模的用途本来就是"约束画面"。
        #
        # 刻意**不声明** `consumes`：`art.bible` / `episode.plan` 目前只在
        # 产物键层面存在、没有 agent 声明**能力**，写上去就是造假依赖。
        AgentSpec(
            agent_id="previs",
            crew_title="白模预演师",
            crew_title_en="Previs Artist",
            department=Department.CAMERA,
            stage=Stage.SCRIPT,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("previs.layouts",),
            produces=("previs.layouts",),
            requires=("shot.list", "script.final"),
        ),
        AgentSpec(
            agent_id="storyboard_artist",
            crew_title="分镜师",
            crew_title_en="Storyboard Artist",
            department=Department.CAMERA,
            stage=Stage.STORYBOARD,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("shot.list",),
            produces=("shot.list", "shot.breakdown"),
            requires=("script.final", "character.anchor_set", "scene.library"),
            consumes=("pacing.plan",),
            task_profile="script.structured",
            prompt_template="crew.storyboard_artist.v1",
            max_reasoning_steps=4,
            real_world_duty="把剧本拆解为镜头表（景别/机位/运动）",
            notes=("**每镜时长硬上限 10 秒** —— H3 实测 12 秒必 OOM，"
                   "且降分辨率无效（瓶颈是注意力序列长度）。"),
        ),
        AgentSpec(
            agent_id="cinematographer",
            crew_title="摄影指导",
            crew_title_en="Cinematographer (DoP)",
            department=Department.CAMERA,
            stage=Stage.RENDER,
            execution=ExecutionKind.IMAGE_GEN,
            capabilities=("shot.image",),
            produces=("shot.image",),
            # **`requires` 里必须有 `previs.layouts`**（2026-10-19 加）：
            # 出图时把白模首帧当 ControlNet 控制图
            # （`EpisodeRunner._control_frame_for` 优先取 `previs_first`）。
            #
            # 注意：孤儿产出判定看的是 **`requires`** 不是 `consumes` ——
            # `consumes` 的语义是「允许调用的下游能力」，与"需要这个输入"无关。
            requires=("shot.list", "lighting.plan", "previs.layouts"),
            # ⚠️ 技术质检**不是能力而是工具**（确定性规则判定，无需模型决策）。
            #    早期版本把它写成 consumes=("review.tech") —— 那会让路由找不到实现者。
            tools=("qc.tech_check",),
            task_profile="shot.image.final",
            prompt_template="crew.cinematographer.v1",
            max_reasoning_steps=2,
            real_world_duty="构图、景别、镜头运动、画面质感",
            notes="逐镜出图。本机实测 30~32 秒/张（Z-Image 4bit）。",
        ),
        AgentSpec(
            agent_id="gaffer",
            crew_title="灯光师",
            crew_title_en="Gaffer",
            department=Department.CAMERA,
            stage=Stage.STORYBOARD,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("lighting.plan",),
            produces=("lighting.plan",),
            requires=("scene.library",),
            task_profile="script.structured",
            prompt_template="crew.gaffer.v1",
            max_reasoning_steps=2,
            real_world_duty="设计每场的光线方案（主光/辅光/色温/氛围）",
            notes="把光照从「摄影指导顺手写的提示词」提升为**独立可审查的产物**。",
        ),
        AgentSpec(
            agent_id="camera_operator",
            crew_title="摄影师",
            crew_title_en="Camera Operator",
            department=Department.CAMERA,
            stage=Stage.RENDER,
            execution=ExecutionKind.VIDEO_GEN,
            capabilities=("shot.video",),
            produces=("shot.video",),
            requires=("shot.image",),
            tools=("qc.tech_check",),
            task_profile="shot.video.identity_locked",
            prompt_template="crew.camera_operator.v1",
            max_reasoning_steps=2,
            real_world_duty="实际拍摄（把静帧变成运动镜头）",
            notes=("图生视频。本机 H3 实测 442~518 秒/5 秒片段 —— "
                   "**渲染时间是真正的瓶颈**。"),
        ),

        # ================= 录音组（2 人）=================
        AgentSpec(
            agent_id="dialogue_editor",
            crew_title="对白剪辑",
            crew_title_en="Dialogue Editor",
            department=Department.SOUND,
            stage=Stage.RENDER,
            execution=ExecutionKind.AUDIO_GEN,
            capabilities=("shot.voice",),
            produces=("shot.voice",),
            requires=("script.final",),
            task_profile="audio.dialogue",
            prompt_template="crew.dialogue_editor.v1",
            max_reasoning_steps=2,
            real_world_duty="对白录制与剪辑、口型对齐",
            notes=("**优先用 H3 原生音轨** —— 音画同出，零额外成本，"
                   "且省一次模型加载（本机同时只能加载一个模型）。"),
        ),
        AgentSpec(
            agent_id="composer",
            crew_title="作曲",
            crew_title_en="Composer",
            department=Department.SOUND,
            stage=Stage.RENDER,
            execution=ExecutionKind.AUDIO_GEN,
            capabilities=("shot.music",),
            produces=("shot.music",),
            requires=("shot.list",),
            task_profile="audio.dialogue",
            prompt_template="crew.composer.v1",
            max_reasoning_steps=2,
            real_world_duty="配乐与情绪铺底",
            notes="背景音乐。需与对白做音量闪避（ducking）。",
        ),

        # ================= 剪辑组（2 人）=================
        AgentSpec(
            agent_id="pacing_editor",
            crew_title="剪辑师",
            crew_title_en="Film Editor",
            department=Department.EDITORIAL,
            stage=Stage.GATE,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("pacing.plan",),
            produces=("pacing.plan", "pacing.report"),
            requires=("shot.list",),
            task_profile="script.structured",
            prompt_template="crew.pacing_editor.v1",
            max_reasoning_steps=3,
            real_world_duty="控制节奏、镜头长短、转场方式",
            notes=("在分镜阶段介入（定节奏计划），而非等素材出来才剪 —— "
                   "真实剧组里剪辑师也从筹备期就参与。"),
        ),
        AgentSpec(
            agent_id="assembly_editor",
            crew_title="合成剪辑",
            crew_title_en="Online Editor",
            department=Department.EDITORIAL,
            stage=Stage.ASSEMBLE,
            execution=ExecutionKind.ASSEMBLY,
            capabilities=("episode.assemble",),
            produces=("episode.master", "delivery.evidence"),
            requires=("shot.video", "shot.voice", "shot.music"),
            # 纯确定性工具 —— 不调模型
            tools=("ffmpeg.assemble", "gate.assert"),
            task_profile="",
            max_reasoning_steps=2,
            real_world_duty="套底、调色、混音、输出母版",
            notes=("**不调模型** —— 全部是确定性工具。这也是为什么它只需 2 步推理预算。"),
        ),

        # ================= 制片组（2 人）=================
        AgentSpec(
            agent_id="line_producer",
            crew_title="制片主任",
            crew_title_en="Line Producer",
            department=Department.PRODUCTION,
            stage=Stage.STORYBOARD,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("production.schedule",),
            produces=("production.schedule",),
            requires=("shot.list",),
            task_profile="script.structured",
            prompt_template="crew.line_producer.v1",
            max_reasoning_steps=3,
            real_world_duty="排期、预算、资源调配",
            notes=("**本机约束的落点**：8 GB 显存同时只能加载一个模型，"
                   "故必须**按模型分批调度**（把所有用同一底模的镜头排在一起），"
                   "而非按镜头顺序逐个跑。"),
        ),
        AgentSpec(
            agent_id="compliance_officer",
            crew_title="版权与合规",
            crew_title_en="Clearance & Compliance",
            department=Department.PRODUCTION,
            stage=Stage.ASSEMBLE,
            execution=ExecutionKind.ASSEMBLY,
            capabilities=("compliance.check",),
            produces=("compliance.report",),
            requires=("episode.master",),
            tools=("gate.assert",),
            max_reasoning_steps=2,
            real_world_duty="版权清理、内容审查、署名规范",
            notes=("**不调模型** —— 核对归属标注是否已注入产品界面、"
                   "授权是否覆盖当前部署地区。与 governance.LicenseGate 分工："
                   "门禁在**路由期**拒绝不可用模型；本职位在**交付前**核对标注。"),
        ),

        # ================= 审片组（1 人）=================
        AgentSpec(
            agent_id="review_board",
            crew_title="审片",
            crew_title_en="Review Board",
            department=Department.REVIEW,
            stage=Stage.GATE,
            execution=ExecutionKind.VLM,
            capabilities=("review.board",),
            produces=("review.verdict",),
            requires=("shot.image", "shot.video"),
            task_profile="review.vlm_blind",
            prompt_template="crew.review_board.v1",
            max_reasoning_steps=3,
            real_world_duty="成片审查：艺术质量与观众感受",
            notes=("QC-5 五维品控：完整性 25 / 一致性 40 / 品质感 25 / "
                   "可执行性 / 合规性。**跨模型族盲读**，且不得由生成方自任。"),
        ),
    ]


#: 18 个剧组职位（模块级常量，便于直接 import）。
DEFAULT_ROSTER: tuple[AgentSpec, ...] = tuple(_specs())


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

    for s in roster:
        for r in s.requires:
            if r not in produced and r not in external_inputs:
                problems.append(
                    f"「{s.crew_title}」需要 {r!r}，但无任何职位交付它")

    #: 终态交付物与**档案类产物**无需下游消费者。
    #: 前者是项目产出，后者是留档（美术 bible、角色设定表、场记单、交付证据链）——
    #: 真实剧组里这些本来就是给人查的，不是流水线的中间件。
    terminal_outputs = {
        "episode.master",       # 成片
        "delivery.evidence",    # 交付证据链
        "compliance.report",    # 合规报告（留档）
        "continuity.report",    # 场记报告（留档）
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
        if p not in required and p not in terminal_outputs:
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
        "pacing.plan": ("shot.list",),
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
