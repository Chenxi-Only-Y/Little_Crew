"""AI 生成**预演初稿**（3D 白模场景）—— 白模预演师的"起手式"。

## 在这条链上的位置

    分镜 + 场景白模 + 角色参考图
        → **本模块**（LLM 出一份 `PrevisScene` JSON 初稿）
        → 预演编辑器（`#/previs`）人工微调：摆走位、调机位、录运镜
        → 逐帧导出 → `POST /api/video/whitebox` 编码 mp4 → 当视频模型的参考

用户的话："目前的预演相当于人工修改的" —— 所以这里生成的是**初稿**，
人工再改。这正是本项目一贯的做法（LLM 出 JSON 规格，前端渲染；见
`white_model_prompt.py` 的白模同理）。

## 为什么让 LLM 出 JSON 而不是算法摆位

"谁站在哪、机位怎么走"依赖**剧情理解**（"他退到门口"、"镜头从背后推近"），
不是纯几何问题。算法只能摆出等距网格，LLM 能读懂分镜。而 JSON 又保证
**不可能执行任意代码**、**可校验**、**人可改**、**可复现**。

## 与前端类型的一致性

产出的结构必须与 `web/src/previs/types.ts` 的 `PrevisScene` 对齐：

    {ground:[w,d], actors:[Actor], props:[Prop], shots:[Shot]}
    Actor = {id,name,position:[x,y,z],yaw,scale,pose,poseSpeed,posePhase,color}
    Shot  = {id,name,duration,fps,cameraTracks:[{id,name,keys:[Keyframe]}]}
    Keyframe = {id,time,position,target,fov}
    pose 只能取：idle / walk / run / sit / wave / point
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("little_crew")

#: 姿态预设的合法值 —— 与前端 `POSE_PRESETS`（`previs/humanoid.ts`）一致。
#: 模型只能从这里选；给了别的会被归一化时替换掉（不因为一个字段丢掉整份初稿）。
POSE_PRESETS: tuple[str, ...] = ("idle", "walk", "run", "sit", "wave", "point")

_SYSTEM = """你是影视预演（previs）师，负责把分镜变成**3D 白模场景的初稿**。

白模是"没有材质贴图的灰白体块"，用途是让视频模型知道**谁在哪、镜头怎么走**，
解决它自己编空间导致的漂移/瞬移/比例失控。

## 坐标约定（必须遵守）

    右手系：+X 右、+Y 上、+Z 朝镜头。
    长度单位**米**。人物脚底贴在 y=0。人身高约 1.7 米（scale=1.0）。
    yaw 是绕 Y 轴的弧度，**0 = 面向 -Z**。

## 输出格式（只输出 JSON，不要解释）

{
  "ground": [20, 20],
  "actors": [
    {"id": "a1", "name": "小明", "position": [0, 0, 0], "yaw": 0,
     "scale": 1.0, "pose": "idle", "poseSpeed": 1.0, "posePhase": 0,
     "color": "#8ab4f8"}
  ],
  "props": [
    {"id": "p1", "name": "桌子", "position": [0, 0, -3], "yaw": 0,
     "size": [1.2, 0.75, 0.8], "color": "#9aa0a6"}
  ],
  "shots": [
    {"id": "s1", "name": "镜头1", "duration": 4, "fps": 24,
     "cameraTracks": [
       {"id": "t1", "name": "主机位", "keys": [
         {"id": "k1", "time": 0, "position": [0, 1.6, 5], "target": [0, 1.0, 0], "fov": 40},
         {"id": "k2", "time": 4, "position": [0, 1.6, 3], "target": [0, 1.0, 0], "fov": 40}
       ]}
     ]}
  ]
}

## 硬性要求

