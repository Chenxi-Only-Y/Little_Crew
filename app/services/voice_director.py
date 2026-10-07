"""配音指导**节点** —— 把分镜里的台词合成成逐镜头音频（TTS）。

## 为什么要有它

用户要求"1、2 先做"，其中 ① 是**配音指导**。改之前的状态：

    · 注册表**声明**了 `voice_director`（能力 `tts`/`music`，产出 `voice_track`）
    · 但它的 `workflow_node` 是空的 → **从没进过执行图**
    · 更糟：`voice_track` / `bgm_track` 这两个字段在 `CrewState` 里**根本不存在**

也就是说界面上它像流程的一步，实际既不在图里、产物也没地方放。

## 这一步只做**对白**（TTS）；BGM 不做

`music` 能力在提供者表里是空的（已知空缺）—— 所以 `bgm_track` 留空并**写明原因**，
而不是假装做了。

## 失败要**软着陆**

配音不该让整条流程挂掉（用户可以后期自己配）。所以：

    · 某句没台词 → 记 `skipped`（不是失败）
    · 合成失败 → 记 `failed` + 原因，继续下一句
    · 引擎不可用（edge-tts 没装）→ 整轨记一个可读原因，不抛异常
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("little_crew")


def _dialogue_of(shot: dict) -> str:
    """从分镜条目里取台词。

    不同来源的字段名不完全一致（`dialogue` / `line` / `lines`），
    这里都认一下 —— 取不到就当这句没有台词（跳过，不算失败）。
    """
    if not isinstance(shot, dict):
        return ""
    for key in ("dialogue", "line", "lines", "text"):
        v = shot.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            joined = " ".join(str(x).strip() for x in v if str(x).strip())
            if joined:
                return joined
    return ""


def synthesize_voice_track(state: dict) -> dict:
    """配音指导节点：逐镜头合成对白音频，写进 `state["voice_track"]`。

    Returns:
        `{"voice_track": [...], "bgm_track": [], "current_phase": "voice_director"}`
        —— 出错时**不抛异常**，在条目里记 `status`/`reason`。
    """
    from app.services import tts

    phase = "voice_director"
    storyboard = state.get("storyboard") or []
    if not storyboard:
        return {"voice_track": [], "bgm_track": [], "current_phase": phase}

    # 有没有台词可配？没有就别去碰引擎（省得报"引擎不可用"吓人）
    with_line = [(s, _dialogue_of(s)) for s in storyboard if isinstance(s, dict)]
    with_line = [(s, t) for s, t in with_line if t]
    if not with_line:
        logger.info("[voice] 分镜里没有台词，跳过配音")
        return {"voice_track": [], "bgm_track": [], "current_phase": phase,
                "voice_track_note": "分镜里没有台词（dialogue 为空），无需配音"}

    if not tts.available():
        reason = "配音引擎不可用：" + tts._hint().splitlines()[0]
        logger.warning("[voice] %s", reason)
        return {
            "voice_track": [{"shot_id": "", "text": "", "path": "",
                             "voice": "", "status": "failed", "reason": reason}],
            "bgm_track": [],
            "current_phase": phase,
        }

    # 音色 / 语速从设置读（与设置页「配音」那一组一致）
    voice = ""
    speed = 1.0
    try:
        from app.services import runtime_settings as rs

        voice = str(rs.get("tts_voice") or "").strip()
        speed = float(rs.get("tts_speed") or 1.0)
    except Exception:  # noqa: BLE001  设置读不到就用默认
        pass

    from app.services.asset_paths import asset_path

    out_dir = ""
    try:
        from app.core.config import settings

        out_dir = str(settings.output_dir_path)
    except Exception:  # noqa: BLE001
        out_dir = "outputs"

    session = str(state.get("session_id") or "default")
    items: list[dict] = []
    ok = 0
    for shot, text in with_line:
        sid = str(shot.get("shot_id") or shot.get("id") or "")
        # 文件名用 shot_id；取不到就按序号（避免所有条目写同一个文件互相覆盖）
        fname = f"{sid or f'shot{len(items) + 1}'}.mp3"
        try:
            path = asset_path(out_dir, "voice_director", "audio", fname,
                              session=session)
            res = tts.synthesize(text, path, voice=voice, speed=speed)
            items.append({"shot_id": sid, "text": text, "path": str(path),
                          "voice": res.voice, "status": "ok", "reason": ""})
            ok += 1
        except Exception as exc:  # noqa: BLE001  单句失败不拖垮整轨
            logger.warning("[voice] 镜头 %s 配音失败：%s", sid, exc)
            items.append({"shot_id": sid, "text": text, "path": "",
                          "voice": voice, "status": "failed",
                          "reason": f"{type(exc).__name__}: {exc}"})

    logger.info("[voice] 配音完成：%d/%d 句", ok, len(items))
    return {"voice_track": items, "bgm_track": [], "current_phase": phase}
