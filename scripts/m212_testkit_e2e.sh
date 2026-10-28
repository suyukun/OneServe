#!/usr/bin/env bash
# TASK-M2-12 testkit M2 场景（s6）隔离环境编排 + 真链路实证。
#
# 架构口径（ADR-0005 / docs/plans/M2范围裁定_v2-架构纠正_20260923.md）：
#   HBase＝冷存储，Redis＝热存储；链路＝外围ETL→HBase→【页面选表→加载】→Redis→封装API→对外。
#   本脚本只起「隔离」环境并把 s6 场景（testkit/scenarios/m2load.py）跑起来，**零假 stub**。
#
# 隔离纪律（AGENTS.md 2026-09-23 事故后立规）：
#   · 容器名一律 oneserve-m212-*（HBase 亦用独立名 oneserve-m212-hbase，**不叫 oneserve-hbase**，
#     避免与并行卡/M2-6 抢名）；Redis 用一次性容器（**零 named volume**，绝不写共享栈 oneserve-redis）。
#   · 网关复用共享 APISIX（其 etcd 配置写死 oneserve-etcd，本仓无第二 etcd 配置）：只在共享网关
#     加「临时路由/上游」指向本卡隔离 service，**验后即删**（与 publish_load_e2e.sh / m24 同款；
#     不写共享 Redis 数据，仅临时改写网关配置面）。
#   · 绝不 docker compose down；绝不 down -v；共享栈 one serve-redis / oneserve-service 不动。
#
# 链路（全程真容器/真 Redis/真网关）：
#   ① 起隔离 Redis（oneserve-m212-redis，宿主 16390）＋隔离 HBase（oneserve-m212-hbase，宿主 19090）
#   ② 造数（sync/gen_mock_source.py，297 列真结构＋SM3 真算）→ 容器内 hbase_loader 灌 HBase
#   ③ 构建本仓服务镜像 → 起隔离 service（oneserve-m212-service，宿主 18014，HBASE_HOST 指向隔离 HBase）
#   ④ 在共享 APISIX 加临时上游/路由 /v1/m212/query → /v1/tags/query（key-auth）
#   ⑤ 跑 testkit s6（选表→加载→对账差异=0→幂等→切指针→经网关真调→失败态）
#   ⑥ 收尾：删临时路由/上游/全部 oneserve-m212-* 容器（共享栈未动，共享 Redis 未写）
#
# 用法：bash scripts/m212_testkit_e2e.sh
#       M212_KEEP=1 bash scripts/m212_testkit_e2e.sh   # 保留容器（调试；记得手动清理）
# 退出码：全绿 0；任一断言红 1；docker 不可用 2。
set -eo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
PY="${PY:-python3}"
EVIDENCE_DIR="tests/evidence"
STAMP="$(date +%Y%m%d-%H%M%S)"
EVIDENCE="${M212_EVIDENCE:-$EVIDENCE_DIR/m212-testkit-e2e-$STAMP.txt}"
PASS=0; FAIL=0

say() { echo "[m212] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR" sync/out
exec > >(tee "$EVIDENCE") 2>&1
say "===== testkit M2 场景（s6）隔离环境 e2e $(date '+%F %T') ====="
say "仓库=$PWD python=$($PY --version 2>&1) docker=$DOCKER_BIN"

# ---------- 常量 ----------
DATE="$(date +%Y%m%d)"
USERS=600
FILL=0.30
SEED=42
CSV="sync/out/src_m212_$DATE.csv"
IMG="oneserve/service:m212-e2e"
REDIS_CT="oneserve-m212-redis"
HBASE_CT="oneserve-m212-hbase"
SVC_CT="oneserve-m212-service"
PORT=18014
REDIS_PORT=16390
HBASE_PORT=19090
ROUTE_ID="oneserve-m212-route"
UP_ID="oneserve-m212-up"
ROUTE_URI="/v1/m212/query"
ADMIN_TOKEN="admin321"
APPKEY="oneserve-dev-appkey-001"
REDIS_URL="redis://$REDIS_CT:6379/0"
TMP="$(mktemp -d)"

if [ "${M212_KEEP:-0}" = "1" ]; then KEEP=1; else KEEP=0; fi

# ---------- 临时网关助手（Admin API；9180 不发布宿主，须容器网内调） ----------
GW_HELPER="sync/out/m212_gw.py"
cat > "$GW_HELPER" <<'PYEOF'
import json, os, sys, urllib.request, urllib.error
ADMIN = os.environ.get("APISIX_ADMIN", "http://oneserve-apisix:9180") + "/apisix/admin"
KEY = os.environ.get("APISIX_ADMIN_KEY", "***REMOVED***")
ROUTE_ID = os.environ["GW_ROUTE_ID"]; UP_ID = os.environ["GW_UP_ID"]
# ROUTE_URI / NODE 只在 up 分支需要：**惰性读取**（down/check 缺这两个 env 也不崩——
# 本卡实测踩过：cleanup 调 down 时未传 GW_ROUTE_URI/GW_NODE，模块级硬取 KeyError 被
# `|| true` 静默吞掉，临时路由残留共享网关）。
_ROUTE_URI = os.environ.get("GW_ROUTE_URI", "")
_NODE = os.environ.get("GW_NODE", "")

def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{ADMIN}/{path}", data=data, method=method)
    r.add_header("X-API-KEY", KEY)
    if data: r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]

