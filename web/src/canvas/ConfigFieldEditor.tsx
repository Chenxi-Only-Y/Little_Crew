/**
 * 单个配置项的编辑器（属性面板用）。
 *
 * 支持的控件类型来自注册表 `ConfigField.type`：
 *   select / text / password / number / toggle / slider
 *
 * 三条行为约定：
 *   1. **密钥不回显**：后端把已配置的密钥掩码成 `••••已配置`，这里显示占位提示；
 *      留空=沿用原值，重填=替换。
 *   2. **值置空 = 清除覆盖**：回退到"模式默认"或全局默认（后端语义：空串=清除）。
 *   3. 保存失败要**明确提示**，不能静默——否则用户以为改了、其实没生效。
 */

import { useState } from 'react';
import { MASK_SENTINEL, saveSettings } from '../api/settings';
import { getToken } from '../api/auth';
import type { ConfigField } from '../domain/registry';

const MODE_HINT: Record<string, string> = {
  unset: '— 请选择 —',
  cloud: '云端 API',
  local: '本地推理',
};

interface Props {
  field: ConfigField;
  /** 后端返回的当前值（密钥为掩码或 undefined） */
  current: unknown;
  onSaved: (key: string, value: unknown) => void;
}

export default function ConfigFieldEditor({ field, current, onSaved }: Props) {
  const isSecret = field.secret || field.type === 'password';
  // 密钥字段初始留空（不回显）；其余回显当前值
  const [value, setValue] = useState<string>(
    isSecret ? '' : current === undefined || current === null ? '' : String(current),
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // 切换选中节点时同步外部值
  const currentStr = isSecret ? '' : current === undefined || current === null ? '' : String(current);
  const [lastKey, setLastKey] = useState(field.key);
  if (lastKey !== field.key) {
    setLastKey(field.key);
    setValue(currentStr);
    setMsg(null);
  }

  const commit = async (raw: string) => {
    setBusy(true);
    setMsg(null);
    // 空串 = 清除覆盖（后端语义），密钥留空也走这条
    const res = await saveSettings({ [field.key]: raw }, getToken());
    setBusy(false);
    if (res.ok) {
      setMsg({ ok: true, text: '已保存' });
      onSaved(field.key, raw);
    } else {
      setMsg({ ok: false, text: res.error ?? '保存失败' });
    }
  };

  const optionLabel = (o: string) => {
    if (field.key.endsWith('_provider_mode')) return MODE_HINT[o] ?? o;
    return o;
  };

  const control = (() => {
    switch (field.type) {
      case 'select': {
        const opts = field.options.length ? field.options : [];
        return (
          <select
            value={value}
            disabled={busy}
            onChange={(e) => {
              setValue(e.target.value);
              void commit(e.target.value);
            }}
          >
            {opts.map((o) => (
              <option key={o} value={o}>
                {optionLabel(o)}
              </option>
            ))}
          </select>
        );
      }
      case 'number':
        return (
          <input
            type="number"
            value={value}
            disabled={busy}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => void commit(value)}
          />
        );
      case 'slider':
        return (
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={value || '1'}
            disabled={busy}
            onChange={(e) => setValue(e.target.value)}
            onMouseUp={() => void commit(value)}
            onTouchEnd={() => void commit(value)}
          />
        );
      case 'toggle':
        return (
          <input
            type="checkbox"
            checked={value === 'true' || value === '1'}
            disabled={busy}
            onChange={(e) => {
              const v = e.target.checked ? '1' : '0';
              setValue(v);
              void commit(v);
            }}
          />
        );
      case 'password':
        return (
          <input
            type="password"
            value={value}
            disabled={busy}
            placeholder={current === MASK_SENTINEL ? '已配置，留空沿用' : '未配置'}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => void commit(value)}
          />
        );
      default:
        return (
          <input
            type="text"
            value={value}
            disabled={busy}
            onChange={(e) => setValue(e.target.value)}
            onBlur={() => void commit(value)}
          />
        );
    }
  })();

  return (
    <div className="field">
      <label>
        {field.label}
        {field.scope === 'role' && (
          <span style={{ marginLeft: 4, opacity: 0.6, fontSize: 9 }}>职业级</span>
        )}
      </label>
      {control}
      {field.help && <div className="hint">{field.help}</div>}
      {msg && (
        <div
          className="hint"
          style={{ color: msg.ok ? 'var(--st-done)' : 'var(--st-failed)' }}
        >
          {msg.ok ? '✓' : '✕'} {msg.text}
        </div>
      )}
    </div>
  );
}
