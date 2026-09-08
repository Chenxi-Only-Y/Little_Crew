"""Agent 6 结果聚合：汇总阶段产物为一份展示用的图文结果。"""

from app.agents.base import BaseAgent
from app.core.state import CrewState


class Aggregator(BaseAgent):
    name = "aggregator"

    def run(self, state: CrewState) -> dict:
        """结果聚合
        inputs:
            story_outline / script / storyboard / prompts / images（来自前序阶段）
        outputs:
            result: dict  {session_id, outline, script, storyboard, prompts, images}
        """
        result = {
            "session_id": state.get("session_id", ""),
            "outline": state.get("story_outline"),
            "script": state.get("script"),
            "storyboard": state.get("storyboard"),
            "prompts": state.get("prompts"),
            "images": state.get("images"),
            # 方案B 角色一致性产物
            "character_profiles": state.get("character_profiles"),
            "character_reference_images": state.get("character_reference_images"),
            "style_reference_image": state.get("style_reference_image"),
            "consistency_score": state.get("consistency_score"),
            "character_consistency_report": state.get("character_consistency_report"),
            # 质检总监汇总报告
            "quality_report": state.get("quality_report"),
            # 文学策划 / 剧本评审
            "script_review": state.get("script_review"),
            "critic_report": state.get("critic_report"),
            # 场景设计师
            "scene_reference_images": state.get("scene_reference_images"),
            "scene_design_profiles": state.get("scene_design_profiles"),
            # 剪辑指导/节奏导演
            "rhythm_plan": state.get("rhythm_plan"),
            # 场记/连续性检查
            "continuity_report": state.get("continuity_report"),
            # 发行总监/分发协调
            "publish_results": state.get("publish_results"),
            # AI 思考过程（各节点累积）
            "thinking_log": state.get("thinking_log") or [],
        }
        return {"result": result, "current_phase": "done"}
