/**
 * 3D 白模视口（Three.js 场景）。
 *
 * 渲染原则（见 previs/types.ts）：**纯白模，无材质、无光照着色**。
 * 这里全部用 `meshBasicMaterial`，颜色即最终像素，不参与光照计算——
 * 让视频模型看到的是"形状与位置"，而不是"材质与光影"。
 * 用基础材质还有个好处：编辑预览与导出帧**完全一致**，不会"所见非所得"。
 *
 * 相机不由用户鼠标控制，而是**由镜头关键帧驱动**（这才是预演的意义）。
 * 场景里的导航用右键/中键拖拽由我们自己不实现——避免与"拖人物"抢事件。
 */

import { useEffect, useMemo, useRef } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { NoToneMapping, SRGBColorSpace, type Camera, type Scene, type WebGLRenderer } from 'three';
/**
 * 轨道控制取自 `three` 自带的 examples（**不引 drei**，项目刻意压依赖面）。
 * 用静态 import：这是 ESM 构建，`require` 在浏览器里不存在。
 */
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { usePrevis } from './store';
import { sampleCamera, type Vec3, PREVIS_COLOR } from './types';
import {
  HUMAN_BONES,
  HUMAN_SKELETON,
  samplePose,
  skeletonHeight,
  skeletonPositionsGrounded,
} from './humanoid';
import * as humanoidNs from './humanoid';
import * as typesNs from './types';
import * as bodySpecNs from './bodySpec';
import * as bodyMeshNs from './bodySpecMesh';
import { DrawPlane } from './DrawPlane';
import { buildBody } from './bodySpecMesh';

/**
 * 模块级注册表：把 Three.js 的 renderer/scene/camera 暴露给导出器。
 *
 * 为什么不用 React ref 往外传：导出是**命令式的一次性批处理**，
 * 需要同步拿到这三个对象；用 ref + 状态回传会引入时序问题
 * （组件重挂载后 ref 会指向旧对象）。模块级注册表最直接。
 */
export const three = {
  renderer: null as WebGLRenderer | null,
  scene: null as Scene | null,
  camera: null as Camera | null,
};

/** 自动化测试入口：骨架与 Three.js 对象挂到 window（供像素级与几何验证） */
declare global {
  interface Window {
    __previsThree?: typeof three;
    __previsHumanoid?: typeof humanoidNs;
    __previsTypes?: typeof typesNs;
    /** 白模规格模块。做几何自查时不必再靠截图猜。 */
    __previsBodySpec?: typeof bodySpecNs;
    __previsBodyMesh?: typeof bodyMeshNs;
  }
}
if (typeof window !== 'undefined') {
  window.__previsThree = three;
  window.__previsHumanoid = humanoidNs;
  window.__previsTypes = typesNs;
  window.__previsBodySpec = bodySpecNs;
  window.__previsBodyMesh = bodyMeshNs;
}

/** 标记为"辅助对象"的东西，导出时会被隐藏（网格线留在画面里会被模型当成真实结构） */
const HELPER = { helper: true } as const;

/**
 * 地面：**同时**带 `helper` 与 `ground` 两个标记。
 *
 * 为什么要分开标：两者行为不同——
 *   - 渲染/剪影：地面属辅助，要隐藏（它是参照，不是要被拍进去的结构）
 *   - 深度图：地面**是否计入是可选项**（ControlNet-Union 作者实测：
 *     无细节的光滑地面会压制提示词要求的纹理）
 * 只用一个标记就没法表达这个差别。
 */
const GROUND = { helper: true, ground: true } as const;

function Ground({ size }: { size: [number, number] }) {
  const [w, d] = size;
  // 网格格数固定为跨度的一半（每格 2 单位）。等分太密时线会挤到亚像素，
  // 在透视下退化成一整条色带（实测如此），反而看不清尺度。
  const span = Math.max(w, d);
  const cells = Math.max(4, Math.round(span / 2));
  return (
    <group>
      {/* 地面本体：带 ground 标记，深度图里可选计入（见 GROUND 的说明） */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.01, 0]} userData={GROUND}>
        <planeGeometry args={[w, d]} />
        <meshBasicMaterial color={PREVIS_COLOR.GROUND} />
      </mesh>
      <gridHelper args={[span, cells, '#3a3a44', '#26262e']} userData={HELPER} />
      {/* 坐标轴：只在编辑时帮助辨向 */}
      <axesHelper args={[1.2]} userData={HELPER} />
    </group>
  );
}

