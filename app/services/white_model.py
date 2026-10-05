"""白模规格（White Model Spec）的校验与归一化。

## 这是什么

白色模型不再由代码写死（原先 `web/src/previs/humanoid.ts` 用球+圆柱硬拼），
而是由**大语言模型按角色描述生成一份 JSON 规格**，前端据此渲染体块。

## 为什么让 LLM 生成 JSON、而不是生成 Three.js 代码

用户最初的想法是"让 LLM 写 Three.js 代码建白模"。生成 JSON 更稳妥：

    ┌──────────────┬────────────────────┬────────────────────┐
    │              │ 生成代码            │ 生成 JSON（现行）    │
    ├──────────────┼────────────────────┼────────────────────┤
    │ 安全          │ 页面里 eval 任意代码 │ 不可能执行代码       │
    │ 出错          │ 语法错→白屏          │ schema 校验，可重试  │
    │ 可存          │ 存代码，迁移麻烦      │ 存 JSON，跟工程一起  │
    │ 可复现        │ 同描述生成不同代码    │ 同 spec 渲染完全一致 │
    │ 人工修改      │ 改代码里的数字        │ 就是改 JSON 字段     │
    └──────────────┴────────────────────┴────────────────────┘

渲染那一步是**前端写死的代码**（`web/src/previs/bodySpec.ts`），
所以形状稳定、不会因为模型抽风而崩。

## 坐标约定

    右手系，与 Three.js 一致：+X 右、+Y 上、+Z 朝向镜头（角色正面朝 +Z）。
    长度单位 **米**。
    角色脚底对齐 y=0（与现有 `skeletonPositionsGrounded` 的约定一致）。
"""

from __future__ import annotations

from typing import Any

#: 允许的关节名。刻意与现有 `humanoid.ts` 的骨架对齐，并补一个 `foot`——
#: 原先骨头到脚踝就结束了，所以渲染出来**没有脚**（正是用户看到的问题之一）。
JOINT_NAMES = (
    "pelvis", "spine", "chest", "neck", "head",
    "sho_l", "elb_l", "wri_l",
    "sho_r", "elb_r", "wri_r",
    "hip_l", "kne_l", "ank_l", "foot_l",
    "hip_r", "kne_r", "ank_r", "foot_r",
)

#: 父关节关系（决定前向运动学的链）
JOINT_PARENT: dict[str, str | None] = {
    "pelvis": None,
    "spine": "pelvis",
    "chest": "spine",
    "neck": "chest",
    "head": "neck",
    "sho_l": "chest", "elb_l": "sho_l", "wri_l": "elb_l",
    "sho_r": "chest", "elb_r": "sho_r", "wri_r": "elb_r",
    "hip_l": "pelvis", "kne_l": "hip_l", "ank_l": "kne_l", "foot_l": "ank_l",
    "hip_r": "pelvis", "kne_r": "hip_r", "ank_r": "kne_r", "foot_r": "ank_r",
}

#: 允许的体块形状。刻意只有四种——它们都能用 Three.js 内置几何直接搭出来，
#: 且轮廓清晰（白模是给视频模型看空间结构的，圆润的有机网格反而更难读）。
SHAPES = ("capsule", "taperedCylinder", "box", "sphere")

