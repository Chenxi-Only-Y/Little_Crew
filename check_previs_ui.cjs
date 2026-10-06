const os = require('node:os'), path = require('node:path'), { spawn } = require('node:child_process');
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 11961, udd = path.join(os.tmpdir(), 'lc_previs');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  for (let i = 0; i < 45; i++) { try { const r = await fetch('http://127.0.0.1:8000/api/registry'); if (r.ok) break; } catch { } await sleep(800); }
  const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${udd}`, '--window-size=1920,1200', 'about:blank'], { stdio: 'ignore' });
  for (let i = 0; i < 40; i++) { try { const r = await fetch(`http://127.0.0.1:${PORT}/json/version`); if (r.ok) break; } catch { } await sleep(250); }
  const page = (await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json()).find(x => x.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl); let id = 0; const p = new Map(); const errs = [];
  ws.addEventListener('message', e => {
    const m = JSON.parse(e.data);
    if (m.id && p.has(m.id)) { p.get(m.id)(m); p.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') errs.push(String(m.params?.exceptionDetails?.exception?.description || '').slice(0, 200));
  });
  await new Promise(r => ws.addEventListener('open', r, { once: true }));
  const send = (m, params = {}) => new Promise(res => { const i = ++id; p.set(i, res); ws.send(JSON.stringify({ id: i, method: m, params })); });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) return 'EXC: ' + String(r.result.exceptionDetails.exception?.description || '').slice(0, 240);
    return r.result?.result?.value;
  };
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:5183/#/home' });
  await sleep(6000);
  await ev(`(async()=>{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'director',password:'test1234'})}); const j=await r.json();
    localStorage.setItem('lc_token',j.token); localStorage.setItem('lc_user',j.username);})()`);

  errs.length = 0;
  await send('Page.navigate', { url: 'http://127.0.0.1:5183/#/previs' });
  await sleep(15000);
  console.log('  预演页异常:', errs.length ? '❌ ' + errs[0] : '✅ 无');
  console.log('  有「AI 出初稿」按钮:', await ev(`[...document.querySelectorAll('button')].some(b=>/AI 出初稿/.test(b.textContent))`));
  console.log('  3D canvas:', await ev(`!!document.querySelector('canvas')`));

  console.log('');
  console.log('  点它（此时没有会话 → 应给"先去导演台跑一轮"的提示，而不是崩）：');
  errs.length = 0;
  await ev(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>/AI 出初稿/.test(x.textContent)); if(b) b.click();})()`);
  await sleep(2500);
  console.log('    异常:', errs.length ? '❌ ' + errs[0] : '✅ 无');
  console.log('    提示文本:', await ev(`(()=>{const t=document.body.innerText;
    const m=t.match(/还没有可用的分镜[^\\n]{0,60}/); return m?m[0]:'(没找到提示)';})()`));

  const shot = await send('Page.captureScreenshot', { format: 'png' });
  if (shot.result?.data) {
    require('node:fs').writeFileSync('D:\\AI\\ds.6\\data\\previs-draft.png',
      Buffer.from(shot.result.data, 'base64'));
    console.log('\n  截图: data/previs-draft.png');
  }
  ws.close(); edge.kill(); await sleep(200);
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
