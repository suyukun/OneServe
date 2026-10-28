#!/usr/bin/env bash
# task-003 造数→加载→对账→切换：把模拟标签灌进服务栈 Redis（oneserve-003）。
#
# 复用 task-006 同步链路脚本（存储格式单一事实来源，本脚本零新序列化代码）：
#   ① host：gen_mock_source.py 造数（297 列真实结构 × 假值，键＝SM3 真算，gmssl）
#   ② 容器内（/sync 只读挂载 + SYNC_REDIS_URL 指向 oneserve-redis）：
#      loader.py 灌新空间 → reconcile.py 对账三查 → switch.py 原子切 meta 指针
#   ③ read_user.py 冒烟（经 meta 指针读回，data_as_of 正确）
#
# 演示口径：FLUSHDB 全量重灌（演示库专用，可重复执行幂等）。
# 用法：bash scripts/load_demo_data.sh   # 前置：栈已起（COMPOSE_PROJECT_NAME=oneserve-003 bash scripts/up.sh）
set -euo pipefail
export PATH="$HOME/.docker/bin:$PATH"
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-oneserve-003}"   # 默认 task-003 独占栈；外部可覆盖（压测口径 oneserve-load）

PY=python3.13
DATE=20260920          # 数据日期＝版本号＝data_as_of（演示固定值，复跑确定）
USERS=2000             # 演示规模（万级容量口径归 task-004/005）
FILL=0.30              # 填充率取偏高演示档（中性容量档 0.15 归容量实测）
SEED=42
OUT="sync/out/src_task003_${DATE}.csv"

mkdir -p sync/out
echo "== ① 造数（host，gmssl 真算 SM3；明文假号不落盘）=="
$PY sync/gen_mock_source.py --date "$DATE" --users "$USERS" --fill "$FILL" \
    --seed "$SEED" --out "$OUT"

echo "== ② 演示库清残（FLUSHDB；演示专用库，重复执行幂等）=="
docker compose exec -T redis redis-cli FLUSHDB

echo "== ③ 加载 → 双缓冲新空间 tags:user:${DATE}（容器内复用 loader，存储格式以它为准）=="
docker compose exec -T -e SYNC_REDIS_URL=redis://oneserve-redis:6379/0 service \
    python /sync/loader.py --csv "/sync/out/src_task003_${DATE}.csv" --version "$DATE"

echo "== ④ 对账三查（行数/抽样逐值/覆盖率）=="
docker compose exec -T -e SYNC_REDIS_URL=redis://oneserve-redis:6379/0 service \
    python /sync/reconcile.py --csv "/sync/out/src_task003_${DATE}.csv" --version "$DATE"

echo "== ⑤ 原子切 meta 指针 tags:user:latest → ${DATE}（三闸放行后单条 SET）=="
docker compose exec -T -e SYNC_REDIS_URL=redis://oneserve-redis:6379/0 service \
    python /sync/switch.py --version "$DATE"

echo "== ⑥ 读方冒烟（经 meta 指针；首行用户）=="
KEY0=$(sed -n '2p' "$OUT" | cut -d, -f1)
docker compose exec -T -e SYNC_REDIS_URL=redis://oneserve-redis:6379/0 service \
    python /sync/read_user.py "$KEY0"

echo "== 造数完成：演示数据已上线（首行键 ${KEY0:0:16}…）=="
