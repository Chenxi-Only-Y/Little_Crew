# 小剧组 Little Crew — 职业体系 v2（新表）落地对照

> 来源：用户 2026-10-02 给出的新职业表（7 部门 / 16 职业）。
> 用途：把新表与现状逐条对照，标出实现缺口、架构影响、落地顺序与待确认项。
> 前置：`01-usage.md`（现状基线）、`04-frontend-rewrite.md`（前端重写准备）。

---

## 1. 这不是加几个职业，是产品定位升级

| | 现状（v1） | 新表（v2） |
| --- | --- | --- |
| 定位 | LLM 多智能体**文生图流水线**：文本 → 提示词 → 云端 API 出图 → 合成 | **ComfyUI 原生制片流程**：含**节点工作流搭建、LoRA 训练、ControlNet、白模预演、配音、节奏剪辑** |
| 部门 | 5 | **7** |
| 职业 | 13 | **15** |
| 核心生产者 | 云端图像 API（MiniMax / DashScope） | **ComfyUI + LoRA + ControlNet**（本地/可控） |
| 音频 | 无（`video.py` 里 Edge TTS 是 `pass # TODO`） | **配音指导**（TTS 音色 / 情感 / BGM / 音效） |
| 预演 | 无 | **白膜预演师**（白模场景、走位、机位、预演视频） |
| 工作流本体 | 写死在 `main_workflow.build_graph()` | 由**工作流架构师**作为一等职业来迭代维护 |

**判断**：v1 的 23 节点图（文本链路）只是 v2 的一个**子流程**。
v2 新增的重型环节集中在**本地 GPU 长任务**（LoRA 训练、批量生图、ControlNet、白模渲染、TTS），
这些与现有「秒级 mock 流转 + 同步 HTTP」的执行模型差异很大，需要独立的**任务/队列/取消**能力。

---

## 2. 新表（7 部门 / 16 职业）

| # | 部门 | 职业 | 英文 | 系统 key | 职责 |
| --- | --- | --- | --- | --- | --- |
| 0 | **基建** | 工作流架构师 | workflow_architect | `workflow_architect` | 搭建、迭代、维护主工作流（LangGraph 节点图），定义节点接口与数据格式 |
| 0 | 基建 | 技术指导 | technical_director | `technical_director` | 模型选型、LoRA 训练调优、视频生成参数优化 |
| 1 | **策划** | 策划总监 | showrunner | `showrunner` | 定调、统筹全流程、市场定位 |
| 1 | 策划 | 文学策划 | script_editor | `script_editor` | 剧本评估、修改意见、IP 改编 |
| 1 | 策划 | 编剧 | screenwriter | `screenwriter` | 生成结构化剧本、分镜脚本 |
| 1 | 策划 | 分镜师 | storyboarder | `storyboarder` | 拆解机位、景别、运动、时长、走位 |
| 2 | **资产** | 角色设计师 | character_designer | `character_designer` | 角色三视图、LoRA 训练、角色圣经 |
| 2 | 资产 | 场景设计师 | scene_designer | `scene_designer` | 场景概念图、白模场景搭建 |
| 3 | **生图** | 摄影师 | photographer | `photographer` | 提示词编写、ControlNet 控制、批量生图 |
| 3 | 生图 | 质检总监 | quality_assessor | `quality_assessor` | 抽卡筛选、修复崩坏画面（手/脸/闪烁） |
| 4 | **白膜** | 白膜预演师 | previs_artist | `previs_artist` | 搭白模、摆走位、录机位、导出预演视频 |
| 5 | **动态** | 场记 | continuity | `continuity` | 角色一致性、场景连贯性、道具连贯性检查 |
| 5 | 动态 | 配音指导 | voice_director | `voice_director` | TTS 音色选型、情感提示词、BGM/音效匹配 |
| 6 | **后期** | 剪辑指导 | supervising_editor | `supervising_editor` | 节奏把控、镜头组接指导 |
| 6 | 后期 | 剪辑师 | editor | `editor` | 音画对齐、转场、字幕、输出 |
| 6 | 后期 | 发行总监 | distributor | `distributor` | 多平台分发、数据回收、运营策略 |

> 注：新表 #1 中「分镜师」归在**策划**部门（原属导演分镜部），部门归属已变更。

---

## 3. 新旧对照与实现缺口

评级：**✅ 可基本复用** / **🔶 需扩展** / **🆕 全新** / **♻️ 仅改名或归部门**

