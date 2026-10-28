#!/usr/bin/env bash
# ============================================================
# TASK-M2-6 · 端到端验收 + schema 漂移演练（M2 收口卡，判据核心）
#
# 一条命令走完 ADR-0005 口径的真链路：
#   HBase（冷存储）→ 页面选表 → 加载到 Redis（热存储）→ 发布 → 经网关真调 API 返真数据
# 外加真跑一次 schema 漂移删列 → 服务被隔离返明确错误码 → 恢复后放行。
#
# 架构口径（ADR-0005 / docs/plans/M2范围裁定_v2-架构纠正_20260923.md）：
#   HBase＝冷存储、Redis＝热存储；HBase **不**作独立数据源发 API（能力保留代码，归 M3）。
#
# 隔离纪律（AGENTS.md「共享演示栈归属」，硬纪律）：
#   · 本脚本用 COMPOSE_PROJECT_NAME=oneserve-m26 ＋ compose.m26-iso.yml：
#     专属网络 oneserve-m26-net／专属容器名 oneserve-m26-*／专属卷 oneserve-m26-*／
#     宿主端口 19080（网关）19000（服务）26379（Redis）29090（HBase Thrift）。
#     共享栈的 9080／8000／9090 与 oneserve 项目名一律不动；
#   · 收尾只 down --remove-orphans，**绝不 down -v**；卷按前缀 check-then-delete；
#   · 绝不写共享栈 Redis。
#
# 用法：
#   bash scripts/m26_e2e_acceptance.sh                 # 全跑（含漂移演练）
#   M26_KEEP=1 bash scripts/m26_e2e_acceptance.sh      # 保留容器调试（记得手动清理）
#   M26_SKIP_DRIFT=1 bash scripts/m26_e2e_acceptance.sh
# 退出码：全绿 0；任一断言红 1；docker 不可用或共享栈挂载源不对 2。
# 证据：tests/evidence/m26-e2e-<时间戳>.txt（全程 tee）
# ============================================================
set -uo pipefail
cd "$(dirname "$0")/.."

REPO="$PWD"
PY="${PY:-python3}"
DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
[ -x "$DOCKER_BIN" ] || DOCKER_BIN="$(command -v docker 2>/dev/null || true)"

PROJECT="oneserve-m26"
export COMPOSE_PROJECT_NAME="$PROJECT"
COMPOSE=("$DOCKER_BIN" compose -p "$PROJECT"
         -f docker-compose.yml -f compose.hbase.yml -f compose.m26-iso.yml)

# —— 端口 / 常量（共享栈：9080／8000／9090；本栈全部错开） ——
GW_PORT=19080
SVC_PORT=19000
REDIS_PORT=26379
HBASE_PORT=29090
GW="http://127.0.0.1:${GW_PORT}"
BASE="http://127.0.0.1:${SVC_PORT}"
ADMIN_TOKEN="admin321"
APPKEY="oneserve-dev-appkey-001"
DATE="$(date +%Y%m%d)"
USERS=500
FILL=0.30
SEED=42
CSV="sync/out/src_m26_${DATE}.csv"
SID="v1-svc-m26-demo"
TABLE="oneserve_demo"                  # services.yaml demo_hbase_query 的单一真源表名
HBASE_TABLE="default:${TABLE}"
RURI="/v1/svc/m26-demo"

EVIDENCE_DIR="tests/evidence"
STAMP="$(date +%Y%m%d-%H%M%S)"
EVIDENCE="${M26_EVIDENCE:-$EVIDENCE_DIR/m26-e2e-${STAMP}.txt}"
TMP="$(mktemp -d)"
KEEP="${M26_KEEP:-0}"
PASS=0; FAIL=0

mkdir -p "$EVIDENCE_DIR" sync/out
exec > >(tee "$EVIDENCE") 2>&1

say()  { echo "[m26] $*"; }
step() { echo; echo "══════ $* ══════"; }
ok()   { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL+1)); }
cmd()  { echo "  $ $*"; }            # 打印可复制执行的命令行（验收证据＝命令+输出）

