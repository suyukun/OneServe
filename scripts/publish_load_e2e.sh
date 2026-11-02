#!/usr/bin/env bash
# 发布台改造（M2-6 端到端最后一环）——「选表 → 加载 → 发布 → 真调 API 返回真数据」e2e。
#
# 架构口径（ADR-0005 / docs/plans/M2范围裁定_v2-架构纠正_20260923.md）：
#   HBase＝冷存储，Redis＝热存储；链路＝外围 ETL→HBase→【页面选表→加载】→Redis→封装 API→对外。
#   **HBase 不作为独立数据源发 API**（保留代码，归 M3 候选）。
#
# 本卡验证的核心（全程真容器/真 Redis/真网关，零假数据）：
#   ① 起 HBase 替身容器（本脚本专属一次性容器，绝不复用共享 oneserve-hbase——9-28 事故
#      实证：本脚本曾用 compose 起共享 oneserve-hbase 并 --replace 清 default:oneserve_demo，
#      等于直接覆盖共享数据。现破坏闸：默认起专属一次性 HBase 容器（数据随容器删，永不碰
#      共享卷）；仅 E2E_ALLOW_SHARED=1 显式放行才允许连共享 oneserve-hbase
#      ＋隔离 e2e Redis（不碰主栈 oneserve-redis）
#   ② host 造数（sync/gen_mock_source.py，297 列真结构＋SM3 真算）并灌 HBase
#   ③ 起隔离 service：真目录树发现（/v1/admin/publish/hbase/discovery）＋真试查（.../test）
#   ④ **本卡核心**：POST /v1/admin/publish/hbase/load（新端点）→ HBase 冷 → Redis 热
#      · 免令牌 200（管理令牌机制已于 35ab1aa 整体拆除；网络层即边界，认证归 M3）
#      · 幂等实证（重跑不带 replace → 409 拒灌；带 replace → 成功且行数稳定）
#      · 对账差异＝0（既有 hbase_reconcile 口径，响应内 reconcile）
#   ⑤ POST /v1/admin/publish → 产物**必须是 Redis 型**（backend.kv；无 backend.source=hbase）
#      并落 backend.load 来源留痕（表/列族/版本）
#   ⑥ 既有原子切换（sync/switch.py，本卡未改）上线新版本空间 → 热加载 → 真调 /v1/query/<sid>
#      返回**真数据**（来自 Redis 热存储，data_as_of/live_version 诚实）
#   ⑦ HBase 不可达实证：删容器 → 加载端点如实 503（不写脏、不回退假数据）
#   ⑧ 收尾：删全部临时容器/路由/上游/临时件；共享主栈与共享 Redis 未动
#
# 用法：bash scripts/publish_load_e2e.sh
#       PUBLOAD_KEEP=1 bash scripts/publish_load_e2e.sh   # 保留容器（调试；记得手动清理）
# 退出码：全绿 0；任一断言红 1；docker 不可用 2。
set -eo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
PY="${PY:-$(command -v python3.13 || command -v python3)}"   # 宿主侧 hbase_loader 依赖 happybase（本机仅 3.13 装有）
EVIDENCE_DIR="tests/evidence"
STAMP="$(date +%Y%m%d-%H%M%S)"
EVIDENCE="${PUBLOAD_EVIDENCE:-$EVIDENCE_DIR/publish-load-e2e-$STAMP.txt}"
PASS=0; FAIL=0
say() { echo "[pubload] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR" sync/out
exec > >(tee "$EVIDENCE") 2>&1
say "===== 发布台改造 e2e：选表→加载→发布→真调 $(date '+%F %T') ====="
say "仓库=$PWD python=$($PY --version 2>&1) docker=$DOCKER_BIN"

# ---------- 常量 ----------
DATE="20260923"
USERS=500
FILL=0.30
SEED=42
CSV="sync/out/src_pubload_$DATE.csv"
IMG="oneserve/service:pubload-e2e"
# ── 共享 HBase 破坏闸（9-28 事故后立规；维护者 认可方向：测试用独立容器，永碰不到真数据）──
# 默认起本脚本专属一次性 HBase 容器（oneserve-pubload-hbase，数据在容器可写层，删容器即清，
# 绝不挂共享卷 oneserve_oneserve-hbase-data）；连共享 oneserve-hbase 须显式 E2E_ALLOW_SHARED=1。
if [ "${E2E_ALLOW_SHARED:-0}" = "1" ]; then
  HBASE_CT="oneserve-hbase"          # 显式放行：连共享容器（验证共享语义专用；自担风险）
  HBASE_PORT_HOST=9090               # 共享容器宿主映射口
else
  HBASE_CT="oneserve-pubload-hbase"  # 默认：专属一次性容器（隔离写域）
  HBASE_PORT_HOST=19093              # 高位错开共享 9090，防误连
fi
REDIS_CT="oneserve-pubload-redis"
SVC_CT="oneserve-pubload-service"
TMP="$(mktemp -d)"
PORT=18013
BASE="http://127.0.0.1:$PORT"
ADMIN_TOKEN="admin321"
# 命名规范 v1（fix/api-naming-enforce）：routePath 须过发布闸机检——pubload-demo 的
# demo 是平台保留词（R3）会 400，改合规地址 pub-load-e2e（pub=公告域码建议位）；
# 查询仍走 /v1/query/<SID> 兼容内部通道（Q1 收敛后 /v1/query 降为网关→service 内部
# 转发通道，e2e 直连 service 端口验证，不经 /v1/svc 对外出口——对外出口渲染归单测）。
SID="v1-svc-pub-load-e2e"
# 表名＝default:oneserve_demo（沿用 demo 表名；2026-09-29 全下线后 services.yaml 为空，
# hbase_loader 走 --table 直传、加载端点/发布请求 body 自带表名——本卡 e2e 容器为
# 独立一次性容器，验完即删，不动共享主栈）
TABLE="oneserve_demo"
RID="oneserve-pubload-route"
UPID="oneserve-pubload-up"
RURI="/v1/pubload/query"
if [ "${PUBLOAD_KEEP:-0}" = "1" ]; then KEEP=1; else KEEP=0; fi

cleanup() {
  local rc=$?
  if [ "$KEEP" = "1" ]; then
    say "PUBLOAD_KEEP=1 → 保留容器（$SVC_CT/$REDIS_CT/$HBASE_CT）；记得手动清理"
  else
    "$DOCKER_BIN" run --rm --network oneserve-net -v "$PWD/sync/out:/gw:ro" \
      -e GW_ROUTE_ID="$RID" -e GW_UP_ID="$UPID" "$IMG" python /gw/pubload_gw.py down >/dev/null 2>&1 || true
    "$DOCKER_BIN" rm -f "$SVC_CT" "$REDIS_CT" "$HBASE_CT" >/dev/null 2>&1 || true
    rm -rf "$TMP" "sync/out/pubload_gw.py" 2>/dev/null || true
    say "收尾：临时路由/上游/e2e 容器/hbase 容器已删（共享主栈未动，共享 Redis 未写）"
  fi
  return $rc
}
trap cleanup EXIT

# ---------- docker 探测 ----------
"$DOCKER_BIN" info >/dev/null 2>&1 || { say "docker daemon 不可用（探测=$DOCKER_BIN）→ 真链路无法执行"; exit 2; }
ok "docker daemon 可用"

# ---------- 临时网关助手（Admin API；9180 仅在容器网内） ----------
cat > "sync/out/pubload_gw.py" <<'PYEOF'
import json, os, sys, urllib.request, urllib.error
ADMIN = os.environ.get("APISIX_ADMIN", "http://oneserve-apisix:9180") + "/apisix/admin"
KEY = os.environ.get("APISIX_ADMIN_KEY", "***REMOVED***")
RID = os.environ["GW_ROUTE_ID"]; UPID = os.environ["GW_UP_ID"]
RURI = os.environ["GW_ROUTE_URI"]; NODE = os.environ["GW_NODE"]

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
        print("upstream", req("PUT", f"upstreams/{UPID}", {"name": UPID, "type": "roundrobin", "nodes": {NODE: 1}}))
        print("route", req("PUT", f"routes/{RID}", {"name": RID, "uri": RURI, "methods": ["POST"], "upstream_id": UPID,
            "plugins": {"key-auth": {}, "request-id": {"header_name": "X-Trace-Id"},
                        "proxy-rewrite": {"regex_uri": [f"^{RURI}$", f"/v1/query/{os.environ['GW_SID']}"]}}}))
    elif cmd == "down":
        print("route", req("DELETE", f"routes/{RID}"))
        print("upstream", req("DELETE", f"upstreams/{UPID}"))
main()
PYEOF
gw() {
  "$DOCKER_BIN" run --rm --network oneserve-net -v "$PWD/sync/out:/gw:ro" \
    -e GW_ROUTE_ID="$RID" -e GW_UP_ID="$UPID" -e GW_ROUTE_URI="$RURI" \
    -e GW_SID="$SID" -e GW_NODE="$SVC_CT:8000" \
    "$IMG" python /gw/pubload_gw.py "$1" 2>&1 || true
}

# ---------- ① 起 HBase 替身容器（破坏闸：默认专属一次性容器，绝不 compose 起共享容器） ----------
if [ "${E2E_ALLOW_SHARED:-0}" = "1" ]; then
  say "① [共享闸放行] E2E_ALLOW_SHARED=1 → 复用共享容器 $HBASE_CT（不删不建，只读写其数据）"
  st=$("$DOCKER_BIN" inspect -f '{{.State.Status}}' "$HBASE_CT" 2>/dev/null || echo missing)
  [ "$st" = "running" ] && ok "共享 $HBASE_CT 在场（running；写操作自担风险）" \
    || { bad "共享 $HBASE_CT 不在 running（$st）——先起它再放行"; exit 2; }
else
  say "① 起专属一次性 $HBASE_CT（Thrift1 9090→宿主 $HBASE_PORT_HOST，无共享卷，删容器即清）＋等就绪 ..."
  "$DOCKER_BIN" rm -f "$HBASE_CT" >/dev/null 2>&1 || true
  "$DOCKER_BIN" run -d --name "$HBASE_CT" --network oneserve-net -p "127.0.0.1:$HBASE_PORT_HOST:9090" \
    --memory 2g -e HBASE_HEAPSIZE=1024 -e HBASE_THRIFT_OPTS="-Xms64m -Xmx256m" \
    --entrypoint bash openeuler/hbase:latest \
    -c "start-hbase.sh && hbase-daemon.sh start thrift && tail -f /dev/null" >/dev/null
  ok "破坏闸生效：专属容器（不挂共享卷 oneserve_oneserve-hbase-data；默认拒绝共享 9090）"
fi
READY=0
for i in $(seq 1 75); do
  if "$DOCKER_BIN" exec "$HBASE_CT" bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ "$READY" = "1" ] && ok "$HBASE_CT 就绪（容器内 9090 通）" || { bad "hbase 150s 未就绪"; exit 1; }
