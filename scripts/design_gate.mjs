#!/usr/bin/env node
/**
 * OneServe Design Gate v1.2
 * ──────────────────────────────────────────────────────────────
 * 静态设计门禁：10 项可判定断言 ＋ stylelint（可选）
 *
 * 模式：
 *   node scripts/design_gate.mjs                  # 棘轮模式（默认）：存量放行，新增阻断
 *   node scripts/design_gate.mjs --strict          # 严格模式：全量阻断（CI/手动审查用）
 *   node scripts/design_gate.mjs --baseline        # 基线报告模式：输出明细，不阻断
 *   node scripts/design_gate.mjs --update-baseline # 更新基线快照文件
 *
 * 基线棘轮：scripts/design_gate_baseline.json 记录存量违例指纹。
 *   棘轮模式下，仅不在基线中的新增违例触发 exit 1；存量违例计数但不阻断。
 *   解锁提交死锁：47 存量违例不再挡 pre-commit。
 *
 * v1.2 修正：
 *   - A6/A7 改元素级计数（解析 HTML 结构，非字符串匹配）
 *   - 源/产物去重：只计 admin/ 真源，跳过 portal/admin/ 镜像
 *   - 忽略 <style> 内 CSS 定义与 JS 字符串/Alpine 三元表达式
 */

import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from "fs";
import { join, relative, extname, basename } from "path";
import { execSync } from "child_process";
import { createHash } from "crypto";

const ROOT = new URL("..", import.meta.url).pathname.replace(/\/$/, "");
const BASELINE_FILE = join(ROOT, "scripts", "design_gate_baseline.json");
const STRICT_MODE = process.argv.includes("--strict");
const BASELINE_MODE = process.argv.includes("--baseline");
const UPDATE_BASELINE = process.argv.includes("--update-baseline");

// ─── 合法值集 ─────────────────────────────────────────────────
const LEGAL_FONT_SIZES = new Set([9, 10, 11, 12, 13, 14, 15, 16, 18, 20, 24, 26, 30, 32, 36]);
const LEGAL_OS_SPACES = new Set([0, 4, 8, 12, 16, 24, 32, 48]);
const LEGACY_SPACES = new Set([1, 2, 3, 6, 7, 10, 15, 18, 22]);
const ALL_LEGAL_SPACES = new Set([...LEGAL_OS_SPACES, ...LEGACY_SPACES]);
const LEGAL_RADIUS_PATTERNS = [
  /^0(px)?$/,
  /^[23468]px$/,
  /^1[04]px$/,
  /^999px$/,
  /^var\(--radius/,
];

const TOKEN_HEX_SET = new Set([
  "c8102e","e85b6b","009423","cb272d","d4760a",
  "5070dd","505372","785db0","0ca8df","ff994d","3fbe95","fb628b",
  "ffffff","fafafa","f5f5f5","f0f0f0","d9d9d9","bfbfbf","8c8c8c",
  "595959","434343","262626","1f1f1f","141414","000000",
  "2563eb","6b4a00","4a3305",
  "3fb965","ef6b6b","e2a03f","7b93ec",
  "12151c","1a2029","202836","263244","2e3c50","253040",
  "e8ebf1","bcc4d0","828d9c","5a6472","6ea8fe","14161a",
  "fff","000",
  "e6f4ff","0958d9","fff1f0","cf1322",
]);

// ─── 工具 ─────────────────────────────────────────────────────
function walkDir(dir, exts, skipDirs = []) {
  const results = [];
  try {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      try {
        const st = statSync(full);
        if (st.isDirectory()) {
          if (!skipDirs.includes(entry)) results.push(...walkDir(full, exts, skipDirs));
        } else if (exts.includes(extname(entry))) {
          results.push(full);
        }
      } catch {}
    }
  } catch {}
  return results;
}

const violations = [];
function addV(file, line, rule, msg) {
  violations.push({ file: relative(ROOT, file), line, rule, message: msg });
}

/** Generate a stable fingerprint for a violation */
function violationFingerprint(v) {
  return `${v.rule}|${v.file}|${v.line}|${v.message}`;
}

// ─── 文件列表 ─────────────────────────────────────────────────
// CSS: portal/assets (excl vendor/tokens.css) + admin/ inline styles are checked via HTML
const cssFiles = [
  ...walkDir(join(ROOT, "portal/assets"), [".css"], ["vendor"]).filter(f => !f.endsWith("tokens.css")),
  ...walkDir(join(ROOT, "admin"), [".css"]),
];

