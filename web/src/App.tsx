/**
 * 应用入口：登录门禁 + 四页路由。
 *
 * 启动顺序：先确认登录态 → 再拉注册表。
 * 为什么这个顺序重要：注册表是公开端点，未登录也能拉到；
 * 若先拉注册表，未登录用户会先看到画布闪一下再被踢回登录页。
 *
 * 站点结构（见 `app/router.ts`）：首页 / 导演台 / 用户 / 设置 —— 就这四页。
 * 运行相关操作（▶ 运行、项目保存）属于**导演台**，放在导演台的状态条里，
 * 而不是全局顶栏：它们只对当前画布有意义。
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
import { clearAuth, getToken, getUsername } from './api/auth';
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
  const page = usePage();

  // ---- 启动：确认登录态 ----
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
      const { verifyToken } = await import('./api/auth');
      const ok = await verifyToken(token);
      if (!alive) return;
      if (!ok) clearAuth();
      setAuth(ok ? 'authenticated' : 'anonymous');
    })();
    return () => {
      alive = false;
    };
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

  if (auth === 'checking') {
    return <div className="boot-screen">正在校验登录状态…</div>;
  }

  if (auth === 'anonymous') {
    return <LoginPage onSuccess={() => setAuth('authenticated')} />;
  }

  const onLogout = () => {
    if (!window.confirm('退出登录？未保存的画布改动会丢失。')) return;
    clearAuth();
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
  );
}
