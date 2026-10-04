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

      {/* 导航**任何滚动位置都完全透明**，不加底、不随滚动变化。
          曾短暂加过"滚动后的小面板"，但用户要的是透明，已去掉。
          所以这里也没有滚动监听——那只是为那块底服务的死代码。 */}
      <nav className="pill-dock" aria-label="主导航">
        {/* ⚠️ 这里**不放品牌字**了。
            原先左上角有"小剧组 · DIRECTOR CONSOLE"，但它既挤在居中的药丸
            旁边、又在每个页面重复出现；用户要求把它移到首屏左下角当铭牌。
            导航因此只剩"药丸 + 右侧控制"，更干净。 */}
        <div className="dock-center">
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
        </div>

        <span className="dock-spacer" />

        {/* 右侧控制同样需要"滚动时一小块底"（见 `.dock-center` 的说明） */}
        <div className="dock-right">
          {actions}
          {status}

          <span className="pill-chip" title={username ? `已登录：${username}` : '未登录'}>
            {username ?? '未登录'}
          </span>
          <button type="button" className="nav-pill nav-pill-quiet" onClick={onLogout} title="退出登录">
            退出
          </button>
        </div>
      </nav>

      {children}
    </div>
  );
}
