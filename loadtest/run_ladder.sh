#!/usr/bin/env bash
# 压测梯子编排（P3 通用化 · 对抗复核 P0/P1 修正版）：授权/配额前置门禁 →
# 服务登记解析（console_services 直取 /v1/svc/* route）→ latest 版本空间键池取样
# → 起压前命中自检 → 预热 → 梯度档位 → docker stats 采样 → 静置探针 → 审计对账
# → performance-report.md＋.html 双格式（同一数据渲染）。
#
# 用法（用户只提供「服务＋AppKey」，新增接口零开发直接测）：
#   bash loadtest/run_ladder.sh --service v1-svc-usr-asset-tag --appkey sb_xxx \
#       [--secret /path/to/cred.json] [--ladder 100,300,500,800] [--force]
#   或 ONESERVE_APPKEY=sb_xxx bash loadtest/run_ladder.sh --service ... （凭证不进 shell history，推荐）
#   可调：[--keys 2000] [--secs 70] [--ramp 10] [--warmup-users 50] [--warmup-secs 30]
#         [--probe-wait 30] [--host http://localhost:9080]
#
# 凭证传递（P1-6）：首选环境变量 ONESERVE_APPKEY（不进 shell history/ps）；
#   --appkey 参数保留但会进 shell history 与 ps 输出，只建议一次性临时用；
#   --secret＝凭证 JSON 文件（取 appkey 字段，如控制台签发回执；secret 本身
#   不参与调用——网关 key-auth 只验 apikey 头，明文只经内存、不落盘）。
#
# 前置门禁 EXIT 码（P0-1/P0-2＋2026-10-14 并发闸防呆）：
#   10＝凭证未授权目标服务（grants 不含 --service）→ 凭证页签发/授权；
#   11＝quota_qps 低于最低档位且未 --force（429 熔断失真风险）；
#   12＝网关 limit-conn 并发闸低于梯子最高档且未 --force（量纲错配：高档位实测的是
#        并发闸而非服务容量——调低档位/提并发上限，或 --force 明知故测看 429 分列）；
#    3＝起压前命中自检失败（探针非 200 或 found≠true——防跨表键池空投影假达标）；
#    2＝参数/环境错误（容器未跑、服务未登记、latest 缺失、键池空等）。
#
# 前置：目标栈已在跑（apisix 9080/service/redis 三容器；容器名可用
#   LOADTEST_APIXISX_CONTAINER 等环境变量覆盖）。历史上用
#   COMPOSE_PROJECT_NAME=oneserve-load bash scripts/up.sh 起独立压测栈——
#   现默认打当前运行栈（见 loadtest/README.md「起栈」）。
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:$PATH"
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin:$HOME/.docker/bin"

SERVICE="" APPKEY="" SECRET_FILE="" FORCE=0
LADDER="${LOADTEST_LEVELS_CSV:-100,300,500,800}"
KEYS_N="${LOADTEST_KEYS_N:-2000}"
HOST="${LOADTEST_HOST:-http://localhost:9080}"
SECS="${LOADTEST_SECS:-70}" RAMP="${LOADTEST_RAMP:-10}"
WARMUP_USERS="${LOADTEST_WARMUP_USERS:-50}" WARMUP_SECS="${LOADTEST_WARMUP_SECS:-30}"
PROBE_WAIT="${LOADTEST_PROBE_WAIT:-30}"
PROBE_KEYS="${LOADTEST_PROBE_KEYS:-5}"

usage() { sed -n '3,26p' "$0" | sed 's/^#\{1\}\( \)\{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --service) SERVICE="${2:?--service 需值}"; shift 2 ;;
    --appkey) APPKEY="${2:?--appkey 需值}"; shift 2 ;;
    --secret) SECRET_FILE="${2:?--secret 需路径}"; shift 2 ;;
    --ladder) LADDER="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --keys) KEYS_N="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --secs) SECS="$2"; shift 2 ;;
    --ramp) RAMP="$2"; shift 2 ;;
    --warmup-users) WARMUP_USERS="$2"; shift 2 ;;
    --warmup-secs) WARMUP_SECS="$2"; shift 2 ;;
    --probe-wait) PROBE_WAIT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1（--help 看用法）" >&2; exit 2 ;;
  esac
done
[ -n "$SERVICE" ] || usage 2
echo "$LADDER" | grep -Eq '^[0-9]+(,[0-9]+)*$' || { echo "梯子格式非法：${LADDER}（须如 20 或 100,300,500,800）" >&2; exit 2; }
MIN_LEVEL=$(echo "$LADDER" | tr ',' '\n' | sort -n | head -1)
MAX_LEVEL=$(echo "$LADDER" | tr ',' '\n' | sort -n | tail -1)   # limit-conn 并发闸防呆基准（2026-10-14）

