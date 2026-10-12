"""Agent 4 提示词编译（app/agents/prompt_refiner.py）。

方案B 角色一致性：生成图片提示词时注入角色外貌锚定信息。

输入：state.storyboard（分镜描述）+ state.story_outline.style（画风）
     + state.character_profiles（角色外貌锚定，来自 character_designer）
处理：调用 DeepSeek，把文学描述转成结构化 Prompt；若镜头出现角色名，则先用该角色的
      外貌描述替换，并额外生成"角色独立提示词"供 IP-Adapter 参考图使用。
输出：state.prompts —— 每个元素：
    {"shot_id", "positive", "negative", "character_prompt", "character_name",
     "prompt"(=positive 兼容), "negative_prompt"(=negative 兼容),
     "width", "height", "seed"}

Mock 模式：返回含角色外貌的标准模板提示词。
"""

import json

from app.agents._utils import call_deepseek, parse_json
from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

_MOCK_PROMPTS = [
    {
        "shot_id": "S1-01",
        "positive": "wide shot, small town street at dawn, a cute cat detective, watercolor, soft light, high detail",
        "negative": "blurry, low quality, text, watermark",
        "width": 768, "height": 1024, "seed": None,
    },
    {
        "shot_id": "S1-02",
        "positive": "close-up, cat detective face, determined eyes, watercolor, soft light",
        "negative": "blurry, low quality", "width": 768, "height": 1024, "seed": None,
    },
    {
        "shot_id": "S2-01",
        "positive": "medium shot, dark alley, finding clues, watercolor, mysterious",
        "negative": "blurry, low quality", "width": 768, "height": 1024, "seed": None,
    },
]

# 关键提示词模板：把分镜 → 高质量提示词，并强制角色外貌替换
SYSTEM_PROMPT = (
    "系统：你是一位专业的AI绘画提示词工程师。请将分镜描述转换为高质量的图像生成提示词。\n"
    "规则：\n"
    "1.如果分镜中出现角色名，必须先用该角色的外貌描述替换角色名\n"
    "2.外貌描述必须包含：发型、发色、服装、体型、面部特征\n"
    "3.保持画风一致性：{style}\n"
    "4.输出JSON格式：{\"positive\": \"...\", \"negative\": \"...\"}\n"
    "只能输出一个 JSON 数组，每个元素：\n"
    '{"shot_id": "S1-01", "positive": "融合角色外貌的完整正向提示词(建议英文)", '
    '"negative": "负面提示词"}\n'
    "不要输出任何解释文字或 Markdown 代码块。\n"
)

# 角色独立提示词模板：仅含角色外貌，用于 IP-Adapter 参考图生成
CHARACTER_PROMPT_TEMPLATE = (
    "A character portrait of {name}, {appearance}, {clothing}, {hair}, {body_type}, "
    "front view, neutral expression, white background, concept art, high quality, 4k"
)


def _safe_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _extract_character_name(text: str, known_names: list) -> str:
    """在分镜描述/文本中找出出现的已知角色名。"""
    if not text:
        return ""
    for name in known_names:
        if name and name in text:
            return name
    return ""


def _profile_to_appearance(profile: dict) -> str:
    """把 character_profiles 里的锚定字段拼成一句外貌描述。"""
    if not profile:
        return ""
    parts = [str(profile.get("appearance") or "")]
    for k in ("clothing", "hair", "body_type"):
        v = str(profile.get(k) or "").strip()
        if v:
            parts.append(v)
    acc = profile.get("accessories") or []
    if acc:
        parts.append("道具：" + "、".join(str(a) for a in acc))
    return "，".join(p for p in parts if p).strip()


def _build_character_prompt(name: str, profile: dict) -> str:
    """构建"角色独立提示词"（仅角色外貌，供 IP-Adapter 参考图生成）。"""
    if not name or not profile:
        return ""
    appearance = _profile_to_appearance(profile)
    return CHARACTER_PROMPT_TEMPLATE.format(
        name=name,
        appearance=appearance or "an original character",
        clothing=str(profile.get("clothing") or ""),
        hair=str(profile.get("hair") or ""),
        body_type=str(profile.get("body_type") or ""),
    ).strip()


