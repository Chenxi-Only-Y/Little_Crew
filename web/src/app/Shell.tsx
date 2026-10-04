/**
 * 应用外壳：光晕场 + 悬浮药丸导航 + 页面内容。
 *
 * ⚠️ 这里**没有常规导航栏**（设计决定）：
 *   一条 44px 通栏会在首屏切掉一整条"聚光"的高度，剧场感直接没了；
 *   也逼着每页把内容往下让、浪费竖向空间；视觉上还把"外壳"和"内容"
 *   分成两块，而剧场感要的是内容直接落在暗场里。
 *   改成**悬浮药丸**后：导航仍可点、可键盘聚焦、可读屏，但**不占内容高度**。
 *
 * 登录门禁在 `App.tsx`：未登录时这里根本不会渲染。
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
      {/* 舞台光：固定不动，内容从光里穿过 */}
      <div className="glow-stage" aria-hidden="true" />

      <nav className="pill-dock" aria-label="主导航">
        <span className="brand en">
          小剧组<span>DIRECTOR CONSOLE</span>
        </span>

        {PAGES.filter((p) => !p.hidden).map((p) => (
          <button
            key={p.key}
            type="button"
            className="nav-pill"
            aria-current={p.key === page ? 'page' : undefined}
            onClick={() => navigate(p.key)}
            title={p.desc}
          >
            <span aria-hidden="true" className="nav-pill-icon">{p.icon}</span>
            {p.label}
          </button>
        ))}

        <span className="dock-spacer" />

        {actions}
        {status}

        <span className="pill-chip" title={username ? `已登录：${username}` : '未登录'}>
          {username ?? '未登录'}
        </span>
        <button type="button" className="nav-pill nav-pill-quiet" onClick={onLogout} title="退出登录">
          退出
        </button>
      </nav>

      {children}
    </div>
  );
}
