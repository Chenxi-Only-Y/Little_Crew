#!/usr/bin/env node
/**
 * 浏览器视觉验证脚手架（headless Edge + CDP）。
 *
 * ## 为什么要有这个文件
 *
 * 我（AI）做前端验证时，会写一个「起 headless Edge → 连 CDP → 截图 → 读 DOM」
 * 的脚本。这件事我**重复写了 37 次**，每次 60~80 行样板，于是：
 *
 *   1. 每次都用 `os.tmpdir()`（Windows 上是 `C:\Users\<你>\AppData\Local\Temp`）
 *      当 `--user-data-dir`，而**每次不删** → 实测在系统临时目录堆了
 *      **23 个目录 / 948 MB**（单个 35~235 MB，浏览器 profile 很大）。
 *   2. 样板里的细节（等日志、超时、错误收集）每次都重写，容易漏。
 *
 * 所以：**脚手架放这里复用**，临时 profile 固定到 `data/_verify/_browser`
 * （项目内、gitignored），用完可清。
 *
 * ## 用法
 *
 *     node scripts/verify_page.mjs --url http://127.0.0.1:5183/#/director \
 *         --wait 15000 --shot data/_verify/director.png --eval "document.title"
 *
 * 参数：
 *   --url    必填，要打开的地址
 *   --shot   可选，截图输出路径（PNG）
 *   --eval   可选，页面里求值的表达式（可重复），结果打印出来
 *   --wait   可选，导航后等待毫秒（默认 8000）
 *   --login  可选，`用户名:密码`，先在 /#/home 登录再跳转（本项目用 token 存 localStorage）
 *   --keep   可选，保留临时 profile（默认保留，便于排查；删掉加 --clean）
 *
 * ## 也是给我自己的规矩
 *
 * **不要再把验证产物写到系统临时目录**（C 盘）。要写就写到 `data/_verify/`，
 * 并在回复里说明位置。
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
];

function parseArgs(argv) {
  const out = { evals: [], wait: 8000 };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--url') out.url = argv[++i];
    else if (a === '--shot') out.shot = argv[++i];
    else if (a === '--eval') out.evals.push(argv[++i]);
    else if (a === '--wait') out.wait = Number(argv[++i]);
    else if (a === '--login') out.login = argv[++i];
    else if (a === '--port') out.port = Number(argv[++i]);
    else if (a === '--clean') out.clean = true;
  }
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const args = parseArgs(process.argv);
  if (!args.url) {
    console.error('缺少 --url');
    process.exit(2);
  }

  // ⚠️ 固定到一个**项目内**的 profile 目录，别用 os.tmpdir()
  const projectRoot = path.resolve(import.meta.dirname, '..');
  const profileDir = path.join(projectRoot, 'data', '_verify', '_browser');
  mkdirSync(profileDir, { recursive: true });

  const port = args.port || 12100 + (process.pid % 400);
  const edge = EDGE_CANDIDATES.find((p) => existsSync(p));
  if (!edge) {
    console.error('找不到 msedge.exe');
    process.exit(2);
  }

  const child = spawn(edge, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profileDir}`,
    '--window-size=1920,1400', 'about:blank',
  ], { stdio: 'ignore' });

  try {
    // 等 CDP 就绪
    let version = null;
    for (let i = 0; i < 60; i++) {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json/version`);
        if (r.ok) { version = await r.json(); break; }
      } catch { /* 还没起 */ }
      await sleep(250);
    }
    if (!version) throw new Error('CDP 端口没起来');

    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    const page = list.find((x) => x.type === 'page');
    if (!page) throw new Error('没有可用的 page target');

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    let id = 0;
    const pending = new Map();
    const errors = [];
    ws.addEventListener('message', (e) => {
      const m = JSON.parse(e.data);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
      if (m.method === 'Runtime.exceptionThrown') {
        errors.push(String(m.params?.exceptionDetails?.exception?.description || '').slice(0, 220));
      }
    });
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true });
      ws.addEventListener('error', rej, { once: true });
    });
    const send = (method, params = {}) => new Promise((res) => {
      const i = ++id; pending.set(i, res);
      ws.send(JSON.stringify({ id: i, method, params }));
    });
    const evaluate = async (expr) => {
      const r = await send('Runtime.evaluate', {
        expression: expr, returnByValue: true, awaitPromise: true,
      });
      const ex = r.result?.exceptionDetails;
      if (ex) return `EXC: ${String(ex.exception?.description || '').slice(0, 220)}`;
      return r.result?.result?.value;
    };

    await send('Page.enable');
    await send('Runtime.enable');

    // 可选：先登录（本项目把 token 放 localStorage）
    if (args.login) {
      const [u, p] = args.login.split(':');
      const origin = new URL(args.url).origin;
      await send('Page.navigate', { url: `${origin}/#/home` });
      await sleep(5000);
      const r = await evaluate(`(async()=>{try{
        const r=await fetch('/api/auth/login',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({username:${JSON.stringify(u)},password:${JSON.stringify(p)}})});
        const j=await r.json();
        if(!j.token) return 'LOGIN_FAIL '+JSON.stringify(j);
        localStorage.setItem('lc_token', j.token);
        localStorage.setItem('lc_user', j.username || ${JSON.stringify(u)});
        return 'LOGIN_OK';
      }catch(e){return 'LOGIN_ERR '+e}})()`);
      console.log(`  登录: ${r}`);
    }

    await send('Page.navigate', { url: args.url });
    await sleep(args.wait);

    console.log(`  页面: ${args.url}`);
    console.log(`  标题: ${await evaluate('document.title')}`);
    console.log(`  JS 异常: ${errors.length ? '❌ ' + errors[0] : '✅ 无'}`);

    for (const expr of args.evals) {
      console.log(`  eval ${expr} => ${JSON.stringify(await evaluate(expr))}`);
    }

    if (args.shot) {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      if (shot.result?.data) {
        const out = path.isAbsolute(args.shot)
          ? args.shot
          : path.join(projectRoot, args.shot);
        mkdirSync(path.dirname(out), { recursive: true });
        writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
        console.log(`  截图: ${out}`);
      }
    }

    ws.close();
    return errors.length ? 1 : 0;
  } finally {
    child.kill();
    await sleep(200);
    // profile 默认保留（便于下次复用，**不必每次都新建**）；
    // 要清就传 --clean。这也是为了不再往 C 盘堆 profile。
    if (args.clean) {
      try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* ignore */ }
    }
  }
}

main().then((code) => process.exit(code)).catch((e) => {
  console.error('  失败:', e.message);
  process.exit(1);
});
