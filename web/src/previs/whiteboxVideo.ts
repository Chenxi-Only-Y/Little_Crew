/**
 * 白模参考视频：把预演帧序列交给后端编码成 mp4，并（可选）上传换 `file_id`。
 *
 * 为什么由后端编码而不是浏览器：
 *   浏览器要么用 MediaRecorder（基本只能出 webm，而云端只收 mp4），
 *   要么用 WebCodecs + 自己写 mp4 封装——复杂且兼容性差。
 *   后端一次 ffmpeg 调用可靠得多。
 *
 * 为什么上传是可选项：
 *   上传消耗额度、`file_id` **7 天后过期**。让用户能先只看编码结果，
 *   而不是一按导出就必然产生一次上传。
 */

import { getToken } from '../api/auth';

export interface WhiteboxVideoResult {
  status: string;
  frames: number;
  fps: number;
  seconds: number;
  size_mb: number;
  local_path: string;
  /** 超过参考视频时长上限时的提示（不阻断） */
  warning?: string;
  /** 形如 `mm_file://123456`，可直接作为生成请求的参考视频 */
  reference_video?: string;
  /** `file_id` 有效期（天） */
  expires_in_days?: number;
}

export interface WhiteboxVideoOptions {
  /** 帧，**顺序即播放顺序** */
  frames: Blob[];
  fps?: number;
  /** 是否上传换 `file_id`（默认否） */
  upload?: boolean;
  onProgress?: (stage: string) => void;
  /** 中止（导出的帧可能上百张，用户可能想取消） */
  signal?: AbortSignal;
}

export async function encodeWhiteboxVideo(
  opts: WhiteboxVideoOptions,
): Promise<WhiteboxVideoResult> {
  const form = new FormData();
  // 顺序就是时间顺序——后端按**到达顺序**编号，不看文件名
  for (const b of opts.frames) form.append('frames', b, 'frame.png');
  form.append('fps', String(opts.fps ?? 24));
  if (opts.upload) form.append('upload', '1');

  const token = getToken();
  opts.onProgress?.(opts.upload ? '正在编码并上传…' : '正在编码…');

  const res = await fetch('/api/video/whitebox', {
    method: 'POST',
    // 不要手写 Content-Type：multipart 的 boundary 由浏览器生成
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    body: form,
    signal: opts.signal,
  });

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    // 后端把可执行的修复建议放在 detail 里（例如缺 ffmpeg 时的三步解法），
    // 直接透出让用户看到，不要替换成笼统的"请求失败"
    throw new Error(data?.detail || `编码失败（HTTP ${res.status}）`);
  }
  return data as WhiteboxVideoResult;
}
