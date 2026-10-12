"""视频生成提供者适配器（多家 API 统一形状）。

为什么要单独一层
----------------
各家视频 API 的差异集中在三处：**认证头**、**请求体结构**、**任务查询方式**。
把它们塞进 `video.py` 的 if-else 里，每加一家就要动核心流程、且容易碰坏别家。

这里定义一个**统一请求（VideoRequest）+ 每个提供者一个适配器**：
核心流程只构造 VideoRequest，适配器负责翻译成各家形状并轮询取回视频 URL。

已适配
------
- `minimax`         MiniMax H3 / H3-Max（`/v2/video_generation`，content 数组 + role）
- `dashscope`       阿里百炼**一个端点多家模型**（见下），也是本项目最省事的接入方式：
    * `wan3.0-video` / `wan3.0-video-prime`  万相 3.0（media.type 直接用 first_frame/last_frame/reference_*）
    * `vidu/viduq3-*_start-end2video`       Vidu（media 必须正好 2 张 image，第 1 张=首帧、第 2 张=尾帧）
    * `wan2.2-kf2v-flash`                   万相 2.2 首尾帧（固定 5 秒）
- `seedance`        Seedance（火山方舟 Ark；端点/模型名可覆盖，见 `ark_base_url`）

统一请求的能力位
----------------
`first_frame` / `last_frame` 与 `reference_video` 等参考素材**在多数提供者上互斥**
（官方原文：image-to-video and reference-to-video are mutually exclusive）。
适配器会在提交前做一次本地校验，把明显不合法的组合挡住——
否则要等任务跑到 FAILED 才知道，白等几分钟。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 统一请求
# ------------------------------------------------------------------

@dataclass
class VideoRequest:
    """与具体提供者无关的视频生成请求。

    只放**跨家通用**的字段。各家特有的东西放进 `extra`，
    由适配器自行解释（例如万相的 `file`/`link`、Seedance 的专属参数）。
    """

    prompt: str = ""
    first_frame: Optional[str] = None      # URL 或 data URL
    last_frame: Optional[str] = None
    reference_images: list[str] = field(default_factory=list)
    reference_videos: list[str] = field(default_factory=list)
    reference_audios: list[str] = field(default_factory=list)
    duration: int = 5
    resolution: str = "720P"
    ratio: str = "adaptive"
    seed: Optional[int] = None
    watermark: bool = False
    audio: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def used_conditioning(self) -> list[str]:
        """本次请求实际用到了哪些条件维度（用于互斥校验与日志）。"""
        used: list[str] = []
        if self.first_frame:
            used.append("first_frame")
        if self.last_frame:
            used.append("last_frame")
        if self.reference_images:
            used.append("reference_image")
        if self.reference_videos:
            used.append("reference_video")
        if self.reference_audios:
            used.append("reference_audio")
        return used


class VideoAdapterError(RuntimeError):
    """适配器层的可读错误（会一路冒到界面提示）。"""


def validate_exclusive(req: VideoRequest) -> None:
    """首尾帧与参考素材互斥——在提交前挡住，别等任务 FAILED。

    依据：万相官方错误信息原文
        "The two modes are mutually exclusive. Do not pass reference_xx and
         first_frame/last_frame at the same time."
    MiniMax 文档同样写明 i2v 与 r2v 互斥。
    """
    used = set(req.used_conditioning())
    i2v = used & {"first_frame", "last_frame"}
    r2v = used & {"reference_image", "reference_video", "reference_audio"}
    if i2v and r2v:
        raise VideoAdapterError(
            "首尾帧与参考素材互斥，不能在同一次请求里混用："
            f"当前同时传了 {sorted(i2v)} 和 {sorted(r2v)}。"
            "请二选一——要么用首尾帧锚定起止画面，要么用参考素材复刻。"
        )
    if req.last_frame and not req.first_frame:
        # 各家要求 last_frame 与 first_frame 配对（单独给尾帧无意义）
        raise VideoAdapterError("给了尾帧就必须同时给首帧（last_frame 需与 first_frame 配对）。")


# ------------------------------------------------------------------
# 适配器：DashScope（阿里百炼，一个端点多家模型）
# ------------------------------------------------------------------

#: 模型 → 该模型的 media 约定。
#:
#: `start_end_pair` = Vidu 那种"media 必须是正好 2 张 image，顺序即首尾帧"。
#: 其它用 `auto`（万相那套显式 type）。
_DASHSCOPE_MODELS: dict[str, dict[str, Any]] = {
    "wan3.0-video": {"kind": "auto", "durations": (2, 30), "default_duration": 5},
    "wan3.0-video-prime": {"kind": "auto", "durations": (2, 30), "default_duration": 5},
    "wan2.2-kf2v-flash": {"kind": "auto", "durations": (5, 5), "default_duration": 5},
    "vidu/viduq3-turbo_start-end2video": {
        "kind": "start_end_pair", "durations": (1, 16), "default_duration": 5,
    },
    "vidu/viduq3-pro_start-end2video": {
        "kind": "start_end_pair", "durations": (1, 16), "default_duration": 5,
    },
    "vidu/viduq2-turbo_start-end2video": {
        "kind": "start_end_pair", "durations": (1, 10), "default_duration": 5,
    },
    "vidu/viduq2-pro_start-end2video": {
        "kind": "start_end_pair", "durations": (1, 10), "default_duration": 5,
    },
}


def _build_dashscope_body(model: str, req: VideoRequest, extra: dict) -> dict:
    """把统一请求翻成 DashScope 的 body。

    DashScope 的形状：`{model, input:{prompt, media:[{type,url}]}, parameters:{...}}`
    """
    spec = _DASHSCOPE_MODELS.get(model)
    if spec is None:
        # 未登记的模型：按万相那套自动处理（它是这个端点上最宽的约定）
        spec = {"kind": "auto"}
        logger.warning("[video] DashScope 模型 %s 未登记，按 auto 约定构造", model)

    media: list[dict] = []
    if spec["kind"] == "start_end_pair":
        # Vidu：media 必须正好 2 张 image，顺序即首帧/尾帧。
        # 只给首帧时也要求 2 张？官方说"There must be exactly 2 images"——
        # 所以缺尾帧时用首帧补，保证张数合法（否则直接被拒）。
        if not req.first_frame:
            raise VideoAdapterError(
                f"{model} 需要首帧与尾帧两张图（media 必须正好 2 张 image）。"
            )
        media.append({"type": "image", "url": req.first_frame})
        media.append({"type": "image", "url": req.last_frame or req.first_frame})
        if req.reference_videos or req.reference_audios or req.reference_images:
            raise VideoAdapterError(
                f"{model} 只接受首尾帧两张图，不支持参考素材。"
                "需要参考视频请改用 wan3.0-video（走 reference_video）。"
            )
    else:
        if req.first_frame:
            media.append({"type": "first_frame", "url": req.first_frame})
        if req.last_frame:
            media.append({"type": "last_frame", "url": req.last_frame})
        for u in req.reference_images:
            media.append({"type": "reference_image", "url": u})
        for u in req.reference_videos:
            media.append({"type": "reference_video", "url": u})
        for u in req.reference_audios:
            media.append({"type": "reference_audio", "url": u})
        # 万相特有：文件 / 网页
        if extra.get("file"):
            media.append({"type": "file", "url": extra["file"]})
        if extra.get("link"):
            media.append({"type": "link", "url": extra["link"]})

    params: dict[str, Any] = {
        "resolution": req.resolution,
        "duration": req.duration,
        # adaptive 让模型按输入素材比例自动决定，避免用户还要算宽高比
        "ratio": req.ratio,
        "watermark": req.watermark,
        # 短提示词开启改写提升明显（官方建议），除非用户明确关掉
        "prompt_extend": bool(extra.get("prompt_extend", True)),
    }
    if req.audio is not None:
        params["audio"] = req.audio
    if req.seed is not None:
        params["seed"] = req.seed

    body: dict[str, Any] = {"model": model, "parameters": params}
    inp: dict[str, Any] = {}
    if req.prompt:
        inp["prompt"] = req.prompt
    if media:
        inp["media"] = media
    body["input"] = inp
    return body


# ------------------------------------------------------------------
# 适配器：MiniMax（/v2/video_generation，content 数组 + role）
# ------------------------------------------------------------------

def _build_minimax_body(model: str, req: VideoRequest, extra: dict) -> dict:
    """MiniMax v2：`content` 数组，元素用 `role` 区分用途。"""
    content: list[dict] = []
    if req.prompt:
        content.append({"type": "text", "text": req.prompt})
    if req.first_frame:
        content.append({
            "type": "image_url",
            "image_url": {"url": req.first_frame},
            "role": "first_frame",
        })
    if req.last_frame:
        content.append({
            "type": "image_url",
            "image_url": {"url": req.last_frame},
            "role": "last_frame",
        })
    for u in req.reference_images:
        content.append({
            "type": "image_url", "image_url": {"url": u}, "role": "reference_image",
        })
    for u in req.reference_videos:
        content.append({
            "type": "video_url", "video_url": {"url": u}, "role": "reference_video",
        })
    for u in req.reference_audios:
        content.append({
            "type": "audio_url", "audio_url": {"url": u}, "role": "reference_audio",
        })

    body: dict[str, Any] = {
        "model": model,
        "content": content,
        "resolution": req.resolution,
        "duration": req.duration,
        "ratio": req.ratio,
    }
    if req.seed is not None:
        body["seed"] = req.seed
    if req.watermark:
        body["watermark"] = True
    return body


# ------------------------------------------------------------------
# 适配器：Seedance（火山方舟 Ark）
# ------------------------------------------------------------------

def _build_seedance_body(model: str, req: VideoRequest, extra: dict) -> dict:
    """Seedance（方舟 Ark）形状：`{model, content:[{type,text|image_url|video_url}], ...}`。

    ⚠️ 该形状依据方舟公开文档整理，**尚未用真实 key 跑通**。
    端点和模型名都可从配置覆盖（`ark_base_url` / `video_model`），
    首次使用请以 `GET /api/video/providers` 返回的提示为准核对。
    """
    content: list[dict] = []
    if req.prompt:
        content.append({"type": "text", "text": req.prompt})
    if req.first_frame:
        content.append({
            "type": "image_url",
            "image_url": {"url": req.first_frame},
            "role": "first_frame",
        })
    if req.last_frame:
        content.append({
            "type": "image_url",
            "image_url": {"url": req.last_frame},
            "role": "last_frame",
        })
    for u in req.reference_images:
        content.append({
            "type": "image_url", "image_url": {"url": u}, "role": "reference_image",
        })
    # 方舟的参考视频档位并不统一：有的用 video_url，有的只收首尾帧。
    for u in req.reference_videos:
        content.append({
            "type": "video_url", "video_url": {"url": u}, "role": "reference_video",
        })

    body: dict[str, Any] = {
        "model": model,
        "content": content,
        "duration": req.duration,
        "resolution": _seedance_resolution(req.resolution),
    }
    if req.seed is not None:
        body["seed"] = req.seed
    if req.watermark:
        body["watermark"] = True
    return body


def _seedance_resolution(r: str) -> str:
    """方舟用的是 `720p` 这种小写，万相用 `720P`。做一次归一。"""
    return (r or "720P").lower()


# ------------------------------------------------------------------
# 提供者注册表
# ------------------------------------------------------------------

@dataclass(frozen=True)
class VideoProvider:
    """一个可用的视频生成提供者。

    Attributes:
        key:           标识（与设置项 `video_provider` 取值一致）
        label:         中文名
        category:      local / cn_cloud / overseas_cloud
        base_url:      默认端点
        model:         默认模型
        known_models:  可选模型（供 UI 下拉）
        api_key_setting: 从哪个设置项取 key
        build_body:    构造请求体
        conditioning:  支持的条件维度
        durations:     时长范围（含端点）
        has_native_audio: **产出的视频里带不带声音**。
                        ⚠️ 与 `conditioning` 里的 `reference_audio` **不是一回事**：
                        那个说的是"能不能**喂**一段音频当参考"（输入），
                        这个是"产出里**有没有**声音"（输出）。混用会让界面
                        给出错误选项 —— 用户问过"配音不是某些视频模型自带吗"。
        note:          展示给用户的说明
        verified:      **是否用真实 key 跑通过**。
                       False 表示"按公开文档实现、行为可能需按实际调整"——
                       诚实标出来，避免让人以为它已验证。
    """

    key: str
    label: str
    category: str
    base_url: str
    model: str
    known_models: tuple[str, ...]
    api_key_setting: str
    build_body: Callable[[str, VideoRequest, dict], dict]
    conditioning: tuple[str, ...]
    #: 产出的视频里**有没有声音**（见类文档里的说明：与 reference_audio 不同）
    has_native_audio: bool = False
    durations: tuple[int, int] = (2, 15)
    note: str = ""
    verified: bool = False
    #: 提交用哪个端点（多数是 base_url + 这个路径）
    submit_path: str = ""
    #: 查询用哪个端点模板（`{task}` 会被替换）
    query_path: str = ""
    #: 设置页里"自定义端点"对应的设置键。
    #:
    #: 默认按 `{key}_base_url` 推导，但**不能一概而论**：
    #: Seedance 的配置键叫 `ark_base_url`（火山方舟自己的名字），
    #: 与提供者 key `seedance` 不同名。硬按 key 推导会得到一个
    #: 不在写白名单里的键——设置页能填、保存却被静默丢弃。
    #: 这个字段就是用来消除这类命名漂移的（有测试守着）。
    endpoint_setting: str = ""


VIDEO_PROVIDERS: tuple[VideoProvider, ...] = (
    VideoProvider(
        key="minimax",
        label="MiniMax（Hailuo）",
        category="cn_cloud",
        base_url="https://api.minimax.io",
        model="MiniMax-H3",
        known_models=("MiniMax-H3", "MiniMax-H3-Max"),
        api_key_setting="minimax_api_key",
        build_body=_build_minimax_body,
        conditioning=("text", "first_frame", "last_frame",
                      "reference_image", "reference_video", "reference_audio"),
        # ⚠️ 我们**只确认了它能收参考音频**（输入），
        #    没有确认产出里是否自带配乐/对白 —— 官方文档没写清。
        #    所以标 False（不宣称有），避免界面给出"用视频模型原生音频"
        #    却发现产物是无声的。要改这个必须先去官方文档核实。
        has_native_audio=False,
        durations=(4, 15),
        note="H3: 768P/2K，4~15s；H3-Max 更快但无 2K、5~15s。"
             "**支持参考视频**（白模录像走这里）。首尾帧与参考视频互斥。"
             "（端点与模型名依官方 v2 文档实现，尚未用真实 key 跑通）",
        verified=False,
        # 注意是 v2，不是 v1：`/v2/video_generation` 的 content 数组才支持
        # video_url/audio_url 与 role 分派；v1 只有 first_frame_image 那些扁平字段。
        submit_path="/v2/video_generation",
        query_path="/v2/query/video_generation/{task}",
        endpoint_setting="minimax_base_url",
    ),
    VideoProvider(
        key="dashscope_video",
        label="阿里百炼（万相 / Vidu）",
        category="cn_cloud",
        base_url="https://dashscope.aliyuncs.com",
        model="wan3.0-video",
        # 模型名按官方模型列表与 API 参考核对（2026-10）：
        #   https://www.alibabacloud.com/help/en/model-studio/model-list-video-generation
        #   https://www.alibabacloud.com/help/en/model-studio/wan3-0-video
        # ⚠️ 只有在 `_DASHSCOPE_MODELS` 里登记过的才有精确的请求形状；
        #    未登记的会按 auto 约定构造（有 warning），所以这里可以多列，
        #    但请求体形状可能不完全对——这是"允许填"和"保证能跑"的区别。
        known_models=(
            "wan3.0-video", "wan3.0-video-prime",
            "wan2.7-t2v", "wan2.6-t2v",
            "wan2.6-i2v", "wan2.6-i2v-flash",
            "wan2.6-r2v", "wan2.2-kf2v-flash",
            "vidu/viduq3-turbo_start-end2video",
            "vidu/viduq3-pro_start-end2video",
        ),
        api_key_setting="dashscope_api_key",
        build_body=_build_dashscope_body,
        conditioning=("text", "first_frame", "last_frame",
                      "reference_image", "reference_video", "reference_audio"),
        # 官方备注明确写了 wan3.0「30fps、**原生带音频**」——这是产出带声音，
        # 所以置 True。（用户："配音不是某些视频模型自带吗"）
        has_native_audio=True,
        durations=(2, 30),
        note="**一个 key 覆盖多家模型**。wan3.0 时长 2~30s、30fps、原生带音频；"
             "参考视频总长 ≤15s。vidu/*_start-end2video 只收首尾帧两张图。"
             "首尾帧与参考素材互斥。"
             "（按公开文档实现，尚未用真实 key 跑通）",
        verified=False,
        submit_path="/api/v1/services/aigc/video-generation/video-synthesis",
        query_path="/api/v1/tasks/{task}",
        endpoint_setting="dashscope_video_base_url",
    ),
    VideoProvider(
        key="seedance",
        label="Seedance（火山方舟）",
        category="cn_cloud",
        base_url="https://ark.cn-beijing.volces.com",
        # 模型名按方舟官方文档核对（2026-10）：
        #   https://docs.volcengine.com/docs/ark/create-video-generation-task-api?lang=zh
        #   请求示例即用 `doubao-seedance-2-5-260628`；1.0 系列属上一代
        #   （仍出现在官方文档中，未见下线公告）。
        model="doubao-seedance-2-5-260628",
        known_models=(
            "doubao-seedance-2-5-260628",
            "doubao-seedance-2-0-260128",
            "doubao-seedance-2-0-fast-260128",
            "doubao-seedance-2-0-mini-260615",
            "doubao-seedance-1-5-pro-251215",
            "doubao-seedance-1-0-pro-250528",
        ),
        api_key_setting="ark_api_key",
        build_body=_build_seedance_body,
        conditioning=("text", "first_frame", "last_frame", "reference_image"),
        durations=(3, 12),
        note="端点与模型名请按方舟控制台核对（可覆盖 ark_base_url）；"
             "参考视频支持依具体模型而不同。"
             "（按公开文档实现，尚未用真实 key 跑通）",
        verified=False,
        submit_path="/api/v3/contents/generations/tasks",
        query_path="/api/v3/contents/generations/tasks/{task}",
        # 注意：方舟的配置键是 `ark_base_url`，不是 `seedance_base_url`
        endpoint_setting="ark_base_url",
    ),
)


def get_video_provider(key: str) -> VideoProvider | None:
    for p in VIDEO_PROVIDERS:
        if p.key == key:
            return p
    return None


# ------------------------------------------------------------------
# 提供者凭据需求（供设置页渲染输入框）
# ------------------------------------------------------------------
# 为什么要在这里声明、而不是让前端硬编码：
#   视频的密钥是**提供者级**的（`minimax_api_key` 等），与 LLM/图像的
#   `*_api_key_{角色}` 命名不同。前端如果按前缀猜，就会像以前那样
#   ——白名单里有、界面却没输入框，用户根本没法配。
#   所以由后端导出"这个提供者需要填什么"，前端照着渲染。

@dataclass(frozen=True)
class CredentialField:
    """一个需要用户填写的凭据/端点字段。"""

    key: str
    label: str
    kind: str          # "secret" | "text"
    hint: str = ""


def video_credential_fields() -> list[dict]:
    """返回所有视频提供者需要的凭据字段（去重）。

    前端用它渲染"视频模型凭据"区域，键名与 PUT /api/settings 完全一致。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for p in VIDEO_PROVIDERS:
        # 本地/免费提供者不需要 key（api_key_setting 可能为空）
        if not p.api_key_setting or p.api_key_setting in seen:
            continue
        seen.add(p.api_key_setting)
        out.append({
            "key": p.api_key_setting,
            # 标签就叫「API Key」——**不加提供者前缀**（用户明确要求）。
            # 上面「视频来源」已经选了是哪一家，这里再重复一遍提供者名
            # 纯属噪音；而且前端只显示**当前那一家**的 Key，
            # 根本不存在"分不清是哪个"的问题。
            "label": "API Key",
            "kind": "secret",
            "provider": p.key,
            "hint": p.note,
            "models": list(p.known_models),
            "verified": p.verified,
        })
    return out


