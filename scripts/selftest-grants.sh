#!/usr/bin/env bash
# 授权管理页（1b-8 一期）自检：headless Chrome 实跑 admin/selftest-grants.html 断言页。
#
# 口径（设计稿 §7-5）：退出码 0＝断言全绿（data-selftest=green 且已置 done）；
# 非 0＝未全绿或环境不可用（Chrome 缺失／页面加载失败，如实报红不假过）。
# 断言页不经导航暴露（沿 selftest 惯例）；本脚本是它的一键复跑通道。
# 依赖：本机 Google Chrome（headless）＋ node ≥18。静态伺服与 Chrome 用毕即杀（零残留）；
#       /v1/* 无挡板恒 404，页面走「后端不可达→降级演示数据」既有路径（演示态断言被天然覆盖）。
#
# 用法：bash scripts/selftest-grants.sh
#   可选环境变量：GRANTS_SELFTEST_PORT（静态口，默认 8877）、GRANTS_SELFTEST_CDP（调试口，默认 9227）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec node "$ROOT/scripts/selftest_page_cdp.mjs" /admin/selftest-grants.html \
  --port "${GRANTS_SELFTEST_PORT:-8877}" --cdp "${GRANTS_SELFTEST_CDP:-9227}"
