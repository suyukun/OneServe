#!/usr/bin/env node
// doc_fig_check.mjs —— 声明式图形（figures/*.yml）↔ 说明书主文档 机器对账
// 断言：
//   1. f1_overview.yml 六域＋安全贯穿柱 与 说明书 §3.1「架构域划分」域表一致（域名＋内容句逐字）；
//   2. f10_roadmap.yml 五阶段 与 §11.1「实施路线」表一致（阶段名＋范围＋出口判据＋状态逐字）；
// 用法：node scripts/doc_fig_check.mjs   （仓库根目录跑；exit 0=过，exit 1=差异）
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseYaml } from './gen_fig.mjs';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const archDir = join(root, 'docs/architecture');
const doc = readFileSync(join(archDir, '系统架构说明书_v2.md'), 'utf8');

function section(from, to) {
  const i = doc.indexOf(from);
  const j = doc.indexOf(to, i);
  if (i < 0 || j < 0) throw new Error('主文档章节定位失败: ' + from + ' / ' + to);
  return doc.slice(i, j);
}
function tableRows(sec) {
  const lines = sec.split('\n').filter(l => /^\|/.test(l));              // 表格行
  return lines.slice(1)                                                  // 去表头行
    .map(l => l.split('|').slice(1, -1).map(c => c.trim()))
    .filter(cells => cells.length > 0 && !cells.every(c => /^[-: ]*$/.test(c))); // 去表头分隔行
}

let fail = 0;
const miss = (msg) => { console.log('  ✗ ' + msg); fail++; };
const ok = (msg) => console.log('  ✓ ' + msg);

// ---------- ① F1 六域 ↔ §3.1 域表 ----------
console.log('[doc-fig-check] ① F1 六域总览 ↔ §3.1 架构域划分');
{
  const yml = parseYaml(readFileSync(join(archDir, 'figures/f1_overview.yml'), 'utf8'));
  const rows = tableRows(section('### 3.1', '### 3.2'));
  const docDomains = rows.map(r => r[0]);                       // 架构域列
  const docSix = docDomains.filter(d => d !== '安全（贯穿）');   // 六域
  const figSix = yml.rows.flatMap(r => r.blocks.map(b => b.name));
  if (JSON.stringify(figSix) === JSON.stringify(docSix)) ok('六域名称与顺序一致: ' + figSix.join('、'));
  else { miss('六域不一致：图=' + figSix.join('、') + ' vs 文=' + docSix.join('、')); }
  // 安全贯穿柱：文「安全（贯穿）」↔ 图 pillar（名称含「安全」＋tag=贯穿＋内容句逐字）
  const secRow = rows.find(r => r[0] === '安全（贯穿）');
  if (!secRow) miss('§3.1 缺「安全（贯穿）」行');
  else {
    const p = yml.pillar;
    if (p.name.includes('安全') && p.tag === '贯穿') ok('安全贯穿柱在图: ' + p.name + '（' + p.tag + '）');
    else miss('安全柱名称/贯穿标注不一致: ' + p.name + ' / ' + p.tag);
    if (p.desc === secRow[1]) ok('安全贯穿内容句逐字一致: ' + p.desc);
    else miss('安全内容句不一致: 图「' + p.desc + '」 vs 文「' + secRow[1] + '」');
  }
  // 每域内容句逐字（§3.1 内容列）
  let verbatim = 0;
  for (const b of yml.rows.flatMap(r => r.blocks)) {
    const row = rows.find(r => r[0] === b.name);
    if (row && row[1] === b.desc) verbatim++;
    else miss('域「' + b.name + '」内容句不一致: 图「' + b.desc + '」 vs 文「' + (row ? row[1] : '(无行)') + '」');
  }
  if (verbatim === 6) ok('六域内容句全部逐字一致（§3.1 内容列）');
}

// ---------- ② F10 五阶段 ↔ §11.1 实施路线 ----------
console.log('[doc-fig-check] ② F10 演进路线 ↔ §11.1 实施路线');
{
  const yml = parseYaml(readFileSync(join(archDir, 'figures/f10_roadmap.yml'), 'utf8'));
  const rows = tableRows(section('### 11.1', '### 11.2'));
  const docStages = rows.map(r => r[0]);                        // 阶段列
  const figStages = yml.stations.map(s => s.name);
  if (JSON.stringify(figStages) === JSON.stringify(docStages)) ok('五阶段名称与顺序一致: ' + figStages.join('→'));
  else { miss('五阶段不一致：图=' + figStages.join('→') + ' vs 文=' + docStages.join('→')); }
  for (const s of yml.stations) {
    const row = rows.find(r => r[0] === s.name);
    if (!row) { miss('阶段「' + s.name + '」在 §11.1 无对应行'); continue; }
    if (row[1] === s.scope) ok('「' + s.name + '」范围逐字一致');
    else miss('「' + s.name + '」范围不一致: 图「' + s.scope + '」 vs 文「' + row[1] + '」');
    if (row[2] === s.exit) ok('「' + s.name + '」出口判据逐字一致');
    else miss('「' + s.name + '」出口判据不一致: 图「' + s.exit + '」 vs 文「' + row[2] + '」');
    if (row[3] === s.state) ok('「' + s.name + '」状态一致: ' + s.state);
    else miss('「' + s.name + '」状态不一致: 图「' + s.state + '」 vs 文「' + row[3] + '」');
  }
}

if (fail === 0) { console.log('[doc-fig-check] OK：图形数据源与说明书 §3.1/§11.1 完全对账。'); process.exit(0); }
console.log('[doc-fig-check] 差异 ' + fail + ' 项：先判脚本误判还是真漂移；真漂移改图不改文（文为定稿）。');
process.exit(1);
