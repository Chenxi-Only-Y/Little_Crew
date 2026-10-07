# 小剧组 Little Crew — 重构方案与风险清单

> 输入：`docs/recon/01-usage.md`（实测基线）。
> 目标（用户确认）：**前后端几乎全部重构；从「单个职业页面」变为单一「导演台」，
> 不同职业从前端页面降级为「按需提取的组件」，在导演台上编排一系列流程。**

---

## 0. 执行进度（滚动更新）

| 步骤 | 状态 | 产出 |
| --- | --- | --- |
| 未提交改动备份 | ✅ 完成 | `Temp\ds6-recon\uncommitted-20261002_151803\`（含补丁 + RESTORE.ps1） |
| 历史数据删除 | ✅ 完成 | 5 表 32 行 → 0；`outputs` 释放 116.2 MB；见 `10-data-reset.md` |
| 注册表三层结构（V0） | ✅ 完成 | `app/registry/{types,roles,api,__init__}.py`；16 职业 / 7 部门 / 18 节点 / 34 产物 / 12 确认点 |
| 注册表接口 | ✅ 完成 | `GET /api/registry`、`POST /api/registry/validate`（实测 200 / 401 鉴权正常） |
| 注册表护栏 | ✅ 完成 | `tests/test_registry.py`（27 条）；**全量测试 33 passed**（原 6 + 新 27） |
| **云端使用提醒**（只提醒不拦截） | ✅ 完成 | `app/services/cloud_cost.py` + 3 个端点 + 设置页展示；`tests/test_cloud_cost.py`（25 条） |
| **Provider 抽象** | ✅ 完成 | `app/services/providers.py`（30 个 provider / 8 类能力）+ `GET /api/providers`；**并发度改由 provider 声明**（本地=1、云端=4）；设置页清单改从接口取 |
| Provider 护栏 | ✅ 完成 | `tests/test_providers.py`（18 条）；**全量 78 passed** |
| **单例 + thread-local 改造** | ✅ 半完成 | ① 配置不再快照：4 个服务（llm/image/video/quality）的 `mock` 与 `image.output_dir` 改为实时解析；② 跨线程上下文：`runtime_settings.capture()/restore()` + `image_generator` 线程池注入；`tests/test_context.py`（12 条） |
| **确认点重启续跑护栏** | ✅ 完成 | `task_runner.has_checkpoint()` 检测断点；断点丢失时 `resume_run` 拒绝并标记 `failed`，`/api/crew/resume` 返回 **409**（不再静默重放）；`tests/test_checkpoint_resume.py`（5 条） |
| **显式 RunContext 贯穿** | ✅ 完成 | `app/core/context.py`：上下文进 State，节点进入时绑定（含 thread-local 兼容回退）；`scripts/export_registry.py` 导出快照；`tests/test_context.py`（17 条） |
| **前端导演台 F0+F1** | ✅ 完成 | `web/`：Vite 8 + React 19 + React Flow 12 + Tailwind 4 + Zustand；画布渲染 16 职业 / 18 节点 / 34 端口 + 按产物自动连线；截图 `docs/recon/img/director-canvas-f1.png` |
| **前端 F2（连线校验）** | ✅ 完成 | `web/src/domain/validate.ts` 复用后端下发的 `validation_spec`；拖线即时拒绝 + 顶部"拓扑可执行"状态；**等价性实测 6/6**（`pnpm check:parity`）；`/api/registry` 改为公开（纯静态 schema） |
| **前端 F3（运行与实时）** | ✅ 完成 | `app/services/events.py` 事件中心 + `POST /api/sessions` + SSE `GET /api/sessions/{id}/events`；前端运行栏、思考流、节点实时点亮；截图 `docs/recon/img/director-canvas-f3-running.png`；`tests/test_events.py`（20 条）。**全量 146 passed** |
| **命名空间对齐（关键修复）** | ✅ 完成 | 发现**三套命名**断层（注册表 v2 / 工作流 v1 / 阶段名）；加 `Node.workflow_node` + `registry.api.to_registry_node()` 翻译；修正职业→节点指向（`continuity`→`continuity_checker` 等） |
| **不静默默认：云端/本地由用户选** | ✅ 完成 | `resolve_provider()`：职业级 > 全局 > 模式（cloud/local）> **未选则明确报错**；`llm/image/video` 三处调用点全部改造；config 里三个 provider 默认值清空；设置页顶部加"第一步必选"；`GET /api/providers/modes`；`tests/test_providers.py`（32 条） |
| **设置白名单由注册表派生** | ✅ 完成 | `registry.api.allowed_settings()` 取代手写 `_SETTABLE`——原来 `tts_provider`/`lora_train_provider`/`image_provider_image_edit` 等键**不在白名单里，界面改了会被静默丢弃**（已实际发生过） |
| **属性面板可编辑** | ✅ 完成 | `ConfigFieldEditor.tsx` 按注册表 `ConfigField.type` 渲染 select/text/password/number/slider；改动即时保存；密钥不回显（掩码占位）；清空=清除覆盖。`tests/test_context.py` 加 3 条守住 set_many 语义。**164→167 passed** |
| **`set_many` 合并写入（数据丢失修复）** | ✅ 完成 | 原先**整体替换**该用户配置：属性面板只存一个字段就会**清空其余全部设置**（表现为之前存的"云端/本地模式"消失、运行时报"未选择提供者"）。改为默认合并，`merge=False` 可显式替换 |
| **工程文件存取** | ✅ 完成 | `app/services/project_store.py`（整份 JSON 加密入库，便于加字段不迁移）+ 5 个端点 + `ProjectBar.tsx`（保存/另存为/打开/新建/改名/删除、未保存标记）；`tests/test_projects.py`（16 条）。**密钥写入前掩码**、**跨用户隔离**、体积上限、畸形数据归一化 |
| **登录页** | ✅ 完成 | `LoginPage.tsx`（登录/注册同表单）+ `api/auth.ts`（token 唯一实现，退出时清凭据）；未登录挡住整个导演台、**先验登录再拉注册表**（避免画布闪现）；失效 token 会被识别并清除。删除 `main.py` 里已成死代码的 `_SETTABLE` |
| **站点结构收敛为四页** | ✅ 完成 | `app/router.ts`（哈希路由，自实现约 60 行）+ `app/Shell.tsx`（导航）+ `pages/{HomePage,DirectorPage,UserPage,SettingsPage}.tsx`。运行栏/校验/工程栏从全局顶栏**移入导演台**（只对当前画布有意义）。实测 6 项：导航仅 4 项、逐页渲染无报错、刷新停在同一页、无效 hash 回退首页、导演台有运行栏、其它页没有 |
| 旧 Streamlit 前端 | 🟡 标记为待下线 | 保留可运行作灰度回退；在 `frontend/streamlit_app.py` 顶部注明"已被导演台取代"，并写明下线时机 |
| 前端 F4（编排与确认） | ✅ 完成 | 见上 |
| 确认点改用 `SqliteSaver`（真正跨重启可续跑） | ⬜ 待做 | 当前是"安全失败"：拒绝 + 明确报错，而非恢复续跑 |
| 成本拦截 / 预算熔断 | ❌ 不做 | 用户 2026-10-02 决定：只做提醒，不做拦截 |

> 关键修正：新表实为 **16 职业**（基建部含「工作流架构师」+「技术指导」），先前文档误记为 15，已更正。

---

## 1. 先说结论：这不是重构，是「前端平台替换 + 后端服务化」

必须先把这句话说清楚，否则方案会走错方向：

**Streamlit 无法承载「导演台」。** 不是代码写得不好，而是框架模型不匹配：

| 导演台需要 | Streamlit 提供 | 证据 |
| --- | --- | --- |
| 多面板常驻画布、局部刷新 | 每次交互**重跑整个脚本**，无法局部更新 | 现质检页只能 `time.sleep(1.2)` + `st.rerun()` 硬刷 |
| 面板拖拽/停靠/编排 | 无拖拽布局能力；`st.columns` 静态 | 现布局全部硬编码列数 |
| 固定导航与路由 | 侧边栏被**主动 `display:none` 隐藏**，改手写按钮 + `switch_page` | `_styles.py:56-58` |
| 多路推送/长任务进度 | 单会话脚本模型，靠 `st.session_state` 轮询 | `_console.py`、`11_质检总监.py:132-167` |
| 可测试的前端逻辑 | 逻辑与渲染写在 f-string HTML 里 | 各职业页 `render_result` |

**因此重构分两个可独立验收的战场**：
- **后端服务化**（可先行、可独立验收）：单一注册表 + 状态机 + 切片查询 API + provider 抽象。
- **前端重建**（导演台）：消费上述 API 的组件化画布。

好处：后端先行后，**旧 Streamlit 前端仍能跑**（API 保持兼容），可以灰度切换、随时回退。
这也是把风险从「一次大爆炸」压成「两条可回滚的线」的关键。

---

## 2. 目标架构

### 2.1 分层

```
┌─ 前端：导演台 (SPA) ────────────────────────────────────────┐
│  画布 Canvas：布局/停靠/编排（编排 = 选定起点→终点 的执行计划）│
│  组件库 Panels：13 个职业面板，各自 输入/产物/操作 三段式      │
│  共享状态 Store：当前 session + 只订阅自己切片(selector)      │
│  实时通道 SSE/WS：阶段进度、AI 思考流、任务状态                │
└──────────────────────┬─────────────────────────────────────┘
                       │ HTTP + SSE（契约见 §2.4）
