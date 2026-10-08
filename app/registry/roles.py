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
    Artifact("scene_design_profiles", "场景锚定描述", "json", "scene_design_profiles"),
    Artifact("scene_reference_images", "场景概念图", "json", "scene_reference_images"),
    Artifact("scene_whitebox", "场景白模（3D）", "image"),
    # ---- 生图 ----
    Artifact("control_inputs", "结构化控制图（ControlNet）", "json"),
    Artifact("prompts", "生图提示词", "list", "prompts"),
    Artifact("images", "分镜画面", "list", "images"),
    Artifact("quality_report", "质检报告（质量+一致性）", "json", "quality_report"),
    Artifact("fixed_images", "修复后的画面", "list"),
    Artifact("consistent_takes", "抽卡筛优后的画面", "list"),
    # ---- 白模预演 ----
    Artifact("previs", "3D 白模预演（走位/机位）", "json"),
    # ---- 动态（用户职责表里叫"融合视频"）----
    Artifact("continuity_report", "连贯性报告（角色/场景/道具）", "list", "continuity_report"),
    Artifact("voice_track", "配音轨（含每句时长）", "audio"),
    Artifact("bgm_track", "BGM / 音效轨", "audio"),
    # 融合视频线（2026-10-07 补：动态合成师 / 视频质检师）
    # `video` 是**图外产物**：由独立的异步视频任务产出，落在会话 payload。
    # 没有任何节点 produces 它，但「视频质检师」consumes 它 ——
    # 所以它在 `api.EXTERNAL_ARTIFACTS` 里显式登记，校验器才不会误判。
    Artifact("video", "镜头视频（异步任务产出，图外）", "video"),
    Artifact("fused_video", "融合视频（静态图 + 白模对齐）", "video"),
    Artifact("video_report", "视频质检报告（闪烁/变形/崩坏）", "json", "video_report"),
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
    Department("previs", "白模", 4, "用白模预演走位与机位"),
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
    # 后期三环已接进执行图（2026-10-07）
    "inpainter": "inpainter",
    "motion_compositor": "motion_compositor",
    "video_assessor": "video_assessor",
    "rhythm_director": "rhythm_director",
    "continuity_checker": "continuity_checker",
    # ---- 改了名的（关键） ----
    "showrunner": "story_planner",            # 阶段名 writer
    "script_editor": "critics",               # 阶段名 critics
    "technical_director": "prompt_compiler",  # 阶段名 director
    "photographer": "image_generator",        # 阶段名 photographer
    "editor": "aggregator",                   # 阶段名 aggregator
    # ---- v2 新角色 ----
    # `previs_artist` 已**接进执行图**（用户要求"把「预演」构成「白模预演师」
    # 一部分"）：它在分镜确认之后跑，产出 3D 预演**初稿**（`node_previs_artist`），
    # 人工再到 `#/previs` 编辑器微调。
    #
    # 其余三个仍是"声明了但不在图里"：
    #   · workflow_architect —— 它是**工具**（导演台的架构师抽屉），本就不该是执行步骤
    #   · voice_director     —— TTS 有实现，但没接进流程
    #   · distributor        —— 有实现类，但没接进流程
    "workflow_architect": "",
    "previs_artist": "previs_artist",
    # 配音指导已接进执行图（用户要求"1、2 先做"里的 ①）：
    # 它在预演之后、节奏之前跑，逐镜头合成对白音频。
    "voice_director": "voice_director",
    # 发行总监已接进执行图（用户："剪辑师不是必需的，可以点跳过"）——
    # 它在图里但**输入缺失时优雅跳过**，所以"可跳过"语义成立。
    #
    # 注：`editor`（剪辑师）**不需要写在这里** —— 它的 workflow_node
    # 本来就是上面的 `"editor": "aggregator"`（历史命名）。
    "distributor": "distributor",
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

