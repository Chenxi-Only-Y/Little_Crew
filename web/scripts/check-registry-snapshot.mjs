/**
 * 注册表快照时效性守卫。
 *
 * 为什么需要它（这不是假想的问题，是**实际踩到**的）：
 *   `check-validate-parity.mjs` 比的是**拓扑校验器**（前端算法 vs 后端接口），
 *   它**完全不看** `web/src/registry.snapshot.json` 是否过期。
 *   于是出现这种局面——后端注册表已经改了（`video_provider` 的选项里
 *   加上了 `dashscope_video` / `seedance`），而前端快照还是旧的，
 *   界面上**依然选不到**这两个提供者；而所有检查都显示"通过"。
 *
 *   快照过期 = "后端能做到、界面做不到"的静默割裂，这类问题很难被察觉：
 *   代码看着对、接口返回也对，只有用户点开界面才发现少了东西。
 *
 * 本脚本做的事：重新导出一次注册表，与**已提交的快照**逐字节比对。
 * 导出是确定性的（实测两次导出 SHA256 相同），所以可以直接比字节。
 *
 * 运行：
 *   node scripts/check-registry-snapshot.mjs        （不需要后端）
 */

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, '..');
const repoRoot = resolve(webRoot, '..');
const committed = join(webRoot, 'src', 'registry.snapshot.json');

const sha = (buf) => createHash('sha256').update(buf).digest('hex').slice(0, 16);

if (!existsSync(committed)) {
  console.error(`❌ 找不到前端快照：${committed}`);
  process.exit(1);
}

// 找一个可用的 Python（项目自带 venv 优先）
const candidates = [
  join(repoRoot, '.venv', 'Scripts', 'python.exe'),   // Windows
  join(repoRoot, '.venv', 'bin', 'python'),           // POSIX
  'python',
];
const py = candidates.find((c) => c === 'python' || existsSync(c));
if (!py) {
  console.error('❌ 找不到可用的 Python，无法导出注册表');
  process.exit(1);
}

const tmp = mkdtempSync(join(tmpdir(), 'lc-registry-'));
const out = join(tmp, 'snapshot.json');
try {
  execFileSync(py, [join(repoRoot, 'scripts', 'export_registry.py'), '--out', out], {
    stdio: ['ignore', 'ignore', 'inherit'],
    cwd: repoRoot,
  });

  const fresh = readFileSync(out);
  const old = readFileSync(committed);
  const h1 = sha(old);
  const h2 = sha(fresh);

  if (h1 === h2) {
    console.log(`✅ 注册表快照是最新的（sha256 ${h1}）`);
    process.exit(0);
  }

  console.error('❌ 注册表快照已过期！');
  console.error(`   已提交：${h1}`);
  console.error(`   重新导出：${h2}`);
  console.error('');
  console.error('   这意味着**后端注册表改了、前端快照没跟上**，');
  console.error('   表现为"后端支持但界面上看不到/选不到"（静默割裂）。');
  console.error('');
  console.error('   修法：  python scripts/export_registry.py');
  process.exit(1);
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
