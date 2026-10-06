"""小剧组 (Little Crew) — 注册表数据（唯一数据源）。

本文件是**职业 / 部门 / 节点 / 端口 / 确认点 / 配置字段**的唯一权威定义。
前端、工作流、设置页、文档都应从这里派生，禁止在别处再写一份。

命名约定（v2）：
  - Role.key   = 稳定标识，供 per-role 配置键（如 `image_provider_photographer`）使用
  - Node.key   = 工作流节点名，保持与现有 `CrewState` / LangGraph 节点一致
  - Role.en    = 英文展示名（≠ key 时以 en 为准展示，如 策划总监 en=showrunner, key=showrunner）

数据依据：用户 2026-10-02 给出的新职业表（7 部门 / 16 职业）+ `docs/recon/05-roles-v2.md` 的缺口分析。
"""

from __future__ import annotations

from app.registry.types import (
    Artifact,
    Checkpoint,
    ConfigField,
    Department,
    Node,
    Panel,
    Role,
)

# ==================================================================
# 一、产物（端口）
# ==================================================================
# state_key 指向现有 `CrewState` 字段；None 表示 v2 新增（状态需扩展）。

ARTIFACTS: tuple[Artifact, ...] = (
    # ---- 输入 ----
    Artifact("user_input", "一句话创意", "text", "user_input"),
    Artifact("art_brief", "美术设定包（用户填写）", "text", "art_brief"),
    # ---- 策划 ----
    Artifact("project_brief", "项目定位/统筹方案", "json"),
    Artifact("story_outline", "故事大纲", "json", "story_outline"),
    Artifact("script", "分场剧本", "json", "script"),
    Artifact("script_review", "剧本评估报告", "json", "script_review"),
    Artifact("critic_report", "剧评汇总（含历史）", "json", "critic_report"),
    Artifact("adaptation_plan", "IP 改编方案", "json"),
    Artifact("storyboard", "分镜表（机位/景别/运动/时长/走位）", "list", "storyboard"),
    # ---- 资产 ----
    Artifact("art_brief_plan", "美术设定包解析结果", "json", "art_brief_plan"),
    Artifact("character_profiles", "角色外貌锚定（角色圣经）", "json", "character_profiles"),
    Artifact("character_reference_images", "角色三视图", "json", "character_reference_images"),
    Artifact("character_lora", "角色 LoRA 权重", "model"),
    Artifact("scene_design_profiles", "场景锚定描述", "json", "scene_design_profiles"),
    Artifact("scene_reference_images", "场景概念图", "json", "scene_reference_images"),
    Artifact("scene_whitebox", "场景白模（2D）", "image"),
    # ---- 生图 ----
    Artifact("control_inputs", "结构化控制图（ControlNet）", "json"),
    Artifact("prompts", "生图提示词", "list", "prompts"),
    Artifact("images", "分镜画面", "list", "images"),
    Artifact("quality_report", "质检报告（质量+一致性）", "json", "quality_report"),
    Artifact("fixed_images", "修复后的画面", "list"),
    Artifact("consistent_takes", "抽卡筛优后的画面", "list"),
    # ---- 白模预演 ----
    Artifact("previs", "2D 白模预演（走位/机位）", "json"),
    # ---- 动态 ----
    Artifact("continuity_report", "连贯性报告（角色/场景/道具）", "list", "continuity_report"),
    Artifact("voice_track", "配音轨（含每句时长）", "audio"),
    Artifact("bgm_track", "BGM / 音效轨", "audio"),
    # ---- 后期 ----
    Artifact("rhythm_plan", "节奏方案（每镜时长/快慢）", "list", "rhythm_plan"),
    Artifact("edit_plan", "组接方案", "json"),
    Artifact("timeline", "剪辑时间线", "timeline"),
    Artifact("final_video", "成片", "video"),
    Artifact("publish_results", "各平台发布结果", "json", "publish_results"),
    # ---- 汇总 ----
    Artifact("result", "最终聚合结果", "json", "result"),
    # ---- 内部审查（非职业）----
    Artifact("consistency_score", "整体一致性评分", "score", "consistency_score"),
    Artifact(
        "character_consistency_report", "角色跨镜头一致性报告", "json",
        "character_consistency_report",
    ),
)

ARTIFACT_BY_KEY: dict[str, Artifact] = {a.key: a for a in ARTIFACTS}


# ==================================================================
# 二、部门
# ==================================================================

