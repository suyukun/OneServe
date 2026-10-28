#!/usr/bin/env bash
# ============================================================
# dom_probe.sh —— 视觉问题 DOM 探针（量化诊断 · 晚间批卡4 · 2026-10-21）
#
#   用途＝AGENTS.md「同一页面视觉问题 2 轮截图未愈，必须上 DOM 测量探针量化诊断，
#   禁第 3 轮凭截图猜」的落地工具：输入 URL＋CSS 选择器清单，headless Chrome
#   渲染后逐选择器报告 命中/未命中＋外框几何(x,y,w,h)＋display/可见性＋
#   outerHTML 片段（截断）——查结构不猜图。
#   渲染层＝scripts/cdp-helpers.mjs（缓存禁用等已知坑规避同仓库口径，不另起炉灶）；
#   渲染后 DOM（含 JS 动态构建节点），非静态源码比对。
#
# 用法：
#   bash scripts/dom_probe.sh <URL> '<selector>' [<selector>...]
#   bash scripts/dom_probe.sh --file <selectors.txt> <URL>    # 每行一个选择器，// 整行注释
#   bash scripts/dom_probe.sh --help
# 环境变量：PROBE_SNIPPET（外层 HTML 片段截断长度，默认 240 字符）
#           PROBE_SETTLE_MS（导航后等 JS 渲染毫秒数，默认 1200）
# 输出：逐选择器 HIT/MISS＋几何＋片段；末尾汇总
# 退出码：0=全命中  1=有未命中  2=工具错误（页面不可达/Chrome 不可用）
# 只读承诺：仅 GET 页面并读取 DOM，无点击/提交/存储写入。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

URL=""; FILE=""; SELES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --file) FILE="${2:?--file 需要文件参数}"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) if [ -z "$URL" ]; then URL="$1"; else SELES+=("$1"); fi; shift ;;
  esac
done
if [ -n "$FILE" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    # 注释＝整行以 // 开头（不用 #：与 CSS id 选择器 #id 撞车）；行内去首尾空白
    line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    case "$line" in '' | '//'*) continue ;; esac
    SELES+=("$line")
  done < "$FILE"
fi
if [ -z "$URL" ] || [ "${#SELES[@]}" -eq 0 ]; then
  echo "用法: bash scripts/dom_probe.sh <URL> '<selector>' [...] | --file <selectors.txt> <URL>" >&2
  exit 2
fi

# 页面可达性预检（fail fast：连接错误给可读信息，不等 Chrome 超时）
CODE=$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || true)
if [ "$CODE" = "000" ]; then echo "✗ 工具错误: 页面不可达 ${URL}（连接失败）" >&2; exit 2; fi

set +e
node --input-type=module - "$URL" "${SELES[@]}" <<'NODEEOF'
import { startChrome, sleep } from './cdp-helpers.mjs';
const args = process.argv.slice(2);
const url = args[0];
const sels = args.slice(1);
const snippet = parseInt(process.env.PROBE_SNIPPET || '240', 10);
const settle = parseInt(process.env.PROBE_SETTLE_MS || '1200', 10);
let b = null;
try {
  const port = 20000 + Math.floor(Math.random() * 20000);
  b = await startChrome(port, 'dom-probe', url, { navMs: 2200 });
  await sleep(settle);
  const expr = '(() => { const out = []; for (const s of ' + JSON.stringify(sels) + ') {'
    + ' const el = document.querySelector(s);'
    + ' if (!el) { out.push({ sel: s, hit: false }); continue; }'
    + ' const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);'
    + ' let cls = el.className; if (typeof cls !== "string") cls = "";'
    + ' out.push({ sel: s, hit: true, tag: el.tagName.toLowerCase(), id: el.id || "", cls: cls,'
    + ' x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),'
    + ' display: cs.display, visible: (r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"),'
    + ' html: el.outerHTML.replace(/[\\t\\n\\r ]+/g, " ").slice(0, ' + snippet + ') }); }'
    + ' return out; })()';
  const rows = await b.evaluate(expr);
  let hit = 0;
  for (const row of rows) {
    if (row.hit) {
      hit++;
      const shape = '<' + row.tag + (row.id ? '#' + row.id : '') +
        (row.cls ? '.' + String(row.cls).trim().split(/\\s+/).join('.') : '') + '>';
      console.log('HIT  ' + row.sel);
      console.log('     ' + shape + '  几何 x=' + row.x + ' y=' + row.y +
        ' w=' + row.w + ' h=' + row.h + '  display=' + row.display + '  visible=' + row.visible);
      console.log('     ' + row.html);
    } else {
      console.log('MISS ' + row.sel + '  （渲染后 DOM 无命中——结构缺失或选择器错）');
    }
  }
  console.log('== dom_probe 汇总: ' + hit + '/' + rows.length + ' 命中  URL=' + url);
  process.exitCode = (hit === rows.length) ? 0 : 1;
} catch (e) {
  console.error('✗ 工具错误: ' + ((e && e.message) || e));
  process.exitCode = 2;
} finally {
  if (b) { try { await b.close(); } catch (e2) { /* 已尽力关闭 */ } }
}
NODEEOF
RC=$?
set -e
rm -rf /tmp/dom-probe* 2>/dev/null || true
exit $RC