/**
 * 人形角色：关节球 + 肢体圆柱（胶囊近似）。
 *
 * 为什么用圆柱而不是细线：白模靠**体量**穿透 VAE 下采样。
 * 实测细骨架线在下采样后峰值只剩 99/255，而实心体是 253/255。
 *
 * 姿态按镜头时间采样——所以"行走"在导出序列里会自然循环，
 * 用户不需要逐帧摆姿势。
 */
function ActorMesh({ id }: { id: string }) {
  const actor = usePrevis((s) => s.scene.actors.find((a) => a.id === id));
  const selected = usePrevis((s) => s.selected);
  const playhead = usePrevis((s) => s.playhead);

  /**
   * 有白模规格 → 交给规格渲染器；没有 → 回退到写死的骨架。
   *
   * ⚠️ 这个判断必须放在**调用 `samplePose` 之前**，否则每次渲染都会白算一遍
   * 写死骨架的姿态（有规格时那个结果根本不用）。而且 `if (!actor)` 也要在最前，
   * 后面 `actor.color` 之类的取值依赖它。
   */
  const useSpec = Boolean(actor?.bodySpec);
  if (!actor) return null;
  if (useSpec) return <BodySpecActor id={id} />;

  const isSel = selected?.kind === 'actor' && selected.id === id;
  const pose = samplePose(
    actor.pose,
    playhead,
    actor.poseSpeed,
    actor.posePhase,
    actor.poseOverride,
  );
  // 用 **grounded** 版本：骨架以骨盆为原点，直接用会让腿埋进地面（实测过）
  const joints = skeletonPositionsGrounded(pose);
  const s = actor.scale;

  return (
    <group position={actor.position} rotation={[0, actor.yaw, 0]} scale={[s, s, s]}>
      {/* 肢体：每根骨画一个圆柱 */}
      {HUMAN_BONES.map(([child, parent]) => {
        const a = joints[parent];
        const b = joints[child];
        const def = HUMAN_SKELETON[child];
        return (
          <LimbCylinder
            key={`${parent}-${child}`}
            from={a}
            to={b}
            radius={def.radius}
            color={actor.color}
          />
        );
      })}
      {/* 关节球：让弯曲处不出现断裂 */}
      {Object.entries(joints).map(([name, p]) => (
        <mesh key={name} position={p}>
          <sphereGeometry args={[HUMAN_SKELETON[name].radius, 12, 8]} />
          <meshBasicMaterial color={actor.color} />
        </mesh>
      ))}
      {/* 朝向指示：贴地四棱锥指向 -Z */}
      <mesh position={[0, 0.02, -0.5]} rotation={[-Math.PI / 2, 0, 0]}>
        <coneGeometry args={[0.12, 0.3, 4]} />
        <meshBasicMaterial color="#e0925f" />
      </mesh>
      {isSel && (
        <mesh position={[0, skeletonHeight() / 2, 0]} userData={HELPER}>
          <boxGeometry args={[0.72, skeletonHeight() * 1.08, 0.72]} />
          <meshBasicMaterial color={PREVIS_COLOR.ACCENT} wireframe transparent opacity={0.3} />
        </mesh>
      )}
    </group>
  );
}

/**
 * 白模规格渲染：体块（胶囊/锥台/方块/球）。
 *
 * 与 `ActorMesh` 的分工：有 `bodySpec` 就走这里（AI 生成或人工改过的规格），
 * 没有就回退到写死的骨架。两者都用同一个 `seed`（角色 id）派生随机相位，
 * 保证切来切去时光照与相位不跳。
 */