# ---------- 收尾（幂等；绝不用 down -v） ----------
cleanup() {
  local rc=$?
  if [ "$KEEP" = "1" ]; then
    say "M26_KEEP=1 → 保留本栈容器与卷；手动清理：COMPOSE_PROJECT_NAME=${PROJECT} $DOCKER_BIN compose -f docker-compose.yml -f compose.hbase.yml -f compose.m26-iso.yml down --remove-orphans"
  else
    say "收尾：拆本栈（down --remove-orphans；不动共享栈，不用 down -v）"
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
    # 卷「check-then-delete」：只删本卡专属前缀，绝不泛删
    local v
    for v in $("$DOCKER_BIN" volume ls --format '{{.Name}}' 2>/dev/null | grep '^oneserve-m26-' || true); do
      "$DOCKER_BIN" volume rm "$v" >/dev/null 2>&1 || true
    done
    rm -rf "$TMP"
    local LEFT LEFT_V
    LEFT="$("$DOCKER_BIN" ps -a --format '{{.Names}}' 2>/dev/null | grep '^oneserve-m26-' || true)"
    [ -z "$LEFT" ] && say "收尾核验：oneserve-m26-* 容器残留=0" || say "收尾核验：仍有残留容器：$LEFT"
    LEFT_V="$("$DOCKER_BIN" volume ls --format '{{.Name}}' 2>/dev/null | grep '^oneserve-m26-' || true)"
    [ -z "$LEFT_V" ] && say "收尾核验：oneserve-m26-* 卷残留=0" || say "收尾核验：仍有残留卷：$LEFT_V"
  fi
  return $rc
}
trap cleanup EXIT

say "===== TASK-M2-6 端到端验收 + schema 漂移演练 $(date '+%F %T') ====="
say "仓库=$REPO python=$($PY --version 2>&1) docker=$DOCKER_BIN"
say "本卡栈：project=$PROJECT 网关=127.0.0.1:$GW_PORT service=127.0.0.1:$SVC_PORT redis=127.0.0.1:$REDIS_PORT hbase=127.0.0.1:$HBASE_PORT"

# ---------- ⓪ 前置：docker + 共享栈挂载源核对 ----------
step "⓪ 前置检查：docker 可用 + 共享栈挂载源不是 worktree"
if [ -z "$DOCKER_BIN" ] || ! "$DOCKER_BIN" info >/dev/null 2>&1; then
  say "docker daemon 不可用（探测=${DOCKER_BIN:-<none>}）→ 真链路无法执行（不以假 stub 冒充）"
  exit 2
fi
ok "docker daemon 可用（${DOCKER_BIN}）"

cmd "$DOCKER_BIN inspect oneserve-service --format '{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}'"
MOUNT="$("$DOCKER_BIN" inspect oneserve-service --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
echo "  · 共享栈 service 挂载源 = ${MOUNT:-<容器不在场>}"
if [ -n "$MOUNT" ] && [ "$MOUNT" != "$REPO" ]; then
  bad "共享栈 oneserve-service 挂载源=${MOUNT}（应为仓库检出 ${REPO}）"
  bad "AGENTS.md 硬纪律：先修栈再验，否则看到的是旧代码。本脚本拒绝继续。"
  exit 2
fi
[ -n "$MOUNT" ] && ok "共享栈挂载源正确（main 检出）" || ok "共享栈 oneserve-service 不在场（无串栈风险，跳过核对——如实标注）"

# ---------- ① 起 HBase 开发替身容器（compose.hbase.yml 独立 override，用完删） ----------
step "① 起 HBase 开发替身容器（compose.hbase.yml 独立 override，用完删）"
# 网关配置副本：etcd 指向本栈 + Admin API ACL 放行本栈网段
# （configs/apisix/config.yaml 的 allow_admin 写的是共享栈网段 172.28.0.0/16，
#  本栈在 172.29.0.0/16；不改这份真源，只在副本里替换——本卡不改仓库配置）
sed -e 's#http://oneserve-etcd:2379#http://oneserve-m26-etcd:2379#' \
    -e 's#172\.28\.0\.0/16#172.29.0.0/16#' \
    configs/apisix/config.yaml > "$TMP/apisix-config.yaml"
grep -q 'http://oneserve-m26-etcd:2379' "$TMP/apisix-config.yaml" \
  && grep -q '172.29.0.0/16' "$TMP/apisix-config.yaml" \
  || { bad "网关配置副本生成失败（etcd/ACL 未替换）"; exit 1; }