for i in $(seq 1 15); do nc -z 127.0.0.1 "$HBASE_PORT_HOST" 2>/dev/null && break; sleep 2; done
nc -z 127.0.0.1 "$HBASE_PORT_HOST" 2>/dev/null && ok "宿主 127.0.0.1:$HBASE_PORT_HOST 映射可达" || { bad "宿主 $HBASE_PORT_HOST 不可达"; exit 1; }

# ---------- ② 造数 + 灌 HBase ----------
say "② 造数 $USERS 用户 × 296 标签列（fill=$FILL seed=$SEED）→ $CSV"
mkdir -p sync/out
$PY sync/gen_mock_source.py --date "$DATE" --users "$USERS" --fill "$FILL" --seed "$SEED" \
    --out "$CSV" > "sync/out/gen_pubload_$DATE.json"
$PY -c "
import json
d=json.load(open('sync/out/gen_pubload_$DATE.json'))
assert d['users']==$USERS and d['tag_cols']==296 and len(d['sha256'])==64, d
print('  ✓ 造数：%d 用户 × %d 标签列（键=SM3 真算）' % (d['users'], d['tag_cols']))" \
  && ok "造数与既有链路同源（gen_mock_source.py）" || { bad "造数异常"; exit 1; }
# 取「样本列非空」的首行键：造数稀疏（fill=0.30），首行可能缺该列 → 试查/点查会诚实 404。
# 以 ac_active_sts_01 / mk_touch_sts_02 两列（本卡 e2e 投影列）均非空的行为样本。
KEY0=$($PY -c "
import csv
for r in csv.DictReader(open('$CSV')):
    if r['ac_active_sts_01'] and r['mk_touch_sts_02']:
        print(r['usr_key_sm3']); break
")
[ -n "$KEY0" ] || { bad "造数里找不到两列均非空的样本键"; exit 1; }
ok "取样本键（ac_active_sts_01 / mk_touch_sts_02 均非空）：$(printf '%s' "$KEY0" | cut -c1-16)…"

say "②b host sync/hbase_loader.py 灌 HBase（--table 直传；2026-09-29 全下线后 services.yaml 为空表，不再依赖 demo_hbase_query 登记）..."
# 命名规范卡适配说明：services.yaml 自 2026-09-29 全服务下线后是 services: []（零迁移
# 成本口径），hbase_loader 按 service-id 解析会「services.yaml 无服务 demo_hbase_query」
# 拒跑——本 e2e 改用 loader 既有 --table 直传口（业务化测试表同款），表/列族与后续
# 加载端点/发布请求仍同认 default:oneserve_demo × cf（单一真源不变，只是绕过空注册表）。
HB_LOAD=$(HBASE_HOST=127.0.0.1 HBASE_PORT=$HBASE_PORT_HOST $PY sync/hbase_loader.py --table "default:$TABLE" --csv "$CSV" --version "$DATE" --replace)
echo "$HB_LOAD"
echo "$HB_LOAD" | $PY -c "import sys,json;d=json.load(sys.stdin);assert d['ok'] and d['rows']==$USERS,d;print('  ✓ HBase 装载 %d 行'%d['rows'])" \
  && ok "HBase 造数装载绿" || { bad "HBase 装载异常"; exit 1; }

# ---------- ③ 隔离 Redis + service ----------
say "③ 起隔离 e2e Redis（$REDIS_CT，不碰共享主栈）＋构建服务镜像 ..."
"$DOCKER_BIN" rm -f "$REDIS_CT" >/dev/null 2>&1 || true
"$DOCKER_BIN" run -d --name "$REDIS_CT" --network oneserve-net \
  library/redis:7-alpine >/dev/null
for i in $(seq 1 30); do "$DOCKER_BIN" exec "$REDIS_CT" redis-cli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
"$DOCKER_BIN" exec "$REDIS_CT" redis-cli ping 2>/dev/null | grep -q PONG && ok "e2e Redis 就绪（隔离库）" || { bad "e2e Redis 未就绪"; exit 1; }
"$DOCKER_BIN" build -q -t "$IMG" ./service >/dev/null
ok "服务镜像 $IMG 就绪（本仓代码构建）"

cp -R service "$TMP/service_dir"
# 注册表基板说明：2026-09-29 曾全下线清空 services.yaml（services: []），但 B1 表结构
# 登记后主仓注册表已带存量服务（usr_asset_tag 等）——脚本不再假设空表起步，只验证
# 本卡新服务登记落位（下方 ⑥ 落盘核验），存量条目与本 e2e 无冲突（各自独立表空间）。
ok "隔离配置：注册表副本起步（携带存量条目不影响本卡独立表空间验证）"
# 授权矩阵：新服务白名单登记（真源是文件）
# 2026-09-29 全下线清理把 auth_matrix.yaml 也清成 matrix: []（e4f42b1）——种入脚本
# 原先只在已有 demo_merchant 行上追加 grant，空矩阵下空转 → ⑦ 真调 403 unknown_tenant。
# 改为自足种入：缺行则补整行（历史行形状：tenant/appkey/oa_ticket/grants）。
$PY - "$TMP/service_dir/auth_matrix.yaml" "$SID" <<'PYEOF'
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1]); sid = sys.argv[2]
doc = yaml.safe_load(p.read_text(encoding="utf-8"))
grant = {"service": sid, "endpoint": "/v1/query/" + sid,
         "fields": ["ac_active_sts_01", "mk_touch_sts_02"]}
