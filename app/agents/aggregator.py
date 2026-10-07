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
            # ⚠️ 下面这几个是 2026-10-07 节点排查补上的。
            # 原先这个白名单**没有它们**，而 `task_runner` 结束时用 `result`
            # **整体替换**会话 payload、`/api/crew/status` 也只回 payload →
            # 于是这些产物**对外没有任何出口**：
            #   · 配音 mp3 已落盘，前端拿不到（要么再调一次 TTS）；
            #   · 预演初稿已生成，`#/previs` 只能再花一次 LLM 调用重出。
            # 修法：把它们加进白名单（它们都是 CrewState 里已声明的字段）。
            "voice_track": state.get("voice_track"),
            "bgm_track": state.get("bgm_track"),
            "previs": state.get("previs"),
            # 后期三环的产物与说明
            "video_report": state.get("video_report"),
            "fixed_images": state.get("fixed_images"),
            "video": state.get("video"),
            "timeline": state.get("timeline"),
            "final_video": state.get("final_video"),
            # AI 思考过程（各节点累积）
            "thinking_log": state.get("thinking_log") or [],
        }
        return {"result": result, "current_phase": "done"}
