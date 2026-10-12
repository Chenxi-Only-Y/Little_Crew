"""Agent 0.5 角色设计师（app/agents/stage2/character_designer.py）。

方案B 角色一致性：在大纲阶段后、分镜/提示词之前，为每个角色生成**标准参考图**，
供下游 IP-Adapter / 提示词锚定使用。

## 产出规范（2026-10-07 用户拍板，别再凭感觉改）

| 项 | 规定 | 为什么 |
|---|---|---|
| **视图** | **正面 / 3/4 侧 / 侧面**，三个都**全身** | 用户选的是"AI 参考多角度"，不是建模用的严格三视图（正/侧/背）。用途是给 IP-Adapter 拉特征，所以**角度多样 + 画幅一致**比"正交三视图"更有用 |
| **图内文字** | **不要**（提示词排除 + 负面词压制） | 原提示词里的 `character design sheet` 在训练数据里就是**带文字标注的多格设定表**，模型会往图上写字 —— 而它画不出中文，产出一堆乱码（实测："脑强恕得蛇"）。参考图本该是干净纯图 |
| **尺寸** | **768×1024 竖幅** | 全身立绘是竖构图；原 512×512 方形把全身压得很小 |
| **seed** | **由角色名派生**（固定） | 原 `seed: None` → 同一角色重跑一次就换一张脸，与"一致性"直接冲突 |
| **负面提示词** | 必给（见 `_NEGATIVE_PROMPT`） | 排除多视图/文字/水印/多手多脚等 |
| **命名** | `char_{角色名}_{视图}.png`，视图 = `front` / `three_quarter` / `side` | 原 `half_body_expression` 既不是视图、画幅又跟另两张不一致，会往参考图集里掺噪声 |

## 输出

  - `character_reference_images: {"角色名": "主参考图 URL"}`（三个视图里挑，**正面优先**）
  - `character_profiles:        {"角色名": {"appearance", "clothing", "accessories", ...}}`

## Mock 模式

`settings.mock_image=True` 时走**本地**占位图（image_service 自己的 mock 分支），
**不再**用 `picsum.photos` —— 那是外网，离线跑就是一串碎图。

注意：角色本身没有独立 API，本 Agent 复用 `image_service` 生成参考图，
因此不混用 LLM，只做「文本提取 + 图像生成 + 挑图」。
"""

import hashlib
import json
import logging
from typing import Dict, List, Tuple

from app.agents.base import BaseAgent
from app.core.config import settings
from app.core.state import CrewState

logger = logging.getLogger("little_crew")

# ------------------- 固定高质量 Prompt 模板 -------------------
#
# ⚠️ 刻意**不用** `character design sheet` / `reference sheet` 这类词 ——
# 它们在训练数据里指向"带文字标注的多格设定表"，会诱使模型往图上写字。
# 这里要的是**单张、干净、全身立绘**。
_PROMPT_TEMPLATE = (
    "full body character illustration of {name}, {appearance}, "
    "{view}, standing, full body in frame head to toe, "
    "plain light grey background, soft even studio lighting, "
    "clean lineart, flat cel shading, character concept art, "
    "single figure, no text, no annotation, high quality"
)

#: 负面提示词：压制设定表/多视图/文字/常见崩坏
_NEGATIVE_PROMPT = (
    "multiple views, turnaround, character sheet, reference sheet, grid, "
    "collage, panels, split image, text, letters, words, chinese characters, "
    "watermark, signature, logo, caption, annotation, arrows, "
    "cropped, out of frame, close-up, portrait only, "
    "extra limbs, extra fingers, deformed hands, bad anatomy, "
    "blurry, lowres, jpeg artifacts, busy background"
)

# 三种视图：key -> (视图短语, 宽, 高)
# 全部**全身**、同一画幅 —— 画幅不一致会往参考图集里掺噪声。
#
# ⚠️ 侧面那条要写足：实测只写 `side view, profile` 时模型**常给 3/4 角**
# （身子侧了但脸还朝前），三个视图就退化成"两个 3/4 + 一个正面"。
# 补上 `full profile` + `90 degrees` + `facing sideways` 才稳定。
_VIEWS: dict[str, tuple[str, int, int]] = {
    "front": ("front view, facing the camera directly", 768, 1024),
    "three_quarter": ("three-quarter view, body turned 45 degrees", 768, 1024),
    # 侧面最难出：实测 `side view, profile` → 模型给 3/4；
    # 加 `full profile / 90 degrees` 仍是 3/4；再补"只露一只眼 + 鼻梁轮廓"
    # 这类**侧脸专有特征**才勉强压住。
    "side": ("full profile side view, body turned 90 degrees, "
             "head in profile facing sideways, only one eye visible, "
             "nose and muzzle in profile, side silhouette", 768, 1024),
}