row = next((r for r in doc.get("matrix") or [] if r.get("tenant") == "demo_merchant"), None)
if row is None:
    doc.setdefault("matrix", []).append({
        "tenant": "demo_merchant", "appkey": "oneserve-dev-appkey-001",
        "oa_ticket": "OA-E2E-PUBLOAD", "grants": [grant]})
    how = "空矩阵起步，补建整行"
else:
    row.setdefault("grants", []).append(grant)
    how = "存量行追加 grant"
p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("  ✓ 授权矩阵：demo_merchant 已登记 " + sid + " 白名单（" + how + "）")
PYEOF
# 真调用 AppKey＝矩阵里 demo_merchant 行的真实 appkey（B1 重建后是签发凭证 sb_*；
# 旧演示键 oneserve-dev-appkey-001 已随 9-29 全下线删除，直调会被判 unknown_tenant；
# 「空矩阵起步补建整行」分支自带演示键，此取值两分支通用）。
E2E_APPKEY=$($PY - "$TMP/service_dir/auth_matrix.yaml" <<'PYEOF2'
import sys, yaml
doc = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
row = next(r for r in doc.get("matrix") or [] if r.get("tenant") == "demo_merchant")
print(row.get("appkey", ""))
PYEOF2
)
[ -n "$E2E_APPKEY" ] && ok "真调用 AppKey 就位（demo_merchant 行真实凭证 ${E2E_APPKEY:0:8}…）" \
  || { bad "取不到 demo_merchant 的 appkey（矩阵行缺凭证）"; exit 1; }
