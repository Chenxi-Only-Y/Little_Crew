/**
 * 模型字段的**组合框**：可自由输入的文本框 + 可筛选的下拉清单。
 *
 * ## 用户要的形态（原话）
 *
 *   "所有换成文本框 然后下面清单选" / "可以用户自己输入"
 *   → 清单形式选的是：「**输入框里带下拉箭头（输入框兼任筛选）**」
 *   → 显示时机选的是：「**默认收起，点「获取可用模型」才展开**」
 *
 * 所以这个组件的行为是：
 *   1. 平时就是个**文本框** —— 厂商出新模型可以直接打进去（这是硬需求：
 *      清单永远滞后，不能因为"清单里没有"就让人填不了）；
 *   2. 文本框右侧有**下拉箭头**，点开/聚焦时展开清单；
 *   3. 在文本框里**打字即筛选**；
 *   4. 清单来自调用方的 `options`（后端给的"厂商 + 注册表"并集）。
 *
 * ## 为什么不直接用 `<input list=...>`（原生 datalist）
 *
 * 试都不用试，两个原因：
 *   · datalist 的**样式不受控**（各浏览器差很多，本项目是深色主题，
 *     原生下拉在 Windows 上是白底 —— 这个坑本项目踩过，见 tokens.css 里
 *     `原生下拉白底白字` 那条修复记录）；
 *   · 不能显示"这个模型来自厂商还是项目清单"这类附加信息。
 *
 * ## 为什么单独一个文件
 *
 * `ConfigFieldEditor` 已经 500+ 行，再塞一个带键盘导航、点击外部关闭、
 * 筛选状态的小组件会很难读。而它是**通用**的（文字/图像/视频的模型字段
 * 都会用），独立出来也更好测。
 */

import { useEffect, useMemo, useRef, useState } from 'react';

export interface ModelOption {
  /** 模型名（也就是会存进设置的值） */
  id: string;
  /**
   * 类型标注（对话 / 嵌入）。空串表示未知。
   * 只对文字模型有意义 —— 选错嵌入模型会直接报错，所以界面上要标出来。
   */
  kind?: string;
}

interface Props {
  value: string;
  disabled?: boolean;
  /** 可选清单（已按当前提供者过滤） */
  options: readonly ModelOption[];
  /** 值的落盘回调（选中或输入完成时调用） */
  onChange: (v: string) => void;
  placeholder?: string;
}

/** 嵌入模型选了不能对话 —— 用醒目的红字提醒（与「获取可用模型」列表一致）。 */
function kindBadge(kind: string) {
  if (!kind) return null;
  const embed = /embed/i.test(kind);
  return (
    <span
      style={{
        marginLeft: 8,
        fontSize: 11,
        color: embed ? 'var(--st-failed)' : 'var(--text-faint)',
        whiteSpace: 'nowrap',
      }}
    >
      {embed ? '⚠ 不能对话' : kind}
    </span>
  );
}

export default function ModelCombo({ value, disabled, options, onChange, placeholder }: Props) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);
  /** 键盘导航当前高亮第几项（-1 = 没高亮） */
  const [active, setActive] = useState(-1);

  // 外部值变化时同步（切字段、或后端回填）。自己的回声不覆盖用户正在打的字。
  const lastPushed = useRef(value);
  useEffect(() => {
    if (value !== lastPushed.current) {
      lastPushed.current = value;
      setText(value);
    }
  }, [value]);

  // 点组件外部关闭清单
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  /** 打字即筛选。空串显示全部。 */
  const filtered = useMemo(() => {
    const q = text.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.id.toLowerCase().includes(q));
  }, [options, text]);

  const commit = (v: string) => {
    lastPushed.current = v;
    setText(v);
    onChange(v);
  };

  const pick = (v: string) => {
    commit(v);
    setOpen(false);
  };

  return (
    <div ref={boxRef} style={{ position: 'relative' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <input
          type="text"
          className="input"
          value={text}
          disabled={disabled}
          placeholder={placeholder ?? '模型名，例如 wan3.0-video'}
          style={{ flex: 1 }}
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => {
            // 失焦提交（点了清单项时会先触发 item 的 onMouseDown，
            // 所以这里用 mousedown 选、不抢焦点）
            if (text !== value) commit(text.trim());
          }}
          onKeyDown={(e) => {
            if (e.key === 'ArrowDown' && filtered.length) {
              e.preventDefault();
              setOpen(true);
              setActive((i) => Math.min(i + 1, filtered.length - 1));
            } else if (e.key === 'ArrowUp' && filtered.length) {
              e.preventDefault();
              setActive((i) => Math.max(i - 1, -1));
            } else if (e.key === 'Enter') {
              e.preventDefault();
              if (open && active >= 0 && filtered[active]) pick(filtered[active].id);
              else commit(text.trim());
            } else if (e.key === 'Escape') {
              setOpen(false);
            }
          }}
        />
        <button
          type="button"
          className="btn ghost"
          disabled={disabled}
          title="展开模型清单"
          style={{ fontSize: 12, padding: '4px 8px', whiteSpace: 'nowrap' }}
          // 用 mousedown 而不是 click：click 之前 input 会先 blur，
          // 而 blur 里会 commit + 可能重渲染，导致这次展开被吃掉。
          onMouseDown={(e) => {
            e.preventDefault();
            setOpen((v) => !v);
          }}
        >
          ▾
        </button>
      </div>

      {open && (
        <div
          style={{
            position: 'absolute',
            zIndex: 30,
            top: '100%',
            left: 0,
            right: 0,
            marginTop: 4,
            maxHeight: 260,
            overflowY: 'auto',
            background: 'var(--bg-elev)',
            border: '1px solid var(--line)',
            borderRadius: 12,
            boxShadow: '0 12px 32px rgba(0,0,0,.55)',
          }}
        >
          {filtered.length === 0 ? (
            <div style={{ padding: '10px 12px', fontSize: 12, color: 'var(--text-faint)' }}>
              {options.length === 0
                ? '清单还是空的 —— 点下面的「获取可用模型」，或直接输入模型名'
                : '没有匹配的模型；直接按回车就用你输入的名字'}
            </div>
          ) : (
            filtered.map((o, i) => (
              <div
                key={o.id}
                // mousedown 而非 click：避免 input 先 blur 再丢失这次点击
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(o.id);
                }}
                onMouseEnter={() => setActive(i)}
                style={{
                  padding: '7px 12px',
                  fontSize: 12.5,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  background: i === active ? 'rgba(110,123,255,.14)' : 'transparent',
                  color: o.id === value ? 'var(--text)' : 'var(--text-muted)',
                }}
              >
                <span style={{ flex: 1, wordBreak: 'break-all' }}>{o.id}</span>
                {kindBadge(o.kind ?? '')}
              </div>
            ))
          )}
          <div
            style={{
              padding: '6px 12px',
              fontSize: 11,
              color: 'var(--text-faint)',
              borderTop: '1px solid var(--line)',
            }}
          >
            共 {filtered.length} 个{text.trim() ? `（已按「${text.trim()}」筛选）` : ''} ·
            清单里没有的模型名可以直接输入
          </div>
        </div>
      )}
    </div>
  );
}
