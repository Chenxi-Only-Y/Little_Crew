/**
 * 校验器等价性验证：用真实注册表数据比对前端与后端的判定结果。
 *
 * 为什么值得单独做一次：前端为了即时反馈自己实现了一套校验算法，
 * 若与后端不一致，就会出现"画布说能跑、后端说不行"的割裂。
 *
 * 运行（需后端已启动）：
 *   node --experimental-strip-types --no-warnings scripts/check-validate-parity.mjs
 */

const API = process.env.LC_API || 'http://127.0.0.1:8000';

const { validateTopology, specOf } = await import('../src/domain/validate.ts');

const regRes = await fetch(`${API}/api/registry`);
if (!regRes.ok) throw new Error(`取注册表失败：${regRes.status}`);
const registry = (await regRes.json()).registry;
const spec = specOf(registry);
if (!spec) throw new Error('注册表未下发 validation_spec');

/** 取"到某节点为止"的闭合子集：保证依赖可满足，用于构造正向用例 */
function closedSubset(upto) {
  const order = registry.execution_order;
  const idx = order.indexOf(upto);
  return order.slice(0, idx + 1).filter((k) => k !== 'workflow_architect');
}

const cases = [
  { name: '空集合', nodes: [], edges: [], expectOk: true },
  { name: '单个下游节点（缺依赖）', nodes: ['photographer'], edges: [], expectOk: false },
  { name: '编剧+分镜师（分镜师缺资产）', nodes: ['screenwriter', 'storyboarder'], edges: [], expectOk: false },
  {
    name: '到摄影师为止的闭合链（应可执行）',
    nodes: closedSubset('photographer'),
    edges: [{ source: 'storyboarder', target: 'photographer', artifact: 'storyboard' }],
    expectOk: true,
  },
  {
    name: '编剧 -> 摄影师（端口不兼容）',
    nodes: [...closedSubset('photographer'), 'screenwriter'],
    edges: [{ source: 'screenwriter', target: 'photographer', artifact: 'script' }],
    expectOk: false,
  },
  { name: '完整链路（应可执行）', nodes: closedSubset('distributor'), edges: [], expectOk: true },
];

let pass = 0;
let fail = 0;
for (const c of cases) {
  const local = validateTopology(spec, c.nodes, c.edges);
  const res = await fetch(`${API}/api/registry/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      nodes: c.nodes,
      // 与前端同构的边格式（后端两种都接受）
      edges: c.edges.map((e) => ({ source: e.source, target: e.target })),
      available_inputs: ['user_input', 'art_brief'],
    }),
  });
  const remote = await res.json();

  const agree = local.ok === remote.ok;
  const asExpected = local.ok === c.expectOk;
  const mark = agree && asExpected ? 'OK  ' : agree ? 'WARN' : 'DIFF';
  if (agree && asExpected) pass++;
  else fail++;

  console.log(
    `${mark} ${c.name}\n     前端 ok=${local.ok} (${local.issues.length} 条) | 后端 ok=${remote.ok} (${remote.errors.length} 条) | 期望 ok=${c.expectOk}`,
  );
  if (!agree) {
    console.log('     前端:', local.issues.map((i) => i.code).join(', ') || '—');
    console.log('     后端:', remote.errors.map((e) => e.code).join(', ') || '—');
  } else if (!asExpected) {
    console.log('     ↑ 两侧一致但与预期不符：说明用例的依赖没闭合，或用例标签写错了');
  }
}

console.log(`\n通过 ${pass} / 不通过 ${fail}`);
// 用 exitCode 而非 process.exit()：后者在 Windows 上会触发 libuv 断言崩溃
process.exitCode = fail === 0 ? 0 : 1;
