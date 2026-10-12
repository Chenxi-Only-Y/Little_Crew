"""白模预演师**节点** —— 把 AI 初稿生成接进工作流。

## 为什么要有这个节点

用户的要求（原话）："把「预演」构成「白模预演师」一部分，目前的预演相当于
人工修改的" + "AI 生成初稿，人工微调"。

在这之前的状态是：

    · 注册表**声明**了 `previs_artist`（第 11 步，能力 `render_3d`，产出 `previs`）
    · 但 `workflow_node` 是空的 → **它不在执行图里**
    · `#/previs` 是个**独立页面**，要人自己打开、从空白开始摆

所以"预演"和"白模预演师"是脱节的：一个声明了不执行，一个是手动工具。

本节点补上中间那一步：**读分镜 → 出 3D 白模初稿 → 落到 `state["previs"]`**，
用户在预演编辑器里**微调**它（那才是"人工修改"的位置）。

## 失败要**软着陆**

预演是"锦上添花"的结构输入，不是必经产物。推理服务没配、模型抽风、
输出不合法 —— 都不该让整条工作流挂掉。所以这里：

    · 生成失败 → 记 `state["previs"]["error"]`，**继续往下跑**；
    · 其它节点要 previs 时看到空/错就自己降级（与现在"没有 previs 也能跑"一致）。
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("little_crew")


def generate_previs_scene(state: dict) -> dict:
    """白模预演师节点：生成预演初稿，写进 `state["previs"]`。

    Args:
        state: `CrewState`；用到 `storyboard` / `scene_whitebox` / `character_profiles`。

    Returns:
        `{"previs": {...}, "current_phase": "previs_artist", "thinking_log": [...]}`
        出错时 `previs.error` 里有原因，但**不抛异常**。
    """
    from app.services.previs_prompt import generate_scene

    storyboard = state.get("storyboard") or []
    phase = "previs_artist"

    if not storyboard:
        # 没有分镜就没有可预演的镜头 —— 如实说明，不算失败
        logger.info("[previs] 没有分镜，跳过预演初稿生成")
        return {
            "previs": {"scene": None, "generated_at": time.time(),
                       "error": "", "skipped": "没有分镜，无从预演"},
            "current_phase": phase,
            "thinking_log": [{"label": phase,
                              "text": "没有分镜，跳过预演初稿生成。"}],
        }

    res = generate_scene(
        storyboard,
        scene_whitebox=state.get("scene_whitebox") or None,
        characters=state.get("character_profiles") or None,
    )

    if res.get("status") != "ok":
        msg = str(res.get("message") or "未知原因")
        logger.warning("[previs] 生成初稿失败：%s", msg)
        return {
            "previs": {"scene": None, "generated_at": time.time(),
                       "error": msg},
            "current_phase": phase,
            "thinking_log": [
                {"label": phase,
                 "text": f"预演初稿生成失败（{msg}）。"
                         f"可以在预演编辑器里从空白开始手动搭。"},
            ],
        }

    scene = res.get("scene") or {}
    n_actors = len(scene.get("actors") or [])
    n_props = len(scene.get("props") or [])
    n_shots = len(scene.get("shots") or [])
    notes = res.get("notes") or []

    text = (f"生成了预演初稿：{n_shots} 个镜头、{n_actors} 个人物、{n_props} 个道具。"
            f"可在预演编辑器里微调走位与机位。")
    if notes:
        text += f"（自动纠正 {len(notes)} 处：{notes[0]}）"

    logger.info("[previs] 初稿已生成：%d 镜头 / %d 人物", n_shots, n_actors)
    return {
        "previs": {
            "scene": scene,
            "notes": notes,
            "attempts": res.get("attempts", 1),
            "generated_at": time.time(),
            "error": "",
        },
        "current_phase": phase,
        "thinking_log": [{"label": phase, "text": text}],
    }