function BodySpecActor({ id }: { id: string }) {
  const actor = usePrevis((s) => s.scene.actors.find((a) => a.id === id));
  const selected = usePrevis((s) => s.selected);
  const playhead = usePrevis((s) => s.playhead);
  if (!actor || !actor.bodySpec) return null;

  const spec = actor.bodySpec;
  const isSel = selected?.kind === 'actor' && selected.id === id;
  const built = buildBody(spec, { time: playhead, poseOverride: actor.poseOverride });
  /**
   * 两个缩放叠加：
   *   · `actor.scale`  —— 用户拖的"身高倍"
   *   · `built.heightScale` —— 让模型实际高度**等于**规格里写的 `height`
   *     （不乘这个，滑杆写 1.70 会得到 1.77，是明确的欺骗）
   */
  const s = actor.scale * built.heightScale;
  /** 接地：把整组上移，让最低脚掌落在 y=0 */
  const gy = actor.position[1] + built.groundOffset * s;

  return (
    <group position={[actor.position[0], gy, actor.position[2]]} rotation={[0, actor.yaw, 0]} scale={[s, s, s]}>
      {built.segments.map((item, i) => (
        <SpecSegment key={`${item.seg.joint}-${i}`} item={item} color={actor.color} />
      ))}
      {/* 关节球：填住两段肢体交接处的缝。半径为 0 的关节（脚掌等）不画 */}
      {Object.entries(built.joints).map(([name, jw]) => {
        const r = built.radii[name] ?? 0;
        if (r <= 0.001) return null;
        return (
          <mesh key={name} position={jw.pos}>
            <sphereGeometry args={[r, 12, 8]} />
            <meshBasicMaterial color={actor.color} />
          </mesh>
        );
      })}
      {/* 朝向指示：贴地四棱锥指向 -Z */}
      <mesh position={[0, 0.02, -0.5]} rotation={[-Math.PI / 2, 0, 0]}>
        <coneGeometry args={[0.12, 0.3, 4]} />
        <meshBasicMaterial color="#e0925f" />
      </mesh>
      {isSel && (
        <mesh position={[0, spec.proportions.height / 2, 0]} userData={HELPER}>
          <boxGeometry args={[0.8, spec.proportions.height * 1.08, 0.8]} />
          <meshBasicMaterial color={PREVIS_COLOR.ACCENT} wireframe transparent opacity={0.3} />
        </mesh>
      )}
    </group>
  );
}

/** 画一个体块。四种形状各自用最合适的 Three.js 内置几何。 */
function SpecSegment({
  item,
  color,
}: {
  item: {
    seg: import('./types').BodySegment;
    a: Vec3;
    b: Vec3;
    dir: Vec3;
    length: number;
  };
  color: string;
}) {
  const { seg, a, b, length } = item;
  const mid: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
  const mat = seg.color ?? color;

  // `box`（手掌/脚掌）单独处理：它不是"从父到子"的锥体，而是一块有长宽高的板，
  // 所以用中点定位、按盒子的实际尺寸画，并继承父→子方向的旋转。
  if (seg.shape === 'box') {
    const size = seg.size ?? [0.1, 0.1, 0.1];
    const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
    // 方块默认沿 +Y 长，所以把 +Y 转到父→子方向
    const rx = Math.atan2(Math.sqrt(dx * dx + dz * dz), dy);
    const rz = Math.atan2(dx, dz);
    return (
      <mesh position={mid} rotation={[rx, 0, -rz]}>
        <boxGeometry args={[size[0], size[1], size[2]]} />
        <meshBasicMaterial color={mat} />
      </mesh>
    );
  }

  if (seg.shape === 'sphere') {
    const r = Math.max(seg.r0, seg.r1);
    return (
      <mesh position={mid}>
        <sphereGeometry args={[r, 16, 12]} />
        <meshBasicMaterial color={mat} />
      </mesh>
    );
  }

  if (length < 1e-6) return null;
  const dx = b[0] - a[0], dy = b[1] - a[1], dz = b[2] - a[2];
  const rx = Math.atan2(Math.sqrt(dx * dx + dz * dz), dy);
  const rz = Math.atan2(dx, dz);

  if (seg.shape === 'taperedCylinder') {
    return (
      <mesh position={mid} rotation={[rx, 0, -rz]}>
        <cylinderGeometry args={[seg.r1, seg.r0, length, 12]} />
        <meshBasicMaterial color={mat} />
      </mesh>
    );
  }

  // capsule：Three.js 没有内置胶囊，用「圆柱 + 两端球」拼。
  // 注意圆柱长度要减掉两个端球半径，否则胶囊会比关节间距长、看起来脱节。
  const r = Math.min(seg.r0, seg.r1);
  const cylLen = Math.max(0.001, length - r * 2);
  const localA: Vec3 = [a[0], a[1], a[2]];
  const localB: Vec3 = [b[0], b[1], b[2]];
  const dirUnit: Vec3 = [dx / length, dy / length, dz / length];
  const capA: Vec3 = [
    localA[0] + dirUnit[0] * r,
    localA[1] + dirUnit[1] * r,
    localA[2] + dirUnit[2] * r,
  ];
  const capB: Vec3 = [
    localB[0] - dirUnit[0] * r,
    localB[1] - dirUnit[1] * r,
    localB[2] - dirUnit[2] * r,
  ];
  const capMid: Vec3 = [
    (capA[0] + capB[0]) / 2,
    (capA[1] + capB[1]) / 2,
    (capA[2] + capB[2]) / 2,
  ];
  return (
    <group>
      <mesh position={capMid} rotation={[rx, 0, -rz]}>
        <cylinderGeometry args={[r, r, cylLen, 12]} />
        <meshBasicMaterial color={mat} />
      </mesh>
      <mesh position={localA}>
        <sphereGeometry args={[seg.r0, 12, 8]} />
        <meshBasicMaterial color={mat} />
      </mesh>
      <mesh position={localB}>
        <sphereGeometry args={[seg.r1, 12, 8]} />
        <meshBasicMaterial color={mat} />
      </mesh>
    </group>
  );
}

