"""白模预演的生产实现：**布局 → Blender spec → 帧 → mp4 + 首帧**。

## 为什么在 `runtime/` 而不是 `research/`

`src/` 不能 import `research/`（研究脚本不该变成生产依赖）。
这条路要接进 `EpisodeRunner`，所以必须住在 `runtime/`。

## 三块职责

| 块 | 做什么 |
|---|---|
| `layout_to_spec` | 布局 JSON → Blender 的 `shot.json`（**纯函数，可单测**） |
| `encode_frames` / `first_frame` | 帧序列 → mp4 + 首帧（只调 ffmpeg） |
| `render_layout` | 串起来：写 spec → 调 Blender → 编码 |

## 两条不能破的约束

1. **首帧必须来自同一份帧序列** —— 不能"另外画一张首帧"，
   否则白模视频与首帧对不上，下游两个都拿到就矛盾了。
2. **分辨率必须与出图一致**（640×384）—— 否则首帧做 ControlNet 时被缩放，构图偏移。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_RESOLUTION",
    "encode_frames",
    "find_ffmpeg",
    "first_frame",
    "layout_to_spec",
    "render_layout",
    "default_blender_script",
]

#: 与出图一致 —— 不是"随便选一个"。
DEFAULT_RESOLUTION: tuple[int, int] = (640, 384)

#: 帧文件名的匹配前缀（Blender 输出 `f00001.png`）
FRAME_GLOB = "f*.png"


def default_blender_script() -> Path:
    """本包内置的 Blender 侧脚本。**随包发布，不依赖 research/**。"""
    return Path(__file__).resolve().parent / "blender" / "render_previs_simple.py"


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
                   fps: int = 24,
                   form: str = "marker") -> tuple[dict[str, Any], list[str]]:
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
        "form": form,
        "subjects": subj_out,
        # **场景道具**：地面由渲染器无条件画，这里只放剧本里的空间物件。
        # 与 subjects 共用同一套坐标（x = 左右，z = 离相机多远）。
        "set": list(layout.get("set") or ()),
    }
    return spec, warns


def find_ffmpeg() -> str:
    """找 ffmpeg。**优先环境变量，其次 PATH**，都不行就报错 —— 不猜固定路径。"""
    import os

    cand = (os.environ.get("FFMPEG") or "").strip()
    if cand and (Path(cand).is_file() or shutil.which(cand)):
        return cand
    got = shutil.which("ffmpeg")
    if got:
        return got
    raise RuntimeError(
        "找不到 ffmpeg —— 设 `FFMPEG` 环境变量，或把它加进 PATH。"
        "**这条要在编码前就过，不要等到跑完才发现。**")


def _frames(frames_dir: Path) -> list[Path]:
    return sorted(frames_dir.glob(FRAME_GLOB))


def first_frame(frames_dir: Path, out_png: Path) -> Path:
    """把第一帧复制成 `_first.png`。

    ⚠️ **必须是同一份帧序列里的第一帧**，不能另外"画一张首帧" ——
    否则白模视频与首帧可能对不上，下游两个都拿到就矛盾了。
    """
    fs = _frames(frames_dir)
    if not fs:
        raise RuntimeError(f"{frames_dir} 里没有帧（{FRAME_GLOB}）")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fs[0], out_png)
    return out_png


def encode_frames(frames_dir: Path, out_mp4: Path, *, fps: int = 24,
                  ffmpeg: str | None = None) -> Path:
    """帧序列 → mp4（h264，`yuv420p` 保证兼容）。

    **不裁分辨率** —— 帧是多大就是多大，避免"白模分辨率与出图不一致"导致
    首帧做 ControlNet 时被缩放、构图偏移。
    """
    fs = _frames(frames_dir)
    if not fs:
        raise RuntimeError(f"{frames_dir} 里没有帧（{FRAME_GLOB}）")
    exe = ffmpeg or find_ffmpeg()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    # ⚠️ **不能用 `-pattern_type glob`**（2026-10-19 实测）：
    # Windows 版 ffmpeg 对 glob 输入报
    #   `Error opening input file ... \f*.png` / `Function not implemented`
    # 改用 image2 序列模式 `f%05d.png` —— 这是 ffmpeg 的原生序列写法，跨平台可用。
    #
    # `start_number` 必须取**实际首帧编号**（Blender 从 00001 开始），
    # 写死 1 会在帧号不从 1 开始时静默丢帧。
    first = fs[0].stem          # 例如 "f00062"
    digits = "".join(ch for ch in first if ch.isdigit())
    start = int(digits) if digits else 1
    width = len(digits) or 5
    pattern = frames_dir / f"f%0{width}d.png"
    cmd = [
        exe, "-y",
        "-framerate", str(fps),
        "-start_number", str(start), "-i", str(pattern),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
        str(out_mp4),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 编码失败（{r.returncode}）：{r.stderr[-400:]}")
    return out_mp4

def render_layout(layout: Mapping[str, Any], *, blender: str,
                  out_root: str,
                  script: str | None = None,
                  ffmpeg: str | None = None,
                  resolution: tuple[int, int] | None = None,
                  fps: int | None = None,
                  with_control: bool = True,
                  profile: Any = None) -> dict[str, Any]:
    """完整跑一镜：布局 → spec → Blender → 帧 → mp4 + 首帧。

    返回 `{"ok", "mp4", "first", "warnings", "frames", "reason"}`。

    ⚠️ **失败不抛异常，返回 `ok=False` + `reason`。**
    白模是**可选增强**：它坏了不该让整集渲染挂掉，但**必须能被看见**
    （所以 `reason` 要写清楚，不能静默）。
    """
    sid = str(layout.get("shotId") or "shot")
    out_dir = Path(out_root) / sid

    # ---- 按目标视频模型的契约定规格（`alpha.115`）----
    #
    # 给了 `profile` 就用它算出来的分辨率/帧率，并**把时长夹到它的区间**；
    # 没给就退回默认 —— 但默认值 640×384 **喂不进 Seedance**（像素不足），
    # 所以整集跑白模做参考视频时**必须传 profile**。
    prof_dict: dict[str, Any] = {}
    if profile is not None:
        resolution = (int(profile.width), int(profile.height))
        fps = int(profile.fps)
        prof_dict = profile.as_dict()
    elif resolution is None or fps is None:
        resolution = resolution or DEFAULT_RESOLUTION
        fps = fps or 24

    spec, warns = layout_to_spec(layout, out_dir=str(out_dir),
                                 resolution=resolution, fps=fps)
    if profile is not None:
        shot = spec.setdefault("shot", {})
        wanted = float(shot.get("duration_sec") or 0.0) or \
            float((layout.get("shot") or {}).get("duration_sec") or 3.0)
        clamped = profile.clamp_seconds(wanted)
        shot["duration_sec"] = clamped
        if abs(clamped - wanted) > 0.05:
            warns.append(f"时长 {wanted:.1f}s 被夹到 {clamped:.1f}s"
                         f"（目标模型区间）")
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_path = out_dir / "shot.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    script_path = Path(script) if script else default_blender_script()

    try:
        r = subprocess.run(
            [blender, "--background", "--factory-startup",
             "--python", str(script_path), "--", str(spec_path)],
            capture_output=True, text=True, timeout=1800)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "warnings": warns,
                "reason": f"Blender 调用失败：{type(e).__name__}: {e}"[:300]}
    if r.returncode != 0:
        return {"ok": False, "warnings": warns,
                "reason": f"Blender 失败（{r.returncode}）：{r.stderr[-300:]}"}
    if "RENDER_DONE" not in (r.stdout or ""):
        return {"ok": False, "warnings": warns,
                "reason": "Blender 没输出 RENDER_DONE —— 可能没渲出帧"}

    gray = out_dir / "gray"
    n = len(list(gray.glob(FRAME_GLOB)))
    if n == 0:
        return {"ok": False, "warnings": warns,
                "reason": f"{gray} 里没有帧"}
    try:
        mp4 = encode_frames(gray, out_dir / "previs.mp4", fps=fps, ffmpeg=ffmpeg)
        png = first_frame(gray, out_dir / "previs_first.png")
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "warnings": warns, "frames": n,
                "reason": f"编码失败：{type(e).__name__}: {e}"[:300]}
    result: dict[str, Any] = {"ok": True, "mp4": str(mp4), "first": str(png),
                              "warnings": warns, "reason": "", "frames": n,
                              "profile": prof_dict}

    # ---- 第二遍：**给模型**的控制图（简化人形）----
    #
    # **为什么两遍**：A/B 实测（`alpha.107`）发现圆柱当 ControlNet 输入会把
    # 人体压成"柱子形状的涂抹"。所以：
    #   · 给人看 -> 圆柱（`previs.mp4` / `previs_first.png`）
    #   · 给模型 -> 简化人形（`previs_control.png`）
    # 同一份布局、同一个机位，**只换形体** —— 不引入新的不一致。
    if with_control:
        try:
            result["control"] = _render_control(
                layout, blender=blender, out_dir=out_dir, script=script_path,
                resolution=resolution, fps=fps)
        except Exception as e:                                # noqa: BLE001
            warn = (f"控制图渲染失败（{type(e).__name__}: {e}）—— "
                    f"仍可用圆柱首帧，但画质会差（见 SUMMARY.md）")
            result["warnings"] = list(warns) + [warn]
            result["control"] = ""
    return result


def _render_control(layout: Mapping[str, Any], *, blender: str, out_dir: Path,
                    script: Path, resolution: tuple[int, int],
                    fps: int) -> str:
    """用**简化人形**再渲一帧，作为 ControlNet 的控制图。"""
    cdir = out_dir / "control"
    spec, _ = layout_to_spec(layout, out_dir=str(cdir), resolution=resolution,
                             fps=fps, form="figure")
    cdir.mkdir(parents=True, exist_ok=True)
    spec_path = cdir / "shot.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    r = subprocess.run(
        [blender, "--background", "--factory-startup",
         "--python", str(script), "--", str(spec_path)],
        capture_output=True, text=True, timeout=900)
    if r.returncode != 0 or "RENDER_DONE" not in (r.stdout or ""):
        raise RuntimeError(f"Blender 失败：{r.stderr[-200:]}")
    d = cdir / "gray"
    fs = sorted(d.glob(FRAME_GLOB))
    if not fs:
        raise RuntimeError(f"{d} 里没有帧")
    out = out_dir / "previs_control.png"
    shutil.copyfile(fs[0], out)
    return str(out)
