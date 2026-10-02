"""IP-Adapter 服务（app/services/ip_adapter_service.py）。

方案B 角色一致性：用「参考图 + 文本提示词 + 强度」生成保持人物外观的图片。

支持三种实现（settings.ip_adapter_provider）：
  - replicate: 调用 Replicate 上的 IP-Adapter 模型（API 优先）
  - comfyui:   通过 HTTP 调用本地 ComfyUI 工作流
  - mock:      返回带 seed 的 picsum 占位图（默认，零成本）

统一入口：
    from app.services.ip_adapter_service import generate_with_ip_adapter
    url = generate_with_ip_adapter(reference_image_url=..., prompt=..., strength=0.8, seed=42)

实现要点：
  - 复用 httpx 下载参考图到临时文件（不额外造轮子）。
  - replicate / comfyui 任一路径失败时会抛出异常（带清晰信息），便于上游回退到 mock。
"""

import ipaddress
import os
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.core.config import settings

# 临时目录：下载参考图用
_TMP_ROOT = Path(tempfile.gettempdir()) / "little_crew_ipadapter"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# 参考图下载最大字节数（约 8MB）：防超大文件打满磁盘/内存
_REF_MAX_BYTES = 8 * 1024 * 1024


def _assert_public_http(url: str) -> None:
    """校验 URL 仅为公网 http(s)，禁止内网/回环/链路本地等私有地址(防 SSRF)。

    简化处理：解析主机名并拒绝解析出任何私有地址的主机，避免 DNS 重绑绕过。
    """
    s = urlparse(url)
    if s.scheme not in ("http", "https"):
        raise ValueError("参考图 URL 仅支持 http/https")
    host = s.hostname
    if not host:
        raise ValueError("参考图 URL 缺少主机名")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise ValueError(f"无法解析参考图主机：{host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise ValueError(f"参考图 URL 指向内网/保留地址，已拒绝：{host}")
    return None


def _download_reference(reference_image_url: str) -> str:
    """下载参考图到本地临时文件，返回本地路径。"""
    if not reference_image_url:
        raise ValueError("缺少 reference_image_url")
    if reference_image_url.startswith(("http://", "https://")):
        # `safe_get` 会**逐跳**校验重定向：原先用
        # `httpx.Client(follow_redirects=True)`，入口校验只查了第一个 URL，
        # 攻击者让公网服务返回 `302 → http://127.0.0.1/...` 就能绕过。
        from app.services.url_guard import safe_get

        resp = safe_get(reference_image_url, context="参考图下载", timeout=120)
        resp.raise_for_status()
        if len(resp.content) > _REF_MAX_BYTES:
            raise ValueError(f"参考图过大（>{_REF_MAX_BYTES // 1024 // 1024}MB），已拒绝")
        ext = _sniff_ext(resp.headers.get("content-type"))
        path = _TMP_ROOT / f"ref_{uuid.uuid4().hex[:8]}{ext}"
        path.write_bytes(resp.content)
        return str(path)
    # 已支持本地路径（直接使用）。**仅允许本地推理服务自己的输出目录**，
    # 不做"任意已存在路径"的放行——那等于把服务端当任意文件读取器
    # （参考图内容会被上传给第三方 provider）。
    if _is_allowed_local_ref(reference_image_url):
        return reference_image_url
    raise ValueError(f"参考图既不是可访问的 URL，也不在允许的本地目录内：{reference_image_url[:60]}")


def _is_allowed_local_ref(p: str) -> bool:
    """本地参考图必须落在允许的根目录内（防任意文件读取）。"""
    try:
        target = Path(p).resolve()
    except (OSError, ValueError):
        return False
    if not target.is_file():
        return False
    from app.core.config import settings

    roots = []
    try:
        roots.append(Path(str(settings.output_dir_path)).resolve())
    except Exception:  # noqa: BLE001
        pass
    try:
        roots.append(_TMP_ROOT.resolve())
    except Exception:  # noqa: BLE001
        pass
    for root in roots:
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _normalize_references(reference_images) -> list:
    """把参考图输入规范为本地路径列表。

    - 接受单个 str / 已有本地路径，或 list[str]。
    - 返回全部有效路径；全失败则抛错。
    """
    if isinstance(reference_images, str):
        refs = [reference_images]
    else:
        refs = list(reference_images or [])
    paths = []
    for r in refs:
        try:
            paths.append(_download_reference(r))
        except Exception:  # noqa: BLE001  单个失败忽略，其余继续
            continue
    if not paths:
        raise ValueError("没有可用的参考图")
    return paths


def _sniff_ext(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    return ".png"


def _seed_or_random(seed: Optional[int]) -> int:
    return seed if seed is not None else int(uuid.uuid4().hex[:8], 16)


def _resolve_api_key(settings_api_key: str, replicate_token: str) -> str:
    """优先用平台专用 key，否则回退复制 replicate 平台的 token。"""
    return settings_api_key or replicate_token


# ------------------------------------------------------------------
# 各实现
# ------------------------------------------------------------------
def _generate_replicate(reference_images, prompt: str, negative_prompt: str,
                        strength: float, seed: int, model: str) -> str:
    """调用 Replicate 上的 IP-Adapter 模型。

    说明：Replicate 每次调用需指定一个具体的 model version。这里用一个可配置的
    model（默认 cross-attention），并允许通过 ip_adapter_model 指定。不同模型需在其
    inputs 里给出 image/prompt/scale（neg 与 seed 视模型而定，此处做尽力而为的传参）。
    """
    from app.services.runtime_settings import get as rs_get

    token = _resolve_api_key(rs_get("ip_adapter_api_key") or settings.ip_adapter_api_key,
                             rs_get("replicate_api_token") or settings.replicate_api_token)
    if not token:
        raise RuntimeError("IP_ADAPTER_PROVIDER=replicate 但未配置 IP_ADAPTER_API_KEY / REPLICATE_API_TOKEN")

    ref_paths = _normalize_references(reference_images)
    # 多参考图：Replicate 的 IP-Adapter 通常接受 image 列表；单个则直接给 data URI。
    data_urls = []
    for p in ref_paths:
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp"}.get(Path(p).suffix.lstrip(".").lower(), "image/png")
        with open(p, "rb") as f:
            data_urls.append(f"data:{mime};base64," + __import__("base64").b64encode(f.read()).decode())
    if len(data_urls) == 1:
        data_urls = data_urls[0]

    # IP-Adapter 模型常见 id（cross-attention / style）。不同 version 的 inputs 命名不同，
    # 这里给出一套通用输入并做必要的键名规范化。
    inputs = {
        "image": data_urls,
        "prompt": prompt,
        "scale": float(strength),
    }
    if negative_prompt:
        inputs["negative_prompt"] = negative_prompt
    if model == "style":
        inputs["ip_adapter_scale"] = float(strength)

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    predict_url = "https://api.replicate.com/v1/predictions"
    with httpx.Client(timeout=120) as client:
        resp = client.post(predict_url, headers=headers, json={"version": model, "input": inputs})
        resp.raise_for_status()
        pred = resp.json()
        pred_url = pred.get("urls", {}).get("get", "")
        if not pred_url:
            raise RuntimeError(f"Replicate 未返回预测 URL：{resp.text[:200]}")
        return _poll_replicate(client, headers, pred_url)


def _poll_replicate(client: httpx.Client, headers: dict, pred_url: str, timeout: int = 300, interval: int = 3) -> str:
    """轮询 Replicate 预测，返回结果图片 URL。"""
    deadline = _now() + timeout
    while _now() < deadline:
        resp = client.get(pred_url, headers=headers)
        resp.raise_for_status()
        pred = resp.json()
        status = pred.get("status")
        if status in ("succeeded", "succeeded "):
            output = pred.get("output")
            if isinstance(output, list) and output:
                return output[0] if isinstance(output[0], str) else str(output[0].get("url", ""))
            if isinstance(output, str):
                return output
            raise RuntimeError(f"Replicate 成功但无图片 URL：{pred}")
        if status in ("failed", "canceled"):
            raise RuntimeError(f"Replicate 预测失败：{pred}")
        import time

        time.sleep(interval)
    raise TimeoutError(f"Replicate 预测超时：{pred_url}")


def _generate_comfyui(reference_images, prompt: str, negative_prompt: str,
                      strength: float, seed: int) -> str:
    """通过 HTTP 调用本地 ComfyUI 工作流。

    约定：POST /prompt 传入一个标准的 IP-Adapter + 文生图工作流（含 LoadImage + Checkpoint +
    IPAdapterUnifiedLoader / IPAdapter + KSampler + VAE Decode + SaveImage）。
    这里构造一个最小可用的 workflow 模板（image 需要先由调用方上传到 ComfyUI 的 input 目录），
    并轮询 /history/{prompt_id} 取结果图。多参考图时用 ImageConcatMulti 叠加。
    """
    base = settings.comfyui_url.rstrip("/")
    ref_paths = _normalize_references(reference_images)

    # 逐张上传到 ComfyUI input 目录，并构造 LoadImage 节点
    load_nodes = {}
    for i, rp in enumerate(ref_paths):
        image_name = Path(rp).name
        _upload_to_comfyui_input(base, image_name, rp)
        load_nodes[str(10 + i)] = {"class_type": "LoadImage", "inputs": {"image": image_name}}

    # IP-Adapter 输入：单图直接给 LoadImage；多图用 ImageConcatMulti 叠加
    if len(ref_paths) == 1:
        ipadapter_image = [str(10), 0]
    else:
        keys = list(load_nodes.keys())
        load_nodes["20"] = {"class_type": "ImageConcatMulti", "inputs": {"image1": [keys[0], 0]}}
        for k in keys[1:]:
            load_nodes["20"]["inputs"][f"image{int(k) - 10 + 1}"] = [k, 0]
        ipadapter_image = ["20", 0]

    workflow = {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 20, "cfg": 7.0, "sampler_name": "euler", "scheduler": "normal",
            "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": negative_prompt or "", "clip": ["4", 1]}},
        "9": {"class_type": "IPAdapterUnifiedLoader", "inputs": {
            "preset": "PLUS FACE (portraits)", "model": ["4", 0], "ipadapter": ["10", 0]}},
        "10": {"class_type": "IPAdapter", "inputs": {
            "image": ipadapter_image, "model": ["4", 0], "ipadapter": ["9", 0],
            "weight": float(strength), "start_at": 0.0, "end_at": 1.0}},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "12": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": "ipadapter_out"}},
    }
    # 合并 LoadImage 节点（注意：10 作为 IPAdapter 的 ipadapter 引用，需避免与 LoadImage 键冲突）
    # 这里把 IPAdapter 的 ipadapter 输入改为 "9"，确保节点 id 不冲突。
    workflow["10"]["inputs"]["ipadapter"] = ["9", 0]
    workflow.update(load_nodes)
    payload = {"prompt": workflow, "client_id": "little_crew"}

    prompt_id = _comfyui_queue(base, payload)
    return _comfyui_poll(base, prompt_id)


