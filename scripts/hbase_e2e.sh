#!/usr/bin/env bash
# TASK-M2-3 e2e：hbase 数据源服务端到端（门槛稿 M2-3「compose.hbase.yml 场景下端到端」）。
# 前置：hbase 容器已由 hbase_probe.sh 起好（或 HBASE_PROBE_KEEP=1 保留的现场）。
# 链路：redis+hbase 起容器 → happybase 直连 put 演示数据（rowkey=SM3）→
#       起 service 容器（挂 services.yaml/auth_matrix.yaml，HBASE_HOST 指容器网内 DNS）→
#       curl /v1/hbase/query（鉴权/投影/脱敏/审计全链在 service 内执行）→ 断言留证。
# 用法：bash scripts/hbase_e2e.sh   （断言全绿 exit 0；证据存 tests/evidence/）
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="${DOCKER_BIN:-docker}"
EVIDENCE_DIR="tests/evidence"
EVIDENCE="${HBASE_E2E_EVIDENCE:-$EVIDENCE_DIR/hbase-e2e-$(date +%Y%m%d-%H%M%S).txt}"
PASS=0; FAIL=0

say() { echo "[e2e] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR"
exec > >(tee "$EVIDENCE") 2>&1
say "===== TASK-M2-3 hbase 服务 e2e $(date '+%F %T') ====="

cleanup() {
  $DOCKER_BIN rm -f oneserve-e2e-service oneserve-e2e-redis >/dev/null 2>&1 || true
  # hbase 容器由调用方（hbase_probe.sh）收尾；单独直跑时 KEEP_HBASE=1 可保留现场
}
trap cleanup EXIT

# 0) 前置：hbase 容器在场且 9090 就绪（由探针脚本或手动 up）
$DOCKER_BIN info >/dev/null 2>&1 || { echo "docker daemon 不可用"; exit 2; }
# ── 共享 HBase 破坏闸（9-28 事故后立规）────────────────────────────────────
# 本脚本第 3) 步会 disable+delete+recreate default:oneserve_demo——直打共享
# oneserve-hbase 即清共享表（9-28 事故实证）。默认拒绝直跑；须显式 E2E_ALLOW_SHARED=1
# 放行（oneserve_demo 是独立表名，不碰 usr_basic_info/usr_asset_tag/card_acct_summary）。
if [ "${E2E_ALLOW_SHARED:-0}" != "1" ]; then
  echo "[e2e] ✗ 默认拒绝：本脚本会重建 default:oneserve_demo 表，直打共享 oneserve-hbase 有覆盖共享数据风险（9-28 事故实证）。"
  echo "[e2e]   - 连共享容器：E2E_ALLOW_SHARED=1 bash scripts/hbase_e2e.sh（仅动 oneserve_demo 独立表，不碰业务表）"
  echo "[e2e]   - 或用自带隔离 HBase 的脚本：scripts/m212_testkit_e2e.sh / scripts/m26_e2e_acceptance.sh"
  exit 2
fi
say "[共享闸放行] E2E_ALLOW_SHARED=1 → 允许在共享 oneserve-hbase 上重建 oneserve_demo 表（自担风险）"
st=$($DOCKER_BIN inspect -f '{{.State.Status}}' oneserve-hbase 2>/dev/null || echo missing)
[ "$st" = "running" ] || { echo "oneserve-hbase 未运行（先 bash scripts/hbase_probe.sh 或 compose up）"; exit 2; }
ok "oneserve-hbase 在场（running）"

# 1) 起 e2e 专用 redis（service 生命线依赖：ping 预置＋调用统计；与主栈隔离）
$DOCKER_BIN rm -f oneserve-e2e-redis >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name oneserve-e2e-redis --network oneserve-net \
  library/redis:7-alpine >/dev/null
ok "e2e redis 已起（oneserve-net）"

# 2) 服务镜像就绪：从本仓现场构建独立 tag（不碰主栈共享的 oneserve/service:m0——
#    那是并行会话的运行现场；独立 tag 保证 e2e 跑的是本卡代码且零副作用）。
#    无条件重建（层缓存下秒级）——image inspect 短路会跑陈旧镜像（M2-3 二跑实证）。
IMG="oneserve/service:m23-e2e"
say "构建服务镜像 ${IMG}（本仓 service/ 上下文，层缓存加速）..."
$DOCKER_BIN build -q -t "$IMG" ./service >/dev/null
ok "服务镜像 ${IMG} 就绪（本卡代码构建）"

# 3) happybase 直连 put 演示数据（rowkey=SM3 键；put 用例含未授权列→投影断言有效）
say "put 演示数据到 default:oneserve_demo ..."
PUT_RC=0
python3 - <<'PYEOF' || PUT_RC=$?
import happybase
conn = happybase.Connection(host="127.0.0.1", port=9090, timeout=30000)
TABLE = "default:oneserve_demo"
tables = [t.decode() for t in conn.tables()]
if TABLE in tables:
    conn.disable_table(TABLE); conn.delete_table(TABLE)
conn.create_table(TABLE, {"cf": {"max_versions": 1}})
print(f"  · 已建表 {TABLE}")
t = conn.table(TABLE)
import sys; sys.path.insert(0, "service")
from app.kv_executor import sm3_hex
key = sm3_hex("13800138000")
row = {b"cf:ac_active_sts_01": b"band_C",
       b"cf:if_acct_fct_24": b"523877",
       b"cf:mk_touch_sts_02": b"lvl_2",
       b"cf:usr_id": b"should-never-leak",
       b"cf:ac_click_sts_01": b"secret-val"}
t.put(key.encode(), row)
got = t.row(key.encode())
assert got.get(b"cf:if_acct_fct_24") == b"523877"
print(f"  ✓ put 完成 rowkey=SM3({key[:8]}…) 共 {len(row)} 列（含 2 个未授权列）")
PYEOF
[ "$PUT_RC" -eq 0 ] && ok "put 演示数据绿" || { bad "put 演示数据红"; exit 1; }

# 4) 起 service 容器（挂注册表/授权矩阵；HBASE_HOST=容器网内 DNS）
ENVFILE=$(mktemp)
{
  echo "REDIS_URL=redis://oneserve-e2e-redis:6379/0"
  echo "AUDIT_DIR=/data/audit"
  echo "HBASE_HOST=oneserve-hbase"
  echo "HBASE_PORT=9090"
} > "$ENVFILE"
$DOCKER_BIN rm -f oneserve-e2e-service >/dev/null 2>&1 || true
$DOCKER_BIN run -d --name oneserve-e2e-service --network oneserve-net \
  --env-file "$ENVFILE" -p 127.0.0.1:18000:8000 \
  -v "$PWD/service/services.yaml":/app/services.yaml:ro \
  -v "$PWD/service/auth_matrix.yaml":/app/auth_matrix.yaml:ro \
  "$IMG" >/dev/null
rm -f "$ENVFILE"
say "等 service 健康（/healthz，最长 60s）..."
HEALTHY=0
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/healthz || echo 000)
  [ "$code" = "200" ] && { HEALTHY=1; break; }
  sleep 2
