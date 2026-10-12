/**
 * 运行控制 —— 启动创作、显示运行状态、实时「AI 思考过程」。
 *
 * 数据流（F3）：
 *   POST /api/sessions  → 拿到 session_id
 *   EventSource         → status / stage / progress / thinking / review / done / error
 *   store.applyEvent    → 驱动画布节点状态与思考流
 */

import { useEffect, useRef, useState } from 'react';
import { subscribeEvents, startSession } from '../api/sessions';
import { useCanvas } from '../store/canvas';
import { getToken } from '../api/auth';

const STATUS_TEXT: Record<string, string> = {
  idle: '未运行',
  running: '运行中',
  waiting_review: '等待审核',
  done: '已完成',
  failed: '失败',
  cancelled: '已停止',
};

export default function RunBar() {
  const run = useCanvas((s) => s.run);
  const beginRun = useCanvas((s) => s.beginRun);
  const applyEvent = useCanvas((s) => s.applyEvent);
  const endRun = useCanvas((s) => s.endRun);
  /**
   * 当前工程名 —— 它决定产出资产落在哪：
   *
   *     账号 / 项目名 / 部门 / 职业 / 会话 / 类型 / 文件
   *
   * 取自画布 store（工程栏那个输入框写的就是它）。没保存过工程时是
   * 「未命名工程」，后端也用同一个兜底名，两边一致。
   */
  const projectName = useCanvas((s) => s.projectName);

  const [input, setInput] = useState('一只会说话的小猫想当侦探');
  const [busy, setBusy] = useState(false);
  const closers = useRef<Array<() => void>>([]);
  /** 防止 StrictMode 下 effect 跑两次导致重复启动 */
  const autoRan = useRef(false);

  // 组件卸载时关闭所有 SSE 连接，避免泄漏
  useEffect(() => {
    return () => {
      for (const close of closers.current) close();
      closers.current = [];
    };
  }, []);

  const onStart = async (textOverride?: string) => {
    const text = (textOverride ?? input).trim();
    if (!text || busy) return;
    setBusy(true);

    // 先关掉旧连接
    for (const close of closers.current) close();
    closers.current = [];

    const res = await startSession(text, getToken(), undefined, projectName);
    if (!res.ok || !res.sessionId) {
      endRun(res.error ?? '启动失败');
      setBusy(false);
      return;
    }
    beginRun(res.sessionId);

    const close = subscribeEvents(
      res.sessionId,
      {
        onEvent: (ev) => {
          applyEvent(ev);
          if (
            ev.type === 'done' ||
            (ev.type === 'status' && ['done', 'failed', 'cancelled'].includes(ev.status ?? ''))
          ) {
            // 终态后关闭连接，避免浏览器反复重连
            for (const c of closers.current) c();
            closers.current = [];
          }
        },
      },
      getToken(),
    );
    closers.current.push(close);
    setBusy(false);
  };

  const onStop = () => {
    for (const close of closers.current) close();
    closers.current = [];
    endRun('已手动断开事件流（后端任务仍在执行）');
  };

  /**
   * 开发期联调入口：`?autorun=<一句话创意>` 打开页面即自动运行一次。
   * 用于端到端验证运行态（正常使用不会触发）。
   *
   * 注意：这里**故意不做 cleanup 取消定时器**——React StrictMode 会
   * mount→unmount→remount，若在 cleanup 里 clearTimeout，第一次挂载排的定时器
   * 会被取消，而 ref 已置位导致不再重排，autorun 就永远不触发。
   */
  useEffect(() => {
    const auto = new URLSearchParams(window.location.search).get('autorun');
    if (!auto || autoRan.current) return;
    autoRan.current = true;
    setTimeout(() => void onStart(auto), 500);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const statusColor: Record<string, string> = {
    idle: 'var(--s-violet-mid)',
    running: 'var(--st-running)',
    waiting_review: 'var(--st-review)',
    done: 'var(--st-done)',
    failed: 'var(--st-failed)',
    cancelled: 'var(--st-skipped)',
  };

  return (
    <>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => e.key === 'Enter' && void onStart()}
        placeholder="一句话创意，例如：一只会说话的小猫想当侦探"
        style={{
          width: 300,
          background: 'var(--s-white)',
          color: 'var(--s-ink-plum)',
          border: '1px solid var(--s-hairline-cool)',
          borderRadius: 8,
          padding: '6px 10px',
          fontSize: 12,
          fontFamily: 'inherit',
        }}
      />
      <button className="btn" onClick={() => void onStart()} disabled={busy || run.status === 'running'}>
        ▶ 运行
      </button>
      {(run.status === 'running' || run.status === 'waiting_review') && (
        <button className="btn ghost" onClick={onStop}>
          ⏹ 停止
        </button>
      )}
      <span style={{ fontSize: 11, color: statusColor[run.status] ?? 'var(--s-muted)' }}>
        ● {STATUS_TEXT[run.status] ?? run.status}
        {run.current ? ` · ${run.current}` : ''}
        {run.sessionId ? ` · ${run.sessionId}` : ''}
      </span>
      {run.error && (
        <span style={{ fontSize: 11, color: 'var(--st-failed)' }} title={run.error}>
          ⚠ {run.error.slice(0, 60)}
        </span>
      )}
    </>
  );
}

/** 「AI 思考过程」实时面板（默认折叠，展开后滚动显示） */
export function ThinkingPanel() {
  const thinking = useCanvas((s) => s.run.thinking);
  const run = useCanvas((s) => s.run);
  const [open, setOpen] = useState(true);
  const boxRef = useRef<HTMLDivElement>(null);

  // 新条目自动滚到底部
  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [thinking.length]);

  if (run.status === 'idle' && thinking.length === 0) return null;

  return (
    <div
      style={{
        borderTop: '1px solid var(--s-hairline)',
        background: 'rgba(21,15,35,0.6)',
        flex: '0 0 auto',
      }}
    >
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          display: 'flex',
          gap: 8,
          alignItems: 'center',
          padding: '6px 16px',
          cursor: 'pointer',
          fontSize: 11,
          color: 'var(--s-muted)',
        }}
      >
        <span style={{ color: 'var(--s-lime)' }}>{open ? '▾' : '▸'}</span>
        AI 思考过程
        <span style={{ color: 'var(--s-violet-mid)' }}>（{thinking.length} 条）</span>
      </div>
      {open && (
        <div
          ref={boxRef}
          style={{
            maxHeight: 150,
            overflowY: 'auto',
            padding: '0 16px 10px',
            fontSize: 11,
            lineHeight: 1.7,
          }}
        >
          {thinking.length === 0 && (
            <div style={{ color: 'var(--s-violet-mid)' }}>等待第一批产出…</div>
          )}
          {thinking.map((t) => (
            <div key={t.seq} style={{ display: 'flex', gap: 8 }}>
              <span style={{ color: 'var(--s-lime)', flex: '0 0 auto', minWidth: 72 }}>
                {t.label}
              </span>
              <span style={{ color: 'var(--s-muted)' }}>{t.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