def main():
    cmd = sys.argv[1]
    if cmd == "up":
        print("upstream", req("PUT", f"upstreams/{UP_ID}", {
            "name": UP_ID, "type": "roundrobin", "nodes": {_NODE: 1}}))
        print("route", req("PUT", f"routes/{ROUTE_ID}", {
            "name": ROUTE_ID, "uri": _ROUTE_URI, "methods": ["POST"], "upstream_id": UP_ID,
            "plugins": {"key-auth": {}, "request-id": {"header_name": "X-Trace-Id"},
                        "proxy-rewrite": {"regex_uri": [f"^{_ROUTE_URI}$", "/v1/tags/query"]}}}))
    elif cmd == "down":
        print("route", req("DELETE", f"routes/{ROUTE_ID}"))
        print("upstream", req("DELETE", f"upstreams/{UP_ID}"))
    elif cmd == "check":
        code, _ = req("GET", f"routes/{ROUTE_ID}")
        print("exists" if code == 200 else "gone")
    else:
        raise SystemExit(f"未知命令 {cmd}")
main()
PYEOF

gw() {
  "$DOCKER_BIN" run --rm --network oneserve-net -v "$PWD/sync/out:/gw:ro" \
    -e GW_ROUTE_ID="$ROUTE_ID" -e GW_UP_ID="$UP_ID" \
    -e GW_ROUTE_URI="$ROUTE_URI" -e GW_NODE="$SVC_CT:8000" \
    "$IMG" python /gw/m212_gw.py "$1" 2>&1 || true
}

# ---------- 收尾（幂等；绝不用 docker compose down） ----------
cleanup() {
  local rc=$?
  if [ "$KEEP" = "1" ]; then
    say "M212_KEEP=1 → 保留容器（$SVC_CT/$REDIS_CT/$HBASE_CT）；记得手动清理（含临时路由 $ROUTE_ID）"
  else
    gw down >/dev/null 2>&1 || true
    LEFT=$(gw check 2>/dev/null | tail -1 || true)
    if [ "$LEFT" = "gone" ]; then
      say "收尾复核：临时路由/上游已删除（$ROUTE_ID）"
    else
      say "⚠ 收尾复核：临时路由/上游未删净（check=$LEFT）——请人工删 routes/$ROUTE_ID 与 upstreams/$UP_ID"
    fi
    "$DOCKER_BIN" rm -f "$SVC_CT" "$REDIS_CT" "$HBASE_CT" >/dev/null 2>&1 || true
    rm -f "$GW_HELPER" 2>/dev/null || true
    rm -rf "$TMP" 2>/dev/null || true
    say "收尾：临时路由/上游/全部 oneserve-m212-* 容器已删（共享栈未动，共享 Redis 未写）"
  fi
  return $rc
}
trap cleanup EXIT

# ---------- docker 探测 ----------
"$DOCKER_BIN" info >/dev/null 2>&1 || { say "docker daemon 不可用（探测=$DOCKER_BIN）→ 真链路无法执行（不以假 stub 冒充）"; exit 2; }
ok "docker daemon 可用"

# ---------- 共享栈挂载源核验（AGENTS.md 硬纪律） ----------
WD=$("$DOCKER_BIN" inspect oneserve-service --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || echo "")
case "$WD" in
  */Projects/OneServe) ok "共享演示栈挂载源＝main 检出（$WD）" ;;
  "") say "（共享演示栈未起：本卡不依赖它，仅共享 APISIX 需在）" ;;
  *) bad "共享演示栈挂载源不是 main 检出（$WD）→ 先修栈再验（AGENTS.md 事故立规）" ;;
