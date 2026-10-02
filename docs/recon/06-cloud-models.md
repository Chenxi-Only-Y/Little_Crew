# 小剧组 Little Crew — 云端模型选型清单（供用户选择）

> 用途：为「用户自选模型」提供候选清单与选型依据。每项都标了**能力 / 价格 / 中国可直连 / 是否支持自训练**。
> 数据来源：见文末链接。价格为 2026 年公开标价，**属方向性参考**，接入前需以厂商页面为准。
> 关联：`05-roles-v2.md §6`（本地/云端等价 provider 设计）。

---

## 0. 关键结论：优先接**聚合平台**，而不是逐个接原厂

你的系统要支持「用户自选模型」，但**逐个接原厂 SDK 会爆炸**。行业里已有一层聚合 API：

| 聚合平台 | 覆盖 | 对项目的意义 |
| --- | --- | --- |
| **WaveSpeedAI** | 图像（GPT Image / Gemini / Flux / Seedream / Hunyuan / SD3.5…）+ LoRA 训练工具 | 一次接入覆盖数十个模型；有 [LoRA 训练工具集](https://wavespeed.ai/fr/collections/training-tools) |
| **Replicate** | 图像 / 视频 / **LoRA 训练**（`replicate` 已在你 `requirements.txt` 里） | 社区模型最全，已是你项目依赖 |
| **fal.ai** | 图像 / 视频 / LoRA 训练，推理快 | 常与 Replicate 并列对比 |
| **Mobbi / Renderful 等** | 视频为主，30+ 模型一个余额 | 免订阅门槛，比直连原厂低 30–50% |

**架构建议**：provider 注册表分两层——**聚合 provider**（一次接多个模型）+ **直连 provider**（百炼/MiniMax 等你已在用的）。
用户在下拉里看到的是「模型」，背后是哪个 provider 由注册表决定，**用户不需要关心**。

---

## 1. 文生图（`image_generator` 候选）

按 LM Arena 人类盲测 Elo 排序（2025-12 数据，[来源](https://wavespeed.ai/blog/es/posts/complete-guide-ai-image-apis-2026/)）：

| # | 模型 | 厂商 | Elo | 单价(1024²) | 中国直连 | 特点 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | GPT Image 1.5 | OpenAI | 1284 | $0.040 / HD $0.080 | ❌ 需中转 | 综合质量最强，理解复杂提示词 |
| 2 | Gemini 3 Pro Image | Google | 1268 | $0.035 / HD $0.070 | ❌ | 多模态强，适合信息图/图表 |
| 3 | Flux 2 Pro (v1.1) | Black Forest Labs | 1265 | $0.055 | ⚠️ 聚合 | 专业级写实，细节/光影最佳 |
| 4 | Flux 2 Pro | BFL | 1258 | $0.045 | ⚠️ 聚合 | 同上 |
| 5 | Flux 2 Dev | BFL | 1245 | $0.025（可自托管免费） | ⚠️ 聚合 | **开源权重**，可本地跑 |
| 6 | Hunyuan Image 3.0 | 腾讯 | 1238 | $0.030 | ✅ | 中文/亚洲语境最优，性价比高 |
| 7 | Flux 2 Schnell | BFL | 1232 | $0.015（可自托管） | ⚠️ 聚合 | 1–4 步出图，2–5 秒 |
| 8 | Seedream 4.5 | 字节 | 1225 | $0.035 | ✅ | 创意/艺术感强（TikTok 系审美） |
| 9 | Ideogram 2.0 | Ideogram | 1218 | $0.040 | ❌ | **文字渲染最强**（招牌/海报/logo） |
| 10 | DALL·E 3 | OpenAI | 1205 | $0.040 / HD $0.080 | ❌ | 内容安全最严 |
| 11 | SD 3.5 Large | Stability | 1198 | $0.025（可自托管） | ✅ 可自托管 | **开源**，支持 LoRA 训练 |
| 12 | Leonardo Phoenix | Leonardo | 1185 | 订阅制 | ❌ | 画布/放大/工作流齐全 |

**中国生态补充（你已在用的百炼）**：`wan2.7-image-pro` / `wan2.7-image`（万相）、`qwen-image-2.0` / `qwen-image-2.0-pro`（千问，**可微调**）、`z-image-turbo`（已在用）。

**⚠️ 排除项**：**Midjourney 无公开 API**（只有 Discord bot），无法程序化接入——文档里要明确不提供。

---

## 2. 图像编辑 / 崩坏修复（`image_editor` 候选）

| 模型 | 能力 | 说明 |
| --- | --- | --- |
| **Qwen-Image-Edit 系列**（百炼） | 改图内文字、增删/移动物体、改主体动作、**风格迁移、细节增强**；**多图输入 1–3 张** | ✅ 中国直连；`max` 系列角色一致性更强，`plus` 支持多图输出与自定义分辨率；输出 512²–2048² |
| GPT Image 2 Edit | 图像编辑 | 海外，需中转 |
| Flux 2 Pro | img2img / inpainting | 聚合可访问 |
| SD 3.5 Large | img2img / **ControlNet** | 自托管时可完整控制 |

**对应你的「质检总监 · 崩坏修复」**：用一句指令（"修复右手手指数量，其余保持不变"）+ 原图即可，不需要蒙版。这比本地 inpaint 更省事。

---

## 3. 图生视频（`video_generator` 候选）

原厂与聚合都有，价格差 **10 倍**（[来源](https://mobbi.ai/blog/ai-video-generation-cost-2026)）：

| 档位 | 模型 | 约 $/秒(1080p) | 备注 |
| --- | --- | --- | --- |
| 廉价 | **Hailuo（MiniMax）** | ~$0.01 | **你已在用**；渲染 <2 分钟 |
| 廉价 | Seedance（字节） | ~$0.01–0.02 | 性价比最好 |
| 廉价 | Veo 3.1 Fast | ~$0.0125 | 720p，$0.10/8s |
| 中档 | Sora 2 standard | ~$0.02–0.04 | 需 OpenAI API 权限 |
| 中档 | Kling 3.0 | ~$0.03（批量 ~$0.015） | 多镜头 + 原生音频 |
| 高价 | Runway Gen-4 | ~$0.05–0.12 | 有 $95/mo 订阅门槛 |
| 高价 | **Veo 3.1** | ~$0.06 | **每条自带同步音频**（$0.50/8s） |
| 高价 | Sora 2 Pro | ~$0.06–0.10 | 保真最高，15–30 分钟/条 |

**成本要点（写进 UI 提示）**：
- 1 分钟成片原价约 **$0.60（Hailuo）~ $6.00（Sora 2 Pro / Veo）**
- **订阅门槛是隐藏成本**：Sora 2 需 ChatGPT Plus($20)/Pro($200)，Veo 3 需 Gemini Advanced($19.99)，Runway 需 Pro($95)
- **"先廉价试，再昂贵出终稿"** 是标准省钱法，能砍一半以上成本

---

## 4. LoRA / 角色一致性训练（`lora_trainer` 候选）

| 提供方 | 方式 | 底模范围 | 中国直连 |
| --- | --- | --- | --- |
| **阿里百炼** | `POST /api/v1/fine-tunes`，`efficient_sft` | `wan2.7-image-pro`/`wan2.7-image`、`qwen-image-2.0`/`-pro` | ✅ 仅华北2(北京) |
| **Replicate** | LoRA 训练 API | SDXL / Flux 等社区底模 | ⚠️ |
| **fal.ai** | LoRA 训练 API | SDXL / Flux | ⚠️ |
| **WaveSpeedAI** | [训练工具集](https://wavespeed.ai/fr/collections/training-tools) | 自定风格/角色 | ⚠️ |
| 其它平台 | sozee 等 | 多为 SDXL/Flux | ❌ |
| **本地**（DiffSynth） | NF4 QLoRA + peft | **Z-Image-Turbo** | ✅ 本机 |

**⚠️ 三条硬约束（必须让用户看见）**：
1. **LoRA 与底模绑定**：百炼训的只能百炼推理，Replicate 训的只能同底模推理。
   **选了在哪训，就在哪出图**——否则白花钱。
2. **百炼微调仅华北2（北京）可用**，且必须用该地域 API Key，跨地域鉴权失败。
3. 百炼训练有 `generation_type: t2i | i2i`——**角色三视图走 `t2i`**；走错则训练数据格式不匹配。

---

## 5. 配音（`voice_director` 候选，全新能力）

| 提供方 | 说明 |
| --- | --- |
| **MiniMax 语音** | 你已在用 MiniMax，语音可同 key 接入 |
| **阿里 CosyVoice**（百炼） | 中文效果强，中国直连 |
| ElevenLabs | 情感/音色丰富，海外 |
| Fish Audio | 开源系，可自托管 |
| **Edge TTS** | 免费（你 `video.py` 里已预留 `pass # TODO`），适合零成本兜底 |

**音乐/音效**：`ACE-Step` 系列（DiffSynth 配置里已有 `Ace-Step1.5`，本地可跑）+ 各云厂商 BGM 库。

**选型建议**：默认 **Edge TTS（免费兜底）→ 百炼 CosyVoice / MiniMax（正式）**，与图像一样做「本地/云端等价 provider」。

---

## 6. 建议给用户的下拉结构（注册表 key 草案）

```python
# app/registry/providers.py —— 供前端渲染为「类别 → 模型」两级下拉
IMAGE_GEN = {
    "local":      ["z-image-turbo"],                       # 本机，零成本
    "dashscope":  ["wan2.7-image-pro", "qwen-image-2.0-pro", "z-image-turbo"],
    "minimax":    ["image-01"],
    "wavespeed":  ["openai/gpt-image-1.5", "google/gemini-3-pro-image",
                   "black-forest-labs/flux-2-pro", "bytedance/seedream-4.5",
                   "tencent/hunyuan-image-3.0", "ideogram/ideogram-2.0"],
    "replicate":  ["black-forest-labs/flux-2-dev", "stabilityai/sd3.5-large"],
}
IMAGE_EDIT = {
    "local":      ["z-image-inpaint"],                     # 需新增路由（diffusers 自带）
    "dashscope":  ["qwen-image-2.0-pro", "qwen-image-edit-max", "qwen-image-edit-plus"],
    "wavespeed":  ["openai/gpt-image-2/edit"],
}
VIDEO_GEN = {
    "local":      ["minimax-h3"],                          # 本机，Disk Offload
    "minimax":    ["MiniMax-H3"],
    "dashscope":  ["wan-*"],                               # 需核实当前视频模型名
    "wavespeed":  ["kling-3.0", "veo-3.1", "seedance"],
    "mobbi":      ["sora-2-pro", "veo-3.1", "kling-3.0", "hailuo"],
}
LORA_TRAINER = {
    "local":      ["diffsynth-qlora"],                     # NF4 + peft
    "dashscope":  ["wan2.7-image-pro", "qwen-image-2.0-pro"],   # 仅北京地域
    "replicate":  ["flux-lora", "sdxl-lora"],
    "wavespeed":  ["training-tools"],
}
TTS = {
    "edge":       ["edge-tts"],                            # 免费
    "dashscope":  ["cosyvoice"],
    "minimax":    ["minimax-speech"],
    "elevenlabs": ["eleven-v3"],
}
```

**每个模型条目建议带元数据**（供 UI 显示，避免用户瞎选）：
```python
ModelSpec(key, label, capability, price_hint, region_ok, needs_proxy,
          supports_lora, notes)
# 例：ModelSpec("openai/gpt-image-1.5", "GPT Image 1.5", "image_gen",
#              "$0.04/张", region_ok=False, needs_proxy=True,
#              supports_lora=False, notes="综合质量最强；需海外网络")
```

---

## 7. 选型决策建议（写进设置页的"怎么选"提示）

| 用户情况 | 推荐 |
| --- | --- |
| 想零成本 / 有 8GB+ 显卡 | **本地**（Z-Image 出图 + DiffSynth QLoRA） |
| 国内、要中文语境、要能训 LoRA | **阿里百炼**（wan2.7 / qwen-image，北京地域） |
| 已有 MiniMax key、做视频 | **MiniMax**（Hailuo，$0.01/秒 最便宜） |
| 要最高画质、不介意成本 | **GPT Image 1.5 / Flux 2 Pro**（经聚合平台） |
| 要多模型随时对比 | **WaveSpeedAI / Replicate**（一个 key 多模型） |
| 要免费开始 | **Edge TTS + 本地 Z-Image**，零 API 成本 |

---

## 8. 待核实（接入前必须逐项验证，不要凭本文档上线）

1. ~~**百炼当前视频生成模型名**与可用地域~~ → **已查实**，见 §9。
2. **ControlNet 在云端的可用性**：**已确认没有**——托管 API 只提供
   首尾帧与参考素材（`reference_image`/`reference_video`/`reference_audio`），
   **没有** depth / pose / canny 的逐帧结构控制接口。
   那只有开源的 `alibaba-pai/MiniMax-H3-Fun-Controlnet-Union` 有。
   → 结论：逐帧结构控制只能走本地；云端走**参考视频**这条路。
3. 各聚合平台（WaveSpeed / Replicate / fal / Mobbi）的**实际可用性、计费方式、是否需要海外支付**。
4. **价格时效**：视频类价格变动频繁（原文明确说 "prices change frequently"），接入时应做**实时价格查询或人工核对**，不要硬编码。
5. 各模型的**商用授权**（尤其 Seedream 经聚合、MiniMax H3 社区许可等）。

---

## 9. 已实现状态（取代 §8 的多数猜测）

实现见 `app/services/video_adapters.py`。架构：**统一请求 + 每 provider 一个适配器**。

```
核心流程构造 VideoRequest（与提供者无关）
        ↓
适配器翻译成各家形状（认证头 / body / 查询方式不同）
        ↓
异步任务：提交 → 轮询 → 取回视频 URL
```

不把各家塞进 `video.py` 的 if-else：每加一家都要动核心流程、容易碰坏别家。

| 提供者 | 覆盖模型 | 条件能力 | 状态 |
| --- | --- | --- | --- |
| `local` | MiniMax H3 NF4（本机） | 首帧 / 尾帧 | ✅ **已跑通** |
| `minimax` | H3 / H3-Max | 首帧 / 尾帧 / 参考图 / **参考视频** / 参考音频 | 已实现，**未跑通** |
| `dashscope_video` | 万相 3.0（standard/prime）、万相 2.2 kf2v、**Vidu** | 同上 | 已实现，**未跑通** |
| `seedance` | doubao-seedance-1-0-pro / lite-i2v | 首帧 / 尾帧 / 参考图 | 已实现，**未跑通** |

> 「未跑通」是**如实标注**，不是谦辞：这些适配器按官方文档实现，
> 首次使用请按实际返回核对端点与参数名。有测试守住这个标注
> （`test_unverified_providers_are_marked`），防止把未验证的当已验证。

### 一个 key 覆盖多家：阿里百炼

`dashscope_video` 用**同一个端点**调多家模型，靠 `model` 字段区分：

| 模型 | media 约定 | 时长 |
| --- | --- | --- |
| `wan3.0-video` / `-prime` | `type` 直接用 `first_frame`/`last_frame`/`reference_*` | 2~30s、30fps、原生带音频 |
| `vidu/*_start-end2video` | **必须正好 2 张 image**，第 1=首帧、第 2=尾帧 | 1~16s |
| `wan2.2-kf2v-flash` | 首尾帧 | 固定 5s |

请求体：`{model, input:{prompt, media:[{type,url}]}, parameters:{...}}`，
必须带请求头 `X-DashScope-Async: enable`；查询 `GET /api/v1/tasks/{task_id}`。

### 互斥约束（已在代码里做提交前校验）

> 首尾帧（i2v）与参考素材（r2v）**互斥**。

- 万相原文：*"The two modes are mutually exclusive. Do not pass reference_xx
  and first_frame/last_frame at the same time."*
- MiniMax 原文：*"Image-to-video and reference-to-video are mutually exclusive"*

`validate_exclusive()` 在提交前挡住非法组合——不挡的话要等任务 FAILED
才知道，白等几分钟（视频生成 1~5 分钟）。

### 白模参考视频的完整路径

```
预演编辑器导出帧序列
      ↓
POST /v1/files/upload   (multipart, purpose=video_generation_input)
      ↓ 返回 file_id
生成请求用 "mm_file://{file_id}" 作 url（role=reference_video）
      ↓
模型复刻运镜与走位
```

文件限制：参考视频 mp4/mov ≤50MB、参考图 ≤30MB、参考音频 ≤15MB，
**有效期 7 天**（过期需重传，否则返回 file expired）。

---

## 参考来源

- [2026 图像生成 API 完整指南（LM Arena Elo + 价格）](https://wavespeed.ai/blog/es/posts/complete-guide-ai-image-apis-2026/)
- [2026 AI 视频生成成本：每秒价格全表](https://mobbi.ai/blog/ai-video-generation-cost-2026)
- [阿里百炼 · 微调图像生成模型（SFT-LoRA）](https://help.aliyun.com/zh/model-studio/wan-image-generation-finetune-guide)
- [阿里百炼 · 千问图像编辑 API](https://help.aliyun.com/zh/model-studio/qwen-image-edit-api)
- [WaveSpeedAI · LoRA 训练工具](https://wavespeed.ai/fr/collections/training-tools)
- [fal vs Replicate 对比](https://imageat.com/compare/fal-vs-replicate)
- [2026 最佳 LoRA 训练平台](https://www.sozee.ai/resources/best-custom-lora-training-platforms/)
- [TTS 模型排行榜（Artificial Analysis）](https://artificialanalysis.ai/text-to-speech/leaderboard/provider-voice/open-weights)
