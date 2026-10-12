# 《基于多智能体协作的 AIGC 漫剧生成平台》GitHub 同类项目调研

调研时间：2026-10-11 · 数据来源：GitHub API + 源码级核验（非仅 README）

---

## 一、结论先行

**有，而且很多——但你要做的那件事，恰好没人做全。**

GitHub 上"AI 漫剧 / 短剧 + 多智能体"项目已形成三个互不重叠的阵营：

| 阵营 | 代表 | 特征 | 是否用 ComfyUI 做执行引擎 |
|---|---|---|---|
| A. 平台层（画布/Agent 工作台） | TapCanvas、wind-comic、novella | 视觉编排强、Agent 强，**全部走云端 API** | ❌ 源码级核实：**零 ComfyUI 接入** |
| B. ComfyUI 驱动层（工作台） | NiliX、kt-ai-Studio、Go-Ai-Studio、comfyui-auto-drama | ComfyUI 深度集成，但编排停留在**模板填充** | ✅ 但只改 JSON 参数 |
| C. ComfyUI 原生节点 | ComfyUI-Novel-Director | 直接活在节点图里，但**没有编排 DAG** | ✅ 但无编排层 |

**核心空白（你的机会）：**
> 「可视化节点编排」+「Agent 原生执行」+「ComfyUI 作为执行引擎」三者同时满足的开源项目，**目前不存在**。
> A 阵营有前两者、没有第三；B/C 阵营有第三、没有前两者。

---

## 二、阵营 A：平台层项目（最像你的形态，但都不用 ComfyUI）

### A1. [TapCanvas](https://github.com/anymouschina/TapCanvas) — 最接近，也最强
`653 ★ / 164 fork / TypeScript+Go / MIT / 6200 files / 2026-10-09 仍活跃`

- **无限画布**：强类型节点 + 端口 + 连线，组织文档/角色/分镜/图像/视频/音频/字幕/工作流
- **Agent 原生执行**：`POST /public/agents/chat` 统一入口 → **DeepSeek Harness Bridge**（就是你正在用的 DSH）
- **生产工作流内核**：`packages/schemas/workflow-kernel-protocol/` 有 40+ 个共享契约（canvas-plan-protocol、shot-table-protocol、sequence-timeline、generation-contract、workflow-asset-registry…）
- **工程可用性极强**：任务账本、队列、租约、幂等、重试、reconcile、交付证据（delivery evidence）
- ⚠️ **它不驱动 ComfyUI**，模型走自建网关（鲁班 API / new-api）
- 💡 **参考价值最高**：`packages/schemas/*` 那套契约设计，几乎可以直接作为你的数据模型蓝本

### A2. [wind-comic](https://github.com/ChrisChen667788/wind-comic) — 工程质量标杆
`695 ★ / TypeScript / MIT / 2415 files / 6539 tests passing`

- 8 个专业 Agent（Writer→Director→StyleBible→CharDesigner→SceneDesigner→Storyboard→Video→TTS→Lipsync→Editor），用**严格一致性契约**交接
- **Vision-Audit 重试闭环**：镜头打分 < 70 自动重生成
- **多引擎竞速**：Seedance/Kling/Veo/Vidu 同时提交，先出好片者胜
- **Sketch-Lock 分镜**：先出黑白构图草图，再锁定构图生成正片（解决"扩散抽奖"毁掉镜头语言）
- Provider-agnostic，文档明确说"同时支持 ComfyUI"——但只是**把 ComfyUI 当 12+ 个生图引擎之一**，不是架构底座
- 💡 **参考价值**：一致性契约的字段设计、"诚实降级"（失败的镜头标注为 animatic，绝不用静图冒充视频）

### A3. [novella](https://github.com/Agions/novella) — 架构概念可取，README 需打折看
`123 ★ / Tauri v2 + React 19 + Rust / MIT / 920 files`

- **Hub-and-Spoke 多智能体架构** + **ProjectBlackboard 共享黑板**
- 声明式自定义 Agent 插件（UI 注册 Agent：名称/触发阶段/系统 Prompt）
- ⚠️ **诚实提醒**：该 README 引用的模型（DeepSeek-V4、Qwen 3.8-Max、混元 Hy3、GPT-5.6、Kling 3.0）与时间线存在明显虚构/AI 生成痕迹，营销成分高
- ⚠️ **源码级核实：604 个代码文件中"comfy"零命中** —— 它是纯云 API 项目
- 💡 可取：Blackboard 共享状态 + AgentRegistry 扩展机制的概念

