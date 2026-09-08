"""Agent 0.5 角色设计师（app/agents/stage2/character_designer.py）。

方案B 角色一致性：在大纲阶段后、分镜/提示词之前，为每个角色生成**标准参考图**，
供下游 IP-Adapter / 提示词锚定使用。

输入：story_outline.characters（含 role/description/style）
     / script（兜底，可从中提取角色名与相关外貌信息）
处理：
  - 为每个角色生成 3 种姿态的标准参考图（正面标准照 / 侧面3/4照 / 半身表情照）
  - 使用固定高质量 Prompt 模板（front view, neutral expression, white background, ...）
  - 复用 `app/services/image.py` 的 `get_image_service().generate(prompt)`
  - 从多张图中挑最佳作为主参考图
输出：
  - character_reference_images: {"角色名": "https://.../character_xxx.png"}
  - character_profiles:        {"角色名": {"appearance","clothing","accessories", ...}}

Mock 模式（settings.mock_image=True）：直接返回 picsum 占位 URL，零成本可用。

注意：角色本身没有独立 API，本 Agent 通过复用 image_service 生成参考图，
因此这里不混用 LLM，只做「文本提取 + 图像生成 + 挑图」。
"""

import json
from typing import Dict, List, Tuple

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# ------------------- 固定高质量 Prompt 模板 -------------------
# 「角色设计图」三姿态模板。base 部分为固定高质量词；姿态/表情按需替换。
_PROMPT_TEMPLATE = (
    "A character design sheet of {name}, {appearance}, {pose}, {expression}, "
    "white background, concept art, high quality, 4k"
)

# 三种姿态：key -> (pose 短语, expression 短语, 宽高)
_POSES = {
    "front": ("front view", "neutral expression", 512, 512),
    "three_quarter": ("three-quarter side view", "calm expression", 512, 512),
    "half_body_expression": ("half-body shot, head and shoulders", "expressive face", 512, 512),
}

# 姿态优先级：挑选主参考图时据此排序（正面标准照优先）
_POSE_ORDER = ["front", "three_quarter", "half_body_expression"]

# 每个角色生成多少张
_POSES_PER_CHARACTER = 3


def _build_prompt(name: str, appearance: str, pose_key: str) -> str:
    """按固定模板拼装单个姿态的生成 prompt。"""
    pose, expression, _, _ = _POSES.get(pose_key, _POSES["front"])
    return _PROMPT_TEMPLATE.format(name=name, appearance=appearance, pose=pose, expression=expression)


