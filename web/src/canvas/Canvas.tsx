/**
 * 画布 —— React Flow 容器。
 *
 * F1 目标：只读渲染全部节点、端口与状态；支持拖拽/缩放/框选。
 * F2 会在此加：从节点库拖入、连线合法性校验（复用后端同一份注册表规则）。
 */

import {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useCallback, useMemo, useState } from 'react';
import { nodeTypes } from './RoleNode';
import { DRAG_MIME } from './NodeLibrary';
import { useCanvas } from '../store/canvas';
import { KIND_COLOR } from './NodeCard';
import { edgeAllowed, edgeReason, specOf } from '../domain/validate';
import type { ArtifactKind } from '../domain/registry';

export default function Canvas() {
  const nodes = useCanvas((s) => s.nodes);
  const edges = useCanvas((s) => s.edges);
  const registry = useCanvas((s) => s.registry);
  const selectedId = useCanvas((s) => s.selectedId);
  const select = useCanvas((s) => s.select);
  const moveNode = useCanvas((s) => s.moveNode);
  const addNode = useCanvas((s) => s.addNode);
  const connect = useCanvas((s) => s.connect);
  const { screenToFlowPosition } = useReactFlow();

  /**
   * 只在拖拽结束时把位置写回 store。
   *
   * 注意：**不能**在这里喂 `applyNodeChanges`——那会让每次渲染都用 store 里的旧坐标
   * 覆盖 React Flow 的内部状态，导致拖拽"弹回"。由 React Flow 自己管拖拽中的状态。
   */
  const onNodeDragStop = useCallback(
    (_: unknown, node: Node) => moveNode(node.id, node.position),
    [moveNode],
  );

  /** 从节点库拖入：用官方 API 把屏幕坐标换算成画布坐标 */
  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const nodeKey = e.dataTransfer.getData(DRAG_MIME);
      if (!nodeKey) return;
      addNode(nodeKey, screenToFlowPosition({ x: e.clientX, y: e.clientY }));
    },
    [addNode, screenToFlowPosition],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const kindOf = useCallback(
    (key: string): ArtifactKind =>
      registry?.artifacts.find((a) => a.key === key)?.kind ?? 'json',
    [registry],
  );

  /**
   * 拖线时的即时合法性判断（复用后端下发的 validation_spec 规则）。
   *
   * 返回 false 时 React Flow 会拒绝建立连线，用户当场就知道"连不上"，
   * 而不是拖完才报错。
   */
  const spec = useMemo(() => (registry ? specOf(registry) : null), [registry]);
  const [notice, setNotice] = useState<string | null>(null);

  const isValidConnection = useCallback(
    (c: Connection | Edge) => {
      if (!spec) return true; // 没拿到规格时不阻止（后端仍会再校验）
      const src = 'source' in c ? c.source : '';
      const dst = 'target' in c ? c.target : '';
      if (!src || !dst || src === dst) return false;
      const res = edgeAllowed(spec, src, dst);
      if (!res.ok) setNotice(edgeReason(spec, src, dst));
      return res.ok;
    },
    [spec],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      const artifact = (c.sourceHandle as string) || '';
      connect(c.source, c.target, artifact);
      setNotice(null);
    },
    [connect],
  );

  const rfNodes: Node[] = useMemo(
    () =>
      nodes.map((n) => ({
        id: n.id,
        type: n.type,
        position: n.position,
        data: n.data as unknown as Record<string, unknown>,
        selected: n.id === selectedId,
      })),
    [nodes, selectedId],
  );

  const rfEdges: Edge[] = useMemo(
    () =>
      edges.map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        sourceHandle: e.artifact,
        targetHandle: e.artifact,
        // 产物形态着色，便于一眼看出数据流类型
        style: { stroke: KIND_COLOR[kindOf(e.artifact)], strokeWidth: 1.4, opacity: 0.75 },
        animated: false,
      })),
    [edges, kindOf],
  );

  return (
    <div className="canvas-wrap">
      {nodes.length === 0 && (
        <div className="empty-hint">
          画布为空。从左侧节点库拖入环节，或点击工具栏「按执行顺序铺开」。
        </div>
      )}
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={(_, n) => select(n.id)}
        onPaneClick={() => select(null)}
        onDrop={onDrop}
        onDragOver={onDragOver}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        fitView
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#2b2344" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          style={{ width: 180, height: 120 }}
          maskColor="rgba(21,15,35,0.72)"
          nodeColor={(n) => {
            const d = n.data as { status?: string } | undefined;
            return d?.status === 'done' ? '#c2ef4e' : '#4a3f6b';
          }}
        />
      </ReactFlow>

      {notice && (
        <div
          style={{
            position: 'absolute',
            left: 12,
            bottom: 12,
            maxWidth: 520,
            padding: '8px 12px',
            fontSize: 11,
            lineHeight: 1.6,
            color: 'var(--s-white)',
            background: 'rgba(229,72,77,0.14)',
            border: '1px solid var(--st-failed)',
            borderRadius: 8,
          }}
          onClick={() => setNotice(null)}
          title="点击关闭"
        >
          ✕ 连线被拒绝：{notice}
        </div>
      )}
    </div>
  );
}
