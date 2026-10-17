"""角色定妆表 —— 角色资产的「文字半」。

它在链路里的位置
----------------
```
剧本素材
   ↓ 角色设计师（本模块）
character.sheet          ← 可复现的外观描述（文字）
   ↓ 【待做】锚点图生成
character.anchor_set     ← 锚点图（图片）
   ↓
identity.reference / identity.lora  ← 参考图注入 / LoRA
```

**为什么必须先有文字半**：图像半（锚点图）需要文字半作为输入 ——
否则生成的每张锚点图之间就不一致，**锚点本身失去意义**。

⚠️ 本模块**只产出文字半**。它不生成图 —— 本机没有图像条件能力
（见策划书 §5.8）。这**不是**偷懒，是职责边界：
文字描述是 LLM 的活，锚点图是图像模型的活。

两条不可违背的规则
------------------
1. **外观从固定选项里"选"，不"创作"。**
   与 `ir_bridge` 的运镜/景别表同一思路：LLM 只做选择，不产生自由文本。
   自由文本会让"同一角色"在两次生成里得到不同描述 —— 那正是要消灭的问题。

2. **同一角色名 + 同一故事种子 → 必定得到同一外观。**
   确定性用摘要函数实现，不用随机数。
   否则"重跑一集"会让角色换脸，而那看起来像模型问题，其实是我们的问题。

**为什么确定性在这里是硬要求**：平台的缓存键是内容哈希。
外观若不确定，`irHash` 每次都会变 —— 缓存永不命中，且**无法察觉**
（它只是"每次都重跑"，看起来像正常行为）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

__all__ = [
    "Appearance",
    "CharacterSheet",
    "CharacterLibrary",
    "APPEARANCE_OPTIONS",
    "design_appearance",
    "CharacterDesignerRules",
    "character_id_for",
    "script_text",
]


# ==========================================================================
# 外观参数空间
# ==========================================================================
#
# 每一项都是**封闭选项集**，不是自由文本。理由见模块 docstring 规则 1。
# 选项刻意写得足够具体（"齐肩黑直发"而不是"黑发"），
# 因为图像模型对具体描述的遵循度明显更好。

APPEARANCE_OPTIONS: Mapping[str, tuple[str, ...]] = {
    "hair_style": (
        "及腰直长发", "齐肩内扣短发", "高马尾", "低盘发",
        "蓬松微卷中长发", "利落齐耳短发", "侧分长直发", "编发双辫",
    ),
    "hair_color": (
        "乌黑", "深栗棕", "浅亚麻", "银灰", "暗红棕", "墨绿挑染",
    ),
    "eye_color": (
        "深褐", "琥珀", "灰蓝", "墨绿", "浅灰", "酒红",
    ),
    "build": (
        "纤细高挑", "娇小匀称", "结实挺拔", "清瘦文弱", "丰盈端庄",
    ),
    "outfit": (
        "米色长风衣", "深蓝粗布工装", "白色衬衫配马甲", "亚麻长裙",
        "深灰呢料外套", "靛蓝染布短褂", "素色长袍系腰带",
    ),
    "outfit_accent": (
        "领口一枚铜扣", "腰间系麻绳", "袖口卷起两道", "肩头搭一匹短披肩",
        "胸前别一支干花", "下摆绣一道细边",
    ),
    "signature": (
        "左手腕有一道旧疤", "右耳戴一只小银环", "颈间挂着细绳坠子",
        "眉间有一颗小痣", "鬓角有一缕天生的浅色发", "惯常把袖口捏在手里",
    ),
    "age_band": (
        "少年", "青年", "中年", "老年",
    ),
}

#: 选项键的固定顺序。**顺序必须固定** —— 否则字典遍历顺序变化会让
#: 同一个摘要落到不同的选项上，确定性就没了。
OPTION_KEYS: tuple[str, ...] = tuple(APPEARANCE_OPTIONS)


# ==========================================================================
# 从角色名推断的语义线索
# ==========================================================================
#
# ⚠️ 这一节的存在理由值得写下来。
#
# 早期版本对每个槽位**均匀随机**取样，于是产出了：
#
#     老奶奶  ->  青年，深栗棕高马尾，酒红眼睛
#     父亲    ->  中年，墨绿挑染【编发双辫】
#
# 确定性做到了，但**荒唐**。而"老奶奶是青年、父亲扎双辫"这种错误
# 比不一致更伤 —— 不一致看起来像模型的问题，荒唐看起来像我们没动脑子。
#
# 修法不是"加更多选项"，而是**利用已经存在的信息**：
# 称呼本身编码了年龄与性别，直接从名字里读出来。
#
# 这与 `ir_bridge` 用封闭表把"运镜"映射成提示词是同一思路 ——
# **能确定的部分不要交给随机。**

#: 称呼 -> 年龄段。取用时**长称呼优先**（"老奶奶"要先于"奶奶"命中）。
_TITLE_AGE: Mapping[str, str] = {
    "曾祖母": "老年", "曾祖父": "老年", "老奶奶": "老年", "老爷爷": "老年",
    "奶奶": "老年", "爷爷": "老年", "姥姥": "老年", "外婆": "老年",
    "外公": "老年", "祖母": "老年", "祖父": "老年", "婆婆": "老年",
    "大爷": "老年", "阿婆": "老年", "阿公": "老年", "长老": "老年",
    "父亲": "中年", "母亲": "中年", "爸爸": "中年", "妈妈": "中年",
    "爹": "中年", "娘": "中年", "叔叔": "中年", "阿姨": "中年",
    "伯伯": "中年", "婶婶": "中年", "舅舅": "中年", "姑妈": "中年",
    "师父": "中年", "掌柜": "中年", "村长": "中年", "族长": "中年",
    "农夫": "中年",
    "哥哥": "青年", "姐姐": "青年", "兄长": "青年", "嫂嫂": "青年",
    "青年": "青年", "旅人": "青年", "书生": "青年", "猎人": "青年",
    "弟弟": "少年", "妹妹": "少年", "少年": "少年", "少女": "少年",
    "男孩": "少年", "女孩": "少年", "孩童": "少年", "孩子": "少年",
}

#: 称呼 -> 性别倾向。只用于**排除明显不合的选项**，不做"必须如此"的强约束 ——
#: 剧本里完全可能有女猎人、男护士，硬绑会把多样性压没。
_TITLE_GENDER: Mapping[str, str] = {
    "少女": "f", "女孩": "f", "奶奶": "f", "老奶奶": "f", "母亲": "f",
    "妈妈": "f", "姐姐": "f", "妹妹": "f", "阿姨": "f", "婶婶": "f",
    "姑妈": "f", "婆婆": "f", "阿婆": "f", "嫂嫂": "f", "娘": "f",
    "姥姥": "f", "外婆": "f", "祖母": "f", "曾祖母": "f",
    "少年": "m", "男孩": "m", "爷爷": "m", "老爷爷": "m", "父亲": "m",
    "爸爸": "m", "哥哥": "m", "弟弟": "m", "叔叔": "m", "伯伯": "m",
    "舅舅": "m", "爹": "m", "大爷": "m", "阿公": "m", "外公": "m",
    "祖父": "m", "曾祖父": "m",
}

#: 性别明确时**可以排除**的选项。
#:
#: 刻意只列"明显不合"的少数项，不做全量划分 —— 划分越细，多样性损失越大，
#: 而多样性正是"多个角色各不相同"的来源。
_GENDER_BANNED: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "f": {"hair_style": ("编发双辫", "利落齐耳短发")},
    "m": {"hair_style": ("编发双辫", "低盘发")},
}

#: 年龄明确时**可以排除**的发型。
#:
#: ⚠️ 这是**启发式，不是事实**：现实中老年人当然可能扎马尾、留长发。
#: 它存在的理由是"高马尾 + 老年"会被读成**设计事故**而不是角色特征 ——
#: 而一个看起来像事故的定妆表，没人会认真对待。
#:
#: 想突破它很容易：在 `CharacterDesignerRules.overrides` 里直接指定。
#: **启发式的默认值可以被人工覆盖**，这是它敢存在的前提。
_AGE_BANNED: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "老年": {"hair_style": ("高马尾", "编发双辫", "及腰直长发")},
    "少年": {"hair_style": ("低盘发",)},
}


def _age_hint(name: str) -> str:
    """从角色名里读出年龄线索。**长称呼优先** —— "老奶奶"必须比"奶奶"先命中。"""
    for title in sorted(_TITLE_AGE, key=len, reverse=True):
        if title in name:
            return _TITLE_AGE[title]
    return ""


def _gender_hint(name: str) -> str:
    """从角色名里读出性别线索。空串表示"名字没表态"。"""
    for title in sorted(_TITLE_GENDER, key=len, reverse=True):
        if title in name:
            return _TITLE_GENDER[title]
    return ""


def _options_for(key: str, name: str, *, age: str = "") -> tuple[str, ...]:
    """给定槽位、角色名与已知年龄，返回**可用的选项**。

    按语义线索收窄：性别排除明显不合的，年龄再排除一层。

    收窄到空集时退回全集 —— **宁可给出一个不合的选项，
    也不要因为收窄过度而拿不到任何值**（那会让整条链断掉）。
    """
    opts = APPEARANCE_OPTIONS[key]
    banned: set[str] = set()

    g = _gender_hint(name)
    if g:
        banned.update(_GENDER_BANNED.get(g, {}).get(key, ()))

    if age:
        banned.update(_AGE_BANNED.get(age, {}).get(key, ()))

    if not banned:
        return opts
    kept = tuple(o for o in opts if o not in banned)
    return kept or opts


# ==========================================================================
# 外观
# ==========================================================================

@dataclass(frozen=True)
class Appearance:
    """一个角色的外观参数。**全部来自封闭选项集。**"""

    hair_style: str = ""
    hair_color: str = ""
    eye_color: str = ""
    build: str = ""
    outfit: str = ""
    outfit_accent: str = ""
    signature: str = ""
    age_band: str = ""

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in OPTION_KEYS}

    def prompt_fragment(self) -> str:
        """生成写进**每一镜提示词**的外观描述。

        ⚠️ 这是本模块与图像链的**唯一接口**。
        它必须**稳定**（同一角色任何镜头都得到同一串字符），
        否则角色会跨镜变化，而且是不可复现地变。
        """
        parts = [
            f"{self.age_band}",
            f"{self.build}身形",
            f"{self.hair_color}{self.hair_style}",
            f"{self.eye_color}眼睛",
            f"身穿{self.outfit}",
            f"{self.outfit_accent}",
            f"{self.signature}",
        ]
        return "，".join(p for p in parts if p.strip("，"))

    def describe_lines(self) -> tuple[str, ...]:
        """给人看的逐项描述（用于审查与文档）。"""
        labels = {
            "age_band": "年龄段", "build": "体态", "hair_style": "发型",
            "hair_color": "发色", "eye_color": "瞳色", "outfit": "服装",
            "outfit_accent": "服装细节", "signature": "标志特征",
        }
        return tuple(f"{labels[k]}：{getattr(self, k)}" for k in OPTION_KEYS)


# ==========================================================================
# 确定性设计
# ==========================================================================

def _pick(options: Sequence[str], digest: str, slot: int) -> str:
    """从选项里**确定性地**取一项。

    用摘要的**不同片段**给不同槽位取样，而不是拿一个数反复取模 ——
    后者会让多个槽位**同步变化**（都随同一个数走），
    于是"换一个故事种子"会得到整批高度相关的角色，多样性大打折扣。
    """
    # 每个槽位取 8 个十六进制字符（32 位），足够避免同槽内碰撞
    chunk = digest[slot * 8:(slot + 1) * 8] or digest[:8]
    return options[int(chunk, 16) % len(options)]


def design_appearance(name: str, *, story_seed: str = "") -> Appearance:
    """由角色名与故事种子**确定性地**导出一套外观。

    同 ``(name, story_seed)`` 必定得到同一结果 —— 这是缓存正确性的前提。

    ``story_seed`` 让"同一角色名出现在不同故事里"能得到不同外观
    （例如两集都叫"少女"，但那是两个人）。

    **角色名同时参与决策，不只是参与摘要**：
    称呼里编码的年龄与性别会收窄选项集（见 `_age_hint` / `_gender_hint`）。
    所以"老奶奶"不会得到"少年"，"父亲"不会扎双辫 ——
    这不是额外规则，是**本来就在名字里的信息**，不用白不用。
    """
    digest = hashlib.sha256(
        f"{name}\x00{story_seed}".encode("utf-8")).hexdigest()

    age = _age_hint(name)
    vals: dict[str, str] = {}
    for i, k in enumerate(OPTION_KEYS):
        opts = _options_for(k, name, age=age)
        if k == "age_band" and age:
            # 称呼明确了年龄就**直接采用**，不抽样。
            # 只收窄到"同一档"是不够的 —— 奶奶就是老年，没有第二种可能。
            vals[k] = age
        else:
            vals[k] = _pick(opts, digest, i)
    return Appearance(**vals)


def character_id_for(name: str) -> str:
    """角色名 → 资产 id。

    用 ``CHAR-`` + 名字摘要前 4 位十六进制（大写）。
    与 `_characters_of` 里 ``CHAR-\\d{4}`` 的形态一致 ——
    所以定妆表产出的 id 能被剧本解析器**重新认出来**，
    不需要额外维护一张映射表。
    """
    h = hashlib.sha256(name.encode("utf-8")).hexdigest()[:4].upper()
    return f"CHAR-{h}"


def script_text(source: Any) -> str:
    """从各种形态的剧本输入里取出**可解析的文本**。

    为什么要这个函数：上游交给角色设计师的可能是
    ``str``（原始素材）、``ScriptDraft``（编剧的定稿）或场次序列。
    早期版本直接 ``str(draft)`` —— 那得到的是 **dataclass 的 repr**，
    恰好包含 ``@角色名``，于是"能用"。

    **"靠 repr 恰好能用"是最不该留的一类依赖**：
    dataclass 加一个字段、改一次 ``__repr__``，它就静默失效，
    而失效的表现是"角色识别不出来"，看起来像剧本的问题。

    所以这里显式重建文本，用场次的正式字段。
    """
    if source is None:
        return ""
    if isinstance(source, str):
        return source

    # ScriptDraft 及同类：有 scenes 字段
    scenes = getattr(source, "scenes", None)
    if scenes is None and isinstance(source, Mapping):
        scenes = source.get("scenes")
    if scenes:
        lines: list[str] = []
        for i, sc in enumerate(scenes, 1):
            if isinstance(sc, Mapping):
                heading = str(sc.get("heading") or sc.get("id") or i)
                body = str(sc.get("text") or "")
                cast = sc.get("characters") or ()
            else:
                heading = str(getattr(sc, "heading", None)
                              or getattr(sc, "id", None) or i)
                body = str(getattr(sc, "text", "") or "")
                cast = getattr(sc, "characters", ()) or ()
            lines.append(f"[场{i}] {heading}")
            # ⚠️ **必须把结构化的人物表渲染成 `@名字` 标记。**
            #
            # 踩过的坑（2026-10-17）：这里原本只写 heading + body，
            # 于是 `characters` 字段被**丢掉**了。而下游
            # `_characters_of()` 只认文本里的 `@名字` / `CHAR-\d{4}` ——
            # 它读不到结构化字段。
            #
            # 后果**极隐蔽**：真实 LLM 的剧本有 `characters: ["少女", …]`，
            # 但压成文本后没有任何标记 -> 定妆表识别出 **0 个角色** ->
            # `_with_appearance()` 静默跳过（它对认不出的名字 `continue`）->
            # **每镜提示词里没有任何外观信息**，角色一致性在文字层彻底丢失。
            # 而且不报任何错、测试也全过（桩路径用原始素材，不受影响）。
            #
            # 这条函数是"把各种形态压成**可解析文本**"，那它就该产出
            # 下游解析器真正认得的形式（本文件只此一处调用，改动面最小）。
            cast_names = [str(c).strip() for c in cast if str(c).strip()]
            if cast_names:
                lines.append("　".join(f"@{n}" for n in cast_names))
            lines.append(body)
            lines.append("")
        return "\n".join(lines).strip()

    # 退路：有 text 字段就用它
    t = getattr(source, "text", None)
    if isinstance(t, str) and t.strip():
        return t
    return str(source)


# ==========================================================================
# 定妆表
# ==========================================================================

@dataclass
class CharacterSheet:
    """一个角色的定妆表 —— 「文字半」的完整产出。"""

    name: str
    character_id: str
    appearance: Appearance
    #: 这个角色出现在哪些场次（审片用）
    scenes: tuple[str, ...] = ()
    #: 别名（同一角色的不同称呼，如"少女"/"她"）
    aliases: tuple[str, ...] = ()
    #: 人工覆盖的痕迹 —— 若被覆盖过，记下来（可追溯）
    overridden: tuple[str, ...] = ()
    notes: str = ""

    @property
    def prompt_fragment(self) -> str:
        return self.appearance.prompt_fragment()

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "character.sheet",
            "name": self.name,
            "characterId": self.character_id,
            "appearance": self.appearance.as_dict(),
            "promptFragment": self.prompt_fragment,
            "scenes": list(self.scenes),
            "aliases": list(self.aliases),
            "overridden": list(self.overridden),
            "notes": self.notes,
            # 明确标出还缺什么 —— 与成片清单同一原则：把缺口写在交付物里
            "pending": ["anchorSet"],
            "pendingReason": "锚点图需要图像条件能力；本机无 IPAdapter，"
                             "Z-Image 为纯文本版（见策划书 §5.8）",
        }


@dataclass
class CharacterLibrary:
    """一集的角色库。"""

    episode_id: str = ""
    sheets: tuple[CharacterSheet, ...] = ()
    seed: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "character.library",
            "episodeId": self.episode_id,
            "seed": self.seed,
            "count": len(self.sheets),
            "sheets": [s.as_dict() for s in self.sheets],
        }

    def by_name(self, name: str) -> CharacterSheet | None:
        for s in self.sheets:
            if s.name == name or name in s.aliases:
                return s
        return None

    def by_id(self, cid: str) -> CharacterSheet | None:
        for s in self.sheets:
            if s.character_id == cid:
                return s
        return None

    def prompt_prefix_for(self, cid: str) -> str:
        """给定角色 id，返回应写进提示词的外观前缀。**空串表示查无此人。**"""
        s = self.by_id(cid)
        return s.prompt_fragment if s else ""

    def ids(self) -> tuple[str, ...]:
        return tuple(s.character_id for s in self.sheets)


# ==========================================================================
# 角色设计师（规则桩）
# ==========================================================================

class CharacterDesignerRules:
    """角色设计师的规则桩。

    产出 ``character.sheet``（文字半）。

    **为什么不是 LLM**：外观是**从封闭选项里选**，不是创作。
    规则桩在这里不只是"够用"，而是**更正确** ——
    它天然满足"同角色同外观"这条硬要求，而 LLM 需要额外约束才能保证。

    等接上真实 LLM 时，它应当做的是**在同一套选项集上做选择**，
    而不是自由发挥外观描述。
    """

    #: 人工覆盖：``{角色名: {字段: 值}}``。给了就以它为准。
    #:
    #: 这是"设计"真正发生的地方 —— 规则桩给的是**可复现的初稿**，
    #: 导演要改某个角色的发型，就写在这里。
    overrides: Mapping[str, Mapping[str, str]] = {}

    def __init__(self, *, overrides: Mapping[str, Mapping[str, str]] | None = None,
                 seed: str = "") -> None:
        self.overrides = dict(overrides or {})
        self.seed = seed

    # ---- 主流程 --------------------------------------------------------

    def design(self, source: Any, *, episode_id: str = "",
               seed: str = "") -> CharacterLibrary:
        """从剧本素材产出角色库。

        ``source`` 可以是原始文本、``ScriptDraft`` 或场次序列 ——
        由 :func:`script_text` 统一取值（**不靠 repr**）。

        ``seed`` 缺省时用剧本内容的摘要 ——
        于是"同一份剧本"总是得到同一批角色，而"改过的剧本"会重新设计。
        **这正是缓存想要的行为。**
        """
        from little_crew.agent.crew_stubs import _characters_of, _parse_scenes

        text = script_text(source)
        if not seed:
            seed = self.seed or hashlib.sha256(
                text.encode("utf-8")).hexdigest()[:16]

        scenes = _parse_scenes(text, episode_id or "E001")

        # 角色 -> 出现过的场次
        found: dict[str, list[str]] = {}
        for sc in scenes:
            sc_id = str(sc.get("sceneId") or sc.get("id") or "")
            for nm in _characters_of(sc):
                found.setdefault(nm, [])
                if sc_id and sc_id not in found[nm]:
                    found[nm].append(sc_id)

        sheets: list[CharacterSheet] = []
        for name, scene_ids in found.items():
            appearance = design_appearance(name, story_seed=seed)
            overridden: list[str] = []
            ov = self.overrides.get(name) or {}
            if ov:
                # 只接受**在选项集内**的值。越界的值会被拒绝并记录 ——
                # 静默接受一个不存在的选项，会让"定妆表"与实际产出脱节。
                valid: dict[str, str] = {}
                for k, v in ov.items():
                    if k not in APPEARANCE_OPTIONS:
                        continue
                    if v in APPEARANCE_OPTIONS[k]:
                        valid[k] = v
                        overridden.append(k)
                if valid:
                    appearance = Appearance(**{
                        **appearance.as_dict(), **valid})

            cid = character_id_for(name)
            sheets.append(CharacterSheet(
                name=name, character_id=cid, appearance=appearance,
                scenes=tuple(scene_ids),
                overridden=tuple(overridden),
                notes=("外观已人工指定：" + "、".join(overridden)
                       if overridden else "")))

        # 输出顺序必须稳定 —— 否则库的内容哈希会随解析顺序漂移
        sheets.sort(key=lambda s: s.character_id)
        return CharacterLibrary(episode_id=episode_id or "E001",
                                sheets=tuple(sheets), seed=seed)

    # ---- 供编排器调用的 impl -------------------------------------------

    def __call__(self, *, envelope: Any, runtime: Any) -> Any:
        """编排器入口：产出 ``character.sheet``。

        与其它桩同一契约：入参在 ``envelope.payload``。
        """
        from little_crew.agent.protocol import CallResult

        payload = dict(getattr(envelope, "payload", {}) or {})
        source = (payload.get("source.novel")
                  or payload.get("script.final") or "")
        if not source:
            return CallResult.fail(
                "E-CHAR-NO-SOURCE", "缺少剧本素材（source.novel / script.final）")

        lib = self.design(source, episode_id=str(payload.get("episodeId", "")))
        return CallResult.success(
            lib,
            evidence=(f"识别出 {len(lib.sheets)} 个角色："
                      + "、".join(s.name for s in lib.sheets),))


# ==========================================================================
# 与 IR 的连接
# ==========================================================================

def library_digest(lib: CharacterLibrary) -> str:
    """角色库的内容摘要 —— 可作缓存键的一部分。

    改了任一角色的外观，这个摘要就会变，
    于是**所有相关镜头的 irHash 都会变**、缓存正确地失效。
    **这正是 `invalidate_on_anchor_change` 想表达的行为**，
    只不过在图像半就绪之前，变化源是文字半。
    """
    blob = json.dumps(lib.as_dict(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
