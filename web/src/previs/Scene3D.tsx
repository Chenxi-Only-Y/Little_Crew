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
import {
  NoToneMapping,
  SRGBColorSpace,
  Vector3,
  type Camera,
  type Scene,
  type WebGLRenderer,
} from 'three';
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
import { buildBody, dirToQuaternion, localBounds } from './bodySpecMesh';
import { normalizeBodySpec } from './bodySpec';

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
/**
 * 没有 `bodySpec` 的角色，**默认**长成规格白模，而不是旧骨架。
 *
 * ## 为什么要把默认换掉（一个代价很大的教训）
 *
 * 我先前把规格渲染做成了"有 bodySpec 才走"的分支。结果是：
 *
 *   · 后端没在跑、`DEEPSEEK_API_KEY` 也是空的 → AI 生成必然失败；
 *   · 于是 `bodySpec` 一直是 undefined；
 *   · 于是画面**永远渲染旧骨架**（`HUMAN_SKELETON` 那个"树干"形状）；
 *   · 于是我在规格渲染器里改的一切，用户**一次都没看到**。
 *
 * 用户的原话是"变了吗"——确实没变，因为默认路径根本没走我改的代码。
 * **把新实现放在一个默认不会走到的分支里，等于没做。**
 *
 * 旧骨架保留（`用回代码骨架`按钮仍可用），只是不再是默认。
 * 要改回旧默认，把这个开关置 false 即可。
 */
const SPEC_IS_DEFAULT = true;

/**
 * 默认人形规格（模块级只算一次）。
 *
 * `normalizeBodySpec({})` 会把所有字段补成默认值，并铺上 `DEFAULT_SEGMENTS`
 * 的 16 段体块——所以这就是"一个标准人"。
 *
 * ⚠️ 必须缓存成模块常量：若每次渲染都 `normalizeBodySpec({})` 新建对象，
 * 引用每次变化会让下游（`BodySpecActor` 里 `buildBody` 的结果、
 * React 的 key）白重建一遍几何。
 */
const DEFAULT_BODY_SPEC = normalizeBodySpec({ name: '默认人体' });

function ActorMesh({ id }: { id: string }) {
  const actor = usePrevis((s) => s.scene.actors.find((a) => a.id === id));
  const selected = usePrevis((s) => s.selected);
  const playhead = usePrevis((s) => s.playhead);

  if (!actor) return null;

  /**
   * 有规格 → 用规格；没规格且 `SPEC_IS_DEFAULT` → 用默认人类规格。
   * 都没有（开关关掉且无规格）→ 回退到写死的旧骨架。
   */
  if (actor.bodySpec || SPEC_IS_DEFAULT) {
    return <BodySpecActor id={id} />;
  }

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
          <meshLambertMaterial color={actor.color} />
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
 * 与旧骨架（`ActorMesh` 的后半段）分工：
 *   · 有 `bodySpec`（AI 生成或人工改过）→ 用它；
 *   · 没有 → 用 `normalizeBodySpec({})` 出来的**默认人形**。
 *
 * ⚠️ 没有规格时**不能返回 null**：那样画面就空了。默认人形是一条
 * 完整的规格（`DEFAULT_SEGMENTS` 16 段体块），渲染出来就是一具标准人体。
 * 这也是"AI 没配好也能看到白模"的兜底。
 */
function BodySpecActor({ id }: { id: string }) {
  const actor = usePrevis((s) => s.scene.actors.find((a) => a.id === id));
  const selected = usePrevis((s) => s.selected);
  const playhead = usePrevis((s) => s.playhead);
  if (!actor) return null;

  const spec = actor.bodySpec ?? DEFAULT_BODY_SPEC;
  const isSel = selected?.kind === 'actor' && selected.id === id;
  // ⚠️ 必须传 `actor.pose`：右栏「肢体动作」按钮改的就是它。
  // 只读 `spec.pose` 的话，有规格后那些按钮会完全失效。
  const built = buildBody(spec, { time: playhead, poseOverride: actor.poseOverride, pose: actor.pose });
  /**
   * 两个缩放叠加：
   *   · `actor.scale`  —— 用户拖的"身高倍"
   *   · `built.heightScale` —— 让模型实际高度**等于**规格里写的 `height`
   *     （不乘这个，滑杆写 1.70 会得到 1.77，是明确的欺骗）
   */
  const s = actor.scale * built.heightScale;
  /** 接地：把整组上移，让最低脚掌落在 y=0 */
  const gy = actor.position[1] + built.groundOffset * s;
  /**
   * 碰撞箱：从**实际几何**求，随姿态变化。
   *
   * ⚠️ 先前写死成 `[0.8, height × 1.08, 0.8]`——站起来、坐下、跑步、
   * 举手箱子一样大，既框不住也框太多（用户指出的问题）。
   * 现在由 `localBounds(built)` 逐段累加端点与半径得出。
   */
  const bounds = localBounds(built);

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
            <sphereGeometry args={[r, 16, 12]} />
            <meshLambertMaterial color={actor.color} />
          </mesh>
        );
      })}
      {/* 朝向指示：贴地四棱锥指向 -Z */}
      <mesh position={[0, 0.02, -0.5]} rotation={[-Math.PI / 2, 0, 0]}>
        <coneGeometry args={[0.12, 0.3, 4]} />
        <meshBasicMaterial color="#e0925f" />
      </mesh>
      {isSel && (
        <mesh position={bounds.center} userData={HELPER}>
          <boxGeometry args={[bounds.size[0], bounds.size[1], bounds.size[2]]} />
          <meshBasicMaterial color={PREVIS_COLOR.ACCENT} wireframe transparent opacity={0.3} />
        </mesh>
      )}
    </group>
  );
}

