"""内置模型目录与任务档案。

数据来源
--------
- ``docs/02-模型调研与适配方案.md``（模型能力、授权、实测）
- ``research/model-survey/comfyui-5090-model-bench.md``（第三方受控基准）
- ``research/model-survey/awesome-minimax-H3.md``（H3 生态索引）
- ``config/project.toml``（本机硬件约束与三档分辨率）

**"双通道"在本文件的体现**
------------------------
同一个模型可以同时声明 **ComfyUI 通道**（本地节点图）与 **HTTP 通道**（直连服务）。
用户选择"本地 / API"改变的是**走哪条通道**，不改变模型本身。

三种典型形态都在这里：

  ``minimax-h3``      真·双通道：本地 ComfyUI 节点 或 云端 HTTP API
  ``z-image-turbo``   两个**本地**后端：ComfyUI 节点图 或 ``local-gen`` HTTP 服务
  ``deepseek-v4``     仅 HTTP（无本地权重）

**"通道"与"模型"正交**是这套设计的要点：加一个通道不改模型，加一个模型不改通道。

数据的诚实性约定
----------------
``PerformanceRecord.source`` 必须如实标注来源：

  ``local_measured``          本机 4060 实测。**只有这类数字能用于本机工期预算**
  ``third_party_controlled_ab``  他人受控对照（如 5090 基准）。**不可直接外推到 4060**
  ``vendor_claim``            厂商标称。仅作参考

不标来源的性能数字是**有害的** —— 4060 与 5090 差 5~10 倍，
拿别人数字做预算会超支一个数量级。

⚠️ 端点的来源：**环境变量优先，其次本文件的内置默认**
----------------------------------------------------
本文件里的 ``endpoint`` 是**内置默认值**，不是最终值。端点属于
`STRUCTURE.md` 原则 1 里"会因换机器而变化"的部署参数，
因此可由环境变量覆盖（键名与 ``config/.env`` 同一套约定）：

  ``COMFYUI_ENDPOINT``     覆盖 ComfyUI 通道
  ``LOCAL_GEN_ENDPOINT``   覆盖 local-gen HTTP 通道
  ``MINIMAX_ENDPOINT``     覆盖 MiniMax API

**为什么必须有这一层**：``config/.env`` 里早就写了
``LOCAL_GEN_ENDPOINT=`` 与 ``COMFYUI_ENDPOINT=http://127.0.0.1:8188``，
但此前**没有任何代码读它们** —— 端点其实硬编码在下面。
于是"改了 `.env` 却不生效，且不报任何错"。

**声明了却不生效的配置比没有配置更糟** —— 它让人以为改对了。
"""

from __future__ import annotations

import os

from little_crew.models.spec import (
    Backend,
    BackendKind,
    Capability,
    CostProfile,
    Maturity,
    ModelRegistry,
    ModelSpec,
    PerformanceRecord,
    ResourceProfile,
    TaskKind,
    TaskProfile,
)

__all__ = ["build_default_registry", "DEFAULT_TASK_PROFILES", "profile_for",
           "resolve_endpoint"]


# ==========================================================================
# 端点解析：环境变量优先于内置默认
# ==========================================================================

def resolve_endpoint(default: str, env_key: str) -> str:
    """取端点：**环境变量优先，其次内置默认**。

    ``config/.env`` 里声明了 ``COMFYUI_ENDPOINT`` / ``LOCAL_GEN_ENDPOINT``，
    但此前没有代码读它们，端点其实硬编码在 ``build_default_registry`` 里 ——
    于是"改了 `.env` 却不生效，且不报任何错"。

    ``env_key`` 为空、或环境变量为空/全空白时，**回退到默认值而不是空串** ——
    空端点的报错信息远比"用了默认值"难懂
    （`miniMax-h3` 此前就带着空 endpoint，注释说"由 .env 提供"，但 .env 里没有那个键）。
    """
    if not env_key:
        return default
    val = (os.environ.get(env_key) or "").strip()
    return val or default


# ==========================================================================
# 本机实测常量（唯一可用于本机预算的数字）
# ==========================================================================

#: 本机硬件标识。所有 local_measured 记录的 measured_on 都用它。
_LOCAL = "RTX 4060 Laptop 8GB / RAM 15.8GB"


