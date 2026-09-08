"""Agent 7 视觉质量评估 + 角色一致性评分（app/agents/quality_assessor.py）。

功能扩展：在原有的「提示词-图像」质量评分基础上，增加**角色跨镜头一致性**自动评分。

输入：
  - state.images：分镜图列表（每张含 shot_id / local_path / image_url / characters）
  - state.prompts：提示词列表（含 shot_id / prompt / character_name / character_prompt）
  - state.character_reference_images：角色参考图 {"角色名": url}
  - state.character_profiles：角色外貌 {"角色名": {...}}
处理：
  a. 对每张分镜图，按 characters 关联到角色参考图
  b. 用 CLIP（图像特征）计算该镜与该角色参考图的风格/形象相似度
  c. 若无法做图像特征提取，降级为文字描述相似度（character_prompt 与分镜 prompt）
  d. 若两者都不可用，跳过该角色自动评分，标记为「需人工审核」
输出：
  - 每个角色跨镜头平均一致性评分
  - 一致性最低的镜头列表（需人工审核）
  - 整体一致性报告（写入 state）

阈值（与需求一致）：
  < 60  严重漂移，建议重新生成
  60-75 中度漂移，建议人工审核
  > 75  可接受
"""

import math
import sys
from typing import Dict, List, Optional

from app.agents.base import BaseAgent
from app.agents.stage2.consistency_auditor import (
    _try_load_clip,
    _cosine_similarity as _cosine,
)
from app.core.config import settings
from app.core.state import CrewState
from app.services.quality import get_quality_service

# 一致性分级阈值（SEVERE 固定 60；MODERATE 取 settings.consistency_threshold，默认 75）
SEVERE = 60.0
LOW_FEATURE_SIM_FALLBACK = 0.5  # 文字相似度低于此视为需人工（近似映射）


def _MODERATE() -> float:
    """中度漂移/可接受的分界：读设置项。"""
    return float(getattr(settings, "consistency_threshold", 75.0))