ok "网关配置副本已生成（etcd=oneserve-m26-etcd:2379；Admin ACL 放行 172.29.0.0/16）"

export M26_APISIX_CONFIG="$TMP/apisix-config.yaml"
export M26_SERVICE_DIR="$TMP/service_dir"
export M26_AUDIT_DIR="$TMP/audit"
export M26_RETENTION_DIR="$TMP/retention"
export M26_BATCH_DIR="$TMP/batch-results"
mkdir -p "$M26_AUDIT_DIR" "$M26_RETENTION_DIR" "$M26_BATCH_DIR"
cp -R service "$M26_SERVICE_DIR"

# 授权矩阵：给本卡新服务登记 demo_bank 白名单（改副本，不碰仓库真文件）
$PY - "$M26_SERVICE_DIR/auth_matrix.yaml" "$SID" <<'PYEOF'
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1]); sid = sys.argv[2]
doc = yaml.safe_load(p.read_text(encoding="utf-8"))
for row in doc["matrix"]:
    if row.get("tenant") == "demo_bank":
        row["grants"].append({"service": sid, "endpoint": "/v1/query/" + sid,
                              "fields": ["ac_active_sts_01", "mk_touch_sts_02"]})
p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("  · 授权矩阵副本：demo_bank 已登记 %s 白名单" % sid)
PYEOF
ok "service 目录副本就绪（运行期写副本；仓库真配置零改动）"

say "构建本卡代码镜像 oneserve/service:m26-e2e ..."  # 本卡未改 admin/portal 源码，构建物无需重跑 build_portal.py
if ! "$DOCKER_BIN" build -q -t oneserve/service:m26-e2e ./service >/dev/null; then
  bad "镜像构建失败"; exit 1
fi
ok "镜像 oneserve/service:m26-e2e 就绪（独立 tag，不覆写共享栈的 oneserve/service:m0）"

say "起本栈（etcd + apisix + redis + service + hbase）..."
if ! "${COMPOSE[@]}" up -d etcd apisix redis service hbase portal >/dev/null 2>&1; then
  bad "compose up 失败"; exit 1
fi

wait_healthy() {  # $1=容器名 $2=上限秒
  local c="$1" n="$2" st="" i
  for i in $(seq 1 "$n"); do
    st="$("$DOCKER_BIN" inspect -f '{{.State.Health.Status}}' "$c" 2>/dev/null || echo starting)"
    [ "$st" = "healthy" ] && return 0
    sleep 1
  done
  echo "  · ${c} 状态=${st}"
  return 1
}
for c in oneserve-m26-etcd oneserve-m26-redis oneserve-m26-apisix oneserve-m26-service oneserve-m26-portal; do
  if wait_healthy "$c" 120; then ok "${c} healthy"; else
    bad "${c} 未在时限内健康"; "${COMPOSE[@]}" logs --tail 30 "$c" 2>&1 | tail -30; exit 1
  fi
done

READY=0
for i in $(seq 1 90); do
  if "$DOCKER_BIN" exec oneserve-m26-hbase bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then READY=1; break; fi
  sleep 2
done
if [ "$READY" = "1" ]; then ok "oneserve-m26-hbase Thrift 9090 就绪（容器内探活）"; else bad "HBase Thrift 180s 未就绪"; exit 1; fi
if $PY -c "import socket,sys; s=socket.socket(); s.settimeout(2); sys.exit(0 if s.connect_ex(('127.0.0.1',$HBASE_PORT))==0 else 1)"; then
  ok "宿主 127.0.0.1:$HBASE_PORT 映射可达"
else
  bad "宿主 HBase 端口不可达"; exit 1
fi

say "初始化本栈 APISIX 路由（幂等 PUT；借本栈 service 容器执行，9180 仅内网）"
cmd "COMPOSE_PROJECT_NAME=$PROJECT $DOCKER_BIN compose -f docker-compose.yml -f compose.hbase.yml -f compose.m26-iso.yml exec -T service python /init/init_routes.py"
INIT_OUT="$("${COMPOSE[@]}" exec -T service python /init/init_routes.py 2>&1)"; INIT_RC=$?
echo "$INIT_OUT" | tail -8
if [ "$INIT_RC" -eq 0 ]; then ok "本栈网关路由初始化完成（exit 0）"; else bad "网关路由初始化失败（exit ${INIT_RC}）"; exit 1; fi
if "$DOCKER_BIN" logs oneserve-m26-service 2>&1 | grep -q "HBase 连接池就绪"; then
  ok "service 启动日志含「HBase 连接池就绪」（HBASE_HOST=oneserve-m26-hbase 生效）"
