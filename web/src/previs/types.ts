/**
 * 3D 预演（白模）的数据模型。
 *
 * 定位：**只表达"谁在哪、相机怎么走"**，不追求画面好看。
 * 渲染时全部无材质、无光照着色——按实测（docs/recon/img/whitebox-compare.png），
 * 实心剪影下采样后的信号峰值是 253/255，而带材质/光照的彩色画面会让视频模型
 * 学走那些它不该学的东西。
 *
 * 世界坐标约定（右手系，与 Three.js 一致）：
 *   +X 向右，+Y 向上，+Z 朝向观众（即相机默认在 +Z 看向 -Z）
 *   1 个世界单位 ≈ 1 米
 */

export type Vec3 = [number, number, number];

/** 姿势预设（实现见 humanoid.ts） */
export type PosePreset = 'idle' | 'walk' | 'run' | 'sit' | 'wave' | 'point';

/** 人物块：**人形骨架**，可摆姿态。
 *
 * 为什么不再用圆柱：圆柱只能表达"人在那"，表达不了"人在干什么"。
 * 视频模型最难自己编的正是**动作**（手往哪伸、腿怎么迈、转身还是停顿），
 * 所以白模必须能摆出肢体动作。
 */
export interface Actor {
  id: string;
  name: string;
  /** 脚底位置（y 通常为 0，站在地面上） */
  position: Vec3;
  /** 朝向（绕 Y 轴，弧度；0 = 面向 -Z，即面向默认相机） */
  yaw: number;
  /** 人体缩放（1.0 = 身高约 1.7 米） */
  scale: number;
  /** 姿势预设 */
  pose: PosePreset;
  /** 动作速度倍率（仅对循环姿态 walk/run 有意义） */
  poseSpeed: number;
  /** 相位偏移 0~1：让多个人不要完全同步走路 */
  posePhase: number;
  /**
   * 逐关节手动调整（叠加在预设之上）。
   *
   * 为什么是**叠加偏移**而不是直接存绝对角度：
   * 若存绝对值，用户一调角度，"行走"就会立刻定格成静态姿势——
   * 循环动画与手动微调不能共存。叠加则两者兼得：
   * 预设驱动动作，偏移负责个性化（驼背、抬手、外八……）。
   * 键为关节名，值为 [dx, dy, dz] 弧度增量。
   */
  poseOverride?: Record<string, Vec3>;
  /** 显示颜色（**仅编辑器内用于区分**，导出时一律渲染成纯白/灰度） */
  color: string;
}

/** 道具块（桌子、门、树……白模阶段只要体量） */
export interface Prop {
  id: string;
  name: string;
  position: Vec3;
  /** 绕 Y 轴旋转 */
  yaw: number;
  size: Vec3;
  color: string;
  /**
   * 是否为"环境块"（墙 / 建筑 / 围挡）。
   *
   * 只在 UI 上区分：环境块用眼睛图标、归到"环境"分组，
   * 避免和"道具"混在一起显得杂乱。渲染与导出完全一视同仁——
   * 白模里它们就是体量，模型不需要知道哪个是"墙"哪个是"桌子"。
   */
  env?: boolean;
}

/**
 * 相机看向哪里。
 *
 * `actor` = **锁定跟随**：始终看向某个角色的当前位置。
 * 关键在于取「角色位置 + 视线高度」，而不是角色原点（脚底）——
 * 否则机位一靠近，看向点就落到脚下、画面变成仰拍地板。
 */
export type CameraAim =
  | { kind: 'fixed'; target: Vec3 }
  | {
      kind: 'actor';
      actorId: string;
      /** 视线高度（相对角色脚底，米）。1.1 ≈ 胸口/面部 */
      height: number;
    };

export interface CameraKeyframe {
  id: string;
  /** 秒（相对片段起点） */
  time: number;
  /** 机位 */
  position: Vec3;
  /**
   * 看向点（固定目标）。
   *
   * 当 `aim` 为 actor 时，此字段被忽略但仍保留——切回 fixed 时能回到原值，
   * 不必让用户重新摆一次。
   */
  target: Vec3;
  /** 垂直视场角（度） */
  fov: number;
  /** 看向方式；缺省等价于 `{kind:'fixed', target}` */
  aim?: CameraAim;
}

/**
 * 一条相机轨道（一个"机位"）。
 *
 * 多机位切换的实现方式：**一个片段里放多条轨道，按时间区间分派**。
 * 这正是实测里《盗火线》正反打的做法——
 * "三个机位，时间轴上设好四段切换：A 四秒、B 三秒、C 四秒、再切回 B 四秒"。
 */