// HTML: Only TRUE SOURCE files. Skip portal/admin/ (build mirror of admin/)
// and selftest pages (not business pages).
const htmlSourceFiles = [];
// portal/src/ source templates
htmlSourceFiles.push(...walkDir(join(ROOT, "portal/src"), [".html"], ["vendor", "perf-reports", "catalog"]));
// portal root .html (built pages that aren't mirrors)
for (const f of walkDir(join(ROOT, "portal"), [".html"], ["vendor", "perf-reports", "catalog", "admin", "src"])) {
  htmlSourceFiles.push(f);
}
// admin/ true source (NOT portal/admin/ which is a build mirror)
for (const f of walkDir(join(ROOT, "admin"), [".html"], ["vendor"])) {
  // Skip selftest pages
  if (basename(f).startsWith("selftest-")) continue;
  htmlSourceFiles.push(f);
}

console.log(`Design Gate v1.2 · ${cssFiles.length} CSS + ${htmlSourceFiles.length} HTML (source only, mirrors excluded)`);

// ─── 正则 ─────────────────────────────────────────────────────
const HEX_RE = /#[0-9a-fA-F]{3,8}\b/gi;
const FS_RE = /font-size:\s*(\d+)px/g;
const SPACE_RE = /(?:margin|padding|gap)(?:-(?:top|right|bottom|left|x|y))?:\s*(\d+)px/g;
const RADIUS_RE = /border-radius:\s*([^;}\n]+)/g;
const OUTLINE_NONE_RE = /outline:\s*none/g;
const RADIUS_50_RE = /border-radius:\s*50%/g;
const ADMIN_VAR_RE = /--admin-[\w-]+\s*:/g;

// ─── HTML 解析辅助（轻量级，不依赖外部库）─────────────────────

/** Strip <style>...</style> blocks from HTML content, replacing with blank lines to preserve line numbers */
function stripStyleBlocks(html) {
  return html.replace(/<style[\s\S]*?<\/style>/gi, (match) => {
    // Replace with same number of newlines to preserve line numbering
    return match.replace(/[^\n]/g, ' ');
  });
}

/** Strip <script>...</script> blocks similarly */
function stripScriptBlocks(html) {
  return html.replace(/<script[\s\S]*?<\/script>/gi, (match) => {
    return match.replace(/[^\n]/g, ' ');
  });
}

/** Count elements with a specific class in HTML (element-level, not string match)
 *  Only counts actual class="..." attributes on HTML tags, ignoring:
 *  - CSS selectors (.btn-primary { ... })
 *  - Alpine :class bindings (:class="... ? 'btn-primary' : ''")
 *  - JS string literals
 */
function countElementsByClass(htmlContent, className) {
  // Remove <style> and <script> blocks first
  const cleaned = stripStyleBlocks(stripScriptBlocks(htmlContent));
  
  // Match class="..." attributes containing the target class
  // This regex finds class="..." on actual HTML elements
  const classAttrRe = /\bclass\s*=\s*"([^"]*)"/gi;
  let count = 0;
  let m;
  while ((m = classAttrRe.exec(cleaned)) !== null) {
    const classes = m[1].split(/\s+/);
    if (classes.includes(className)) count++;
  }
  return count;
}

/** Count brand-red references in HTML body (outside <style>/<script>)
 *  Counts var(--brand-primary), var(--brand-dark-accent) usage in HTML attributes
 *  Does NOT count CSS definitions inside <style> blocks
 */
function countBrandRedInHtml(htmlContent) {
  const cleaned = stripStyleBlocks(stripScriptBlocks(htmlContent));
  const BRAND_RE = /var\(\s*--brand-(?:primary|dark-accent)\s*\)/gi;
  const matches = cleaned.match(BRAND_RE);
  return matches ? matches.length : 0;
}

// ─── 断言执行 ─────────────────────────────────────────────────
const counts = {};

// A1: 裸 hex（CSS·tokens.css 外）
let hexPureCount = 0, hexFallbackCount = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("/*") || line.trim().startsWith("*")) continue;
    let m; HEX_RE.lastIndex = 0;
    while ((m = HEX_RE.exec(line)) !== null) {
      const h = m[0].slice(1).toLowerCase();
      if (TOKEN_HEX_SET.has(h)) continue;
      const before = line.slice(0, m.index);
      const isFallback = /var\([^)]*$/.test(before);
      if (isFallback) {
        hexFallbackCount++;
      } else {
        hexPureCount++;
        addV(file, i+1, "no-raw-color", `裸 hex ${m[0]}`);
      }
    }
  }
}
counts["A1·裸hex(CSS纯裸)"] = hexPureCount;
counts["A1b·hex(var fallback)"] = hexFallbackCount;

// A2: font-size 越界
let fsBad = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    let m; FS_RE.lastIndex = 0;
    while ((m = FS_RE.exec(lines[i])) !== null) {
      if (!LEGAL_FONT_SIZES.has(parseInt(m[1]))) {
        fsBad++;
        addV(file, i+1, "fs-scale", `font-size:${m[1]}px 越界`);
      }
    }
  }
}
counts["A2·font-size越界"] = fsBad;

