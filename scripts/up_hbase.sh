#!/usr/bin/env bash
# ============================================================
# 一键起 HBase（开发替身）并让门户「选表→加载到 Redis」可用
#
# 背景（维护者 2026-09-23 架构口径 / ADR-0005）：
#   HBase＝冷存储（数据源），Redis＝热存储；链路＝
#   外围ETL→HBase→【页面选表→加载】→Redis→封装API→对外。
#
# 补的断点：共享 oneserve 栈默认**不起** HBase 容器，且 service 容器需要
#   HBASE_HOST/HBASE_PORT 才能连（原 compose 未注入，导致门户发布台
#   选表页永远「HBase 不可达」）。本脚本一并解决。
#
# 用法：
#   bash scripts/up_hbase.sh              # 起 HBase + 重建 service（注入变量）
#   bash scripts/up_hbase.sh --down       # 停 HBase 容器（让位内存）
# （--load 演示数据灌载已随 oneserve_demo 演示遗留清理删除，2026-10-08；业务表造数走
#    sync/gen_mock_source.py ＋ sync/hbase_loader.py --table 直传）
#
# 注意：HBase 容器内存受限（VM 仅 7.7GiB），**用完请 --down**。
#       本脚本只用隔离的 hbase 容器，**不碰** Redis 数据与其它容器。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
export PATH="$(dirname "$DOCKER"):$PATH"

if [ "${1:-}" = "--down" ]; then
  echo "== 停 HBase 容器（让位内存）=="
  docker compose -f docker-compose.yml -f compose.hbase.yml stop hbase 2>/dev/null || true
  docker compose -f docker-compose.yml -f compose.hbase.yml rm -f hbase 2>/dev/null || true
  echo "✓ 已停 HBase。门户 HBase 选表页将如实报『HBase 不可达』（设计如此）。"
  exit 0
fi

echo "== ① 起 HBase 开发替身（compose.hbase.yml 叠加）=="
docker compose -f docker-compose.yml -f compose.hbase.yml up -d hbase

echo "== ② 等 HBase Thrift(9090) 就绪 =="
for i in $(seq 1 40); do
  st=$(docker inspect -f '{{.State.Health.Status}}' oneserve-hbase 2>/dev/null || echo starting)
  [ "$st" = "healthy" ] && { echo "  ✓ HBase healthy"; break; }
  sleep 3
done

echo "== ③ 重建 service（注入 HBASE_HOST/HBASE_PORT）=="
docker compose up -d service

echo "== ④ 等 service healthy =="
for i in $(seq 1 30); do
  st=$(docker inspect -f '{{.State.Health.Status}}' oneserve-service 2>/dev/null || echo starting)
  [ "$st" = "healthy" ] && { echo "  ✓ service healthy"; break; }
  sleep 2
done

echo "== ⑤ 自检：门户发布台 HBase 发现端点 =="
code=$(curl -s -o /tmp/_hbase_disc.$$ -w '%{http_code}' \
  -H 'X-Admin-Token: ${ADMIN_API_TOKEN:-admin321}' \
  http://localhost:8000/v1/admin/publish/hbase/discovery || echo 000)
if [ "$code" = "200" ]; then
  echo "  ✓ discovery 200 —— 门户 /admin/publish 现在可以真选表了"
else
  echo "  ✗ discovery 返回 ${code}（未就绪？看上面日志）"
  head -c 200 /tmp/_hbase_disc.$$ 2>/dev/null || true
  echo
fi
rm -f /tmp/_hbase_disc.$$

cat <<'EOF'

────────────────────────────────────────────────
现在可以演示「页面选表 → 加载到 Redis」：
  1. 打开 http://localhost:8000/admin/publish
  2. 输入管理令牌（admin321，页面会提示）
  3. 数据源切「HBase 真表」→ 选表 → 勾字段 → 试查 → 加载到 Redis → 发布
演示完请收台让位内存：
  bash scripts/up_hbase.sh --down
────────────────────────────────────────────────
EOF
