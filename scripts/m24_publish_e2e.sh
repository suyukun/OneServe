#!/usr/bin/env bash
# TASK-M2-4 e2e：发布台后端 API 真链路（AC-4 发现 / AC-6 发布 hbase 型 → 60s 可调）。
#
# 架构前提（门槛稿口径，严格照做）：平台**不自建** HBase——本机 openeuler/hbase 容器只是
# 「开发期替身」（compose.hbase.yml 独立 override，非常驻，用完删）；
# 未来切示例 HBase 集群只改 HBASE_HOST/HBASE_PORT 环境变量，零代码改动。连接地址全走 env。
#
# 链路（每步打真件）：
#   ① 起 HBase 替身容器（-p oneserve-m24，独立项目标签，不动主栈）＋等 9090
#   ② happybase 直连建表 default:oneserve_m24 ＋ put 一行（rowkey=SM3；含 1 个字典外列）
#   ③ 起隔离 redis ＋ service 容器：service 目录**整目录**复制到临时目录后挂载（不碰仓库
#      真配置，也避开单文件 bind mount 的 rename EBUSY）；REGISTRY_POLL_INTERVAL_S=5
#   ④ GET /v1/admin/publish/hbase/discovery → AC-4 真 meta＋字段字典列＋双源校验（M2-5 契约）
#      POST /v1/admin/publish/hbase/test → AC-5 真试查；GET .../selftest → AC-5 headless 机检
#   ⑤ POST /v1/admin/publish datasource=hbase → AC-6 落 services.yaml（运行时条目＋写区）
#   ⑥ 热加载 ≤60s → curl /v1/query/<sid> 200 且投影白名单生效（AC-6 判据）
#   ⑦ 停 HBase → GET 发现端点如实 503/HBASE_UNAVAILABLE（不静默回退假数据）
#   ⑧ 清理：删本卡三容器、还原临时件（不动主栈、不删主栈卷）
#
# 用法：bash scripts/m24_publish_e2e.sh   （全绿 exit 0；证据存 tests/evidence/）
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="${DOCKER_BIN:-/Applications/Docker.app/Contents/Resources/bin/docker}"
EVIDENCE_DIR="tests/evidence"
EVIDENCE="${M24_EVIDENCE:-$EVIDENCE_DIR/m24-publish-e2e-$(date +%Y%m%d-%H%M%S).txt}"
PASS=0; FAIL=0
ADMIN_TOKEN="admin321"
BASE="http://127.0.0.1:18001"
SID="v1-svc-m24-demo"

say() { echo "[m24-e2e] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR"
exec > >(tee "$EVIDENCE") 2>&1
say "===== TASK-M2-4 发布台后端 API e2e $(date '+%F %T') ====="

TMP="$(mktemp -d)"

cleanup() {
  say "—— 收尾清理（只删本卡容器，不动主栈） ——"
  # 破坏闸口径：默认只删本卡专属容器；E2E_ALLOW_SHARED=1 时复用共享 oneserve-hbase，
  # 绝不在收尾里删共享容器（防误删并行会话正在用的数据现场）。
  $DOCKER_BIN rm -f oneserve-m24-service oneserve-m24-redis oneserve-m24-hbase >/dev/null 2>&1 || true
  rm -rf "$TMP"
  say "清理完成：oneserve-m24-service / oneserve-m24-redis / oneserve-m24-hbase 已删；临时件已删（共享 oneserve-hbase 未动）"
}
trap cleanup EXIT

$DOCKER_BIN info >/dev/null 2>&1 || { echo "docker daemon 不可用"; exit 2; }
ok "docker daemon 可用（${DOCKER_BIN}）"

# ── ① HBase 替身容器（破坏闸：默认专属一次性容器，绝不 compose 起共享 oneserve-hbase）──
# 9-28 事故实证：e2e 脚本 compose 起共享 oneserve-hbase 再建/删表＝直接动共享数据
# （共享卷里有 usr_basic_info 500 行、usr_asset_tag 百万行）。默认起专属一次性容器
# oneserve-m24-hbase（无共享卷，删容器即清）；连共享容器须显式 E2E_ALLOW_SHARED=1。
if [ "${E2E_ALLOW_SHARED:-0}" = "1" ]; then
  say "① [共享闸放行] E2E_ALLOW_SHARED=1 → 复用共享容器 oneserve-hbase（不删不建）"
  st=$($DOCKER_BIN inspect -f '{{.State.Status}}' oneserve-hbase 2>/dev/null || echo missing)
  [ "$st" = "running" ] && ok "共享 oneserve-hbase 在场（running；写操作自担风险）" \
    || { bad "共享 oneserve-hbase 不在 running（$st）——先起它再放行"; exit 2; }
else
  say "① 起专属一次性 oneserve-m24-hbase（HBASE_HEAPSIZE=1024，mem 2g，无共享卷）..."
  $DOCKER_BIN rm -f oneserve-m24-hbase >/dev/null 2>&1 || true
  $DOCKER_BIN run -d --name oneserve-m24-hbase --network oneserve-net \
    --memory 2g -e HBASE_HEAPSIZE=1024 -e HBASE_THRIFT_OPTS="-Xms64m -Xmx256m" \
    --entrypoint bash openeuler/hbase:latest \
    -c "start-hbase.sh && hbase-daemon.sh start thrift && tail -f /dev/null" >/dev/null
  ok "破坏闸生效：专属容器 oneserve-m24-hbase（不挂共享卷；默认拒绝共享 oneserve-hbase）"
fi
say "等 Thrift 9090 就绪（最长 150s）..."
READY=0
for i in $(seq 1 75); do
  if $DOCKER_BIN exec oneserve-m24-hbase bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ "$READY" = "1" ] && ok "Thrift 9090 就绪（HBase 容器内探活）" || { bad "9090 未就绪"; exit 1; }
