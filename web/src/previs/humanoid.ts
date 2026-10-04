/**
 * 人形骨架与姿态。
 *
 * 为什么白模需要"肢体动作"而不只是一个柱子：
 *   视频模型最难自己编的就是**动作**——手往哪伸、腿怎么迈、转身还是停顿。
 *   一个圆柱只能表达"人在那"，表达不了"人在干什么"。
 *
 * 运动学沿用 `D:\AI\local-gen\make_pose_seq.py` 已验证的做法：
 *   每根骨骼用「相对父关节的静止偏移向量」描述，偏移表达在**父关节坐标系**里；
 *   关节旋转作用于该偏移，于是肢体绕父关节摆动——这才符合真实关节的运动方式。
 *   （该脚本的注释记录过：把骨骼长度一律当成 +Y 会出现"炸成海星"。）
 *
 * 角度约定（弧度，与 Three.js 右手系一致）：
 *   x = 前后摆动（腿向前迈 = 正）、y = 扭转、z = 左右张开
 */

export type Vec3 = [number, number, number];

export interface JointDef {
  /** 父关节名（None = 根） */
  parent: string | null;
  /** 相对父关节的静止偏移（在父关节坐标系内） */
  offset: Vec3;
  /** 描画粗细（米）。白模靠**体量**才能穿透 VAE 下采样，细线会被抹掉。 */
  radius: number;
  /** 归类（用于上色与"是否属于躯干"判断） */
  part: 'torso' | 'head' | 'arm_l' | 'arm_r' | 'leg_l' | 'leg_r';
}

/**
 * 骨架定义。顺序保证**父先于子**（前向运动学按此顺序一次遍历即可）。
 *
 * 总高约 1.7 米：骨盆到头顶 0.9 + 腿 0.8。
 */
export const HUMAN_SKELETON: Record<string, JointDef> = {
  pelvis: { parent: null, offset: [0, 0, 0], radius: 0.17, part: 'torso' },
  spine: { parent: 'pelvis', offset: [0, 0.22, 0], radius: 0.16, part: 'torso' },
  chest: { parent: 'spine', offset: [0, 0.24, 0], radius: 0.19, part: 'torso' },
  // 颈短一点：此前颈→头 0.16 加上头球，看起来像被拉长的雪人
  neck: { parent: 'chest', offset: [0, 0.13, 0], radius: 0.085, part: 'torso' },
  head: { parent: 'neck', offset: [0, 0.11, 0], radius: 0.135, part: 'head' },

  // 肩稍宽、稍低，手臂才不会像平举的稻草人
  sho_l: { parent: 'chest', offset: [0.17, 0.06, 0], radius: 0.085, part: 'arm_l' },
  elb_l: { parent: 'sho_l', offset: [0.02, -0.27, 0], radius: 0.075, part: 'arm_l' },
  wri_l: { parent: 'elb_l', offset: [0, -0.25, 0], radius: 0.065, part: 'arm_l' },

  sho_r: { parent: 'chest', offset: [-0.17, 0.06, 0], radius: 0.085, part: 'arm_r' },
  elb_r: { parent: 'sho_r', offset: [-0.02, -0.27, 0], radius: 0.075, part: 'arm_r' },
  wri_r: { parent: 'elb_r', offset: [0, -0.25, 0], radius: 0.065, part: 'arm_r' },

  // 腿加粗：细腿在下采样后容易断
  hip_l: { parent: 'pelvis', offset: [0.10, -0.06, 0], radius: 0.105, part: 'leg_l' },
  kne_l: { parent: 'hip_l', offset: [0, -0.42, 0], radius: 0.09, part: 'leg_l' },
  ank_l: { parent: 'kne_l', offset: [0, -0.40, 0], radius: 0.075, part: 'leg_l' },

  hip_r: { parent: 'pelvis', offset: [-0.10, -0.06, 0], radius: 0.105, part: 'leg_r' },
  kne_r: { parent: 'hip_r', offset: [0, -0.42, 0], radius: 0.09, part: 'leg_r' },
  ank_r: { parent: 'kne_r', offset: [0, -0.40, 0], radius: 0.075, part: 'leg_r' },
};

