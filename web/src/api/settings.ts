/**
 * 运行时设置读写（供属性面板编辑节点配置）。
 *
 * 后端契约（`app/main.py`）：
 *   GET /api/settings   → {settings: {...}}（密钥已掩码为 `••••已配置`）
 *   PUT /api/settings   → 白名单由注册表派生；空串=清除覆盖回退默认；掩码哨兵会被丢弃
 *
 * 注意：白名单由 `registry.api.allowed_settings()` 派生，
 * **界面上能改的一定存得下**（此前手写白名单曾导致部分键被静默丢弃）。
 */

/** 密钥字段回传的掩码哨兵（与后端 `_MASK_SENTINEL` 一致） */
export const MASK_SENTINEL = '••••已配置';

export type SettingsMap = Record<string, unknown>;

export interface SettingsResult {
  ok: boolean;
  settings: SettingsMap;
  error?: string;
}

export async function fetchSettings(token?: string | null): Promise<SettingsResult> {
  try {
    const res = await fetch('/api/settings', {
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, settings: {}, error: body?.message || `接口 ${res.status}` };
    return { ok: true, settings: (body.settings ?? {}) as SettingsMap };
  } catch (err) {
    return { ok: false, settings: {}, error: (err as Error).message };
  }
}

export interface SaveResult {
  ok: boolean;
  settings?: SettingsMap;
  error?: string;
}

/**
 * 保存若干设置项。
 *
 * @param changes 只传要改的键；**值为空串表示清除该覆盖**（回退到默认/模式）
 */
export async function saveSettings(
  changes: SettingsMap,
  token?: string | null,
): Promise<SaveResult> {
  try {
    const res = await fetch('/api/settings', {
      method: 'PUT',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(changes),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) return { ok: false, error: body?.message || `接口 ${res.status}` };
    return { ok: true, settings: (body.settings ?? {}) as SettingsMap };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

export interface TestResult {
  ok: boolean;
  errorType?: string;
  detail?: string;
}

/** 测试某个 provider/model/key 是否连通（供属性面板的「测试」按钮） */
export async function testProvider(
  payload: SettingsMap,
  token?: string | null,
): Promise<TestResult> {
  try {
    const res = await fetch('/api/settings/test', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    return { ok: Boolean(body.ok), errorType: body.error_type, detail: body.detail };
  } catch (err) {
    return { ok: false, errorType: 'network', detail: (err as Error).message };
  }
}
