"""图像生成服务 —— 对接阿里百炼(通义万相) / Replicate。

两种模式：
  - mock（`MOCK_IMAGE=1`，默认）：用 PIL 生成确定性纯色占位图，零成本、随时可测。
  - real（`MOCK_IMAGE=0`）：调用真实 API，支持
      * 阿里百炼 wanx 文生图（异步任务 + 轮询 + 下载落盘）
      * Replicate 文生图（异步任务 + 轮询 + 下载落盘）

统一约定 `generate(prompt) -> (image_url, local_path, ok)`。

用法：
    from app.services.image import get_image_service
    url, path, ok = get_image_service().generate(prompt_dict)
"""

import random
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Tuple

import httpx
from PIL import Image

from app.core.config import settings
from app.services.runtime_settings import get as rs_get

# 限流重试：429/5xx 退避重试
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_RETRY_MAX = 3
_RETRY_BASE = 1.0  # 首退 1s，指数翻倍
_THROTTLE_SEC = 1.5  # 每次真实生成之间的人工间隔，避免突发 429


def _retry_call(fn: Callable[[], str], label: str) -> str:
    """对返回 URL 的调用做 429/5xx 指数退避重试。"""
    last_err: Exception | None = None
    for attempt in range(_RETRY_MAX + 1):
        if attempt:
            time.sleep(max(_THROTTLE_SEC, _RETRY_BASE * (2 ** (attempt - 1))))
        try:
            return fn()
        except httpx.HTTPStatusError as exc:  # 5xx / 429 由状态码判定重试
            last_err = exc
            if exc.response.status_code in _RETRY_STATUSES:
                continue
            raise
        except RuntimeError as exc:
            last_err = exc
            if "429" in str(exc) or "Throttling" in str(exc) or "RateQuota" in str(exc):
                continue
            # 非限流错误：不重试，直接抛出
            raise
    raise RuntimeError(f"{label} 重试 {_RETRY_MAX} 次仍失败：{last_err}")


def _img(name: str, fallback):
    """读取图像相关配置：运行时覆盖(设置页)优先，否则回退。"""
    v = rs_get(name)
    return v if v is not None else fallback


def _dept_for_role(role: str) -> str:
    from app.core.org import dept_for_role

    return dept_for_role(role)


def _run_output_dir(output_dir, role: str, shot_id: str):
    """按「账号 / 部门 / 职业 / 会话」归类输出目录。

    生成的每张图落到: output/{user}/{部门}/{职业}/{会话}/{shot_id}.png，
    会话为空时用 shot_id 前缀归类，避免跨会话/独立生成混在 uuid 目录里。
    """
    from app.services.runtime_settings import user as cur_user, run_session
    from app.services.auth import get_user_store

    user = str(cur_user() or "local")
    # 用唯一 UID 作为账号目录，避免重名/匿名撞在一起
    try:
        user = get_user_store().resolve_uid(user) or user
    except Exception:  # noqa: BLE001
        pass
    dept = _dept_for_role(role)
    role_name = role or "其他"
    sess = run_session() or f"no-session-{shot_id}"
    base = Path(output_dir) / user / dept / role_name / sess
    base.mkdir(parents=True, exist_ok=True)
    return base


def _img_role(name: str, role: str, fallback):
    """按角色读图像配置：优先 `{name}_{role}`（如 image_provider_photographer），否则回退 `{name}`。"""
    if role:
        v = rs_get(f"{name}_{role}")
        if v is not None:
            return v
    return _img(name, fallback)


# ---- 线程局部 role：ImageService.generate 会被 ThreadPoolExecutor 并发调用，
#      存到实例属性会互相覆盖(race)，改为每线程各自持有。 ----
_role_local = threading.local()


def _set_role(role: str) -> None:
    _role_local.role = role or ""


def _cur_role() -> str:
    return getattr(_role_local, "role", "")