/** (子关节, 父关节) 列表，用于画骨 */
export const HUMAN_BONES: Array<[string, string]> = Object.entries(HUMAN_SKELETON)
  .filter(([, d]) => d.parent)
  .map(([k, d]) => [k, d.parent as string]);

/** 每个关节的局部旋转（弧度） */
export type Pose = Record<string, Vec3>;

/** 标准站立。
 *
 * 不是全零：肩略内收、肘微屈、腿微开——全零会渲染成"平举双臂的稻草人"
 * （实测过）。这几度差异决定它看起来像不像人。
 */
export const POSE_IDLE: Pose = {
  sho_l: [0.08, 0, 0.16],
  elb_l: [0.22, 0, 0],
  sho_r: [0.08, 0, -0.16],
  elb_r: [0.22, 0, 0],
  hip_l: [0, 0, 0.045],
  hip_r: [0, 0, -0.045],
  kne_l: [0.05, 0, 0],
  kne_r: [0.05, 0, 0],
};

/** 走路循环：相位 t∈[0,1) */
export function poseWalk(t: number): Pose {
  const ph = t * 2 * Math.PI;
  const swing = 0.42 * Math.sin(ph);
  const swing2 = 0.42 * Math.sin(ph + Math.PI);
  const knee = (p: number) => Math.max(0, -0.9 * Math.sin(p + 0.7));
  return {
    pelvis: [0, 0, 0.06 * Math.sin(ph)],
    spine: [0.05 + 0.03 * Math.cos(2 * ph), 0, 0],
    chest: [0.04, 0, -0.08 * Math.sin(ph)],
    neck: [-0.04, 0, 0],
    sho_l: [-swing2 * 0.75, 0, 0],
    elb_l: [0.28 + 0.22 * Math.max(0, Math.sin(ph + Math.PI)), 0, 0],
    sho_r: [-swing * 0.75, 0, 0],
    elb_r: [0.28 + 0.22 * Math.max(0, Math.sin(ph)), 0, 0],
    hip_l: [swing, 0, 0],
    kne_l: [knee(ph), 0, 0],
    ank_l: [-0.15, 0, 0],
    hip_r: [swing2, 0, 0],
    kne_r: [knee(ph + Math.PI), 0, 0],
    ank_r: [-0.15, 0, 0],
  };
}

/** 跑步：摆幅更大、前倾更多 */
export function poseRun(t: number): Pose {
  const ph = t * 2 * Math.PI;
  const swing = 0.85 * Math.sin(ph);
  const swing2 = 0.85 * Math.sin(ph + Math.PI);
  const knee = (p: number) => Math.max(0.1, -1.5 * Math.sin(p + 0.5));
  return {
    pelvis: [0, 0, 0.09 * Math.sin(ph)],
    spine: [0.16 + 0.05 * Math.cos(2 * ph), 0, 0],
    chest: [0.10, 0, -0.16 * Math.sin(ph)],
    neck: [-0.14, 0, 0],
    sho_l: [-swing2 * 1.05, 0, 0.1],
    elb_l: [1.35, 0, 0],
    sho_r: [-swing * 1.05, 0, -0.1],
    elb_r: [1.35, 0, 0],
    hip_l: [swing, 0, 0],
    kne_l: [knee(ph), 0, 0],
    ank_l: [-0.25, 0, 0],
    hip_r: [swing2, 0, 0],
    kne_r: [knee(ph + Math.PI), 0, 0],
    ank_r: [-0.25, 0, 0],
  };
}

/** 坐着 */
export const POSE_SIT: Pose = {
  pelvis: [0, 0, 0],
  spine: [0.06, 0, 0],
  hip_l: [-1.45, 0, 0.06],
  kne_l: [1.45, 0, 0],
  ank_l: [0.1, 0, 0],
  hip_r: [-1.45, 0, -0.06],
  kne_r: [1.45, 0, 0],
  ank_r: [0.1, 0, 0],
  sho_l: [0.25, 0, 0.12],
  elb_l: [0.6, 0, 0],
  sho_r: [0.25, 0, -0.12],
  elb_r: [0.6, 0, 0],
};

/** 单手举起（打招呼 / 示意） */
export const POSE_WAVE: Pose = {
  sho_r: [-2.5, 0, -0.5],
  elb_r: [0.5, 0, 0],
  sho_l: [0.1, 0, 0.1],
  elb_l: [0.25, 0, 0],
};