### A4. [ai-comic-studio](https://github.com/fandongxu056-prog/ai-comic-studio) — 设计文档满分，实现半成品
`0 ★ / FastAPI + LangGraph + Celery / 125 files / 仅 3 个测试`

- **四阶段管线设计非常完整**，值得逐条抄：
  - Stage1-3 用 Multi-Agent 审查循环（编剧/剧评/风格 → 编排器 → 人工确认点），Stage4 用确定性 Pipeline
  - **QC-5 五维品控模型**：完整性 25% / 一致性 40% / 品质感 25% / 可执行性 / 合规性，≥80 自动通过，65-79 人工快审，<65 必须修订，**最多 3 轮迭代**
  - **跨阶段连续性契约**（S1→S2 角色覆盖率、S2→S3 种子连续性、S3→S4 对白时间码）
  - **Seed 体系贯穿 Stage2→4**：全局种子→角色种子→镜头种子
  - 结构化 ID：`CHAR-0001`、`SH-E001-S003-005`
- ✅ 代码是**真实实现**（LangGraph 图表、22KB 编剧 Agent、真实 LLM 调用、Celery 任务、MinIO/Postgres）
- ⚠️ 但 0 star、未完成、无 ComfyUI
- 💡 **参考价值**：这是全网找到的**最值得抄的设计文档**，尤其 QC-5 与跨阶段契约

---

## 三、阵营 B：ComfyUI 驱动型工作台（用了 ComfyUI，但编排很浅）

### B1. [NiliX](https://github.com/YiIimini/NiliX) — ComfyUI 工程化最深，但私有协议
`3 ★ / Go 单服务 / 24657 KB / **私有项目，仅限本机使用**`

**这是技术含量最高的 ComfyUI 集成案例**，值得当成"工程答案"来读：

- **进程托管**：ComfyUI 由本服务代管启停，端口 8190，自包含目录（拷贝即迁移）
- **六阶段流水线**：方案 → 资产 → 编码 → 渲染 → 质检 → 合成，全阶段幂等、断点续跑
- **智能体审片闭环**：剧本师复核 → 审片官逐镜 VLM 判分 → 修复师自动改写提示词定点返工 → 终审预算耗尽自动拍板 → 微信升级通知
- **审片八维度加权**（主体 20/场景 12/动作 15/运镜 10/可见性 15/技术 15/风格 8/口型 5），**加权分在 Go 侧计算，不信任模型自报** ← 这个细节极其关键
- **两阶段条件缓存**：条件指纹 = md5(提示词+角色+场景+画幅+帧数)，草稿/定稿分辨率各自独立缓存
- **产物时效清单**：每镜记录全部输入指纹（资产指纹、定妆照 mtime、视图代数），current/stale/missing 四态，换定妆照自动标 stale 重渲
- **渲染检查点**：prompt_id 提交即落盘，崩溃重启先查 ComfyUI history 收回已完成任务，**绝不重复烧 GPU**
- **解析器代数指纹** `script_parse_ver`：解析器升级强制重解析旧产物，杜绝"坏方案永久复用"
- 角色资产：形象指纹 sha256 跨项目复用零渲染；双形态契约；Q 版多视图；YuNet 人脸检测中心裁剪
- ⚠️ 私有协议，**不能直接用**，但机制可以全部重写

### B2. [kt-ai-Studio](https://github.com/oskey/kt-ai-Studio) / [Go-Ai-Studio](https://github.com/oskey/Go-Ai-Studio) — 模板填充范式
`90 ★ / FastAPI+Jinja2 / **无 LICENSE 文件**`