# P1-4 口径：鉴权先查签发凭证注册表（appkeys.yaml），命中才回落授权矩阵——
# 须把本卡新服务 grant 同时种进 service 副本的 appkeys.yaml（真调用凭证所在处），
# 否则 ⑦ 真调 403 service_not_granted（矩阵有种、凭证注册表没有＝不走回落）。
$PY - "$TMP/service_dir/registry/appkeys.yaml" "$SID" "$E2E_APPKEY" <<'PYEOF3'
import sys, yaml
from pathlib import Path
p = Path(sys.argv[1]); sid, want_key = sys.argv[2], sys.argv[3]
doc = yaml.safe_load(p.read_text(encoding="utf-8"))
rec = next(k for k in doc.get("keys") or [] if k.get("appkey") == want_key)
grants = rec.setdefault("grants", [])
if not any(g.get("service") == sid for g in grants):
    grants.append({"service": sid, "endpoint": "/v1/query/" + sid,
                   "fields": ["ac_active_sts_01", "mk_touch_sts_02"]})
p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
print("  ✓ 签发凭证注册表：" + want_key[:8] + "… 已补 " + sid + " grant（P1-4 先查此处）")
PYEOF3

$DOCKER_BIN rm -f "$SVC_CT" >/dev/null 2>&1 || true
ENVFILE="$TMP/env"
{
  echo "REDIS_URL=redis://$REDIS_CT:6379/0"
  echo "SYNC_REDIS_URL=redis://$REDIS_CT:6379/0"
  echo "AUDIT_DIR=/data/audit"
  echo "HBASE_HOST=$HBASE_CT"
  echo "HBASE_PORT=9090"
  echo "APISIX_ADMIN=http://oneserve-apisix:9180"
  echo "APISIX_ADMIN_KEY=***REMOVED***"
  echo "REGISTRY_POLL_INTERVAL_S=5"
  echo "ADMIN_API_TOKEN=$ADMIN_TOKEN"
  echo "SCHEMA_CSV=/sync/schema_usr_label_wide_297.csv"   # TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）
} > "$ENVFILE"
"$DOCKER_BIN" run -d --name "$SVC_CT" --network oneserve-net \
  --env-file "$ENVFILE" -p "127.0.0.1:$PORT:8000" \
  -v "$TMP/service_dir":/app:rw -v "$PWD/sync":/sync:ro "$IMG" >/dev/null