// A3: 间距越界
let spBad = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    let m; SPACE_RE.lastIndex = 0;
    while ((m = SPACE_RE.exec(lines[i])) !== null) {
      if (!ALL_LEGAL_SPACES.has(parseInt(m[1]))) {
        spBad++;
        addV(file, i+1, "space-scale", `间距 ${m[1]}px 越界`);
      }
    }
  }
}
counts["A3·间距越界"] = spBad;

// A4: border-radius 越界
let radBad = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    let m; RADIUS_RE.lastIndex = 0;
    while ((m = RADIUS_RE.exec(lines[i])) !== null) {
      const val = m[1].trim();
      const ok = LEGAL_RADIUS_PATTERNS.some(p => p.test(val));
      if (!ok) {
        radBad++;
        addV(file, i+1, "radius-scale", `border-radius:${val} 越界`);
      }
    }
  }
}
counts["A4·radius越界"] = radBad;

// A5: HTML 内联 hex
let inlineHex = 0;
for (const file of htmlSourceFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const styles = lines[i].matchAll(/style="([^"]*)"/g);
    for (const sm of styles) {
      let m; HEX_RE.lastIndex = 0;
      while ((m = HEX_RE.exec(sm[1])) !== null) {
        const h = m[0].slice(1).toLowerCase();
        if (!TOKEN_HEX_SET.has(h)) {
          inlineHex++;
          addV(file, i+1, "inline-hex", `内联 hex ${m[0]}`);
        }
      }
    }
  }
}
counts["A5·HTML内联hex"] = inlineHex;

// A6: 主按钮≤1/页（v1.2: 元素级计数，排除 CSS 定义/Alpine 三元/JS 字符串，只计真源）
let btnOver = 0;
const a6Details = [];
for (const file of htmlSourceFiles) {
  const content = readFileSync(file, "utf8");
  const c = countElementsByClass(content, "btn-primary");
  if (c > 1) {
    btnOver++;
    const relPath = relative(ROOT, file);
    a6Details.push({ file: relPath, count: c });
    addV(file, 0, "btn-primary-limit", `.btn-primary ×${c}（≤1/页·元素级）`);
  }
}
counts["A6·主按钮>1/页"] = btnOver;

// A7: 品牌红≤2处/页（v1.2: 只计 HTML body 中 var(--brand-*) 引用，排除 <style> CSS 定义）
let brandOver = 0;
const a7Details = [];
for (const file of htmlSourceFiles) {
  const content = readFileSync(file, "utf8");
  const c = countBrandRedInHtml(content);
  if (c > 2) {
    brandOver++;
    const relPath = relative(ROOT, file);
    a7Details.push({ file: relPath, count: c });
    addV(file, 0, "brand-red-limit", `品牌红 ×${c}（≤2/页·body引用）`);
  }
}
counts["A7·品牌红>2/页"] = brandOver;

// A8: outline:none 无替代
let focusNone = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (OUTLINE_NONE_RE.test(lines[i])) {
      OUTLINE_NONE_RE.lastIndex = 0;
      focusNone++;
      addV(file, i+1, "focus-none", `outline:none 无替代`);
    }
    OUTLINE_NONE_RE.lastIndex = 0;
  }
}
counts["A8·focus-none"] = focusNone;

// A9: border-radius:50%
let r50 = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (RADIUS_50_RE.test(lines[i])) {
      RADIUS_50_RE.lastIndex = 0;
      r50++;
      addV(file, i+1, "no-radius-50", `border-radius:50% → var(--radius-full)`);
    }
    RADIUS_50_RE.lastIndex = 0;
  }
}
counts["A9·radius:50%"] = r50;

// A10: --admin-* 新定义
let adminVar = 0;
for (const file of cssFiles) {
  const lines = readFileSync(file, "utf8").split("\n");
  for (let i = 0; i < lines.length; i++) {
    let m; ADMIN_VAR_RE.lastIndex = 0;
    while ((m = ADMIN_VAR_RE.exec(lines[i])) !== null) {
      adminVar++;
      addV(file, i+1, "no-admin-var", `新定义 ${m[0].trim()}`);
    }
  }
}
counts["A10·--admin-*"] = adminVar;

// Stylelint（容错）
let stylelintStatus = "skipped";
try {
  execSync("npx stylelint --version", { cwd: ROOT, stdio: "pipe" });
  try {
    execSync('npx stylelint "portal/assets/**/*.css" --ignore-pattern "**/vendor/**" --ignore-pattern "**/tokens.css"', {
      cwd: ROOT, stdio: "pipe", timeout: 30000
    });
    stylelintStatus = "pass";
  } catch (e) {
    stylelintStatus = "fail";
    addV("stylelint", 0, "stylelint", (e.stdout?.toString()||"").slice(0,500));
  }
} catch {
  stylelintStatus = "not-installed";
  console.log("⚠ stylelint 未安装；跳过");
}
counts["stylelint"] = stylelintStatus;

