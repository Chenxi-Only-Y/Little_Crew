/**
 * 白模帧序列导出 —— 直接调用原生 Three.js API。
 *
 * 为什么不用 canvas.toDataURL()：
 *     WebGL 的绘制缓冲在合成后可能被清空，`toDataURL` 会拿到全黑图。
 *     可靠做法是用 `gl.readRenderTargetPixels` / `readPixels` 主动读回像素，
 *     再自己编码 PNG。这样也不依赖浏览器的编码行为，导出结果可预测。
 *
 * 为什么自己写 PNG 编码（而不是引 pngjs 之类）：
 *     PNG 的不可压缩核心就是「IHDR + zlib(IDAT) + IEND」三个块，
 *     而 `CompressionStream('deflate')` 是浏览器内置的 zlib，
 *     所以不到 80 行就能写对，不必再加一个依赖。
 *
 * 导出时会**隐藏相机辅助标记与地面网格**——它们只服务于编辑，
 * 若留在画面里会被视频模型当成真实结构（网格线尤其有害）。
 */

import type { Camera, PerspectiveCamera, Scene, WebGLRenderer } from 'three';
import { Vector2 } from 'three';
import { sampleCamera, type PrevisScene } from './types';

export interface ExportFrame {
  /** 帧序号（从 0 开始） */
  index: number;
  /** 该帧对应的秒数 */
  time: number;
  /** PNG 的 data URL */
  dataUrl: string;
}

export interface ExportResult {
  frames: ExportFrame[];
  width: number;
  height: number;
  fps: number;
  duration: number;
}

/* ------------------------------------------------------------------ */
/* PNG 编码                                                            */
/* ------------------------------------------------------------------ */

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const dv = new DataView(out.buffer);
  dv.setUint32(0, data.length);
  for (let i = 0; i < 4; i += 1) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  const crcInput = out.subarray(4, 8 + data.length);
  dv.setUint32(8 + data.length, crc32(crcInput));
  return out;
}

async function deflate(bytes: Uint8Array): Promise<Uint8Array> {
  // 浏览器内置 zlib；Safari 16.4+ / Chrome 80+ / Firefox 113+ 支持
  const cs = new CompressionStream('deflate');
  const stream = new Blob([bytes as unknown as BlobPart]).stream().pipeThrough(cs);
  const buf = await new Response(stream).arrayBuffer();
  return new Uint8Array(buf);
}

/** 把 RGBA 像素编码成 PNG（data URL） */
async function encodePng(
  rgba: Uint8Array,
  width: number,
  height: number,
): Promise<string> {
  // 每行前加 1 字节 filter（0 = None）
  const stride = width * 4;
  const raw = new Uint8Array((stride + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (stride + 1)] = 0;
    raw.set(rgba.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1);
  }

  const ihdr = new Uint8Array(13);
  const dv = new DataView(ihdr.buffer);
  dv.setUint32(0, width);
  dv.setUint32(4, height);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // color type: RGBA
  ihdr[10] = 0; // compression
  ihdr[11] = 0; // filter
  ihdr[12] = 0; // interlace

  const idat = await deflate(raw);
  const sig = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const parts = [sig, chunk('IHDR', ihdr), chunk('IDAT', idat), chunk('IEND', new Uint8Array(0))];
  const total = parts.reduce((n, p) => n + p.length, 0);
  const png = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    png.set(p, off);
    off += p.length;
  }

  // 分块转 base64，避免 apply 参数过多导致栈溢出
  let bin = '';
  const CH = 0x8000;
  for (let i = 0; i < png.length; i += CH) {
    bin += String.fromCharCode(...png.subarray(i, i + CH));
  }
  return `data:image/png;base64,${btoa(bin)}`;
}

/* ------------------------------------------------------------------ */
/* 导出                                                                */
/* ------------------------------------------------------------------ */

