"""剧组职位的**声明结构**（2026-11-18，B1 全职业插件化时从 `agent/roster.py` 迁入）。

这里只放**结构**（枚举 + `AgentSpec` 数据类），不放任何具体职位：

  · `Department` / `Stage` / `ExecutionKind` / `STAGE_ORDER`：组织、阶段、执行方式
  · `AgentSpec`：一个剧组职位的完整声明（frozen dataclass）

具体职位是**插件声明**：
  · `plugins/core_crew.py`：19 个核心职位（默认单集名册，`default_roster=True`）
  · `plugins/series_showrunner.py`：系列总导演（插件专属，不进默认名册）

为什么拆出来：`plugins/*` 可以 import 本模块（核心公开模块），而**不能**
import `agent/roster.py` —— roster 会触发插件注册，插件再 import roster 就成环。
拆分后依赖是单向的：`roster -> plugin -> plugins.core_crew -> agent.crew_spec`。

`agent/roster.py` 仍 re-export 本模块公开名字，向后兼容既有 17 处导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from little_crew.agent.protocol import (
    AgentManifest,
    Budget,
    Layer,
)

__all__ = [
    "Department",
    "Stage",
    "ExecutionKind",
    "STAGE_ORDER",
    "AgentSpec",
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
    #: 是否必需职业：必需职业不可从 profile 省略（插件卸载时也受保护）
    mandatory: bool = False
    #: 显式顺序约束：本职业必须排在这些职业之后（与产物依赖合并做拓扑）
    after: tuple[str, ...] = ()

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