def _upload_to_comfyui_input(base: str, image_name: str, local_path: str) -> None:
    """把参考图上传到 ComfyUI 的 input 目录（简单实现：走 /upload/image）。"""
    try:
        with open(local_path, "rb") as f:
            files = {"image": (image_name, f, "image/png")}
            data = {"overwrite": "true", "type": "input"}
            with httpx.Client(timeout=120) as client:
                client.post(f"{base}/upload/image", files=files, data=data)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"上传参考图到 ComfyUI 失败：{exc}")


def _comfyui_queue(base: str, payload: dict) -> str:
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{base}/prompt", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return str(data.get("prompt_id", ""))


def _comfyui_poll(base: str, prompt_id: str, timeout: int = 300, interval: int = 2) -> str:
    import time

    if not prompt_id:
        raise RuntimeError("ComfyUI 未返回 prompt_id")
    deadline = _now() + timeout
    while _now() < deadline:
        with httpx.Client(timeout=60) as client:
            resp = client.get(f"{base}/history/{prompt_id}")
            resp.raise_for_status()
            data = resp.json()
        if prompt_id in data:
            outputs = data[prompt_id].get("outputs", {})
            for node in outputs.values():
                images = node.get("images") or []
                if images:
                    return f"{base}/view?filename={images[0]['filename']}&subfolder={images[0].get('subfolder','')}&type={images[0].get('type','output')}"
        time.sleep(interval)
    raise TimeoutError(f"ComfyUI 任务超时：{prompt_id}")