/**
 * 画一个体块。四种形状各自用最合适的 Three.js 内置几何。
 *
 * ⚠️ 朝向**必须**统一走 `dirToQuaternion`。
 *
 * 这里曾经手写了一套 `atan2(√(dx²+dz²), dy)` + `atan2(dx, dz)` 的欧拉角，
 * 与另一处的约定不一致，而且欧拉角**只用两轴无法精确对齐任意方向**
 * （x 与 z 同时非零时有耦合，实测残差 0.15）——结果是朝下的肢体被翻成
 * 朝上、四肢横躺。**一处算错，整具模型散架。**
 *
 * 四元数没有这些问题：误差是机器精度，也不依赖欧拉顺序的约定。
 */
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
  const { seg, a, b, length, dir } = item;
  const mid: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
  const mat = seg.color ?? color;
  /**
   * 朝向用**四元数**（`dirToQuaternion`），不用欧拉角。
   *
   * 欧拉角只用 rx/rz 两轴**无法精确对齐任意方向**（x 与 z 同时非零时
   * 有耦合，实测残差 0.15 → 手脚横躺）。四元数误差是机器精度。
   * 而且它绕开了"欧拉顺序到底是 XYZ 还是 YXZ"这个我错了两次的地方。
   */
  /**
   * 手掌/脚掌的朝向：**长边**要顺着骨方向。
   * 脚掌用 `boxAlign:'z'`（长边是前后），手掌缺省 `'y'`。
   */
  const quat = dirToQuaternion(dir, seg.shape === 'box' ? (seg.boxAlign ?? 'y') : 'y');

  if (seg.shape === 'box') {
    // 手掌/脚掌：一块有长宽高的板，按盒子尺寸画，朝向同段
    const size = seg.size ?? [0.1, 0.1, 0.1];
    return (
      <mesh position={mid} quaternion={quat}>
        <boxGeometry args={[size[0], size[1], size[2]]} />
        <meshLambertMaterial color={mat} />
      </mesh>
    );
  }

  if (seg.shape === 'sphere') {
    const r = Math.max(seg.r0, seg.r1);
    return (
      <mesh position={mid}>
        <sphereGeometry args={[r, 20, 14]} />
        <meshLambertMaterial color={mat} />
      </mesh>
    );
  }

  if (length < 1e-6) return null;

  if (seg.shape === 'taperedCylinder') {
    return (
      <mesh position={mid} quaternion={quat}>
        <cylinderGeometry args={[seg.r1, seg.r0, length, 16]} />
        <meshLambertMaterial color={mat} />
      </mesh>
    );
  }

  /**
   * capsule：Three.js 没有内置胶囊，用「圆柱 + 两端球」拼。
   *
   * ⚠️ 端球的半径要用**各自那一端**的半径（r0 在父端、r1 在子端）。
   * 先前两端都用 `Math.min(r0, r1)`，于是锥度大的段（例如大腿
   * 0.088→0.062）在父端会露出圆柱的平口、看起来像"断开"。
   */
  const cylLen = Math.max(0.001, length - (seg.r0 + seg.r1) / 2);
  const capA: Vec3 = [a[0] + dir[0] * seg.r0, a[1] + dir[1] * seg.r0, a[2] + dir[2] * seg.r0];
  const capB: Vec3 = [b[0] - dir[0] * seg.r1, b[1] - dir[1] * seg.r1, b[2] - dir[2] * seg.r1];
  const capMid: Vec3 = [
    (capA[0] + capB[0]) / 2,
    (capA[1] + capB[1]) / 2,
    (capA[2] + capB[2]) / 2,
  ];
  return (
    <group>
      <mesh position={capMid} quaternion={quat}>
        <cylinderGeometry args={[seg.r1, seg.r0, cylLen, 16]} />
        <meshLambertMaterial color={mat} />
      </mesh>
      <mesh position={a}>
        <sphereGeometry args={[seg.r0, 16, 12]} />
        <meshLambertMaterial color={mat} />
      </mesh>
      <mesh position={b}>
        <sphereGeometry args={[seg.r1, 16, 12]} />
        <meshLambertMaterial color={mat} />
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
      <meshLambertMaterial color={color} />
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
        <meshLambertMaterial color={prop.color} />
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
 * 白模的灯光（用户要求："改光源和着色，针对看不清立体感"）。
 *
 * ## 为什么这里要偏离"无光照"的原始原则
 *
 * `Scene3D.tsx` 开头写的原则是"**纯白模，无材质、无光照着色**"，
 * 理由是"让视频模型看到形状与位置，而不是材质与光影"。
 *
 * 但实测下来，**完全没有明暗时人眼读不出立体感**——白模是一团纯白剪影，
 * 前后重叠的肢体分不开，用户看不出"手在前面还是后面"。这对"摆走位"
 * 是致命的：**看得清比原则重要**。
 *
 * 折中做法：
 *   · 材质改成受光的 `meshLambertMaterial`（保留纯白色，不加贴图）；
 *   · 灯光用**很平的三点光**——主光弱、补光弱、环境光强，
 *     目的是"让法线朝向不同的面有轻微差异"，而不是营造戏剧性光影；
 *   · 仍然**不做阴影**（`castShadow` 全关）：影子会让白模变脏，
 *     而视频模型需要的是干净的体块。
 *
 * ⚠️ 导出用的 `renderer` 是同一个，所以导出的帧**也带这层明暗**。
 * 这是有意的一致（所见即所得）；若将来需要"纯色导出"，应当在这里
 * 按导出开关切换材质，而不是把灯光去掉。
 */
function StageLights() {
  return (
    <>
      {/* 环境光给底，让暗面不至于全黑 */}
      <ambientLight intensity={1.15} color="#ffffff" />
      {/* 主光：右上前方。强度不高，只负责把"朝向不同"区分开 */}
      <directionalLight position={[3.2, 5.0, 4.2]} intensity={0.85} color="#ffffff" />
      {/* 补光：左下后方，补掉主光的死角，避免背光面成为一块死白 */}
      <directionalLight position={[-3.5, 1.6, -2.6]} intensity={0.45} color="#dfe4ff" />
      {/* 顶光：极弱，只用来给头顶/肩部一点层次 */}
      <directionalLight position={[0, 6.0, 0]} intensity={0.3} color="#ffffff" />
    </>
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
    c.minDistance = 0.05;
    c.maxDistance = 500;

    /**
     * ## 去掉垂直锁定（用户报："不是全方位视角"）
     *
     * 我先前写了 `maxPolarAngle = Math.PI * 0.495`，理由是不让相机钻到
     * 地面以下。但那等于把**垂直方向锁死**——只能从上方俯视，
     * 没法转到下方仰视，用户感受就是"只能绕着中心点转，不是自由视角"。
     *
     * 现在放开：极角 0~π 全允许（含正上方与正下方），
     * 想不穿地看就自己往上转，不该由控制强制。
     * 方位角本来就是 360°（没设 min/maxAzimuthAngle）。
     */
    c.minPolarAngle = 0;
    c.maxPolarAngle = Math.PI;

    // 平移不要有边界，否则把场景挪出视野就拉不回来
    c.enablePan = true;
    return c;
  }, [target, gl]);

  /**
   * ## 自由飞行（WASD / QE / 方向键）
   *
   * 只有"绕中心点转 + 平移"其实仍然不自由：**没法把视点本身挪到别处**。
   * 这段键盘控制让相机连同它的注视点一起平移，于是可以走进场景内部、
   * 绕到任意物体背后去看——那才是真正意义上的自由视角。
   *
   * 速度按"到目标点的距离"缩放：离得远就快、贴近了就慢，
   * 否则近处动一下就跑飞、远处又慢得推不动。
   */
  useEffect(() => {
    if (!controls) return;
    const cam = controls.object;
    const keys = new Set<string>();
    /**
     * `Shift` 不靠 `keys` 记录：它的 `e.key` 是 `'Shift'`，
     * 而按住 Shift 再按别的键时，某些键盘布局/输入法会先丢 keyup。
     * 直接读 `e.shiftKey` 最可靠。
     */
    let slowModifier = false;
    const down = (e: KeyboardEvent) => {
      // 焦点在输入框里时不要抢键（右栏有很多滑杆和文本框）
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
      slowModifier = e.shiftKey;
      keys.add(e.key.toLowerCase());
    };
    const up = (e: KeyboardEvent) => {
      slowModifier = e.shiftKey;
      keys.delete(e.key.toLowerCase());
    };
    const blur = () => {
      keys.clear();
      slowModifier = false;
    };

    const fwd = new Vector3();
    const right = new Vector3();
    const move = new Vector3();

    /**
     * ## 飞行速度（返工过一次："太快了"）
     *
     * 第一版是 `step = dist * 0.04`，**按帧算**：60fps 下等于每秒移动
     * 2.4 倍距离——11 米外每秒跑 26 米，用户反馈"太快了"。
     *
     * 现在**按时间算**（不受帧率影响），基准压到人走路量级：
     *
     *     速度 = 到注视点的距离 × 0.22 米/秒
     *     步长 = 速度 × 本帧耗时
     *
     * 于是 5 米处约 1.1 米/秒（接近步行）、15 米处 3.3 米/秒：
     * 远处快速赶路、近处自然放慢。按住 `Shift` 再降一半，便于微调。
     */
    const SPEED_PER_METER = 0.22;
    /** 单帧步长上限（米）：防掉帧时"跳一大步"直接穿过物体 */
    const MAX_STEP = 0.2;
    let last = performance.now();

    const tick = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);   // 卡顿时按 0.1 秒封顶
      last = now;
      if (keys.size === 0) return;

      const dist = cam.position.distanceTo(controls.target);
      const slow = slowModifier ? 0.45 : 1;
      const step = Math.min(MAX_STEP, dist * SPEED_PER_METER * dt * slow);
      // 相机的"前"用注视方向，保证 WASD 与视线一致
      fwd.subVectors(controls.target, cam.position).normalize();
      right.crossVectors(fwd, cam.up).normalize();

      move.set(0, 0, 0);
      if (keys.has('w') || keys.has('arrowup')) move.addScaledVector(fwd, step);
      if (keys.has('s') || keys.has('arrowdown')) move.addScaledVector(fwd, -step);
      if (keys.has('d') || keys.has('arrowright')) move.addScaledVector(right, step);
      if (keys.has('a') || keys.has('arrowleft')) move.addScaledVector(right, -step);
      if (keys.has('e') || keys.has(' ')) move.y += step;
      if (keys.has('q')) move.y -= step;

      if (move.lengthSq() > 0) {
        // 相机与注视点一起移动：等于"整个人走过去看"，不改变朝向
        cam.position.add(move);
        controls.target.add(move);
      }
    };

    window.addEventListener('keydown', down);
    window.addEventListener('keyup', up);
    window.addEventListener('blur', blur);
    // 用 rAF 而不是 useFrame：`controls.update()` 已经在 useFrame 里跑，
    // 这里只负责根据按键改位置，频率不需要与渲染对齐。
    let raf = 0;
    const loop = (now: number) => {
      tick(now);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('keydown', down);
      window.removeEventListener('keyup', up);
      window.removeEventListener('blur', blur);
    };
  }, [controls]);

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
      <StageLights />
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
