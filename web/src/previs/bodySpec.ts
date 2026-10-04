/**
 * 白模规格（v2）：默认人体比例、归一化、以及"规格 → 骨架"的换算。
 *
 * 与后端 `app/services/white_model.py` 一一对应。**两边的关节表与默认值
 * 必须一起改**（后端负责校验，前端负责渲染；不一致会出现"后端放行、
 * 前端找不到关节"这种最难查的问题）。
 *
 * ## v2 的核心：位置不可直写
 *
 * v1 让规格直接写"关节偏移"，结果**手臂撇成八字**——横向偏移会被
 * 前向运动学**逐级累加**（肩 0.17 + 肘 0.02 + … → 手腕 0.40，而躯干 0.15）。
 *
 * v2 只接受三样东西，位置完全由它们推导：
 *
 *     rotations  每关节的旋转弧度（受硬限位钳制）
 *     lengths    每段的长度（米）
 *     radii      关节球半径
 *
 * 于是"手臂横撇"在结构上不可能出现：肩的横向位置由 `shoulderWidth` 决定，
 * 手臂外张只能通过 `sho_*` 的 Z 旋转表达，且被钳在限位内。
 *
 * ## 为什么默认值在这里、而不是让 LLM 全写出来
 *
 * LLM 只需给"偏离默认的地方"（例如"矮胖卡通角色"只改 `proportions`）：
 *   1. 输出短 → 出错概率低、更省 token；
 *   2. 即使模型漏字段，渲染出来的**仍是一具完整的人**，不会缺零件。
 */

import type { BodySegment, PosePreset, Vec3, WhiteModelSpec } from './types';

/** 关节名 → 父关节。与后端 `JOINT_PARENT` 严格一致。 */
export const BODY_JOINT_PARENT: Record<string, string | null> = {
  pelvis: null,
  spine: 'pelvis',
  chest: 'spine',
  neck: 'chest',
  head: 'neck',
  sho_l: 'chest', elb_l: 'sho_l', wri_l: 'elb_l',
  sho_r: 'chest', elb_r: 'sho_r', wri_r: 'elb_r',
  hip_l: 'pelvis', kne_l: 'hip_l', ank_l: 'kne_l', foot_l: 'ank_l',
  hip_r: 'pelvis', kne_r: 'hip_r', ank_r: 'kne_r', foot_r: 'ank_r',
};

/** 关节中文名（属性面板用） */
export const BODY_JOINT_LABELS: Record<string, string> = {
  pelvis: '骨盆', spine: '脊柱', chest: '胸', neck: '颈', head: '头',
  sho_l: '左肩', elb_l: '左肘', wri_l: '左腕',
  sho_r: '右肩', elb_r: '右肘', wri_r: '右腕',
  hip_l: '左髋', kne_l: '左膝', ank_l: '左踝', foot_l: '左脚',
  hip_r: '右髋', kne_r: '右膝', ank_r: '右踝', foot_r: '右脚',
};

/**
 * 每个关节的旋转限位：三个轴各一个 `[下限, 上限]`（弧度），
 * 顺序为 `[绕X, 绕Y, 绕Z]`。与后端 `JOINT_LIMITS` 一致。
 *
 * 这是 v2 的关键防线：即使模型（或手改 JSON 的用户）写出离谱的角度，
 * 也会被夹住，不会出现"胳膊穿进身体"或"腿反折"。
 */
export type JointLimits = [
  [number, number], // 绕 X
  [number, number], // 绕 Y
  [number, number], // 绕 Z
];

