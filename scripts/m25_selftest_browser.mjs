#!/usr/bin/env node
/* TASK-M2-5：selftest-publish.html 人肉复核通道的 headless 跑法（CDP 直连 Chrome）。
 *
 * 口径（门槛稿 AC-5 原文）：**机检以后端 selftest API 断言为准**（本卡＝
 *   tests/test_publish_hbase.py，node 驱动适配层纯函数＋静态机检，断言不依赖前端渲染）；
 *   浏览器页**仅作人肉复核通道**。本脚本证明该人肉通道「可 headless 跑且断言全绿」，
 *   即 AC-5「headless 可跑＝判据成立」的前端侧证据。
 *
 * 用法：node scripts/m25_selftest_browser.mjs
 * 退出码：0＝断言页全绿（data-selftest=green）；1＝未全绿或环境不可用。
 * 依赖：本机 Google Chrome（headless=new）＋ node ≥18（内置 fetch/WebSocket）。无第三方包。
 */
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8799, CDP = 9223;
const CHROME = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 1) 静态伺服仓库根（admin 页以 ../portal/assets 引用共享资源；无外网）
//    ＋ /v1/* 同源挡板 API（admin/mock/publish-wizard.json；卡3 五步向导全链路无后端可跑）。
//    挡板有状态：切版三闸（未装载拒切）→发布（覆盖更新语义：同 route 更新不追加）→下线置 offline。
//    卡3 契约换源：表源＝/v1/admin/load/assets、字段源＝/v1/admin/load/schema（B3.1 冻结）；
//    旧 discovery/test/load 路由随发布页迁移下线（端点本体归卡6 清理）。
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8' };
const mock = JSON.parse(fs.readFileSync(path.join(ROOT, 'admin/mock/publish-wizard.json'), 'utf8'));
const stub = {
  services: JSON.parse(JSON.stringify((mock.console && mock.console.services) || [])),
  live: { version: (mock.syncStatus && mock.syncStatus.liveVersion) || null, rows: 3 },
  loaded: {}, nextId: 1,
};
// 卡3：已装载版本集＝assets 全表版本并集（切版三闸的「已装载」判定源）
for (const t of ((mock.assets && mock.assets.tables) || []))
  for (const v of (t.versions || [])) stub.loaded[v.version] = true;
function json(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}
async function stubApi(req, res) {
  const url = req.url.split('?')[0];
  const body = await new Promise((r) => { let b = ''; req.on('data', (c) => { b += c; }); req.on('end', () => r(b)); })
    .then((b) => { try { return b ? JSON.parse(b) : {}; } catch { return {}; } });
  // 卡3：流程② 表源/字段源（B3.1 冻结契约；挡板无状态直回 mock）
  if (url === '/v1/admin/load/assets')
    return json(res, 200, mock.assets || { ok: true, tables: [] });
  if (url === '/v1/admin/load/schema')
    return json(res, 200, mock.schema || { ok: false, error: 'SCHEMA_NOT_FOUND' });
  if (url === '/v1/admin/publish/sync/status')
    return json(res, 200, { ok: true, liveVersion: stub.live.version,
      versionMeta: stub.live.version ? { rows: stub.live.rows } : null, recentSwitches: [] });
  if (url === '/v1/admin/publish/sync/switch') {
    const v = String(body.version || '');
    if (!stub.loaded[v])
      return json(res, 409, { ok: false, error: 'SWITCH_NOT_LOADED', message: '该版本尚未加载，请先完成加载' });
    const from = stub.live.version;
    stub.live = { version: v, rows: 3 };
    return json(res, 200, { ok: true, from, to: v });
  }
  if (url === '/v1/admin/publish/sync/rollback') {
    const from = stub.live.version;
    stub.live = { version: (mock.syncStatus && mock.syncStatus.liveVersion) || null, rows: 3 };
    return json(res, 200, { ok: true, from, to: stub.live.version });
  }
  if (url === '/v1/admin/publish' && req.method === 'GET')
    return json(res, 200, { services: stub.services });
  if (url === '/v1/admin/publish' && req.method === 'POST') {
    // 卡3（B3.2 前向契约）：覆盖更新语义——同 route 命中既有条目则更新（action=updated），
    // 否则新建（action=created）；auditId 如实 null（机制归卡4）。
    const before = stub.services.find((s) => s.routePath === body.routePath) || null;
    const entry = before || { id: 'stub-' + stub.nextId++, serviceName: body.serviceName,
      routePath: body.routePath, status: 'published',
      fields: (body.fields || []).map((f) => f.name) };
    entry.serviceName = body.serviceName; entry.status = 'published';
    entry.loadSource = { table: body.table, version: body.loadVersion || '' };
    entry.version = 'V1';
    if (!before) stub.services.unshift(entry);
    return json(res, 200, { entry, before,
      ok: true, serviceId: entry.id, routePath: body.routePath,
      action: before ? 'updated' : 'created', dataVersion: body.loadVersion || '',
      auditId: null });
  }
  const off = url.match(/^\/v1\/admin\/publish\/([^/]+)\/offline$/);
  if (off && req.method === 'POST') {
    const s = stub.services.find((x) => x.id === off[1]);
    if (!s) return json(res, 404, { error: 'NOT_FOUND', message: '服务不存在' });
    s.status = 'offline';
    return json(res, 200, { entry: s, before: null });
  }
  return json(res, 404, { error: 'NOT_FOUND', message: '挡板未实现该端点' });
}
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/v1/')) return stubApi(req, res);
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end('404'); }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise(r => server.listen(PORT, '127.0.0.1', r));

// 2) 起 headless Chrome（CDP 调试端口；用户数据目录临时、结束即回收）
const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'm25-chrome-'));
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
  const send = (method, params) => new Promise(r => { const id = ++mid; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });

  await send('Page.enable', {});
  await send('Runtime.enable', {});
  await send('Page.navigate', { url: `http://127.0.0.1:${PORT}/admin/selftest-publish.html` });

  // 3) 等运行器完成标记（data-selftest 中间态恒为 red，不能当结束判据）
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
  const summary = await evaluate("document.getElementById('summary').textContent");
  const rows = await evaluate("Array.from(document.querySelectorAll('#results tbody tr')).map(function(tr){var c=tr.querySelectorAll('td');return {id:c[1].textContent,st:c[3].textContent,d:c[4].textContent}})");

  console.log('[browser] data-selftest=' + state);
  console.log('[browser] ' + summary);
  (rows || []).forEach(x => { if (x.st !== 'PASS') console.log('  [' + x.st + '] ' + x.id + ' — ' + x.d); });
  if (state !== 'green') { process.exitCode = 1; console.error('[browser] 断言页未全绿'); }
} catch (e) {
  console.error('[browser] 失败：' + e.message);
  process.exitCode = 1;
} finally {
  try { ws && ws.close(); } catch { /* 忽略收尾异常 */ }
  chrome.kill('SIGKILL');
  server.close();
  try { fs.rmSync(userDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); }
  catch { /* 临时目录回收竞态不改变断言结论 */ }
}
