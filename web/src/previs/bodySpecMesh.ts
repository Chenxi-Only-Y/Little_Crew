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

import type { BodySegment, Vec3, WhiteModelSpec } from './types';
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
/**
 * 按规格做前向运动学。
 *
 * ## ⚠️ 姿态的两种语义，混用会把姿势彻底搞坏
 *
 * `humanoid.ts` 的姿态预设（`poseWalk` / `poseRun` / …）给的是
 * **绝对关节角**——"跑步时肩膀应转到 −1.05 弧度"，那是一个完整的姿势描述，
 * 不是"在静止姿态上再摆多少"。
 *
 * 而 `idle` 是特例：它几乎就是零值（只有几度的自然下垂），
 * 所以它适合当**偏移量**叠在规格的静止旋转之上。
 *
 * 我先前一律相加，于是跑步时肩被算成 `0.04 + (−1.05)` 之外还要再被
 * 规格的其它角度干扰，画面上四肢翻折、畸形（用户报的"姿势还是有问题"）。
 *
 * 现在的规则：
 *   · `usePoseDirectly = true`（非 idle 预设）→ **预设值直接用**，
 *     规格的 rotations 不参与。这样跑步就是跑步，不管规格把肩调成几度。
 *   · `usePoseDirectly = false`（idle）→ `规格旋转 + 姿态偏移`，
 *     这正是"静止姿态 + 用户微调"的正确语义。
 *
 * 无论哪种，用户的逐关节微调（`poseOverride`）始终是**叠加**——
 * 它本来就是偏移量（见 `Actor.poseOverride` 的说明）。
 */
