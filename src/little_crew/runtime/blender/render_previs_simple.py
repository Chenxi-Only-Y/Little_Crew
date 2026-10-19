"""白模最小渲染器：**N 个几何体，各自有坐标**。

## 与 `render_previs.py` 的区别

| | `render_previs.py`（旧） | 本脚本（新） |
|---|---|---|
| 角色 | 一个**可绑骨**的人，固定在原点 | **N 个胶囊 + 球头**，各自 `x`/`z` |
| 姿态 | `pose_at` / 动作模板 / 关节求解 | ❌ **全部删掉**（新范围不要姿态） |
| 深度图 | `measure_subject_depth` / 合成器 | ❌ **删掉**（不要纹理/灰度） |
| 机位 | `setup_camera` | ✅ **逐字复用**（`alpha.90` 修对的那套） |

**为什么另写而不是改**：旧脚本那套骨架/姿态在新范围下**全是负担**，
而新范围要的东西（多个人各有坐标）它**结构上表达不了**。

## 世界坐标怎么摆

既有公式把**相机放在原点之外、看向原点**：

```
cam = (sin(a)·d, -cos(a)·d, sin(θ)·d)      focus = (0, 0, 0.55·h)
```

所以**世界原点就是"主体距离 d 处"那个点**。角色按"离相机 z、偏右 x"摆：

```
forward = (-sin a, cos a)        # 相机水平朝向（指向原点）
right   = ( cos a, sin a)
P = cam + z·forward + x·right
```

`z = d, x = 0` 时 `P = (0,0)` —— 正好回到原点，与旧公式一致。

## 输入

```json
{"outDir": "...", "resolution": {"width": 640, "height": 384},
 "fps": 24,
 "shot": {"shot_size": "medium", "lens_mm": 50, "azimuth_deg": 0,
          "angle_deg": 0, "duration_sec": 2.5, "camera_movement": "static"},
 "subjects": [{"who": "少女", "x": 0, "z": 2.45, "height_cm": 168}]}
```
"""
from __future__ import annotations

import json
import math
import os
import sys

import bpy  # type: ignore  # noqa: F401  (只在 Blender 里可用)
from mathutils import Vector  # type: ignore

#: 景别 → 画面垂直覆盖高度（相对主体身高）。**与校验器 / 旧脚本一致。**
SHOT_COVER = {
    "extreme_wide": 8.0, "wide": 4.0, "full": 1.15, "medium_full": 1.0,
    "medium": 0.70, "medium_close": 0.50, "close": 0.34, "extreme_close": 0.20,
}
SENSOR_HEIGHT_MM = 24.0
FOCUS_RATIO = 0.55

#: 人物几何：胶囊半径按身高的比例（简单、不追求解剖正确）
BODY_RADIUS_RATIO = 0.075
HEAD_RADIUS_RATIO = 0.055


def _load_spec() -> dict:
    argv = sys.argv
    if "--" in argv:
        rest = argv[argv.index("--") + 1:]
        if rest:
            with open(rest[0], encoding="utf-8") as fh:
                return json.load(fh)
    raise SystemExit("用法: blender -b --python render_previs_simple.py -- spec.json")


def _clear() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)


def _setup(res_x: int, res_y: int, fps: int) -> None:
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_WORKBENCH"
    sc.display.shading.light = "STUDIO"
    sc.display.shading.color_type = "SINGLE"
    sc.display.shading.single_color = (0.75, 0.75, 0.75)
    sc.render.resolution_x = res_x
    sc.render.resolution_y = res_y
    sc.render.resolution_percentage = 100
    sc.render.fps = fps
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = False
    # 背景压暗，人物轮廓才清楚
    try:
        sc.world = bpy.data.worlds.new("W")
        sc.world.color = (0.18, 0.18, 0.18)
    except Exception:                                       # noqa: BLE001
        pass


def _capsule(name: str, loc, radius: float, half_len: float,
             vertical: bool = True):
    """胶囊：圆柱 + 两端半球。用三个物体拼，够用且不用建模。"""
    parts = []
    bpy.ops.mesh.primitive_cylinder_add(radius=radius, depth=half_len * 2,
                                        location=loc)
    cyl = bpy.context.object
    cyl.name = name
    parts.append(cyl)
    for sign in (1, -1):
        off = loc.copy()
        if vertical:
            off.z += sign * half_len
        else:
            off.y += sign * half_len
        bpy.ops.mesh.primitive_uv_sphere_add(radius=radius, location=off)
        s = bpy.context.object
        s.name = f"{name}_cap{sign}"
        parts.append(s)
    return parts


