# 未提交改动说明：local-gen 本地推理接入（待决策）

> 状态：**未提交**（`git status` 显示 5 个文件 modified，共 +188 / −4 行）。
> 来源：上一次会话完成；本次勘察发现。
> 用途：供决定「先 commit 落基线，还是并入 provider 重构」时参考。

---

## 1. 这批改动在做什么

把 `D:\AI\local-gen`（本机零成本推理服务）接成项目的一个 **provider**：

```
用户设置页选「本地推理 local-gen(零成本)」
   → IMAGE_PROVIDER=local / VIDEO_PROVIDER=local
   → image.py / video.py 调 http://127.0.0.1:8100
   → Z-Image-Turbo 出图 / MiniMax H3 出视频
```

**它不需要 ComfyUI**——`local-gen` 自己的 README 明确写着"纯 Python + FastAPI，不用 ComfyUI"，
这与你后来的决策（不引入 ComfyUI）天然一致。

`local-gen` 本体是独立工程（不在本仓库）：~61GB 权重下载 / 整理后 ~47GB、
独立 CUDA venv（因为本项目 `.venv` 的 torch 是 **2.14.0+cpu**，没有 CUDA）。

---

## 2. 逐文件说明

### 2.1 `app/core/config.py`（+8 行）—— 新增 4 个配置项

```python
# ------------------- 本地推理服务（零成本测试，部署于 D:\AI\local-gen）-------------------
local_gen_url: str = "http://127.0.0.1:8100"
local_image_steps: int = 8         # Z-Image-Turbo 是蒸馏模型，固定 8 步（勿调高，否则画质变差）
local_video_steps: int = 50        # MiniMax H3 步数；Disk Offload 下很慢，测试建议 8~15
local_gen_timeout: int = 7200      # 本地视频生成超时（秒），Disk Offload 可能几十分钟
```
注释里保留了实测结论（8 步、50 步很慢），这是有价值的领域知识。

### 2.2 `app/services/image.py`（+48 行）—— 新增 `_local()` 文生图

- 在 `_real_image()` 的分派链里插入 `if provider == "local"` 分支。
- `_local()`：POST `{base}/v1/images/generations`，body 含
  `prompt / negative_prompt / width / height / num_inference_steps / response_format=url`，
  可选 `seed`。
- 用 `_retry_call()`（继承 429/5xx 指数退避）包裹；超时给到 **1800s**，
  注释说明"Disk Offload 下第一张图要等权重按需读盘"。
- **返回值沿用 mock 约定**：把本地文件路径同时作为 `image_url` 与 `local_path`，
  避免下游再拉一次网络。
- 从本地服务取回图片字节，落到项目自己的输出目录（按账号/部门/职业/会话归类）。

### 2.3 `app/services/video.py`（+113 行）—— 新增 `_real_local()` 图生视频

这段最复杂，因为它要适配本地服务的**异步任务模型**：

| 环节 | 实现 |
| --- | --- |
| 分派 | `synthesize()` 里新增 `if provider == "local"` |
| 提交 | POST `{base}/v1/videos/generations` → 拿 `job_id` |
| 轮询 | GET `{base}/v1/videos/generations/{job_id}`，`interval=10s`，默认 `timeout=7200s` |
| 下载 | 成功后 GET 视频 URL，落 `{out_dir}/{session_id}/video/{shot_id}.mp4` |
| 运镜提示词 | 固定 `_LOCAL_MOTION_PROMPT`（"镜头缓慢推近…电影感光影，稳定运镜"） |
| 首帧 | 复用现有 `_image_ref()`（本地图转 **base64 data URL**） |
| 分辨率 | 新增 `_local_size()`：**宽高对齐到 32 的倍数**，短边 480 / 长边上限 864 |

`_local_size()` 的注释写明依据："MiniMax H3 要求宽高均为 32 的倍数；8GB 显存 + Disk Offload
下面积不能大，官方低配置示例用的是 480x832 / 832x480 这一档"。

### 2.4 `frontend/pages/9_设置.py`（+3 / −2）—— 暴露给用户

```python
"image": ["(自动)", "local", "dashscope", "replicate", "minimax"],   # 新增 local
"video": ["(自动)", "ffmpeg", "local", "minimax"],                    # 新增 local
PROVIDER_LABEL["local"] = "本地推理 local-gen(零成本)"                 # 新增标签
```

### 2.5 `.env.example`（+18 / −1）—— 文档与模板

