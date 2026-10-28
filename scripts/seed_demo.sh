#!/usr/bin/env bash
# ============================================================
# OneServe 开源 quickstart · 第一个查询演示数据（轻量 seed）
#
# 场景：合作方（partner-demo 租户）查询用户基本信息 —— A 型 KV 点查。
# 规模：默认 150 条（可用 SEED_DEMO_RECORDS 调整），秒级灌入；数据由本脚本内置生成，
#       不依赖任何外部 CSV/JSON 文件。
#
# 灌数机制（沿用 service/services.yaml 的服务注册与键空间，不另造协议）：
#   v1-svc-usr-basic-info 声明了四类键——
#     alias_key         tags:usr_basic_info:latest            → 当前线上版本
#     data_key          tags:usr_basic_info:{version}:{key}   → 单条 KV（非空标签紧凑 JSON）
#     version_meta_key  sync:ver:usr_basic_info:{version}     → data_as_of 等元信息
#   本脚本只写这几类键：先批量写数据键，再写版本元信息，最后原子切 alias。
#   重复执行先定向清理本演示版本空间，不执行 FLUSHDB，不影响其余键。
#
# 用法：
#   bash scripts/seed_demo.sh
#   默认自动探测（与 service 本体同款连接方式优先，不绑定容器名）：
#     1) redis-py 直连 REDIS_URL（缺省 redis://localhost:6379/0）——优先用仓库
#        .venv 里的 Python，其次 python3；装过 service/requirements.txt 即有 redis 包
#     2) docker 容器内 redis-cli（REDIS_CONTAINER，缺省 oneserve-redis；compose 形态兜底）
#     3) 本机 redis-cli（REDIS_HOST/REDIS_PORT）
#   也可显式指定 REDIS_MODE=py|docker|cli，以及：
#     REDIS_URL=redis://host:port/db  REDIS_HOST=... REDIS_PORT=... REDIS_DB=...
#     REDIS_CONTAINER=...  SEED_DEMO_RECORDS=150  SEED_DEMO_VERSION=20260101
#
# 第一个查询（seed 完成后执行；最小栈把 service 发布在 8000）：
#   curl -s -X POST http://localhost:8000/v1/query/v1-svc-usr-basic-info \
#     -H 'apikey: uat-c49d18ab72e03f55' \
#     -H 'Content-Type: application/json' \
#     -d '{"key":"demo-partner-0001"}' | jq .
#
#   期望：HTTP 200，found=true，fields 为 12 个白名单字段。
#   说明：apikey 是仓库 service/auth_matrix.yaml 中 partner-demo 租户的演示凭证，
#         不是生产密钥；直连服务时由该请求头定位租户与字段白名单。
#   此命令对应「redis + service」最小栈（service 发布在 8000）。完整 compose 栈的
#   service 不对外发布，验收路径以本最小栈为准。
# ============================================================
set -euo pipefail

SECONDS=0

# ---------- 演示目标（与 service/services.yaml、auth_matrix.yaml 对齐） ----------
SERVICE_ID="v1-svc-usr-basic-info"
TENANT="partner-demo"
APPKEY="uat-c49d18ab72e03f55"
KV_NAMESPACE="tags:usr_basic_info"

VERSION="${SEED_DEMO_VERSION:-20260101}"
RECORDS="${SEED_DEMO_RECORDS:-150}"
SERVICE_ORIGIN="${SEED_DEMO_SERVICE_ORIGIN:-http://localhost:8000}"
FIRST_KEY="demo-partner-0001"

if [ -n "${SEED_DEMO_DATA_AS_OF:-}" ]; then
  DATA_AS_OF="$SEED_DEMO_DATA_AS_OF"
else
  DATA_AS_OF="${VERSION:0:4}-${VERSION:4:2}-${VERSION:6:2}"
fi

ALIAS_KEY="${KV_NAMESPACE}:latest"
DATA_KEY_PREFIX="${KV_NAMESPACE}:${VERSION}:"
VERSION_META_KEY="sync:ver:${KV_NAMESPACE#tags:}:${VERSION}"

