# 小剧组 · 导演台（前端重写）

ComfyUI 式**节点画布**，取代旧的 Streamlit 十三职业页。
设计依据：`docs/recon/03-before-after.md`（形态对照）、`docs/recon/04-frontend-rewrite.md`（技术准备）。

## 快速开始

```bash
cd web
pnpm install
pnpm dev          # http://127.0.0.1:5183（/api 代理到后端 8000）
```

后端可选：`python scripts/run_server.py`。
**后端没起也能看**——前端会回退到内置的注册表快照（见下）。

其他命令：

```bash
pnpm build            # 产物到 dist/
pnpm typecheck        # tsc 类型检查
pnpm check:parity     # 校验器等价性：与后端 /api/registry/validate 比对（需后端已启动）
```

## 唯一硬约束

> **前端不得自己定义职业 / 部门 / 阶段 / 产物 / provider 清单——一律来自后端注册表。**

原因见 `docs/recon/04-frontend-rewrite.md §1`：v1 里同一概念散落在 5 个文件
（`app/core/org.py`、`frontend/_org.py`、`_console.py`、`_segment_select.py`、
`main.py`），并且**已经真实不一致**（`continuity` vs `continuity_checker`）。

因此 `src/canvas/*` 只渲染，不做业务判断；端口能否连接由注册表的 `consumes/produces` 决定。

## 注册表来源

```
1. GET /api/registry     ← 首选（需要登录 token）
2. src/registry.snapshot.json  ← 兜底（未登录/后端不可达时）
```

快照由后端**直接导出**，不是手写 fixture：

```bash
python scripts/export_registry.py     # 从 app/registry 导出到 src/registry.snapshot.json
```

改动注册表后请重新导出，否则前端看到的仍是旧结构。

## 目录

```
src/
├── api/registry.ts       注册表加载（接口优先、快照兜底）
├── domain/registry.ts    与后端契约一一对应的类型（唯一来源）
├── store/canvas.ts       画布状态（Zustand）：拓扑、布局、选中
├── canvas/
│   ├── Canvas.tsx        React Flow 容器（拖拽/缩放/框选/拖入）
│   ├── NodeLibrary.tsx   左侧节点库（按部门分组）
│   ├── RoleNode.tsx      节点桥接（注册表 -> NodeCard）
│   ├── NodeCard.tsx      节点卡片（状态/端口/确认点徽标）
│   └── Inspector.tsx     右侧属性面板
└── styles/tokens.css     设计 token（从旧 _styles.py 迁移，去掉 !important 对抗）
```

## 页面结构（就这五页）

| 路由 | 页面 | 内容 |
| --- | --- | --- |
| `#/home` | 首页 | 配置就绪提示、入口卡片、我的工程、最近创作 |
| `#/director` | 导演台 | 节点库 + 画布 + 属性面板；运行栏、校验、工程栏都在**本页** |
| `#/previs` | 预演 | 3D 白模编辑器：摆走位、录机位、导出结构帧 |
| `#/user` | 用户 | 昵称、头像、账号信息、退出 |
| `#/settings` | 设置 | ① 云端/本地（必选）② 按职业指定模型 ③ 费用提醒 |

路由用**哈希**（`app/router.ts`，约 60 行自实现）：纯静态构建不需要服务端 rewrite，
刷新任意页面都不会 404。没有引 react-router —— 几个页面用不上，
让全部页面依赖一个路由库不划算。

运行相关操作（▶ 运行、工程保存）**只在导演台**，不放全局顶栏：
它们只对当前画布有意义。

预演编辑器**按需加载**（`React.lazy`）：它带 Three.js（约 910 KB），
静态引入会让首页也得先下载完 3D 引擎（实测单块涨到 1.38 MB）。
拆开后主包 477 KB，点进「预演」才拉那 910 KB。

## 3D 预演编辑器（#/previs）

用途（路线一「文生图 → 图生白模 → 白模生视频」的白模那一环）：
**先把「谁在哪、相机怎么走」摆出来，再导出结构帧喂给视频模型**，
解决视频模型自己编空间导致的漂移、瞬移、比例失控。