class PromptCompiler(BaseAgent):
    name = "prompt_compiler"

    def run(self, state: CrewState) -> dict:
        """提示词编译
        inputs:
            storyboard: list[dict]       镜头列表
            story_outline.style: str     整体画风（可选）
            character_profiles: dict     角色外貌锚定（可选）
        outputs:
            prompts: list[dict]          与分镜一一对应（含角色外貌注入）
            error: str                   失败信息（仅在异常时出现）
        """
        storyboard = state.get("storyboard")
        if not storyboard:
            return {"prompts": [], "error": "缺少 storyboard", "current_phase": "prompt_compiler"}

        profiles = dict(state.get("character_profiles") or {})
        known_names = list(profiles.keys())
        style = (state.get("story_outline") or {}).get("style", "")

        if settings.mock_llm:  # mock 分支：返回占位提示词（含角色外貌标准模板）
            return {"prompts": self._mock_prompts(profiles, storyboard), "current_phase": "prompt_compiler"}

        payload = {
            "style": style,
            "storyboard": storyboard,
            "character_profiles": profiles,
        }
        if state.get("feedback"):  # 人工审核后重做：把风格/提示词反馈并入
            payload["feedback"] = state["feedback"]

        try:
            data = parse_json(call_deepseek(SYSTEM_PROMPT.replace("{style}", style), _safe_dump(payload), role="director"))
            if isinstance(data, dict):  # 容忍被包裹在对象里
                data = data.get("prompts")
            if not isinstance(data, list):
                raise ValueError(f"提示词返回非数组：{type(data).__name__}")
            prompts = self._normalize(data, storyboard, profiles, known_names, style)
        except Exception as exc:  # noqa: BLE001
            return {"prompts": [], "error": f"提示词编译失败：{type(exc).__name__}: {exc}", "current_phase": "prompt_compiler"}

        return {"prompts": prompts, "current_phase": "prompt_compiler"}

    # ---------------------------------------------------------------
    # Mock：返回含角色外貌的标准模板提示词
    # ---------------------------------------------------------------
    def _mock_prompts(self, profiles: dict, storyboard: list) -> list:
        """mock 分支：对每个 mock 镜头，若分镜描述出现角色名则注入外貌并生成 character_prompt。"""
        known_names = list(profiles.keys())
        desc_by_shot = {
            s.get("shot_id"): s.get("description", "") for s in storyboard if isinstance(s, dict)
        }
        out = []
        for item in _MOCK_PROMPTS:
            sid = item["shot_id"]
            positive = item["positive"]
            desc = desc_by_shot.get(sid, "")
            character_name = (
                _extract_character_name(desc, known_names)
                or _extract_character_name(positive, known_names)
            )
            profile = profiles.get(character_name, {})
            if character_name and profile:
                appearance = _profile_to_appearance(profile)
                positive = f"{appearance}, {positive}"
            new_item = dict(item)
            new_item["positive"] = positive
            new_item["negative"] = item.get("negative", "")
            new_item["character_name"] = character_name
            new_item["character_prompt"] = _build_character_prompt(character_name, profile)
            new_item["prompt"] = positive
            new_item["negative_prompt"] = item.get("negative", "")
            out.append(new_item)
        return out

    # ---------------------------------------------------------------
    # 规整 + 注入
    # ---------------------------------------------------------------
    def _normalize(self, prompts: list, storyboard: list, profiles: dict,
                   known_names: list, style: str) -> list:
        """规整提示词并注入角色外貌锚定。

        - 按 shot_id 对齐分镜描述作为兜底。
        - 从分镜描述中提取角色名；若命中 profile 则把外貌描述注入 positive 前缀。
        - 生成 character_prompt（仅角色外貌）供 IP-Adapter 参考图使用。
        - 保留 prompt/negative_prompt 作为正向/负向的兼容别名。
        """
        desc_by_shot = {
            s.get("shot_id"): {"description": s.get("description", ""), "dialogue": s.get("dialogue", "")}
            for s in storyboard if isinstance(s, dict)
        }
        out = []
        for item in prompts:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("shot_id") or "").strip()
            if not sid:
                continue

            info = desc_by_shot.get(sid, {})
            desc = str(info.get("description") or "")
            positive = str(item.get("positive") or item.get("prompt") or "").strip()
            negative = str(item.get("negative") or item.get("negative_prompt") or "").strip()

            # 兜底文本
            if not positive:
                positive = desc
            if not positive:
                positive = f"{sid} 场景画面，电影感构图，柔和光影，高细节"

            # 从分镜描述/正向词中找角色名
            character_name = _extract_character_name(desc, known_names) or _extract_character_name(positive, known_names)
            profile = profiles.get(character_name, {})
            appearance = _profile_to_appearance(profile)

            # 注入外貌到正向词前缀：{角色外貌描述}, {动作/场景}, {画风}
            injected = positive
            if character_name and appearance:
                injected = f"{appearance}, {positive}"

            # 生成"角色独立提示词"（仅角色外貌，用于 IP-Adapter 参考图）
            character_prompt = _build_character_prompt(character_name, profile)

            item["shot_id"] = sid
            item["positive"] = injected
            item["negative"] = negative or ""
            item["character_name"] = character_name
            item["character_prompt"] = character_prompt
            # 兼容别名（下游 image_generator / image.py 读取 prompt / negative_prompt）
            item["prompt"] = injected
            item["negative_prompt"] = negative or ""
            item.setdefault("width", 768)
            item.setdefault("height", 1024)
            item.setdefault("seed", None)
            out.append(item)
        return out
