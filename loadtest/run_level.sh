#!/usr/bin/env bash
# 单档执行器：一个并发档＝审计基线 → stats 采样起 → locust（起压＋全并发窗口）
# → 采样停 → 审计终值 → 追加对账行 → 打一行结果摘要。
# 供 run_ladder.sh 循环调用（目标/凭证/结果目录全由环境注入），也可单档前台执行：
#   LOADTEST_RUN_DIR=<dir> LOCUST_ROUTE=... LOCUST_APIKEY=... LOCUST_KEYS_FILE=... \
#     bash loadtest/run_level.sh 300
set -euo pipefail
cd "$(dirname "$0")/.."

: "${LOADTEST_RUN_DIR:?缺 LOADTEST_RUN_DIR（结果目录，由 run_ladder.sh 注入）}"
: "${LOCUST_ROUTE:?缺 LOCUST_ROUTE}"
: "${LOCUST_APIKEY:?缺 LOCUST_APIKEY}"
: "${LOCUST_KEYS_FILE:?缺 LOCUST_KEYS_FILE}"
KEY_FIELD="${LOCUST_KEY_FIELD:-key}"
METHOD="${LOCUST_METHOD:-POST}"
HOST="${LOADTEST_HOST:-http://localhost:9080}"
SECS="${LOADTEST_SECS:-70}"
RAMP="${LOADTEST_RAMP:-10}"
SVC_C="${LOADTEST_SERVICE_CONTAINER:-oneserve-service}"
REDIS_C="${LOADTEST_REDIS_CONTAINER:-oneserve-redis}"
APISIX_C="${LOADTEST_APIXISX_CONTAINER:-oneserve-apisix}"
U="${1:?用法: run_level.sh <并发数>}"
RESULTS="$LOADTEST_RUN_DIR"
mkdir -p "$RESULTS"
ulimit -n 10240 2>/dev/null || true

# A窗 2026-10-19（压测页容器内触发使能）：容器内＝审计卷本地直读（免 docker）；
# 宿主执行保持原 docker exec 口径不变。
audit_count() {
  if [ -r /data/audit/audit.jsonl ]; then
    wc -l < /data/audit/audit.jsonl | tr -d '[:space:]'
  else
    docker exec "$SVC_C" sh -c 'wc -l < /data/audit/audit.jsonl' | tr -d '[:space:]'
  fi
}

[ -f "$RESULTS/audit_recon_raw.txt" ] || echo "level,audit_baseline,audit_after,delta" > "$RESULTS/audit_recon_raw.txt"
BASE=$(audit_count)
echo "== 档位 ${U}：起压 ${RAMP}s ＋ 全并发 $((SECS-RAMP))s；audit 基线 $BASE =="

# A窗 2026-10-19：容器内无 docker CLI/sock——写表头后跳过资源采样（summarize 对
# 缺行容错，报告如实少此节）；宿主执行保持原采样不变。
SAMP=""
if command -v docker >/dev/null 2>&1; then
  ( echo "ts,container,cpu_pct,mem_used,mem_limit,block_io"
    while :; do
      ts=$(date +%s)
      docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.BlockIO}}' \
        "$APISIX_C" "$SVC_C" "$REDIS_C" | sed "s/^/${ts},/"
      sleep 2
    done ) > "$RESULTS/stats_${U}.csv" &
  SAMP=$!
else
  echo "ts,container,cpu_pct,mem_used,mem_limit,block_io" > "$RESULTS/stats_${U}.csv"
  echo "(容器内触发形态：docker CLI 不在场，跳过容器资源采样)" >&2
fi

LOCUST_KEY_FIELD="$KEY_FIELD" LOCUST_METHOD="$METHOD" python3 -m locust -f loadtest/locustfile.py --headless \
    -u "$U" -r $(( U / RAMP )) -t "${SECS}s" --host "$HOST" \
    --csv "$RESULTS/locust_${U}" --only-summary --exit-code-on-error 0 \
    2>&1 | tee "$RESULTS/locust_${U}_summary.log"

[ -n "$SAMP" ] && { kill "$SAMP" 2>/dev/null || true; wait "$SAMP" 2>/dev/null || true; } || true
AFTER=$(audit_count)
echo "$U,$BASE,$AFTER,$((AFTER-BASE))" >> "$RESULTS/audit_recon_raw.txt"
echo "== 档位 ${U} 完成：audit ${BASE} → ${AFTER}（delta $((AFTER-BASE))）=="