def _subject(name: str, pos: Vector, height_m: float,
             params: dict | None = None, *, form: str = "marker") -> list:
    """放一个人。**两种形态，用途不同。**

    ## 为什么要两种

    A/B 实测（`alpha.107`）发现一条真实的张力：

    | 形态 | 给人看（位置参考） | 给模型（ControlNet 输入） |
    |---|---|---|
    | **等高圆柱** | ✅ 不假装是人，读位置最清楚 | ❌ **模型会照抄圆柱轮廓** → 人体塌成"柱子形状的涂抹" |
    | **简化人形** | ⚠️ 会被问"为啥这么糙" | ✅ 有无肩/头/腿的轮廓，模型能正常出人 |

    实测证据：`research/previs-ab/` 的 A/B 两图（像素差 9.81%，但 B 组画质崩塌）。

    **所以分两路**：同一份布局、同一个机位，只换形体。
    """

    if form == "figure":
        # ---- 简化人形：躯干锥台 + 球头 ----
        # 比例按真人：肩宽 ≈ 0.24h、髋宽 ≈ 0.17h、头 ≈ 0.13h（7.6 头身）
        tb, tt = height_m * 0.05, height_m * 0.76
        d = tt - tb
        bpy.ops.mesh.primitive_cone_add(
            vertices=24, radius1=height_m * 0.085, radius2=height_m * 0.115,
            depth=d, location=(pos.x, pos.y, tb + d / 2))
        torso = bpy.context.object
        torso.name = f"{name}_torso"
        parts = [torso]
        hr = height_m * 0.072
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=hr,
            location=(pos.x, pos.y, tt + height_m * 0.045 + hr * 0.35))
        head = bpy.context.object
        head.name = f"{name}_head"
        parts.append(head)
        # 腿：两根细圆柱，给模型"下半身在哪"的线索（不画关节）
        for sign, tag in ((-1, "L"), (1, "R")):
            lc = Vector((pos.x + sign * height_m * 0.045, pos.y, tb * 0.5))
            bpy.ops.mesh.primitive_cylinder_add(
                vertices=16, radius=height_m * 0.040, depth=tb,
                location=lc)
            leg = bpy.context.object
            leg.name = f"{name}_leg{tag}"
            parts.append(leg)
        return parts

    # ---- 等高圆柱：位置标记（默认）----
    #
    # 直径取 0.17 × 身高（按**髋宽**，不按肩宽）：
    # 圆柱是等宽的，取肩宽会显得"上下一样胖"，夸大遮挡面积。
    r = height_m * 0.085
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=32, radius=r, depth=height_m,
        location=(pos.x, pos.y, height_m / 2.0))
    ob = bpy.context.object
    ob.name = name
    return [ob]


def _ground() -> None:
    """**地面平面** —— 零数据、零判断，但它提供"视平线在哪"这个关键参考。

    没有地面就看不出机位是平视还是俯拍，也看不出人站在哪一侧。
    """
    bpy.ops.mesh.primitive_plane_add(size=40.0, location=(0.0, 0.0, 0.0))
    g = bpy.context.object
    g.name = "ground"


def _prop(label: str, pos: Vector, w: float, d: float, h: float) -> list:
    """场景道具：一个盒体。`w`/`d`/`h` 是宽/进深/高（米）。

    **只给方盒子** —— 不做材质、不做造型。门的"门"字不在模型上，
    但 2 米高的薄板在画面里的位置与比例，已经足够判断构图。
    """
    bpy.ops.mesh.primitive_cube_add(size=1.0,
                                    location=(pos.x, pos.y, h / 2.0))
    box = bpy.context.object
    box.name = label
    box.scale = (max(w, 0.01), max(d, 0.01), max(h, 0.01))
    return [box]


def _camera(shot: dict, subject_h: float):
    sc = bpy.context.scene
    cam_data = bpy.data.cameras.new("CAM")
    cam_obj = bpy.data.objects.new("CAM", cam_data)
    sc.collection.objects.link(cam_obj)
    sc.camera = cam_obj
    # ⚠️ 与旧脚本 / 校验器一致：让 sensor_height 真正掌管垂直视场
    cam_data.sensor_fit = "VERTICAL"

    size = str(shot.get("shot_size", "medium"))
    cover = SHOT_COVER.get(size, 0.70) * subject_h
    lens = float(shot.get("lens_mm", 50.0))
    cam_data.lens = lens
    fov_v = 2 * math.atan(SENSOR_HEIGHT_MM / (2 * lens))
    dist = (cover / 2) / math.tan(fov_v / 2)

    ang = math.radians(float(shot.get("angle_deg", 0.0)))
    azim = math.radians(float(shot.get("azimuth_deg", 0.0)))
    focus = Vector((0.0, 0.0, FOCUS_RATIO * subject_h))
    cam_obj.location = focus + Vector((
        math.sin(azim) * dist, -math.cos(azim) * dist, math.sin(ang) * dist))
    direction = focus - cam_obj.location
    cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    return cam_obj, dist, focus, azim


