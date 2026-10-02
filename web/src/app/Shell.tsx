/**
 * 应用外壳：顶部导航 + 页面切换。
 *
 * 站点只有四页（首页 / 导演台 / 用户 / 设置），见 `router.ts`。
 * 登录门禁留在 `App.tsx`：未登录时这里根本不会渲染。
 */

import { PAGES, navigate, type PageKey } from './router';

interface Props {
  page: PageKey;
  username: string | null;
  /** 顶栏右侧的页面自定义操作（如导演台的运行按钮） */
  actions?: React.ReactNode;
  status?: React.ReactNode;
  onLogout: () => void;
  children: React.ReactNode;
}

export default function Shell({ page, username, actions, status, onLogout, children }: Props) {
  return (
    <div className="app-shell">
      <header className="app-toolbar">
        <div className="app-brand">
          小剧组 · <span>导演台</span>
        </div>

        <nav className="app-nav">
          {PAGES.map((p) => (
            <button
              key={p.key}
              className={`nav-item${p.key === page ? ' active' : ''}`}
              onClick={() => navigate(p.key)}
              title={p.desc}
            >
              <span style={{ opacity: 0.7, marginRight: 4 }}>{p.icon}</span>
              {p.label}
            </button>
          ))}
        </nav>

        {status}

        <div className="app-spacer" />

        {actions}

        <span className="pill" title={username ? `已登录：${username}` : '未登录'}>
          ☺ {username ?? '未登录'}
        </span>
        <button className="btn ghost" onClick={onLogout} title="退出登录">
          退出
        </button>
      </header>

      {children}
    </div>
  );
}
