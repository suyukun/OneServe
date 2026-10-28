#!/usr/bin/env node
/* admin/selftest-*.html 断言页通用 headless 跑法（CDP 直连 Chrome）。
 *
 * 与 scripts/m25_selftest_browser.mjs 同族（该版为发布页专用，含 /v1 挡板；
 * 本版通用化：无挡板——后端 API 一律 404，页面走既有「不可达降级演示数据」路径，
 * 断言页对演示态的断言因此天然被覆盖）。
 *
 * 用法：node scripts/selftest_page_cdp.mjs <pagePath> [--port N] [--cdp N]
 *   pagePath 形如 /admin/selftest-grants.html（仓库根为静态文档根）。
 * 退出码：0＝断言页全绿（data-selftest=green 且已置 done）；1＝未全绿或环境不可用。
 * 依赖：本机 Google Chrome（headless=new）＋ node ≥18（内置 fetch/WebSocket）。无第三方包。
 * 收尾：Chrome（SIGKILL）＋静态伺服＋临时用户目录全部即杀即清（零残留）。
 */
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const pagePath = args.find((a) => !a.startsWith('--'));
const opt = (name, dflt) => {
  const i = args.indexOf('--' + name);
  return i >= 0 ? Number(args[i + 1]) : dflt;
};
const PORT = opt('port', 8877);
const CDP = opt('cdp', 9227);
const CHROME = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (!pagePath || !pagePath.startsWith('/')) {
  console.error('用法：node scripts/selftest_page_cdp.mjs <pagePath> [--port N] [--cdp N]');
  process.exit(1);
}

// 静态伺服仓库根（admin 页以 ../portal/assets 等相对路径引共享资源；无外网）。
// /v1/* 一律 404：断言页依赖页面既有「后端不可达→降级演示数据」路径，不造假挡板。
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
               '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' };
const server = http.createServer((req, res) => {
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) {
    res.writeHead(404, { 'Content-Type': 'application/json; charset=utf-8' });
    return res.end(JSON.stringify({ error: 'not_found' }));
  }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise((r) => server.listen(PORT, '127.0.0.1', r));

// headless Chrome（CDP 调试端口；用户数据目录临时、结束即回收）
const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'selftest-chrome-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', `--remote-debugging-port=${CDP}`, `--user-data-dir=${userDir}`,
  'about:blank'], { stdio: 'ignore' });

let ws;
try {
  let up = false;
  for (let i = 0; i < 40 && !up; i++) { try { await fetch(`http://127.0.0.1:${CDP}/json/version`); up = true; } catch { await sleep(250); } }
  if (!up) throw new Error('Chrome CDP 未就绪（检查 ' + CHROME + ' 是否可用）');

  const tab = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  let mid = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  const send = (method, params) => new Promise((r) => { const id = ++mid; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });

  await send('Page.enable', {});
  await send('Runtime.enable', {});
  await send('Page.navigate', { url: `http://127.0.0.1:${PORT}${pagePath}` });

  // 等运行器完成标记（data-selftest 中间态恒为 red，不能当结束判据）
  let state = '';
  for (let i = 0; i < 120; i++) {
    await sleep(500);
    const r = await send('Runtime.evaluate', {
      expression: "[document.documentElement.getAttribute('data-selftest') || '', document.documentElement.getAttribute('data-selftest-done') || ''].join('|')",
      returnByValue: true });
    const parts = String(r.result?.result?.value || '').split('|');
    state = parts[0];
    if (parts[1] === '1') break;
  }
  const evaluate = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result?.result?.value;
  const summary = await evaluate("document.getElementById('summary') && document.getElementById('summary').textContent");
  const rows = await evaluate("Array.from(document.querySelectorAll('#results tbody tr')).map(function(tr){var c=tr.querySelectorAll('td');return {id:c[1]?c[1].textContent:'',st:c[3]?c[3].textContent:'',d:c[4]?c[4].textContent:''}})");

  console.log('[selftest] page=' + pagePath + ' data-selftest=' + state);
  if (summary) console.log('[selftest] ' + summary.trim());
  (rows || []).forEach((x) => { if (x.st && x.st !== 'PASS') console.log('  [' + x.st + '] ' + x.id + ' — ' + x.d); });
  if (state !== 'green') { process.exitCode = 1; console.error('[selftest] 断言页未全绿'); }
} catch (e) {
  console.error('[selftest] 失败：' + e.message);
  process.exitCode = 1;
} finally {
  try { ws && ws.close(); } catch { /* 忽略收尾异常 */ }
  chrome.kill('SIGKILL');
  server.close();
  try { fs.rmSync(userDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); }
  catch { /* 临时目录回收竞态不改变断言结论 */ }
}
