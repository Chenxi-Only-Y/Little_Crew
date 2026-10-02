# 🎬 小剧组 (Little Crew)

基于 **LangGraph 多智能体协作** 的 AI 漫剧自动生成平台。

用户输入一句话创意，系统通过 **多智能体接力** 协作，输出漫剧图文序列，
并可进一步合成视频片段：故事大纲 → 分场剧本 → 剧评 → 分镜 → 提示词 →
画面 → 质量评估 → 聚合 → 图片/视频产物。

---

## 🔧 技术栈

| 层 | 技术 |
| --- | --- |
| 工作流编排 | LangGraph (StateGraph + checkpointer + interrupt) |
| 后端 API | FastAPI + Python 3.11+ |
| 前端界面 | **导演台**：React + TypeScript + Vite（`web/`，端口 5183） |
| 画布 | @xyflow/react（节点画布） + Zustand |
| 预演（白模） | Three.js + @react-three/fiber（人形骨架 / 环境 / 多机位） |
| 大语言模型 | DeepSeek（OpenAI 兼容协议） |
| 图像生成 | 阿里百炼（Z-Image / wanx）/ Replicate |
| 视频生成 | MiniMax H3 · 阿里百炼（万相 / Vidu）· Seedance · 本地 H3 · FFmpeg |
| 视频编码 | ffmpeg（`imageio-ffmpeg` 自带一份，零配置可用） |
| 存储 | SQLite（开发）/ 可切 PostgreSQL |
| 配置 | python-dotenv + pydantic-settings |

> 所有外部能力均有 **mock 开关**（见下），无密钥/依赖也能端到端跑通；填入 `.env` 并置 0 切真实。

---

## 🧠 工作流（多部门接力 + 人工确认点 + 质量闭环）

```
START
  → story_planner  (策划总监)
  → screenwriter   (编剧)   → critics (文学策划/剧评)
  → review_critics (确认点: 文学策划审核)
  → review_script  (确认点: 剧本审核)
  → art_brief_parser (美术设定包)
  → character_designer ╮ (角色设计) ╮ 并行
  → scene_designer  ╯ (场景设计)    ╯
  → consistency_auditor (角色一致性评估)
  → review_assets  (确认点: 资产确认)
  → storyboarder   (分镜师) → review_storyboard (确认点: 分镜审核)
  → rhythm_director (剪辑指导/节奏)
  → continuity_checker (场记/连贯性)
  → review_rhythm / review_continuity (确认点)
  → prompt_compiler (技术指导/提示词) → review_prompts (确认点)
  → image_generator (摄影师/图片) → quality_assessor (质检) — 低于阈值自动回 image_generator 重试(≤3)
  → review_quality → review_images (确认点: 图片/质检审核)
  → aggregator (剪辑师/发行) → END
```

- **HITL**：`HITL_ENABLED=1` 时确认点用 `interrupt()` 原地暂停，人工可 **approve / redo(+feedback)**；续跑依赖 checkpointer，`redo` 回到对应阶段而非重跑上游，并注入反馈。
- **质量闭环**：`quality_assessor` 用 CLIP 风格评分（默认为占位），低于 `QUALITY_THRESHOLD` 自动重试。
- **部门视角**：前端把各`职业`归到 5 个部门（创作策划部/导演分镜部/美术资产部/技术制作部/后期合成分发部），可切换不同页面查看同一创作的不同环节。

---

## 🗂️ 目录结构

