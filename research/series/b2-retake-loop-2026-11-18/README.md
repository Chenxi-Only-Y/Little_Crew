# B2 自动重拍闭环（VLM diff → 自动 hint → ≤2 次重拍 → 仍红升级人工）验证证据

- 日期：2026-11-18
- 版本线：0.8.x（只改实现/自检/工具，未 bump 版本）
- 任务：task-1（B2 自动循环 + 接 `tools/series_run.py --retake-red`）
- 状态：**离线自检全绿；半真实验证（真实 shot manifest / 真实脚本 / 真实章计划，假 renderer + 假 judge）完成；真实 VLM 验证 1 次（真实两章边界帧 → 真实 qwen3-vl-plus 判红 → 自动 hint，约 ¥0.05）完成；真实云端出图未跑**（总云花费约 ¥0.05，预算 ≤¥0.30）

## 1. 改动

| 文件 | 内容 |
|---|---|
| `src/little_crew/runtime/boundary_retake.py` | 新增 `hint_from_diffs()`（diff→针对性中文 hint，确定性/去重/≤400 字）与 `run_retake_loop()`（契约红不重渲直接升级；画面红最多 `MAX_RETRIES=2` 次补拍-复审；judge=None 诚实升级；证据 `delivery/retake-loop.json` / `delivery/retake-escalation.json`）。旧 `plan`/`write`/`run_retake` 签名与 `retake-plan.json` / `retake-result.json` 保持向后兼容 |
| `src/little_crew/runtime/boundary_retake_check.py`（新建） | 自检 70 项（hint / 循环 / 严格上限 / 升级证据 / 诚实边界 / 源码接线守卫 / 旧接口兼容） |
| `tools/series_run.py` | `--retake-red` 改接 `run_retake_loop`；画面红接真 VLM judge 闭包（`_boundary_retake_judge`，优先重拍 shot 的 manifest `out`，退回 `sorted(png)`）；新增 `--retake-max`（默认 2）；续跑分支也走 loop（judge=None → 诚实升级）；行留痕 `retakeAttempts/retakeEscalated/retakeLevel/retakeResult`；`summary.retakeEscalated/retakeCostCny`；`run-summary.md` 新增「重拍」列；stdout 打印 hint/attempts/是否升级 |

## 2. 自检结果

```
python -X utf8 -m little_crew.runtime.boundary_retake_check     -> 结果：70/70 通过
python -X utf8 -m little_crew.runtime.boundary_audit_check      -> 结果：23/23 通过
python -X utf8 -m little_crew.runtime.shot_manifest_check       -> 结果：19/19 通过
python -X utf8 -m little_crew.runtime.chapter_plan_check        -> 结果：16/16 通过
python -X utf8 -m little_crew.runtime.plugin_check              -> 结果：25/25 通过
python -X utf8 -m little_crew.runtime.series_bible_check        -> 结果：44/44 通过（任务书写 27；并发 agent 扩到 44）
python -X utf8 -m little_crew.runtime.report_html_check         -> 结果：17/17 通过
```

完整输出见 `boundary_retake_check-output.txt`。

## 3. 半真实验证（云花费 ¥0）

临时工作目录均在 `%TEMP%`，下列证据为运行产物 JSON/文本摘录。

### 3.1 `run_retake_loop` 跑在真实集 workdir 的真实 shot manifest 上
- 真实来源：`outputs/_series/最后一班-迷你/E02`（复制到 temp；2 个真实 shot.json + 真实云出图 PNG 副本）
- renderer 假（写真实 PNG 副本，**0 次云调用**）；judge 假（第 1 次红「光线/时段不连续」→ 第 2 次绿）
- 结果：`attempts=2 / ok=true / escalated=false / costCny=0.2（模拟记账）`
- shot.json 版本确实递增：`SH-E02-001-001: 4→6`，`SH-E02-002-001: 1→3`（含真实 history 追加）
- 证据：`semi-real-loop-real-manifest.json`

### 3.2 同真实 workdir + `judge=None` → 诚实升级、不重渲
- `escalated=true / attempts=0 / renderer 调用 0 次 / reason 含「无复审」`
- 证据：`semi-real-no-judge-escalation.json`

### 3.3 `tools/series_run.py --resume --retake-red`（续跑分支接线）
- 真实：`research/series/最后一班-迷你/scripts/E01,E02.txt`、章计划（由三章试点计划改名+**故意把 E02 entry 地点改成"医院"制造契约红**）、真实 shot manifest 副本
- 为免云端重跑整集，resume 标记 `run.report.json(ok=true)` 为**合成**（已在 JSON 中注明）
- 结果（stdout）：
  ```
  ↷ E01 已成功（续跑跳过）
  ↷ E02 已成功（续跑跳过） · 边界重拍 attempts=0 level=red ⚠升级人工
  --- 2/2 成功 · 场景¥0.0 · 重拍¥0.0 · 墙钟 0.0s · 重拍升级 1 集：E02
  ```