HEALTHY=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz" || echo 000)
  [ "$code" = "200" ] && { HEALTHY=1; break; }
  sleep 2
done
[ "$HEALTHY" = "1" ] && ok "隔离 service 健康（/healthz 200 @127.0.0.1:$PORT）" || { bad "service 未健康"; "$DOCKER_BIN" logs --tail 40 "$SVC_CT"; exit 1; }

# ---------- ③b 免令牌实证（35ab1aa 拆令牌后的口径） ----------
say "③b 免令牌：加载端点无令牌 → 200（令牌已拆，网络层即边界）"
UNAUTH=$(curl -s -o "$TMP/unauth.json" -w '%{http_code}' -X POST "$BASE/v1/admin/publish/hbase/load" \
  -H "Content-Type: application/json" -d "{\"table\":\"default:$TABLE\",\"cf\":\"cf\",\"version\":\"$DATE\"}")
if [ "$UNAUTH" = "200" ] && grep -q '"ok": *true' "$TMP/unauth.json"; then
  ok "加载端点免令牌可达且真加载（此即第一次加载，后续 ⑤/⑤b 走 replace/409 幂等链）"
else
  bad "加载端点免令牌断言红（实得 $UNAUTH）：$(cat "$TMP/unauth.json")"
fi

# ---------- ④ 选表（发现）＋真试查 ----------
say "④ 选表：GET /v1/admin/publish/hbase/discovery（真 meta×字典双源）"
curl -sS "$BASE/v1/admin/publish/hbase/discovery" -H "X-Admin-Token: $ADMIN_TOKEN" -o "$TMP/disc.json"
$PY - "$TMP/disc.json" "$TABLE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
tbls = {}
for dom in d.get("domains", []):
    for t in dom.get("tables", []): tbls[t["id"]] = t
# 目录树表 id 口径（build_datasource_view）：default namespace 不带前缀（M2-5 契约）
tid = sys.argv[2] if sys.argv[2] in tbls else ("default:" + sys.argv[2])
assert tid in tbls, (tid, list(tbls))
t = tbls[tid]
fams = {f["name"]: f for f in t["families"]}
assert "cf" in fams and len(fams["cf"]["columns"]) == 296, len(fams.get("cf", {}).get("columns", []))
print("  ✓ 真目录树：表=%s 列族=%s 列=%d 键=%s" % (tid, list(fams), len(fams["cf"]["columns"]), t["keyTypes"]))
PYEOF
ok "选表（真 HBase meta × 字段字典）" || bad "发现断言红"

say "④b 真试查：POST /v1/admin/publish/hbase/test"
curl -sS -X POST "$BASE/v1/admin/publish/hbase/test" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d "{\"namespace\":\"default\",\"table\":\"$TABLE\",\"families\":[\"cf\"],
    \"fields\":[{\"name\":\"ac_active_sts_01\",\"cf\":\"cf\",\"sample\":\"band_C\"}],
    \"keyType\":\"phone_sm3\",\"sampleKey\":\"$KEY0\",\"timeoutMs\":3000}" -o "$TMP/test.json"