| 新职业 | 现状对应 | 评级 | 差距说明 |
| --- | --- | --- | --- |
| 工作流架构师 | 无（能力散在 `build_graph()` + `_STAGE_ORDER`） | 🆕 | 要把「图」变成一等对象：节点接口/schema、拓扑校验、版本化、可视化编辑。**这是导演台画布的后端基座** |
| 技术指导 | `PromptCompiler`（`prompt_refiner.py`，只做提示词编译） | 🔶 大 | 新增模型选型、**LoRA 训练调优**、视频参数优化 → 需要训练任务编排 + 参数搜索 |
| 策划总监 | `StoryPlanner`（`planner.py`，一句话→大纲） | 🔶 | 新增**统筹全流程 + 市场定位** → 需要全局状态视角与定位产出 |
| 文学策划 | `ScriptCritic`（`critics.py`，评分+建议） | 🔶 | 新增 **IP 改编** → 需要改编方案产出 |
| 编剧 | `Screenwriter`（`writer.py`） | ✅ | 职责一致（"分镜脚本"或与分镜师重叠，待澄清） |
| 分镜师 | `Storyboarder`（`shot_composer.py`） | ✅ | 现有已含机位/景别/时长；新增"运动/走位"需扩字段 |
| 角色设计师 | `CharacterDesigner`（三姿态参考图+外貌） | 🔶 | 新增 **LoRA 训练 + 角色圣经** → 需要训练数据集导出与训练任务 |
| 场景设计师 | `SceneDesigner`（氛围参考图，自然语言 prompt） | 🔶 | 新增**白模场景搭建** → 需从文生图转向 3D 白模资产 |
| 摄影师 | `ImageGenerator`（并行生图+IP-Adapter+降级链） | 🔶 | 新增 **ControlNet 控制 + 批量抽卡** → 需要 ControlNet 输入通道与批量策略 |
| 质检总监 | `QualityAssessor`（CLIP 评分+一致性） | 🔶 | 新增**崩坏修复**（手/脸/闪烁）→ 需要 inpaint/修复闭环（**当前 local-gen 无 inpaint 能力**） |
| 白膜预演师 | 无 | 🆕 | 白模、走位、机位录制、预演视频导出 —— 全新管线 |
| 场记 | `ContinuityChecker`（规则为主，LLM 可选） | 🔶 | 扩展到**角色/场景/道具**三类一致性；现有仅文本穿帮检查 |
| 配音指导 | 无（`video.py` 里 Edge TTS 是 `pass # TODO`） | 🆕 | TTS 音色/情感/BGM/音效四件事，全新增 |
| 剪辑指导 | `RhythmDirector`（情绪→tempo 查表，0.5–5s，60s 上限） | 🔶 | 升级为**镜头组接指导**（现有只是时长建议表） |
| 剪辑师 | 视频合成服务（`video.py`：FFmpeg 幻灯片 / MiniMax I2V） | 🔶 大 | 新增**音画对齐、转场、字幕** → 需真实时间线编辑，非幻灯片拼接 |
| 发行总监 | `Distributor`（骨架，`_real_upload` 抛 NotImplementedError） | ✅ | 职责一致（"数据回收/运营策略"需扩） |

**缺口统计**：✅ 2 个 / ♻️ 0 / 🔶 11 个 / 🆕 3 个（工作流架构师、白膜预演师、配音指导）
**净新增职业**：2（白膜预演师、配音指导）+ 2 个重构为职业（工作流架构师、技术指导）

---

## 4. 对现有架构的影响（按严重度）

### 4.1 节点图规模与拓扑（高）
- 业务节点：23 → **预计 30+**（新增 previs、voice、LoRA 训练、ControlNet、字幕/转场等）。
- 现有 `main_workflow.py` 的 `_STAGE_ORDER` 是**线性 13 环节**；v2 出现**非线性**需求：
  LoRA 训练要在角色设计后、生图前；白模预演要在分镜后、生图前（或并行）；
  配音依赖最终剪辑时长，剪辑又依赖配音时长 → **存在来回依赖，需要引入"迭代/回环"或"分阶段提交"**。
- 结论：v1 的「一次性线性图」必须演进为**可编排 DAG + 可回环**，这正是导演台画布要解决的问题。

### 4.2 长任务执行模型（高）
现状 `task_runner`：后台线程 + 进程内 dict（`_JOBS/_THINKING/_CANCEL`），任务时长以**秒**计。
v2 新增任务时长以**分钟~小时**计：
| 新任务 | 预估耗时 | 现有能力 |
| --- | --- | --- |
| LoRA 训练 | 分钟~小时 | ❌ 无 |
| 批量生图（抽卡） | 分钟级（N 张） | 🔶 有并发(4线程)但无队列 |
| ControlNet 批量 | 分钟级 | ❌ 无 |
| 白模预演渲染 | 分钟级 | ❌ 无 |
| TTS + 混音 | 秒~分钟 | ❌ 无 |
- 结论：需要**任务队列 + 进度持久化 + 取消 + 断点**（现 `MemorySaver` 已在 `01-usage.md §6.1` 暴露过问题）。