if ! [[ "$VERSION" =~ ^[0-9]{8}$ ]]; then
  echo "!! SEED_DEMO_VERSION 须为 8 位日期（YYYYMMDD），当前：$VERSION" >&2
  exit 1
fi
if ! [[ "$RECORDS" =~ ^[0-9]+$ ]] || [ "$RECORDS" -lt 1 ] || [ "$RECORDS" -gt 2000 ]; then
  echo "!! SEED_DEMO_RECORDS 须为 1~2000 的整数，当前：$RECORDS" >&2
  exit 1
fi

# ---------- Redis 访问方式：redis-py 直连优先（service 同款），容器/redis-cli 兜底 ----------
REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REDIS_MODE="${REDIS_MODE:-auto}"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_DB="${REDIS_DB:-0}"
REDIS_CONTAINER="${REDIS_CONTAINER:-oneserve-redis}"

log() { echo "== $*"; }
die() { echo "!! $*" >&2; exit 1; }

# redis-py 直连驱动（临时文件）：只实现本脚本用到的 redis-cli 子集
#   PING | DEL k... | SET k v | HSET k f v ... | --scan --pattern p | --pipe（stdin 每行 key\tvalue）
PY_DRIVER=$(mktemp "${TMPDIR:-/tmp}/oneserve-seed-driver.XXXXXX.py")
trap 'rm -f "$PY_DRIVER" "${TMP_FILE:-}"' EXIT
cat > "$PY_DRIVER" <<'PYEOF'
import os
import sys

import redis


def connect():
    url = os.environ.get("SEED_REDIS_URL", "")
    if url:
        return redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=3, socket_timeout=10
        )
    return redis.Redis(
        host=os.environ.get("SEED_REDIS_HOST", "localhost"),
        port=int(os.environ.get("SEED_REDIS_PORT", "6379")),
        db=int(os.environ.get("SEED_REDIS_DB", "0")),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=10,
    )


def main():
    args = sys.argv[1:]
    r = connect()
    if args[:1] == ["PING"]:
        r.ping()
        print("PONG")
    elif args[:1] == ["DEL"]:
        print(r.delete(*args[1:]) if len(args) > 1 else 0)
    elif args[:1] == ["SET"]:
        print(r.set(args[1], args[2]))
    elif args[:1] == ["HSET"]:
        print(r.hset(args[1], mapping=dict(zip(args[2::2], args[3::2]))))
    elif args[:1] == ["--scan"]:
        pattern = args[args.index("--pattern") + 1]
        for key in r.scan_iter(match=pattern, count=500):
            print(key)
    elif args[:1] == ["--pipe"]:
        pipe = r.pipeline(transaction=False)
        n = 0
        for line in sys.stdin:
            line = line.rstrip("\n")
            if not line:
                continue
            key, _, value = line.partition("\t")
            pipe.set(key, value)
            n += 1
        pipe.execute()
        print(f"pipe ok: {n} set, errors: 0")
    else:
        raise SystemExit(f"unsupported args: {args}")


if __name__ == "__main__":
    main()
PYEOF

# 找一个带 redis 包的 Python（优先仓库 .venv——quickstart 已建；其次 python3）
PY_BIN=""
for cand in "$REPO_ROOT/.venv/bin/python" python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import redis' >/dev/null 2>&1; then
    PY_BIN="$cand"
    break
  fi
done

py_redis() {
  SEED_REDIS_URL="$REDIS_URL" \
  SEED_REDIS_HOST="$REDIS_HOST" \
  SEED_REDIS_PORT="$REDIS_PORT" \
  SEED_REDIS_DB="$REDIS_DB" \
    "$PY_BIN" "$PY_DRIVER" "$@"
}

