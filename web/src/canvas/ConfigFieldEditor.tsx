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

import { useEffect, useRef, useState } from 'react';

import ModelCombo from './ModelCombo';
import { MASK_SENTINEL, saveSettings } from '../api/settings';
import { getToken, handleUnauthorized } from '../api/auth';
import type { ConfigField } from '../domain/registry';

const MODE_HINT: Record<string, string> = {
  unset: '— 请选择 —',
  cloud: '云端 API',
  local: '本地推理',
};

/** 一个模型条目：名称 + 类型（`chat` 对话 / `embed` 嵌入 / `""` 未知）。 */
export interface ModelInfo {
  id: string;
  kind: string;
}

interface Props {
  field: ConfigField;
  /** 后端返回的当前值（密钥为掩码或 undefined） */
  current: unknown;
  onSaved: (key: string, value: unknown) => void;
  /**
   * 模型字段的**下拉清单**（只含当前提供者的模型，形如
   * `Seedance（火山方舟） · doubao-seedance-2-5-260628`）。
   *
   * 由调用方按"当前选中的提供者"过滤好再传进来 —— 组件自己不知道
   * 用户选了哪家，也不该去猜。
   *
   * ⚠️ 用户要求："前面厂商是什么后面才是什么厂商的模型，
   * 我不要下滑是所有的"。所以**不要**退回用注册表那份
   * "所有提供者并集"的 `field.options`。
   */
  modelChoices?: string[];
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
  /**
   * **限制**下拉能选哪些值（白名单）。
   *
   * ## 为什么光有 `optionLabels` 不够（这里返工过一次）
   *
   * `field.options` 来自**注册表**，而注册表是后端在**应用启动时**算好的
   * 静态快照——那会儿用户还没选"云端 / 本地"模式，所以它把两种模式的
   * 提供者都列进去了。
   *
   * 我第一版只按模式过滤了 `optionLabels`（显示名），于是：
   *   · 选项**还在**（用户照样能选中云端提供者 → 就是用户报的那个问题）；
   *   · 被滤掉显示名的那些，在列表里退化显示成内部 key（`deepseek`），
   *     比原来**更难看懂**。
   *
   * 所以真正要过滤的是**选项本身**：这个 prop 给出的值才进下拉，
   * `field.options` 只当"全集"用。
   *
   * 缺省（不传）时保持原行为：用 `field.options` 全部。
   */
  allowedOptions?: readonly string[];
  /**
   * 把下拉选项**分组显示**（`值 → 组名`）。用于"本地 / 云端"分块。
   *
   * 为什么用 `<optgroup>` 而不是在列表里插一行"── 云端 ──"：
   * 插入的占位项**本身是个 option**（有 value），一旦它的 value 等于
   * 当前值就会被回显成选中项，用户会看到一个假的选项。`optgroup` 是
   * 纯分组标签，没有 value，不会参与选中。
   *
   * 未提供分组的选项归入 `<optgroup label="">`（无标题），
   * 否则它们会从列表里消失。
   */
  optionGroups?: Record<string, string>;
  /**
   * 提供模型字段的**下拉清单**（只在"模型"这个字段上有意义）。
   *
   * 用户问题：选了「本地 vLLM / Ollama」后，"模型"一栏是空的 ——
   * 怎么知道本机有哪些模型？
   *
   * 注册表里给的 `known_models` 是**云端模型名**（DeepSeek/OpenAI 那类），
   * 对本地服务无用；本地装了什么只有本地服务自己知道。所以点按钮去问它。
   *
   * 云端同样适用（"如果是 api 就检测厂商模型"）——
   * 优先问厂商的 `/models`，问不到才回退到已核对的清单。
   */
}