- 机制：内置 workflow JSON 模板（`wf_base_character`、`wf_8views`、`image_z_image_turbo`、`video_wan2_2_14B_i2v`、`video_ltx2_i2v`），**通过固定节点 ID 注入**正向/负向提示词、Seed、宽高、时长
- 作者原话："**只需保证以下节点 ID 与项目模板一致即可**" ← 这就是模板填充范式的天花板
- 双向 WebSocket 通信，支持排队/中断/进度实时监控
- 画风锁定 Style Guard、角色多视角三视图固定特征、场景指纹、LLM 自动规划多人站位（前景/中景/远景）
- Go-Ai-Studio 是重写版，扩展探店/讲解/配音/语音克隆
- ⚠️ **无 LICENSE = 保留所有权利，商用有法律风险**；Go-Ai-Studio README 自述"实际出图和视频生成尚未进行人工测试"

### B3. [comfyui-auto-drama](https://github.com/qiukaihui/comfyui-auto-drama) — 控制台零依赖
`MIT / Python 标准库 / 演示教学用途`

- 控制台本体零第三方依赖，走远程 ComfyUI（H3 ref2va）
- **失败码体系**：F-SUBMIT-API / F-TIMEOUT / F-COUNT 自动打码定位 ← 值得借鉴
- **链式衔接**：上段末帧接下段首帧
- **R2V 权重检测回退**：提交时检测服务器是否已加载 Ref2VA 权重，缺失则回退 FL2VA 并在界面提示"身份锁定弱" ← 诚实降级的好例子
- 提示词方法论参考 Higgsfield 开源长片《Hell Grind》：角色资产化 / 七层提示词 / 镜头契约 / 失败诊断

---

## 四、阵营 C：ComfyUI 原生节点（真正"活在节点图里"）

### C1. [ComfyUI-Novel-Director](https://github.com/Work-Fisher/ComfyUI-Novel-Director)
`MIT / ComfyUI 自定义节点包`

- 纯节点实现整套漫剧流程：演员选角(6人,可串联无限扩展) → 有声剧脚本加载 → 分镜脚本加载 → 视频提示词加载 → **场景处理器(Iterator)** → 时长计算器 → 实时存档 → 最终合并
- 输入是 **LLM 生成的三份 JSON**（Audio JSON / Visual JSON / Video JSON）——**JSON 就是节点间的契约**
- `Scene Iterator` 机制自动对齐文本/语音/画面/运镜，实现"一键生成 100 集"
- 自动为台词加 `角色名:` 前缀适配 Qwen-TTS；输出角色字典供 GPT-SoVITS 建音色库
- **这证明了一件事：Agent 语义完全可以在 ComfyUI 节点图里表达** —— 但它没有上层编排 DAG，图是人手搭的

