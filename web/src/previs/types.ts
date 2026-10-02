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

/** 人物块：白模不需要像人，胶囊/圆柱足够表达占位与体量 */
export interface Actor {
  id: string;
  name: string;
  /** 脚底位置（y 通常为 0，站在地面上） */
  position: Vec3;
  /** 朝向（绕 Y 轴，弧度；0 = 面向 -Z，即面向默认相机） */
  yaw: number;
  /** [宽, 高, 厚]（世界单位） */
  size: Vec3;
  /** 显示颜色（**仅编辑器内用于区分**，导出时一律渲染成纯白剪影） */
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
}

/**
 * 相机关键帧。
 *
 * 为什么用"关键帧 + 插值"而不是录一条轨迹：
 * 白模的用途是**定义起点与终点**（H3 只吃首尾帧），
 * 关键帧让用户明确摆出这两端，而不是在一堆采样点里找。
 */
export interface CameraKeyframe {
  id: string;
  /** 秒（相对片段起点） */
  time: number;
  /** 机位 */
  position: Vec3;
  /** 看向的目标点 */
  target: Vec3;
  /** 垂直视场角（度） */
  fov: number;
}

/** 一条片段（一个镜头） */
export interface Shot {
  id: string;
  name: string;
  /** 时长（秒）。本地 H3 安全上限 10 秒，云端可到 15。 */
  duration: number;
  fps: number;
  cameraKeys: CameraKeyframe[];
}

export interface PrevisScene {
  /** 地面尺寸 [宽, 深]（世界单位） */
  ground: [number, number];
  actors: Actor[];
  props: Prop[];
  shots: Shot[];
}

export const DEFAULT_SCENE: PrevisScene = {
  ground: [12, 12],
  actors: [
    {
      id: 'actor_1',
      name: '角色 A',
      position: [0, 0, 0],
      yaw: 0,
      size: [0.5, 1.7, 0.3],
      color: '#c8f04a',
    },
  ],
  props: [
    {
      id: 'prop_1',
      name: '桌子',
      position: [1.8, 0, -0.6],
      yaw: 0,
      size: [1.2, 0.75, 0.7],
      color: '#8b8bf0',
    },
  ],
  shots: [
    {
      id: 'shot_1',
      name: '镜头 1',
      duration: 7,
      fps: 24,
      cameraKeys: [
        { id: 'k0', time: 0, position: [0, 1.6, 4.2], target: [0, 1.0, 0], fov: 40 },
        { id: 'k1', time: 7, position: [1.4, 1.5, 3.0], target: [0.6, 0.9, -0.4], fov: 40 },
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
 * 取某时刻的相机姿态。
 *
 * 关键帧按 time 排序后**线性插值**。不引入缓动曲线：
 * 预演的用途是给出确定的起止构图，中间用什么曲线应由视频模型决定，
 * 这里额外加缓动反而会与模型自身的运动叠加、产生意外。
 */
export function sampleCamera(shot: Shot, time: number): CameraKeyframe {
  const keys = [...shot.cameraKeys].sort((a, b) => a.time - b.time);
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
      };
    }
  }
  return { ...last, time };
}

/** 简易唯一 id（不引 uuid 依赖） */
export function newId(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
}
