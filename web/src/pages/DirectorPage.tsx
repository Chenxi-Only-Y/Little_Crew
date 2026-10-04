/**
 * 导演台页：画布 + 节点库 + 属性面板 + 运行/工程栏。
 *
 * 这是应用的核心页面，其余三页（首页/用户/设置）是它的配套：
 *   首页 = 从哪进来；设置 = 配好模型；用户 = 谁在用。
 */

import { useMemo, useState } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import Canvas from '../canvas/Canvas';
import NodeLibrary from '../canvas/NodeLibrary';
import Inspector from '../canvas/Inspector';
import RunBar, { ThinkingPanel } from '../canvas/RunBar';
import ReviewPanel from '../canvas/ReviewPanel';
import ProjectBar from '../canvas/ProjectBar';
import { useCanvas } from '../store/canvas';
import { specOf, validateTopology } from '../domain/validate';

export default function DirectorPage() {
  const registry = useCanvas((s) => s.registry);
  const nodes = useCanvas((s) => s.nodes);
  const edges = useCanvas((s) => s.edges);
  const error = useCanvas((s) => s.error);
  const layoutFromRegistry = useCanvas((s) => s.layoutFromRegistry);
  const [showIssues, setShowIssues] = useState(false);

  /** 画布可执行性：与后端 `/api/registry/validate` 同一套规则 */
  const validation = useMemo(() => {
    const spec = registry ? specOf(registry) : null;
    if (!spec) return null;
    return validateTopology(
      spec,
      nodes.map((n) => n.id),
      edges.map((e) => ({ source: e.source, target: e.target, artifact: e.artifact })),
    );
  }, [registry, nodes, edges]);

  return (
    <ReactFlowProvider>
      <div className="app-statusbar">
        <RunBar />
        {validation && (
          <button
            className="btn ghost"
            onClick={() => setShowIssues((v) => !v)}
            style={{
              color: validation.ok ? 'var(--st-done)' : 'var(--st-review)',
              borderColor: validation.ok ? 'var(--st-done)' : 'var(--st-review)',
            }}
            title="复用后端同一套规则做预校验（后端执行时还会再校验一次）"
          >
            {validation.ok
              ? `✓ 拓扑可执行${validation.order.length ? ` · ${validation.order.length} 步` : ''}`
              : `⚠ ${validation.issues.length} 个问题`}
          </button>
        )}
        <button
          className="btn ghost"
          onClick={() => registry && layoutFromRegistry(registry)}
          title="按部门从左到右、执行顺序从上到下重新铺开"
        >
          按执行顺序铺开
        </button>
      </div>

      {showIssues && validation && !validation.ok && (
        <div className="issue-panel">
          {validation.issues.map((it, i) => (
            <div key={`${it.code}-${i}`}>
              <span style={{ color: 'var(--st-review)' }}>[{it.code}]</span> {it.message}
            </div>
          ))}
          <div style={{ marginTop: 4, opacity: 0.7 }}>
            提示：这是"选中的节点集合"级别的校验；把缺失的上游节点加入画布即可满足依赖。
          </div>
        </div>
      )}

      {error && <div className="issue-panel" style={{ color: 'var(--st-review)' }}>⚠ {error}</div>}

      <div className="app-body">
        <NodeLibrary />
        <Canvas />
        <Inspector />
      </div>

      <ReviewPanel />
      <ProjectBar />
      <ThinkingPanel />
    </ReactFlowProvider>
  );
}
