#!/usr/bin/env node
/* align_scan.mjs —— 表单行对齐 CDP 扫描器（2026-10-16）
 * 出处：docs/reviews/表单对齐规范缺口分析_20261016.md ④节「门禁可行性注解」推荐方案
 * （CDP 实测可行且划算：对每个含 input/select 的行容器，测同行 button 的
 *  getBoundingClientRect() 与同行全部 input 的 top/bottom 差取最大，>2px 报违例）。
 *
 * 用法：
 *   node scripts/align_scan.mjs            # 全站扫描（portal/src + admin 指定页），真栈 http://localhost:8000
 *   node scripts/align_scan.mjs --warn     # 违例只警告，exit code 恒 0
 *   node scripts/align_scan.mjs --pages my,admin/monitor  # 只扫指定页（逗号分隔，可省略 .html）
 * 依赖：本地 8000 端口跑着 OneServe 真栈；复用 scripts/cdp-helpers.mjs（startChrome/loginVia/dotenvValue/sleep）。
 *
 * 判据（与缺口分析 ④ 一致）：
 *   行容器 = computed display 为 flex/grid 的元素，且其子孙含可见 input/select，
 *            且其直接子元素中既有含表单控件的分支也有可见 button。
 *   违例   = 行内 button 与同行全部 input 的 getBoundingClientRect() 逐一对比，
 *            任一 input 的 top/bottom 差 > 2px（取最大者为准，2026-10-16 从「最近 input」升级）。
 *   排除   = ①块流内按钮（非 flex/grid 直接子元素，天然不构成「同行」）；
 *            ②条形图/KPI 排列（行内无表单控件即不入规则）。
 * 输出：终端逐页报告（检查行数/违例数/页面+选择器+像素差）＋
 *       JSON 落 tests/evidence/align-scan-20261016.json；有违例 exit 1（--warn 降级为 0）。
 */
import { startChrome, loginVia, dotenvValue, sleep } from './cdp-helpers.mjs';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BASE = process.env.ALIGN_BASE || 'http://localhost:8000';
const WARN = process.argv.includes('--warn');
const pagesArgIdx = process.argv.indexOf('--pages');
const ROOT_SRC = ['index', 'my', 'audit', 'grants', 'catalog', 'apply', 'trial', 'ops', 'docs', 'errors', 'detail'];
const ADMIN = ['accounts', 'appkeys', 'load', 'monitor', 'perf', 'publish', 'published', 'upstreams', 'versions'];
const PAGES = pagesArgIdx >= 0
  ? process.argv[pagesArgIdx + 1].split(',').map(p => p.replace(/\.html$/, '')).map(p => p.startsWith('admin/') ? p : (ADMIN.includes(p) ? 'admin/' + p : p))
  : [...ROOT_SRC.map(p => p), ...ADMIN.map(p => 'admin/' + p)];

const USER = process.env.ALIGN_USER || 'tester';
const PASS = process.env.ALIGN_PASS || dotenvValue('ONESERVE_TESTER_PASSWORD', ROOT) || '***REMOVED***';   // tester 口令，非 bootstrap-admin

