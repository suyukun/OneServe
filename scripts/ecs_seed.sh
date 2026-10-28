#!/usr/bin/env bash
# OneServe ECS 演示数据灌数脚本（v3 · 随离线包内置）。
#
# 目的：ECS 无外网、无仓库副本、无构建上下文，rehearse.sh 彩排链跑不了——本脚本把
# 「彩排链的数据种子」拆出来，全部经 docker compose exec 在 compose 网内执行，起栈后
# 一键灌数，使门户首页 KPI／目录页／试用台／运行视图全部有真数据可显。
#
# 与彩排链的对应（数据口径完全同源，seed=42 确定性复现）：
#   ① 用户标签 KV：容器内 gen_mock_source.py（gmssl 镜像内置）→ loader → reconcile
#      → switch（与 scripts/load_demo_data.sh 同链路，仅造数位置从 host 挪进容器）；
#   ② 资讯标签 KV：容器内 gen_news_data.py（与 scripts/load_news_data.sh 同链路）；
#   ③ 公告种子：list portal:announcements（门户 /v1/portal/announcements 读取）。
#      调用量类 KPI（累计/今日/成功率）不灌种子——由 service/app/stats.py 挂审计链路
#      对每次真实 API 调用实时计数（portal:stats:live，诚信线 2026-09-22：数字必须真）。
#
# 前置：restore → compose up（六容器 healthy）→ init_routes 已跑（顺序固定，见 manifest）。
# 用法：bash scripts/ecs_seed.sh            # 在解包目录或仓库根均可（自动定位 compose 目录）
# 幂等：可重复执行（目标版本空间定向清理后重灌，不用 FLUSHDB，不影响其余键）。
# 依赖：bash + docker compose + curl（smoke 用；无 curl 仅跳过末尾自检，不阻断灌数）。
set -euo pipefail
export PATH="$PATH:$HOME/.docker/bin"

# ---------- 定位 compose 目录（包内布局 <包>/scripts+<包>/compose；仓库布局 scripts+根） ----------
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
if [ -f "$SCRIPT_DIR/../compose/docker-compose.yml" ]; then
  COMPOSE_DIR=$(cd "$SCRIPT_DIR/../compose" && pwd)
elif [ -f "$SCRIPT_DIR/../docker-compose.yml" ]; then
  COMPOSE_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
else
  echo "错误：未找到 docker-compose.yml（脚本应位于包内 scripts/ 或仓库 scripts/）" >&2
  exit 1
fi
cd "$COMPOSE_DIR"
COMPOSE="docker compose -f docker-compose.yml"
ENV_FILE=""
if [ -f ../.env ]; then                       # 包内布局：.env 在包根（restore 生成）
  COMPOSE="docker compose -f docker-compose.yml --env-file ../.env"
  ENV_FILE="$(cd .. && pwd)/.env"
elif [ -f ./.env ]; then                      # 仓库布局：.env 在仓库根
  ENV_FILE="$(pwd)/.env"
fi

# ---------- 种子参数（与彩排链同参，确定性：同 seed 复跑同数据） ----------
DATE=20260920          # 用户标签数据日期＝版本号＝data_as_of（演示固定值）
USERS=2000             # 演示规模
FILL=0.30              # 填充率（演示偏高档）
SEED=42
NEWS_DATE=20260921     # 资讯标签版本
NEWS_COUNT=1000
REDIS_IN_NET="redis://oneserve-redis:6379/0"

log() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

# ---------- 0. 前置：service/redis 容器在跑（healthy 由引导单第 5 步保证，这里快速核） ----------
$COMPOSE exec -T redis redis-cli PING >/dev/null 2>&1 || die "redis 容器不可用（先起栈：见 manifest「目标机恢复」）"
$COMPOSE exec -T service python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" >/dev/null 2>&1 \
  || die "service 容器不健康（docker compose ps 核对六容器 healthy 后重试）"

