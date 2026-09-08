"""Agent 2 编剧（app/agents/writer.py）。

输入：state.story_outline —— 故事大纲。
处理：调用 DeepSeek（System Prompt 强制 JSON 输出），改编为分场剧本。
输出：state.script —— 更新字典；失败时记录 state.error。
"""

import json

from app.agents._utils import call_deepseek, parse_json
from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

_MOCK_SCRIPT = {
    "title": "猫侦探事务所 · 第一场",
    "scenes": [
        {"scene_id": "S1", "location": "小镇街道", "atmosphere": "清晨微光", "action": ["主角登场"],
         "dialogue": [{"character": "咪咪", "line": "新的冒险，开始了。"}], "transition": "切"},
        {"scene_id": "S2", "location": "幽暗小巷", "atmosphere": "神秘", "action": ["发现线索"],
         "dialogue": [{"character": "小杰", "line": "等等我，我也去！"}], "transition": "切"},
    ],
}

SYSTEM_PROMPT = (
    "你是漫剧编剧。根据故事大纲改编为分场剧本。\n"
    "必须只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。\n"
    "JSON 结构如下（建议 3-5 场）：\n"
    "{\n"
    '  "title": "剧本标题",\n'
    '  "scenes": [\n'
    '    {\n'
    '      "scene_id": "S1",\n'
    '      "location": "场景地点",\n'
    '      "atmosphere": "氛围描述",\n'
    '      "action": ["画面动作描述"],\n'
    '      "dialogue": [{"character": "角色名", "line": "台词"}],\n'
    '      "transition": "转场方式"\n'
    "    }\n"
    "  ]\n"
    "}\n"
)


def _safe_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Screenwriter(BaseAgent):
    name = "screenwriter"

    def run(self, state: CrewState) -> dict:
        """编剧
        inputs:
            story_outline: dict  故事大纲
        outputs:
            script: dict         分场剧本
            error: str           失败信息（仅在异常时出现）
        """
        outline = state.get("story_outline")
        if not outline:
            return {"script": {}, "error": "缺少 story_outline", "current_phase": "screenwriter"}

        if settings.mock_llm:  # mock 分支：返回占位剧本
            return {"script": _MOCK_SCRIPT, "current_phase": "screenwriter"}

        try:
            user_msg = _safe_dump(outline)
            if state.get("feedback"):  # 人工确认点①重做：把审核反馈并入提示
                user_msg += "\n\n【编剧审核反馈，请据此修改剧本】" + _safe_dump(state["feedback"])
            data = parse_json(call_deepseek(SYSTEM_PROMPT, user_msg, role="screenwriter"))
            if not isinstance(data, dict):
                raise ValueError(f"编剧返回非 object：{type(data).__name__}")
            script = data
        except Exception as exc:  # noqa: BLE001
            return {"script": {}, "error": f"编剧失败：{type(exc).__name__}: {exc}", "current_phase": "screenwriter"}

        return {"script": script, "current_phase": "screenwriter"}