### 4.3 前端画布（中）
- 节点从 13 → **15**，且需支持**新端口类型**：`model`（模型/LoRA）、`pose/motion`（走位）、
  `audio`（音色/BGM）、`timeline`（剪辑时间线）、`3d`（白模）。
- 端口兼容矩阵要扩：现有 `domain/ports.ts` 设计需加入上述类型。
- 新节点需要**专属控件**：LoRA 训练（数据集/步数/学习率）、ControlNet（控制图/权重）、
  白模（3D 视口）、配音（音色选择/试听）、剪辑（时间线）。

### 4.4 provider 层（中）
- 现有 provider：`local`(Z-Image) / `dashscope` / `minimax` / `replicate` / mock。
- v2 还需要：**ComfyUI**（工作流执行）、**LoRA 训练器**、**TTS**（Edge TTS / 其它）、
  **3D/白模渲染**。→ 印证了 `02-refactor-plan.md §2.6` 的 provider 注册表必须做，且键要能扩。

### 4.5 产物与数据模型（中）
- 现有 `Shot` 实体需扩：`controlImage`、`lora`、`pose`、`audio`、`subtitle`、`transition`。
- 新增实体：`CharacterBible`（角色圣经）、`LoraModel`（训练产物）、`Previs`（白模预演）、
  `AudioTrack`（配音/BGM）、`Timeline`（剪辑时间线）。

### 4.6 与重构计划的关系（重要）
新表**不推翻**既有方案，反而**强化**了三件事的必要性：
1. **单一注册表**（`02-refactor-plan.md §2.2`）——现在注册表字段必须扩到
   `comfyuiNodes` / `trainable` / `portTypes`，否则 16 个职业 × 多视角一定失控。
2. **provider 抽象**（§2.6）——ComfyUI / LoRA / TTS 都是新增实现，不该改核心。
3. **导演台画布**（`03-before-after.md`）——v2 的非线性拓扑**只有画布能表达**，
   一堆职业页面更不可能承载。

---

## 5. 建议的落地顺序（v2 增量，叠加在 P0–P6 之上）

| 阶段 | 内容 | 为什么这个顺序 |
| --- | --- | --- |
| **V0** | 把新表落成**注册表数据**（16 职业 / 7 部门 / 端口 / 配置键），前端只读渲染 | 不动业务逻辑就能先看到 v2 的骨架；也是画布的输入 |
| **V1** | 改名与归部门的机械迁移（`showrunner`/`script_editor`/`supervising_editor` 等），保持行为不变 | 低风险、可全量回归；先消除命名债 |
| **V2** | **Workflow As Object**：节点接口/schema 化 + 拓扑校验 + ComfyUI 节点映射（工作流架构师落地） | v2 的地基，后续所有新节点都挂在它上面 |
| **V3** | 长任务基建：任务队列 + 进度持久化 + 取消（LoRA/批量生图/预演/TTS 共用） | 三种长任务同时需要，先做基建再各自接入 |
| **V4** | 三条新管线：**LoRA 训练**（角色设计师/技术指导）、**ControlNet 生图**（摄影师）、**白模预演**（白膜预演师） | 依赖 V2+V3 |
| **V5** | 音频与剪辑：**配音指导** + **剪辑师时间线**（音画对齐/转场/字幕/输出） | 依赖最终时长，天然靠后 |
| **V6** | 导演台画布前端（F0–F5，见 `04-frontend-rewrite.md`）承载 v2 全部节点 | 画布是能力的最终呈现 |

> 与既有计划的关系：`P0 护栏 → P1 止血 → P2 注册表` 仍然先做（V0/V1 就是 P2 的扩展），
> 然后 V2/V3 基建，再 V4/V5 新管线，最后 V6 画布。**顺序不变，只是范围变大。**

---

## 6. 已确认决策（2026-10-02 用户答复）

| # | 问题 | 决策 | 影响 |
| --- | --- | --- | --- |
| D1 | 主执行引擎 | **不是 ComfyUI**，用本项目自己的工作流（LangGraph 主图） | 「工作流架构师」职责 = 维护 **LangGraph 主图**：节点接口、数据格式、拓扑校验、可视化编辑。**不引入 ComfyUI 依赖**；ComfyUI 仅作为将来可选的 provider |
| D2 | LLM 的本地/云端 | **全项目 LLM 一律支持「API + 本地」两种** | 现有 11 家预设已含 `ollama`/`vllm` 本地实现（`llm.py:_LOCAL_PROVIDERS`），需把这个能力**统一为所有环节的默认能力**，并在注册表/属性面板暴露为「本地/云端」开关 |
| D3 | 白模形态 | **先全部 2D** | `previs_artist` 先做 2D 走位/机位示意图（PIL 绘制，零新依赖）；3D 作为同职业第二种模式后续接入 |
| D4 | LoRA 训练 | **优先本地低显存**（技术已就绪），保留 API 路线 | 见 §6.1（重要发现：本机已具备 QLoRA 训练条件） |