新增三组说明：
- **本地推理**：`LOCAL_GEN_URL` / `LOCAL_IMAGE_STEPS=8` / `LOCAL_VIDEO_STEPS=50` / `LOCAL_GEN_TIMEOUT=7200`
- **视频生成**：`VIDEO_PROVIDER` / `VIDEO_MODEL=MiniMax-H3` / `MINIMAX_API_KEY`
- `VIDEO_MAX_DURATION=10`，注释写明**关键实测结论**：
  > "H3 模型本身支持 4~15，但本机 8GB 显存实测：**10s 通过、12s 起必 CUDA OOM**
  > （降分辨率没用，瓶颈是 full attention 的序列长度）"

> ⚠️ 文件首行被加了一个 **UTF-8 BOM**（`﻿#` 而非 `#`）。
> 对 dotenv 通常无害，但建议顺手去掉，避免个别解析器把首个键名读成 `\ufeffDEEPSEEK_API_KEY`。

---

## 3. 顺带发现的一处不一致（建议 commit 前或重构时修）

| 位置 | 现状 | 风险 |
| --- | --- | --- |
| `video.py` | `seconds = max(4, min(15, int(settings.video_max_duration or 5)))` —— **硬编码上限 15** | 本地模式下若设为 11–15，**必然 OOM** |
| `.env.example` | 已建议 `VIDEO_MAX_DURATION=10` | 但设置页 UI 仍允许选到 **15** |
| `local-gen` | `app/engines/h3.py` 有 `SAFE_MAX_SECONDS = 10`，超出只打 warning | 不会拦截，会真的 OOM |

**建议**：本地 provider 自己声明上限（provider 能力的一部分），
UI 按 provider 动态限制可选范围——这与 `08-solutions.md §1`「并发度由 provider 声明」是同一个设计原则。

---

## 4. 这批改动的价值：里面固化了大量实测评测结论

这些结论都是**跑出来的**，不是查文档抄的，重做一遍成本很高：

| 结论 | 出处 |
| --- | --- |
| Z-Image 加载 33s，1024²/8 步 ≈ **32 秒/张** | local-gen README |
| H3 832×480、124 帧、5.17 秒、**8 步 ≈ 8.6 分钟**（每步 ~54s） | local-gen README |
| H3 **50 步 ≈ 45 分钟** | local-gen README |
| 本机安全时长上限 **10 秒**（12s 必 OOM，**降分辨率无效**） | local-gen README 表格 |
| `expandable_segments:True` **救不回** 12s OOM | local-gen README |
| Z-Image 不能用 Disk Offload（safetensors 多分片句柄段错误），改 diffusers + 4bit | local-gen README |
| Z-Image **8 步 / cfg=1.0**（调成 SD 习惯的 7 会劣化） | local-gen README |
| H3 文本编码器 14.27GB 需打补丁才能读 | local-gen README |
| 当前 4 个配置项默认值与依据 | `config.py` 注释 |

**这些是资产。** commit 下来才有版本记录，否则未提交状态一旦误删就没了。

---

## 5. 两个选项的对比

| | A. 先 `git commit` 落基线（推荐） | B. 不提交，并入 provider 重构 |
| --- | --- | --- |
| 回退点 | ✅ 重构前有干净可用的提交 | ❌ 出错时无法区分是重构还是这批改动引起 |
| 上述实测评测结论 | ✅ 进版本历史 | ⚠️ 若重构中重写这段代码，结论可能丢失 |
| 重构难度 | 无影响（provider 抽象照样在此基础上做） | 略省一步 |
| 与"删历史数据"配合 | ✅ 代码有基线，数据可放心删 | ⚠️ 代码数据同时处于中间态 |
| `local` 分支的代码质量 | 现有实现是"加一个 if 分支" | 正好可作为 provider 抽象的首个改造案例 |
| 风险 | 极低（只是记录现状） | 中（无回退点 + 知识丢失） |

**建议 A**：先 commit（可标注为 "feat: 接入本地 local-gen 推理 provider"），
然后在重构中把这 4 处新增配置 + 2 个 provider 分支**改造成 provider 注册表的第一个实现**。
这样既保住了回退点和实测结论，也不影响重构方向。

---

## 6. 提交前建议顺手做的两件小事

1. **去掉 `.env.example` 的 UTF-8 BOM**（第 2.5 节）。
2. **修本地视频时长上限**：把 `video.py` 的硬编码 15 改为由 provider 声明，
   或至少本地模式钳制到 10（`local-gen` 的 `SAFE_MAX_SECONDS`）。
