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
import { usePrevis } from './store';
import { sampleCamera, type Vec3 } from './types';
import {
  HUMAN_BONES,
  HUMAN_SKELETON,
  samplePose,
  skeletonHeight,
  skeletonPositionsGrounded,
} from './humanoid';
import * as humanoidNs from './humanoid';

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
  }
}
if (typeof window !== 'undefined') {
  window.__previsThree = three;
  window.__previsHumanoid = humanoidNs;
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
        <meshBasicMaterial color="#1b1730" />
      </mesh>
      <gridHelper args={[span, cells, '#6a6398', '#3a3458']} userData={HELPER} />
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
  if (!actor) return null;

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
        <meshBasicMaterial color="#e8736a" />
      </mesh>
      {isSel && (
        <mesh position={[0, skeletonHeight() / 2, 0]} userData={HELPER}>
          <boxGeometry args={[0.72, skeletonHeight() * 1.08, 0.72]} />
          <meshBasicMaterial color="#c8f04a" wireframe transparent opacity={0.3} />
        </mesh>
      )}
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
          <meshBasicMaterial color="#c8f04a" wireframe />
        </mesh>
      )}
    </group>
  );
}

/** 相机关键帧可视化 */
function CameraGizmos() {
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const selected = usePrevis((s) => s.selected);
  const select = usePrevis((s) => s.select);
  const show = usePrevis((s) => s.showHelpers);
  const shot = scene.shots[shotIndex];

  // 用 useMemo 复用几何体，避免每帧重建（原先每帧 new bufferAttribute 会漏显存）
  const lines = useMemo(
    () =>
      (shot?.cameraKeys ?? []).map((k) => ({
        id: k.id,
        positions: new Float32Array([...k.position, ...k.target]),
      })),
    [shot?.cameraKeys],
  );

  if (!show || !shot) return null;

  return (
    <group>
      {shot.cameraKeys.map((k) => {
        const isSel = selected?.kind === 'camera' && selected.id === k.id;
        const line = lines.find((l) => l.id === k.id);
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
                color={isSel ? '#c8f04a' : '#61d6a0'}
                wireframe={!isSel}
                transparent
                opacity={isSel ? 1 : 0.7}
              />
            </mesh>
            {/*
              只给**选中的**关键帧画指向线。
              未选中的线会横穿整个画面（关键帧常在相机与角色之间），
              实测非常碍眼，而它们并不提供额外信息——小方块本身已标示机位。
            */}
            {isSel && line && (
              /**
               * 用 `<lineSegments>` 而不是 `<line>`：
               * R3F 里小写 `<line>` 会被 React 当成 **SVG 的 line 元素**，
               * 拿不到 `userData`，也不会被加进 Three.js 场景。
               * lineSegments 走 BufferGeometry + 顶点对，语义正确。
               */
              <lineSegments userData={HELPER}>
                <bufferGeometry>
                  <bufferAttribute
                    attach="attributes-position"
                    args={[line.positions, 3]}
                  />
                </bufferGeometry>
                <lineBasicMaterial color="#c8f04a" transparent opacity={0.6} />
              </lineSegments>
            )}
          </group>
        );
      })}
    </group>
  );
}

/** 相机由镜头关键帧驱动 */
function CameraRig() {
  const camRef = useRef<import('three').PerspectiveCamera>(null);
  const scene = usePrevis((s) => s.scene);
  const shotIndex = usePrevis((s) => s.shotIndex);
  const set = useThree((s) => s.set);

  useFrame(() => {
    const cam = camRef.current;
    const shot = scene.shots[shotIndex];
    if (!cam || !shot) return;
    const pose = sampleCamera(shot, usePrevis.getState().playhead);
    cam.position.set(pose.position[0], pose.position[1], pose.position[2]);
    cam.lookAt(pose.target[0], pose.target[1], pose.target[2]);
    cam.fov = pose.fov;
    cam.updateProjectionMatrix();
  });

  // 让 R3F 用这台相机（比 drei 的 makeDefault 少一个依赖面）
  useEffect(() => {
    if (camRef.current) set({ camera: camRef.current });
  }, [set]);

  return <perspectiveCamera ref={camRef} fov={40} near={0.05} far={300} position={[0, 1.6, 4.2]} />;
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
