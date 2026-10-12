"""白模规格（v2）的提示词与生成流程。

调用链：

    用户一句话描述角色
      → `build_messages()` 组提示词
      → `LLMService.chat_json()` 拿回 JSON
      → `white_model.normalize_spec()` 归一化 + 校验
      → 校验失败则**带上错误重试一次**
      → 成功返回 spec

## v2 的核心约束：位置不可直写

旧 schema 让模型直接写"关节偏移"，结果**手臂撇成八字**——因为横向偏移
会被前向运动学逐级累加放大（肩 0.17 + 肘 0.02 + …，到手腕就成了 0.40，
而躯干只有 0.15）。

v2 只让模型写三样东西，位置**完全由它们推导**：

    rotations  每关节的旋转弧度（受硬限位钳制）
    lengths    每段的长度（米）
    radii      关节球半径

所以"手臂横撇"在结构上不可能再出现：肩的横向位置由 `shoulderWidth`
决定，手臂的外张角度只能通过 `sho_*` 的 Z 旋转表达，且有上下限。

## 为什么要"带上错误重试一次"

LLM 输出 JSON 最常见的两类问题是：引用了不存在的关节名、
或者 `segments` 为空。这两类都会在 `normalize_spec` 里抛 `SpecError`。
把错误文本回灌给模型让它改，比无条件重试有效得多——模型知道错在哪。

只重试一次：再多就是烧钱，且第二次还错说明是模型能力问题，
此时应当把原因返回给用户看，而不是无限循环。
"""

from __future__ import annotations

import json
from typing import Any

from app.services import white_model

#: 关节清单：名字 + 中文提示 + 限位。写进提示词让模型知道能引用什么、
#: 以及每个关节能转到多少度。不写清楚它会发明 `hand_l` 这类名字。
_JOINT_TABLE = "\n".join(
    f"  {name:<8} 父={parent or '（根）':<7} {white_model.JOINT_HINTS.get(name, ''):<28} "
    f"X∈[{white_model.JOINT_LIMITS[name][0][0]:.2f}, {white_model.JOINT_LIMITS[name][0][1]:.2f}] "
    f"Y∈[{white_model.JOINT_LIMITS[name][1][0]:.2f}, {white_model.JOINT_LIMITS[name][1][1]:.2f}] "
    f"Z∈[{white_model.JOINT_LIMITS[name][2][0]:.2f}, {white_model.JOINT_LIMITS[name][2][1]:.2f}]"
    for name, parent in white_model.JOINT_PARENT.items()
)

_DEFAULT_ROT = json.dumps(white_model.DEFAULT_ROTATIONS, ensure_ascii=False, indent=2)
_DEFAULT_LEN = json.dumps(white_model.DEFAULT_LENGTHS, ensure_ascii=False, indent=2)

_SYSTEM = f"""你是"白模建模师"。把一句角色描述变成一份 **JSON 规格（version 2）**，
用来在 Three.js 里拼出一个**白色模型（白模）**。

## 白模是什么

白模是给视频生成模型看**空间结构**用的：无材质、无光照、纯色。
它需要**轮廓清楚、体块分明**，不是有细节的有机角色。
形状只用四种：capsule（胶囊）、taperedCylinder（锥台）、box（方块）、sphere（球）。

## 坐标与单位

右手系（Three.js）：+X 向右、+Y 向上、+Z 朝向镜头。单位**米**。脚底在 y=0。

## ⚠️ 最重要的一条：**位置不可直写**

你**不能**指定关节的位置或偏移。位置由「旋转 + 段长」推导：

    子关节位置 = 父关节位置 + 父关节旋转后的 (0, -段长, 0)

所以想要"手臂向两侧张开"，改 `rotations.sho_l` 的 **Z 分量**；
想要"手臂前伸"，改 **X 分量**。**不要去挪位置**——那是旧版的做法，
会让手臂撇成八字，已被禁止。

## 可用的关节（只能用这些名字）

{_JOINT_TABLE}

角度单位是**弧度**。上面的区间是硬限位，写超了会被夹住。

## 三个参数区

### 1. `proportions` —— 整体比例

    height        总身高（米），含头顶。范围 0.2~4.0
    headRatio     头身比，越大越写实，越小越萌。范围 4~12
    shoulderWidth 肩宽系数：肩距 = 身高 × 该值。范围 0.4~1.6
    limbRatio     四肢粗细系数，1.0 是标准。范围 0.5~1.8
    headScale     头的额外大小系数。范围 0.5~2.0
    chubbiness    躯干丰满度系数。范围 0.5~2.0
    legRatio      腿长系数。范围 0.5~1.5

### 2. `rotations` —— 每关节的旋转（弧度，可只写要偏离默认的）

默认站姿是：手臂自然下垂（肩 Z=±0.05）、肘微屈、膝微屈。

{_DEFAULT_ROT}

### 3. `lengths` —— 每段的长度（米，可只写要偏离默认的）

{_DEFAULT_LEN}

## 输出格式（严格 JSON，不要解释、不要 markdown 代码块）

{{
  "version": 2,
  "name": "角色名（短）",
  "description": "一句话说明你建成了什么",
  "proportions": {{ "height": 1.70, "headRatio": 7.5 }},
  "rotations": {{ "sho_l": [0.04, 0, 0.05], "elb_l": [-0.10, 0, 0] }},
  "lengths": {{ "elb_l": 0.25, "wri_l": 0.23 }},
  "radii": {{ "head": 0.135 }},
  "segments": [
    {{"joint": "chest",  "shape": "capsule",         "r0": 0.15, "r1": 0.15}},
    {{"joint": "elb_l",  "shape": "taperedCylinder", "r0": 0.055, "r1": 0.046}},
    {{"joint": "wri_l",  "shape": "box",             "r0": 0, "r1": 0, "size": [0.075, 0.16, 0.03]}},
    {{"joint": "foot_l", "shape": "box",             "r0": 0, "r1": 0, "size": [0.09, 0.05, 0.20]}}
  ],
  "pose": "idle"
}}

`pose` 只能是：idle / walk / run / sit / wave / point（它只影响初始姿态，
不影响几何结构）。

## 建白模的要求

1. **每个可动部位都要有体块**：躯干（spine + chest）、两条上臂、两条前臂、
   两只手、两条大腿、两条小腿、两只脚、头。少一个就是缺零件。
   其中 `foot_l` / `foot_r` 是**脚掌**（用 box，Z 方向长一些），一定要给。
2. **手掌也要给**（`wri_l` / `wri_r` 用 box，扁一些）。
3. **四肢要细**：标准成年人上臂半径约 0.055 米、大腿约 0.088 米、
   小腿约 0.062 米。给大了整个模型像橡皮泥捏的。
4. **躯干不要用三个球叠**：用 `spine` 和 `chest` 两段 capsule。
5. **比例合理**：默认 7.5 头身、1.70 米是标准成年人。
   卡通角色可以 headRatio=4~5、height=1.2 这样调。
"""