| 能力 | 实现 |
| --- | --- |
| 场景对象 | 人物（柱体+头球）、道具（盒体），可拖拽或数值编辑 |
| 相机 | **关键帧驱动**（不是鼠标控制）；在时间轴上加关键帧、线性插值 |
| 播放 | 按镜头 fps 步进，保证预览与导出看到同一串帧 |
| 导出 | 原生 Three.js 逐帧渲染 → `readPixels` 读回 → 自写 PNG 编码 |

三条设计决定：

1. **纯白模**：全部 `meshBasicMaterial`，无光照着色。白模只表达形状与位置；
   带材质光照的画面会让视频模型学走不该学的东西。
2. **导出时隐藏辅助线**：网格线与坐标轴若留在画面里，会被模型当成真实结构。
   用 `userData.helper` 标记，导出时统一隐藏、结束恢复。
3. **不用 `canvas.toDataURL()`**：WebGL 绘制缓冲在合成后可能被清空，会拿到全黑图。
   改为 `readPixels` 主动读回，再用浏览器内置的 `CompressionStream('deflate')`
   自己编 PNG（不到 80 行，不加依赖）。

⚠️ 逐帧结构控制（ControlNet-Union）**尚不可用**：本地只有 FL2VA 权重，
缺 ControlNet 分支，`local-gen` 也没有 `control_video` 路由。
目前预演产物能走的是**首尾帧**通道（`first_frame` / `last_frame`）。

## 进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| F0 | 脚手架（Vite + React + TS + Tailwind token） | ✅ |
| F1 | 画布只读：全部节点 + 端口 + 状态 + 自动布局/连线 | ✅ |
| F2 | 可编辑：拖入节点、**连线合法性校验**（拖线即时反馈 + 顶部可执行性状态） | ✅ |
| F3 | **运行与实时**：运行栏、AI 思考流、节点实时点亮（SSE） | ✅ |
| F4 | **编排与确认**：确认点内容展示 + 通过 / 退回重做 / 返回上一步 | ✅ |
| F5 | **属性面板可编辑**：注册表 config 字段 → 控件，改动即时保存 | ✅ |
| F5 | **工程文件**：保存 / 另存为 / 打开 / 新建 / 改名 / 删除 | ✅ |
| F5 | **登录页**：登录 / 注册 / 退出；未登录挡住整个导演台 | ✅ |

旧 Streamlit 前端（`frontend/`）已被本导演台取代，保留可运行作灰度回退；
在完成一轮真实创作确认无回归后即可下线。

### 登录（F5）

- token 存在 `localStorage`（`lc_token` / `lc_user`），退出时清空。
- 启动顺序：**先验登录态 → 再拉注册表**。注册表是公开端点，未登录也能拉到；
  若顺序反了，未登录用户会先看到画布闪一下再被踢回登录页。
- 失效的 token 会被识别并清除，避免"界面像已登录、每个请求都 401"的迷惑状态。
- 开发期仍可用 `?token=<token>` 直接进入（见 `api/auth.ts`）。

⚠️ **已知安全取舍**：`EventSource`（SSE）无法自定义请求头，token 只能放查询参数，
因此后端日志里的 SSE URL 会含 token。本机/局域网自用可接受；
**若要暴露到公网，应改成一次性 ticket**（先 POST 换短效 ticket，再用 ticket 订阅 SSE）。

### 工程文件（F5）

一个"工程" = 画布形状（放哪些节点、连了哪些线、各节点**参数与备注**）。
运行结果不在这里（那是运行历史），**密钥也不在**（后端写入前掩码）。

| 端点 | 说明 |
| --- | --- |
| `GET /api/projects` | 列出当前用户的工程（按最近修改排序） |
| `POST /api/projects` | 保存；带 `project_id` 即覆盖，不带则新建 |
| `GET /api/projects/{id}` | 读取 |
| `DELETE /api/projects/{id}` | 删除 |
| `POST /api/projects/{id}/rename` | 改名 |

设计要点：

- **整份 JSON 加密入库**（不是固定列）：工程结构会随前端演进，固定列每加字段就要迁移；
  整份 JSON 让旧工程能被新版本读出来（缺失字段按默认补）。
- **边不存 `artifact`/`id`**：存了会在节点改名后失效，加载时从两端节点的
  `produces`/`consumes` 重新推导。
- **加载时只保留注册表里仍存在的节点**，并**明确告知丢弃了什么** ——
  静默丢弃会让画布与保存时不一致，而用户以为"打开成功、内容完好"。