class CharacterDesigner(BaseAgent):
    """为故事中的每个角色生成标准参考图，并产出锚定用的 character_profiles。"""

    name = "character_designer"

    # ---------------------------------------------------------------
    # 主入口
    # ---------------------------------------------------------------
    def run(self, state: CrewState) -> dict:
        """角色设计师
        inputs:
            story_outline: dict  含 characters[]（name/role/description/style）
            script: dict         分场剧本（兜底提取角色名）
            feedback: dict       资产确认重做反馈（character → 只重做该角色；edited_appearance → 覆盖外貌）
        outputs:
            character_profiles: dict          每个角色的详细外貌描述
            character_reference_images: dict  每个角色的参考图 URL
            current_phase: str
        """
        characters = self._collect_characters(state)
        if not characters:
            return {
                "character_profiles": {},
                "character_reference_images": {},
                "current_phase": self.name,
            }

        # 资产确认的重做反馈：决定是否只重做某个角色 / 是否用编辑后的外貌
        feedback = state.get("feedback") or {}
        target_char = str(feedback.get("character") or "").strip() if isinstance(feedback, dict) else ""
        edited_appearance = str(feedback.get("edited_appearance") or "").strip() if isinstance(feedback, dict) else ""

        profiles: Dict[str, dict] = {}
        reference_images: Dict[str, str] = {}
        existing_profiles = dict(state.get("character_profiles") or {})
        existing_refs = dict(state.get("character_reference_images") or {})

        for char in characters:
            name = (char.get("name") or "").strip()
            if not name:
                continue
            # 只重做目标角色时，其余沿用已有资产
            if target_char and name != target_char:
                if existing_profiles.get(name) or existing_refs.get(name):
                    profiles[name] = existing_profiles.get(name, {})
                    if existing_refs.get(name):
                        reference_images[name] = existing_refs[name]
                else:
                    profile = self._extract_appearance_from_script(char, state)
                    profiles[name] = profile
                    sheet = self._generate_character_sheet(name, profile)
                    best = self._select_best_reference(sheet)
                    if best:
                        reference_images[name] = best
                continue

            profile = self._extract_appearance_from_script(char, state)
            if edited_appearance and name == target_char:
                profile["appearance"] = edited_appearance  # 覆盖为编辑后的外貌
            profiles[name] = profile
            sheet = self._generate_character_sheet(name, profile)
            best = self._select_best_reference(sheet)
            if best:
                reference_images[name] = best

        return {
            "character_profiles": profiles,
            "character_reference_images": reference_images,
            "current_phase": self.name,
        }

    # ---------------------------------------------------------------
    # 角色收集
    # ---------------------------------------------------------------
    def _collect_characters(self, state: CrewState) -> list:
        """从 story_outline.characters 收集角色；若无则从 script 台词里的角色名兜底；
        仍无则从「美术设定包」解析出的角色兜底（独立生成用）。"""
        chars = list((state.get("story_outline") or {}).get("characters") or [])
        if chars:
            return [c for c in chars if isinstance(c, dict)]
        script = state.get("script") or {}
        names = set()
        for scene in script.get("scenes", []):
            for d in scene.get("dialogue", []):
                n = (d.get("character") or "").strip()
                if n:
                    names.add(n)
        if names:
            return [{"name": n, "role": "配角", "description": "", "style": "标准角色"} for n in names]
        # 兜底：美术设定包解析出的角色（无剧本独立生成）
        plan_chars = list((state.get("art_brief_plan") or {}).get("characters") or [])
        return [c for c in plan_chars if isinstance(c, dict)]

    # ---------------------------------------------------------------
    # 2c + 新增方法：_extract_appearance_from_script
    # ---------------------------------------------------------------
    def _extract_appearance_from_script(self, char: dict, state: CrewState) -> dict:
        """从角色项/剧本中提取可复现的外貌锚定信息。

        优先用 story_outline.characters 里的 description/style；
        若角色在剧本台词中有身份线索，也尽量并入。返回：
        {"appearance", "clothing", "accessories", "hair", "body_type", "style"}
        """
        name = (char.get("name") or "").strip()
        # 美术设定包解析出的角色自带完整外貌字段，直接优先采用
        direct_appearance = str(char.get("appearance") or "").strip()
        direct_clothing = str(char.get("clothing") or "").strip()
        direct_accessories = char.get("accessories") or []
        direct_style = str(char.get("style") or "").strip()
        if direct_appearance or direct_clothing or direct_accessories:
            return {
                "appearance": direct_appearance or f"{direct_style} 风格的原创角色",
                "clothing": direct_clothing or "标准服装",
                "accessories": [a for a in (direct_accessories if isinstance(direct_accessories, list) else [])],
                "hair": "",
                "body_type": "",
                "style": direct_style or "",
                "source": "art_brief",
            }

        desc = str(char.get("description") or "").strip()
        style = str(char.get("style") or "").strip()

        # 从剧本中检索该角色相关台词/动作作为补充（尽力而为）
        script_hint = self._script_hint_for(name, state.get("script") or {})

        # 组装能“复现”的锚定字段。没有精确信息时给一个不冲突的笼统描述。
        appearance = desc or f"{style} 风格的原创角色".strip()
        clothing = self._sniff_field([desc, script_hint], ["穿着", "服饰", "服装", "衣", "袍", "裙"])
        accessories = self._sniff_accessories([desc, script_hint])

        profile = {
            "appearance": appearance,
            "clothing": clothing or (f"{style} 统一着装" if style else "标准服装"),
            "accessories": accessories,
            "hair": self._sniff_field([desc, style], ["发", "长发", "短发", "束发", "白发"]),
            "body_type": self._sniff_field([desc], ["身材", "壮硕", "瘦削", "高挑", "矮小"]),
            "style": style,
            "source": "outline" if desc else "script_fallback",
        }
        return profile

    def _script_hint_for(self, name: str, script: dict) -> str:
        """收集剧本中该角色的台词，拼成一段上下文（用于外貌推断的兜底）。"""
        lines = []
        for scene in script.get("scenes", []):
            for d in scene.get("dialogue", []):
                if (d.get("character") or "").strip() == name:
                    lines.append(str(d.get("line", "")))
        return "，".join(lines)[:200]

    def _sniff_field(self, texts: List[str], keywords: List[str]) -> str:
        """在文本里找含关键字的那一句/段，近似提取外貌字段。"""
        for t in texts:
            if not t:
                continue
            for kw in keywords:
                if kw in t:
                    # 取该句里关键字附近的一小段，避免整段太长
                    return t
        return ""

    def _sniff_accessories(self, texts: List[str]) -> list:
        """从文本里抓取常见的道具/配饰关键字。"""
        known = ["斗笠", "剑", "刀", "簪", "玉佩", "扇", "项链", "护腕", "腰带", "斗篷", "帽子", "面具"]
        found = []
        for t in texts:
            if not t:
                continue
            for kw in known:
                if kw in t and kw not in found:
                    found.append(kw)
        return found

    # ---------------------------------------------------------------
    # 2a + 新增方法：_generate_character_sheet
    # ---------------------------------------------------------------
    def _generate_character_sheet(self, name: str, profile: dict) -> List[dict]:
        """为单个角色生成 3 种姿态的标准参考图。

        每个姿态产出 {"pose": key, "url", "local_path", "ok"}。
        mock 模式下直接返回 picsum 占位 URL。
        """
        appearance = profile.get("appearance", "")
        results: List[dict] = []
        for pose_key in _POSE_ORDER:
            prompt_text = _build_prompt(name, appearance, pose_key)
            if settings.mock_image:
                # 需求：mock 返回占位图 URL
                seed = f"{name}-{pose_key}"
                url = f"https://picsum.photos/seed/{seed}/512/512"
                results.append({"pose": pose_key, "url": url, "local_path": "", "ok": True})
                continue
            shot_id = f"char_{name}_{pose_key}"
            _, width, height = pose_key, _POSES[pose_key][2], _POSES[pose_key][3]
            prompt = {
                "shot_id": shot_id,
                "prompt": prompt_text,
                "width": width,
                "height": height,
                "seed": None,
            }
            try:
                url, local_path, ok = get_image_service().generate(prompt, role="character_designer")
                results.append({
                    "pose": pose_key, "url": url, "local_path": local_path, "ok": bool(ok),
                })
            except Exception as exc:  # noqa: BLE001
                results.append({"pose": pose_key, "url": "", "local_path": "", "ok": False, "error": str(exc)})
        return results

    # ---------------------------------------------------------------
    # 2d + 新增方法：_select_best_reference
    # ---------------------------------------------------------------
    def _select_best_reference(self, sheet: List[dict]) -> str:
        """从多张生成图中挑最佳作为主参考图。

        规则：取状态 ok 的图里，按姿态优先级最靠前的；都失败则返回空。
        （真正的“视觉质量/一致性”评估由 quality_assessor / consistency_reviewer 负责，
        这里先按姿态优先级选，保证至少有 1 张可用。）
        """
        ok = [s for s in sheet if s.get("ok") and s.get("url")]
        if not ok:
            return ""
        ok.sort(key=lambda s: _POSE_ORDER.index(s.get("pose")) if s.get("pose") in _POSE_ORDER else 99)
        return ok[0]["url"]


# 延迟导入，避免模块顶层循环依赖
def get_image_service():
    from app.services.image import get_image_service as _g

    return _g()