#: 生成顺序 / 挑主参考图的优先级（正面优先）
_VIEW_ORDER = ["front", "three_quarter", "side"]

#: 兼容旧名：历史上用过 `half_body_expression`（半身表情照）
_LEGACY_VIEW_ALIASES = {"half_body_expression": "front"}

# 每个角色生成几张
_VIEWS_PER_CHARACTER = len(_VIEW_ORDER)


def _seed_for(name: str, view_key: str) -> int:
    """由**角色名 + 视图**派生固定 seed。

    为什么不用 `None`：那样每次重跑同一角色都会**换一张脸**，
    与"角色一致性"的目标直接冲突。同名同视图 → 同一个 seed → 同一张图。
    （不同角色之间也会自然不同，因为名字进了哈希。）
    """
    h = hashlib.sha256(f"{name}::{view_key}".encode("utf-8")).hexdigest()
    return int(h[:8], 16)          # 32 位正整数，diffusers 接受


def _build_prompt(name: str, appearance: str, view_key: str) -> str:
    """按固定模板拼装单个视图的生成 prompt。"""
    view, _, _ = _VIEWS.get(view_key, _VIEWS["front"])
    return _PROMPT_TEMPLATE.format(name=name, appearance=appearance, view=view)


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
        sheet_paths: Dict[str, str] = {}
        existing_profiles = dict(state.get("character_profiles") or {})
        existing_refs = dict(state.get("character_reference_images") or {})
        existing_sheets = dict(state.get("character_sheets") or {})

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
                    if existing_sheets.get(name):
                        sheet_paths[name] = existing_sheets[name]
                else:
                    profile = self._extract_appearance_from_script(char, state)
                    profiles[name] = profile
                    sheet = self._generate_character_sheet(name, profile, state)
                    best = self._select_best_reference(sheet)
                    if best:
                        reference_images[name] = best
                    sp = self._generate_sheet_image(name, profile, state)
                    if sp:
                        sheet_paths[name] = sp
                continue

            profile = self._extract_appearance_from_script(char, state)
            if edited_appearance and name == target_char:
                profile["appearance"] = edited_appearance  # 覆盖为编辑后的外貌
            profiles[name] = profile
            sheet = self._generate_character_sheet(name, profile, state)
            best = self._select_best_reference(sheet)
            if best:
                reference_images[name] = best
            sp = self._generate_sheet_image(name, profile, state)
            if sp:
                sheet_paths[name] = sp

        return {
            "character_profiles": profiles,
            "character_reference_images": reference_images,
            "character_sheets": sheet_paths,
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
    def _generate_character_sheet(self, name: str, profile: dict,
                                  state: CrewState | None = None) -> List[dict]:
        """为单个角色生成 3 个视图的标准参考图。

        每个视图产出 {"view": key, "url", "local_path", "ok", "seed"}。
        （`pose` 键保留作兼容，值与 `view` 相同。）

        mock 模式交给 `image_service` 自己的 mock 分支（**本地**生成占位图），
        不再拼 `picsum.photos` 的 URL —— 那是外网，离线就是碎图。
        """
        appearance = profile.get("appearance", "")
        # ⚠️ 产出的**归档归属**必须正确，否则角色图落进 `_global/`（账号丢失），
        # 与同一次运行的分镜图被劈成两个目录。
        #
        # 取上下文的优先级：
        #   1. `state["run_context"]` —— **权威**（`task_runner` 按已认证用户写入，
        #      且 seed 覆盖不了它）。实测带它跑 → 6 张图全部落到 `{账号}/{工程名}/…`。
        #   2. 回退 `capture()` —— 老调用方/测试的用法（只 `set_context` 不写
        #      `run_context`）。**必须在进入本方法时就抓**，晚了线程本地可能已变。
        from app.services.runtime_settings import capture as _ctx_capture
        from app.services.runtime_settings import restore as _ctx_restore

        ctx = _ctx_capture()
        if state and state.get("run_context"):
            from app.core.context import RunContext

            ctx = RunContext.from_state(state).as_dict()

        results: List[dict] = []
        for view_key in _VIEW_ORDER:
            _ctx_restore(ctx)
            prompt_text = _build_prompt(name, appearance, view_key)
            _, width, height = _VIEWS[view_key]
            seed = _seed_for(name, view_key)
            shot_id = f"char_{name}_{view_key}"
            prompt = {
                "shot_id": shot_id,
                "prompt": prompt_text,
                # 固定负面词：压掉设定表/多视图/文字/崩坏（见 _NEGATIVE_PROMPT）
                "negative_prompt": _NEGATIVE_PROMPT,
                # 固定 seed：同名同视图 → 同一个人（原先 None → 每次换脸）
                "seed": seed,
                "width": width,
                "height": height,
            }
            try:
                url, local_path, ok = get_image_service().generate(prompt, role="character_designer")
                results.append({
                    "view": view_key, "pose": view_key,
                    "url": url, "local_path": local_path,
                    "ok": bool(ok), "seed": seed,
                })
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "view": view_key, "pose": view_key,
                    "url": "", "local_path": "", "ok": False,
                    "seed": seed, "error": str(exc),
                })
        return results

    # ---------------------------------------------------------------
    # 2e：角色设定表（16:9，正脸放大 + 正/侧/背三视图）
    # ---------------------------------------------------------------
    def _generate_sheet_image(self, name: str, profile: dict,
                              state: CrewState | None = None) -> str:
        """额外生成一张 **16:9 角色设定表**（给人看的交付物）。

        ## 为什么单独出一张，而不是拿逐视角图拼

        用户 2026-10-07 选定了「16:9 单图」路线，取舍是：

          · **一致性**：四个视角在**同一张图**里生成 → 天然同一个人。
          · **代价**：一张 16:9 塞不下"大脸特写 + 三套精细服装"，
            模型会**随机丢配饰**（实测 Y2K 少女的"灰百褶裙粉内衬 / 银色珠链 /
            蕾丝及膝袜 / 粉白厚底鞋"常被简化掉）。

        对照的另一条路（每格独立生成再拼接）配饰能全保住，但四张独立采样
        → **脸会漂**。两条路都实测过，各有致命短板；用户选了"要一致性"。

        ## 所以它**不参与下游锚定**

        `character_reference_images` 仍是逐视角的独立图。
        这张设定表只是**展示层**（也是 `character_sheets` 这个新键的内容）。

        返回：本地文件路径；关闭时或失败时返回空串（**软失败，不阻断流程**）。
        """
        if not getattr(settings, "character_sheet_enabled", False):
            return ""

        appearance = str(profile.get("appearance") or "").strip()
        if not appearance:
            return ""

        from app.agents.stage2.character_sheet_template import (
            SHEET_NEGATIVE,
            build_sheet_prompt,
        )

        # 与逐视角图一样，从 state 的 run_context 定归档归属
        # （否则会落进 `_global/`，账号丢失）
        from app.services.runtime_settings import capture as _ctx_capture
        from app.services.runtime_settings import restore as _ctx_restore

        ctx = _ctx_capture()
        if state and state.get("run_context"):
            from app.core.context import RunContext

            ctx = RunContext.from_state(state).as_dict()
        _ctx_restore(ctx)

        prompt = build_sheet_prompt(name, appearance)
        w = int(getattr(settings, "character_sheet_width", 1920) or 1920)
        h = int(getattr(settings, "character_sheet_height", 1080) or 1080)
        try:
            _url, local_path, ok = get_image_service().generate(
                {
                    "shot_id": f"sheettable_{name}",
                    "prompt": prompt,
                    "negative_prompt": SHEET_NEGATIVE,
                    # 固定 seed：同名角色重跑得到同一张设定表
                    "seed": _seed_for(name, "sheettable"),
                    "width": w,
                    "height": h,
                },
                role="character_designer",
            )
            return local_path if ok and local_path else ""
        except Exception as exc:  # noqa: BLE001  设定表是附加交付物，不该拖垮流程
            logger.warning("[character_designer] 设定表生成失败（不影响其余流程）：%s", exc)
            return ""

    # ---------------------------------------------------------------
    # 2d + 新增方法：_select_best_reference
    # ---------------------------------------------------------------
    def _select_best_reference(self, sheet: List[dict]) -> str:
        """从多张生成图中挑**主参考图**。

        规则：取状态 ok 的图里，按 `_VIEW_ORDER` 优先级最靠前的
        （**正面全身优先** —— 正面信息最全，最适合给 IP-Adapter 当锚定）。
        都失败则返回空。

        （真正的"视觉质量/一致性"评估由 quality_assessor 负责，
        这里先按视图优先级选，保证至少有 1 张可用。）
        """
        ok = [s for s in sheet if s.get("ok") and s.get("url")]
        if not ok:
            return ""
        def _rank(s: dict) -> int:
            v = s.get("view") or s.get("pose") or ""
            v = _LEGACY_VIEW_ALIASES.get(v, v)
            return _VIEW_ORDER.index(v) if v in _VIEW_ORDER else 99
        ok.sort(key=_rank)
        return ok[0]["url"]


# 延迟导入，避免模块顶层循环依赖
def get_image_service():
    from app.services.image import get_image_service as _g

    return _g()