export const BODY_JOINT_LIMITS: Record<string, JointLimits> = {
  spine: [[-0.6, 0.6], [-0.6, 0.6], [-0.4, 0.4]],
  chest: [[-0.5, 0.5], [-0.6, 0.6], [-0.35, 0.35]],
  neck: [[-0.5, 0.5], [-1.0, 1.0], [-0.35, 0.35]],
  head: [[-0.4, 0.4], [-1.2, 1.2], [-0.3, 0.3]],
  sho_l: [[-2.6, 2.6], [-1.6, 1.6], [-0.2, 1.7]],
  elb_l: [[-2.6, 0.05], [-1.5, 1.5], [-0.3, 0.3]],
  wri_l: [[-1.2, 1.2], [-0.9, 0.9], [-0.6, 0.6]],
  sho_r: [[-2.6, 2.6], [-1.6, 1.6], [-1.7, 0.2]],
  elb_r: [[-2.6, 0.05], [-1.5, 1.5], [-0.3, 0.3]],
  wri_r: [[-1.2, 1.2], [-0.9, 0.9], [-0.6, 0.6]],
  hip_l: [[-1.4, 1.4], [-0.6, 0.6], [-1.5, 0.1]],
  kne_l: [[0.0, 2.4], [-0.2, 0.2], [-0.2, 0.2]],
  ank_l: [[-0.8, 0.8], [-0.4, 0.4], [-0.3, 0.3]],
  foot_l: [[-0.3, 0.3], [-0.3, 0.3], [-0.3, 0.3]],
  hip_r: [[-1.4, 1.4], [-0.6, 0.6], [-0.1, 1.5]],
  kne_r: [[0.0, 2.4], [-0.2, 0.2], [-0.2, 0.2]],
  ank_r: [[-0.8, 0.8], [-0.4, 0.4], [-0.3, 0.3]],
  foot_r: [[-0.3, 0.3], [-0.3, 0.3], [-0.3, 0.3]],
};

/**
 * 默认段长（米）：**父→子的竖直距离**。
 *
 * v2 里关节位置完全由它和旋转推导，不再有"随便写个偏移"的口子。
 * 骨盆为原点时：踝约 −0.95、髋 −0.06、胸 +0.40、头顶 +0.59，
 * 配合脚掌与头顶球半径整体约 1.73 米（随后会归一化到
 * `proportions.height`，所以这里只需比例正确）。
 */
export const BODY_DEFAULT_LENGTHS: Record<string, number> = {
  spine: 0.20,
  chest: 0.20,
  neck: 0.11,
  head: 0.08,

  elb_l: 0.25, wri_l: 0.23,
  elb_r: 0.25, wri_r: 0.23,

  kne_l: 0.43, ank_l: 0.43, foot_l: 0.09,
  kne_r: 0.43, ank_r: 0.43, foot_r: 0.09,
};

/** 默认关节球半径（米）。关节球用来填住两段肢体交接处的缝隙。 */
export const BODY_DEFAULT_RADII: Record<string, number> = {
  pelvis: 0.062, spine: 0.062, chest: 0.068, neck: 0.034, head: 0.115,
  // 肘/腕/膝/踝的球要**略小于**相接的肢体半径，否则交接处会鼓出疙瘩
  sho_l: 0.055, elb_l: 0.048, wri_l: 0.04,
  sho_r: 0.055, elb_r: 0.048, wri_r: 0.04,
  hip_l: 0.08, kne_l: 0.07, ank_l: 0.052, foot_l: 0.0,
  hip_r: 0.08, kne_r: 0.07, ank_r: 0.052, foot_r: 0.0,
};

/**
 * 默认关节旋转（弧度）—— **标准站姿，也是姿态的唯一来源**。
 *
 * 关键点：**手臂的横向位置由 `sho_*` 的 Z 旋转决定**。
 *   · Z = 0    → 手臂垂直贴着躯干；
 *   · Z = 0.05 → 约 2.9°，略略外张，这才是"自然下垂"而非"立正"；
 *   · Z 再大   → 开始变"八"字（旧版让偏移直写，所以失控到 0.16）。
 *
 * 肘用**负**的绕 X 表示屈肘（与 `humanoid.ts` 的角度约定一致：
 * 关节限位里 `elb_*` 的 X 是 [−2.6, 0.05]）。
 */
export const BODY_DEFAULT_ROTATIONS: Record<string, Vec3> = {
  spine: [0.02, 0, 0],
  chest: [0.015, 0, 0],
  neck: [-0.02, 0, 0],
  head: [0, 0, 0],
  sho_l: [0.04, 0, 0.05],
  elb_l: [-0.10, 0, 0],
  wri_l: [0, 0, 0],
  sho_r: [0.04, 0, -0.05],
  elb_r: [-0.10, 0, 0],
  wri_r: [0, 0, 0],
  hip_l: [0, 0, 0.03],
  kne_l: [0.04, 0, 0],
  ank_l: [0, 0, 0],
  foot_l: [0, 0, 0],
  hip_r: [0, 0, -0.03],
  kne_r: [0.04, 0, 0],
  ank_r: [0, 0, 0],
  foot_r: [0, 0, 0],
};

