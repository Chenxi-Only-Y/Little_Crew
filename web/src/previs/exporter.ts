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

/** 自动化测试入口：导出函数挂到 window（配合 __previsThree 做端到端验证） */
declare global {
  interface Window {
    __previsExporter?: { exportShot: typeof exportShot; dataUrlToBlob: typeof dataUrlToBlob };
  }
}
if (typeof window !== 'undefined') {
  window.__previsExporter = { exportShot, dataUrlToBlob };
}
