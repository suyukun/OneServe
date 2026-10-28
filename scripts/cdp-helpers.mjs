#!/usr/bin/env node
/* CDP 公共助手（收口批续作 2026-10-15，任务件 A）——从
 * tests/evidence/wall-enable-20261015/wall_enable_verify.mjs 抽取的复用三件＋已知坑注释。
 * 后续任何「headless Chrome 直驱活栈」出证脚本请从这里 import，勿再复制粘贴整段。
 *
 * ⚠️ 已知坑（全部为 2026-10-14/15 真栈实测，非推测）：
 * 1. 重定向风暴撕文档：无 token 访问首页会产生「多个 401 → 多个排队
 *    location.replace(/login.html)」；风暴尾波里提交登录时，同 URL replace 会撕掉
 *    携带登录响应的文档（CDP Network 事件实证：POST WILL-SEND→RESPONSE 200 已到达，
 *    但接收文档已换、promise 永不落定）。纯自动化层伪影，产品链路无恙。
 *    规避＝①墙态页用独立实例跑完即弃；②登录流从 login.html 干净实例直驱起步；
 *    ③登录前绝不预写 localStorage 回址键（预写＝必踩本坑，12+ 次二分实证）。
 * 2. 缓存穿透：必须 Network.setCacheDisabled=true（＝旧口径 setCacheEnabled(false)），
 *    否则持久缓存的 site-gate.js 旧版会让「秒踢登录页」类热修看似未生效（当日两次踩实）。
 * 3. /json/new 需 PUT：Chrome 新版 GET /json/new 返回 405，须 {method:'PUT'}。
 * 4. JWT payload 中文：atob 出的是 latin1 串，displayName 等中文须经
 *    TextDecoder('utf-8') 还原，直接 JSON.parse 会乱码。
 * 5. evaluate 异常吞没：Runtime.evaluate 的业务异常藏在 result.exceptionDetails，
 *    不抛错只回 undefined——本助手已代抛，消费方勿裸判空。
 * 6. 登录表单契约：#login-username/#login-password/#login-form/#login-submit
 *    （portal/login.html 现形）；submit 走 dispatchEvent(new Event('submit',
 *    {cancelable:true}))，不点按钮（按钮 handler 挂 form 上）。
 */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 起 headless Chrome＋CDP 会话（tab 级）。port 自选举者传入避免撞车；tag 用于临时 profile 前缀。 */
export async function startChrome(port, tag, startUrl, opts = {}) {
  const CHROME = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-first-run',
    '--no-default-browser-check', '--window-size=' + (opts.windowSize || '1280,900'),
    '--remote-debugging-port=' + port, '--user-data-dir=' + fs.mkdtempSync(path.join(os.tmpdir(), tag)),
    'about:blank'], { stdio: 'ignore' });
  let up = false;
  for (let i = 0; i < 40 && !up; i++) { try { await fetch('http://127.0.0.1:' + port + '/json/version'); up = true; } catch { await sleep(250); } }
  if (!up) throw new Error('Chrome CDP 未就绪: ' + port);
  const tab = await (await fetch('http://127.0.0.1:' + port + '/json/new?about:blank', { method: 'PUT' })).json();   // 坑3：PUT
  const ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  let mid = 0; const pending = new Map();
  ws.addEventListener('message', (ev) => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } });
  const send = (method, params) => new Promise((r) => { const id = ++mid; pending.set(id, r); ws.send(JSON.stringify({ id, method, params })); });
  const evaluate = async (expr) => {   // 坑5：exceptionDetails 代抛
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error('evaluate 异常: ' + JSON.stringify(r.result.exceptionDetails.exception && r.result.exceptionDetails.exception.description || r.result.exceptionDetails.text));
    return r.result && r.result.result && r.result.result.value;
  };
  await send('Page.enable', {});
  await send('Runtime.enable', {});
  await send('Network.enable', {});
  await send('Network.setCacheDisabled', { cacheDisabled: true });   // 坑2：缓存穿透
  if (startUrl && startUrl !== 'about:blank') { await send('Page.navigate', { url: startUrl }); await sleep(opts.navMs || 2200); }
  const shot = async (name, dir) => {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    if (!r || !r.result || !r.result.data) throw new Error('截图失败');
    const out = path.join(dir || process.cwd(), name);
    fs.writeFileSync(out, Buffer.from(r.result.data, 'base64'));
    console.log('  [shot] ' + out);
    return out;
  };
  const nav = async (url, ms) => { await send('Page.navigate', { url }); await sleep(ms || 1800); };
  const close = async () => { try { ws.close(); } catch {} try { chrome.kill('SIGKILL'); } catch {} };
  return { chrome, send, evaluate, shot, nav, close };
}

/** 在 login.html 页内填表并派发 submit（坑6 表单契约；坑1：只在干净实例上用）。 */
export const submitLoginExpr = (user, pass) =>
  "(function(){var u=document.getElementById('login-username');var p=document.getElementById('login-password');u.value=" + JSON.stringify(user) + ";p.value=" + JSON.stringify(pass) + ";document.getElementById('login-form').dispatchEvent(new Event('submit',{cancelable:true}));return document.getElementById('login-submit').textContent})()";

/** 轮询等待离开登录页；返回最终 href（含超时未离开的 href——调用方自判）。 */
export async function waitLoggedIn(b, tries) {
  const n = tries || 40;
  for (let i = 0; i < n; i++) {
    const h = await b.evaluate('location.href');
    if (h && h.indexOf('/login.html') < 0) return h;
    await sleep(400);
  }
  return await b.evaluate('location.href');
}

/** 便捷组合：干净实例直驱登录（login.html 起步→填表提交→等离开）。 */
export async function loginVia(b, user, pass) {
  await b.evaluate(submitLoginExpr(user, pass));
  return waitLoggedIn(b);
}

/** localStorage 中 OIDC token 的 JWT claims 解码表达式（坑4：TextDecoder UTF-8）。 */
export const claimsExpr = '(function(){try{var t=localStorage.getItem("oneserve-oidc-token")||"";var parts=t.split(".");if(parts.length<2){return null}var b=atob(parts[1].replace(/-/g,"+").replace(/_/g,"/"));var u=new Uint8Array(b.length);for(var i=0;i<b.length;i++){u[i]=b.charCodeAt(i)}return JSON.parse(new TextDecoder("utf-8").decode(u))}catch(e){return {decodeError:String(e && e.message || e)}}})()';

/** .env 读一个键（口令不进仓不进日志；与测试侧 _dotenv_value 同口径）。 */
export function dotenvValue(key, rootDir) {
  const p = path.join(rootDir || process.cwd(), '.env');
  let text = '';
  try { text = fs.readFileSync(p, 'utf8'); } catch { return ''; }
  for (const line of text.split('\n')) {
    const s = line.trim();
    if (!s || s.startsWith('#')) continue;
    const i = s.indexOf('=');
    if (i > 0 && s.slice(0, i).trim() === key) return s.slice(i + 1).trim();
  }
  return '';
}
