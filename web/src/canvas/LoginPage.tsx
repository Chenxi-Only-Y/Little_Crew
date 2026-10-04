/**
 * 登录 / 注册卡片。
 *
 * ⚠️ 两个容易搞错的地方，先写在这里：
 *
 * 1. **产品名是「小剧组·Little Crew」**，不是「小剧组 导演台」。
 *    "导演台"只是其中一个**页面**的名字，不是产品名。
 *    （我先前把登录页标题写成「小剧组 导演台」，已改。）
 *
 * 2. 本组件是**弹层**而不是整页。它是被 `AuthGate` 在需要登录时
 *    覆盖上来的（进入导演台/预演/设置时触发），所以它：
 *      · 不再自己占 `100vh`，而是 `position: fixed` 铺一层遮罩；
 *      · 点遮罩可以关闭（`onClose`），未登录也能先看首页。
 *
 * 其它设计要点（保留原有决定）：
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

export default function LoginPage({
  onSuccess,
  onClose,
}: {
  onSuccess: () => void;
  /** 关闭弹层（未登录也可以先看首页）。不传则不能关。 */
  onClose?: () => void;
}) {
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
      className="auth-overlay"
      /* 点遮罩关闭。用 `e.target === e.currentTarget` 判断，避免点卡片内部也关掉。 */
      onMouseDown={(e) => {
        if (onClose && e.target === e.currentTarget) onClose();
      }}
    >
      <form className="card-frost auth-card" onSubmit={submit} role="dialog" aria-modal="true">
        {/* 品牌名：小剧组·Little Crew（中英同级，与首屏大标题一致） */}
        <div className="auth-brand">
          小剧组<span className="auth-brand-en">·Little Crew</span>
        </div>
        <p className="auth-sub">
          {mode === 'login' ? '登录后继续你的创作' : '创建一个账号开始使用'}
        </p>

        <label className="auth-label">用户名</label>
        <input
          className="auth-input"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          autoFocus
        />

        <label className="auth-label">密码</label>
        <input
          className="auth-input"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
        />

        {mode === 'register' && (
          <>
            <label className="auth-label">确认密码</label>
            <input
              className="auth-input"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
            />
          </>
        )}

        {error && <div className="auth-error">✕ {error}</div>}

        <button className="btn auth-submit" type="submit" disabled={busy}>
          {busy ? '处理中…' : mode === 'login' ? '登录' : '注册并登录'}
        </button>

        <div className="auth-switch">
          {mode === 'login' ? '还没有账号？' : '已有账号？'}
          <button
            type="button"
            className="auth-switch-btn"
            onClick={() => {
              setMode(mode === 'login' ? 'register' : 'login');
              setError(null);
            }}
          >
            {mode === 'login' ? '注册一个' : '去登录'}
          </button>
        </div>
      </form>
    </div>
  );
}