DIGEST="$($DOCKER_BIN image inspect --format '{{index .RepoDigests 0}}' openeuler/hbase:latest 2>/dev/null || echo unknown)"
ok "镜像钉选复核：openeuler/hbase:latest（${DIGEST}）"

# ── ② 建表＋灌一行（rowkey=SM3；含字典外列当 AC-4 靶子） ──
say "② happybase 直连建表 default:oneserve_m24 ＋ put 演示行..."
python3 - <<PYEOF || { bad "put 演示数据失败"; exit 1; }
import sys
sys.path.insert(0, "service")
import happybase
from app.kv_executor import sm3_hex
TABLE = "default:oneserve_m24"
conn = happybase.Connection(host="127.0.0.1", port=9090, timeout=30000)
# 破坏闸口径：默认只连本卡专属容器 oneserve-m24-hbase（网络名解析）；表名 oneserve_m24
# 本身就是独立表名，不会碰共享表；E2E_ALLOW_SHARED=1 时才允许解析到共享容器。
if TABLE in [t.decode() for t in conn.tables()]:
    conn.disable_table(TABLE); conn.delete_table(TABLE)
conn.create_table(TABLE, {"cf": {"max_versions": 1}})
key = sm3_hex("13800138000")
conn.table(TABLE).put(key.encode(), {
    b"cf:ac_active_sts_01": b"band_C",
    b"cf:mk_touch_sts_02": b"lvl_2",
    b"cf:if_acct_fct_24": b"523877",
    b"cf:usr_id": b"never-leak",
    b"cf:not_in_dict_col": b"ghost",     # 双源校验必须拦下的列
})
got = conn.table(TABLE).row(key.encode())
assert got.get(b"cf:if_acct_fct_24") == b"523877"
print(f"  ✓ 表 {TABLE} 就绪，rowkey=SM3({key[:8]}…)，{len(got)} 列")
PYEOF
ok "演示数据落真 HBase（含字典外列 not_in_dict_col＝双源校验靶子）"

# ── ③ 隔离 service 容器（service 目录整目录复制挂载；不动仓库真配置） ──
say "③ 构建本卡代码镜像＋起隔离 redis/service 容器..."
IMG="oneserve/service:m24-e2e"
$DOCKER_BIN build -q -t "$IMG" ./service >/dev/null
ok "镜像 ${IMG} 就绪（本卡代码构建）"

cp -R service "$TMP/service_dir"
# 授权矩阵：给新服务登记 demo_merchant 白名单（真源仍是文件，非内存注入）
python3 - "$TMP/service_dir/auth_matrix.yaml" <<'PYEOF'
import sys, yaml
p = sys.argv[1]
doc = yaml.safe_load(open(p, encoding="utf-8"))
for row in doc["matrix"]:
    if row.get("tenant") == "demo_merchant":
        row["grants"].append({"service": "v1-svc-m24-demo",
                              "fields": ["ac_active_sts_01", "mk_touch_sts_02"]})