/** 需要临时隐藏的辅助对象（按名字标记，见 Scene3D 的 `userData.helper`） */
function setHelpersVisible(scene: Scene, visible: boolean): Array<[object, boolean]> {
  const touched: Array<[object, boolean]> = [];
  scene.traverse((o) => {
    if (o.userData && o.userData.helper) {
      touched.push([o, o.visible]);
      o.visible = visible;
    }
  });
  return touched;
}

function restore(touched: Array<[object, boolean]>) {
  for (const [obj, v] of touched) {
    (obj as { visible: boolean }).visible = v;
  }
}

export interface ExportOptions {
  width?: number;
  height?: number;
  /** 只导出某一帧（用于"导出首帧/尾帧"） */
  only?: number[];
  onProgress?: (done: number, total: number) => void;
}

/**
 * 按镜头渲染并导出帧序列。
 *
 * 相机姿态在每个采样时刻由 `sampleCamera` 重新求值并直接写入相机，
 * **不依赖 React 渲染循环**——渲染循环的帧率与片段 fps 无关，
 * 靠它推进会导致丢帧或重复帧。
 */
export async function exportShot(
  renderer: WebGLRenderer,
  threeScene: Scene,
  camera: Camera,
  previsScene: PrevisScene,
  shotIndex: number,
  opts: ExportOptions = {},
): Promise<ExportResult> {
  const shot = previsScene.shots[shotIndex];
  if (!shot) throw new Error('没有可导出的镜头');

  const width = opts.width ?? 832;
  const height = opts.height ?? 480;
  const fps = shot.fps || 24;
  const total = Math.max(1, Math.round(shot.duration * fps));

  const prevSize = renderer.getSize(new Vector2());
  const prevRatio = renderer.getPixelRatio();
  renderer.setPixelRatio(1);
  renderer.setSize(width, height, false);

  const hidden = setHelpersVisible(threeScene, false);
  const buffer = new Uint8Array(width * height * 4);
  const frames: ExportFrame[] = [];

  try {
    const indices = opts.only ?? Array.from({ length: total }, (_, i) => i);
    for (let n = 0; n < indices.length; n += 1) {
      const i = indices[n];
      const time = (i / fps) * 1;
      const pose = sampleCamera(shot, time);
      camera.position.set(pose.position[0], pose.position[1], pose.position[2]);
      camera.lookAt(pose.target[0], pose.target[1], pose.target[2]);
      // 只有透视相机有 fov；正交相机跳过（导出仍可用，只是不受 fov 关键帧影响）
      if ((camera as PerspectiveCamera).isPerspectiveCamera) {
        const pc = camera as PerspectiveCamera;
        pc.fov = pose.fov;
        pc.updateProjectionMatrix();
      }

      renderer.render(threeScene, camera);
      // 从 framebuffer 读回像素（y 轴是反的，下面翻转）
      const gl = renderer.getContext();
      gl.readPixels(0, 0, width, height, gl.RGBA, gl.UNSIGNED_BYTE, buffer);

      const flipped = new Uint8Array(buffer.length);
      const stride = width * 4;
      for (let y = 0; y < height; y += 1) {
        flipped.set(
          buffer.subarray((height - 1 - y) * stride, (height - y) * stride),
          y * stride,
        );
      }

      frames.push({ index: i, time, dataUrl: await encodePng(flipped, width, height) });
      opts.onProgress?.(n + 1, indices.length);
    }
  } finally {
    restore(hidden);
    renderer.setPixelRatio(prevRatio);
    if (prevSize && prevSize.x) renderer.setSize(prevSize.x, prevSize.y, false);
  }

  return { frames, width, height, fps, duration: shot.duration };
}

/** 把 data URL 转成 Blob（用于上传） */
export function dataUrlToBlob(dataUrl: string): Blob {
  const [head, b64] = dataUrl.split(',');
  const mime = /:(.*?);/.exec(head)?.[1] ?? 'image/png';
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes as unknown as BlobPart], { type: mime });
}

/* ------------------------------------------------------------------ */
/* 深度图导出                                                          */
/* ------------------------------------------------------------------ */