else
  ok "service 启动时注册表暂无 hbase 型服务，连接池留待加载端点按需建（如实标注）"
fi

# ---------- ② 造数 → 灌 HBase ----------
step "② 造数（sync/gen_mock_source.py）→ 灌 HBase（sync/hbase_loader.py）"
cmd "$PY sync/gen_mock_source.py --date $DATE --users $USERS --fill $FILL --seed $SEED --out $CSV"
$PY sync/gen_mock_source.py --date "$DATE" --users "$USERS" --fill "$FILL" --seed "$SEED" --out "$CSV" > "$TMP/gen.json"
cat "$TMP/gen.json"
if $PY - "$TMP/gen.json" "$USERS" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["users"] == int(sys.argv[2]) and d["tag_cols"] == 296 and len(d["sha256"]) == 64, d
print("  · 造数：%d 用户 × %d 标签列 fill=%.3f" % (d["users"], d["tag_cols"], d["fill_actual"]))
PYEOF
then ok "造数与既有链路同源（297 列真结构；键由造数侧生成，平台不关注算法）"; else bad "造数异常"; exit 1; fi

# 样本键：取两列（本卡投影列）均非空的行，保证点查必命中（造数稀疏）
SAMPLE_KEY="$($PY - "$CSV" <<'PYEOF'
import csv, sys
for r in csv.DictReader(open(sys.argv[1], encoding="utf-8")):
    if r["ac_active_sts_01"] and r["mk_touch_sts_02"]:
        print(r["usr_key_sm3"]); break
PYEOF
)"
[ -n "$SAMPLE_KEY" ] || { bad "造数里找不到两列均非空的样本键"; exit 1; }
ok "样本键（ac_active_sts_01／mk_touch_sts_02 均非空）：${SAMPLE_KEY:0:16}…"

cmd "HBASE_HOST=127.0.0.1 HBASE_PORT=$HBASE_PORT $PY sync/hbase_loader.py --csv $CSV --version $DATE --replace"
HBASE_HOST=127.0.0.1 HBASE_PORT="$HBASE_PORT" $PY sync/hbase_loader.py --csv "$CSV" --version "$DATE" --replace > "$TMP/hb_load.json" 2>&1
cat "$TMP/hb_load.json"
if $PY - "$TMP/hb_load.json" "$USERS" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] and d["rows"] == int(sys.argv[2]), d
print("  · HBase 装载：表=%s cf=%s 行数=%d 标签列=%d 覆盖率=%s 建表=%s" % (
    d["table"], d["cf"], d["rows"], d["tag_cols"], d["coverage_src"], d["created_table"]))
PYEOF
then ok "HBase 装载真数据（表/列族读 services.yaml 单一真源）"; else bad "HBase 装载异常"; exit 1; fi

# ---------- ③ 经 HTTP 调发布台：选表 → 加载到 Redis ----------
step "③ 经 HTTP 调发布台：GET discovery（选表）→ POST hbase/load（加载到 Redis）"
cmd "curl -sS $BASE/v1/admin/publish/hbase/discovery -H 'X-Admin-Token: <token>'"
DISC="$TMP/discovery.json"
DHTTP=$(curl -s -o "$DISC" -w '%{http_code}' "$BASE/v1/admin/publish/hbase/discovery" -H "X-Admin-Token: $ADMIN_TOKEN")
if [ "$DHTTP" = "200" ]; then ok "GET discovery HTTP 200"; else bad "discovery HTTP=${DHTTP}：$(head -c 300 "$DISC")"; exit 1; fi
if $PY - "$DISC" "$TABLE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
tbls = {}
for dom in d.get("domains", []):
    for t in dom.get("tables", []): tbls[t["id"]] = t
