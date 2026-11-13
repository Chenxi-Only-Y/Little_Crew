r"""角色设定板题面：**按四视图模板组装**（2026-11-13）。

模板来源：`D:\AI\ds.12\通用版-任意角色四视图模板.md` 与
`四视图角色设定智能体-系统提示词模板.md`（用户提供的两份工程化模板）。

## 为什么单独一个模块

- 题面是**产物质量的主要变量**，必须能单独改、单独测、单独 A/B；
- 它不碰"文字半"（`agent/character_sheet` 的封闭选项外观）——
  文字半保证**可复现**，本模块负责**把可复现的字段讲清楚**。

## 组装顺序（模板 §三，固定不改）

```
身份基准 → 布局 → 脸型骨相 → 五官 → 皮肤 → 发型 → 首饰
       → 服饰(上衣/下装/鞋) → 身材 → 灯光质感 → 负面词
```

## 两条纪律（模板里写死的）

1 **只写看得见的东西**：结构、比例、位置、材质、光向；
   不写"好看/高级/精致"这类无信息形容词（模板原话）。
2 **负面词每次带齐**：换脸 / 裁切 / 文字水印 / 娃娃脸磨皮塑料 四组，不许省略。

## 槽位来源

| 槽 | 来源 |
|---|---|
| 身份、发型、上衣、配色、标记 | **文字半**（`CharacterSheet.appearance`，封闭选项，可复现） |
| 脸型骨相、眼型、眉鼻唇、皮肤、下装、鞋、首饰 | 由 `character_id` 的哈希**确定性派生**（同一角色永远同一套） |
| 画风、灯光 | 参数传入（默认「高精度 3D 半写实国漫」+ 灰棚柔光） |
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

#: 通用骨架：构图 + 一致性句（模板 §一，一个字都别随便删）
_LAYOUT = (
    "【布局】横向 16:9 宽幅，干净的浅灰影棚背景，背景与地面自然衔接，无道具。"
    "左侧约占四成半为同一角色的大幅正面头肩特写：完整露出头部轮廓、面部、耳饰、脖颈与锁骨，"
    "头顶保留少量空间，不裁发顶/帽饰/角；角色正对镜头，头部端正，双眼平视，无歪头。"
    "右侧依次并列正面全身、严格侧面全身、背面全身三个视图，三者等高、脚底齐平，"
    "从头顶（含发饰/帽饰/角）到鞋底完整入画，不裁鞋子；站姿自然，双臂放松垂于体侧，无手势。"
    "四个视图必须属于同一角色：脸型、五官、肤色、发型、服装结构、配饰完全一致。"
)

#: 灯光与质感（同一份骨架，多角色摆一起才像同一套设定集）
_LIGHTING = (
    "【灯光质感】大面积柔光自正前上方照射，辅以极轻轮廓光；脚下仅有浅淡接触阴影；"
    "低饱和、克制配色；清晰细腻，材质细节可辨（织物纹理、金属高光、皮革哑光、发丝层次）；"
    "皮肤用次表面散射与局部肤色微差，高光克制；无文字、无水印、无标签、无边框、无信息栏。"
)

#: 负面词四组（模板要求每次都带齐）
NEGATIVE_GROUPS: dict[str, str] = {
    "换脸": "同一张脸被替换、四视图不是同一人、发型不一致、服装结构漂移",
    "裁切": "裁发顶、裁耳朵、裁脚、裁鞋底、半身截断、画面出血",
    "文字水印": "任何文字、水印、标签、边框、信息栏、色卡、日期戳",
    "畸形质感": "娃娃脸、婴儿肥、磨皮塑料肌、过度对称、多余手指、肢体粘连、解剖错误",
    # 面部细节是参考图的重点：这些是"脸垮/脸平/脸假"的具体形态
    "面部崩坏": "小眼睛、单眼皮、内眦赘皮过重、眼神呆滞无神、眼距过宽、鼻梁塌陷、"
               "嘴歪、牙齿外露、浓重假睫毛、厚重修容、油光满面、法令纹、面部发腮、"
               "五官比例失调、面部对称到失真",
}

#: 派生槽的封闭选项（确定性：由 character_id 哈希选，同一角色永不变）
#:
#: ⚠️ **这一批选项是按参考图改的**（2026-11-13，用户提供 10 张模板成品）：
#: 参考图的共同点不是服装，而是**面部细节密度** ——
#: 大而圆的眼睛、清晰双眼皮与卧蚕、根根分明的睫毛、淡粉腮红、
#: 雾面果冻唇、空气感碎刘海、瓷白通透肤色。旧选项只写"窄长鹅蛋脸/细长眼"，
#: 生成出来是"清秀但平"，密度不够。**面部密度是最能拉开档次的变量。**
_FACES = (
    "小而精致的鹅蛋脸，脸宽约 0.63 脸长；颧骨柔和内收，下颌线收紧、下巴小巧微尖；"
    "面颊有极轻的软组织感，饱满但不发腮",
    "小巧的瓜子脸，脸宽约 0.61 脸长；颧弓内收、下颌线纤细、下巴尖圆；"
    "苹果肌位置偏高，笑起来有极浅的卧蚕阴影",
    "柔和的鹅蛋脸，脸宽约 0.66 脸长；下颌圆润收窄、下巴略短而圆；"
    "面颊饱满，皮肤下隐约可见软组织体积",
    "窄长脸，脸宽约 0.62 脸长；颧骨骨点清楚、下颌角略方但收得利落；"
    "下巴略平，面部转折干净",
)
_EYES = (
    "偏大的杏眼，眼裂中等偏大、眼头略低，眼尾微下垂；清晰的双眼皮褶，"
    "下眼睑有淡淡卧蚕与极窄阴影；上睫毛根根分明、末端微翘；虹膜{eye_color}，"
    "瞳孔中央有一点高光；眼神明亮不呆滞",
    "圆润的大眼，眼裂偏大、眼尾平直；内双褶明显，卧蚕饱满；"
    "睫毛纤长清晰，眼头有极淡珠光；虹膜{eye_color}；眼神温柔有神",
    "狭长的猫眼，眼裂中等、眼尾上挑；双眼皮清晰、眼尾加一点上扬眼线；"
    "下睫毛短而密；虹膜{eye_color}；眼神清冷专注",
    "偏圆的鹿眼，眼裂大而圆、眼头圆润；双眼皮宽一点，卧蚕明显；"
    "睫毛浓密，眼尾略下垂显无辜；虹膜{eye_color}；眼神澄澈",
)
_BROWS = (
    "眉为细软雾眉，平直略弯、眉尾自然收尖，边缘有极细的绒毛感；"
    "鼻梁直挺、鼻头小巧带一点肉感；唇形小巧、唇峰清晰、唇珠微凸，"
    "哑光豆沙色，边缘自然柔和",
    "眉为远山眉，眉峰柔和、眉尾细长；鼻梁细挺、鼻翼窄；"
    "唇偏薄、唇峰利落，雾面粉调，微张一点",
    "眉为平直野生眉，眉尾松散；鼻头圆润小巧；"
    "唇形饱满、唇珠明显，果冻质感浅粉，自然闭合",
)
_MAKEUP = (
    "眼妆：眼尾用低饱和棕粉晕染，卧蚕用珠光细闪勾一条浅线；腮红：面颊与鼻头淡粉扫过，"
    "位置偏高、边界柔和；唇妆：哑光雾面、唇线柔和；无夸张假睫毛与浓重眼线",
    "眼妆：极淡的杏色打底、眼尾一点点红调；腮红：苹果肌与眼下淡粉；"
    "唇妆：果冻光泽、唇峰高光；妆面干净通透",
    "眼妆：冷调灰棕小烟熏只压眼尾；腮红：横向轻扫至眼下；"
    "唇妆：豆沙雾面、唇珠一点高光；整体克制",
)
_SKINS = (
    "冷白瓷肌，通透带极淡粉调；鼻梁与颧骨有极淡自然红；弱毛孔、不磨皮、"
    "面颊有细腻的皮肤微纹理与极浅的血色过渡",
    "奶白肌，脸颊有柔和血色与极淡粉颊；弱毛孔、通透不假面；"
    "眼下有极淡的自然阴影",
    "暖白瓷肌，面颊淡粉；毛孔极细、皮肤有次表面散射的通透感",
)
_JEWELRY = (
    "左耳一枚细银环，右耳一枚小耳钉",
    "耳饰为一对细银流苏，垂至下颌线",
    "左耳三枚细银环，右耳一枚素银耳骨夹",
    "耳饰为银色细链垂坠，末端一枚小星芒",
)
_BOTTOMS = (
    "同色系高腰阔腿长裤，腰线清晰，裤脚自然垂落",
    "同色系长裙，裙摆至脚踝，褶皱自然，外罩薄纱层",
    "束脚长裤，膝下缠灰白布绑腿，裤脚略收",
)
_SHOES = (
    "黑色圆头厚底皮鞋，鞋面哑光",
    "黑色薄底短靴，鞋头微翘",
    "米白绣花布鞋，鞋面有极淡的墨竹绣样",
)
_BUILDS = (
    "身形修长挺拔，肩背平直，腰细腿长，比例匀称",
    "身形高挑，肩线利落，腰细，腿长比例好",
    "身形匀称，肩线柔和，腰线清楚，体态自然",
)

#: **画风预设**（2026-11-13）：骨架不变，只换画风/质感/光照/负面词。
#:
#: 为什么做成预设而不是让用户自由填一句：同一套设定集要放在一起才像一套
#: —— 四个角色用四种随手写的画风词，摆在一起就是四个世界。
#: 预设保证**同一风格的质感描述一致**，换风格只换一个键。
STYLE_PRESETS: dict[str, dict[str, str]] = {
    "3d-guoman": {
        "label": "高精度 3D 半写实国漫（默认）",
        "prefix": "高精度 3D 半写实国漫角色四视图角色设定图，CG 建模质感",
        "look": "真实皮肤材质与次表面散射，五官按国漫审美美化但解剖可信；发丝为片状分层",
        "lighting": "大面积柔光自正前上方照射，辅以极轻轮廓光；脚下浅淡接触阴影",
        "negative": "塑料感、卡通粗描边、像素化、低模面数",
    },
    "2.5d-film": {
        "label": "2.5D 动画电影风格",
        "prefix": "2.5D 动画电影风格角色四视图设定图，动画电影概念设计",
        "look": ("赛璐璐色块 + 厚涂渐变的混合渲染；干净的明暗分界线与柔和过渡并存；"
                 "五官精致但保留手绘动画的造型感；发丝按动画分层处理"),
        "lighting": "电影感布光：主光柔和偏侧、边缘光明显、背景有极淡的氛围渐层与光晕",
        "negative": "PBR 金属反光、写实毛孔、CG 塑料脸、真人照片感、过度3D高光",
    },
    "painting-cg": {
        "label": "绘画感 CG（厚涂概念设计）",
        "prefix": "绘画感 CG 角色四视图设定图，厚涂概念设计稿",
        "look": ("可见的厚涂笔触与色彩堆叠（不是照片）；边缘有画笔起收的松动感；"
                 "色块区分明、略带纸面/画布颗粒；五官靠明暗体积塑造"),
        "lighting": "统一的绘画性光源，暖冷对比明确，阴影处带色相变化而非纯黑",
        "negative": "照片写实、3D 塑料高光、平滑渐变、线条描边、像素噪点",
    },
    "stylized-real": {
        "label": "风格化写实（比例风格化 + 材质写实）",
        "prefix": "风格化写实角色四视图设定图，stylized realism",
        "look": ("比例与头身比适度风格化（眼略大、脸偏小），但材质与光照按写实标准："
                 "真实布料纤维、皮革纹理、金属反射；皮肤半透光"),
        "lighting": "写实影棚光：主光 + 柔光箱补光 + 轮廓光；接触阴影与材质高光分明",
        "negative": "夸张卡通比例、简化材质、纯色平涂、塑料玩具感",
    },
    "nextgen-game": {
        "label": "次世代游戏角色（高模 + PBR）",
        "prefix": "次世代游戏角色四视图设定图，3A 游戏角色概念，高模烘焙级细节",
        "look": ("PBR 材质四件套：布料纤维/皮革纹理/金属反射/皮肤次表面；"
                 "模型边缘有真实厚度与倒角，接缝与缝线可见；装备有功能性结构细节"),
        "lighting": "游戏引擎级三点布光 + 环境光照（HDRI 灰棚），AO 阴影清晰，高光可控",
        "negative": "卡通渲染、手绘笔触、低模方块、塑料质感、无细节平涂",
    },
}

#: 用户常用简称 -> 预设键
_STYLE_ALIASES = {
    "3d": "3d-guoman", "国漫": "3d-guoman", "默认": "3d-guoman",
    "2.5d": "2.5d-film", "25d": "2.5d-film", "动画电影": "2.5d-film",
    "动画": "2.5d-film", "film": "2.5d-film",
    "painting": "painting-cg", "厚涂": "painting-cg", "绘画感": "painting-cg",
    "cg": "painting-cg",
    "风格化写实": "stylized-real", "stylized": "stylized-real", "写实": "stylized-real",
    "nextgen": "nextgen-game", "次世代": "nextgen-game", "3a": "nextgen-game",
    "游戏": "nextgen-game",
}


def resolve_style(name: str) -> dict[str, str]:
    """把用户输入解析成风格预设（未知则视为自定义画风前缀）。"""
    key = str(name or "").strip().lower()
    key = _STYLE_ALIASES.get(key, key)
    if key in STYLE_PRESETS:
        return dict(STYLE_PRESETS[key])
    base = dict(STYLE_PRESETS["3d-guoman"])
    if key:
        base["label"] = name
        base["prefix"] = str(name).strip()
    return base


#: 画风默认值（模板 §二 槽 2）
#: 兼容旧调用：默认画风 = `3d-guoman` 预设
DEFAULT_STYLE = STYLE_PRESETS["3d-guoman"]["prefix"]


def _h(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)


def _pick(options: tuple[str, ...], seed: str, salt: str) -> str:
    return options[_h(f"{seed}|{salt}") % len(options)]


def _grab(app: Mapping[str, Any], *keys: str) -> list[str]:
    """从外观字段里按顺序取非空值（字段名中文/英文都试一遍）。"""
    out = []
    for k in keys:
        cands = (k,) + tuple(_ALIASES.get(k, ()))
        for c in cands:
            v = app.get(c)
            if v and str(v) not in out:
                out.append(str(v))
                break
    return out


#: 中文槽位名 → 文字半的字段名（**两种命名都认**）
#:
#: 文字半的封闭选项字段是 `hair_style / hair_color / eye_color / outfit /
#: outfit_accent / signature / build / age_band / gender`；
#: 但手工写的角色卡常用 `hair / outfit / palette / mark`。
#: 早期只认前者，后果是"手工卡里的发型被丢掉、题面退化成随机发型"（实测踩到）。
_ALIASES = {
    "发型": ("hair_style", "hair"),
    "发色": ("hair_color",),
    "瞳色": ("eye_color",),
    "服装": ("outfit",),
    "服装点缀": ("outfit_accent",),
    "标记": ("signature", "mark"),
    "细节": ("mark",),
    "身形": ("build",),
    "年龄": ("age_band",),
    "性别": ("gender",),
    "配色": ("palette",),
}


def slots_for(sheet: Any, *, style: str = "") -> dict[str, str]:
    """把一个角色的外观字段填进模板的 12 个槽（确定性派生）。"""
    app = getattr(sheet, "appearance", {}) or {}
    if not isinstance(app, Mapping):
        app = {}
    cid = str(getattr(sheet, "character_id", "") or "CHAR")
    name = str(getattr(sheet, "name", "") or "角色")

    gender = (app.get("gender") or app.get("性别") or "女性")
    age = (app.get("age_band") or app.get("年龄") or "青年")
    identity = f"{age}{gender}，{name}"

    hair_bits = _grab(app, "发色") + _grab(app, "发型")
    hair = "，".join(hair_bits) if hair_bits else _pick(
        ("墨黑长发，高马尾", "深栗棕长卷发，中分", "银灰短发，两侧剃短"), cid, "hair")
    eye_color = (app.get("eye_color") or app.get("瞳色") or "灰棕")
    top_bits = _grab(app, "服装")
    accent = _grab(app, "服装点缀")
    top = "，".join(top_bits + accent) if top_bits else _pick(
        ("青灰交领窄袖短打，棉麻质感", "灰紫细罗纹露肩短上衣", "炭灰战术背心"), cid, "top")
    sig = _grab(app, "标记") + _grab(app, "signature")
    build = (app.get("build") or app.get("身形") or _pick(_BUILDS, cid, "build"))

    palette = (app.get("palette") or app.get("配色") or "")
    style_preset = resolve_style(style) if style else resolve_style("3d-guoman")
    return {
        "配色": str(palette),
        "身份": identity,
        "画风": style_preset["prefix"],
        "风格描述": style_preset["look"],
        "风格光照": style_preset["lighting"],
        "风格负面": style_preset["negative"],
        "脸型骨相": _pick(_FACES, cid, "face"),
        "眼睛": _pick(_EYES, cid, "eyes").format(eye_color=eye_color),
        "眉鼻唇": _pick(_BROWS, cid, "brows"),
        "妆容": _pick(_MAKEUP, cid, "makeup"),
        "皮肤": _pick(_SKINS, cid, "skin"),
        # 发型要有"空气感"：参考图里最抓眼的不是发型本身，而是碎发与蓬松度
        "发型": (hair + "；发丝有空气感与蓬松层次，额前碎刘海自然分开、"
                        "鬓角与颈侧留少量碎发贴脸，发尾有轻微飞舞感"),
        "细节": "，".join(sig),
        "首饰": _pick(_JEWELRY, cid, "jewelry"),
        "上衣": top,
        "下装": _pick(_BOTTOMS, cid, "bottom"),
        "鞋": _pick(_SHOES, cid, "shoes"),
        "身材": build,
        "场景光": "浅灰影棚、正前上方大面积柔光、极轻轮廓光、低饱和冷灰",
    }


def negative_words(*, style_negative: str = "") -> str:
    """负面词（模板四组 + 面部崩坏 + 当前风格的专属否定项）。"""
    groups = list(NEGATIVE_GROUPS.values())
    if style_negative:
        groups.append(style_negative)
    return "【负面词】不要：" + "；".join(groups) + "。"


def build_prompt(sheet: Any, *, style: str = "") -> str:
    """按模板 §三 的顺序组装完整题面。"""
    s = slots_for(sheet, style=style)
    return (
        f"{s['画风']}。风格要点：{s['风格描述']}。\n"
        f"【身份基准】{s['身份']}，{s['脸型骨相']}。\n"
        f"{_LAYOUT}\n"
        f"【面部细节·最高优先级】面部占画面信息密度的大头，必须精细：\n"
        f"  眼睛 {s['眼睛']}。\n"
        f"  眉鼻唇 {s['眉鼻唇']}。\n"
        f"  妆容 {s['妆容']}。\n"
        f"【皮肤】{s['皮肤']}。\n"
        f"【发型】{s['发型']}。\n"
        f"【首饰】{s['首饰']}。\n"
        f"【服饰】上衣：{s['上衣']}。下装：{s['下装']}。鞋：{s['鞋']}。"
        + (f"配色：{s['配色']}。" if s.get("配色") else "")
        + (f"细节：{s['细节']}（必须画出来）。" if s.get("细节") else "")
        + "\n"
        f"【身材】{s['身材']}。\n"
        f"【灯光质感】{s['风格光照']}；脚下仅有浅淡接触阴影；低饱和、克制配色；"
        f"材质细节可辨；无文字、无水印、无标签、无边框、无信息栏。\n"
        f"【场景光】{s['场景光']}。\n"
        f"{negative_words(style_negative=s.get('风格负面', ''))}"
    )


#: 质检表（模板 §六）。生成模型不保证成立，**必须看到成图再下结论**。
QC_CHECKLIST: tuple[str, ...] = (
    "四个视图都在，无人被裁切（头顶/鞋底/手指）",
    "左特写占比约 45%，右侧三视图等高、脚底齐平",
    "正面/侧面/背面朝向正确（侧面是真侧面，背面是真背面）",
    "同一张脸、同一套发型、同一套服装结构、同一副耳饰",
    "上衣领口/肩带/露肩侧/抽绳位置前后一致",
    "裤腰高度、腰带扣、结构片方向、垂带长度一致",
    "鞋型与鞋底厚度一致",
    "无文字、无水印、无信息栏残留",
    "背景干净、无杂物、接触阴影自然",
    "解剖可信（手指数量、肩颈连接、脚踝方向）",
    "面部细节达标：眼睛有神、睫毛与卧蚕清晰、妆面干净、肤色通透",
    "面部四视图为同一张脸（脸型/眼型/鼻唇/妆容一致）",
)


def manifest_extra(sheet: Any, *, style: str = "") -> dict[str, Any]:
    """给 `sheet.json` 的附加信息：槽位快照 + 质检表（供人工/后续 VLM 复核）。"""
    return {"slots": slots_for(sheet, style=style),
            "negativeGroups": list(NEGATIVE_GROUPS),
            "qcChecklist": list(QC_CHECKLIST),
            "notVerified": "四视图一致性需**看成品**判断；本清单未自动核验"}