DEPARTMENTS: tuple[Department, ...] = (
    Department("infra", "基建", 0, "搭建与维护主工作流、模型与训练策略"),
    Department("planning", "策划", 1, "把点子变成可执行的剧本与分镜"),
    Department("assets", "资产", 2, "确定角色与场景长什么样"),
    Department("imaging", "生图", 3, "写出能用的提示词并批量出图、筛优"),
    Department("previs", "白膜", 4, "用白模预演走位与机位"),
    Department("motion", "动态", 5, "贯穿一致性与声音"),
    Department("post", "后期", 6, "节奏、剪辑、分发"),
)

DEPT_BY_KEY: dict[str, Department] = {d.key: d for d in DEPARTMENTS}


# ==================================================================
# 三、可复用的配置字段片段
# ==================================================================

#: 模式字段的键名（与 `providers.MODE_KEY_BY_CAPABILITY` 对齐）
MODEL_MODE_FIELDS: dict[str, ConfigField] = {
    "llm": ConfigField(
        "llm_provider_mode", "文字模型：云端 / 本地", "select", "global",
        ("unset", "cloud", "local"), default="unset",
        help="必选。云端默认 DeepSeek；本地默认 Ollama。未选时运行会明确报错，不会猜",
    ),
    "image_gen": ConfigField(
        "image_provider_mode", "图片生成：云端 / 本地", "select", "global",
        ("unset", "cloud", "local"), default="unset",
        help="必选。云端默认阿里百炼；本地用你自己的显卡出图（不产生 API 费用，8GB 显存约 32 秒/张）",
    ),
    "video_gen": ConfigField(
        "video_provider_mode", "视频生成：云端 / 本地", "select", "global",
        ("unset", "cloud", "local"), default="unset",
        help="必选。云端默认 MiniMax（约 $0.01/秒）；本地 8GB 时长上限 10 秒",
    ),
    "lora_train": ConfigField(
        "lora_train_provider_mode", "LoRA 训练：云端 / 本地", "select", "global",
        ("unset", "cloud", "local"), default="unset",
        help="必选。本地用你自己的显卡训练（不产生 API 费用，较慢）；云端按训练步数计费",
    ),
    "tts": ConfigField(
        "tts_provider_mode", "配音：云端 / 本地", "select", "global",
        ("unset", "cloud", "local"), default="unset",
        help="必选。云端默认百炼 CosyVoice；本地默认 Edge TTS（免费）",
    ),
}


def _provider_options(capability: str) -> tuple[str, ...]:
    """某能力的提供者下拉选项，**从 provider 注册表派生**。

    ⚠️ 第一项是**占位符** `"(自动)"`，语义是"**不覆盖、跟随模式**"
    （视频的 `video_provider` 用的写法是 `"(按模式)"`，含义相同）。
    界面上它显示成「**跟随模式**」（映射在前端 `optionLabel` 里）——
    用户看不懂"自动/按模式"的区别（其实没有区别，只是历史写法）。

    这两个字符串是**存储值**，后端 `providers.resolve_provider` 靠它们判断
    "用户没设覆盖"；`providers.PLACEHOLDER_PROVIDERS` 是唯一定义处。
    **不要**为了好看把存储值也改掉。

    ## 为什么要派生而不是硬编码（这里修过一次真缺陷）

    原先两个函数各自写死一份列表：

        _llm_fields   ("(自动)", "deepseek", "zhipu", "openai", … 13 个)
        _image_fields ("(自动)", "local", "dashscope", "replicate", "minimax")

    于是**在 `providers.py` 里新增提供者，设置页里却选不到**——
    键在白名单里（能存）、有实现（能跑），就是没有下拉项。
    这正是本项目已经吃过多次亏的"两份手写清单漂移"。

    现在从 `PROVIDERS` 派生：只要在注册表里声明了，下拉就自动有。
    按 `capability` 过滤，避免把图像提供者混进文字模型下拉
    （`local` / `minimax` / `dashscope` 都是跨能力重名的 key）。

    `available=False` 的（仅登记未接入）**不列入**——列出来会让用户
    选到一个必然报错的选项。

    **协议不是 `openai` 的也不列入**（`ProviderSpec.api_style`）：本项目
    LLM 客户端是 langchain `ChatOpenAI`，固定请求 `{base_url}/chat/completions`。
    协议对不上的（Responses / Anthropic Messages / 无法验证）选中后必然失败，
    而界面上一句 note 挡不住用户去点。

    这是用户"下线或不兼容的就不要了"的落地：**能选 = 能用**。
    不在能选列表里的条目仍留在注册表里备查（附原因），只是不给选。

    ⚠️ 用**惰性 import**：`app.registry` 是被大量模块早期导入的包，
    在模块顶层引入 `app.services.providers` 会把服务层拖进 registry 的
    导入链（providers 自身又 import video_adapters / runtime_settings），
    容易出现难查的循环导入。放在函数里最稳。
    """
    from app.services.providers import PROVIDERS

    keys = ["(自动)"]
    for p in PROVIDERS:
        if p.capability != capability or not p.available:
            continue
        # 只有 openai 风格能被现有客户端直接调用；其余（含 unverified）不列
        if getattr(p, "api_style", "openai") != "openai":
            continue
        if p.key not in keys:
            keys.append(p.key)
    return tuple(keys)