export type DepthPolarity = 'near_white' | 'far_white';

export interface DepthOptions {
  width?: number;
  height?: number;
  /** near_white = 近处亮（常见 depth ControlNet 约定）；far_white = 远处亮 */
  polarity?: DepthPolarity;
  /**
   * 是否把地面计入深度。
   *
   * 默认 **false**。依据 ControlNet-Union 作者实测：无细节的光滑地面在深度图上
   * 只是一片渐变，而 ControlNet 会坚持"这些表面是光滑的"，从而压制提示词要求的纹理。
   * 需要地面参与空间关系时再打开。
   */
  includeGround?: boolean;
  onProgress?: (done: number, total: number) => void;
}

interface Tri {
  /** 三个顶点的裁剪/相机空间坐标 */
  v: [number, number, number][];
  /** 面法线（世界空间），用于背面剔除 */
  n: [number, number, number];
}

/**
 * 从场景几何体收集三角面（世界坐标）。
 *
 * 为什么 CPU 算深度而不是再写一套深度渲染器：
 *   白模全是**不透明实体几何**，没有透明、没有纹理、没有着色器分支，
 *   所以"遍历三角面 + 加权 z-buffer"就是这个场景的精确算法。
 *   再写一套 GPU 管线（自定义 depth material + 第二 renderer）成本更高，
 *   而且要维护两条渲染路径，容易出现"预览与导出不一致"。
 */
function collectTriangles(
  root: import('three').Object3D,
  camera: Camera,
  includeGround: boolean,
) {
  const tris: Tri[] = [];
  // 相机视图矩阵：camera.matrixWorldInverse 由 updateMatrixWorld 维护
  const view = camera.matrixWorldInverse;

  root.updateMatrixWorld(true);

  root.traverse((obj) => {
    const mesh = obj as import('three').Mesh;
    if (!mesh.isMesh) return;

    // 跳过辅助对象与不可见对象（与渲染保持一致）
    let p: import('three').Object3D | null = obj;
    while (p) {
      if (!p.visible) return;
      if (p.userData && p.userData.helper) return;
      // 地面用 `userData.ground` 区分于普通 helper：
      // 剪影/渲染时它要被隐藏，但深度图里"是否计入"是可选项
      if (!includeGround && p.userData && p.userData.ground) return;
      p = p.parent;
    }

    const geo = mesh.geometry as import('three').BufferGeometry;
    const posAttr = geo.getAttribute('position');
    if (!posAttr) return;
    const idx = geo.getIndex();

    const m = mesh.matrixWorld;

    const vertexAt = (i: number): [number, number, number] => {
      const x = posAttr.getX(i);
      const y = posAttr.getY(i);
      const z = posAttr.getZ(i);
      // 世界坐标
      const e = m.elements;
      const wx = e[0] * x + e[4] * y + e[8] * z + e[12];
      const wy = e[1] * x + e[5] * y + e[9] * z + e[13];
      const wz = e[2] * x + e[6] * y + e[10] * z + e[14];
      // 相机空间（视图矩阵）
      const ve = view.elements;
      const cx = ve[0] * wx + ve[4] * wy + ve[8] * wz + ve[12];
      const cy = ve[1] * wx + ve[5] * wy + ve[9] * wz + ve[13];
      const cz = ve[2] * wx + ve[6] * wy + ve[10] * wz + ve[14];
      return [cx, cy, cz];
    };

    const emit = (a: number, b: number, c: number) => {
      const va = vertexAt(a);
      const vb = vertexAt(b);
      const vc = vertexAt(c);
      // 背面剔除（相机空间里判断朝向）：法线朝相机才画
      const ux = vb[0] - va[0], uy = vb[1] - va[1], uz = vb[2] - va[2];
      const wx = vc[0] - va[0], wy = vc[1] - va[1], wz = vc[2] - va[2];
      const nx = uy * wz - uz * wy;
      const ny = uz * wx - ux * wz;
      const nz = ux * wy - uy * wx;
      // 相机看向 -Z，所以"面向相机"= 法线 z 分量为正
      if (nz <= 0) return;
      tris.push({ v: [va, vb, vc], n: [nx, ny, nz] });
    };

    if (idx) {
      for (let i = 0; i < idx.count; i += 3) {
        emit(idx.getX(i), idx.getX(i + 1), idx.getX(i + 2));
      }
    } else {
      for (let i = 0; i < posAttr.count; i += 3) {
        emit(i, i + 1, i + 2);
      }
    }
  });

  return tris;
}

