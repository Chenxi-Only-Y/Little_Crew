"""角色设定表提示词模板（用户 2026-10-07 定稿的固定格式）。

## 版式（不许改）

```
┌──────────────┬─────────┬─────────┬─────────┐
│              │         │         │         │
│  正脸放大     │  正面   │  侧面   │  背面   │
│  （≈1/3 宽）  │  全身   │  全身   │  全身   │
│              │         │         │         │
└──────────────┴─────────┴─────────┴─────────┘
                   16 : 9
```

## 为什么用**一次生成 16:9**，而不是"4 张独立生成 + 拼接"

两条路都实跑过（2026-10-07）：

| | 一次生成 16:9 | 4 张独立 + 拼接 |
|---|---|---|
| 布局 | ✅ 一次到位，完全符合版式 | ⚠️ 要逐格写死取景才能对齐，否则脸图成中景、三格大小不一 |
| 一致性 | ✅ **同一张图里生成，天然同一人** | ⚠️ 四张独立生成 → 脸/服装会漂（实测侧视像另一个人） |
| 细节 | 中等（每格 ~250px） | 高（每格原图 768×1024） |
| 耗时 | **29 秒 / 1 张** | 28 秒 × 4 = **112 秒** |

**结论**：设定表用**一次生成** —— 用户要的是一张「给人看的设定图」，
版式准确 + 一致性 比单格像素更重要。要单张高清参考图时另走
`character_designer` 的单视角路径（或 `character_sheet.compose_character_sheet` 拼接）。

## 硬约束（漏一条就出不来）

1. **同一角色、同一套发型和服装** —— 必须明写，否则四个视角会变成四个人
2. **站姿自然、双手自然下垂** —— 否则每格姿势乱、不像设定图
3. **发夹/配饰位置逐视角保持一致** —— 不一致等于资产作废
4. **浅灰偏白影棚背景 + 柔和自然阴影 + 大量留白** —— 拼图时底色统一

## 实测记录

  · 尺寸 1216×688（16:9）→ 每格约 250px 宽；**29~42 秒**
  · 侧脸要**写足**：只写 `side view, profile` 会退化成 3/4 角
  · 负面词里必须有 `text/letters/words/chinese characters` ——
    否则模型会往图上写标注，而它写不对中文，产出乱码
"""

from __future__ import annotations

#: 设定表尺寸（16:9）。1216×688 实测每格约 250px，够看清版式；
#: 想更清楚可以给 1820×1024（更慢）。
SHEET_SIZE = (1216, 688)

#: 固定版式模板。占位符只有 3 个：`{name}` / `{appearance}` / `{style}`。
SHEET_TEMPLATE = """生成一张角色设定图（character design sheet），横幅 16:9 构图。
画面布局：左侧是一张占画面约三分之一的大尺寸面部特写，
右侧并排站立同一个角色的正面全身、侧面全身、背面全身三个视图。
三个视图必须是完全同一个角色、同一套发型和服装，
站姿自然，双手自然下垂。
角色：{name}。{appearance}
{style}
整体为精致 3D 渲染质感，柔和影棚光，浅灰偏白背景，
柔和自然阴影，8K，精细发丝与材质，画面干净克制、大量留白。"""

#: 默认风格/镜头段（与角色无关，可整体替换）
SHEET_STYLE = "85mm 人像镜头，柔和珍珠质感轮廓光，高级时尚大片质感。"

#: 负面提示词。`text/letters/words/chinese characters` 是**必须的** ——
#: 否则模型会往图上写标注，而它写不对中文，产出一堆乱码。
#: `duplicated character / inconsistent face / different person` 防的是
#: "四个视角变成四个人"。
SHEET_NEGATIVE = (
    "multiple panels beyond the layout, duplicated character, inconsistent face, "
    "different person, mismatched outfit, text, letters, words, chinese characters, "
    "watermark, signature, logo, caption, annotation, arrows, grid lines, "
    "extra limbs, extra fingers, deformed hands, bad anatomy, "
    "blurry, lowres, jpeg artifacts"
)

#: `{appearance}` 建议的**六段结构**（颗粒度基准）。
#: 每段要具体到能画出来；含糊的形容词（"很漂亮""有气质"）模型只会平均化，
#: 四个视角就全不一样了。
APPEARANCE_SECTIONS = (
    "1. 气质基调：年龄/性别/整体气质，如「年轻东方女性，清冷柔美，带轻甜酷和梦幻少女感」",
    "2. 面部（逐部位）：肤色→脸型→眉→眼（形状/颜色/神态/卧蚕/睫毛/眼妆/下眼睑）→鼻→唇",
    "3. 发型：颜色→长度→发量→卷度→光泽→刘海→侧发→发饰，并显式要求「各视角保持一致」",
    "4. 服装（分上下身）：每件单品写清 品类+颜色+材质+版型+装饰细节",
    "5. 配饰（逐个列）：耳→颈→包→袜→鞋；材质与颜色必须成对出现（如「银色金属扣」）",
    "6. 身形：如「身材高挑，九头身时装模特比例」",
)


def build_sheet_prompt(name: str, appearance: str, style: str | None = None) -> str:
    """拼出设定表提示词。

    参数：
        name:       角色名
        appearance: 外貌与服装描述（建议按 `APPEARANCE_SECTIONS` 的六段写）
        style:      风格/镜头段；None 用 `SHEET_STYLE`
    """
    return SHEET_TEMPLATE.format(
        name=(name or "").strip() or "角色",
        appearance=(appearance or "").strip(),
        style=(style if style is not None else SHEET_STYLE).strip(),
    )