def build_messages(
    description: str,
    name: str = "",
    pose: str = "",
    height_hint: str = "",
) -> tuple[str, str]:
    """返回 (system, user)。"""
    lines = [f"角色描述：{description.strip()}"]
    if name.strip():
        lines.append(f"名称：{name.strip()}")
    if height_hint.strip():
        lines.append(f"体型提示：{height_hint.strip()}")
    if pose.strip():
        lines.append(f"初始姿态：{pose.strip()}")
    lines.append("")
    lines.append("请只输出 JSON。")
    return _SYSTEM, "\n".join(lines)


def _repair_messages(description: str, bad_output: str, error: str) -> tuple[str, str]:
    """校验失败后的第二次尝试：把错误回灌给模型。"""
    user = (
        f"角色描述：{description.strip()}\n\n"
        f"你上一次的输出没有通过校验，错误是：\n{error}\n\n"
        f"上一次的输出（供参考）：\n{bad_output[:1200]}\n\n"
        f"请修正后**只输出 JSON**。注意：\n"
        f"  · 关节名只能用给定清单里的名字；\n"
        f"  · `segments` 不能为空；\n"
        f"  · **不要写任何位置/偏移字段**，位置由 rotations 与 lengths 推导。"
    )
    return _SYSTEM, user


def generate_spec(
    description: str,
    *,
    name: str = "",
    pose: str = "",
    height_hint: str = "",
    role: str = "previs_artist",
) -> dict[str, Any]:
    """生成一份白模规格。

    返回 `{"status": "ok", "spec": {...}, "attempts": 1|2}`
    或 `{"status": "error", "message": ..., "raw": ...}`。

    出错时**不抛异常**：让 API 层决定怎么呈现（返回 400 带可读原因，
    或把原始输出给用户让他自己改——那是 C 模式的能力）。
    """
    from app.services.llm import get_llm

    llm = get_llm()
    system, user = build_messages(description, name, pose, height_hint)

    try:
        raw = llm.chat_json(system, user, role=role)
    except Exception as exc:  # noqa: BLE001 —— LLM 未配置/网络失败都走这里
        return {"status": "error", "message": f"调用模型失败：{exc}"}

    # `chat_json` 解析失败时不抛异常，而是返回带 `_parse_error` 的字典
    if isinstance(raw, dict) and raw.get("_parse_error"):
        parsed = None
        raw_text = str(raw.get("raw") or "")
        first_error = f"输出不是合法 JSON：{raw.get('error')}"
    else:
        parsed = raw
        raw_text = json.dumps(raw, ensure_ascii=False)
        first_error = ""

    if parsed is not None:
        try:
            return {"status": "ok", "spec": white_model.normalize_spec(parsed), "attempts": 1}
        except white_model.SpecError as exc:
            first_error = str(exc)

    # ---- 第二次：带上错误让模型自己修 ----
    system2, user2 = _repair_messages(description, raw_text, first_error)
    try:
        raw2 = llm.chat_json(system2, user2, role=role)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"重试时调用模型失败：{exc}", "raw": raw_text[:800]}

    if isinstance(raw2, dict) and raw2.get("_parse_error"):
        return {
            "status": "error",
            "message": f"模型两次输出都不是合法 JSON（{raw2.get('error')}）",
            "raw": str(raw2.get("raw") or "")[:800],
        }
    try:
        return {"status": "ok", "spec": white_model.normalize_spec(raw2), "attempts": 2}
    except white_model.SpecError as exc:
        return {
            "status": "error",
            "message": f"模型两次输出都不合规格：{exc}",
            "raw": json.dumps(raw2, ensure_ascii=False)[:800],
        }
