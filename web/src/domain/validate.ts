/**
 * 画布连线与依赖校验 —— 前端预校验。
 *
 * 与后端 `app/registry/api.py:validate_topology` **语义等价**：
 *   规则数据来自注册表下发的 `validation_spec`（后端导出），
 *   前端只实现算法，不自己写规则 —— 这样两边不会漂移。
 *
 * 规则（与后端一致）：
 *   1. 节点必须在注册表里
 *   2. 连线必须满足产物依赖：source.produces ∩ target.consumes ≠ ∅
 *   3. 每个节点的 consumes 必须由"已选节点"产出，或属于外部输入
 *      （后端会更严格地要求 key 完全一致；前端为了即时反馈按 kind 放宽）
 *   4. 不得有环
 */

import type { Registry } from '../domain/registry';

export interface ValidationIssue {
  code: 'unknown_node' | 'incompatible_edge' | 'missing_dependency' | 'cycle';
  node?: string;
  edge?: string;
  message: string;
}

export interface ValidationResult {
  ok: boolean;
  issues: ValidationIssue[];
  order: string[];
}

interface SpecNode {
  label: string;
  produces: string[];
  consumes: string[];
  can_skip: boolean;
}

export interface ValidationSpec {
  artifacts: Record<string, { kind: string; label: string }>;
  nodes: Record<string, SpecNode>;
  compatible_kinds: Record<string, string[]>;
  external_inputs: string[];
}

export function specOf(registry: Registry): ValidationSpec | null {
  // validation_spec 由后端下发；旧快照可能没有
  const spec = (registry as unknown as { validation_spec?: ValidationSpec }).validation_spec;
  return spec && spec.nodes ? spec : null;
}

/** 两个节点之间某条边是否合法 */
export function edgeAllowed(
  spec: ValidationSpec,
  source: string,
  target: string,
): { ok: boolean; artifacts: string[] } {
  const s = spec.nodes[source];
  const t = spec.nodes[target];
  if (!s || !t) return { ok: false, artifacts: [] };
  const shared = s.produces.filter((a) => t.consumes.includes(a));
  return { ok: shared.length > 0, artifacts: shared };
}

/** 供即时反馈的简短原因（拖线失败时提示） */
export function edgeReason(spec: ValidationSpec, source: string, target: string): string {
  const s = spec.nodes[source];
  const t = spec.nodes[target];
  if (!s || !t) return '节点不存在于注册表';
  const need = t.consumes.map((a) => spec.artifacts[a]?.label ?? a);
  return `「${s.label}」的产出无法被「${t.label}」消费（它需要：${need.join('、') || '无'}）`;
}

/**
 * 校验一组节点与连线是否可执行。
 *
 * @param spec    注册表下发的校验规格
 * @param nodeIds 画布上的节点
 * @param edges   画布上的连线（source/target/artifact）
 */
export function validateTopology(
  spec: ValidationSpec,
  nodeIds: string[],
  edges: Array<{ source: string; target: string; artifact: string }>,
): ValidationResult {
  const issues: ValidationIssue[] = [];
  const known = new Set(Object.keys(spec.nodes));
  const chosen = nodeIds.filter((id) => known.has(id));
  for (const id of nodeIds) {
    if (!known.has(id)) {
      issues.push({ code: 'unknown_node', node: id, message: `节点不在注册表中：${id}` });
    }
  }

  // 连线合法性
  for (const e of edges) {
    if (!known.has(e.source) || !known.has(e.target)) continue;
    const { ok } = edgeAllowed(spec, e.source, e.target);
    if (!ok) {
      issues.push({
        code: 'incompatible_edge',
        edge: `${e.source}->${e.target}`,
        message: edgeReason(spec, e.source, e.target),
      });
    }
  }

  // 依赖是否可满足
  const produced = new Set<string>();
  for (const id of chosen) for (const a of spec.nodes[id].produces) produced.add(a);
  const provided = new Set(spec.external_inputs);
  const satisfiable = new Set([...produced, ...provided]);
  for (const id of chosen) {
    const missing = spec.nodes[id].consumes.filter((a) => !satisfiable.has(a));
    if (missing.length > 0) {
      issues.push({
        code: 'missing_dependency',
        node: id,
        message: `「${spec.nodes[id].label}」缺少上游产物：${missing
          .map((a) => spec.artifacts[a]?.label ?? a)
          .join('、')}`,
      });
    }
  }

  // 环检测（仅在无其它错误时给出顺序）
  const order = issues.length === 0 ? topoOrder(chosen, edges) : [];

  return { ok: issues.length === 0, issues, order };
}

/** 拓扑排序；有环时返回空数组。
 *
 * 注意：只依据**显式连线**；声明的 consumes 依赖已在上面单独校验，
 * 不参与排序（否则会自动"补边"，与用户看到的画布不符）。
 */
function topoOrder(
  nodeIds: string[],
  edges: Array<{ source: string; target: string; artifact: string }>,
): string[] {
  const keys = [...new Set(nodeIds)];
  const keySet = new Set(keys);
  const deps = new Map<string, Set<string>>(keys.map((k) => [k, new Set<string>()]));

  for (const e of edges) {
    if (keySet.has(e.source) && keySet.has(e.target) && e.source !== e.target) {
      deps.get(e.target)!.add(e.source);
    }
  }

  const out: string[] = [];
  const remaining = new Map(deps);
  while (remaining.size > 0) {
    const ready = keys.filter((k) => remaining.has(k) && remaining.get(k)!.size === 0);
    if (ready.length === 0) return []; // 有环
    for (const k of ready) {
      out.push(k);
      remaining.delete(k);
    }
    for (const v of remaining.values()) {
      for (const k of ready) v.delete(k);
    }
  }
  return out;
}
