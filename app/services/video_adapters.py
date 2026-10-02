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
    durations: tuple[int, int] = (2, 15)
    note: str = ""
    verified: bool = False
    #: 提交用哪个端点（多数是 base_url + 这个路径）
    submit_path: str = ""
    #: 查询用哪个端点模板（`{task}` 会被替换）
    query_path: str = ""


VIDEO_PROVIDERS: tuple[VideoProvider, ...] = (
    VideoProvider(
        key="dashscope_video",
        label="阿里百炼（万相 / Vidu）",
        category="cn_cloud",
        base_url="https://dashscope.aliyuncs.com",
        model="wan3.0-video",
        known_models=(
            "wan3.0-video", "wan3.0-video-prime",
            "wan2.2-kf2v-flash",
            "vidu/viduq3-turbo_start-end2video",
            "vidu/viduq3-pro_start-end2video",
        ),
        api_key_setting="dashscope_api_key",
        build_body=_build_dashscope_body,
        conditioning=("text", "first_frame", "last_frame",
                      "reference_image", "reference_video", "reference_audio"),
        durations=(2, 30),
        note="**一个 key 覆盖多家模型**。wan3.0 时长 2~30s、30fps、原生带音频；"
             "参考视频总长 ≤15s。vidu/*_start-end2video 只收首尾帧两张图。"
             "首尾帧与参考素材互斥。"
             "（按公开文档实现，尚未用真实 key 跑通）",
        verified=False,
        submit_path="/api/v1/services/aigc/video-generation/video-synthesis",
        query_path="/api/v1/tasks/{task}",
    ),
    VideoProvider(
        key="seedance",
        label="Seedance（火山方舟）",
        category="cn_cloud",
        base_url="https://ark.cn-beijing.volces.com",
        model="doubao-seedance-1-0-pro",
        known_models=("doubao-seedance-1-0-pro", "doubao-seedance-1-0-lite-i2v"),
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
    ),
)


def get_video_provider(key: str) -> VideoProvider | None:
    for p in VIDEO_PROVIDERS:
        if p.key == key:
            return p
    return None


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

    body = provider.build_body(provider.model, req, req.extra)
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
