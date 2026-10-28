#!/usr/bin/env bash
# ============================================================
# selftest_page.sh —— 通用页面 DOM 锚点自检（晚间批卡4 · 2026-10-21）
#
#   用途＝输入 URL 清单＋每 URL 的关键 DOM 锚点（id/class/任意 CSS 选择器断言），
#   headless Chrome 渲染后逐项 ✓/✗＋汇总＋退出码。渲染后 DOM（含 JS 动态构建
#   节点），渲染层＝scripts/cdp-helpers.mjs（缓存禁用等已知坑规避同仓库口径）。
#   与 portal/admin/selftest-*.html 页内断言面板分工：页内面板自检应用逻辑，
#   本工具在仓侧批量断言页面结构（可进门禁/巡检）。
#
# 用法：
#   bash scripts/selftest_page.sh <config-file>              # 批量多页
#   bash scripts/selftest_page.sh --url <URL> --anchor id=x [--anchor class=y ...]
#   bash scripts/selftest_page.sh --help
# config 格式（// 整行注释；URL 段后接该页锚点，可多段）：
#   URL http://127.0.0.1:8000/admin/selftest-tenants.html
#   ANCHOR id=create-btn
#   ANCHOR class=caliber
#   ANCHOR sel=.panel button[type=button]
# 锚点写法：id=x → #x；class=y → .y（多 class 空格分隔＝同时具备）；sel=… → 原样 CSS 选择器
# 环境变量：PAGE_SETTLE_MS（每页导航后等 JS 渲染毫秒数，默认 1200）
# 输出：逐锚点 ✓/✗＋每页小计＋总计；退出码 0=全过 1=有未命中 2=工具错误
# 只读承诺：仅 GET 页面并读取 DOM，无点击/提交/存储写入。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

CFG=$(mktemp)
trap 'rm -f "$CFG"' EXIT

# 页面可达性预检（fail fast：连不上即工具错误，不等 Chrome 超时）
check_url() {
  local u="$1" CODE
  CODE=$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "$u" 2>/dev/null || true)
  if [ "$CODE" = "000" ]; then echo "✗ 工具错误: 页面不可达 ${u}（连接失败）" >&2; exit 2; fi
}

URL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:?--url 需要参数}"; check_url "$URL"; printf 'U\t%s\n' "$URL" >> "$CFG"; shift 2 ;;
    --anchor) a="${2:?--anchor 需要参数}"; [ -n "$URL" ] || { echo "✗ --anchor 必须跟在 --url 之后" >&2; exit 2; }; printf 'A\t%s\n' "$a" >> "$CFG"; shift 2 ;;
    -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
    *) if [ -f "$1" ]; then CFG_IN="$1"; else echo "✗ 未知参数或配置文件不存在: $1" >&2; exit 2; fi; shift ;;
  esac
done

# 配置文件模式：解析成 U/A 行（与 CLI 同一通道）
if [ -n "${CFG_IN:-}" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    case "$line" in '' | '//'*) continue ;; esac
    kw="${line%% *}"; rest="${line#* }"
    case "$kw" in
      URL) check_url "$rest"; printf 'U\t%s\n' "$rest" >> "$CFG" ;;
      ANCHOR) printf 'A\t%s\n' "$rest" >> "$CFG" ;;
      *) echo "✗ 配置行无法识别（应为 URL/ANCHOR）: $line" >&2; exit 2 ;;
    esac
  done < "$CFG_IN"
fi

grep -q '^U' "$CFG" || { echo "✗ 未提供任何 URL（config 或 --url）" >&2; exit 2; }
grep -q '^A' "$CFG" || { echo "✗ 未提供任何 ANCHOR（config 或 --anchor）" >&2; exit 2; }

set +e
node --input-type=module - "$CFG" <<'NODEEOF'
import fs from 'node:fs';
import { startChrome, sleep } from './cdp-helpers.mjs';
const cfgPath = process.argv[2];
const settle = parseInt(process.env.PAGE_SETTLE_MS || '1200', 10);
const toSel = (a) => {
  if (a.startsWith('id=')) return '#' + a.slice(3).trim();
  if (a.startsWith('class=')) return '.' + a.slice(6).trim().split(/\s+/).join('.');
  return a.replace(/^sel=/, '');
};
const pages = [];
for (const l of fs.readFileSync(cfgPath, 'utf8').split('\n')) {
  if (!l.trim()) continue;
  const tab = l.indexOf('\t');
  const kind = l.slice(0, tab), val = l.slice(tab + 1);
  if (kind === 'U') pages.push({ url: val, anchors: [] });
  else if (pages.length) pages[pages.length - 1].anchors.push(val);
}
const total = { pass: 0, fail: 0 };
let b = null;
try {
  const port = 20000 + Math.floor(Math.random() * 20000);
  b = await startChrome(port, 'selftest-page', 'about:blank');
  for (let i = 0; i < pages.length; i++) {
    const pg = pages[i];
    console.log('== 页 ' + (i + 1) + '/' + pages.length + ': ' + pg.url + ' ==');
    await b.nav(pg.url, 2200);
    await sleep(settle);
    const sels = pg.anchors.map(toSel);
    const expr = '(() => { const out = []; for (const s of ' + JSON.stringify(sels) + ')' +
      ' out.push({ sel: s, hit: !!document.querySelector(s) }); return out; })()';
    const rows = await b.evaluate(expr);
    let npass = 0;
    for (let j = 0; j < rows.length; j++) {
      const spec = pg.anchors[j];
      if (rows[j].hit) { npass++; total.pass++; console.log('  ✓ ANCHOR ' + spec + ' → ' + rows[j].sel); }
      else { total.fail++; console.log('  ✗ ANCHOR ' + spec + ' → ' + rows[j].sel + '（渲染后 DOM 未命中）'); }
    }
    console.log('  -- 本页 ' + npass + '/' + rows.length);
  }
  console.log('== selftest_page 汇总: PASS=' + total.pass + ' FAIL=' + total.fail + '（页 ' + pages.length + ' 个） ==');
  process.exitCode = total.fail === 0 ? 0 : 1;
} catch (e) {
  console.error('✗ 工具错误: ' + ((e && e.message) || e));
  process.exitCode = 2;
} finally {
  if (b) { try { await b.close(); } catch (e2) { /* 已尽力关闭 */ } }
}
NODEEOF
RC=$?
set -e
rm -rf /tmp/selftest-page* 2>/dev/null || true
exit $RC
