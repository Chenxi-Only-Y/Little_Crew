/**
 * 白模规格 → Three.js 几何。
 *
 * ## 与 `humanoid.ts` 的分工
 *
 * `humanoid.ts` 是**写死的骨架**（球+圆柱），处理"姿态预设怎么动"。
 * 这个文件只做一件事：把一份**规格**（AI 生成或人工改过的 JSON）
 * 换算成具体的体块位置与尺寸。它**复用** `humanoid.ts` 的矩阵工具与
 * 姿态函数——两套运动学实现迟早会不一致，那种偏差极难查。
 *
 * ## v2 的推算（位置不可直写）
 *
 *     关节旋转 = resolvedRotation(规格, 关节) + 姿态叠加
 *     关节偏移 = 父旋转 × (0, −段长, 0) + 横向基础偏移
 *     关节位置 = 父位置 + 关节偏移
 *
 * "横向基础偏移"只来自**比例**（肩宽/髋宽），规格不能直写，
 * 所以"手臂撇成八字"在结构上无法出现。
 *
 * ## 接地
 *
 * 规格里的关节以骨盆为原点。渲染前整体上移，让**最低的脚掌落在 y=0**。
 * 不用固定常量：AI 生成的角色腿长各不相同，固定值会让长腿陷地、短腿悬空。
 */

import type { BodySegment, PosePreset, Vec3, WhiteModelSpec } from './types';
import {
  BODY_JOINT_PARENT,
  normalizeBodySpec,
  resolvedOffset,
  resolvedRadii,
  resolvedRotation,
  resolvedSegment,
} from './bodySpec';
import { applyPoseOverride, mm, mv, rotX, rotY, rotZ, samplePose } from './humanoid';

export interface BodyJointWorld {
  pos: Vec3;
  rot: number[];
}

/** 姿态预设只驱动**旋转**（位置由规格的段长决定），所以这里只取旋转 */
function poseRotations(preset: PosePreset, time: number): Record<string, Vec3> {
  const p = samplePose(preset, time, 1, 0) as Record<string, Vec3>;
  return p;
}

/**
 * 按规格做前向运动学。
 *
 * 与 `humanoid.forwardKinematics` 同一套公式，只是：
 *   · 偏移来自 `resolvedOffset`（父旋转 × 段长 + 比例推出的横向偏移）；
 *   · 关节自身旋转 = 规格旋转 + 姿态叠加。
 */
export function specForwardKinematics(
  spec: WhiteModelSpec,
  pose: Record<string, Vec3>,
): Record<string, BodyJointWorld> {
  const out: Record<string, BodyJointWorld> = {};
  for (const name of Object.keys(BODY_JOINT_PARENT)) {
    const parent = BODY_JOINT_PARENT[name];

    const base = resolvedRotation(spec, name);
    const add = pose[name] ?? ([0, 0, 0] as Vec3);
    // 姿态是**叠加偏移**（与 v1 的 poseOverride 同一语义）：预设驱动动作，
    // 规格给的是"静止"基准，两者相加。这样"行走"不会因规格而定格。
    const [rx, ry, rz] = [base[0] + add[0], base[1] + add[1], base[2] + add[2]];
    const local = mm(rotZ(rz), mm(rotY(ry), rotX(rx)));

    if (parent === null) {
      out[name] = { pos: [0, 0, 0], rot: local };
      continue;
    }
    const p = out[parent];
    if (!p) {
      // 父关节不存在（规格被手改坏了）——跳过而不是让整页崩
      out[name] = { pos: [0, 0, 0], rot: local };
      continue;
    }
    /**
     * ⚠️ 偏移要**在父的坐标系里**旋转，这样"父转了，子跟着绕父摆"才成立
     * （与 `forwardKinematics` 的约定一致）。横向分开量已经在
     * `resolvedOffset` 里加好，见那里的说明。
     */
    const rot = mm(p.rot, local);
    const off = mv(p.rot, resolvedOffset(spec, name));
    out[name] = {
      pos: [p.pos[0] + off[0], p.pos[1] + off[1], p.pos[2] + off[2]],
      rot,
    };
  }
  return out;
}

export interface BuiltBody {
  joints: Record<string, BodyJointWorld>;
  radii: Record<string, number>;
  segments: Array<{
    seg: BodySegment;
    a: Vec3;
    b: Vec3;
    /** 父→子的单位方向 */
    dir: Vec3;
    length: number;
  }>;
  /** 骨盆需要上移多少才能让最低脚掌落在 y=0 */
  groundOffset: number;
  /**
   * 归一化到 `proportions.height` 所需的统一缩放。
   *
   * 渲染时把它乘到 group 的 scale 上即可——这样"滑杆写 1.70 米，
   * 量出来就是 1.70 米"，而不是一个碰巧接近的数。
   */
  heightScale: number;
}

/**
 * 关节球半径：**不许超过相接肢体最细处的 0.92 倍**。
 *
 * 为什么夹这一刀：关节球是用来填缝的，给大了会在肘/膝/踝上鼓出疙瘩——
 * 模型看起来像"一串珠子"就是这个原因。夹一刀之后，无论规格（或用户
 * 手改的 JSON）把半径写成多少，都不会长出瘤子。
 */