- `run-summary.json`: `retakeEscalated=["E02"]`, `retakeCostCny=0.0`
- `run-summary.md` 表格已含「重拍」列：`E02 | ✅ | ... | 升级(0次)`
- `E02/delivery/retake-escalation.json`: `needsHuman=true`, reason 含「契约红项需人工改章卡/章契约：补拍图片修不了契约（diffs：placeKey …）」，`suggestedActions` 3 条
- 证据：`semi-real-series-run.json`、`semi-real-series-run-stdout.txt`、`semi-real-run-summary.json`、`semi-real-run-summary.md`

### 3.4 judge 闭包帧选择离线验证
- `series_run._boundary_retake_judge` 在 `prev_frames=[]` 时直接得到 `yellow`（不触发 VLM 云调用）
- 证据：`semi-real-judge-offline.json`

### 3.5 真 VLM 验证：真实帧 → 真实 VLM 判词 → 自动 hint（成本约 ¥0.05）
证明「VLM diff → 自动 hint」不是只对假判官成立：
- 真实帧（真实集 `outputs/_series/最后一班-迷你` 的真实云端出图）：
  左 = E01 最后一镜 `SH-E01-002-001_cloud.png`；右 = E02 第一镜 `SH-E02-001-001_cloud.png`
  → 已存对比图 `real-vlm-boundary-compare.jpg`
- 调用：`boundary_audit.audit_visual([prev], [cur], judge=真实 _default_vlm 同路径)` — **1 次 qwen3-vl-plus 调用**（并排对比图高 512；prompt 1019 tokens / completion 86 tokens / 3.3s），成本按 `models/registry.py` `CostProfile(cny_per_call=0.05)` 估算约 **¥0.05**
- **真实 VLM 原始判词**（`vlmRaw`，已解析）：
  `sameSpace=false, sameCostume=false, sameLighting=false`；
  diff =「从樱花树下的公园长椅场景切换到雨雾中的火车站台，人物从黑长发、露肩灰衣变为银短发、和风灰袍，光线由柔和日光转为昏暗湿冷的傍晚/雨天氛围。」
- 真实 boundary 记录：`level=red`，`diffs=["空间不一致","人物造型不一致","光线/时段不连续"]`
- `hint_from_diffs(真实记录)` 自动生成（133 字，另一份喂合并形态 boundary 的记录结果相同）：
  「沿用上一章同一空间/背景/陈设，不要换景；人物沿用同一服装/发型/妆造，不要改造型；保持与上一章同一光线/时段/色温；上一版被判：从樱花树下的公园长椅场景切换到雨雾中的火车站台，人物从黑长发、露肩灰衣变为银短发、和风灰袍，光线由柔和日光转为昏暗湿冷的傍晚/雨天氛围。」
- 结论：真实 VLM 直接判红，**无需构造红记录**；真实判词 → 三类约束 hint 全部命中，链路对真实判官成立。
- 诚实说明：这对真实帧本身跨章差异很大（E01 公园长椅 / E02 火车站台），真实集自身边界即红；本节只证明「真 VLM 判词 → 自动 hint」链路，不代表该集已修好。
- 证据：`real-vlm-hint.json`（含 `vlmRaw/judgeSentence/boundary/hint/vlmVerdictFields/generatedAt/usageLastCall`）、`real-vlm-boundary-compare.jpg`

## 4. 未验证 / 诚实边界

- **未真实调用云端出图**（¥0.05/张）：所有循环验证都注入假 renderer；真实出图路径 `run_retake` 沿用既有实现，本次未花出图云钱。
- 云端 VLM 已真实调用 **1 次**（3.5 节，约 ¥0.05），但**「补拍后真 VLM 复审」未跑**：复审链路的 judge 闭包仍是假判官，真复审需要真实重拍出图（出图云钱未花）。
- `series_run` **主分支**（新集首次渲染 + `--boundary-vlm` 真 VLM 复审）未真跑：需要整集真渲染（预算内不可行）。只做了：源码接线守卫（`boundary_retake_check` H9）+ judge 闭包离线验证（3.4）+ 真 VLM 判词→hint 链路（3.5）。
- 半真实 3.3 中 resume 标记是合成的（仅为跳过整集云端重跑）；shot manifest / PNG / 脚本 / 章计划均为真实文件。
- 未跑 `tests/run_all.py`（全量约 4 分钟，按任务分工由 lead 收尾跑）。`boundary_retake_check` 尚未注册进 `tests/run_all.py`（需 lead 登记，预期 70 项）。
