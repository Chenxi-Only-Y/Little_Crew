# 小剧组 Little Crew — 前端重写准备（导演台 / ComfyUI 式画布）

> 前置：`01-usage.md`（现状基线）、`02-refactor-plan.md`（方案与风险）、`03-before-after.md`（形态对照）。
> 已定：前端**重新写**；路线 **B（前后端分离，分两刀）**；形态 **ComfyUI 式节点画布**。
> 本文 = 动手前的技术准备：技术栈、目录结构、类型契约、节点模型、API 依赖、阶段清单。

---

## 1. 结论先行：前端只有一条硬约束

> **前端不得自己定义任何「职业 / 部门 / 阶段 / 产物 / provider」清单，必须全部由后端注册表下发。**

原因是有实测证据的：现在这套映射散落在 **5 个文件**里，并且**已经真实不一致**：

| 位置 | 「场记」的 key | 其它不一致 |
| --- | --- | --- |
| 后端 `app/core/org.py` | `continuity_checker` | 13 职业 + `editor`/`distributor` |
| 前端 `_org.py:61` | **`continuity`** ⚠️ | 5 部门，含 `ready`/`real_job`/`agent` |
| 前端 `_console.py:_ROLE_TO_PAGE` | `continuity_checker` | 又一套标签与页面映射 |
| 后端 `main.py:_ROLES` | **`continuity`** ⚠️ | 用于 `llm_*` 覆盖键 |
| `_segment_select.py:FULL_ROLES` | 11 项（无 aggregator/distributor） | 第 4 套阶段顺序 |

**如果前端重写时再抄一份，就是第 6 份。** 所以重写的第一条验收标准是：
`frontend/` 里 `grep -r "策划总监\|character_designer\|continuity"` **零硬编码命中**（除测试夹具）。

---

## 2. 技术栈（建议）

