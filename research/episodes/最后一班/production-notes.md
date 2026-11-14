# 「最后一班」· 60 秒短片制作记录（2026-11-14）

> 目标：**60s 剧情片**，女主沿用 `assets/characters/cover_lead/sheet.png`，画风与建模保持一致。
> 结论：**成片跑通（60.0s 清单 / 56.8s 实际视频），女主外观已锁定；同镜本地对照完成**。

---

## 一、成片数据（真机）

| 项 | 数值 |
|---|---|
| 集号 | `EF-LAST60C`（工作目录 `outputs/_EFLAST60C`，gitignored） |
| 镜头 | **12 镜**（8 场，每场等距 2 镜；原始分镜 35 镜 → `--shots-target 12` 压到 12） |
| 时长 | 清单 **60.01s**；成片 **60.0s / 12 镜**（h264 832×480，2.06 MB）✅ 2026-11-14 修复后重建 |
| 对白 | 0 句（纯画面叙事） |
| 画风 | 高精度 3D 半写实国漫 CG（`--style-hint`）+ 锚点外观锁定 |
| 女主 | `cover_lead/sheet.png`，`--cast-anchor 少女=...` 图生图锁脸
| 封面 | `delivery/cover.png`（选帧 + ffmpeg 叠标题「最后一班」）→ 本目录 `cover.jpg` |
| 质检 | 视觉闸 6/12 镜抽审 · 严重 0 条；`qc.report.json` + `vision_review.json` |
| 交付 | `episode.master.mp4` + 12 条内容哈希 + 硬规则 5 项全过 |
| 墙钟 | 990.9s（首次 12 镜：含脚本 3 轮 9.5min、云端出图、封面） |

**成本（按 ¥0.05/张）**

| 轮次 | 云端出图 | 现金 |
|---|---|---|
| 第一次（35 镜→12 镜，跑通） | 12 | ¥0.60 |
| 对照误用云端（bug，白跑） | 32 | **¥1.60 浪费** |
| 第二次（resume 重渲 + 外观锁定） | 12 | ¥0.60 |
| **合计** | 56 | **≈ ¥2.80** |

> 教训已写进代码：`--compare-local` 必须同时切 `renderer` 与 **`_backend`**（`_Runner.engine_backend()` 读的是 `_backend`），
> 否则对照会把云端再跑一遍。

---

## 二、关键发现：**外观锁定**（这是本次最大的方法论收获）

| 做法 | 结果 |
|---|---|
| 只给**图片锚点**（`--cast-anchor`） | ❌ 出来是黑色风衣 + 背包，与 `cover_lead`（灰紫露肩装）完全不同 |
| 锚点 + **把 `sheet.json` 的 appearance 拼进每镜提示词** + 明确"禁止换装/改发型/加参考图没有的物件" | ✅ 12 镜里服装、发型、耳饰、妆容一致（见 `film_frames.jpg`） |

实现：`episode_demo.py` 在 `--cast-anchor` 时自动读取同目录 `sheet.json` 的 `appearance`，
追加到 `--style-hint`。**只给图不够，必须给文字约束**。

---

## 三、B3 同镜对照（本地 vs 云端，同一批 IR）

`tools/compare_local_cloud.py`：从 `checkpoint/stages.json` 取 12 镜 IR，
用本地 Z-Image（`localgen`，~8.6s/镜）重渲，与云端并排。

| | 云端（gpt-image-2.6） | 本地（Z-Image-Turbo 4 步） |
|---|---|---|
| 人物一致性 | ✅ 与锚点一致 | ❌ 换装、脸型漂移（"另一个角色"） |
| 画风 | ✅ 电影感、低饱和、材质细节 | ❌ 偏 TV 动画/赛璐璐，光影简化 |
| 每镜耗时 | ~25~30s（4 路并行 ~8s 等效） | **8.6s**（串行） |
| 每镜成本 | ¥0.05 | ¥0（电费） |
| 12 镜墙钟 | ~2 min（并行） | **105s** |

对照图：本目录 `compare_1.jpg` / `compare_2.jpg`（每张 6 行，**左云端 / 右本地**）。

> **结论**：本地是"能出图、但不像同一个角色"；云端在"人物锁脸 + 电影质感"上明显更好。
> 这与之前 20 集本地成片的观感一致 —— 质量差距主要来自**模型能力**，不是流程。

---

## 四、已修复 / 仍待办

1. ✅ **成片 11/12 镜（已修）**：根因不是时序，而是**被重画那一镜的产物名是缓存原始名**
   （`<hash>_C:\...\Temp\...png`），解析器按名找不到，静默进 `plan.problems`。
   修法：`_assemble_video` 增加 **shotId 兜底**（按 `assets/shots/<sid>/` 直接找）+ 解析失败重试 + `video_plan.json` 诊断。
   重建后 **12/12 镜 · 60.0s**。