done
[ "$HEALTHY" = "1" ] && ok "service 健康（/healthz 200）" || { bad "service 60s 未健康"; $DOCKER_BIN logs --tail 30 oneserve-e2e-service || true; exit 1; }
HBASE_LOGS=$($DOCKER_BIN logs oneserve-e2e-service 2>&1 | grep -c "HBase 连接池就绪" || true)
[ "$HBASE_LOGS" -ge 1 ] && ok "启动日志含「HBase 连接池就绪」（hbase 型服务在场才建池）" || bad "未见 HBase 连接池日志"

# 5) 经服务查 HBase（正式口径：调用方传 SM3 键；鉴权走授权矩阵 demo_bank 条目）
KEY=$(python3 -c "import sys; sys.path.insert(0, 'service'); from app.kv_executor import sm3_hex; print(sm3_hex('13800138000'))")
BODY=$(curl -sS -X POST http://127.0.0.1:18000/v1/hbase/query \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" \
  -d "{\"key\": \"$KEY\"}")
echo "  · 响应体: $BODY"

say "响应断言（AC-1/AC-2/AC-14）..."
ASSERT_RC=0
printf '%s' "$BODY" | BODY="$BODY" python3 - <<'PYEOF' || ASSERT_RC=$?
import json, os, sys
body_raw = os.environ["BODY"]
d = json.loads(body_raw)
assert d.get("service_id") == "demo_hbase_query", f"service_id 不符: {d}"
assert d.get("found") is True, f"found 应为 true: {d}"
fields = d.get("fields") or {}
grant_expect = {"ac_active_sts_01", "if_acct_fct_24", "mk_touch_sts_02"}
assert set(fields) <= grant_expect, f"投影结果混入未授权字段（红线）: {sorted(fields)}"
assert fields.get("if_acct_fct_24") == "B", f"脱敏档位不符（523877→B）: {fields}"
assert "523877" not in body_raw, "脱敏字段原值出现（红线）"
for leaked in ("usr_id", "ac_click_sts_01",
               "should-never-leak", "secret-val"):
    assert leaked not in body_raw, f"越权字段/值泄露: {leaked}"