export function specForwardKinematics(
  spec: WhiteModelSpec,
  pose: Record<string, Vec3>,
  usePoseDirectly = false,
): Record<string, BodyJointWorld> {
  const out: Record<string, BodyJointWorld> = {};
  for (const name of Object.keys(BODY_JOINT_PARENT)) {
    const parent = BODY_JOINT_PARENT[name];

    const base = resolvedRotation(spec, name);
    const poseVal = pose[name];
    let rx: number;
    let ry: number;
    let rz: number;
    if (usePoseDirectly && poseVal) {
      // 预设给的是绝对角度：直接采用，规格的静止旋转不参与
      [rx, ry, rz] = poseVal;
    } else {
      const add = poseVal ?? ([0, 0, 0] as Vec3);
      [rx, ry, rz] = [base[0] + add[0], base[1] + add[1], base[2] + add[2]];
    }
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
 * 关节球半径：**比相接肢体略粗**，让交接处真正连上。
 *
 * ## 这里的目标改过一次，方向是反的
 *
 * 我先前把它夹在"不超过最细肢体的 0.92 倍"，理由是"给大了会在肘/膝
 * 鼓出疙瘩、像一串珠子"。那个顾虑本身没错，但**夹过头了**：
 * 球比肢体细 → 两段肢体之间出现可见的细腰，视觉上就是**一节一节断开**。
 * 用户看到的"关节断开"正是这个。
 *
 * 正确的做法是**略粗一点点**（1.05 倍）：球稍微鼓出，交接处被包住，
 * 读起来是"一根连续的肢体、关节处略粗"——那正是白模需要的形态。
 * 真正要防的不是"略粗"，而是"粗很多"（那才会成串珠），所以上限留在 1.25 倍。
 */
function clampJointRadii(
  radii: Record<string, number>,
  segments: Array<{ seg: BodySegment }>,
): Record<string, number> {
  const thinnest: Record<string, number> = {};
  for (const { seg } of segments) {
    /**
     * ⚠️ **必须跳过 `r0/r1` 为 0 的段**（`box` 形状：手掌、脚掌）。
     *
     * 它们按设计不用半径、只用 `size`；若把 0 当成"最细半径"，
     * 与它相接的关节球会被算成 `0 × 1.05 ≈ 0` ——球直接缩没，
     * 手腕/脚踝处**看起来是断的**。实测 `wri_l` 的球半径被算成 0.0061，
     * 而相邻前臂半径是 0.045，差了 7 倍。
     */
    const r = Math.min(seg.r0, seg.r1);
    if (!(r > 0)) continue;
    const parent = BODY_JOINT_PARENT[seg.joint];
    if (parent) thinnest[parent] = Math.min(thinnest[parent] ?? Infinity, r);
    thinnest[seg.joint] = Math.min(thinnest[seg.joint] ?? Infinity, r);
  }
  const out: Record<string, number> = {};
  for (const [name, r] of Object.entries(radii)) {
    const thinnestAdj = thinnest[name];
    // 躯干/头这些"本身就是主体"的关节不按肢体推，直接用规格给的半径
    const isTorso = ['pelvis', 'spine', 'chest', 'head'].includes(name);
    if (thinnestAdj === undefined || isTorso) {
      out[name] = r;
      continue;
    }
    // 取"规格值"与"相邻肢体 ×1.05"里较大的那个，再用 1.25 倍封顶
    out[name] = Math.min(Math.max(r, thinnestAdj * 1.05), thinnestAdj * 1.25);
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
   * 姿态处理（见 `specForwardKinematics` 的说明）：
   *   · `idle`      → 规格的 rotations 就是静止姿态，不叠加任何预设；
   *   · 其余预设     → 预设给的是**绝对角度**，直接采用、不叠加规格旋转；
   *   · 用户的逐关节微调（`poseOverride`）永远**叠加**在最上层。
   */
  const isIdle = safe.pose === 'idle';
  const poseBase = isIdle
    ? ({} as Record<string, Vec3>)
    : (samplePose(safe.pose, opts.time ?? 0, 1, 0) as Record<string, Vec3>);
  const pose = applyPoseOverride(poseBase, opts.poseOverride);

  const joints = specForwardKinematics(safe, pose, !isIdle);

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

/**
 * 把"局部 +Y 轴要指向的方向"转成 Three.js 的欧拉角 `[rx, 0, -rz]`。
 *
 * ## ⚠️ 这里有个曾经让整具模型散架的坑
 *
 * Three.js 的欧拉顺序是 `R = Rx(rx) · Ry(0) · Rz(-rz)`。
 * 把 +Y 代进去，方向是：
 *
 *     R · (0,1,0) = (sin(-rz)·cos(rx),  cos(rx)·cos(rz),  −sin(rx))
 *
 * 反解出：
 *
 *     rx = −asin(dir.y)
 *     rz = −atan2(dir.x, dir.z)
 *
 * 我先前写的是 `pitch = acos(dir.y)` + `rz = atan2(dir.x, dir.z)`，
 * 两处都错，而 `acos` 丢掉符号是**最致命**的那处：
 * 朝下的向量（dir.y = −1）会得到 `pitch = π`，于是 `Rx(π)` 把 +Y
 * 轴转成了 **−Y 的反方向**——所有朝下的肢体（大腿、小腿、上臂、前臂）
 * **全部被翻成朝上**，画面上就是"四肢呈 V 形往外撇、关节错位断开"。
 *
 * 而几何数据一直是**正确**的（实测大腿 `dir = [0.03, −1, 0]`，
 * 几乎垂直向下）。所以这个 bug 只能在渲染这一步找，查数据是查不出来的。
 */
export function dirToEuler(dir: Vec3): Vec3 {
  const y = Math.max(-1, Math.min(1, dir[1]));
  const rx = -Math.asin(y);
  // 完全竖直时 atan2(0,0) 无意义，此时 yaw 取 0 即可
  const rz = Math.abs(dir[0]) < 1e-9 && Math.abs(dir[2]) < 1e-9
    ? 0
    : -Math.atan2(dir[0], dir[2]);
  return [rx, 0, rz];
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
