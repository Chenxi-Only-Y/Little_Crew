/**
 * 首页：首屏（剧场感）+ 低密度的两段内容。
 *
 * 设计决定（见 `styles/tokens.css` 顶部）：
 *   首屏几乎不放高密度信息——只有眉题 + 一句大标题 + 一段说明 + 入口。
 *   用户打开应用最想知道"上次做到哪了、现在能不能跑"，那些放在
 *   **首屏之下**的第二、三段里，而不是塞进首屏。
 *
 * 所以这一页是：**一个大留白的首屏**，然后才是数据。
 */

import { useEffect, useState } from 'react';
import { navigate } from '../app/router';
import { getToken } from '../api/auth';
import { getUsername } from '../api/auth';
import { fetchSettings } from '../api/settings';
import { listProjects, type ProjectSummary } from '../api/projects';

interface HistoryItem {
  session_id: string;
  user_input: string;
  status: string;
  phase: string;
  created: string;
  updated: string;
}

const STATUS_LABEL: Record<string, string> = {
  done: '已完成',
  review: '等待审核',
  running: '进行中',
  failed: '失败',
  cancelled: '已取消',
  resume_lost: '断点丢失',
};

const STATUS_COLOR: Record<string, string> = {
  done: 'var(--st-done)',
  review: 'var(--st-review)',
  running: 'var(--st-running)',
  failed: 'var(--st-failed)',
};

/** 必须先选"云端 / 本地"的能力（后端 providers.MODE_KEY_BY_CAPABILITY） */
const MODE_KEYS: Array<[string, string]> = [
  ['llm_provider_mode', '文字模型'],
  ['image_provider_mode', '图片生成'],
  ['video_provider_mode', '视频生成'],
];

/** 入口：首屏只放三个，且**不含**导航里已有的"用户" */
const ENTRIES: Array<{ icon: string; title: string; desc: string; to: 'director' | 'previs' | 'settings' }> = [
  {
    icon: '⚏',
    title: '导演台',
    desc: '在画布上取用职业、连线、运行，并在 12 个确认点逐步决定。',
    to: 'director',
  },
  {
    icon: '◲',
    title: '3D 预演',
    desc: '摆白模走位、录机位，导出结构帧作为视频模型的空间依据。',
    to: 'previs',
  },
  {
    icon: '⚙',
    title: '设置',
    desc: '先决定云端还是本地，再填密钥。没有默认值，不会替你猜。',
    to: 'settings',
  },
];

