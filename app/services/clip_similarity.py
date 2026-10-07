"""CLIP 图像相似度的共享工具。

## 为什么单独一个模块

原先这套「加载 CLIP 模型 + 算余弦相似度 + 映射成分数」的逻辑长在
`app/agents/stage2/consistency_auditor.py` 里，而那个 Agent 的**产出与
「图像质检师」完全重复**（两者都产出 `consistency_score` /
`character_consistency_report`）—— 2026-10-07 用户决定**删掉「美术总监·
一致性审查」这个环节**，职责归图像质检师。

但**辅助函数还有人用**（图像质检师一直在 import 它们），所以把它们抽到这里，
删掉那个 Agent 时不留孤儿。

## 降级行为

CLIP 加载失败/超时/被 `LITTLE_CREW_SKIP_CLIP=1` 跳过时**返回 None**
（调用方据此降级为文字相似度或标记"需人工审核"），**不抛异常** ——
一致性评分不该让整条流程挂掉。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from app.core.config import settings

#: 模型缓存与失败原因（避免重复尝试加载）
_MODEL_CACHE: Dict[str, Optional[object]] = {}
_MODEL_LOADED = False
_MODEL_FAILURE_REASON: Optional[str] = None

#: 模型加载/下载超过此时长即视为不可用，快速降级
_MODEL_LOAD_TIMEOUT = 25


def _try_load_clip():
    """尝试加载 CLIP 图像模型；失败/超时返回 None 并缓存原因。

    按 settings.consistency_model 选择后端：
      - clip: 用 openai/clip 包（CLIP/ViT-B/32，需 torchvision）
      - sentence-transformers: 用 SentenceTransformer("clip-ViT-B-32")（默认）
    两者都带「限时加载 + 环境变量跳过」，避免无网/慢网卡死：
      - 设置 LITTLE_CREW_SKIP_CLIP=1 直接跳过，快速降级。
    """
    global _MODEL_LOADED, _MODEL_FAILURE_REASON
    if _MODEL_LOADED:
        return _MODEL_CACHE.get("clip")
    _MODEL_LOADED = True

    import os

    if (getattr(settings, "skip_clip", False)
            or str(os.environ.get("LITTLE_CREW_SKIP_CLIP", "")).strip() in ("1", "true", "True")):
        _MODEL_FAILURE_REASON = "LITTLE_CREW_SKIP_CLIP 已设置，跳过 CLIP 加载"
        return None

    backend = (settings.consistency_model or "sentence-transformers").lower()
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

    def _load_sentence_transformers():
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer("clip-ViT-B-32")

    def _load_clip_pkg():
        import clip  # openai/clip（需 torchvision）

        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model, _preprocess = clip.load("ViT-B/32", device=device)

        class _ClipImageModel:
            """把 openai/clip 的图像分支封装成 encode(img_path) -> 向量 的接口。"""

            def __init__(self, clip_model, preprocess, device):
                self.clip_model = clip_model
                self.preprocess = preprocess
                self.device = device

            def encode(self, source: str):
                import torch
                from PIL import Image

                image = Image.open(source).convert("RGB")
                x = self.preprocess(image).unsqueeze(0).to(self.device)
                with torch.no_grad():
                    feat = self.clip_model.encode_image(x)
                return feat[0].cpu().tolist()

        return _ClipImageModel(model, _preprocess, device)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_load_sentence_transformers if backend != "clip" else _load_clip_pkg)
            model = future.result(timeout=_MODEL_LOAD_TIMEOUT)
        _MODEL_CACHE["clip"] = model
        return model
    except FutTimeout:
        _MODEL_FAILURE_REASON = f"CLIP({backend}) 加载超时(>{_MODEL_LOAD_TIMEOUT}s)，已降级"
        return None
    except Exception as exc:  # noqa: BLE001
        _MODEL_FAILURE_REASON = f"{type(exc).__name__}: {exc}"
        return None


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """余弦相似度 [-1,1]。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _similarity_to_score(sim: float) -> float:
    """把余弦相似度 [-1,1] 映射为 0-100 分。"""
    # 0.8 余弦 ≈ 80 分：线性映射，留 0.2 的负向空间到 0 分
    return round(max(0.0, min(100.0, sim * 100.0)), 2)