if [ "$REDIS_MODE" = "auto" ]; then
  if [ -n "$PY_BIN" ] && py_redis PING >/dev/null 2>&1; then
    REDIS_MODE="py"
  elif command -v docker >/dev/null 2>&1 && \
       docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq "$REDIS_CONTAINER"; then
    REDIS_MODE="docker"
  elif command -v redis-cli >/dev/null 2>&1; then
    REDIS_MODE="cli"
  else
    echo "!! 救生绳：REDIS_URL=redis://<host>:<port>/<db> 显式直连，或 REDIS_MODE=docker REDIS_CONTAINER=<容器名> 走容器" >&2
    echo "   自动探测均失败：redis-py 直连 $REDIS_URL ✗ · 运行中容器 $REDIS_CONTAINER ✗ · 本机 redis-cli ✗" >&2
    echo "   先起一个 Redis：docker run -d -p 6379:6379 valkey/valkey:8-alpine（6379 已占用则跳过）" >&2
    exit 1
  fi
fi

case "$REDIS_MODE" in
  py)    [ -n "$PY_BIN" ] || die "REDIS_MODE=py 需带 redis 包的 Python（未找到 $REPO_ROOT/.venv 或可 import redis 的 python3；先 pip install -r service/requirements.txt）" ;;
  docker) ;;
  cli)   ;;
  *)     die "未知 REDIS_MODE=$REDIS_MODE（可用：py|docker|cli）" ;;
esac

redis_do() {
  case "$REDIS_MODE" in
    py)     py_redis "$@" ;;
    docker) docker exec -i "$REDIS_CONTAINER" redis-cli -n "$REDIS_DB" "$@" ;;
    cli)    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -n "$REDIS_DB" "$@" ;;
  esac
}

redis_pipe() {
  if [ "$REDIS_MODE" = "py" ]; then
    py_redis --pipe
  elif [ "$REDIS_MODE" = "docker" ]; then
    docker exec -i "$REDIS_CONTAINER" redis-cli -n "$REDIS_DB" --pipe
  else
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" -n "$REDIS_DB" --pipe
  fi
}

# ---------- 0. 连通性 ----------
redis_do PING 2>/dev/null | grep -q PONG || die "Redis 不可达（mode=${REDIS_MODE}）"
case "$REDIS_MODE" in
  py)     REDIS_DESC="$REDIS_URL" ;;
  docker) REDIS_DESC="container $REDIS_CONTAINER" ;;
  cli)    REDIS_DESC="${REDIS_HOST}:${REDIS_PORT}" ;;
esac
log "Redis 就绪（mode=${REDIS_MODE}，${REDIS_DESC}）"

# ---------- 1. 定向清理本演示版本空间（幂等重灌；不 FLUSHDB） ----------
log "清理版本空间 ${DATA_KEY_PREFIX}*"
EXISTING=$(redis_do --scan --pattern "${DATA_KEY_PREFIX}*" || true)
if [ -n "$EXISTING" ]; then
  # 键名不含空白；150 个键一次 DEL，避免逐键起进程
  # shellcheck disable=SC2086
  redis_do DEL $EXISTING >/dev/null
fi
redis_do DEL "$ALIAS_KEY" "$VERSION_META_KEY" >/dev/null

# ---------- 2. 内置造数：12 个白名单字段，确定性、零外部文件 ----------
log "生成 ${RECORDS} 条演示记录（版本 ${VERSION}）"
AGE_BANDS=(A B C D E F)
CHANNELS=(APP WEB OTA PARTNER PORTAL APP)
CITY_CODES=(1101 3101 4401 5101 3301 4201)
CUST_LEVELS=(L1 L2 L3 L4 L2 L3)
EDU_LEVELS=(1 2 3 4 2 3)
GENDER_CODES=(1 2 0 1 2 1)
MARITAL_STS=(10 20 30 20 10 40)
OCCUPATIONS=(11 21 31 41 51 61)
RISK_LEVELS=(R1 R2 R3 R4 R3 R2)

TMP_FILE=$(mktemp "${TMPDIR:-/tmp}/oneserve-seed-demo.XXXXXX")
: > "$TMP_FILE"