export interface CameraTrack {
  id: string;
  name: string;
  /** 生效区间（秒，含头不含尾）；两条都为空则与其他轨道均分片段时长 */
  start?: number;
  end?: number;
  /** 该机位自己的关键帧（相对**片段**时间，不是轨道内时间） */
  keys: CameraKeyframe[];
}

/** 一条片段（一个镜头） */
export interface Shot {
  id: string;
  name: string;
  /** 时长（秒）。本地 H3 安全上限 10 秒，云端可到 15。 */
  duration: number;
  fps: number;
  /**
   * 相机轨道（多机位）。**至少一条**。
   *
   * 早期版本只有一个 `cameraKeys` 数组，无法表达"切机位"；
   * 而实测表明正反打、多机位调度是白模最常被用到的能力之一。
   */
  cameraTracks: CameraTrack[];
  /** 旧格式（单机位）——读取时会被迁移进 cameraTracks */
  cameraKeys?: CameraKeyframe[];
}

export interface PrevisScene {
  /** 地面尺寸 [宽, 深]（世界单位） */
  ground: [number, number];
  actors: Actor[];
  props: Prop[];
  shots: Shot[];
}

/**
 * 3D 视口的语义色。
 *
 * ⚠️ 这里原先散落着旧品牌的两个强调色（荧光绿 `#c8f04a`、紫蓝 `#8b8bf0`），
 * 而界面已改成**中性深色 + 唯一强调色（青）**。不统一的话，
 * 3D 视口里的荧光绿会和界面的青色打架，整屏像两套皮肤拼在一起。
 *
 * 分配原则（与设计系统一致：**界面只有明度层级，彩色只来自内容本身**）：
 *   · `MODEL`  角色/白模 —— **近白**。它就叫"白模"，白是它的定义；
 *                近白在近黑底上对比最强，轮廓最清楚（白模的用途就是读轮廓）。
 *   · `PROP`   道具 —— 冷灰。道具是配角，不该抢镜。
 *   · `ACCENT` 选中框 / 线框 / 机位轨道 —— **光晕青**，与界面的交互色一致
 *                （这些是"界面反馈"，不是"内容"，所以可以用强调色）。
 *   · `GROUND` 地面 —— 中性近黑，去掉原来的紫调。
 */
export const PREVIS_COLOR = {
  MODEL: '#dcdce2',
  PROP: '#7c8698',
  ACCENT: '#22d3ee',
  GROUND: '#15151a',
  /**
   * 新增角色时轮换用的一组**近白系**颜色。
   *
   * 为什么还要"轮换"：视口里可能有多个角色，全同色就分不清谁是谁。
   * 为什么必须是近白系而不是以前的荧光绿/橙/红：
   *   · 它叫"白模"，白是它的定义；
   *   · **导出白模参考视频用的是真实渲染帧**（不是强制白色），
   *     所以编辑器的颜色会直接进到喂给视频模型的那段素材里。
   */
  ACTOR_CYCLE: ['#dcdce2', '#c3c3cb', '#eeeef2', '#b0b0ba'],
} as const;

export const DEFAULT_SCENE: PrevisScene = {
  ground: [12, 12],
  actors: [
    {
      id: 'actor_1',
      name: '角色 A',
      position: [0, 0, 0],
      yaw: 0,
      scale: 1,
      pose: 'idle',
      poseSpeed: 1,
      posePhase: 0,
      color: PREVIS_COLOR.MODEL,
    },
  ],
  props: [
    {
      id: 'prop_1',
      name: '桌子',
      position: [1.8, 0, -0.6],
      yaw: 0,
      size: [1.2, 0.75, 0.7],
      color: PREVIS_COLOR.PROP,
    },
  ],
  shots: [
    {
      id: 'shot_1',
      name: '镜头 1',
      duration: 7,
      fps: 24,
      cameraTracks: [
        {
          id: 'track_1',
          name: '机位 1',
          keys: [
            { id: 'k0', time: 0, position: [0, 1.6, 4.2], target: [0, 1.0, 0], fov: 40 },
            { id: 'k1', time: 7, position: [1.4, 1.5, 3.0], target: [0.6, 0.9, -0.4], fov: 40 },
          ],
        },
      ],
    },
  ],
};

/** 线性插值 */
export function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

export function lerpVec(a: Vec3, b: Vec3, t: number): Vec3 {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t)];
}

/**
 * 把旧格式（单 `cameraKeys`）迁移成新格式（`cameraTracks`）。
 *
 * 为什么要迁移而不是直接改：已保存的工程里存的是旧格式，
 * 直接读新字段会拿到 undefined → 相机没有轨迹 → 画面全黑。
 */