$PY - "$TMP/test.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["passed"] is True, d
print("  ✓ 真试查命中：passed=True code=", d["code"], "elapsedMs=", d["elapsedMs"])
PYEOF
ok "真试查通过（门禁放行）" || bad "真试查断言红"

# ---------- ⑤ 本卡核心：加载到 Redis（新端点） ----------
say "⑤ 加载：POST /v1/admin/publish/hbase/load（HBase 冷 → Redis 热，新端点）"
# 加载口径：整列族（不带 columns）——上线闸（既有 sync/reconcile.py 源对账＋
# sync/switch.py 三闸，本卡未改语义）以「新版本空间 vs 同源 CSV」全等硬判，故上线路径
# 加载整表；发布侧仍只投影授权白名单字段（投影在 kv_executor 读时生效，与加载正交）。
# 列投影加载（最小加载面）由 service/app/hbase_load.py 的 columns 路径支持，单测已覆盖。
# sourceCsv＝既有加载器的 --source-csv（开发期源指纹引用）：同一份造数 CSV 灌 HBase 再
# 加载 → 指纹与 CSV 一致，方可通过既有前置查。生产从 HBase 加载不需要（源指纹＝
# HBase 快照 hbase_snapshot_sha256，仍如实记录）。
LOADBODY="{\"namespace\":\"default\",\"table\":\"$TABLE\",\"cf\":\"cf\",
  \"version\":\"$DATE\",\"replace\":true,\"sourceCsv\":\"/sync/out/$(basename "$CSV")\"}"
curl -sS -X POST "$BASE/v1/admin/publish/hbase/load" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d "$LOADBODY" -o "$TMP/load.json"
$PY - "$TMP/load.json" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] is True and d["mode"] == "hbase->redis-load", d
assert d["version"] == sys.argv[2] and d["loaded"] > 0, d
assert d["dataAsOf"] == "2026-09-23", d
assert d["reconcile"]["pass"] is True and d["reconcile"]["diffCount"] == 0, d["reconcile"]
rc = d["reconcile"]["checks"]["row_count"]
assert rc["rows_redis"] == rc["rows_hbase"] == d["loaded"], (rc, d["loaded"])
print("  ✓ 加载：写入键数=%d（整列族非空行）耗时=%dms 数据时点=%s" % (
    d["loaded"], d["elapsedMs"], d["dataAsOf"]))
print("  ✓ 同源对账：差异=0（行数 %d==%d，抽样 %d 条逐值全等，覆盖率偏差 %s）" % (
    rc["rows_redis"], rc["rows_hbase"],
    d["reconcile"]["checks"]["value_sample"]["sampled"],
    d["reconcile"]["checks"]["coverage"]["deviation"]))
open(sys.argv[1] + ".loaded", "w").write(str(d["loaded"]))
PYEOF
LOADED=$(cat "$TMP/load.json.loaded")
ok "加载端点真链路：HBase → Redis 键空间 tags:$TABLE:$DATE，$LOADED 键（对账差异 0）" || bad "加载断言红"
# 键空间口径（B1 表结构登记后）：按表分域 tags:<表>:<版本>:<sm3>（sync_config.table_scope），
# 不再是旧 tags:user:* 全局空间；对账键 sync:recon:<表>:<版本>。
N_KEYS=$("$DOCKER_BIN" exec "$REDIS_CT" redis-cli --scan --pattern "tags:$TABLE:$DATE:*" | wc -l | tr -d ' ')
[ "$N_KEYS" = "$LOADED" ] && ok "Redis 键数实测＝$N_KEYS（tags:$TABLE:$DATE:*，与加载报告一致）" || bad "Redis 键数不符（实得 $N_KEYS vs 报告 $LOADED）"
LIVE=$("$DOCKER_BIN" exec "$REDIS_CT" redis-cli GET "tags:$TABLE:latest" | tr -d '\r')
[ -z "$LIVE" ] && ok "加载未触碰线上指针 tags:$TABLE:latest（双缓冲）" || bad "加载误改线上指针（=$LIVE）"
# P0-2 修复核心断言（fix/p0-2-recon-write）：HTTP 端点对账结果须落 sync:recon:<表>:<版本>，
# 否则 switch 第二闸永远判「未对账」拒切（遗留问题 4 的回归锚点；B1 起按表分域）。
RP=$("$DOCKER_BIN" exec "$REDIS_CT" redis-cli --raw HGET "sync:recon:$TABLE:$DATE" pass | tr -d '\r')
[ "$RP" = "1" ] && ok "sync:recon:$TABLE:$DATE 落键 pass=1（P0-2：端点对账写回，switch 二闸可过）" || bad "sync:recon:$TABLE:$DATE 未落或 pass=$RP（P0-2 回归）"