export default function HomePage() {
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      const token = getToken();
      const user = getUsername();
      const [s, p] = await Promise.all([fetchSettings(token), listProjects(token)]);
      if (!alive) return;
      if (s.ok) setSettings(s.settings);
      if (p.ok) setProjects(p.projects);
      if (!s.ok && !p.ok) setErr(s.error ?? '无法连接后端');

      if (user) {
        try {
          const res = await fetch(`/api/crew/history/${encodeURIComponent(user)}`, {
            headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          });
          const body = await res.json().catch(() => ({}));
          if (alive && res.ok) setHistory((body.history ?? body.sessions ?? []) as HistoryItem[]);
        } catch {
          /* 历史拉不到不影响首页其它部分 */
        }
      }
      if (alive) setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, []);

  const unset = MODE_KEYS.filter(([k]) => {
    const v = settings?.[k];
    return !v || v === 'unset';
  });

  const username = getUsername();

  return (
    <div className="page">
      {/* ══════════ 首屏：只有眉题 / 大标题 / 说明 / 入口 ══════════ */}
      <section style={{ paddingTop: 'var(--sp-8)', paddingBottom: 'var(--section-y)' }}>
        <p className="eyebrow enter" style={{ '--i': 0 } as React.CSSProperties}>
          AI 漫剧制作 · 多智能体
        </p>

        {/* 大标题：中英**同级**（用户要求"和中文一样"），
            所以 `hero-glow` 加在整行 `<h1>` 上而不是只圈住中文——
            否则只有中文发光，英文会像被落下的。 */}
        <h1 className="hero-title hero-glow enter" style={{ '--i': 1 } as React.CSSProperties}>
          小剧组<span className="hero-title-en">·Little Crew</span>
        </h1>

        <p className="lead enter" style={{ '--i': 2 } as React.CSSProperties}>
          一句话，交给一个剧组。把一部漫剧拆成 16 个职业、18 个环节——
          你在导演台上按需取用、连成一条线，然后在 12 个人工确认点
          决定留还是重做。AI 干活，你掌镜。
        </p>

        <div
          className="enter"
          style={{
            '--i': 3,
            display: 'flex',
            gap: 'var(--sp-3)',
            flexWrap: 'wrap',
          } as React.CSSProperties}
        >
          <button className="btn" onClick={() => navigate('director')}>
            进入导演台
          </button>
          <button className="btn ghost" onClick={() => navigate('previs')}>
            先搭白模预演
          </button>
        </div>

        {/* 首屏左下角的铭牌。
            原先这行在导航栏左上角（`小剧组 · DIRECTOR CONSOLE`），
            用户要求挪到这里：导航只留药丸与右侧控制，品牌落到首屏。
            英文不发音、不发光——它是**铭牌**，不是第二个标题。 */}
        <p className="hero-latin enter" style={{ '--i': 4 } as React.CSSProperties}>
          DIRECTOR CONSOLE
        </p>
      </section>

      {/* ══════════ 环境就绪状态（有问题才出现） ══════════ */}
      {(err || unset.length > 0) && (
        <section className="section-tight" style={{ paddingTop: 0 }}>
          {err && (
            <div className="issue-panel error" style={{ margin: '0 0 var(--sp-4)' }}>
              ✕ {err}
            </div>
          )}
          {!loading && unset.length > 0 && (
            <div className="issue-panel warn" style={{ margin: 0 }}>
              <div style={{ marginBottom: 'var(--sp-2)' }}>
                ⚠ 还有 {unset.length} 项没选「云端 / 本地」。跑到对应环节会明确报错，
                而不是替你猜——猜本地会失败，猜云端会产生你没预期的费用。
              </div>
              <div style={{ color: 'var(--text-faint)', fontSize: 12, marginBottom: 'var(--sp-3)' }}>
                未选择：{unset.map(([, label]) => label).join('、')}
              </div>
              <button className="btn" onClick={() => navigate('settings')}>
                去设置
              </button>
            </div>
          )}
        </section>
      )}

      {/* ══════════ 入口 ══════════ */}
      <section className="section-tight">
        <p className="eyebrow">从这里开始</p>
        <div className="grid-entries">
          {ENTRIES.map((e, i) => (
            <button
              key={e.to}
              className="entry enter"
              style={{ '--i': i } as React.CSSProperties}
              onClick={() => navigate(e.to)}
            >
              <div style={{ fontSize: 24, marginBottom: 'var(--sp-4)', opacity: 0.9 }}>
                {e.icon}
              </div>
              <h3>{e.title}</h3>
              <p style={{ margin: 0, color: 'var(--text-muted)', fontSize: 13, lineHeight: 'var(--lh-body)' }}>
                {e.desc}
              </p>
            </button>
          ))}
        </div>
      </section>

      {/* ══════════ 工程与创作记录 ══════════ */}
      <section className="section-tight" style={{ paddingBottom: 'var(--sp-7)' }}>
        <p className="eyebrow">接着上次</p>

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 'var(--sp-4)' }}>
          <div className="card">
            <div className="card-title" style={{ fontSize: 15, marginBottom: 'var(--sp-4)' }}>
              我的工程
              <span className="pill-chip">{projects.length}</span>
            </div>
            {projects.length === 0 ? (
              <div className="empty-hint" style={{ padding: 'var(--sp-5) 0' }}>
                还没有工程。
                <br />
                去导演台搭一张图，点「保存」即可。
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 'var(--sp-3)' }}>
                {projects.slice(0, 6).map((p) => (
                  <div
                    key={p.project_id}
                    style={{ display: 'flex', gap: 'var(--sp-3)', fontSize: 13, alignItems: 'baseline' }}
                  >
                    <span style={{ flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {p.name}
                    </span>
                    <span className="mono" style={{ color: 'var(--text-faint)', fontSize: 11, whiteSpace: 'nowrap' }}>
                      {p.node_count} 节点 · {p.updated.slice(5, 16)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="card">
            <div className="card-title" style={{ fontSize: 15, marginBottom: 'var(--sp-4)' }}>
              最近创作
              <span className="pill-chip">{history.length}</span>
            </div>
            {history.length === 0 ? (
              <div className="empty-hint" style={{ padding: 'var(--sp-5) 0' }}>
                还没有创作记录。
              </div>
            ) : (
              <div style={{ display: 'grid', gap: 'var(--sp-3)' }}>
                {history.slice(0, 6).map((h) => (
                  <div
                    key={h.session_id}
                    style={{ display: 'flex', gap: 'var(--sp-3)', fontSize: 13, alignItems: 'baseline' }}
                  >
                    <span
                      style={{ flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      title={h.user_input}
                    >
                      {h.user_input || '(无描述)'}
                    </span>
                    <span
                      style={{
                        color: STATUS_COLOR[h.status] ?? 'var(--text-faint)',
                        fontSize: 11,
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {STATUS_LABEL[h.status] ?? h.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {username && (
          <dl className="kv" style={{ marginTop: 'var(--sp-6)' }}>
            <dt>当前账号</dt>
            <dd>{username}</dd>
          </dl>
        )}
      </section>
    </div>
  );
}