# ---------- 1. 用户标签：定向清残 → 容器内造数 → 灌库 → 对账 → 原子切 ----------
log "① 用户标签：清目标版本空间（幂等重灌口径，不 FLUSHDB）"
$COMPOSE exec -T redis sh -c "redis-cli --scan --pattern 'tags:user:${DATE}:*' | xargs -r redis-cli DEL" >/dev/null
$COMPOSE exec -T redis redis-cli DEL "tags:user:latest" "sync:ver:${DATE}" "sync:recon:${DATE}" >/dev/null

log "② 造数（service 容器内，gmssl 真算 SM3；${USERS} 户 × 296 标签列，seed=${SEED}）"
$COMPOSE exec -T service python /sync/gen_mock_source.py \
    --date "$DATE" --users "$USERS" --fill "$FILL" --seed "$SEED" --out /tmp/seed_user.csv

log "③ 加载 → 双缓冲新空间 tags:user:${DATE}（容器内 loader，存储格式单一事实来源）"
$COMPOSE exec -T -e SYNC_REDIS_URL="$REDIS_IN_NET" service \
    python /sync/loader.py --csv /tmp/seed_user.csv --version "$DATE"

log "④ 对账三查（行数/抽样逐值/覆盖率）"
$COMPOSE exec -T -e SYNC_REDIS_URL="$REDIS_IN_NET" service \
    python /sync/reconcile.py --csv /tmp/seed_user.csv --version "$DATE"

log "⑤ 原子切 meta 指针 tags:user:latest → ${DATE}（三闸放行）"
$COMPOSE exec -T -e SYNC_REDIS_URL="$REDIS_IN_NET" service \
    python /sync/switch.py --version "$DATE"

# ---------- 2. 资讯标签：清残 → 容器内直灌＋切指针 ----------
log "⑥ 资讯标签：清残 + 造数上线（${NEWS_COUNT} 条，版本 ${NEWS_DATE}）"
$COMPOSE exec -T redis sh -c "redis-cli --scan --pattern 'tags:news:${NEWS_DATE}:*' | xargs -r redis-cli DEL" >/dev/null
$COMPOSE exec -T redis redis-cli DEL "tags:news:latest" "sync:ver:news:${NEWS_DATE}" >/dev/null
$COMPOSE exec -T -e SYNC_REDIS_URL="$REDIS_IN_NET" service \
    python /sync/gen_news_data.py --count "$NEWS_COUNT" --version "$NEWS_DATE" --seed "$SEED"

# ---------- 3. 公告种子（门户 /v1/portal/announcements 读取；调用量 KPI 不灌数＝实时真计数） ----------
log "⑦ 公告种子：list portal:announcements（2 条）"
$COMPOSE exec -T redis redis-cli DEL portal:announcements >/dev/null
$COMPOSE exec -T redis redis-cli RPUSH portal:announcements \
    '{"tag":"新增","title":"风险域 5 个字段通过 OA 白名单审批上线，目录已更新","time":"2026-09-18T10:00:00+08:00"}' \
    '{"tag":"维护","title":"9 月 22 日 02:00-04:00 同步窗口例行维护，期间点查不受影响","time":"2026-09-15T09:00:00+08:00"}' >/dev/null

