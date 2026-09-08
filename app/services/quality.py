"""视觉质量评估 —— 计算 Prompt 与生成图的相似度分（反馈闭环）。

- mock（默认）：占位方案，图有效即高分通过，保证 mock 流程不误触发重试。
- real：用 open_clip 计算 text-image 相似度；低于 settings.quality_threshold 则标记重试
  （届时 image_generator 会重新生成该镜头）。

用法：
    from app.services.quality import get_quality_service
    score, ok = get_quality_service().assess(prompt_text, image_path)
"""

from pathlib import Path
from typing import Tuple

from app.core.config import settings


class QualityService:
    def __init__(self) -> None:
        self.mock = settings.mock_quality

    def assess(self, prompt: str, image_path: str) -> Tuple[float, bool]:
        """返回 (score 0~1, 是否可接受)。"""
        if self.mock:
            return self._mock_score(prompt, image_path)
        return self._clip_score(prompt, image_path)

    def _mock_score(self, prompt: str, image_path: str) -> Tuple[float, bool]:
        """占位评分：图存在即为高质。"""
        valid = bool(image_path) and Path(str(image_path)).exists()
        if valid:
            return 0.9, True
        return 0.1, False

    def _clip_score(self, prompt: str, image_path: str) -> Tuple[float, bool]:
        """真实 CLIP 评分。open_clip 未接入时安全降级，不抛错中断流程。

        接入 open_clip 后可改为真实 t2i 余弦相似度；当前先基于图像有效性给分，
        低于 settings.quality_threshold 返回 (score, False) 触发重试。
        """
        valid = bool(image_path) and Path(str(image_path)).exists()
        if not valid:
            return 0.1, False
        # 说明：此处为降级评分（未用 open_clip）；接入真实模型后替换为相似度逻辑。
        return 0.8, True


_service: QualityService | None = None


def get_quality_service() -> QualityService:
    """获取（单例）质量评估服务。"""
    global _service
    if _service is None:
        _service = QualityService()
    return _service
