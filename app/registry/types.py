"""小剧组 (Little Crew) — 注册表类型定义。

设计目标：把系统中**四处散落的概念**收敛为**唯一数据源**。
下面记的是**重构前**的散落位置（前端那几处随 Streamlit 前端一并删除，
现为 `web/` 导演台，它从 `/api/registry` 读，不再自带清单）：
  - 职业 role      → 曾散在 `app/core/org.py` / `frontend/_org.py` / `_console.py` / 各页面文件名
  - 部门 dept      → 曾隐式写在 `ROLE_ORG` 元组里 + 前端 `_org.py`
  - 阶段 stage     → 曾硬编码在 `main_workflow._STAGE_ORDER` / `_console._ALL_PHASES` / `_segment_select.FULL_ROLES`
  - 确认点 checkpoint → 曾散在 `REVIEW_*_POINT` 常量 + 各 review 节点 + `_console._CHECKPOINT_OPTIONS`

三层结构（关键设计，避免"1 职业 = 1 节点"的错误假设）：
    Role （职业）  —— 面向用户，15 个
      └─ Panel（面板）—— 画布上"看什么"，一个职业可有多个视图
           └─ Node（节点）—— 执行单元，可被多个 Role / Panel 复用

本模块只放**类型**；数据在 `app/registry/roles.py`；派生查询在 `app/registry/api.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ------------------------------------------------------------------
# 产物（端口）—— 节点之间流动的数据
# ------------------------------------------------------------------

#: 产物形态，决定画布上端口的颜色/图标与兼容性判断
ArtifactKind = Literal[
    "text",      # 一句话/自由文本
    "json",      # 结构化对象
    "list",      # 结构化列表
    "image",     # 图片（路径或 URL）
    "video",     # 视频
    "audio",     # 音频
    "score",     # 0-1 或 0-100 评分
    "model",     # 模型权重文件（训练产物等）
    "timeline",  # 剪辑时间线
]


@dataclass(frozen=True)
class Artifact:
    """一个可流通的产物（= 画布上的端口类型）。

    Attributes:
        key:       注册表内唯一标识（v2 命名，可与 `CrewState` 的字段名不同）
        label:     中文展示名
        kind:      形态
        state_key: 当前 `CrewState` 里对应的字段名（兼容映射；v2 新增产物为 None）
    """

    key: str
    label: str
    kind: ArtifactKind
    state_key: str | None = None


# ------------------------------------------------------------------
# 配置字段（属性面板）—— 每个节点可暴露的可调参数
# ------------------------------------------------------------------

ConfigScope = Literal["global", "role", "node"]
ConfigType = Literal["select", "text", "password", "number", "toggle", "slider"]


@dataclass(frozen=True)
class ConfigField:
    """一个可配置字段，对应 `runtime_settings` 里的键（可为 per-role 覆盖）。

    Attributes:
        key:      设置键名，如 "image_provider_photographer"（per-role）或 "max_images"
        label:    中文展示名
        type:     控件类型
        scope:    global=全局一项 / role=按职业覆盖 / node=仅该节点
        options:  select 的可选值
        default:  默认值
        secret:   True 表示不回显（API Key 类）
        help:     说明文字
        options_from:
            该 select 的选项**由注册表派生**，而不是手写。
            取值为能力名（`llm` / `image_gen` / `video_gen` / `tts`），
            序列化时替换成"该能力当前已登记的提供者"。
            为什么要这样：手写清单**必然漂移**——实测 `video_provider` 的选项
            只有 `('(按模式)', 'local', 'minimax', 'ffmpeg')`，而适配器里明明有
            `dashscope_video` 与 `seedance`（都已接好），用户**根本选不到**。
            （同类问题在 `allowed_settings()` 上修过一次：那次是"能填却被白名单丢弃"。）
    """

    key: str
    label: str
    type: ConfigType = "text"
    scope: ConfigScope = "global"
    options: tuple[str, ...] = ()
    default: object = None
    secret: bool = False
    help: str = ""
    options_from: str = ""
    #: 该 select 的选项是**该能力下所有提供者的模型名并集**（取值为能力名）。
    #:
    #: 为什么要有它：`video_model` / `image_model_*` 原本是 `type="text"`、
    #: `options=[]` —— 界面上就是个**空框**，用户不知道能填什么
    #: （用户报过："视频模型没有api填入"、"图像生成无法快捷选择"）。
    #: 而注册表里早就登记了各提供者的模型清单（`known_models`）。
    #:
    #: 生成的选项带**来源前缀**（`MiniMax · MiniMax-H3`）：不同提供者的模型名
    #: 会混在同一个下拉里，不带来源就分不清（`image-01` 是谁家的？）。
    #:
    #: ⚠️ 这份清单是**人工核对**的，厂商上新模型时**不会自动出现**。
    #:    界面上按**当前选中的提供者**过滤后展示（`厂商 · 模型`）。
    #:    厂商新模型由用户在输入框里**手动填写** —— 曾有一条"自动问厂商
    #:    `/models`"的路径（`GET /api/providers/models`），已按用户要求
    #:    整体删除（见 commit `b767b55` 之后的删除提交）。
    options_from_models: str = ""


# ------------------------------------------------------------------
# 能力分类 —— 决定"用户能选哪些模型"
# ------------------------------------------------------------------

ProviderCapability = Literal[
    "llm",         # 文本大模型（API 或本地）
    "image_gen",   # 文生图
    "image_edit",  # 图像编辑 / 崩坏修复
    "video_gen",   # 图生视频
    "tts",         # 语音合成
    "music",       # BGM / 音效
    # ⚠️ 名字里的 `2d` 是历史遗留：实际的预演是 **3D**（前端 Three.js 体块场景，
    # 见 `web/src/previs/`）。这个能力目前**只是注册表声明，没有调用点**；
    # 改名会牵动前端类型与测试，暂未动。
    "render_3d",
]


# ------------------------------------------------------------------
# 节点（执行单元）
# ------------------------------------------------------------------

NodeKind = Literal["agent", "service"]
"""agent=继承 BaseAgent 的智能体；service=服务型（如视频合成、发行）。"""

StageStatus = Literal["pending", "running", "done", "skipped", "stale", "failed"]
"""阶段状态。注意 `skipped` 与 `done` 必须区分（现状把二者混淆，见 01-usage §6.3）。"""


@dataclass(frozen=True)
class Node:
    """执行单元 —— 工作流里的一个环节。

    Attributes:
        key:         节点标识（= 现状 workflow 节点名，如 "photographer"）
        label:       中文展示名
        dept:        所属部门 key
        kind:        agent / service
        produces:    产出的产物 key 列表 → 决定"往下能连谁"
        consumes:    依赖的产物 key 列表 → 决定"能不能连"
        capability:  调用的外部能力（决定属性面板显示哪些 provider 配置）
        can_skip:    是否可被 start_stage/end_stage 跳过
        iter_group:  迭代组名（同组内可回环，由运行时限轮次）；None = 不参与迭代
        config:      该节点暴露的配置字段
        legacy_keys: 旧命名别名（数据已清空，此处仅留作迁移期参考）
        agent_class: 实现该节点的类名（便于对照代码）
        workflow_node: 该节点在工作流图里的节点名（用于事件对齐；空 = 尚未进图）
    """

    key: str
    label: str
    dept: str
    kind: NodeKind = "agent"
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    capability: tuple[ProviderCapability, ...] = ()
    can_skip: bool = True
    iter_group: str | None = None
    config: tuple[ConfigField, ...] = ()
    legacy_keys: tuple[str, ...] = ()
    agent_class: str = ""

    #: 该节点在**工作流图**里的节点名（`build_graph()` 中 `add_node` 用的键）。
    #:
    #: 为什么需要它：系统里存在三套命名，容易混淆——
    #:   1. 注册表 Role/Node key（v2 新表）：`showrunner` / `script_editor`
    #:   2. 工作流图节点名（v1 实现）：`story_planner` / `critics`
    #:   3. 阶段名 / 配置键（v1 词表）：`writer` / `critics` / `director` / `photographer`
    #: 三者对多数节点相同，但对少数节点不同（见各条目的注释）。
    #: 事件广播与状态查询必须按**工作流节点名**对齐，否则画布点不亮。
    #: 空字符串表示"该节点尚未进入工作流图"（v2 新角色）。
    workflow_node: str = ""

    #: per-role 配置键的命名空间 —— **运行时实际读取的那个 role**。
    #:
    #: 为什么不直接用节点 key：Agent 运行时按"角色名"读配置
    #: （LLM 读 `llm_provider_{role}`、图像读 `image_provider_{role}`），
    #: 而角色名与注册表的 v2 节点名未必相同（如节点 technical_director
    #: 运行时传的 role 是 "director"）。若按节点 key 生成配置键，
    #: **界面写进去的键运行时读不到**，用户以为改了配置、实际没生效。
    #: 空字符串表示与 `key` 相同（多数情况）。
    config_namespace: str = ""

    @property
    def role_namespace(self) -> str:
        """实际用于配置键的命名空间（未显式指定时即 `key`）。"""
        return self.config_namespace or self.key


# ------------------------------------------------------------------
# 面板（画布上的"视图"）
# ------------------------------------------------------------------

PanelKind = Literal[
    "main",        # 节点主体：状态 + 产物预览 + 操作
    "report",      # 评估/报告类视图
    "gallery",     # 图集/资产浏览
    "timeline",    # 时间线编辑器
    "training",    # 训练任务视图
    "console",     # 参数/控制台
    "publish",     # 发布/分发
]


@dataclass(frozen=True)
class Panel:
    """一个职业在画布上暴露的视图。

    一个 Role 可以有多个 Panel：主视图是画布上的节点卡片，
    其余是节点内可展开的子视图（如质检的"逐镜详情"、"重拍选择"）。

    Attributes:
        key:   面板标识（建议 f"{node_key}:{kind}"）
        label: 中文展示名
        kind:  面板类型
        node:  绑定到哪个节点（执行该面板的操作）
    """

    key: str
    label: str
    kind: PanelKind
    node: str


# ------------------------------------------------------------------
# 职业（面向用户）
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Role:
    """一个职业 —— 面向用户的概念，可含一个或多个面板。

    Attributes:
        key:      职业标识（系统内唯一、稳定；供设置页 per-role 配置键使用）
        label:    中文职位名，如「摄影师」
        en:       英文名（展示/文档用），如 "photographer"
        dept:     所属部门 key
        duty:     一句话职责
        real_job: 对应的真人职业（展示用）
        node:     主节点 key（画布上这个职业的默认执行节点）
        panels:   该职业暴露的面板（首个应为主面板）
        legacy_keys: 旧命名（数据已清空，仅留档）
    """

    key: str
    label: str
    en: str
    dept: str
    duty: str
    real_job: str
    node: str
    panels: tuple[Panel, ...] = ()
    legacy_keys: tuple[str, ...] = ()

    @property
    def main_panel(self) -> Panel | None:
        """主面板（画布节点本体）。"""
        return self.panels[0] if self.panels else None


# ------------------------------------------------------------------
# 部门
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Department:
    """一个部门 —— 职业的分组。

    Attributes:
        key:   部门标识
        label: 中文名
        order: 展示顺序（0 起）
        desc:  一句话说明
    """

    key: str
    label: str
    order: int
    desc: str = ""


# ------------------------------------------------------------------
# 人工确认点
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Checkpoint:
    """一个人工确认点。

    Attributes:
        key:   检查点标识（用于 `enabled_checkpoints` 白名单）
        label: 中文名
        node:  绑定的节点（该节点执行后暂停）
        after: 属于哪个节点的产出被审核
    """

    key: str
    label: str
    node: str
    after: str = ""
