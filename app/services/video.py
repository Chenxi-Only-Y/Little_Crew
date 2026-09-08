"""视频合成服务 —— 图片 + 台词(可选 TTS) → MP4（FFmpeg）。

- mock（默认）：免依赖，落一个 manifest.json 占位，返回目录（produced=False）。
- real：用 FFmpeg 把分镜图做成幻灯片（含转场），若提供台词则用 Edge TTS 生成配音混流。

注意：真实合成需要系统安装 FFmpeg，且用 `edge-tts` 做免费配音。
"""

import json
import pathlib
import shutil
import subprocess
from typing import List, Tuple

from app.core.config import settings


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
    def __init__(self) -> None:
        self.mock = settings.mock_video

    def synthesize(
        self,
        images: List[dict],
        script: dict,
        out_dir: str,
        session_id: str,
        video_count: int | None = None,
    ) -> Tuple[str, int]:
        """生成视频。

        video_count: 可选，指定生成几个视频段（0/None=跟图片数走；>0 截取前 N 个）。

        Returns:
            (output_path_or_dir, produced) —— produced=1 表示真实产物，0 表示占位。
        """
        dialogue = _extract_dialogue(script)
        if self.mock:
            return self._mock(images, dialogue, out_dir, session_id)
        if (settings.video_provider or "ffmpeg").lower() == "minimax":
            return self._real_minimax(images, dialogue, out_dir, session_id, video_count=video_count)
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
                      video_count: int | None = None) -> Tuple[str, int]:
        """调用 MiniMax-H3 图生视频：每张分镜图生成一段视频（I2V）。

        视频参数：resolution=768P（最高 768P），duration=min(请求, video_max_duration)。
        输出目录 structure: <out_dir>/<session_id>/video/<shot_id>.mp4（每段一个文件）。
        """
        import time as _t
        import uuid as _uuid
        import base64
        import pathlib

        key = settings.minimax_api_key or settings.video_api_key
        if not key:
            raise RuntimeError("MOCK_VIDEO=0 且 VIDEO_PROVIDER=minimax 但未配置 MINIMAX_API_KEY")
        base = (settings.video_base_url or settings.minimax_base_url or "https://api.minimax.io").rstrip("/")
        from app.services.url_guard import assert_public_url
        assert_public_url(base, context="video_base_url")
        model = settings.video_model or "MiniMax-H3"
        # 视频质量：768P / 1080P / 2K（用户可选）
        resolution = str(settings.video_quality or "768P").upper()
        if resolution not in ("768P", "1080P", "2K"):
            resolution = "768P"
        duration = max(1, min(int(settings.video_max_duration or 15), 15))

        import httpx

        img_items = [im for im in images if (im.get("local_path") or im.get("image_url"))]
        if not img_items:
            raise RuntimeError("没有可用图片，无法调用 MiniMax 图生视频。")
        # 视频数量：优先用传入的 video_count；否则用 settings.video_count（0=跟图片数走）
        cap = int(video_count if video_count is not None else (settings.video_count or 0))
        if cap > 0:
            img_items = img_items[:cap]

        d = pathlib.Path(out_dir) / session_id / "video"
        d.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        produced = 0
        for im in img_items:
            sid = str(im.get("shot_id") or _uuid.uuid4().hex[:8])
            img_ref = self._image_ref(im)
            if not img_ref:
                continue
            motion = "镜头缓慢推近，画面中的角色/景物做轻微自然运动，电影感光影，稳定运镜"
            body = {
                "model": model,
                "content": [
                    {"type": "text", "text": motion},
                    {"type": "image_url", "image_url": {"url": img_ref}, "role": "first_frame"},
                ],
                "resolution": resolution,
                "duration": duration,
                "ratio": self._ratio(im),
            }
            with httpx.Client(timeout=60) as client:
                resp = client.post(f"{base}/v2/video_generation", headers=headers, json=body)
                if resp.status_code >= 400:
                    raise RuntimeError(f"MiniMax 图生视频提交失败({resp.status_code}): {resp.text[:300]}")
                task_id = resp.json().get("task_id")
                if not task_id:
                    raise RuntimeError(f"MiniMax 未返回 task_id：{resp.text[:300]}")
                file_id = self._poll_minimax(client, headers, task_id)
                dl_url = self._retrieve_minimax(client, headers, file_id)  # 在 with 块内，client 未关闭
            if dl_url:
                with httpx.Client(timeout=90) as client:
                    vr = client.get(dl_url)
                    vr.raise_for_status()
                out_mp4 = d / f"{sid}.mp4"
                out_mp4.write_bytes(vr.content)
                produced = 1
        return str(d), produced

    def _image_ref(self, im: dict) -> str:
        """返回可用于 MiniMax content.image_url 的图片引用（本地图转 data URL / 远端 URL）。"""
        import pathlib
        import base64

        url = im.get("image_url") or ""
        if url and str(url).startswith(("http://", "https://")):
            return url
        path = im.get("local_path") or ""
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

    def _poll_minimax(self, client, headers: dict, task_id: str, timeout: int = 600, interval: int = 8) -> str:
        """轮询 MiniMax 图生视频任务状态，返回成功时的 file_id。"""
        import time as _t

        deadline = _t.time() + timeout
        while _t.time() < deadline:
            resp = client.get(f"{settings.video_base_url or settings.minimax_base_url}/v1/query/video_generation",
                              params={"task_id": task_id}, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                _t.sleep(interval)
                continue
            status = str(data.get("status") or "")
            if status in ("Success", "success"):
                return data.get("file_id") or ""
            if status in ("Fail", "Failed", "fail"):
                raise RuntimeError(f"MiniMax 图生视频失败：{data}")
            _t.sleep(interval)
        raise TimeoutError(f"MiniMax 图生视频超时：{task_id}")

    def _retrieve_minimax(self, client, headers: dict, file_id: str) -> str:
        """用 file_id 换取视频下载 URL。"""
        resp = client.get(f"{settings.video_base_url or settings.minimax_base_url}/v1/files/retrieve",
                          params={"file_id": file_id}, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return (data.get("file") or {}).get("download_url") or ""


_service: VideoService | None = None


def get_video_service() -> VideoService:
    """获取（单例）视频服务。"""
    global _service
    if _service is None:
        _service = VideoService()
    return _service