def video_endpoint_fields() -> list[dict]:
    """可覆盖的端点字段。

    键名取 `endpoint_setting`，**不是**按提供者 key 推导——
    两者可能不同名（Seedance 的配置键叫 `ark_base_url`）。
    推导错了会导致"设置页能填、保存却被静默丢弃"。
    """
    out: list[dict] = []
    for p in VIDEO_PROVIDERS:
        field = p.endpoint_setting or f"{p.key}_base_url"
        out.append({
            "key": field,
            "label": "API 端点",
            "kind": "text",
            "provider": p.key,
            "hint": f"留空用默认：{p.base_url}",
        })
    return out


def provider_credential_spec() -> dict:
    """给设置页用的完整凭据说明（按能力分组）。"""
    return {
        "video": {
            "keys": video_credential_fields(),
            "endpoints": video_endpoint_fields(),
        }
    }


# ------------------------------------------------------------------
# 通用提交 + 轮询
# ------------------------------------------------------------------

def submit_and_wait(
    provider: VideoProvider,
    api_key: str,
    req: VideoRequest,
    *,
    timeout: int = 900,
    interval: int = 10,
    on_task: Callable[[str], None] | None = None,
) -> str:
    """提交任务并轮询到成功，返回视频下载 URL。

    异步任务模型（PENDING → RUNNING → SUCCEEDED/FAILED）是这几家的共同形状，
    差别只在**字段取值**（`output.task_id` vs 顶层 `task_id` 等），
    所以轮询逻辑共用、只在取字段处做兼容。
    """
    validate_exclusive(req)

    # 用调用方选定的模型，而不是适配器的默认模型。
    #
    # ⚠️ 这里原先写的是 `provider.build_body(provider.model, ...)` ——
    # 于是用户在设置页改的 `video_model` **完全被忽略**：
    # `video.py::_via_adapter` 算出了 model 却从不使用（死变量），
    # 而注册表声明了 `video_model`、白名单也放行它，
    # 结果是"设置页能改、PUT 返回 200、提示已保存，实际恒用默认模型"。
    # 现在优先取 `req.extra["model"]`（调用方传入），回退到适配器默认。
    model = str(req.extra.get("model") or provider.model).strip() or provider.model
    body = provider.build_body(model, req, req.extra)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    # DashScope 必须显式声明异步，否则报 "does not support synchronous calls"
    if provider.key == "dashscope_video":
        headers["X-DashScope-Async"] = "enable"

    url = provider.base_url.rstrip("/") + provider.submit_path
    from app.services.url_guard import assert_public_url

    assert_public_url(url, context=f"{provider.key}_base_url")

    with httpx.Client(timeout=60) as client:
        resp = client.post(url, headers=headers, json=body)
        if resp.status_code >= 400:
            raise VideoAdapterError(
                f"{provider.label} 提交失败({resp.status_code})：{resp.text[:400]}"
            )
        data = resp.json()
        task_id = (
            (data.get("output") or {}).get("task_id")
            or data.get("task_id")
            or data.get("id")
        )
        if not task_id:
            raise VideoAdapterError(
                f"{provider.label} 未返回任务 id：{str(data)[:400]}"
            )
        if on_task:
            on_task(str(task_id))
        logger.info("[video] %s 任务已提交：%s", provider.label, task_id)

        query_url = provider.base_url.rstrip("/") + provider.query_path.replace(
            "{task}", str(task_id)
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(interval)
            q = client.get(query_url, headers=headers)
            if q.status_code >= 400:
                raise VideoAdapterError(
                    f"{provider.label} 查询失败({q.status_code})：{q.text[:300]}"
                )
            qd = q.json()
            out = qd.get("output") or qd
            status = str(out.get("task_status") or out.get("status") or "").upper()
            if status in ("SUCCEEDED", "SUCCESS", "SUCCEED"):
                video_url = (
                    out.get("video_url")
                    or ((out.get("content") or {}) if isinstance(out.get("content"), dict) else {}).get("url")
                    or (out.get("result") or {}).get("video_url")
                )
                if not video_url:
                    raise VideoAdapterError(
                        f"{provider.label} 任务成功但未返回视频地址：{str(qd)[:400]}"
                    )
                return str(video_url)
            if status in ("FAILED", "CANCELED", "CANCELLED"):
                msg = out.get("message") or out.get("code") or str(qd)[:300]
                raise VideoAdapterError(f"{provider.label} 任务失败：{msg}")
            # PENDING / RUNNING → 继续等

    raise VideoAdapterError(
        f"{provider.label} 任务超时（{timeout}s 未完成）。"
        "视频生成通常 1~5 分钟；若持续超时请检查网络或改用更快档位。"
    )
