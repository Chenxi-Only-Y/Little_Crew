/**
 * 3D 预演编辑器的状态。
 *
 * 与导演台的画布 store 分开：预演编辑器是**独立工具**，
 * 不参与 LangGraph 工作流（白模预演师目前尚未进图，见 registry 的 workflow_node 为空）。
 * 混进同一个 store 会让两个工具的选中态、脏标记互相干扰。
 */

import { create } from 'zustand';
import {
  DEFAULT_SCENE,
  newId,
  sampleCamera,
  type Actor,
  type CameraKeyframe,
  type PrevisScene,
  type Prop,
  type Shot,
  type Vec3,
} from './types';

export type Selection =
  | { kind: 'actor'; id: string }
  | { kind: 'prop'; id: string }
  | { kind: 'camera'; id: string }
  | null;

interface PrevisState {
  scene: PrevisScene;
  /** 当前编辑的镜头 index */
  shotIndex: number;
  selected: Selection;
  /** 播放头（秒） */
  playhead: number;
  playing: boolean;
  /** 是否显示相机辅助（导出时会被关掉） */
  showHelpers: boolean;

  setScene: (s: PrevisScene) => void;
  resetScene: () => void;

  select: (sel: Selection) => void;
  setPlayhead: (t: number) => void;
  setPlaying: (v: boolean) => void;
  setShowHelpers: (v: boolean) => void;
  setShotIndex: (i: number) => void;

  addActor: () => void;
  addProp: () => void;
  updateActor: (id: string, patch: Partial<Actor>) => void;
  updateProp: (id: string, patch: Partial<Prop>) => void;
  removeSelected: () => void;

  /** 相机关键帧 */
  addCameraKey: (atTime: number) => void;
  updateCameraKey: (id: string, patch: Partial<CameraKeyframe>) => void;
  removeCameraKey: (id: string) => void;

  /** 镜头 */
  addShot: () => void;
  updateShot: (patch: Partial<Pick<Shot, 'name' | 'duration' | 'fps'>>) => void;
}

function withShot(scene: PrevisScene, index: number, fn: (s: Shot) => Shot): PrevisScene {
  return { ...scene, shots: scene.shots.map((s, i) => (i === index ? fn(s) : s)) };
}

export const usePrevis = create<PrevisState>((set) => ({
  scene: DEFAULT_SCENE,
  shotIndex: 0,
  selected: { kind: 'actor', id: 'actor_1' },
  playhead: 0,
  playing: false,
  showHelpers: true,

  setScene: (s) => set({ scene: s, shotIndex: 0, selected: null, playhead: 0, playing: false }),
  resetScene: () =>
    set({ scene: DEFAULT_SCENE, shotIndex: 0, selected: null, playhead: 0, playing: false }),

  select: (sel) => set({ selected: sel }),
  setPlayhead: (t) => set({ playhead: Math.max(0, t) }),
  setPlaying: (v) => set({ playing: v }),
  setShowHelpers: (v) => set({ showHelpers: v }),
  setShotIndex: (i) => set({ shotIndex: i, playhead: 0, playing: false }),

  addActor: () =>
    set((st) => {
      const a: Actor = {
        id: newId('actor'),
        name: `角色 ${st.scene.actors.length + 1}`,
        position: [0, 0, 0],
        yaw: 0,
        size: [0.5, 1.7, 0.3],
        color: '#f0b23d',
      };
      return {
        scene: { ...st.scene, actors: [...st.scene.actors, a] },
        selected: { kind: 'actor', id: a.id },
      };
    }),

  addProp: () =>
    set((st) => {
      const p: Prop = {
        id: newId('prop'),
        name: `道具 ${st.scene.props.length + 1}`,
        position: [0, 0, 0],
        yaw: 0,
        size: [1, 1, 1],
        color: '#8b8bf0',
      };
      return {
        scene: { ...st.scene, props: [...st.scene.props, p] },
        selected: { kind: 'prop', id: p.id },
      };
    }),

  updateActor: (id, patch) =>
    set((st) => ({
      scene: {
        ...st.scene,
        actors: st.scene.actors.map((a) => (a.id === id ? { ...a, ...patch } : a)),
      },
    })),

  updateProp: (id, patch) =>
    set((st) => ({
      scene: {
        ...st.scene,
        props: st.scene.props.map((p) => (p.id === id ? { ...p, ...patch } : p)),
      },
    })),

  removeSelected: () =>
    set((st) => {
      const sel = st.selected;
      if (!sel || sel.kind === 'camera') return st;
      const scene =
        sel.kind === 'actor'
          ? { ...st.scene, actors: st.scene.actors.filter((a) => a.id !== sel.id) }
          : { ...st.scene, props: st.scene.props.filter((p) => p.id !== sel.id) };
      return { scene, selected: null };
    }),

  addCameraKey: (atTime) =>
    set((st) => {
      const shot = st.scene.shots[st.shotIndex];
      if (!shot) return st;
      // 从当前播放头采样一个姿态作为新关键帧的起点（避免新帧跳到原点）
      const cur = sampleCamera(shot, atTime);
      const k: CameraKeyframe = {
        id: newId('key'),
        time: atTime,
        position: [...cur.position] as Vec3,
        target: [...cur.target] as Vec3,
        fov: cur.fov,
      };
      return {
        scene: withShot(st.scene, st.shotIndex, (s) => ({
          ...s,
          cameraKeys: [...s.cameraKeys, k].sort((a, b) => a.time - b.time),
        })),
        selected: { kind: 'camera', id: k.id },
      };
    }),

  updateCameraKey: (id, patch) =>
    set((st) => ({
      scene: withShot(st.scene, st.shotIndex, (s) => ({
        ...s,
        cameraKeys: s.cameraKeys.map((k) => (k.id === id ? { ...k, ...patch } : k)),
      })),
    })),

  removeCameraKey: (id) =>
    set((st) => {
      const shot = st.scene.shots[st.shotIndex];
      // 至少留一个关键帧，否则相机无姿态可用
      if (!shot || shot.cameraKeys.length <= 1) return st;
      return {
        scene: withShot(st.scene, st.shotIndex, (s) => ({
          ...s,
          cameraKeys: s.cameraKeys.filter((k) => k.id !== id),
        })),
        selected: st.selected?.kind === 'camera' && st.selected.id === id ? null : st.selected,
      };
    }),

  addShot: () =>
    set((st) => {
      const shot: Shot = {
        id: newId('shot'),
        name: `镜头 ${st.scene.shots.length + 1}`,
        duration: 7,
        fps: 24,
        cameraKeys: [
          { id: newId('key'), time: 0, position: [0, 1.6, 4.2], target: [0, 1, 0], fov: 40 },
          { id: newId('key'), time: 7, position: [0, 1.6, 3.0], target: [0, 1, 0], fov: 40 },
        ],
      };
      return {
        scene: { ...st.scene, shots: [...st.scene.shots, shot] },
        shotIndex: st.scene.shots.length,
        playhead: 0,
      };
    }),

  updateShot: (patch) =>
    set((st) => ({
      scene: withShot(st.scene, st.shotIndex, (s) => ({ ...s, ...patch })),
    })),
}));

/**
 * 开发/自动化测试入口：把 store 挂到 window。
 *
 * 为什么需要：像"改播放头后画面是否真的变了"这类**跨 store→3D 渲染**的验证，
 * 只有驱动真实浏览器读回像素才能证明；纯 DOM 断言看不出画面是否更新。
 */
declare global {
  interface Window {
    __previsStore?: typeof usePrevis;
  }
}
if (typeof window !== 'undefined') {
  window.__previsStore = usePrevis;
}