- 跨用户**读/改/删一律失败**（读返回 404 不暴露存在性，改返回 403）。

⚠️ `window.__lcStore` 暴露了 store（开发与自动化测试用）。
像"点了保存是否真的存住"这类跨 store→API→DB 的链路，只有驱动真实界面才能验证。

### 属性面板编辑（F5）

选中节点后，其配置项由注册表 `ConfigField` 渲染为控件并**即时保存**：

| 类型 | 控件 | 保存时机 |
| --- | --- | --- |
| `select` | 下拉 | 选择即存 |
| `text` / `number` / `password` | 输入框 | 失焦存 |
| `slider` | 滑杆 | 松手存 |
| `toggle` | 复选框 | 勾选即存 |

三条约定：

1. **密钥不回显** —— 后端把已配置的密钥掩码成 `••••已配置`；留空=沿用，重填=替换。
2. **清空 = 清除覆盖** —— 回退到「模式默认」或全局默认（后端语义：空串即清除）。
3. **写入是合并** —— 只改你动的那一个键，不会清掉其它设置。
   （`runtime_settings.set_many` 原先整体替换，曾导致改一个字段清空全部设置。）

### 人工确认点（F4）

工作流在 9 个确认点 `interrupt()` 暂停；`ReviewPanel` 显示该确认点内容并提供决策：

| 决策 | 后端参数 | 行为 |
| --- | --- | --- |
| 通过并继续 | `approve` | 放行，继续下一环节 |
| 退回重做 | `redo` + `feedback` | **回到同一环节重新生成**（不重跑上游） |
| 返回上一步 | `back` | 回到上一确认点 |

⚠️ 后端 `POST /api/crew/resume` 在**断点丢失**（服务重启过）时返回 **409**，
而不是静默重放整张图（那会重复计费）。前端已区分该情况并给出明确提示。

### 联调入口

后端起来后（`python scripts/run_server.py`），可直接带 token 打开导演台：
`http://127.0.0.1:5183/?token=<登录token>`

- `?token=` 会写入 localStorage 并用于 API 与 SSE
  （`EventSource` 无法自定义请求头，SSE 只能走查询参数——后端中间件已支持）
- `?autorun=<一句话创意>` 打开即自动运行一次（便于端到端观察运行态）

### 实时事件（SSE）

`GET /api/sessions/{id}/events` 推送：

| 事件 | 载荷 | 用途 |
| --- | --- | --- |
| `status` | `status` / `phase` | 整体状态（running / waiting_review / done / failed） |
| `stage` | `stage` / `status` / `workflow_node` | 单个环节状态（done / skipped / review） |
| `progress` | `nodes` / `current` | **完整快照**，晚连上也能一次对齐 |
| `thinking` | `label` / `text` | AI 思考过程增量 |
| `review` | `review` | 到达人工确认点 |
| `done` / `error` | — | 终态 |

⚠️ **去重必须做**：`EventSource` 断线会自动重连，而服务端 `replay=True` 会重发历史；
前端按 `seq` 去重（`store/canvas.ts` 的 `lastSeq`），否则思考日志会成倍重复。

### 命名空间（三套，别混淆）

| 命名空间 | 例子 | 用在哪 |
| --- | --- | --- |
| 注册表 key（v2） | `showrunner` / `script_editor` | 画布渲染、配置键、事件 |
| 工作流节点名（v1 实现） | `story_planner` / `critics` | `build_graph()` 的 `add_node` |
| 阶段名（= 配置键词表） | `writer` / `director` | `_STAGE_ORDER` / `_make_stage_node` |

翻译由 `registry.api.to_registry_node()` 负责；**事件统一输出注册表 key**，
否则按注册表渲染的节点点不亮。对应关系见 `Node.workflow_node`。

### 校验器不漂移的保障

前端为了"拖线即时反馈"自己实现了一份校验算法（`src/domain/validate.ts`），
规则数据来自后端下发的 `registry.validation_spec`。为防两边漂移：

```bash
pnpm check:parity   # 用 6 组真实用例比对前端与后端判定，必须全 OK
```

后端在执行前仍会**再校验一次**（`POST /api/registry/validate`），前端的判定不作为执行依据。

## 与旧前端的关系

旧前端在 `frontend/`（Streamlit），**暂时保留可跑**，便于灰度对比。
待 F5 完成后再整体移除。