def _llm_fields(role_key: str) -> tuple[ConfigField, ...]:
    """某职业的 LLM 配置（API 或本地，二者等价可选）。

    键名以该节点的**配置命名空间**开头（见 `Node.config_namespace`）：
    正好一个能力时用能力名（`llm`），否则用节点 key。
    """
    return (
        ConfigField(f"llm_provider_{role_key}", "文字模型来源", "select", "role",
                    _provider_options("llm"),
                    help="API、本地（ollama/vllm）与聚合网关（openrouter 等）等价可选"),
        ConfigField(f"llm_model_{role_key}", "模型", "select", "role",
                    options_from_models="llm",
                    help="留空则用该提供者的默认模型；聚合网关的模型名形如 "
                         "anthropic/claude-…；厂商新模型可直接输入"),
        ConfigField(f"llm_api_key_{role_key}", "API Key", "password", "role", secret=True,
                    help="本地 provider（ollama/vllm）无需填写"),
    )


def _image_fields(role_key: str) -> tuple[ConfigField, ...]:
    """某职业的图像生成配置（本地/云端等价）。键名规则同 `_llm_fields`。"""
    return (
        ConfigField(f"image_provider_{role_key}", "图片模型来源", "select", "role",
                    _provider_options("image_gen"),
                    help="带「本地」前缀的用你自己的显卡推理，不产生 API 费用；其余为云端"),
        ConfigField(f"image_model_{role_key}", "模型", "select", "role",
                    options_from_models="image_gen",
                    help="留空则用该提供者的默认模型；清单只列**当前提供者**已登记的模型，厂商新模型可直接输入"),
        ConfigField(f"image_api_key_{role_key}", "API Key", "password", "role", secret=True),
    )


_IMAGE_QUALITY = ConfigField(
    "image_quality", "图片质量", "select", "global",
    ("standard", "high", "ultra"), "standard",
)
_MAX_IMAGES = ConfigField(
    "max_images", "镜头/图片数上限", "number", "global", default=8,
    help="全片最多生成多少张图（控制成本）",
)
_COST_BUDGET = ConfigField(
    "cost_budget_per_run", "单次创作费用上限", "number", "global", default=0,
    help="0=不限制。按量计费的 provider 会在此预算内中止（v2 新增）",
)


# ==================================================================
# 三·补、命名空间映射（必须定义在 ROLE_NODE_SPECS / NODES 之前）
# ==================================================================
# 系统里有**三套命名**，只对多数节点相同：
#   ① 注册表 key（v2 新表）  : showrunner / script_editor / technical_director
#   ② 工作流图节点名（v1 实现）: story_planner / critics / prompt_compiler
#   ③ 运行时 role（读配置用） : writer / critics / director / photographer
# 事件广播按 ①，配置键按 ③ —— 搞混会出现"界面改了、运行不生效"。

#: 注册表节点 key → 工作流图节点名（① → ②）
WORKFLOW_NODE_BY_KEY: dict[str, str] = {
    # ---- 与 ② 同名（多数） ----
    "screenwriter": "screenwriter",
    "character_designer": "character_designer",
    "scene_designer": "scene_designer",
    "storyboarder": "storyboarder",
    "quality_assessor": "quality_assessor",
    "rhythm_director": "rhythm_director",
    "continuity_checker": "continuity_checker",
    # ---- 改了名的（关键） ----
    "showrunner": "story_planner",            # 阶段名 writer
    "script_editor": "critics",               # 阶段名 critics
    "technical_director": "prompt_compiler",  # 阶段名 director
    "photographer": "image_generator",        # 阶段名 photographer
    "editor": "aggregator",                   # 阶段名 aggregator
    # ---- v2 新角色：尚未进入工作流图（空 = 无对应节点） ----
    "workflow_architect": "",
    "previs_artist": "",
    "voice_director": "",
    "distributor": "",
}