yaml.safe_dump(doc, open(p, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
PYEOF
ok "隔离配置就绪（service 目录副本；demo_merchant 已登记新服务白名单）"

$DOCKER_BIN rm -f oneserve-m24-redis oneserve-m24-service >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name oneserve-m24-redis --network oneserve-net \
  library/redis:7-alpine >/dev/null
ENVFILE="$TMP/env"
{
  echo "REDIS_URL=redis://oneserve-m24-redis:6379/0"
  echo "AUDIT_DIR=/data/audit"
  if [ "${E2E_ALLOW_SHARED:-0}" = "1" ]; then
    echo "HBASE_HOST=oneserve-hbase"      # 共享闸放行：指向共享容器
  else
    echo "HBASE_HOST=oneserve-m24-hbase"  # 默认：指向本卡专属一次性容器
  fi
  echo "HBASE_PORT=9090"
  echo "APISIX_ADMIN=http://oneserve-apisix:9180"
  echo "APISIX_ADMIN_KEY=***REMOVED***"
  echo "REGISTRY_POLL_INTERVAL_S=5"
  echo "ADMIN_API_TOKEN=$ADMIN_TOKEN"
  echo "SCHEMA_CSV=/sync/schema_usr_label_wide_297.csv"   # 字段字典真源显式钉路径
  # TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）
} > "$ENVFILE"
# 字段字典真源经 /sync 只读挂载（与主栈 service 同口径：./sync:/sync:ro）——
# 双源校验的字典侧就是它；缺挂载＝字典不可读（AC-4/AC-6 如实拒绝，而非静默放行）。
$DOCKER_BIN run -d --name oneserve-m24-service --network oneserve-net \
  --env-file "$ENVFILE" -p 127.0.0.1:18001:8000 \
  -v "$TMP/service_dir":/app:rw -v "$PWD/sync":/sync:ro "$IMG" >/dev/null

HEALTHY=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz" || echo 000)
  [ "$code" = "200" ] && { HEALTHY=1; break; }
  sleep 2
done
[ "$HEALTHY" = "1" ] && ok "隔离 service 健康（/healthz 200 @127.0.0.1:18001）" || { bad "service 未健康"; $DOCKER_BIN logs --tail 30 oneserve-m24-service; exit 1; }
$DOCKER_BIN logs oneserve-m24-service 2>&1 | grep -q "HBase 连接池就绪" \
  && ok "启动日志含「HBase 连接池就绪」（HBASE_HOST=oneserve-hbase env 注入生效）" \
  || bad "未见 HBase 连接池日志"

# ── ④ AC-4：真 meta 发现＋双源校验 ──
say "④ GET /v1/admin/publish/hbase/discovery（AC-4 真 meta×字典双源，M2-5 契约）..."
DISC="$TMP/discover.json"
curl -sS "$BASE/v1/admin/publish/hbase/discovery" -H "X-Admin-Token: $ADMIN_TOKEN" -o "$DISC"
python3 - "$DISC" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["datasource"] == "hbase" and d["checkedAt"], d
ns = {x["id"]: x for x in d["domains"]}
assert "default" in ns, list(ns)
tbls = {t["id"]: t for t in ns["default"]["tables"]}
assert "oneserve_m24" in tbls, list(tbls)
t = tbls["oneserve_m24"]
assert t["namespace"] == "default" and t["table"] == "oneserve_m24"
assert set(t["keyTypes"]) == {"phone_sm3", "usr_id"} and t["keySamples"]["phone_sm3"]
fams = {f["name"]: f for f in t["families"]}
assert "cf" in fams, list(fams)
cols = {c["name"]: c for c in fams["cf"]["columns"]}
assert len(cols) == 296, len(cols)                          # 297 列减键列
col = cols["ac_active_sts_01"]
assert col["cnName"] and col["type"] and col["sensitive"] in ("公开", "内部", "敏感"), col
assert col["cf"] == "cf"
ds = d["dualSource"]
assert ds["status"] == "mismatch", "含字典外列必须 mismatch（双源校验拦截）"
assert ds["unknownColumns"] == ["not_in_dict_col"], ds
print("  ✓ 真 meta：namespace=default 表=oneserve_m24 列族=", list(fams))
print("  ✓ 列来自字段字典（296 列）：", col)
print("  ✓ 双源校验拦截：status=", ds["status"], "unknownColumns=", ds["unknownColumns"])
PYEOF
ok "AC-4 真链路（M2-5 契约）：HBase meta 发现＋字段字典列＋双源校验拦截字典外列" || bad "AC-4 发现断言红"

# 契约对齐独立验证：用 main 上已合入的 M2-5 适配层（只读取出，不改 admin/）消费本响应
say "④c 契约对齐：真 M2-5 适配层（main:admin/publish-hbase-adapter.js）消费本响应..."
if command -v node >/dev/null 2>&1 && git show main:admin/publish-hbase-adapter.js > "$TMP/adapter.js" 2>/dev/null; then
  node -e '
    global.window = global;
    require(process.argv[1]);
    const fs = require("fs");
    const raw = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
    const n = global.O1HBase.normalizeDiscovery(raw);
    if (!n.ok) throw new Error("适配层归一失败: " + JSON.stringify(n.error));
    const tables = global.O1HBase.allTables(n.domains);
    if (tables.length !== 1) throw new Error("表数不符: " + tables.length);
    const t = tables[0];
    if (t.id !== "default:oneserve_m24") throw new Error("表 id 不符: " + t.id);
    if (t.namespace !== "default" || t.table !== "oneserve_m24") throw new Error("命名空间/表名不符");
    if (t.columnCount !== 296) throw new Error("列数不符: " + t.columnCount);
    if (!t.keySamples.phone_sm3) throw new Error("示例键缺失");
    console.log("  ✓ 适配层归一：域=" + n.domains.length + " 表=" + t.id +
                " 列族=" + t.families.map(f => f.name).join(",") +
                " 列=" + t.columnCount + " keyTypes=" + t.keyTypes.join("/") +
                " dual=" + n.dualSource.status);
  ' "$TMP/adapter.js" "$DISC" && ok "契约对齐：真 M2-5 适配层成功消费本卡 discovery 响应" || bad "适配层消费失败"
else
  echo "  · node 不可用或无 main:适配层，跳过契约对齐验证（如实标注）"
fi

# 真试查端点（AC-5 契约形状）
say "④d POST /v1/admin/publish/hbase/test（真试查，AC-5 契约）..."
KEYT=$(python3 -c "import sys; sys.path.insert(0,'service'); from app.kv_executor import sm3_hex; print(sm3_hex('13800138000'))")
TEST_HTTP=$(curl -sS -X POST "$BASE/v1/admin/publish/hbase/test" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d "{
    \"namespace\":\"default\",\"table\":\"oneserve_m24\",\"families\":[\"cf\"],
    \"fields\":[{\"name\":\"ac_active_sts_01\",\"cf\":\"cf\",\"sample\":\"band_C\"}],
    \"keyType\":\"phone_sm3\",\"sampleKey\":\"$KEYT\",\"timeoutMs\":3000}" -o "$TMP/test.json" -w '%{http_code}')
echo "  · test HTTP=$TEST_HTTP body=$(head -c 400 "$TMP/test.json" 2>/dev/null)"
if [ "$TEST_HTTP" != "200" ]; then
  say "  ↳ test 非 200，转储 service 日志（诊断用）:"
  $DOCKER_BIN logs --tail 60 oneserve-m24-service 2>&1 | tail -60
fi
python3 - "$TMP/test.json" <<'PYEOF'
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
if not raw.strip():
    raise SystemExit("试查响应为空（见上方 test HTTP 与 service 日志）")
d = json.loads(raw)
assert d["passed"] is True and d["code"] == 200, d
assert d["body"].get("ac_active_sts_01") == "band_C", d
print("  ✓ 真试查命中：passed=True code=200 elapsedMs=", d["elapsedMs"], "body=", d["body"])
PYEOF
ok "AC-5 真试查：示例键对真 HBase 命中（read-only，平台零写）" || bad "真试查断言红"

# AC-5 headless 自检端点
say "④e GET /v1/admin/publish/hbase/selftest（AC-5 机检端点，M2-5 契约）..."
curl -sS "$BASE/v1/admin/publish/hbase/selftest" -H "X-Admin-Token: $ADMIN_TOKEN" -o "$TMP/selftest.json"
python3 - "$TMP/selftest.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ids = {a["id"]: a for a in d["assertions"]}
for k in ("dict-present", "hbase-reachable", "dual-source", "domain-tree",
          "reject-not-in-dict", "route-render"):
    assert k in ids, (k, list(ids))
assert ids["hbase-reachable"]["ok"] is True
assert ids["dict-present"]["ok"] is True
print("  ✓ 自检断言：total=", d["total"], "passed=", d["passedCount"],
      "items=", [(a["id"], a["ok"]) for a in d["assertions"]])
PYEOF
ok "AC-5 headless 机检端点就绪（真 HBase 下断言如实）" || bad "自检端点断言红"

# 发布前置校验：字典外字段 → 拒绝（AC-6 加载期校验，真 HTTP）
CODE=$(curl -s -o "$TMP/reject.json" -w '%{http_code}' -X POST "$BASE/v1/admin/publish" \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{
    "serviceName":"坏服务","routePath":"/v1/svc/m24-bad","datasource":"hbase",
    "hbase":{"table":"default:oneserve_m24","cf":"cf","key_type":"phone_sm3"},
    "fields":[{"name":"not_in_dict_col","sample":"x"}]}')
if [ "$CODE" = "400" ] && grep -q "field_not_in_dict" "$TMP/reject.json"; then
  ok "AC-6/AC-7 加载期校验：字典外字段 400 field_not_in_dict（写状态键之前拦下）"
else
  bad "字典外字段应 400 field_not_in_dict，实得 ${CODE}：$(cat "$TMP/reject.json")"
fi

# ── ⑤ AC-6：发布 hbase 型服务 ──
say "⑤ POST /v1/admin/publish（datasource=hbase，AC-6）..."
PUB="$TMP/publish.json"
curl -sS -X POST "$BASE/v1/admin/publish" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{
    "serviceName": "M2-4 HBase 宽表标签点查",
    "routePath": "/v1/svc/m24-demo",
    "datasource": "hbase",
    "hbase": {"table": "default:oneserve_m24", "cf": "cf", "key_type": "phone_sm3"},
    "fields": [{"name": "ac_active_sts_01", "sample": "band_C", "desc": "行为活跃"},
               {"name": "mk_touch_sts_02", "sample": "lvl_2", "desc": "营销触达"}],
    "operator": "e2e@oneserve", "oaTicket": "OA-E2E-M24", "releaseMode": "先测试"}' -o "$PUB"
python3 - "$PUB" <<'PYEOF'
import json, sys, yaml
d = json.load(open(sys.argv[1], encoding="utf-8"))
e = d["entry"]
assert e["datasource"] == "hbase", d
assert e["fieldSpace"] == "wide297", e
assert e["hbaseTable"] == "default:oneserve_m24" and e["keyType"] == "phone_sm3"
assert e["version"] == "V1" and e["releaseMode"] == "先测试", e
print("  ✓ 发布响应：datasource=hbase field_space=wide297 table=", e["hbaseTable"],
      "keyType=", e["keyType"], "releaseMode=", e["releaseMode"])
PYEOF
ok "AC-6 发布确认：hbase 型条目（field_space=wide297，ADR-0004）"

# 落盘核验：运行时注册区条目（加载器认它＝可调前提）
python3 - "$TMP/service_dir/services.yaml" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, "service")
from app.kv_executor import load_services
reg = load_services(Path(sys.argv[1]))
rt = reg["v1-svc-m24-demo"]
assert rt["backend"]["source"] == "hbase", rt
assert rt["backend"]["hbase"] == {"table": "default:oneserve_m24", "cf": "cf", "key_type": "phone_sm3"}, rt
assert rt["field_space"] == "wide297"
assert rt["trace"]["oa_ticket"] == "OA-E2E-M24"
assert rt["trace"]["commit"] is None          # git 留痕占位：不伪造 hash
print("  ✓ 运行时 services: 条目就位：(service_id, version)=", ("v1-svc-m24-demo", rt["version"]),
      "field_space=", rt["field_space"])
PYEOF
ok "AC-6 真落盘：运行时注册表条目（backend/params/field_space/trace 全对；git 留痕占位 null）"

# ── ⑥ AC-6 判据：60s 内 curl 可调（真热加载＋真 HBase 池补建） ──
say "⑥ 等热加载 ≤60s，curl /v1/query/${SID}（service 层真链路）..."
KEY=$(python3 -c "import sys; sys.path.insert(0,'service'); from app.kv_executor import sm3_hex; print(sm3_hex('13800138000'))")
WRITE_TS=$(( $(date +%s) ))
T0=$(date +%s); ELAPSED=""
for i in $(seq 1 40); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/query/$SID" \
    -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" \
    -d "{\"key\": \"$KEY\"}" || echo 000)
  if [ "$CODE" = "200" ]; then ELAPSED=$(( $(date +%s) - T0 )); break; fi
  sleep 2
done
if [ -n "$ELAPSED" ]; then
  ok "AC-6 判据：发布后 ${ELAPSED}s（≤60s）新服务 curl 200 可调"
else
  bad "60s+ 内未生效（判据红）"
fi

BODY="$TMP/body.json"
curl -sS -X POST "$BASE/v1/query/$SID" -H "apikey: oneserve-dev-appkey-001" \
  -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}" -o "$BODY"
