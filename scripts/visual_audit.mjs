#!/usr/bin/env node
/**
 * OneServe 视觉走查批代理 · 布局断言扫描器 v5
 *
 * 无登录模式（无登录模式决策）：门户/管理台公开可访，直接开页扫描（历史 JWT 登录
 * 注入逻辑随登录拆除移除，形态见 git 史）。
 * 断言：折行（精准）+ 对齐 + 横向溢出。
 */

import puppeteer from 'puppeteer';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const EVIDENCE_DIR = path.join(ROOT, 'tests/evidence/visual-audit-20261015');
const CHROME_PATH = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const BASE_URL = 'http://localhost:8000';

const backstopConfig = JSON.parse(fs.readFileSync(path.join(ROOT, 'backstop.json'), 'utf8'));
const SCENARIOS = backstopConfig.scenarios;

const VIEWPORTS = [
  { label: 'wide', width: 1440, height: 900 },
  { label: 'narrow', width: 800, height: 900 },
];

const WORST_CASE_INJECT = () => {
  const longName = '测试用超长中文名称单位机构企业名称字段验证数据填充';
  const longPath = '/api/v1/very/long/nested/path/segment/that/keeps/going/and/going/to/test/wrapping/behavior/in/narrow/containers/with/extreme/data';
  
  document.querySelectorAll('td, code, .mono, .endpoint-path, pre').forEach(el => {
    if (el.children.length > 2) return;
    const orig = el.textContent.trim();
    if (orig.length > 0 && orig.length < 100) {
      if (orig.includes('/') || orig.includes('api') || orig.includes('path') || orig.includes('GET') || orig.includes('POST')) {
        el.textContent = longPath;
      } else if (orig.length < 30) {
        el.textContent = longName;
      }
    }
  });
};

