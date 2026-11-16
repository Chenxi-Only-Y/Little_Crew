"""景别 / 运镜 / 机位 / 特殊镜头词表（2026-11-16 吸收整理）。

来源：用户提供的四张 AI 漫剧提示词大全（运镜 8 / 景别 6 / 机位 5 / 特殊镜头与节奏 5）
+ 公开资料补充（推拉摇移跟升降甩、变焦/跳焦、斯坦尼康/摇臂/航拍、双人/群像/插入/反应/空镜、
  子弹时间/延时/定格/分屏/匹配剪辑/跳切/焦点转移、动态幅度）。

**分工**（对应名册职责）：

| 词表 | 归属 | 说明 |
|---|---|---|
景别（size） | **分镜师** | 决定每镜"拍多大" |
运镜（move）+ 动态幅度 | **分镜师** | 决定镜头怎么动；图像阶段作构图暗示，视频阶段执行 |
机位（angle） | **导演默认 / 摄影师执行** | 导演给 `cameraDefaults`，摄影师按镜执行 |
特殊镜头（special） | **摄影师** | 一镜到底/手持/子弹时间等（时长受分镜 ≤10s 约束） |
剪辑节奏（cut） | **后期统筹（剪辑组）** | 快速切换/匹配剪辑/跳切/转场 |

设计约束：确定性（固定顺序、别名归一）、中英双语、**未知值报错不静默**。
"""
from __future__ import annotations

import re
from typing import Any

#: 景别（规范名 → 英文片段, 中文片段）
SHOT_SIZES: dict[str, tuple[str, str]] = {
    "extreme_close": ("extreme close-up, detail only", "大特写，只拍局部细节"),
    "close": ("close-up, face fills frame", "特写，面部占满画面"),
    "medium_close": ("medium close-up, chest up", "近景，胸部以上入镜"),
    "medium": ("medium shot, waist up", "中景，腰部以上入镜"),
    "medium_full": ("medium full shot, knees up", "中全景，膝盖以上入镜"),
    "full": ("full body shot, head to toe", "全景，全身完整入镜"),
    "long": ("long shot, subject small in environment", "远景，人物与场景一起入镜"),
    "extreme_long": ("extreme long shot, vast environment", "大远景，人物占比极小"),
    # 叙事型景别（补充）
    "two_shot": ("two-shot, two subjects in frame", "双人镜头，两人同框"),
    "group_shot": ("group shot, ensemble framing", "群像镜头"),
    "insert": ("insert shot, detail cutaway", "插入镜头，局部细节"),
    "reaction": ("reaction shot, emotional response", "反应镜头，情绪回应"),
    "establishing": ("establishing shot, setting the scene", "定场镜头，交代环境"),
    "empty": ("empty shot, no characters, mood only", "空镜，无人，只留氛围"),
}

