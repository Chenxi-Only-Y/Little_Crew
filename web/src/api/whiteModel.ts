/**
 * 白模生成 API。
 *
 * 后端按角色描述让 LLM 产出一份**规格（JSON）**，前端据此渲染体块。
 * 为什么让模型生成 JSON 而不是 Three.js 代码：见
 * `app/services/white_model.py` 开头的对照表。
 */

import { getToken } from './auth';
import { normalizeBodySpec } from '../previs/bodySpec';
import type { WhiteModelSpec } from '../previs/types';

export interface GenerateOptions {
  description: string;
  name?: string;
  pose?: string;
  heightHint?: string;
}

export interface GenerateResult {
  ok: boolean;
  spec?: WhiteModelSpec;
  /** 用了几次尝试（1 = 一次通过，2 = 带上错误重试后通过） */
  attempts?: number;
  error?: string;
}

function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/**
 * 生成一份白模规格。
 *
 * ⚠️ 即使后端返回 200 也要再 `normalizeBodySpec` 一次：
 * 工程文件是用户可编辑的（C 模式能直接改 JSON），前端不能假定
 * 拿到的东西一定合法——**前端自己也要能站住**。
 */
export async function generateWhiteModel(opts: GenerateOptions): Promise<GenerateResult> {
  const description = opts.description.trim();
  if (!description) return { ok: false, error: '请先描述一下这个角色' };

  try {
    const res = await fetch('/api/previs/white-model', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        description,
        name: opts.name ?? '',
        pose: opts.pose ?? '',
        heightHint: opts.heightHint ?? '',
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      // 后端把可读原因放在 detail；400 是"模型没给出合规格的东西"
      return { ok: false, error: String(body?.detail || `请求失败（${res.status}）`) };
    }
    if (!body?.spec) return { ok: false, error: '后端没有返回规格' };
    return {
      ok: true,
      spec: normalizeBodySpec(body.spec),
      attempts: typeof body.attempts === 'number' ? body.attempts : undefined,
    };
  } catch (err) {
    return { ok: false, error: `无法连接后端：${(err as Error).message}` };
  }
}
