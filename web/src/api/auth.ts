/**
 * 登录态。
 *
 * token 存在 localStorage（`lc_token`）。为什么用 localStorage 而不是 cookie：
 * - 后端 `EventSource`（SSE）**无法自定义请求头**，只能把 token 放查询参数；
 *   用 cookie 则要改成带 cookie 的 fetch-stream，改动面大。
 * - 这是本机/局域网自用工具，不是面向公网的多租户服务。
 *
 * ⚠️ 已知取舍：token 会出现在 SSE 请求的 URL 里（后端日志可见）。
 * loobpack 自用可接受；**若要暴露到公网，应改为一次性 ticket**
 * （先 POST 换一个短效 ticket，再用 ticket 订阅 SSE）。
 */

const TOKEN_KEY = 'lc_token';
const USER_KEY = 'lc_user';

export function getToken(): string | null {
  try {
    // 开发期便利：`?token=` 可直接打开导演台联调（会写入 localStorage 后生效）。
    // 正式使用走登录页。
    const fromUrl = new URLSearchParams(window.location.search).get('token');
    if (fromUrl) {
      window.localStorage.setItem(TOKEN_KEY, fromUrl);
      return fromUrl;
    }
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getUsername(): string | null {
  try {
    return window.localStorage.getItem(USER_KEY);
  } catch {
    return null;
  }
}

/**
 * 凭据发生变化（登录 / 退出 / 失效清除）时派发。
 *
 * ## 为什么需要（用户报的"啥情况"+过滤时灵时不灵）
 *
 * 设置页的设置在**组件挂载时只拉一次**（`useEffect(..., [])`）。若那一刻
 * token 还没就绪（或刚被清掉），`GET /api/settings` 会 401，页面就停在
 * "没有任何设置"的状态：所有字段用默认值。
 *
 * 后果不只是显示 —— 提供者下拉的过滤依赖"云端/本地"模式的值，
 * 模式读成 `unset` 就**不过滤**，于是本地与云端又混在一起。
 * 这类 bug 表现为**时灵时不灵**（取决于登录与挂载的先后），最难查。
 *
 * 有了这个事件，任何关心凭据的功能都可以在登录/退出后重新拉数据，
 * 而不用去猜"我挂载时 token 到底在不在"。
 */
export const AUTH_CHANGED_EVENT = 'lc:auth-changed';

function emitAuthChanged() {
  try {
    window.dispatchEvent(new CustomEvent(AUTH_CHANGED_EVENT));
  } catch {
    /* 非浏览器环境（测试）忽略 */
  }
}

function persist(token: string, username: string) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, username);
  } catch {
    /* localStorage 不可用（隐私模式）时静默降级为「本次会话有效」 */
  }
  emitAuthChanged();
}

export function clearAuth() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    /* 同上 */
  }
  emitAuthChanged();
}

export interface AuthResult {
  ok: boolean;
  token?: string;
  username?: string;
  error?: string;
}

async function post(path: string, username: string, password: string): Promise<AuthResult> {
  try {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const body = await res.json().catch(() => ({}));
    // 后端用 {status:"error", message} 表达业务失败（HTTP 仍是 200）
    if (!res.ok || body?.status === 'error' || !body?.token) {
      return { ok: false, error: body?.message || body?.detail || `请求失败（${res.status}）` };
    }
    persist(body.token as string, String(body.username || username));
    return { ok: true, token: body.token, username: String(body.username || username) };
  } catch (err) {
    return { ok: false, error: `无法连接后端：${(err as Error).message}` };
  }
}

export const login = (u: string, p: string) => post('/api/auth/login', u, p);
export const register = (u: string, p: string) => post('/api/auth/register', u, p);

/**
 * 校验当前 token 是否还有效（后端 `/api/settings` 需要登录）。
 *
 * 为什么需要：localStorage 里的 token 可能已失效（用户被删、库被清空）。
 * 不校验就会出现"界面像已登录、每个请求都 401"的迷惑状态。
 */
export async function verifyToken(token: string): Promise<boolean> {
  try {
    const res = await fetch('/api/settings', {
      headers: { Authorization: `Bearer ${token}` },
    });
    return res.ok;
  } catch {
    // 后端不可达 ≠ 未登录；按"有效"处理，让后续请求给出真实错误
    return true;
  }
}

/* ══════════════════════════════════════════════════════════════
   登录门禁（按需触发）
   ══════════════════════════════════════════════════════════════ */

/**
 * 什么时候需要登录：**开始使用某个功能时**，不是打开网页时。
 *
 * 用户要求："不用进网页就登录，是开始使用功能弹出卡片
 * （进入导演台或者预演或者设置的时候）"。
 *
 * 为什么用**自定义事件**而不是 React Context：
 * 触发点在 `router.navigate` 里（一个普通模块，不是组件），
 * 而监听点在 `AuthGate` 组件里。用事件能让两者解耦——
 * navigate 不需要知道有没有 Provider，也不需要把 auth 状态
 * 沿着组件树往下传。
 */
export const REQUIRE_AUTH_EVENT = 'lc:require-auth';

/** 需要登录才能进入的页面。`home` 与 `user` 不需要。 */
const GATED_PAGES = new Set(['director', 'previs', 'settings']);

export function pageNeedsAuth(page: string): boolean {
  return GATED_PAGES.has(page);
}

/**
 * 请求登录：弹出登录卡片。
 *
 * `onSuccess` 会在登录成功后调用（用来继续用户原本想做的事，
 * 例如"登录完自动进入导演台"）。只有**最近一次**请求的回调会被记住——
 * 用户连点两次入口时，应该继续最后一次意图。
 */
let pending: (() => void) | null = null;

export function requireAuth(onSuccess?: () => void): void {
  pending = onSuccess ?? null;
  window.dispatchEvent(new CustomEvent(REQUIRE_AUTH_EVENT));
}

/**
 * 任何请求收到 **401** 时调用：清掉失效凭据并请用户重新登录。
 *
 * ## 为什么需要（用户报的 bug）
 *
 * 原先 token 在**页面使用期间**失效时，前端是**静默失败**：
 *
 *   · 保存设置 → `PUT /api/settings` 401 → 只显示一句"保存失败"，
 *     用户以为是后端坏了或自己填错了，**不会想到要重新登录**；
 *   · 更迷惑的是：界面顶部仍显示 `director`、仍像是登录态，
 *     而每个写操作都悄悄不生效（用户原话"本地选择过后切换不了云端"）。
 *
 * 后端把 token 有效期设成 90 天，所以这不是"每天都会遇到"，
 * 而是"某一天突然全都保存不了"——**越罕见越难排查**，
 * 因此必须给一个明确的出口。
 *
 * 做成"收到 401 就调"的通用函数，调用方只需传一次，
 * 不必各自写一套判断。
 */
export function handleUnauthorized(): void {
  clearAuth();
  requireAuth();
}

/** 供 `AuthGate` 在登录成功后取走并清空待执行动作。 */
export function consumePendingIntent(): (() => void) | null {
  const fn = pending;
  pending = null;
  return fn;
}