# ==========================================================================
# 文本模型（仅 HTTP —— 无本地权重）
# ==========================================================================

def _text_models() -> list[ModelSpec]:
    return [
        ModelSpec(
            model_id="deepseek-v4",
            display_name="DeepSeek V4",
            task=TaskKind.TEXT_STRUCTURED,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://api.deepseek.com",
                api_key_env="DEEPSEEK_API_KEY",
                path="/chat/completions",
                # ⚠️ 模板必须由 json.dumps 生成后再手工插入占位符，
                # 不要手写 JSON 字符串 —— 提示词里的引号会破坏结构。
                #
                # ⚠️ **两个模板都要有**：`body_template` 是不带
                # `response_format` 的**纯文本**请求；`body_template_json`
                # 才带 json_object。早先只有一个且硬编码了 json_object，
                # 于是**纯文本调用必然 400**（DeepSeek 要求启用 json_object
                # 时提示词里必须有 "json" 字样）。踩过。
                body_template=(
                    '{"model": "deepseek-chat",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature}}'
                ),
                body_template_json=(
                    '{"model": "deepseek-chat",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature},'
                    ' "response_format": {"type": "json_object"}}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(
                context_tokens=128_000,
                supports_negative_prompt=False,
                structured_output_score=0.92,
            ),
            cost=CostProfile(kind="metered", cny_per_1k_tokens=0.001),
            license_id="deepseek-v4",
            maturity=Maturity.PRODUCTION,
            prompt_profile="zh-structured-v1",
            notes="分镜结构化输出的主力。中文剧作理解好，JSON 模式稳定。",
        ),
        ModelSpec(
            model_id="qwen3-max",
            display_name="通义千问 3 Max",
            task=TaskKind.TEXT_LONG_CONTEXT,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://dashscope.aliyuncs.com",
                api_key_env="DASHSCOPE_API_KEY",
                path="/compatible-mode/v1/chat/completions",
                body_template=(
                    '{"model": "qwen3-max",'
                    ' "messages": [{"role": "user", "content": "{prompt}"}],'
                    ' "temperature": {temperature}}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(context_tokens=1_000_000,
                                  structured_output_score=0.88),
            cost=CostProfile(kind="metered", cny_per_1k_tokens=0.002),
            license_id="qwen3-max",
            maturity=Maturity.PRODUCTION,
            prompt_profile="zh-structured-v1",
            notes="百万级上下文 —— 整本小说改编用它，避免分段丢失伏笔。",
        ),
        # ── 跨模型族盲读的**审核方** ──────────────────────────────
        #
        # 为什么单独列一条而不是复用 deepseek：**平台要求"审核方与生成方
        # 不同族"**，同族判定会被作废为 ESCALATE（见 docs/00 §5.9）。
        # MiMo 是小米的模型，与 DeepSeek 不同厂 —— 族属成立。
        #
        # 端点是官方 OpenAI 兼容接口（注意域名是 xiaomimimo.com，
        # 不是 xiaomi.com —— 拼错会得到 DNS 失败而不是 401，更难查）。
        ModelSpec(
            model_id="mimo-v2.6-flash",
            display_name="小米 MiMo v2.6 Flash",
            task=TaskKind.TEXT_STRUCTURED,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://api.xiaomimimo.com/v1",
                api_key_env="MIMO_API_KEY",
                path="/chat/completions",
                # 与 deepseek 同样的两个模板：纯文本 / 严格 JSON。
                # ⚠️ 模型名写死成 mimo-v2.6-flash（本条目就是它）；
                # 换型号请**新增条目**而不是改这里 ——
                # 改了会让"这条记录代表哪个模型"变得不可考。
                body_template=(
                    '{"model": "mimo-v2.6-flash",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature}}'
                ),
                body_template_json=(
                    '{"model": "mimo-v2.6-flash",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature},'
                    ' "response_format": {"type": "json_object"}}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(context_tokens=128_000,
                                  supports_negative_prompt=False,
                                  structured_output_score=0.90),
            cost=CostProfile(kind="metered", cny_per_1k_tokens=0.001),
            license_id="mimo",
            maturity=Maturity.BETA,
            prompt_profile="zh-structured-v1",
            notes="跨模型族盲读的审核方 —— 与生成方（DeepSeek）不同厂，"
                  "满足「审核方与生成方不同族」的硬要求。",
        ),
        # ── 第三模型族：GLM（2026-10-19 加入）──────────────────────
        #
        # ## 为什么需要第三个族
        #
        # 在此之前只有两个族（DeepSeek 生成 / MiMo 审核），于是
        # `dramaturge` 与 `story_editor` **只能用同一个模型** ——
        # 所谓「双维审查」实际是同一模型的两个提示词，
        # 独立性弱于跨族（见 docs/00 §5.10.16 ①）。
        #
        # 加入 GLM 后：生成方 DeepSeek、剧评人 MiMo、文风编辑 GLM ——
        # **三个职位三个族**，跨族约束在「审查视角级」上也成立了。
        #
        # ## ⚠️ 这是一个**推理模型**（实测 2026-10-19）
        #
        # `reasoning_tokens` 约占 completion 的 **80%**：
        #
        #     max_tokens=16   -> completion 16，其中 reasoning 15，**content 为空**
        #     max_tokens=2000 -> completion 176，其中 reasoning 173，content 正常
        #
        # **所以 `max_tokens` 给小了会返回空字符串** —— 而空字符串在
        # 上层看起来像"模型没答"，很难归因。本条目给了足够额度。
        #
        # 实际计费 token 约为显式输出的 **5 倍**，成本估算要按这个算。
        ModelSpec(
            model_id="GLM-5.3-flash",
            display_name="智谱 GLM-5.3 Flash",
            task=TaskKind.TEXT_STRUCTURED,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://open.bigmodel.cn/api/paas/v4",
                api_key_env="GLM_API_KEY",
                path="/chat/completions",
                body_template=(
                    '{"model": "GLM-5.3-flash",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature},'
                    ' "max_tokens": 16384}'
                ),
                body_template_json=(
                    '{"model": "GLM-5.3-flash",'
                    ' "messages": {messages},'
                    ' "temperature": {temperature},'
                    ' "max_tokens": 16384,'
                    ' "response_format": {"type": "json_object"}}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(context_tokens=128_000,
                                  supports_negative_prompt=False,
                                  structured_output_score=0.88),
            cost=CostProfile(kind="metered", cny_per_1k_tokens=0.002),
            license_id="glm",
            maturity=Maturity.BETA,
            prompt_profile="zh-structured-v1",
            notes="第三个模型族。推理模型（reasoning_tokens 约占 80%），"
                  "max_tokens 必须给足否则返回空内容。"
                  "用于让「剧评人 / 文风编辑」分属不同族。",
        ),

        ModelSpec(
            model_id="local-llm",
            display_name="本地 LLM（Ollama 等）",
            task=TaskKind.TEXT_COMPLETE,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="http://127.0.0.1:11434",     # 检测到本机装了 Ollama
                path="/v1/chat/completions",
                body_template=(
                    '{"model": "{model_name}",'
                    ' "messages": [{"role": "user", "content": "{prompt}"}],'
                    ' "temperature": {temperature}}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(context_tokens=32_000, structured_output_score=0.6),
            cost=CostProfile(kind="local", power_cny_per_hour=0.6),
            resource=ResourceProfile(vram_peak_mb=6_000, ram_peak_mb=8_000,
                                     exclusive_gpu=True),
            license_id="local-llm",
            maturity=Maturity.BETA,
            prompt_profile="zh-structured-v1",
            notes=("零成本档。质量低于云端，适合草稿阶段与批量粗筛；"
                   "**注意与图像模型争抢显存** —— 本机同时只能加载一个模型。"),
        ),
        ModelSpec(
            model_id="vlm-judge-cloud",
            display_name="云端视觉审片模型",
            task=TaskKind.VLM_JUDGE,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://dashscope.aliyuncs.com",
                api_key_env="DASHSCOPE_API_KEY",
                path="/compatible-mode/v1/chat/completions",
                body_template=(
                    '{"model": "qwen3-vl-max",'
                    ' "messages": [{"role": "user", "content": ['
                    '{"type": "text", "text": "{prompt}"},'
                    '{"type": "image_url", "image_url": {"url": "{image_url}"}}]}]}'
                ),
                response_path="choices.0.message.content",
            ),),
            capability=Capability(structured_output_score=0.85),
            cost=CostProfile(kind="metered", cny_per_call=0.05),
            license_id="qwen3-vl-max",
            maturity=Maturity.PRODUCTION,
            notes=("**跨模型族盲读**的审核方。必须与生成方不同族 —— "
                   "这是「不自我签收」的落点（见设计文档 §12）。"),
        ),
    ]


