#!/usr/bin/env bash
# ============================================================
# TASK-M2-3.8 / AC-8-b · schema 漂移防护一键演练（真 Redis 链路，可复现）
#
# 说明（2026-09-23 修正）：本机 Docker 可用，**真 Redis 链路是唯一演练路径**。
# 前一位开发者在 Docker 可用的情况下误判「Docker 不可用」而退化成假 store 演练，
# 正是本次「语义占位符被误判删列」假阳性 bug 漏检的原因。本脚本不再以假 store
# 顶替：Docker 不可用时如实退出 2，并打印这一根因。
#
# 演练闭环（全程对真 Redis；专属容器/随机宿主端口/无名卷）：
#   ① 起专属 Redis 容器（随机端口、--rm、无名卷；**严禁 docker compose down -v**）；
#   TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）。
#   ② 真实配置对账（真 services.yaml＋真 auth_matrix.yaml＋表级真字典 CSV）：
#      现役服务 ok、零误隔离；
#   ③ 人为真删列（真被引用列从字典副本移除）→ CLI 对账 → 该服务 quarantined；
#   ④ 拒查（真执行器读真 Redis 状态键）→ TagQueryError(503, schema_drift_quarantined)；
#   ⑤ 增列不泄露（字典多列、无人引用）→ 不触发隔离、不进状态键/投影；
#   ⑥ 恢复真字典 → CLI --recover → ok → 执行器放行；
#   ⑦ 清场：删本脚本写入的 sync:drift:* 键＋删容器（不动任何共享卷）。
#
# 自动化口径（如实，不得假称已生效）：本演练证明隔离机制对 A 型（KV/HBase）真实生效；
# 但隔离状态键由**手工 CLI**（scripts/reconcile_drift.py）写入，AC-8-b 所称「自动转
# quarantined」的自动触发器属 M2 生产化，本阶段未实现——删列后须人工跑一次 CLI。
#
# 用法：bash scripts/drill_schema_drift.sh
# 退出码：全绿 0；任一断言红 1；Docker 不可用无法走真链路 2。
#
# 演练目标重定位（P2 2026-10-08）：旧演示服务 user_tag_query / demo_hbase_query /
# news_tag_query 已于 2026-09-29 全量下线（维护者：主线聚焦，资产保留），
# 演练改打唯一现役服务 v1-svc-usr-basic-info（field_space=wide297，键空间
# tags:usr_basic_info:*）。字典真源同步换为其表级字典 sync/schema_usr_basic_info.csv
# （该服务 12 个引用字段全在表级字典、全不在宽表 297 CSV——用 297 CSV 会把现役服务
# 基线误判隔离）；与卡2 TODO「297 宽表字典换源为表级 schema 元数据」方向一致。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"
EVIDENCE_DIR="tests/evidence"
mkdir -p "$EVIDENCE_DIR"
EVIDENCE="${DRIFT_DRILL_EVIDENCE:-$EVIDENCE_DIR/drift-drill-$(date +%Y%m%d-%H%M%S).txt}"
exec > >(tee "$EVIDENCE") 2>&1

