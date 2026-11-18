# #8 并行写台账锁 —— 真实并行验证证据（2026-11-18）

> 任务：#8「并行写台账锁」——并行跑多集时，`runtime/episode.py` 原来在场景视图回写段
> 整份 `load() → 内存 upsert → save()` 覆盖 `series/series.json`，后写者会覆盖前写者的
> scenes/cast/usage/episodes（并发丢更新）。修复：**按集写 + 跨进程合并锁**，之后才允许
> `--mode parallel` 并行开场景视图。

## 实现（本目录证据对应的工作区改动）

| 文件 | 改动 |
|---|---|
| `src/little_crew/runtime/series_bible.py` | 新增 `LedgerLockTimeout`；`_lock(base, timeout_s=30.0)` 跨进程锁（纯 stdlib：`O_CREAT\|O_EXCL` 锁文件、10~50ms 抖动重试、默认 30s 超时、锁文件 mtime > **300s** 判陈旧可破并回传 `staleBroke`、`finally` 必删锁；Windows 上删锁竞争产生的瞬时 `EACCES` 按"被占"重试）；`_atomic_write`（同目录 tmp + `os.replace`）；`save` 改原子写（路径/返回不变）；`locked_merge`（锁内重读最新 → mutate → 原子写回）；`merge_episode`（锁内只合并**本集增量**：scenes 走 `upsert_scene`（tier 只升不降 / cardHash 变才升 version / assets 按视图合并）、usage 按 `(episodeId, sceneKey, action)` 去重、cast 走 `upsert_character`、episodes 走 `upsert_episode`；返回 `{ok, episodeId, added, summary, waitMs, staleBroke}`） |
| `src/little_crew/runtime/episode.py` | 场景视图回写段：边跑边收集本集 `scenes/usage/cast` 增量 → 只调一次 `merge_episode`，不再 `save(_series_dir, _bible)` 整份覆盖；`delivery/series.json` 每集文件保留（`seriesId/episodeId/summary/actions` 形状不变，summary 用合并后最新值）；失败不阻断且打印。另把角色图集登记段（原 `load→upsert_character→save` 整份覆盖）也改成 `locked_merge` |
| `src/little_crew/runtime/series_bible_check.py` | 27 项 → **44 项**（新增 17 项：锁超时/陈旧锁/异常释放/原子写无残留/locked_merge/merge_episode 幂等/保留他集/tier/assets/version/4 进程并发无丢更新） |
| `tools/series_run.py` | 并行分支确认传 `--scene-views` / `--scene-views-no-llm` / `--scene-views-hero-max`；并行启动提示与 `run-summary.json`/`.md` 如实记录 `sceneViewsParallel`（`mode/sceneViews/sceneViewsNoLlm/heroMax`）；未动 `--retake-*`/边界审逻辑 |

## 复现命令

```powershell
$env:PYTHONPATH="D:\AI\ds.8\Little_Crew\src"
$env:PYTHONIOENCODING="utf-8"
$env:LITTLE_CREW_ALLOW_UNVERIFIED="1"
D:\Python\python.exe -X utf8 tools\series_run.py `
  --series-dir research/series/ledger-lock-2026-11-18/series `
  --scripts-dir research/series/ledger-lock-2026-11-18/series/scripts `
  --episodes 3 --shots 2 --render-path cloud --scene-views --hero-max 1 `
  --mode parallel --out-root outputs/_LOCKTEST_2026-11-18 --timeout 1800
```

### 种子剧集（成本控制）

`series/` = 复制 `research/series/最后一班-迷你` 的 `series.json` + `E01/E02` 剧本，并补一个
单场剧本 `E03.txt`（同一"地铁站台·夜"，不新增场景），使 3 集全部命中台账已有资产
（站台 T3 10 个资产 / 医院走廊 T2 4 个 / 河堤 T2 4 个，跑前已核对文件全部存在）：

- 场景视图：全部 `reuse` → 预期 **¥0**
- 云渲染：3 集 × 2 镜 × ¥0.05/张 = 预期 **¥0.30**
- LLM/视觉审查：deepseek-flash，每集约 1k tokens，成本可忽略
- **预估直接成本 ≈ ¥0.30～0.40（最坏某场升档重生成 T3 九宫格 +¥0.45/场，上限 ≈ ¥1.2）**

## 实际结果

**运行：2026-11-18 14:10:19 → 14:12:23（124.6s）· `3/3` 集成功 · 直接成本 ¥0.30**

| 指标 | 实测 |
|---|---|
| 集成功 | **3/3**（E01/E02/E03，每集 `ok=true`、coverage 1.0） |
| 场景视图 | **全部复用**：E01 复用 13 视图 / E02 13 / E03 9，`tierPlanSource=llm-series`，**¥0.00** |
| 云渲染 | 3 集 × 2 镜 = 6 张 × ¥0.05 = **¥0.30** |
| LLM/视觉审查 | deepseek-flash 12 次调用 / 3952 in + 3630 out tokens（成本可忽略，未折算） |
| **直接成本合计** | **¥0.30**（本次会话跑了 2 次：首跑发现 `summary.wallS` 字段 bug、修复后重跑，两次合计约 **¥0.60**，≤¥1.0 优先档） |
| 墙钟 | `run-summary.wallS=124.5s`；逐集 wallSeconds E01 100.26 / E02 57.99 / E03 124.11 |
| `run-summary` | `mode=parallel`、`sceneViews=true`、`sceneViewsParallel=true`、`ok=3/3`、`sceneCostCny=0.0` |