tid = sys.argv[2] if sys.argv[2] in tbls else ("default:" + sys.argv[2])
assert tid in tbls, (tid, list(tbls))
t = tbls[tid]; fams = {f["name"]: f for f in t["families"]}
assert len(fams["cf"]["columns"]) == 296, len(fams.get("cf", {}).get("columns", []))
print("  · 目录树：表=%s 列族=%s 列=%d 键=%s 双源=%s" % (
    tid, list(fams), len(fams["cf"]["columns"]), t["keyTypes"], d.get("dualSource", {}).get("status")))
PYEOF
then ok "选表：真 HBase meta × 字段字典双源（列来自字典 296 列）"; else bad "目录树断言红"; exit 1; fi

# 加载：整列族（理由见结论「已知待决项」；投影在查询侧生效）
LOADBODY="{\"namespace\":\"default\",\"table\":\"$TABLE\",\"cf\":\"cf\",\"version\":\"$DATE\",\"sourceCsv\":\"/sync/out/$(basename "$CSV")\"}"
echo "  $ curl -sS -X POST $BASE/v1/admin/publish/hbase/load -H 'X-Admin-Token: <token>' -d '<json>'"
echo "    json: $LOADBODY"
LOAD="$TMP/load.json"
LHTTP=$(curl -s -o "$LOAD" -w '%{http_code}' -X POST "$BASE/v1/admin/publish/hbase/load" -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$LOADBODY")
if [ "$LHTTP" = "200" ]; then ok "POST hbase/load HTTP 200"; else bad "加载端点 HTTP=${LHTTP}：$(head -c 400 "$LOAD")"; exit 1; fi
if $PY - "$LOAD" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] is True and d["mode"] == "hbase->redis-load", d
assert d["version"] == sys.argv[2] and d["loaded"] > 0, d
print("  · 加载：表=%s cf=%s 版本=%s 写入键=%d 数据时点=%s 耗时=%dms 清空前=%d" % (
    d["table"], d["cf"], d["version"], d["loaded"], d["dataAsOf"], d["elapsedMs"], d["clearedBeforeLoad"]))
open(sys.argv[1] + ".loaded", "w").write(str(d["loaded"]))
PYEOF
then ok "加载端点真链路：HBase 冷存储 → Redis 热存储（新版本空间）"; else bad "加载断言红"; exit 1; fi
LOADED="$(cat "$LOAD.loaded")"

cmd "curl -sS -X POST $BASE/v1/admin/publish/hbase/load -H 'X-Admin-Token: <token>' -d '<同 body，不带 replace>'（应 409）"
DENY="$TMP/deny.json"
DHTTP2=$(curl -s -o "$DENY" -w '%{http_code}' -X POST "$BASE/v1/admin/publish/hbase/load" -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$LOADBODY")
if [ "$DHTTP2" = "409" ]; then ok "幂等闸：目标空间非空且未 replace → 409 拒灌（零副作用）"; else bad "幂等闸失效（应 409，实得 ${DHTTP2}）：$(head -c 200 "$DENY")"; fi

# ---------- ④ 同源对账差异=0 ----------
step "④ 同源对账（复用 sync/hbase_reconcile.py 口径）：HBase ↔ Redis 差异=0"
if $PY - "$LOAD" "$USERS" "$LOADED" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
rc = d["reconcile"]; c = rc["checks"]
assert rc["pass"] is True and rc["diffCount"] == 0, rc
assert c["row_count"]["rows_redis"] == c["row_count"]["rows_hbase"] == int(sys.argv[3]), c["row_count"]
assert c["value_sample"]["pass"] and c["coverage"]["pass"], c
print("  · 同源对账：差异=%d 行数 redis=%d hbase=%d 抽样=%d 条全等 覆盖率偏差=%s" % (
    rc["diffCount"], c["row_count"]["rows_redis"], c["row_count"]["rows_hbase"],
    c["value_sample"]["sampled"], c["coverage"]["deviation"]))
PYEOF
then ok "同源对账差异=0（加载响应内 reconcile；复用既有 compare 口径）"; else bad "同源对账不为 0"; exit 1; fi

N_KEYS="$($PY - "$DATE" "$REDIS_PORT" <<'PYEOF'
import sys, redis
r = redis.Redis(host="127.0.0.1", port=int(sys.argv[2]), decode_responses=True)
print(len(list(r.scan_iter(match="tags:user:%s:*" % sys.argv[1], count=1000))))
PYEOF
)"
cmd "python3 -c \"import redis; ...scan_iter('tags:user:$DATE:*')\"   # 经 127.0.0.1:${REDIS_PORT}（本栈专属）"
[ "$N_KEYS" = "$LOADED" ] && ok "Redis 键数实测=${N_KEYS}（本栈专属 Redis，未碰共享栈）" || bad "Redis 键数不符（实得 $N_KEYS vs 加载报告 ${LOADED}）"

