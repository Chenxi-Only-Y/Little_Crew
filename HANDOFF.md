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
