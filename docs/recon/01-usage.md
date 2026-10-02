# 小剧组 Little Crew — 现状说明（重构基线）

> 本文是**重构前**的实测基线。所有结论标注了证据来源；凡未经实测的推断都明确写成「未验证」。
> 采样时间：2026-10-02（Asia/Shanghai）。仓库：`D:\AI\ds.6`，分支 `main`。

---

## 1. 一句话定位与当前形态

基于 LangGraph 多智能体协作的 AI 漫剧生成平台：一句话创意 → 大纲 → 剧本 → 剧评 → 美术资产 →
分镜 → 节奏/连贯性 → 提示词 → 画面 → 质检 → 聚合，带 9 个人工确认点。

**当前形态（重构要替换的对象）**：后端 FastAPI 单文件 + 前端 **Streamlit 多页应用**——
主页 + 13 个职业页 + 5 个部门页 + 登录/设置/我的创作。交互模型是
**「一个职业 = 一个独立页面」**，通过全页跳转（`st.switch_page`）串联。

**目标形态（本次重构）**：**单一「导演台」**，各职业从页面降级为**按需提取的可编排组件**，
在同一个画布上组织工作流。

---

## 2. 运行状态（实测）

| 项 | 实测结果 | 证据 |
| --- | --- | --- |
| Python | 3.12.13（`.venv`） | `.venv\Scripts\python.exe -V` |
| 依赖完整性 | `pip check` → No broken requirements | 逐项比对 `requirements.txt` 约束 |
| 关键版本 | langgraph 1.2.11 / fastapi 0.141.1 / streamlit 1.60.0 / torch 2.14.0 / streamlit-cookies-manager 0.2.0 | `pip list` |
| 应用可导入 | `import app.main` OK；图编译 **25 节点**（23 业务 + START/END） | `app_workflow.get_graph().nodes` |
| 命令行全流程 | ✅ exit 0，12 个环节全跑通（mock） | `scripts/run_demo.py` |
| 后端 API | ✅ `/health` → `{"status":"ok"}`；无 token 访问受保护路径 → **401** | 起 `run_server.py` 后 HTTP 探测 |
| 前端 | ✅ 根路径 200、`/_stcore/health` → `ok` | 起 streamlit 后探测 8501 |
| 回归测试 | ✅ **6 passed in 1.65s** | `pytest tests/test_character_consistency_e2e.py -q` |
| 数据库 | 13 sessions / 10 users / 3 api_settings / 1 profiles / 2 critic_history | `data/little_crew.db` |
| 产物 | `data/outputs` 共 166 文件 ≈ 116 MB（165 png + 1 mp4） | 目录统计 |

**结论**：项目**当前是可运行、可测的**——这是重构最重要的前提，基线已锁定。

⚠️ 本机 `local-gen` 服务（8100）**未运行**；8000/8501 在勘察结束时均已停止。

---

## 3. 怎么用：四条入口（全部实测）

### 3.1 一键启动
`start.bat`：校验 `.venv` → 起后端 8000（`scripts/run_server.py`）→ 起前端 8501（streamlit）
→ `timeout 8s` → 打开浏览器。两个黑窗需保持开启。

### 3.2 命令行全流程（无服务、无密钥）
```powershell
.venv\Scripts\python.exe scripts\run_demo.py "一只会说话的小猫想当侦探" [session_id]
```
实测产出：`story_outline` / `script` / `storyboard`(3 镜) / `prompts`(3) / `images`(3) /
`character_*` / `scene_*` / `rhythm_plan` / `continuity_report` / `quality_report` /
`thinking_log`(12 条) 全部生成。

> 注意：此路径**不写 `sessions` 表**（`run_once` 直接返回），且产物归档有缺陷，见 §6.2。

### 3.3 后端 API
```powershell
.venv\Scripts\python.exe scripts\run_server.py   # http://127.0.0.1:8000/docs
```
26 个端点。中间件 `auth_guard` 对非公开路径强制要求 `Authorization: Bearer <token>`；
业务层再用 `_require_owner()` 校验会话属主（防 IDOR）。

### 3.4 前端
```powershell
.venv\Scripts\python.exe -m streamlit run frontend\streamlit_app.py --server.port 8501
```
未登录会被硬性重定向到登录页（`handle_nav(allow_anon=False)`）。

---

## 4. 端到端流程（实测轨迹）

