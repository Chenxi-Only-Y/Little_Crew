/**
 * 节点库 —— 左侧按部门分组的环节清单，支持拖入画布 / 点击加入。
 *
 * 数据全部来自注册表（`registry.roles` / `registry.departments`），
 * 前端不自己维护职业清单。
 *
 * 拖拽实现：库这侧只负责把节点 key 放进 dataTransfer；
 * 坐标换算交给画布侧用 React Flow 的 `screenToFlowPosition`（官方做法）。
 */

import { useCanvas } from '../store/canvas';
import { STATUS_COLOR } from './NodeCard';
import type { StageStatus } from '../domain/registry';

export const DRAG_MIME = 'application/x-lc-node';

export default function NodeLibrary() {
  const registry = useCanvas((s) => s.registry);
  const addNode = useCanvas((s) => s.addNode);
  const nodes = useCanvas((s) => s.nodes);

  if (!registry) return <aside className="node-library" />;

  const onCanvas = new Set(nodes.map((n) => n.id));

  return (
    <aside className="node-library">
      {registry.departments.map((dept) => {
        const roles = registry.roles.filter((r) => r.dept === dept.key);
        if (roles.length === 0) return null;
        return (
          <div key={dept.key}>
            <div className="dept-title">
              {dept.label}
              <span style={{ marginLeft: 6, opacity: 0.6, textTransform: 'none' }}>
                {dept.desc}
              </span>
            </div>
            {roles.map((r) => {
              const node = registry.nodes.find((n) => n.key === r.node);
              const placed = onCanvas.has(r.node);
              const status = (nodes.find((n) => n.id === r.node)?.data.status ??
                'pending') as StageStatus;
              return (
                <button
                  key={r.key}
                  className="lib-item"
                  draggable
                  title={node ? `输入 ${node.consumes.length} · 输出 ${node.produces.length}` : r.duty}
                  onDragStart={(e) => {
                    e.dataTransfer.setData(DRAG_MIME, r.node);
                    e.dataTransfer.effectAllowed = 'move';
                  }}
                  onClick={() =>
                    addNode(r.node, { x: 40 + Math.random() * 60, y: 40 + Math.random() * 60 })
                  }
                  style={placed ? { opacity: 0.55 } : undefined}
                >
                  <span className="lib-dot" style={{ background: STATUS_COLOR[status] }} />
                  <span>{r.label}</span>
                  <span className="lib-key">{r.node}</span>
                </button>
              );
            })}
          </div>
        );
      })}
    </aside>
  );
}