┌──────────────────────▼─────────────────────────────────────┐
│  后端：FastAPI（路由变薄）                                   │
│  ├─ Registry  单一注册表：role/dept/stage/checkpoint/artifact│
│  ├─ Workflow  LangGraph 图（由注册表生成，非硬编码）          │
│  ├─ Runner    任务编排：确认点持久化、断点续跑、取消、切片推送 │
│  ├─ Providers 统一接口：llm/image/video/quality（local 为一个实现）│
│  └─ Stores    Session/Asset/User/Settings（可换 PG）         │
└────────────────────────────────────────────────────────────┘
```

### 2.2 核心：单一注册表（**收敛靶心**，消除 5 处重复定义）

现状：`role/dept/stage/checkpoint` 四概念散落在 **5 个文件**（`main_workflow._STAGE_ORDER`、
`org.ROLE_ORG`、`_console._ALL_PHASES/_ROLE_TO_PAGE/_CHECKPOINT_OPTIONS`、
`_segment_select.FULL_ROLES`、`_org.DEPARTMENTS`），靠注释人工对齐。

目标（一份数据驱动全栈）：
```python
# app/registry/roles.py —— 唯一数据源
ROLES = [
  Role(key="writer", label="策划总监", dept="创作策划部",
       stage="writer",                # 对应工作流节点
       produces=["story_outline"],    # 产出字段（= 面板展示区）
       consumes=["user_input"],
       checkpoint=None,               # 是否有人工确认点
       panel="writer",                # 前端面板契约名
       page_legacy="pages/01_策划总监.py"),  # 迁移期兼容旧路由
  ...
]
```
由此**自动派生**：工作流节点顺序与路由、`_STAGE_ORDER`、部门分组、前端导航与组件、
确认点清单、`.env.example` 的 provider 列表、产物输出目录的部门/职业名。

**验收**：全仓库只有一处出现 `writer/策划总监/创作策划部` 的映射；`grep` 不再出现第二处。

### 2.3 状态统一：状态机 + 任务解耦（解决 §6.1/6.3）

```
session.status ∈ {draft, running, waiting_review, resuming, done, failed, cancelled}
stage.status  ∈ {pending, running, done, skipped, stale, failed}
```
- **确认点持久化**：`MemorySaver` → **`SqliteSaver`（与业务同库）**，
  `waiting_review` 必须**同时**满足「DB 标记 + checkpoint 存在」，否则降级为
  `failed(stale_checkpoint)` 并明确提示，禁止静默重放。
- **跳过语义**：跳过 = `skipped`（不是 `done`），进度展示区分「跳过/沿用/完成」。
- **任务解耦**：`task_runner` 拆为 `Job` 实体（id/type/session/progress/result），
  结束点：`_THINKING/_CANCEL/_JOBS` 三个进程内 dict → 可查询、可持久化的 Job 表
  （同时为多进程部署铺路）。

### 2.4 导演台 API 契约（前端重建的起点）

现状前端每个面板都拉**整个 payload**（`st.session_state["payload"]`）。目标：**按切片取数**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/registry` | 一次性下发 role/dept/stage/checkpoint/artifact 注册表（前端不再硬编码） |
| POST | `/api/sessions` | 新建会话（一句话创意 + 编排计划 plan） |
| GET | `/api/sessions/{id}` | 会话元信息 + 各阶段状态（**不含产物**） |
| GET | `/api/sessions/{id}/panel/{role}` | **单个面板的切片**（产物 + 可执行操作） |
| POST | `/api/sessions/{id}/plan` | 提交/修改编排计划（start_stage→end_stage、启用哪些确认点） |
| POST | `/api/sessions/{id}/resume` | approve/redo/back（**校验 checkpoint 存在**） |
| POST | `/api/sessions/{id}/jobs` | 触发单职业动作（reassess/retry/scene/rhythm/continuity/publish/video） |
| GET | `/api/sessions/{id}/events` | **SSE**：阶段进度 + AI 思考流 + job 进度（替代轮询） |
| GET | `/api/sessions/{id}/assets/{shot_id}` | 单镜头资产（图/质量/一致性），供面板按需加载 |