/** 指向（伸手） */
export const POSE_POINT: Pose = {
  sho_r: [-1.45, 0, -0.25],
  elb_r: [0.1, 0, 0],
  chest: [0, -0.15, 0],
  spine: [0, -0.1, 0],
};

export type PosePreset = 'idle' | 'walk' | 'run' | 'sit' | 'wave' | 'point';

export const POSE_PRESETS: Array<{ key: PosePreset; label: string }> = [
  { key: 'idle', label: '站立' },
  { key: 'walk', label: '行走（循环）' },
  { key: 'run', label: '奔跑（循环）' },
  { key: 'sit', label: '坐下' },
  { key: 'wave', label: '举手' },
  { key: 'point', label: '指向' },
];

/**
 * 取某时刻的姿态。
 *
 * 行走/奔跑按**时间**推进（同一镜头里所有人同步），
 * 其余是静态姿态。这样"走路"在导出序列里会自然循环，
 * 而不需要用户逐帧摆。
 */
export function samplePose(
  preset: PosePreset,
  time: number,
  speed = 1,
  phaseOffset = 0,
  override?: Record<string, Vec3>,
): Pose {
  const base = (() => {
    switch (preset) {
      case 'walk':
        return poseWalk((((time * speed * 0.9 + phaseOffset) % 1) + 1) % 1);
      case 'run':
        return poseRun((((time * speed * 1.6 + phaseOffset) % 1) + 1) % 1);
      case 'sit':
        return POSE_SIT;
      case 'wave':
        return POSE_WAVE;
      case 'point':
        return POSE_POINT;
      default:
        return POSE_IDLE;
    }
  })();
  return applyPoseOverride(base, override);
}

/** 关节分组：供属性面板按部位折叠，避免 17 个关节一次铺满 */
export const JOINT_GROUPS: Array<{ label: string; joints: string[] }> = [
  { label: '躯干 / 头', joints: ['pelvis', 'spine', 'chest', 'neck', 'head'] },
  { label: '左臂', joints: ['sho_l', 'elb_l', 'wri_l'] },
  { label: '右臂', joints: ['sho_r', 'elb_r', 'wri_r'] },
  { label: '左腿', joints: ['hip_l', 'kne_l', 'ank_l'] },
  { label: '右腿', joints: ['hip_r', 'kne_r', 'ank_r'] },
];

/** 关节中文名 */
export const JOINT_LABELS: Record<string, string> = {
  pelvis: '骨盆', spine: '脊柱', chest: '胸', neck: '颈', head: '头',
  sho_l: '左肩', elb_l: '左肘', wri_l: '左腕',
  sho_r: '右肩', elb_r: '右肘', wri_r: '右腕',
  hip_l: '左髋', kne_l: '左膝', ank_l: '左踝',
  hip_r: '右髋', kne_r: '右膝', ank_r: '右踝',
};

/** 旋转轴的中文说明（右手系，与 Three.js 一致） */
export const AXIS_LABELS = ['前后摆动', '扭转', '左右张开'];

/**
 * 把叠加偏移合并进基础姿态。
 *
 * 逐关节相加即可：这些角度是**局部旋转**，彼此独立，
 * 不存在"必须先转谁"的顺序问题（顺序由 FK 的父子链固定）。
 */
export function applyPoseOverride(
  base: Pose,
  override?: Record<string, Vec3>,
): Pose {
  if (!override) return base;
  const out: Pose = { ...base };
  for (const k of Object.keys(override)) {
    const o = override[k];
    const b = base[k] ?? [0, 0, 0];
    if (o[0] === 0 && o[1] === 0 && o[2] === 0) {
      out[k] = b;
    } else {
      out[k] = [b[0] + o[0], b[1] + o[1], b[2] + o[2]];
    }
  }
  return out;
}

/* ---------------- 前向运动学 ---------------- */

/**
 * 矩阵工具**导出**出去了：白模规格渲染器（`bodySpecMesh.ts`）要用同一套。
 *
 * 为什么不让它自己写一份：旋转合成顺序（`rotZ @ rotY @ rotX`）是有约定的，
 * 两套实现迟早会不一致，而那种"看起来差不多、其实镜像了"的偏差极难查。
 */
