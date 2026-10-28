#!/usr/bin/env bash
# ============================================================
# OneServe 开源 quickstart · 第一个查询端到端自测
#
# 做的事：起最小栈（redis + service，隔离端口）→ 跑 scripts/seed_demo.sh
#         （走生产同款自动探测：宿主 redis-py 直连隔离端口优先，无 redis 包时
#           回落容器内 redis-cli）→ 一条 curl 验证 HTTP 200/业务 JSON → 拆栈。
#
# 用法：
#   bash scripts/seed_demo_selftest.sh
#   可选环境变量：
#     SEED_DEMO_REDIS_PORT=16379      宿主机 Redis 端口（隔离，别占 6379）
#     SEED_DEMO_SERVICE_PORT=18000    宿主机 service 端口
#     SEED_DEMO_KEEP=1                结束后保留容器，便于看日志
#     SEED_DEMO_SERVICE_IMAGE=oneserve/service:m0
#     SEED_DEMO_REDIS_IMAGE=valkey/valkey:8-alpine
#
# 前置：docker、curl、jq。
# 说明：service 容器用现成镜像提供运行时依赖，当前工作树的 service/ 只读挂载到
#       /app，确保被验证的是本仓库当前代码与注册表。
# 安全：容器/网络名固定带 oneserve-seed-demo- 前缀；退出时只拆除本脚本创建的资源。
# ============================================================
set -euo pipefail

SECONDS=0
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"

REDIS_NAME="oneserve-seed-demo-redis"
SERVICE_NAME="oneserve-seed-demo-service"
NET_NAME="oneserve-seed-demo-net"
REDIS_PORT="${SEED_DEMO_REDIS_PORT:-16379}"
SERVICE_PORT="${SEED_DEMO_SERVICE_PORT:-18000}"
REDIS_IMAGE="${SEED_DEMO_REDIS_IMAGE:-valkey/valkey:8-alpine}"
SERVICE_IMAGE="${SEED_DEMO_SERVICE_IMAGE:-oneserve/service:m0}"
KEEP="${SEED_DEMO_KEEP:-0}"

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/oneserve-seed-demo-selftest.XXXXXX")

log() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

