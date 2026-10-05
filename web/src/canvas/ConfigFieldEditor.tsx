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
  /**
   * 下拉选项的**显示名**（值 → 友好名）。
   *
   * 注册表给的 `options` 是内部标识（`deepseek` / `zhipu` / `vllm`），
   * 直接显示对用户没有意义。后端 `/api/providers` 早就有友好名
   * （`DeepSeek` / `智谱 GLM` / `本地 vLLM`），只是下拉没用上。
   *
   * 用**可选 prop** 而不是改注册表加字段：注册表是后端派生的事实来源，
   * 为显示名再给它加一列会多一处需要同步的地方；而"值→名"的映射
   * 本来就在 provider 清单里，由调用方传入最直接。
   * 缺省时退回显示原值——不传也不会坏。
   */
  optionLabels?: Record<string, string>;
}

export default function ConfigFieldEditor({ field, current, onSaved, optionLabels }: Props) {
  const isSecret = field.secret || field.type === 'password';
  // 密钥字段初始留空（不回显）；其余回显当前值
  const [value, setValue] = useState<string>(
    isSecret ? '' : current === undefined || current === null ? '' : String(current),
  );
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  /**
   * 后端是否已存有该密钥。
   *
   * 后端把已配置的密钥下发成掩码哨兵 `••••已配置`，据此判断"要不要显示
   * 清除按钮"。注意 `current` 是**掩码**而不是真值，所以任何情况下都不能
   * 把它当值提交回后端（那会把掩码写进密钥字段）。
   */
  const hasStoredSecret = isSecret && String(current ?? '') === MASK_SENTINEL;

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

  /**
   * 数字项的提交：**不能把原始字符串直接送出去**。
   *
   * 三个坑：
   *   1. 清空输入框得到空串 `""`。后端 `runtime_settings.set_many` 的语义是
   *      「空串会**存为覆盖**」而不是"恢复默认"——用户以为清空=回默认，
   *      实际写进了一条空覆盖记录。所以空串必须走**显式清除**分支并说清楚。
   *   2. 输入 `abc`、或半途状态（`-`、`1e`）会得到 `NaN`，存进去后端只能靠
   *      `int()/float()` 兜，或直接崩在转换处。非有限数一律**拒绝提交**。
   *   3. 提交后要把输入框**回填成真实生效值**：否则用户清空后看到的是空框，
   *      而 store/后端里并不是空，界面与实际状态脱节（下一次失焦又会重新提交）。
   */
  const commitNumber = async (raw: string) => {
    const trimmed = raw.trim();

    if (trimmed === '') {
      setBusy(true);
      setMsg(null);
      const res = await saveSettings({ [field.key]: '' }, getToken());
      setBusy(false);
      if (res.ok) {
        setValue('');
        setMsg({ ok: true, text: '已清除覆盖，回退默认' });
        onSaved(field.key, '');
      } else {
        // 清除失败就把原值填回去，避免界面停在一个并未生效的空值上
        setValue(String(current ?? ''));
        setMsg({ ok: false, text: res.error ?? '清除失败' });
      }
      return;
    }

    const num = Number(trimmed);
    if (!Number.isFinite(num)) {
      setValue(String(current ?? ''));
      setMsg({ ok: false, text: `不是有效数字：${trimmed.slice(0, 20)}（未提交）` });
      return;
    }

    await commit(String(num));
  };

  /**
   * 下拉选项显示什么。
   *
   * 三种来源按优先级：
   *   1. `*_provider_mode` 用内置的 MODE_HINT（云端 API / 本地推理 / 未选择）；
   *   2. 调用方传入的 `optionLabels`（provider 友好名）；
   *   3. 都没有就显示原值——**绝不显示空白**，否则用户会以为下拉坏了。
   */
  const optionLabel = (o: string) => {
    if (field.key.endsWith('_provider_mode')) return MODE_HINT[o] ?? o;
    return optionLabels?.[o] ?? o;
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
            onBlur={() => void commitNumber(value)}
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
            placeholder={hasStoredSecret ? '已配置，留空沿用' : '未配置'}
            onChange={(e) => setValue(e.target.value)}
            // ⚠️ 密钥框**空值绝不提交**。
            //
            // 密钥不回显，所以 value 的初值恒为 ''。若无条件
            // `commit(value)`，用户只要点进这个框再点走（哪怕什么都没输），
            // 就会把已存的真实密钥抹掉——而后端把空串当"清除覆盖"，
            // 接口还返回 200、界面显示"已保存"。实测：
            //   PUT {"minimax_api_key":"sk-real"} → 库里有值
            //   PUT {"minimax_api_key":""}        → 库里变成 ''，原密钥没了
            // 这比"填了不生效"更糟：它是**静默删除用户的数据**。
            // 要清除请用下方显式的「清除」按钮。
            onBlur={() => {
              if (value.trim() === '') {
                setValue('');
                return;
              }
              void commit(value);
            }}
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
      {/*
        密钥的「清除」必须是**显式动作**。
        原先"清空输入框再失焦"就是隐式的清除路径，而密钥框初值本来就是空的，
        等于一碰就删（实测会把已存密钥抹掉）。做成按钮后意图明确、也免得误删。
      */}
      {hasStoredSecret && (
        <button
          type="button"
          className="btn ghost"
          disabled={busy}
          style={{ fontSize: 11, padding: '2px 6px', marginTop: 4 }}
          onClick={() => {
            if (!window.confirm(`确定要清除「${field.label}」已保存的密钥？`)) return;
            void commit('');
          }}
        >
          清除已保存的密钥
        </button>
      )}
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
