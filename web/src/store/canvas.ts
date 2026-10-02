/**
 * 画布状态（Zustand）。
 *
 * 边界：
 *   - 这里只放**纯前端**状态（拓扑、选中、布局）
 *   - 注册表（职业/节点/端口）来自 `GET /api/registry`，不在此定义
 */

import { create } from 'zustand';
import type { Registry, StageStatus } from '../domain/registry';
import type { RunEvent } from '../api/sessions';

/** 「AI 思考过程」的一条记录 */
export interface ThinkingEntry {
  label: string;
  text: string;
  seq: number;
}

/** 会话运行态（由 SSE 事件驱动） */
export interface RunState {
  sessionId: string | null;
  /** 会话整体状态：idle / running / waiting_review / done / failed / cancelled */
  status: string;
  /** 当前环节 */
  current: string | null;
  /** 最近一次确认点的内容（F4 会用来渲染审核面板） */
  review: unknown;
  /** AI 思考过程（增量累积） */
  thinking: ThinkingEntry[];
  error: string | null;
}

/* 登录 token 的唯一实现在 `api/auth.ts`：本文件不再转发，
   避免出现第二个来源——两份实现一旦漂移，就会出现
   "某个调用点读不到 token"的隐蔽问题。 */

/** 画布上的一个节点 */
export interface CanvasNodeData {
  roleKey: string;
  nodeKey: string;
  label: string;
  dept: string;
  consumes: string[];
  produces: string[];
  canSkip: boolean;
  hasCheckpoint: boolean;
  status: StageStatus;
  /** 与注册表索引解耦：DataView 需要它来渲染端口 */
  [key: string]: unknown;
}

export interface CanvasNode {
  id: string;
  type: 'role';
  position: { x: number; y: number };
  data: CanvasNodeData;
}

export interface CanvasEdge {
  id: string;
  source: string;
  target: string;
  /** 该连线承载的产物 key */
  artifact: string;
}

export interface RegistryState {
  registry: Registry | null;
  source: 'api' | 'snapshot' | 'none';
  loading: boolean;
  error: string | null;

  nodes: CanvasNode[];
  edges: CanvasEdge[];
  selectedId: string | null;

  /** 运行态（SSE 驱动） */
  run: RunState;

  /**
   * 已处理的最大事件序号。
   *
   * EventSource 断线会自动重连，而服务端 `replay=True` 会把全部历史重发；
   * 不去重会导致思考日志成倍重复（实测累积出 30 条、实际只有 3 条）。
   */
  lastSeq: number;

  /* ---- 工程文件（F5） ---- */
  /** 当前工程 id（保存过才有；用于"覆盖保存"） */
  projectId: string | null;
  /** 当前工程名 */
  projectName: string;
  /** 是否有未保存改动 */
  dirty: boolean;

  /** 从注册表按执行顺序自动铺开节点（F1：只读基线布局） */
  layoutFromRegistry: (reg: Registry) => void;
  setRegistry: (reg: Registry, source: RegistryState['source']) => void;
  setError: (msg: string | null) => void;
  select: (id: string | null) => void;
  moveNode: (id: string, position: { x: number; y: number }) => void;
  /** 从节点库拖入一个新节点（F2） */
  addNode: (nodeKey: string, position: { x: number; y: number }) => void;
  /** 连线（F2 会做合法性校验） */
  connect: (source: string, target: string, artifact: string) => void;
  removeNode: (id: string) => void;
  clear: () => void;

  /* ---- 运行态操作（F3） ---- */
  beginRun: (sessionId: string) => void;
  /** 把一条 SSE 事件应用到画布 */
  applyEvent: (ev: RunEvent) => void;
  endRun: (message?: string) => void;
  /** 清掉/更新确认点内容（F4：做出决策后交回运行态） */
  applyReview: (review: unknown, status?: string) => void;

  /* ---- 工程文件（F5） ---- */
  /** 把当前画布序列化成可保存的文档（节点/边/视口） */
  toDoc: () => CanvasDocLike;
  /** 用工程文档**整体替换**当前画布；返回被丢弃的内容（供界面提示） */
  loadDoc: (doc: CanvasDocLike) => { droppedNodes: string[]; droppedEdges: number };
  /** 设置某节点的参数覆盖（属性面板保存后回填） */
  setNodeConfig: (id: string, key: string, value: unknown) => void;
  /** 设置某节点的备注 */
  setNodeNote: (id: string, note: string) => void;
  /** 记住当前工程（保存/打开后设置） */
  setProject: (id: string | null, name: string) => void;
  setDirty: (v: boolean) => void;
}

