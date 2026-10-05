/**
 * 设置页：**按能力分组**，每组把相关设置收在一张卡里。
 *
 * ## 为什么改成这样（用户反馈）
 *
 * 原来的结构是三张横跨全页的大卡：
 *
 *     ① 用云端还是本地？    5 个模式下拉（llm / image_gen / video_gen / lora_train / tts）
 *     ② 按职业指定模型      12 个职业，折叠着
 *     ③ 模型凭据            提供者级密钥
 *
 * 用户的原话是"为啥不能把 3 大项整合在一起，按职业分"。
 * 问题确实存在：**同一个能力的信息被劈在三张卡里**——
 * 想配"图像生成"，要在 ① 里找它的模式、在 ② 里翻到角色设计师/摄影师改覆盖、
 * 再到 ③ 里找对应提供者的 Key。三处来回跳，还容易漏。
 *
 * 现在按能力收成一张张卡：**这张卡里就是这个能力的全部设置**。
 *
 * 为什么按"能力"而不是按"职业"分组：`image_provider_mode` 这类模式字段
 * 是**能力级**的（5 个），而 `image_provider_photographer` 这类覆盖是
 * **职业级**的。按职业分会把同一个模式下拉复制很多遍（或让人找不到它在哪）。
 * 按能力分组后，职业覆盖作为该能力的"细化"折叠在下面——
 * 既满足"按职业分"，又不重复。
 *
 * ## 三条仍然成立的设计原则
 *
 *   1. **必选项要显眼**：云端/本地没选时后端会明确报错（不猜），
 *      所以未选的能力卡片会高亮，并在页面顶部汇总提示。
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

/**
 * 能力分组定义。
 *
 * `cap` 与后端 `/api/providers/modes` 的 `capability` 对齐
 * （`image_edit` 归入"图像生成"——对用户来说都是"出图"，不该分成两卡）。
 * `prefix` 用来把注册表里的字段键归到本组
 * （字段形如 `llm_provider_writer` / `image_model_photographer`）。
 */
interface CapGroup {
  cap: string;
  title: string;
  desc: string;
  /** 把字段键归到本组的前缀（不含尾下划线） */
  prefixes: string[];
  /** 本能力产出的资产类型，用于文案 */
  kinds: string;
}

const CAP_GROUPS: CapGroup[] = [
  {
    cap: 'llm',
    title: '文字模型',
    desc: '策划、剧本、分镜等所有"写字"的环节都走这里。',
    prefixes: ['llm'],
    kinds: '剧本 · 分镜 · 提示词',
  },
  {
    cap: 'image_gen',
    title: '图像生成',
    desc: '角色设定图、场景图、分镜图；也包含图像编辑（inpaint）。',
    prefixes: ['image'],
    kinds: '角色图 · 场景图 · 分镜图',
  },
  {
    cap: 'video_gen',
    title: '视频生成',
    desc: '分镜成片与白模参考视频。云端提供者的 Key 填在这一组里。',
    prefixes: ['video'],
    kinds: '镜头成片 · 白模参考视频',
  },
  {
    cap: 'tts',
    title: '配音',
    desc: '台词转语音。默认 Edge TTS 免费，不需要 Key。',
    prefixes: ['tts'],
    kinds: '对白配音',
  },
  {
    cap: 'lora_train',
    title: 'LoRA 训练',
    desc: '角色一致性训练。零成本走本地，云端按训练步数计费。',
    prefixes: ['lora'],
    kinds: '角色 LoRA 权重',
  },
];

/** 字段键 → 所属能力分组。匹配不到返回 null（不归任何组，不丢字段）。 */
function groupOfKey(key: string): CapGroup | null {
  for (const g of CAP_GROUPS) {
    if (g.prefixes.some((p) => key.startsWith(`${p}_`))) return g;
  }
  return null;
}