/* 页内扫描表达式：返回 [{row, button, selector, dTop, dBottom, diff}] */
const SCAN_EXPR = `(() => {
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const isInput = el => el.matches('input:not([type=hidden]):not([type=submit]):not([type=button]), select, textarea');
  const selOf = el => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.body; n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) { parts.unshift(s + '#' + n.id); break; }
      if (n.className && typeof n.className === 'string' && n.className.trim()) s += '.' + n.className.trim().split(/\\s+/).slice(0, 2).join('.');
      parts.unshift(s);
    }
    return parts.join(' > ');
  };
  const out = [];
  const all = document.querySelectorAll('body *');
  const rows = [];
  for (const el of all) {
    const s = getComputedStyle(el);
    if (s.display !== 'flex' && s.display !== 'inline-flex' && s.display !== 'grid' && s.display !== 'inline-grid') continue;
    if (!visible(el)) continue;
    const kids = Array.from(el.children).filter(visible);
    const kidInputs = kids.filter(k => k.matches('input, select, textarea') || k.querySelector('input, select, textarea'));   // 含直接子控件（my-caller-controls 型）
    const kidBtns = kids.filter(k => {
      if (k.tagName === 'BUTTON' || (k.tagName === 'INPUT' && ['submit','button'].includes(k.type))) return true;
      // 按钮列包裹层（如 .filter-actions / .audit-filter-actions）：只含按钮、不含表单控件即可；
      // 块流内不相邻的按钮由下方的「同行」垂直邻近判定天然排除
      return !!k.querySelector('button') && !k.querySelector('input, select, textarea');
    });
    if (kidInputs.length && kidBtns.length) rows.push({ el, kidInputs, kidBtns });
  }
  let checked = 0;
  for (const { el, kidBtns } of rows) {
    // 行内全部可见表单控件（含嵌套在字段容器里的）
    const inputs = Array.from(el.querySelectorAll('input, select, textarea')).filter(n => isInput(n) && visible(n));
    if (!inputs.length) continue;
    checked++;
    for (const bk of kidBtns) {
      const btns = bk.tagName === 'BUTTON' || bk.tagName === 'INPUT' ? [bk] : Array.from(bk.querySelectorAll('button')).filter(visible);
      for (const btn of btns) {
        const br = btn.getBoundingClientRect();
        const bcY = (br.top + br.bottom) / 2;
        // 违例判定＝同行全部 input 逐一对比、取最大差（2026-10-16 升级：混用 32/28 高度的
        // 行——如 grants——只看「最近 input」会漏报；任一 input 超差即违例）
        let worst = null;
        for (const inp of inputs) {
          const ir = inp.getBoundingClientRect();
          // 「同行」判定：input 与 button 垂直中心须接近——排除块流按钮/
          // 整面板大容器把上下两块拼成假行（trial/perf 误报源）
          if (Math.abs((ir.top + ir.bottom) / 2 - bcY) > 20) continue;   // 同行容差 20px：过滤行 flex-end 错位中心差≤10px 仍命中，上下两块的 chips 行（≈40px）排除
          const dTop = Math.round((br.top - ir.top) * 10) / 10;
          const dBottom = Math.round((br.bottom - ir.bottom) * 10) / 10;
          const diff = Math.max(Math.abs(dTop), Math.abs(dBottom));
          const dCenter = (dTop + dBottom) / 2;
          // 违例＝顶/底差 >2px 且中心差 >1px（纯「等高不等、中心已对齐」的 chips/控件高度差不算基线错位）
          if (diff > 2 && Math.abs(dCenter) > 1 && (!worst || diff > worst.diff)) worst = { dTop, dBottom, diff };
        }
        if (worst) out.push({ row: selOf(el), button: selOf(btn), dTop: worst.dTop, dBottom: worst.dBottom, diff: worst.diff });
      }
    }
  }
  return { checked, violations: out };
})()`;

let browser;
const report = { generatedAt: new Date().toISOString(), base: BASE, thresholdPx: 2, pages: [], totals: { rows: 0, violations: 0 } };
try {
  browser = await startChrome(9241, 'align-scan-', BASE + '/login.html');
  const href = await loginVia(browser, USER, PASS);
  if (String(href).indexOf('/login.html') >= 0) throw new Error('登录未离开 login.html（实测 href=' + href + '）');
  console.log('[align_scan] 已登录 -> ' + href);

  for (const page of PAGES) {
    const url = BASE + '/' + page + '.html';
    await browser.nav(url, 2000);
    await sleep(600);   // 等 JS 渲染过滤行
    const cur = await browser.evaluate('location.href');
    if (String(cur).indexOf('/' + page + '.html') < 0) {
      console.log('[align_scan] ' + page + ': 被重定向到 ' + cur + '，跳过');
      report.pages.push({ page, skipped: String(cur) });
      continue;
    }
    const res = await browser.evaluate(SCAN_EXPR);
    report.pages.push({ page, rows: res.checked, violations: res.violations });
    report.totals.rows += res.checked; report.totals.violations += res.violations.length;
    console.log('[align_scan] ' + page + ': 行 ' + res.checked + ' · 违例 ' + res.violations.length);
    for (const v of res.violations) console.log('    ✗ ' + v.button + '  (top ' + v.dTop + 'px / bottom ' + v.dBottom + 'px, diff ' + v.diff + 'px)  行=' + v.row);
  }

  const outPath = path.join(ROOT, 'tests/evidence/align-scan-20261016.json');
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(report, null, 2) + '\n');
  console.log('[align_scan] 汇总: 行 ' + report.totals.rows + ' · 违例 ' + report.totals.violations + (report.totals.violations ? (WARN ? '（--warn 降级）' : '') : ' · 全部对齐'));
  console.log('[align_scan] JSON -> ' + outPath);
  process.exitCode = report.totals.violations > 0 && !WARN ? 1 : 0;
} catch (e) {
  console.error('[align_scan] 失败: ' + (e && e.message || e));
  process.exitCode = 2;
} finally { if (browser) await browser.close(); }