export default function ConfigFieldEditor({
  field,
  current,
  onSaved,
  optionLabels,
  modelChoices,
  allowedOptions,
  optionGroups,
}: Props) {
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

  /**
   * 外部值变化时同步到本地状态。
   *
   * ## ⚠️ 这里修过两次，第二次是我自己引入的"时灵时不灵"
   *
   * **第一次**：原逻辑只在 `field.key` 变了时才同步 `value`。但设置是
   * **登录后异步到**的 —— 首渲染时 `current` 还是 `undefined`，`value` 被
   * 锁成 `''`（下拉显示"请选择"），后端值到了也不再同步。后果不止显示：
   * "按模式过滤提供者"依赖这个值，读成 `unset` 就**不过滤**。
   *
   * **第二次（本次）**：我改成"用户动过这个字段就不再自动跟随"。
   * 于是出现**时灵时不灵**：
   *   · 刷新页面 → 组件重挂载、标记重置 → 值正确（"灵"）；
   *   · 在同一页里改别的字段（比如切换云端/本地模式）→ 本组件没重挂载、
   *     标记仍是"动过" → `value` **卡在旧值**，与新算出的选项不匹配
   *     （"不灵"）。用户看到的就是"切了但第二项不变"。
   *
   * 正确判据不是"用户动过没有"，而是"**这个外部变化是不是我自己写入的
   * 回声**"：
   *   · `current` 等于我们上次提交的值 → 是我们自己的回声，跳过（否则会和
   *     用户的输入打架）；
   *   · `current` 变了且不是我们写的 → 是**外部**改的（换设置、别的入口、
   *     后端归一化），必须跟随 —— 否则界面显示的是过期值。
   *
   * 用 `useEffect` 而不是渲染期 setState：同步是副作用，写在渲染里会在
   * 并发渲染下重复执行。
   */
  const currentStr = isSecret ? '' : current === undefined || current === null ? '' : String(current);
  /** 我们最近一次提交的值。用来区分"自己的回声"与"外部改动"。 */
  const lastSavedRef = useRef<string | null>(null);

  useEffect(() => {
    if (currentStr === lastSavedRef.current) return; // 自己的回声，别动
    setValue(currentStr);
  }, [currentStr]);

  const [lastKey, setLastKey] = useState(field.key);
  if (lastKey !== field.key) {
    // 换字段：重置跟踪状态，并立即采用新字段的外部值
    setLastKey(field.key);
    lastSavedRef.current = null;
    setValue(currentStr);
    setMsg(null);
  }

  const commit = async (raw: string) => {
    // 记下"这是我自己写的值"：之后 `current` 变成它时就知道是自己的回声，
    // 不该再覆盖用户输入；而**其它**外部改动仍会正常同步进来
    // （见上面 useEffect 的说明 —— 用"动过没有"当判据会造成时灵时不灵）。
    lastSavedRef.current = raw;
    setBusy(true);
    setMsg(null);
    // 空串 = 清除覆盖（后端语义），密钥留空也走这条
    const res = await saveSettings({ [field.key]: raw }, getToken());
    setBusy(false);
    if (res.ok) {
      setMsg({ ok: true, text: '已保存' });
      onSaved(field.key, raw);
    } else if (res.status === 401) {
      // 未登录 / 凭据失效：**不能只说"保存失败"** —— 用户会以为是后端
      // 坏了或自己填错了，而真正该做的是重新登录。
      // （用户报的"本地选择过后切换不了云端"正是这个：界面看着已登录，
      //   每次保存都 401，却没有任何提示。）
      setMsg({ ok: false, text: '登录已失效，请重新登录后重试' });
      handleUnauthorized();
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
      // 走的是自己的分支（不经 commit），所以也要记下"这是我写的回声"
      lastSavedRef.current = '';
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
    // 占位符说人话。
    //
    // 注册表里存的值是 `(自动)` / `(按模式)` —— 都是"**不覆盖、跟随模式**"
    // 的意思，但用户看不懂这两个词有什么区别（其实没有区别，只是历史写法）。
    // 它们都是"不指定来源"：真正决定用哪家的是上面那个"云端 / 本地"。
    //
    // ⚠️ 值本身必须保持不变：后端 `providers.resolve_provider` 靠它判断
    //    "用户没设覆盖"（而它会跳过占位符继续用模式默认）。只改显示。
    if (o === '(自动)' || o === '(按模式)') return '跟随模式';
    return optionLabels?.[o] ?? o;
  };

  const control = (() => {
    switch (field.type) {
      case 'select': {
        /**
         * **模型字段** → 用 `ModelCombo`（可自由输入 + 可筛选的下拉清单）。
         *
         * ## 为什么不用普通 `<select>`（试过，不行）
         *
         * `video_model` / `image_model_*` / `llm_model_*` 的选项是**所有提供者
         * 的模型名并集**（后端 `options_from_models` 派生），于是：
         *
         *   · **当前值可能不在清单里** —— 用户手填过厂商新模型、或清单还没收录。
         *     普通下拉遇到这种情况会**退回显示第一项**，用户看到的值与后端存的
         *     不是一回事（本项目踩过好几次）；
         *   · 清单**永远滞后**：厂商上新模型时不会自动出现，所以必须**允许直接
         *     输入**（用户明确要求："可以用户自己输入"）。
         *
         * ## 我第一版做错的地方
         *
         * 我发明了"下拉 + 自定义…切文本框 + 从清单里选切回"这套 —— 用户要的是
         * **一个输入框带下拉箭头**，不是两个模式来回切。已改成 `ModelCombo`。
         *
         * ⚠️ 不做「测试」按钮 —— 用户明确说过"不要测试了"。
         */
        const isModelField = /(^|_)model(_|$)/.test(field.key);
        if (isModelField) {
          /**
           * 清单 = **当前提供者的模型**（`modelChoices`，由调用方按提供者过滤好，
           * 形如 `Seedance（火山方舟） · doubao-seedance-2-5-260628`）。
           *
           * ⚠️ 用户明确要求："**前面厂商是什么后面才是什么厂商的模型，
           * 我不要下滑是所有的**" —— 所以这里**不再**用注册表那份
           * "所有提供者并集"的 `field.options`（那会把 MiniMax、阿里百炼、
           * Seedance 的模型全混在一个下拉里）。
           *
           * `modelChoices` 没给时的兜底：用字段自带选项（老路径，避免半新半旧
           * 时下拉变空）。
           */
          const seen = new Set<string>();
          //: 显示名 → 存进去的值（显示 `厂商 · 模型`，但存的是**模型名**）
          const opts: Array<{ id: string; kind?: string; label?: string }> = [];
          for (const label of modelChoices ?? field.options) {
            if (!label) continue;
            // 显示名里的 ` · ` 后面才是模型名；没有分隔符就用整体
            const idx = label.lastIndexOf(' · ');
            const id = idx === -1 ? label : label.slice(idx + 3);
            if (!id || seen.has(id)) continue;
            seen.add(id);
            opts.push({ id, label });
          }
          return (
            <ModelCombo
              value={value}
              disabled={busy}
              options={opts}
              onChange={(v) => {
                setValue(v);
                void commit(v);
              }}
            />
          );
        }
        const all = field.options.length ? field.options : [];
        // allowedOptions 是白名单：不传=全集；传了就只留其中的
        // （"(自动)" 等非提供者项由调用方一并放进白名单）
        const opts = allowedOptions ? all.filter((o) => allowedOptions.includes(o)) : all;
        return (
          <select
            value={value}
            disabled={busy}
            onChange={(e) => {
              setValue(e.target.value);
              void commit(e.target.value);
            }}
          >
            {(() => {
              if (!optionGroups) {
                return opts.map((o) => (
                  <option key={o} value={o}>
                    {optionLabel(o)}
                  </option>
                ));
              }
              // 按组名归并，保持 opts 原顺序（组按首次出现排序）
              const order: string[] = [];
              const buckets = new Map<string, string[]>();
              for (const o of opts) {
                const g = optionGroups[o] ?? '';
                if (!buckets.has(g)) {
                  buckets.set(g, []);
                  order.push(g);
                }
                buckets.get(g)!.push(o);
              }
              return order.map((g) =>
                g ? (
                  <optgroup key={g} label={g}>
                    {buckets.get(g)!.map((o) => (
                      <option key={o} value={o}>
                        {optionLabel(o)}
                      </option>
                    ))}
                  </optgroup>
                ) : (
                  buckets.get(g)!.map((o) => (
                    <option key={o} value={o}>
                      {optionLabel(o)}
                    </option>
                  ))
                ),
              );
            })()}
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