assert d.get("datasource") == "hbase", f"datasource 标注不符: {d}"
print("  ✓ service_id/found/投影白名单/脱敏档位/datasource 全部符合")
PYEOF
[ "$ASSERT_RC" -eq 0 ] && ok "响应体断言绿（白名单外字段名零出现＝AC-2）" || bad "响应体断言红 (rc=$ASSERT_RC)"

HDR=$(curl -sS -D - -o /dev/null -X POST http://127.0.0.1:18000/v1/hbase/query \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" \
  -d "{\"key\": \"$KEY\"}" | tr -d "\r")
echo "$HDR" | grep -qi "^X-OneServe-DataSource: hbase" && ok "响应头 X-OneServe-DataSource: hbase" || bad "数据源响应头缺失"

# 6) 诚实空值＋未登记调用方 403（fail-closed 双向断言）
KEY2=$(python3 -c "import sys; sys.path.insert(0, 'service'); from app.kv_executor import sm3_hex; print(sm3_hex('13900139000'))")
MISS=$(curl -sS -X POST http://127.0.0.1:18000/v1/hbase/query \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" \
  -d "{\"key\": \"$KEY2\"}")
echo "$MISS" | python3 -c 'import sys, json; d = json.load(sys.stdin); assert d["found"] is False and d["fields"] is None' \
  && ok "行不存在→found=false＋fields=null（诚实空值）" || bad "诚实空值断言红: $MISS"

CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:18000/v1/hbase/query \
  -H "apikey: not-registered-key" -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}")
[ "$CODE" = "403" ] && ok "未登记 AppKey→403（fail-closed）" || bad "未登记调用方应 403，实得 $CODE"

# 7) AC-14：审计七元组落盘且 endpoint 可区分数据源
sleep 1
AUDIT_HITS=$($DOCKER_BIN exec oneserve-e2e-service sh -c "grep -c 'datasource=hbase' /data/audit/audit.jsonl 2>/dev/null" || echo 0)
AUDIT_HITS=${AUDIT_HITS%%$'\n'*}   # grep 计 0 时会多打一行（0\n0），取首行保数值语义
[ "$AUDIT_HITS" -ge 1 ] && ok "审计 JSONL 含 datasource=hbase 条目（AC-14，$AUDIT_HITS 条）" || bad "审计未见 hbase 数据源条目"

# 8) 收尾由 trap 完成（容器清理＋hbase down 让位内存）
say "===== e2e 结果: PASS=$PASS FAIL=$FAIL ====="
if [ "$FAIL" -eq 0 ] && [ "$PASS" -ge 5 ]; then say "e2e 全绿，证据已存 $EVIDENCE"; exit 0; fi
exit 1