/**
 * 导出一帧深度图。
 *
 * 实现：把所有三角面投到屏幕、按深度做加权 z-buffer 填充。
 * 深度用**透视校正的 1/z** 线性映射到 0-255（近处大），
 * 这样近远层次均匀，不会像直接线性映射那样把远处挤成一片黑。
 */
export function renderDepthFrame(
  threeScene: Scene,
  camera: Camera,
  width: number,
  height: number,
  polarity: DepthPolarity = 'near_white',
  includeGround = false,
): Uint8Array {
  camera.updateMatrixWorld(true);
  (camera as unknown as { updateProjectionMatrix: () => void }).updateProjectionMatrix();
  camera.updateMatrixWorld(true);
  // 视图矩阵需要手动同步（renderer.render 会做，这里不经过 renderer）
  camera.matrixWorldInverse.copy(camera.matrixWorld).invert();

  const tris = collectTriangles(threeScene, camera, includeGround);

  const depth = new Float32Array(width * height).fill(Infinity);
  const proj = camera.projectionMatrix;

  const project = (v: [number, number, number]) => {
    const e = proj.elements;
    const x = e[0] * v[0] + e[4] * v[1] + e[8] * v[2] + e[12];
    const y = e[1] * v[0] + e[5] * v[1] + e[9] * v[2] + e[13];
    const w = e[3] * v[0] + e[7] * v[1] + e[11] * v[2] + e[15];
    if (w <= 1e-6) return null;
    const sx = ((x / w) * 0.5 + 0.5) * width;
    const sy = (1 - ((y / w) * 0.5 + 0.5)) * height;
    return { sx, sy, w };
  };

  for (const t of tris) {
    const p = t.v.map(project);
    if (p.some((q) => q === null)) continue;
    const [a, b, c] = p as { sx: number; sy: number; w: number }[];
    const minX = Math.max(0, Math.floor(Math.min(a.sx, b.sx, c.sx)));
    const maxX = Math.min(width - 1, Math.ceil(Math.max(a.sx, b.sx, c.sx)));
    const minY = Math.max(0, Math.floor(Math.min(a.sy, b.sy, c.sy)));
    const maxY = Math.min(height - 1, Math.ceil(Math.max(a.sy, b.sy, c.sy)));
    const area = (b.sx - a.sx) * (c.sy - a.sy) - (c.sx - a.sx) * (b.sy - a.sy);
    if (Math.abs(area) < 1e-9) continue;

    for (let y = minY; y <= maxY; y += 1) {
      for (let x = minX; x <= maxX; x += 1) {
        const px = x + 0.5;
        const py = y + 0.5;
        const w0 = ((b.sx - a.sx) * (py - a.sy) - (px - a.sx) * (b.sy - a.sy)) / area;
        const w1 = ((px - a.sx) * (c.sy - a.sy) - (c.sx - a.sx) * (py - a.sy)) / area;
        const w2 = 1 - w0 - w1;
        if (w0 < 0 || w1 < 0 || w2 < 0) continue;
        // 透视校正插值 1/w
        const invW = w2 / a.w + w1 / b.w + w0 / c.w;
        const z = 1 / invW;
        const i = y * width + x;
        if (z < depth[i]) depth[i] = z;
      }
    }
  }

  // 映射到 0-255。
  //
  // ⚠️ 第一版按**相机 near/far** 归一化，结果整幅图 max=3（几乎全黑）。
  // 根因：near=0.05 是防穿模用的极端值，1/near=20，而场景实际深度跨度只有约 4，
  // 于是 (1/z - 1/far)/(1/near - 1/far) ≈ 0.012，被压扁到最低几档。
  // 改为按**本帧实际出现的深度范围**归一化——自适应任何场景尺度，
  // 且保证 0-255 被用满，不会因 near/far 取值不当而浪费动态范围。
  let dMin = Infinity;
  let dMax = -Infinity;
  for (let i = 0; i < depth.length; i += 1) {
    const d = depth[i];
    if (Number.isFinite(d)) {
      if (d < dMin) dMin = d;
      if (d > dMax) dMax = d;
    }
  }

  const out = new Uint8Array(width * height * 4);
  const hasSurface = Number.isFinite(dMin) && dMax > dMin;
  // 用 1/z 而非 z 做插值：近远层次更均匀（远处不会挤成一片黑）
  const invMin = hasSurface ? 1 / dMin : 0;   // 最近 → 最大
  const invMax = hasSurface ? 1 / dMax : 0;
  const span = invMin - invMax;

  for (let i = 0; i < width * height; i += 1) {
    const d = depth[i];
    let v = 0;
    if (hasSurface && Number.isFinite(d) && d > 0) {
      const t = (1 / d - invMax) / span;      // 0 = 最远，1 = 最近
      v = Math.max(0, Math.min(1, t));
      if (polarity === 'far_white') v = 1 - v;
    }
    const px = Math.round(v * 255);
    out[i * 4] = px;
    out[i * 4 + 1] = px;
    out[i * 4 + 2] = px;
    out[i * 4 + 3] = 255;
  }
  return out;
}

