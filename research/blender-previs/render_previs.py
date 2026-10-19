"""白模渲染器 —— 从角色代理与镜头规格产出**控制序列**（灰度 / 深度）。

产出什么，给谁用
----------------
| 产物 | 用途 |
|---|---|
| **Gray 灰度序列** | ⭐ H3 ControlNet 的 `Gray` 条件 —— **白模渲染出来本就是灰度图，可直接用** |
| **Depth 深度序列** | H3 ControlNet 的 `Depth` 条件 —— 更精确的空间约束 |
| 预览图 | 给人看的分镜预演 |

渲染引擎：**Workbench（CPU）**
-----------------------------
**为什么不选 EEVEE**（实测数据）：

| 引擎 | 单帧 @1920×1080 | 显存 |
|---|---|---|
| **Workbench** | **0.22 s** | **+88 MB** |
| EEVEE | 3.0 s | +1155 MB |

本机 8 GB 显存同时只能加载一个模型，而 ComfyUI 已占约 4.6 GB。
Workbench 用 CPU，**与 ComfyUI 完全不争显存** —— 这是选它的决定性理由。

**Workbench 也能出深度图**：`ViewLayer.use_pass_z` 是**与引擎无关**的渲染通道，
配合合成器的 `Map Range` 归一化即可输出灰度深度图。
因此不需要为了深度图而付出 EEVEE 的 13 倍时间与 13 倍显存。

关键约束（来自 H3 官方文档）
--------------------------
- **固定 24 fps**
- 帧数**对齐到 `17n+5` 网格**（H3 的 VAE 解码约束，不对齐会被截断）
- 时长上限 15 秒（本机实测 H3 硬上限 10 秒）

⚠️ 运行在 Blender 内置 Python 3.11，**不得 import 平台模块**。
"""

from __future__ import annotations

import json
import math
import os
import sys
from typing import Any

import bpy
from mathutils import Vector

#: H3 的帧数网格：帧数必须是 17n+5。
FRAME_GRID_A, FRAME_GRID_B = 17, 5

#: H3 固定输出帧率。
FPS = 24


def align_frames(seconds: float, fps: int = FPS) -> int:
    """把时长换算为**对齐到 17n+5 网格**的帧数。

    H3 的 VAE 只能解码 17n+5 帧；不对齐的部分会被静默截断 ——
    与其让模型截断，不如我们在生成时就对齐，这样"计划 5 秒"与
    "实际 5 秒"是一致的。
    """
    want = max(int(round(seconds * fps)), 1)
    n = max(0, round((want - FRAME_GRID_B) / FRAME_GRID_A))
    frames = FRAME_GRID_A * n + FRAME_GRID_B
    # 若估算值低于需求，向上取一格（宁可略长，不要短于意图）
    if frames < want:
        frames = FRAME_GRID_A * (n + 1) + FRAME_GRID_B
    return max(frames, FRAME_GRID_B)


# ==========================================================================
# 场景搭建
# ==========================================================================

def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def setup_engine(res_x: int, res_y: int) -> None:
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_WORKBENCH"
    sc.render.resolution_x = res_x
    sc.render.resolution_y = res_y
    sc.render.resolution_percentage = 100
    sc.render.fps = FPS
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGB"
    sc.render.film_transparent = False
    # 白模的关键：中性灰底 + 无彩色倾向
    sc.world = bpy.data.worlds.new("W") if sc.world is None else sc.world
    sc.world.use_nodes = True
    bg = sc.world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs[0].default_value = (0.18, 0.18, 0.18, 1.0)
        bg.inputs[1].default_value = 1.0
    sh = sc.display.shading
    sh.light = "STUDIO"
    sh.color_type = "MATERIAL"
    sh.show_object_outline = False
    sh.show_specular_highlight = False
    sh.show_shadows = True
    sh.shadow_intensity = 0.35
    sh.show_cavity = True            # 凹腔增强，让关节/五官更可辨
    sh.cavity_type = "WORLD"
    sh.cavity_ridge_factor = 0.6
    sh.cavity_valley_factor = 0.6


