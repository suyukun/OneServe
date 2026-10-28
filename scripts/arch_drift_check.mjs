#!/usr/bin/env node
// arch_drift_check.mjs —— 架构图（.c4）↔ docker-compose 机器对账
//
// 判据（口径与 docs/architecture/deployment.c4 头注一致）：
//   1. 图侧真源：docs/architecture/*.c4 中 deployment 块的 node 实例
//      （instanceOf 指向 oneserve.* 容器者才计入；ext 外部边界实例不计）。
//   2. compose 侧真源：docker-compose.yml（主栈）＋ compose.hbase.yml ＋ compose.kafka.yml
//      ＋ compose.metadata.yml（元数据库叠加栈，2026-10-21 起纳入——1b 批已实施，保持图=真相）。
//      不入对账：compose.m26-iso.yml、compose.sync.yml（一次性起撤临时栈，deployment.c4 头注已声明）。
//   3. 容器名→service 名映射（kebab 差异）：batchWorker→batch-worker、casdoorDb→casdoor-db、
//      metadataDb→metadata-db，其余 1:1。
//   4. 双向比对：
//      图有 compose 无 ＝ 疑似过期图（模型该删）；
//      compose 有 图无 ＝ 疑似漏画（模型该补；一次性 init job 亦计入，如实暴露）。
// 用法：node scripts/arch_drift_check.mjs   （在仓库根目录跑）
//   exit 0 = 一致；exit 1 = 有差异（差异清单打印到 stdout）。

import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const archDir = join(root, 'docs/architecture');

// ---------- 图侧：解析 deployment 块里的 node 实例 ----------
//   一次性 init job（casdoorInit/topicInit）与主容器同 instanceOf，需显式映射到各自 service 名。
const nameMap = { batchWorker: 'batch-worker', casdoorDb: 'casdoor-db', metadataDb: 'metadata-db', casdoorInit: 'casdoor-init', topicInit: 'topic-init' };
const graphServices = new Set();
for (const f of readdirSync(archDir).filter(f => f.endsWith('.c4'))) {
  const src = readFileSync(join(archDir, f), 'utf8');
  // 2026-10-19：模型容器按说明书 §3.3 分组后 instanceOf 为嵌套 FQN（oneserve.dataPlane.apisix 等），
  //   取路径末段为容器 id 再映射；node id 优先（casdoorInit 与 casdoorN 同容器，靠 id 区分）。
  // 写法A（直挂式）：node x '标题' { instanceOf oneserve.fqn }
  for (const m of src.matchAll(/node\s+(\w+)\s+'[^']*'\s*\{\s*instanceOf\s+oneserve\.([\w.]+)/g)) {
    const containerId = m[2].split('.').pop();
    graphServices.add(nameMap[m[1]] ?? nameMap[containerId] ?? containerId);
  }
  // 写法B（分区实例赋值式，2026-10-19 1fa6e57 起）：xxxI = instanceOf oneserve.fqn '标题'
  //   实例 id 优先（casdoorInitI→casdoor-init 靠 id 区分同容器的一次性任务），去尾 I 后查映射；否则用 FQN 末段。
  for (const m of src.matchAll(/(\w+)\s*=\s*instanceOf\s+oneserve\.([\w.]+)/g)) {
    const instId = m[1].replace(/I$/, '');
    const containerId = m[2].split('.').pop();
    graphServices.add(nameMap[instId] ?? nameMap[containerId] ?? containerId);
  }
}

// ---------- compose 侧：解析 services 键（优先 js-yaml，缺则正则降级） ----------
const composeFiles = ['docker-compose.yml', 'compose.hbase.yml', 'compose.kafka.yml', 'compose.metadata.yml'];
const composeServices = new Map(); // service -> file
let yaml = null;
try {
  yaml = createRequire(join(root, 'package.json'))('js-yaml');
} catch { /* 降级正则 */ }
for (const f of composeFiles) {
  const src = readFileSync(join(root, f), 'utf8');
  let names = [];
  if (yaml) {
    const doc = yaml.load(src);
    names = Object.keys(doc?.services ?? {});
  } else {
    let inSvc = false;
    for (const line of src.split('\n')) {
      if (/^services:/.test(line)) { inSvc = true; continue; }
      if (inSvc && /^[^\s#]/.test(line)) inSvc = false;
      const m = inSvc ? line.match(/^  ([A-Za-z0-9_-]+):/) : null;
      if (m) names.push(m[1]);
    }
  }
  for (const n of names) composeServices.set(n, f);
}

// ---------- 双向比对 ----------
const stale = [...graphServices].filter(s => !composeServices.has(s));   // 图有 compose 无
const missing = [...composeServices.keys()].filter(s => !graphServices.has(s)); // compose 有 图无

if (graphServices.size === 0) {
  console.error('[arch-drift-check] 图侧解析结果为 0——脚本与模型写法脱节（脚本 bug），不是漂移；请修脚本正则，勿动模型或 compose。');
  process.exit(2);
}
console.log('[arch-drift-check] 图侧实例 ' + graphServices.size + ' 个: ' + [...graphServices].sort().join(', '));
console.log('[arch-drift-check] compose 侧 (' + composeFiles.join(' + ') + '): ' + [...composeServices.keys()].sort().join(', '));
if (stale.length === 0 && missing.length === 0) {
  console.log('[arch-drift-check] OK：图 ↔ compose 完全一致。');
  process.exit(0);
}
for (const s of stale) console.log('  ✗ 疑似过期图：' + s + ' 在图中，但 compose 无此 service');
for (const s of missing) console.log('  ✗ 疑似漏画：' + s + ' 在 ' + composeServices.get(s) + '，图中无对应实例');
console.log('[arch-drift-check] 差异 ' + (stale.length + missing.length) + ' 项：先判脚本误判还是真漂移；真漂移修模型，不动 compose。');
process.exit(1);