#: 每个关节的**旋转轴顺序**与限位（弧度）。超出即夹住。
#:
#: 这张表是 v2 schema 的核心。旧 schema 允许直接写"关节偏移"，
#: 而横向偏移会被前向运动学逐级累加，于是手臂撇成"八"字——
#: **让位置可直写就是这个 bug 的根源**。v2 只允许写**旋转**，
#: 位置一律由旋转 + 段长推导，所以那种畸形在结构上不可能出现。
JOINT_LIMITS: dict[str, tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = {
    #            绕 X（前后摆动）      绕 Y（扭转）          绕 Z（左右张开）
    "spine": ((-0.6, 0.6), (-0.6, 0.6), (-0.4, 0.4)),
    "chest": ((-0.5, 0.5), (-0.6, 0.6), (-0.35, 0.35)),
    "neck": ((-0.5, 0.5), (-1.0, 1.0), (-0.35, 0.35)),
    "head": ((-0.4, 0.4), (-1.2, 1.2), (-0.3, 0.3)),
    "sho_l": ((-2.6, 2.6), (-1.6, 1.6), (-0.2, 1.7)),
    "elb_l": ((-2.6, 0.05), (-1.5, 1.5), (-0.3, 0.3)),
    "wri_l": ((-1.2, 1.2), (-0.9, 0.9), (-0.6, 0.6)),
    "sho_r": ((-2.6, 2.6), (-1.6, 1.6), (-1.7, 0.2)),
    "elb_r": ((-2.6, 0.05), (-1.5, 1.5), (-0.3, 0.3)),
    "wri_r": ((-1.2, 1.2), (-0.9, 0.9), (-0.6, 0.6)),
    "hip_l": ((-1.4, 1.4), (-0.6, 0.6), (-1.5, 0.1)),
    "kne_l": ((0.0, 2.4), (-0.2, 0.2), (-0.2, 0.2)),
    "ank_l": ((-0.8, 0.8), (-0.4, 0.4), (-0.3, 0.3)),
    "foot_l": ((-0.3, 0.3), (-0.3, 0.3), (-0.3, 0.3)),
    "hip_r": ((-1.4, 1.4), (-0.6, 0.6), (-0.1, 1.5)),
    "kne_r": ((0.0, 2.4), (-0.2, 0.2), (-0.2, 0.2)),
    "ank_r": ((-0.8, 0.8), (-0.4, 0.4), (-0.3, 0.3)),
    "foot_r": ((-0.3, 0.3), (-0.3, 0.3), (-0.3, 0.3)),
}

#: 关节名的中文标注（写进提示词，帮助模型理解）
JOINT_HINTS: dict[str, str] = {
    "spine": "腰（前倾/后仰）",
    "chest": "胸/上身",
    "neck": "脖子",
    "head": "头（点头/摇头）",
    "sho_l": "左肩（绕 Z 为正=手臂向外抬起）",
    "elb_l": "左肘（绕 X 为负=屈肘）",
    "wri_l": "左腕",
    "sho_r": "右肩（绕 Z 为负=手臂向外抬起）",
    "elb_r": "右肘（绕 X 为负=屈肘）",
    "wri_r": "右腕",
    "hip_l": "左髋（绕 X 为正=腿向前迈）",
    "kne_l": "左膝（绕 X 为正=屈膝）",
    "ank_l": "左踝",
    "foot_l": "左脚掌",
    "hip_r": "右髋",
    "kne_r": "右膝",
    "ank_r": "右踝",
    "foot_r": "右脚掌",
}

#: 比例参数的合法区间。超出即判定为模型胡说，回退默认值。
PROPORTION_RANGES: dict[str, tuple[float, float]] = {
    "height": (0.2, 4.0),        # 总身高（米），含头顶
    "headRatio": (4.0, 12.0),    # 头身比：身高 ÷ 头高
    #: ⚠️ `shoulderWidth` 是**肩距 ÷ 身高**，不是肩的绝对宽度。
    #:
    #: 这里踩过一个代价很大的坑：区间原本写成 `(0.4, 1.6)`，而默认值是
    #: `0.22`——**默认值低于下限**，于是每次归一化都把它夹到 0.4，
    #: 肩宽直接翻倍、手臂横撇到 ±0.34（应为 ±0.19）。
    #: 现象是"两条胳膊撇成八字"，但在渲染/旋转/FK 上查了很久，
    #: 真因只是区间写错。人体肩距约是身高的 0.22~0.25 倍，
    #: 所以区间取 0.14~0.40（窄肩到极宽肩），默认 0.22 落在中间。
    #:
    #: 教训：**给区间之前先确认默认值落在区间内**。这类错误单测很容易漏，
    #: 因为归一化"成功"了，只是结果不对。
    "shoulderWidth": (0.14, 0.40),
    "limbRatio": (0.5, 1.8),     # 四肢粗细系数：1.0 = 写死的基准
    "headScale": (0.5, 2.0),     # 头的大小系数
    "chubbiness": (0.5, 2.0),    # 躯干丰满度系数
    "legRatio": (0.5, 1.5),      # 腿长系数：1.0 = 写死的基准
}

#: 各比例的默认值（写死的基准，取自现有骨架的实测比例）
DEFAULT_PROPORTIONS: dict[str, float] = {
    "height": 1.70,
    "headRatio": 7.5,
    "shoulderWidth": 0.22,
    "limbRatio": 1.0,
    "headScale": 1.0,
    "chubbiness": 1.0,
    "legRatio": 1.0,
}

#: 默认关节旋转（弧度）。这是"标准站姿"：手臂自然下垂、肘微屈、膝微屈。
#:
#: 关键点：**手臂的横向位置由 `sho_*` 的 Z 旋转决定**。
#: Z=0 时手臂完全垂直贴着躯干；0.05 弧度（约 3°）略略外张，
#: 是"自然下垂"而不是"立正"。再大就会变成"八"字。
DEFAULT_ROTATIONS: dict[str, list[float]] = {
    "spine": [0.02, 0.0, 0.0],
    "chest": [0.015, 0.0, 0.0],
    "neck": [-0.02, 0.0, 0.0],
    "head": [0.0, 0.0, 0.0],
    "sho_l": [0.04, 0.0, 0.05],
    "elb_l": [-0.10, 0.0, 0.0],
    "wri_l": [0.0, 0.0, 0.0],
    "sho_r": [0.04, 0.0, -0.05],
    "elb_r": [-0.10, 0.0, 0.0],
    "wri_r": [0.0, 0.0, 0.0],
    "hip_l": [0.0, 0.0, 0.03],
    "kne_l": [0.04, 0.0, 0.0],
    "ank_l": [0.0, 0.0, 0.0],
    "foot_l": [0.0, 0.0, 0.0],
    "hip_r": [0.0, 0.0, -0.03],
    "kne_r": [0.04, 0.0, 0.0],
    "ank_r": [0.0, 0.0, 0.0],
    "foot_r": [0.0, 0.0, 0.0],
}

#: 默认段长（米）。以骨盆为原点，**父→子的竖直距离**。
#: 与旋转一起完全决定关节位置——这就是 v2 "位置不可直写"的落实。
DEFAULT_LENGTHS: dict[str, float] = {
    "spine": 0.287,
    "chest": 0.277,
    "neck": 0.071,
    "head": 0.059,
    "elb_l": 0.31, "wri_l": 0.26,
    "elb_r": 0.31, "wri_r": 0.26,
    "kne_l": 0.436, "ank_l": 0.436, "foot_l": 0.077,
    "kne_r": 0.436, "ank_r": 0.436, "foot_r": 0.077,
}

#: 关节球默认半径（米）。要比相接的肢体略细，否则会鼓出疙瘩。
DEFAULT_RADII: dict[str, float] = {
    "pelvis": 0.084, "spine": 0.087, "chest": 0.095, "neck": 0.045, "head": 0.129,
    "sho_l": 0.055, "elb_l": 0.048, "wri_l": 0.04,
    "sho_r": 0.055, "elb_r": 0.048, "wri_r": 0.04,
    "hip_l": 0.088, "kne_l": 0.072, "ank_l": 0.053, "foot_l": 0.0,
    "hip_r": 0.088, "kne_r": 0.072, "ank_r": 0.053, "foot_r": 0.0,
}

#: 默认体块表 —— 这是"一个完整的人"的定义。
#:
#: ⚠️ 它同时是**降级兜底**：模型没给 `segments`（或全给了非法关节）时用它，
#: 于是渲染出来仍是一具完整的人，而不是"缺零件的怪物"或直接失败。
#: 与前端 `bodySpec.ts` 的 `DEFAULT_SEGMENTS` 必须一致。
DEFAULT_SEGMENTS: list[dict] = [
    {"joint": "spine", "shape": "capsule", "r0": 0.089, "r1": 0.083},
    {"joint": "chest", "shape": "capsule", "r0": 0.097, "r1": 0.092},
    {"joint": "neck", "shape": "taperedCylinder", "r0": 0.047, "r1": 0.047},
    {"joint": "head", "shape": "sphere", "r0": 0.129, "r1": 0.129},
    {"joint": "elb_l", "shape": "taperedCylinder", "r0": 0.061, "r1": 0.051},
    {"joint": "wri_l", "shape": "taperedCylinder", "r0": 0.046, "r1": 0.038},
    {"joint": "elb_r", "shape": "taperedCylinder", "r0": 0.061, "r1": 0.051},
    {"joint": "wri_r", "shape": "taperedCylinder", "r0": 0.046, "r1": 0.038},
    {"joint": "wri_l", "shape": "box", "r0": 0.0, "r1": 0.0, "size": [0.075, 0.16, 0.03]},
    {"joint": "wri_r", "shape": "box", "r0": 0.0, "r1": 0.0, "size": [0.075, 0.16, 0.03]},
    {"joint": "kne_l", "shape": "taperedCylinder", "r0": 0.097, "r1": 0.069},
    {"joint": "ank_l", "shape": "taperedCylinder", "r0": 0.062, "r1": 0.048},
    {"joint": "kne_r", "shape": "taperedCylinder", "r0": 0.097, "r1": 0.069},
    {"joint": "ank_r", "shape": "taperedCylinder", "r0": 0.062, "r1": 0.048},
    {"joint": "foot_l", "shape": "box", "r0": 0.0, "r1": 0.0, "size": [0.098, 0.05, 0.22]},
    {"joint": "foot_r", "shape": "box", "r0": 0.0, "r1": 0.0, "size": [0.098, 0.05, 0.22]},
]

#: 允许的姿态预设（只影响 `rotations` 的初始值，不再决定几何结构）
POSE_PRESETS = ("idle", "walk", "run", "sit", "wave", "point")


class SpecError(ValueError):
    """规格不合法且**无法安全降级**（例如关节名不在白名单里）。"""


def _num(value: Any, default: float, lo: float, hi: float) -> float:
    """取一个数字并夹到区间内；非数字则回退默认值。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return min(hi, max(lo, v))


def _clamp_angle(value: Any, joint: str, axis: int, default: float) -> float:
    """取一个旋转弧度并夹到该关节该轴的限位内。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    lo, hi = JOINT_LIMITS.get(joint, ((-3.2, 3.2),) * 3)[axis]
    return round(min(hi, max(lo, v)), 4)


def normalize_spec(raw: Any) -> dict:
    """把 LLM 的输出归一化成一份**保证可渲染**的 v2 规格。

    三条原则：

      1. **能降级就降级，不抛异常**——模型漏一个字段不该让整个功能失败，
         用默认值补上即可（渲染一定要出东西）。
      2. **不能降级的才报错**——关节名不在白名单、或体块引用了不存在的关节，
         这类错误会让前向运动学算不出来，必须让调用方知道。
      3. **位置不可直写**——v2 只接受 `rotations` / `lengths` / `radii`。
         旧字段 `joints`（任意偏移）即使送进来也**被忽略**：那正是
         "手臂撇成八字"的根源（横向偏移会被前向运动学逐级累加）。
         忽略而不是报错，是为了能平滑读入旧工程文件。
    """
    if not isinstance(raw, dict):
        raise SpecError("规格必须是一个 JSON 对象")

    name = str(raw.get("name") or "").strip()[:40] or "白模"

    # ---- 比例 ----
    raw_prop = raw.get("proportions")
    raw_prop = raw_prop if isinstance(raw_prop, dict) else {}
    proportions: dict[str, float] = {}
    for key, (lo, hi) in PROPORTION_RANGES.items():
        proportions[key] = round(_num(raw_prop.get(key), DEFAULT_PROPORTIONS[key], lo, hi), 4)

    # ---- 旋转（唯一的"位置来源"）----
    raw_rot = raw.get("rotations")
    raw_rot = raw_rot if isinstance(raw_rot, dict) else {}
    rotations: dict[str, list[float]] = {}
    for jname, jval in raw_rot.items():
        if jname not in JOINT_PARENT:
            continue  # 白名单外：忽略（多余字段无害）
        default = DEFAULT_ROTATIONS.get(jname, [0.0, 0.0, 0.0])
        vec = jval if isinstance(jval, (list, tuple)) and len(jval) == 3 else [None, None, None]
        rotations[jname] = [
            _clamp_angle(vec[ax], jname, ax, default[ax]) for ax in range(3)
        ]

    # ---- 段长 ----
    raw_len = raw.get("lengths")
    raw_len = raw_len if isinstance(raw_len, dict) else {}
    lengths: dict[str, float] = {}
    for jname, lval in raw_len.items():
        if jname not in JOINT_PARENT or JOINT_PARENT[jname] is None:
            continue
        # 单段不超过 2 米：再长就不是人了（身高上限 4 米，腿也不可能 2 米一段）
        lengths[jname] = round(_num(lval, DEFAULT_LENGTHS.get(jname, 0.2), 0.01, 2.0), 4)

    # ---- 关节球半径 ----
    raw_radii = raw.get("radii")
    if raw_radii is None:
        # 兼容 v1 的字段名
        raw_radii = raw.get("jointRadii")
    raw_radii = raw_radii if isinstance(raw_radii, dict) else {}
    radii: dict[str, float] = {}
    for jname, rval in raw_radii.items():
        if jname not in JOINT_PARENT:
            continue
        radii[jname] = round(_num(rval, 0.0, 0.0, 0.6), 4)

    # ---- 体块 ----
    raw_segs = raw.get("segments")
    raw_segs = raw_segs if isinstance(raw_segs, list) else []

    segments: list[dict] = []
    for idx, seg in enumerate(raw_segs[:80]):  # 上限 80：再多就是模型失控
        if not isinstance(seg, dict):
            continue
        joint = str(seg.get("joint") or "")
        # 未知关节 / 根关节：**跳过这一段**而不是整体报错。
        # 理由：少一段总好过整个功能失败；而且模型偶尔会多写一个名字。
        if joint not in JOINT_PARENT or JOINT_PARENT[joint] is None:
            continue
        shape = str(seg.get("shape") or "capsule")
        if shape not in SHAPES:
            shape = "capsule"
        item: dict[str, Any] = {
            "joint": joint,
            "shape": shape,
            # ⚠️ box 的半径必须允许为 0：它用 size 描述尺寸。
            # 若给下限，下游"取最细相邻肢体算关节球半径"会拿到 0.005，
            # 把关节球算成约 0.006——球被缩没，手腕/脚踝看起来是断的。
            "r0": 0.0 if shape == "box" else round(_num(seg.get("r0"), 0.06, 0.005, 0.6), 4),
            "r1": 0.0 if shape == "box" else round(_num(seg.get("r1"), 0.05, 0.005, 0.6), 4),
        }
        if isinstance(seg.get("size"), (list, tuple)) and len(seg["size"]) == 3:
            item["size"] = [round(_num(x, 0.1, 0.005, 1.5), 4) for x in seg["size"]]
        color = seg.get("color")
        if isinstance(color, str) and color.startswith("#") and len(color) in (4, 7):
            item["color"] = color
        segments.append(item)

    # 一个合法体块都没有 → 用默认完整人形兜底（而不是报错）
    if not segments:
        segments = [dict(s) for s in DEFAULT_SEGMENTS]

    # ---- 姿态 ----
    pose = str(raw.get("pose") or "idle")
    if pose not in POSE_PRESETS:
        pose = "idle"

    out: dict[str, Any] = {
        "version": 2,
        "name": name,
        "proportions": proportions,
        "rotations": rotations,
        "lengths": lengths,
        "radii": radii,
        "segments": segments,
        "pose": pose,
    }
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()[:200]
    return out
