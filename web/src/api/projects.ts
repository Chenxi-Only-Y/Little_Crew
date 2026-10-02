/**
 * 工程文件（导演台画布）的存取。
 *
 * 后端契约（`app/main.py`）：
 *   GET    /api/projects                 列出当前用户的工程
 *   POST   /api/projects                 保存（带 project_id 则覆盖）
 *   GET    /api/projects/{id}            读取
 *   DELETE /api/projects/{id}            删除
 *   POST   /api/projects/{id}/rename     重命名
 *
 * 注意：密钥**不会**随工程保存（后端写入前掩码），所以工程文件可以放心分享。
 */

export interface ProjectSummary {
  project_id: string;
  name: string;
  node_count: number;
  created: string;
  updated: string;
}

export interface CanvasNodeDoc {
  id: string;
  position: { x: number; y: number };
  config: Record<string, unknown>;
  note: string;
}

export interface CanvasEdgeDoc {
  source: string;
  target: string;
  sourceHandle?: string | null;
  targetHandle?: string | null;
}

export interface CanvasDoc {
  nodes: CanvasNodeDoc[];
  edges: CanvasEdgeDoc[];
  viewport: Record<string, number>;
}

export interface ProjectDoc {
  project_id: string;
  name: string;
  canvas: CanvasDoc;
  settings_snapshot: Record<string, unknown>;
  version: number;
  created: string;
  updated: string;
}

function auth(token?: string | null): HeadersInit {
  return {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export async function listProjects(token?: string | null): Promise<{
  ok: boolean;
  projects: ProjectSummary[];
  error?: string;
}> {
  try {
    const res = await fetch('/api/projects', { headers: auth(token) });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, projects: [], error: body?.detail || `接口 ${res.status}` };
    return { ok: true, projects: (body.projects ?? []) as ProjectSummary[] };
  } catch (err) {
    return { ok: false, projects: [], error: (err as Error).message };
  }
}

export async function saveProject(
  name: string,
  canvas: CanvasDoc,
  settingsSnapshot: Record<string, unknown>,
  token?: string | null,
  projectId?: string | null,
): Promise<{ ok: boolean; projectId?: string; error?: string }> {
  try {
    const res = await fetch('/api/projects', {
      method: 'POST',
      headers: auth(token),
      // 带上 project_id 即为"覆盖保存"，不带则新建
      body: JSON.stringify({
        name,
        canvas,
        settings_snapshot: settingsSnapshot,
        project_id: projectId || undefined,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      // 413 = 体积超限；后端 detail 里已写明原因，直接透出
      return { ok: false, error: body?.detail || body?.message || `接口 ${res.status}` };
    }
    return { ok: true, projectId: body.project_id };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export async function loadProject(
  projectId: string,
  token?: string | null,
): Promise<{ ok: boolean; project?: ProjectDoc; error?: string }> {
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
      headers: auth(token),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, error: body?.detail || `接口 ${res.status}` };
    return { ok: true, project: body.project as ProjectDoc };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export async function deleteProject(
  projectId: string,
  token?: string | null,
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, {
      method: 'DELETE',
      headers: auth(token),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { ok: false, error: body?.detail || `接口 ${res.status}` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export async function renameProject(
  projectId: string,
  name: string,
  token?: string | null,
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(`/api/projects/${encodeURIComponent(projectId)}/rename`, {
      method: 'POST',
      headers: auth(token),
      body: JSON.stringify({ name }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return { ok: false, error: body?.detail || `接口 ${res.status}` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}
