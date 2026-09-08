"""Agent 0.6 场景设计师（app/agents/stage2/scene_designer.py）。

方案B 场景一致性：从剧本提取所有唯一场景，为每个场景生成**氛围参考图**，
供下游分镜/提示词编译做"舞台"锚定。

输入：state.script.scenes[] （scene_id / location / atmosphere / action[] / dialogue[]）
处理：
  - 按 scene_id 去重；相同 setting 描述（location+atmosphere）的合并为同一场景
  - 对每个场景生成一张氛围参考图（复用 image_service.generate）
  - 结构化：{scene_id, setting_text, characters_in_scene}
输出：
  - scene_reference_images: {"S1": "https://.../scene_S1.png"}
  - scene_design_profiles:  {"S1": {"setting", "characters_in_scene", "style"}}

Mock 模式（settings.mock_image=True）：返回占位图 + 场景描述文本，零成本。
"""

from typing import Dict, List, Tuple

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

# 场景氛围图 Prompt 模板
_PROMPT_TEMPLATE = "{setting_text}，{style}，concept art, environment design, 4k"


def _compose_setting(scene: dict) -> str:
    """把场景的 location + atmosphere 合并为 setting 文本。"""
    loc = str(scene.get("location") or "").strip()
    atm = str(scene.get("atmosphere") or "").strip()
    parts = [p for p in (loc, atm) if p]
    return "，".join(parts) if parts else "未命名场景"


def _characters_in_scene(scene: dict, known_names: list) -> List[str]:
    """从场景台词里提取该场景出现的角色（尽力而为，用角色参考图名匹配）。"""
    chars = []
    for d in scene.get("dialogue", []):
        c = str(d.get("character") or "").strip()
        if c and c not in chars:
            chars.append(c)
    return chars or known_names


class SceneDesigner(BaseAgent):
    """为每个场景生成氛围参考图，产出场景锚定描述。"""

    name = "scene_designer"

    # ---------------------------------------------------------------
    # 主入口
    # ---------------------------------------------------------------
    def run(self, state: CrewState) -> dict:
        """场景设计师
        inputs:
            script: dict          分场剧本（scenes[]）
            story_outline: dict   生成风格（style）
            character_reference_images: dict（用于判断场景里的角色，可选）
        outputs:
            scene_reference_images: dict  场景参考图 URL
            scene_design_profiles: dict   场景锚定描述
            current_phase: str
        """
        script = state.get("script")
        if not script or not script.get("scenes"):
            # 兜底：无剧本时从「美术设定包」解析出的场景生成（独立生成用）。
            plan_scenes = list((state.get("art_brief_plan") or {}).get("scenes") or [])
            if not plan_scenes:
                return {
                    "scene_reference_images": {},
                    "scene_design_profiles": {},
                    "current_phase": self.name,
                }
            script = {"scenes": plan_scenes}

        style = (state.get("story_outline") or {}).get("style", "") or \
            (state.get("art_brief_plan") or {}).get("style", "")
        known_names = list((state.get("character_reference_images") or {}).keys())

        # 精确重做部分场景：feedback.scene_ids 指定要重新生成的场景，其余沿用现有
        feedback = state.get("feedback") or {}
        target_scene_ids = set(str(s) for s in (feedback.get("scene_ids") or [])) if isinstance(feedback, dict) else set()
        existing_refs = dict(state.get("scene_reference_images") or {})
        existing_profiles = dict(state.get("scene_design_profiles") or {})

        # 场景去重：按 scene_id；相同 setting 描述（location+atmosphere 相同）合并
        unique = self._dedupe_scenes(script.get("scenes") or [])

        scene_refs: Dict[str, str] = {}
        scene_profiles: Dict[str, dict] = {}

        for scene in unique:
            sid = str(scene.get("scene_id") or "").strip()
            if not sid:
                continue
            if target_scene_ids:
                # 精确重做模式：只重做被点选的场景，其余沿用已有
                if sid in existing_refs or sid in existing_profiles:
                    scene_refs[sid] = existing_refs.get(sid, "")
                    scene_profiles[sid] = existing_profiles.get(sid, {})
                    if sid not in target_scene_ids:
                        continue  # 未点选：保留原样
            setting_text = _compose_setting(scene)
            characters = _characters_in_scene(scene, known_names)
            scene_profiles[sid] = {
                "setting": setting_text,
                "characters_in_scene": characters,
                "style": style,
            }
            scene_refs[sid] = self._generate_scene_image(sid, setting_text, style, scene)

        return {
            "scene_reference_images": scene_refs,
            "scene_design_profiles": scene_profiles,
            "current_phase": self.name,
        }

    # ---------------------------------------------------------------
    # 场景去重
    # ---------------------------------------------------------------
    def _dedupe_scenes(self, scenes: list) -> List[dict]:
        """按 scene_id 去重；相同 setting（location+atmosphere）合并为同一场景。"""
        seen_sid = set()
        seen_setting = set()
        out = []
        for s in scenes:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("scene_id") or "").strip()
            setting = _compose_setting(s)
            # 若 scene_id 或 setting 已出现过则跳过（合并）
            if (sid and sid in seen_sid) or (setting and setting in seen_setting):
                continue
            if sid:
                seen_sid.add(sid)
            if setting:
                seen_setting.add(setting)
            out.append(s)
        return out

    # ---------------------------------------------------------------
    # 场景氛围图
    # ---------------------------------------------------------------
    def _generate_scene_image(self, sid: str, setting_text: str, style: str, scene: dict) -> str:
        """为单个场景生成氛围参考图；mock 返回占位图。"""
        prompt = _PROMPT_TEMPLATE.format(setting_text=setting_text, style=style or "电影感")
        if settings.mock_image:
            return f"https://picsum.photos/seed/scene_{sid}/768/512"
        from app.services.image import get_image_service

        gen_prompt = {
            "shot_id": f"scene_{sid}",
            "prompt": prompt,
            "width": 768,
            "height": 512,
            "seed": None,
        }
        try:
            url, _local, ok = get_image_service().generate(gen_prompt, role="scene_designer")
            return url if ok else ""
        except Exception:  # noqa: BLE001
            return ""
