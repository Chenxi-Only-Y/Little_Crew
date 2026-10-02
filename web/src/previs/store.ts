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
  migrateShot,
  newId,
  sampleCamera,
  type Actor,
  type CameraAim,
  type CameraKeyframe,
  type CameraTrack,
  type PrevisScene,
  type Prop,
  type Shot,
  type Vec3,
} from './types';
import { ENV_GROUND, applyEnvPreset, type EnvPreset } from './env';

/** 取当前片段（已迁移到新格式，避免旧工程读不到轨道） */
function currentShot(scene: PrevisScene, index: number): Shot | null {
  const s = scene.shots[index];
  return s ? migrateShot(s) : null;
}

function withTrack(
  scene: PrevisScene,
  shotIndex: number,
  trackId: string,
  fn: (t: CameraTrack) => CameraTrack,
): PrevisScene {
  const shot = currentShot(scene, shotIndex);
  if (!shot) return scene;
  const next: Shot = {
    ...shot,
    cameraTracks: shot.cameraTracks.map((t) => (t.id === trackId ? fn(t) : t)),
  };
  return { ...scene, shots: scene.shots.map((s, i) => (i === shotIndex ? next : s)) };
}

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

  /** 相机关键帧（按轨道操作，支持多机位） */
  addCameraKey: (atTime: number, trackId?: string) => void;
  updateCameraKey: (trackId: string, id: string, patch: Partial<CameraKeyframe>) => void;
  removeCameraKey: (trackId: string, id: string) => void;
  /** 设定关键帧的看向方式（固定目标 / 锁定某个角色） */
  setKeyAim: (trackId: string, id: string, aim: CameraAim | undefined) => void;

  /** 机位（轨道）增删改 */
  addTrack: () => void;
  removeTrack: (trackId: string) => void;
  updateTrack: (trackId: string, patch: Partial<Pick<CameraTrack, 'name' | 'start' | 'end'>>) => void;

  /** 当前正在编辑的机位（属性面板与绘制轨道都用它） */
  activeTrackId: string | null;
  setActiveTrack: (id: string | null) => void;

  /** 绘制轨道模式：在视口里点地面生成关键帧 */
  drawing: boolean;
  setDrawing: (v: boolean) => void;
  /** 绘制时放置关键帧的高度（米） */
  drawHeight: number;
  setDrawHeight: (v: number) => void;
  /** 在视口里放一个关键帧（由 Scene3D 的射线拾取调用） */
  placeKeyAt: (pos: Vec3) => void;

  /** 镜头 */
  addShot: () => void;
  updateShot: (patch: Partial<Pick<Shot, 'name' | 'duration' | 'fps'>>) => void;

  /** 逐关节姿态偏移（叠加在预设之上，见 Actor.poseOverride） */
  setJointOffset: (id: string, joint: string, axis: 0 | 1 | 2, value: number) => void;
  /** 清掉某个关节的偏移 */
  clearJoint: (id: string, joint: string) => void;
  /** 清掉全部偏移（回到纯预设） */
  clearAllJoints: (id: string) => void;

  /** 环境预设（替换环境块，保留用户自建道具） */
  applyEnv: (preset: EnvPreset) => void;
}

function withShot(scene: PrevisScene, index: number, fn: (s: Shot) => Shot): PrevisScene {
  return { ...scene, shots: scene.shots.map((s, i) => (i === index ? fn(s) : s)) };
}

/**
 * 载入场景时把**旧格式**（单 cameraKeys）迁移成 cameraTracks。
 *
 * 不做这一步的话，打开旧工程会拿到没有轨道的片段 → 相机无姿态 → 画面全黑，
 * 而用户只会觉得"工程坏了"。
 */
function normalizeScene(s: PrevisScene): PrevisScene {
  return { ...s, shots: (s.shots ?? []).map(migrateShot) };
}

