"""Agent 8 剧评/剧本评审（app/agents/critics.py）。

输入：state.script（分场剧本）+ state.story_outline。
处理：调用 DeepSeek 对剧本打分并给出强项/问题/建议（默认 mock，零成本）。
输出：
  - state.script_review —— {score, strengths[], issues[], suggestions[]}（兼容旧字段）
  - state.critic_report  —— 三维评分（逻辑/冲突/动机）+ 建议 + 修改历史，供「文学策划」职页展示。
"""

import time

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState
from app.services.llm import get_llm

SYSTEM_PROMPT = (
    "你是漫剧剧本评审。根据故事大纲与分场剧本，输出结构化 JSON 评审意见：\n"
    '{"score": 0-10, "strengths": ["强项"], "issues": ["问题"], "suggestions": ["修改建议"]}\n'
    "只输出 JSON，不要解释。"
)

_MOCK = {
    "score": 8.0,
    "strengths": ["结构清晰", "人物鲜明", "冲突渐进"],
    "issues": ["节奏稍快", "结尾冲突可更强烈"],
    "suggestions": ["增加一个铺垫镜头", "强化结尾的对抗戏"],
}


class ScriptCritic(BaseAgent):
    name = "critics"

    def run(self, state: CrewState) -> dict:
        """剧评
        inputs:
            script: dict          分场剧本
            story_outline: dict   故事大纲（可选）
        outputs:
            script_review: dict   评审意见（旧字段）
            critic_report: dict   三维评分 + 建议 + 修改历史
        """
        script = state.get("script")
        if not script:
            return {"script_review": {}, "critic_report": {}, "current_phase": "critics"}

        try:
            review = self._mock() if settings.mock_critics else self._real(script, state.get("story_outline") or {})
        except Exception as exc:  # noqa: BLE001
            review = {"score": 0, "strengths": [], "issues": [], "suggestions": [f"评审失败: {exc}"]}

        report = self._build_critic_report(review, script, state)
        return {"script_review": review, "critic_report": report, "current_phase": "critics"}

    def _mock(self) -> dict:
        return dict(_MOCK)

    def _real(self, script: dict, outline: dict) -> dict:
        import json

        payload = json.dumps({"outline": outline, "script": script}, ensure_ascii=False)
        return get_llm().chat_json(SYSTEM_PROMPT, f"剧本内容：{payload}", role="critics")

    # ---------------------------------------------------------------
    # 三维评分 + 汇总报告
    # ---------------------------------------------------------------
    def _build_critic_report(self, review: dict, script: dict, state: CrewState) -> dict:
        """生成三维评分报告：逻辑/冲突/动机 + 建议 + 修改历史。"""
        overall10 = float(review.get("score") or 0)          # 0-10
        overall = round(overall10 * 10, 2)                   # 0-100
        n_issues = len(review.get("issues") or [])
        n_strengths = len(review.get("strengths") or [])

        # 三维分：基于 overall / issues 数量 / strengths 数量 做启发式拆分（mock 稳定）
        logic = self._clamp(overall - n_issues * 4 - n_strengths, 0, 100)
        tension = self._clamp(overall - n_issues * 2, 0, 100)
        motive = self._clamp(overall - max(0, n_issues - 1) * 6, 0, 100)

        scores = {"logic": round(logic, 1), "tension": round(tension, 1),
                  "motive": round(motive, 1), "overall": overall}

        # 修改历史：追加一条本次评审记录
        history = list(state.get("critic_report", {}).get("history") or [])
        history.append({
            "time": time.time(),
            "overall_score": overall,
            "logic": scores["logic"], "tension": scores["tension"], "motive": scores["motive"],
            "note": "mock 评审" if settings.mock_critics else "DeepSeek 评审",
        })
        # 只保留最近 20 条
        history = history[-20:]

        return {
            "scores": scores,
            "strengths": review.get("strengths") or [],
            "issues": review.get("issues") or [],
            "suggestions": review.get("suggestions") or [],
            "history": history,
            "generated_at": time.time(),
        }

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, v))