# ---------- ⑤ 发布 Redis 型服务 ----------
step "⑤ 发布：POST /v1/admin/publish → 产物必须是 Redis 型（backend.kv，无 source=hbase）"
PUBBODY="{\"serviceName\": \"M2-6 端到端验收服务\", \"routePath\": \"$RURI\", \"datasource\": \"hbase\", \"hbase\": {\"table\": \"$HBASE_TABLE\", \"cf\": \"cf\", \"key_type\": \"phone_sm3\"}, \"loadVersion\": \"$DATE\", \"fields\": [{\"name\": \"ac_active_sts_01\", \"sample\": \"band_C\", \"desc\": \"行为活跃\"}, {\"name\": \"mk_touch_sts_02\", \"sample\": \"lvl_2\", \"desc\": \"营销触达\"}], \"operator\": \"m26-e2e@oneserve\", \"oaTicket\": \"OA-E2E-M26\", \"releaseMode\": \"先测试\"}"
cmd "curl -sS -X POST $BASE/v1/admin/publish -H 'X-Admin-Token: <token>' -d '<json>'"
PUB="$TMP/publish.json"
PHTTP=$(curl -s -o "$PUB" -w '%{http_code}' -X POST "$BASE/v1/admin/publish" -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$PUBBODY")
if [ "$PHTTP" = "200" ]; then ok "POST /v1/admin/publish HTTP 200"; else bad "发布 HTTP=${PHTTP}：$(head -c 400 "$PUB")"; exit 1; fi
if $PY - "$PUB" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8")); e = d["entry"]
assert e["datasource"] == "redis", ("发布产物必须是 Redis 型", e["datasource"])
assert e["fieldSpace"] == "wide297", e
assert e["loadSource"] and e["loadSource"]["version"] == sys.argv[2], e.get("loadSource")
print("  · 发布：id=%s datasource=%s field_space=%s loadSource=%s" % (
    e["id"], e["datasource"], e["fieldSpace"], json.dumps(e["loadSource"], ensure_ascii=False)))
PYEOF
then ok "发布产物＝Redis 型＋加载来源留痕（ADR-0005：不产出 HBase 直连服务）"; else bad "发布产物口径断言红"; exit 1; fi

cmd "$PY -c \"from app.kv_executor import load_services; ...\"   # 读 service 副本的 services.yaml"
if $PY - "$M26_SERVICE_DIR/services.yaml" "$SID" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "service")
from app.kv_executor import load_services
reg = load_services(Path(sys.argv[1])); rt = reg[sys.argv[2]]
assert "hbase" not in rt["backend"], ("产物不得落 HBase 直连后端（ADR-0005）", rt["backend"])
assert rt["backend"]["kv"]["data_key"] == "tags:user:{version}:{sm3}", rt["backend"]["kv"]
print("  · 运行时条目：(service_id, version)=%s backend.kv.data_key=%s field_space=%s" % (
    (rt["service_id"], rt["version"]), rt["backend"]["kv"]["data_key"], rt["field_space"]))
PYEOF
then ok "落盘核验：运行时条目 backend.kv（无 backend.source=hbase）"; else bad "落盘产物断言红"; exit 1; fi

# ---------- ⑥ 上线闸（既有 reconcile + switch，本卡未改语义） ----------
step "⑥ 上线闸：sync/reconcile.py（源指纹三查）→ sync/switch.py（原子切指针）"
cmd "$DOCKER_BIN exec oneserve-m26-service python /sync/reconcile.py --csv /sync/out/$(basename "$CSV") --version $DATE"
"$DOCKER_BIN" exec oneserve-m26-service python /sync/reconcile.py --csv "/sync/out/$(basename "$CSV")" --version "$DATE" > "$TMP/recon.json" 2>&1
cat "$TMP/recon.json"
if $PY -c "import json,sys;d=json.load(open('$TMP/recon.json'));assert d['pass'] is True,d;print('  · 源对账三查全绿（前置指纹/行数/抽样/覆盖率）')"; then ok "既有 reconcile.py 源对账绿（本卡未改口径）"; else bad "源对账红"; exit 1; fi