- `pose` 只能取：idle / walk / run / sit / wave / point
- 每个 shot 的 `cameraTracks` 至少一条，每条至少 2 个关键帧（否则没有运镜）
- 关键帧 `time` 必须落在 [0, duration] 内，**首帧 time=0、末帧 time=duration**
- 不要让人物重叠（间距 ≥ 0.8 米），不要让机位穿进地面（position 的 y ≥ 0.3）
- `ground` 要能装下所有人物与道具，别给太小
- 只输出 JSON，不要 markdown 包裹
"""


def _norm_pose(v: Any) -> str:
    p = str(v or "").strip().lower()
    return p if p in POSE_PRESETS else "idle"


def _num(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _vec3(v: Any, default: tuple[float, float, float]) -> list[float]:
    if isinstance(v, (list, tuple)) and len(v) >= 3:
        return [_num(v[0], default[0], -1e4, 1e4),
                _num(v[1], default[1], -1e4, 1e4),
                _num(v[2], default[2], -1e4, 1e4)]
    return list(default)


def normalize_scene(raw: Any) -> tuple[dict, list[str]]:
    """把模型输出归一化成 `PrevisScene`，返回 `(scene, 修复说明)`。

    **尽力保住初稿**：某个字段不合法就修它（而不是丢掉整份），
    并把修了什么记进说明 —— 用户能看到"哪里被自动纠正了"。
    """
    notes: list[str] = []
    if not isinstance(raw, dict):
        return {"ground": [20, 20], "actors": [], "props": [], "shots": []}, ["输出不是对象"]

    ground = raw.get("ground")
    if not (isinstance(ground, (list, tuple)) and len(ground) >= 2):
        ground = [20, 20]
        notes.append("ground 缺失，用默认 [20,20]")
    ground = [_num(ground[0], 20, 4, 400), _num(ground[1], 20, 4, 400)]

    actors: list[dict] = []
    for i, a in enumerate(raw.get("actors") or []):
        if not isinstance(a, dict):
            continue
        pose = a.get("pose")
        if str(pose or "").lower() not in POSE_PRESETS:
            notes.append(f"角色{i + 1} 的 pose={pose!r} 不在预设里，改成 idle")
        actors.append({
            "id": str(a.get("id") or f"a{i + 1}"),
            "name": str(a.get("name") or f"角色{i + 1}"),
            "position": _vec3(a.get("position"), (0.0, 0.0, 0.0)),
            "yaw": _num(a.get("yaw"), 0.0, -6.2832, 6.2832),
            "scale": _num(a.get("scale"), 1.0, 0.3, 3.0),
            "pose": _norm_pose(pose),
            "poseSpeed": _num(a.get("poseSpeed"), 1.0, 0.1, 4.0),
            "posePhase": _num(a.get("posePhase"), 0.0, 0.0, 1.0),
            "color": str(a.get("color") or "#8ab4f8"),
        })
    # 脚底贴地：y 一律压到 0（模型常给错）
    for a in actors:
        if abs(a["position"][1]) > 1e-6:
            a["position"][1] = 0.0
    if actors:
        notes.append("所有人物的 y 已对齐到 0（脚底贴地）")

    props: list[dict] = []
    for i, p in enumerate(raw.get("props") or []):
        if not isinstance(p, dict):
            continue
        props.append({
            "id": str(p.get("id") or f"p{i + 1}"),
            "name": str(p.get("name") or f"道具{i + 1}"),
            "position": _vec3(p.get("position"), (0.0, 0.0, -3.0)),
            "yaw": _num(p.get("yaw"), 0.0, -6.2832, 6.2832),
            "size": _vec3(p.get("size"), (1.0, 1.0, 1.0)),
            "color": str(p.get("color") or "#9aa0a6"),
        })

    shots: list[dict] = []
    for si, sh in enumerate(raw.get("shots") or []):
        if not isinstance(sh, dict):
            continue
        dur = _num(sh.get("duration"), 4.0, 0.5, 30.0)
        tracks_in = sh.get("cameraTracks")
        if not isinstance(tracks_in, list) or not tracks_in:
            tracks_in = [{"id": "t1", "name": "主机位", "keys": [
                {"time": 0, "position": [0, 1.6, 5], "target": [0, 1.0, 0]},
                {"time": dur, "position": [0, 1.6, 5], "target": [0, 1.0, 0]},
            ]}]
            notes.append(f"镜头{si + 1} 没有机位，补了一个默认机位")
        tracks: list[dict] = []
        for ti, t in enumerate(tracks_in):
            if not isinstance(t, dict):
                continue
            keys_in = t.get("keys") if isinstance(t.get("keys"), list) else []
            keys: list[dict] = []
            for ki, k in enumerate(keys_in):
                if not isinstance(k, dict):
                    continue
                pos = _vec3(k.get("position"), (0.0, 1.6, 5.0))
                if pos[1] < 0.3:
                    pos[1] = 0.3      # 别穿地
                keys.append({
                    "id": str(k.get("id") or f"k{ki + 1}"),
                    "time": _num(k.get("time"), 0.0, 0.0, dur),
                    "position": pos,
                    "target": _vec3(k.get("target"), (0.0, 1.0, 0.0)),
                    "fov": _num(k.get("fov"), 40.0, 10.0, 120.0),
                })
            keys.sort(key=lambda x: x["time"])
            if len(keys) < 2:
                # 少于 2 帧就没有运镜 —— 补一帧（保持不动也比坏掉好）
                base = keys[0] if keys else {
                    "id": "k1", "time": 0.0, "position": [0.0, 1.6, 5.0],
                    "target": [0.0, 1.0, 0.0], "fov": 40.0}
                keys = [
                    {**base, "id": "k1", "time": 0.0},
                    {**base, "id": "k2", "time": dur},
                ]
                notes.append(f"镜头{si + 1} 的机位关键帧不足 2 个，已补齐")
            else:
                # 首末帧时间对齐到 [0, duration]
                if abs(keys[0]["time"]) > 1e-6:
                    keys[0] = {**keys[0], "time": 0.0}
                    notes.append(f"镜头{si + 1} 的首帧时间对齐到 0")
                if abs(keys[-1]["time"] - dur) > 1e-6:
                    keys[-1] = {**keys[-1], "time": dur}
                    notes.append(f"镜头{si + 1} 的末帧时间对齐到 {dur}")
            tracks.append({
                "id": str(t.get("id") or f"t{ti + 1}"),
                "name": str(t.get("name") or f"机位{ti + 1}"),
                "keys": keys,
            })
        shots.append({
            "id": str(sh.get("id") or f"s{si + 1}"),
            "name": str(sh.get("name") or f"镜头{si + 1}"),
            "duration": dur,
            "fps": int(_num(sh.get("fps"), 24, 6, 60)),
            "cameraTracks": tracks,
        })

    return {"ground": ground, "actors": actors, "props": props, "shots": shots}, notes


def _storyboard_digest(storyboard: Any, scene_whitebox: Any = None,
                       characters: Any = None) -> str:
    """把分镜/场景/角色压成给模型看的紧凑描述。"""
    import json

    lines: list[str] = []

    if characters:
        lines.append("【角色】")
        lines.append(json.dumps(characters, ensure_ascii=False)[:1500])
        lines.append("")

    if scene_whitebox:
        lines.append("【场景白模（已有）】")
        lines.append(json.dumps(scene_whitebox, ensure_ascii=False)[:1200])
        lines.append("")

    lines.append("【分镜】")
    if isinstance(storyboard, list):
        for sh in storyboard[:24]:
            if isinstance(sh, dict):
                sid = sh.get("shot_id") or sh.get("id") or "?"
                lines.append(f"  {sid}: {json.dumps(sh, ensure_ascii=False)[:400]}")
            else:
                lines.append(f"  {str(sh)[:200]}")
    elif isinstance(storyboard, dict):
        lines.append(json.dumps(storyboard, ensure_ascii=False)[:3000])
    else:
        lines.append(str(storyboard)[:2000])

    return "\n".join(lines)


def generate_scene(storyboard: Any, *, scene_whitebox: Any = None,
                   characters: Any = None, note: str = "",
                   role: str = "previs_artist") -> dict:
    """生成预演初稿。

    Returns:
        `{"status": "ok", "scene": {...}, "notes": [...], "attempts": 1|2}`
        或 `{"status": "error", "message": ...}`。

    **出错不抛异常**（与 `white_model_prompt.generate_spec` 一致）：
    让 API 层决定怎么呈现；预演页仍可人工从空白开始搭。
    """
    from app.services.llm import get_llm

    try:
        llm = get_llm()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": f"取模型失败：{exc}"}

    if getattr(llm, "mock", False):
        # mock：给一个**明确标注**的最小可用场景，不假装是分析结果
        scene, _n = normalize_scene({
            "ground": [12, 12],
            "actors": [{"id": "a1", "name": "主角", "position": [0, 0, 0],
                        "yaw": 0, "pose": "idle"}],
            "props": [],
            "shots": [{"id": "s1", "name": "镜头1", "duration": 4, "fps": 24,
                       "cameraTracks": [{"id": "t1", "name": "主机位", "keys": [
                           {"time": 0, "position": [0, 1.6, 5],
                            "target": [0, 1.0, 0], "fov": 40},
                           {"time": 4, "position": [0, 1.6, 3.5],
                            "target": [0, 1.0, 0], "fov": 40}]}]}],
        })
        return {"status": "ok", "scene": scene, "attempts": 1,
                "notes": ["**mock 模式**：这是占位场景，不是按你的分镜生成的"]}

    user = _storyboard_digest(storyboard, scene_whitebox, characters)
    if note.strip():
        user += f"\n\n【额外要求】\n{note.strip()}"
    user += "\n\n请输出预演初稿 JSON。"

    def _call(u: str) -> tuple[dict | None, str]:
        try:
            raw = llm.chat_json(_SYSTEM, u, role=role)
        except Exception as exc:  # noqa: BLE001
            return None, f"调用模型失败：{exc}"
        if isinstance(raw, dict) and raw.get("_parse_error"):
            return None, f"输出不是合法 JSON：{raw.get('error')}"
        if not isinstance(raw, dict):
            return None, "输出不是 JSON 对象"
        return raw, ""

    raw, err = _call(user)
    if raw is None:
        return {"status": "error", "message": err}

    scene, notes = normalize_scene(raw)

    # 空场景 = 没生成出东西 → 回灌错误重试一次（与白模规格同一套做法）
    if not scene["actors"] and not scene["shots"]:
        user2 = (
            user + f"\n\n你上一次的输出没有可用内容（{err or '既没有 actors 也没有 shots'}）。\n"
                   f"请**至少给出 1 个 actor 和 1 个 shot**，只输出 JSON。"
        )
        raw2, err2 = _call(user2)
        if raw2 is not None:
            scene2, notes2 = normalize_scene(raw2)
            if scene2["actors"] or scene2["shots"]:
                return {"status": "ok", "scene": scene2, "attempts": 2,
                        "notes": notes2}
        return {"status": "error",
                "message": f"模型没有给出可用的场景（{err or err2}）"}

    return {"status": "ok", "scene": scene, "attempts": 1, "notes": notes}


__all__ = ["generate_scene", "normalize_scene", "POSE_PRESETS", "_SYSTEM"]
