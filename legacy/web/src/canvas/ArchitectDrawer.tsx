/**
 * 工作流架构师抽屉 —— 在**导演台画布**上改工作流（用户选的就是"放画布上"）。
 *
 * ## 为什么在这里
 *
 * `docs/recon/05-roles-v2.md` 里对"工作流架构师"的定位原文是：
 *
 *     "要把「图」变成一等对象：节点接口/schema、拓扑校验、版本化、可视化编辑。
 *      **这是导演台画布的后端基座**"
 *
 * 所以入口放在导演台的状态栏上（"拓扑可执行"那个按钮旁边），逻辑自洽。
 *
 * ## 交互流程（与后端闸门一一对应）
 *
 *     输入诉求 → 提提案 → 看预演（改了什么 / 两道校验过没过）
 *        → 删节点时必须**二次确认**（勾选后才允许点"批准并生效"）
 *        → 批准 → 后端换图 + 自动生成代码补丁 → 可查看/下载补丁
 *        → 不满意可以回滚
 *
 * ## 刻意做的两件事
 *
 * 1. **不伪装**：提案不合法时（校验不过），界面**不给**"批准"按钮，
 *    而是把问题列出来 —— 否则用户点了才发现被拒。
 * 2. **把"运行时生效"和"落成代码"分开说**：A2 换的内存里的图（重启失效），
 *    A3 是补丁（要人工 merge 才永久）。这一点不写清楚，用户会以为改完就永久了。
 */

import { useCallback, useEffect, useState } from 'react';
import { getToken } from '../api/auth';

interface PreviewProblem {
  kind: string;
  message: string;
  where?: string;
}

interface ProposalView {
  id: string;
  summary?: string;
  author?: string;
  status?: string;
  needs_second_confirm?: boolean;
  removes_nodes?: string[];
  ops?: unknown[];
  risks?: string[];
  graph_version?: number;
  patch_path?: string;
  preview?: {
    ok?: boolean;
    changes?: string[];
    problems?: PreviewProblem[];
    before?: Record<string, number>;
    after?: Record<string, number>;
  };
  events?: Array<{ event: string; actor?: string }>;
}

/** 统一带鉴权的请求（本项目其它地方也是这个写法）。 */
async function api(path: string, init?: RequestInit) {
  const token = getToken();
  const r = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
}

const C = {
  panel: 'var(--s-panel, #16101f)',
  line: 'var(--s-hairline, #2a2140)',
  text: 'var(--s-white, #ede9f5)',
  muted: 'var(--s-violet-mid, #9b8bbd)',
  ok: 'var(--st-done, #4ade80)',
  warn: 'var(--st-review, #ffb23d)',
  err: 'var(--st-failed, #f87171)',
};

