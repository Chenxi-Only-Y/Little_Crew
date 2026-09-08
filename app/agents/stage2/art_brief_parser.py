"""Agent 0.45 美术设定包解析器（app/agents/stage2/art_brief_parser.py）。

美术资产部「独立生成」的入口：用户只给一段自由文本「美术设定包」
（例："白发剑客，戴斗笠，手持青锋长剑；青蛇化作的少女；竹林雨夜客栈"），
本 Agent 负责把它解析为结构化 {characters, scenes, style}，供
character_designer / scene_designer 在没有剧本时使用。

这样美术资产部不依赖创作策划部也能独立产资产；当有剧本时，解析结果仍可作补充。
"""

from app.agents.base import BaseAgent
from app.core.state import CrewState

_SYSTEM = (
    "你是影视美术设定的结构化助手。把用户给的「美术设定包」解析成 JSON，"
    "只输出 JSON，不要多余文字。JSON 结构为：\n"
    '{"style": "整体美术风格 一句话",\n'
    ' "characters": [{"name": "角色名", "appearance": "外貌描述",'
    ' "clothing": "服饰", "accessories": ["道具"], "style": "该角色风格"}],\n'
    ' "scenes": [{"scene_id": "S1", "location": "场景/地点", '
    '"atmosphere": "氛围", "description": "场景描述"}]}\n'
    "若设定包里没有角色/场景，对应数组给空数组。scene_id 用 S1,S2,S3… 顺序编号。"
)


class ArtBriefParser(BaseAgent):
    """把自由文本美术设定包解析为结构化的人物/场景/风格。"""

    name = "art_brief_parser"

    def run(self, state: CrewState) -> dict:
        brief = (state.get("art_brief") or "").strip()
        if not brief or state.get("art_brief_plan"):
            return {"current_phase": self.name}
        plan = self._parse(brief)
        return {"art_brief_plan": plan, "current_phase": self.name}

    def _parse(self, brief: str) -> dict:
        """解析设定包。优先 LLM；失败或 mock 时用确定性兜底。"""
        plan = self._parse_llm(brief)
        if plan:
            return plan
        return self._parse_fallback(brief)

    def _parse_llm(self, brief: str) -> dict | None:
        try:
            from app.services.llm import get_llm

            llm = get_llm()
            if llm.mock:
                return None
            plan = llm.chat_json(_SYSTEM, brief, role="writer")
            if isinstance(plan, dict):
                return {
                    "style": str(plan.get("style") or ""),
                    "characters": [c for c in plan.get("characters") or [] if isinstance(c, dict)],
                    "scenes": [s for s in plan.get("scenes") or [] if isinstance(s, dict)],
                }
        except Exception:  # noqa: BLE001
            return None
        return None

    def _parse_fallback(self, brief: str) -> dict:
        """确定性兜底：先按强分隔符切出独立实体，每个实体再按「，」取首段作名。

        例："白发剑客，戴斗笠，手持青锋长剑；青蛇化作的少女；竹林雨夜客栈"
          -> 角色[白发剑客, 青蛇化作的少女] + 场景[S1 竹林雨夜客栈]
        """
        import re

        # 强分隔符：分号/句号/换行 —— 视为不同实体
        entities = [e.strip() for e in re.split(r"[；;。\n]+", brief) if e.strip()]
        characters = []
        scenes = []
        scene_id = 1
        for entity in entities:
            # 取第一个逗号前的短语作为「名」，整段作为外貌/地点
            name = re.split(r"[，,]+", entity)[0].strip() or entity
            if any(k in entity for k in ("场景", "地点", "屋", "林", "山", "街", "寺", "殿", "阁", "店", "村", "城", "湖")):
                scenes.append({
                    "scene_id": f"S{scene_id}", "location": entity, "atmosphere": "", "description": entity,
                })
                scene_id += 1
            else:
                characters.append({
                    "name": name, "appearance": entity, "clothing": "", "accessories": [], "style": "",
                })
        return {"style": "", "characters": characters, "scenes": scenes}