# ==================================================================
# 三、职业捆绑组（用户 2026-10-07 指定）
# ==================================================================
#
# ## 这是什么
#
# 把若干职业捆成一组：**要用就一起用**。用于"在画布上选一段运行"的交互
# —— 选中组里任一职业时整组一起选中（它们互相依赖，单跑没意义）。
#
# ## 为什么必须**手工指定**，不能自动推导
#
# 试过按"有产物依赖就捆绑"自动推导 —— **结果是全部节点变成一捆**：
# 依赖太密（摄影师吃分镜、场记吃摄影师、剪辑吃摄影师…），
# 传递闭包会吞掉全图。所以这是**业务判断**，只能人工定。
#
# ## 与部门（Department）的区别
#
#   · 部门   = **组织归属**，一个职业只属于一个部门；
#   · 捆绑组 = **使用约束**，可以跨部门
#     —— 如「策划」组里的 `storyboarder`（策划部）与
#     `continuity`（动态部）必须一起用。
#
# ## ⚠️ 目前只是**数据**，还没有约束力
#
# "在画布上选一段运行"（`start_stage`/`end_stage` 的替代方案）还没做，
# 所以这里只把分组声明出来 + 下发到快照，**没有**执行期校验。
# 做那个功能时再据此自动带上同组职业。
BUNDLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (key, label, 成员职业 key)
    ("planning", "策划", ("showrunner", "script_editor", "screenwriter", "storyboarder")),
    ("imagery", "图片", ("photographer", "quality_assessor", "inpainter")),
    ("video", "视频", ("motion_compositor", "video_assessor", "editor")),
    ("continuity", "场记与技术指导", ("continuity", "technical_director")),
)