> 兼容策略：现有 26 个端点**保留为别名**直到前端切换完成，前端按 `registry` 驱动，
> 路由由配置生成——这样「13 个页面」的可寻址性不丢，但不再是唯一入口。

### 2.5 状态实体规范化（替代 20 键扁平 payload）

现状 payload 是 20 个扁平 key，无类型、无归属。建议建立显式实体（可先只做投影层，不动存储）：

| 实体 | 对应现状 key | 产出方 → 消费方 |
| --- | --- | --- |
| Session | `session_id/status/phase` | — |
| Shot 镜头 | `storyboard[]` + `prompts[]` + `images[]` 按 `shot_id` 合并 | storyboarder → photographer |
| Asset 资产 | `character_profiles/reference_images`、`scene_*` | character/scene_designer → IP-Adapter |
| Report 报告 | `script_review/critic_report/quality_report/continuity_report/rhythm_plan` | 各审查方 → 面板 |
| Plan 编排计划 | `start_stage/end_stage/enabled_checkpoints/retry_shots` | 用户 → workflow |

⚠️ **Shot 合并是重点**：当前 `storyboard / prompts / images` 三个并行数组靠 `shot_id` 隐式对齐
（`image_generator` 末尾 `ordered = [images_by_shot[p["shot_id"]] ...]`），任何一处漏项就错位。
规范化为单一 `Shot` 实体可消除这类隐患。