/**
 * 各关节的**横向基础偏移**（米），只在需要左右分开时用。
 *
 * 肩与髋必须左右分开，但它们的横向位置**不该由 LLM 写**——
 * 那正是 v1 出问题的地方。所以这里按"身高 × 比例系数"推导：
 *   · 肩横向 = ±height × shoulderWidth / 2
 *   · 髋横向 = ±height × 0.055（略窄于肩）
 * 其余关节横向一律为 0（由旋转决定）。
 */
/**
 * 肩/髋的**下垂量**（占身高的比例）。
 *
 * 肩不是长在胸球正中心，而是胸腔上沿稍下一点；髋在骨盆稍下。
 * 这两个值很小（约 6 厘米 / 1.70 米），但少了它们关节会挤在一起、
 * 四肢根部会互相穿插。
 */
export const BODY_ROOT_DROP = { shoulder: 0.035, hip: 0.035 };

/**
 * 髋的**横向分开量**（占身高的比例）。
 *
 * 肩的横向分开量不在这里——它由 `proportions.shoulderWidth` 决定
 * （那才是用户能拖的滑杆）。髋没有对应的滑杆，所以固定成身高的 5.5%。
 */
export const BODY_ROOT_WIDTH = { hip: 0.055 };

/** 默认体块表 —— 这是"一个完整的人"的定义，也是缺字段时的兜底。 */
export const DEFAULT_SEGMENTS: BodySegment[] = [
  // 躯干：两段胶囊（不是三个球，那样会糊成一坨菱形）
  { joint: 'spine', shape: 'capsule', r0: 0.064, r1: 0.06 },
  { joint: 'chest', shape: 'capsule', r0: 0.07, r1: 0.068 },
  { joint: 'neck', shape: 'taperedCylinder', r0: 0.036, r1: 0.036 },
  { joint: 'head', shape: 'sphere', r0: 0.115, r1: 0.115 },

  // 手臂：上臂略粗、前臂略细
  { joint: 'elb_l', shape: 'taperedCylinder', r0: 0.055, r1: 0.046 },
  { joint: 'wri_l', shape: 'taperedCylinder', r0: 0.046, r1: 0.038 },
  { joint: 'elb_r', shape: 'taperedCylinder', r0: 0.055, r1: 0.046 },
  { joint: 'wri_r', shape: 'taperedCylinder', r0: 0.046, r1: 0.038 },
  // 手掌：方块，扁而略长
  { joint: 'wri_l', shape: 'box', r0: 0, r1: 0, size: [0.075, 0.16, 0.03] },
  { joint: 'wri_r', shape: 'box', r0: 0, r1: 0, size: [0.075, 0.16, 0.03] },

  // 腿：大腿粗、小腿**明显更细**（先前 0.085→0.07 对比太小，像柱子）
  { joint: 'kne_l', shape: 'taperedCylinder', r0: 0.088, r1: 0.062 },
  { joint: 'ank_l', shape: 'taperedCylinder', r0: 0.062, r1: 0.048 },
  { joint: 'kne_r', shape: 'taperedCylinder', r0: 0.088, r1: 0.062 },
  { joint: 'ank_r', shape: 'taperedCylinder', r0: 0.062, r1: 0.048 },
  // 脚掌：方块，前后方向长
  { joint: 'foot_l', shape: 'box', r0: 0, r1: 0, size: [0.09, 0.05, 0.20] },
  { joint: 'foot_r', shape: 'box', r0: 0, r1: 0, size: [0.09, 0.05, 0.20] },
];

export const DEFAULT_PROPORTIONS = {
  height: 1.70,
  headRatio: 7.5,
  shoulderWidth: 0.22,
  limbRatio: 1.0,
  headScale: 1.0,
  chubbiness: 1.0,
  legRatio: 1.0,
};

/** 把任意值夹到区间内 */
function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