#: 注册表节点 key → **配置命名空间**（① → ③，即运行时实际读取的 role）
CONFIG_NAMESPACE_BY_KEY: dict[str, str] = {
    "showrunner": "writer",              # planner.py 调用 role="writer"
    "script_editor": "critics",           # critics.py 调用 role="critics"
    "technical_director": "director",     # prompt_refiner.py 调用 role="director"
    "continuity_checker": "continuity",   # continuity_checker.py 调用 role="continuity"
}


def _workflow_node(key: str) -> str:
    """该注册表节点对应的工作流节点名；未在表中则假定同名。"""
    return WORKFLOW_NODE_BY_KEY.get(key, key)


def _config_namespace(key: str) -> str:
    """该节点用于配置键的命名空间（默认与节点同名）。"""
    return CONFIG_NAMESPACE_BY_KEY.get(key, key)


# ==================================================================
# 四、职业 + 其面板 + 其节点
# ==================================================================
# 说明：Node 需要显式声明 produces/consumes（画布端口与连线校验的依据）。
# 一个 Role 对应一个主节点；节点也可以不对应任何职业（见 §5 系统节点）。

ROLES: tuple[Role, ...] = (
    # ---------------- 基建 ----------------
    Role(
        key="workflow_architect", label="工作流架构师", en="workflow_architect",
        dept="infra", real_job="工作流架构师",
        duty="搭建、迭代、维护主工作流（LangGraph 节点图），定义节点接口与数据格式",
        node="workflow_architect",
        panels=(
            Panel("workflow_architect:main", "工作流", "main", "workflow_architect"),
            Panel("workflow_architect:console", "节点接口 / 数据格式", "console", "workflow_architect"),
        ),
    ),
    Role(
        key="technical_director", label="技术指导", en="technical_director",
        dept="infra", real_job="技术指导",
        duty="模型选型、LoRA 训练调优、视频生成参数优化",
        node="technical_director",
        panels=(
            Panel("technical_director:main", "模型与参数", "main", "technical_director"),
            Panel("technical_director:training", "LoRA 训练", "training", "technical_director"),
        ),
        legacy_keys=("director",),
    ),
    # ---------------- 策划 ----------------
    Role(
        key="showrunner", label="策划总监", en="showrunner",
        dept="planning", real_job="策划总监",
        duty="定调、统筹全流程、市场定位",
        node="showrunner",
        panels=(
            Panel("showrunner:main", "统筹与定位", "main", "showrunner"),
            Panel("showrunner:console", "全流程总览", "console", "showrunner"),
        ),
        legacy_keys=("writer", "story_planner"),
    ),
    Role(
        key="script_editor", label="文学策划", en="script_editor",
        dept="planning", real_job="文学策划",
        duty="剧本评估、修改意见、IP 改编",
        node="script_editor",
        panels=(
            Panel("script_editor:main", "剧本评估", "main", "script_editor"),
            Panel("script_editor:report", "逐项意见与历史", "report", "script_editor"),
        ),
        legacy_keys=("critics",),
    ),
    Role(
        key="screenwriter", label="编剧", en="screenwriter",
        dept="planning", real_job="编剧",
        duty="生成结构化剧本、分镜脚本",
        node="screenwriter",
        panels=(Panel("screenwriter:main", "剧本", "main", "screenwriter"),),
    ),
    Role(
        key="storyboarder", label="分镜师", en="storyboarder",
        dept="planning", real_job="分镜师",
        duty="拆解机位、景别、运动、时长、走位",
        node="storyboarder",
        panels=(Panel("storyboarder:main", "分镜表", "main", "storyboarder"),),
    ),
    # ---------------- 资产 ----------------
    Role(
        key="character_designer", label="角色设计师", en="character_designer",
        dept="assets", real_job="角色设计师",
        duty="角色三视图、LoRA 训练、角色圣经",
        node="character_designer",
        panels=(
            Panel("character_designer:main", "角色与三视图", "main", "character_designer"),
            Panel("character_designer:gallery", "角色圣经", "gallery", "character_designer"),
            Panel("character_designer:training", "角色 LoRA", "training", "character_designer"),
        ),
    ),
    Role(
        key="scene_designer", label="场景设计师", en="scene_designer",
        dept="assets", real_job="场景设计师",
        duty="场景概念图、白模场景搭建（2D）",
        node="scene_designer",
        panels=(
            Panel("scene_designer:main", "场景与白模", "main", "scene_designer"),
            Panel("scene_designer:gallery", "场景图集", "gallery", "scene_designer"),
        ),
    ),
    # ---------------- 生图 ----------------
    Role(
        key="photographer", label="摄影师", en="photographer",
        dept="imaging", real_job="摄影师",
        duty="提示词编写、ControlNet 控制、批量生图",
        node="photographer",
        panels=(
            Panel("photographer:main", "画面", "main", "photographer"),
            Panel("photographer:gallery", "抽卡结果", "gallery", "photographer"),
        ),
        legacy_keys=("image_generator",),
    ),
    Role(
        key="quality_assessor", label="质检总监", en="quality_assessor",
        dept="imaging", real_job="质检总监",
        duty="抽卡筛选、修复崩坏画面（手/脸/闪烁）",
        node="quality_assessor",
        panels=(
            Panel("quality_assessor:main", "质检总览", "main", "quality_assessor"),
            Panel("quality_assessor:report", "逐镜详情与重拍", "report", "quality_assessor"),
        ),
    ),
    # ---------------- 白膜 ----------------
    Role(
        key="previs_artist", label="白模预演师", en="previs_artist",
        dept="previs", real_job="预演师",
        duty="搭白模、摆走位、录机位、导出预演视频（2D）",
        node="previs_artist",
        panels=(
            Panel("previs_artist:main", "白模与走位", "main", "previs_artist"),
            Panel("previs_artist:timeline", "预演序列", "timeline", "previs_artist"),
        ),
    ),
    # ---------------- 动态 ----------------
    Role(
        key="continuity", label="场记", en="continuity",
        dept="motion", real_job="场记",
        duty="角色一致性、场景连贯性、道具连贯性检查",
        # 注意：职业 key 是 continuity（配置键用），节点 key 沿用 v1 的 continuity_checker
        node="continuity_checker",
        panels=(
            Panel("continuity:main", "连贯性检查", "main", "continuity_checker"),
            Panel("continuity:report", "问题清单", "report", "continuity_checker"),
        ),
        legacy_keys=("continuity_checker",),
    ),
    Role(
        key="voice_director", label="配音指导", en="voice_director",
        dept="motion", real_job="配音指导",
        duty="TTS 音色选型、情感提示词、BGM/音效匹配",
        node="voice_director",
        panels=(
            Panel("voice_director:main", "配音与音效", "main", "voice_director"),
            Panel("voice_director:timeline", "音轨时间线", "timeline", "voice_director"),
        ),
    ),
    # ---------------- 后期 ----------------
    Role(
        key="supervising_editor", label="剪辑指导", en="supervising_editor",
        dept="post", real_job="剪辑指导",
        duty="节奏把控、镜头组接指导",
        # 节点沿用 v1 的 rhythm_director（该节点已实现节奏方案）
        node="rhythm_director",
        panels=(Panel("supervising_editor:main", "节奏与组接", "main", "rhythm_director"),),
        legacy_keys=("rhythm_director",),
    ),
    Role(
        key="editor", label="剪辑师", en="editor",
        dept="post", real_job="剪辑师",
        duty="音画对齐、转场、字幕、输出",
        node="editor",
        panels=(
            Panel("editor:main", "时间线", "main", "editor"),
            Panel("editor:timeline", "剪辑时间线", "timeline", "editor"),
        ),
        legacy_keys=("aggregator",),
    ),
    Role(
        key="distributor", label="发行总监", en="distributor",
        dept="post", real_job="发行运营",
        duty="多平台分发、数据回收、运营策略",
        node="distributor",
        panels=(Panel("distributor:main", "分发", "publish", "distributor"),),
    ),
)

