/**
 * 设置页：先选「云端 / 本地」，再按职业细化，最后看费用提醒。
 *
 * 三条设计原则：
 *   1. **必选项放最前**：云端/本地没选时后端会明确报错（不猜），
 *      所以这一块必须在页面顶部且高亮未选项。
 *   2. **能改的一定存得下**：白名单由后端从注册表派生，本页只渲染
 *      注册表实际声明的配置字段，不自己造键。
 *   3. **费用只提醒不拦截**（用户 2026-10-02 的决定）：
 *      云端项标出单价与本次预估，但不阻止执行。
 */

import { useEffect, useMemo, useState } from 'react';
import ConfigFieldEditor from '../canvas/ConfigFieldEditor';
import { fetchSettings, type SettingsMap } from '../api/settings';
import { getToken } from '../api/auth';
import { useCanvas } from '../store/canvas';
import type { ConfigField } from '../domain/registry';

/** 费用审计返回的单项 */
interface CostItem {
  key?: string;
  label?: string;
  provider?: string;
  is_cloud?: boolean;
  note?: string;
  unit_cost?: number;
  currency?: string;
}

/** 后端下发的"提供者需要填什么凭据" */
interface CredField {
  key: string;
  label: string;
  kind: 'secret' | 'text';
  provider: string;
  hint?: string;
  models?: string[];
  verified?: boolean;
}

interface CredentialSpec {
  video?: { keys: CredField[]; endpoints: CredField[] };
}

/**
 * 把后端下发的凭据字段适配成 `ConfigField`，以便复用 `ConfigFieldEditor`。
 *
 * 为什么复用而不是另写输入框：密钥字段有"**不回显**、留空=沿用原值、
 * 置空=清除覆盖"三条约定，`ConfigFieldEditor` 已经实现并有测试。
 * 另写一份必然漂移——本项目已经因为重复实现吃过亏。
 */
function toConfigField(c: CredField): ConfigField {
  return {
    key: c.key,
    label: c.label,
    type: c.kind === 'secret' ? 'password' : 'text',
    scope: 'global',
    options: [],
    default: '',
    secret: c.kind === 'secret',
    help: c.hint ?? '',
  };
}

