/**
 * 注册表来源：优先 `GET /api/registry`，失败则回退到构建时导出的快照。
 *
 * 为什么要快照兜底：
 *   前端启动时通常还没登录 token，而 `/api/registry` 受鉴权保护。
 *   快照由 `scripts/export_registry.py` **从后端注册表直接导出**，
 *   因此不会像手写 fixture 那样与后端漂移。
 */

import type { Registry } from '../domain/registry';
import snapshot from '../registry.snapshot.json';

export type RegistrySource = 'api' | 'snapshot' | 'none';

export interface LoadResult {
  registry: Registry | null;
  source: RegistrySource
  error: string | null;
}

const SNAPSHOT = snapshot as unknown as Registry;

export async function loadRegistry(token?: string | null): Promise<LoadResult> {
  try {
    const res = await fetch('/api/registry', {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    if (!res.ok) {
      return {
        registry: SNAPSHOT,
        source: 'snapshot',
        error: res.status === 401 ? '未登录，已使用内置快照' : `接口 ${res.status}，已使用内置快照`,
      };
    }
    const body = (await res.json()) as { registry: Registry };
    return { registry: body.registry, source: 'api', error: null };
  } catch (err) {
    return {
      registry: SNAPSHOT,
      source: 'snapshot',
      error: `后端不可达（${(err as Error).message}），已使用内置快照`,
    };
  }
}

export { SNAPSHOT as registrySnapshot };