const RUN_ASSERTIONS = () => {
  const violations = [];
  
  // === a) 折行检测（2026-10-15 收紧版：只报单行语义元素，排除多行正文容器） ===
  const candidates = document.querySelectorAll('td, th, code, .mono, .text-truncate, [class*="truncate"], span, a, button, label, .badge');
  
  candidates.forEach(el => {
    try {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      if (rect.x < -50 || rect.y < -50) return;
      
      const style = window.getComputedStyle(el);
      const isNowrap = style.whiteSpace === 'nowrap' || style.whiteSpace === 'pre';
      const isTruncated = style.textOverflow === 'ellipsis' || 
        el.classList.contains('text-truncate') || 
        (el.className && typeof el.className === 'string' && el.className.includes('truncate'));
      const isTableCell = el.tagName === 'TD' || el.tagName === 'TH';
      const isCode = el.tagName === 'CODE' || el.classList.contains('mono');
      const isBadge = el.classList.contains('badge');
      const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
      const isShortText = (el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'LABEL') && text.length <= 8;
      
      // 2026-10-15 收紧：排除多行正文容器（p/描述类 td/长文本非 mono td）
      const isDescriptionTd = isTableCell && !isCode && !isNowrap && !isTruncated && text.length > 20;
      const isBodySpan = el.tagName === 'SPAN' && !isCode && !isBadge && !isNowrap && !isTruncated && text.length > 20;
      const isParagraph = el.tagName === 'P' || el.closest('p');
      if (isDescriptionTd || isBodySpan || isParagraph) return;
      
      // 2026-10-15 收紧：nowrap 元素在横滚容器内＝设计行为，不报
      if (isNowrap) {
        let p = el.parentElement;
        while (p && p !== document.body) {
          const ps = window.getComputedStyle(p);
          if (ps.overflowX === 'auto' || ps.overflowX === 'scroll') break;
          p = p.parentElement;
        }
        if (p && p !== document.body) return; // inside scrollable container
      }
      
      const shouldBeSingleLine = isNowrap || isTruncated || (isTableCell && isCode) || (isTableCell && isNowrap) || isCode || isBadge || isShortText;
      if (!shouldBeSingleLine) return;
      
      let lineHeight = parseFloat(style.lineHeight);
      if (isNaN(lineHeight)) lineHeight = (parseFloat(style.fontSize) || 14) * 1.5;
      
      let contentHeight = rect.height;
      if (isTableCell || el.tagName === 'BUTTON' || isBadge) {
        contentHeight -= (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
      }
      
      // 2026-10-15 收紧：ratio 阈值 1.6→3.0，只报真病（排除边界误报）
      const ratio = contentHeight / lineHeight;
      if (ratio >= 3.0) {
        let selector = el.tagName.toLowerCase();
        if (el.id) selector += '#' + el.id;
        else if (el.className && typeof el.className === 'string') {
          const cls = el.className.split(' ').filter(c => c && !c.startsWith('__')).slice(0, 3).join('.');
          if (cls) selector += '.' + cls;
        }
        
        violations.push({
          type: 'wrap', selector, text: text.slice(0, 40),
          x: Math.round(rect.x), y: Math.round(rect.y),
          w: Math.round(rect.width), h: Math.round(rect.height),
          contentH: Math.round(contentHeight), lineHeight: Math.round(lineHeight),
          ratio: ratio.toFixed(2),
          severity: ratio > 6 ? 'high' : ratio > 4 ? 'medium' : 'low',
          reason: isNowrap ? 'nowrap' : isTruncated ? 'truncate' : isTableCell ? 'td' : isCode ? 'code' : isBadge ? 'badge' : 'short-text',
        });
      }
    } catch (e) {}
  });
  
  // === b) 对齐检测 ===
  document.querySelectorAll('input[type="text"], input[type="search"], input[type="number"], input:not([type]), select').forEach(input => {
    const inputRect = input.getBoundingClientRect();
    if (inputRect.height === 0 || inputRect.width === 0) return;
    
    let container = input.closest('form, .row, .d-flex, .input-group, .col-auto, [class*="form-row"], .mb-3');
    if (!container) container = input.parentElement;
    if (!container) return;
    
    container.querySelectorAll('button, .btn, [role="button"]').forEach(btn => {
      const btnRect = btn.getBoundingClientRect();
      if (btnRect.height === 0 || btnRect.width === 0) return;
      
      if (Math.abs(inputRect.top - btnRect.top) < 15) {
        const bottomDiff = Math.abs(inputRect.bottom - btnRect.bottom);
        if (bottomDiff > 4) {
          let inputSel = input.tagName.toLowerCase();
          if (input.id) inputSel += '#' + input.id;
          else if (input.name) inputSel += '[name="' + input.name + '"]';
          
          let btnSel = btn.tagName.toLowerCase();
          if (btn.id) btnSel += '#' + btn.id;
          else btnSel += '("' + (btn.textContent || '').trim().slice(0, 6) + '")';
          
          violations.push({
            type: 'align', selector: inputSel + ' ↔ ' + btnSel,
            text: 'bottom差=' + Math.round(bottomDiff) + 'px',
            x: Math.round(inputRect.x), y: Math.round(inputRect.y),
            bottomDiff: Math.round(bottomDiff),
            severity: bottomDiff > 8 ? 'high' : 'medium',
          });
        }
      }
    });
  });
  
  // === c) 横向溢出 ===
  const docEl = document.documentElement;
  if (docEl.scrollWidth > window.innerWidth + 2) {
    violations.push({
      type: 'overflow', selector: 'document',
      text: 'scrollWidth=' + docEl.scrollWidth + ' innerWidth=' + window.innerWidth,
      x: 0, y: 0, overBy: docEl.scrollWidth - window.innerWidth,
      severity: (docEl.scrollWidth - window.innerWidth) > 50 ? 'high' : 'medium',
    });
  }
  
  return violations;
};

async function main() {
  console.log('🔍 OneServe 视觉走查扫描器 v5（无登录模式：直接开页扫描）');
  
  const browser = await puppeteer.launch({
    executablePath: CHROME_PATH,
    headless: 'new',
    args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
  });
  
  const allResults = {};
  const stats = { wrap: 0, align: 0, overflow: 0, total: 0, pagesFailed: 0 };
  
  for (const scenario of SCENARIOS) {
    const label = scenario.label;
    const url = scenario.url;
    allResults[label] = {};
    
    for (const vp of VIEWPORTS) {
      const tag = label + '@' + vp.label;
      const t0 = Date.now();
      let page;
      
      try {
        page = await browser.newPage();
        await page.setViewport({ width: vp.width, height: vp.height });
        
        await page.goto(url, { waitUntil: 'networkidle2', timeout: 15000 });
        await new Promise(r => setTimeout(r, 2000));
        
        const currentUrl = page.url();
        
        // 注入最坏数据
        await page.evaluate(WORST_CASE_INJECT);
        await new Promise(r => setTimeout(r, 500));
        
        const violations = await page.evaluate(RUN_ASSERTIONS);
        
        const screenshotName = label + '_' + vp.label + '.png';
        await page.screenshot({ path: path.join(EVIDENCE_DIR, screenshotName), fullPage: true });
        
        allResults[label][vp.label] = {
          violations, screenshot: screenshotName,
          elapsed: Date.now() - t0, finalUrl: currentUrl,
        };
        
        violations.forEach(v => { stats[v.type]++; stats.total++; });
        
        const icon = violations.length === 0 ? '✅' : '⚠️';
        console.log(icon + ' ' + tag + ': ' + violations.length + '违例 (' + (Date.now()-t0) + 'ms)');
        
      } catch (err) {
        console.log('❌ ' + tag + ': ' + err.message.slice(0, 80));
        allResults[label][vp.label] = { error: err.message, elapsed: Date.now() - t0 };
        stats.pagesFailed++;
      } finally {
        if (page) await page.close().catch(() => {});
      }
    }
  }
  
  await browser.close();
  
  fs.writeFileSync(path.join(EVIDENCE_DIR, 'audit-results.json'), 
    JSON.stringify({ stats, results: allResults }, null, 2));
  
  console.log('\n📊 折行=' + stats.wrap + ' 对齐=' + stats.align + ' 溢出=' + stats.overflow + 
    ' 总计=' + stats.total + ' 失败=' + stats.pagesFailed);
}

main().catch(err => { console.error('FATAL:', err); process.exit(1); });