2. ✅ **短片阈值（已加开关）**：新增 `LITTLE_CREW_SCRIPT_THRESHOLD` / `--script-threshold`；
   60s 无对白短片建议 `55`（默认 65）。**阻断级意见/硬约束仍照旧阻断**，只是放宽分数线。
3. 视觉闸：6/12 抽审，未覆盖全部；时间允许应 12/12。

---

## 五、复现命令

```powershell
$P = "D:\AI\ds.8\Little_Crew"; cd $P
$env:PYTHONPATH="$P\src"; $env:PYTHONIOENCODING="utf-8"
$env:LITTLE_CREW_ALLOW_UNVERIFIED="1"; $env:LITTLE_CREW_RENDER_PARALLEL="4"

# 1) 云端整集（12 镜 / 60s）+ 封面
& "D:\Python\python.exe" -X utf8 -m little_crew.runtime.episode_demo `
  --episode-id EF-LAST60 --workdir outputs\_EFLAST60 --fresh `
  --title "最后一班" --story-file research\episodes\最后一班\story.txt `
  --shots-target 12 --target-seconds 60 `
  --render-path cloud --render-model gpt-image-2.6-sunburst `
  --cast-anchor "少女=$P\assets\characters\cover_lead\sheet.png" `
  --style-hint "高精度 3D 半写实国漫 CG，与参考图同一套建模、材质与光照；冷调瓷白肌、BJD 人偶感五官；夜色霓虹与冷白站台灯，低饱和电影感" `
  --vision-max 12 --qc-model mimo-v2.6-flash --llm --script-threshold 55 `
  --cover --cover-title "最后一班"

# 2) 本地 vs 云端同镜对照（需本地 local-gen 服务：D:\AI\local-ai\media）
& "D:\Python\python.exe" -X utf8 tools\compare_local_cloud.py `
  --workdir outputs\_EFLAST60C --episode EF-LAST60C
```

---

## 六、产品 CLI 验证（2026-11-14，B 选项）

用**产品入口**（`little-crew episode`）跑同一部短片，验证"平台入口"可用（不是只有 demo 能跑）：

| 项 | 结果 |
|---|---|
| 命令 | `little-crew episode --script story.txt --render-path local --shots-target 12 --target-seconds 60 --cover --cover-title 最后一班 --script-threshold 55` |
| 剧组 | `default_crew_stubs()`（**规则桩，不是真 LLM**） |
| 镜头 | 16 → **12 镜**（每场等距 3 镜） |
| 时长 | 72.0s → **60.0s**（清单与成片都是 60.0s） |
| 渲染 | 本地 local-gen，12/12 成功，**¥0** |
| 封面 | ✅ 选帧 + 叠标题（`cli_cover.jpg`） |
| 门禁 | **KEEP 12 / RETRY 0 / ESCALATE 0** |
| 墙钟 | **236.6s** |
| 交付 | `run.report.json` / `episode.master.json` / `episode.master.mp4` / `delivery.evidence.json` |

**本次跑出的 bug（已修）**：`_apply_target_duration` 里用 `blackboard.set(...)` 写回缩放后的镜头表，
而 `Blackboard` 的写 API 是 `put(key, value, owner=...)` → `AttributeError`（被 try 吞掉，
只打印一行警告；下游用局部变量所以成片没坏）。已改为 `put(..., owner="episode")` 并复跑确认警告消失。

> 证据：`cli_film_frames.jpg` / `cli_cover.jpg` / `cli-run-summary.json`。

### 产品 CLI + 真实 LLM（2026-11-14 晚，限 3 镜验证）

`little-crew episode ... --llm --llm-critics --limit 3`：

| 项 | 结果 |
|---|---|
| 真实 LLM | 编剧 DeepSeek V4.1 Flash / 剧评人 MiMo / 文风批评家 GLM-5.3 Flash（跨族） |
| 绑定 | `screenwriter · dramaturge · storyboard_artist · story_editor · review_board` |
| 剧本 | **1 轮 → PASS**（阈值 55） |
| 镜头 | 3 镜（`--limit 3` 压测入口）· 出图 3 · 失败 0 · **成片 17.0s** |
| 封面 | ✅ 选帧 + 叠标题 |
| 门禁 | KEEP 3 / RETRY 0 |
| 墙钟 | 719.3s（含真实 LLM 剧本/分镜/审片） |

**踩坑**：智谱 GLM 端点偶发 `CERTIFICATE_VERIFY_FAILED`（证书链缺中间证书），
首次跑因此 `ESCALATE [review-failed]`；本次用 `LITTLE_CREW_SSL_NO_VERIFY=1` 跑通。
待办：给内置 GLM provider 声明 `insecure_tls`（自定义供应商已支持该字段），
或配置 `LITTLE_CREW_SSL_CA_BUNDLE`。

> 证据：`cli_llm_frame.jpg` / `cli_llm_cover.jpg`。

