/**
 * 属性面板 —— 显示选中节点的详情，并**可编辑其配置项**。
 *
 * 配置项来自注册表（`node.config`），读写走后端 `GET/PUT /api/settings`：
 *   - 白名单由注册表派生，界面上能改的一定存得下
 *   - 密钥字段不回显（后端掩码），留空=沿用、重填=替换
 *   - 值置空 = 清除该覆盖，回退到"模式默认"或全局默认
 */

import { useEffect, useState } from 'react';
import { useCanvas } from '../store/canvas';
import { getToken } from '../api/auth';
import { STATUS_COLOR, STATUS_LABEL } from './NodeCard';
import { fetchSettings, type SettingsMap } from '../api/settings';
import ConfigFieldEditor from './ConfigFieldEditor';

export default function Inspector() {
  const selectedId = useCanvas((s) => s.selectedId);
  const nodes = useCanvas((s) => s.nodes);
  const registry = useCanvas((s) => s.registry);
  const removeNode = useCanvas((s) => s.removeNode);
  const setNodeConfig = useCanvas((s) => s.setNodeConfig);
  const setNodeNote = useCanvas((s) => s.setNodeNote);

  /** 当前设置（含密钥掩码）；属性面板据此回显 */
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const res = await fetchSettings(getToken());
      if (!alive) return;
      if (res.ok) {
        setSettings(res.settings);
        setSettingsError(null);
      } else {
        // 后端不可达时也要让面板可用（只是没有初值）
        setSettings({});
        setSettingsError(res.error ?? '读取失败');
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  if (!selectedId) {
    return (
      <aside className="inspector">
        <h3>属性</h3>
        <p className="ins-sub">点击画布上的节点查看详情。</p>
        {registry && (
          <div style={{ fontSize: 11, color: 'var(--s-muted)', lineHeight: 1.7 }}>
            <div>注册表：{registry.roles.length} 职业 / {registry.nodes.length} 节点</div>
            <div>确认点：{registry.checkpoints.length} 个</div>
            <div>产物：{registry.artifacts.length} 种</div>
          </div>
        )}
      </aside>
    );
  }

  const canvasNode = nodes.find((n) => n.id === selectedId);
  const node = registry?.nodes.find((n) => n.key === selectedId);
  if (!canvasNode || !node) {
    return (
      <aside className="inspector">
        <h3>属性</h3>
        <p className="ins-sub">节点不在注册表中。</p>
      </aside>
    );
  }

  const role = registry?.roles.find((r) => r.node === node.key);
  const dept = registry?.departments.find((d) => d.key === node.dept);
  const checkpoint = registry?.checkpoints.find((c) => c.node === node.key);
  const labelOf = (k: string) => registry?.artifacts.find((a) => a.key === k)?.label ?? k;
  const status = canvasNode.data.status;
  const note = (canvasNode.data as unknown as { note?: string }).note ?? '';
  const localConfig = (canvasNode.data as unknown as { config?: Record<string, unknown> }).config ?? {};
  const overrideCount = Object.values(localConfig).filter((v) => v !== '' && v != null).length;

  return (
    <aside className="inspector">
      <h3>{node.label}</h3>
      <p className="ins-sub">
        {dept?.label} · {role?.duty ?? (node.agent_class || '—')}
      </p>

      <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 10 }}>
        <span className="status-dot" style={{ background: STATUS_COLOR[status] }} />
        <span style={{ fontSize: 11 }}>{STATUS_LABEL[status]}</span>
        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--s-violet-mid)' }}>
          {node.kind} · {node.can_skip ? '可跳过' : '必跑'}
        </span>
      </div>

      {checkpoint && (
        <div className="field">
          <label>人工确认点</label>
          <div style={{ fontSize: 11, color: 'var(--st-review)' }}>⚑ {checkpoint.label}</div>
        </div>
      )}

      <div className="field">
        <label>输入（{node.consumes.length}）</label>
        <div style={{ fontSize: 11, color: 'var(--s-muted)', lineHeight: 1.8 }}>
          {node.consumes.length === 0 ? '—' : node.consumes.map(labelOf).join('、')}
        </div>
      </div>

      <div className="field">
        <label>输出（{node.produces.length}）</label>
        <div style={{ fontSize: 11, color: 'var(--s-muted)', lineHeight: 1.8 }}>
          {node.produces.length === 0 ? '—' : node.produces.map(labelOf).join('、')}
        </div>
      </div>

      {node.capability.length > 0 && (
        <div className="field">
          <label>能力</label>
          <div style={{ fontSize: 11, color: 'var(--s-muted)' }}>{node.capability.join('、')}</div>
        </div>
      )}

      {role && role.panels.length > 1 && (
        <div className="field">
          <label>面板（{role.panels.length}）</label>
          <div style={{ fontSize: 11, color: 'var(--s-muted)', lineHeight: 1.8 }}>
            {role.panels.map((p) => `${p.label}（${p.kind}）`).join('、')}
          </div>
        </div>
      )}

      {node.config.length > 0 && (
        <div className="field">
          <label>配置项（{node.config.length}）</label>
          {!settings ? (
            <div className="hint">加载设置中…{settingsError ? `（${settingsError}）` : ''}</div>
          ) : (
            <>
              {node.config.map((c) => (
                <ConfigFieldEditor
                  key={c.key}
                  field={c}
                  current={settings[c.key]}
                  onSaved={(k, v) => {
                    setSettings((s) => ({ ...s, [k]: v }));
                    // 同时记进画布，这样"保存工程"会把参数一并带走
                    setNodeConfig(node.key, k, v);
                  }}
                />
              ))}
              <div className="hint">
                改动立即保存。把值清空 = 清除该覆盖，回退到「模式默认」或全局默认。
                {overrideCount > 0 && ` 本节点已有 ${overrideCount} 项覆盖。`}
              </div>
            </>
          )}
        </div>
      )}

      <div className="field">
        <label>备注</label>
        <textarea
          rows={2}
          value={note}
          placeholder="给自己的提示，例如「这段等美术定稿后再跑」"
          onChange={(e) => setNodeNote(node.key, e.target.value)}
        />
      </div>

      {node.legacy_keys.length > 0 && (
        <div className="field">
          <label>旧命名（兼容用）</label>
          <div style={{ fontSize: 11, color: 'var(--s-violet-mid)' }}>
            {node.legacy_keys.join('、')}
          </div>
        </div>
      )}

      <button className="btn ghost" style={{ width: '100%', marginTop: 8 }} onClick={() => removeNode(node.key)}>
        从画布移除
      </button>
    </aside>
  );
}
