"""相机参数 → 提示词（确定性映射，2026-11-15）。

## 为什么需要

分镜里"电影感/浅景深"这类形容词**不可复现**：同一句话两次生成可能完全不同。
而"85mm / f1.4 / 70mm 胶片"是**参数**：可查表、可复现、可 A/B。

这正是从 Open-Generative-AI 的 Cinema Studio 学到的做法：
用结构化选项（机身/镜头/焦距/光圈）拼一段**固定后缀**进提示词，
**不调模型、不花 token**。

## 设计约束

1 **确定性**：字段顺序固定（focal → aperture → body → lens → angle），
   同样输入永远得到同样字符串 —— 否则 IR 哈希会漂移、缓存永不命中。
2 **双语**：Z-Image 这类中文原生模型吃中文片段（见 `ir_bridge` 的两套词表），
   SDXL/云端模型吃英文。
3 **严格校验**：未知字段/未知取值直接报错并列出允许值 ——
   静默忽略会让"我明明设了 85mm"变成"其实没生效"。
4 **不重复**：运镜词表仍在 `ir_bridge.CAMERA_MOVE_PROMPT`（那里有镜头尺寸/动作）；
   本模块只管**机位/光学参数**。

## 用法

```python
from little_crew.ir.camera_lexicon import camera_prompt, canonical_camera
camera_prompt({"focal": "85mm", "aperture": "f1.4", "body": "70mm"}, "en")
# -> "85mm lens portrait compression, f/1.4 shallow depth of field, shot on 70mm film"
```
"""

from __future__ import annotations

import json
from typing import Any, Mapping

#: 每个字段允许的取值 → (英文片段, 中文片段)
#:
#: 键是**规范名**；值里给的是**规范值**。用户可传别名（"85" / "F1.4" / "70"），
#: 由 `canonical_camera()` 归一。
CAMERA_TABLES: dict[str, dict[str, tuple[str, str]]] = {
    "focal": {
        "14mm": ("14mm ultra-wide lens, dramatic perspective", "14mm 超广角，夸张透视"),
        "24mm": ("24mm wide lens, environmental context", "24mm 广角，交代环境"),
        "35mm": ("35mm lens, natural human-eye perspective", "35mm 人眼视角，自然"),
        "50mm": ("50mm standard lens, neutral perspective", "50mm 标准镜头，中正"),
        "85mm": ("85mm portrait lens, subtle compression, subject isolation",
                 "85mm 人像镜头，轻微压缩感，主体突出"),
        "135mm": ("135mm telephoto compression, strong background separation",
                  "135mm 长焦压缩，背景强分离"),
    },
    "aperture": {
        "f1.4": ("f/1.4 aperture, shallow depth of field, creamy bokeh",
                 "f/1.4 大光圈，浅景深，柔和虚化"),
        "f2": ("f/2 aperture, shallow depth of field", "f/2 光圈，浅景深"),
        "f2.8": ("f/2.8 aperture, moderate background blur", "f/2.8 光圈，背景适度虚化"),
        "f4": ("f/4 aperture, balanced depth of field", "f/4 光圈，景深均衡"),
        "f8": ("f/8 aperture, deep focus, sharp environment", "f/8 光圈，深景深，环境清晰"),
        "f11": ("f/11 aperture, deep focus", "f/11 光圈，深景深"),
    },
    "body": {
        "digital8k": ("shot on 8K digital cinema camera, clean detail",
                      "8K 数字电影机，细节干净"),
        "70mm": ("shot on 70mm film, fine grain, halation highlights",
                 "70mm 胶片，细腻颗粒，高光光晕"),
        "16mm": ("shot on 16mm film, visible grain, vintage texture",
                 "16mm 胶片，明显颗粒，复古质感"),
        "s35": ("shot on Super 35 cinema camera", "Super 35 电影机"),
    },
    "lens": {
        "anamorphic": ("anamorphic lens, oval bokeh, horizontal flares",
                       "变形宽银幕镜头，椭圆光斑，横向光晕"),
        "macro": ("macro lens, extreme close detail", "微距镜头，极近细节"),
        "vintage": ("vintage prime lens, soft warm rendering", "复古定焦，柔和暖调"),
        "tilt_shift": ("tilt-shift lens, miniature effect", "移轴镜头，微缩感"),
        "swirl": ("swirl bokeh lens, characterful background", "旋焦，背景有笔触感"),
        "clinical": ("clinical sharp prime lens, high micro-contrast",
                     "锐利定焦，高微反差"),
    },
    "angle": {
        "eye_level": ("eye-level camera angle", "平视机位"),
        "low": ("low camera angle, subject looming", "低机位仰拍，主体压迫感"),
        "high": ("high camera angle, subject vulnerable", "高机位俯拍，主体弱小"),
        "dutch": ("dutch angle, tilted horizon, unease", "荷兰角，地平线倾斜，不安感"),
        "overhead": ("overhead top-down angle", "正俯拍，顶视"),
        "subjective": ("subjective POV shot, first-person eyeline",
                       "主观镜头，第一人称视点"),
        "over_shoulder": ("over-the-shoulder shot, foreground shoulder",
                          "过肩镜头，前景带肩"),
    },
}