function num(v: unknown, dflt: number, lo: number, hi: number): number {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? clamp(n, lo, hi) : dflt;
}

/** 读一个旋转弧度并按该关节该轴的限位钳制 */
function angle(v: unknown, joint: string, axis: 0 | 1 | 2, dflt: number): number {
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n)) return dflt;
  const [lo, hi] = BODY_JOINT_LIMITS[joint]?.[axis] ?? [-3.2, 3.2];
  return clamp(n, lo, hi);
}

/**
 * 归一化一份（可能来自 LLM 或手改的）规格，保证**一定渲染得出来**。
 *
 * 与后端 `normalize_spec` 同样的原则：能降级就降级（漏字段补默认、
 * 非法体块跳过），不能降级的才报错。后端已经校验过一次，这里是
 * **前端自己也要能站住**——工程文件是用户可编辑的（C 模式能直接改 JSON），
 * 不能假定它一定合法。
 *
 * ⚠️ 旧字段 `joints`（v1 的任意偏移）**被忽略**。忽略而不是报错，
 * 是为了能平滑读入旧工程文件。
 */
export function normalizeBodySpec(raw: unknown): WhiteModelSpec {
  const r = (raw ?? {}) as Record<string, unknown>;
  const rawProp = (r.proportions ?? {}) as Record<string, unknown>;

  const proportions = {
    height: num(rawProp.height, DEFAULT_PROPORTIONS.height, 0.2, 4),
    headRatio: num(rawProp.headRatio, DEFAULT_PROPORTIONS.headRatio, 4, 12),
    /**
   * ⚠️ `shoulderWidth` 的区间是**肩距 ÷ 身高**，不是肩的绝对宽。
   *
   * 这里踩过一个代价很大的坑：先前区间写成 `(0.4, 1.6)`，而默认值是 `0.22`
   * ——**默认值低于下限**，于是每次归一化都把它夹到 0.4，肩宽直接翻倍，
   * 手臂横撇到 ±0.34（应该 ±0.19）。表现就是"两条胳膊撇成八字"，
   * 而我在渲染、旋转、FK 上查了半天，真因只是区间写错。
   *
   * 人体参考：肩距约是身高的 0.22~0.25 倍。所以区间取 `0.14~0.40`
   * （从"窄肩"到"极宽肩"），默认 0.22 落在中间。
   *
   * 教训：**给区间之前先确认默认值在区间内**。这类错误单测很容易漏，
   * 因为归一化"成功"了，只是结果不对。
   */
  shoulderWidth: num(rawProp.shoulderWidth, DEFAULT_PROPORTIONS.shoulderWidth, 0.14, 0.4),
    limbRatio: num(rawProp.limbRatio, DEFAULT_PROPORTIONS.limbRatio, 0.5, 1.8),
    headScale: num(rawProp.headScale, DEFAULT_PROPORTIONS.headScale, 0.5, 2),
    chubbiness: num(rawProp.chubbiness, DEFAULT_PROPORTIONS.chubbiness, 0.5, 2),
    legRatio: num(rawProp.legRatio, DEFAULT_PROPORTIONS.legRatio, 0.5, 1.5),
  };

  // ---- 旋转（唯一的姿态来源）----
  const rotations: Record<string, Vec3> = {};
  const rawRot = (r.rotations ?? {}) as Record<string, unknown>;
  for (const k of Object.keys(rawRot)) {
    if (!(k in BODY_JOINT_PARENT)) continue;
    const d = BODY_DEFAULT_ROTATIONS[k] ?? ([0, 0, 0] as Vec3);
    const v = rawRot[k];
    const arr = Array.isArray(v) && v.length === 3 ? v : [undefined, undefined, undefined];
    rotations[k] = [
      angle(arr[0], k, 0, d[0]),
      angle(arr[1], k, 1, d[1]),
      angle(arr[2], k, 2, d[2]),
    ];
  }

  // ---- 段长 ----
  const lengths: Record<string, number> = {};
  const rawLen = (r.lengths ?? {}) as Record<string, unknown>;
  for (const k of Object.keys(rawLen)) {
    if (!(k in BODY_JOINT_PARENT) || BODY_JOINT_PARENT[k] === null) continue;
    lengths[k] = num(rawLen[k], BODY_DEFAULT_LENGTHS[k] ?? 0.2, 0.01, 2);
  }

  // ---- 关节球半径 ----
  const radii: Record<string, number> = {};
  const rawRadii = ((r.radii ?? r.jointRadii) ?? {}) as Record<string, unknown>;
  for (const k of Object.keys(rawRadii)) {
    if (!(k in BODY_JOINT_PARENT)) continue;
    radii[k] = num(rawRadii[k], BODY_DEFAULT_RADII[k] ?? 0.06, 0, 0.6);
  }

  // ---- 体块 ----
  const segments: BodySegment[] = [];
  const rawSegs = r.segments;
  if (Array.isArray(rawSegs)) {
    for (const s of rawSegs.slice(0, 80)) {
      if (!s || typeof s !== 'object') continue;
      const seg = s as Record<string, unknown>;
      const joint = String(seg.joint ?? '');
      // 未知关节 / 根关节 → 跳过这一段（少一段好过整页白屏）
      if (!(joint in BODY_JOINT_PARENT) || BODY_JOINT_PARENT[joint] === null) continue;
      const shape = String(seg.shape ?? 'capsule');
      const isBox = shape === 'box';
      const item: BodySegment = {
        joint,
        shape: (['capsule', 'taperedCylinder', 'box', 'sphere'] as const).includes(
          shape as 'capsule',
        )
          ? (shape as BodySegment['shape'])
          : 'capsule',
        /**
         * ⚠️ `box` 的半径**必须允许为 0**。
         *
         * `box`（手掌、脚掌）用 `size` 描述尺寸，`r0/r1` 按约定是 0。
         * 若给它们套上 `0.005` 的下限，下游"取最细相邻肢体算关节球半径"
         * 就会拿到 0.005，把该关节的球算成 `0.005 × 1.05 ≈ 0.006`
         * ——球被缩没，**手腕与脚踝看起来是断的**（实测踩过）。
         */
        r0: isBox ? 0 : num(seg.r0, 0.06, 0.005, 0.6),
        r1: isBox ? 0 : num(seg.r1, 0.05, 0.005, 0.6),
      };
      if (Array.isArray(seg.size) && seg.size.length === 3) {
        item.size = [
          num(seg.size[0], 0.1, 0.005, 1.5),
          num(seg.size[1], 0.1, 0.005, 1.5),
          num(seg.size[2], 0.1, 0.005, 1.5),
        ];
      }
      if (typeof seg.color === 'string' && /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(seg.color)) {
        item.color = seg.color;
      }
      segments.push(item);
    }
  }
  // 一个合法体块都没有 → 用默认完整人形兜底
  if (segments.length === 0) segments.push(...DEFAULT_SEGMENTS.map((s) => ({ ...s })));

  const poseRaw = String(r.pose ?? 'idle') as PosePreset;
  const POSES: PosePreset[] = ['idle', 'walk', 'run', 'sit', 'wave', 'point'];
  const pose = POSES.includes(poseRaw) ? poseRaw : 'idle';

  return {
    version: 2,
    name: String(r.name ?? '').trim().slice(0, 40) || '白模',
    ...(typeof r.description === 'string' && r.description.trim()
      ? { description: r.description.trim().slice(0, 200) }
      : {}),
    proportions,
    rotations,
    lengths,
    radii,
    segments,
    pose,
  };
}

