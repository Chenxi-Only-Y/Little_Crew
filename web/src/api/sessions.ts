/**
 * 会话与实时事件（SSE）客户端。
 *
 * 后端契约见 `app/main.py`：
 *   POST /api/sessions                 启动一次创作 → {session_id}
 *   GET  /api/sessions/{id}/events     SSE：status / stage / thinking / review / progress / done / error
 *   GET  /api/sessions/{id}/events/snapshot   已发生事件（排查用）
 */

export type EventType =
  | 'status'
  | 'stage'
  | 'thinking'
  | 'review'
  | 'progress'
  | 'done'
  | 'error';

export interface RunEvent {
  type: EventType;
  session_id: string;
  ts: number;
  seq: number;
  /* 各类型载荷 */
  status?: string;
  stage?: string;
  phase?: string;
  nodes?: Record<string, string>;
  current?: string | null;
  label?: string;
  text?: string;
  review?: unknown;
  message?: string;
}

export interface StartResult {
  ok: boolean;
  sessionId?: string;
  error?: string;
}

/** 审批决策：通过 / 重做（回到上一环节，可带反馈） */
export type ReviewDecision = 'approve' | 'redo' | 'back';

export interface ResumeResult {
  ok: boolean;
  error?: string;
  /** 断点已丢失（服务重启过）——后端返回 409，此时应引导重新创作 */
  checkpointLost?: boolean;
}

/**
 * 对人工确认点做出决策并继续运行。
 *
 * 后端契约（`POST /api/crew/resume`）：
 *   200 → {status:"accepted"}
 *   409 → 断点丢失（服务可能重启过）——**不会静默重放整张图**，
 *         见 docs/recon/01-usage.md §6.1
 */
export async function resumeSession(
  sessionId: string,
  decision: ReviewDecision,
  feedback: Record<string, unknown> = {},
  token?: string | null,
): Promise<ResumeResult> {
  try {
    const res = await fetch('/api/crew/resume', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ session_id: sessionId, decision, feedback }),
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 409) {
      return { ok: false, error: body?.message || '工作流断点已丢失', checkpointLost: true };
    }
    if (!res.ok) {
      return { ok: false, error: body?.message || `接口 ${res.status}` };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

/**
 * 启动一次创作。
 *
 * `project` 是**工程名**（导演台工程栏那个）。它决定产出资产落在哪：
 *
 *     账号 / 项目名 / 部门 / 职业 / 会话 / 类型 / 文件
 *
 * 不带的话后端会用「未命名工程」兜底——那也得有地方落盘，
 * 而且统一叫这个名字，事后一眼能认出"这批没归到任何工程"。
 */
export async function startSession(
  userInput: string,
  token?: string | null,
  seed?: Record<string, unknown>,
  project?: string | null,
): Promise<StartResult> {
  try {
    const res = await fetch('/api/sessions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        user_input: userInput,
        ...(seed ? { seed } : {}),
        ...(project ? { project } : {}),
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      return { ok: false, error: body?.message || `接口 ${res.status}` };
    }
    return { ok: true, sessionId: body.session_id };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export interface StreamHandlers {
  onEvent: (ev: RunEvent) => void;
  /** 命名为 onIssue 而非 onError：参数是 RunEvent，不要用 `Event` 当形参名遮蔽 DOM 全局类型 */
  onIssue?: (ev: RunEvent) => void;
  onOpen?: () => void;
}

/**
 * 订阅实时事件流。
 *
 * 鉴权：浏览器 `EventSource` 不能自定义请求头，故 token 走查询参数
 * （后端同时支持 Authorization 头，见 `/api/sessions/{id}/events` 的说明）。
 *
 * Returns:
 *   关闭函数。调用方负责在组件卸载/切换会话时调用，避免连接泄漏。
 */
export function subscribeEvents(
  sessionId: string,
  handlers: StreamHandlers,
  token?: string | null,
): () => void {
  const qs = token ? `?token=${encodeURIComponent(token)}` : '';
  const es = new EventSource(`/api/sessions/${sessionId}/events${qs}`);

  // 后端用 event: <type> 分帧，故需按类型分别监听
  const TYPES: EventType[] = ['status', 'stage', 'thinking', 'review', 'progress', 'done', 'error'];
  const listeners: Array<[string, (e: MessageEvent) => void]> = [];

  for (const t of TYPES) {
    const fn = (e: MessageEvent) => {
      let parsed: RunEvent;
      try {
        parsed = JSON.parse(e.data) as RunEvent;
      } catch {
        return;
      }
      if (parsed.type === 'error') handlers.onIssue?.(parsed);
      handlers.onEvent(parsed);
    };
    es.addEventListener(t, fn as EventListener);
    listeners.push([t, fn]);
  }

  es.onopen = () => handlers.onOpen?.();
  // EventSource 自带重连；这里只把连接错误交给调用方展示
  es.onerror = () => {
    handlers.onIssue?.({
      type: 'error',
      session_id: sessionId,
      ts: Date.now() / 1000,
      seq: -1,
      message: '事件流连接中断（浏览器会自动重连）',
    });
  };

  return () => {
    for (const [t, fn] of listeners) es.removeEventListener(t, fn as EventListener);
    es.close();
  };
}