# ---- 凭证解析（P1-6）：ONESERVE_APPKEY 环境变量（首选）> --appkey 参数 > --secret 文件 > 交互输入 ----
# （key-auth 只验 apikey；secret 不参与调用，交互输入不回显、明文不落盘）
CRED_SOURCE=""
if [ -n "${ONESERVE_APPKEY:-}" ]; then
  APPKEY="$ONESERVE_APPKEY"; CRED_SOURCE="env:ONESERVE_APPKEY"
elif [ -n "$APPKEY" ]; then
  CRED_SOURCE="--appkey（会进 shell history，建议改用 ONESERVE_APPKEY）"
elif [ -n "$SECRET_FILE" ]; then
  [ -f "$SECRET_FILE" ] || { echo "secret 文件不存在：${SECRET_FILE}" >&2; exit 2; }
  APPKEY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["appkey"])' "$SECRET_FILE") \
    || { echo "secret 文件解析失败（需 JSON 且含 appkey 字段）" >&2; exit 2; }
  CRED_SOURCE="--secret ${SECRET_FILE}"
fi
if [ -z "$APPKEY" ]; then
  printf 'AppKey（输入不回显）：' >&2
  IFS= read -rs APPKEY < /dev/tty || true
  echo >&2
  [ -n "$APPKEY" ] || { echo "未提供 AppKey（ONESERVE_APPKEY / --appkey / --secret / 交互输入四选一）" >&2; exit 2; }
  CRED_SOURCE="interactive"
fi
MASKED=$(python3 -c 'import sys; v=sys.argv[1]; print(v[:7]+"****"+v[-4:] if len(v)>11 else v[:2]+"****")' "$APPKEY")

# ---- 前置检查：docker 可用＋三容器在跑 ----
# A窗 2026-10-19（压测页容器内触发使能）：在服务容器内运行（审计卷本地在场）时无
# docker CLI/sock——跳过容器自检（栈活着由 discover/起压前预检真打证明）；宿主不变。
IN_SERVICE_CONTAINER=0
[ -d /data/audit ] && IN_SERVICE_CONTAINER=1
if [ "$IN_SERVICE_CONTAINER" = "1" ]; then
  echo "(容器内触发形态：跳过 docker CLI/容器自检)" >&2
else
command -v docker >/dev/null || { echo "docker 不在 PATH（见 README 前置）" >&2; exit 2; }
SVC_C="${LOADTEST_SERVICE_CONTAINER:-oneserve-service}"
REDIS_C="${LOADTEST_REDIS_CONTAINER:-oneserve-redis}"
APISIX_C="${LOADTEST_APIXISX_CONTAINER:-oneserve-apisix}"
for c in "$APISIX_C" "$SVC_C" "$REDIS_C"; do
  docker inspect -f '{{.Name}} {{.State.Running}}' "$c" 2>/dev/null | grep -q ' true$' \
    || { echo "容器未运行：${c}（先起栈，见 loadtest/README.md）" >&2; exit 2; }
done
fi

# ---- 结果目录：results/<时间戳>-<服务短名>（HTML 化配套，为版本管理式页面铺路） ----
SHORT=$(printf '%s' "$SERVICE" | sed -e 's#^/##' -e 's#^v1/svc/##' -e 's#^v1-svc-##' -e 's#[^A-Za-z0-9._-]#_#g')
TS=$(date +%Y%m%d-%H%M%S)
RUN_DIR="loadtest/results/$TS-$SHORT"
mkdir -p "$RUN_DIR"
echo "== 压测梯子：${SERVICE} @ ${HOST}｜凭证 ${MASKED}（${CRED_SOURCE}）｜档位 ${LADDER}（最低 ${MIN_LEVEL}）｜结果 ${RUN_DIR} =="

# ---- 1) 发现＋前置门禁（P0-1/P0-2）：grants 授权校验、quota_qps 档位比对、
#         console_services route 解析、latest 版本空间键池取样（EXIT 10/11/2 直通） ----
GATE_ARGS=""
if [ "$FORCE" = 1 ]; then GATE_ARGS="--force"; fi
python3 loadtest/discover.py --service "$SERVICE" --out-dir "$RUN_DIR" --keys "$KEYS_N" \
  --appkey "$APPKEY" --cred-source "$CRED_SOURCE" --host "$HOST" --levels "$LADDER" \
  --secs "$SECS" --ramp "$RAMP" --warmup-users "$WARMUP_USERS" --warmup-secs "$WARMUP_SECS" \
  --min-level "$MIN_LEVEL" --max-level "$MAX_LEVEL" $GATE_ARGS

# ---- 从 meta.json 取目标事实（route/入参键字段/方法，供探针与压测共用） ----
ROUTE=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["service"]["route"])' "$RUN_DIR/meta.json")
# ---- 目标环境注入（locustfile.py / run_level.sh / 起压前探针共用）----
KEY_FIELD=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["service"]["key_field"])' "$RUN_DIR/meta.json")
METHOD=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["service"]["method"])' "$RUN_DIR/meta.json")