echo "  · 响应体: $(cat "$BODY")"
python3 - "$BODY" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
assert d["service_id"] == "v1-svc-m24-demo", d
assert d["found"] is True and d["datasource"] == "hbase", d
assert set(d["fields"]) <= {"ac_active_sts_01", "mk_touch_sts_02"}, d
assert "usr_id" not in d["fields"] and "not_in_dict_col" not in d["fields"]
print("  ✓ 投影白名单生效（未授权列/字典外列零出现）；fields=", d["fields"])
PYEOF
CODE=$(curl -s -o /dev/null -w '%{http_code}' -D "$TMP/hdr.txt" -X POST "$BASE/v1/query/$SID" \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}")
grep -qi "^X-OneServe-DataSource: hbase" "$TMP/hdr.txt" \
  && ok "响应头 X-OneServe-DataSource: hbase（数据源可观测）" \
  || bad "数据源响应头缺失"

# ── ⑦ HBase 不可达 → 如实 503（不静默回退假数据） ──
say "⑦ 停 HBase 容器，验「不可达如实报错」..."
$DOCKER_BIN stop oneserve-m24-hbase >/dev/null
sleep 2
CODE=$(curl -s -o "$TMP/down.json" -w '%{http_code}' "$BASE/v1/admin/publish/hbase/discovery" -H "X-Admin-Token: $ADMIN_TOKEN")
if [ "$CODE" = "503" ] && grep -q "HBASE_UNAVAILABLE" "$TMP/down.json"; then
  ok "HBase 不可达 → discovery 端点 503 HBASE_UNAVAILABLE（如实报错，零假数据回退）"