/** 取一个关节最终的旋转：规格给了就用（已钳制），否则用默认 */
export function resolvedRotation(spec: WhiteModelSpec, joint: string): Vec3 {
  const over = spec.rotations[joint];
  const d = BODY_DEFAULT_ROTATIONS[joint] ?? ([0, 0, 0] as Vec3);
  const src: Vec3 = over ? over : d;
  return [
    angle(src[0], joint, 0, d[0]),
    angle(src[1], joint, 1, d[1]),
    angle(src[2], joint, 2, d[2]),
  ];
}

/**
 * 取一个关节的**偏移向量**。
 *
 * 这是 v2 的核心推算，也是"位置不可直写"的落实。
 *
 * ## 通用情形：纯粹的"沿骨向下（或向上）段长"
 *
 * 除肩/髋外，所有关节的偏移就是 `(0, ±len, 0)`——由调用方用父旋转转过去。
 *
 * ## 肩/髋：横向分开量 + **小幅**下垂
 *
 * 这两个关节需要左右分开，但**横向分开量不是"骨长"的一部分**。
 * 我在这里错过两次，都记下来：
 *
 *   错误写法一：`[lateral, -len, 0]`
 *     → 调用方还会 `mv(parentRot, 该向量)`，整向量一起转，
 *       横向分量被当成"沿某轴的长度"转走，肩跑到世界 0.34（应为 0.19）。
 *
 *   错误写法二：把它当成一根斜骨，长度 `√(len² + lateral²)`
 *     → 长度守恒了，但**竖直分量被算成 `-len`**（0.2 米！），
 *       于是肩从胸掉到髋下面（实测 y=0.82，胸在 1.41）。
 *
 * 正确：横向取 `lateral`，**竖直只取一小段 `shoulderDrop`**（约 0.06 米）。
 * 肩本来就长在胸腔上沿稍下一点的位置，不该再"往下走一整根骨头"。
 */
