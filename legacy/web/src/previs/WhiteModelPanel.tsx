/**
 * 白模规格面板：A 锁定预览 / B 默认参数 / C 完整编辑。
 *
 * ## 三档是**分层**而不是互斥（用户明确的设计要求）
 *
 *   A 锁定预览 —— 只读展示。适合快速预览、演示、防止误改；不满意就重新生成。
 *                 但只读太死，不能作为主力。
 *   B 默认界面 —— 滑杆改关键参数（身高/肩宽/胖瘦/头身比/四肢比例）。
 *                 新手用这一档。
 *   C 进阶     —— 展开每个 segment、直接改 JSON 源码。老手用这一档。
 *
 * 同一份 spec 三种视图，切档**不改变数据**，所以 `A→B→C` 可以随便来回。
 *
 * ## 为什么把 JSON 编辑放在 C 而不是藏起来
 *
 * 生成总有不满意的时候。只提供"重新生成一句话"会陷入抽卡；
 * 而参数滑杆只覆盖常见维度。给一个能直接改 JSON 的口子，
 * 意味着**用户永远有最终控制权**——这不是"给高级用户的彩蛋"，
 * 而是这个功能能真正投产的前提。
 */

import { useMemo, useState } from 'react';
import { generateWhiteModel } from '../api/whiteModel';
import { BODY_JOINT_LABELS, DEFAULT_PROPORTIONS, normalizeBodySpec } from './bodySpec';
import type { Actor, WhiteModelSpec } from './types';

type Mode = 'locked' | 'basic' | 'full';

const MODES: Array<{ key: Mode; label: string; hint: string }> = [
  { key: 'locked', label: '预览', hint: '只读，防止误改。不满意就重新生成一句话。' },
  { key: 'basic', label: '参数', hint: '拖滑杆调比例，实时看效果。' },
  { key: 'full', label: '进阶', hint: '逐个体块编辑，或直接改 JSON。' },
];

interface Props {
  actor: Actor;
  onApply: (spec: WhiteModelSpec) => void;
}

