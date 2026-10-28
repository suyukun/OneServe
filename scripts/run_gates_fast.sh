#!/usr/bin/env bash
# OneServe 治理门禁·快速子集（pre-commit 专用）
# 用法：bash scripts/run_gates_fast.sh
# 范围：前端静态检查（stylelint + design_gate strict 模式·T2 清账后全量阻断）
# 全量门禁（含 pytest）请用：bash scripts/run_gates.sh
# 退出码：全部绿 → 0；任一失败 → 1
set -euo pipefail
cd "$(dirname "$0")/.."   # 切到仓库根

echo "== OneServe 治理门禁（fast·pre-commit） =="

GATES_FAILED=0

# 1) Stylelint（CSS lint·增量）
if command -v npx >/dev/null 2>&1; then
  echo "-- Stylelint --"
  if npx stylelint --version >/dev/null 2>&1; then
    npx stylelint "portal/assets/**/*.css" --ignore-pattern "**/vendor/**" --ignore-pattern "**/tokens.css" || GATES_FAILED=1
  else
    echo "⚠ stylelint 未安装；跳过"
  fi
else
  echo "⚠ npx 不可用；跳过 stylelint"
fi

# 2) Design Gate 棘轮模式（存量放行，新增阻断）
if command -v node >/dev/null 2>&1; then
  echo "-- Design Gate（strict 模式·T2 清账后全量阻断） --"
  node scripts/design_gate.mjs --strict || GATES_FAILED=1
else
  echo "⚠ design_gate 未执行：node 不可用"
  GATES_FAILED=1
fi

# 3) git 状态检查（仅提醒）
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ 警告：工作区存在未提交改动（仅提醒，不阻断）"
fi

if [ "$GATES_FAILED" -ne 0 ]; then
  echo "== 快速门禁未过（exit 1）=="
  exit 1
fi
echo "== 快速门禁全绿（exit 0）=="