class QualityAssessor(BaseAgent):
    name = "quality_assessor"

    def run(self, state: CrewState) -> dict:
        """视觉质量评估 + 角色一致性评分
        inputs:
            images: list[dict]                   图片结果
            prompts: list[dict]                  提示词列表
            character_reference_images: dict     角色参考图
            character_profiles: dict             角色外貌
        outputs:
            images: list[dict]                   更新 quality / status
            consistency_score: float             整体角色一致性（0-100）
            character_consistency_report: dict   每角色跨镜头一致性报告
            current_phase: str
        """
        images = list(state.get("images") or [])
        prompts = {p.get("shot_id"): p for p in (state.get("prompts") or [])}
        char_refs = dict(state.get("character_reference_images") or {})
        char_profiles = dict(state.get("character_profiles") or {})

        # 1) 原有：提示词-图像 质量评分
        svc = get_quality_service()
        for im in images:
            sid = im.get("shot_id")
            prompt = prompts.get(sid, {})
            self._quality_check(im, prompt, svc)

        # 2) 角色跨镜头一致性
        consistency_report = self._assess_character_consistency(images, prompts, char_refs, char_profiles)
        overall = consistency_report.get("overall_score")

        # 仅当本次真的产出角色一致性（有打分的角色）时才覆盖 state 里已有的评分，
        # 避免在「镜头未关联角色」时用 None 清掉一致性审查(assets 点)已给出的评分。
        out = {"images": images, "current_phase": self.name}
        have_scores = overall is not None or any(
            c.get("avg_score") is not None for c in (consistency_report.get("characters") or {}).values()
        )
        if have_scores:
            out["consistency_score"] = overall
            out["character_consistency_report"] = consistency_report

        # 3) 质检汇总报告（供「质检总监」独立职业页展示）
        quality_report = self._build_quality_report(images, consistency_report)
        out["quality_report"] = quality_report
        return out

    # ---------------------------------------------------------------
    # 质检报告
    # ---------------------------------------------------------------
    def _build_quality_report(self, images: list, consistency_report: dict) -> dict:
        """把逐镜质量 + 角色一致性聚合为一张「质检报告」。"""
        import time

        # 逐镜：quality(图片质量) + consistency(该镜最低角色一致性) + 判定
        shots = []
        failed_shots = []
        char_shots = (consistency_report.get("characters") or {})
        # 建立 shot_id -> {char: score} 映射
        shot_char_score: Dict[str, list] = {}
        for _name, info in char_shots.items():
            if isinstance(info, dict) and isinstance(info.get("shots"), dict):
                for sid, sc in info["shots"].items():
                    shot_char_score.setdefault(sid, []).append(sc)

        for im in images:
            sid = im.get("shot_id")
            quality = im.get("quality")
            status = im.get("status", "ok")
            cons_scores = shot_char_score.get(sid, [])
            cons = round(sum(cons_scores) / len(cons_scores), 2) if cons_scores else None
            # 判定：质量失败 或 一致性低于中度阈值 -> 需关注
            level = self._level(cons) if cons is not None else ("pending" if quality is None else "ok")
            if status == "failed" or (cons is not None and cons < _MODERATE()):
                level = "attention"
            row = {
                "shot_id": sid,
                "quality": quality,
                "consistency": cons,
                "status": status,
                "level": level,
            }
            shots.append(row)
            if level == "attention":
                failed_shots.append(row)

        lowest = consistency_report.get("lowest_shots") or []
        # 整体评分：用一致性整体分（若为空退回图片质量均分）
        overall = consistency_report.get("overall_score")
        if overall is None:
            qs = [s["quality"] for s in shots if s["quality"] is not None]
            overall = round(sum(qs) / len(qs), 2) if qs else None

        return {
            "overall_score": overall,
            "shots": shots,
            "lowest_shots": lowest,
            "failed_shots": failed_shots,
            "method": consistency_report.get("method") or ("mock" if settings.mock_quality else "quality"),
            "generated_at": time.time(),
        }

    # ---------------------------------------------------------------
    # 原有质量检查
    # ---------------------------------------------------------------
    def _quality_check(self, im: dict, prompt: dict, svc) -> None:
        score, ok = svc.assess(
            prompt.get("prompt", ""), im.get("local_path") or im.get("image_url", "")
        )
        im["quality"] = round(float(score), 3)
        if not ok or float(im.get("quality", 1.0)) < settings.quality_threshold:
            im["status"] = "failed"

    # ---------------------------------------------------------------
    # 角色一致性
    # ---------------------------------------------------------------
    def _assess_character_consistency(
        self, images: list, prompts: dict, char_refs: dict, char_profiles: dict
    ) -> dict:
        """计算每个角色的跨镜头一致性，返回报告 dict。

        报告结构：
        {
          "overall_score": float|null,
          "characters": {"角色名": {"avg_score": float|null, "shots": {shot_id: score}, "level": "severe|moderate|ok|manual"}},
          "lowest_shots": [{"shot_id":..., "character":..., "score":...}],   # 需人工审核
          "manual_review": bool,   # 无法自动评分，需人工
          "method": "clip" | "text" | "manual",
        }
        """
        # 特征提取可用性判断（独立于 prompt-image 的 mock 开关，仅由能否加载模型决定）
        model = _try_load_clip()

        char_scores: Dict[str, Dict[str, float]] = {}
        char_shot_scores: Dict[str, dict] = {}
        lowest_shots = []
        method_used = "manual"

        for name, ref_url in char_refs.items():
            ref_vec = None
            # 角色参考图特征
            if model is not None and ref_url:
                ref_vec = self._encode(model, ref_url)

            per_shot: Dict[str, float] = {}
            for im in images:
                sid = im.get("shot_id")
                chars_in_shot = im.get("characters") or []
                # 只算该角色出现的镜头
                if name not in chars_in_shot:
                    continue
                img_url = im.get("local_path") or im.get("image_url")
                score = self._shot_character_score(
                    sid, name, ref_vec, ref_url, img_url, prompts.get(sid, {}), model, char_profiles
                )
                if score is None:
                    # 无法自动评分 → 标记需人工
                    method_used = "manual"
                    continue
                per_shot[sid] = round(score, 2)

            if per_shot:
                avg = round(sum(per_shot.values()) / len(per_shot), 2)
                char_scores[name] = avg
                char_shot_scores[name] = per_shot
                # 该角色一致性最低的镜头
                worst = min(per_shot.items(), key=lambda kv: kv[1])
                if worst[1] < _MODERATE():
                    lowest_shots.append({
                        "shot_id": worst[0], "character": name, "score": worst[1],
                        "level": self._level(worst[1]),
                    })
            else:
                char_scores[name] = None
                char_shot_scores[name] = {}

        if char_scores and any(v is not None for v in char_scores.values()):
            vals = [v for v in char_scores.values() if v is not None]
            method_used = "clip" if model is not None else "text"
            overall = round(sum(vals) / len(vals), 2)
        else:
            overall = None

        return {
            "overall_score": overall,
            "characters": {
                name: {
                    "avg_score": char_scores.get(name),
                    "shots": char_shot_scores.get(name, {}),
                    "level": self._level(char_scores[name]) if char_scores.get(name) is not None else "manual",
                }
                for name in char_refs
            },
            # 兼容 _assets_content / consistency_auditor 字段：角色名->均分 + 需人工审核名单
            "character_scores": {n: s for n, s in char_scores.items() if s is not None},
            "flagged": {
                "characters": [ls["character"] for ls in lowest_shots],
                "shots": [ls["shot_id"] for ls in lowest_shots],
            },
            "lowest_shots": sorted(lowest_shots, key=lambda x: x["score"]),
            "manual_review": overall is None,
            "method": method_used,
        }

    def _shot_character_score(self, sid, name, ref_vec, ref_url, img_url, prompt, model, char_profiles):
        """对单个镜头-角色计算相似度分；返回 0-100 或 None（无法评分）。"""
        if model is not None and ref_vec is not None and img_url:
            img_vec = self._encode(model, img_url)
            if img_vec is not None:
                return _similarity_to_score(_cosine(ref_vec, img_vec))
        # 图像特征不可用 → 文字相似度
        return self._text_similarity_score(name, prompt, char_profiles)

    def _text_similarity_score(self, name, prompt, char_profiles) -> Optional[float]:
        """降级方案：用 character_prompt 与镜头 prompt 的文字重合度做近似相似度。

        若两者文本都缺失，返回 None（触发人工审核标记）。
        """
        char_prompt = str(prompt.get("character_prompt") or "") if prompt else ""
        prompt_text = str(prompt.get("prompt") or prompt.get("positive") or "") if prompt else ""
        if not char_prompt and not prompt_text:
            # 试从角色外貌生成一个文本锚点
            prof = char_profiles.get(name, {})
            char_prompt = str(prof.get("appearance") or "")
        if not char_prompt or not prompt_text:
            return None
        sim = _text_overlap(_tokens(char_prompt), _tokens(prompt_text))
        return round(sim * 100.0, 2)

    def _encode(self, model, source: str) -> Optional[List[float]]:
        try:
            emb = model.encode(source)
            return [float(v) for v in emb]
        except Exception as exc:  # noqa: BLE001
            print(f"[quality_assessor] 编码失败 {source[:40]}：{exc}", file=sys.stderr)
            return None

    @staticmethod
    def _level(score: float) -> str:
        if score < SEVERE:
            return "severe"
        if score <= _MODERATE():
            return "moderate"
        return "ok"


def _similarity_to_score(sim: float) -> float:
    """余弦相似度 [-1,1] -> 0-100。"""
    return round(max(0.0, min(100.0, sim * 100.0)), 2)


def _tokens(text: str) -> set:
    """简单分词：中文按单字、英文按单词。"""
    import re

    text = (text or "").lower()
    words = re.findall(r"[a-z0-9]+", text)
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    return set(words) | set(chars)


def _text_overlap(a: set, b: set) -> float:
    """Jaccard 相似度（文本词集合重叠度）。"""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