def _generate_mock(reference_images, prompt: str, seed: int) -> str:
    """mock：返回带 seed 的 picsum 占位图。"""
    return f"https://picsum.photos/seed/ipadapter_{seed}/512/512"


# ------------------------------------------------------------------
# 统一入口
# ------------------------------------------------------------------
def generate_with_ip_adapter(
    reference_images,
    prompt: str,
    negative_prompt: str = "",
    strength: float = 0.8,
    seed: int = None,
) -> str:
    """使用 IP-Adapter 生成图片，返回图片 URL。

    Args:
        reference_images: 参考图（单个 URL/本地路径，或 list[URL] 支持多角色）
        prompt: 目标文本提示词
        negative_prompt: 负面提示词
        strength: 参考强度 0~1
        seed: 随机种子

    Returns:
        str: 生成的图片 URL
    """
    if strength <= 0:
        strength = settings.ip_adapter_strength
    provider = (settings.ip_adapter_provider or "mock").lower()
    model = settings.ip_adapter_model or "cross-attention"
    seed = seed if seed is not None else _seed_or_random(None)
    strength = float(strength) if 0 < strength <= 1 else settings.ip_adapter_strength

    if provider == "replicate":
        return _generate_replicate(reference_images, prompt, negative_prompt, strength, seed, model)
    if provider == "comfyui":
        return _generate_comfyui(reference_images, prompt, negative_prompt, strength, seed)
    # mock（默认）
    return _generate_mock(reference_images, prompt, seed)


def _now() -> float:
    import time

    return time.time()


# 单例服务（供 Agent 调用）
_service = None


def get_ip_adapter_service():
    global _service
    if _service is None:
        _service = IPAdapterService()
    return _service


class IPAdapterService:
    """面向 Agent 的薄封装，提供与 image_service 一致的调用面。"""

    def __init__(self) -> None:
        self.provider = settings.ip_adapter_provider or "mock"

    def generate(self, reference_images, prompt: str,
                 negative_prompt: str = "", strength: float = 0.8, seed: int = None) -> str:
        return generate_with_ip_adapter(reference_images, prompt, negative_prompt, strength, seed)
