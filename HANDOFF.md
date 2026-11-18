# 交接与续跑（HANDOFF）

> 上下文到上限时先读这份，再继续。最后更新：2026-11-18。

## 当前状态

- 版本：**0.8.6**（版本线停在 0.8.*，不升 0.9/1.0）
- HEAD：**收口提交**（= `git log -1 --format=%h`；最后一个**代码**提交 = `4152a76`）
- 未推送提交：**4**（`d6f3e4b` #7 · `697960d` #8 · `4152a76` B1 · **本收口提交**；
  origin/main 当前 = `5021dfc`。**用户说"推"才推**）
- 全量：**3408/3408** + 架构守卫 PASS（2026-11-18 两次实跑：提交前 247.26s / 提交后 230.18s）
- dirty：0（收尾文档提交后）
- 镜像/tag：**`little-crew:0.8.6`（269MB，冒烟通过：/healthz 200、roster=19、doctor blocking=0）**；
  tag **`v0.8.6`** 指向最终提交（本地，未推送）；证据 `research/deploy/2026-11-18/`
- 本轮提交：分三条 **`feat(#7)` → `feat(#8)` → `feat(B1)`**（本文件在第一条提交里已更新到终态，
  后续只落代码/文档；三项在同轮完成）

## 已完成（不要重做）

- B1/B2/B3/B4/B5/A1/A2/A3/A4/A5 全部完成（详见 CHANGELOG 0.8.*）
- #6 门禁定位+修复：云端出图落盘前归一 engine_resolution（832x480）；
  CH01 复跑 gate kept 2/2、retry 0 ✅
- **#7 B2 自动循环（2026-11-18）**：边界审红 → VLM/契约 diff **自动生成 hint** → 只补拍边界镜
  → 复审；**重试上限 2**；仍红/契约红/无判官/无 shot **一律诚实升级人工**；
  接进 `series_run --retake-red --retake-max`（主+续跑分支）。
  自检 `boundary_retake_check` **70/70**；真实 VLM 判词→hint 真机一次（¥0.05）；
  证据 `research/series/b2-retake-loop-2026-11-18/`
- **#8 并行写台账锁（2026-11-18）**：系列台账改**按集增量 + 跨进程合并锁**
  （`O_CREAT|O_EXCL` 锁文件+原子写+陈旧锁可破+幂等）→ **允许并行开场景视图**。
  真实 3 集 `--mode parallel --scene-views`：3/3、¥0.30、墙钟 124.5s、**0 丢更新**；
  压力 1320 次并发合并 0 丢更新；自检 `series_bible_check` 27→**44/44**；
  证据 `research/series/ledger-lock-2026-11-18/`
- **B1 全职业插件化 20/20（2026-11-18）**：插件 API 加 `positions` 声明 + `default_roster`；
  19 职位迁 `plugins/core_crew`；总导演 `series_showrunner` 声明**第 20 职位**；
  名册/编制/依赖图/无限画布**从注册表构建**（默认仍是 19，卸载不影响默认流水线）。
  自检 `core_crew_check` **50/50**
- 基线卫生：`HANDOFF.md` 入项目根白名单（此前架构守卫一直 FAIL）；3 个套件期望数同步实际。
- **收尾（2026-11-18）**：全量 **3408/3408** PASS（提交后复跑 230.18s）· Docker 镜像
  **`little-crew:0.8.6`**（实构建 300.1s / 269MB + 容器冒烟通过）· **`tag v0.8.6`** 指向最终提交 ·
  旧镜像 `little-crew:0.4.1` 已删 · 证据 `research/deploy/2026-11-18/`

## 待做队列（按序）

1. ~~#7 B2 自动循环~~ ✅ / ~~#8 并行台账锁~~ ✅ / ~~B1 全职业插件化 20/20~~ ✅ / ~~全量回归+镜像+tag~~ ✅
2. **发布动作（等用户指示）**：推送本轮 3 个提交（tag 如需一起推再推）；**用户说"推"才推**
3. **人工项（按用户要求挂起）**：盲判/评分/录屏/答辩包
4. （可选，非阻塞）真机复跑"补拍后真 VLM 复审"端到端：本轮只验证到
   `VLM diff → hint` + 半真实循环，**补拍后真 VLM 复审/真云端出图未端到端跑**（要真花钱）

## 规范与限制（必须遵守；之前口头约定）

### 密钥/安全
- API key 只在 `D:\AI\ds.8\api key\little-crew.env`；**不得进仓库/镜像/日志/截图**
- 项目内 `config/.env` 键值留空；`secrets_check` 必须通过
- 不同步 `ds.8` 其他目录到别的电脑；只同步 `Little_Crew/`

### 工作区
- 一次性脚本放 `D:\AI\ds.8\附加脚本\scripts\`（命名 `YYYYMMDD_用途.py`），不进项目
- 草稿/临时产物放 `outputs/` 或临时目录；项目只留产品代码/测试/文档/研究证据
- `outputs/` 不进 git；证据进 `research/`；`research/script-rounds` 自动噪声要回滚（如 E100.json）

### 版本/发布
- 版本线固定在 **0.8.***；镜像只在项目收尾统一重建（不随每个补丁重建）
- 推送只在用户说“推送/同步”或每天一次；不自动推
- 每批改动：`tests/run_all.py` 全量 + 架构守卫必须 PASS；文档计数同步

### 架构纪律
- 分层依赖：ir→governance→models/capability→agent/compiler→runtime/review→api/cli
- `agent` 不得 import `runtime`；`review` 不得 import `runtime`
- 新职业/功能**一律插件化**：接口+注册+可卸载+默认不影响 19 职位
- 插件只能依赖核心公开模块；核心不得 import 具体插件

### 场景/章节纪律
- 九宫格 = **9 张分开生成** + 程序拼板，不在一张图里画九格
- 正交平面图只作草图/ControlNet，不回灌 img2img
- 档位由 LLM 读整部剧本决定（人工可覆盖）；Tier3 每集 ≤2 场
- 总导演只读章名+章卡+边界契约，**不读章节正文**
- 边界审 = 契约（机器）+ 画面（VLM）；红项只重拍边界镜；VLM 不是人脸同一性度量

### 成本/诚实
- 单镜生图 ≈¥0.05；花钱动作先评估/记录；不无限重试（上限 2 次，仍红转人工）
- 未真跑不得写“已验证”；失败/限制如实记录
- 人工项（盲判/评分/录屏/答辩包）按用户要求挂起

---

## 关键文件

- 门禁报告：`<workdir>/delivery/gate.report.json`
- 场景台账：`series/series.json`；shot 记录：`assets/shots/<shotId>/shot.json`
- 边界重拍：`runtime/boundary_retake.py`（`hint_from_diffs` / `run_retake_loop`）+ `tools/retake_shot.py`（`--ref-frame` 是关键）
- 台账锁：`runtime/series_bible.py`（`_lock` / `locked_merge` / `merge_episode`）+ `runtime/episode.py` 回写段
- 插件：`plugin/__init__.py`（`positions` / `ensure_default_plugins`）· `plugins/core_crew.py`（19 职位）· `plugins/series_showrunner.py`（第 20 职位）· `agent/crew_spec.py` · `agent/roster.py`
- 本轮证据：`research/series/b2-retake-loop-2026-11-18/`、`research/series/ledger-lock-2026-11-18/`
- 待办清单：`docs/20-待办清单.md`

## 纪律（收口三条）

- 新职业/功能一律插件化（接口+注册+可卸载+默认不影响 19 职位）
- 版本 0.8.*；镜像只在收尾重建
- 不把未验证的补丁混进提交；失败要如实写