export default function WhiteModelPanel({ actor, onApply }: Props) {
  const spec = actor.bodySpec;
  const [mode, setMode] = useState<Mode>('basic');
  const [desc, setDesc] = useState('');
  const [hint, setHint] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [attempts, setAttempts] = useState<number | null>(null);
  const [jsonText, setJsonText] = useState('');
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const [jsonOpen, setJsonOpen] = useState(false);

  const activeMode = MODES.find((m) => m.key === mode)!;

  const generate = async () => {
    if (!desc.trim()) return setErr('先描述一下这个角色，例如「矮胖的卡通猫，会说话」');
    setBusy(true);
    setErr(null);
    const res = await generateWhiteModel({
      description: desc,
      name: actor.name,
      heightHint: hint,
    });
    setBusy(false);
    if (!res.ok || !res.spec) return setErr(res.error ?? '生成失败');
    setAttempts(res.attempts ?? null);
    onApply(res.spec);
  };

  /** 改一个比例字段（B 档滑杆用） */
  const setProp = (key: keyof typeof DEFAULT_PROPORTIONS, value: number) => {
    if (!spec) return;
    const next = normalizeBodySpec({
      ...spec,
      proportions: { ...spec.proportions, [key]: value },
    });
    onApply(next);
  };

  /** 改一个体块的半径（C 档用） */
  const setSeg = (index: number, patch: Partial<WhiteModelSpec['segments'][number]>) => {
    if (!spec) return;
    const segments = spec.segments.map((s, i) => (i === index ? { ...s, ...patch } : s));
    onApply(normalizeBodySpec({ ...spec, segments }));
  };

  const removeSeg = (index: number) => {
    if (!spec) return;
    onApply(normalizeBodySpec({ ...spec, segments: spec.segments.filter((_, i) => i !== index) }));
  };

  const addSeg = () => {
    if (!spec) return;
    onApply(
      normalizeBodySpec({
        ...spec,
        segments: [...spec.segments, { joint: 'chest', shape: 'capsule', r0: 0.08, r1: 0.08 }],
      }),
    );
  };

  const jsonPretty = useMemo(
    () => (spec ? JSON.stringify(spec, null, 2) : ''),
    [spec],
  );

  const applyJson = () => {
    setJsonErr(null);
    try {
      const parsed = JSON.parse(jsonText);
      onApply(normalizeBodySpec(parsed));
      setJsonOpen(false);
    } catch (e) {
      setJsonErr(`JSON 解析失败：${(e as Error).message}`);
    }
  };

  return (
    <div style={{ marginTop: 10 }}>
      <div className="seg" style={{ marginBottom: 8 }}>
        {MODES.map((m) => (
          <button
            key={m.key}
            type="button"
            className={`seg-btn${mode === m.key ? ' is-on' : ''}`}
            onClick={() => setMode(m.key)}
            title={m.hint}
          >
            {m.label}
          </button>
        ))}
      </div>
      <div className="hint" style={{ marginBottom: 8 }}>{activeMode.hint}</div>

      {/* ── 生成入口：A 与 B 都提供（A 的主要动作就是"重新生成"） ── */}
      {mode !== 'full' && (
        <>
          <label className="auth-label" style={{ marginTop: 0 }}>角色描述（一句话）</label>
          <textarea
            className="auth-input"
            value={desc}
            onChange={(e) => setDesc(e.target.value)}
            placeholder="例如：矮胖的卡通猫侦探，头大、腿短、穿风衣"
            rows={2}
            style={{ resize: 'vertical', fontSize: 12 }}
          />
          <label className="auth-label">体型提示（可选）</label>
          <input
            className="auth-input"
            value={hint}
            onChange={(e) => setHint(e.target.value)}
            placeholder="例如：1.2 米高 / 头身比 4"
            style={{ fontSize: 12 }}
          />
          <button
            className="btn"
            style={{ width: '100%', marginTop: 8, justifyContent: 'center' }}
            disabled={busy}
            onClick={() => void generate()}
          >
            {busy ? '生成中…（模型要几秒）' : spec ? '重新生成' : 'AI 生成白模'}
          </button>
          {attempts === 2 && (
            <div className="hint" style={{ marginTop: 4 }}>
              第一次输出不合规格，已带上错误让模型重试一次。
            </div>
          )}
          {err && <div className="auth-error">{err}</div>}
        </>
      )}

      {/* ── A 档：只读摘要 ── */}
      {mode === 'locked' && spec && (
        <dl className="kv" style={{ marginTop: 10 }}>
          <dt>名称</dt><dd>{spec.name}</dd>
          <dt>身高</dt><dd>{spec.proportions.height.toFixed(2)} 米</dd>
          <dt>头身比</dt><dd>{spec.proportions.headRatio.toFixed(1)}</dd>
          <dt>体块</dt><dd>{spec.segments.length} 段</dd>
          <dt>姿态</dt><dd>{spec.pose}</dd>
          {spec.description && (<><dt>说明</dt><dd>{spec.description}</dd></>)}
        </dl>
      )}

      {/* ── B 档：关键参数滑杆 ── */}
      {mode === 'basic' && spec && (
        <div style={{ marginTop: 10 }}>
          <PropSlider label="身高（米）" value={spec.proportions.height} min={0.6} max={2.4} step={0.01}
            onChange={(v) => setProp('height', v)} />
          <PropSlider label="头身比" value={spec.proportions.headRatio} min={4} max={12} step={0.1}
            onChange={(v) => setProp('headRatio', v)} />
          <PropSlider label="肩宽" value={spec.proportions.shoulderWidth} min={0.12} max={0.4} step={0.005}
            onChange={(v) => setProp('shoulderWidth', v)} />
          <PropSlider label="胖瘦" value={spec.proportions.chubbiness} min={0.5} max={2} step={0.05}
            onChange={(v) => setProp('chubbiness', v)} />
          <PropSlider label="头大小" value={spec.proportions.headScale} min={0.5} max={2} step={0.05}
            onChange={(v) => setProp('headScale', v)} />
          <PropSlider label="四肢粗细" value={spec.proportions.limbRatio} min={0.5} max={1.8} step={0.05}
            onChange={(v) => setProp('limbRatio', v)} />
          <PropSlider label="腿长" value={spec.proportions.legRatio} min={0.6} max={1.4} step={0.02}
            onChange={(v) => setProp('legRatio', v)} />
          <button
            className="btn ghost"
            style={{ width: '100%', marginTop: 6, fontSize: 11 }}
            onClick={() =>
              onApply(normalizeBodySpec({ ...spec, proportions: { ...DEFAULT_PROPORTIONS } }))
            }
          >
            比例全部还原
          </button>
        </div>
      )}

      {/* ── C 档：逐体块 + JSON 源码 ── */}
      {mode === 'full' && spec && (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
            <span className="card-title" style={{ margin: 0 }}>体块（{spec.segments.length}）</span>
            <button className="btn ghost" style={{ fontSize: 10, padding: '2px 6px', marginLeft: 'auto' }}
              onClick={addSeg}>+ 加一段</button>
          </div>
          <div style={{ maxHeight: 260, overflowY: 'auto' }}>
            {spec.segments.map((s, i) => (
              <div key={`${s.joint}-${i}`} style={{ borderBottom: '1px solid var(--glass-line-lite)', padding: '5px 0' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
                  <span style={{ color: 'var(--text)' }}>{BODY_JOINT_LABELS[s.joint] ?? s.joint}</span>
                  <span style={{ color: 'var(--text-faint)', fontSize: 10 }}>{s.shape}</span>
                  <button className="btn ghost" style={{ fontSize: 9, padding: '1px 5px', marginLeft: 'auto' }}
                    onClick={() => removeSeg(i)}>删</button>
                </div>
                {s.shape !== 'box' ? (
                  <>
                    <PropSlider label="父端半径" value={s.r0} min={0.005} max={0.3} step={0.005}
                      compact onChange={(v) => setSeg(i, { r0: v })} />
                    <PropSlider label="子端半径" value={s.r1} min={0.005} max={0.3} step={0.005}
                      compact onChange={(v) => setSeg(i, { r1: v })} />
                  </>
                ) : (
                  <div className="hint" style={{ fontSize: 10 }}>
                    尺寸 {s.size?.map((x) => x.toFixed(3)).join(' × ') ?? '—'}（米）
                  </div>
                )}
              </div>
            ))}
          </div>

          <button className="btn ghost" style={{ width: '100%', marginTop: 8, fontSize: 11 }}
            onClick={() => { setJsonText(jsonPretty); setJsonOpen((v) => !v); }}>
            {jsonOpen ? '收起 JSON 源码' : '直接改 JSON 源码'}
          </button>
          {jsonOpen && (
            <>
              <textarea
                className="auth-input"
                value={jsonText}
                onChange={(e) => setJsonText(e.target.value)}
                rows={14}
                spellCheck={false}
                style={{ marginTop: 6, fontFamily: 'var(--font-mono)', fontSize: 11, resize: 'vertical' }}
              />
              {jsonErr && <div className="auth-error">{jsonErr}</div>}
              <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                <button className="btn" style={{ flex: 1, justifyContent: 'center' }} onClick={applyJson}>
                  应用
                </button>
                <button className="btn ghost" onClick={() => setJsonText(jsonPretty)}>重置</button>
              </div>
            </>
          )}
        </div>
      )}

      {!spec && mode === 'full' && (
        <div className="hint" style={{ marginTop: 8 }}>
          还没有规格。先切到「参数」档生成一个，或直接从工程文件里载入。
        </div>
      )}
    </div>
  );
}

/** 一个带数字显示的滑杆。B 档与 C 档共用。 */
function PropSlider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  compact,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  compact?: boolean;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5, margin: compact ? '2px 0' : '5px 0' }}>
      <span style={{
        fontSize: compact ? 10 : 11,
        color: 'var(--text-muted)',
        width: compact ? 56 : 64,
        flex: '0 0 auto',
      }}>
        {label}
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ flex: '1 1 auto', minWidth: 0 }}
      />
      <span className="mono" style={{ fontSize: 10, width: 38, textAlign: 'right', color: 'var(--text-faint)' }}>
        {value.toFixed(step < 0.01 ? 3 : 2)}
      </span>
    </div>
  );
}