if [ "$REDIS_MODE" = "py" ]; then
  # redis-py 管道：TSV 行（造数保证键/值均不含 \t 与换行）
  emit_set() { printf '%s\t%s\n' "$1" "$2" >> "$TMP_FILE"; }
else
  # redis-cli --pipe：RESP 协议帧
  emit_set() {
    local key="$1" value="$2"
    printf '*3\r\n$3\r\nSET\r\n$%d\r\n%s\r\n$%d\r\n%s\r\n' \
      "${#key}" "$key" "${#value}" "$value" >> "$TMP_FILE"
  }
fi

for ((i = 1; i <= RECORDS; i++)); do
  n=$(( (i - 1) % 6 ))
  key=$(printf 'demo-partner-%04d' "$i")
  blacklist=0
  if [ $(( i % 17 )) -eq 0 ]; then blacklist=1; fi
  vip=0
  if [ $(( i % 11 )) -eq 0 ]; then vip=1; fi
  open_date=$(printf '%04d-%02d-%02d' $(( 2018 + i % 6 )) $(( 1 + i % 12 )) $(( 1 + i % 28 )))

  json="{\"age_band\":\"${AGE_BANDS[$n]}\","
  json+="\"blacklist_flag\":\"$blacklist\","
  json+="\"channel_cd\":\"${CHANNELS[$n]}\","
  json+="\"city_code\":\"${CITY_CODES[$n]}\","
  json+="\"cust_level\":\"${CUST_LEVELS[$n]}\","
  json+="\"edu_level\":\"${EDU_LEVELS[$n]}\","
  json+="\"gender_cd\":\"${GENDER_CODES[$n]}\","
  json+="\"marital_sts\":\"${MARITAL_STS[$n]}\","
  json+="\"occupation_cd\":\"${OCCUPATIONS[$n]}\","
  json+="\"open_date\":\"$open_date\","
  json+="\"risk_level\":\"${RISK_LEVELS[$n]}\","
  json+="\"vip_flag\":\"$vip\"}"

  emit_set "${DATA_KEY_PREFIX}${key}" "$json"
done

# ---------- 3. 批量写数据键（管道一次往返） ----------
log "写入数据键（${REDIS_MODE} 管道）"
PIPE_OUT=$(redis_pipe < "$TMP_FILE" 2>&1) || die "批量写入失败：$PIPE_OUT"
printf '%s\n' "$PIPE_OUT" | grep -q 'errors: 0' || die "批量写入有错误：$PIPE_OUT"

# ---------- 4. 版本元信息 → 原子切 alias（先数据后指针，读方不读脏） ----------
redis_do HSET "$VERSION_META_KEY" \
  rows "$RECORDS" \
  cols 12 \
  data_as_of "$DATA_AS_OF" \
  source "scripts/seed_demo.sh" >/dev/null
redis_do SET "$ALIAS_KEY" "$VERSION" >/dev/null

COUNT=$(redis_do --scan --pattern "${DATA_KEY_PREFIX}*" | wc -l | tr -d ' ')
[ "$COUNT" = "$RECORDS" ] || die "写入后键数 ${COUNT} != ${RECORDS}（数据不完整）"
log "完成：${COUNT}/${RECORDS} 条，版本 ${VERSION}，data_as_of ${DATA_AS_OF}，耗时 ${SECONDS}s"

# ---------- 5. 把「第一个查询」直接打出来 ----------
echo
echo "演示数据已就绪：服务 ${SERVICE_ID} · 租户 ${TENANT} · ${COUNT} 条"
echo
echo "第一个查询："
echo "curl -s -X POST ${SERVICE_ORIGIN}/v1/query/${SERVICE_ID} \\"
echo "  -H 'apikey: ${APPKEY}' -H 'Content-Type: application/json' \\"
echo "  -d '{\"key\":\"${FIRST_KEY}\"}' | jq ."
echo
echo "期望：HTTP 200，found=true，fields 含 12 个白名单字段。"
