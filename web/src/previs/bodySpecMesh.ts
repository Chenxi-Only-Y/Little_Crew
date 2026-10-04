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
  opts: { time?: number; poseOverride?: Record<string, Vec3>; pose?: PosePreset } = {},
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
   *
   * ⚠️ 姿态以 `opts.pose`（即 `Actor.pose`）为准，`spec.pose` 只作兜底。
   *
   * 这里踩过一个坑：右栏「肢体动作」按钮改的是 `Actor.pose`，
   * 而渲染器先前只读 `spec.pose` —— **一旦有了规格，那些按钮就完全失效**
   * （点"坐下"没反应，但碰撞箱却按坐姿算，箱子与模型对不上）。
   * 现在以 actor 的姿势为权威，规格只提供各关节的休息角度。
   */
  const activePose: PosePreset = opts.pose ?? safe.pose;
  const isIdle = activePose === 'idle';
  const poseBase = isIdle
    ? ({} as Record<string, Vec3>)
    : (samplePose(activePose, opts.time ?? 0, 1, 0) as Record<string, Vec3>);
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

  /**
   * 接地：把整组上移，让**整个模型最低的那个几何点**落在 y=0。
   *
   * ## 为什么不能只看"脚掌关节的中心"（这里返工过一次）
   *
   * 旧写法只看最低的 `foot_*` / `ank_*` **关节中心**。站姿下没问题，
   * 但坐姿时踝只到骨盆下方 0.49，脚掌中心也还在骨盆下方——
   * 于是 `groundOffset` 几乎不变，**骨盆高度一点没降，人悬在空中坐着**
   * （用户报的"坐下像悬空"）。
   *
   * 正确的物理含义是"人坐下来，屁股就落到座位面上"：
   * 姿态一变、脚相对骨盆变高了，整具身体就该**下沉**同样的量。
   * 所以接地基准取**含关节球半径的最低顶点**——那才是真正的"身体最低处"。
   */
  /**
   * ⚠️ 这里**不要**再加"按姿态人为下沉"的系数。
   *
   * 我加过一版 `POSE_SINK`（坐姿乘 0.56 把骨盆压低），那是**错的方向**：
   * 大腿水平 + 小腿垂直时，髋到踝的**垂直距离只有 0.49**（站立时 0.93），
   * 所以骨盆自然会落到座位上（实测 21% 身高 ≈ 0.36 米，正是座椅高度）。
   * 强行乘系数只会把**脚埋进地面**去换骨盆高度——治标不治本。
   * 这是几何必然，不是缺陷。
   */
  let lowest = Infinity;
  for (const [name, jw] of Object.entries(joints)) {
    const y = jw.pos[1] - (radii[name] ?? 0);
    if (y < lowest) lowest = y;
  }
  for (const item of segments) {
    /**
     * 圆柱按自身半径外扩；`box`（脚掌/手掌）按**它自己旋转后的世界半高**。
     *
     * ⚠️ 用 `boxHalfExtents` 而不是写死的外扩量——先前写死 `0.02`、
     * 而脚掌实际半高是 0.025，且不随缩放，导致接地基准偏低、
     * 整个模型悬在地面上方（实测箱底 −0.102、踝高 7.9%，真实 4%）。
     * 更要紧的是：这里与 `localBounds` 必须用**同一个公式**，
     * 否则"测出来的包围盒"和"渲染出来的网格"对不上，排查会白费好几轮。
     */
    let r: number;
    if (item.seg.shape === 'box') {
      const size = item.seg.size ?? [0.1, 0.1, 0.1];
      r = boxHalfExtents(size, dirToQuaternion(item.dir))[1];
    } else {
      r = Math.max(item.seg.r0, item.seg.r1);
    }
    const y = Math.min(item.a[1], item.b[1]) - r;
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
 * 把"局部 +Y 轴要指向的方向"转成 Three.js 的**四元数**。
 *
 * ## 为什么改用四元数，而不是继续调欧拉角
 *
 * 用欧拉角（只用 `rx` / `rz` 两个轴）**无法精确对齐任意方向**——
 * 当方向同时有 x 与 z 分量时会出现耦合：我试过两版公式，
 * 第二版对纯轴向完美（误差 0），但 `[0.05, −0.996, −0.075]`
 * 这类方向的误差仍有 **0.15**（z 分量符号反掉）。
 *
 * `Quaternion.setFromUnitVectors(from, to)` 是"把某轴对齐到某方向"的
 * 标准精确解法，**误差理论上为 0**。而且它直接返回四元数，
 * 交给 `mesh.quaternion` 即可——不必再操心欧拉顺序（XYZ/YXZ/ZYX）
 * 这个我错了两次的地方。
 *
 * ⚠️ 关键前提：`dir` 必须是**局部空间**的方向。
 * 渲染时把 mesh 放进角色的 group 里，group 自己的旋转由世界变换承担，
 * 所以这里用局部 `dir` 是对的。
 */
export function dirToQuaternion(dir: Vec3): [number, number, number, number] {
  const [dx, dy, dz] = dir;
  const len = Math.hypot(dx, dy, dz);
  if (len < 1e-9) return [0, 0, 0, 1]; // 退化：单位四元数
  const x = dx / len;
  const y = dy / len;
  const z = dz / len;
  // 从 (0,1,0) 转到 (x,y,z)：q = (cross, 1 + dot)，再归一化
  //   dot = y, cross = (0,1,0) × (x,y,z) = (z, 0, −x)
  const w = 1 + y;
  if (w < 1e-9) {
    // 方向与 +Y 恰好相反：任取一条垂直轴旋转 180°
    return [1, 0, 0, 0];
  }
  const qx = z;
  const qy = 0;
  const qz = -x;
  const qw = w;
  const qlen = Math.hypot(qx, qy, qz, qw);
  return [qx / qlen, qy / qlen, qz / qlen, qw / qlen];
}

/**
 * 一个 `box` 体块在**世界轴**上的半个尺寸。
 *
 * ## 为什么需要它（这里返工过一次）
 *
 * `box`（脚掌、手掌）是斜着放的，它在世界 Y 轴上的半高**不等于** `size[1]/2`。
 * 我先前在 `localBounds` 里按**半对角线**外扩（脚掌算出 0.112），
 * 而接地逻辑按半高算（0.025）——**两处不一致**，于是我"测量"到的
 * 包围盒底部是 −0.102，而实际渲染的脚是贴地的。测量和渲染对不上，
 * 就会像这次一样白查好几轮。
 *
 * 正确做法：把四元数转成 3×3 旋转矩阵，再按
 * `extent_j = Σ_i |R[j][i]| · half_i` 求世界轴半长（AABB 的标准公式）。
 */
export function boxHalfExtents(
  size: Vec3,
  quat: [number, number, number, number],
): Vec3 {
  const [x, y, z, w] = quat;
  // 四元数 → 旋转矩阵（行优先，Three.js 的约定）
  const xx = x * x, yy = y * y, zz = z * z;
  const xy = x * y, xz = x * z, yz = y * z;
  const wx = w * x, wy = w * y, wz = w * z;
  const m = [
    1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy),
    2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx),
    2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy),
  ];
  const h = [Math.abs(size[0]) / 2, Math.abs(size[1]) / 2, Math.abs(size[2]) / 2];
  const out: Vec3 = [0, 0, 0];
  for (let j = 0; j < 3; j++) {
    out[j] = Math.abs(m[j * 3]) * h[0] + Math.abs(m[j * 3 + 1]) * h[1] + Math.abs(m[j * 3 + 2]) * h[2];
  }
  return out;
}