export default function SettingsPage() {
  const registry = useCanvas((s) => s.registry);
  const [settings, setSettings] = useState<SettingsMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cost, setCost] = useState<CostItem[] | null>(null);
  const [creds, setCreds] = useState<CredentialSpec | null>(null);
  /**
   * 后端已**真正接入**的能力集合（如 `llm` / `image_gen` / `video_gen`）。
   *
   * 为什么需要它：注册表里有 5 个"云端/本地"模式字段，但 `lora_train` 与
   * `tts` 目前**没有任何调用点**——只是声明。原先前端把它们一起标成
   * "（必选）"并警告"未选择会报错"，**那是假的**：不选也不会报错。
   * 用户于是被推着去配两个不影响任何行为的模式。
   * 这里按后端下发的 `implemented` 只对真会用到的提示必选。
   * 拉不到时**保守地全部视为必选**（宁可多提示，也不要漏提示真需要的）。
   */
  const [implementedCaps, setImplementedCaps] = useState<Set<string> | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const token = getToken();
        const r = await fetch('/api/providers/modes', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await r.json().catch(() => ({}));
        const modes = (body?.modes ?? []) as Array<{ capability: string; implemented?: boolean }>;
        if (alive && r.ok && modes.length) {
          setImplementedCaps(new Set(modes.filter((m) => m.implemented).map((m) => m.capability)));
        }
      } catch {
        /* 拉不到 → implementedCaps 保持 null → 视为全部必选（保守） */
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

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

  /**
   * 把注册表里所有配置字段**按能力分组**，组内再分"模式 / 全局 / 按职业"。
   *
   * 一个字段可能被多个节点声明（`image_provider_mode` 在角色设计师、
   * 场景设计师、摄影师三处都有）——按 key 去重，否则同一个下拉会渲染三遍
   * （原来"① 用云端还是本地"里就出现过重复的"图片生成"）。
   */
  const grouped = useMemo(() => {
    type Entry = { node: string; nodeLabel: string; field: ConfigField };
    const map = new Map<
      string,
      { mode: Entry | null; global: Entry[]; byRole: Map<string, { label: string; fields: ConfigField[] }>; others: Entry[] }
    >();
    for (const g of CAP_GROUPS) {
      map.set(g.cap, { mode: null, global: [], byRole: new Map(), others: [] });
    }
    if (!registry) return { groups: map, ungrouped: [] as Entry[] };

    const seen = new Set<string>();
    const ungrouped: Entry[] = [];

    for (const n of registry.nodes) {
      for (const f of n.config) {
        const g = groupOfKey(f.key);
        if (!g) {
          // 与选模型无关的字段（阈值、时长上限等）单独收在页尾，不能丢
          if (!seen.has(f.key)) {
            seen.add(f.key);
            ungrouped.push({ node: n.key, nodeLabel: n.label, field: f });
          }
          continue;
        }
        if (seen.has(f.key)) continue;
        seen.add(f.key);

        const bucket = map.get(g.cap)!;
        const entry: Entry = { node: n.key, nodeLabel: n.label, field: f };
        if (f.key.endsWith('_provider_mode')) {
          bucket.mode = entry;
          continue;
        }
        /**
         * 带职业后缀的字段（`image_provider_photographer`）归到"按职业"，
         * 其余是能力级全局项（`video_model`、`video_max_duration`）。
         *
         * 判据用"后缀是否是某个注册表职业 key"，而不是"有没有下划线"——
         * `video_max_duration` 也有下划线，但它是全局项。
         */
        const roleKeys = new Set(registry.roles.map((r) => r.key));
        const suffix = f.key.split('_').slice(-1)[0];
        // 职业 key 可能是下划线连接的（character_designer），所以做长后缀匹配
        const matchedRole = [...roleKeys]
          .sort((a, b) => b.length - a.length)
          .find((rk) => f.key.endsWith(`_${rk}`));
        if (matchedRole) {
          const cur = bucket.byRole.get(matchedRole) ?? {
            label: registry.roles.find((r) => r.key === matchedRole)?.label ?? matchedRole,
            fields: [],
          };
          cur.fields.push(f);
          bucket.byRole.set(matchedRole, cur);
        } else {
          void suffix;
          bucket.global.push(entry);
        }
      }
    }
    return { groups: map, ungrouped };
  }, [registry]);

  /**
   * 未选择的模式，分成两拨：
   *   · blocked —— 已接入的能力（不选真的会在跑到那一步时报错）；
   *   · optional —— 仅注册表声明、当前无调用点（不选不影响任何行为）。
   *
   * 分开的原因是原先只有一拨：把 `lora_train` / `tts` 也算作"必选"，
   * 于是用户被告知"还有 2 项未选择，会报错"——而它们根本没人调用。
   * 误导性的必选提示比不提示更糟：用户会去填一堆无用的配置。
   */
  const { blockedModes, optionalModes } = useMemo(() => {
    const blocked: CapGroup[] = [];
    const optional: CapGroup[] = [];
    if (settings == null) return { blockedModes: blocked, optionalModes: optional };
    for (const g of CAP_GROUPS) {
      const entry = grouped.groups.get(g.cap)?.mode;
      if (!entry) continue;
      const v = settings[entry.field.key];
      const unset = !v || v === 'unset';
      if (!unset) continue;
      // 字段名形如 `llm_provider_mode` / `lora_train_provider_mode`
      const cap = entry.field.key.replace(/_provider_mode$/, '');
      (implementedCaps === null || implementedCaps.has(cap) ? blocked : optional).push(g);
    }
    return { blockedModes: blocked, optionalModes: optional };
  }, [grouped, settings, implementedCaps]);

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

  /** 渲染一个字段：设置值 + 保存回调，避免每处都写一遍 */
  const renderField = (f: ConfigField, node: string) => (
    <ConfigFieldEditor
      key={`${node}:${f.key}`}
      field={f}
      current={settings?.[f.key]}
      onSaved={(k, v) => setSettings((s) => ({ ...(s ?? {}), [k]: v }))}
    />
  );

  return (
    <div className="page">
      <h2 className="page-title">设置</h2>
      <p className="page-sub">
        按能力分组：每张卡里就是这个能力的<strong>全部</strong>设置——先选云端还是本地，
        再按需给个别职业单独指定，最后填提供者密钥。
      </p>

      {error && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--st-review)' }}>⚠ {error}</div>
        </div>
      )}

      {blockedModes.length > 0 && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 13, color: 'var(--st-review)' }}>
            ⚠ 还有 {blockedModes.length} 项没选：
            {blockedModes.map((g) => g.title).join('、')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 6, lineHeight: 1.7 }}>
            跑到对应环节时后端会<strong>明确报错</strong>，而不是替你猜——
            猜本地会失败、猜云端会产生你没预期的费用。
          </div>
        </div>
      )}
      {optionalModes.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7 }}>
            另有 {optionalModes.length} 项（{optionalModes.map((g) => g.title).join('、')}）
            当前<strong>未接入流程</strong>，不选不影响运行；将来接入后会自动变为必选。
          </div>
        </div>
      )}

      {/* ── 每个能力一张卡：模式 + 该能力的职业覆盖 + 该能力的凭据 ── */}
      {CAP_GROUPS.map((g) => {
        const bucket = grouped.groups.get(g.cap);
        if (!bucket) return null;
        const hasAnything =
          bucket.mode || bucket.global.length || bucket.byRole.size || g.cap === 'video';
        if (!hasAnything) return null;

        const needsPick =
          bucket.mode != null &&
          settings != null &&
          (!settings[bucket.mode.field.key] || settings[bucket.mode.field.key] === 'unset');
        const implemented =
          bucket.mode == null ||
          implementedCaps === null ||
          implementedCaps.has(bucket.mode.field.key.replace(/_provider_mode$/, ''));

        return (
          <div
            className="card"
            key={g.cap}
            style={{
              marginBottom: 14,
              borderColor: needsPick && implemented ? 'var(--st-review)' : undefined,
            }}
          >
            <div className="card-title" style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
              {g.title}
              <span style={{ fontSize: 11, color: 'var(--text-faint)', fontWeight: 400 }}>
                {g.kinds}
              </span>
              {needsPick && implemented && (
                <span style={{ color: 'var(--st-review)', fontSize: 12, fontWeight: 400 }}>（必选）</span>
              )}
              {!implemented && (
                <span style={{ color: 'var(--text-faint)', fontSize: 12, fontWeight: 400 }}>
                  未接入（可选）
                </span>
              )}
            </div>
            <div
              style={{
                fontSize: 12,
                color: 'var(--text-muted)',
                lineHeight: 1.7,
                marginBottom: 12,
              }}
            >
              {g.desc}
            </div>

            {/* 模式（云端 / 本地）—— 与它的模型字段并排，标签对齐 */}
            <div className="settings-grid">
              {bucket.mode && (
                <div className="settings-cell">{renderField(bucket.mode.field, bucket.mode.node)}</div>
              )}
              {bucket.global.map((e) => (
                <div className="settings-cell" key={e.field.key}>
                  {renderField(e.field, e.node)}
                </div>
              ))}
            </div>

            {/* 该能力的提供者凭据（目前只有视频有提供者级密钥） */}
            {g.cap === 'video' && creds?.video?.keys?.length ? (
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 8, lineHeight: 1.7 }}>
                  视频的密钥是<strong>提供者级</strong>的，与按职业的密钥不是同一个键。
                  想用云端视频，必须在这里填对应提供者的 Key；只用本地 / FFmpeg 则不需要。
                </div>
                <div className="settings-grid">
                  {creds.video.keys.map((c) => (
                    <div className="settings-cell" key={c.key}>
                      {renderField(toConfigField(c), 'video-cred')}
                      {c.verified === false && (
                        <div style={{ fontSize: 11, color: 'var(--st-review)', marginTop: 4 }}>
                          ⚠ 该提供者按公开文档实现，尚未用真实 key 跑通；首次使用请核对返回的报错
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ) : null}

            {/* 按职业细化：默认收起，避免一屏铺满 */}
            {bucket.byRole.size > 0 && (
              <details style={{ marginTop: 12 }}>
                <summary
                  style={{
                    cursor: 'pointer',
                    fontSize: 12,
                    color: 'var(--text-muted)',
                    padding: '4px 0',
                  }}
                >
                  按职业单独指定（{bucket.byRole.size} 个职业）—— 不填就跟随上面的选择
                </summary>
                <div style={{ marginTop: 10 }}>
                  {[...bucket.byRole.entries()].map(([roleKey, v]) => (
                    <details key={roleKey} style={{ marginBottom: 6, paddingLeft: 8 }}>
                      <summary
                        style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text)', padding: '4px 0' }}
                      >
                        {v.label}
                        <span
                          style={{ color: 'var(--text-faint)', marginLeft: 6, fontSize: 11 }}
                        >
                          {roleKey}
                        </span>
                      </summary>
                      <div className="settings-grid" style={{ paddingTop: 8, paddingLeft: 8 }}>
                        {v.fields.map((f) => (
                          <div className="settings-cell" key={f.key}>
                            {renderField(f, roleKey)}
                          </div>
                        ))}
                      </div>
                    </details>
                  ))}
                </div>
              </details>
            )}
          </div>
        );
      })}

      {/* ── 与选模型无关的其它配置（阈值、时长上限…）——不能丢 ── */}
      {grouped.ungrouped.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title">其它配置</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 12 }}>
            阈值、时长上限等与"用哪个模型"无关的参数。
          </div>
          <div className="settings-grid">
            {grouped.ungrouped.map((e) => (
              <div className="settings-cell" key={e.field.key}>
                {renderField(e.field, e.node)}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── 费用提醒（只提醒不拦截）── */}
      <div className="card">
        <div className="card-title">费用提醒</div>
        <div style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.7, marginBottom: 10 }}>
          只提示，不拦截。云端项会在发起真实请求前标出单价与本次预估。
        </div>
        {!cost ? (
          <div style={{ fontSize: 12, color: 'var(--text-faint)' }}>费用审计不可用。</div>
        ) : (
          <>
            <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>
              云端 {cloudItems.length} 项 · 本地 {localItems.length} 项
            </div>
            <div className="settings-grid">
              {(cost ?? []).map((c, i) => (
                <div
                  key={c.key ?? i}
                  className="settings-cell"
                  style={{ fontSize: 12, color: 'var(--text-muted)', lineHeight: 1.6 }}
                >
                  <span style={{ color: c.is_cloud ? 'var(--glow-violet)' : 'var(--text)' }}>
                    {c.is_cloud ? '云' : '本地'}
                  </span>{' '}
                  {c.label ?? c.key}
                  {c.provider ? ` · ${c.provider}` : ''}
                  {c.unit_cost != null ? ` · ${c.unit_cost}${c.currency ?? ''}` : ''}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