```
little_crew/
├── app/
│   ├── core/
│   │   ├── config.py            # 环境/密钥/mock 开关/限额等
│   │   └── state.py             # LangGraph CrewState (TypedDict)
│   ├── agents/                  # 各环节智能体（继承 BaseAgent）
│   │   ├── base.py / _utils.py
│   │   ├── planner.py / writer.py / critics.py
│   │   ├── shot_composer.py / prompt_refiner.py
│   │   ├── image_generator.py / quality_assessor.py
│   │   └── aggregator.py
│   ├── registry/                # 唯一事实来源：职业/节点/端口/校验规则
│   │   ├── roles.py             # 7 部门 / 16 职业 / 18 节点 / 34 端口
│   │   ├── api.py               # allowed_settings() 等派生逻辑
│   │   └── types.py
│   ├── workflows/main_workflow.py  # 图编排 + 确认点 + HITL
│   ├── services/
│   │   ├── llm.py / image.py / quality.py / video.py
│   │   ├── providers.py         # 提供者能力声明（条件/时长/分辨率）
│   │   ├── video_adapters.py    # 多家视频 API 的统一适配层
│   │   ├── whitebox_video.py    # 白模帧序列 → mp4 → 上传换 file_id
│   │   ├── runtime_settings.py  # 按用户的运行时设置（加密落库）
│   │   ├── session_store.py     # SQLite 会话/结果 + 用户历史
│   │   ├── auth.py              # 注册/登录/token
│   │   ├── task_runner.py       # 后台线程跑工作流 + 进度
│   │   ├── quota.py             # 限流/每日配额
│   │   └── logging.py           # 日志 + 密钥脱敏
│   └── main.py                  # FastAPI 入口（所有端点）
├── web/                         # 导演台（React + Vite，端口 5183）
│   ├── src/app/router.ts        # 5 个页面：首页 / 导演台 / 预演 / 用户 / 设置
│   ├── src/canvas/              # 节点画布（ComfyUI 式）
│   ├── src/previs/              # 3D 白模预演编辑器
│   │   ├── humanoid.ts          # 17 关节骨架 + 姿态 + 前向运动学
│   │   ├── env.ts               # 环境预设（房间/广场/街道/舞台）
│   │   ├── types.ts             # 多机位轨道 + 锁定跟随
│   │   ├── exporter.ts          # 剪影/深度/帧序列导出
│   │   └── whiteboxVideo.ts     # 白模参考视频上传封装
│   └── src/registry.snapshot.json  # 后端注册表导出（构建时校验一致性）
├── scripts/                     # run_demo / run_server / check_secrets / export_registry
├── docs/recon/                  # 重构勘察记录（01~10）
├── data/outputs/                # 图片/结果（gitignored）
├── .github/workflows/ci.yml     # CI（安全扫描 + 测试 + 密钥防泄漏）
├── requirements.lock            # 环境锁定（pip freeze）
├── Dockerfile / docker-compose.yml  # 只含后端；前端容器化见 compose 注释
├── .env.example / requirements.txt / LICENSE / README.md
└── DESIGN.md                    # 导演台的品牌设计令牌（配色/字体）
```

---

## 🚀 快速开始

### 1. 安装依赖（每个用户建自己的虚拟环境）

> `.venv/`、`data/outputs/`、`*.db` 均已 gitignore，不随仓库提交；请勿直接用系统 Python 或他人环境的虚拟环境，避免相互污染。

```bash
# 1) 创建虚拟环境（该命令会自动装上 pip，关键）
python -m venv .venv

# 2) 激活
# Windows (PowerShell):
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate

# 3) 升级 pip（可选，推荐）
python -m pip install --upgrade pip

# 4) 安装全部依赖
pip install -r requirements.txt
```

> **排错**：如果 `python -m venv .venv` 后仍报
> `No module named pip`，说明基础 Python 不完整，补一条即可：
> ```bash
> python -m ensurepip --upgrade
> ```
>
> **PyCharm 用户**：`Settings` → `Project` → `Python Interpreter` → `Add Interpreter` →
> `Existing` → 选 `.venv\Scripts\python.exe`（Linux/macOS 为 `.venv/bin/python`），
> 由 PyCharm 用该解释器执行 `pip install -r requirements.txt`。

> 依赖较重的有 `sentence-transformers`、`clip`、`replicate`（角色一致性/质量评估用，会拉 torch）。
> 若只需核心"文字→图片"链路、不想下这么大，可先手动安装 `fastapi uvicorn httpx langgraph langchain-openai python-dotenv pydantic-settings pillow imageio-ffmpeg`，其余按需再补。

### 2. 配置 `.env`

```bash
cp .env.example .env
```

`.env.example` 默认全部 mock（零密钥零成本）。接入真实模型时填：

```env
DEEPSEEK_API_KEY=sk-...            # 大模型（DeepSeek）
MOCK_LLM=0
MOCK_IMAGE=0                        # 图像走真实 API
DASHSCOPE_API_KEY=sk-...            # 阿里百炼
IMAGE_PROVIDER=dashscope            # dashscope | replicate
IMAGE_MODEL=z-image-turbo           # z-image* 走 multimodal 同步；其他走 wanx 异步
# 可选开关
SHOT_MODE=total                     # total=全片5-8镜 | per_scene=按场细分
HITL_ENABLED=0                      # 1=人工确认点交互
MOCK_QUALITY=1  QUALITY_THRESHOLD=0.5
MOCK_CRITICS=1
MOCK_VIDEO=1                        # 0 需本机 FFmpeg + edge-tts
RATE_LIMIT_PER_MINUTE=10  QUOTA_DAILY_GENERATIONS=5  LOG_LEVEL=INFO
```

### 3. 运行

```bash
# 命令行一次性跑通（非 HITL）
python scripts/run_demo.py "一只会说话的小猫想当侦探"

# 后端 API（异步任务 + 状态轮询 + 登录 + 历史 + 视频）
python scripts/run_server.py        # http://127.0.0.1:8000/docs

# 导演台（前端）——需要 Node.js 与 pnpm
cd web
pnpm install                        # 首次
pnpm run dev                        # http://127.0.0.1:5183
```

> **Windows 用户可以直接双击 `start.bat`**：它会检查 `.venv` 与 `pnpm`、
> 首次自动装前端依赖、拉起后端与导演台并打开浏览器。

前端在开发期把 `/api` 代理到 `http://127.0.0.1:8000`（见 `web/vite.config.ts`），
所以前后端同源、无需处理 CORS。

