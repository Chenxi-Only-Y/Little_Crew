/**
 * 环境块预设。
 *
 * 设计原则：**环境不是"墙"，是"边界"。**
 *   室内用四面墙定义边界；广场用地平线上的一圈体量定义边界；
 *   街道用两侧建筑定义边界。三者是同一件事的不同形式。
 *
 * 实现上**复用道具系统**（不是另起一套环境系统）：
 * 道具已经能渲染、能进深度图与剪影、能随工程存档。
 * 另建一套要重复这些能力，且容易出现"环境不进导出"这类不一致。
 * 只是给它打个 `env` 标记，在 UI 上单独分组。
 *
 * 为什么"一大块地面 + 一圈体量"就够：
 * 视频模型需要知道的是**空间边界与尺度**，不是建筑细节。
 * 白模给细节反而会让它去学那些细节。
 */

import { newId, type Prop, type Vec3 } from './types';

export type EnvPreset = 'room' | 'plaza' | 'street' | 'stage' | 'clear';

export interface EnvPresetMeta {
  key: EnvPreset;
  label: string;
  hint: string;
}

export const ENV_PRESETS: EnvPresetMeta[] = [
  { key: 'room', label: '房间', hint: '四壁 + 天花板高度，室内对话/独白' },
  { key: 'plaza', label: '广场', hint: '一圈远处建筑，户外开阔场景' },
  { key: 'street', label: '街道', hint: '两侧连续建筑，纵向纵深' },
  { key: 'stage', label: '舞台', hint: '三面围挡 + 开放正面，表演/讲台' },
  { key: 'clear', label: '清空环境', hint: '只留下地面，移除所有环境块' },
];

/** 环境块统一用暗色：它是背景体量，不该在编辑器里抢视线 */
const ENV_COLOR = '#3a3458';

interface Box {
  name: string;
  position: Vec3;
  yaw: number;
  size: Vec3;
}

function buildRoom(w: number, d: number, h: number): Box[] {
  const t = 0.25; // 墙厚
  return [
    { name: '后墙', position: [0, 0, -d / 2], yaw: 0, size: [w, h, t] },
    { name: '左墙', position: [-w / 2, 0, 0], yaw: Math.PI / 2, size: [d, h, t] },
    { name: '右墙', position: [w / 2, 0, 0], yaw: Math.PI / 2, size: [d, h, t] },
    // 前墙留空当"入口"，否则相机常常贴在上面
  ];
}

function buildPlaza(radius: number, count: number): Box[] {
  const out: Box[] = [];
  for (let i = 0; i < count; i += 1) {
    const a = (i / count) * Math.PI * 2 + 0.15;
    const r = radius + (i % 3) * (radius * 0.13);
    out.push({
      name: `建筑${i + 1}`,
      position: [Math.cos(a) * r, 0, Math.sin(a) * r],
      // 让正面朝向中心（观感更像围合）
      yaw: -a + Math.PI / 2,
      size: [4 + (i % 4) * 1.8, 6 + (i % 5) * 3, 3.5 + (i % 3) * 1.2],
    });
  }
  return out;
}

function buildStreet(length: number, count: number): Box[] {
  const out: Box[] = [];
  const gap = 3.2; // 街道宽度
  for (let i = 0; i < count; i += 1) {
    const z = -length / 2 + (i / Math.max(1, count - 1)) * length;
    out.push({
      name: `左楼${i + 1}`,
      position: [-gap, 0, z],
      yaw: 0,
      size: [3.5, 5 + (i % 4) * 2.5, 2.6],
    });
    out.push({
      name: `右楼${i + 1}`,
      position: [gap, 0, z],
      yaw: 0,
      size: [3.5, 5.5 + (i % 3) * 2.5, 2.6],
    });
  }
  return out;
}

function buildStage(w: number, d: number, h: number): Box[] {
  const t = 0.25;
  return [
    { name: '背幕', position: [0, 0, -d / 2], yaw: 0, size: [w, h, t] },
    { name: '左翼', position: [-w / 2, 0, -d / 6], yaw: Math.PI / 2, size: [d * 0.6, h, t] },
    { name: '右翼', position: [w / 2, 0, -d / 6], yaw: Math.PI / 2, size: [d * 0.6, h, t] },
  ];
}

/**
 * 生成环境块。
 *
 * `scale` 是整体尺度参考（通常取场景地面尺寸），
 * 让预设能适配用户当前的地面大小，而不是写死一套尺寸。
 */
export function makeEnvProps(preset: EnvPreset, ground: [number, number]): Prop[] {
  let boxes: Box[] = [];
  const w = Math.max(6, ground[0] * 0.7);
  const d = Math.max(6, ground[1] * 0.7);

  switch (preset) {
    case 'room':
      boxes = buildRoom(w, d, 3.0);
      break;
    case 'plaza':
      boxes = buildPlaza(Math.max(14, Math.max(ground[0], ground[1]) * 0.8), 14);
      break;
    case 'street':
      boxes = buildStreet(Math.max(24, d * 2), 7);
      break;
    case 'stage':
      boxes = buildStage(w, d, 4.0);
      break;
    case 'clear':
    default:
      boxes = [];
  }

  return boxes.map((b) => ({
    id: newId('env'),
    name: b.name,
    position: b.position,
    yaw: b.yaw,
    size: b.size,
    color: ENV_COLOR,
    env: true,
  }));
}

/** 应用预设：**替换**已有的环境块，保留用户自建的道具 */
export function applyEnvPreset(
  currentProps: Prop[],
  preset: EnvPreset,
  ground: [number, number],
): Prop[] {
  const keep = currentProps.filter((p) => !p.env);
  return [...keep, ...makeEnvProps(preset, ground)];
}

/**
 * 各预设建议的地面尺寸。
 *
 * 地面太小会让视频模型以为"世界是个盒子"，所以广场/街道要放大。
 */
export const ENV_GROUND: Record<EnvPreset, [number, number] | null> = {
  room: [12, 12],
  plaza: [80, 80],
  street: [30, 120],
  stage: [14, 12],
  clear: null, // 不改地面
};