# ==========================================================================
# 图像模型
# ==========================================================================

def _image_models() -> list[ModelSpec]:
    return [
        ModelSpec(
            model_id="z-image-turbo",
            display_name="Z-Image Turbo",
            task=TaskKind.IMAGE_T2I,
            # ⚠️ **双通道示范**：同一模型，本地两处可跑
            backends=(
                Backend(
                    kind=BackendKind.COMFYUI,
                    endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                    clip_type="lumina2",
                    required_kinds=("model.checkpoint", "prompt.text",
                                    "latent.empty", "sample.ksampler", "decode.vae",
                                    "io.save_image"),
                    weight_files=("z-image-turbo.safetensors",),
                    notes="本地 ComfyUI 通道",
                ),
                Backend(
                    kind=BackendKind.HTTP,
                    endpoint=resolve_endpoint("http://127.0.0.1:8100",
                                       "LOCAL_GEN_ENDPOINT"),
                    path="/v1/images/generations",
                    api_key_env="LOCAL_GEN_API_KEY",    # 本地服务通常不需要
                    body_template=(
                        '{"model": "z-image-turbo", "prompt": "{prompt}",'
                        ' "negative_prompt": "{negative_prompt}",'
                        ' "width": {width}, "height": {height},'
                        ' "num_inference_steps": {steps}, "seed": {seed}}'
                    ),
                    response_path="data.0.url",
                    is_local_by_default=True,
                    notes="local-gen HTTP 通道（零成本本地服务）",
                ),
            ),
            capability=Capability(
                max_resolution=(1536, 1536),
                supports_negative_prompt=True,
                supports_reference_image=True,
                supports_lora=False,          # 本机无法训练，且 4bit 底模不支持
                supports_controlnet=True,
                chinese_text_score=0.75,
            ),
            resource=ResourceProfile(
                # 4bit NF4 量化版实测；fp32 底模需 22.93 GB > 本机 15.8 GB 内存
                vram_peak_mb=7_500, ram_peak_mb=14_000, disk_gb=7.6,
                needs_offload=True, exclusive_gpu=True,
            ),
            cost=CostProfile(kind="local", power_cny_per_hour=0.6),
            performance=PerformanceRecord(
                # 本机实测：4bit 文生图 30~32 s/张
                measured_ms={"1024x1024@4step": 31_000},
                measured_on=_LOCAL,
                source="local_measured",
            ),
            license_id="z-image-turbo",
            maturity=Maturity.BETA,
            prompt_profile="zh-danbooru-tags",
            notes=("草稿档主力。4 步出图，速度是它最大的优势。"
                   "**fp32 底模需 22.93 GB 内存 > 本机 15.8 GB，故无法训练角色 LoRA** "
                   "—— 一致性必须参考图优先。"),
        ),
        ModelSpec(
            model_id="flux2-klein-4b",
            display_name="FLUX.2 Klein 4B",
            task=TaskKind.IMAGE_EDIT,
            backends=(Backend(
                kind=BackendKind.COMFYUI,
                endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                clip_type="flux2",
                required_kinds=("model.checkpoint", "prompt.text",
                                "latent.empty", "sample.ksampler", "decode.vae",
                                "io.save_image"),
            ),),
            capability=Capability(
                max_resolution=(2048, 2048),
                supports_negative_prompt=True,
                supports_reference_image=True,
                chinese_text_score=0.85,
            ),
            resource=ResourceProfile(vram_peak_mb=8_000, ram_peak_mb=14_000,
                                     needs_offload=True, exclusive_gpu=True),
            cost=CostProfile(kind="local", power_cny_per_hour=0.6),
            license_id="flux2-klein-4b",
            maturity=Maturity.BETA,
            prompt_profile="en-natural",
            notes=("**4B = Apache-2.0 可商用；9B 禁商用** —— 同名相反，"
                   "授权门禁按 license_id 精确区分，不可混用。"),
        ),
        ModelSpec(
            model_id="sdxl-base",
            display_name="SDXL Base 1.0",
            task=TaskKind.IMAGE_T2I,
            backends=(Backend(
                kind=BackendKind.COMFYUI,
                endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                clip_type="sdxl",
                required_kinds=("model.checkpoint", "prompt.text", "prompt.negative",
                                "latent.empty", "sample.ksampler", "decode.vae",
                                "io.save_image"),
                weight_files=("sd_xl_base_1.0.safetensors",),
            ),),
            capability=Capability(
                max_resolution=(1536, 1536),
                supports_negative_prompt=True,
                supports_reference_image=True,
                supports_lora=True,
                supports_controlnet=True,
                chinese_text_score=0.3,       # 中文文字渲染弱
            ),
            resource=ResourceProfile(vram_peak_mb=6_500, disk_gb=6.9,
                                     exclusive_gpu=True),
            cost=CostProfile(kind="local", power_cny_per_hour=0.6),
            license_id="sdxl-base-1.0",
            maturity=Maturity.PRODUCTION,
            prompt_profile="en-danbooru-tags",
            notes=("生态最成熟、ControlNet/IPAdapter 支持最完整的档。"
                   "**它是编译规则表里的默认底模** —— 装上即可跑通端到端闭环。"),
        ),
    ]


