"""Agent 3 分镜构建（app/agents/shot_composer.py）。

输入：state.script —— 分场剧本。
处理：调用 DeepSeek（System Prompt 强制 JSON 数组），拆解为 5-8 个镜头。
输出：state.storyboard —— 更新列表；失败时记录 state.error。
"""

import json

from app.agents._utils import call_deepseek, parse_json
from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

_MOCK_STORYBOARD = [
    {"shot_id": "S1-01", "scene_id": "S1", "shot_type": "远景", "camera": "固定机位",
     "description": "小镇街道，主角登场，清晨微光", "duration_s": 4.0, "dialogue": "新的冒险，开始了。", "transition": "切"},
    {"shot_id": "S1-02", "scene_id": "S1", "shot_type": "近景", "camera": "推镜头",
     "description": "主角特写，眼神坚定", "duration_s": 4.0, "dialogue": "", "transition": "切"},
    {"shot_id": "S2-01", "scene_id": "S2", "shot_type": "中景", "camera": "侧拍",
     "description": "幽暗小巷，发现线索", "duration_s": 4.0, "dialogue": "等等我，我也去！", "transition": "切"},
]
from app.core.state import CrewState

# 分镜粒度：total=全片5-8个镜头(收紧) / per_scene=按场细分(后续启用)
_SHOT_MODE_HINT = {
    "total": "全片总共生成 5-8 个镜头（不是每场 5-8 个），控制镜头总数。",
    "per_scene": "每个场景拆解为 3-5 个镜头，镜头数可较多，画面更细。",
}.get(settings.shot_mode, "全片总共生成 5-8 个镜头。")

SYSTEM_PROMPT = (
    "你是分镜导演。把分场剧本拆解成具体的镜头列表。\n"
    "必须只输出一个 JSON 数组，不要输出任何解释文字或 Markdown 代码块。\n"
    "要求："
    + _SHOT_MODE_HINT
    + "\n数组元素结构：\n"
    "{\n"
    '  "shot_id": "S1-01",\n'
    '  "scene_id": "S1",\n'
    '  "shot_type": "远景|中景|近景|特写",\n'
    '  "camera": "机位/运镜描述",\n'
    '  "description": "本镜头画面文学描述",\n'
    '  "duration_s": 4.0,\n'
    '  "dialogue": "本镜台词(可为空字符串)",\n'
    '  "transition": "转场方式"\n'
    "}\n"
)


def _safe_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class Storyboarder(BaseAgent):
    name = "storyboarder"

    def run(self, state: CrewState) -> dict:
        """分镜构建
        inputs:
            script: dict  分场剧本
        outputs:
            storyboard: list[dict]  镜头列表
            error: str              失败信息（仅在异常时出现）
        """
        script = state.get("script")
        if not script:
            return {"storyboard": [], "error": "缺少 script", "current_phase": "storyboarder"}

        if settings.mock_llm:  # mock 分支：返回占位分镜
            return {"storyboard": _MOCK_STORYBOARD, "current_phase": "storyboarder"}

        try:
            user_msg = _safe_dump(script)
            if state.get("feedback"):  # 人工确认点②重做：把审核反馈并入提示
                user_msg += "\n\n【分镜审核反馈，请据此修改分镜】" + _safe_dump(state["feedback"])
            data = parse_json(call_deepseek(SYSTEM_PROMPT, user_msg, role="storyboarder"))
            if isinstance(data, dict):  # 容忍被包裹在对象里
                data = data.get("storyboard") or data.get("shots")
            if not isinstance(data, list):
                raise ValueError(f"分镜返回非数组：{type(data).__name__}")
            storyboard = self._enforce_shot_count(data)
        except Exception as exc:  # noqa: BLE001
            return {"storyboard": [], "error": f"分镜构建失败：{type(exc).__name__}: {exc}", "current_phase": "storyboarder"}

        return {"storyboard": storyboard, "current_phase": "storyboarder"}

    def _enforce_shot_count(self, shots: list) -> list:
        """total 模式下把镜头数确定性裁剪到不超过 max_images（可配置）。

        均匀抽样以保留镜头在叙事上的分布；不足上限则原样返回。
        """
        if settings.shot_mode != "total":
            return shots
        # `max_images` 是用户可配置项（注册表声明 + /api/settings 可存），
        # 所以必须走 runtime_settings.resolve —— 此前只读 config，
        # 于是"设成 3 张仍出 8 张"，而且不报错。
        from app.services.runtime_settings import resolve as rs_resolve

        max_shots = max(1, int(rs_resolve("max_images", getattr(settings, "max_images", 8)) or 8))
        if len(shots) <= max_shots:
            return shots
        if max_shots <= 1:
            return [shots[0]]
        n = len(shots)
        idx = [round(i * (n - 1) / (max_shots - 1)) for i in range(max_shots)]
        return [shots[i] for i in idx]