/** 连接两点的圆柱（Three.js 的圆柱默认沿 +Y，故需旋转对齐方向） */
function LimbCylinder({
  from,
  to,
  radius,
  color,
}: {
  from: Vec3;
  to: Vec3;
  radius: number;
  color: string;
}) {
  const mid: Vec3 = [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2];
  const dx = to[0] - from[0];
  const dy = to[1] - from[1];
  const dz = to[2] - from[2];
  const len = Math.sqrt(dx * dx + dy * dy + dz * dz);
  if (len < 1e-6) return null;
  // 绕 Z 转 -X、绕 X 转 +Z，把 +Y 对齐到目标方向
  const rx = Math.atan2(Math.sqrt(dx * dx + dz * dz), dy);
  const rz = Math.atan2(dx, dz);
  return (
    <mesh position={mid} rotation={[rx, 0, -rz]}>
      <cylinderGeometry args={[radius, radius, len, 10]} />
      <meshBasicMaterial color={color} />
    </mesh>
  );
}

function PropMesh({ id }: { id: string }) {
  const prop = usePrevis((s) => s.scene.props.find((p) => p.id === id));
  const selected = usePrevis((s) => s.selected);
  if (!prop) return null;

  const [w, h, d] = prop.size;
  const isSel = selected?.kind === 'prop' && selected.id === id;

  return (
    <group
      position={[prop.position[0], prop.position[1] + h / 2, prop.position[2]]}
      rotation={[0, prop.yaw, 0]}
    >
      <mesh>
        <boxGeometry args={[w, h, d]} />
        <meshBasicMaterial color={prop.color} />
      </mesh>
      {isSel && (
        <mesh userData={HELPER}>
          <boxGeometry args={[w * 1.18, h * 1.18, d * 1.18]} />
          <meshBasicMaterial color={PREVIS_COLOR.ACCENT} wireframe />
        </mesh>
      )}
    </group>
  );
}

/** 相机关键帧可视化（当前机位用实色，其它机位暗显） */
function CameraGizmos() {
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const selected = usePrevis((s) => s.selected);
  const select = usePrevis((s) => s.select);
  const show = usePrevis((s) => s.showHelpers);
  const activeTrackId = usePrevis((s) => s.activeTrackId);
  const shot = scene.shots[shotIndex];

  // 用 useMemo 复用几何体，避免每帧重建（原先每帧 new bufferAttribute 会漏显存）
  const lines = useMemo(
    () =>
      (shot?.cameraTracks ?? []).flatMap((t) =>
        t.keys.map((k) => ({
          id: k.id,
          positions: new Float32Array([...k.position, ...k.target]),
        })),
      ),
    [shot?.cameraTracks],
  );

  if (!show || !shot) return null;

  return (
    <group>
      {shot.cameraTracks.flatMap((track) => {
        const isActive = !activeTrackId || track.id === activeTrackId;
        return track.keys.map((k) => {
          const isSel = selected?.kind === 'camera' && selected.id === k.id;
          const line = lines.find((l) => l.id === k.id);
          // 机位关键帧：选中 → 界面强调色；当前机位 → 完成色；其余 → 中性灰。
          // 原先的 `#4a4370` 是旧品牌的紫调，新的明度层级里没有紫。
          const base = isSel
            ? PREVIS_COLOR.ACCENT
            : isActive
              ? '#3fb950'
              : '#4a4a52';
          return (
            <group key={k.id}>
              <mesh
                position={k.position}
                userData={HELPER}
                onPointerDown={(e) => {
                  e.stopPropagation();
                  select({ kind: 'camera', id: k.id });
                }}
              >
                {/* 缩小并半透明：关键帧常在相机与角色之间，太大太实会挡住主体 */}
                <boxGeometry args={[0.14, 0.14, 0.22]} />
                <meshBasicMaterial
                  color={base}
                  wireframe={!isSel}
                  transparent
                  opacity={isSel ? 1 : isActive ? 0.7 : 0.35}
                />
              </mesh>
              {/*
                只给**选中的**关键帧画指向线。
                未选中的线会横穿整个画面（关键帧常在相机与角色之间），
                实测非常碍眼，而它们并不提供额外信息——小方块本身已标示机位。
              */}
              {isSel && line && (
                <lineSegments userData={HELPER}>
                  <bufferGeometry>
                    <bufferAttribute attach="attributes-position" args={[line.positions, 3]} />
                  </bufferGeometry>
                  <lineBasicMaterial color={PREVIS_COLOR.ACCENT} transparent opacity={0.6} />
                </lineSegments>
              )}
            </group>
          );
        });
      })}
    </group>
  );
}

