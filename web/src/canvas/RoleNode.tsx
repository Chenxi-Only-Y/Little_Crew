/**
 * React Flow 节点封装 —— 把注册表数据喂给通用的 NodeCard。
 *
 * 分层原因：React Flow 的 nodeTypes 需要稳定的组件引用，
 * 而端口标签/颜色要查注册表；这里做一次桥接，避免 NodeCard 依赖 store。
 */

import { memo, useCallback } from 'react';
import type { NodeProps } from '@xyflow/react';
import NodeCard from './NodeCard';
import type { CanvasNodeData } from '../store/canvas';
import type { ArtifactKind } from '../domain/registry';
import { useCanvas } from '../store/canvas';

function RoleNodeInner({ data, selected }: NodeProps) {
  const d = data as unknown as CanvasNodeData;
  const registry = useCanvas((s) => s.registry);

  const artifactLabel = useCallback(
    (key: string) => registry?.artifacts.find((a) => a.key === key)?.label ?? key,
    [registry],
  );
  const artifactKind = useCallback(
    (key: string): ArtifactKind => registry?.artifacts.find((a) => a.key === key)?.kind ?? 'json',
    [registry],
  );
  const deptLabel = registry?.departments.find((x) => x.key === d.dept)?.label ?? d.dept;

  return (
    <NodeCard
      label={d.label}
      deptLabel={deptLabel}
      status={d.status}
      consumes={d.consumes}
      produces={d.produces}
      hasCheckpoint={d.hasCheckpoint}
      canSkip={d.canSkip}
      selected={Boolean(selected)}
      artifactLabel={artifactLabel}
      artifactKind={artifactKind}
    />
  );
}

export const RoleNode = memo(RoleNodeInner);
export const nodeTypes = { role: RoleNode };
export type { CanvasNodeData };
export { RoleNodeInner as _RoleNodeInner };
export const nodeIdOf = (id: string) => id;