/** 画布文档形状（与 api/projects.ts 的 CanvasDoc 对齐） */
export interface CanvasDocLike {
  nodes: Array<{
    id: string;
    position: { x: number; y: number };
    config: Record<string, unknown>;
    note: string;
  }>;
  edges: Array<{
    source: string;
    target: string;
    sourceHandle?: string | null;
    targetHandle?: string | null;
  }>;
  viewport: Record<string, number>;
}

/** 画布节点 data 的可选字段（工程文件相关） */
export interface NodeExtras {
  /** 该节点的参数覆盖（形如 {image_provider_photographer: "local"}） */
  config?: Record<string, unknown>;
  /** 用户备注 */
  note?: string;
}

/** 部门 -> 画布列（按部门顺序从左到右铺） */
function deptColumn(reg: Registry, deptKey: string): number {
  const idx = reg.departments.findIndex((d) => d.key === deptKey);
  return idx < 0 ? 0 : idx;
}

const COL_W = 300;
const ROW_H = 170;

/**
 * 依据注册表的执行顺序铺开节点。
 *
 * 布局策略：部门决定列（左→右），执行顺序决定行（上→下）。
 * 这样画布一眼就能看出「哪个部门的哪个环节先跑」。
 */
export function buildLayout(reg: Registry): { nodes: CanvasNode[]; edges: CanvasEdge[] } {
  const order = reg.execution_order;
  const checkpointNodes = new Set(reg.checkpoints.map((c) => c.node));
  const roleByNode = new Map(reg.roles.map((r) => [r.node, r]));
  const rowInCol = new Map<string, number>();

  const nodes: CanvasNode[] = [];
  for (const nodeKey of order) {
    const node = reg.nodes.find((n) => n.key === nodeKey);
    if (!node) continue;
    // 只画有职业的节点 + 系统节点；这里全部画出来，便于看清完整链路
    const role = roleByNode.get(nodeKey);
    const col = deptColumn(reg, node.dept);
    const row = rowInCol.get(node.dept) ?? 0;
    rowInCol.set(node.dept, row + 1);

    nodes.push({
      id: nodeKey,
      type: 'role',
      position: { x: col * COL_W, y: row * ROW_H },
      data: {
        roleKey: role?.key ?? '',
        nodeKey: node.key,
        label: node.label,
        dept: node.dept,
        consumes: node.consumes,
        produces: node.produces,
        canSkip: node.can_skip,
        hasCheckpoint: checkpointNodes.has(node.key),
        status: 'pending',
        config: {},
        note: '',
      },
    });
  }

  // 按「产物」自动连线：某节点的 produces 命中另一节点的 consumes 就连
  const edges: CanvasEdge[] = [];
  const producers = new Map<string, string[]>();
  for (const n of nodes) {
    for (const a of n.data.produces) {
      const list = producers.get(a) ?? [];
      list.push(n.id);
      producers.set(a, list);
    }
  }
  for (const n of nodes) {
    for (const a of n.data.consumes) {
      for (const src of producers.get(a) ?? []) {
        if (src === n.id) continue;
        edges.push({ id: `${src}->${n.id}:${a}`, source: src, target: n.id, artifact: a });
      }
    }
  }

  return { nodes, edges };
}