# ---- 2) 起压前命中自检（P0-2）：PROBE_KEYS 键逐一真查，须全部 200 且 found=true ----
# （键池与目标表错配时，跨表键在授权投影下全 miss → 空投影假达标，故命中率<100% 拒跑）
echo "idx,http_code,found" > "$RUN_DIR/preflight.txt"
HIT_N=0; HIT_OK=0; IDX=0
while IFS= read -r K; do
  IDX=$((IDX+1)); [ "$IDX" -gt "$PROBE_KEYS" ] && break
  CODE=$(curl -s -o "$RUN_DIR/preflight_$IDX.json" -w '%{http_code}' -X "$METHOD" \
    -H "apikey: $APPKEY" -H 'Content-Type: application/json' \
    -d "{\"$KEY_FIELD\": \"$K\"}" "$HOST$ROUTE")
  FOUND=$(python3 -c 'import json,sys; print("true" if json.load(open(sys.argv[1])).get("found") else "false")' "$RUN_DIR/preflight_$IDX.json" 2>/dev/null || echo "false")
  echo "$IDX,$CODE,$FOUND" >> "$RUN_DIR/preflight.txt"
  HIT_N=$((HIT_N+1))
  if [ "$CODE" = "200" ] && [ "$FOUND" = "true" ]; then HIT_OK=$((HIT_OK+1)); fi
done < "$RUN_DIR/keys.txt"
cat "$RUN_DIR/preflight.txt"
if [ "$HIT_OK" -lt "$HIT_N" ] || [ "$HIT_OK" -eq 0 ]; then
  if grep -q '^\([0-9]*\),429,' "$RUN_DIR/preflight.txt"; then
    echo "起压前命中自检失败：$HIT_OK/$HIT_N 键命中——探针被网关限流拒绝（HTTP 429，多为 quota_qps 窗口未恢复或 quota_daily 已耗尽）；稍等重试或调高配额后重跑（明细见 $RUN_DIR/preflight*.json）" >&2
  else
    echo "起压前命中自检失败：$HIT_OK/$HIT_N 键命中（须 100%——键池与目标表错配＝空投影假达标；明细见 $RUN_DIR/preflight*.json）" >&2
  fi
  exit 3
fi
echo "== 起压前命中自检 $HIT_OK/$HIT_N ✓（HTTP 200＋found=true）=="

export LOCUST_ROUTE="$ROUTE" LOCUST_APIKEY="$APPKEY" LOCUST_KEYS_FILE="$RUN_DIR/keys.txt" \
       LOCUST_KEY_FIELD="$KEY_FIELD" LOCUST_METHOD="$METHOD"
export LOADTEST_HOST="$HOST" LOADTEST_SECS="$SECS" LOADTEST_RAMP="$RAMP" LOADTEST_RUN_DIR="$RUN_DIR"

# ---- 4) 预热（热缓存/连接池，不计入正式统计）----
echo "== 预热：$WARMUP_USERS 并发 × ${WARMUP_SECS}s =="
python3 -m locust -f loadtest/locustfile.py --headless -u "$WARMUP_USERS" \
    -r $(( WARMUP_USERS / 10 + 1 )) -t "${WARMUP_SECS}s" --host "$HOST" \
    --only-summary --exit-code-on-error 0 2>&1 | tee "$RUN_DIR/warmup.log" >/dev/null

# ---- 5) 梯子逐档（run_level.sh：审计基线→采样→locust→审计终值→对账行）----
for U in $(echo "$LADDER" | tr ',' ' '); do
  bash loadtest/run_level.sh "$U"
done

# ---- 6) 静置探针：等瓶颈恢复后连发 5 个单请求计时 ----
echo "== 静置 ${PROBE_WAIT}s 后探针 5 发 =="
sleep "$PROBE_WAIT"
LINES=$(wc -l < "$RUN_DIR/keys.txt" | tr -d ' ')
echo "ts,probe,http_code,time_total_s" > "$RUN_DIR/probe.txt"
for i in 1 2 3 4 5; do
  KN=$(( RANDOM % LINES + 1 ))
  K=$(awk -v n="$KN" 'NR==n' "$RUN_DIR/keys.txt")
  printf '%s,%s,%s\n' "$(date +%FT%T%z)" "probe$i" \
    "$(curl -s -o /dev/null -w '%{http_code},%{time_total}' -X "$METHOD" \
      -H "apikey: $APPKEY" -H 'Content-Type: application/json' \
      -d "{\"$KEY_FIELD\": \"$K\"}" "$HOST$ROUTE")" >> "$RUN_DIR/probe.txt"
done
cat "$RUN_DIR/probe.txt"

# ---- 7) 审计对账（endpoint/caller 按本次服务与凭证派生）→ 8) 汇总 CSV → 9) 报告 MD＋HTML ----
python3 loadtest/audit_check.py "$RUN_DIR"
python3 loadtest/summarize.py "$RUN_DIR"
python3 loadtest/report_gen.py "$RUN_DIR"

echo "== 梯子完成：报告 $(cd "$RUN_DIR" && pwd)/performance-report.md ＋ performance-report.html（同一数据渲染）=="