### 2.6 Provider 抽象（承接未提交的 local-gen 改动）

现状：`image.py` 4 个 `if provider ==` 分支 + `threading.local()` 存 role；
`video.py` 同样；provider 列表 3 处重复。

目标：
```python
class ImageProvider(Protocol):
    def generate(self, spec: ImageSpec, ctx: RunContext) -> ImageResult: ...

PROVIDERS: dict[str, ImageProvider] = {
    "local": LocalGenProvider, "dashscope": DashScopeProvider,
    "minimax": MiniMaxProvider, "replicate": ReplicateProvider, "mock": MockProvider,
}
```
`RunContext` 显式携带 `user/session/role`，**去掉 `_role_local` 线程共享 hack**。
`local` 只是其中一个实现——你现在的未提交改动即成为首个「新增 provider 不改核心」的验证案例。

---

## 3. 前端路线选择（**需你拍板，见 §7 决策点**）

| 路线 | 做法 | 工作量 | 风险 | 代价 |
| --- | --- | --- | --- | --- |
| **A. Streamlit 单页导演台** | 1 个页面 + 动态容器，职业变「面板组件」，`st.tabs`/`columns` + 按需渲染 | 小（1–2 周量级） | 低 | 仍是整页 rerun；无真拖拽；视觉能做到「像导演台」，交互做不到 |
| **B. 前后端分离（推荐）** | FastAPI + React/Vue/Svelte 单页；后端提供 §2.4 契约 + SSE | 大（4–8 周量级） | 中（新增构建链/部署） | 需要前端构建与部署；但真正满足「编排」诉求 |

**我的建议：B，但按「后端先行」切两刀**——
1. 第 1 刀做后端服务化（§2.2–2.6），旧 Streamlit 继续跑，随时可验收；
2. 第 2 刀用 `docs/recon/01-usage.md §7.3` 发现的**天然组件边界**（职业页已是
   `render_result(payload)` 纯函数）低成本平移为前端组件。

若你更看重「尽快看到导演台」，则先 A 后 B：A 的组件接口按 §2.4 设计，
将来平移到 B 时只换渲染层，不重写业务。

---

## 4. 分阶段执行计划（每阶段都有可验收的门）