esac
"$DOCKER_BIN" inspect oneserve-apisix >/dev/null 2>&1 && ok "共享 APISIX 在（临时路由宿主）" || { bad "共享 APISIX 不在——网关真调无法进行"; }

# ---------- ① 隔离 Redis ＋ 隔离 HBase ----------
say "① 起隔离 Redis（$REDIS_CT :$REDIS_PORT）＋隔离 HBase（$HBASE_CT :$HBASE_PORT，Thrift1 9090）..."
"$DOCKER_BIN" rm -f "$REDIS_CT" "$HBASE_CT" >/dev/null 2>&1 || true
"$DOCKER_BIN" run -d --name "$REDIS_CT" --network oneserve-net \
  -p "127.0.0.1:$REDIS_PORT:6379" library/redis:7-alpine >/dev/null
for i in $(seq 1 30); do
  "$DOCKER_BIN" exec "$REDIS_CT" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 1
done
"$DOCKER_BIN" exec "$REDIS_CT" redis-cli ping 2>/dev/null | grep -q PONG && ok "隔离 Redis 就绪（一次性容器，零 named volume）" || { bad "隔离 Redis 未就绪"; exit 1; }
"$DOCKER_BIN" run -d --name "$HBASE_CT" --network oneserve-net -p "127.0.0.1:$HBASE_PORT:9090" \
  --memory 2g -e HBASE_HEAPSIZE=1024 -e HBASE_THRIFT_OPTS="-Xms64m -Xmx256m" \
  --entrypoint bash openeuler/hbase:latest \
  -c "start-hbase.sh && hbase-daemon.sh start thrift && tail -f /dev/null" >/dev/null
READY=0
for i in $(seq 1 75); do
  if "$DOCKER_BIN" exec "$HBASE_CT" bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ "$READY" = "1" ] && ok "隔离 HBase 就绪（容器内 9090 通）" || { bad "隔离 HBase 150s 未就绪"; exit 1; }

# ---------- ② 造数 ＋ 容器内灌 HBase ----------
say "② 造数 $USERS 用户 × 296 标签列（fill=$FILL seed=$SEED）→ $CSV"
$PY sync/gen_mock_source.py --date "$DATE" --users "$USERS" --fill "$FILL" --seed "$SEED" \
    --out "$CSV" > "sync/out/gen_m212_$DATE.json"
$PY -c "
import json
d=json.load(open('sync/out/gen_m212_$DATE.json'))
assert d['users']==$USERS and d['tag_cols']==296 and len(d['sha256'])==64, d
print('  ✓ 造数：%d 用户 × %d 标签列（键=SM3 真算）' % (d['users'], d['tag_cols']))" \
  && ok "造数与既有链路同源（gen_mock_source.py）" || { bad "造数异常"; exit 1; }

say "②b 构建服务镜像 $IMG ..."
"$DOCKER_BIN" build -q -t "$IMG" ./service >/dev/null
ok "服务镜像就绪（本仓代码构建）"

say "②c 容器内 sync/hbase_loader.py 灌隔离 HBase（容器网内 $HBASE_CT:9090）..."
HB_LOAD=$("$DOCKER_BIN" run --rm --network oneserve-net \
  -e HBASE_HOST="$HBASE_CT" -e HBASE_PORT="9090" \
  -v "$PWD/sync:/sync:ro" "$IMG" \
  python /sync/hbase_loader.py --csv "/sync/out/$(basename "$CSV")" --version "$DATE" --replace 2>&1) || true
echo "$HB_LOAD"
echo "$HB_LOAD" | $PY -c "import sys,json;d=json.loads(sys.stdin.read().strip().splitlines()[-1]);assert d['ok'] and d['rows']==$USERS,d" \
  && ok "HBase 装载 $USERS 行（表/列族读 services.yaml）" || { bad "HBase 装载异常"; exit 1; }

