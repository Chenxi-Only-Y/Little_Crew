"""Agent 3.5 剪辑指导/节奏导演（app/agents/stage3/rhythm_director.py）。

方案B：根据每个分镜对应的剧本情绪标签，决定镜头建议时长与节奏标签（快/中/慢），
并把时长写回 storyboard 每个镜头的 duration 字段。

输入：state.storyboard[]（含 shot_id/scene_id/shot_type/description/duration_s）
     + state.script.scenes[]（含 atmosphere 情绪）/ scene_id
处理：
  - 关联镜头场景 -> 剧本场景的情绪(气氛)标签
  - 依据情绪->节奏映射表决定 tempo 与时长区间
  - 总量控制：超出 MAX_TOTAL_S 时按配比压缩
输出：
  - state.rhythm_plan —— list[{shot_id, duration, tempo}]
  - storyboard —— 更新每个镜头的 duration_s
Mock：每镜头返回预设 2-3 秒（中节奏）。
"""

from typing import Dict, List, Optional

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# 情绪 -> (tempo, 时长区间)
EMOTION_TO_TEMPO = {
    "紧张": ("fast", (0.5, 1.5)), "战斗": ("fast", (0.5, 1.5)), "追逐": ("fast", (0.5, 1.5)),
    "打斗": ("fast", (0.5, 1.5)), "危机": ("fast", (0.5, 1.5)), "爆发": ("fast", (0.5, 1.5)),
    "惊悚": ("fast", (0.5, 1.5)),
    "对话": ("medium", (2.0, 3.0)), "日常": ("medium", (2.0, 3.0)), "过渡": ("medium", (2.0, 3.0)),
    "平静": ("medium", (2.0, 3.0)), "温馨": ("medium", (2.0, 3.0)), "悬疑": ("medium", (2.0, 3.0)),
    "抒情": ("slow", (3.0, 5.0)), "沉思": ("slow", (3.0, 5.0)), "风景": ("slow", (3.0, 5.0)),
    "回忆": ("slow", (3.0, 5.0)), "离别": ("slow", (3.0, 5.0)), "壮阔": ("slow", (3.0, 5.0)),
}

# 常见情绪关键字 -> tempo（用于从 atmosphere/description 里模糊匹配）
_EMOTION_KEYWORDS = {
    "fast": ["紧张", "战斗", "追逐", "打斗", "危机", "爆发", "惊悚", "追杀", "冲突", "激烈"],
    "medium": ["对话", "日常", "过渡", "平静", "温馨", "悬疑", "平常", "交谈", "生活"],
    "slow": ["抒情", "沉思", "风景", "回忆", "离别", "壮阔", "唯美", "静谧", "忧伤", "景"],
}

# 总时长上限（秒）
MAX_TOTAL_S = 60.0


def _classify_tempo(text: str) -> str:
    """从文本里模糊匹配出节奏标签。"""
    text = text or ""
    for tempo, kws in _EMOTION_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return tempo
    return "medium"


def _tempo_range(tempo: str) -> tuple:
    return {"fast": (0.5, 1.5), "medium": (2.0, 3.0), "slow": (3.0, 5.0)}.get(tempo, (2.0, 3.0))


def _midpoint(tempo: str) -> float:
    lo, hi = _tempo_range(tempo)
    return round((lo + hi) / 2.0, 2)


class RhythmDirector(BaseAgent):
    name = "rhythm_director"

    def run(self, state: CrewState) -> dict:
        """剪辑指导
        inputs:
            storyboard: list[dict]   分镜列表
            script: dict             分场剧本（取各场景 atmosphere 情绪）
        outputs:
            rhythm_plan: list[dict]  每镜 {shot_id, duration, tempo}
            storyboard: list[dict]   更新后的分镜（duration_s 写回）
        """
        storyboard = list(state.get("storyboard") or [])
        if not storyboard:
            return {"rhythm_plan": [], "current_phase": self.name}

        # 场景 -> 情绪标签（从 script.scenes[].atmosphere 提取）
        scene_atmo = self._scene_emotions(state.get("script") or {})

        plan: List[dict] = []
        for shot in storyboard:
            sid = str(shot.get("shot_id") or "").strip()
            scene_id = str(shot.get("scene_id") or "").strip()
            desc = str(shot.get("description") or "")
            atmo = scene_atmo.get(scene_id, "")
            # 综合情绪文本：场景气氛 + 镜头描述
            emotion_text = f"{atmo} {desc}"
            tempo = _classify_tempo(emotion_text)
            # 由节奏区间中值决定时长（mock 无情绪关键字 -> medium -> 2-3 秒）
            duration = _midpoint(tempo)
            plan.append({"shot_id": sid, "duration": duration, "tempo": tempo})

        # 总时长控制：超出 MAX_TOTAL_S 按配比压缩
        total = sum(p["duration"] for p in plan)
        if total > MAX_TOTAL_S and plan:
            scale = MAX_TOTAL_S / total
            for p in plan:
                p["duration"] = round(p["duration"] * scale, 2)

        # 写回 storyboard 的 duration_s
        by_id = {p["shot_id"]: p for p in plan}
        updated = []
        for shot in storyboard:
            s = dict(shot)
            p = by_id.get(str(shot.get("shot_id") or "").strip())
            if p:
                s["duration_s"] = p["duration"]
                s["tempo"] = p["tempo"]
            updated.append(s)

        return {"rhythm_plan": plan, "storyboard": updated, "current_phase": self.name}

    def _scene_emotions(self, script: dict) -> Dict[str, str]:
        """提取每个场景的情绪/气氛文本。"""
        out: Dict[str, str] = {}
        for scene in script.get("scenes", []):
            if not isinstance(scene, dict):
                continue
            sid = str(scene.get("scene_id") or "").strip()
            if sid:
                out[sid] = str(scene.get("atmosphere") or "")
        return out