# ---------- 4. 演示手机号（保证试用台一输即中：投影字段 ≥4 的种子用户） ----------
log "⑧ 挑演示手机号（demo_bank 白名单投影字段 ≥4，试用台「网页查询」输入即出标签）"
PHONES_JSON=$($COMPOSE exec -T -e SYNC_REDIS_URL="$REDIS_IN_NET" service python - <<'PYEOF'
import json, sys, yaml
sys.path.insert(0, "/sync")
from gen_mock_source import fake_phone, sm3_hex           # 同 seed 同参数＝与造数完全同源
from common import data_key, get_live, redis_conn

matrix = yaml.safe_load(open("/app/auth_matrix.yaml", encoding="utf-8"))["matrix"]
wl = set()
for row in matrix:
    if row.get("tenant") == "demo_bank":
        for g in row.get("grants", []):
            if g.get("service") == "user_tag_query":
                wl = set(g["fields"])
r = redis_conn("redis://oneserve-redis:6379/0")
live = get_live(r)
out = []
for threshold in (4, 1):                                  # 先挑字段厚的，兜底放宽
    for i in range(2000):
        ph = fake_phone(42, i)
        tags = json.loads(r.get(data_key(live, sm3_hex(ph))) or "{}")
        hits = len(wl & set(tags))
        if hits >= threshold and ph not in {p["phone"] for p in out}:
            out.append({"phone": ph, "fields": hits, "key": sm3_hex(ph)})
        if len(out) >= 3:
            break
    if len(out) >= 3:
        break
print(json.dumps(out, ensure_ascii=False))
PYEOF
)
echo "$PHONES_JSON" | grep -q '"phone"' || die "演示手机号挑选失败（种子数据异常）"
P1=$(echo "$PHONES_JSON" | grep -o '"phone": "[0-9]*"' | head -1 | grep -o '[0-9]*')
KEY0=$($COMPOSE exec -T service python - <<'PYEOF'
import sys
sys.path.insert(0, "/sync")
from gen_mock_source import fake_phone, sm3_hex
print(sm3_hex(fake_phone(42, 0)))
PYEOF
)
echo "候选（手机号/可显字段数/SM3键）：$PHONES_JSON"

# ---------- 5. 端到端自检（无 curl 只跳过，不阻断） ----------
if command -v curl >/dev/null 2>&1; then
  log "⑨ 自检 a：门户 KPI 接口 GET /v1/portal/overview（首页/运行视图同源数据面）"
  OV=$(curl -s http://localhost:8000/v1/portal/overview)
  echo "  $OV" | head -c 300; echo
  echo "$OV" | grep -Eq '"coveredUsers": *[1-9]' || die "overview.coveredUsers 为 0（门户端点或种子异常）"

  log "⑨ 自检 b：试用台演示通道 POST /demo/query（门户同源 → nginx → 网关 → service）"
  DR=$(curl -s -X POST http://localhost:8000/demo/query \
       -H "Content-Type: application/json" -d "{\"key\": \"${KEY0}\"}")
  echo "  ${DR:0:300}"
  echo "$DR" | grep -q '"found": *true' || die "演示通道未命中（数据或链路异常）"

  if [ -n "$ENV_FILE" ] && grep -q '^APISIX_APPKEY=' "$ENV_FILE"; then
    log "⑨ 自检 c：网关正式通道 POST /v1/tags/query（带 key，键＝首行种子用户）"
    set -a; . "$ENV_FILE"; set +a
    TR=$(curl -s -X POST http://localhost:9080/v1/tags/query \
         -H "apikey: $APISIX_APPKEY" -H "Content-Type: application/json" \
         -d "{\"key\": \"${KEY0}\"}")
    echo "  ${TR:0:300}"
    echo "$TR" | grep -q '"found": *true' || die "正式通道未命中（路由/授权/数据异常）"
  fi
else
  log "⑨ 本机无 curl：跳过端到端自检（灌数已完成；可按 manifest 手工验证）"
fi

# ---------- 6. 演示就绪横幅 ----------
echo
echo "=============================================================="
echo "演示数据已上线（ecs_seed v3 · seed=${SEED} 确定性可复灌）"
echo "  用户标签：${USERS} 户（版本 ${DATE}，对账三查绿＋指针已切）"
echo "  资讯标签：${NEWS_COUNT} 条（版本 ${NEWS_DATE}）"
echo "  调用量 KPI：真实计数（审计链路实时累计，无调用显示 0——不灌演示数字）"
echo "门户：        http://localhost:8000（目录/试用台/新鲜度＝种子真值；KPI＝实时真计数）"
echo "演示手机号：  ${P1:-见上方候选} 等 3 个候选（试用台「网页查询」输入即出标签）"
echo "收台/清场：    docker compose -f docker-compose.yml down -v（重灌＝重跑本脚本）"
echo "=============================================================="
