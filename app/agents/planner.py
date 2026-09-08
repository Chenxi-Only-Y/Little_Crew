"""Agent 1 故事策划（app/agents/planner.py）。

输入：state.user_input —— 一句话创意 / 故事大纲。
处理：调用 DeepSeek（System Prompt 强制 JSON 输出），解析为结构化故事大纲。
输出：state.story_outline —— 更新字典；失败时记录 state.error。

异常处理：API 调用失败 / 返回非法 JSON 时，不抛异常，而是把错误写入 state.error
并返回空大纲，保证工作流不中断。
"""

import json
import re

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState
from app.services.llm import get_llm

_MOCK_OUTLINE = {
    "title": "猫侦探事务所",
    "logline": "一只会说话的小猫为了实现侦探梦，与人形少年搭档解开小镇谜案。",
    "genre": "温馨悬疑",
    "style": "日系清新水彩，柔和光影",
    "characters": [{"name": "咪咪", "role": "主角", "description": "会说话的橘猫侦探", "style": "圆脸大眼睛"}],
    "plot_beats": [{"order": 1, "summary": "咪咪醒来发现自己会说话", "conflict": "身世成谜"}],
}

SYSTEM_PROMPT = (
    "你是漫剧故事策划。根据用户的一句话创意，扩写为结构化「故事大纲」。\n"
    "必须只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。\n"
    "JSON 结构如下：\n"
    "{\n"
    '  "title": "故事标题",\n'
    '  "logline": "一句话梗概",\n'
    '  "genre": "题材",\n'
    '  "style": "整体画风描述(供后续绘图), 例如: 日系清新水彩, 柔和光影",\n'
    '  "characters": [{"name": "角色名", "role": "主角|配角|反派", '
    '"description": "角色简介", "style": "角色外观/画风"}],\n'
    '  "plot_beats": [{"order": 1, "summary": "情节点", "conflict": "冲突"}]}\n'
)


class StoryPlanner(BaseAgent):
    name = "story_planner"

    def run(self, state: CrewState) -> dict:
        """故事策划
        inputs:
            user_input: str  一句话创意 / 故事大纲
        outputs:
            story_outline: dict  故事大纲
            error: str          失败信息（仅在异常时出现）
        """
        user_input = (state.get("user_input") or "").strip()
        if not user_input:
            return {"story_outline": {}, "error": "缺少 user_input", "current_phase": "story_planner"}

        if settings.mock_llm:  # mock 分支：返回占位大纲
            return {"story_outline": dict(_MOCK_OUTLINE), "current_phase": "story_planner"}

        try:
            outline = self._call_deepseek(user_input)
        except Exception as exc:  # noqa: BLE001
            return {
                "story_outline": {},
                "error": f"故事策划失败：{type(exc).__name__}: {exc}",
                "current_phase": "story_planner",
            }

        return {"story_outline": outline, "current_phase": "story_planner"}

    def _call_deepseek(self, user_input: str) -> dict:
        from app.agents._utils import call_deepseek, parse_json as _pj
        raw = call_deepseek(SYSTEM_PROMPT, f"用户创意：{user_input}", role="writer")
        data = _pj(raw)
        if not isinstance(data, dict):
            raise ValueError(f"故事策划返回非 object：{type(data).__name__}")
        return data