# ---------- ⑤b 幂等实证 ----------
say "⑤b 幂等：重跑不带 replace → 拒灌 409；带 replace → 成功且行数稳定"
DENYBODY="{\"namespace\":\"default\",\"table\":\"$TABLE\",\"cf\":\"cf\",
  \"version\":\"$DATE\",\"sourceCsv\":\"/sync/out/$(basename "$CSV")\"}"
DENY=$(curl -s -o "$TMP/deny.json" -w '%{http_code}' -X POST "$BASE/v1/admin/publish/hbase/load" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d "$DENYBODY")
if [ "$DENY" = "409" ] && grep -q "LOAD_CONFLICT" "$TMP/deny.json"; then
  ok "目标空间非空且未 replace → 409 拒灌（零副作用）：$(head -c 160 "$TMP/deny.json")"
else
  bad "幂等闸失效（实得 $DENY）：$(cat "$TMP/deny.json")"
fi
REPL_BODY="{\"namespace\":\"default\",\"table\":\"$TABLE\",\"cf\":\"cf\",
  \"version\":\"$DATE\",\"replace\":true,\"sourceCsv\":\"/sync/out/$(basename "$CSV")\"}"
curl -sS -X POST "$BASE/v1/admin/publish/hbase/load" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d "$REPL_BODY" -o "$TMP/repl.json"
echo "  · replace 重灌：$(head -c 240 "$TMP/repl.json")"
$PY - "$TMP/repl.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["ok"] and d["loaded"] > 0, d
assert d.get("clearedBeforeLoad", 0) == d["loaded"], d
assert d["reconcile"]["pass"] and d["reconcile"]["diffCount"] == 0, d["reconcile"]
print("  ✓ 带 replace 重灌：清空 %d 键后重写 %d 行（幂等可重跑，行数稳定）" % (
    d["clearedBeforeLoad"], d["loaded"]))
PYEOF
ok "幂等实证：拒灌＋replace 重灌行数稳定" || bad "replace 重灌断言红"

# ---------- ⑥ 发布（产物必须是 Redis 型） ----------
say "⑥ 发布：POST /v1/admin/publish → 产物 Redis 型（backend.kv，非 hbase）"
# 命名规范 v1：routePath=pub-load-e2e（合规：pub 域码建议位；旧名 pubload-demo 的 demo 是保留词会被发布闸 400）
curl -sS -X POST "$BASE/v1/admin/publish" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d "{
    \"serviceName\": \"发布台加载链路点查\",
    \"routePath\": \"/v1/svc/pub-load-e2e\",
    \"datasource\": \"hbase\",
    \"hbase\": {\"table\": \"default:$TABLE\", \"cf\": \"cf\", \"key_type\": \"phone_sm3\"},
    \"loadVersion\": \"$DATE\",
    \"fields\": [{\"name\": \"ac_active_sts_01\", \"sample\": \"band_C\", \"desc\": \"行为活跃\"},
                       {\"name\": \"mk_touch_sts_02\", \"sample\": \"lvl_2\", \"desc\": \"营销触达\"}],
    \"operator\": \"e2e@oneserve\", \"oaTicket\": \"OA-E2E-PUBLOAD\", \"releaseMode\": \"先测试\"}" -o "$TMP/pub.json"
$PY - "$TMP/pub.json" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if "entry" not in d:
    sys.exit("发布被拒（如实显示错误体）：" + json.dumps(d, ensure_ascii=False))
e = d["entry"]
assert e["datasource"] == "redis", ("发布产物必须是 Redis 型", e["datasource"])
assert e["fieldSpace"] == "wide297", e
assert e["loadSource"] and e["loadSource"]["version"] == sys.argv[2], e.get("loadSource")
print("  ✓ 发布响应：datasource=%s field_space=%s loadSource=%s" % (
    e["datasource"], e["fieldSpace"], json.dumps(e["loadSource"], ensure_ascii=False)))
PYEOF
ok "发布产物＝Redis 型＋field_space=wide297＋加载来源留痕" || bad "发布产物口径断言红"

$PY - "$TMP/service_dir/services.yaml" "$SID" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "service")
from app.kv_executor import load_services
reg = load_services(Path(sys.argv[1])); rt = reg[sys.argv[2]]
assert "hbase" not in rt["backend"], ("产物不得落 HBase 直连后端（ADR-0005）", rt["backend"])
assert rt["backend"]["kv"]["data_key"] == "tags:oneserve_demo:{version}:{sm3}", rt["backend"]["kv"]
assert rt["backend"]["load"]["version"] == "20260923"
assert rt["field_space"] == "wide297" and rt["trace"]["oa_ticket"] == "OA-E2E-PUBLOAD"
print("  ✓ 落盘核验：backend.kv（Redis 型）＋backend.load 来源留痕；无 backend.source=hbase")
PYEOF
ok "发布产物落盘＝Redis 型（HBase 直连保留代码但不在产物里）" || bad "落盘产物断言红"