#: 字段顺序固定（确定性的一部分）
CAMERA_FIELD_ORDER = ("focal", "aperture", "body", "lens", "angle")

#: 别名 → 规范值（大小写不敏感；`85`/`85mm`、`f/1.4`/`F1.4` 都收）
_ALIASES: dict[str, dict[str, str]] = {
    "focal": {k: k for k in CAMERA_TABLES["focal"]} | {
        k.replace("mm", ""): k for k in CAMERA_TABLES["focal"]},
    "aperture": {k: k for k in CAMERA_TABLES["aperture"]} | {
        k.replace("f", "f/"): k for k in CAMERA_TABLES["aperture"]} | {
        k.upper(): k for k in CAMERA_TABLES["aperture"]},
    "body": {k: k for k in CAMERA_TABLES["body"]} | {
        "70": "70mm", "16": "16mm", "70mm_film": "70mm", "16mm_film": "16mm",
        "8k": "digital8k", "digital": "digital8k", "super35": "s35",
        "super_35": "s35", "35mm": "s35", "s35mm": "s35"},
    "lens": {k: k for k in CAMERA_TABLES["lens"]} | {
        "anamorphic_lens": "anamorphic", "tilt-shift": "tilt_shift",
        "tilt": "tilt_shift", "swirl_bokeh": "swirl"},
    "angle": {k: k for k in CAMERA_TABLES["angle"]} | {
        "eye": "eye_level", "low_angle": "low", "high_angle": "high",
        "dutch_angle": "dutch", "top": "overhead", "top_down": "overhead",
        "pov": "subjective", "first_person": "subjective", "主观": "subjective",
        "ots": "over_shoulder", "over-shoulder": "over_shoulder",
        "过肩": "over_shoulder"},
}

LANGUAGES = ("en", "zh")


def canonical_camera(camera: Mapping[str, Any] | None) -> dict[str, str]:
    """校验 + 归一化相机参数。返回 `{字段: 规范值}`（按字段顺序）。

    未知字段 / 未知取值 **抛 ValueError**（带允许值）——不静默忽略。
    """
    if not camera:
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_val in camera.items():
        key = str(raw_key or "").strip().lower()
        if key not in CAMERA_TABLES:
            raise ValueError(
                f"未知相机字段 {raw_key!r}；可用：{list(CAMERA_FIELD_ORDER)}")
        val = str(raw_val or "").strip()
        if not val:
            continue
        canon = _ALIASES[key].get(val.lower())
        if canon is None:
            raise ValueError(
                f"相机字段 {key!r} 的取值 {val!r} 不认识；可用："
                f"{list(CAMERA_TABLES[key])}")
        out[key] = canon
    return {k: out[k] for k in CAMERA_FIELD_ORDER if k in out}


def camera_terms(camera: Mapping[str, Any] | None,
                 language: str = "en") -> list[str]:
    """返回按固定顺序排列的提示词片段列表（中文或英文）。"""
    lang = "zh" if str(language).lower().startswith("zh") else "en"
    canon = canonical_camera(camera)
    idx = 1 if lang == "zh" else 0
    return [CAMERA_TABLES[k][v][idx] for k, v in canon.items()]


def camera_prompt(camera: Mapping[str, Any] | None,
                  language: str = "en") -> str:
    """拼成一段提示词后缀；无相机参数时返回空串。"""
    terms = camera_terms(camera, language)
    if not terms:
        return ""
    sep = "，" if str(language).lower().startswith("zh") else ", "
    return sep.join(terms)


def camera_fingerprint(camera: Mapping[str, Any] | None) -> str:
    """归一化后的稳定 JSON（进证据/可复核用）。无参数时为空串。"""
    canon = canonical_camera(camera)
    if not canon:
        return ""
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)
