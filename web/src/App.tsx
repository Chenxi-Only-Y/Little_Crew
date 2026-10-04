/**
 * 应用入口：按需登录门禁 + 五页路由。
 *
 * ## 登录改成"按需"了（用户要求）
 *
 * 以前是**启动门禁**：没登录就整页被登录页挡住。
 * 现在改成：**打开网页先看首页**，只有在"开始使用功能"时
 * （进入导演台 / 预演 / 设置）才弹登录卡片。
 *
 * 为什么这样更好：
 *   · 首页本来就是"最近创作 + 三个入口"，不需要身份也能看；
 *   · 启动就拦会造成"我还没决定要不要用，先让我注册"的劝退感；
 *   · 弹卡片能保留背后的首页画面（遮罩是半透明的），
 *     用户始终知道自己还在哪。
 *
 * 触发点在 `router`/`Shell`（导航按钮）与首页入口卡片，
 * 统一走 `requireAuth()` 这个自定义事件（见 `api/auth.ts` 的说明）。
 *
 * 站点结构（见 `app/router.ts`）：首页 / 导演台 / 预演 / 用户 / 设置。
 */

import { lazy, Suspense, useEffect, useState } from 'react';
import LoginPage from './canvas/LoginPage';
import Shell from './app/Shell';
import HomePage from './pages/HomePage';
import DirectorPage from './pages/DirectorPage';
import UserPage from './pages/UserPage';
import SettingsPage from './pages/SettingsPage';
import { usePage } from './app/router';
import { loadRegistry } from './api/registry';
import {
  clearAuth,
  consumePendingIntent,
  getToken,
  getUsername,
  REQUIRE_AUTH_EVENT,
  verifyToken,
} from './api/auth';
import { useCanvas } from './store/canvas';

/**
 * 预演编辑器**按需加载**。
 *
 * 它带 Three.js（约 1 MB+）。若静态引入，首页/导演台也得先下载完 3D 引擎才能显示——
 * 实测未拆分时单块 1.38 MB。拆开后主包只剩业务代码，
 * 用户点进「预演」时才拉那 1 MB。
 */
const PrevisPage = lazy(() => import('./previs/PrevisPage'));

type AuthState = 'checking' | 'anonymous' | 'authenticated';

export default function App() {
  const setRegistry = useCanvas((s) => s.setRegistry);
  const setError = useCanvas((s) => s.setError);
  const source = useCanvas((s) => s.source);
  const registry = useCanvas((s) => s.registry);
  const [loading, setLoading] = useState(true);
  const [auth, setAuth] = useState<AuthState>('checking');
  /** 登录卡片是否显示（受 requireAuth 事件驱动，也可被用户关掉） */
  const [authOpen, setAuthOpen] = useState(false);
  const page = usePage();

  // ---- 启动：确认登录态（但**不**用它拦页面） ----
  useEffect(() => {
    let alive = true;
    void (async () => {
      const token = getToken();
      if (!token) {
        if (alive) setAuth('anonymous');
        return;
      }
      // localStorage 里的 token 可能已失效（用户被删/库被清空）；
      // 不校验就会出现"界面像已登录、每个请求都 401"的迷惑状态。
      const ok = await verifyToken(token);
      if (!alive) return;
      if (!ok) clearAuth();
      setAuth(ok ? 'authenticated' : 'anonymous');
    })();
    return () => {
      alive = false;
    };
  }, []);

  /**
   * 监听"请求登录"事件（进导演台/预演/设置时由 Shell 与首页入口触发）。
   *
   * 已经是登录态时**不弹**——避免用户点导航时被自己的登录卡片挡一下。
   */
  useEffect(() => {
    const onRequire = () => {
      if (getToken()) return; // 已登录，直接放行
      setAuthOpen(true);
    };
    window.addEventListener(REQUIRE_AUTH_EVENT, onRequire);
    return () => window.removeEventListener(REQUIRE_AUTH_EVENT, onRequire);
  }, []);

  // ---- 已登录后才拉注册表 ----
  useEffect(() => {
    if (auth !== 'authenticated') return;
    let alive = true;
    void (async () => {
      const { registry: reg, source: src, error: err } = await loadRegistry();
      if (!alive) return;
      if (reg) setRegistry(reg, src);
      setError(err);
      setLoading(false);
    })();
    return () => {
      alive = false;
    };
  }, [auth, setRegistry, setError]);

  // 未登录时没有注册表可拉，别让状态条一直显示"加载中…"
  useEffect(() => {
    if (auth === 'anonymous') setLoading(false);
  }, [auth]);

  /**
   * 登录成功：关掉卡片，并**继续用户原本想做的事**。
   *
   * 例：用户点"进入导演台"→ 弹卡片 → 登录成功后自动进导演台，
   * 而不是让他再点一次（那是很烦的一步）。
   */
  const onLoginSuccess = () => {
    setAuth('authenticated');
    setAuthOpen(false);
    const intent = consumePendingIntent();
    intent?.();
  };

  const onLogout = () => {
    if (!window.confirm('退出登录？未保存的画布改动会丢失。')) return;
    clearAuth();
    setAuth('anonymous');
    // 整页重载：一并清掉 SSE 连接与内存中的画布状态，避免残留
    window.location.hash = '#/home';
    window.location.reload();
  };

  const sourceLabel =
    source === 'api' ? '后端注册表' : source === 'snapshot' ? '内置快照' : '未加载';

  const status = (
    <span className="pill-chip mono" title="注册表来源">
      {loading ? '加载中…' : sourceLabel}
      {registry && ` · ${registry.roles.length} 职业 / ${registry.nodes.length} 节点`}
    </span>
  );

  return (
    <>
      <Shell page={page} username={getUsername()} status={status} onLogout={onLogout}>
        {page === 'home' && <HomePage />}
        {page === 'director' && <DirectorPage />}
        {page === 'previs' && (
          <Suspense
            fallback={
              <div className="page">
                <p className="page-sub">正在加载 3D 引擎…</p>
              </div>
            }
          >
            <PrevisPage />
          </Suspense>
        )}
        {page === 'user' && <UserPage />}
        {page === 'settings' && <SettingsPage />}
      </Shell>

      {/* 登录卡片：按需弹出。`checking` 期间不弹，避免已登录用户被闪一下。 */}
      {authOpen && auth !== 'authenticated' && (
        <LoginPage onSuccess={onLoginSuccess} onClose={() => setAuthOpen(false)} />
      )}
    </>
  );
}
