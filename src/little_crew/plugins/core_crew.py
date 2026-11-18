"""核心剧组（Core Crew）· **默认名册插件**（2026-11-18，B1 全职业插件化）。

19 个核心职位从 `agent/roster.py` 的 `_specs()` **原样迁入**，改为通过
`PluginManifest.positions` 声明 —— 从此职位不再硬编码在核心代码里：

  · 名册（`DEFAULT_ROSTER`）、编制（`crew_profile`）、依赖图（`pipeline_graph`）、
    导演台画布（`console`）全部从**插件注册表**构建
  · `provides["crew.roster"]` 返回 `SPECS`，兼容旧的"核心名册"能力名
  · `default_roster=True`：这 19 位构成**默认单集名册**（插件专属职位不进默认名册）
  · 插件机制在 `plugin/__init__.py`；`DEFAULT_PLUGIN_MODULES` 会在名册导入时
    自动 `ensure_default_plugins()`，所以默认行为与迁移前完全一致

**20/20 口径**：19 个核心职位（本插件）+ 1 个总导演/showrunner
（`plugins/series_showrunner.py` 的 `positions`），全部通过插件 positions 声明。
"""

from __future__ import annotations

from little_crew.agent.crew_spec import (
    AgentSpec,
    Department,
    ExecutionKind,
    Stage,
)
from little_crew.plugin import PluginManifest

__all__ = ["SPECS", "PLUGIN"]


# ==========================================================================
# 19 个核心剧组职位（默认名册）
# ==========================================================================