cleanup() {
  if [ "$KEEP" = "1" ]; then
    echo "== SEED_DEMO_KEEP=1：保留容器（自行 docker rm -f ${REDIS_NAME} ${SERVICE_NAME}）"
    return 0
  fi
  docker rm -f "$SERVICE_NAME" "$REDIS_NAME" >/dev/null 2>&1 || true
  docker network rm "$NET_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

for bin in docker curl jq; do
  command -v "$bin" >/dev/null 2>&1 || die "缺少命令：$bin"
done

docker image inspect "$SERVICE_IMAGE" >/dev/null 2>&1 || \
  die "本地无镜像 ${SERVICE_IMAGE}：先 bash scripts/up.sh 构建，或设 SEED_DEMO_SERVICE_IMAGE"

if command -v lsof >/dev/null 2>&1; then
  for port in "$REDIS_PORT" "$SERVICE_PORT"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
      die "端口 ${port} 已被占用（用 SEED_DEMO_REDIS_PORT / SEED_DEMO_SERVICE_PORT 改）"
    fi
  done
fi

# ---------- 0. 清掉上次残留（同名容器/网络） ----------
docker rm -f "$SERVICE_NAME" "$REDIS_NAME" >/dev/null 2>&1 || true
docker network rm "$NET_NAME" >/dev/null 2>&1 || true

# ---------- 1. 最小栈：redis + service ----------
log "创建隔离网络 ${NET_NAME}"
docker network create "$NET_NAME" >/dev/null

log "启动 Redis（localhost:${REDIS_PORT}）"
docker run -d --name "$REDIS_NAME" --network "$NET_NAME" \
  -p "${REDIS_PORT}:6379" "$REDIS_IMAGE" >/dev/null

REDIS_READY=0
for _ in $(seq 1 40); do
  if docker exec "$REDIS_NAME" redis-cli ping 2>/dev/null | grep -q PONG; then
    REDIS_READY=1
    break
  fi
  sleep 0.25
done
[ "$REDIS_READY" = "1" ] || die "Redis 未就绪"

log "启动 service（localhost:${SERVICE_PORT}；当前 service/ 只读挂载）"
docker run -d --name "$SERVICE_NAME" --network "$NET_NAME" \
  -p "${SERVICE_PORT}:8000" \
  -v "$REPO_ROOT/service:/app:ro" \
  -e REDIS_URL="redis://${REDIS_NAME}:6379/0" \
  -e AUDIT_DIR=/tmp/audit \
  -e APISIX_ADMIN=http://localhost:1 \
  -e APISIX_ADMIN_KEY=seed-demo \
  -e REGISTRY_POLL_INTERVAL_S=3600 \
  "$SERVICE_IMAGE" >/dev/null

SERVICE_READY=0
for _ in $(seq 1 60); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${SERVICE_PORT}/healthz" 2>/dev/null || true)
  if [ "$CODE" = "200" ]; then
    SERVICE_READY=1
    break
  fi
  sleep 1
done
if [ "$SERVICE_READY" != "1" ]; then
  docker logs --tail 40 "$SERVICE_NAME" >&2 || true
  die "service 未就绪（healthz 非 200）"
fi

# ---------- 2. 灌数 ----------
log "执行 scripts/seed_demo.sh"
TIME_LOG="$TMP_DIR/seed.time"
SEED_LOG="$TMP_DIR/seed.log"
if { time env REDIS_URL="redis://localhost:${REDIS_PORT}/0" REDIS_CONTAINER="$REDIS_NAME" \
    bash scripts/seed_demo.sh >"$SEED_LOG"; } 2>"$TIME_LOG"; then
  cat "$SEED_LOG"
  grep '^real' "$TIME_LOG" | sed 's/^/   灌数耗时 /'
else
  cat "$SEED_LOG" >&2 || true
  cat "$TIME_LOG" >&2 || true
  die "seed_demo.sh 执行失败"
fi

# ---------- 3. 二次灌入（幂等：键数不翻倍） ----------
EXPECTED="${SEED_DEMO_RECORDS:-150}"
SEED_VERSION="${SEED_DEMO_VERSION:-20260101}"
COUNT_1=$(docker exec "$REDIS_NAME" redis-cli --scan --pattern "tags:usr_basic_info:${SEED_VERSION}:*" | wc -l | tr -d ' ')
log "幂等复跑 seed"
REDIS_URL="redis://localhost:${REDIS_PORT}/0" REDIS_CONTAINER="$REDIS_NAME" \
  bash scripts/seed_demo.sh >/dev/null
COUNT_2=$(docker exec "$REDIS_NAME" redis-cli --scan --pattern "tags:usr_basic_info:${SEED_VERSION}:*" | wc -l | tr -d ' ')
[ "$COUNT_1" = "$EXPECTED" ] || die "首灌键数 ${COUNT_1} != ${EXPECTED}"
[ "$COUNT_2" = "$COUNT_1" ] || die "复跑后键数 ${COUNT_2} != ${COUNT_1}（非幂等）"
log "键数稳定：${COUNT_1} → ${COUNT_2}"

# ---------- 4. 第一个查询 ----------
QUERY_JSON="$TMP_DIR/query.json"
log "第一个查询 POST /v1/query/v1-svc-usr-basic-info"
HTTP_CODE=$(curl -sS -o "$QUERY_JSON" -w '%{http_code}' \
  -X POST "http://localhost:${SERVICE_PORT}/v1/query/v1-svc-usr-basic-info" \
  -H 'apikey: uat-c49d18ab72e03f55' \
  -H 'Content-Type: application/json' \
  -d '{"key":"demo-partner-0001"}')

echo "HTTP ${HTTP_CODE}"
jq . "$QUERY_JSON"

[ "$HTTP_CODE" = "200" ] || die "查询 HTTP ${HTTP_CODE}（期望 200）"
jq -e '.found == true and (.fields | length) == 12' "$QUERY_JSON" >/dev/null \
  || die "响应业务校验失败（found!=true 或字段数!=12）"

# ---------- 5. 结果 ----------
echo
echo "=============================================================="
echo "自测通过：seed → 查询 200 → 出数（12 字段）→ 幂等复跑稳定"
echo "总耗时：${SECONDS}s（seed 单次见上方 real）"
echo "拆栈：trap 退出自动执行（SEED_DEMO_KEEP=1 可保留）"
echo "=============================================================="