#: 运镜（规范名 → 英文片段, 中文片段）
CAMERA_MOVES: dict[str, tuple[str, str]] = {
    "static": ("static camera, stable composition", "固定机位，构图稳定"),
    "push_in": ("camera slowly pushing in, tightening framing", "镜头缓慢向前推进，画面收紧"),
    "pull_out": ("camera slowly pulling back, revealing context", "镜头缓慢向后拉远，展现环境"),
    "pan_left": ("camera panning left, horizontal sweep", "机位固定，镜头向左摇"),
    "pan_right": ("camera panning right, horizontal sweep", "机位固定，镜头向右摇"),
    "tilt_up": ("camera tilting up, vertical reveal", "机位固定，镜头上摇"),
    "tilt_down": ("camera tilting down, vertical reveal", "机位固定，镜头下摇"),
    "whip_pan": ("whip pan, fast blurred transition", "甩镜头，快速摇动产生模糊过渡"),
    "truck_left": ("camera tracking left, lateral movement", "镜头与人物平行左移，横向运动"),
    "truck_right": ("camera tracking right, lateral movement", "镜头与人物平行右移，横向运动"),
    "rise": ("camera rising vertically, revealing scale", "镜头自下而上垂直升起"),
    "fall": ("camera descending vertically", "镜头自上而下垂直下降"),
    "orbit": ("camera orbiting around subject", "镜头围绕人物环绕旋转"),
    "follow": ("camera following behind subject, tracking", "镜头从背后跟随人物前进"),
    "steadicam": ("smooth steadicam movement, flowing", "斯坦尼康稳定移动"),
    "crane_up": ("crane rising, grand reveal", "摇臂上升，宏大揭示"),
    "crane_down": ("crane descending", "摇臂下降"),
    "aerial": ("aerial drone shot, sweeping view", "航拍，俯瞰移动"),
    "zoom_in": ("zoom in, focal length tightening", "变焦推近（不改变透视）"),
    "zoom_out": ("zoom out, focal length widening", "变焦拉远"),
    "dolly_zoom": ("dolly zoom, background stretching with subject stable",
                   "希区柯克变焦，人物大小不变而背景拉伸"),
    "handheld": ("handheld camera, slight shake, urgent", "轻微手持震动，现场感"),
    "fast_cut": ("rapid cut sequence, quick montage", "多个短镜头快速切换"),
    "slow_motion": ("slow motion, extended key moment", "慢动作，关键瞬间拉长"),
    "long_take": ("long take, uninterrupted shot", "长镜头，一镜到底"),
}

#: 动态幅度（补充自 Vidu/官方提示词指南的"动态幅度"）
MOTION_AMPLITUDES: dict[str, tuple[str, str]] = {
    "small": ("subtle motion, minimal camera movement", "小幅动态，镜头稳定"),
    "medium": ("moderate motion, steady camera", "中等动态"),
    "large": ("large dynamic motion, fast action", "大动态，快速运动"),
}

#: 特殊镜头（执行归属摄影师；节奏型归剪辑组）
SPECIAL_SHOTS: dict[str, tuple[str, str]] = {
    "long_take": ("long take, uninterrupted shot", "长镜头，一镜到底"),
    "handheld": ("handheld camera, slight shake, urgent", "手持震动，现场感"),
    "fast_cut": ("rapid cut sequence, quick montage", "快速切换，节奏紧凑"),
    "slow_motion": ("slow motion, extended key moment", "慢动作，情绪更浓"),
    "bullet_time": ("bullet time, frozen time hyper-slow orbit", "子弹时间，定格环绕"),
    "time_lapse": ("time lapse, time compressed", "延时摄影，时间压缩"),
    "stop_motion": ("stop motion, frame-by-frame", "定格动画"),
    "split_screen": ("split screen, parallel frames", "分屏"),
    "match_cut": ("match cut, visual continuity", "匹配剪辑"),
    "jump_cut": ("jump cut, discontinuous time", "跳切，压缩时间"),
    "rack_focus": ("rack focus, focus shift between planes", "焦点转移，前后景虚焦切换"),
}

#: 归属（写进提示词/文档，让"交给相关职业"有据可查）
OWNERS: dict[str, str] = {
    "shot_size": "storyboard_artist",
    "camera_move": "storyboard_artist",
    "motion_amplitude": "storyboard_artist",
    "angle": "director (cameraDefaults) / camera_operator (per-shot)",
    "special": "camera_operator",
    "cut_rhythm": "post_production (剪辑组)",
}

#: 景区别名（含中文、旧版 ir_bridge 键）
_SIZE_ALIASES: dict[str, str] = {
    **{k: k for k in SHOT_SIZES},
    "ecu": "extreme_close", "大特写": "extreme_close", "极特写": "extreme_close",
    "特写": "close", "近景": "medium_close", "中近景": "medium_close",
    "中景": "medium", "中全景": "medium_full", "全景": "full",
    "远景": "long", "大远景": "extreme_long", "极远景": "extreme_long",
    "wide": "long", "extreme_wide": "extreme_long",   # 旧词表兼容
    "double": "two_shot", "双人": "two_shot", "双人镜头": "two_shot",
    "群像": "group_shot", "插入": "insert", "插入镜头": "insert",
    "反应镜头": "reaction", "反应": "reaction",
    "定场": "establishing", "定场镜头": "establishing",
    "空镜": "empty", "空镜头": "empty",
}

