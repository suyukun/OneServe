#!/usr/bin/env bash
# 清卷重建：down -v（删容器/网络/卷，审计等数据一并清空）→ up.sh 全新起。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"
cd "$(dirname "$0")/.."

echo "== 清卷（oneserve-* 容器/网络/卷全删）=="
docker compose down -v --remove-orphans
bash scripts/up.sh
