#!/usr/bin/env node
// build_doc_html.mjs —— 《OneServe 系统架构说明书 v2》单文件自包含 HTML 构建（可重复执行）。
//
// 输入：docs/architecture/系统架构说明书_v2.md（已含七处插图引用）
// 输出：docs/architecture/系统架构说明书_v2.html（图片 base64 内联、mermaid 引擎内联，离线单文件可开）
//
// 依赖纪律：
// - 零仓库 npm 依赖：markdown-it 与 mermaid@12 安装进系统临时目录（registry=npmjs），
//   用完即删，仓库不新增 node_modules；
// - 附录 B 错误码表：经系统 python3 导入 service/app/errors.py 的 CODES（仓库既有同源
//   惯例，见 errors.py 模块文档串），构建期转 JSON 注入，不手工维护。
//
// 用法：node scripts/build_doc_html.mjs

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DOC_DIR = path.join(ROOT, 'docs', 'architecture');
const MD_PATH = path.join(DOC_DIR, '系统架构说明书_v2.md');
const HTML_PATH = path.join(DOC_DIR, '系统架构说明书_v2.html');
const ERRORS_PACKAGE_DIR = path.join(ROOT, 'service', 'app');
const NPM_REGISTRY = 'https://registry.npmjs.org';

const DOC_TITLE = 'OneServe 系统架构说明书';
const DOC_VERSION = '版本 v2.0';
const DOC_DATE = '2026-10-20';
const DOC_BADGE = '单文件离线文档';

// 附录 B 占位句（构建时被错误码全表替换；措辞变化须同步本常量）
const APPENDIX_B_PLACEHOLDER =
  '错误码集中定义于服务层错误码表（全量九十七码），文档构建时自动注入全表，与代码同步生成，不手工维护。';
const EXPECTED_CODE_COUNT = 97; // 与正文「九十七码」表述互锁，防码表漂移后文档说谎

const escapeHtml = (s) =>
  String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

// ───────────────────────── 1. 临时依赖（markdown-it ＋ mermaid@12，用完即删） ─────────────────────────

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'oneserve-doc-build-'));
let MarkdownIt;
let mermaidJs;
try {
  fs.writeFileSync(path.join(tmpDir, 'package.json'),
    JSON.stringify({ name: 'oneserve-doc-build-tmp', private: true }, null, 2));
  try {
    execFileSync('npm', [
      'install', '--no-save', '--no-audit', '--no-fund', '--loglevel=error',
      `--registry=${NPM_REGISTRY}`, 'markdown-it@14', 'mermaid@12',
    ], { cwd: tmpDir, encoding: 'utf8' });
  } catch (err) {
    throw new Error(`临时依赖安装失败（registry=${NPM_REGISTRY}）：${err.message}`);
  }
  const requireFromTmp = createRequire(path.join(tmpDir, 'package.json'));
  MarkdownIt = requireFromTmp('markdown-it');
  const mermaidMin = path.join(tmpDir, 'node_modules', 'mermaid', 'dist', 'mermaid.min.js');
  if (!fs.existsSync(mermaidMin)) {
    throw new Error(`mermaid dist 缺失：${mermaidMin}`);
  }
  mermaidJs = fs.readFileSync(mermaidMin, 'utf8');
  if (mermaidJs.includes('</script')) {
    throw new Error('mermaid.min.js 含 </script 字面量，无法安全内联，需改用拆分转义方案');
  }
} finally {
  fs.rmSync(tmpDir, { recursive: true, force: true });
}

// ───────────────────────── 2. 附录 B 错误码全表（service/app/errors.py 同源） ─────────────────────────

const dumpScript =
  'import json,sys\n' +
  `sys.path.insert(0, ${JSON.stringify(ERRORS_PACKAGE_DIR)})\n` +
  'import errors\n' +
  'print(json.dumps([{"code": e.code, "http": e.http_status, "alt": list(e.alt_statuses),'
  + ' "meaning": e.meaning, "scope": e.scope, "source": e.source} for e in errors.CODES],'
  + ' ensure_ascii=False))\n';