#: 职业 key → 捆绑组 key
BUNDLE_BY_ROLE: dict[str, str] = {
    role_key: bkey for bkey, _label, keys in BUNDLES for role_key in keys
}


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
        duty="模型选型、视频生成参数优化",
        node="technical_director",
        bundle="continuity",
        panels=(
            Panel("technical_director:main", "模型与参数", "main", "technical_director"),
        ),
        legacy_keys=("director",),
    ),
    # ---------------- 策划 ----------------
    Role(
        key="showrunner", label="策划总监", en="showrunner",
        dept="planning", real_job="策划总监",
        duty="定调、统筹全流程、市场定位",
        node="showrunner",
        bundle="planning",
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
        bundle="planning",
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
        bundle="planning",
        panels=(Panel("screenwriter:main", "剧本", "main", "screenwriter"),),
    ),
    Role(
        key="storyboarder", label="分镜师", en="storyboarder",
        dept="planning", real_job="分镜师",
        duty="拆解机位、景别、运动、时长、走位",
        node="storyboarder",
        bundle="planning",
        panels=(Panel("storyboarder:main", "分镜表", "main", "storyboarder"),),
    ),
    # ---------------- 资产 ----------------
    Role(
        key="character_designer", label="角色设计师", en="character_designer",
        dept="assets", real_job="角色设计师",
        duty="角色三视图、角色圣经",
        node="character_designer",
        panels=(
            Panel("character_designer:main", "角色与三视图", "main", "character_designer"),
            Panel("character_designer:gallery", "角色圣经", "gallery", "character_designer"),
        ),
    ),
    Role(
        key="scene_designer", label="场景设计师", en="scene_designer",
        dept="assets", real_job="场景设计师",
        duty="场景概念图、白模场景搭建（3D）",
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
        bundle="imagery",
        panels=(
            Panel("photographer:main", "画面", "main", "photographer"),
            Panel("photographer:gallery", "抽卡结果", "gallery", "photographer"),
        ),
        legacy_keys=("image_generator",),
    ),
    Role(
        key="quality_assessor", label="图像质检师", en="quality_assessor",
        dept="imaging", real_job="质检师",
        # 用户 2026-10-07 拍板："图像质检师就是质检总监" —— 只是**换个名字**，
        # 不是新增职业。所以 `key` / 节点名保持 `quality_assessor` 不变
        # （改 key 会牵动图规格、前端快照与 13 个文件，风险不值得）。
        #
        # ⚠️ 但职责要**收窄**：修图（`fixed_images`）拆给新的「修图师」。
        # 这条 duty 原先写"抽卡筛选、**修复崩坏画面**"，与修图师重复。
        duty="抽卡筛选静态图，检查崩坏、手部畸形、风格统一",
        node="quality_assessor",
        bundle="imagery",
        panels=(
            Panel("quality_assessor:main", "质检总览", "main", "quality_assessor"),
            Panel("quality_assessor:report", "逐镜详情与重拍", "report", "quality_assessor"),
        ),
    ),
    Role(
        key="inpainter", label="修图师", en="inpainter",
        dept="imaging", real_job="修图师",
        # 用户 2026-10-07 要求补的职业。职责原先**声明**在「图像质检师」的
        # `produces` 里（`fixed_images`），这里拆出来独立成环。
        #
        # ⚠️ **但实现是没有的** —— 我查证过，别被"能力已声明"骗了：
        #   · `ImageService` 只有 `generate()`（文生图），**没有 edit / inpaint 方法**；
        #   · `_local` 分支走 Z-Image **文生图**，无 inpaint 路由；
        #   · `z-image-inpaint` 只出现在**三个声明处**（`providers.py` 的
        #     ProviderSpec、`cloud_cost.py` 的成本表、`local_probe.py` 的探测表），
        #     **没有任何代码真正调用它做修复**。
        #
        # 而且 `fixed_images`（修复后的画面）这个产物**从来没有被写入过** ——
        # `quality_assessor.run()` 只返回 `quality_report`。这个声明**一直是空的**
        # （我接手时就是），不是我这次改出来的。
        #
        # 所以这个节点目前是**占位**：跑起来会优雅跳过并说明"修复后端未接入"，
        # 等真正接上 inpaint 路由（或 dashscope 的 qwen-image-edit）才能干活。
        duty="Inpainting/Outpainting 修复 AI 生图瑕疵，扩图，精修关键帧",
        node="inpainter",
        bundle="imagery",
        panels=(
            Panel("inpainter:main", "修复与扩图", "main", "inpainter"),
            Panel("inpainter:gallery", "修复前后对比", "gallery", "inpainter"),
        ),
    ),
    # ---------------- 白模 ----------------
    Role(
        key="previs_artist", label="白模预演师", en="previs_artist",
        dept="previs", real_job="预演师",
        duty="搭白模、摆走位、录机位、导出预演视频（3D）",
        node="previs_artist",
        panels=(
            Panel("previs_artist:main", "白模与走位", "main", "previs_artist"),
            Panel("previs_artist:timeline", "预演序列", "timeline", "previs_artist"),
        ),
    ),
    # ---------------- 动态（用户职责表里叫"融合视频"）----------------
    Role(
        key="motion_compositor", label="动态合成师", en="motion_compositor",
        dept="motion", real_job="动态合成师",
        # 用户 2026-10-07："缺的 agent 也补上"。
        # 它是**全新岗位**（原先项目里完全没有）：把静态图与白模视频**对齐融合**
        # （Depth + IP-Adapter 之类）生成视频，解决视频模型自己编空间的问题。
        #
        # ⚠️ 实现在 `app/services/post_production.py::composite_motion`，
        # 当前**没有融合后端**（ComfyUI 节点未接），所以它会**优雅跳过**并说明。
        duty="将静态图与白模视频对齐，融合生成视频（Depth + IP-Adapter）",
        node="motion_compositor",
        bundle="video",
        panels=(
            Panel("motion_compositor:main", "融合与对齐", "main", "motion_compositor"),
            Panel("motion_compositor:timeline", "融合结果", "timeline", "motion_compositor"),
        ),
    ),
    Role(
        key="video_assessor", label="视频质检师", en="video_assessor",
        dept="motion", real_job="视频质检师",
        # 用户 2026-10-07："缺的 agent 也补上"。
        # 检查融合视频有没有闪烁、变形、崩坏，筛出最佳片段。
        #
        # ⚠️ 它消费 `video`（独立异步视频任务的产物，不在图状态里），
        # 没有视频时**优雅跳过** —— 见 `app/services/post_production.py::assess_video`。
        duty="检查融合视频是否有闪烁、变形、崩坏，筛选最佳片段",
        node="video_assessor",
        bundle="video",
        panels=(
            Panel("video_assessor:main", "视频质检", "main", "video_assessor"),
            Panel("video_assessor:report", "问题片段", "report", "video_assessor"),
        ),
    ),
    Role(
        key="continuity", label="场记", en="continuity",
        dept="motion", real_job="场记",
        duty="角色一致性、场景连贯性、道具连贯性检查",
        # 注意：职业 key 是 continuity（配置键用），节点 key 沿用 v1 的 continuity_checker
        node="continuity_checker",
        bundle="continuity",
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
        bundle="video",
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
        # ⚠️ 原先这里是 `capability=()` + `agent_class=""` —— **它没有能力、
        # 也没有实现**，只是一个占位（排在执行顺序第 1 位却永不执行，
        # 用户问过"为啥没有实现"）。
        #
        # 现在按用户要求"接 A"：给它 **`llm` 能力**，让它真的能干活 ——
        # 分析当前工作流并**产出变更提案**（`app/workflows/architect.py`）。
        #
        # 但它**不直接改图**：用户明确要求"每次参与执行需要用户审核"，
        # 所以它的产出只是一份提案，必须过审核闸门才允许生效。
        #
        # 配置键用 `_config_namespace` 取（与其它职业一致）；架构师没有
        # 别名，所以命名空间就是 `workflow_architect`，即
        # `llm_model_workflow_architect` / `llm_provider_workflow_architect`
        # / `llm_api_key_workflow_architect`。
        capability=("llm",),
        config=_llm_fields(_config_namespace("workflow_architect")),
        agent_class="",
    ),
    "technical_director": dict(
        consumes=("script", "storyboard"),
        produces=("control_inputs",),
        capability=("llm",),
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
        produces=("character_profiles", "character_reference_images"),
        capability=("image_gen",),
        config=(MODEL_MODE_FIELDS["image_gen"],) + _image_fields("character_designer"),
        agent_class="CharacterDesigner",
    ),
    "scene_designer": dict(
        consumes=("script", "art_brief_plan"),
        produces=("scene_design_profiles", "scene_reference_images", "scene_whitebox"),
        capability=("image_gen", "render_3d"),
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
        # `scene_reference_images` 是 2026-10-07 加的：原先由已删除的
        # 「美术总监·一致性审查」消费（它比对角色/场景参考图）。
        consumes=("images", "prompts", "character_reference_images",
                  "scene_reference_images"),
        # ⚠️ 原先这里还产出 `fixed_images`（修复后的画面）—— 那是**修图师的活**，
        # 2026-10-07 拆给 `inpainter`。留着会让"谁产修复图"有两个答案。
        # `consistency_score` / `character_consistency_report` 原先是
        # 「美术总监·一致性审查」声明的产出，**其实一直由本节点在算**
        # （`quality_assessor.py:88-89`）—— 那是重复声明。该环节删除后归到这里。
        produces=("quality_report", "consistent_takes",
                  "consistency_score", "character_consistency_report"),
        capability=("image_edit",),
        config=(
            ConfigField("quality_threshold", "合格阈值", "number", "global", default=0.5),
            ConfigField("consistency_threshold", "一致性阈值", "number", "global", default=75.0),
            # 注意：`image_provider_image_edit`（修复模型来源）**也留在这里**，
            # 同时 `inpainter` 声明了同一个键 —— 这是**有意的别名**：
            # 键是已存设置（白名单派生自注册表），删掉会让用户的设置静默丢失。
            # 两个节点都能改它，改的是同一个值。见 `inpainter` 那段的说明。
            ConfigField("image_provider_image_edit", "修复模型来源", "select", "role",
                        ("(自动)", "local", "dashscope"),
                        help="崩坏修复（手/脸/闪烁）；local 需 inpaint 路由，dashscope 用指令编辑"),
        ),
        agent_class="QualityAssessor",
    ),
    "inpainter": dict(
        consumes=("images", "quality_report", "character_reference_images"),
        produces=("fixed_images",),
        capability=("image_edit",),
        config=(
            # 这个键的命名空间用**能力名** `image_edit`（不是职业名）——
            # 历史原因：它原先挂在 `quality_assessor` 上，而那个节点没有自己的
            # Role 命名空间，所以用能力名避免和摄影师的 `image_provider_photographer`
            # 抢键。现在有了独立的 `inpainter` 职业，但**键不能改**：
            # 用户已存的设置就存在这个键上，改了会静默丢失（设置白名单是派生的）。
            ConfigField("image_provider_image_edit", "修复模型来源", "select", "role",
                        ("(自动)", "local", "dashscope"),
                        help="崩坏修复（手/脸/闪烁）；local 需 inpaint 路由，"
                             "dashscope 用指令编辑"),
            # ⚠️ role 级配置键**必须包含角色命名空间**（`inpainter`），
            # 否则界面写入的键与运行读取的键不一致（有测试守住）。
            ConfigField("inpainter_outpaint", "扩图开关", "select", "role",
                        ("关", "开"),
                        help="开：对边缘被裁的主体做 Outpainting 扩图"),
        ),
        agent_class="",
    ),
    "video_assessor": dict(
        # ⚠️ 消费 `video`：那是**独立异步视频任务**的产物（落在会话 payload），
        # 不在图状态里。所以这个节点现在会**优雅跳过** —— 见
        # `app/services/post_production.py`。
        consumes=("video", "storyboard"),
        produces=("video_report",),
        capability=("video_gen",),
        config=(
            ConfigField("video_assessor_flicker_threshold", "闪烁阈值", "number", "global",
                        default=0.35,
                        help="帧间差异超过它视为闪烁；越低越严格"),
        ),
        agent_class="",
    ),
    "motion_compositor": dict(
        # 融合：把静态图与白模视频对齐（Depth + IP-Adapter 之类）生成视频。
        consumes=("images", "previs", "scene_whitebox"),
        produces=("fused_video",),
        capability=("video_gen",),
        config=(
            ConfigField("composite_mode", "融合方式", "select", "global",
                        ("depth", "pose", "depth+pose"), "depth",
                        help="用白模的哪种结构条件去约束静态图生成视频"),
        ),
        agent_class="",
    ),
    "previs_artist": dict(
        consumes=("storyboard", "scene_whitebox", "character_reference_images"),
        produces=("previs",),
        capability=("render_3d",),
        config=(
            # ⚠️ 这里的历史（我核对过，别凭注释想当然）：
            #
            # 这段注释原先写"只保留 2d：3D 预演成本与复杂度都远超当前定位，
            # 留着 3d 选项会让用户选了却没有任何反应。**本项目统一 2D**" ——
            # **这句话已经过时**：3D 预演后来做出来了，而且**成了唯一实现**：
            #   · 前端 `web/src/previs/` 是完整 Three.js 3D 场景（Scene3D /
            #     bodySpecMesh / exporter 逐帧渲染）；
            #   · 后端 `white_model.py` 生成体块 JSON 规格（右手系、单位米）；
            #   · `whitebox_video.py` 把 3D 帧序列编码成 mp4 当参考视频。
            #
            # 选项值仍留 `"2d"` 是**兼容已存的设置值**，不是还在跑 2D。
            # 能力名 `render_3d` 同理（它是声明，没有调用点）。
            ConfigField("previs_mode", "白模模式", "select", "global", ("3d",), "3d",
                        help="3D 体块预演：用体块摆走位、调机位，导出参考视频。"
                             "不产生 API 费用（渲染在前端完成，逐帧导出）"),
            ConfigField("previs_size", "预演画布尺寸", "text", "global", default="1280x720"),
            # ⚠️ 必须声明文字模型三键 —— 白模规格是**让 LLM 生成 JSON** 的
            # （`white_model_prompt.generate_spec(..., role="previs_artist")`）。
            #
            # 漏了这三个键的实际后果（2026-10-08 实测）：
            #   `role="previs_artist"` 取不到专属密钥 → 回退全局 `llm_api_key`
            #   （`.env` 里是空的）→ `/api/previs/white-model` 直接 400：
            #   "LLM 客户端未初始化（provider=deepseek, role=previs_artist）"；
            #   而且设置页里**根本找不到**能填 `previs_artist` 密钥的地方
            #   → 白模生成功能对用户完全不可用。
            #
            # `previs_artist` 是**最后一个**补上这三键的 LLM 节点。
            # 有测试守着"凡是需要 LLM 的节点都必须声明这三键"。
            *_llm_fields("previs_artist"),
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
        # ⚠️ 这两个字段**原先漏了**，是真 bug：底下的 `Node(...)` 里写了
        # `capability=` / `config=` 也**会被静默丢弃** —— 因为这段推导式
        # 是**重新构造**一个新 Node，只复制上面列出的那些字段。
        #
        # 表现：系统节点（美术设定包解析 / 一致性审查 / 剪辑指导 / 发行总监）
        # 无论如何都改不出能力与配置字段。我给「美术设定包解析」补
        # `capability=("llm",)` 时就是被它吃掉的（补了读出来还是空）。
        #
        # 注意顺序：`**` 展开在前，`workflow_node` / `config_namespace`
        # 是**派生值**，放最后覆盖 —— 与下面 `NODES` 的写法保持一致。
        capability=n.capability,
        config=n.config,
        workflow_node=_workflow_node(n.key),
        config_namespace=_config_namespace(n.key),
    )
    for n in (
        Node(
            key="art_brief_parser", label="美术设定包解析", dept="assets",
            consumes=("art_brief",), produces=("art_brief_plan",),
            # ⚠️ 这个能力**原先漏了**：它确实在调 LLM
            # （`ArtBriefParser._parse_llm` → `llm.chat_json(...)`），
            # 只是运行时**借用**编剧的配置（`role="writer"`），所以没声明。
            #
            # 补上之后：
            #   · 画布上会标出"这个节点要用文字模型"；
            #   · 设置页「文字模型」卡里会出现它自己的
            #     `llm_model_art_brief_parser` / `llm_provider_art_brief_parser`
            #     / `llm_api_key_art_brief_parser`，可单独指定；
            #   · 运行时也改成按自己的 role 取（见该 Agent 的 `_parse_llm`）。
            capability=("llm",),
            agent_class="ArtBriefParser",
            config=_llm_fields("art_brief_parser"),
        ),
        # ⚠️ 「美术总监·一致性审查」**已删除**（用户 2026-10-07）：
        #   它的产出（整体一致性评分 / 角色跨镜头一致性报告）与「图像质检师」
        #   **完全重复**（后者本来就在算，见 quality_assessor.py:88-89），
        #   而且它在摄影师**之前**跑，只能比"参考图之间"，算不出真正的
        #   跨镜头一致性 —— 那本该在出图之后算。
        #   这两个产出已归到 quality_assessor（见下面的 ROLE_NODE_SPECS）。
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