PASS=0; FAIL=0
say() { echo "[drill] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

say "===== schema 漂移防护演练 AC-8-b（真 Redis） $(date '+%F %T') ====="
say "仓库=$PWD python=$($PY --version 2>&1)"

# ---------- Docker 探测：真链路唯一路径；不可用如实放弃，不以假 store 顶替 ----------
DOCKER="${DOCKER:-/Applications/Docker.app/Contents/Resources/bin/docker}"
if [ ! -x "$DOCKER" ]; then DOCKER="$(command -v docker 2>/dev/null || true)"; fi
if [ -x "$DOCKER" ] && "$DOCKER" info >/dev/null 2>&1; then
  say "环境：Docker 可用（${DOCKER}）→ 走真 Redis 链路"
else
  say "环境：Docker 不可用（探测路径=${DOCKER:-<none>}）→ 真 Redis 演练无法执行。"
  say "  本 bug 漏检根因＝误判 Docker 不可用后退化为假 store 演练；本脚本拒绝以假 store 冒充实链路。"
  exit 2
fi

DRILL_IMAGE="${DRILL_IMAGE:-library/redis:7-alpine}"
PORT="$($PY -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
CID="$("$DOCKER" run -d --rm --name "oneserve-drift-drill-$$" \
        -p "127.0.0.1:${PORT}:6379" "$DRILL_IMAGE" \
        redis-server --save "" --appendonly no)"
cleanup() { "$DOCKER" rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT
export SYNC_REDIS_URL="redis://127.0.0.1:${PORT}/0"
for _ in $(seq 1 60); do
  "$DOCKER" exec "$CID" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 0.25
done
"$DOCKER" exec "$CID" redis-cli ping | grep -q PONG || { say "Redis 容器未就绪"; exit 2; }
ok "起专属 Redis 容器（127.0.0.1:${PORT}，--rm 无名卷；未触碰任何共享栈/卷）"

# 现役演练目标（P2 2026-10-08 重定位）：唯一在册服务＋其表级字典真源
SID='v1-svc-usr-basic-info'
DICT_CSV='sync/schema_usr_basic_info.csv'
# 本脚本只会写下面这些 sync:drift:* 状态键；收尾按前缀清理（不碰业务键）
DRIFT_KEYS="sync:drift:${SID}"

# ---------- ② 真实配置对账：零误隔离 ----------
say "--- ② 真实配置对账（真 services.yaml＋auth_matrix.yaml＋表级真字典 ${DICT_CSV}）"
set +e; OUT="$($PY scripts/reconcile_drift.py --csv "$DICT_CSV" 2>&1)"; RC=$?; set -e
echo "$OUT"
CHECK="$(echo "$OUT" | $PY -c '
import sys, json
d = json.load(sys.stdin)
svc = d["services"]
bad = [k for k, s in svc.items() if s["state"] == "quarantined"]
print("quarantined=" + ",".join(sorted(bad)))
print("ok=" + ",".join(sorted(k for k, s in svc.items() if s["state"] == "ok")))
print("na=" + ",".join(sorted(k for k, s in svc.items() if s["state"] == "not_applicable")))
')"
echo "$CHECK"
if [ "$RC" -eq 0 ] && echo "$CHECK" | grep -q '^quarantined=$'; then
  ok "真实配置对账 exit 0 且零误隔离（现役 ${SID} 全 ok）"
else
  bad "真实配置对账出现误隔离或非 0 退出（rc=${RC}）"
fi
echo "$CHECK" | grep -q "ok=.*${SID}" \
  && ok "现役服务 ${SID} 如实 ok（表级字典 12 列全覆盖）" \
  || bad "现役服务 ${SID} 未在对账 ok 清单"
echo "$CHECK" | grep -q 'user_tag_query\|demo_hbase_query\|news_tag_query' \
  && bad "已下线演示服务仍出现在对账面（应随 2026-09-29 下线退场）" \
  || ok "已下线演示服务不在对账面（现役清单如实）"

# ---------- ③ 人为真删列 → 该服务 quarantined ----------
say "--- ③ 人为真删列（从字典副本移除被引用真列 gender_cd）→ 对账隔离"
TMP_CSV="$($PY - <<'PY'
import csv, tempfile
rows = list(csv.DictReader(open("sync/schema_usr_basic_info.csv", encoding="utf-8")))
fd, path = tempfile.mkstemp(suffix=".csv")
with open(fd, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
    for r in rows:
        if r["字段名"] != "gender_cd":
            w.writerow(r)
print(path)
PY
)"
set +e; OUT2="$($PY scripts/reconcile_drift.py --csv "$TMP_CSV" 2>&1)"; RC2=$?; set -e
echo "$OUT2"
echo "$OUT2" | $PY -c '
import sys, json
d = json.load(sys.stdin); svc = d["services"]
sid = "v1-svc-usr-basic-info"
assert svc[sid]["state"] == "quarantined", svc[sid]
assert svc[sid]["missing"] == ["gender_cd"], svc[sid]
print("  删列命中：%s = quarantined（missing=[gender_cd]）" % sid)
' || bad "删列后未按预期隔离"
[ "$RC2" -eq 1 ] && ok "删列对账 exit 1（如实报告有隔离态）" || bad "删列对账退出码应为 1，实得 $RC2"

# 真 Redis 状态键值核对
STATE_SVC="$("$DOCKER" exec "$CID" redis-cli GET "sync:drift:${SID}")"
echo "  state[sync:drift:${SID}]=$STATE_SVC"
echo "$STATE_SVC" | grep -q '"state":"quarantined"' \
  && ok "真 Redis 状态键＝quarantined" || bad "真 Redis 状态键非 quarantined"

# ---------- ④ 拒查：真执行器读真 Redis 状态键 ----------
say "--- ④ 拒查（真执行器＋真 Redis 状态键 → 503 / 明确错误码）"
KVKEY="abababababababababababababababababababababababababababababababab"
"$DOCKER" exec "$CID" redis-cli SET tags:usr_basic_info:latest 20260920 >/dev/null
"$DOCKER" exec "$CID" redis-cli SET "tags:usr_basic_info:20260920:$KVKEY" '{"gender_cd":"0","age_band":"A"}' >/dev/null
"$DOCKER" exec "$CID" redis-cli HSET sync:ver:usr_basic_info:20260920 data_as_of 2026-09-20 >/dev/null
$PY - <<'PY'
import asyncio, os, sys
sys.path.insert(0, "service")
import redis.asyncio as aioredis
from app import kv_executor as kvx
from app import schema_drift as sd

KEY = "ab" * 32

class RedisStore:
    """与 main.RedisStore 同形的真 Redis 适配器（异步 get/hget）。"""
    def __init__(self, c): self._c = c
    async def get(self, k): return await self._c.get(k)
    async def hget(self, k, f): return await self._c.hget(k, f)

async def main():
    r = aioredis.from_url(os.environ["SYNC_REDIS_URL"], decode_responses=True)
    services = kvx.load_services(); matrix = kvx.load_auth_matrix()
    svc = services["v1-svc-usr-basic-info"]
    grant = kvx.tenant_grant(matrix, "v1-svc-usr-basic-info", tenant="demo_merchant")
    raw = await r.get(sd.drift_state_key("v1-svc-usr-basic-info"))
    print("  真实执行前读到状态键：%s" % raw)
    assert sd.is_quarantined(sd.decode_state(raw)), "执行前真 Redis 状态键应为 quarantined"
    try:
        await kvx.execute(RedisStore(r), svc, grant, KEY)
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == sd.QUARANTINE_CODE, (exc.status, exc.code)
        print("  拒查响应：HTTP %d  error=%s" % (exc.status, exc.code))
        print("  人话：%s" % exc.message)
    else:
        raise SystemExit("隔离态仍返回数据（AC-8-b 红线）")
    await r.aclose()

asyncio.run(main())
PY
ok "执行器（真 Redis 状态键）命中隔离 → 503 / schema_drift_quarantined（不读脏）"

# ---------- ⑤ 增列不泄露 ----------
say "--- ⑤ 增列不泄露（字典多列、无人引用）"
TMP_ADD="$($PY - <<'PY'
import csv, tempfile
rows = list(csv.DictReader(open("sync/schema_usr_basic_info.csv", encoding="utf-8")))
fd, path = tempfile.mkstemp(suffix=".csv")
ADD = "zz_drill_added_238b"
with open(fd, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader()
    for r in rows: w.writerow(r)
    w.writerow({"attnum": 999, "字段名": ADD, "数据类型": "text", "cn_name": "演练增列",
                "业务口径": "演练", "敏感级": "内部"})
print(path)
PY
)"
set +e; OUT3="$($PY scripts/reconcile_drift.py --csv "$TMP_ADD" 2>&1)"; RC3=$?; set -e
echo "$OUT3" | $PY -c '
import sys, json
d = json.load(sys.stdin); svc = d["services"]
assert svc["v1-svc-usr-basic-info"]["state"] == "ok", svc["v1-svc-usr-basic-info"]
assert svc["v1-svc-usr-basic-info"]["added_count"] >= 1
assert "zz_drill_added_238b" not in json.dumps(d, ensure_ascii=False), "新增列名泄露进报告"
print("  增列：现役服务仍 ok（added_count=%d），新列名零出现于对账报告" % svc["v1-svc-usr-basic-info"]["added_count"])
' || bad "增列不泄露断言失败"
[ "$RC3" -eq 0 ] && ok "增列不触发隔离（对账 exit 0）" || bad "增列误触发隔离（rc=${RC3}）"
"$DOCKER" exec "$CID" redis-cli GET "sync:drift:${SID}" | grep -q 'zz_drill_added_238b' \
  && bad "新增列名泄露进状态键" || ok "新增列名零出现于状态键"

# ---------- ⑥ 恢复真字典 → --recover → 放行 ----------
say "--- ⑥ 恢复真字典 → CLI --recover → ok → 执行器放行"
# 真字典已恢复：先重跑对账写 ok（机器比对真 CSV），再 --recover 收敛（幂等）
set +e; $PY scripts/reconcile_drift.py --csv "$DICT_CSV" >/dev/null 2>&1; set -e
set +e; OUT5="$($PY scripts/reconcile_drift.py --csv "$DICT_CSV" --recover 2>&1)"; RCR=$?; set -e
echo "$OUT5"
[ "$RCR" -eq 0 ] && ok "真字典恢复 → CLI（对账＋--recover）→ 无残留隔离（exit 0）" \
  || bad "恢复后仍有隔离（rc=${RCR}）"
$PY - <<'PY'
import asyncio, os, sys
sys.path.insert(0, "service")
import redis.asyncio as aioredis
from app import kv_executor as kvx
from app import schema_drift as sd

KEY = "ab" * 32

class RedisStore:
    def __init__(self, c): self._c = c
    async def get(self, k): return await self._c.get(k)
    async def hget(self, k, f): return await self._c.hget(k, f)

async def main():
    r = aioredis.from_url(os.environ["SYNC_REDIS_URL"], decode_responses=True)
    services = kvx.load_services(); matrix = kvx.load_auth_matrix()
    svc = services["v1-svc-usr-basic-info"]
    grant = kvx.tenant_grant(matrix, "v1-svc-usr-basic-info", tenant="demo_merchant")
    raw = await r.get(sd.drift_state_key("v1-svc-usr-basic-info"))
    assert sd.decode_state(raw)["state"] == sd.STATE_OK, raw
    out = await kvx.execute(RedisStore(r), svc, grant, KEY)
    assert out["found"] is True and out["fields"] == {"age_band": "A", "gender_cd": "0"}, out
    print("  恢复后执行器放行：found=true fields=%s" % out["fields"])
    await r.aclose()

asyncio.run(main())
PY
ok "执行器（真 Redis）恢复后放行（found=true，字段按白名单投影）"

# ---------- ⑦ 清场（只删本脚本写入的 sync:drift:* 键；容器 --rm 自动回收） ----------
say "--- ⑦ 清场（删本脚本写入的 sync:drift:* 键；不动任何共享卷）"
for k in $DRIFT_KEYS; do "$DOCKER" exec "$CID" redis-cli DEL "$k" >/dev/null; done
LEFT="$("$DOCKER" exec "$CID" redis-cli --scan --pattern 'sync:drift:*' | wc -l | tr -d ' ')"
[ "$LEFT" = "0" ] && ok "sync:drift:* 已清空（残留=${LEFT}）" || bad "sync:drift:* 残留 ${LEFT} 个键"

say "===== 演练结果: PASS=$PASS FAIL=$FAIL ====="
say "自动化口径（如实）：隔离状态键由手工 CLI 写入；自动触发器归 M2 生产化，本阶段未实现。"
if [ "$FAIL" -eq 0 ]; then
  say "AC-8-b 真 Redis 演练全绿，证据已存 $EVIDENCE"
  exit 0
fi
say "演练存在失败项，见 $EVIDENCE"
exit 1
