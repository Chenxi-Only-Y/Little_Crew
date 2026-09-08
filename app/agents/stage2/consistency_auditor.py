"""Agent 9 一致性审查（app/agents/stage2/consistency_auditor.py）。

方案B 角色/场景风格一致性审查：对角色参考图 + 场景概念图做风格特征比对，
输出整体一致性评分和逐角色/逐场景的偏差与建议。

输入：角色参考图（state.character_reference_images）+ 场景概念图（state.images 按 scene 分组，
     或未来可选的 state.scene_reference_images）。
处理：
  a. 用 CLIP / sentence_transformers 提取每张图的风格特征向量
  b. 计算所有角色图两两之间的风格相似度（余弦）
  c. 计算所有场景图两两之间的风格相似度（余弦）
  d. 汇总生成一致性报告
输出：
  - consistency_score: float（0-100）
  - character_consistency_report: dict
      {"pairwise_similarity": [...], "flagged": [...], "suggestions": [...]}

实现：优先用 sentence_transformers 的 CLIP 图像模型；库缺失或模型加载失败时
优雅降级为模拟相似度（85-95 随机），并打印警告。阈值默认 80 分（100 为完全一致）。
"""

import math
import random
import sys
import typing
from typing import Dict, List, Optional, Tuple

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# 默认一致性阈值（100 为完全一致，低于此值视为偏差）
DEFAULT_THRESHOLD = 80.0

# 模拟相似度区间（降级时使用）
_MOCK_LO, _MOCK_HI = 85.0, 95.0

# 模型滞后加载：尝试一次，失败则标记 unavailable
_MODEL_CACHE: Dict[str, Optional[object]] = {}
_MODEL_LOADED = False
_MODEL_FAILURE_REASON: Optional[str] = None


_MODEL_LOAD_TIMEOUT = 25  # 秒：模型加载/下载超过此时长即视为不可用，快速降级


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