// ─── 汇总 ────────────────────────────────────────────────────
const totalV = Object.entries(counts)
  .filter(([k]) => !k.startsWith("A1b") && k !== "stylelint")
  .reduce((s,[,v]) => s + (typeof v === "number" ? v : 0), 0);

console.log("\n═══ Design Gate Summary ═══");
for (const [rule, count] of Object.entries(counts)) {
  const icon = typeof count === "number" ? (count === 0 ? "✓" : "✗") : (count === "pass" ? "✓" : "⚠");
  console.log(`  ${icon} ${rule}: ${count}`);
}
console.log(`\n总违例: ${totalV}`);

// ─── 基线棘轮逻辑 ────────────────────────────────────────────

/** Load existing baseline or return empty set */
function loadBaseline() {
  if (!existsSync(BASELINE_FILE)) return { fingerprints: [], version: "1.0", generatedAt: null };
  try {
    return JSON.parse(readFileSync(BASELINE_FILE, "utf8"));
  } catch {
    return { fingerprints: [], version: "1.0", generatedAt: null };
  }
}

/** Save current violations as baseline */
function saveBaseline() {
  const fps = violations.map(violationFingerprint);
  const data = {
    version: "1.2",
    generatedAt: new Date().toISOString(),
    totalViolations: totalV,
    summary: Object.fromEntries(Object.entries(counts).filter(([k]) => !k.startsWith("A1b") && k !== "stylelint")),
    fingerprints: [...new Set(fps)],
    violationCount: fps.length
  };
  writeFileSync(BASELINE_FILE, JSON.stringify(data, null, 2) + "\n");
  console.log(`\n✓ 基线已更新：${data.fingerprints.length} 条指纹 → ${BASELINE_FILE}`);
}

if (UPDATE_BASELINE) {
  saveBaseline();
  process.exit(0);
}

if (BASELINE_MODE) {
  console.log("\n═══ 存量违例明细 ═══");
  const byRule = {};
  for (const v of violations) {
    if (!byRule[v.rule]) byRule[v.rule] = [];
    byRule[v.rule].push(v);
  }
  for (const [rule, items] of Object.entries(byRule)) {
    console.log(`\n--- [${rule}] ${items.length} 处 ---`);
    const byFile = {};
    for (const it of items) {
      if (!byFile[it.file]) byFile[it.file] = [];
      byFile[it.file].push(it);
    }
    for (const [file, fitems] of Object.entries(byFile)) {
      console.log(`  ${file}: ${fitems.length} 处`);
      for (const fi of fitems.slice(0, 5)) {
        console.log(`    L${fi.line}: ${fi.message}`);
      }
      if (fitems.length > 5) console.log(`    ... +${fitems.length - 5} more`);
    }
  }
  console.log("\n✓ 基线模式完成（exit 0）");
  process.exit(0);
}

// ─── 棘轮 / 严格 判定 ────────────────────────────────────────
if (STRICT_MODE) {
  // 严格模式：全量阻断（同 v1.1 行为）
  if (totalV > 0 || stylelintStatus === "fail") {
    console.log("\n✗ FAILED (--strict)");
    for (const v of violations.slice(0, 50)) {
      console.log(`  ${v.file}:${v.line} [${v.rule}] ${v.message}`);
    }
    if (violations.length > 50) console.log(`  ...+${violations.length-50} more`);
    process.exit(1);
  }
  console.log("\n✓ PASSED (--strict)");
  process.exit(0);
}

// 棘轮模式（默认）：只拦新增违例
const baseline = loadBaseline();
const baselineFPs = new Set(baseline.fingerprints || []);

const newViolations = violations.filter(v => !baselineFPs.has(violationFingerprint(v)));
const baselinedCount = violations.length - newViolations.length;

if (newViolations.length > 0 || stylelintStatus === "fail") {
  console.log(`\n✗ FAILED (棘轮模式：${newViolations.length} 新增违例，${baselinedCount} 存量放行)`);
  for (const v of newViolations.slice(0, 50)) {
    console.log(`  🆕 ${v.file}:${v.line} [${v.rule}] ${v.message}`);
  }
  if (newViolations.length > 50) console.log(`  ...+${newViolations.length-50} more`);
  if (stylelintStatus === "fail") console.log("  🆕 stylelint: fail");
  console.log(`\n💡 存量 ${baselinedCount} 条违例已在基线中，不阻断。跑 --update-baseline 可刷新基线。`);
  process.exit(1);
}

console.log(`\n✓ PASSED (棘轮模式：${baselinedCount} 存量放行，0 新增)`);
process.exit(0);
