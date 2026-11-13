"""后期统筹（`post_production`）：定后期方案，产出 `post.plan`（2026-11-13）。

## 为什么合并成一个职位

原来是两个职位：**剪辑师**（定节奏，`pacing.plan`）与**合成剪辑**（拼接成片）。
问题是：
- 剪辑师"节奏好不好"没有可测判据（对照实验里还让结果变差 4.62 vs 3.79）；
- 合成剪辑是纯执行（ffmpeg），本来就不该占职位。

合并后：**一个职位定方案 + 一份可读计划**，执行仍归代码：
```
字幕（样式/行数/安全区）· 转场（同场 cut / 跨场 fade / 跳接 dissolve）
BGM（增益/淡入淡出/对白闪避）· 音轨策略（native | tts+bgm）
响度目标（-16 LUFS 两遍）· 调色策略（暂不执行，仅记录目标）
```

## 纪律

- **不调模型**（ASSEMBLY）：全部是确定性规则，可由计划复现
- **音轨策略二选一**：`native`（视频模型自带音轨，H3 FL2VA）| `tts+bgm`（静帧路径）
- 计划里每个数字都要**能对上已有实现**（响度 -16 是已有两遍归一的实际目标）
"""

from __future__ import annotations

from typing import Any, Mapping

#: 响度目标（与成片阶段的 EBU R128 两遍归一一致）
TARGET_LUFS = -16.0
#: 字幕规格（烧录字幕：单行 16 字、最多 2 行、底部安全区 8%）
SUB_SPEC = {"style": "burned", "maxCharsPerLine": 16, "maxLines": 2,
            "safeAreaBottom": 0.08}
#: BGM 默认参数
BGM_SPEC = {"gainDb": -22.0, "fadeInS": 2.0, "fadeOutS": 3.0, "duckingDb": -6.0}
#: 转场规则：同场硬切，换场淡入淡出，跳接叠化
TRANSITIONS = {"sameScene": "cut", "sceneChange": "fade", "timeJump": "dissolve"}


def _scene_of(shot: Any) -> str:
    for key in ("scene_id", "sceneId", "scene"):
        v = getattr(shot, key, None) if not isinstance(shot, Mapping) else shot.get(key)
        if v:
            return str(v)
    sid = str(getattr(shot, "shot_id", "") if not isinstance(shot, Mapping)
              else shot.get("shot_id") or "")
    # SH-EF40-S01-004 -> S01
    parts = sid.split("-")
    return parts[2] if len(parts) >= 3 else ""


def build_plan(payload: Mapping[str, Any]) -> dict[str, Any]:
    """从镜头表 + 剧本 + 运行模式生成后期计划（**纯函数**，可复现）。"""
    shots = list(payload.get("shot.list") or payload.get("shots") or ())
    script = payload.get("script.final")
    script_text = str(getattr(script, "text", None) or script or "")
    native_audio = bool(payload.get("nativeAudio", False))

    # 转场：按相邻镜头的场次是否变化决定
    seq = []
    prev_scene = None
    for i, s in enumerate(shots):
        scene = _scene_of(s)
        if i == 0:
            kind = "cut"
        elif scene and prev_scene and scene != prev_scene:
            kind = TRANSITIONS["sceneChange"]
        else:
            kind = TRANSITIONS["sameScene"]
        seq.append({"index": i + 1, "type": kind,
                    "fromScene": prev_scene, "toScene": scene})
        prev_scene = scene or prev_scene

    # 字幕条数估算：按场次内的对白行（剧本里以"xxx："或引号开头）
    lines = [ln.strip() for ln in script_text.splitlines() if ln.strip()]
    dialogue = [ln for ln in lines if ("：" in ln or ":" in ln or '"' in ln or '"' in ln)]
    est_subtitles = max(0, len(dialogue))

    return {
        "kind": "post.plan",
        "audioStrategy": "native" if native_audio else "tts+bgm",
        "audioNativeNote": ("视频模型自带音轨（H3 FL2VA），不再叠加 TTS；"
                            "仍做响度归一" if native_audio else
                            "静帧路径：TTS 对白 + BGM，无环境音（H3 才有原生音效）"),
        "subtitle": {**SUB_SPEC, "estimatedCount": est_subtitles},
        "bgm": dict(BGM_SPEC),
        "transitions": {"rules": dict(TRANSITIONS), "sequence": seq,
                        "counts": {k: sum(1 for x in seq if x["type"] == k)
                                   for k in set(x["type"] for x in seq)} if seq else {}},
        "loudness": {"targetLufs": TARGET_LUFS, "approach": "EBU R128 两遍（先扫后归）"},
        "colorGrade": {"executed": False,
                       "note": "调色暂不执行；本字段只记录目标，供后续接 LUT"},
        "shotCount": len(shots),
        "engine": str(payload.get("engine", "") or ""),
    }


class PostProduction:
    """职位实现：把 payload 变成 `post.plan`（不调模型，纯规则）。"""

    produces_key = "post.plan"

    def __call__(self, *, envelope: Any, runtime: Any) -> Any:
        from little_crew.agent.protocol import CallResult

        payload = dict(getattr(envelope, "payload", {}) or {})
        try:
            plan = build_plan(payload)
        except Exception as exc:                              # noqa: BLE001
            return CallResult.fail("F-POST-PLAN", f"{type(exc).__name__}: {exc}")
        return CallResult.success(
            {self.produces_key: plan},
            evidence=(f"{plan['shotCount']} 镜 · 音轨 {plan['audioStrategy']} · "
                      f"字幕约 {plan['subtitle']['estimatedCount']} 条 · "
                      f"响度目标 {plan['loudness']['targetLufs']} LUFS"))