class ConsistencyAuditor(BaseAgent):
    """角色 + 场景风格一致性审查 Agent。"""

    name = "consistency_auditor"

    def __init__(self, threshold: float | None = None) -> None:
        # 优先用 settings.consistency_threshold，其次传入值，最后回落 DEFAULT_THRESHOLD
        self.threshold = settings.consistency_threshold if threshold is None else threshold

    # ---------------------------------------------------------------
    # 主入口
    # ---------------------------------------------------------------
    def run(self, state: CrewState) -> dict:
        """一致性审查
        inputs:
            character_reference_images: dict  {"角色名": url}
            images: list[dict]               分镜图（按 scene_id 作场景概念图来源）
            scene_reference_images: dict(可选) {"场景": url}
        outputs:
            consistency_score: float
            character_consistency_report: dict
            current_phase: str
        """
        char_images = dict(state.get("character_reference_images") or {})
        scene_images = self._collect_scene_images(state)

        vectors = self._embed_all(char_images, scene_images)

        if vectors is None:
            # 优雅降级：返回模拟评分
            return self._fallback_report(char_images, scene_images)

        char_vecs = {name: vectors["characters"].get(name) for name in char_images}
        scene_vecs = {name: vectors["scenes"].get(name) for name in scene_images}

        char_sim = self._pairwise(char_vecs)
        scene_sim = self._pairwise(scene_vecs)

        report = self._build_report(char_images, scene_images, char_sim, scene_sim)
        # 整体分数：角色相似度均分与场景相似度均分合并
        all_scores = list(char_sim["scores"].values()) + list(scene_sim["scores"].values())
        overall = round(sum(all_scores) / len(all_scores), 2) if all_scores else 100.0

        return {
            "consistency_score": overall,
            "character_consistency_report": report,
            "current_phase": self.name,
        }

    # ---------------------------------------------------------------
    # 场景概念图来源
    # ---------------------------------------------------------------
    def _collect_scene_images(self, state: CrewState) -> Dict[str, str]:
        """收集场景概念图。优先用 state.scene_reference_images；
        否则从 images 按 scene_id 分组（每组取首个有效 URL）。"""
        explicit = dict(state.get("scene_reference_images") or {})
        if explicit:
            return explicit
        images = state.get("images") or []
        by_scene: Dict[str, str] = {}
        for im in images:
            sid = im.get("shot_id") or ""
            scene = im.get("scene_id") or sid.split("-")[0] if sid else "scene"
            url = im.get("image_url") or im.get("local_path")
            if url and scene not in by_scene:
                by_scene[scene] = url
        return by_scene

    # ---------------------------------------------------------------
    # 特征提取
    # ---------------------------------------------------------------
    def _embed_all(self, char_images: Dict[str, str], scene_images: Dict[str, str]):
        """对角色图 & 场景图编码；返回 {"characters": {...}, "scenes": {...}} 或 None。"""
        if settings.mock_quality:
            # 全局 mock 开关也走降级（与 quality_assessor 一致）
            return None
        model = _try_load_clip()
        if model is None:
            print(
                f"[consistency_auditor] CLIP 模型不可用，降级为模拟评分。"
                f" 原因：{_MODEL_FAILURE_REASON or '未安装 clip/sentence_transformers'}",
                file=sys.stderr,
            )
            return None

        chars: Dict[str, List[float]] = {}
        scenes: Dict[str, List[float]] = {}

        # 图片可来自 URL 或本地路径，交由句子向量模型编码
        for name, url in char_images.items():
            vec = self._encode_one(model, url)
            if vec is not None:
                chars[name] = vec
        for name, url in scene_images.items():
            vec = self._encode_one(model, url)
            if vec is not None:
                scenes[name] = vec

        if not chars and not scenes:
            return None
        return {"characters": chars, "scenes": scenes}

    def _encode_one(self, model, source: str) -> Optional[List[float]]:
        try:
            emb = model.encode(source)
            return [float(v) for v in emb]
        except Exception as exc:  # noqa: BLE001
            print(f"[consistency_auditor] 编码失败 {source[:40]}：{exc}", file=sys.stderr)
            return None

    # ---------------------------------------------------------------
    # 相似度
    # ---------------------------------------------------------------
    def _pairwise(self, vecs: Dict[str, List[float]]) -> dict:
        """计算命名实体的两两相似度。返回 {"scores": {name: 平均分}, "pairs": [...]}。"""
        names = list(vecs.keys())
        pairs = []
        scores: Dict[str, List[float]] = {n: [] for n in names}
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                sim = _cosine_similarity(vecs[a], vecs[b])
                score = _similarity_to_score(sim)
                pairs.append({"a": a, "b": b, "similarity": round(sim, 4), "score": score})
                scores[a].append(score)
                scores[b].append(score)
        avg = {n: round(sum(v) / len(v), 2) if v else 100.0 for n, v in scores.items()}
        return {"scores": avg, "pairs": pairs}

    # ---------------------------------------------------------------
    # 报告
    # ---------------------------------------------------------------
    def _build_report(self, char_images, scene_images, char_sim, scene_sim) -> dict:
        flagged_chars = [
            n for n, s in char_sim["scores"].items() if s < self.threshold
        ]
        flagged_scenes = [
            n for n, s in scene_sim["scores"].items() if s < self.threshold
        ]
        suggestions = []

        for n in flagged_chars:
            suggestions.append(
                f"角色「{n}」与其他角色的风格平均相似度 {char_sim['scores'][n]:.1f} 分低于阈值 "
                f"{self.threshold:.0f}，建议统一描边/配色/光比，或换用同一角色的参考图并保持 seed/风格关键词一致。"
            )
        for n in flagged_scenes:
            suggestions.append(
                f"场景「{n}」与其他场景风格相似度 {scene_sim['scores'][n]:.1f} 分低于阈值 "
                f"{self.threshold:.0f}，建议在提示词中补足统一的全局风格关键词（色调/笔触/光影）。"
            )
        if not flagged_chars and not flagged_scenes:
            suggestions.append("整体风格一致，无需调整。")

        return {
            "threshold": self.threshold,
            "model": "clip-ViT-B-32",
            "character_pairwise_similarity": char_sim["pairs"],
            "scene_pairwise_similarity": scene_sim["pairs"],
            "character_scores": char_sim["scores"],
            "scene_scores": scene_sim["scores"],
            "flagged": {"characters": flagged_chars, "scenes": flagged_scenes},
            "suggestions": suggestions,
        }

    # ---------------------------------------------------------------
    # 降级
    # ---------------------------------------------------------------
    def _fallback_report(self, char_images, scene_images) -> dict:
        """CLIP 不可用时返回模拟评分（属优雅降级，不报错）。"""
        char_names = list(char_images.keys())
        scene_names = list(scene_images.keys())

        def _rand_pairs(names):
            pairs = []
            scores = {n: [] for n in names}
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    a, b = names[i], names[j]
                    sc = round(random.uniform(_MOCK_LO, _MOCK_HI), 2)
                    pairs.append({"a": a, "b": b, "similarity": round(sc / 100.0, 4), "score": sc})
                    scores[a].append(sc)
                    scores[b].append(sc)
            return pairs, {n: round(sum(v) / len(v), 2) if v else 100.0 for n, v in scores.items()}

        char_pairs, char_scores = _rand_pairs(char_names)
        scene_pairs, scene_scores = _rand_pairs(scene_names)

        all_vals = list(char_scores.values()) + list(scene_scores.values())
        overall = round(sum(all_vals) / len(all_vals), 2) if all_vals else 95.0

        report = {
            "threshold": self.threshold,
            "model": "mock (CLIP unavailable)",
            "degraded": True,
            "character_pairwise_similarity": char_pairs,
            "scene_pairwise_similarity": scene_pairs,
            "character_scores": char_scores,
            "scene_scores": scene_scores,
            "flagged": {"characters": [], "scenes": []},
            "suggestions": [f"未加载 CLIP（{_MODEL_FAILURE_REASON or '库缺失'}），当前为模拟评分，仅供参考。"],
        }
        return {"consistency_score": overall, "character_consistency_report": report, "current_phase": self.name}