/** 轴对齐包围盒（模型局部坐标，已含接地偏移） */
export interface LocalBounds {
  min: Vec3;
  max: Vec3;
  center: Vec3;
  size: Vec3;
}

/**
 * 从**实际几何**求包围盒 —— 这就是"碰撞箱"。
 *
 * ## 为什么不能用固定值（用户指出的问题）
 *
 * 我先前把选择框写死成 `[0.8, height × 1.08, 0.8]`：
 * **站起来、坐着、跑步、举手，箱子一样大**。这显然是错的——
 * 一个人坐下时高度只剩六成，伸手时横向宽一倍，
 * 固定的箱子既框不住、也框太多。
 *
 * 正确的做法是把每段体块的端点与半径都算进去：
 *   · 关节位置（`joints`）给出骨架的极值；
 *   · 每段肢体沿它的两个端点各按该端半径外扩（圆柱侧面也算进去）；
 *   · 关节球按各自半径外扩。
 *
 * 所以箱子**随姿态变化**：坐姿会矮、举手会高、跑步会前倾加长。
 */
export function localBounds(built: BuiltBody): LocalBounds {
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;

  const grow = (x: number, y: number, z: number, r: number) => {
    if (x - r < minX) minX = x - r;
    if (x + r > maxX) maxX = x + r;
    if (y - r < minY) minY = y - r;
    if (y + r > maxY) maxY = y + r;
    if (z - r < minZ) minZ = z - r;
    if (z + r > maxZ) maxZ = z + r;
  };

  // 关节球
  for (const [name, jw] of Object.entries(built.joints)) {
    grow(jw.pos[0], jw.pos[1], jw.pos[2], built.radii[name] ?? 0);
  }

  /**
   * 每段体块：按**它实际渲染的网格**外扩。
   *
   * ⚠️ 两个容易错的地方，都踩过：
   *   1. `box` 的 mesh 位置在 `a`、`b` 的**中点**（见 `SpecSegment`），
   *      不是在两个端点上；
   *   2. `box` 在世界轴上的半长要用 `boxHalfExtents`（含旋转）算，
   *      不能用半对角线，也不能用 `size[1]/2`。
   *
   * 圆柱/胶囊是沿父→子方向从 `a` 到 `b`，用两端半径外扩即可。
   */
  for (const item of built.segments) {
    if (item.seg.shape === 'box') {
      const mid: Vec3 = [
        (item.a[0] + item.b[0]) / 2,
        (item.a[1] + item.b[1]) / 2,
        (item.a[2] + item.b[2]) / 2,
      ];
      const size = item.seg.size ?? [0.1, 0.1, 0.1];
      const ext = boxHalfExtents(size, dirToQuaternion(item.dir));
      grow(mid[0], mid[1], mid[2], 0);
      if (mid[0] - ext[0] < minX) minX = mid[0] - ext[0];
      if (mid[0] + ext[0] > maxX) maxX = mid[0] + ext[0];
      if (mid[1] - ext[1] < minY) minY = mid[1] - ext[1];
      if (mid[1] + ext[1] > maxY) maxY = mid[1] + ext[1];
      if (mid[2] - ext[2] < minZ) minZ = mid[2] - ext[2];
      if (mid[2] + ext[2] > maxZ) maxZ = mid[2] + ext[2];
      continue;
    }
    const r = Math.max(item.seg.r0, item.seg.r1);
    grow(item.a[0], item.a[1], item.a[2], r);
    grow(item.b[0], item.b[1], item.b[2], r);
  }

  // 全空（理论上不会发生）时给一个最小盒子，避免 NaN 传进渲染
  if (!Number.isFinite(minX)) {
    minX = minY = minZ = -0.1;
    maxX = maxY = maxZ = 0.1;
  }

  return {
    min: [minX, minY, minZ],
    max: [maxX, maxY, maxZ],
    center: [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2],
    size: [maxX - minX, maxY - minY, maxZ - minZ],
  };
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