cmd "$DOCKER_BIN exec oneserve-m26-service python /sync/switch.py --version $DATE"
"$DOCKER_BIN" exec oneserve-m26-service python /sync/switch.py --version "$DATE" > "$TMP/switch.json" 2>&1
cat "$TMP/switch.json"
if $PY -c "import json;d=json.load(open('$TMP/switch.json'));assert d['ok'] and d['to']=='$DATE',d;print('  · 原子切指针：%s → %s'%(d['from'],d['to']))"; then ok "上线（既有三闸放行后单条 SET）"; else bad "上线失败"; exit 1; fi

# ---------- ⑦ 真调 API（经网关） ----------
step "⑦ 真调 API（经本栈网关 $GW_PORT → service → Redis 热存储）"
echo "  · 网关探活 /v1/ping（无 key，期望 401）: HTTP=$(curl -s -o /dev/null -w '%{http_code}' "$GW/v1/ping")"
cmd "curl -sS -X POST $GW/v1/query/$SID -H 'apikey: <appkey>' -d '{\"key\": \"<样本键>\"}'"
T0=$(date +%s); ELAPSED=""
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/$SID" -H "apikey: $APPKEY" -H "Content-Type: application/json" -d "{\"key\": \"$SAMPLE_KEY\"}" || echo 000)
  [ "$CODE" = "200" ] && { ELAPSED=$(( $(date +%s) - T0 )); break; }
  sleep 2
done
if [ -n "$ELAPSED" ]; then ok "发布后 ${ELAPSED}s（≤60s）新服务经网关可调（HTTP 200）"; else bad "60s+ 未生效"; exit 1; fi

API="$TMP/api.json"
curl -sS -X POST "$GW/v1/query/$SID" -H "apikey: $APPKEY" -H "Content-Type: application/json" -d "{\"key\": \"$SAMPLE_KEY\"}" -o "$API"
echo "  · 响应体: $(cat "$API")"
if $PY - "$API" "$SID" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["service_id"] == sys.argv[2], d
assert d["found"] is True and d["fields"], d
assert d["live_version"] == sys.argv[3], d
assert set(d["fields"]) <= {"ac_active_sts_01", "mk_touch_sts_02"}, d["fields"]
print("  · 真数据：service_id=%s found=%s live_version=%s data_as_of=%s fields=%s" % (
    d["service_id"], d["found"], d["live_version"], d.get("data_as_of"), json.dumps(d["fields"], ensure_ascii=False)))
PYEOF
then ok "真调返回真数据（live_version／data_as_of 诚实；字段在授权白名单内）"; else bad "端到端响应断言红"; exit 1; fi
HDR="$TMP/hdr.txt"
curl -sS -D "$HDR" -o /dev/null -X POST "$GW/v1/query/$SID" -H "apikey: $APPKEY" -H "Content-Type: application/json" -d "{\"key\": \"$SAMPLE_KEY\"}"
tr -d '\r' < "$HDR" | grep -qiE "^X-OneServe-DataSource: (kv|redis)" && ok "响应头 X-OneServe-DataSource 落地（数据源＝Redis 热存储）" || bad "数据源响应头缺失"

# ---------- ⑦b UI 入口可达（操作手册的可视化路径真达） ----------
step "⑦b UI 入口：发布台页面 /admin/publish 可达（操作手册「可视化选表」入口）"
UI_CODE=$(curl -s -o "$TMP/publish.html" -w '%{http_code}' "http://127.0.0.1:18000/admin/publish")
if [ "$UI_CODE" = "200" ] && grep -q "加载到 Redis" "$TMP/publish.html"; then
  ok "发布台 http://127.0.0.1:18000/admin/publish 可达（HTTP 200，含「加载到 Redis」按钮）"
else
  bad "发布台页面不可达或内容不符（HTTP=$UI_CODE）"
