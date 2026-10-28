#!/usr/bin/env bash
# 下线全套（保留卷：审计/etcd/redis 数据留证可复查；清卷重建用 reset.sh）。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"
cd "$(dirname "$0")/.."

docker compose down
echo "== 已下线（卷保留）=="
docker volume ls --filter name=oneserve- --format '{{.Name}}'
