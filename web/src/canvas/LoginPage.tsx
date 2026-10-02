/**
 * 登录 / 注册页（F5）。
 *
 * 未登录时整个导演台被这页挡住——此前靠 `?token=` 手工传，
 * 正式使用不可行，而且没人知道参数从哪来。
 *
 * 设计要点：
 * - 同一表单支持"登录"与"注册"两个动作，避免用户在两页之间找入口。
 * - 后端业务失败是 HTTP 200 + `{status:"error", message}`，
 *   所以**不能只看 res.ok**，要读 `status`（见 `api/auth.ts`）。
 * - 记住最近使用的用户名（不记密码）。
 */

import { useState } from 'react';
import { login, register } from '../api/auth';

const LAST_USER_KEY = 'lc_last_user';

function lastUser(): string {
  try {
    return window.localStorage.getItem(LAST_USER_KEY) ?? '';
  } catch {
    return '';
  }
}

export default function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState(lastUser());
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;

    const u = username.trim();
    if (!u) return setError('请填写用户名');
    if (!password) return setError('请填写密码');
    if (mode === 'register' && password !== confirm) return setError('两次输入的密码不一致');

    setBusy(true);
    setError(null);
    const res = mode === 'login' ? await login(u, password) : await register(u, password);
    setBusy(false);

    if (!res.ok) {
      setError(res.error ?? '操作失败');
      return;
    }
    try {
      window.localStorage.setItem(LAST_USER_KEY, u);
    } catch {
      /* 忽略隐私模式下的写入失败 */
    }
    onSuccess();
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'var(--s-ink-deep)',
      }}
    >
      <form
        onSubmit={submit}
        style={{
          width: 340,
          padding: '26px 26px 22px',
          border: '1px solid var(--s-hairline)',
          borderRadius: 14,
          background: 'var(--s-ink)',
        }}
      >
        <div style={{ fontSize: 20, fontWeight: 600, marginBottom: 2 }}>
          小剧组 <span style={{ color: 'var(--s-lime)' }}>导演台</span>
        </div>
        <p style={{ fontSize: 12, color: 'var(--s-muted)', margin: '0 0 18px' }}>
          {mode === 'login' ? '登录后继续你的创作' : '创建一个账号开始使用'}
        </p>

        <label style={{ display: 'block', fontSize: 12, marginBottom: 4 }}>用户名</label>
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          autoFocus
          style={inputStyle}
        />

        <label style={{ display: 'block', fontSize: 12, margin: '12px 0 4px' }}>密码</label>
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          style={inputStyle}
        />

        {mode === 'register' && (
          <>
            <label style={{ display: 'block', fontSize: 12, margin: '12px 0 4px' }}>
              确认密码
            </label>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              style={inputStyle}
            />
          </>
        )}

        {error && (
          <div style={{ marginTop: 12, fontSize: 12, color: 'var(--st-failed)' }}>✕ {error}</div>
        )}

        <button
          className="btn"
          type="submit"
          disabled={busy}
          style={{ width: '100%', marginTop: 18, justifyContent: 'center' }}
        >
          {busy ? '处理中…' : mode === 'login' ? '登录' : '注册并登录'}
        </button>

        <div style={{ marginTop: 14, fontSize: 12, color: 'var(--s-muted)', textAlign: 'center' }}>
          {mode === 'login' ? '还没有账号？' : '已有账号？'}
          <button
            type="button"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login');
              setError(null);
            }}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--s-lime)',
              cursor: 'pointer',
              fontSize: 12,
              fontFamily: 'inherit',
              padding: '0 4px',
            }}
          >
            {mode === 'login' ? '注册一个' : '去登录'}
          </button>
        </div>
      </form>
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  background: 'var(--s-white)',
  color: 'var(--s-ink-plum)',
  border: '1px solid var(--s-hairline-cool)',
  borderRadius: 6,
  padding: '7px 9px',
  fontSize: 13,
  fontFamily: 'inherit',
};