/**
 * 相机：**两种模式**。
 *
 * `shot` —— 由当前生效机位驱动（多机位按时间分派 + 锁定跟随）。
 *   每帧重新解析，所以角色走动时"锁定跟随"会自然生效。
 *
 * `free` —— 由鼠标轨道控制，`CameraRig` **完全不碰相机**。
 *
 * ⚠️ 为什么必须分模式，而不是"让轨道控制和关键帧共用一台相机"：
 * 两者都要写 `position` / `quaternion`。若 `useFrame` 里继续写，
 * 鼠标刚转过去就被立刻覆写回去（用户看到的是"转了但弹回原位"）；
 * 若轨道控制也每帧写，那预览与导出的构图就不再由关键帧决定。
 * **谁写相机必须是排他的**，所以用模式切，而不是让两者协商。
 */
function CameraRig() {
  const camRef = useRef<import('three').PerspectiveCamera>(null);
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const viewMode = usePrevis((s) => s.viewMode);
  const set = useThree((s) => s.set);

  useFrame(() => {
    // 自由视角：交给 OrbitControls，这里一个字都不写
    if (usePrevis.getState().viewMode !== 'shot') return;
    const cam = camRef.current;
    const shot = scene.shots[shotIndex];
    if (!cam || !shot) return;
    // 每帧重新解析：角色走动时锁定跟随会自然生效
    const pose = sampleCamera(scene, shot, usePrevis.getState().playhead);
    cam.position.set(pose.position[0], pose.position[1], pose.position[2]);
    cam.lookAt(pose.target[0], pose.target[1], pose.target[2]);
    cam.fov = pose.fov;
    cam.updateProjectionMatrix();
  });

  // 让 R3F 用这台相机（比 drei 的 makeDefault 少一个依赖面）
  useEffect(() => {
    if (camRef.current) set({ camera: camRef.current });
  }, [set]);

  /**
   * 从自由视角切回机位视角时，把相机**立刻**摆回关键帧构图。
   *
   * 不做这一步的话：`useFrame` 虽然会写回正确位置，但 `useFrame`
   * 依赖 R3F 的渲染循环；切换那一帧可能出现"停在自由视角的构图"
   * 的闪烁。这里同步写一次，消除那一下。
   */
  useEffect(() => {
    if (viewMode !== 'shot') return;
    const cam = camRef.current;
    const shot = scene.shots[shotIndex];
    if (!cam || !shot) return;
    const pose = sampleCamera(scene, shot, usePrevis.getState().playhead);
    cam.position.set(pose.position[0], pose.position[1], pose.position[2]);
    cam.lookAt(pose.target[0], pose.target[1], pose.target[2]);
    cam.fov = pose.fov;
    cam.updateProjectionMatrix();
  }, [viewMode, scene, shotIndex]);

  return (
    <>
      <perspectiveCamera ref={camRef} fov={40} near={0.05} far={300} position={[0, 1.6, 4.2]} />
      {/* 只在自由视角下挂载：它自己会接管鼠标事件，
          若一直挂着，机位视角下拖角色就会被它抢走（这正是当初
          "不实现视口导航"想回避的冲突，现在用模式隔离解决）。 */}
      {viewMode === 'free' && <FreeOrbit target={camRef.current} />}
    </>
  );
}