export function resolvedOffset(spec: WhiteModelSpec, joint: string): Vec3 {
  const parent = BODY_JOINT_PARENT[joint];
  if (parent === null) return [0, 0, 0];

  const p = spec.proportions;
  const base = DEFAULT_PROPORTIONS.height;
  const scaleAll = p.height / base;

  const lenRaw = spec.lengths[joint] ?? BODY_DEFAULT_LENGTHS[joint] ?? 0.2;
  // 腿长系数只作用于膝以下；头大小系数只作用于颈/头
  let len = lenRaw * scaleAll;
  if (joint.startsWith('kne_') || joint.startsWith('ank_') || joint.startsWith('foot_')) {
    len *= p.legRatio;
  }
  if (joint === 'neck' || joint === 'head') len *= p.headScale;

  /** 躯干链朝上，其余朝下 */
  const up = joint === 'spine' || joint === 'chest' || joint === 'neck' || joint === 'head';
  const vy = up ? len : -len;

  // 肩/髋：横向分开 + 一点点下垂（不是一整根骨头）
  if (joint.startsWith('sho_')) {
    const lateral = (p.height * p.shoulderWidth) / 2;
    const drop = p.height * BODY_ROOT_DROP.shoulder;
    return [joint.endsWith('_l') ? lateral : -lateral, -drop, 0];
  }
  if (joint.startsWith('hip_')) {
    const lateral = p.height * BODY_ROOT_WIDTH.hip;
    const drop = p.height * BODY_ROOT_DROP.hip;
    return [joint.endsWith('_l') ? lateral : -lateral, -drop, 0];
  }
  return [0, vy, 0];
}

/** 把比例系数折算成各关节球的最终半径 */
export function resolvedRadii(spec: WhiteModelSpec): Record<string, number> {
  const p = spec.proportions;
  const scaleAll = p.height / DEFAULT_PROPORTIONS.height;
  const out: Record<string, number> = {};
  for (const name of Object.keys(BODY_DEFAULT_RADII)) {
    const over = spec.radii[name];
    const r = over !== undefined ? over : BODY_DEFAULT_RADII[name];
    let v = r * scaleAll;
    if (name === 'head') v *= p.headScale;
    if (name === 'chest' || name === 'spine' || name === 'pelvis') v *= p.chubbiness;
    out[name] = v;
  }
  return out;
}

/** 把比例系数折算成体块的最终半径 / 尺寸 */
export function resolvedSegment(spec: WhiteModelSpec, seg: BodySegment): BodySegment {
  const p = spec.proportions;
  const scaleAll = p.height / DEFAULT_PROPORTIONS.height;
  const r0 = seg.r0 * scaleAll * p.limbRatio;
  const r1 = seg.r1 * scaleAll * p.limbRatio;
  const out: BodySegment = { ...seg, r0, r1 };
  if (seg.size) {
    out.size = [seg.size[0] * scaleAll, seg.size[1] * scaleAll, seg.size[2] * scaleAll];
  }
  return out;
}