export function rotX(a: number): number[] {
  const c = Math.cos(a), s = Math.sin(a);
  return [1, 0, 0, 0, c, -s, 0, s, c];
}
export function rotY(a: number): number[] {
  const c = Math.cos(a), s = Math.sin(a);
  return [c, 0, s, 0, 1, 0, -s, 0, c];
}
export function rotZ(a: number): number[] {
  const c = Math.cos(a), s = Math.sin(a);
  return [c, -s, 0, s, c, 0, 0, 0, 1];
}
/** 3x3 行主序矩阵乘 */
export function mm(a: number[], b: number[]): number[] {
  const o = new Array(9).fill(0);
  for (let r = 0; r < 3; r += 1) {
    for (let c = 0; c < 3; c += 1) {
      o[r * 3 + c] = a[r * 3] * b[c] + a[r * 3 + 1] * b[3 + c] + a[r * 3 + 2] * b[6 + c];
    }
  }
  return o;
}
export function mv(m: number[], v: Vec3): Vec3 {
  return [
    m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
    m[3] * v[0] + m[4] * v[1] + m[5] * v[2],
    m[6] * v[0] + m[7] * v[1] + m[8] * v[2],
  ];
}

export interface JointWorld {
  pos: Vec3;
  rot: number[];
}

/**
 * 前向运动学：返回每个关节的世界位置与累积旋转。
 *
 * R_world[child] = R_world[parent] @ R_local[child]
 * pos[child]     = pos[parent] + R_world[child] @ offset_rest[child]
 *
 * 偏移随**子关节自身**的旋转一起转，肢体才会绕父关节摆动。
 */
export function forwardKinematics(pose: Pose): Record<string, JointWorld> {
  const out: Record<string, JointWorld> = {};
  for (const name of Object.keys(HUMAN_SKELETON)) {
    const def = HUMAN_SKELETON[name];
    const [rx, ry, rz] = pose[name] ?? [0, 0, 0];
    const local = mm(rotZ(rz), mm(rotY(ry), rotX(rx)));
    if (def.parent === null) {
      out[name] = { pos: [0, 0, 0], rot: local };
    } else {
      const p = out[def.parent];
      const rot = mm(p.rot, local);
      const off = mv(rot, def.offset);
      out[name] = {
        pos: [p.pos[0] + off[0], p.pos[1] + off[1], p.pos[2] + off[2]],
        rot,
      };
    }
  }
  return out;
}

/**
 * 骨盆相对脚底的静止高度（米）。
 *
 * ⚠️ 骨架以**骨盆为原点**建链，所以静止姿态下脚在 y = -0.88。
 * 若直接把骨架组放在 y=0，角色的腿会**全部埋到地面以下**——
 * 实测表现为"只剩上半身的一团"，很容易误判成姿态算错。
 * 渲染时必须把整组上移这个高度，让脚落在地面上。
 */
export const PELVIS_HEIGHT: number = (() => {
  const fk = forwardKinematics(POSE_IDLE);
  // 取两只脚里较低的那只作为地面接触点
  return Math.max(fk.ank_l.pos[1], fk.ank_r.pos[1]) * -1;
})();

/** 骨架各关节在世界坐标下的位置（脚底对齐 y=0） */
export function skeletonPositions(pose: Pose): Record<string, Vec3> {
  const fk = forwardKinematics(pose);
  const out: Record<string, Vec3> = {};
  for (const k of Object.keys(fk)) out[k] = fk[k].pos;
  return out;
}

/** 骨架各关节位置，**已把脚底对齐到 y=0**（渲染直接可用） */
export function skeletonPositionsGrounded(pose: Pose): Record<string, Vec3> {
  const raw = skeletonPositions(pose);
  const out: Record<string, Vec3> = {};
  for (const k of Object.keys(raw)) {
    out[k] = [raw[k][0], raw[k][1] + PELVIS_HEIGHT, raw[k][2]];
  }
  return out;
}

/** 根据骨架算出整体高度（用于体量显示） */
export function skeletonHeight(): number {
  const grounded = skeletonPositionsGrounded(POSE_IDLE);
  let maxY = 0;
  for (const k of Object.keys(grounded)) {
    maxY = Math.max(maxY, grounded[k][1] + HUMAN_SKELETON[k].radius);
  }
  return maxY;
}