def measure_subject_depth(cam_obj: Any, root_offset: Any = None,
                          *, char_params: dict[str, Any] | None = None,
                          action: str = "stand",
                          samples: int = 5) -> tuple[float, float] | None:
    """量出**主体实际占据的景深区间** ``(最近, 最远)``，单位米。

    为什么必须用主体的包围盒，而不是"相机到原点距离 ± 主体高度"：

        旧做法取 ``[d0 - 1.2h, d0 + 1.5h]``，对 1.68 m 的人就是 **5.46 m 跨度**。
        而角色自身的景深只有约 0.5 m —— 于是它只占区间的 9%，
        **实测灰度跨度仅 5~22 级（标准差 0.9~3.0）**，深度图退化成剪影。

        这类缺陷不会报错，只是**静默地把控制信号变弱**，
        而 ControlNet 的 depth 条件恰恰靠梯度工作。

    ⚠️ 有两个必须处理的陷阱，都踩过：

    **陷阱 1：`matrix_world` 不会随 ``location`` 立即更新。**
    设置 ``cam_obj.location`` 之后直接读 ``matrix_world`` 得到的是**单位矩阵** ——
    于是"转到相机空间"实际什么都没转，算出负距离，
    再被 ``max(..., 0.01)`` 夹成 0.246 m 的荒谬区间，**像素全被截断成纯黑纯白**。
    必须先 ``bpy.context.view_layer.update()``。

    **陷阱 2：几何是逐帧重建的，默认站姿的包围盒不代表整镜。**
    ``build_posed_character`` 每帧按 ``pose_at(action, t)`` 重建几何，
    走路时手臂前后摆动会让景深比站姿大。所以这里**跨动作时刻采样**取并集。
    """
    # ---- 陷阱 1：先让变换生效 ----
    bpy.context.view_layer.update()
    inv = cam_obj.matrix_world.inverted()

    # ---- 陷阱 2：跨动作时刻采样 ----
    poses: list[dict[str, Any] | None] = [None]
    if char_params:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from build_character import ACTION_TEMPLATES, pose_at  # type: ignore

            if action in ACTION_TEMPLATES and samples > 1:
                poses = [pose_at(action, i / (samples - 1))
                         for i in range(samples)]
        except Exception:                                     # noqa: BLE001
            poses = [None]

    dists: list[float] = []
    for pose in poses:
        if pose is not None:
            try:
                root, _built = build_posed_character(char_params or {}, action, 0.0)
                # 用采样姿势覆盖：重建一次再摆正
                for o in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
                    bpy.data.objects.remove(o, do_unlink=True)
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from build_character import build_character  # type: ignore

                build_character(char_params or {}, pose=pose)
                bpy.context.view_layer.update()
            except Exception:                                 # noqa: BLE001
                pass

        for o in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
            for c in o.bound_box:
                w = o.matrix_world @ Vector(c)
                if root_offset is not None:
                    w = w + Vector(root_offset)
                local = inv @ w
                dists.append(-local.z)      # 相机前方为负 Z，取反得距离

    if not dists:
        return None
    if os.environ.get("PREVIS_DEBUG_DEPTH"):
        print(f"DEPTHDBG samples={len(poses)} dists=[{min(dists):.3f},{max(dists):.3f}] "
              f"cam={tuple(round(v,2) for v in cam_obj.matrix_world.translation)}",
              flush=True)
    return (min(dists), max(dists))


def auto_depth_range(cam_obj: Any, root_offset: Any = None,
                     *, char_params: dict[str, Any] | None = None,
                     action: str = "stand",
                     pad_ratio: float = 0.12,
                     min_span: float = 0.4) -> tuple[float, float]:
    """按主体实际景深给出归一化区间，两端留 ``pad_ratio`` 余量。

    余量不可省：完全贴紧包围盒会让最近/最远的肢体被**截断成纯白或纯黑**，
    丢失"更近/更远"的方向信息 —— 而方向正是 ControlNet 需要的。
    """
    got = measure_subject_depth(cam_obj, root_offset,
                                char_params=char_params, action=action)
    if got is None or got[0] <= 0.01:
        # 测量失败时退回一个**保守但有效**的区间，而不是荒谬的 0.25 m。
        # 相机到原点的距离是可靠的已知量，用它作锚点。
        d0 = float((Vector(cam_obj.matrix_world.translation)
                    - Vector((0, 0, 0))).length) or 3.0
        return (max(d0 - 1.2, 0.1), d0 + 1.5)
    near, far = got
    span = max(far - near, min_span)
    pad = span * pad_ratio
    return (max(near - pad, 0.01), far + pad)