def _move(cam_obj, shot: dict, dist: float, azim: float, frames: int) -> None:
    """机位运动关键帧。**只做机位，不做角色走位。**"""
    move = str(shot.get("camera_movement", "static"))
    if move == "static":
        return
    sc = bpy.context.scene
    sc.frame_start = 1
    sc.frame_end = frames
    start = cam_obj.location.copy()
    fwd = Vector((-math.sin(azim), math.cos(azim), 0.0))   # 相机水平朝向
    right = Vector((math.cos(azim), math.sin(azim), 0.0))
    step = {
        "dolly_in": fwd * (dist * 0.25),
        "push_in": fwd * (dist * 0.25),
        "dolly_out": fwd * (-dist * 0.25),
        "truck_left": right * (-dist * 0.20),
        "truck_right": right * (dist * 0.20),
    }.get(move)
    if step is None:
        print(f"WARN_MOVE 未知运镜 {move!r}，按 static 处理")
        return
    cam_obj.location = start
    cam_obj.keyframe_insert("location", frame=1)
    cam_obj.location = start + step
    cam_obj.keyframe_insert("location", frame=frames)


def main() -> None:
    spec = _load_spec()
    shot = spec.get("shot", {})
    subs = spec.get("subjects") or []
    out_dir = str(spec.get("outDir", "out"))
    fps = int(spec.get("fps", 24))
    res = spec.get("resolution") or {}
    rx, ry = int(res.get("width", 640)), int(res.get("height", 384))

    _clear()
    _setup(rx, ry, fps)

    if not subs:
        raise SystemExit("spec 里没有 subjects —— 无法渲染")
    subject_h = max(float(s.get("height_cm") or 168) for s in subs) / 100.0

    cam_obj, dist, focus, azim = _camera(shot, subject_h)

    # ---- 按"离相机 z、偏右 x"摆人 ----
    fwd = Vector((-math.sin(azim), math.cos(azim), 0.0))
    right = Vector((math.cos(azim), math.sin(azim), 0.0))
    placed = []
    for s in subs:
        z = float(s.get("z") or dist)
        x = float(s.get("x") or 0.0)
        h = float(s.get("height_cm") or 168) / 100.0
        pos = Vector((cam_obj.location.x, cam_obj.location.y, 0.0)) \
            + fwd * z + right * x
        _subject(str(s.get("who") or "A"), pos, h,
                 form=str(spec.get("form") or "marker"))
        placed.append({"who": s.get("who"), "z": z, "x": x})

    # ---- 场景：地面 + 道具盒体 ----
    _ground()
    set_out = []
    for it in (spec.get("set") or []):
        try:
            sx = float(it.get("x") or 0.0)
            sz = float(it.get("z") or dist)
            sw = float(it.get("w") or 0.5)
            sd = float(it.get("d") or 0.3)
            sh = float(it.get("h") or 1.0)
        except (TypeError, ValueError):
            continue
        pp = Vector((cam_obj.location.x, cam_obj.location.y, 0.0)) \
            + fwd * sz + right * sx
        _prop(str(it.get("label") or "prop"), pp, sw, sd, sh)
        set_out.append({"label": it.get("label"), "x": sx, "z": sz,
                        "w": sw, "d": sd, "h": sh})

    frames = max(1, int(round(float(shot.get("duration_sec", 2.5)) * fps)))
    _move(cam_obj, shot, dist, azim, frames)

    gray_dir = os.path.join(out_dir, "gray")
    os.makedirs(gray_dir, exist_ok=True)
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = frames
    for f in range(1, frames + 1):
        bpy.context.scene.frame_set(f)
        bpy.context.scene.render.filepath = os.path.join(
            gray_dir, f"f{f:05d}.png")
        bpy.ops.render.render(write_still=True)

    print("RENDER_DONE " + json.dumps({
        "frames": frames, "fps": fps,
        "durationSec": round(frames / fps, 3),
        "engine": "BLENDER_WORKBENCH",
        "resolution": [rx, ry],
        "cameraMovement": str(shot.get("camera_movement", "static")),
        "shotSize": str(shot.get("shot_size", "medium")),
        "form": str(spec.get("form") or "marker"),
        "subjects": placed,
        "set": set_out,
        "distance": round(dist, 3),
        "grayFrames": frames, "depthFrames": 0,
        "outDir": out_dir,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
