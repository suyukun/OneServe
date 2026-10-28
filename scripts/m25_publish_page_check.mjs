#!/usr/bin/env node
/* TASK-M2-5 起源脚本；发布台重写（SPEC v1）后判据同步更新：真实 admin/publish.html 页 headless 检查（不是 selftest 探针页）。
 * 判据（发布台重设计 SPEC §八.1 诚信线＋§五.1 单一路径）：
 *   · 后端不在场（静态伺服，/v1/* 全 404）→ ① 出现如实错误条
 *     「暂时读不到数据表清单」＋重试按钮，表卡片零张（不回落示例数据冒充真表）；
 *   · 向导骨架在位（Stepper＋四步容器＋服务管理区）；Redis 存量分支 UI 已整体下线；
 *   · 未选表时①的下一步 disabled（一步一屏硬闸）。
 * 用法：node scripts/m25_publish_page_check.mjs    退出码 0＝判据成立。
 * 依赖：本机 Google Chrome（headless=new）＋ node ≥18；无第三方包。
 */
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = 8798, CDP = 9322;
const CHROME = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let failed = 0;
const ok = (cond, msg) => { console.log((cond ? '  ✓ ' : '  ✗ ') + msg); if (!cond) failed++; };

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8' };
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/v1/')) { res.writeHead(404, { 'Content-Type': 'application/json' }); return res.end('{"error":"NOT_FOUND"}'); }
  const p = path.join(ROOT, decodeURIComponent(req.url.split('?')[0]));
  if (!p.startsWith(ROOT) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end('404'); }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
  fs.createReadStream(p).pipe(res);
});
await new Promise(r => server.listen(PORT, '127.0.0.1', r));

const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'm25-page-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
  '--no-default-browser-check', `--remote-debugging-port=${CDP}`, `--user-data-dir=${userDir}`, 'about:blank'], { stdio: 'ignore' });
let ws;
try {
  let up = false;
  for (let i = 0; i < 40 && !up; i++) { try { await fetch(`http://127.0.0.1:${CDP}/json/version`); up = true; } catch { await sleep(250); } }
  if (!up) throw new Error('Chrome CDP 未就绪');
  const tab = await (await fetch(`http://127.0.0.1:${CDP}/json/new?about:blank`, { method: 'PUT' })).json();
  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  let mid = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
  const send = (method, params) => new Promise(r => { const id = ++mid; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
  await send('Page.enable', {});
  await send('Page.navigate', { url: `http://127.0.0.1:${PORT}/admin/publish.html` });
  await sleep(2500);
  const ev = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result?.result?.value;

  console.log('== admin/publish.html headless 检查（后端不在场＝/v1/* 全 404）==');
  // 1) 向导骨架在位
  ok(await ev("!!document.getElementById('wizard-stepper') && !!document.getElementById('step-1') && !!document.getElementById('step-4')"), '四步向导骨架在位（Stepper＋①~④）');
  ok(await ev("!!document.getElementById('services-section') && !!document.getElementById('service-tbody')"), '已发布服务管理区在位');

  // 2) 后端不在场 → 如实错误条＋重试，零表卡片（诚信线）
  const errShown = await ev("(() => { const el = document.getElementById('discovery-error'); return !!el && getComputedStyle(el).display !== 'none'; })()");
  const errTitle = await ev("(document.getElementById('discovery-error') || {}).querySelector('strong') ? document.getElementById('discovery-error').querySelector('strong').textContent : ''");
  ok(errShown, '发现失败 → 如实错误条可见');
  ok(/暂时读不到数据表清单/.test(String(errTitle)), '错误标题＝SPEC 人话句式：' + errTitle);
  ok(await ev("!!document.getElementById('discovery-retry-btn')"), '错误条带重试按钮');
  const tables = await ev("Alpine.$data(document.body).tables.length");
  const cards = await ev("document.querySelectorAll('button.table-card').length");
  ok(tables === 0 && cards === 0, '表卡片零张（tables=' + tables + ' cards=' + cards + '）——未回落示例数据冒充真表');
  const nextDisabled = await ev("document.getElementById('next-1').disabled");
  ok(nextDisabled === true, '未选表时①的下一步 disabled（一步一屏硬闸）');

  // 3) Redis 存量分支 UI 已整体下线（SPEC §五.1）
  const legacy = await ev("['ds-switch','ds-kind-redis','key-banner','scenario-select','field-table','release-mode-select'].filter(function(id){return !!document.getElementById(id)}).join(',')");
  ok(legacy === '', 'Redis 存量/两段式控件零残留（' + (legacy || '无') + '）');

  // 4) 七类文案扫描（页面正文零命中）
  const jargonHits = await ev("(function(){ var main = document.querySelector('main') || document.body; var t = main.outerHTML;" +
    " var res = [/O-\\d+/, /M2-\\d+/, /AC-\\d+/, /\\bT1\\b/, /dual-source/, /\\bscan\\b/, /rowkey/, /\\bmeta\\b/, /CSV/," +
    " /业务域/, /数据域/, /字段表即表单/, /测试环境/, /生产环境/, /仅管理员/, /留痕/, /可追溯/, /零明文/, /自证/]" +
    ".filter(function(re){ return re.test(t) }).map(function(re){return re.source}).join(','); return res; })()");
  ok(jargonHits === '', '七类文案扫描零命中（' + (jargonHits || '全部规则通过') + '）');
} catch (e) {
  console.error('[page] 失败：' + e.message); failed++;
} finally {
  try { ws && ws.close(); } catch { /* 收尾异常不改结论 */ }
  chrome.kill('SIGKILL'); server.close();
  try { fs.rmSync(userDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); }
  catch { /* 临时目录回收竞态不改变检查结论 */ }
}
console.log(failed ? `== 检查未通过（${failed} 条失败） ==` : '== 检查全绿 ==');
process.exitCode = failed ? 1 : 0;
