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
  AUTH_CHANGED_EVENT,
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
   * 凭据变化（登录 / 退出 / 失效清除）时同步 `auth` 状态。
   *
   * ## ⚠️ 为什么必须监听（用户报的"不能点击登录"）
   *
   * `auth` 原先**只在启动时算一次**。而 token 是**运行期间**失效的
   * （后端 TTL 90 天，所以是"某一天突然全都 401"）。失效后：
   *
   *   · 设置页 401 → `handleUnauthorized()` → `clearAuth()` + `requireAuth()`
   *     → `setAuthOpen(true)`；
   *   · **但登录卡片的渲染条件是 `authOpen && auth !== 'authenticated'`**
   *     （见本文件下方），而 `auth` 还停在 `authenticated` ——
   *   · 于是卡片**渲染不出来**，用户面对一个"说未登录、却没有任何登录入口"
   *     的页面。点导航也没用（`onRequire` 里 `getToken()` 那次判断也过不去）。
   *
   * 用户的截图正是这个状态：设置页写着"登录已失效"，而页面上**只有一个
   * 不显眼的「重新登录」按钮**、没有任何自动弹出的登录卡片。
   */
  useEffect(() => {
    const sync = () => setAuth(getToken() ? 'authenticated' : 'anonymous');
    window.addEventListener(AUTH_CHANGED_EVENT, sync);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, sync);
  }, []);

  /**
   * 监听"请求登录"事件（进导演台/预演/设置时由 Shell 与首页入口触发，
   * 以及任何请求收到 401 时由 `handleUnauthorized()` 触发）。
   *
   * ## ⚠️ 为什么不再判断 `getToken()`
   *
   * 原先是 `if (getToken()) return;`，注释写着"已经是登录态时不弹，
   * 避免用户点导航时被卡片挡一下"。但**发起方比这里更清楚要不要登录**：
   *
   *   · 导航到受限页面时会先判断"没 token 才 requireAuth"；
   *   · 而**收到 401** 的路径恰恰相反 —— 那时 token **还在**（只是失效了），
   *     `handleUnauthorized()` 先 `clearAuth()` 再 `requireAuth()`，
   *     所以这里其实读不到它。真正的问题是**竞态**：若事件在 `clearAuth()`
   *     之前/之间被处理，`getToken()` 仍有值 → 直接 return → **卡片不弹**。
   *
   * 这正是用户截图里的状态：设置页说"登录已失效"，却**没有任何登录入口**。
   *
   * 现在无条件打开卡片。已登录时被多弹一次是可接受的（而且 `requireAuth()`
   * 的调用方都在"确实需要凭据"的位置）；反过来"该弹时没弹"会让用户
   * 彻底卡住 —— 两者代价差得很远。
   */
  useEffect(() => {
    const onRequire = () => setAuthOpen(true);
    window.addEventListener(REQUIRE_AUTH_EVENT, onRequire);
    return () => window.removeEventListener(REQUIRE_AUTH_EVENT, onRequire);
  }, []);

  /**
   * 拉注册表。
   *
   * ## ⚠️ 为什么**不能**只在 `authenticated` 时拉（用户报的 bug）
   *
   * 原先条件是 `if (auth !== 'authenticated') return;`。后果：
   *
   *   · `auth` 只在"有 token 且 token 有效"时才变成 `authenticated`；
   *   · token 失效时 `verifyToken` 返回 false → `clearAuth()` → `anonymous`；
   *   · 而 `anonymous` 在这里**直接 return，注册表永不加载**；
   *   · 设置页开头就是 `if (!registry) return <p>注册表尚未加载…</p>` ——
   *     于是整页空白、**什么都点不了，包括切换云端/本地**。
   *
   * 用户看到的正是这个（截图："注册表尚未加载，无法列出可配置项" +
   * 状态条"加载中…"）。
   *
   * 而 `loadRegistry()` **本来就有内置快照兜底**（`api/registry.ts`，
   * 未登录时回退到构建时从后端导出的快照）—— 那道兜底形同虚设，
   * 因为请求根本没发出去。
   *
   * 所以现在：**只要不在 `checking` 就拉**。未登录时拿到快照即可正常渲染，
   * 用户能看到界面、能被引导去登录，而不是面对一块白板。
   */
  useEffect(() => {
    if (auth === 'checking') return;
    let alive = true;
    void (async () => {
      const { registry: reg, source: src, error: err } = await loadRegistry(getToken());
      if (!alive) return;
      if (reg) setRegistry(reg, src);
      // 未登录时 "未登录，已使用内置快照" 不是错误，只是提示；
      // 但仍要 setLoading(false)，否则状态条永远显示"加载中…"
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