export default function SettingsPage() {  const registry = useCanvas((s) => s.registry);
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cost, setCost] = useState<CostItem[] | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [creds, setCreds] = useState<CredentialSpec | null>(null);

  /**
   * 拉取"提供者凭据"清单。
   *
   * 为什么必须由后端下发、而不是前端硬编码：
   *   视频的密钥是**提供者级**的（`minimax_api_key` / `dashscope_api_key` /
   *   `ark_api_key`），与 LLM、图像的 `*_api_key_{角色}` 命名完全不同。
   *   前端原先只按 `llm_/image_/video_/tts_/lora_` 前缀渲染字段，
   *   结果就是这三个键在白名单里（能存）却**没有输入框**——
   *   用户根本没法配视频 API key。由后端声明就不会再有这种漂移。
   */
  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const token = getToken();
        const r = await fetch('/api/providers', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        if (alive && r.ok && body?.credential_spec) {
          setCreds(body.credential_spec as CredentialSpec);
        }
      } catch {
        /* 凭据清单拉不到不影响本页其余部分 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const token = getToken();
      const res = await fetchSettings(token);
      if (!alive) return;
      if (res.ok) setSettings(res.settings);
      else setError(res.error ?? '读取设置失败');

      try {
        const r = await fetch('/api/cost/audit', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        if (alive && r.ok) setCost((body.items ?? body.audit ?? []) as CostItem[]);
      } catch {
        /* 费用审计失败不影响设置本身 */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  /** 模式字段：注册表里所有 `*_provider_mode`（每个能力一个，承载它的节点各不同） */
  const modeFields = useMemo(() => {
    if (!registry) return [] as Array<{ node: string; field: ConfigField }>;
    const out: Array<{ node: string; field: ConfigField }> = [];
    for (const n of registry.nodes) {
      for (const f of n.config) {
        if (f.key.endsWith('_provider_mode')) out.push({ node: n.key, field: f });
      }
    }
    return out;
  }, [registry]);

  /** 各职业的 provider/model/key 字段（模式字段已单独提出，不再重复显示） */
  const roleGroups = useMemo(() => {
    if (!registry) return [] as Array<{ node: string; label: string; fields: ConfigField[] }>;
    const groups: Array<{ node: string; label: string; fields: ConfigField[] }> = [];
    for (const n of registry.nodes) {
      const fields = n.config.filter(
        (f) =>
          !f.key.endsWith('_provider_mode') &&
          (f.key.startsWith('llm_') ||
            f.key.startsWith('image_') ||
            f.key.startsWith('video_') ||
            f.key.startsWith('tts_') ||
            f.key.startsWith('lora_')),
      );
      if (fields.length) groups.push({ node: n.key, label: n.label, fields });
    }
    return groups;
  }, [registry]);

  const unsetModes = modeFields.filter(({ field }) => {
    const v = settings?.[field.key];
    return settings != null && (!v || v === 'unset');
  });

  const cloudItems = (cost ?? []).filter((c) => c.is_cloud);
  const localItems = (cost ?? []).filter((c) => c.is_cloud === false);

  if (!registry) {
    return (
      <div className="page">
        <h2 className="page-title">设置</h2>
        <p className="page-sub">注册表尚未加载，无法列出可配置项。</p>
      </div>
    );
  }

  return (
    <div className="page">
      <h2 className="page-title">设置</h2>
      <p className="page-sub">
        这里没有默认值：先显式选「云端 API」还是「本地推理」，再按需细化到每个职业。
      </p>

      {error && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--st-review)' }}>⚠ {error}</div>
        </div>
      )}

      {/* ---------------- 第一步：云端 / 本地 ---------------- */}
      <div
        className="card"
        style={{
          marginBottom: 14,
          borderColor: unsetModes.length ? 'var(--st-review)' : undefined,
        }}
      >
        <div className="card-title">① 用云端还是本地？<span style={{ color: 'var(--st-review)' }}>（必选）</span></div>
        {unsetModes.length > 0 && (
          <div style={{ fontSize: 12, color: 'var(--st-review)', marginBottom: 10 }}>
            ⚠ 还有 {unsetModes.length} 项未选择。跑到对应环节时后端会明确报错，
            而不是替你猜（猜本地会失败、猜云端会产生你没预期的费用）。
          </div>
        )}

        <div className="grid-cards">
          {modeFields.map(({ node, field }) => (
            <div key={`${node}:${field.key}`}>
              <ConfigFieldEditor
                field={field}
                current={settings?.[field.key]}
                onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
              />
            </div>
          ))}
        </div>
      </div>

      {/* ---------------- 第二步：按职业细化 ---------------- */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title">
          ② 按职业指定模型
          <button
            className="btn ghost"
            style={{ marginLeft: 'auto' }}
            onClick={() => setShowAdvanced((v) => !v)}
          >
            {showAdvanced ? '收起' : `展开（${roleGroups.length} 个职业）`}
          </button>
        </div>
        <div style={{ fontSize: 12, color: 'var(--s-muted)', lineHeight: 1.7 }}>
          不填就跟随上面的「云端 / 本地」选择。想混搭时在这里单独指定——
          例如图片走本地省钱、视频走云端提速。
        </div>

        {showAdvanced && (
          <div style={{ marginTop: 14 }}>
            {roleGroups.map((g) => (
              <details key={g.node} style={{ marginBottom: 8 }}>
                <summary
                  style={{
                    cursor: 'pointer',
                    fontSize: 12,
                    padding: '6px 0',
                    color: 'var(--s-white)',
                  }}
                >
                  {g.label}
                  <span style={{ color: 'var(--s-violet-mid)', marginLeft: 6, fontSize: 11 }}>
                    {g.node}
                  </span>
                </summary>
                <div style={{ paddingLeft: 12, paddingTop: 6 }}>
                  {g.fields.map((f) => (
                    <ConfigFieldEditor
                      key={f.key}
                      field={f}
                      current={settings?.[f.key]}
                      onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
                    />
                  ))}
                </div>
              </details>
            ))}
          </div>
        )}
      </div>

      {/* ---------------- 提供者凭据（视频模型等） ---------------- */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title">③ 模型凭据<span style={{ color: 'var(--s-muted)', fontWeight: 400, marginLeft: 8, fontSize: 12 }}>视频模型必须填这里</span></div>
        <div style={{ fontSize: 12, color: 'var(--s-muted)', lineHeight: 1.7, marginBottom: 10 }}>
          视频的密钥是**提供者级**的，与上面按职业的密钥不是同一个键。
          想用云端视频（含白模参考视频），必须在这里填对应提供者的 Key。
          只用本地 / FFmpeg 则不需要。
        </div>
        {!creds?.video?.keys?.length ? (
          <div style={{ fontSize: 12, color: 'var(--s-muted)' }}>凭据清单不可用。</div>
        ) : (
          <div className="grid-cards">
            {creds.video.keys.map((c) => (
              <div key={c.key}>
                <ConfigFieldEditor
                  field={toConfigField(c)}
                  current={settings?.[c.key]}
                  onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
                />
                {c.verified === false && (
                  <div style={{ fontSize: 11, color: 'var(--st-review)', marginTop: 2 }}>
                    ⚠ 该提供者按公开文档实现，尚未用真实 key 跑通；首次使用请核对返回的报错
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {creds?.video?.endpoints?.length ? (
          <details style={{ marginTop: 12 }}>
            <summary style={{ cursor: 'pointer', fontSize: 12, color: 'var(--s-white)' }}>
              高级：自定义端点（代理 / 私有部署时用）
            </summary>
            <div style={{ fontSize: 11, color: 'var(--s-muted)', margin: '6px 0' }}>
              留空即用官方默认地址。只在走代理或私有网关时才需要改。
            </div>
            <div className="grid-cards">
              {creds.video.endpoints.map((c) => (
                <div key={c.key}>
                  <ConfigFieldEditor
                    field={toConfigField(c)}
                    current={settings?.[c.key]}
                    onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
                  />
                </div>
              ))}
            </div>
          </details>
        ) : null}
      </div>

      {/* ---------------- 费用提醒（只提醒，不拦截） ---------------- */}
      <div className="card">
        <div className="card-title">④ 费用提醒</div>
        {cost == null ? (
          <div style={{ fontSize: 12, color: 'var(--s-muted)' }}>费用审计不可用。</div>
        ) : (
          <>
            <div style={{ fontSize: 12, marginBottom: 10 }}>
              {cloudItems.length > 0 ? (
                <span style={{ color: 'var(--st-review)' }}>
                  本档累计 {cloudItems.length} 项会走云端计费
                </span>
              ) : (
                <span style={{ color: 'var(--st-done)' }}>✓ 当前没有走云端计费的项</span>
              )}
              {localItems.length > 0 && (
                <span style={{ color: 'var(--s-muted)', marginLeft: 10 }}>
                  · {localItems.length} 项走本地（零成本）
                </span>
              )}
            </div>

            <dl className="kv">
              {(cost ?? []).map((c, i) => (
                <div key={c.key ?? i} style={{ display: 'contents' }}>
                  <dt>{c.label ?? c.key ?? '—'}</dt>
                  <dd>
                    <span
                      style={{ color: c.is_cloud ? 'var(--st-review)' : 'var(--st-done)' }}
                    >
                      {c.provider ?? '—'}
                    </span>
                    {c.is_cloud ? ' · 云端' : ' · 本地'}
                    {c.note ? ` · ${c.note}` : ''}
                  </dd>
                </div>
              ))}
            </dl>

            <div
              style={{
                marginTop: 12,
                paddingTop: 10,
                borderTop: '1px solid var(--s-hairline)',
                fontSize: 11,
                color: 'var(--s-muted)',
                lineHeight: 1.7,
              }}
            >
              本项目**不做费用拦截**，只做提醒——避免在你明确知道要花这笔钱时挡住你。
              云端单价以各服务商实际账单为准。
            </div>
          </>
        )}
      </div>
    </div>
  );
}