SPECS: tuple[AgentSpec, ...] = (
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
            consumes=("script.draft", "script.revise", "script.critique", "style.guard", "character.design", "scene.design", "continuity.check", "shot.list", "production.schedule", "shot.video", "shot.voice", "shot.music", "review.board", "compliance.check", "lighting.plan"),
            task_profile="script.structured",
            prompt_template="crew.director.v1",
            max_reasoning_steps=8,
            real_world_duty="把握全片艺术方向，做最终创作决策，协调各部门",
            notes=("调度枢纽。持有全局预算，按阶段推进并把失败路由到返工链。"
                   "**不得直调 L3** —— 由 CapabilityRegistry.audit() 强制（R2 禁止跳层）。"
                   "可在 `episode.plan.cameraDefaults` 给**整集默认机位/光学**（景别/运镜仍归分镜师）。"),
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
            mandatory=True,
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

        # ================= 美术组（4 人）=================
        AgentSpec(
            agent_id="production_designer",
            crew_title="美术指导",
            crew_title_en="Production Designer",
            department=Department.ART,
            stage=Stage.ASSET,
            execution=ExecutionKind.TEXT_LLM,
            # 2026-11-15：**资产生产与美术裁决分离**。
            # 角色资产由角色设计师产出、场景资产由场景设计师产出；
            # 美术指导只管总体基调与一致性裁决（不再下场产场景资产）。
            capabilities=("art.direction",),
            produces=("art.bible",),
            requires=("script.final",),
            task_profile="shot.image.final",
            prompt_template="crew.production_designer.v1",
            max_reasoning_steps=3,
            real_world_duty="总体视觉风格与美术 bible，裁决角色/场景资产的一致性",
            notes=("产出视觉基调（art.bible）；**不直接产场景资产** —— "
                   "场景卡归场景设计师（`scene.library`），资产层保持对称。"),
        ),
        AgentSpec(
            agent_id="scene_designer",
            crew_title="场景设计师",
            crew_title_en="Scene Designer",
            department=Department.ART,
            stage=Stage.ASSET,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("scene.design",),
            produces=("scene.library",),
            requires=("script.final",),
            task_profile="script.structured",
            prompt_template="crew.scene_designer.v1",
            max_reasoning_steps=3,
            real_world_duty="逐场设计空间/材质/道具/色板/年代，产出场景库（场景卡）",
            notes=("**与角色设计师同级、同组**：角色资产是『图+外观』，"
                   "场景资产是『场景卡（+可选场景概念图）』。"
                   "场景卡以**逐字稳定**的文本进入 IR → 跨镜/跨集不漂移。"),
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
            # 2026-11-15 合并：原「质检员」并入场记（**同一维度只留一个权威**）。
            # 单镜质检要**看图**判崩没崩 → 执行方式必须是 VLM（文本 LLM 做不了）。
            execution=ExecutionKind.VLM,
            capabilities=("continuity.check", "shot.qc"),
            produces=("continuity.report", "continuity.sheet", "qc.report"),
            requires=("character.anchor_set", "shot.image"),
            task_profile="review.vlm_blind",
            prompt_template="crew.continuity_supervisor.v1",
            max_reasoning_steps=3,
            real_world_duty=("单镜质检（崩坏判定 + 返工指令）+ 跨镜连贯性审计"
                             "（服装/道具/轴线/视线）"),
            notes=("**合并说明（2026-11-15）**：原「质检员」并入本职位 —— "
                   "两者都以画面为对象、都要看图，分开只会让返工通道出现两个来源。"
                   "分工：**单镜崩坏=唯一权威（可下返工指令）**；"
                   "跨镜一致性=审计模式（只报警，不进裁定）。"
                   "**跨模型族盲读**的落点之一：审核模型必须与生成模型不同族。"),
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
            task_profile="script.structured",
            prompt_template="crew.storyboard_artist.v1",
            max_reasoning_steps=4,
            mandatory=True,
            real_world_duty="把剧本拆解为镜头表：**景别 + 运镜**（机位由导演默认/摄影师覆盖）",
            notes=("**每镜时长硬上限 10 秒** —— H3 实测 12 秒必 OOM，"
                   "且降分辨率无效（瓶颈是注意力序列长度）。"),
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
            real_world_duty="机位与光学执行 + 特殊镜头（手持/航拍/长镜头）；\n"
                            "景别/运镜由分镜师定，默认机位由导演 `cameraDefaults` 给",
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
            agent_id="post_production",
            crew_title="后期统筹",
            crew_title_en="Post-Production Supervisor",
            department=Department.EDITORIAL,
            stage=Stage.ASSEMBLE,
            # **不调模型**：字幕/转场/特效/BGM/音轨策略都是**确定性规则**，
            # 执行交给 ffmpeg；职位只负责"定方案 + 出计划"。
            execution=ExecutionKind.ASSEMBLY,
            capabilities=("post.plan",),
            produces=("post.plan",),
            requires=("shot.list", "script.final"),
            max_reasoning_steps=1,
            real_world_duty="后期统筹：字幕/转场/特效/BGM/音轨策略，产出后期计划",
            notes=("**合并了原「剪辑师」与「合成剪辑」**：节奏判断并入本职位（但它不出分数），",
                   "拼接/混流/字幕烧录仍由 ffmpeg 执行。",
                   "音轨策略二选一：`native`（视频模型自带音轨，H3 FL2VA）| `tts+bgm`（静帧路径）。",
                   "判据可数：字幕条数/时长、响度下限、BGM 增益、转场类型与数量。"),
        ),
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
            # 2026-11-15：内容审查改为 **VLM 初审 + 人工/法务复审**（初审绝不宣布通过），
            # 授权/交付两腿仍是确定性工具 —— 所以执行方式记 VLM（因为有视觉调用）。
            execution=ExecutionKind.VLM,
            capabilities=("compliance.check",),
            produces=("compliance.report",),
            requires=("episode.master",),
            # 2026-11-15：**LicenseGate 归口为本职位的职能工具**（`license.audit`）。
            # 注意「归口」≠「搬家」：引擎仍在 `governance/license.py`，
            # 由 **router 在路由期**调用做硬门禁（还没花钱就拒）；这里声明的是
            # **权责归属** —— 合规官名下的工具，交付报告的授权部分由它汇总。
            tools=("license.audit", "gate.assert"),
            max_reasoning_steps=2,
            real_world_duty="版权清理、内容审查、署名规范",
            notes=("**三腿**：①授权硬门禁（`LicenseGate` 引擎，归口为本职位工具 "
                   "`license.audit`，路由期执行）②交付审计（代码：AI 标识/署名/素材登记）"
                   "③内容 **VLM 初审 + 人工/法务复审**。"
                   "审核结论只有 `blocked`（硬规则没过）或 `needs_human_review`（等人工签字），"
                   "**永远不产生 `signed`** —— 不可自签收。"),
        ),

        # ================= 审片组（1 人）=================
        AgentSpec(
            agent_id="review_board",
            crew_title="审片",
            crew_title_en="Review Board",
            department=Department.REVIEW,
            stage=Stage.GATE,
            execution=ExecutionKind.TEXT_LLM,
            capabilities=("review.board",),
            produces=("review.verdict",),
            requires=("shot.image", "shot.video"),
            task_profile="review.vlm_blind",
            prompt_template="crew.review_board.v1",
            max_reasoning_steps=3,
            real_world_duty="成片审查：艺术质量与观众感受（审文本镜头表；看图由质检员承担）",
            notes=("QC-5 五维品控：完整性 25 / 一致性 40 / 品质感 25 / "
                   "可执行性 / 合规性。**跨模型族盲读**，且不得由生成方自任。"),
        ),
        AgentSpec(
            agent_id="cover_designer",
            crew_title="封面设计师",
            crew_title_en="Cover Designer",
            department=Department.REVIEW,
            stage=Stage.GATE,
            # **必须看图**：哪张能当门面是纯视觉判断（主体完整/未切头/无崩坏）
            execution=ExecutionKind.VLM,
            capabilities=("cover.select", "cover.generate"),
            produces=("cover.image",),
            requires=("shot.image",),
            task_profile="review.vlm_blind",
            prompt_template="crew.key_art.v1",
            max_reasoning_steps=1,
            real_world_duty="产出封面主视觉：可从成片选帧，也可用角色/美术设定生成新封面",
            notes=("**双模式**：①`select` 从成片选帧（拼图一次判断：候选等距抽样 8 张 → "
                   "ffmpeg 拼 2×4 → VLM 一次选格 → 复制**原分辨率**那张）；"
                   "②`generate` 以**角色设定图 + 美术/剧情设定**为输入做图生图，产出海报感封面。"
                   "默认 select（可追溯）；generate 会引入新内容，需显式开。"
                   "宁可不出封面（模型回「不选」）也不选有硬伤的一张。"),
        ),
)


PLUGIN = PluginManifest(
    name="core_crew",
    version="0.1.0",
    description="19 个核心剧组职位（默认名册）",
    provides={"crew.roster": lambda: SPECS},
    positions=SPECS,
    default_roster=True,
    optional=False,
)