### 4.1 HITL 确认点闭环（`HITL_ENABLED=1`）
```
POST /api/crew/run            → {"status":"accepted","session_id":"17b1a62a"}
GET  /api/crew/status/{id}    → status=review phase=review thinking=3
                                首个确认点 node=critics review_point=5 title='文学策划审核'
POST /api/crew/resume redo    → 回到 critics 重新生成，再次停在 review
POST /api/crew/resume approve → 前进到下一确认点 thinking=4
```
**续跑机制**：`Command(resume={decision,feedback,content})`；`redo` 回到对应阶段节点（不重跑上游），
`approve` 放行；`back` 回到上一步。

### 4.2 seed 续跑（前端「从某一环开始」的后端支撑）
带 `seed` + `start_stage=storyboarder` 启动，预置 outline/script/storyboard：
`writer` **未执行（被跳过）**，流程从分镜继续。✅ 机制有效。

### 4.3 走完全程
连续 6 次 `approve` 后 `status=done phase=done`，落库 `images=3 storyboard=1 prompts=3
thinking_log=6`，`quality_report.overall_score=0.9`、`consistency_score=95.0`。

### 4.4 中文编码
- 经 API（Python httpx）落库：`user_input` = `'一只会说话的小猫想当侦探'` → **正常**。
- 用 PowerShell 5.1 发中文会变成 `????`（ANSI 发送）→ **是探针假象，不是项目 bug**。
  后续自动化测试请用 Python httpx，勿用 `Invoke-RestMethod` 拼中文 body。

---

## 5. 数据与配置流

### 5.1 配置优先级（详见 `app/services/runtime_settings.py`）
```
职业级覆盖  >  当前用户覆盖  >  _global 覆盖  >  .env / 环境变量  >  代码预设
llm_provider_{role} · image_provider_{role} · video_provider_{role} · local_* 等
```
- 运行时设置持久化在 `sessions` 同库的 `api_settings(user_id, payload)`，**Fernet 加密**，
  密钥文件 `data/.little_crew.key`（gitignored）。
- 前端设置页（`frontend/pages/9_设置.py`）支持每个职业独立选模型/Key，并有「测试通道」
  返回错误分类（auth / network / model / rate_limit / unwired）。

### 5.2 产物归档
```
data/outputs/<账号UID 或 _global>/<部门中文名>/<职业 key>/<会话ID>/<shot_id>.png
```
- 归一化依赖 `runtime_settings.set_context(user, session)`；
  **只有服务端 `task_runner` 会调用它**。
- 目录名由 `app/core/org.py` 的 `ROLE_ORG` 映射（职业 → 部门/职业中文名）。

### 5.3 数据表
| 表 | 用途 | 现有行数 |
| --- | --- | --- |
| `sessions` | 会话状态/阶段/payload（含生成全量产物） | 13 |
| `users` | username/uid/token/salt/hash/token_at | 10 |
| `api_settings` | 运行时配置（加密） | 3 |
| `profiles` | 显示名/头像 | 1 |
| `critic_history` | 剧评修改历史（跨会话） | 2 |

---

## 6. 现状缺陷清单（证据充分，重构应一并解决）

### 6.1 确认点状态双写不一致（**最高风险**）
**实测**：会话 `17b1a62a` 停在确认点 → 杀进程 → 重启 → `resume` 返回 `accepted` **不报错**，
但 `thinking` 由 3 变 **0**，阶段依次出现 `review_script` → `review_assets` → `continuity_checker`
→ `quality_assessor`，最终又停在 `review`（`review_point=5`）。落库 `payload` 只有
`{node, review_point, title, content}`，`images=0 / storyboard=0 / thinking_log=0`。

**根因**：`compile_graph()` 用 `MemorySaver()`，图状态随进程消失；而 `sessions.status` 仍为
`'review'`，`/api/crew/resume` 仅校验该列即放行 → **DB 说「在等审核」，checkpointer 说「无此线程」**，
于是 `Command(resume=...)` 在空 checkpoint 上**从头重放整个图**。

**后果**：真实 provider 下静默重烧一遍 API 费用；前端显示上下文已空的「等待审核」会话。