/** 自由视角的鼠标轨道控制（旋转 / 平移 / 缩放）。 */
function FreeOrbit({ target }: { target: import('three').PerspectiveCamera | null }) {
  const gl = useThree((s) => s.gl);

  /**
   * `three` 自带 `examples/jsm/controls/OrbitControls.js`，直接用它，
   * 不引 `@react-three/drei` —— 项目刻意压依赖面，而这里只需要一个轨道控制。
   *
   * ⚠️ 用**静态 import**（文件顶部），不要 `require`：
   * 这是 ESM 构建（Vite），`require` 在浏览器里根本不存在。
   */
  const controls = useMemo(() => {
    if (!target) return null;
    const c = new OrbitControls(target, gl.domElement);
    c.enableDamping = true;
    c.dampingFactor = 0.08;
    c.screenSpacePanning = true;   // 平移跟着屏幕走，比默认的"贴地面"直观
    c.minDistance = 0.5;
    c.maxDistance = 80;
    // 允许俯视到接近正上方，但不允许转到地面以下（穿地观看很迷惑）
    c.maxPolarAngle = Math.PI * 0.495;
    return c;
  }, [target, gl]);

  useEffect(() => {
    if (!controls) return;
    return () => controls.dispose();
  }, [controls]);

  useFrame(() => {
    controls?.update();   // 开了 damping 就必须每帧 update
  });

  return null;
}

/**
 * 播放推进：**按镜头 fps 步进**，不是按显示帧率。
 *
 * 为什么要按 fps 步进：预览与导出必须看到同一串帧。
 * 若按显示帧率自由推进，60Hz 屏幕上同一秒会走过 60 个中间姿态，
 * 而导出只有 24 帧——用户看到的和导出的不是一回事。
 */
function Playback() {
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const acc = useRef(0);

  useFrame((_, delta) => {
    const st = usePrevis.getState();
    const shot = scene.shots[shotIndex];
    if (!st.playing || !shot) return;
    acc.current += delta;
    const step = 1 / (shot.fps || 24);
    if (acc.current < step) return;
    // 一次最多推进若干帧，防止切换标签页回来后疯狂追帧
    const steps = Math.min(4, Math.floor(acc.current / step));
    acc.current -= steps * step;
    const next = st.playhead + steps * step;
    if (next >= shot.duration) {
      st.setPlayhead(shot.duration);
      st.setPlaying(false);
      acc.current = 0;
    } else {
      st.setPlayhead(next);
    }
  });
  return null;
}

/** 把 Three.js 对象注册出去，供导出器使用 */
function Bridge() {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);
  const camera = useThree((s) => s.camera);
  useEffect(() => {
    three.renderer = gl;
    three.scene = scene;
    three.camera = camera;
  }, [gl, scene, camera]);
  return null;
}

export default function Scene3D() {
  const scene = usePrevis((s) => s.scene);
  const actorIds = useMemo(() => scene.actors.map((a) => a.id), [scene.actors]);
  const propIds = useMemo(() => scene.props.map((p) => p.id), [scene.props]);

  return (
    <Canvas
      style={{ width: '100%', height: '100%', background: '#000' }}
      /**
       * 关闭色调映射与色彩空间转换。
       *
       * 实测：默认管线下输入 `#c8f04a`(200,240,74) 渲染出来是 (210,222,121)，
       * 颜色被 sRGB→线性→再编码改过。对白模而言"我设什么颜色就得到什么颜色"
       * 更重要——导出帧要与编辑器所见一致，否则调参无意义。
       */
      gl={{
        preserveDrawingBuffer: true,
        antialias: true,
        toneMapping: NoToneMapping,
        outputColorSpace: SRGBColorSpace,
      }}
      onPointerMissed={() => usePrevis.getState().select(null)}
    >
      <Bridge />
      <CameraRig />
      <Playback />
      <Ground size={scene.ground} />
      {/* 绘制轨道的拾取面：只在绘制模式下存在，放在角色之前以便优先接管点击 */}
      <DrawPlane />
      {actorIds.map((id) => (
        <ActorMesh key={id} id={id} />
      ))}
      {propIds.map((id) => (
        <PropMesh key={id} id={id} />
      ))}
      <CameraGizmos />
    </Canvas>
  );
}

/** 未使用但保留的类型引用，避免被 tree-shaking 掉类型导出 */
export type _V = Vec3;