export default function ArchitectDrawer({ open, onClose }: {
  open: boolean;
  onClose: () => void;
}) {
  const [req, setReq] = useState('');
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState('');
  const [prop, setProp] = useState<ProposalView | null>(null);
  const [list, setList] = useState<ProposalView[]>([]);
  const [confirmed, setConfirmed] = useState(false);
  const [patch, setPatch] = useState('');
  const [graph, setGraph] = useState<{ version?: number; nodes?: number } | null>(null);

  const loadGraph = useCallback(async () => {
    const { body } = await api('/api/workflow/graph');
    if (body?.graph) setGraph(body.graph);
  }, []);

  const loadList = useCallback(async () => {
    const { body } = await api('/api/workflow/proposals');
    if (body?.proposals) setList(body.proposals);
  }, []);

  useEffect(() => {
    if (!open) return;
    void loadGraph();
    void loadList();
  }, [open, loadGraph, loadList]);

  const propose = async () => {
    if (!req.trim()) return;
    setBusy('propose');
    setMsg('');
    setProp(null);
    setPatch('');
    setConfirmed(false);
    const { ok, body } = await api('/api/workflow/proposal', {
      method: 'POST',
      body: JSON.stringify({ request: req.trim() }),
    });
    setBusy('');
    if (!ok) {
      setMsg(`✕ ${body?.detail ?? `请求失败（HTTP）`}`);
      return;
    }
    setProp(body.proposal);
    void loadList();
  };

  const approve = async (apply: boolean) => {
    if (!prop) return;
    setBusy('approve');
    setMsg('');
    const { ok, body } = await api(`/api/workflow/proposal/${prop.id}/approve`, {
      method: 'POST',
      body: JSON.stringify({ confirm_removals: confirmed, apply }),
    });
    setBusy('');
    if (!ok) {
      const d = body?.detail;
      if (typeof d === 'string') setMsg(`✕ ${d}`);
      else if (d?.message) {
        // 二次确认 / 拓扑校验失败都会走到这里 —— 把原因原样展示
        setMsg(`✕ ${d.message}${d.problems?.length ? `（${d.problems.length} 个问题，见下）` : ''}`);
        if (d.problems) {
          setProp((p) => p ? {
            ...p,
            preview: { ...(p.preview ?? {}), ok: false, problems: d.problems },
          } : p);
        }
      } else setMsg('✕ 请求失败');
      return;
    }
    setProp(body.proposal);
    setMsg(apply ? `✓ 已生效（图版本 ${body.graph_version}）—— 注意：这是**运行时**生效，重启后会回到代码里那张图` : '✓ 已批准（未生效）');
    void loadGraph();
    void loadList();
  };

  const reject = async () => {
    if (!prop) return;
    setBusy('reject');
    const { ok, body } = await api(`/api/workflow/proposal/${prop.id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason: '用户在导演台驳回' }),
    });
    setBusy('');
    if (ok) {
      setProp(body.proposal);
      setMsg('已驳回（没有改动任何东西）');
      void loadList();
    }
  };

  const showPatch = async () => {
    if (!prop) return;
    setBusy('patch');
    const { ok, body } = await api(`/api/workflow/proposal/${prop.id}/patch?regenerate=1`);
    setBusy('');
    if (ok) setPatch(body.patch);
    else setMsg('✕ 取补丁失败');
  };

  const rollback = async () => {
    setBusy('rollback');
    const { ok, body } = await api('/api/workflow/rollback', { method: 'POST', body: '{}' });
    setBusy('');
    if (ok) {
      setMsg(`✓ 已回滚到上一版（图版本 ${body.graph_version}）`);
      void loadGraph();
    } else setMsg(`✕ ${body?.detail ?? '回滚失败'}`);
  };

  if (!open) return null;

  const pv = prop?.preview ?? {};
  const blocked = pv.ok === false;
  const needConfirm = Boolean(prop?.needs_second_confirm);
  /**
   * 空提案（0 条操作）**不允许批准**。
   *
   * 它技术上"合法"（改完的图等价于原图），但让人点"批准"没有任何意义 ——
   * 而且会误导：用户以为批准了什么，实际什么都没变。
   * 实测就出现过：本机没填 LLM 密钥 → 架构师给不出 ops → 按钮却亮着。
   */
  const empty = (prop?.ops ?? []).length === 0;
  const canApprove = Boolean(prop) && !blocked && !empty
    && (!needConfirm || confirmed)
    && prop?.status !== 'applied' && prop?.status !== 'rejected';

  return (
    <div
      style={{
        position: 'fixed', top: 0, right: 0, bottom: 0, width: 560,
        background: C.panel, borderLeft: `1px solid ${C.line}`,
        boxShadow: '-18px 0 40px rgba(0,0,0,.5)',
        display: 'flex', flexDirection: 'column', zIndex: 40,
        fontSize: 12, color: C.text,
      }}
    >
      {/* 头部 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '10px 14px', borderBottom: `1px solid ${C.line}`, flex: '0 0 auto',
      }}>
        <strong style={{ fontSize: 13 }}>工作流架构师</strong>
        {graph && (
          <span style={{ fontSize: 10, color: C.muted }}>
            当前图 v{graph.version} · {graph.nodes} 节点
          </span>
        )}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button className="btn ghost" style={{ fontSize: 11 }}
                  disabled={busy === 'rollback'} onClick={() => void rollback()}
                  title="回到上一版图（换错了的退路）">
            回滚
          </button>
          <button className="btn ghost" style={{ fontSize: 11 }} onClick={onClose}>关闭</button>
        </span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: 14 }}>
        {/* 输入诉求 */}
        <div style={{ marginBottom: 10, color: C.muted, lineHeight: 1.6 }}>
          说清你想怎么改工作流，架构师会给出**变更提案**。改动要你批准才生效。
        </div>
        <textarea
          value={req}
          onChange={(e) => setReq(e.target.value)}
          rows={2}
          placeholder="例：我们不需要剧评这一步了 / 把分镜提前到角色设计之前"
          style={{
            width: '100%', boxSizing: 'border-box', resize: 'vertical',
            background: 'rgba(21,15,35,0.55)', color: C.text,
            border: `1px solid ${C.line}`, borderRadius: 8, padding: 8,
            fontFamily: 'inherit', fontSize: 12,
          }}
        />
        <div style={{ marginTop: 6, display: 'flex', gap: 6, alignItems: 'center' }}>
          <button className="btn ghost" disabled={busy === 'propose' || !req.trim()}
                  onClick={() => void propose()}>
            {busy === 'propose' ? '分析中…' : '让架构师出提案'}
          </button>
          {msg && <span style={{ fontSize: 11, color: msg.startsWith('✕') ? C.err : C.ok }}>{msg}</span>}
        </div>

        {/* 提案 */}
        {prop && (
          <div style={{ marginTop: 14, borderTop: `1px solid ${C.line}`, paddingTop: 10 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
              <strong style={{ fontSize: 12 }}>提案 {prop.id}</strong>
              <span style={{ fontSize: 10, color: C.muted }}>{prop.status}</span>
            </div>
            <div style={{ marginBottom: 8 }}>{prop.summary}</div>

            {/* 改了什么 */}
            {(pv.changes ?? []).length > 0 && (
              <div style={{ marginBottom: 8 }}>
                <div style={{ color: C.muted, marginBottom: 2 }}>会改这些：</div>
                <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
                  {(pv.changes ?? []).map((c, i) => <li key={i}>{c}</li>)}
                </ul>
              </div>
            )}

            {/* 规模对比 */}
            {pv.before && pv.after && (
              <div style={{ fontSize: 11, color: C.muted, marginBottom: 8 }}>
                节点 {pv.before.nodes} → {pv.after.nodes} ·
                普通边 {pv.before.edges} → {pv.after.edges} ·
                条件边 {pv.before.conditional} → {pv.after.conditional}
              </div>
            )}

            {/* 问题：不给"批准"按钮，先把问题摆出来 */}
            {blocked && (
              <div style={{
                border: `1px solid ${C.err}`, borderRadius: 8, padding: 8,
                background: 'rgba(248,113,113,.08)', marginBottom: 8,
              }}>
                <div style={{ color: C.err, marginBottom: 4 }}>
                  ⚠ 这份提案通不过校验，**不能生效**：
                </div>
                <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
                  {(pv.problems ?? []).map((p, i) => (
                    <li key={i}>{p.message}{p.where ? `（${p.where}）` : ''}</li>
                  ))}
                </ul>
              </div>
            )}

            {/* 空提案：明确说明"没有可应用的改动"，而不是让人点批准 */}
            {empty && !blocked && (
              <div style={{
                border: `1px solid ${C.warn}`, borderRadius: 8, padding: 8,
                background: 'rgba(255,178,61,.08)', marginBottom: 8,
              }}>
                <span style={{ color: C.warn }}>
                  这份提案**没有任何改动**（0 条操作），无法批准。
                </span>
                <div style={{ marginTop: 4, color: C.muted, lineHeight: 1.6 }}>
                  常见原因：没配置文字模型（架构师要调它才能分析）。
                  见下面的「风险」。
                </div>
              </div>
            )}

            {/* 风险 */}
            {(prop.risks ?? []).length > 0 && (
              <div style={{ marginBottom: 8 }}>
                <div style={{ color: C.muted, marginBottom: 2 }}>风险：</div>
                <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.7, color: C.warn }}>
                  {(prop.risks ?? []).map((r, i) => <li key={i}>{r}</li>)}
                </ul>
              </div>
            )}

            {/* 二次确认（删节点） */}
            {needConfirm && (
              <label style={{
                display: 'flex', gap: 8, alignItems: 'flex-start',
                border: `1px solid ${C.warn}`, borderRadius: 8, padding: 8,
                background: 'rgba(255,178,61,.08)', marginBottom: 8, cursor: 'pointer',
              }}>
                <input type="checkbox" checked={confirmed}
                       onChange={(e) => setConfirmed(e.target.checked)}
                       style={{ marginTop: 2 }} />
                <span style={{ lineHeight: 1.6 }}>
                  这份提案会**删掉节点**：
                  <code style={{ marginLeft: 4 }}>{(prop.removes_nodes ?? []).join('、')}</code>
                  <br />
                  确认要删（这是二次确认，避免误删）
                </span>
              </label>
            )}

            {/* 操作 */}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button className="btn ghost"
                      disabled={!canApprove || busy === 'approve'}
                      title={!canApprove ? '先解决问题或勾选二次确认' : '批准并立即生效（运行时）'}
                      onClick={() => void approve(true)}>
                批准并生效
              </button>
              <button className="btn ghost"
                      disabled={!canApprove || busy === 'approve'}
                      title="只批准，先不改运行时（留给代码补丁那条路）"
                      onClick={() => void approve(false)}>
                只批准不生效
              </button>
              <button className="btn ghost" disabled={busy === 'reject'}
                      onClick={() => void reject()}>驳回</button>
              <button className="btn ghost" disabled={busy === 'patch'}
                      onClick={() => void showPatch()}>查看补丁</button>
            </div>

            <div style={{ marginTop: 8, fontSize: 10, color: C.muted, lineHeight: 1.6 }}>
              「批准并生效」改的是**运行时内存里的图**（重启会回到代码里那张）；
              要永久保留，请用「查看补丁」把 diff 合进
              <code> graph_spec.default_spec()</code>。
            </div>

            {/* 补丁 */}
            {patch && (
              <div style={{ marginTop: 10 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                  <span style={{ color: C.muted }}>代码补丁</span>
                  <a href={`/api/workflow/proposal/${prop.id}/patch/download`}
                     style={{ fontSize: 11 }}>下载 .patch</a>
                </div>
                <pre style={{
                  maxHeight: 260, overflow: 'auto', fontSize: 10, lineHeight: 1.5,
                  background: 'rgba(21,15,35,0.55)', border: `1px solid ${C.line}`,
                  borderRadius: 8, padding: 8, whiteSpace: 'pre-wrap',
                }}>{patch}</pre>
              </div>
            )}
          </div>
        )}

        {/* 历史提案 */}
        {list.length > 0 && (
          <div style={{ marginTop: 16, borderTop: `1px solid ${C.line}`, paddingTop: 10 }}>
            <div style={{ color: C.muted, marginBottom: 4 }}>最近的提案</div>
            {list.slice(0, 6).map((x) => (
              <div key={x.id}
                   onClick={() => { setProp(x); setPatch(''); setConfirmed(false); }}
                   style={{
                     padding: '5px 6px', borderRadius: 6, cursor: 'pointer',
                     display: 'flex', gap: 8, alignItems: 'baseline',
                     background: prop?.id === x.id ? 'rgba(110,123,255,.12)' : 'transparent',
                   }}>
                <span style={{ color: C.muted, fontSize: 10, minWidth: 92 }}>{x.id}</span>
                <span style={{ fontSize: 10, color: x.status === 'applied' ? C.ok : C.muted,
                               minWidth: 62 }}>{x.status}</span>
                <span style={{ flex: 1, wordBreak: 'break-all' }}>{x.summary}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