**台账无丢更新（`validation.json`：54 项检查全过）**

```
episodes=3  scenes=3  usage=8  cast=3
seed（跑前）：episodes=2 / usage=4
本次新增 usage：4 条（E01/站台 reused、E01/医院走廊 reused、E02/河堤 reused、E03/站台 reused）
每集 usage 行数：E01 4 / E02 3 / E03 1（= 历史 + 本次，无重复行）
共享场景 地铁站台：episodes=[E01,E02,E03]，tier 保持 3，cardHash 未变，usage 计数 4
```

- **并发证据**：三集子进程在同一 4 秒窗口内分别写同一份 `series/series.json`
  （台账落盘时刻 E03 `14:10:23`、E01 `14:10:25`、E02 `14:10:27`），最终三集数据全在、JSON 完整、
  无锁/tmp 残留。
- **幂等证据**：`(E02, 地铁站台, reused)` 历史台账里已有一条（上次串行跑留下的），本次再合并
  **没有重复追加**（所以新增是 4 条而不是 5 条），`usage` 唯一键数 = 行数。
- **成本降本来源**：种子台账 3 个场景的资产文件跑前已核对全部存在，3 集全部命中
  `decide_reuse` → 场景视图 0 新生成；9 张/场的 T3 九宫格复用台账既有资产。
- **修复记录**：首跑（`logs/run1.log`）暴露 `summary.wallS=0.0` —— `t_all` 在并行块之后才起表；
  已移到并行启动前（`tools/series_run.py`），重跑后 `wallS=124.5s`。

## 证据清单

| 路径 | 内容 |
|---|---|
| `run-summary.json` / `run-summary.md` | 真实并行跑汇总（含 `mode/sceneViewsParallel/ok/sceneCostCny/wallS`；`series/` 下同一份） |
| `validation.json` | 独立校验脚本结果（54 项全过）：台账丢更新比对 / usage 逐条 / run-summary / 成本对账 / 落盘时刻 |
| `series/series.json` | 跑后台账（3 集、3 场景、8 usage、3 cast） |
| `series/_seed_before.json` | 跑前台账快照（用于逐条对比"无丢更新"：E01/E02 + 4 usage） |
| `series/scripts/E01.txt` `E02.txt` `E03.txt` | 3 集种子剧本（E01/E02 复制自 最后一班-迷你，E03 为单场同站台补集） |
| `out-root-delivery/E0*/` | 每集 `series.json` / `scene_views.json` / `image_calls.json` / `token_usage.json` / `run.report.json`（成本与结果原始证据） |
| `logs/run2.log` | 成功跑（修复后）完整 stdout；`logs/run1.log` 为修复前首跑 |
| `validate_locktest.py` | 本次校验脚本（可复跑：`python validate_locktest.py <repo_root>`） |
| `ledger_episode_integ.py` | 离线集成自检（假生图直接调 `_run_scene_views`，2 集 9/9 过，验证回写段走 `merge_episode` 且异常不阻断） |
| `checks/*.out.txt` | `series_bible_check`(**44/44**)、`episode_check`(**99/99**)、`shot_manifest_check`(19/19)、`scene_views_check`(47/47)、`scene_tier_planner_check`(20/20)、离线集成(9/9) |
| `stress/` | 独立并发压力脚本 + 8 份 JSON 结果：8 进程 × 5 集 × 5 轮、6 进程 × 5 集 × 8 轮，**多轮重复全部 0 丢更新**（详见下节） |

## 独立并发压力验证数据

同一套锁用 8/6 个独立子进程（各自不同 episodeId）同时抢锁合并，跑多轮：

| 配置 | 锁内合并次数 | 最终台账 | 结果 |
|---|---|---|---|
| 8 进程 × 5 集 × 5 轮 | 200 | episodes 40 / scenes 40 / usage 200 / cast 8 | ✅ 0 丢更新（3 次重复全过） |
| 6 进程 × 5 集 × 8 轮 | 240 | episodes 30 / scenes 30 / usage 150 / cast 6 | ✅ 0 丢更新（3 次重复全过） |

6 轮重复共 1320 次并发锁内合并，全部 `workersOk`、`episodes/scenes/usage/cast` 与期望完全一致、
`usagePerEpisode` 恒为 5、目录只剩 `series.json`（无锁/tmp 残留）、JSON 完整；累计等锁
11.5–32.4s/轮，证明锁确实在并发竞争下工作（不是顺序执行）。

> Windows 实跑发现并修掉一个平台坑：锁文件被另一进程删除的瞬间，`O_CREAT|O_EXCL` 可能返回
> `EACCES`（而不是 `EEXIST`），早期压力用例因此让 1 个 worker 失败；现在把 `PermissionError`
> 按"被占/正在释放"重试处理，6 轮重复压力 0 失败。

## 已知边界

- 锁是"陈旧可破"的近似锁：破陈旧锁与活进程极端竞争时理论上可能双持有（临界区只是
  读-合并-原子写，毫秒级；300s 陈旧阈值实际不会撞上活进程），换取的是纯 stdlib、无平台分支。
- 跨集复用决策仍是"乐观"的（每集开头读快照决定复用/升档），并行时两集可能同时决定生成
  同一场景 —— 只多花生成成本，**不会丢数据**（合并锁保证增量合并）。
- 本文成本为工作区自带估算单价（场景视图 ¥0.05/张、云渲染 ¥0.05/张）；LLM token 用量已记录但
  未折算金额。