### 6.2 CLI/测试路径产物落错目录
**实测**：`run_demo.py` 产出落在
`data/outputs/_global/技术制作部/photographer/no-session-S1-01/S1-01.png`。
**根因**：`_run_output_dir()` 读 `runtime_settings.run_session()`，而 `run_once()` 从不
`set_context()`，导致 `user→_global`、`sess→no-session-<shot_id>`。
现状：`_global` 109 文件（多为 no-session 调试产物），`local` 56 文件。

### 6.3 跳过节点导致进度陈旧
`_make_stage_node` 的跳过分支返回 `{"current_phase": stage_name}`，但**不经过 `_attach_thinking`**；
若节点返回空 dict，`task_runner` 的 `data.get("current_phase", node)` 便沿用上一环。
实测 `start_stage=storyboarder` 时经历阶段里仍出现 `critics`（实际被跳过）。
状态语义还需区分「完成 / 跳过 / 沿用」。

### 6.4 `api_settings` 加密降级 + 明文遗留
3 行中 **2 行为明文**（`{"llm_provider": "zh...`、`{}`），1 行为密文（`gAAAAAB...`）。
`_decrypt` 有向后兼容，但 `_encrypt` 失败时**静默返回明文**，无告警。

### 6.5 质量评分量纲不一致（真实不一致，未标注口径差异）
同一份 payload 里，质检页按 **0–100** 渲染（`f"{round(float(q)*100,1):.0f}"`，
`_score_frac = score/100`），但 `quality.py` 的 mock/CLIP 产出是 **0–1**；
实测 `quality_report.overall_score = 0.9` 与 `consistency_score = 95.0` 并列，
页面会显示「整体评分 0.9 / 100」。

### 6.6 开发期数据混入
`users` 表含 `sec_761832888` / `final_11564357` / `review_1725435688` / `verifyuser` 等测试账号；
`data/` 根目录有游离脚本 `e2e_real_drive.py`、`drv_review3.py`（不在 `scripts/`）。

---

## 7. 前端现状（重构主战场）

### 7.1 页面清单（28 个 .py）
| 类别 | 文件 | 说明 |
| --- | --- | --- |
| 主页 | `streamlit_app.py` | 快捷入口 + 5 部门卡片 |
| 职业页 ×13 | `pages/01_策划总监.py` … `pages/13_发行总监.py` | 多页应用的页面 |
| 部门页 ×5 | `pages/20_{创作策划,美术资产,导演分镜,技术制作,后期合成分发}部.py` | 各 5 行，纯跳板 |
| 其它 | `pages/7_我的创作.py`、`8_登录.py`、`9_设置.py` | 历史 / 登录 / 设置 |
| 基础设施 | `_api.py` `_styles.py` `_console.py` `_org.py` `_roles.py` `_segment_select.py` `renderers.py` `_dept.py` | 共享层 |

### 7.2 交互机制（重构时必须正视的约束）
- **Streamlit 侧边栏被 CSS 强制隐藏**（`[data-testid="stSidebar"]{display:none!important}`），
  导航改为手写卡片按钮 + `_nav_to` 标志 + `handle_nav()` 在回调外 `st.switch_page()`。
- **每次交互 = 整页 rerun**：无局部更新、无真正并排常驻面板。
- **登录态**靠 `streamlit-cookies-manager`，为等前端组件回传 cookie 需
  `st.rerun()` 轮询最多 10 次（`_cookie_try`），体验上是一次「加载态抖屏」。
- **设计系统是 170 行 CSS 字符串**（`SENTRY_CSS`），大量 `!important` 覆盖 Streamlit 内部
  `data-testid`，与框架实现强耦合。

### 7.3 职业页的真实结构（**重要：组件边界已天然存在**）
职业页本身极薄，例如 `pages/01_策划总监.py`（49 行）只做一件事：
```python
render_console_v2(
    role_key="writer", start_key="writer",
    eyebrow="创作策划部 · 策划总监", title="策划总监",
    subtitle="...", input_label="一句话创意", placeholder="...",
    render_result=_render_result,          # ← 纯函数：(payload) -> 渲染
    dept_page="pages/20_创作策划部.py",
)
```
`_console.py`（475 行）已把「输入区 + 分段选择 + 启动/轮询 + 进度 + 审核」全部收敛为一处。
**这意味着「职业组件化」的抽取成本比看起来低**——难点不在组件，而在**画布与状态模型**。

### 7.4 渲染层问题
- 每个面板渲染时都读 `st.session_state["payload"]`（**整个会话 payload**，含 images/
  characters 等），彼此复制、无共享缓存；`_console` 与各页反复 `backend_status()` 轮询。