| 阶段 | 内容 | 验收门（可自动化） |
| --- | --- | --- |
| **P0 护栏** | 补 3 个测试：确认点续跑（含重启）、seed/start_stage 续跑、产物归档路径 | 新增测试全绿；**基线仍是 6 passed** 不变红 |
| **P1 止血** | 修 §6.1 状态不一致（SqliteSaver + 状态对齐）、§6.2 归档路径、§6.3 跳过语义、§6.5 量纲 | 重启后 `resume` 要么真续跑要么明确失败；`run_demo` 产物落在 `<uid>/<部门>/<职业>/<session>/` |
| **P2 注册表** | 落地 §2.2 单一注册表，工作流/导航/确认点/文档由它派生 | `grep` 证明映射只存在一处；6+ 测试全绿；旧前端功能不退化 |
| **P3 Provider** | 落地 §2.6 抽象，把未提交的 `local` 改为标准实现 | `local-gen` 起服务后端到端出 1 图 1 视频；mock 路径不变 |
| **P4 服务层** | Job 实体、SSE 通道、切片 API（§2.4） | 前端只需调用切片 API；`/events` 能实时收到阶段与思考流 |
| **P5 导演台** | 前端按 §3 选定路线重建；13 面板组件化 + 编排计划 UI | 面板可单独提取/移除；编排 = 选起点终点 + 勾确认点；端到端跑通一次完整创作 |
| **P6 收尾** | 清明文遗留（§6.4）、清测试账号与游离脚本、更新 README/DESIGN 与实际对齐 | `.env.example` 与注册表一致；`api_settings` 无明文行 |

**排序理由**：P1 先于 P2——注册表动的是结构，而 P1 修的是**会产生真实费用/错误结论**的行为缺陷；
带着 §6.1 做结构重构，会让「重放」问题在更长链路里更难定位。

---

## 5. 风险清单

| # | 风险 | 影响 | 概率 | 缓解 / 验证 |
| --- | --- | --- | --- | --- |
| R1 | **前端平台替换超预算**（Streamlit→SPA） | 高 | 中 | 按 §3 切两刀；后端先行保持旧前端可跑；必要时先做路线 A |
| R2 | **checkpointer 迁移**：SqliteSaver 与现有 `sessions` 表/thread_id 约定冲突 | 高 | 中 | `thread_id = session_id` 已有约定；先加迁移测试与旧库兼容读；保留 `MemorySaver` 回退开关 |
| R3 | **注册表重构反而引入行为偏差**（阶段顺序/跳过边界） | 高 | 中 | P0 测试先锁行为；对 `_STAGE_ORDER`/`_skipped_by_scope` 做等价性对照测试 |
| R4 | **产物归档路径变更**导致历史 `data/outputs` 失联 | 中 | 高 | 归档函数做「新路径写入 + 旧路径读兼容」；`_migrated`/`no-session-*` 一次性迁移脚本 + 干跑报告 |
| R5 | **auth 迁移**（cookie → 正规会话）破坏登录态 | 中 | 中 | token 表结构不变、`resolve()` 语义不变；登录做双写过渡；退出登录单独回归 |
| R6 | **local-gen 未验证**：8GB 显存 + Disk Offload 下真实耗时/失败模式未知 | 中 | 高 | P3 前先单独起服务跑 1 图 1 视频，记录耗时与失败码；`local_gen_timeout` 默认 7200s 需实测定标 |
| R7 | **多进程部署下进程内状态失效**（`task_runner` 三个 dict、单例 store） | 中 | 中 | P4 的 Job 表化；单例改为显式注入；先声明单 worker 约束并在文档标注 |
| R8 | **SSE 与 Streamlit 不兼容**（路线 A 下无法消费） | 中 | 中（若选 A） | 路线 A 期间保留轮询端点；SSE 仅在路线 B 启用 |
| R9 | **mock 数据与真实链路脱节**：`_MOCK_PROMPTS` 仅 3 个 shot_id，分镜 >3 时错位 | 中 | 高 | P2 注册表化时改为按分镜动态生成 mock；加入「分镜数 > mock 条数」测试 |
| R10 | **质量/一致性量纲混乱**导致面板结论错误 | 中 | 高 | P1 统一为 0–100 并在模型层声明单位；加断言测试 |
| R11 | **密钥/明文遗留**在重构中被固化到新存储 | 中 | 中 | P6 前先做一次 `api_settings` 解密体检；`_encrypt` 失败改为抛错而非静默明文 |

---

## 6. 明确不做（避免范围蔓延）