# ==========================================================================
# 视频模型
# ==========================================================================

def _video_models() -> list[ModelSpec]:
    return [
        ModelSpec(
            model_id="minimax-h3",
            display_name="MiniMax H3",
            task=TaskKind.VIDEO_I2V,
            backends=(
                Backend(
                    kind=BackendKind.COMFYUI,
                    endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                    clip_type="minimax",
                    required_kinds=("video.i2v_h3", "decode.vae", "io.save_image"),
                    notes="本地 ComfyUI 节点通道（需 Comfy-Org/MiniMax-H3 权重）",
                ),
                Backend(
                    kind=BackendKind.HTTP,
                    # ⚠️ 此前这里留空，注释说"由 config/.env 提供"，
                      # 但 .env 里并没有 MINIMAX_ENDPOINT 这个键 ——
                      # 于是它**一直是个空端点**。声明了却不存在的配置
                      # 比没有配置更糟：它让人以为已经接好了。
                      endpoint=resolve_endpoint(
                          "https://api.minimaxi.com", "MINIMAX_ENDPOINT"),
                    api_key_env="MINIMAX_API_KEY",
                    path="/v1/video_generation",
                    body_template=(
                        '{"model": "MiniMax-Hailuo-3", "prompt": "{prompt}",'
                        ' "first_frame_image": "{image_url}",'
                        ' "duration": {duration}, "resolution": "{resolution}"}'
                    ),
                    response_path="task_id",
                    async_task=True,
                    status_path="/v1/query/video_generation?task_id={task_id}",
                    status_success_value="Success",
                    notes="官方 API 或自建服务。endpoint 由 config/.env 提供",
                ),
                Backend(
                    kind=BackendKind.HTTP,
                    # **本地零成本通道**（2026-10-18 加）。
                    #
                    # 在此之前 H3 只有两条路：本机 ComfyUI（没起）与 MiniMax
                    # 官方 API（没有 key）。而 `capability.native_audio=True`
                    # 早就声明了 —— **模型档案知道它有音频，但没有任何一条
                    # 能走通的通道**。声明了却走不通，比没声明更糟。
                    endpoint=resolve_endpoint("http://127.0.0.1:8100",
                                              "LOCAL_GEN_ENDPOINT"),
                    api_key_env="LOCAL_GEN_API_KEY",   # 本地服务通常不需要
                    path="/v1/videos/generations",
                    body_template=(
                        '{"prompt": "{prompt}", "image_path": "{image_path}",'
                        ' "width": {width}, "height": {height},'
                        # ⚠️ 占位符名必须与 `h3_video.video_values()` 的键**完全一致**。
                        # 这里最初写 `{duration}`（借自 ComfyUI/H3 的命名），
                        # 而值那边给的是 `seconds` —— 真调用立刻报
                        # "请求体模板里有未替换的占位符 ['duration']"。
                        # **假渲染器不看模板，所以那种单测永远发现不了。**
                        ' "seconds": {seconds},'
                        ' "num_inference_steps": {steps}, "seed": {seed}}'
                    ),
                    # 响应长这样（实测）：
                    #   {"job_id": "...", "status": "queued", "poll": "..."}
                    #   -> {"status": "succeeded",
                    #       "result": {"url": "...mp4", "frames": 107, "fps": 24}}
                    response_path="result.url",
                    async_task=True,
                    status_path="/v1/videos/generations/{task_id}",
                    status_success_value="succeeded",
                    is_local_by_default=True,
                    notes=("local-gen 本地异步通道（零成本）。"
                           "**实测产物自带 AAC 音轨**（容器里 2 条 trak："
                           "avc1 视频 + mp4a 音频）—— 不需要另做 TTS"),
                ),
            ),
            capability=Capability(
                max_resolution=(1344, 768),
                # ⚠️ 本机实测硬上限：12 秒必 OOM，且**降分辨率无效**
                # （瓶颈是注意力序列长度，不是像素数）
                max_duration_s=10.0,
                supports_reference_image=True,
                supports_multi_reference=True,   # ref2v 走 COMFY_AUTOGROW_V3 动态端口
                native_audio=True,
            ),
            resource=ResourceProfile(
                vram_peak_mb=8_000, ram_peak_mb=15_800, disk_gb=27.7,
                needs_offload=True, exclusive_gpu=True,
            ),
            cost=CostProfile(kind="metered", cny_per_second=0.5),
            performance=PerformanceRecord(
                # 本机实测：5 秒 / 8 步
                measured_ms={"5s@8step": 480_000},
                measured_on=_LOCAL,
                source="local_measured",
            ),
            license_id="minimax-h3",
            maturity=Maturity.BETA,
            prompt_profile="zh-natural",
            notes=("音画同出，漫剧主力。**授权排除 US/EU/UK/KR，中国大陆在范围内**；"
                   "营收 2000 万美元门槛；界面须标注。"
                   "本机硬上限 10 秒，12 秒必 OOM 且降分辨率无效。"),
        ),
        ModelSpec(
            model_id="wan22-5b-ti2v",
            display_name="Wan 2.2 5B TI2V",
            task=TaskKind.VIDEO_T2V,
            backends=(Backend(
                kind=BackendKind.COMFYUI,
                endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                clip_type="wan",
                required_kinds=("video.i2v", "decode.vae", "io.save_image"),
            ),),
            capability=Capability(
                max_resolution=(1280, 704),
                max_duration_s=5.04,          # 121 帧 @ 24fps
                native_audio=False,
            ),
            resource=ResourceProfile(vram_peak_mb=10_000, ram_peak_mb=15_000,
                                     needs_offload=True, exclusive_gpu=True),
            cost=CostProfile(kind="local", power_cny_per_hour=0.6),
            performance=PerformanceRecord(
                # 第三方受控基准（5090），**不可外推到 4060**
                measured_ms={"1280x704@121frame": 355_100, "704x384@49frame": 34_600},
                measured_on="RTX 5090 Laptop 24GB",
                source="third_party_controlled_ab",
            ),
            license_id="wan2.2-5b",
            maturity=Maturity.PRODUCTION,
            prompt_profile="zh-natural",
            notes=("Apache-2.0 可商用，提示词遵循与物理真实感最好。"
                   "**但 fp16 需 10 GB 显存 > 本机 8 GB**，需 offload 后重测。"
                   "⚠️ Wan 开源线到 2.2 为止，**2.7 开源是假消息**。"),
        ),
    ]