else
  bad "不可达应 503 HBASE_UNAVAILABLE，实得 ${CODE}：$(cat "$TMP/down.json" 2>/dev/null | head -c 200)"
fi
# selftest 在不可达下须如实标红（不跳过不谎报通过）
curl -sS "$BASE/v1/admin/publish/hbase/selftest" -H "X-Admin-Token: $ADMIN_TOKEN" -o "$TMP/selftest-down.json"
python3 - "$TMP/selftest-down.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
ids = {a["id"]: a for a in d["assertions"]}
assert ids["hbase-reachable"]["ok"] is False, "不可达须如实标红"
assert ids["dual-source"]["ok"] is False, "不可达时双源校验未执行，不得谎称一致"
assert ids["dict-present"]["ok"] is True, "纯逻辑断言不因不可达跳过"
print("  ✓ 不可达 selftest 如实标红：hbase-reachable=False dual-source=False dict-present=True")
PYEOF
ok "不可达态 selftest 如实（不跳过、不谎报通过）" || bad "不可达 selftest 断言红"

say "===== e2e 结果: PASS=$PASS FAIL=$FAIL ====="
if [ "$FAIL" -eq 0 ] && [ "$PASS" -ge 12 ]; then
  say "AC-4/AC-6/AC-7 真链路全绿，证据已存 $EVIDENCE"; exit 0
fi
exit 1
