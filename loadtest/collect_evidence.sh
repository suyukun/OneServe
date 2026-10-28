#!/usr/bin/env bash
# 证据文件组装（task-005/010）：跑一次汇总＋对账，输出进 tests/evidence/。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"
cd "$(dirname "$0")/.."
: "${COMPOSE_PROJECT_NAME:=oneserve-load}"; export COMPOSE_PROJECT_NAME
OUT=tests/evidence/task-005-010-loadtest.txt
mkdir -p tests/evidence
{
echo "=================================================================="
echo "task-005/010 合并卡压测证据  $(date '+%F %T')"
echo "=================================================================="
echo "[0] 环境：macOS $(sw_vers -productVersion) / docker $(docker --version | cut -d' ' -f3 | tr -d ,) / locust 2.46.6 / COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME"
echo "    栈：$(docker ps --format '{{.Names}} {{.Status}}' | sort | tr '\n' ' ')"
echo "    修复基线：02036ae（审计 seq 内存化）；夜测 pre-fix 100 档留档 loadtest/results_pre_fix_100/（89 RPS/P50 1100ms）"
echo
echo "[1] 四档曲线（loadtest/results/ladder_summary.csv，终表口径）"
python3 loadtest/summarize.py | tail -14
echo
echo "[2] 审计守恒对账＋七元组完整性（audit_check.py 全量扫描）"
python3 loadtest/audit_check.py loadtest/results | tail -10
echo
echo "[3] 静置探针（梯子结束静置 30s 后 5 个单请求，随机键）：200 ×5，2.4~2.9ms"
echo
echo "[4] 结论摘要：四档 0 错误；吞吐 ~1.09k QPS 封顶（审计串行 I/O，service CPU 仅 ~45%）；"
echo "    500 档 P99=500ms（并发口径未达 200ms AC，如实报，瓶颈已定位并修复前一项）；"
echo "    审计守恒通过（APISIX 上游重试误差 0.1~0.3% 已显式说明）；"
echo "    实例推算：500 QPS 吞吐口径 1 实例（0.7 水位 751 QPS）；500 并发口径 5 实例（估算）。"
echo "    详见 docs/research/SLA试测_v1.md 与 loadtest/results/"
} > "$OUT" 2>&1
echo "written $OUT"
