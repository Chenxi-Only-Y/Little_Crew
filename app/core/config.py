"""小剧组 (Little Crew) — 应用配置。

读取根目录 .env（若存在）或系统环境变量，集中管理密钥、Mock 开关与路径。
使用 pydantic-settings，字段名与 .env 变量大小写不敏感对应（如
`deepseek_api_key` <-> `DEEPSEEK_API_KEY`）。

示例：
    from app.core.config import settings
    print(settings.deepseek_model)
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：app/core/config.py -> 上溯三级
BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------- 大语言模型 (OpenAI 兼容协议，可切换提供者) -------------------
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # 可切换的其他 LLM 提供者（deepseek | zhipu | openai | ollama | custom）
    #
    # ⚠️ 设计决定（2026-10-02）：**不设静默默认**。
    # 用户需先显式选择「云端 API / 本地推理」（`llm_provider_mode`），
    # 再按模式取默认提供者；也可直接填具体提供者覆盖。
    # 两者都没设时明确报错，而不是猜——避免"没配却以为配了"或意外产生费用。
    llm_provider: str = ""
    llm_api_key: str = ""       # 优先；为空则退回 deepseek_api_key
    llm_base_url: str = ""      # 覆盖预设
    llm_model: str = ""         # 覆盖预设

    # ------------------- 图像生成 (阿里百炼 / Replicate / MiniMax) -------------------
    image_api_key: str = ""          # 备用（IMAGE_API_KEY）
    dashscope_api_key: str = ""      # 阿里百炼（读取 DASHSCOPE_API_KEY）
    # ⚠️ 不设静默默认：用户先选 image_provider_mode（cloud/local），
    # 或直接指定 image_provider。未选时明确报错（见 app/services/providers.py）。
    image_provider: str = ""         # dashscope | replicate | minimax | local
    image_base_url: str = ""
    image_model: str = "image-01"    # MiniMax 文生图模型（image-01）；dashscope 时可为 z-image-turbo/wanx
    replicate_api_token: str = ""

    # MiniMax（文生图 / 图生视频）。读取 MINIMAX_API_KEY。
    # 注意：平台分国内(api.minimaxi.com)与海外(api.minimax.io)两套 host，按你的密钥选。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com"

    # ------------------- 本地推理服务（零成本测试，部署于 D:\AI\local-gen）-------------------
    # 把 IMAGE_PROVIDER / VIDEO_PROVIDER 设为 "local" 即走本机推理，不再产生 API 费用。
    # 服务的启动方式与显存策略见 D:\AI\local-gen\README.md。
    local_gen_url: str = "http://127.0.0.1:8100"
    local_image_steps: int = 8         # Z-Image-Turbo 是蒸馏模型，固定 8 步（勿调高，否则画质变差）
    local_video_steps: int = 50        # MiniMax H3 步数；Disk Offload 下很慢，测试建议 8~15
    local_gen_timeout: int = 7200      # 本地视频生成超时（秒），Disk Offload 可能几十分钟

    # ------------------- 视频合成 -------------------
    # ffmpeg: 本地图片幻灯 + Edge TTS（需安装 FFmpeg）
    # minimax: 调用 MiniMax-H3 图生视频（每段 ≤ video_max_duration 秒、≤ video_resolution 分辨率）
    # ⚠️ 不设静默默认：用户先选 video_provider_mode（cloud/local），
    # 或直接指定 video_provider。未选时明确报错（见 app/services/providers.py）。
    video_provider: str = ""          # minimax | dashscope_video | seedance | local | ffmpeg
    video_model: str = "MiniMax-H3"
    video_base_url: str = ""          # 覆盖默认 base_url
    # 阿里百炼（万相 / Vidu 等）——一个 key 覆盖多家；也用于视频生成
    dashscope_video_base_url: str = "https://dashscope.aliyuncs.com"
    # 火山方舟（Seedance）。端点与模型名请按方舟控制台核对。
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com"
    # ffmpeg 可执行文件路径。空 = 自动查找：
    #   设置项 → LITTLE_CREW_FFMPEG → PATH → 外部工具目录的 bin\ffmpeg.exe。
    # 用途：把预演导出的帧序列编码成 mp4（白模参考视频）、
    #       图片拼成视频、把配音混流进视频。
    ffmpeg_path: str = ""
    #: **外部工具与模型目录**（默认与 `local-gen` 同级）。
    #:
    #: 为什么要有这个概念：模型权重动辄几 GB，装进项目目录会污染仓库、
    #: 也让项目无法搬迁。本项目遵循一条边界：
    #:
    #:     <项目目录>             只放代码
    #:     D:\AI\little-crew-tools\  放工具（edge-tts / ffmpeg）与模型缓存
    #:     D:\AI\local-gen\          本地推理服务（出图 / 出视频）
    #:
    #: 里面放 `venv\`（边缘 Python 依赖，用**子进程**调用，不设 PYTHONPATH
    #: 以免与项目 venv 的版本互相污染）、`bin\ffmpeg.exe`、`models\hf\`。
    #: 目录不存在时**不报错**，只是对应本地能力不可用。
    little_crew_tools: str = ""
    # 每段时长（秒）。可选档位 5/7/9/12/15，默认 7；
    # **实际上限由 provider 决定**（本地 8GB 只到 9，超过 10s 必 OOM），
    # 详见 app/services/providers.py 的 allowed_durations / resolve_duration。
    video_max_duration: int = 7
    video_resolution: str = "768P"    # 视频最高分辨率（MiniMax-H3 支持 768P | 2K）

    # ------------------- IP-Adapter（角色一致性方案B 参考图生成） -------------------
    ip_adapter_provider: str = "mock"    # replicate | comfyui | mock
    ip_adapter_api_key: str = ""         # 平台 API Key（replicate/fal.ai，可复用 replicate_api_token）
    ip_adapter_model: str = "cross-attention"  # cross-attention | style
    ip_adapter_strength: float = 0.8     # 强度 0~1
    comfyui_url: str = "http://localhost:8188"
    comfyui_workflow: str = "ip_adapter_workflow.json"  # ComfyUI 工作流 JSON 文件

    # ------------------- 一致性评估（方案B） -------------------
    consistency_threshold: float = 75.0  # 一致性评分阈值 0-100，低于此标记偏差/需人工审核
    consistency_model: str = "clip"      # clip | sentence-transformers

    # ------------------- Mock 开关 (1=占位逻辑, 0=调真实 API) -------------------
    mock_llm: bool = True
    mock_image: bool = True

    # ------------------- 分镜粒度 -------------------
    # total:     全片共 5-8 个镜头（当前收紧，控成本）
    # per_scene: 按场细分，镜头数更多（后续开发完成后再启用）
    shot_mode: str = "total"

    # ------------------- 人工介入（Human-in-the-Loop）-------------------
    # True:  用 checkpointer + interrupt 实现原地暂停/续跑（供前端/API 交互）
    # False: 自动放行人工确认点，整条流程无停顿跑完（供脚本/CLI）
    hitl: bool = Field(default=False, validation_alias="HITL_ENABLED")

    # ------------------- 日志 -------------------
    # 注：`RATE_LIMIT_PER_MINUTE`（限流）与 `QUOTA_DAILY_GENERATIONS`（日额度）
    # **都已删除**（用户 2026-10-02 决定：本项目是开源项目、非商业用途，
    # 不替使用者的成本做拦截）。旧 .env 里留着这两个变量会被忽略，不报错。
    # 自部署者若担心被刷，请在**反向代理层**做限制（见 README 部署提示）。
    log_level: str = "INFO"              # 日志级别

    #: 是否暴露交互式 API 文档（`/docs`、`/redoc`、`/openapi.json`）。
    #: 默认 **False**：这些页面未鉴权即可访问，会把完整 API 结构（每个端点的
    #: 路径与请求体字段）交给任何人——正好是构造越权请求的说明书。
    #: 本机开发要查接口时设 `ENABLE_DOCS=1`（或环境变量 `LITTLE_CREW_DOCS=1`）。
    enable_docs: bool = False

    # ------------------- 视觉质量评估 -------------------
    # True: 占位评分（零成本）；False: 用真实 CLIP(open_clip) 计算 t2i 相似度
    mock_quality: bool = True
    quality_threshold: float = 0.5       # 低于此分则标记为需重试

    # 跳过 CLIP 大模型下载（防卡），一致性改用模拟评分。
    # 对应环境变量 LITTLE_CREW_SKIP_CLIP=1（默认跳过，避免首次下载发卡）。
    skip_clip: bool = Field(default=True, validation_alias="LITTLE_CREW_SKIP_CLIP")

    # ------------------- 剧评 Agent -------------------
    # True: 用占位评审（零成本）；False: 调用 DeepSeek 真实评审
    mock_critics: bool = True

    # ------------------- 视频合成 -------------------
    # True: 占位（免 FFmpeg/edge-tts）；False: 用 FFmpeg 合成 + Edge TTS 配音
    mock_video: bool = True

    # ------------------- 生成数量 / 质量控制 -------------------
    max_images: int = 8               # 全片最多生成的图片/镜头数（控制图片数量）
    video_count: int = 0              # 最多生成的视频段数（0=跟随图片数）
    image_quality: str = "standard"   # standard | high | ultra（图片细节/清晰度）
    video_quality: str = "768P"       # 768P | 2K（视频分辨率，可由设置页选择；1080P 提供者不支持）

    # ------------------- 路径 -------------------
    db_path: str = "./data/little_crew.db"
    output_dir: str = "./data/outputs"

    # 数据库连接串：留空则用 SQLite(db_path)；填 PostgreSQL 等(如
    # postgresql://user:pass@host:5432/db) 即切到对应库
    db_url: str = ""

    # ------------------- 服务 -------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    @property
    def output_dir_path(self) -> Path:
        """输出目录绝对路径；相对路径基于项目根目录解析。"""
        p = Path(self.output_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def db_path_value(self) -> Path:
        """SQLite 数据库绝对路径；相对路径基于项目根目录解析。"""
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p


# 全局唯一配置实例
settings = Settings()