let codes;
try {
  codes = JSON.parse(execFileSync('python3', ['-c', dumpScript],
    { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
} catch (err) {
  throw new Error(`错误码表解析失败（service/app/errors.py）：${err.message}`);
}
if (codes.length !== EXPECTED_CODE_COUNT) {
  throw new Error(
    `错误码数 ${codes.length} ≠ ${EXPECTED_CODE_COUNT}：errors.py 与文档「九十七码」表述已漂移，`
    + '请同步修正附录 B 占位句与本脚本 EXPECTED_CODE_COUNT');
}

const cell = (s) => String(s).replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
const scopeLabel = (c) => {
  if (c.scope === 'row') return '批量结果行（非 HTTP）';
  if (c.scope === 'job') return '批量任务级（非 HTTP）';
  return c.source === 'gateway' ? 'HTTP 面（网关侧）' : 'HTTP 面';
};
const errorTableMd = [
  '| 错误码 | HTTP 状态 | 说明 | 适用范围 |',
  '|---|---|---|---|',
  ...codes.map((c) => {
    const status = c.http == null ? '—'
      : (c.alt && c.alt.length ? [c.http, ...c.alt].join('/') : String(c.http));
    return `| ${cell(c.code)} | ${status} | ${cell(c.meaning)} | ${scopeLabel(c)} |`;
  }),
].join('\n');

// ───────────────────────── 3. markdown 预处理 ＋ 渲染（自定义图片/围栏/标题规则） ─────────────────────────

const mdBytes = fs.readFileSync(MD_PATH);
const sourceSha256 = createHash('sha256').update(mdBytes).digest('hex'); // 源文档指纹，注入封面与 <meta>
let md = mdBytes.toString('utf8');
if (!md.includes(APPENDIX_B_PLACEHOLDER)) {
  throw new Error('附录 B 占位句未命中：说明书措辞已变，请同步 APPENDIX_B_PLACEHOLDER');
}
const appendixBReplacement =
  `全量 ${codes.length} 码错误码全表如下（构建时由 \`scripts/build_doc_html.mjs\` 从 \`service/app/errors.py\` `
  + '解析注入，与代码同步生成，不手工维护）。\n\n' + errorTableMd + '\n\n';
md = md.replace(APPENDIX_B_PLACEHOLDER, appendixBReplacement);

const mdLib = MarkdownIt({ html: false, linkify: false, typographer: false, breaks: false });

const counters = { img: 0, mermaid: 0 };
const toc = []; // { tag: 'h2'|'h3', text, id }
const slugSeen = new Map();
const slugify = (raw) => {
  let s = raw.trim().toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff]+/g, '-')
    .replace(/^-+|-+$/g, '');
  if (!s) s = 'sec';
  const n = (slugSeen.get(s) || 0) + 1;
  slugSeen.set(s, n);
  return n === 1 ? s : `${s}-${n}`;
};

// 标题：h2/h3 加锚点 id 并收集目录项
mdLib.renderer.rules.heading_open = (tokens, idx) => {
  const open = tokens[idx];
  const text = tokens[idx + 1].content;
  if (open.tag === 'h2' || open.tag === 'h3') {
    const id = slugify(text);
    toc.push({ tag: open.tag, text, id });
    return `<${open.tag} id="${id}">`;
  }
  return `<${open.tag}>`;
};

// 图片：读文件转 data URI（SVG/PNG base64），包 <figure>＋<figcaption alt｜来源：原路径>
mdLib.renderer.rules.image = (tokens, idx) => {
  const tok = tokens[idx];
  const src = tok.attrGet('src');
  if (!src || /^(https?:)?\/\//.test(src) || path.isAbsolute(src)) {
    throw new Error(`插图引用须为仓库相对路径：${src}`);
  }
  const abs = path.join(DOC_DIR, src);
  const ext = path.extname(src).toLowerCase();
  const mime = ext === '.svg' ? 'image/svg+xml' : ext === '.png' ? 'image/png' : null;
  if (!mime) throw new Error(`不支持的图片类型：${src}`);
  const dataUri = `data:${mime};base64,${fs.readFileSync(abs).toString('base64')}`;
  const alt = (tok.content || '').trim();
  counters.img += 1;
  return '<figure class="fig">'
    + `<img src="${dataUri}" alt="${escapeHtml(alt)}">`
    + `<figcaption>${escapeHtml(alt)}｜来源：${escapeHtml(src)}</figcaption>`
    + '</figure>';
};

// 围栏：mermaid → <pre class="mermaid">（页面加载后 mermaid.run 渲染）；其余按普通代码块
mdLib.renderer.rules.fence = (tokens, idx) => {
  const tok = tokens[idx];
  const lang = tok.info.trim().split(/\s+/)[0];
  if (lang === 'mermaid') {
    counters.mermaid += 1;
    return `<pre class="mermaid">${escapeHtml(tok.content)}</pre>`;
  }
  return `<pre><code${lang ? ` class="language-${lang}"` : ''}>${escapeHtml(tok.content)}</code></pre>`;
};

const bodyHtml = mdLib.render(md);

const h2Count = toc.filter((e) => e.tag === 'h2').length;
const h3Count = toc.filter((e) => e.tag === 'h3').length;
if (counters.img !== 7) throw new Error(`插图数 ${counters.img} ≠ 7`);
if (counters.mermaid !== 3) throw new Error(`mermaid 块 ${counters.mermaid} ≠ 3`);
if (h2Count !== 17) throw new Error(`h2 章节数 ${h2Count} ≠ 17（12 章＋5 附录）`);

// ───────────────────────── 4. 目录（全部 h2/h3 锚点；置于文首横线后、读者导航之下） ─────────────────────────

const tocItems = [];
for (const e of toc) {
  if (e.tag === 'h2') tocItems.push({ ...e, children: [] });
  else if (tocItems.length) tocItems[tocItems.length - 1].children.push(e);
}
const tocHtml = '<nav class="toc" id="toc">\n<div class="toc-title">目录</div>\n<ol>\n'
  + tocItems.map((h2) => {
    const subs = h2.children.length
      ? '\n  <ul>\n' + h2.children
        .map((h3) => `    <li><a href="#${h3.id}">${escapeHtml(h3.text)}</a></li>`).join('\n')
        + '\n  </ul>'
      : '';
    return `  <li><a href="#${h2.id}">${escapeHtml(h2.text)}</a>${subs}</li>`;
  }).join('\n')
  + '\n</ol>\n</nav>';

// 目录插在文首 <hr> 之后：封面区（含版本/读者导航既有文字）保持在最顶，目录紧随其后
const hrIdx = bodyHtml.indexOf('<hr>');
if (hrIdx < 0) throw new Error('文首分隔线 <hr> 未命中，无法定位目录插入点');
const mainHtml = bodyHtml.slice(0, hrIdx + 4) + '\n' + tocHtml + '\n' + bodyHtml.slice(hrIdx + 4);

// ───────────────────────── 5. 封面 ＋ CSS ＋ 组装 ─────────────────────────

// 构建时间（东八区，YYYY-MM-DD HH:MM）：UTC 毫秒 +8h 后取 ISO 前段，免依赖 Intl 时区库
const buildTimeCn = new Date(Date.now() + 8 * 60 * 60 * 1000).toISOString().slice(0, 16).replace('T', ' ');

const coverHtml = [
  '<header class="cover">',
  '  <p class="cover-system">OneServe</p>',
  `  <h1 class="cover-title">系统架构说明书</h1>`,
  `  <p class="cover-meta">${DOC_VERSION}｜${DOC_DATE}</p>`,
  `  <p class="cover-meta">源文档 sha256：${sourceSha256.slice(0, 16)}｜构建时间：${buildTimeCn}（东八区）</p>`,
  `  <p class="cover-badge">${DOC_BADGE}</p>`,
  '</header>',
].join('\n');

const css = `
:root { color-scheme: light; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  margin: 0; background: #fff; color: #111; line-height: 1.75; font-size: 15px;
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui,
    -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
.doc { max-width: 980px; margin: 0 auto; padding: 28px 32px 64px; }
h1, h2, h3, h4 { line-height: 1.35; }
h1 { font-size: 26px; margin: 24px 0 16px; }
h2 { font-size: 21px; margin: 34px 0 14px; padding-bottom: 6px; border-bottom: 2px solid #333; }
h3 { font-size: 17px; margin: 26px 0 10px; }
p { margin: 10px 0; }
a { color: #0b4fa8; }
hr { border: none; border-top: 1px solid #bbb; margin: 26px 0; }
ul, ol { padding-left: 24px; }
li { margin: 3px 0; }

/* 封面区 */
.cover { border: 3px double #111; padding: 38px 28px 30px; text-align: center; margin: 8px 0 30px; }
.cover-system { font-size: 20px; font-weight: 700; letter-spacing: .4em; text-indent: .4em; margin: 0 0 10px; }
.cover-title { font-size: 34px; margin: 0 0 16px; padding: 0; border: none; }
.cover-meta { font-size: 15px; color: #222; margin: 4px 0; }
.cover-badge { display: inline-block; margin: 14px 0 0; padding: 3px 14px; font-size: 13px;
  border: 1px solid #444; border-radius: 999px; color: #222; letter-spacing: .12em; }

/* 目录 */
.toc { border: 1px solid #888; background: #f7f7f7; padding: 16px 24px; margin: 22px 0 34px; }
.toc-title { font-size: 17px; font-weight: 700; margin-bottom: 8px; }
.toc ol { margin: 0; padding-left: 22px; }
.toc ul { margin: 3px 0 8px; padding-left: 20px; list-style: none; }
.toc li { margin: 3px 0; }

/* 表格：细边框、黑白可读 */
table { border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px; }
th, td { border: 1px solid #555; padding: 6px 9px; text-align: left; vertical-align: top; }
th { background: #ececec; font-weight: 600; }
tbody tr:nth-child(even) { background: #f6f6f6; }

/* 插图：整宽自适应＋图注 */
figure.fig { margin: 22px 0; padding: 12px; border: 1px solid #999; background: #fff; }
figure.fig img { display: block; width: 100%; height: auto; }
figure.fig figcaption { margin-top: 10px; padding-top: 8px; border-top: 1px dashed #888;
  font-size: 13px; color: #333; text-align: center; }

/* mermaid 容器：白底 */
pre.mermaid { border: 1px solid #999; background: #fff; padding: 16px 10px; margin: 20px 0;
  text-align: center; overflow-x: auto; }
pre.mermaid svg { max-width: 100%; height: auto; background: #fff; }

/* 代码块 */
pre { background: #f4f4f4; border: 1px solid #ddd; border-radius: 4px; padding: 12px 14px;
  overflow-x: auto; font-size: 13px; line-height: 1.55; }
code { font-family: "SF Mono", Menlo, Consolas, "Courier New", monospace;
  background: #efefef; border-radius: 3px; padding: 1px 5px; font-size: .92em; }
pre code { background: none; padding: 0; border-radius: 0; }
blockquote { margin: 12px 0; padding: 6px 14px; border-left: 4px solid #888; background: #f6f6f6; }

/* A4 打印：分页友好、黑白可读 */
@media print {
  @page { size: A4; margin: 16mm 14mm; }
  body { font-size: 11.5px; }
  .doc { max-width: none; padding: 0; }
  h2 { page-break-before: always; }
  h2, h3 { page-break-after: avoid; }
  table, figure.fig, pre, pre.mermaid { page-break-inside: avoid; }
  a { color: #000; text-decoration: none; }
  .toc { background: none; }
}
`;

const mermaidBoot = `
(function () {
  if (typeof mermaid === 'undefined') return;
  mermaid.initialize({
    startOnLoad: false,
    theme: 'neutral',
    themeVariables: { background: '#ffffff' },
    fontFamily: '"PingFang SC","Hiragino Sans GB","Microsoft YaHei",system-ui,sans-serif'
  });
  var run = function () { mermaid.run({ querySelector: 'pre.mermaid' }); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run);
  else run();
})();
`;

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="source-sha256" content="${sourceSha256}">
<title>${DOC_TITLE} ${DOC_VERSION}（${DOC_BADGE}）</title>
<style>${css}</style>
</head>
<body>
<div class="doc">
${coverHtml}
${mainHtml}
</div>
<script>
${mermaidJs}
</script>
<script>${mermaidBoot}</script>
</body>
</html>
`;

// ───────────────────────── 6. 自包含校验 ＋ 落盘 ─────────────────────────

// 文档区（封面＋目录＋正文，不含内联脚本/样式）不得出现任何外部引用（锚点除外）
const docPart = coverHtml + '\n' + mainHtml;
const badSrc = docPart.match(/src="(?!data:)/g) || [];
const badHref = docPart.match(/href="https?:\/\//g) || [];
if (badSrc.length || badHref.length) {
  throw new Error(`文档区存在外部引用：src=${badSrc.length}，href=${badHref.length}`);
}
const tableCount = (docPart.match(/<table>/g) || []).length;
if (tableCount < 20) throw new Error(`表格数异常：${tableCount}`);

fs.writeFileSync(HTML_PATH, html, 'utf8');
const sizeBytes = fs.statSync(HTML_PATH).size;

console.log(JSON.stringify({
  output: path.relative(ROOT, HTML_PATH),
  sizeBytes,
  sizeMB: (sizeBytes / 1024 / 1024).toFixed(2),
  h2: h2Count,
  h3: h3Count,
  mermaidBlocks: counters.mermaid,
  img: counters.img,
  tables: tableCount,
  appendixBCodes: codes.length,
  externalRefs: { src: badSrc.length, hrefHttp: badHref.length },
}, null, 2));