- 质检页在脚本执行中 `time.sleep(1.2)` + `st.rerun()` 做「轮询」→ 阻塞式刷新。
- 重度依赖 `unsafe_allow_html` 手写 HTML 卡片，样式与逻辑混在 f-string 里。

---

## 8. 概念模型与「注册表」散落清单（**重构的收敛靶心**）

系统实际有 4 个正交概念，但**没有任何单一数据源**：

| 概念 | 含义 | 现状定义位置（重复定义 ⚠️） |
| --- | --- | --- |
| **职业 role** | 13 个可独立查看/触发的环节 | `app/core/org.py:ROLE_ORG`；`frontend/_org.py`；`_console.py:_ROLE_LABEL/_ROLE_TO_PAGE`；各 `pages/NN_*.py` 文件名 |
| **部门 dept** | 5 个分组 | `app/core/org.py:ROLE_ORG`（隐式）；`frontend/_org.py:DEPARTMENTS` |
| **阶段 stage** | 工作流 13 环节顺序 | `app/workflows/main_workflow.py:_STAGE_ORDER` + `_STAGE_LABEL`；`_console.py:_ALL_PHASES/_STEP_LABELS`；`_segment_select.py:FULL_ROLES`；`_roles.py:ROLES` |
| **确认点 checkpoint** | 9 个人工检查点 | `main_workflow.py:REVIEW_*_POINT` + 各 `review_*_node`；`_console.py:_CHECKPOINT_OPTIONS`；`_REVIEW_ROLE` |

另外 provider 列表 **3 处重复**：`app/core/config.py` 默认值、`frontend/pages/9_设置.py:PROVIDER_OPTIONS`
+ `PROVIDER_DEFAULT_MODEL`、`.env.example`；`app/services/image.py` 已有 4 个 `if provider ==` 分支。

> `_segment_select.py` 的注释已经自己写明「与后端 `main_workflow._STAGE_ORDER` 对齐」——
> 靠**人工注释保持同步**，这正是重构要用单一注册表消除的债。

---

## 9. 未验证项（重构前需补测，勿当作已确认）

1. **真实 provider 行为未验证**：`.env` 里 `MOCK_LLM=0`/`MOCK_IMAGE=0` 且密钥为空，
   真实 LLM/图像/视频链路本次**全部以 mock 验证**。已知 `ip_adapter_service` 把
   `ip_adapter_model`（`"cross-attention"`）当 Replicate version 传，真实调用很可能 404。
2. **local-gen 集成未验证**：`IMAGE_PROVIDER=local` / `VIDEO_PROVIDER=local` 是**未提交改动**
   （5 文件 +188 行），服务当前 DOWN，未做端到端出图/出视频验证。
3. **未提交改动未评审**：`git status` 显示 `.env.example`、`config.py`、`image.py`、`video.py`、
   `9_设置.py` 已修改但未提交——重构起点的基线应先把这批改动落定或明确纳入方案。
4. **并发/多用户未压测**：`task_runner` 的 `_THINKING/_CANCEL/_JOBS` 为进程内 dict；
   多用户并发、多进程（uvicorn workers）下的行为未验证。
5. **FFmpeg 配音未实现**：`video.py` 第 118–120 行为 `pass  # TODO`，幻灯片时长固定 4s。
6. **发布链路是骨架**：`distributor.py:_real_upload()` 直接 `raise NotImplementedError`。

---

## 10. 重构可用的护栏（已具备）

| 护栏 | 现状 | 重构中的用法 |
| --- | --- | --- |
| 回归测试 | 6 passed / 1.65s，全 mock 无网络 | 每次提交必跑；先扩充见下 |
| 冒烟脚本 | `run_demo.py` 约 10 秒跑完 12 环节 | CLI 路径回归 |
| API 契约 | `/api/crew/{run,resume,status}` 行为已实测固化 | 前端替换期的契约测试 |
| 数据快照 | 数据库已备份 `C:\Users\OnlyY\AppData\Local\Temp\ds6-recon\little_crew_20261002_144228.db` | 迁移/回滚对照 |

**建议先补 3 个测试再动手**（当前完全没有覆盖）：① 确认点续跑（含重启场景）；
② `seed`/`start_stage` 续跑；③ 产物归档路径。