class ImageService:
    def __init__(self) -> None:
        self.mock = settings.mock_image
        self.output_dir = settings.output_dir_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------- 统一入口 -------------------

    def generate(self, prompt: dict, role: str = "") -> Tuple[str, str, bool]:
        """生成一张图。

        Args:
            prompt: 生成参数（shot_id/prompt/width/height/seed 等）
            role:   调用方职业（如 photographer/character_designer/scene_designer），
                    用于按职业读取图像模型配置（可选）。
        Returns:
            (image_url, local_path, ok)
        """
        _set_role(role)
        if self.mock:
            return self._mock_image(prompt)
        return self._real_image(prompt)

    # ------------------- mock 模式 -------------------

    def _mock_image(self, prompt: dict) -> Tuple[str, str, bool]:
        """生成确定性纯色占位图（同 seed 同色）。"""
        width = int(prompt.get("width", 768))
        height = int(prompt.get("height", 1024))
        rnd = random.Random(prompt.get("seed"))
        color = (rnd.randint(30, 230), rnd.randint(30, 230), rnd.randint(30, 230))
        img = Image.new("RGB", (width, height), color)

        out_dir = _run_output_dir(self.output_dir, _cur_role(), prompt.get("shot_id", "img"))
        path = out_dir / f"{prompt['shot_id']}.png"
        img.save(path)
        return str(path), str(path), True

    # ------------------- real 模式 -------------------

    def _real_image(self, prompt: dict) -> Tuple[str, str, bool]:
        provider = _img_role("image_provider", _cur_role(), settings.image_provider or "dashscope").lower()
        if provider == "replicate":
            return self._replicate(prompt)
        if provider == "minimax":
            return self._minimax(prompt)
        return self._dashscope(prompt)

    def _dashscope(self, prompt: dict) -> Tuple[str, str, bool]:
        """阿里百炼文生图路由。

        z-image* 模型走 multimodal-generation 同步接口；
        其它（wanx / qwen-image 等）走 image-synthesis 异步接口。
        """
        if "z-image" in _img_role("image_model", _cur_role(), settings.image_model).lower():
            return self._dashscope_zimage(prompt)
        return self._dashscope_wanx(prompt)

    def _dashscope_wanx(self, prompt: dict) -> Tuple[str, str, bool]:
        """通义万相文生图：提交异步任务 -> 轮询 -> 下载。"""
        key = (_img_role("dashscope_api_key", _cur_role(), settings.dashscope_api_key)
               or _img_role("image_api_key", _cur_role(), settings.image_api_key))
        if not key:
            raise ValueError("MOCK_IMAGE=0 但未配置 DASHSCOPE_API_KEY（阿里百炼）")

        size = f"{prompt.get('width', 768)}*{prompt.get('height', 1024)}"
        body = {
            "model": _img_role("image_model", _cur_role(), settings.image_model),
            "input": {"prompt": prompt.get("prompt", "")},
            "parameters": {"size": size, "n": 1},
        }
        if prompt.get("seed") is not None:
            body["parameters"]["seed"] = int(prompt["seed"])

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        submit_url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"

        with httpx.Client(timeout=120) as client:
            resp = client.post(submit_url, headers=headers, json=body)
            if resp.status_code >= 400:
                raise RuntimeError(f"DashScope 提交失败({resp.status_code}): {resp.text}")
            task_id = resp.json().get("output", {}).get("task_id")
            if not task_id:
                raise RuntimeError(f"DashScope 未返回 task_id：{resp.text}")
            img_url = self._poll_dashscope(client, headers, task_id)

        return self._save_remote(img_url, prompt["shot_id"])

    def _dashscope_zimage(self, prompt: dict) -> Tuple[str, str, bool]:
        """Z-Image 文生图：multimodal-generation 同步接口（该账号不支持异步）。

        返回结构：output.choices[0].message.content[0].image = 图片 URL。
        """
        key = (_img_role("dashscope_api_key", _cur_role(), settings.dashscope_api_key)
               or _img_role("image_api_key", _cur_role(), settings.image_api_key))
        if not key:
            raise ValueError("MOCK_IMAGE=0 但未配置 DASHSCOPE_API_KEY（阿里百炼）")

        text = str(prompt.get("prompt") or "").strip()
        if not text:
            raise ValueError("图片生成 prompt 为空，无法调用 Z-Image")

        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # 注意：该账号不支持异步 multimodal，务必用同步，不加 X-DashScope-Async
        }
        url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
        body = {
            "model": _img_role("image_model", _cur_role(), settings.image_model),
            "input": {"messages": [{"role": "user", "content": [{"text": text}]}]},
            "parameters": {"n": 1},
        }

        def _call() -> str:
            with httpx.Client(timeout=120) as client:
                resp = client.post(url, headers=headers, json=body)
            if resp.status_code >= 400:
                raise RuntimeError(f"Z-Image 调用失败({resp.status_code}): {resp.text}")
            return self._extract_zimage_url(resp.json())

        img_url = _retry_call(_call, "Z-Image")
        return self._save_remote(img_url, prompt["shot_id"])

    def _extract_zimage_url(self, data: dict) -> str:
        """从 Z-Image 响应中取图片 URL。"""
        choices = data.get("output", {}).get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", [])
            for item in content:
                if isinstance(item, dict) and item.get("image"):
                    return item["image"]
        raise RuntimeError(f"Z-Image 未返回图片 URL：{data}")

    def _poll_dashscope(
        self,
        client: httpx.Client,
        headers: dict,
        task_id: str,
        timeout: int = 120,
        interval: int = 3,
    ) -> str:
        """轮询 DashScope 异步任务，返回结果图片 URL。"""
        tasks_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = client.get(tasks_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            output = data.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                results = output.get("results") or []
                if results and results[0].get("url"):
                    return results[0]["url"]
                raise RuntimeError(f"DashScope 成功但无图片 URL：{data}")
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(f"DashScope 任务失败：{data}")
            time.sleep(interval)
        raise TimeoutError(f"DashScope 任务超时：{task_id}")

    def _replicate(self, prompt: dict) -> Tuple[str, str, bool]:
        """Replicate 文生图。

        说明：Replicate 每次调用需指定一个具体的 model version。
        请在下方 REPLICATE_MODEL_VERSION 填真实版本号，并填入 REPLICATE_API_TOKEN。
        也可直接改用阿里百炼（IMAGE_PROVIDER=dashscope）。
        """
        key = (_img_role("replicate_api_token", _cur_role(), settings.replicate_api_token)
               or _img_role("image_api_key", _cur_role(), settings.image_api_key))
        if not key:
            raise ValueError("MOCK_IMAGE=0 但未配置 REPLICATE_API_TOKEN / IMAGE_API_KEY")

        # TODO: 填入真实 model version，例如 stability 的 sdxl 版本
        model_version = settings.image_base_url or ""
        if not model_version:
            raise NotImplementedError(
                "Replicate 集成需在 .env 的 IMAGE_BASE_URL 填入具体 model version；"
                "或改用 IMAGE_PROVIDER=dashscope。"
            )

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        body = {
            "version": model_version,
            "input": {
                "prompt": prompt.get("prompt", ""),
                "negative_prompt": prompt.get("negative_prompt", ""),
            },
        }
        with httpx.Client(timeout=60) as client:
            resp = client.post("https://api.replicate.com/v1/predictions", headers=headers, json=body)
            resp.raise_for_status()
            pred = resp.json()
            pred_url = pred.get("urls", {}).get("get", "")
            if not pred_url:
                raise RuntimeError(f"Replicate 未返回预测 URL：{resp.text}")
            img_url = self._poll_replicate(client, headers, pred_url)
        return self._save_remote(img_url, prompt["shot_id"])

    def _poll_replicate(
        self,
        client: httpx.Client,
        headers: dict,
        pred_url: str,
        timeout: int = 180,
        interval: int = 3,
    ) -> str:
        """轮询 Replicate 预测，返回结果图片 URL。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = client.get(pred_url, headers=headers)
            resp.raise_for_status()
            pred = resp.json()
            status = pred.get("status")
            if status in ("succeeded", "canceled"):
                output = pred.get("output")
                if isinstance(output, list) and output:
                    return output[0] if isinstance(output[0], str) else output[0].get("url", "")
                if isinstance(output, str):
                    return output
                raise RuntimeError(f"Replicate 成功但无图片 URL：{pred}")
            if status in ("failed", "canceled"):
                raise RuntimeError(f"Replicate 预测失败：{pred}")
            time.sleep(interval)
        raise TimeoutError(f"Replicate 预测超时：{pred_url}")

    def _minimax(self, prompt: dict) -> Tuple[str, str, bool]:
        """MiniMax 文生图（image-01）。同步接口，返回 data.image_urls[0]。"""
        key = (_img_role("minimax_api_key", _cur_role(), settings.minimax_api_key)
               or _img_role("image_api_key", _cur_role(), settings.image_api_key))
        if not key:
            raise ValueError("MOCK_IMAGE=0 但未配置 MINIMAX_API_KEY / IMAGE_API_KEY（MiniMax 文生图）")

        width = int(prompt.get("width", 768))
        height = int(prompt.get("height", 1024))
        url = _img_role("image_base_url", _cur_role(), settings.minimax_base_url or "https://api.minimax.io").rstrip("/")
        from app.services.url_guard import assert_public_url
        assert_public_url(url, context="image_base_url")
        model = _img_role("image_model", _cur_role(), settings.image_model or "image-01")
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        # 图片质量：向 prompt 追加细节词（standard/high/ultra）
        quality = str(_img_role("image_quality", _cur_role(), settings.image_quality) or "standard").lower()
        base_prompt = str(prompt.get("prompt", ""))
        if quality == "ultra":
            base_prompt = base_prompt + ", ultra detailed, professional, 8k, sharp focus"
        elif quality == "high":
            base_prompt = base_prompt + ", high detail, sharp focus"
        body = {
            "model": model,
            "prompt": base_prompt,
            "aspect_ratio": self._aspect_ratio(width, height),
            "n": 1,
            "prompt_optimizer": True,
        }
        if prompt.get("seed") is not None:
            body["seed"] = int(prompt["seed"])

        def _call() -> str:
            # 明确的 connect/read 超时，避免卡死
            with httpx.Client(timeout=httpx.Timeout(90, connect=15)) as client:
                resp = client.post(f"{url}/v1/image_generation", headers=headers, json=body)
            if resp.status_code >= 400:
                raise RuntimeError(f"MiniMax 文生图失败({resp.status_code}): {resp.text[:300]}")
            urls = resp.json().get("data", {}).get("image_urls") or []
            if not urls:
                raise RuntimeError(f"MiniMax 文生图未返回图片：{resp.text[:300]}")
            return urls[0]

        img_url = _retry_call(_call, "MiniMax 文生图")
        return self._save_remote(img_url, prompt["shot_id"])

    @staticmethod
    def _aspect_ratio(width: int, height: int) -> str:
        """把宽高映射为 MiniMax 支持的 aspect_ratio 之一（1:1/16:9/4:3/3:2/2:3/3:4/9:16/21:9）。"""
        r = width / height if height else 1.0
        if r >= 1.9:
            return "21:9"
        if r >= 1.5:
            return "16:9"
        if r >= 1.2:
            return "3:2"
        if r >= 0.9:
            return "1:1"
        if r >= 0.7:
            return "4:3"
        if r >= 0.5:
            return "3:4"
        return "9:16"

    def _save_remote(self, img_url: str, shot_id: str) -> Tuple[str, str, bool]:
        """把远端图片下载到本地输出目录（按账号/部门/职业/会话归类）。"""
        if not img_url:
            return "", "", False
        out_dir = _run_output_dir(self.output_dir, _cur_role(), shot_id)
        path = out_dir / f"{shot_id}.png"
        with httpx.Client(timeout=60) as client:
            resp = client.get(img_url)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        return img_url, str(path), True


_image_instance: ImageService | None = None


def get_image_service() -> ImageService:
    """获取（单例）图像服务。"""
    global _image_instance
    if _image_instance is None:
        _image_instance = ImageService()
    return _image_instance