- **不换 LangGraph**：编排内核工作正常（25 节点/HITL/seed 均已实测），只把它**由注册表生成**。
- **不改外部 provider 协议**：DashScope/MiniMax/Replicate 的调用细节保持，只换组织方式。
- **不动 MIT 许可与现有产物格式**（png/mp4 命名与目录结构，除 §6.2 归档修正）。
- **不在本次引入 PG/Redis**：`DB_URL` 预留已存在，但迁移不在关键路径（避免与 P1 状态修复叠加风险）。

---

## 6.5 远期规划（用户 2026-10-07 明确"先保留，后续开发"）

### 修图师 · Inpainting / Outpainting 修复

**状态**：注册表里有这个职业与节点（`inpainter`，能力 `image_edit`，
产出 `fixed_images`），**执行图里也会跑到它，但它直接跳过**并说明
"修复后端未接入"。这是**有意为之**的占位 —— 用户原话：
"修图师目前先保留，后续开发，流程直接跳过"。

**为什么只能占位**（查证过，别被"能力已声明"骗了）：

| 看起来有的 | 实际情况 |
| --- | --- |
| 提供者表里有 `z-image-inpaint`（本地·免费） | 只出现在**三个声明处**：`providers.py` 的 `ProviderSpec`、`cloud_cost.py` 的成本表、`local_probe.py` 的探测表 —— **没有任何代码调用它做修复** |
| 设置里有"修复模型来源"（`image_provider_image_edit`） | 键存在、能存能读，但读到的值**没有任何消费点** |
| `ImageService` 有 `image_edit` 能力 | `ImageService` **只有 `generate()`（文生图）**，没有 edit / inpaint 方法；`_local` 分支走的是 Z-Image **文生图**接口 |
| 产物 `fixed_images` 已声明 | 它**从来没有被写入过**（`quality_assessor.run()` 只返回 `quality_report`）。这个空声明在本次改动**之前就存在** |

**要做的话，两条路**：

1. **本地**（免费，但要动 `D:\AI\local-gen` 那个外部项目）：
   给它加一个 inpaint 路由（mask + 原图 → 修复图），本项目再补
   `ImageService.inpaint()` 与 `inpainter` 节点实现。
   ⚠️ `local-gen` 被用户划为**外部项目**，动它之前必须先问。
2. **云端**（不动外部项目，但要花钱）：
   接 dashscope 的 `qwen-image-edit`（提供者表里已声明，成本表里也有）。
   这条只改本项目。

**实现时要一起处理的三件事**：
- `inpainter` 现在是"无条件跳过"，改成真干活后要**只修用户标记的图**
  （修图很贵，不该对每张图都跑），并支持"没标记就跳过"；
- `fixed_images` 要有真实写入者前，先把 `roles.py` 里 `inpainter`
  的 `produces` 与实际对齐（现在是"声明了但落不了地"，已加注释说明）；
- 修完之后 `aggregator` 要把 `fixed_images` 导出去（本次已加进白名单）。

### 另外两件同批确认"暂时不做"的

- **视频质检的逐帧检测**：现在只报"有几段可检"，**不给假分数**
  （编一个"闪烁 0.9 合格"比不检查更糟）。真做要 ffmpeg 抽帧 + 帧间比较。
- **BGM / 音效轨**（`music` 能力）：提供者表里是空的，`bgm_track` 留空并写明原因。

---

## 7. 需要你拍板的决策点

| # | 决策 | 选项 | 影响面 |
| --- | --- | --- | --- |
| D1 | **前端路线** | A 单页 Streamlit / B 前后端分离（推荐 B，可分两刀） | 决定 P5 的工作量与最终交互上限 |
| D2 | **未提交改动如何处理** | 先落定提交（作为基线）/ 并入 P3 一起改 | 决定重构起点是否干净 |
| D3 | **历史产物是否迁移** | 迁移到新归档结构 / 只保证新产物正确 | 影响 R4 的工作量 |
| D4 | **真实 provider 何时验证** | P3 前先验一遍真实链路 / 全程 mock 到 P5 | 决定 R6/未验证项何时收敛 |
| D5 | **前端技术栈（若选 B）** | React / Vue / Svelte | 影响组件库与设计系统的复用方式（现 `SENTRY_CSS` 可直接搬） |

---

## 8. 立即可做的第一步（无需等决策）

**P0 护栏**与 **P1 止血**中的 §6.2、§6.3、§6.5 三项，与 §3 的路线选择**完全解耦**，
无论选 A 还是 B 都必须做，且能立刻降低后续风险。建议从这里开始。
