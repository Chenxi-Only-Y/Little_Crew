"""视频合成服务 —— 图片 + 台词(可选 TTS) → MP4（FFmpeg）。

- mock（默认）：免依赖，落一个 manifest.json 占位，返回目录（produced=False）。
- real：用 FFmpeg 把分镜图做成幻灯片（含转场），若提供台词则用 Edge TTS 生成配音混流。

注意：真实合成需要系统安装 FFmpeg，且用 `edge-tts` 做免费配音。
"""

import json
import logging
import pathlib
import shutil
import subprocess
from typing import List, Tuple

from app.core.config import settings

logger = logging.getLogger("little_crew")


def _extract_dialogue(script: dict) -> str:
    """从剧本提取台词文本，用于配音。"""
    lines = []
    for scene in script.get("scenes", []):
        for d in scene.get("dialogue", []):
            lines.append(d.get("line", ""))
    return "".join(lines)


def _video_dir(out_dir: str, session_id: str) -> pathlib.Path:
    """按「账号(UID) / 部门 / 职业 / 会话」归类视频输出目录。"""
    from app.services.runtime_settings import user as cur_user
    from app.services.auth import get_user_store
    from app.core.org import dept_for_role, label_for_role

    user = str(cur_user() or "local")
    try:
        user = get_user_store().resolve_uid(user) or user
    except Exception:  # noqa: BLE001
        pass
    dept = dept_for_role("editor")
    role = label_for_role("editor")
    d = pathlib.Path(out_dir) / user / dept / role / session_id / "video"
    d.mkdir(parents=True, exist_ok=True)
    return d