export const useCanvas = create<RegistryState>((set, get) => ({
  registry: null,
  source: 'none',
  loading: false,
  error: null,

  nodes: [],
  edges: [],
  selectedId: null,

  run: {
    sessionId: null,
    status: 'idle',
    current: null,
    review: null,
    thinking: [],
    error: null,
  },

  lastSeq: -1,

  projectId: null,
  projectName: '未命名工程',
  dirty: false,

  layoutFromRegistry: (reg) => {
    const { nodes, edges } = buildLayout(reg);
    set({ nodes, edges, selectedId: null });
  },

  setRegistry: (reg, source) => {
    set({ registry: reg, source, error: null });
    get().layoutFromRegistry(reg);
  },

  setError: (msg) => set({ error: msg }),

  select: (id) => set({ selectedId: id }),

  moveNode: (id, position) =>
    set((s) => ({
      dirty: true,
      nodes: s.nodes.map((n) => (n.id === id ? { ...n, position } : n)),
    })),

  addNode: (nodeKey, position) => {
    const reg = get().registry;
    if (!reg) return;
    if (get().nodes.some((n) => n.id === nodeKey)) {
      // 已在画布上：只移动位置，避免重复节点
      get().moveNode(nodeKey, position);
      return;
    }
    const node = reg.nodes.find((n) => n.key === nodeKey);
    if (!node) return;
    const role = reg.roles.find((r) => r.node === nodeKey);
    const newNode: CanvasNode = {
      id: nodeKey,
      type: 'role',
      position,
      data: {
        roleKey: role?.key ?? '',
        nodeKey: node.key,
        label: node.label,
        dept: node.dept,
        consumes: node.consumes,
        produces: node.produces,
        canSkip: node.can_skip,
        hasCheckpoint: reg.checkpoints.some((c) => c.node === node.key),
        status: 'pending',
        config: {},
        note: '',
      },
    };
    set((s) => ({ nodes: [...s.nodes, newNode], selectedId: nodeKey, dirty: true }));
  },

  connect: (source, target, artifact) =>
    set((s) => {
      if (source === target) return s;
      const id = `${source}->${target}:${artifact}`;
      if (s.edges.some((e) => e.id === id)) return s;
      return { edges: [...s.edges, { id, source, target, artifact }], dirty: true };
    }),

  removeNode: (id) =>
    set((s) => ({
      nodes: s.nodes.filter((n) => n.id !== id),
      edges: s.edges.filter((e) => e.source !== id && e.target !== id),
      selectedId: s.selectedId === id ? null : s.selectedId,
      dirty: true,
    })),

  clear: () => set({ nodes: [], edges: [], selectedId: null, dirty: true }),

  /* ---------------- 工程文件（F5） ---------------- */

  toDoc: () => {
    const s = get();
    return {
      nodes: s.nodes.map((n) => {
        const d = n.data as unknown as NodeExtras;
        return {
          id: n.id,
          position: { x: n.position.x, y: n.position.y },
          config: d.config ?? {},
          note: d.note ?? '',
        };
      }),
      // 边只存拓扑与端口：id 与 artifact 可由这两者重新推导，
      // 存 id 反而会在节点改名后失效
      edges: s.edges.map((e) => ({
        source: e.source,
        target: e.target,
        sourceHandle: (e as unknown as { sourceHandle?: string }).sourceHandle ?? null,
        targetHandle: (e as unknown as { targetHandle?: string }).targetHandle ?? null,
      })),
      viewport: {},
    };
  },

  loadDoc: (doc) => {
    // 先算出会被丢弃的内容：`set` 的回调里没法把结果带出去，故在此先行计算。
    // 为什么必须告诉用户：工程来自旧版本时可能含已删除的节点，
    // 静默丢弃会让画布与保存时不一致，而用户以为"打开成功、内容完好"。
    const reg0 = get().registry;
    const known0 = new Set((reg0?.nodes ?? []).map((n) => n.key));
    const droppedNodes = doc.nodes.filter((n) => !known0.has(n.id)).map((n) => n.id);
    const kept0 = new Set(doc.nodes.filter((n) => known0.has(n.id)).map((n) => n.id));
    const droppedEdges = doc.edges.filter(
      (e) => !kept0.has(e.source) || !kept0.has(e.target),
    ).length;

    set((s): Partial<RegistryState> => {
      const reg = s.registry;
      if (!reg) return {};

      // 只保留注册表里仍存在的节点：工程可能来自旧版本，含已删除的节点
      const known = new Set(reg.nodes.map((n) => n.key));
      const kept = doc.nodes.filter((n) => known.has(n.id));
      const keptIds = new Set(kept.map((n) => n.id));

      const nodes: CanvasNode[] = kept.map((n) => {
        const meta = reg.nodes.find((r) => r.key === n.id)!;
        const role = reg.roles.find((r) => r.node === n.id);
        return {
          id: n.id,
          type: 'role',
          position: { x: n.position.x, y: n.position.y },
          data: {
            roleKey: role?.key ?? '',
            nodeKey: meta.key,
            label: meta.label,
            dept: meta.dept,
            consumes: meta.consumes,
            produces: meta.produces,
            canSkip: meta.can_skip,
            hasCheckpoint: reg.checkpoints.some((c) => c.node === meta.key),
            status: 'pending',
            config: n.config ?? {},
            note: n.note ?? '',
          },
        };
      });

      // 边两端都必须在画布上，否则会留下悬空连线。
      // `artifact` 不存盘（存了会在节点改名后失效），这里从两端的
      // produces/consumes 重新推导；推不出来就留空，连线依然成立。
      const edges = doc.edges
        .filter((e) => keptIds.has(e.source) && keptIds.has(e.target))
        .map((e) => {
          const src = nodes.find((n) => n.id === e.source)!;
          const dst = nodes.find((n) => n.id === e.target)!;
          const produced = src.data.produces as string[];
          const consumed = dst.data.consumes as string[];
          const artifact =
            consumed.find((a) => produced.includes(a)) ?? produced[0] ?? '';
          return { id: `${e.source}->${e.target}:${artifact}`, source: e.source, target: e.target, artifact };
        });

      return { nodes, edges, selectedId: null, dirty: false };
    });

    return { droppedNodes, droppedEdges };
  },

  setNodeConfig: (id, key, value) =>
    set((s) => ({
      dirty: true,
      nodes: s.nodes.map((n) =>
        n.id === id
          ? (() => {
              const d = n.data as unknown as NodeExtras;
              return { ...n, data: { ...n.data, config: { ...(d.config ?? {}), [key]: value } } };
            })()
          : n,
      ),
    })),

  setNodeNote: (id, note) =>
    set((s) => ({
      dirty: true,
      nodes: s.nodes.map((n) =>
        n.id === id ? { ...n, data: { ...n.data, note } } : n,
      ),
    })),

  setProject: (id, name) => set({ projectId: id, projectName: name, dirty: false }),

  setDirty: (v) => set({ dirty: v }),

  /* ---------------- 运行态（F3） ---------------- */

  beginRun: (sessionId) =>
    set((s) => ({
      run: {
        sessionId,
        status: 'running',
        current: null,
        review: null,
        thinking: [],
        error: null,
      },
      lastSeq: -1,
      // 新一轮开始：把节点状态清回 pending，边保留（拓扑不变）
      nodes: s.nodes.map((n) => ({ ...n, data: { ...n.data, status: 'pending' as StageStatus } })),
    })),

  applyEvent: (ev) =>
    set((s) => {
      const run = { ...s.run };

      /**
       * 幂等保护：EventSource 断线自动重连时，服务端 `replay=True` 会把全部历史重发。
       * 若不去重，思考日志会成倍重复（实测过一次运行累积出 30 条、实际只有 3 条）。
       * 用 `seq` 判重；工具类错误事件的 seq 为 -1，不参与去重。
       */
      if (ev.seq >= 0 && ev.seq <= s.lastSeq) {
        return {};   // 已处理过（重连重发），原样不动作
      }
      // 记录进度：所有分支都要带上它，否则后续事件会被误判为重复
      const seqPatch = ev.seq >= 0 ? { lastSeq: ev.seq } : {};

      /** 按阶段名更新节点状态；注册表里的节点 key 与阶段名一致 */
      const setStage = (stage: string, status: StageStatus) => ({
        nodes: s.nodes.map((n) =>
          n.id === stage ? { ...n, data: { ...n.data, status } } : n,
        ),
      });

      switch (ev.type) {
        case 'status':
          run.status = ev.status ?? run.status;
          if (ev.phase) run.current = ev.phase;
          return { run, ...seqPatch };

        case 'stage': {
          if (!ev.stage) return { run, ...seqPatch };
          // 阶段名与节点 key 在注册表里是一致的（node.key === stage）
          const status = (ev.status ?? 'done') as StageStatus;
          const patch = setStage(ev.stage, status);
          if (status === 'done') run.current = ev.stage;
          return { run, ...patch, ...seqPatch };
        }

        case 'progress': {
          // 完整快照：晚连上也能一次性对齐
          if (!ev.nodes) return { run, ...seqPatch };
          const map = ev.nodes;
          run.current = ev.current ?? run.current;
          return {
            run,
            nodes: s.nodes.map((n) =>
              map[n.id] ? { ...n, data: { ...n.data, status: map[n.id] as StageStatus } } : n,
            ),
            ...seqPatch,
          };
        }

        case 'thinking':
          if (ev.text) {
            run.thinking = [
              ...run.thinking,
              { label: ev.label ?? '', text: ev.text, seq: ev.seq },
            ];
          }
          return { run, ...seqPatch };

        case 'review':
          run.review = ev.review ?? null;
          run.status = 'waiting_review';
          return { run, ...seqPatch };

        case 'done':
          run.status = 'done';
          return { run, ...seqPatch };

        case 'error':
          run.error = ev.message ?? '运行出错';
          if (run.status === 'running') run.status = 'failed';
          return { run, ...seqPatch };

        default:
          return { run, ...seqPatch };
      }
    }),

  endRun: (message) =>
    set((s) => ({
      run: { ...s.run, status: s.run.status === 'running' ? 'idle' : s.run.status,
             error: message ?? s.run.error },
    })),

  applyReview: (review, status) =>
    set((s) => ({
      run: {
        ...s.run,
        review,
        // 未显式指定状态时：清空 review → 回到 running，否则置为 waiting_review
        status: status ?? (review ? 'waiting_review' : 'running'),
      },
    })),
}));

/**
 * 开发期把 store 挂到 window，便于在浏览器控制台/自动化脚本里检查与驱动状态。
 *
 * 为什么需要：像"点了保存是否真的存住"这类**跨 store→API→DB** 的链路，
 * 只有驱动真实界面才能验证；纯 HTTP 测试证明不了按钮接对了。
 * 生产构建下也保留（只读得到内部状态，不引入额外权限）。
 */
declare global {
  interface Window {
    __lcStore?: typeof useCanvas;
  }
}
if (typeof window !== 'undefined') {
  window.__lcStore = useCanvas;
}
