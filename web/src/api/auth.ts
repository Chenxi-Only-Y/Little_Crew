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

function persist(token: string, username: string) {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, username);
  } catch {
    /* localStorage 不可用（隐私模式）时静默降级为「本次会话有效」 */
  }
}

export function clearAuth() {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    /* 同上 */
  }
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