# ---------- ③ 隔离 service ----------
say "③ 起隔离 service（$SVC_CT :$PORT，HBASE_HOST=$HBASE_CT）..."
"$DOCKER_BIN" rm -f "$SVC_CT" >/dev/null 2>&1 || true
ENVFILE="$TMP/env"
{
  echo "REDIS_URL=$REDIS_URL"
  echo "SYNC_REDIS_URL=$REDIS_URL"
  echo "AUDIT_DIR=/data/audit"
  echo "HBASE_HOST=$HBASE_CT"
  echo "HBASE_PORT=9090"
  echo "APISIX_ADMIN=http://oneserve-apisix:9180"
  echo "APISIX_ADMIN_KEY=***REMOVED***"
  echo "ADMIN_API_TOKEN=$ADMIN_TOKEN"
  echo "SCHEMA_CSV=/sync/schema_usr_label_wide_297.csv"   # TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）
  echo "REGISTRY_POLL_INTERVAL_S=5"
} > "$ENVFILE"
"$DOCKER_BIN" run -d --name "$SVC_CT" --network oneserve-net --env-file "$ENVFILE" \
  -p "127.0.0.1:$PORT:8000" -v "$PWD/sync:/sync:ro" "$IMG" >/dev/null
HEALTHY=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || echo 000)
  [ "$code" = "200" ] && { HEALTHY=1; break; }
  sleep 2
done
[ "$HEALTHY" = "1" ] && ok "隔离 service 健康（/healthz 200 @127.0.0.1:$PORT）" || { bad "service 未健康"; "$DOCKER_BIN" logs --tail 40 "$SVC_CT"; exit 1; }
"$DOCKER_BIN" logs "$SVC_CT" 2>&1 | grep -q "HBase 连接池就绪" && ok "启动日志含「HBase 连接池就绪」（隔离 HBase 真连）" || bad "未见 HBase 连接池就绪日志"

# ---------- ④ 临时网关路由 ----------
say "④ 在共享 APISIX 加临时上游/路由 $ROUTE_URI → /v1/tags/query ..."
gw up
sleep 2
# 下发生效核验：helper 若静默失败（曾踩 IndentationError），此处立即红，不把假象留给 s6
GWCHECK=$(gw check 2>/dev/null | tail -1 || true)
[ "$GWCHECK" = "exists" ] && ok "临时路由已下发并核验存在（$ROUTE_ID → $SVC_CT:8000）" \
  || { bad "临时路由未生效（check=$GWCHECK）——s6 网关真调必 404，先排查 helper/Admin API"; }

# ---------- ⑤ 跑 testkit s6 ----------
say "⑤ 跑 testkit s6（M2 端到端＋冷热加载六步机检）..."
set +e
$PY testkit/run.py s6 \
  --svc "http://127.0.0.1:$PORT" \
  --gw "http://127.0.0.1:9080" \
  --route "$ROUTE_URI" \
  --admin-token "$ADMIN_TOKEN" \
  --version "$DATE" \
  --redis-url "redis://127.0.0.1:$REDIS_PORT/0" \
  --source-csv "$CSV" \
  --docker-bin "$DOCKER_BIN" \
  --hbase-ct "$HBASE_CT" \
  --env-name "本机M2-12"
S6_RC=$?
set -e
[ "$S6_RC" = "0" ] && ok "s6 六步机检全绿（run.json/report.md/checks.txt 见 testkit/reports）" || bad "s6 场景红（exit $S6_RC，先看 checks.txt）"

# 把最近一次 s6 产物拷入 tests/evidence 留档（照 m15-7 惯例）
LATEST_S6=$(ls -dt testkit/reports/*-s6 2>/dev/null | head -1 || true)
if [ -n "$LATEST_S6" ]; then
  DEST="$EVIDENCE_DIR/m212-s6-$(basename "$LATEST_S6")"
  mkdir -p "$DEST"
  cp -f "$LATEST_S6"/run.json "$LATEST_S6"/report.md "$LATEST_S6"/checks.txt "$DEST"/ 2>/dev/null || true
  ok "s6 产物留档 $DEST（run.json/report.md/checks.txt）"
fi

# ---------- ⑥ 结论 ----------
say "===== e2e 结果: PASS=$PASS FAIL=$FAIL ====="
say "定位（如实）：本卡只把 M2 新能力纳入 testkit 回归；真集群（NC-M2-9）未做，跑在开发替身容器上。"
if [ "$FAIL" -eq 0 ] && [ "$S6_RC" = "0" ]; then
  say "testkit M2 场景（s6）隔离环境全绿，证据已存 $EVIDENCE"
  exit 0
fi
say "存在失败项，见 $EVIDENCE"
exit 1