export const usePrevis = create<PrevisState>((set) => ({
  scene: DEFAULT_SCENE,
  shotIndex: 0,
  selected: { kind: 'actor', id: 'actor_1' },
  playhead: 0,
  playing: false,
  showHelpers: true,

  setScene: (s) =>
    set({ scene: normalizeScene(s), shotIndex: 0, selected: null, playhead: 0, playing: false }),
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
        scale: 1,
        pose: 'idle',
        poseSpeed: 1,
        // 相位错开，避免多人走路完全同步（那看起来像复制粘贴）
        posePhase: (st.scene.actors.length * 0.37) % 1,
        color: ['#c8f04a', '#f0b23d', '#e8736a', '#61d6a0'][st.scene.actors.length % 4],
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

  addCameraKey: (atTime, trackId) =>
    set((st) => {
      const shot = currentShot(st.scene, st.shotIndex);
      if (!shot) return st;
      const tid = trackId ?? st.activeTrackId ?? shot.cameraTracks[0]?.id;
      if (!tid) return st;
      // 从当前时刻采样一个姿态作为新关键帧的起点（避免新帧跳到原点）
      const cur = sampleCamera(st.scene, shot, atTime);
      const k: CameraKeyframe = {
        id: newId('key'),
        time: atTime,
        position: [...cur.position] as Vec3,
        target: [...cur.target] as Vec3,
        fov: cur.fov,
      };
      return {
        scene: withTrack(st.scene, st.shotIndex, tid, (t) => ({
          ...t,
          keys: [...t.keys, k].sort((a, b) => a.time - b.time),
        })),
        selected: { kind: 'camera', id: k.id },
        activeTrackId: tid,
      };
    }),

  updateCameraKey: (trackId, id, patch) =>
    set((st) => ({
      scene: withTrack(st.scene, st.shotIndex, trackId, (t) => ({
        ...t,
        keys: t.keys.map((k) => (k.id === id ? { ...k, ...patch } : k)),
      })),
    })),

  removeCameraKey: (trackId, id) =>
    set((st) => {
      const shot = currentShot(st.scene, st.shotIndex);
      const track = shot?.cameraTracks.find((t) => t.id === trackId);
      // 每条轨道至少留一个关键帧，否则该机位无姿态可用
      if (!track || track.keys.length <= 1) return st;
      return {
        scene: withTrack(st.scene, st.shotIndex, trackId, (t) => ({
          ...t,
          keys: t.keys.filter((k) => k.id !== id),
        })),
        selected: st.selected?.kind === 'camera' && st.selected.id === id ? null : st.selected,
      };
    }),

  setKeyAim: (trackId, id, aim) =>
    set((st) => ({
      scene: withTrack(st.scene, st.shotIndex, trackId, (t) => ({
        ...t,
        keys: t.keys.map((k) => (k.id === id ? { ...k, aim } : k)),
      })),
    })),

  addTrack: () =>
    set((st) => {
      const shot = currentShot(st.scene, st.shotIndex);
      if (!shot) return st;
      // 新机位默认与第一个机位错开，避免完全重叠看不出区别
      const base = shot.cameraTracks[0]?.keys[0];
      const t: CameraTrack = {
        id: newId('track'),
        name: `机位 ${shot.cameraTracks.length + 1}`,
        keys: [
          {
            id: newId('key'),
            time: 0,
            position: base ? [base.position[0] + 2, base.position[1], base.position[2]] : [2, 1.6, 4],
            target: base ? [...base.target] as Vec3 : [0, 1, 0],
            fov: base?.fov ?? 40,
          },
        ],
      };
      const next: Shot = { ...shot, cameraTracks: [...shot.cameraTracks, t] };
      return {
        scene: { ...st.scene, shots: st.scene.shots.map((s, i) => (i === st.shotIndex ? next : s)) },
        activeTrackId: t.id,
      };
    }),

  removeTrack: (trackId) =>
    set((st) => {
      const shot = currentShot(st.scene, st.shotIndex);
      // 至少留一条轨道，否则片段没有相机
      if (!shot || shot.cameraTracks.length <= 1) return st;
      const next: Shot = {
        ...shot,
        cameraTracks: shot.cameraTracks.filter((t) => t.id !== trackId),
      };
      return {
        scene: { ...st.scene, shots: st.scene.shots.map((s, i) => (i === st.shotIndex ? next : s)) },
        activeTrackId: st.activeTrackId === trackId ? next.cameraTracks[0].id : st.activeTrackId,
      };
    }),

  updateTrack: (trackId, patch) =>
    set((st) => ({
      scene: withTrack(st.scene, st.shotIndex, trackId, (t) => ({ ...t, ...patch })),
    })),

  activeTrackId: null,
  setActiveTrack: (id) => set({ activeTrackId: id }),

  drawing: false,
  setDrawing: (v) => set({ drawing: v }),
  drawHeight: 1.5,
  setDrawHeight: (v) => set({ drawHeight: v }),

  placeKeyAt: (pos) =>
    set((st) => {
      const shot = currentShot(st.scene, st.shotIndex);
      if (!shot) return st;
      const tid = st.activeTrackId ?? shot.cameraTracks[0]?.id;
      if (!tid) return st;
      const k: CameraKeyframe = {
        id: newId('key'),
        time: st.playhead,
        position: [pos[0], st.drawHeight, pos[2]],
        // 看向点取当前该机位的看向，保持用户已设的构图意图
        target: (() => {
          const cur = sampleCamera(st.scene, shot, st.playhead);
          return [...cur.target] as Vec3;
        })(),
        fov: 40,
      };
      return {
        scene: withTrack(st.scene, st.shotIndex, tid, (t) => ({
          ...t,
          keys: [...t.keys, k].sort((a, b) => a.time - b.time),
        })),
        selected: { kind: 'camera', id: k.id },
      };
    }),

  addShot: () =>
    set((st) => {
      const shot: Shot = {
        id: newId('shot'),
        name: `镜头 ${st.scene.shots.length + 1}`,
        duration: 7,
        fps: 24,
        cameraTracks: [
          {
            id: newId('track'),
            name: '机位 1',
            keys: [
              { id: newId('key'), time: 0, position: [0, 1.6, 4.2], target: [0, 1, 0], fov: 40 },
              { id: newId('key'), time: 7, position: [0, 1.6, 3.0], target: [0, 1, 0], fov: 40 },
            ],
          },
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

  setJointOffset: (id, joint, axis, value) =>
    set((st) => ({
      scene: {
        ...st.scene,
        actors: st.scene.actors.map((a) => {
          if (a.id !== id) return a;
          const cur = a.poseOverride ?? {};
          const j = [...(cur[joint] ?? [0, 0, 0])] as Vec3;
          j[axis] = value;
          return { ...a, poseOverride: { ...cur, [joint]: j } };
        }),
      },
    })),

  clearJoint: (id, joint) =>
    set((st) => ({
      scene: {
        ...st.scene,
        actors: st.scene.actors.map((a) => {
          if (a.id !== id || !a.poseOverride) return a;
          const next = { ...a.poseOverride };
          delete next[joint];
          return { ...a, poseOverride: next };
        }),
      },
    })),

  clearAllJoints: (id) =>
    set((st) => ({
      scene: {
        ...st.scene,
        actors: st.scene.actors.map((a) =>
          a.id === id ? { ...a, poseOverride: undefined } : a,
        ),
      },
    })),

  applyEnv: (preset) =>
    set((st) => {
      const props = applyEnvPreset(st.scene.props, preset, st.scene.ground);
      const g = ENV_GROUND[preset];
      return {
        scene: {
          ...st.scene,
          props,
          // 地面太小时模型会以为世界是个盒子，所以预设会连带调整地面
          ground: g ?? st.scene.ground,
        },
        // 环境块换了之后，之前选中的可能已不存在
        selected: st.selected?.kind === 'prop' && !props.some((p) => p.id === st.selected?.id)
          ? null
          : st.selected,
      };
    }),
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