class VideoService:
    # ---- mock 开关：每次实时解析，不做快照（原先 __init__ 里快照导致改配置不生效）----
    @property
    def mock(self) -> bool:
        from app.services.providers import is_mock

        return is_mock("video")

    @mock.setter
    def mock(self, value: bool) -> None:
        from app.services.providers import set_override

        set_override("video", bool(value))

    def synthesize(
        self,
        images: List[dict],
        script: dict,
        out_dir: str,
        session_id: str,
        video_count: int | None = None,
        text_prompt: str = "",
        reference_video: str = "",
    ) -> Tuple[str, int]:
        """生成视频。

        Args:
            video_count: 可选，指定生成几个视频段（0/None=跟图片数走；>0 截取前 N 个）。
            text_prompt: 无图片时（纯文生视频）用作提示词——通常是用户的一句话创意。
                         有图片时忽略（画面已由首帧决定，只需描述怎么动）。
            reference_video: **参考视频**（图片/视频 URL，或 `mm_file://{file_id}`）。
                         走"参考驱动"而非"首尾帧"——白模录像就是从这里进去的。
                         注意**首尾帧与参考视频互斥**（官方限制），
                         所以给了它就不再发首帧。

        Returns:
            (output_path_or_dir, produced) —— produced=1 表示真实产物，0 表示占位。
        """
        dialogue = _extract_dialogue(script)
        if self.mock:
            return self._mock(images, dialogue, out_dir, session_id)
        # 提供者解析：不静默默认；未选择时明确报错（见 providers.resolve_provider）
        from app.services.providers import resolve_provider

        provider, why = resolve_provider("video_gen")
        if not provider:
            raise RuntimeError(why)
        provider = provider.lower()
        if provider == "minimax":
            return self._real_minimax(images, dialogue, out_dir, session_id,
                                      video_count=video_count,
                                      reference_video=reference_video)
        if provider == "dashscope_video":
            return self._real_dashscope(images, dialogue, out_dir, session_id,
                                        video_count=video_count,
                                        text_prompt=text_prompt,
                                        reference_video=reference_video)
        if provider == "seedance":
            return self._real_seedance(images, dialogue, out_dir, session_id,
                                       video_count=video_count,
                                       text_prompt=text_prompt,
                                       reference_video=reference_video)
        if provider == "local":
            return self._real_local(images, dialogue, out_dir, session_id,
                                    video_count=video_count, text_prompt=text_prompt)
        return self._real(images, dialogue, out_dir, session_id)

    def _mock(self, images: List[dict], dialogue: str, out_dir: str, session_id: str) -> Tuple[str, int]:
        """占位：写 manifest，返回目录（不产出真视频）。"""
        d = pathlib.Path(out_dir) / session_id / "video"
        d.mkdir(parents=True, exist_ok=True)
        manifest = {
            "session_id": session_id,
            "images": [im.get("local_path") for im in images],
            "dialogue": dialogue,
            "note": f"MOCK_VIDEO={int(self.mock)}：真实视频需安装 FFmpeg 并设 MOCK_VIDEO=0。",
        }
        path = d / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(d), 0

    def _real(self, images: List[dict], dialogue: str, out_dir: str, session_id: str) -> Tuple[str, int]:
        """真实合成：FFmpeg 幻灯片 + Edge TTS 配音。"""
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("未安装 FFmpeg，无法合成视频。请安装 FFmpeg 并设 MOCK_VIDEO=0。")

        img_paths = [im.get("local_path") for im in images if im.get("local_path")]
        if not img_paths:
            raise RuntimeError("没有可用图片，无法合成视频。")
        d = pathlib.Path(out_dir) / session_id / "video"
        d.mkdir(parents=True, exist_ok=True)
        out_mp4 = d / "out.mp4"

        # 1) 图片幻灯片（每镜固定时长，含淡入淡出）。TODO: 按镜头 duration_s 精确控制。
        concat = d / "list.txt"

        def _ffconcat_escape(p):  # FFmpeg concat 转义单引号/反斜杠/换行
            return str(p).replace("\\", "\\\\").replace("'", "'\\''").replace("\n", "")

        with open(concat, "w", encoding="utf-8") as f:
            for p in img_paths:
                f.write(f"file '{_ffconcat_escape(p)}'\nduration 4\n")
            # 重复最后一张避免 FFmpeg 末尾截断
            f.write(f"file '{_ffconcat_escape(img_paths[-1])}'\n")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                 "-vf", "fade=t=in:st=0:d=0.4,fade=t=out:st=3.6:d=0.4", "-pix_fmt", "yuv420p", str(out_mp4)],
                check=True, capture_output=True, timeout=600,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"FFmpeg 视频合成超时（>600s）：{exc}")

        # 2) 可选 TTS 配音（Edge TTS 免费）。TODO: 需安装 edge-tts 并在此合成音频、mux 进视频。
        if dialogue:
            # 预留：edge-tts 生成 audio.mp3 后 `ffmpeg -i out.mp4 -i audio.mp3 -shortest out_final.mp4`
            pass  # 见 TODO，避免在未装 edge-tts 时报错

        return str(out_mp4), 1

    # ---------------------------------------------------------------
    # MiniMax-H3 图生视频（每段 ≤ video_max_duration 秒、≤ video_resolution 分辨率）
    # ---------------------------------------------------------------
    def _real_minimax(self, images: List[dict], dialogue: str, out_dir: str, session_id: str,
                      video_count: int | None = None, text_prompt: str = "",
                      reference_video: str = "") -> Tuple[str, int]:
        """MiniMax（Hailuo）：统一交给适配器。

        为什么删掉原先的独立实现：
            这里曾自己拼 `/v2/video_generation` 的 body、自己轮询——
            与 `video_adapters` 里的 minimax 重复。两处一旦漂移，就会出现
            "界面按适配器声明做校验、实际按旧代码发送"这类极难排查的问题
            （例如尾帧/参考视频的 role 只在一边支持）。
            现在只保留适配器一条路。
        """
        return self._via_adapter("minimax", images, dialogue, out_dir, session_id,
                                 video_count=video_count, text_prompt=text_prompt,
                                 reference_video=reference_video)

    def _real_dashscope(self, images: List[dict], dialogue: str, out_dir: str, session_id: str,
                        video_count: int | None = None, text_prompt: str = "",
                        reference_video: str = "") -> Tuple[str, int]:
        """走阿里百炼的视频端点（万相 / Vidu 等，一个 key 覆盖多家）。

        统一交给 `video_adapters` 处理：那里负责把统一请求翻成该端点的形状
        （万相用显式 media.type，Vidu 要求 media 正好两张 image 且顺序即首尾帧）。
        """
        return self._via_adapter("dashscope_video", images, dialogue, out_dir, session_id,
                                 video_count=video_count, text_prompt=text_prompt,
                                 reference_video=reference_video)

    def _real_seedance(self, images: List[dict], dialogue: str, out_dir: str, session_id: str,
                       video_count: int | None = None, text_prompt: str = "",
                       reference_video: str = "") -> Tuple[str, int]:
        """走火山方舟（Seedance）。

        ⚠️ 端点与模型名按方舟公开文档整理，**尚未用真实 key 跑通**；
        首次使用请按返回的提示核对 `ark_base_url` 与 `video_model`。
        """
        return self._via_adapter("seedance", images, dialogue, out_dir, session_id,
                                 video_count=video_count, text_prompt=text_prompt,
                                 reference_video=reference_video)

    def _via_adapter(self, provider_key: str, images: List[dict], dialogue: str,
                     out_dir: str, session_id: str, video_count: int | None = None,
                     text_prompt: str = "", reference_video: str = "") -> Tuple[str, int]:
        """经适配器生成视频（云端通用路径）。

        `reference_video` 非空时走**参考驱动**（白模录像就是从这里进去）；
        否则按有无首帧走图生视频/纯文生视频。
        """
        import pathlib

        from app.core.config import settings
        from app.services import runtime_settings as rs
        from app.services.video_adapters import (
            VideoAdapterError, VideoRequest, get_video_provider, submit_and_wait,
        )

        prov = get_video_provider(provider_key)
        if prov is None:
            raise RuntimeError(f"未登记的视频提供者：{provider_key}")

        api_key = str(rs.get(prov.api_key_setting) or getattr(
            settings, prov.api_key_setting, "") or "").strip()
        if not api_key:
            raise RuntimeError(
                f"{prov.label} 需要 API Key（设置项：{prov.api_key_setting}）。"
                "请到「设置」页填写。"
            )

        model = str(rs.get("video_model") or settings.video_model or prov.model).strip()
        # 端点解析：全局覆盖 → 该提供者的端点设置 → 提供者默认。
        # 用 `endpoint_setting` 而不是按 `{provider_key}_base_url` 拼——
        # 两者可能不同名（Seedance 的键是 `ark_base_url`）。
        endpoint_key = prov.endpoint_setting or f"{provider_key}_base_url"
        base_url = str(
            rs.get("video_base_url")
            or settings.video_base_url
            or rs.get(endpoint_key)
            or getattr(settings, endpoint_key, "")
            or ""
        ).strip() or prov.base_url

        cap = int(video_count if video_count is not None else (settings.video_count or 0))
        items = [im for im in images if (im.get("local_path") or im.get("image_url"))]
        if cap > 0 and items:
            items = items[:cap]

        # 时长：按提供者声明的范围收敛（各家不同，例如 Vidu 1~16、万相 2~30）
        want = int(settings.video_max_duration or 5)
        lo, hi = prov.durations
        duration = max(lo, min(hi, want))
        if duration != want:
            logger.warning("[video] %s 时长 %ds 超出 %d~%d，收敛为 %ds",
                           prov.label, want, lo, hi, duration)

        req = VideoRequest(
            prompt=str(text_prompt or dialogue or "").strip(),
            duration=duration,
            resolution=str(settings.video_resolution or "720P"),
            ratio="adaptive",
            reference_videos=[reference_video] if reference_video else [],
        )
        if items and not reference_video:
            # 有图且没走参考驱动 → 图生视频（首帧）
            req.first_frame = self._image_ref(items[0])
            last = self._image_ref(items[0], url_key="last_image", path_key="last_local_path")
            if last:
                req.last_frame = last

        # 用适配器自己的端点（可能在 base_url 之外，例如方舟）
        from dataclasses import replace

        prov = replace(prov, base_url=base_url)

        d = pathlib.Path(out_dir) / session_id / "video"
        d.mkdir(parents=True, exist_ok=True)
        try:
            dl_url = submit_and_wait(prov, api_key, req, timeout=1200, interval=10)
        except VideoAdapterError as exc:
            raise RuntimeError(str(exc)) from exc

        out_mp4 = d / "out.mp4"
        with httpx.Client(timeout=300) as client:
            vr = client.get(dl_url)
            vr.raise_for_status()
        out_mp4.write_bytes(vr.content)
        return str(d), 1

    # ---------------------------------------------------------------
    # 本地 MiniMax H3 图生视频（D:\AI\local-gen 服务，零 API 成本）
    # ---------------------------------------------------------------
    # 本地服务是「提交 -> 返回 job_id -> 轮询」的异步模型，因为 Disk Offload 下
    # 每段视频可能跑几十分钟，不能让 HTTP 连接一直挂着。
    _LOCAL_MOTION_PROMPT = "镜头缓慢推近，画面中的角色/景物做轻微自然运动，电影感光影，稳定运镜"

    def _real_local(self, images: List[dict], dialogue: str, out_dir: str, session_id: str,
                    video_count: int | None = None, text_prompt: str = "") -> Tuple[str, int]:
        """调用本地 MiniMax H3 生成视频：每张分镜图生成一段。

        条件能力（见 providers 里 local 的 conditioning 声明）：
            - `image_base64`      → 首帧（起点构图）
            - `last_image_base64` → 尾帧（终点布局；白模预演的产物送这里）
        只给首帧时运动过程由模型自行插值；给了尾帧则"首起尾收"，落点被钉死。

        无图片时走**纯文生视频**（H3 不传 keyframe），提示词取 `text_prompt`。
        """
        import pathlib
        import time as _t
        import uuid as _uuid

        import httpx

        # 注意：本地服务在 127.0.0.1，属于私网地址，因此这里有意不做 url_guard 公网校验
        base = (settings.local_gen_url or "http://127.0.0.1:8100").rstrip("/")

        # 两种模式（H3 的 image_base64 是可选参数，不传即文生视频）：
        #   - 有图：每张图生成一段（图生视频）
        #   - 无图：生成 cap 段纯文生视频（cap<=0 时默认 1 段）
        # 原先"无图直接报错"把本可支持的"纯文生视频"路线挡在了外面。
        img_items = [im for im in images if (im.get("local_path") or im.get("image_url"))]
        cap = int(video_count if video_count is not None else (settings.video_count or 0))

        if img_items:
            if cap > 0:
                img_items = img_items[:cap]
        else:
            segments = cap if cap > 0 else 1
            logger.info("[video] 无图片输入 → 走纯文生视频（不带 keyframe），段数=%d", segments)
            img_items = [{} for _ in range(segments)]

        d = pathlib.Path(out_dir) / session_id / "video"
        d.mkdir(parents=True, exist_ok=True)
        produced = 0
        steps = int(settings.local_video_steps or 50)
        # 本地安全上限 10s：这里显式收敛（此前是 max(4, min(15, ...))，会把 12/15 直接发出去 → 必 OOM）
        from app.services.providers import resolve_duration

        seconds, dur_note = resolve_duration("local", settings.video_max_duration)
        if dur_note:
            logger.warning("[video] %s", dur_note)
        timeout = int(settings.local_gen_timeout or 7200)

        for im in img_items:
            sid = str(im.get("shot_id") or _uuid.uuid4().hex[:8])
            img_ref = self._image_ref(im)
            # `im` 非空却没解析出图引用 = 图片路径损坏，跳过该段（避免发出空首帧）。
            # 但 `im` 为空 dict 表示**纯文生视频**，此时本就没有首帧，不能跳过。
            if not img_ref and im:
                logger.warning("[video] 分镜 %s 的图片无法读取，跳过该段", sid)
                continue

            width, height = self._local_size(im)
            # 有首帧时用"运动描述"（画面已定，只需描述怎么动）；
            # 无首帧的纯文生视频必须描述**画面内容**，优先用用户创意（text_prompt）。
            if img_ref:
                prompt_text = str(im.get("prompt") or "").strip() or self._LOCAL_MOTION_PROMPT
            else:
                prompt_text = (
                    str(im.get("prompt") or "").strip()
                    or str(text_prompt or "").strip()
                    or str(dialogue or "").strip()
                    or self._LOCAL_MOTION_PROMPT
                )

            body = {
                "prompt": prompt_text,
                "width": width,
                "height": height,
                "seconds": seconds,
                "num_inference_steps": steps,
            }
            # 首帧：仅在解析出图引用时发送（H3 的 image_base64 是可选参数）
            if img_ref:
                body["image_base64"] = img_ref
            # 尾帧（终点布局）：H3 的 keyframes 传 2 张时会放到 index [0, -1]，
            # 即"首帧起、尾帧收"，中间由模型插值。
            # 这是当前**唯一**能把运动终点钉死的通道——白模（预演）给出的
            # 终点布局图就送到这里（见 providers.previs_plan）。
            # 未提供则不发送该字段，保持原行为（纯首帧引导）。
            last_ref = self._image_ref(
                im, url_key="last_image", path_key="last_local_path"
            )
            if last_ref:
                body["last_image_base64"] = last_ref
            with httpx.Client(timeout=httpx.Timeout(120, connect=10)) as client:
                resp = client.post(f"{base}/v1/videos/generations", json=body)
                if resp.status_code >= 400:
                    raise RuntimeError(f"本地图生视频提交失败({resp.status_code}): {resp.text[:300]}")
                job_id = str(resp.json().get("job_id") or "")
                if not job_id:
                    raise RuntimeError(f"本地服务未返回 job_id：{resp.text[:300]}")

            dl_url = self._poll_local(base, job_id, timeout=timeout, interval=10)
            if dl_url:
                with httpx.Client(timeout=300) as client:
                    vr = client.get(dl_url)
                    vr.raise_for_status()
                out_mp4 = d / f"{sid}.mp4"
                out_mp4.write_bytes(vr.content)
                produced = 1
        return str(d), produced

    @staticmethod
    def _local_size(im: dict, short_side: int = 480, max_long_side: int = 864) -> Tuple[int, int]:
        """按分镜图比例算一个 H3 能吃的分辨率（32 的倍数，控制在低显存范围内）。

        MiniMax H3 要求宽高均为 32 的倍数；8GB 显存 + Disk Offload 下面积不能大，
        官方低配置示例用的是 480x832 / 832x480 这一档。
        """
        w = int(im.get("width") or 0)
        h = int(im.get("height") or 0)

        def _align(v: float) -> int:
            return max(32, int(round(v / 32.0)) * 32)

        if w <= 0 or h <= 0:
            return 832, 480
        if w >= h:
            out_h = short_side
            out_w = min(max_long_side, short_side * (w / h))
        else:
            out_w = short_side
            out_h = min(max_long_side, short_side * (h / w))
        return _align(out_w), _align(out_h)

    def _poll_local(self, base: str, job_id: str, timeout: int = 7200, interval: int = 10) -> str:
        """轮询本地服务的视频任务，返回可下载的视频 URL。"""
        import time as _t

        import httpx

        deadline = _t.time() + timeout
        while _t.time() < deadline:
            with httpx.Client(timeout=60) as client:
                resp = client.get(f"{base}/v1/videos/generations/{job_id}")
            resp.raise_for_status()
            data = resp.json()
            status = str(data.get("status") or "")
            if status == "succeeded":
                return str((data.get("result") or {}).get("url") or "")
            if status == "failed":
                raise RuntimeError(f"本地图生视频失败：{data.get('error')}")
            _t.sleep(interval)
        raise TimeoutError(f"本地图生视频超时（>{timeout}s）：{job_id}")

    def _image_ref(self, im: dict, url_key: str = "image_url",
                   path_key: str = "local_path") -> str:
        """返回可直接提交给视频服务的图片引用（本地图转 data URL / 远端 URL）。

        Args:
            url_key / path_key: 取哪个字段。默认取首帧（`image_url`/`local_path`）；
                传 `last_image` / `last_local_path` 即取尾帧（终点布局）。
        """
        import pathlib
        import base64

        url = im.get(url_key) or ""
        if url and str(url).startswith(("http://", "https://")):
            return url
        path = im.get(path_key) or ""
        if path:
            p = pathlib.Path(path)
            if p.exists():
                mime = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
                data = base64.b64encode(p.read_bytes()).decode()
                return f"data:{mime};base64,{data}"
        return ""

    @staticmethod
    def _ratio(im: dict) -> str:
        """根据图片宽高推断 MiniMax 视频 ratio（adaptive/16:9/4:3/1:1/3:4/9:16/21:9）。"""
        w = im.get("width") or 0
        h = im.get("height") or 0
        if w and h:
            r = w / h
            if r >= 1.7:
                return "16:9"
            if r >= 1.2:
                return "4:3"
            if r >= 0.8:
                return "1:1"
            if r >= 0.5:
                return "3:4"
            return "9:16"
        return "adaptive"


_service: VideoService | None = None


def get_video_service() -> VideoService:
    """获取（单例）视频服务。"""
    global _service
    if _service is None:
        _service = VideoService()
    return _service