ROLE_BY_KEY: dict[str, Role] = {r.key: r for r in ROLES}


# ==================================================================
# 五、节点
# ==================================================================
# 含两类：① 由 Role 派生的主节点；② 不对应任何职业的系统节点。

#: 职业 → 主节点的额外属性（端口/能力/配置）。未列出的职业用空端口。
ROLE_NODE_SPECS: dict[str, dict] = {
    "workflow_architect": dict(
        kind="service", can_skip=False,
        consumes=(), produces=(),
        capability=(),
        agent_class="",
    ),
    "technical_director": dict(
        consumes=("script", "storyboard"),
        produces=("control_inputs",),
        capability=("llm", "lora_train"),
        # 配置键用 **_config_namespace**（= 运行时实际读取的 role），不是节点 key。
        # 例：本节点叫 technical_director，但 prompt_refiner 调用 role="director"，
        # 故键是 llm_provider_director —— 见 CONFIG_NAMESPACE_BY_KEY 的说明。
        config=_llm_fields(_config_namespace("technical_director")),
        legacy_keys=("director", "prompt_compiler"),
        agent_class="PromptCompiler",
    ),
    "showrunner": dict(
        consumes=("user_input",),
        produces=("project_brief", "story_outline"),
        capability=("llm",),
        # 模式字段放最前：**必须先选云端/本地**，再谈具体提供者
        config=(MODEL_MODE_FIELDS["llm"],) + _llm_fields(_config_namespace("showrunner")),
        legacy_keys=("writer", "story_planner"),
        agent_class="StoryPlanner",
    ),
    "script_editor": dict(
        consumes=("script", "story_outline"),
        produces=("script_review", "critic_report", "adaptation_plan"),
        capability=("llm",),
        config=_llm_fields(_config_namespace("script_editor")),
        legacy_keys=("critics",),
        agent_class="ScriptCritic",
    ),
    "screenwriter": dict(
        consumes=("story_outline", "art_brief"),
        produces=("script",),
        capability=("llm",),
        config=_llm_fields("screenwriter"),
        agent_class="Screenwriter",
    ),
    "storyboarder": dict(
        consumes=("script", "character_profiles", "scene_design_profiles"),
        produces=("storyboard",),
        capability=("llm",),
        config=_llm_fields("storyboarder"),
        agent_class="Storyboarder",
    ),
    "character_designer": dict(
        consumes=("story_outline", "art_brief_plan"),
        produces=("character_profiles", "character_reference_images", "character_lora"),
        capability=("image_gen", "lora_train"),
        # 两个模式字段放最前：图片生成 与 LoRA 训练**各自**先选云端/本地
        config=(MODEL_MODE_FIELDS["image_gen"], MODEL_MODE_FIELDS["lora_train"])
        + _image_fields("character_designer") + (
            # LoRA 训练的具体提供者（模式已在上方选定，这里可覆盖）
            ConfigField("lora_train_provider", "LoRA 训练来源（覆盖模式）", "select", "global",
                        # 占位符：表示"不覆盖、跟随上面的模式"（界面显示「跟随模式」）
                        ("(按模式)",),
                        help="留空/按模式即用上方的模式默认；也可强制指定",
                        default="", options_from="lora_train"),
            ConfigField("lora_rank", "LoRA 秩", "number", "global", default=32,
                        help="百炼要求 2 的倍数；越大拟合越强、越慢"),
            ConfigField("lora_train_steps", "训练步数", "number", "global", default=800,
                        help="百炼按步数计费，建议 ≥500 才收敛"),
        ),
        agent_class="CharacterDesigner",
    ),
    "scene_designer": dict(
        consumes=("script", "art_brief_plan"),
        produces=("scene_design_profiles", "scene_reference_images", "scene_whitebox"),
        capability=("image_gen", "render_2d"),
        config=_image_fields("scene_designer"),
        agent_class="SceneDesigner",
    ),
    "photographer": dict(
        consumes=("storyboard", "character_reference_images",
                  "scene_reference_images", "control_inputs", "previs"),
        # 提示词由摄影师编写（新表职责："提示词编写、ControlNet 控制、批量生图"）
        produces=("prompts", "images"),
        capability=("image_gen",),
        config=(MODEL_MODE_FIELDS["image_gen"],) + _image_fields("photographer") + (_MAX_IMAGES,),
        legacy_keys=("image_generator",),
        agent_class="ImageGenerator",
    ),
    "quality_assessor": dict(
        consumes=("images", "prompts", "character_reference_images"),
        produces=("quality_report", "fixed_images", "consistent_takes"),
        capability=("image_edit",),
        config=(
            ConfigField("quality_threshold", "合格阈值", "number", "global", default=0.5),
            ConfigField("consistency_threshold", "一致性阈值", "number", "global", default=75.0),
            # 例外：这个"修复模型"不属于任何独立职业（该节点没有自己的 Role），
            # 故用能力名 image_edit 作为命名空间，避免与摄影师的
            # image_provider_photographer 抢同一个键。
            ConfigField("image_provider_image_edit", "修复模型来源", "select", "role",
                        ("(自动)", "local", "dashscope"),
                        help="崩坏修复（手/脸/闪烁）；local 需 inpaint 路由，dashscope 用指令编辑"),
        ),
        agent_class="QualityAssessor",
    ),
    "previs_artist": dict(
        consumes=("storyboard", "scene_whitebox", "character_reference_images"),
        produces=("previs",),
        capability=("render_2d",),
        config=(
            # 只保留 2d：3D 预演（白模建模 + 渲染）成本与复杂度都远超当前定位，
            # 留着"3d"选项会让用户选了却没有任何反应。本项目统一 2D。
            ConfigField("previs_mode", "白模模式", "select", "global", ("2d",), "2d",
                        help="2D 俯视示意图：画出走位与机位，不产生 API 费用、无需下载模型"),
            ConfigField("previs_size", "预演画布尺寸", "text", "global", default="1280x720"),
        ),
    ),
    "continuity": dict(
        consumes=("storyboard", "character_profiles", "scene_design_profiles", "images"),
        produces=("continuity_report",),
        capability=("llm",),
        config=_llm_fields("continuity"),
        legacy_keys=("continuity_checker",),
        agent_class="ContinuityChecker",
    ),
    "voice_director": dict(
        consumes=("storyboard", "script"),
        produces=("voice_track", "bgm_track"),
        capability=("tts", "music"),
        config=(
            MODEL_MODE_FIELDS["tts"],
            ConfigField("tts_provider", "配音来源（覆盖模式）", "select", "global",
                        ("(按模式)",),
                        help="按模式即用上方模式默认；系统语音不需要 Key",
                        options_from="tts"),
            ConfigField("tts_voice", "默认音色", "text", "global"),
            ConfigField("tts_speed", "语速", "slider", "global", default=1.0),
        ),
    ),
    "supervising_editor": dict(
        # 依赖配音时长：总时长与剪辑点由「画面时长 vs 配音时长」共同决定
        consumes=("storyboard", "script", "voice_track"),
        produces=("rhythm_plan", "edit_plan"),
        capability=(),
        config=(
            ConfigField("rhythm_max_total_s", "总时长上限（秒）", "number", "global", default=60),
        ),
        legacy_keys=("rhythm_director",),
        agent_class="RhythmDirector",
    ),
    "editor": dict(
        consumes=("images", "rhythm_plan", "edit_plan", "voice_track", "bgm_track"),
        produces=("timeline", "final_video", "result"),
        capability=("video_gen",),
        config=(
            MODEL_MODE_FIELDS["video_gen"],
            ConfigField("video_provider", "视频来源（覆盖模式）", "select", "global",
                        ("(按模式)",),
                        help="按模式即用上方模式默认；选项由注册表派生（含 dashscope_video / seedance）",
                        options_from="video_gen"),
            # 模型清单由注册表派生（各提供者的 known_models 并集，带来源前缀）。
            # 原来是 `type="text"` + 空 options —— 界面上就是个**空框**，
            # 用户不知道能填什么（用户报过"视频模型没有api填入"）。
            ConfigField("video_model", "视频模型", "select", "global",
                        options_from_models="video_gen",
                        help="留空则用该提供者的默认模型；清单只列**当前提供者**已登记的模型，厂商新模型可直接输入"),
            ConfigField("video_max_duration", "每段时长（秒）", "select", "global",
                        options=("5", "7", "9", "12", "15"), default="7",
                        help="本地仅 5/7/9（8GB 显存超过 10s 必 OOM）；云端可到 15"),
            ConfigField("video_count", "视频段数（0=跟图片数）", "number", "global", default=0),
            # ---- 声音从哪来 ----
            #
            # 为什么要这一项：**音频有两条来源，而它们会打架**。
            # 阿里百炼的万相 wan3.0 官方写明「原生带音频」（产出里就有
            # 对白/环境音），而 TTS 也会生成一条配音轨 —— 两条同时存在，
            # 要么互相盖掉，要么糊在一起。用户问过
            # "配音不是某些视频模型自带吗"，指的就是这个。
            #
            # 取值（**存储值用英文 key**，界面显示名由前端给，见
            # `ConfigFieldEditor` 的 `optionLabel`）：
            #   auto    —— 不指定，按提供者能力自动决定（默认）
            #   native  —— 用**视频模型原生**音频
            #   tts     —— 用 TTS 配音（覆盖原生）
            #   mix     —— 两者混音（TTS 为主，原生降为背景）
            #   none    —— 不要声音
            #
            # ⚠️ `native` / `mix` 只对**声明了 `has_native_audio`** 的提供者
            #    有意义（见 `video_adapters.VideoProvider`）。前端会按当前
            #    提供者过滤掉不可选项 —— 否则用户选了"原生音频"，
            #    产物却是无声的，比不给选更糟。
            ConfigField("video_audio", "声音来源", "select", "global",
                        options=("auto", "native", "tts", "mix", "none"),
                        default="auto",
                        help="万相 wan3.0 原生带音频；Seedance / 图片拼接没有声音，只能靠配音"),
        ),
        legacy_keys=("aggregator",),
        agent_class="",
    ),
    "distributor": dict(
        consumes=("final_video", "story_outline"),
        produces=("publish_results",),
        capability=(),
    ),
}

