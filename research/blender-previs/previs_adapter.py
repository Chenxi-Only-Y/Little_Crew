"""把 `PrevisAgent` 的布局 JSON 适配成 Blender 的 `shot.json`，并调 Blender 出片。

## 两边的格式差（这是接线真正的工作量）

| 项 | 布局（我方） | Blender spec（既有脚本） |
|---|---|---|
| 角色 | **`subjects` 列表**（可多个） | **`character` 单块**（只建一个可绑骨的人） |
| 身高 | 每个 subject 各自 `height_cm` | 只有全局一个 |
| 站位 | 每个 subject 的 `x` / `z` | ❌ **没有**（角色固定在原点） |
| 输出目录 / 分辨率 | ❌ 没有 | **必须给** |

## 所以适配器必须**明确说清丢了什么**

既有 `build_character.py` 只建**一个**角色、且**固定站在原点** ——
它支持不了"两个人隔着半个房间"。

**适配器不假装支持**：取第一个 subject 当主体，其余的记进
`droppedSubjects`，并返回一条 `warning`。

> 一条明说的限制，好过一条悄悄丢掉的字段。

## 这一步验什么

**验"布局 → Blender → 视频"这条路通不通**，不是验多人渲染。
多人渲染需要另写一个最小渲染器（新范围下反而更简单），记为下一步。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

__all__ = ["layout_to_spec", "render_layout"]

#: 新范围下**不再需要**的角色参数（姿态/纹理都不在范围）。
#: 保留是因为既有 `build_character.py` 的签名要求它们。
_DEFAULT_CHARACTER: dict[str, Any] = {
    "height_cm": 168, "build": "slim", "head_ratio": 7.6,
    "shoulder_width_m": 0.40, "hair": "long_straight",
    "arm_spread": 7.0, "arm_bend": 10.0, "stance_width": 1.0,
}

#: 默认分辨率 —— 与出图一致，否则首帧做 ControlNet 会被缩放、构图偏移。
DEFAULT_RESOLUTION: tuple[int, int] = (640, 384)


def layout_to_spec(layout: Mapping[str, Any], *,
                   out_dir: str,
                   resolution: tuple[int, int] = DEFAULT_RESOLUTION,
                   fps: int = 24) -> tuple[dict[str, Any], list[str]]:
    """布局 → Blender spec。返回 `(spec, warnings)`。

    **警告必须被调用方看到** —— 丢掉的角色、被忽略的字段都进 `warnings`。
    """
    warns: list[str] = []
    shot = dict(layout.get("shot") or {})
    subs = list(layout.get("subjects") or ())

    # ---- 角色：只保留一个，其余记下来 ----
    primary = subs[0] if subs else {}
    char = dict(_DEFAULT_CHARACTER)
    h = float(primary.get("height_cm") or 0) or 168.0
    char["height_cm"] = h
    # **全部角色都进 spec**（`render_previs_simple.py` 支持 N 个）
    subj_out = [{"who": str(s.get("who") or f"P{i}"),
                 "x": float(s.get("x") or 0.0),
                 "z": float(s.get("z") or 0.0),
                 "height_cm": float(s.get("height_cm") or 168.0)}
                for i, s in enumerate(subs)]
    if not subj_out:
        warns.append("布局里没有 subjects —— 渲染器会拒绝")

    # ---- 站位：既有脚本不能摆位置 ----
    # 站位由 `subjects` 表达（`render_previs_simple.py` 按 x/z 摆人）

    # ---- 运动：新范围不走路，只保留机位运动 ----
    move = str(shot.get("camera_movement") or "static")
    spec = {
        "outDir": str(out_dir),
        "resolution": {"width": int(resolution[0]), "height": int(resolution[1])},
        "character": char,
        "shot": {
            "shot_size": str(shot.get("shot_size") or "medium"),
            "lens_mm": float(shot.get("lens_mm") or 50.0),
            "angle_deg": float(shot.get("angle_deg") or 0.0),
            "azimuth_deg": float(shot.get("azimuth_deg") or 0.0),
            "duration_sec": float(shot.get("duration_sec") or 3.0),
            "camera_movement": move,
            # 新范围：不做角色路径（不需要姿态/走位细节）
            "path": "stand",
        },
        "fps": int(fps),
        # ⚠️ **`subjects` 是主路径** —— 由 `render_previs_simple.py` 消费。
        # 保留 `character` 是给旧脚本（`render_previs.py`）的兼容块，新脚本忽略它。
        "subjects": subj_out,
    }
    return spec, warns


def render_layout(layout: Mapping[str, Any], *, blender: str,
                  script: str, out_root: str,
                  encoder: str | None = None,
                  resolution: tuple[int, int] = DEFAULT_RESOLUTION,
                  fps: int = 24) -> dict[str, Any]:
    """完整跑一镜：布局 → spec → Blender → 帧 → mp4 + 首帧。

    返回 `{"ok", "mp4", "first", "warnings", "frames", "reason"}`。
    """
    sid = str(layout.get("shotId") or "shot")
    out_dir = Path(out_root) / sid
    spec, warns = layout_to_spec(layout, out_dir=str(out_dir),
                                 resolution=resolution, fps=fps)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "shot.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    r = subprocess.run(
        [blender, "--background", "--factory-startup",
         "--python", script, "--", str(spec_path)],
        capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        return {"ok": False, "warnings": warns,
                "reason": f"Blender 失败（{r.returncode}）：{r.stderr[-300:]}"}
    if "RENDER_DONE" not in (r.stdout or ""):
        return {"ok": False, "warnings": warns,
                "reason": "Blender 没输出 RENDER_DONE —— 可能没渲出帧"}

    gray = out_dir / "gray"
    if encoder is None:
        return {"ok": False, "warnings": warns,
                "reason": "没有编码器路径，帧已渲好但未编码",
                "frames": len(list(gray.glob("f*.png")))}
    e = subprocess.run(
        [sys.executable, encoder, str(gray), str(out_dir / "previs"), str(fps)],
        capture_output=True, text=True, timeout=900)
    if e.returncode != 0:
        return {"ok": False, "warnings": warns,
                "reason": f"编码失败：{(e.stdout or '')[-200:]}{(e.stderr or '')[-200:]}"}
    return {"ok": True, "mp4": str(out_dir / "previs.mp4"),
            "first": str(out_dir / "previs_first.png"),
            "warnings": warns, "reason": "",
            "frames": len(list(gray.glob("f*.png")))}