function clampJointRadii(
  radii: Record<string, number>,
  segments: Array<{ seg: BodySegment }>,
): Record<string, number> {
  const thinnest: Record<string, number> = {};
  for (const { seg } of segments) {
    const parent = BODY_JOINT_PARENT[seg.joint];
    const r = Math.min(seg.r0, seg.r1);
    if (parent && r > 0) thinnest[parent] = Math.min(thinnest[parent] ?? Infinity, r);
    if (r > 0) thinnest[seg.joint] = Math.min(thinnest[seg.joint] ?? Infinity, r);
  }
  const out: Record<string, number> = {};
  for (const [name, r] of Object.entries(radii)) {
    const cap = thinnest[name];
    // 躯干/头这些"本身就是主体"的关节不夹
    const isTorso = ['pelvis', 'spine', 'chest', 'head'].includes(name);
    out[name] = cap !== undefined && !isTorso ? Math.min(r, cap * 0.92) : r;
  }
  return out;
}

/**
 * 把规格 + 姿态 + 时间编译成一份可直接渲染的体块表。
 *
 * `time` 只对循环姿态（walk/run）有意义。
 */
export function buildBody(
  spec: WhiteModelSpec,
  opts: { time?: number; poseOverride?: Record<string, Vec3> } = {},
): BuiltBody {
  /**
   * ⚠️ 先归一化。
   *
   * 这里踩过一次坑：测试时直接往 store 里塞了 `segments: []` 的规格，
   * `buildBody` 忠实地渲染出**零个体块**——画面上只剩一排关节球，
   * 看起来像"腿断了"。而调用方（面板）其实是走 `normalizeBodySpec` 的，
   * 所以这个 bug 只在绕过面板时才出现。
   *
   * 结论：**渲染器不能信任输入**。归一化在这里做一次，
   * 无论规格来自面板、工程文件、还是调试脚本，都保证是一具完整的人。
   */
  const safe = normalizeBodySpec(spec);

  /**
   * 姿态：`idle` 时**不叠加**任何预设旋转——规格自带的 `rotations`
   * 就是静止姿态（那才是"标准站姿"的定义处）。其余预设叠加在规格之上。
   */
  const poseAdd =
    safe.pose === 'idle'
      ? ({} as Record<string, Vec3>)
      : applyPoseOverride(poseRotations(safe.pose, opts.time ?? 0), undefined);
  const pose = applyPoseOverride(poseAdd, opts.poseOverride);

  const joints = specForwardKinematics(safe, pose);

  const segments = safe.segments
    .map((s) => resolvedSegment(safe, s))
    .map((seg) => {
      const parent = BODY_JOINT_PARENT[seg.joint];
      if (!parent) return null;
      const a = joints[parent]?.pos;
      const b = joints[seg.joint]?.pos;
      if (!a || !b) return null;
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      const dz = b[2] - a[2];
      const length = Math.hypot(dx, dy, dz);
      const dir: Vec3 = length > 1e-6 ? [dx / length, dy / length, dz / length] : [0, -1, 0];
      return { seg, a: [...a] as Vec3, b: [...b] as Vec3, dir, length };
    })
    .filter((x): x is NonNullable<typeof x> => x !== null);

  const radii = clampJointRadii(
    resolvedRadii(safe),
    segments.map((s) => ({ seg: s.seg })),
  );

  // ---- 接地：最低的脚掌（没有脚掌就退回脚踝）----
  const feetJoints = ['foot_l', 'foot_r'].filter((n) => joints[n]);
  const probe = feetJoints.length ? feetJoints : ['ank_l', 'ank_r'].filter((n) => joints[n]);
  let lowest = Infinity;
  for (const n of probe) {
    const y = joints[n].pos[1];
    if (y < lowest) lowest = y;
  }
  const groundOffset = Number.isFinite(lowest) ? -lowest : 0;

  /**
   * 身高归一化。
   *
   * 实测踩过：规格写 `height: 1.70`，量出来是 **1.773**——因为
   * `height` 原先只是"整体缩放系数"，没有任何一步保证最终高度等于它。
   * 用户拖滑杆写 1.70 却得到 1.77，是明确的欺骗。
   *
   * 这里量出实际最高的顶点（关节位置 + 关节球半径），除以目标身高，
   * 得到统一缩放。渲染时乘到 group 上，于是"写多少就是多少"。
   */
  let topY = -Infinity;
  for (const [name, jw] of Object.entries(joints)) {
    const r = radii[name] ?? 0;
    const y = jw.pos[1] + groundOffset + r;
    if (y > topY) topY = y;
  }
  const rawHeight = Number.isFinite(topY) && topY > 1e-6 ? topY : safe.proportions.height;
  const heightScale = safe.proportions.height / rawHeight;

  return { joints, radii, segments, groundOffset, heightScale };
}

/** 计算一段体块的朝向所需的欧拉角（把局部 +Y 转到 `dir`） */
export function dirToEuler(dir: Vec3): Vec3 {
  const y = Math.max(-1, Math.min(1, dir[1]));
  const pitch = Math.acos(y);
  const yaw = Math.atan2(dir[0], dir[2]);
  return [pitch, 0, -yaw];
}

/** 规格里所有 segment 引用到的关节名（用于 C 模式的"物体树"展示） */
export function specJointTree(spec: WhiteModelSpec): Array<{ name: string; depth: number }> {
  const used = new Set(spec.segments.map((s) => s.joint));
  const out: Array<{ name: string; depth: number }> = [];
  const walk = (name: string, depth: number) => {
    if (used.has(name)) out.push({ name, depth });
    for (const k of Object.keys(BODY_JOINT_PARENT)) {
      if (BODY_JOINT_PARENT[k] === name) walk(k, depth + 1);
    }
  };
  walk('pelvis', 0);
  return out;
}
