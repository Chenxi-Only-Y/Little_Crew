# 交接与续跑（HANDOFF）

> 上下文到上限时先读这份，再继续。最后更新：2026-11-18。

## 当前状态

- 版本：**0.8.6**（版本线停在 0.8.*，不升 0.9/1.0）
- HEAD：**24381e7**
- 未推送提交：**27**
- 全量：3251/3238 + 架构守卫 PASS（#6 修复后需再跑一次确认）
- dirty：0

## 已完成（不要重做）

- B1/B2/B3/B4/B5/A1/A2/A3/A4/A5 全部完成（详见 CHANGELOG 0.8.*）
- #6 门禁定位+修复：云端出图落盘前归一 engine_resolution（832x480）；
  CH01 复跑 gate kept 2/2、retry 0 ✅

## 待做队列（按序）

1. **#7 B2 自动循环**：VLM diff → 自动生成 hint；
un_retake 重试上限 2 次；仍红升级人工；接进 series_run --retake-red
2. **#8 并行写台账锁**：parallel 模式多集共写 series.json → 按集写 + 合并锁，然后允许并行开场景视图
3. **B1 全职业插件化（目标 20/20）**：插件 API 加 positions 声明 → 19 职位迁 plugins/core_crew → 名册/编制/依赖图从注册表构建 → 无限画布读注册表
4. 推送 27 个提交
5. 收尾：全量回归、Docker 镜像 little-crew:0.8.6、tag 0.8.6


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

- 门禁报告：<workdir>/delivery/gate.report.json
- 场景台账：series/series.json；shot 记录：ssets/shots/<shotId>/shot.json
- 重拍：
untime/boundary_retake.py + 	ools/retake_shot.py（--ref-frame 是关键）
- 插件：
untime/plugin/__init__.py、plugins/series_showrunner.py
- 待办清单：docs/20-待办清单.md

## 纪律

- 新职业/功能一律插件化（接口+注册+可卸载+默认不影响 19 职位）
- 版本 0.8.*；镜像只在收尾重建
- 不把未验证的补丁混进提交；失败要如实写