#: 运镜别名（含中文、旧版 ir_bridge 键）
_MOVE_ALIASES: dict[str, str] = {
    **{k: k for k in CAMERA_MOVES},
    "fixed": "static", "固定": "static", "固定镜头": "static",
    "推镜头": "push_in", "推": "push_in", "dolly_in": "push_in",
    "拉镜头": "pull_out", "拉": "pull_out", "dolly_out": "pull_out",
    "摇镜头": "pan_left", "摇": "pan_left", "pan": "pan_left",
    "上摇": "tilt_up", "下摇": "tilt_down", "tilt": "tilt_up",
    "甩镜头": "whip_pan", "甩": "whip_pan", "whip": "whip_pan",
    "移镜头": "truck_left", "移": "truck_left", "truck": "truck_left",
    "升降镜头": "rise", "升": "rise", "降": "fall",
    "环绕镜头": "orbit", "环绕": "orbit", "arc": "orbit",
    "跟镜头": "follow", "跟拍": "follow", "跟": "follow",
    "斯坦尼康": "steadicam", "摇臂": "crane_up", "航拍": "aerial",
    "无人机": "aerial", "变焦推": "zoom_in", "变焦拉": "zoom_out",
    "希区柯克变焦": "dolly_zoom", "变焦": "dolly_zoom", "dolly-zoom": "dolly_zoom",
    "震动镜头": "handheld", "手持": "handheld",
    "快速切换": "fast_cut", "慢动作": "slow_motion",
    "长镜头": "long_take", "一镜到底": "long_take",
}

_AMP_ALIASES: dict[str, str] = {
    **{k: k for k in MOTION_AMPLITUDES},
    "小": "small", "小幅": "small", "小动态": "small", "稳": "small",
    "中": "medium", "中等": "medium", "中等动态": "medium",
    "大": "large", "大幅": "large", "大动态": "large", "快速运动": "large",
}


def _norm(v: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(v or "").strip().lower())


def _canon(table: dict, aliases: dict, value: Any) -> str:
    raw = _norm(value)
    if not raw:
        return ""
    got = aliases.get(raw)
    if got is None:
        raise ValueError(f"未知取值 {value!r}；可用：{list(table)} 或中文名")
    return got


def canonical_shot_size(value: Any) -> str:
    return _canon(SHOT_SIZES, _SIZE_ALIASES, value)


def canonical_move(value: Any) -> str:
    return _canon(CAMERA_MOVES, _MOVE_ALIASES, value)


def canonical_motion_amplitude(value: Any) -> str:
    return _canon(MOTION_AMPLITUDES, _AMP_ALIASES, value)


def _prompt(table: dict, value: Any, language: str, canonical) -> str:
    try:
        key = canonical(value)
    except ValueError:
        return str(value or "")
    if not key:
        return ""
    return table[key][1 if str(language).lower().startswith("zh") else 0]


def shot_size_prompt(value: Any, language: str = "en") -> str:
    return _prompt(SHOT_SIZES, value, language, canonical_shot_size)


def move_prompt(value: Any, language: str = "en") -> str:
    return _prompt(CAMERA_MOVES, value, language, canonical_move)


def amplitude_prompt(value: Any, language: str = "en") -> str:
    return _prompt(MOTION_AMPLITUDES, value, language, canonical_motion_amplitude)


def validate_shot_terms(*, shot_size: Any = "", move: Any = "",
                        amplitude: Any = "") -> dict[str, str]:
    """严格校验（未知即抛）；返回归一结果，供写入 IR 前检查。"""
    out = {}
    if str(shot_size or "").strip():
        out["shotSize"] = canonical_shot_size(shot_size)
    if str(move or "").strip():
        out["cameraMove"] = canonical_move(move)
    if str(amplitude or "").strip():
        out["motionAmplitude"] = canonical_motion_amplitude(amplitude)
    return out
