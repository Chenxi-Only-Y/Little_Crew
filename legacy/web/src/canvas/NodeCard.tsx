/**
 * 节点卡片 —— 画布上每个环节的渲染单元。
 *
 * 契约（见 docs/recon/03-before-after.md §5）：
 *   节点显示 状态 / 部门 / 端口（输入·输出）/ 确认点徽标。
 *   端口的"能不能连"由注册表的 consumes/produces 决定，前端不自行判断。
 */

import { Handle, Position } from '@xyflow/react';
import type { ArtifactKind, StageStatus } from '../domain/registry';

/** 产物形态 -> 颜色（与 tokens.css 的状态色系协调） */
export const KIND_COLOR: Record<ArtifactKind, string> = {
  text: '#8fb8ff',
  json: '#c2ef4e',
  list: '#a8d94a',
  image: '#fa7faa',
  video: '#ff9d5c',
  audio: '#8ee6d0',
  score: '#ffd166',
  model: '#c99cff',
  timeline: '#7fd1ff',
};

export const STATUS_COLOR: Record<StageStatus, string> = {
  pending: 'var(--st-pending)',
  running: 'var(--st-running)',
  done: 'var(--st-done)',
  review: 'var(--st-review)',
  skipped: 'var(--st-skipped)',
  stale: 'var(--st-stale)',
  failed: 'var(--st-failed)',
};

export const STATUS_LABEL: Record<StageStatus, string> = {
  pending: '待运行',
  running: '运行中',
  done: '已完成',
  review: '待审核',
  skipped: '已跳过',
  stale: '待重算',
  failed: '失败',
};

export interface NodeCardProps {
  label: string;
  deptLabel: string;
  status: StageStatus;
  consumes: string[];
  produces: string[];
  hasCheckpoint: boolean;
  canSkip: boolean;
  selected: boolean;
  artifactLabel: (key: string) => string;
  artifactKind: (key: string) => ArtifactKind;
}

/** 端口行：输入在左、输出在右，按索引垂直分布以便连线 */
function ports(list: string[], side: 'in' | 'out', kindOf: (k: string) => ArtifactKind, labelOf: (k: string) => string) {
  const total = Math.max(1, list.length);
  return list.map((key, i) => {
    const top = `${((i + 1) / (total + 1)) * 100}%`;
    const color = KIND_COLOR[kindOf(key)] ?? 'var(--s-muted)';
    return (
      <div key={`${side}-${key}`} className="port-row" style={{ position: 'absolute', top, [side === 'in' ? 'left' : 'right']: 10 }}>
        <Handle
          id={key}
          type={side === 'in' ? 'target' : 'source'}
          position={side === 'in' ? Position.Left : Position.Right}
          style={{ background: color, width: 8, height: 8, position: 'relative', transform: 'none', left: 0, right: 0, top: 0 }}
        />
        <span className="port-chip" style={{ color }}>
          {labelOf(key)}
        </span>
      </div>
    );
  });
}

export default function NodeCard(props: NodeCardProps) {
  const { label, deptLabel, status, consumes, produces, hasCheckpoint, canSkip, selected } = props;
  const color = STATUS_COLOR[status] ?? 'var(--st-pending)';

  return (
    <div className={`node-card${selected ? ' selected' : ''}`} style={{ position: 'relative' }}>
      <div className="node-head">
        <span className="status-dot" style={{ background: color }} title={STATUS_LABEL[status]} />
        <span className="node-label">{label}</span>
        {hasCheckpoint && <span className="review-badge">⚑ 审核</span>}
        <span className="node-kind">{canSkip ? '可跳过' : '必跑'}</span>
      </div>

      <div className="node-meta">
        {deptLabel} · {STATUS_LABEL[status]}
      </div>

      {/* 端口区：给绝对定位留出高度 */}
      <div className="node-ports" style={{ position: 'relative', minHeight: 16 + Math.max(consumes.length, produces.length) * 16 }}>
        <div style={{ fontSize: 10, color: 'var(--s-violet-mid)' }}>输入 / 输出</div>
        {ports(consumes, 'in', props.artifactKind, props.artifactLabel)}
        {ports(produces, 'out', props.artifactKind, props.artifactLabel)}
      </div>
    </div>
  );
}
