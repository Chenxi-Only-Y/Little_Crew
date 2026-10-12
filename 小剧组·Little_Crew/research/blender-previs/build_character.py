"""参数化角色代理生成器 —— 为 3D 白模预演提供确定性几何。

设计原则（与平台架构一致）
--------------------------
**同一套参数 ⇒ 必然同一几何**，因此白模可哈希、可缓存、可复现。
这与平台 `ir` 的六项规范化、三级缓存键是**同一套机制**，不是新增机制。

为什么不用文生 3D 模型
----------------------
文生 3D（Hunyuan3D 等）产出的是**带纹理的精细资产**，且**一致性是弱项**——
同一角色生成两次会得到两个不同的人。本模块只需要几何、不需要纹理，
因此"低模胶囊体"不是妥协，而是**恰好够用**。

⚠️ 本脚本运行在 **Blender 内置 Python 3.11** 中，与平台 Python 3.12 隔离。
因此**不得 import 平台任何模块** —— 参数以字典传入。

用法
----
    blender --background --factory-startup --python build_character.py -- <params.json>

或用 :func:`build_character` 在 Blender 内直接调用。
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any

import bpy

# ==========================================================================
# 人体比例常量（基于成人平均值）
# ==========================================================================

#: 头高占身高的比例。头身比 7.5 表示"身高 = 7.5 个头高"。
#: 真实成人约 7.0~8.0；动漫风格常见 6.0~7.0（头更大）。
_HEAD_RATIO_TYPICAL = 7.5

#: 体型 -> 相对肩宽系数（**乘以身高**得到肩宽，单位米）。
#:
#: ⚠️ 这里是**系数**，不是米。成人肩宽约 0.23~0.25 × 身高。
#: 早期版本把参数文件里写的"米"与这里的"系数"混用，导致
#: 肩宽算出 0.647 m（身高系数 0.385）—— 是一个宽得离谱的角色。
#: 现统一为：``shoulder_width`` 参数一律按**米**解释，见 `_shoulder_m()`。
_BUILD_SHOULDER = {
    "slim": 0.238,
    "normal": 0.248,
    "athletic": 0.258,
    "heavy": 0.275,
    "child": 0.215,
}

#: 体型 -> 躯干**半轴**相对**身高**的比例。
#:
#: ⚠️ 改用"身高比"而非"肩宽比"：早先用肩宽推导躯干与肢体半径，
#: 中间环节一多误差就累积 —— 实测下来大腿半径只有成人的 58%、
#: 小腿只有 57%，渲染出来腿部像竹竿。直接给身高比更可控、可核对。
#: 参考：成人胸厚（前后）约 0.20 m / 1.7 m ≈ 0.118；此处为**半轴**故减半。
_BUILD_TORSO_W = {
    "slim": 0.078,
    "normal": 0.090,
    "athletic": 0.098,
    "heavy": 0.115,
    "child": 0.082,
}

#: 肢体半径 / 身高。参考成人实测。
_LIMB_RATIO = {
    "slim": {"arm": 0.0285, "thigh": 0.0455, "shin": 0.0360, "neck": 0.0335},
    "normal": {"arm": 0.0310, "thigh": 0.0490, "shin": 0.0390, "neck": 0.0355},
    "athletic": {"arm": 0.0335, "thigh": 0.0525, "shin": 0.0415, "neck": 0.0370},
    "heavy": {"arm": 0.0370, "thigh": 0.0570, "shin": 0.0450, "neck": 0.0400},
    "child": {"arm": 0.0300, "thigh": 0.0470, "shin": 0.0375, "neck": 0.0345},
}

#: 骨盆半宽 / 身高。成人骨盆宽约 0.19~0.21 × 身高（此处为**半宽**故减半）。
_BUILD_HIP_W = {
    "slim": 0.095,
    "normal": 0.102,
    "athletic": 0.107,
    "heavy": 0.118,
    "child": 0.098,
}

#: 体型 -> 躯干横截面半径系数（相对肩宽）。
_BUILD_TORSO_DEPTH = {
    "slim": 0.52,
    "normal": 0.60,
    "athletic": 0.66,
    "heavy": 0.78,
    "child": 0.56,
}

#: 体型 -> 默认头身比。
_BUILD_HEAD_RATIO = {
    "slim": 7.7,
    "normal": 7.5,
    "athletic": 7.4,
    "heavy": 7.2,
    "child": 5.5,
}


# ==========================================================================
# 比例解算
# ==========================================================================

def _shoulder_m(params: dict[str, Any], height_m: float, build: str) -> float:
    """解出肩宽（**米**）。

    **单位用显式后缀声明，不靠数值大小猜。**

    教训：早期版本用 ``v <= 1.0 ? 系数 : 米`` 来猜单位，结果
    ``shoulder_width: 0.40``（本意"0.40 米"）被当成系数，
    算出肩宽 **0.504 m**（肩宽/身高 = 0.300，成人应为 0.23~0.25）——
    一个宽得离谱的角色，而**从渲染图上看不出来**。

    系数区间（0.18~0.30）与米区间（0.30~0.50）**中间有重叠**，
    任何基于阈值的猜测都会在 0.30~0.50 段出错。故改为显式命名：

    | 参数 | 单位 | 例 |
    |---|---|---|
    | ``shoulder_width_m`` | **米** | ``0.40`` |
    | ``shoulder_width_ratio`` | 身高系数 | ``0.235`` |
    | ``shoulder_width`` | **米**（向后兼容，按米解释） | ``0.40`` |
    | 未给 | 按体型系数 × 身高 | — |

    超出合理区间（0.18~0.30 × 身高）会**告警并夹紧**。
    """
    if "shoulder_width_m" in params:
        sw = float(params["shoulder_width_m"])
    elif "shoulder_width_ratio" in params:
        sw = height_m * float(params["shoulder_width_ratio"])
    elif "shoulder_width" in params:
        sw = float(params["shoulder_width"])          # 按米
    else:
        sw = height_m * _BUILD_SHOULDER.get(build, 0.235)

    if sw <= 0:
        raise ValueError(f"肩宽必须为正，得到 {sw}")

    lo, hi = height_m * 0.18, height_m * 0.30
    if not (lo <= sw <= hi):
        clamped = min(max(sw, lo), hi)
        print(f"WARN_SHOULDER 肩宽 {sw:.3f} m 超出合理区间 [{lo:.3f}, {hi:.3f}]"
              f"（身高 {height_m:.2f} m），已夹紧到 {clamped:.3f} m。"
              f"提示：若要按身高系数给值，请用 shoulder_width_ratio")
        sw = clamped
    return sw


def solve_proportions(params: dict[str, Any]) -> dict[str, float]:
    """把角色参数解算为一组**绝对尺寸**（单位：米，与 Blender 一致）。

    解算规则：**身高是主尺寸**，其余部位由既定人体比例派生。
    头高与肩宽可显式覆盖，未给则按体型默认值。

    返回的键即几何生成所需的全部尺寸 —— 这一步是**纯函数**，
    不依赖 Blender 状态，便于单独测试。
    """
    height_m = float(params.get("height_cm", 170.0)) / 100.0
    build = str(params.get("build", "normal"))

    head_ratio = float(params.get("head_ratio") or _BUILD_HEAD_RATIO.get(build, _HEAD_RATIO_TYPICAL))
    if head_ratio <= 0:
        raise ValueError(f"head_ratio 必须为正，得到 {head_ratio}")

    head_h = height_m / head_ratio
    # 头是略扁的椭球：宽约为高的 0.72，深约为高的 0.80
    head_w = head_h * 0.72
    head_d = head_h * 0.80

    shoulder_w = _shoulder_m(params, height_m, build)
    #: 躯干半轴（**半**宽），由身高比直接给出
    torso_w = height_m * _BUILD_TORSO_W.get(build, 0.090)
    torso_depth_k = _BUILD_TORSO_DEPTH.get(build, 0.60)

    limb = _LIMB_RATIO.get(build, _LIMB_RATIO["normal"])

    # 颈部：约 0.4 个头高；半径由身高比给出
    neck_h = head_h * 0.40
    neck_r = height_m * limb["neck"]

    # 躯干：肩到胯。成人约 2.2~2.4 个头高
    torso_h = head_h * 2.30
    #: 骨盆**半**宽
    hip_w = height_m * _BUILD_HIP_W.get(build, 0.102)

    # 腿：从胯到地面
    leg_h = height_m - head_h - neck_h - torso_h
    if leg_h <= 0:
        raise ValueError(
            f"参数矛盾：身高 {height_m:.3f}m 减去头({head_h:.3f})+颈({neck_h:.3f})"
            f"+躯干({torso_h:.3f}) 后腿部为负（{leg_h:.3f}）。"
            f"请调小 head_ratio 或增大身高。"
        )
    thigh_h = leg_h * 0.52
    shin_h = leg_h * 0.48

    # 手臂：约 0.44 倍身高（指尖到肩）
    arm_h = height_m * 0.44
    upper_arm_h = arm_h * 0.47
    fore_arm_h = arm_h * 0.42
    hand_h = arm_h * 0.11

    # 肢体半径：**直接由身高比给出**，不由其他尺寸推导（避免误差累积）
    arm_r = height_m * limb["arm"]
    thigh_r = height_m * limb["thigh"]
    shin_r = height_m * limb["shin"]

    return {
        "height": height_m,
        "headRatio": head_ratio,
        "headH": head_h, "headW": head_w, "headD": head_d,
        "neckH": neck_h, "neckR": neck_r,
        "shoulderW": shoulder_w, "hipW": hip_w,
        #: 躯干**自身**的宽度（比肩窄 —— 肩关节在肩宽两端，躯干在其内侧）
        "torsoW": torso_w,
        "torsoH": torso_h, "torsoDepthK": torso_depth_k,
        "thighH": thigh_h, "shinH": shin_h,
        "upperArmH": upper_arm_h, "foreArmH": fore_arm_h, "handH": hand_h,
        "armR": arm_r, "thighR": thigh_r, "shinR": shin_r,
        # 关键高度（自地面起算）
        "yHip": leg_h,
        "yShoulder": leg_h + torso_h,
        "yNeckTop": leg_h + torso_h + neck_h,
        "yHeadCenter": leg_h + torso_h + neck_h + head_h * 0.5,
        "yTop": height_m,
    }


# ==========================================================================
# 几何构建
# ==========================================================================

def _new_scene(*, keep_camera: bool = True) -> None:
    """清空场景中的**角色几何**，得到确定性的空白起点。

    ⚠️ **不要用 ``bpy.ops.wm.read_factory_settings(use_empty=True)``** ——
    它会重建整个主数据库，使**此前创建的 Material 等数据块的 Python 引用失效**
    （报 ``ReferenceError: StructRNA of type Material has been removed``）。

    ⚠️ **必须保留相机**：逐帧按姿态重建几何时本函数会被调用上百次，
    若把相机一并删除，第二帧就报 ``Cannot render, no camera``。
    相机是渲染的**基础设施**，不属于角色几何 —— 两者的生命周期不同。

    同样**不清理 materials** —— 材质需跨帧复用，否则会堆积上千个数据块。
    """
    cam = bpy.context.scene.camera
    bpy.ops.object.select_all(action="SELECT")
    if keep_camera and cam is not None:
        cam.select_set(False)
    bpy.ops.object.delete(use_global=False)

    for coll in (bpy.data.meshes, bpy.data.cameras,
                 bpy.data.lights, bpy.data.objects):
        for item in list(coll):
            if item.users == 0:
                coll.remove(item)


def _mat(name: str, *, value: float = 0.8, emission: bool = False):
    """建一个简单材质。

    白模只需要**中性灰**：灰度控制吃的是明暗结构，
    不是颜色。因此材质刻意做成无颜色倾向的灰。
    """
    # 复用同名材质 —— 逐帧重建几何时不能每帧新建，否则材质数据块会堆积上千个
    m = bpy.data.materials.get(name)
    if m is not None:
        return m
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    if emission:
        src = nt.nodes.new("ShaderNodeEmission")
        src.inputs["Color"].default_value = (value, value, value, 1.0)
    else:
        src = nt.nodes.new("ShaderNodeBsdfDiffuse")
        src.inputs["Color"].default_value = (value, value, value, 1.0)
    nt.links.new(src.outputs["BSDF" if not emission else "Emission"],
                 out.inputs["Surface"])
    return m


def _sphere(name: str, loc, scale, mat) -> Any:
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=1.0, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = scale
    ob.data.materials.append(mat)
    return ob


def _capsule(name: str, loc, radius: float, half_len: float, mat, *, vertical: bool = True) -> Any:
    """用圆柱 + 两端半球拼一个胶囊体（比布尔运算更可控、更快）。

    ``half_len`` 是圆柱部分的**半长**（不含两端球冠）。
    """
    parts = []
    # 圆柱
    depth = half_len * 2
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=20, radius=radius, depth=depth, location=loc,
        rotation=(0, 0, 0) if vertical else (0, math.pi / 2, 0),
    )
    cyl = bpy.context.active_object
    cyl.name = f"{name}_cyl"
    cyl.data.materials.append(mat)
    parts.append(cyl)
    # 两端球冠
    axis = (0, 0, 1) if vertical else (1, 0, 0)
    for sign in (1, -1):
        c = [loc[i] + axis[i] * sign * half_len for i in range(3)]
        s = _sphere(f"{name}_cap{'A' if sign > 0 else 'B'}", c,
                    (radius, radius, radius), mat)
        parts.append(s)
    # 合并为一个对象，减少对象数（也让"同一角色"在图上更紧凑）
    bpy.ops.object.select_all(action="DESELECT")
    for p in parts:
        p.select_set(True)
    bpy.context.view_layer.objects.active = parts[0]
    bpy.ops.object.join()
    ob = bpy.context.active_object
    ob.name = name
    return ob


def solve_joints(params: dict[str, Any]) -> dict[str, tuple[float, float, float]]:
    """解算**默认站姿**的关节位置。

    这是 :func:`solve_joints_posed` 在空姿态下的退化情形 ——
    **只有一套骨架定义**，避免两套解算器各自漂移。

    早期版本这里是独立实现（按绝对高度摆点），与姿态解算器并存，
    结果两者的"默认站姿"并不相同（手臂外张 6° vs 0°），
    差异虽小但会让"默认"失去意义。

    站姿参数（可选）：
      ``stance_width`` 站姿宽度系数（相对胯宽）
      ``arm_spread``   手臂外张角（度）—— 映射到姿态的 ``arm_out``
      ``arm_bend``     肘部弯曲角（度）—— 映射到姿态的 ``elbow_bend``
    """
    pose: dict[str, float] = {}
    if "arm_spread" in params:
        v = float(params["arm_spread"])
        pose["arm_out_L"] = pose["arm_out_R"] = v
    if "arm_bend" in params:
        v = float(params["arm_bend"])
        pose["elbow_bend_L"] = pose["elbow_bend_R"] = v
    return solve_joints_posed(params, pose)


# ==========================================================================
# 姿态（动作模板的数学基础）
# ==========================================================================

#: 姿态参数的**全部合法键**。用白名单是因为这些值要经 LLM 选择 ——
#: 一个拼错的键如果被静默忽略，会得到"看起来对但姿势不对"的白模。
POSE_KEYS = (
    "arm_swing_L", "arm_swing_R",          # 上臂矢状面摆动（前后）
    "arm_out_L", "arm_out_R",              # 上臂冠状面外张（左右）
    "elbow_bend_L", "elbow_bend_R",        # 肘部弯曲
    "leg_swing_L", "leg_swing_R",          # 髋部摆动（迈步）
    "knee_bend_L", "knee_bend_R",          # 膝部弯曲
    "spine_lean", "spine_twist",           # 躯干前倾 / 扭转
    "head_pitch", "head_yaw",              # 头部俯仰 / 转向
    "pelvis_drop",                         # 重心下沉（下蹲类）
)

#: 姿态数值的安全范围（度）。超出即报错而非夹紧 ——
#: 因为一个 300° 的关节旋转一定是配置写错，静默夹紧会掩盖错误。
_POSE_LIMIT = 170.0


def validate_pose(pose: Mapping[str, Any], *, allow_unknown: bool = True) -> dict[str, float]:
    """校验并归一化姿态参数。

    **未知键告警但不报错**（``allow_unknown``），因为动作模板库会扩展；
    但**越界值一律报错** —— 那是配置错误，不是"新模板"。
    """
    out: dict[str, float] = {}
    for k, v in pose.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"姿态参数 {k!r} 必须是数值，得到 {v!r}")
        if abs(fv) > _POSE_LIMIT:
            raise ValueError(
                f"姿态参数 {k!r} = {fv} 超出 ±{_POSE_LIMIT}° —— "
                f"这通常是配置写错（关节不可能旋转这么多）")
        if k not in POSE_KEYS:
            print(f"WARN_POSE 未知姿态参数 {k!r}，已忽略（合法键见 POSE_KEYS）")
            continue
        out[k] = fv
    return out


def solve_joints_posed(params: dict[str, Any], pose: Mapping[str, Any]
                       ) -> dict[str, tuple[float, float, float]]:
    """按姿态解算关节位置（**层级式正向运动学**）。

    **为什么需要它**：早期版本的动作模板只让角色"整体平移"，
    没有肢体姿势 —— 渲染出来是"一个僵硬人形在滑行"，
    既不像走路，也提供不了 H3 `Pose` 条件需要的骨架结构。

    层级：骨盆 → 脊柱 → 胸 → 颈 → 头，四肢各挂在其根关节上。
    每一段的旋转都在**父坐标系**里施加，所以改一个关节不会破坏上游。

    姿态全部为**零表示直立站姿** —— 与 `solve_joints` 的结果一致，
    因此默认姿态下二者等价（有测试保证）。
    """
    import mathutils

    p = solve_proportions(params)
    po = validate_pose(pose)

    def rot(deg: float, axis: str):
        return mathutils.Matrix.Rotation(math.radians(deg), 4, axis)

    # ---- 中轴链 ----
    # 骨盆：可下沉
    pelvis = mathutils.Vector((0.0, 0.0, p["yHip"] - po.get("pelvis_drop", 0.0)))
    # 脊柱：前倾（绕 X）+ 扭转（绕 Z）
    spine_m = rot(po.get("spine_lean", 0.0), "X") @ rot(po.get("spine_twist", 0.0), "Z")
    #: 半段躯干 —— 骨盆→脊柱→胸 是**连续两段**，各占半个躯干高。
    #:
    #: ⚠️ 曾经把胸也算成 ``pelvis + half``，于是胸落在脊柱的位置上，
    #: 整条上半身矮了半个躯干（实测 0.254 m）—— 而**渲染出来只是"矮一点"**，
    #: 从图上完全看不出来。是靠"默认姿态必须与原解算一致"的断言才发现的。
    half_torso = mathutils.Vector((0, 0, p["torsoH"] * 0.5))
    spine = pelvis + spine_m @ half_torso
    chest = spine + spine_m @ half_torso

    neck_m = spine_m @ rot(po.get("head_pitch", 0.0) * 0.35, "X")
    neck = chest + neck_m @ mathutils.Vector((0, 0, p["neckH"] * 0.5))

    head_m = spine_m @ rot(po.get("head_pitch", 0.0), "X") @ rot(po.get("head_yaw", 0.0), "Z")
    head = chest + head_m @ mathutils.Vector((0, 0, p["neckH"] + p["headH"] * 0.5))

    J: dict[str, tuple[float, float, float]] = {}
    for k, v in (("pelvis", pelvis), ("spine", spine), ("chest", chest),
                 ("neck", neck), ("head", head)):
        J[k] = (v.x, v.y, v.z)

    half_sh = p["shoulderW"] / 2
    for side, sx in (("L", 1.0), ("R", -1.0)):
        # 默认外张 6°：与 solve_joints 的默认站姿一致 ——
        # 零姿态两者应给出同一副骨架，否则"默认姿态"就是另一种站姿了。
        swing = po.get(f"arm_swing_{side}", 0.0)
        out_w = po.get(f"arm_out_{side}", 6.0)
        bend = po.get(f"elbow_bend_{side}", 6.0)
        leg_sw = po.get(f"leg_swing_{side}", 0.0)
        knee_b = po.get(f"knee_bend_{side}", 0.0)

        # 肩：在胸的坐标系里
        sh = chest + spine_m @ mathutils.Vector((sx * half_sh * 0.88, 0.0, p["armR"] * 0.1))
        # 上臂朝向：前摆(绕X) + 外张(绕Y)
        arm_m = spine_m @ rot(swing, "X") @ rot(sx * out_w, "Y")
        el = sh + arm_m @ mathutils.Vector((0, 0, -p["upperArmH"]))
        # 前臂：在臂坐标系里再弯肘（肘只能向后弯，故取负）
        fore_m = arm_m @ rot(-bend, "X")
        wr = el + fore_m @ mathutils.Vector((0, 0, -p["foreArmH"]))
        he = wr + fore_m @ mathutils.Vector((0, 0, -p["handH"] * 0.9))

        # 髋：在骨盆坐标系里
        hp = pelvis + rot(po.get("spine_twist", 0.0) * 0.4, "Z") @ mathutils.Vector(
            (sx * p["hipW"] * 0.26 * float(params.get("stance_width", 1.0)), 0.0, 0.0))
        leg_m = rot(leg_sw, "X")
        kn = hp + leg_m @ mathutils.Vector((0, 0, -p["thighH"]))
        shin_m = leg_m @ rot(knee_b, "X")
        an = kn + shin_m @ mathutils.Vector((0, 0, -p["shinH"]))
        to = an + mathutils.Vector((0, p["shinR"] * 1.4, 0))
        # 脚踩地：若踝高于地面则不强制（迈步时正常）
        to.z = max(0.0, min(an.z, to.z))

        for nm, v in ((f"shoulder_{side}", sh), (f"elbow_{side}", el),
                      (f"wrist_{side}", wr), (f"handEnd_{side}", he),
                      (f"hip_{side}", hp), (f"knee_{side}", kn),
                      (f"ankle_{side}", an), (f"toe_{side}", to)):
            J[nm] = (v.x, v.y, v.z)
    return J


# ==========================================================================
# 动作模板库
# ==========================================================================
#
# 设计原则与平台一致：**LLM 只选模板名，不产生坐标。**
# 数值空间推理对 LLM 不可靠 —— 让它输出关节角会得到"看似合理但穿模"的结果。
#
# 每个模板是一个**关键帧列表**：``[(t, pose), ...]``，t ∈ [0, 1]。
# t 为 0 与 1 的项是端点；中间项按需插值。
#
# ⚠️ 模板库来自人工设计，**不复用鲲鹏导演台的模板数据** ——
# 鲲鹏的模板是给 Three.js 的骨骼绑定用的，格式与关节语义都不同，
# 强行转换的收益低于重新设计的成本（见策划书 §3.8.5）。

ACTION_TEMPLATES: dict[str, list[tuple[float, dict[str, float]]]] = {
    # 静立：零姿态
    "stand": [(0.0, {}), (1.0, {})],

    # 走路：四肢交替摆动。上臂与对侧腿同相（正常步态）
    "walk": [
        (0.00, {"arm_swing_L": 22, "arm_swing_R": -22, "leg_swing_L": -20,
                "leg_swing_R": 20, "knee_bend_L": 6, "knee_bend_R": 22,
                "elbow_bend_L": 16, "elbow_bend_R": 16, "pelvis_drop": 0.01}),
        (0.25, {"arm_swing_L": 0, "arm_swing_R": 0, "leg_swing_L": 0,
                "leg_swing_R": 0, "knee_bend_L": 14, "knee_bend_R": 14,
                "elbow_bend_L": 16, "elbow_bend_R": 16, "pelvis_drop": 0.0}),
        (0.50, {"arm_swing_L": -22, "arm_swing_R": 22, "leg_swing_L": 20,
                "leg_swing_R": -20, "knee_bend_L": 22, "knee_bend_R": 6,
                "elbow_bend_L": 16, "elbow_bend_R": 16, "pelvis_drop": 0.01}),
        (0.75, {"arm_swing_L": 0, "arm_swing_R": 0, "leg_swing_L": 0,
                "leg_swing_R": 0, "knee_bend_L": 14, "knee_bend_R": 14,
                "elbow_bend_L": 16, "elbow_bend_R": 16, "pelvis_drop": 0.0}),
        (1.00, {"arm_swing_L": 22, "arm_swing_R": -22, "leg_swing_L": -20,
                "leg_swing_R": 20, "knee_bend_L": 6, "knee_bend_R": 22,
                "elbow_bend_L": 16, "elbow_bend_R": 16, "pelvis_drop": 0.01}),
    ],

    # 快跑：幅度更大，躯干前倾
    "run": [
        (0.00, {"arm_swing_L": 48, "arm_swing_R": -48, "leg_swing_L": -40,
                "leg_swing_R": 40, "knee_bend_L": 20, "knee_bend_R": 70,
                "elbow_bend_L": 80, "elbow_bend_R": 80, "spine_lean": 12}),
        (0.50, {"arm_swing_L": -48, "arm_swing_R": 48, "leg_swing_L": 40,
                "leg_swing_R": -40, "knee_bend_L": 70, "knee_bend_R": 20,
                "elbow_bend_L": 80, "elbow_bend_R": 80, "spine_lean": 12}),
        (1.00, {"arm_swing_L": 48, "arm_swing_R": -48, "leg_swing_L": -40,
                "leg_swing_R": 40, "knee_bend_L": 20, "knee_bend_R": 70,
                "elbow_bend_L": 80, "elbow_bend_R": 80, "spine_lean": 12}),
    ],

    # 回头：头转向，肩部微跟
    "look_back": [
        (0.00, {}),
        (0.45, {"head_yaw": 62, "spine_twist": 14, "arm_out_L": 5}),
        (1.00, {"head_yaw": 58, "spine_twist": 12, "arm_out_L": 5}),
    ],

    # 指向：右臂抬起前指
    "point_forward": [
        (0.00, {}),
        (0.40, {"arm_swing_R": -78, "elbow_bend_R": 8, "arm_out_R": 6,
                "spine_lean": 4, "head_pitch": -4}),
        (1.00, {"arm_swing_R": -74, "elbow_bend_R": 10, "arm_out_R": 6,
                "spine_lean": 4, "head_pitch": -4}),
    ],

    # 坐下：重心下沉、髋膝大幅弯曲
    "sit_down": [
        (0.00, {}),
        (1.00, {"pelvis_drop": 0.42, "leg_swing_L": 78, "leg_swing_R": 78,
                "knee_bend_L": -82, "knee_bend_R": -82, "spine_lean": 10,
                "arm_swing_L": 18, "arm_swing_R": 18}),
    ],

    # 打招呼：右臂举起挥动
    "wave": [
        (0.00, {"arm_swing_R": -30, "arm_out_R": 40, "elbow_bend_R": 70}),
        (0.30, {"arm_swing_R": -30, "arm_out_R": 62, "elbow_bend_R": 82,
                "head_pitch": -3}),
        (0.60, {"arm_swing_R": -30, "arm_out_R": 44, "elbow_bend_R": 72}),
        (1.00, {"arm_swing_R": -30, "arm_out_R": 60, "elbow_bend_R": 80}),
    ],

    # 跌倒/受击：后仰、手臂张开
    "recoil": [
        (0.00, {}),
        (0.18, {"spine_lean": -16, "head_pitch": 14, "arm_out_L": 32,
                "arm_out_R": 32, "arm_swing_L": 26, "arm_swing_R": 26,
                "leg_swing_L": -10, "knee_bend_L": 12}),
        (1.00, {"spine_lean": -6, "head_pitch": 5, "arm_out_L": 14,
                "arm_out_R": 14, "leg_swing_L": -4}),
    ],
}


def pose_at(template: str, t: float) -> dict[str, float]:
    """在归一化时刻 ``t ∈ [0,1]`` 采样动作模板，线性插值。

    **不含随机性**：同一 ``(template, t)`` 必然得到同一姿态 ——
    这是白模可哈希、可缓存的前提。
    """
    if template not in ACTION_TEMPLATES:
        raise KeyError(f"未知动作模板 {template!r}；可用：{sorted(ACTION_TEMPLATES)}")
    keys = ACTION_TEMPLATES[template]
    t = min(max(float(t), 0.0), 1.0)

    if t <= keys[0][0]:
        return dict(keys[0][1])
    if t >= keys[-1][0]:
        return dict(keys[-1][1])

    for i in range(len(keys) - 1):
        t0, p0 = keys[i]
        t1, p1 = keys[i + 1]
        if t0 <= t <= t1:
            span = (t1 - t0) or 1.0
            w = (t - t0) / span
            names = set(p0) | set(p1)
            return {k: p0.get(k, 0.0) * (1 - w) + p1.get(k, 0.0) * w for k in names}
    return dict(keys[-1][1])


def _limb(name: str, a: tuple[float, float, float], b: tuple[float, float, float],
          r_a: float, r_b: float, mat: Any) -> list[Any]:
    """在两个关节之间接一段**锥形**肢体。

    用圆锥台（``primitive_cone_add`` 带两端半径）而不是圆柱 ——
    真实肢体是近端粗、远端细，锥形让白模更像人、也让控制信号的
    深度/明暗更有结构。
    """
    import mathutils

    va, vb = mathutils.Vector(a), mathutils.Vector(b)
    d = vb - va
    length = d.length
    if length < 1e-6:
        return []
    mid = (va + vb) / 2

    bpy.ops.mesh.primitive_cone_add(
        vertices=20, radius1=r_a, radius2=r_b, depth=length, location=mid,
    )
    ob = bpy.context.active_object
    ob.name = name
    ob.data.materials.append(mat)
    ob.rotation_euler = d.to_track_quat("Z", "Y").to_euler()
    return [ob]


def _joint(name: str, at: tuple[float, float, float], r: float, mat: Any) -> Any:
    """关节处的球体 —— 它让相邻肢体自然融合，消除"分离的条状物"观感。"""
    return _sphere(name, at, (r, r, r), mat)


def build_character(params: dict[str, Any], *, root_name: str = "CHAR_PROXY",
                    pose: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构建角色代理并返回解算出的比例与关节位置。

    ``pose`` 给定时按该姿态构建 —— 逐帧传入不同姿态即得到动画。
    这比"建好骨骼再驱动"更笨，但对白模**足够且更可控**：
    每帧约 40 个图元、几十毫秒，而渲染本身就要 55 ms。

    返回 ``{"proportions": {...}, "joints": {...}, "root": name}``。
    """
    p = solve_proportions(params)
    J = solve_joints_posed(params, pose) if pose else solve_joints(params)
    _new_scene()                     # 先清场，再建材质（顺序不能反）
    body = _mat("MAT_body", value=0.72)
    hair = _mat("MAT_hair", value=0.30)   # 头发稍暗，给出可辨认的头部结构

    objs: list[Any] = []

    # ---- 躯干：胯 -> 胸的锥台（下窄上宽更接近人体）----
    # 半径取 torsoW（躯干自身宽度），不是 shoulderW ——
    # 肩关节位于肩宽两端，躯干在其内侧，混用会让躯干跟肩一样宽（"方盒子"）。
    objs += _limb("torso",
                  J["pelvis"], J["chest"],
                  p["torsoW"] * 0.86, p["torsoW"] * 1.00, body)
    # 躯干压扁成椭圆截面（前后比左右薄）
    for o in objs:
        o.scale[1] = p["torsoDepthK"] / 0.60
    # 胯部关节（球体连接躯干与腿）
    objs.append(_joint("pelvis_j", J["pelvis"], p["hipW"] * 0.30, body))

    # ---- 颈与头 ----
    objs += _limb("neck", J["neck"], J["head"],
                  p["neckR"] * 1.15, p["neckR"] * 0.95, body)
    objs.append(_joint("neck_j", J["neck"], p["neckR"] * 1.1, body))
    objs.append(_sphere("head", J["head"],
                        (p["headW"] / 2, p["headD"] / 2, p["headH"] / 2), body))

    # ---- 头发 ----
    hair_style = str(params.get("hair", "none"))
    if hair_style != "none":
        long = hair_style in ("long_straight", "long", "ponytail")
        objs.append(_sphere(
            "hair", (J["head"][0], J["head"][1], J["head"][2] + p["headH"] * 0.05),
            (p["headW"] / 2 * 1.07, p["headD"] / 2 * 1.07,
             p["headH"] / 2 * (1.22 if long else 1.03)), hair))
        if long:
            # 长发：颈后到肩背的一束
            objs += _limb("hair_back",
                          (0.0, J["head"][1] + p["headD"] * 0.28, J["head"][2]),
                          (0.0, J["neck"][1] + p["headD"] * 0.30, J["chest"][2]),
                          p["headW"] * 0.34, p["headW"] * 0.26, hair)

    # ---- 四肢：关节之间接锥台 ----
    for side in ("L", "R"):
        sh, el, wr, he = (J[f"shoulder_{side}"], J[f"elbow_{side}"],
                          J[f"wrist_{side}"], J[f"handEnd_{side}"])
        hp, kn, an, to = (J[f"hip_{side}"], J[f"knee_{side}"],
                          J[f"ankle_{side}"], J[f"toe_{side}"])
        # 肩关节球 —— 消除手臂与躯干的缝
        objs.append(_joint(f"shoulder_j_{side}", sh, p["armR"] * 1.15, body))
        objs += _limb(f"upperArm_{side}", sh, el, p["armR"] * 1.02, p["armR"] * 0.90, body)
        objs.append(_joint(f"elbow_j_{side}", el, p["armR"] * 0.92, body))
        objs += _limb(f"foreArm_{side}", el, wr, p["armR"] * 0.86, p["armR"] * 0.68, body)
        objs.append(_joint(f"wrist_j_{side}", wr, p["armR"] * 0.66, body))
        objs += _limb(f"hand_{side}", wr, he, p["armR"] * 0.62, p["armR"] * 0.42, body)

        objs.append(_joint(f"hip_j_{side}", hp, p["thighR"] * 1.05, body))
        objs += _limb(f"thigh_{side}", hp, kn, p["thighR"] * 1.02, p["thighR"] * 0.84, body)
        objs.append(_joint(f"knee_j_{side}", kn, p["thighR"] * 0.86, body))
        objs += _limb(f"shin_{side}", kn, an, p["shinR"] * 1.02, p["shinR"] * 0.76, body)
        objs.append(_joint(f"ankle_j_{side}", an, p["shinR"] * 0.74, body))
        objs += _limb(f"foot_{side}", an, to, p["shinR"] * 0.72, p["shinR"] * 0.44, body)

    # ---- 汇总为一个对象 ----
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    root = bpy.context.active_object
    root.name = root_name

    return {"proportions": p, "joints": J, "root": root.name}


# ==========================================================================
# CLI
# ==========================================================================

def _params_from_argv() -> dict[str, Any]:
    argv = sys.argv
    if "--" not in argv:
        return {}
    rest = argv[argv.index("--") + 1:]
    if not rest:
        return {}
    path = rest[0]
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    params = _params_from_argv()
    result = build_character(params)
    p = result["proportions"]
    print("CHARACTER_BUILT", json.dumps({
        "root": result["root"],
        "heightM": round(p["height"], 4),
        "headRatio": round(p["headRatio"], 2),
        "shoulderW": round(p["shoulderW"], 4),
        "legH": round(p["yHip"], 4),
        "headH": round(p["headH"], 4),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
