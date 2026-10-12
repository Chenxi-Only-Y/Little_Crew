/**
 * 用户页：昵称、头像、账号信息。
 *
 * 后端契约（`app/main.py`）：
 *   GET /api/user/profile → {profile: {username, display_name, avatar, avatar_color}}
 *   PUT /api/user/profile → 同上（只传要改的字段）
 *
 * 头像用 `avatar` 存 URL、`avatar_color` 存底色（无图时显示首字母色块），
 * 与本项目既有 profiles 表一致，不改后端结构。
 */

import { useEffect, useState } from 'react';
import { getToken, getUsername, clearAuth } from '../api/auth';

interface Profile {
  username?: string;
  display_name?: string;
  avatar?: string;
  avatar_color?: string;
  updated?: string;
}

const COLORS = [
  '#c8f04a', '#61d6a0', '#f0b23d', '#e8736a',
  '#8b8bf0', '#4ac8f0', '#f06ab0', '#b0b0c0',
];

export default function UserPage() {
  const token = getToken();
  const username = getUsername() ?? '';

  const [profile, setProfile] = useState<Profile>({});
  const [displayName, setDisplayName] = useState('');
  const [avatar, setAvatar] = useState('');
  const [color, setColor] = useState(COLORS[0]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const res = await fetch('/api/user/profile', {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        });
        const body = await res.json().catch(() => ({}));
        if (!alive) return;
        const p: Profile = body.profile ?? {};
        setProfile(p);
        setDisplayName(p.display_name ?? '');
        setAvatar(p.avatar ?? '');
        setColor(p.avatar_color || COLORS[0]);
      } catch {
        /* 拉不到就留空，用户仍可填了再存 */
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, [token]);

  const save = async () => {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const res = await fetch('/api/user/profile', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          display_name: displayName,
          avatar,
          avatar_color: color,
        }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body?.status === 'error') {
        setMsg({ ok: false, text: body?.message || body?.detail || `保存失败（${res.status}）` });
      } else {
        setProfile(body.profile ?? {});
        setMsg({ ok: true, text: '已保存' });
      }
    } catch (err) {
      setMsg({ ok: false, text: `保存失败：${(err as Error).message}` });
    } finally {
      setBusy(false);
    }
  };

  const shown = displayName.trim() || profile.display_name || username || '未命名';
  const initial = shown.slice(0, 1).toUpperCase();

  return (
    <div className="page">
      <h2 className="page-title">用户</h2>
      <p className="page-sub">昵称与头像会显示在界面右上角。</p>

      {loading ? (
        <div className="card" style={{ fontSize: 12, color: 'var(--s-muted)' }}>
          加载中…
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1fr)', gap: 14 }}>
          <div className="card">
            <div className="card-title">资料</div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
              {avatar ? (
                <img
                  src={avatar}
                  alt=""
                  width={56}
                  height={56}
                  style={{ borderRadius: 28, objectFit: 'cover', border: '1px solid var(--s-hairline)' }}
                  onError={(e) => {
                    // 图挂了就退回色块，避免显示裂图
                    (e.target as HTMLImageElement).style.display = 'none';
                  }}
                />
              ) : (
                <div
                  style={{
                    width: 56,
                    height: 56,
                    borderRadius: 28,
                    background: color,
                    color: 'var(--s-ink-plum)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 24,
                    fontWeight: 700,
                  }}
                >
                  {initial}
                </div>
              )}
              <div>
                <div style={{ fontSize: 14, fontWeight: 600 }}>{shown}</div>
                <div style={{ fontSize: 11, color: 'var(--s-violet-mid)' }}>@{username}</div>
              </div>
            </div>

            <div className="field">
              <label>昵称</label>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="留空则显示用户名"
              />
            </div>

            <div className="field">
              <label>头像图片地址（可选）</label>
              <input
                value={avatar}
                onChange={(e) => setAvatar(e.target.value)}
                placeholder="https://… 或本地路径；留空则用色块"
              />
              <div className="hint">不填就用下面的颜色 + 昵称首字母。</div>
            </div>

            <div className="field">
              <label>头像底色</label>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {COLORS.map((c) => (
                  <button
                    key={c}
                    onClick={() => setColor(c)}
                    title={c}
                    style={{
                      width: 26,
                      height: 26,
                      borderRadius: 13,
                      background: c,
                      cursor: 'pointer',
                      border: c === color ? '2px solid var(--s-white)' : '1px solid var(--s-hairline)',
                    }}
                  />
                ))}
              </div>
            </div>

            <button className="btn" onClick={() => void save()} disabled={busy}>
              {busy ? '保存中…' : '保存'}
            </button>
            {msg && (
              <span
                className="pill"
                style={{
                  marginLeft: 10,
                  color: msg.ok ? 'var(--st-done)' : 'var(--st-failed)',
                }}
              >
                {msg.ok ? '✓' : '✕'} {msg.text}
              </span>
            )}
          </div>

          <div className="card">
            <div className="card-title">账号</div>
            <dl className="kv">
              <dt>用户名</dt>
              <dd>@{username}</dd>
              <dt>昵称</dt>
              <dd>{profile.display_name || '（未设置）'}</dd>
              <dt>资料更新时间</dt>
              <dd>{profile.updated || '—'}</dd>
            </dl>

            <div
              style={{
                marginTop: 16,
                paddingTop: 14,
                borderTop: '1px solid var(--s-hairline)',
                fontSize: 12,
                color: 'var(--s-muted)',
                lineHeight: 1.7,
              }}
            >
              登录状态存在浏览器本地（localStorage）。在公用电脑上用完请点右上角「退出」，
              退出会清除本机保存的凭据。
            </div>

            <button
              className="btn ghost"
              style={{ marginTop: 12 }}
              onClick={() => {
                if (!window.confirm('退出登录？未保存的画布改动会丢失。')) return;
                clearAuth();
                window.location.hash = '#/home';
                window.location.reload();
              }}
            >
              退出登录
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
