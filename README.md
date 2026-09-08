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
| 前端界面 | Streamlit（多页） |
| 大语言模型 | DeepSeek（OpenAI 兼容协议） |
| 图像生成 | 阿里百炼（Z-Image / wanx）/ Replicate |
| 视频合成 | FFmpeg + Edge TTS |
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
│   ├── workflows/main_workflow.py  # 图编排 + 确认点 + HITL
│   ├── services/
│   │   ├── llm.py / image.py / quality.py / video.py
│   │   ├── session_store.py     # SQLite 会话/结果 + 用户历史
│   │   ├── auth.py              # 注册/登录/token
│   │   ├── task_runner.py       # 后台线程跑工作流 + 进度
│   │   ├── quota.py             # 限流/每日配额
│   │   └── logging.py           # 日志 + 密钥脱敏
│   └── main.py                  # FastAPI 入口（所有端点）
├── frontend/
│   ├── streamlit_app.py         # 主页（登录/卡片入口）
│   ├── _styles.py               # Sentry 设计系统 CSS + 导航/登录态(cookie)
│   ├── _api.py                  # 后端/本地调用封装
│   ├── renderers.py / _console.py / _dept.py  # 各环节结果渲染 / 控制台 / 部门
│   └── pages/
│       ├── 01~13_*.py           # 各职业环节页（策划/编剧/角色/场景/分镜/剪辑/场记/技术/摄影/质检/发行…）
│       ├── 20_*.py              # 5 个部门入口页（创作策划/导演分镜/美术资产/技术制作/后期分发）
│       ├── 7_我的创作.py         # 历史回顾
│       ├── 8_登录.py            # 登录/注册
│       └── 9_设置.py            # 模型/API Key/生成参数
├── scripts/                     # run_demo / run_server / check_secrets
├── data/outputs/                # 图片/结果（gitignored）
├── .github/workflows/ci.yml     # CI（安全扫描 + 测试 + 密钥防泄漏）
├── requirements.lock            # 环境锁定（pip freeze）
├── Dockerfile / docker-compose.yml
├── .env.example / requirements.txt / LICENSE / README.md
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
> 若只需核心"文字→图片"链路、不想下这么大，可先手动安装 `fastapi uvicorn streamlit httpx langgraph langchain-openai python-dotenv pydantic-settings`，其余按需再补。

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

# 前端
streamlit run frontend/streamlit_app.py   # http://localhost:8501

# Docker 一键起后端+前端
docker compose up --build
```

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
- **登录态持久化**：前端用浏览器 cookie（`streamlit-cookies-manager`）存 token，刷新/关闭重开浏览器不掉线。cookie 为**普通 JS 可读、非 HttpOnly**，本地/演示够用；公网部署建议升级为「服务端会话 + HttpOnly Cookie」，不要把凭据放进 URL。
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