| 层 | 选型 | 理由 |
| --- | --- | --- |
| 框架 | **React + TypeScript + Vite** | 画布生态最成熟；类型可对齐后端契约 |
| 画布 | **React Flow（`@xyflow/react`）** | [节点式 UI 的事实标准](https://reactflow.dev/)，自定义节点/端口/缩略图开箱即用；官方维护着[资源清单](https://github.com/xyflow/awesome-node-based-uis) |
| 服务端状态 | **TanStack Query** | 切片取数 + 缓存 + 失效重取，天然贴合 `/panel/{role}` 按需拉取 |
| 客户端状态 | **Zustand** | 画布拓扑、选中节点、属性面板草稿等纯前端状态 |
| 实时 | **SSE（`fetch` + `EventSource`）** | 阶段进度/AI 思考流/job 进度；比轮询省，且比 WS 简单 |
| 样式 | **Tailwind CSS + CSS 变量** | 现有 `_styles.py` 的 Sentry token 直接搬成变量（见 §7） |
| 表单/校验 | **react-hook-form + zod** | 属性面板与编排计划校验；zod schema 可与 §4 类型同源 |
| 图表 | 暂不引入 | 质检评分先用轻量原生条；需要时再加 |

> 版本号请在开工时用 `pnpm add` 锁定；本次勘察环境无法访问 npm，故不写死小版本。
> 若你偏好 Vue/Svelte，§3–§6 的契约与结构**完全平移**，只有渲染层不同。

---

## 3. 目录结构

```
frontend/                        # 新前端（与旧 frontend/ 并存，灰度切换）
├── src/
│   ├── app/
│   │   ├── App.tsx               # 路由壳（登录 / 导演台 / 设置）
│   │   ├── providers.tsx         # QueryClient / Theme / SSE Provider
│   │   └── router.tsx
│   ├── canvas/                   # ★ 画布核心
│   │   ├── Canvas.tsx            # React Flow 容器、缩放/平移/框选
│   │   ├── NodeLibrary.tsx       # 左侧节点库（按部门分组，来自 registry）
│   │   ├── NodeCard.tsx          # 通用节点外壳（标题/状态灯/端口/操作条）
│   │   ├── nodes/                # 13 个职业节点专属渲染（产物预览区）
│   │   │   ├── WriterNode.tsx
│   │   │   ├── StoryboarderNode.tsx
│   │   │   ├── PhotographerNode.tsx
│   │   │   └── ...
│   │   ├── edges.ts              # 连线规则与产物类型着色
│   │   └── compile.ts            # 画布拓扑 → 执行计划 + 前端预校验
│   ├── panels/                   # 右侧属性面板（由 registry 的 config schema 驱动）
│   │   ├── NodeInspector.tsx
│   │   ├── PlanInspector.tsx     # 编排计划：起点/终点/确认点开关
│   │   └── RunInspector.tsx      # 运行控制：运行/暂停/停止/队列
│   ├── domain/                   # ★ 与后端契约一一对应的类型与纯逻辑
│   │   ├── registry.ts           # Role/Dept/Stage/Checkpoint/Artifact 类型
│   │   ├── session.ts            # Session/StageState/Shot/Asset/Report
│   │   ├── graph.ts              # CanvasNode/CanvasEdge/WorkflowDoc
│   │   └── ports.ts              # 端口类型 → 颜色/图标/兼容矩阵
│   ├── api/
│   │   ├── client.ts             # fetch 封装、token/cookie、错误规范化
│   │   ├── registry.ts           # GET /api/registry
│   │   ├── sessions.ts           # 会话 CRUD、panel 切片、resume
│   │   ├── jobs.ts               # 单职业动作（reassess/retry/video/…）
│   │   └── events.ts             # SSE 订阅（阶段/思考/job 进度）
│   ├── store/                    # Zustand：拓扑、选中、UI、运行态
│   ├── ui/                       # 基础组件（Button/Card/StatusDot/Port…）
│   └── styles/tokens.css         # Sentry 设计 token → CSS 变量
└── (构建产物交由 FastAPI 托管或独立部署)
```

---

## 4. 类型契约（前端视角，与后端注册表对齐）

```ts
// domain/registry.ts —— 全部由 GET /api/registry 下发，前端零硬编码
export type ArtifactKey =
  | 'user_input' | 'story_outline' | 'script' | 'script_review'
  | 'art_brief_plan' | 'character_profiles' | 'character_reference_images'
  | 'scene_design_profiles' | 'scene_reference_images' | 'storyboard'
  | 'rhythm_plan' | 'continuity_report' | 'prompts' | 'images'
  | 'quality_report' | 'consistency_score' | 'character_consistency_report'
  | 'critic_report' | 'publish_results';

export interface Port { key: ArtifactKey; label: string; kind: 'text' | 'json' | 'image' | 'list' | 'score'; }

export interface RoleNodeSpec {
  key: string;                 // 唯一，如 'photographer'（以后端为准，统一 continuity_checker）
  label: string;               // 「摄影师」
  dept: string;                // 「技术制作部」
  stage: string;               // 工作流节点名
  agent: string;               // Agent 类名/说明
  duty: string;                // 一句话职责
  inputs: Port[];              // 依赖的产物 → 决定「能不能连」
  outputs: Port[];             // 产出的产物 → 决定「往下能连谁」
  checkpoint: boolean;         // 是否人工确认点
  configSchema: ConfigField[]; // 属性面板字段（对应 runtime_settings 职业级覆盖）
  parallelGroup?: string;      // 同组可并行（如 character_designer / scene_designer）
  legacyPage?: string;         // 迁移期兼容旧路由
}

export interface ConfigField {
  key: string;                 // 'image_provider_photographer'
  label: string;               // 「图片模型」
  type: 'select' | 'text' | 'password' | 'number' | 'toggle';
  options?: string[];
  default?: unknown;
  scope: 'role' | 'global';    // 职业级 or 全局
  secret?: boolean;            // true → 不回显，仅提交
}
```

```ts
// domain/session.ts
export type StageStatus = 'pending' | 'running' | 'done' | 'skipped' | 'stale' | 'failed';
export interface StageState { stage: string; status: StageStatus; confidence?: 'ok' | 'stale'; }
export interface Session {
  id: string; userInput: string;
  status: 'draft'|'running'|'waiting_review'|'resuming'|'done'|'failed'|'cancelled';
  stages: StageState[];
  plan: RunPlan;
}
export interface RunPlan {
  startStage?: string; endStage?: string;
  enabledCheckpoints: string[];
  retryShots?: string[];
  topology?: WorkflowDoc;      // 保存的工程拓扑
}
export interface Shot {        // ★ 规范化后的镜头实体（替代三数组隐式对齐）
  id: string; sceneId: string; shotType: string; camera: string;
  description: string; durationS: number; dialogue: string; transition: string;
  tempo?: string; prompt?: string; negativePrompt?: string; seed?: number;
  image?: { url: string; localPath: string; status: 'ok'|'failed'; method: string };
  quality?: number; consistency?: number; level?: 'ok'|'moderate'|'severe'|'attention';
}
```

```ts
// domain/graph.ts —— 工程文件（可保存/复用，对应 ComfyUI 的 workflow JSON）
export interface WorkflowDoc {
  version: 1;
  nodes: Array<{ id: string; role: string; position: {x:number;y:number}; config: Record<string, unknown> }>;
  edges: Array<{ id: string; source: string; target: string; artifact: ArtifactKey }>;
}
```

---

## 5. 节点与连线的交互模型

### 5.1 节点外壳（统一）
```
┌─────────────────────────────┐
│ ● 摄影师            [⚑待审核] │  ← 状态灯 + 徽标
│ 技术制作部 · 图片生成         │
├── 输入 ────────── 输出 ─────┤
│ ◀ prompts        images ▶   │  ← 端口带产物类型着色
├─────────────────────────────┤
│ ▸ 3/3 张已生成  [缩略图条]    │  ← 产物预览（按角色定制）
│ [查看] [重拍已选] [重新生成]   │  ← 节点级操作
└─────────────────────────────┘
```

### 5.2 连线合法性（**重要限定**）
- 连线 = 产物依赖：`source.outputs[].key` 必须能被 `target.inputs[]` 接受。
- 前端**预校验**并给出可读原因（「分镜师需要 storyboard，当前无上游产出」）。
- 后端**再校验一次**（前端校验不可信）。校验失败 → 运行按钮禁用 + 节点高亮。
- **不做任意连线**：这保证拓扑可执行、可复现。若要放开，需给每个节点写输入适配层（另立项）。

### 5.3 编排能力 = 现在 `start_stage/end_stage` 的超集
| 现有能力 | 画布上如何表达 |
| --- | --- |
| `start_stage` | 「从此节点开始」——上游节点置灰为「复用已有产物」 |
| `end_stage` | 「到此节点结束」——下游节点置灰为「不执行」 |
| `enabled_checkpoints` | 节点上的 ⚑ 开关 |
| `retry_shots` | 质检节点内勾选镜头 → 只重跑这些 |
| `seed` | 「复用上次产物」开关（读上次 payload 作为输入） |
| **新增** | 多段/分支编排、**工程文件保存复用**、拓扑级校验 |

---

## 6. 前端依赖的后端接口（重写的前提）

按 `02-refactor-plan.md §2.4`，前端重写需要后端先提供（或并行提供）以下接口：

| 优先级 | 接口 | 前端用途 | 现状 |
| --- | --- | --- | --- |
| **P0** | `GET /api/registry` | 节点库、属性面板、端口、确认点 —— **一切的单一数据源** | ❌ 需新增 |
| **P0** | `GET /api/sessions/{id}/panel/{role}` | 单节点按需取数（替代整包 payload） | ❌ 需新增 |
| **P1** | `GET /api/sessions/{id}/events` (SSE) | 阶段进度、AI 思考流、job 进度 | ❌ 需新增 |
| **P1** | `POST /api/sessions` / `GET /api/sessions/{id}` | 会话创建与元信息（不含产物） | 🔶 部分可用（`/api/crew/run`、`/status`） |
| **P1** | `POST /api/sessions/{id}/resume` | 节点内联 approve/redo/back | ✅ 可用（`/api/crew/resume`），但需补 checkpoint 存在性校验 |
| **P2** | `POST /api/sessions/{id}/plan` | 提交编排计划与拓扑 | 🔶 现靠 `seed` 传，需正式化 |
| **P2** | `GET/PUT /api/workflows/{doc}` | 工程文件保存/复用 | ❌ 需新增 |

> **并行策略**：后端 P0 未就绪期间，前端可用一份 `registry.json` **快照夹具**（由后端注册表
> 导出生成，不手写）驱动开发；接口就绪后切换，避免前端阻塞。

---

## 7. 设计系统迁移（视觉连续性不丢）

现有 `_styles.py` 的 Sentry 设计 token **直接搬成 CSS 变量**，无需重新设计：

```css
:root{
  --s-primary:#150f23;      /* midnight violet 主背景 */
  --s-ink-plum:#1f1633;     /* 卡片底 */
  --s-lime:#c2ef4e;         /* electric lime 主强调/主按钮 */
  --s-pink:#fa7faa;  --s-violet:#6a5fc1;  --s-violet-deep:#422082;  --s-violet-mid:#79628c;
  --s-hairline:#362d59;     /* 描边 */
  --s-muted:rgba(255,255,255,.72);
}
```
字体沿用 `Rubik`（UI）+ `Monaco`（代码/提示词）。
> 注：当时参考的 `DESIGN.md`（Sentri 风格设计分析）**已在 UI 重做时删除** ——
> 那套紫罗兰/荧光绿的语言与本项目的导演台形态不匹配，现用 `web/src/styles/tokens.css`
> 里从零定义的设计系统（近黑 + 唯一强调色 + 光晕纵深）。本节余下内容属于当时的规划记录。

现状 CSS 里大量 `!important` 覆盖 Streamlit 内部 `data-testid` 的写法**全部丢弃**——
那是框架对抗，新前端不再需要。

---

## 8. 认证方案变更（需注意的破坏点）

| | 现在 | 重写后 |
| --- | --- | --- |
| 存储 | `streamlit-cookies-manager` 普通 cookie（JS 可读） | 服务端会话 + **HttpOnly cookie**（或 Authorization header） |
| 刷新恢复 | 为等组件回传 cookie，`st.rerun()` 轮询最多 10 次（抖屏） | 一次 `GET /api/auth/me` 静默恢复 |
| 登出 | 各页自行清 session_state + cookie | 单一 `POST /api/auth/logout` |

⚠️ 这要求后端补 `/api/auth/me`、`/api/auth/logout`（现有仅有 register/login）。
现有 token 表结构（`users.token/token_at`、90 天 TTL、登录轮换）可继续用，不必重建用户体系。

---

## 9. 分阶段清单（前端侧）

| 阶段 | 交付 | 完成判据 |
| --- | --- | --- |
| **F0 脚手架** | Vite + TS + Tailwind + token；`api/client.ts`；注册表快照夹具 | `pnpm dev` 起得来；能渲染节点库 |
| **F1 画布只读** | React Flow + NodeCard；从 registry 渲染 13 节点、端口、状态 | 能加载真实会话并显示各节点状态与缩略图 |
| **F2 画布可编辑** | 拖入节点、连线（含合法性校验）、位置持久化 | 连出完整链；非法连线给出可读错误 |
| **F3 运行与实时** | 运行/暂停/停止；SSE 驱动节点状态与思考流 | 运行中只有活跃节点在变；思考流实时滚动 |
| **F4 编排与确认** | 属性面板（职业级配置）、⚑ 确认点内联 approve/redo/back、重拍选镜 | 不开任何旧页面即可走完一次完整创作 |
| **F5 工程与收尾** | 工程文件存/取/复用；登录/设置/历史页；删旧前端 | 旧 `frontend/` 可整体移除 |

---

## 10. 需要你确认的

1. **技术栈是否认可**（React + TS + Vite + React Flow + Tailwind；或你另有偏好）。
2. **新前端目录用 `frontend/`（覆盖旧的）还是 `web/`**（与旧前端并存灰度）——我建议 `web/`，切换完再删旧。
3. **D2 仍未定**：那 5 个未提交的 local-gen 改动先 `git commit` 成基线节点，还是并入 provider 重构。
4. **是否现在就把 F0 脚手架搭起来**（可独立于后端进度推进）。

> 建议顺序：**先把 `git commit` 落了（D2）**，再开 F0。这样任何时候都能回到「重构前最后可用状态」。