#: 不对应任何职业的系统节点（v1 已有，v2 保留）
SYSTEM_NODES: tuple[Node, ...] = tuple(
    Node(
        key=n.key, label=n.label, dept=n.dept,
        consumes=n.consumes, produces=n.produces,
        legacy_keys=n.legacy_keys, agent_class=n.agent_class,
        workflow_node=_workflow_node(n.key),
        config_namespace=_config_namespace(n.key),
    )
    for n in (
        Node(
            key="art_brief_parser", label="美术设定包解析", dept="assets",
            consumes=("art_brief",), produces=("art_brief_plan",),
            agent_class="ArtBriefParser",
        ),
        Node(
            key="consistency_auditor", label="美术总监·一致性审查", dept="assets",
            consumes=("character_reference_images", "scene_reference_images"),
            produces=("consistency_score", "character_consistency_report"),
            legacy_keys=("consistency_auditor",),
            agent_class="ConsistencyAuditor",
        ),
    )
)


NODES: tuple[Node, ...] = tuple(
    Node(
        key=r.node,
        label=r.label,
        dept=r.dept,
        legacy_keys=tuple(ROLE_NODE_SPECS.get(r.key, {}).get("legacy_keys", ())),
        workflow_node=_workflow_node(r.node),
        config_namespace=_config_namespace(r.node),
        **{k: v for k, v in ROLE_NODE_SPECS.get(r.key, {}).items() if k != "legacy_keys"},
    )
    for r in ROLES
)

