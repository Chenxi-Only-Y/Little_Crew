"""把现有后端**登记成通道**（本地实现）。

这一层不改动原模块，只做"适配"：把各自的签名折成统一 `dict -> dict`。

## 为什么适配而不是重写

`tts_breeze` / `video_operator` / `music_bed` 都已经真跑验证过（字节级确定性、
失败语义、时长契约）。**重写等于把已验证的东西再赌一次。**
适配层薄，且谁都能对着原模块读明白。

## 云端通道

`CloudChannel` 是**通用**的：给它 endpoint、key 环境变量、请求模板，
它就能承载任何云端能力（文本/图像/视频/语音）。**没配就是不可用**，
绝不静默退回本地。
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

from little_crew.runtime.capability_channels import Channel, register

# ---------------------------------------------------------------- 本地：TTS


class BreezeTts(Channel):
    """本机 breeze-tts-2（离线、可复现）。"""

    name = "breeze"
    capability = "tts"
    location = "local"

    def available(self) -> tuple[bool, str]:
        from little_crew.runtime import tts_breeze as T

        if not T.exe_path().is_file():
            return False, f"找不到 exe：{T.exe_path()}"
        if not T.model_path().is_file():
            return False, f"找不到模型：{T.model_path()}"
        return True, ""

    def call(self, request: dict) -> dict:
        from little_crew.runtime import tts_breeze as T

        out = Path(str(request.get("path") or ""))
        if not str(out):
            return {"ok": False, "error": "缺少 path"}
        r = T.synth(str(request.get("text") or ""), out,
                    voice=str(request.get("voice") or ""),
                    instruction=str(request.get("instruction") or ""),
                    seed=request.get("seed") or 42)
        return {"ok": bool(r.get("ok")), "path": str(out) if r.get("ok") else "",
                "seconds": float(r.get("seconds") or 0),
                "provider": self.name, "model": "breeze-tts-2",
                "servedModel": "breeze-tts-2", "error": r.get("error") or "",
                "sha256": r.get("sha256") or ""}


# ---------------------------------------------------------------- 本地：视频


class LocalVideo(Channel):
    """本机视频：`kenburns`（静帧缓推）或 `h3`（真运动，若服务在）。"""

    name = "local-video"
    capability = "video"
    location = "local"

    def available(self) -> tuple[bool, str]:
        return True, ""                                # kenburns 恒可用

    def call(self, request: dict) -> dict:
        from little_crew.runtime import video_operator as V

        backend = str(request.get("backend") or "kenburns")
        r = V.render(request.get("image") or "", request.get("path") or "",
                     duration=float(request.get("duration") or 0),
                     backend=backend,
                     prompt=str(request.get("prompt") or ""),
                     ffmpeg=str(request.get("ffmpeg") or "ffmpeg"))
        return {"ok": bool(r.get("ok")), "path": str(request.get("path")) if r.get("ok") else "",
                "seconds": float(r.get("seconds") or 0),
                "provider": self.name, "model": backend,
                "servedModel": backend, "error": r.get("error") or "",
                "sha256": r.get("sha256") or ""}


# ---------------------------------------------------------------- 本地：音乐


class LocalMusic(Channel):
    """本机音乐：素材优先，没有就程序生成垫音。"""

    name = "music-bed"
    capability = "music"
    location = "local"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def call(self, request: dict) -> dict:
        from little_crew.runtime import music_bed as M

        r = M.resolve(float(request.get("seconds") or 0),
                      request.get("out_dir") or "outputs/_music",
                      mood=str(request.get("mood") or ""),
                      ffmpeg=str(request.get("ffmpeg") or "ffmpeg"))
        return {"ok": bool(r.get("ok")), "path": str(r.get("path") or ""),
                "seconds": float(r.get("seconds") or 0),
                "provider": self.name, "model": str(r.get("source") or ""),
                "servedModel": str(r.get("source") or ""),
                "error": r.get("error") or ""}


# ---------------------------------------------------------------- 本地：文本


class LocalText(Channel):
    """本机/内置 LLM（`models/registry.py` 的默认模型，可换 id）。

    说明：文本这一层本来就有注册表（含 endpoint/api_key/model 规格），
    这里只是把它**折成统一通道**，让上层用同一套 `call("text", {...})`。
    """

    name = "llm"
    capability = "text"
    location = "local"

    def __init__(self, model_id: str = "") -> None:
        self.model_id = str(model_id or "")

    def available(self) -> tuple[bool, str]:
        try:
            from little_crew.agent.llm import (DEFAULT_MODEL_ID, model_spec_for,
                                              resolve_api_key)

            mid = self.model_id or DEFAULT_MODEL_ID
            spec = model_spec_for(mid)
            key = resolve_api_key(spec)
            if not key:
                return False, f"缺少密钥（{getattr(spec.backends[0], 'api_key_env', '?')}）"
            return True, ""
        except Exception as e:                                # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def call(self, request: dict) -> dict:
        from little_crew.agent.llm import (DEFAULT_MODEL_ID, LlmClient,
                                          model_spec_for)

        mid = self.model_id or str(request.get("model") or DEFAULT_MODEL_ID)
        client = LlmClient(model_id=mid)
        reply = client.chat(str(request.get("prompt") or ""),
                            system=str(request.get("system") or ""))
        return {"ok": True, "text": reply.text, "path": "", "seconds": 0.0,
                "provider": self.name, "model": mid,
                "servedModel": str(getattr(reply, "served_model", "") or ""),
                "error": ""}


# ---------------------------------------------------------------- 云端：通用


class CloudChannel(Channel):
    """**通用云端通道**：一个 endpoint + 一个 key，承载任意能力。

    约定（与多数云端 API 一致）：
        请求 POST JSON -> 响应 `{data:[{url}]}` 或 `{url}`，或 `{id}` 异步任务
    没配 endpoint/key 时 `available()=False`，`call()` 明确报"未配置"。
    """

    def __init__(self, capability: str, *, name: str, endpoint_env: str,
                 key_env: str, model: str = "") -> None:
        self.capability = str(capability)
        self.name = str(name)
        self.location = "cloud"
        self.endpoint_env = str(endpoint_env)
        self.key_env = str(key_env)
        self.model = str(model)

    def available(self) -> tuple[bool, str]:
        ep = os.environ.get(self.endpoint_env)
        if not ep:
            return False, f"未配置 {self.endpoint_env}"
        return True, ""

    def call(self, request: dict) -> dict:
        endpoint = str(os.environ.get(self.endpoint_env) or "").rstrip("/")
        key = str(os.environ.get(self.key_env) or "")
        if not endpoint:
            return {"ok": False, "error": f"未配置 {self.endpoint_env}"}
        body = {k: v for k, v in (request or {}).items()
                if k not in ("path", "out_dir", "ffmpeg")}
        if self.model:
            body.setdefault("model", self.model)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(endpoint, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                payload = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:                                # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        url = ""
        plist = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(plist, list) and plist and isinstance(plist[0], dict):
            url = str(plist[0].get("url") or "")
        if not url and isinstance(payload, dict):
            url = str(payload.get("url") or "")
        out = Path(str(request.get("path") or ""))
        if url and out.name:
            try:
                with urllib.request.urlopen(url, timeout=300) as r2:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(r2.read())
            except Exception as e:                            # noqa: BLE001
                return {"ok": False, "error": f"下载失败：{type(e).__name__}: {e}"}
        return {"ok": True, "path": str(out) if out.name else "",
                "seconds": float(request.get("seconds") or 0),
                "provider": self.name,
                "model": str(payload.get("model") or self.model) if isinstance(payload, dict) else self.model,
                "servedModel": str(payload.get("model") or "") if isinstance(payload, dict) else "",
                "error": "", "url": url}


def register_builtin() -> None:
    """登记内置通道（幂等）。测试里可以重复调用来重置。"""
    for ch in (BreezeTts(), LocalVideo(), LocalMusic(), LocalText()):
        register(ch)
    # 云端通道：默认全部"未配置"（只有设了 endpoint 才可用）
    register(CloudChannel("tts", name="cloud-tts",
                          endpoint_env="LITTLE_CREW_CLOUD_TTS_ENDPOINT",
                          key_env="LITTLE_CREW_CLOUD_TTS_KEY"))
    register(CloudChannel("video", name="cloud-video",
                          endpoint_env="LITTLE_CREW_CLOUD_VIDEO_ENDPOINT",
                          key_env="LITTLE_CREW_CLOUD_VIDEO_KEY"))
    register(CloudChannel("image", name="cloud-image",
                          endpoint_env="LITTLE_CREW_CLOUD_IMAGE_ENDPOINT",
                          key_env="LITTLE_CREW_CLOUD_IMAGE_KEY"))
    register(CloudChannel("music", name="cloud-music",
                          endpoint_env="LITTLE_CREW_CLOUD_MUSIC_ENDPOINT",
                          key_env="LITTLE_CREW_CLOUD_MUSIC_KEY"))
    register(CloudChannel("text", name="cloud-text",
                          endpoint_env="LITTLE_CREW_CLOUD_TEXT_ENDPOINT",
                          key_env="LITTLE_CREW_CLOUD_TEXT_KEY"))
