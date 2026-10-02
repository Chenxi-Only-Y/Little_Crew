/**
 * 人工确认点审核面板（F4）。
 *
 * 工作流在 9 个确认点处 `interrupt()` 暂停；这里把该确认点的内容展示出来，
 * 并提供 **通过 / 重做 / 返回上一步** 三个决策，续跑走 `POST /api/crew/resume`。
 *
 * 关键行为：断点丢失（服务重启过）时后端返回 409，此时**明确告知并引导重新创作**，
 * 而不是假装成功——见 docs/recon/01-usage.md §6.1（此前会静默重放整张图、重复计费）。
 */

import { useState } from 'react';
import { resumeSession, type ReviewDecision } from '../api/sessions';
import { useCanvas } from '../store/canvas';
import { getToken } from '../api/auth';

/** 确认点内容的展示策略：不同类型给不同的可读渲染 */
function renderContent(content: unknown) {
  if (content == null) return <span style={{ color: 'var(--s-violet-mid)' }}>（无内容）</span>;

  if (typeof content === 'string') {
    return <div style={{ whiteSpace: 'pre-wrap' }}>{content}</div>;
  }

  if (Array.isArray(content)) {
    return (
      <ol style={{ margin: '4px 0 0 18px', padding: 0 }}>
        {content.map((it, i) => (
          <li key={i} style={{ marginBottom: 4 }}>
            {typeof it === 'object' ? <KeyVals obj={it as Record<string, unknown>} /> : String(it)}
          </li>
        ))}
      </ol>
    );
  }

  if (typeof content === 'object') {
    return <KeyVals obj={content as Record<string, unknown>} />;
  }
  return <span>{String(content)}</span>;
}

/** 以「键：值」形式平铺展示对象；嵌套对象/数组折叠为一行摘要 */
function KeyVals({ obj }: { obj: Record<string, unknown> }) {
  const entries = Object.entries(obj);
  if (entries.length === 0) {
    return <span style={{ color: 'var(--s-violet-mid)' }}>（空）</span>;
  }
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '2px 10px' }}>
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: 'contents' }}>
          <span style={{ color: 'var(--s-violet-mid)' }}>{labelOf(k)}</span>
          <span style={{ color: 'var(--s-muted)', wordBreak: 'break-word' }}>
            {summarize(v)}
          </span>
        </div>
      ))}
    </div>
  );
}

const LABELS: Record<string, string> = {
  node: '环节',
  review_point: '确认点',
  title: '标题',
  score: '评分',
  strengths: '优点',
  issues: '问题',
  suggestions: '建议',
  summary: '摘要',
  scenes: '场景',
  characters: '角色',
  shots: '镜头',
  images: '画面',
  quality_report: '质检报告',
};

const labelOf = (k: string) => LABELS[k] ?? k;

function summarize(v: unknown): string {
  if (v == null) return '—';
  if (Array.isArray(v)) {
    if (v.length === 0) return '（空）';
    if (v.every((x) => typeof x !== 'object')) return v.join('、');
    return `${v.length} 项`;
  }
  if (typeof v === 'object') {
    const keys = Object.keys(v as object);
    return `{${keys.slice(0, 4).join(', ')}${keys.length > 4 ? '…' : ''}}`;
  }
  return String(v);
}

export default function ReviewPanel() {
  const run = useCanvas((s) => s.run);
  const applyReview = useCanvas((s) => s.applyReview);
  const [feedback, setFeedback] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  if (run.status !== 'waiting_review' || !run.review) return null;

  const review = run.review as Record<string, unknown>;
  const title = String(review.title ?? '人工确认');
  const point = review.review_point;
  const content = review.content;

  const decide = async (decision: ReviewDecision) => {
    if (!run.sessionId || busy) return;
    setBusy(true);
    setMessage(null);
    const fb = decision === 'approve' ? {} : { note: feedback };
    const res = await resumeSession(run.sessionId, decision, fb, getToken());
    setBusy(false);
    if (res.ok) {
      setFeedback('');
      // 交回运行态：清掉 review，SSE 会继续推送后续进度
      applyReview(null);
      setMessage(decision === 'approve' ? '已通过，继续执行…' : '已退回重做…');
    } else if (res.checkpointLost) {
      setMessage(`⚠ ${res.error}`);
    } else {
      setMessage(`✕ ${res.error}`);
    }
  };

  return (
    <div
      style={{
        borderTop: '1px solid var(--st-review)',
        background: 'rgba(255,178,61,0.07)',
        padding: '10px 16px',
        flex: '0 0 auto',
        maxHeight: 260,
        overflowY: 'auto',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
        <span style={{ color: 'var(--st-review)', fontWeight: 600, fontSize: 12 }}>
          ⚑ 待审核
        </span>
        <span style={{ fontSize: 12 }}>{title}</span>
        {point != null && (
          <span style={{ fontSize: 10, color: 'var(--s-violet-mid)' }}>确认点 #{String(point)}</span>
        )}
        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--s-violet-mid)' }}>
          环节 {String(review.node ?? '—')}
        </span>
      </div>

      <div
        style={{
          fontSize: 11,
          lineHeight: 1.7,
          color: 'var(--s-white)',
          background: 'rgba(21,15,35,0.55)',
          border: '1px solid var(--s-hairline)',
          borderRadius: 8,
          padding: '8px 10px',
          marginBottom: 8,
        }}
      >
        {renderContent(content)}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <button className="btn" onClick={() => void decide('approve')} disabled={busy}>
          ✓ 通过并继续
        </button>
        <button className="btn ghost" onClick={() => void decide('redo')} disabled={busy}>
          ↻ 退回重做
        </button>
        <button className="btn ghost" onClick={() => void decide('back')} disabled={busy}>
          ← 返回上一步
        </button>
        <input
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder='重做意见（可选）：例如「冲突再强一点」'
          style={{
            flex: '1 1 240px',
            minWidth: 200,
            background: 'var(--s-white)',
            color: 'var(--s-ink-plum)',
            border: '1px solid var(--s-hairline-cool)',
            borderRadius: 8,
            padding: '6px 10px',
            fontSize: 12,
            fontFamily: 'inherit',
          }}
        />
      </div>

      {message && (
        <div
          style={{
            marginTop: 6,
            fontSize: 11,
            color: message.startsWith('⚠') || message.startsWith('✕')
              ? 'var(--st-failed)'
              : 'var(--s-lime)',
          }}
        >
          {message}
        </div>
      )}
    </div>
  );
}