export interface DepthResult {
  frames: ExportFrame[];
  width: number;
  height: number;
  fps: number;
  duration: number;
  polarity: DepthPolarity;
  includeGround: boolean;
}

/** 导出深度序列（与 exportShot 并行的一套，共用 PNG 编码） */
export async function exportDepth(
  threeScene: Scene,
  camera: Camera,
  previsScene: PrevisScene,
  shotIndex: number,
  opts: DepthOptions = {},
): Promise<DepthResult> {
  const shot = previsScene.shots[shotIndex];
  if (!shot) throw new Error('没有可导出的镜头');

  const width = opts.width ?? 832;
  const height = opts.height ?? 480;
  const polarity = opts.polarity ?? 'near_white';
  const includeGround = opts.includeGround ?? false;
  const fps = shot.fps || 24;
  const total = Math.max(1, Math.round(shot.duration * fps));

  const frames: ExportFrame[] = [];
  for (let i = 0; i < total; i += 1) {
    const time = i / fps;
    const pose = sampleCamera(shot, time);
    camera.position.set(pose.position[0], pose.position[1], pose.position[2]);
    camera.lookAt(pose.target[0], pose.target[1], pose.target[2]);
    if ((camera as import('three').PerspectiveCamera).isPerspectiveCamera) {
      const pc = camera as import('three').PerspectiveCamera;
      pc.fov = pose.fov;
      pc.updateProjectionMatrix();
    }
    camera.updateMatrixWorld(true);
    const rgba = renderDepthFrame(threeScene, camera, width, height, polarity, includeGround);
    frames.push({ index: i, time, dataUrl: await encodePng(rgba, width, height) });
    opts.onProgress?.(i + 1, total);
  }

  return {
    frames, width, height, fps, duration: shot.duration, polarity, includeGround,
  };
}

/** 自动化测试入口：导出函数挂到 window（配合 __previsThree 做端到端验证） */
declare global {
  interface Window {
    __previsExporter?: {
      exportShot: typeof exportShot;
      exportDepth: typeof exportDepth;
      renderDepthFrame: typeof renderDepthFrame;
      dataUrlToBlob: typeof dataUrlToBlob;
    };
  }
}
if (typeof window !== 'undefined') {
  window.__previsExporter = { exportShot, exportDepth, renderDepthFrame, dataUrlToBlob };
}