```bash
# Docker 一键起后端 + 导演台
docker compose up --build
#   导演台  http://127.0.0.1:8080
#   后端    http://127.0.0.1:8000/docs
```

容器化的做法与几个取舍：

| 项 | 做法 | 为什么 |
| --- | --- | --- |
| 前端镜像 | **多阶段**：`node:22-alpine` 里 `pnpm build` → `nginx:alpine` 托管 `dist` | 后端镜像不必多背一个 Node 运行时与整套 devDeps |
| 构建上下文 | **仓库根**（`context: .` + `dockerfile: web/Dockerfile`） | Dockerfile 要 `COPY web/package.json`，从 `web/` 作上下文找不到 |
| `/api` 反代 | nginx `proxy_pass http://api:8000` | 保持**同源**：前端代码里全是相对路径 `/api/...`，同源就不用处理 CORS，也不用分"开发走 vite 代理、生产走别的" |
| SSE | `proxy_buffering off` + `proxy_read_timeout 3600s` | `/api/sessions/{id}/events` 是长连接事件流。不关缓冲的话事件会被 nginx 攒着不发，界面进度看起来"卡住不动"，而后端日志一切正常 |
| 静态托管 | 交给 nginx，**不塞进 FastAPI** | FastAPI 的鉴权是**全局中间件**；由它托管静态文件的话，未登录访问页面会先被 401 挡住，连登录页都看不到 |
| 资源缓存 | `assets/` 长期缓存（带内容哈希）、`index.html` 不缓存 | 反过来的话，前端发版后用户仍拿旧 index，而它引用的 assets 已被替换 → 白屏 |
| 启动顺序 | `depends_on: condition: service_healthy` | nginx 一启动就要反代 `/api`，后端没就绪会让首次访问拿到 502 |

> 上传白模帧序列（15 秒 × 24fps 的 PNG）可能较大，nginx 侧设了
> `client_max_body_size 128m`，避免被 413 提前拒掉。

### 4. 模型凭据填在哪

| 能力 | 密钥键 | 位置 |
| --- | --- | --- |
| 文字（LLM） | `llm_api_key_{角色}` | 设置页 → ② 按职业指定模型 |
| 图像 | `image_api_key_{角色}` | 设置页 → ② |
| **视频** | `minimax_api_key` / `dashscope_api_key` / `ark_api_key` | **设置页 → ③ 模型凭据** |

视频的密钥是**提供者级**的（与按职业的命名不同），必须填在 ③。
想用白模参考视频（把预演录像送进视频模型复刻运镜），就得先配这个 Key。

---

## 🔌 API 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/auth/register` | 注册 `{username,password}` → token |
| POST | `/api/auth/login` | 登录 → token |
| POST | `/api/crew/run` | 异步启动创作（限流+每日配额）→ `{session_id}` |
| GET | `/api/crew/status/{id}` | 轮询进度/结果/确认点 |
| POST | `/api/crew/resume` | 确认点 approve/redo(+feedback) |
| GET | `/api/crew/history/{uid}` | 用户创作历史（按 token 隔离） |
| POST | `/api/crew/video/{id}` | 对已完成会话合成视频（mock/真） |

认证：登录后后续请求带 `Authorization: Bearer <token>`；`/run`、`/history` 按 token 用户隔离。

---

## 🔐 Keys & Security

- 密钥只经 `config.py` 从 `.env`/系统环境变量读取，代码无硬编码（`check_secrets.py` 可自检）。
- `.env`、`data/`、`*.db` 均被 gitignore；仓库只提交 `.env.example`。
- 日志/异常经 `redact()` 脱敏，不泄漏密钥。
- **登录态持久化**：导演台把 token 存在浏览器 `localStorage`（键 `lc_token`），刷新/重开不掉线。该 token 为**普通 JS 可读、非 HttpOnly**，本地/演示够用；公网部署建议升级为「服务端会话 + HttpOnly Cookie」，不要把凭据放进 URL。SSE 订阅因浏览器 `EventSource` 无法自定义请求头，token 需走 `?token=` 查询参数——非 HTTPS 部署下会明文出现在 URL 与日志中，务必配 HTTPS。
- 开源许可证：**MIT**（见 `LICENSE`）。集成外部模型请留意各厂商条款。

---

## 🏷️ 里程碑对照（10 周规划）

| 阶段 | 状态 |
| --- | --- |
| 核心工作流（文本） | ✅ 多智能体 + 全链路 |
| 视觉接入（图像/质量） | ✅ z-image 真实出图 + CLIP 质量回环 |
| 用户体系 | ✅ 注册/登录、token、历史、按用户隔离 |
| 生产化 | ✅ 异步任务、持久化、限流/配额、日志、错误兜底、Docker |
| 完整版扩展 | ✅ 剧评、质量评估、视频合成（mock 可跑/真实现待 FFmpeg） |
| ⬜ 待做 | 真视频端到端、PostgreSQL/Redis 切换、一键分发、小说输入等 |
