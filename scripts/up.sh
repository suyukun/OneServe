#!/usr/bin/env bash
# 一键起全套（task-002）：.env 就位 → compose up → 等健康 → 初始化 APISIX 路由。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"
cd "$(dirname "$0")/.."

# .env 不存在则从模板生成（首次一键起的「一键」体验）
[ -f .env ] || { cp .env.example .env; echo "已从 .env.example 生成 .env"; }
set -a; . ./.env; set +a

# TLS 预留（蓝图 1a）：nginx conf 443 块引用 /etc/nginx/certs，无证书＝portal 起不来；
# 幂等自签证书兜底（已存在且未过期则跳过；certs/ 不入 git）
if [ -x scripts/gen-dev-cert.sh ]; then scripts/gen-dev-cert.sh; fi

echo "== docker compose --profile full up -d --build =="
# 一键开箱批（2026-10-28）：compose 默认档＝demo（3 服务：redis/service/portal，自动灌数）；
# 本脚本＝full 档入口，显式 --profile full 起 6 服务全栈（etcd/apisix/batch-worker 随档加入）。
docker compose --profile full up -d --build

# 等四组件健康（healthcheck 定义在 docker-compose.yml）
echo "== 等待健康检查全绿 =="
for svc in etcd redis apisix service; do
  for i in $(seq 1 60); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "oneserve-$svc" 2>/dev/null || echo starting)
    [ "$st" = "healthy" ] && break
    sleep 2
  done
  printf '%-10s %s
' "$svc" "$st"
  [ "$st" = "healthy" ] || { echo "oneserve-$svc 未在时限内转健康" >&2; exit 1; }
done

# APISIX 路由/consumer/key-auth 初始化（Admin API 幂等 PUT；借 service 容器执行，9180 仅内网）
echo "== 初始化 APISIX 路由 =="
docker compose exec -T service python /init/init_routes.py

# 网关层只读自检（1.10-6 归链路 · 晚间批卡4 2026-10-21）：路由初始化后立即实证——
# 五项只读 curl（key-auth 401/200、正式通道、代理通道、演示通道），见 scripts/gateway_selftest.sh；
# 失败＝栈不可用，与上方健康检查同语义退出非零；特殊场景 GW_SELFTEST=0 bash scripts/up.sh 可跳过
if [ "${GW_SELFTEST:-1}" = "1" ]; then
  echo "== 网关层自检（1.10-6 只读序列） =="
  bash scripts/gateway_selftest.sh || { echo "网关层自检未全绿（见上行 ✗ 项；跳过用 GW_SELFTEST=0）" >&2; exit 1; }
fi

echo "== 全部就绪 =="
docker compose ps
echo
echo "端到端验证：curl -H 'apikey: $APISIX_APPKEY' http://localhost:9080/v1/ping"
echo "无 key 应 401：curl -i http://localhost:9080/v1/ping"