export function migrateShot(shot: Shot): Shot {
  if (shot.cameraTracks && shot.cameraTracks.length > 0) return shot;
  const keys = shot.cameraKeys ?? [];
  return {
    ...shot,
    cameraTracks: [
      {
        id: `${shot.id}_track0`,
        name: '机位 1',
        keys: keys.length
          ? keys
          : [{ id: `${shot.id}_k0`, time: 0, position: [0, 1.6, 4], target: [0, 1, 0], fov: 40 }],
      },
    ],
  };
}

/**
 * 取某时刻生效的轨道（多机位分派）。
 *
 * 两种排布方式：
 *   1. 显式给了 `start`/`end` 的轨道，按区间命中；
 *   2. 都没给时，**按数组顺序均分**片段时长（最省事，用户只要加轨道就自动切）
 * 命中的轨道内，再按该轨道自己的关键帧插值。
 */
export function activeTrack(shot: Shot, time: number): CameraTrack {
  const tracks = shot.cameraTracks;
  const explicit = tracks.filter((t) => t.start != null || t.end != null);
  if (explicit.length === tracks.length && explicit.length > 0) {
    const hit = tracks.find(
      (t) => time >= (t.start ?? 0) && time < (t.end ?? shot.duration),
    );
    if (hit) return hit;
    return tracks[tracks.length - 1];
  }
  // 均分：n 条轨道把 [0, duration) 切成 n 段
  const n = Math.max(1, tracks.length);
  const seg = shot.duration / n;
  const idx = Math.min(n - 1, Math.max(0, Math.floor(time / Math.max(1e-6, seg))));
  return tracks[idx];
}

/**
 * 在单条轨道内按时间插值出相机姿态（位置 + 视角）。
 *
 * 关键帧之间**线性插值**。不引入缓动曲线：预演的用途是给出确定的起止构图，
 * 中间用什么曲线应由视频模型决定，额外加缓动会与模型自身的运动叠加。
 */
export function sampleTrack(track: CameraTrack, time: number): CameraKeyframe {
  const keys = [...track.keys].sort((a, b) => a.time - b.time);
  if (keys.length === 0) {
    return { id: 'fallback', time, position: [0, 1.6, 4], target: [0, 1, 0], fov: 40 };
  }
  if (time <= keys[0].time) return { ...keys[0], time };
  const last = keys[keys.length - 1];
  if (time >= last.time) return { ...last, time };

  for (let i = 0; i < keys.length - 1; i += 1) {
    const a = keys[i];
    const b = keys[i + 1];
    if (time >= a.time && time <= b.time) {
      const span = b.time - a.time;
      const t = span <= 0 ? 0 : (time - a.time) / span;
      return {
        id: `${a.id}~${b.id}`,
        time,
        position: lerpVec(a.position, b.position, t),
        target: lerpVec(a.target, b.target, t),
        fov: lerp(a.fov, b.fov, t),
        // 看向方式在插值区间内取前一个关键帧的设定：
        // 锁定目标不该在两个关键帧之间"渐变"，那是切换语义，不是插值语义
        aim: a.aim,
      };
    }
  }
  return { ...last, time };
}

/** 角色在场景里的位置（找不到时任取原点，避免相机飞到 NaN） */
export function actorPosition(scene: PrevisScene, actorId: string): Vec3 | null {
  const a = scene.actors.find((x) => x.id === actorId);
  return a ? a.position : null;
}

/**
 * 把关键帧解析成实际使用的"机位 + 看向点"。
 *
 * 锁定跟随在这里生效：`aim.kind === 'actor'` 时取该角色**当前**位置
 * 加上视线高度。因为每个时刻都会重新解析，角色走动时相机自然跟着转——
 * 不需要用户为每一帧手动改看向点。
 */
export function resolveCamera(
  scene: PrevisScene,
  kf: CameraKeyframe,
): { position: Vec3; target: Vec3; fov: number } {
  let target = kf.target;
  const aim = kf.aim;
  if (aim && aim.kind === 'actor') {
    const p = actorPosition(scene, aim.actorId);
    if (p) target = [p[0], p[1] + aim.height, p[2]];
  }
  return { position: kf.position, target, fov: kf.fov };
}

/** 取某时刻的相机姿态（含多机位分派与锁定跟随） */
export function sampleCamera(
  scene: PrevisScene,
  shot: Shot,
  time: number,
): { position: Vec3; target: Vec3; fov: number; trackName: string } {
  const track = activeTrack(shot, time);
  const kf = sampleTrack(track, time);
  const r = resolveCamera(scene, kf);
  return { ...r, trackName: track.name };
}

/** 简易唯一 id（不引 uuid 依赖） */
export function newId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}
