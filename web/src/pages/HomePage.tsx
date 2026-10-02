/**
 * 首页：最近创作 + 四个入口 + 环境概览。
 *
 * 定位是"接着上次干活"——用户打开应用最想知道的通常是
 * "我上次做到哪了、现在能不能跑"。所以这里放会话历史与配置就绪状态，
 * 而不是放宣传文案。
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
  running: 'var(--st-running, var(--s-lime))',
  failed: 'var(--st-failed)',
};

/** 必须先选"云端 / 本地"的能力（后端 providers.MODE_KEY_BY_CAPABILITY） */
const MODE_KEYS: Array<[string, string]> = [
  ['llm_provider_mode', '文字模型'],
  ['image_provider_mode', '图片生成'],
  ['video_provider_mode', '视频生成'],
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

  return (
    <div className="page">
      <h2 className="page-title">开始创作</h2>
      <p className="page-sub">
        小剧组把一部漫剧拆成 16 个职业、18 个环节；在导演台上按需取用并串起来。
      </p>

      {err && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--st-review)' }}>⚠ {err}</div>
        </div>
      )}

      {!loading && unset.length > 0 && (
        <div className="card" style={{ borderColor: 'var(--st-review)', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--st-review)', marginBottom: 6 }}>
            ⚠ 还有 {unset.length} 项没选「云端 / 本地」，跑到对应环节会明确报错
          </div>
          <div style={{ fontSize: 12, color: 'var(--s-muted)', marginBottom: 10 }}>
            未选择：{unset.map(([, label]) => label).join('、')}
          </div>
          <button className="btn" onClick={() => navigate('settings')}>
            去设置
          </button>
        </div>
      )}

      <div className="grid-cards" style={{ marginBottom: 20 }}>
        <button className="page-card" onClick={() => navigate('director')}>
          <div style={{ fontSize: 22, marginBottom: 6 }}>⚏</div>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>导演台</div>
          <div style={{ fontSize: 11, color: 'var(--s-muted)' }}>
            在画布上取用职业、连线、运行并逐步确认
          </div>
        </button>
        <button className="page-card" onClick={() => navigate('settings')}>
          <div style={{ fontSize: 22, marginBottom: 6 }}>⚙</div>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>设置</div>
          <div style={{ fontSize: 11, color: 'var(--s-muted)' }}>
            选云端还是本地、填密钥、看费用提醒
          </div>
        </button>
        <button className="page-card" onClick={() => navigate('user')}>
          <div style={{ fontSize: 22, marginBottom: 6 }}>☺</div>
          <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 2 }}>用户</div>
          <div style={{ fontSize: 11, color: 'var(--s-muted)' }}>昵称、头像与账号信息</div>
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
        <div className="card">
          <div className="card-title">📁 我的工程（{projects.length}）</div>
          {projects.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--s-muted)' }}>
              还没有工程。去导演台搭一张图，点「保存」即可。
            </div>
          ) : (
            projects.slice(0, 6).map((p) => (
              <div
                key={p.project_id}
                style={{ fontSize: 12, padding: '4px 0', display: 'flex', gap: 8 }}
              >
                <span style={{ flex: '1 1 auto' }}>{p.name}</span>
                <span style={{ color: 'var(--s-violet-mid)', fontSize: 11 }}>
                  {p.node_count} 节点 · {p.updated.slice(5, 16)}
                </span>
              </div>
            ))
          )}
        </div>

        <div className="card">
          <div className="card-title">🕘 最近创作（{history.length}）</div>
          {history.length === 0 ? (
            <div style={{ fontSize: 12, color: 'var(--s-muted)' }}>还没有创作记录。</div>
          ) : (
            history.slice(0, 6).map((h) => (
              <div
                key={h.session_id}
                style={{ fontSize: 12, padding: '4px 0', display: 'flex', gap: 8 }}
              >
                <span
                  style={{
                    flex: '1 1 auto',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                  title={h.user_input}
                >
                  {h.user_input || '(无描述)'}
                </span>
                <span
                  style={{
                    color: STATUS_COLOR[h.status] ?? 'var(--s-violet-mid)',
                    fontSize: 11,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {STATUS_LABEL[h.status] ?? h.status}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