# ==========================================================================
# 音频模型
# ==========================================================================

def _audio_models() -> list[ModelSpec]:
    return [
        ModelSpec(
            model_id="h3-native-audio",
            display_name="H3 原生音轨",
            task=TaskKind.AUDIO_TTS,
            backends=(Backend(
                kind=BackendKind.COMFYUI,
                endpoint=resolve_endpoint("http://127.0.0.1:8188",
                                       "COMFYUI_ENDPOINT"),
                required_kinds=("video.i2v_h3",),
                notes="随视频一同生成，无需独立 TTS 节点",
            ),),
            capability=Capability(native_audio=True),
            resource=ResourceProfile(vram_peak_mb=8_000, exclusive_gpu=True),
            cost=CostProfile(kind="local"),
            license_id="minimax-h3",
            maturity=Maturity.BETA,
            notes=("**零额外成本**：音轨与视频同一遍前向产出，不额外占时间。"
                   "优先于独立 TTS —— 省一次模型加载（本机同时只能加载一个模型）。"),
        ),
        ModelSpec(
            model_id="tts-http",
            display_name="云端 TTS",
            task=TaskKind.AUDIO_TTS,
            backends=(Backend(
                kind=BackendKind.HTTP,
                endpoint="https://dashscope.aliyuncs.com",
                api_key_env="DASHSCOPE_API_KEY",
                path="/api/v1/services/aigc/multimodal-generation/generation",
                body_template=(
                    '{"model": "qwen3-tts-flash", "input": {"text": "{text}",'
                    ' "voice": "{voice}"}}'
                ),
                response_path="output.audio.url",
            ),),
            capability=Capability(native_audio=True),
            cost=CostProfile(kind="metered", cny_per_call=0.02),
            license_id="tts-http",
            maturity=Maturity.PRODUCTION,
            notes="旁白与独白。H3 音轨不可用时降级到这里。",
        ),
    ]


