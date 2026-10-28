#!/usr/bin/env bash
# 下线全套（保留卷：审计/etcd/redis 数据留证可复查；清卷重建用 reset.sh）。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"
cd "$(dirname "$0")/.."

# 一键开箱批（2026-10-28）：带 --profile full 才能连 full 档组件（etcd/apisix/batch-worker）
# 一起下线；demo 档用户裸 `docker compose down` 同样有效（默认档本就不含三件）。
docker compose --profile full down
echo "== 已下线（卷保留）=="
docker volume ls --filter name=oneserve- --format '{{.Name}}'
