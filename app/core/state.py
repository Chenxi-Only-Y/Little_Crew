"""小剧组 (Little Crew) — LangGraph 全局状态定义。

所有 Agent 之间通过一个共享的 `CrewState` 传递数据。
每个 Agent 的 `run()` 返回 `dict`，由 LangGraph 增量合并进该状态。

字段按阶段分组、全部可选（`total=False`），便于逐环节写入。
"""

from typing import Annotated, TypedDict


def _last_value(a, b):
    """并行更新时合并：后写的值覆盖（last-write-wins）。用于 current_phase 等单值字段。"""
    return b if b is not None else a


def _append(a, b):
    """并行更新时合并：把 b（列表）追加到 a（列表）之后。用于 thinking_log 等累积字段。"""
    return (a or []) + (b or [])


class CrewState(TypedDict, total=False):
    # ------------------- 输入 -------------------
    user_input: str          # 用户的一句话创意 / 故事大纲
    session_id: str          # 会话 ID（用于结果落盘、进度追踪）

    # 运行上下文（用户/会话/职业）：随 State 传递，避免依赖 thread-local。
    # 结构：{"user_id": str, "session_id": str, "role": str}
    # 每个节点进入时会用它绑定上下文（见 app/core/context.py 与 main_workflow._bind_context）。
    run_context: dict

    # 美术资产部独立生成入口：用户填写的「美术设定包」自由文本（无剧本时据此设计角色/场景）。
    art_brief: str
    # 美术设定包解析结果：{characters:[{name,appearance,clothing,accessories}],
    #                        scenes:[{scene_id,location,atmosphere,description}], style}
    art_brief_plan: dict

    # ------------------- Stage 0 故事策划 -------------------
    story_outline: dict      # {title, logline, genre, style, characters[], plot_beats[]}

    # ------------------- Stage 1 编剧 -------------------
    script: dict             # {title, scenes[]: {scene_id, location, atmosphere,
                             #   action[], dialogue[], transition}}
    script_review: dict      # 剧评：{score, strengths[], issues[], suggestions[]}

    # 文学策划(剧评)汇总报告，供独立「文学策划」职业页展示。
    # 格式：{"scores": {"logic": 0-100, "tension": 0-100, "motive": 0-100, "overall": 0-100},
    #       "strengths": [...], "issues": [...], "suggestions": [...],
    #       "history": [{time, score_overall, note}]}
    # 默认：{}
    critic_report: dict

    # 场景设计师(方案B)产物：每个场景的氛围参考图 URL。
    # 格式：{"S1": "https://.../scene_S1.png", ...}
    # 默认：{}
    scene_reference_images: dict

    # 场景设计师(方案B)产物：每个场景的锚定描述。
    # 格式：{"S1": {"setting": "...", "characters_in_scene": [...], "style": "..."}}
    # 默认：{}
    scene_design_profiles: dict

    # 剪辑指导(节奏导演)产物：每个镜头的建议时长 + 节奏标签。
    # 格式：[{"shot_id": "S1-01", "duration": 2.0, "tempo": "fast|medium|slow"}]
    # 默认：[]
    rhythm_plan: list

    # 场记(连续性检查)报告：穿帮/一致性风险列表。
    # 格式：[{"risk": str, "shots": [shot_id, ...], "severity": "high|medium|low",
    #        "suggestion": str, "rule": "1|2|3"}]
    # 默认：[]
    continuity_report: list

    # 发行总监(分发协调)产物：各平台发布状态 + 链接。
    # 格式：{"bilibili": {"platform": "bilibili", "title": "...", "description": "...",
    #        "tags": [...], "status": "draft|publishing|success|failed", "url": "..."}}
    # 默认：{}
    publish_results: dict

    # ------------------- 人工检查点（执行前勾选） -------------------
    # 用户勾选要人工检查的职业 key（如 ["script","assets","storyboard","images","critics",...]）。
    # 空/缺省 = 开启全部默认检查点；未勾选的检查点自动放行。
    enabled_checkpoints: list

    # ------------------- AI 思考过程（零额外 token，节点产出时追加） -------------------
    # 每个节点返回 brief 的「思考/决策」叙述，按阶段累积，供前端「AI 思考过程」面板展示。
    # 格式：[{"label": "阶段名", "text": "思考/决策叙述"}]
    thinking_log: Annotated[list, _append]

    # ------------------- Stage 3 分镜构建 -------------------
    storyboard: list         # [{shot_id, scene_id, shot_type, camera, description,
                             #   duration_s, dialogue, transition}]

    # ------------------- Stage 3.5 提示词编译 -------------------
    prompts: list            # [{shot_id, prompt, negative_prompt, width, height, seed}]

    # ------------------- Stage 4a 图片生成 -------------------
    images: list             # [{shot_id, image_url, local_path, status: pending|ok|failed}]

    # ------------------- Stage 4b 视频生成 / 剪辑 -------------------
    # ⚠️ 视频生成是**独立的异步后台任务**（`POST /api/crew/video/{sid}`），
    # 结果落在**会话 payload** 的 `video` 键里（`{dir_or_path, produced}`），
    # 不在图状态里。这里补这个字段是为了：
    #   · 剪辑师 / 发行总监能从 `state["video"]` 读到它
    #     （`Distributor.run()` 的 docstring 就是按 `video` 写的）；
    #   · 允许"同一次运行里已经有视频"时直接使用。
    # 格式：{"dir_or_path": str, "produced": int, "error": ""}
    # 默认：{}
    video: dict

    # 剪辑师产出的时间线。格式：[{"index": 1, "source": "..."}]；默认 []
    timeline: list

    # 成片。格式：{"dir_or_path": str, "segments": int}；默认 {}
    final_video: dict

    # ------------------- Stage 结果聚合 -------------------
    result: dict             # 最终汇总：{session_id, outline, script, storyboard, images}

    # ------------------- 角色一致性（方案 B） -------------------
    # 每个角色的详细外貌描述，用于在提示词中注入角色锚定信息。
    # 格式：{"角色名": {"appearance": "外观", "clothing": "服饰", "accessories": ["道具"]}}
    # 默认：{}
    character_profiles: dict

    # 每个角色的参考图 URL，用于 IP-Adapter 的参考图输入。
    # 格式：{"角色名": "https://.../character_xxx.png"}
    # 默认：{}
    character_reference_images: dict

    # 每个角色的**16:9 角色设定表**本地路径（正脸放大 + 正/侧/背三视图）。
    # 格式：{"角色名": "D:/.../sheettable_角色名.png"}
    # 默认：{}
    #
    # ⚠️ 这是**给人看的展示交付物**，**不参与下游锚定** ——
    # IP-Adapter 用的是 `character_reference_images` 里的逐视角独立图。
    # 原因：CLIP 把整图编成**一个**向量，4 个视角会混在一起，
    # 拿去生图会出现"画面被脸占据/生成多个副本/角度串味"。
    # 见 `app/agents/stage2/character_sheet_template.py`。
    character_sheets: dict

    # 整体画风参考图 URL（可选），用于锁定整体艺术风格。
    # 默认：""
    style_reference_image: str

    # 风格一致性评分（0-100），由一致性审查 Agent 输出。
    # 默认：0.0
    consistency_score: float

    # 详细一致性报告，包含每个角色的跨镜头一致性评分和问题列表。
    # 格式：{"characters": {"角色名": {"score": 0-100, "issues": [...]}}, ...}
    # 默认：{}
    character_consistency_report: dict

    # ------------------- 白模预演（3D previs） -------------------
    # **AI 生成的预演初稿**（白模预演师的产出）。
    #
    # 为什么要它：视频模型会**自己编空间**（漂移/瞬移/比例失控），
    # 所以要把"谁在哪、镜头怎么走"先摆出来当结构输入。
    #
    # 流程：本字段（初稿）→ 预演编辑器 `#/previs` **人工微调** → 逐帧导出
    #       → `POST /api/video/whitebox` 编码 mp4 → 当参考视频。
    #
    # 结构与前端 `web/src/previs/types.ts` 的 `PrevisScene` 一致：
    #   {"ground": [w, d], "actors": [...], "props": [...], "shots": [...]}
    #
    # 格式：{"scene": PrevisScene, "notes": [自动纠正说明], "attempts": 1|2,
    #        "generated_at": float, "error": ""}
    # 默认：{}
    previs: dict

    # ------------------- 配音（配音指导） -------------------
    # ⚠️ 注册表**声明**配音指导产出 `voice_track` / `bgm_track`，但这两个字段
    # 原先在 CrewState 里**根本不存在** —— 又一次"声明了但没有落点"。
    #
    # 逐镜头的对白音频（edge-tts 免费 / 云端 TTS 按量计费）。
    # 格式：[{"shot_id", "text", "path", "voice", "status": ok|skipped|failed,
    #        "reason": ""}]
    # 默认：[]
    voice_track: list

    # BGM / 音效轨。
    # ⚠️ **目前没有实现**：`music` 能力在提供者表里是空的（已知空缺，见
    # `tests/test_providers.py` 的 `optional = {"music"}`）。
    # 保留字段是为了让"注册表声明"与"状态"一致，将来接上不用改结构。
    # 格式：[{"id", "path", "source", "status"}]
    # 默认：[]
    bgm_track: list

    # 质检总监(质量评估)汇总报告，供独立「质检总监」职业页展示。
    # 格式：{"overall_score": float, "shots": [{shot_id, quality, consistency, status, level}],
    #       "lowest_shots": [...], "failed_shots": [...], "generated_at": float}
    # 默认：{}
    quality_report: dict

    # ------------------- 后期三环的产物与说明 -------------------
    # ⚠️ **这一段是补漏**。2026-10-07 的节点排查发现：`inpainter` /
    # `motion_compositor` / `video_assessor` / `distributor` / `voice_director`
    # 这几个节点返回的键**根本不在 CrewState 里**，而 LangGraph 对未声明的
    # channel **静默丢弃**（`_algo.py`：`wrote to unknown channel …, ignoring it`）。
    #
    # 后果实测：
    #   · `video_report` → 视频质检唯一的产出（几段可检 / 为何没检）全部消失；
    #   · `*_note`（跳过原因）→ 节点被设计成"优雅跳过并**说明**"，
    #     而说明本身被丢掉，用户只看到节点 done、拿不到任何原因；
    #   · `fixed_images` → 注册表声明的产物永远落不了地。
    #
    # **教训**：往图里加节点时，返回的每个键都必须先在 CrewState 里声明，
    # 否则"看起来跑通了、实际数据没了"。有测试守着（见
    # `tests/test_state_fields.py::test_node_return_keys_are_declared`）。

    # 视频质检报告。格式：{"clips": int, "dir_or_path": str, "checked": bool,
    #                       "reason": str}；默认 {}
    video_report: dict

    # 修图师的产出（修复后的画面）。格式同 `images`；默认 []
    fixed_images: list

    # 各节点"为什么跳过"的说明。**必须能传到界面** —— 否则"优雅跳过"
    # 对用户就是"莫名其妙没做"。
    # 格式：str；默认 ""
    motion_note: str
    video_assessor_note: str
    inpainter_note: str
    distributor_note: str
    voice_track_note: str

    # 动态合成师是否强制重跑视频（已有视频时默认不重复生成）。默认 False
    video_regenerate: bool

    # ------------------- 流程控制 -------------------
    current_phase: Annotated[str, _last_value]  # 当前阶段标识（用于进度展示，并行时取最后写入）
    review_point: int        # 当前人工确认点编号 (1 剧本 / 2 资产 / 3 分镜 / 4 图片)
    review_decision: str     # 人工反馈：confirm | retry | edit
    feedback: dict           # 人工修改 / 重试指令
    retry_count: dict        # {node_name: int} 各节点重试次数
    error: str               # 任一环节失败则记录错误信息
    # start_stage: 分段开始。指定要开始生成的环节(如 "screenwriter")，则该环节之前的
    #             各 Agent 一律跳过(不运行)，AI 从 start_stage 往下跑。
    start_stage: str
    # end_stage: 分段结束。指定 AI 执行到的最后一个环节；超过它的环节跳过(不调 AI)，
    #           结果聚合仍会组装已产出的内容。
    end_stage: str