### C2. 相关节点生态
- [comfyui_LLM_party](https://github.com/8dw4522b9k-cyber/comfyui_LLM_party_arc)（原 heshengtao，AGPL-3.0）：ComfyUI 里做 LLM Agent 框架，支持 MCP server、多 Agent 放射/环形交互、RAG/GraphRAG、QQ/飞书/Discord 接入。**注意：这是镜像 fork，0 star，原项目已停止维护**
- [comfyui-mcp](https://github.com/yutianxiao6/comfyui-mcp)（Apache-2.0, 4★）：通用 MCP server + Agent Skill，用于构建/校验/运行/管理 ComfyUI 工作流 —— **让 LLM 直接操作 ComfyUI 图**
- [comfyui-runpod-agentic-nodes](https://github.com/ssube/comfyui-runpod-agentic-nodes)：在 ComfyUI 里可视化设计/规划/启动/监控 typed agent workspaces

---

## 五、学术与标准（写论文/答辩必备）

| 文献 | 价值 |
|---|---|
| [**ComfyBench** (CVPR 2025)](https://github.com/xxyQwQ/ComfyBench) | 首个"LLM Agent 在 ComfyUI 中自主设计协作式 AI 系统"的基准。**直接为你的选题提供学术合法性** |
| [**GenAgent** (上海 AI Lab)](https://github.com/xxyQwQ/GenAgent) | 用自动工作流生成构建协作式 AI 系统，ComfyUI 案例研究。含 Workspace 概念（存当前工作流 + 自然语言描述 + 工作状态） |
| [ComfyUI 官方 SDK 设计文档](https://docs.comfy.org/zh/development/api-development/sdks-design) | 官方对"如何在 ComfyUI 之上做上层抽象"的设计取舍 |

---

## 六、竞品矩阵

| 项目 | ★ | 多 Agent | 可视化编排 | Agent 原生 | **真用 ComfyUI 执行** | 一致性机制 | 可观测/回溯 | 许可 |
|---|---|---|---|---|---|---|---|---|
| TapCanvas | 653 | ✅ | ✅ 无限画布 | ✅ | ❌ | 资产库+版本 | ✅ 交付证据 | MIT |
| wind-comic | 695 | ✅ 8 角色 | ⚠️ 导演台 | ✅ | ❌ 仅作引擎之一 | 一致性契约+Vision-Audit | ✅ 健康报告 | MIT |
| novella | 123 | ✅ Hub-Spoke | ⚠️ 编辑器 | ✅ 黑板 | ❌ | IP-Adapter 锚点 | ⚠️ | MIT |
| ai-comic-studio | 0 | ✅ 审查循环 | ❌ | ⚠️ LangGraph | ❌ | QC-5+跨阶段契约 | ✅ review_history | MIT |
| **NiliX** | 3 | ✅ 审片闭环 | ❌ | ⚠️ 固定流程 | ✅✅ 最深 | 指纹+时效清单 | ✅ agent_state | 私有 |
| kt-ai-Studio | 90 | ⚠️ 仅 LLM 调用 | ❌ | ❌ | ✅ 模板填充 | 三视图+场景指纹 | ⚠️ 日志 | **无** |
| Go-Ai-Studio | — | ⚠️ | ❌ | ❌ | ✅ 模板填充 | 同 kt | ⚠️ | 见仓库 |
| comfyui-auto-drama | — | ⚠️ | ❌ | ❌ | ✅ | 参考资产+质检 | ✅ 失败码 | MIT |
| Novel-Director | — | ❌ | ✅ 节点图 | ❌ | ✅✅ 原生 | 选角+IP-Adapter | ❌ | MIT |
| ARIS-Movie-Director | 67 | ✅ 跨模型辩论 | ❌ | ✅ | ❌ | **确定性 token-diff 门禁** | ✅ 198 节点 wiki | MIT |
| **你的目标** | — | ✅ | ✅ | ✅ | ✅ | ? | ? | ? |

---

## 七、可复用资产清单（按性价比排序）

### 直接可用（MIT，可 fork 改）
1. **TapCanvas 的 `packages/schemas/`** — 40+ 个前后端共享契约（会话计划、分镜表、时间线、生成契约、资产注册表）。**数据模型基本不用自己设计**
2. **ai-comic-studio 的四阶段设计 + QC-5 评分模型 + 跨阶段契约** — 文档级参考，代码可读
3. **ComfyUI-Novel-Director 的 JSON 契约设计** — 三份 JSON 驱动整条流水线，是"Agent 输出 → 节点图输入"的最小可行范式

### 机制可重写（协议不允许直接用，但思想照搬）
4. **NiliX 的六大工程机制**：条件缓存 → 产物时效清单 → 渲染检查点 → 解析器代数指纹 → 加权审片不信任模型自报 → 断点续跑幂等。**这六条决定平台能不能真正批量生产**
5. **wind-comic 的 Sketch-Lock 分镜 + 多引擎竞速 + 诚实降级**
6. **ARIS 的"不自我签收"门禁**：生成图形的模型不能自己审自己，必须跨模型盲读 + 确定性 diff 才能 KEEP

### 学术引用
7. ComfyBench (CVPR 2025) + GenAgent — 论证"LLM Agent 编排 ComfyUI"是已被验证的研究方向

---

## 八、给你的差异化建议

### 真正的空白（三块）

**空白 1：双层同构 DAG（最核心的原创点）**
现有项目的 Agent 编排层和 ComfyUI 执行层是**两套割裂的东西**——Agent 层吐出一个 workflow JSON，塞给 ComfyUI 就完事。没有人做**统一中间表示（IR）**：
```
语义图（角色/镜头/连续性）  ←→  统一 DAG IR  ←→  ComfyUI 节点图
```
同一个 IR 既能表达"这个镜头要保证角色一致性"这种语义约束，又能编译成 ComfyUI 可执行的 prompt graph。**这是你"自研 Transformer Agent + 节点化架构"最该发力的地方。**

**空白 2：Transformer Agent 是无人区**
所有竞品的 Agent 都是 LLM 提示词或 LangGraph 状态机。**没有任何项目自研 Transformer 架构的调度 Agent。** 但要清醒：
- 别做"自己训一个 LLM 写剧本"——打不过 DeepSeek
- 该做的方向是 **① 调度决策**（何时分支、何时重试、选哪个引擎、预算怎么分配）**② 一致性嵌入**（把角色/风格编码成向量而非提示词，用于跨镜头检索与校验）**③ 轻量验证模型**（快速判断产出是否可用，替代昂贵的 VLM 审片）
- 这三块才是有数据可训、有指标可量化的

**空白 3：连续性作为可验证契约**
只有 ARIS 一家在认真做"确定性门禁"。现有漫剧项目的一致性检查基本是"让 VLM 看一眼说行不行"——**同一体系自证**。你可以做：确定性 token-diff + 跨模型盲读 + 硬门禁（不过不准进下一阶段）

### 差异化定位一句话
> 别人做的是「Agent 排流程 → 云端 API 出片」或「人手搭 ComfyUI 图 → 批量跑」。
> 你做的是 **「语义级节点编排 → 自动编译成 ComfyUI 执行图 → 确定性验收 → 回写黑板」的闭环**。

---

## 九、风险提醒

1. **别重复造 wind-comic / TapCanvas 已经做完的东西**（画布 UI、多引擎接入、FFmpeg 合成）——那是几千小时的工程量，且它们已开源
2. **ComfyUI 的 API 不是稳定契约**：节点 ID 会随自定义节点版本漂移（kt-ai-Studio 的痛点）。你的 IR → 节点图编译层必须有**版本探测 + 缺失回退 + 显式降级提示**
3. **GPU 成本是真正的约束**：NiliX 的缓存/时效清单/检查点三件套不是优化项，是**能不能跑通一集**的生死线
4. **`kt-ai-Studio` 无 LICENSE，`novella` 的 README 有虚构成分** —— 引用前先核实
5. ComfyBench 已把"LLM Agent 自主设计 ComfyUI 工作流"做成基准，**你的论文/答辩必须引用并说明差异**，否则会被质疑创新性

---

## 十、Z-Image 生态调研：两条被忽略的官方能力（2026-10-11）

> **本章记录一次推翻了本项目两条既有结论的调研。**
> 起因是排查"角色一致性"，结果发现了**空间一致性**的解。
> 方法论上的教训写在第 10.5 节。

### 10.1 起因与结论

**起因**：角色跨镜不一致（§5.7 实测：20 个"少女"各不相同），
排查参考图注入的可行性。

**结论（两条，都改变了方案）**：

| # | 发现 | 影响 |
|---|---|---|
| **1** | **Z-Image 有官方 ControlNet**（`PAI/Z-Image-Turbo-Fun-Controlnet-Union-2.1`，56,423 下载，lite 版 **1.92 GB**） | **P3 白模控制不再需要等 API**，下载量从 39.46 GB 降到 1.92 GB |
| **2** | **`TextEncodeZImageOmni` 支持参考图**（`image_encoder` + `vae` 双路径），训练脚本引用 `Z-Image-Omni-Base` + SigLIP | **本地参考图能力"架构上可行"** —— 推翻了此前"架构级缺失"的判断 |

### 10.2 发现一：Z-Image ControlNet（已核实）

| 变体 | 体积 |
|---|---|
| Union 2.1（完整） | 6,401.5 MB |
| **Union 2.1 lite** | **1,923.2 MB** ⭐ |
| Tile 2.1 lite | 1,923.2 MB |
| Union 2.1-8steps | 6,401.5 MB / 1,923.2 MB 两档 |

**与 H3 方案的对照**：

| 维度 | H3 ControlNet | **Z-Image ControlNet** |
|---|---|---|
| 下载量 | 39.46 GB | **1.92 GB**（差 **20.6 倍**） |
| 与现有底模 | 需换到 H3 视频链路 | ✅ 同生态 |
| 显存风险 | ⚠️ 19.53 GB 在 8 GB 上未验证 | ✅ Z-Image 4bit 已稳定运行 |
| 控制条件 | 8 种 | depth / pose / canny / tile（**含 depth**） |
| 本机节点 | — | ✅ `ZImageFunControlnet` 已在 |
| 本机训练脚本 | — | ✅ `Z-Image-Turbo-Fun-Controlnet-Union-2.1.sh` 已在 |

**与白模产出的天然对齐**：白模已实现 Gray / Depth 双通道，
而该模型官方 `results/` 目录提供的样例正是 `gray.png`。

**⚠️ 未验证**：`ZImageFunControlnet` 与本机 **4bit 量化底模**的搭配；
lite 版 1.92 GB 的实际显存占用。

### 10.3 发现二：Z-Image 的参考图能力（部分证实）

`comfy_extras/nodes_zimage.py` 的实现：

```python
if image_encoder is not None:
    encoded_images.append(image_encoder.encode_image(image))   # 路径一：CLIP-Vision
if vae is not None:
    ref_latents.append(vae.encode(image))                      # 路径二：VAE 参考潜变量
```

**两条参考路径，且本机 VAE 已有（0.16 GB）。**

官方训练脚本 `Z-Image-Omni-Base.sh` 引用的组件：

```
Tongyi-MAI/Z-Image-Omni-Base:transformer/*.safetensors
Tongyi-MAI/Z-Image-Omni-Base:siglip/model.safetensors      ← SigLIP 视觉编码器
```

脚本中另有被注释的 **"Image(s) to image training"** 段落
（`--data_file_keys "image,edit_image"`，`--extra_inputs "edit_image"`）。

**⚠️ 不确定性（必须标注）**：

| 项 | 状态 |
|---|---|
| 被官方脚本引用 | ✅ 证实 |
| **公开可下载** | ❌ **未能证实**（HF 404 / ModelScope API 404 / 搜索 API 404） |
| 可能原因 | 尚未发布 / 已改名 / 需授权 |

### 10.4 一个正向的副产品：LoRA 训练脚本本机已有

`D:\AI\local-gen\DiffSynth-Studio\examples\z_image\model_training\lora\` 下：

| 脚本 | 用途 |
|---|---|
| `Z-Image-Turbo.sh` | 文生图 LoRA 训练 |
| `Z-Image-Omni-Base.sh` | 多模态（含 image-to-image）训练 |
| `Z-Image-Turbo-Fun-Controlnet-Union-2.1.sh` | ControlNet LoRA 训练 |
| `Z-Image-Turbo-Fun-Controlnet-Tile-2.1-8steps.sh` | Tile 版 |
| `Z-Image.sh` | 基座 |

**官方超参（可直接照搬到租用的 GPU）**：

```
--lora_base_model "dit"
--lora_target_modules "to_q,to_k,to_v,to_out.0,w1,w2,w3"
--lora_rank 32
--learning_rate 1e-4
--num_epochs 5
--max_pixels 1048576
--dataset_repeat 50
--use_gradient_checkpointing
--remove_prefix_in_ckpt "pipe.dit."
```

**这消解了一个此前的顾虑**：租 GPU 训 LoRA 时**不需要从零写训练脚本**，
用官方脚本 + 改数据集路径即可。

### 10.5 方法论教训（这一节比结论更重要）

**本次调研推翻了本项目两条写在文档里的结论。** 复盘：

| 错误结论 | 错在哪 | 教训 |
|---|---|---|
| "H3 ControlNet 跑不了，所以白模控制没解" | **只查了 H3，没查 Z-Image 自己有没有** | **别把"某个模型没有"当成"这条路没有"** |
| "图像条件能力在本地是**架构级缺失**" | 把「**我们的 checkpoint 是纯文本版**」说成了「**架构不支持**」 | **区分「当前配置不支持」与「架构不支持」** |

**共同根因**：**把"当前环境的观测"当成了"架构的结论"。**

**防复发措施**：

1. 断言"架构不支持"前，**必须读源码或 schema**，而不能只看当前 checkpoint 的 config
2. 排查某能力缺失时，**换底模再查一遍** —— 同一生态里往往有别的变体
3. 本项目的 skill 文档（`local-gen/SKILL.md`）里的"未实现"清单**也可能过时** ——
   它的判断基于当时的权重，权重更新后结论可能失效

> **可推广的一条**：`local-gen/SKILL.md` 的"未实现"表里，
> "H3 ControlNet"与"跨机位角色一致性"两条本次都有新证据。
> **文档里的否定结论应当带日期与依据**，否则会被当成永久事实。