# ==========================================================================
# 组装
# ==========================================================================

def build_default_registry() -> ModelRegistry:
    """构建内置模型目录。"""
    return ModelRegistry(
        _text_models() + _image_models() + _video_models() + _audio_models()
    )


#: 任务档案：把"某个任务需要什么"表达成可求解的约束。
#: **生产代码不应硬编码模型 id**，而应通过档案求解 —— 换模型不改代码。
DEFAULT_TASK_PROFILES: dict[str, TaskProfile] = {
    "shot.image.draft": TaskProfile(
        task=TaskKind.IMAGE_T2I,
        must={"supports_negative_prompt": True},
        prefer={"estimatedMs": (2.0, "min"), "chinese_text_score": (1.0, "max")},
        phase="draft",
    ),
    "shot.image.final": TaskProfile(
        task=TaskKind.IMAGE_T2I,
        must={"supports_negative_prompt": True,
              "max_resolution": (1344, 768)},
        prefer={"chinese_text_score": (1.5, "max"), "estimatedMs": (1.0, "min")},
        phase="final",
    ),
    "shot.image.identity_locked": TaskProfile(
        task=TaskKind.IMAGE_T2I,
        must={"supports_reference_image": True},
        prefer={"chinese_text_score": (1.0, "max")},
        phase="final",
    ),
    "shot.video": TaskProfile(
        task=TaskKind.VIDEO_I2V,
        must={"supports_reference_image": True},
        prefer={"estimatedMs": (1.0, "min")},
        phase="final",
    ),
    "shot.video.identity_locked": TaskProfile(
        task=TaskKind.VIDEO_REF2V,
        must={"supports_multi_reference": True},
        phase="final",
    ),
    "script.structured": TaskProfile(
        task=TaskKind.TEXT_STRUCTURED,
        must={"structured_output_score": 0.8},
        prefer={"context_tokens": (1.0, "max"), "costCny": (1.0, "min")},
    ),
    "script.long_context": TaskProfile(
        task=TaskKind.TEXT_LONG_CONTEXT,
        must={"context_tokens": 500_000},
        prefer={"context_tokens": (1.0, "max")},
    ),
    "review.vlm_blind": TaskProfile(
        task=TaskKind.VLM_JUDGE,
        # 盲读要求：审核方必须与生成方不同族。族别在编排层校验，
        # 这里只表达"需要一个能看图的审核模型"。
        must={"structured_output_score": 0.7},
    ),
    "audio.dialogue": TaskProfile(
        task=TaskKind.AUDIO_TTS,
        must={"native_audio": True},
        prefer={"costCny": (2.0, "min")},
    ),
}


def profile_for(name: str) -> TaskProfile:
    """按名取任务档案。**用名而非硬编码模型 id**，换模型时只改注册表。"""
    try:
        return DEFAULT_TASK_PROFILES[name]
    except KeyError as e:
        raise KeyError(
            f"未知任务档案 {name!r}。可用：{sorted(DEFAULT_TASK_PROFILES)}"
        ) from e