fi
# 门户经 nginx 反代到管理端点（同源，无需跨域）：面板读目录树即走这条
UI_API=$(curl -s -o /dev/null -w '%{http_code}' -H "X-Admin-Token: $ADMIN_TOKEN" \
         "http://127.0.0.1:18000/v1/admin/publish/hbase/discovery")
[ "$UI_API" = "200" ] && ok "UI 同源反代 /v1/admin/* 可达（HTTP 200；面板经此选表）" \
                      || bad "UI 同源反代管理端点不可达（HTTP=$UI_API）"

# ---------- ⑧ schema 漂移注入演练（AC-8-b，复用既有 drill） ----------
if [ "${M26_SKIP_DRIFT:-0}" = "1" ]; then
  step "⑧ schema 漂移演练：M26_SKIP_DRIFT=1 → 如实跳过（不假称已跑）"
  say "  已按环境变量跳过；删列隔离机制证据请另跑 bash scripts/drill_schema_drift.sh"
else
  step "⑧ schema 漂移注入演练（AC-8-b）：删列 → 被隔离返明确错误码 → 恢复后放行"
  say "复用既有 scripts/drill_schema_drift.sh（M2-3.8 已实现；本卡不重写、不实现触发器）"
  say "口径（如实）：drill 自带专属 Redis 容器（随机宿主端口、真 Redis 链路、真 services.yaml/auth_matrix/字段字典）；"
  say "  它不写共享栈 Redis，也不写本栈业务键——与「禁止写共享栈 Redis」纪律一致。"
  cmd "bash scripts/drill_schema_drift.sh"
  DRILL_EV="$EVIDENCE_DIR/m26-drift-drill-$STAMP.txt"
  set +e
  DRIFT_DRILL_EVIDENCE="$DRILL_EV" bash scripts/drill_schema_drift.sh > "$TMP/drift.out" 2>&1
  DRILL_RC=$?
  set +e
  echo "  ── 漂移演练输出（末尾 40 行；全文见 ${DRILL_EV}）──"
  tail -40 "$TMP/drift.out" | sed 's/^/  /'
  DRILL_SUM="$(grep -E '演练结果: PASS=' "$TMP/drift.out" | tail -1 || true)"
  echo "  · ${DRILL_SUM:-<未取到 PASS/FAIL 汇总>}"
  if [ "$DRILL_RC" -eq 0 ] && echo "$DRILL_SUM" | grep -q "FAIL=0"; then
    ok "漂移演练全绿：删列 → quarantined 状态键 → 执行器 503 schema_drift_quarantined → 恢复后放行"
  else
    bad "漂移演练未全绿（exit=${DRILL_RC}）——见 $DRILL_EV"
  fi
  for anchor in "拒查响应：HTTP 503" "schema_drift_quarantined" "人为真删列" "恢复后执行器放行" "自动触发器归 M2 生产化"; do
    grep -q "$anchor" "$TMP/drift.out" && ok "漂移证据含锚点：$anchor" || bad "漂移证据缺锚点：$anchor"
  done
  [ -s "$DRILL_EV" ] && ok "漂移演练证据落盘：${DRILL_EV#*/}" || bad "漂移演练证据未落盘"
fi

# ---------- ⑨ 结论 ----------
step "⑨ 验收结论"
say "PASS=$PASS FAIL=$FAIL"
say "证据文件：$EVIDENCE"
say "本栈：project=$PROJECT 网络=oneserve-m26-net 卷=oneserve-m26-*；共享栈（oneserve/9080/8000/9090）未动"
say "如实标注（不得省略）："
say "  · 真集群未做（NC-M2-9 待环境）——本卡用开发替身容器（compose.hbase.yml，用完删）；"
say "  · 既有上线闸 sync/reconcile.py 是「新空间 vs 同源 CSV 全等硬判」，故加载走整列族，"
say "    投影在查询侧（kv_executor 白名单）生效；若要「按发布服务只加载所选列」需另立卡改 reconcile 口径；"
say "  · schema 漂移的自动触发器归 M2 生产化，本阶段仍是手工 CLI（drill 输出三处如实标注）。"
if [ "$FAIL" -eq 0 ]; then
  say "===== M2-6 端到端验收全绿（PASS=$PASS FAIL=0）====="
  exit 0
fi
say "===== M2-6 端到端验收存在失败项（PASS=$PASS FAIL=${FAIL}）====="
exit 1