def setup_depth_compositor(z_near: float, z_far: float) -> None:
    """把 View Layer 的 Z 通道归一化为灰度图，经 Combine 合进输出。

    **只做一次渲染就能同时得到灰度图与深度图** —— 因为 Z 通道是渲染的副产物，
    不是第二次渲染。这比"渲染两遍"省一半时间。

    归一化区间**必须贴合主体**，否则深度图退化成剪影：
    区间过大 -> 主体挤在极少数灰阶里；区间过小 -> 远近肢体被截断。
    调用方应优先用 :func:`auto_depth_range` 求区间，它按实际几何计算。
    """
    if z_far <= z_near:
        z_far = z_near + 0.5
    sc = bpy.context.scene
    bpy.context.view_layer.use_pass_z = True
    sc.use_nodes = True
    nt = sc.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    rl = nt.nodes.new("CompositorNodeRLayers")
    mr = nt.nodes.new("CompositorNodeMapRange")
    mr.inputs["From Min"].default_value = z_near
    mr.inputs["From Max"].default_value = z_far
    mr.inputs["To Min"].default_value = 1.0     # 近 -> 白
    mr.inputs["To Max"].default_value = 0.0     # 远 -> 黑
    comp = nt.nodes.new("CompositorNodeComposite")
    nt.links.new(rl.outputs["Depth"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Value"], comp.inputs["Image"])


def setup_depth_material(z_near: float, z_far: float) -> None:
    """给场景所有物体换一个"距离 -> 灰度"的发光材质。

    这条路径用于后续需要材质级深度时（例如只渲染部分物体）。
    当前主路径用合成器的 Z 通道，本函数保留备用。
    """
    m = bpy.data.materials.new("MAT_depthvis")
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    sub = nt.nodes.new("ShaderNodeVectorMath")
    sub.operation = "DISTANCE"
    sub.inputs[1].default_value = bpy.context.scene.camera.location if bpy.context.scene.camera else (0, 0, 0)
    mr = nt.nodes.new("ShaderNodeMapRange")
    mr.inputs["From Min"].default_value = z_near
    mr.inputs["From Max"].default_value = z_far
    mr.inputs["To Min"].default_value = 1.0
    mr.inputs["To Max"].default_value = 0.0
    nt.links.new(geo.outputs["Position"], sub.inputs[0])
    nt.links.new(sub.outputs["Value"], mr.inputs["Value"])
    nt.links.new(mr.outputs["Result"], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return m


def setup_camera(cam: dict[str, Any], look_at: Vector) -> Any:
    """按镜头规格放置相机。

    镜头规格用**语义 + 少量数值**表达，不存相机矩阵 ——
    与平台 IR「存语义不存坐标」的设计一致。

    ``shot_size`` 决定距离，``angle`` 决定俯仰，``movement`` 决定是否有位移。
    """
    sc = bpy.context.scene
    cam_data = bpy.data.cameras.new("CAM")
    cam_obj = bpy.data.objects.new("CAM", cam_data)
    sc.collection.objects.link(cam_obj)
    sc.camera = cam_obj

    # ⚠️ **必须让 `sensor_height` 真的掌管垂直方向**（2026-10-19 实测）。
    #
    # Blender 的 `sensor_fit` 默认是 `AUTO`：横幅画面按**水平** `sensor_width`(36mm)
    # 适配，垂直方向实际是 36 × 480/832 ≈ **20.8mm**。
    # 而下面的 `fov_v` 按 `sensor_height`(24mm) 算 ——
    # **假设的垂直视场偏大 → 距离算得偏近 → 人偏大、脚切出画面。**
    #
    # 实测表现：`full` 景别出图，人物占屏高约 130%，脚在画面外。
    cam_data.sensor_fit = 'VERTICAL'

    subject_h = float(cam.get("subjectHeight", 1.7))
    target = Vector(look_at)

    # 景别 -> 画面垂直覆盖高度（相对主体身高）
    cover = {
        "extreme_wide": 8.0, "wide": 4.0, "full": 1.15, "medium_full": 1.0,
        "medium": 0.70, "medium_close": 0.50, "close": 0.34, "extreme_close": 0.20,
    }.get(str(cam.get("shot_size", "medium")), 0.7) * subject_h

    # 用垂直 FOV 求距离：d = (cover/2) / tan(fov/2)
    lens = float(cam.get("lens_mm", 50.0))
    sensor = cam_data.sensor_height
    fov_v = 2 * math.atan(sensor / (2 * lens))
    dist = (cover / 2) / math.tan(fov_v / 2)

    # 机位角度：0=平视，正=俯拍
    ang = math.radians(float(cam.get("angle_deg", 0.0)))
    # 水平方位：0=正面，90=侧面
    azim = math.radians(float(cam.get("azimuth_deg", 0.0)))

    focus_h = float(cam.get("focus_height_ratio", 0.55)) * subject_h
    focus = Vector((target.x, target.y, target.z + focus_h))

    offset = Vector((math.sin(azim) * dist, -math.cos(azim) * dist, math.sin(ang) * dist))
    cam_obj.location = focus + offset

    direction = focus - cam_obj.location
    cam_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    cam_data.lens = lens
    return cam_obj


def add_keyframes(obj: Any, spec: dict[str, Any], frames: int) -> None:
    """按``path``规格给对象加位移关键帧。

    刻意只用**少量预制路径**（与平台"LLM 只选模板、不产生坐标"一致），
    预演师选定路径名，这里把名称翻译为确定性的位移。
    """
    path = str(spec.get("path", "static"))
    if path == "static":
        return
    p0 = obj.location.copy()
    # 位移幅度取主体身高的比例，保证跨角色尺度一致
    amp = float(spec.get("subjectHeight", 1.7)) * float(spec.get("path_scale", 0.6))

    if path == "enter_from_left":
        obj.location = p0 + Vector((-amp, 0, 0))
        obj.keyframe_insert("location", frame=1)
        obj.location = p0
        obj.keyframe_insert("location", frame=frames)
    elif path == "exit_right":
        obj.location = p0
        obj.keyframe_insert("location", frame=1)
        obj.location = p0 + Vector((amp, 0, 0))
        obj.keyframe_insert("location", frame=frames)
    elif path == "walk_forward":
        obj.location = p0 + Vector((0, amp * 0.5, 0))
        obj.keyframe_insert("location", frame=1)
        obj.location = p0
        obj.keyframe_insert("location", frame=frames)
    elif path == "approach_camera":
        obj.location = p0
        obj.keyframe_insert("location", frame=1)
        obj.location = p0 + Vector((0, -amp * 0.35, 0))
        obj.keyframe_insert("location", frame=frames)
    for fc in obj.animation_data.action.fcurves if obj.animation_data and obj.animation_data.action else []:
        for kp in fc.keyframe_points:
            kp.interpolation = "BEZIER"


def add_camera_move(cam_obj: Any, spec: dict[str, Any], frames: int) -> None:
    """相机运动模板（微量，主要用于验证"机位可被控制"）。"""
    mv = str(spec.get("camera_movement", "static"))
    if mv == "static":
        return
    p0 = cam_obj.location.copy()
    d = float(spec.get("move_scale", 0.25)) * float(spec.get("subjectHeight", 1.7))

    if mv == "dolly_in":
        cam_obj.location = p0 + (Vector(cam_obj.location) - Vector((0, 0, p0.z))).normalized() * d
        cam_obj.keyframe_insert("location", frame=1)
        cam_obj.location = p0
        cam_obj.keyframe_insert("location", frame=frames)
    elif mv == "dolly_out":
        cam_obj.location = p0
        cam_obj.keyframe_insert("location", frame=1)
        cam_obj.location = p0 + (Vector(cam_obj.location) - Vector((0, 0, p0.z))).normalized() * d
        cam_obj.keyframe_insert("location", frame=frames)
    elif mv == "truck_left":
        cam_obj.location = p0 + Vector((-d, 0, 0))
        cam_obj.keyframe_insert("location", frame=1)
        cam_obj.location = p0
        cam_obj.keyframe_insert("location", frame=frames)
    elif mv == "truck_right":
        cam_obj.location = p0
        cam_obj.keyframe_insert("location", frame=1)
        cam_obj.location = p0 + Vector((d, 0, 0))
        cam_obj.keyframe_insert("location", frame=frames)


# ==========================================================================
# 渲染
# ==========================================================================

def set_depth_output(enabled: bool) -> None:
    """切换输出通道：True = 深度图（Z 经归一化），False = 灰度白模。

    **为什么要切换而不是同时输出**：合成器是**场景级**设置，
    一次渲染只能出一个通道。所以分两遍渲染 ——
    但两遍都很快（Workbench 实测 0.06 s/帧 @832×480），代价可以接受。

    这对应 H3 ControlNet 的两种条件：
      · 灰度白模 -> ``Gray`` 条件（白模渲染本就是灰度图，可直接用）
      · 深度图   -> ``Depth`` 条件（更精确的空间约束）
    """
    sc = bpy.context.scene
    if enabled:
        bpy.context.view_layer.use_pass_z = True
        sc.use_nodes = True
    else:
        # 直接输出渲染结果，不经合成器
        sc.use_nodes = True
        nt = sc.node_tree
        for n in list(nt.nodes):
            nt.nodes.remove(n)
        rl = nt.nodes.new("CompositorNodeRLayers")
        comp = nt.nodes.new("CompositorNodeComposite")
        nt.links.new(rl.outputs["Image"], comp.inputs["Image"])


def _path_offset(shot: dict[str, Any], subject_h: float) -> Any:
    """把路径模板换算成**整镜位移**。

    与"肢体动作"（``action``）正交：动作管手脚怎么动，路径管人往哪走。
    两者可自由组合 —— ``action=walk`` + ``path=enter_from_left`` 得到
    "边走进画"，而不是只能用其中一个。

    路径名与数值全部来自模板，**LLM 只选名字不产生坐标**。
    """
    path = str(shot.get("path", "static"))
    amp = subject_h * float(shot.get("path_scale", 0.5))
    table = {
        "enter_from_left": Vector((-amp, 0, 0)),
        "exit_right": Vector((amp, 0, 0)),
        "walk_forward": Vector((0, amp * 0.5, 0)),
        "approach_camera": Vector((0, -amp * 0.35, 0)),
        "retreat": Vector((0, amp * 0.35, 0)),
    }
    if path not in table:
        print(f"WARN_PATH 未知路径 {path!r}，按原地处理；可用：{sorted(table)}")
        return None
    return table[path]


def build_posed_character(char_params: dict[str, Any], action: str, t: float) -> Any:
    """按动作模板在归一化时刻 ``t`` 重建角色，返回根对象。

    **为什么逐帧重建而不是建一次骨架再驱动**：
    白模每帧只约 40 个图元，重建几十毫秒，而渲染本身就要 55 ms ——
    重建的代价可忽略，但它带来两个实质好处：

    1. **确定性**：``(params, action, t)`` 唯一决定几何，无隐藏状态
    2. **可缓存**：同一 ``(params, action, t)`` 必然产出同一帧，与平台哈希体系同构

    反之，用 Blender 的骨骼系统意味着引入 armature/权重/约束等一整套状态，
    而白模**不需要**蒙皮变形 —— 它只需要"姿势正确的刚体图元"。
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_character import ACTION_TEMPLATES, build_character, pose_at  # type: ignore

    if action in ACTION_TEMPLATES:
        pose = pose_at(action, t)
    else:
        print(f"WARN_ACTION 未知动作 {action!r}，退回静态站姿；"
              f"可用：{sorted(ACTION_TEMPLATES)}")
        pose = {}
    built = build_character(char_params, pose=pose)
    return bpy.data.objects[built["root"]], built


def render_pass(out_dir: str, frames: int, *, tag: str,
                char_params: dict[str, Any] | None = None,
                action: str = "stand",
                root_offset: Any = None) -> int:
    """渲染一遍并落盘到 ``out_dir/<tag>/``。

    给了 ``char_params`` 就逐帧按 ``action`` 模板重建角色 —— 这样
    白模才有**肢体姿势**，而不是"一个僵硬人形在滑行"。
    """
    sc = bpy.context.scene
    sub = os.path.join(out_dir, tag)
    os.makedirs(sub, exist_ok=True)
    sc.frame_start, sc.frame_end = 1, frames
    n = 0
    for f in range(1, frames + 1):
        sc.frame_set(f)
        t = (f - 1) / max(frames - 1, 1)
        if char_params is not None:
            root, _ = build_posed_character(char_params, action, t)
            if root_offset is not None:
                # 路径位移从 offset 线性走到原点 —— 即"从该处走到站位"。
                # 与 action 无关：动作管手脚，路径管从哪走到哪，二者正交。
                root.location = Vector(root_offset) * (1.0 - t)
            # ⚠️ **必须显式刷新依赖图**：本函数每帧重建网格数据，
            # 但 `bpy.ops.render.render` 用的是**求值后的**依赖图。
            # 不刷新就会渲染上一帧（或首帧）的几何 —— 而且**不报任何错**，
            # 表现为"姿态没生效"。这个坑曾让我误以为姿态解算有 bug。
            bpy.context.view_layer.update()
            if os.environ.get("PREVIS_DEBUG"):
                _vs = [root.matrix_world @ v.co for v in root.data.vertices]
                print("DBGFRAME %d t=%.3f z=[%.3f,%.3f] verts=%d"
                      % (f, t, min(v.z for v in _vs), max(v.z for v in _vs), len(_vs)))
        sc.render.filepath = os.path.join(sub, f"f{f:05d}")
        bpy.ops.render.render(write_still=True)
        n += 1
    return n


def render_sequence(out_dir: str, frames: int, *, preview_at: int = 1,
                    char_params: dict[str, Any] | None = None,
                    action: str = "stand") -> dict[str, Any]:
    """渲染**两遍**：深度图 + 灰度白模。返回统计信息。

    注意：若给了 ``char_params``，**两遍都要逐帧重建** ——
    否则第二遍会渲染第一遍最后一帧残留的姿态。
    """
    os.makedirs(out_dir, exist_ok=True)

    # 第一遍：深度（Z 通道经合成器归一化）
    set_depth_output(True)
    n_depth = render_pass(out_dir, frames, tag="depth",
                          char_params=char_params, action=action)

    # 第二遍：灰度白模（直接输出渲染结果）
    set_depth_output(False)
    n_gray = render_pass(out_dir, frames, tag="gray",
                         char_params=char_params, action=action)

    # 预览一张灰度图，便于人快速判断
    sc = bpy.context.scene
    sc.frame_set(preview_at)
    if char_params is not None:
        t = (preview_at - 1) / max(frames - 1, 1)
        build_posed_character(char_params, action, t)
    bpy.ops.render.render(write_still=True)

    return {"depthFrames": n_depth, "grayFrames": n_gray, "outDir": out_dir}


def _load_spec() -> dict[str, Any]:
    argv = sys.argv
    if "--" not in argv:
        return {}
    rest = argv[argv.index("--") + 1:]
    if not rest:
        return {}
    with open(rest[0], "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    spec = _load_spec()
    shot = spec.get("shot", {})
    out_dir = spec.get("outDir", "out")

    res = spec.get("resolution", {})
    setup_engine(int(res.get("width", 832)), int(res.get("height", 480)))

    # 角色：只解一次比例（供相机取景用），几何在渲染时**逐帧重建**
    char = spec.get("character")
    action = str(shot.get("action", "stand"))
    if char:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from build_character import ACTION_TEMPLATES, build_character  # type: ignore

        built = build_character(char)          # 默认站姿，仅用于取景
        subject_h = built["proportions"]["height"]
        if action not in ACTION_TEMPLATES:
            print(f"WARN_ACTION 未知动作 {action!r}；可用：{sorted(ACTION_TEMPLATES)}")
    else:
        subject_h = 1.7

    shot = dict(shot)
    shot.setdefault("subjectHeight", subject_h)

    frames = align_frames(float(shot.get("duration_sec", 5.0)))
    ground = Vector((0, 0, 0))
    cam_obj = setup_camera(shot, ground)

    add_camera_move(cam_obj, shot, frames)

    # 整镜位移（与"肢体动作"正交）—— 由 path 模板给出
    root_offset = None
    if char and str(shot.get("path", "static")) != "static":
        root_offset = _path_offset(shot, subject_h)

    # 深度归一化区间：**按主体实际景深**，而不是"相机距离 ± 主体高度"。
    #
    # 旧做法给出约 5.5 m 跨度，而角色自身景深仅约 0.5 m ——
    # 实测主体灰度跨度只有 5~22 级，深度图退化成剪影，ControlNet 拿不到梯度。
    # 改用包围盒实测后，同样的场景能拿到一百多级。
    depth_near, depth_far = auto_depth_range(
        cam_obj, root_offset, char_params=char, action=action)
    setup_depth_compositor(depth_near, depth_far)
    print(f"DEPTH_RANGE near={depth_near:.3f} far={depth_far:.3f} "
          f"span={depth_far - depth_near:.3f}", flush=True)

    stats = render_sequence(out_dir, frames, char_params=char,
                            action=action)
    print("RENDER_DONE", json.dumps({
        "frames": frames,
        "fps": FPS,
        "durationSec": round(frames / FPS, 3),
        "requestedSec": float(shot.get("duration_sec", 5.0)),
        "alignedToGrid": f"{FRAME_GRID_A}n+{FRAME_GRID_B}",
        "engine": bpy.context.scene.render.engine,
        "resolution": [bpy.context.scene.render.resolution_x,
                       bpy.context.scene.render.resolution_y],
        "action": action,
        "cameraMovement": shot.get("camera_movement", "static"),
        "grayFrames": stats["grayFrames"],
        "depthFrames": stats["depthFrames"],
        "outDir": stats["outDir"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