### 6.1 LoRA 训练调研结论（实测）

**核心发现：本机已具备 Z-Image LoRA 训练的完整条件**，不需要额外买服务。

| 证据 | 内容 |
| --- | --- |
| DiffSynth-Studio 训练框架 | `diffsynth/diffusion/training_module.py` 的 `add_lora_to_model()` 基于 **peft**；另有 `auto_detect_lora_target_modules()`、`mapping_lora_state_dict()` |
| Z-Image 训练例子 | `examples/z_image/model_training/`：`train.py` + `lora/`（6 个模型的正规 LoRA 训练脚本）+ `validate_lora/`（训练后验证） |
| **低显存专用脚本** | `model_training/special/quant_training/Z-Image-Turbo-bitsandbytes_nf4.sh` —— 官方 **NF4 量化训练**配方 |
| 量化训练支持 | `diffsynth/core/quant/base.py` 明确写着支持「gradients pass through frozen quantized layers to reach LoRA branches」，并提供 `check_differentiable()` 验证梯度能否穿过量化层 |
| 环境依赖（local-gen venv） | `peft 0.21.0`、`bitsandbytes 0.50.2`、`accelerate 1.15.0`、`diffusers 0.40.0`、`torch 2.9.1+cu128` |
| GPU | RTX 4060 Laptop **8GB**（8188 MiB），驱动 591.74 |

**结论**：
- **本地可行**：走 NF4 QLoRA（`bitsandbytes` + `peft`），8GB 上可训。代价是慢（分钟~小时级），
  适合小数据集（15–30 张三视图 + 打标 caption）。
- **需补的只有一层**：`local-gen` 服务目前**只暴露推理路由**（5 个），训练要作为
  **独立的训练任务/路由**加入（或离线脚本 + 任务队列）。
- **API 路线**：公共 LoRA 训练 API（Replicate / fal.ai / Fireworks / Magnific 等）普遍支持
  **SDXL / Flux 底模**，**未必支持 Z-Image-Turbo**。⚠️ **LoRA 与底模绑定**——用 SDXL 训的 LoRA
  不能给 Z-Image 用。所以 API 路线的实际意义是「换底模」，需要取舍。

**建议**：与 D2 保持一致——把 LoRA 训练也做成 **provider（`lora_trainer`）**，
先实现 `local`（DiffSynth QLoRA），API 实现按需再加。这样与「LLM 支持 API + 本地」的思路统一。

### 6.2 额外发现：ControlNet 也已就绪

`photographer` 职责里的 **ControlNet 控制**同样是现成的：
- 底模：`PAI/Z-Image-Turbo-Fun-Controlnet-Union-2.1`（含 8steps 版）
- 推理例子：`examples/z_image/model_inference/Z-Image-Turbo-Fun-Controlnet-Union-2.1*.py`
- 低显存版本：`model_inference_low_vram/` 下有对应脚本

即「摄影师」的 ControlNet 能力**无需新依赖**，只需在 `local-gen` 暴露参数并接到 provider 层。

---

## 7. 仍待确认

1. **崩坏修复**（质检总监）：`local-gen` 当前**没有 inpaint/img2img 路由**。
   要做"修复手/脸/闪烁"必须新增 inpaint（技术上现成：本机 `diffusers` 自带
   `ZImageInpaintPipeline`，与现有权重共用、无需再下模型）。**是否在 v2 加入？**
2. **分镜师的"走位"与白膜预演师的重叠度**：走位/机位由谁定义、谁执行？两者需划清边界，
   否则会互相覆盖（建议：分镜师出「走位意图」，白膜预演师出「可执行的白模走位」）。
3. **是否先做 V0**（把 16 职业落成注册表 + 前端只读渲染），让你**看到 v2 骨架**再决定深度？

---

## 8. 我的建议

**先说一件事**：v2 的野心（ComfyUI 原生 + LoRA 训练 + 白模预演 + 配音剪辑）比 v1 大一个数量级，
而 v1 目前**还有 6 个未修的实测缺陷**（`01-usage.md §6`，其中最严重的确认点重启重放会重复烧钱）。

建议路径：
1. **先 `git commit` 锁定 v1 基线**（D2，仍未定）；
2. 做 **P0 护栏 + P1 止血**（约 1–2 天量级）——把地基建在能回退、有测试的状态上；
3. 做 **V0 + V1**（新表落注册表 + 改名迁移）——你会立刻看到 v2 骨架，且全部可回归；
4. 之后再按 §6 的答复决定 V2/V3 的深度。

这样每一步都有可用产物、可回退，不会出现"改了半个月发现方向不对"的情况。