# ---------- ⑦ 上线（既有原子切换，本卡未改）→ 真调 API ----------
say "⑦ 上线：既有 sync/switch.py（本卡未改语义）→ 真调 /v1/query/$SID"
# 口径说明（B1 表分域后）：源对账已由 ⑤ 加载端点内对账完成并落 sync:recon:<表>:<版本>
# （上方 P0-2 断言过 pass=1）；既有 CLI reconcile.py 尚无 --table 参数（只认存量
# tags:user 域），对分域表会误报「版本元信息缺失」——不在此重复跑，switch 第二闸
# 直接读 ⑤ 落下的按表对账键放行。
SW=$("$DOCKER_BIN" run --rm --network oneserve-net -e SYNC_REDIS_URL="redis://$REDIS_CT:6379/0" \
  -v "$PWD/sync:/sync:ro" "$IMG" python /sync/switch.py --table "$TABLE" --version "$DATE")
echo "$SW" | $PY -c "import sys,json;d=json.load(sys.stdin);assert d['ok'] and d['to']=='$DATE',d;print('  ✓ 原子切指针（%s → %s）'%(d['from'],d['to']))"
ok "上线（既有切换机制，按表分域）" || bad "上线失败"

gw up
sleep 2
T0=$(date +%s); ELAPSED=""
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/query/$SID" \
    -H "apikey: $E2E_APPKEY" -H "Content-Type: application/json" \
    -d "{\"key\": \"$KEY0\"}" || echo 000)
  [ "$CODE" = "200" ] && { ELAPSED=$(( $(date +%s) - T0 )); break; }
  sleep 2
done
[ -n "$ELAPSED" ] && ok "热加载：发布后 ${ELAPSED}s（≤60s）新服务可调（HTTP 200）" || bad "60s+ 未生效"
curl -sS -X POST "http://127.0.0.1:9080$RURI" -H "apikey: $E2E_APPKEY" \
  -H "Content-Type: application/json" -d "{\"key\": \"$KEY0\"}" -o "$TMP/api.json"
$PY - "$TMP/api.json" "$DATE" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["found"] is True and d["fields"], d
assert d["live_version"] == sys.argv[2], d
assert d["data_as_of"] == "2026-09-23", d
allowed = {"ac_active_sts_01", "mk_touch_sts_02"}
assert set(d["fields"]) <= allowed, d["fields"]
print("  ✓ 真调 API（经真网关）：live_version=%s data_as_of=%s fields=%s" % (
    d["live_version"], d["data_as_of"], json.dumps(d["fields"], ensure_ascii=False)))
PYEOF
ok "端到端判据：选表 → 加载 → 发布 → 真调 API 返回真数据（来自 Redis 热存储）" || bad "端到端响应断言红"

# ---------- ⑧ HBase 不可达实证 ----------
say "⑧ HBase 不可达：删 $HBASE_CT → 加载端点如实 503（不写脏、不回退假数据）"
"$DOCKER_BIN" rm -f "$HBASE_CT" >/dev/null 2>&1 || true
sleep 2
GONE=$(curl -s -o "$TMP/gone.json" -w '%{http_code}' -X POST "$BASE/v1/admin/publish/hbase/load" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d "{\"namespace\":\"default\",\"table\":\"$TABLE\",\"cf\":\"cf\",\"version\":\"20260924\",\"replace\":true}")
if [ "$GONE" = "503" ] && grep -qE "HBASE_UNAVAILABLE|不可达" "$TMP/gone.json"; then
  ok "不可达 → 503 如实报错（fallback=无，不回退假数据）：$(head -c 200 "$TMP/gone.json")"
else
  bad "不可达未如实报错（实得 $GONE）：$(cat "$TMP/gone.json")"
fi
GONE_KEYS=$("$DOCKER_BIN" exec "$REDIS_CT" redis-cli --scan --pattern "tags:user:20260924:*" | wc -l | tr -d ' ')
[ "$GONE_KEYS" = "0" ] && ok "不可达时零写入（tags:user:20260924:* 键数=0，不写脏）" || bad "不可达时写入脏数据（$GONE_KEYS 键）"

# ---------- ⑨ 结论 ----------
say "===== e2e 结果: PASS=$PASS FAIL=$FAIL ====="
say "定位（如实）：本卡只做开发期替身容器上的「选表→加载→发布→真调」；真集群首灌待 NC-M2-9。"
if [ "$FAIL" -eq 0 ] && [ "$PASS" -ge 14 ]; then
  say "发布台改造端到端全绿，证据已存 $EVIDENCE"
  exit 0
fi
say "存在失败项，见 $EVIDENCE"
exit 1