ALL_NODES: tuple[Node, ...] = NODES + SYSTEM_NODES
NODE_BY_KEY: dict[str, Node] = {n.key: n for n in ALL_NODES}


# ==================================================================
# 六、人工确认点（每个职业节点都可挂）
# ==================================================================

CHECKPOINTS: tuple[Checkpoint, ...] = (
    Checkpoint("critics", "文学策划·剧本评估检查", "script_editor", "script_review"),
    Checkpoint("script", "编剧·剧本检查", "screenwriter", "script"),
    Checkpoint("assets", "角色/场景·资产检查", "character_designer", "character_reference_images"),
    Checkpoint("storyboard", "分镜师·分镜检查", "storyboarder", "storyboard"),
    Checkpoint("rhythm", "剪辑指导·节奏检查", "rhythm_director", "rhythm_plan"),
    Checkpoint("continuity", "场记·连贯性检查", "continuity_checker", "continuity_report"),
    Checkpoint("prompts", "技术指导·提示词检查", "technical_director", "prompts"),
    Checkpoint("images", "摄影师·画面检查", "photographer", "images"),
    Checkpoint("quality", "质检总监·质量检查", "quality_assessor", "quality_report"),
    Checkpoint("previs", "白模预演师·预演检查", "previs_artist", "previs"),
    Checkpoint("voice", "配音指导·配音检查", "voice_director", "voice_track"),
    Checkpoint("edit", "剪辑师·成片检查", "editor", "timeline"),
)

CHECKPOINT_BY_KEY: dict[str, Checkpoint] = {c.key: c for c in CHECKPOINTS}
