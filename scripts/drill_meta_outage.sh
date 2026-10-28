#!/usr/bin/env bash
# ============================================================
# 拔库演练（卡-1b7 步8 · 蓝图阶段一验收线③）
#   DR runbook §5 六步在隔离环境实跑：拔 metadata-db → 数据面照跑（判据1）→
#   写端点 WRITE_FROZEN 拒绝＋告警（判据2）→ 拉起对账解冻试写闭环（判据3）。
#
# 隔离纪律（禁碰 dev 容器组）：
#   - 隔离栈只经 compose.metadata.yml -p oneserve-meta 单起（容器 oneserve-metadata-db）；
#   - 数据面最小服务面＝一次性 redis（oneserve-drill-redis）＋宿主机 uvicorn
#     （127.0.0.1 高位口，AUDIT_DIR/SNAPSHOT 全在 /tmp 演练区，仓内真源只读）；
#   - 演练库卷 down -v 清场；收尾 docker ps 清单与基线比对（drill 专名除外）。
#
# 用法：bash scripts/drill_meta_outage.sh          # 全六步，exit 0＝三判据全过
#   DRILL_KEEP=1 bash …                            # 失败保留现场（栈不拆，便于排查）
# 证据：tests/evidence/meta-outage-drill-<日期>/（拒绝体 JSON、告警行、version_history、
#   快照 md5、uvicorn 日志、判据摘要）
# 前置：docker 可达；宿主机 python3 带 fastapi/uvicorn/redis/yaml；
#   镜像 postgres:17-alpine、redis:7-alpine 在位（离线环境不拉取）。
# 口令：演练六口令临时生成于 /tmp（chmod 600，永不进仓）。
# 口径注记（演练实证 2026-10-20）：
#   - 引导超户 META_DB_USER 不得与三业务角色同名（同名＝01_roles.sh 建角色撞车，
#     initdb 失败、容器退出码 3）——演练用 meta_boot；
#   - db 态对账口径：迁移窗口用「四源 vs 表 vs 快照」三方；运行期合法写只落库＋快照
#     （源 yaml 是迁移基线不动），故恢复对账＝DB↔快照两方（reconcile 源侧取快照目录）。
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE=(docker compose -f compose.metadata.yml -p oneserve-meta)
DRILL_DATE=${DRILL_DATE:-$(date +%Y%m%d)}
EV=tests/evidence/meta-outage-drill-$DRILL_DATE
WORK=${DRILL_WORK:-$(mktemp -d /tmp/drill-meta.XXXXXX)}
PORT_REDIS=${PORT_REDIS:-18799}
PORT_SVC=${PORT_SVC:-18780}
AUDIT_DIR="$WORK/audit"
SRC="$WORK/src"
SNAP="$WORK/snapshots"
UV_PID=""

step() { printf '\n════ 步 %s｜%s ════\n' "$1" "$2"; }
ok()   { printf '  [PASS] %s\n' "$1"; }
die()  { printf '  [FAIL] %s\n' "$1" >&2; exit 1; }
need() { command -v "$1" >/dev/null || die "缺依赖：$1"; }
cleanup() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ "${DRILL_KEEP:-0}" != "1" ]; then
    echo "（失败清场：DRILL_KEEP=1 可保留现场排查）"
    lsof -ti :"$PORT_SVC" 2>/dev/null | xargs kill -9 2>/dev/null || true
    docker rm -f oneserve-drill-redis >/dev/null 2>&1 || true
    "${COMPOSE[@]}" --env-file "$WORK/drill.env" down -v >/dev/null 2>&1 || true
  fi
  exit $rc
}
trap cleanup EXIT

# ── 步 0｜前置与清场 ──────────────────────────────────────────
step 0 "前置与清场"
need docker; need curl; need openssl
if command -v lsof >/dev/null; then
  for p in "$PORT_REDIS" "$PORT_SVC"; do
    lsof -ti :"$p" >/dev/null 2>&1 && die "端口 $p 被占用（上轮残留进程？先清理：lsof -ti :$p | xargs kill）"
  done
fi
python3 -c "import fastapi, uvicorn, redis, yaml" 2>/dev/null || die "宿主机缺 python 包（fastapi/uvicorn/redis/yaml）"
docker info >/dev/null 2>&1 || die "docker 不可达"
for img in postgres:17-alpine library/redis:7-alpine; do
  docker images -q "$img" | grep -q . || die "镜像未就位：${img}（离线不拉取）"
done
rm -rf "$WORK"; mkdir -p "$WORK" "$AUDIT_DIR" "$EV"
umask 077
{
  echo "META_DB_USER=meta_boot"
  echo "META_DB_PASSWORD=$(openssl rand -hex 16)"
  echo "META_DB_NAME=oneserve_meta"
  echo "META_APP_PASSWORD=$(openssl rand -hex 16)"
  echo "META_RO_PASSWORD=$(openssl rand -hex 16)"
  echo "META_MIGRATE_PASSWORD=$(openssl rand -hex 16)"
} > "$WORK/drill.env"
chmod 600 "$WORK/drill.env"
docker rm -f oneserve-drill-redis >/dev/null 2>&1 || true
"${COMPOSE[@]}" --env-file "$WORK/drill.env" down -v >/dev/null 2>&1 || true
# dev 组基线采集须在演练自清理之后（上轮残留的演练容器不得计入基线）
BASE_PS=$(docker ps --format '{{.Names}}' | sort | grep -v '^oneserve-drill' || true)
echo "dev 组基线容器数：$(echo "$BASE_PS" | wc -l | tr -d ' ')（收尾比对，drill 专名除外）"
mkdir -p "$SRC/registry"
cp service/services.yaml service/upstreams.yaml service/auth_matrix.yaml "$SRC/"
cp service/registry/appkeys.yaml "$SRC/registry/"
ok "演练区 ${WORK}（口令 /tmp 600）；四源副本就绪；演练卷清零"

# ── 步 1｜正常态起隔离栈＋基线快照 ────────────────────────────
step 1 "正常态起隔离栈（含 metadata-db）→ 基线快照生成"
"${COMPOSE[@]}" --env-file "$WORK/drill.env" up -d >/dev/null
st=starting
for i in $(seq 1 45); do
  st=$(docker inspect oneserve-metadata-db --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  [ "$st" = healthy ] && break; sleep 2
done
[ "$st" = healthy ] || die "metadata-db 45×2s 未 healthy（st=${st}）"
ok "oneserve-metadata-db healthy（postgres:17-alpine，卷 oneserve-meta_oneserve-metadata-db-data）"
"${COMPOSE[@]}" --env-file "$WORK/drill.env" exec -T metadata-db \
  psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -q -f - \
  < migrations/0001_meta_init.sql >/dev/null
ok "十表 DDL 应用（migrations/0001_meta_init.sql，meta_migrate 角色）"
# 0002+ 扩展链（2026-10-21 微调，收口批读侧小批顺带）：0002 起 grant 扩展列
# （effective_from/to 等）入库——只 apply 0001＝apply 对账缺列红（且 2>&1|tail -3
# 会因 stdout 块缓冲把 stderr 错误行挤出尾窗，apply 段同批改落盘全量可见）。
# 迁移链逐个以 meta_migrate 真实通道应用，失败即 die 不桥接（2026-10-21 微批更新）：
# 历史缺口＝0003/0004 membership GRANT（usage_app/usage_ro → meta_app/meta_ro）需
# ADMIN option 而 configs/metadata-db/init/01_roles.sh 未授 meta_migrate，曾以「引导
# 超户 meta_boot 桥接＋WARN」兜底（越域缺陷如实登记在案）；根修已落＝init 补
# GRANT usage_app, usage_ro TO meta_migrate WITH ADMIN OPTION，桥接随之拆除——演练
# 回归「真实三角色通道」验证本位：超户桥接会重新制造「e2e 超户起库测不出、生产实爆」
# 盲区，权限契约再破应当场红。迁移链＝已跟踪（已提交）迁移按序，单一工作树并行
# 纪律——他批未提交的在途迁移不入演练链，提交后自动纳入。
for mig in $(git ls-files 'migrations/*.sql' | grep -v 0001_meta_init); do
  "${COMPOSE[@]}" --env-file "$WORK/drill.env" exec -T metadata-db \
      psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -q -f - \
      < "$mig" >"$WORK/mig.err" 2>&1 \
    || { tail -8 "$WORK/mig.err" >&2; die "迁移链以 meta_migrate 应用失败（真实三角色通道不桥接）：$mig"; }
done
ok "迁移链按序应用（已跟踪迁移；meta_migrate 真实三角色通道，无超户桥接）"
META_MIGRATE_SRC_DIR="$SRC" python3 scripts/meta_migrate.py apply > "$WORK/apply.log" 2>&1 \
  || { tail -15 "$WORK/apply.log" >&2; die "四 yaml 迁移装载失败（apply 非零，完整输出 $WORK/apply.log）"; }
tail -3 "$WORK/apply.log"
ok "四 yaml 迁移装载（apply 幂等；装载后六表对账全 OK）"
export META_WRITER=db META_SNAPSHOT_DIR="$SNAP" META_MIGRATE_SRC_DIR="$SRC"
python3 scripts/meta_reconcile.py --baseline >/dev/null
python3 scripts/meta_reconcile.py >/dev/null
ok "基线快照导出（库→快照）＋三方对账绿（源＝四源副本，迁移窗口口径）"
( cd "$SNAP" && md5 -r services.yaml upstreams.yaml registry/appkeys.yaml auth_matrix.yaml 2>/dev/null \
  || md5sum services.yaml upstreams.yaml registry/appkeys.yaml auth_matrix.yaml ) \
  | tee "$EV/snapshot-baseline-md5.txt"
ok "快照版本号（md5 四件套）已留证"
docker run -d --rm --name oneserve-drill-redis -p 127.0.0.1:$PORT_REDIS:6379 \
  library/redis:7-alpine >/dev/null
( cd service && REDIS_URL=redis://127.0.0.1:$PORT_REDIS/0 AUDIT_DIR="$AUDIT_DIR" \
    META_WRITER=db META_SNAPSHOT_DIR="$SNAP" \
    REGISTRY_POLL_INTERVAL_S=3 nohup python3 -m uvicorn app.main:app \
    --host 127.0.0.1 --port $PORT_SVC > "$WORK/uvicorn.log" 2>&1 & echo $! > "$WORK/uvicorn.pid" )
UV_PID=$(cat "$WORK/uvicorn.pid")
code=000
for i in $(seq 1 30); do
  sleep 1
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_SVC/healthz || true)
  [ "$code" = 200 ] && break
done
[ "$code" = 200 ] || die "服务面 30s 未就绪（healthz=${code}，日志见 $WORK/uvicorn.log）"
ok "最小服务面就绪（uvicorn 127.0.0.1:$PORT_SVC ＋ 一次性 redis :${PORT_REDIS}）"
AK=$(python3 -c "import yaml; d=yaml.safe_load(open('service/registry/appkeys.yaml')); print(next(k['appkey'] for k in d['keys'] if k.get('tenant')=='demo_bank' and k.get('status','active')=='active'))")
docker exec oneserve-drill-redis redis-cli SET tags:usr_asset_tag:latest v1 >/dev/null
docker exec oneserve-drill-redis redis-cli SET 'tags:usr_asset_tag:v1:13000000001' '{"aum_band":"A","dep_bal_band":"B"}' >/dev/null
docker exec oneserve-drill-redis redis-cli HSET sync:ver:usr_asset_tag:v1 data_as_of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
ping_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_SVC/v1/ping)
q_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT_SVC/v1/query/v1-svc-usr-asset-tag \
  -H "apikey: $AK" -H 'Content-Type: application/json' -d '{"key":"13000000001"}')
[ "$ping_code" = 200 ] || die "基线 /v1/ping 非 200（${ping_code}）"
[ "$q_code" = 200 ] || die "基线 /v1/query 非 200（${q_code}）"
ok "基线数据面：/v1/ping 200；/v1/query/v1-svc-usr-asset-tag 200（灌数生效）"

# ── 步 2｜拔库 → 数据面照跑（判据1） ──────────────────────────
step 2 "拔库（stop metadata-db）→ 数据面照跑"
"${COMPOSE[@]}" --env-file "$WORK/drill.env" stop metadata-db >/dev/null
for i in 1 2 3; do
  if docker exec oneserve-metadata-db pg_isready -U meta_boot -d oneserve_meta >/dev/null 2>&1; then
    die "pg_isready 第 $i 次可达（拔库未生效）"
  fi
  echo "  pg_isready 第 $i 连败（UNREACHABLE）"
done
ok "不可达三连败（docker exec pg_isready；DR runbook §2 确认口径）"
sleep 1
ping_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT_SVC/v1/ping)
q_body=$(curl -s -X POST http://127.0.0.1:$PORT_SVC/v1/query/v1-svc-usr-asset-tag \
  -H "apikey: $AK" -H 'Content-Type: application/json' -d '{"key":"13000000001"}')
echo "$q_body" > "$EV/judge1-query-body.json"
q_code=$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:$PORT_SVC/v1/query/v1-svc-usr-asset-tag \
  -H "apikey: $AK" -H 'Content-Type: application/json' -d '{"key":"13000000001"}')
[ "$ping_code" = 200 ] || die "拔库后 /v1/ping 非 200（${ping_code}）"
[ "$q_code" = 200 ] || die "拔库后 /v1/query 非 200（${q_code}）"
echo "$q_body" | grep -q '"found":true' || die "拔库后 query 非 found:true（数据面读径受损）"
ok "判据1 √ 数据面照起照跑：/v1/ping 200；/v1/query 200 且 found=true（读侧零 DB 依赖）"

# ── 步 3｜写操作被拒＋告警（判据2） ──────────────────────────
step 3 "写操作被拒＋告警（WRITE_FROZEN）"
pub_code=$(curl -s -o "$EV/judge2-publish-frozen.json" -w '%{http_code}' -X POST \
  http://127.0.0.1:$PORT_SVC/v1/admin/publish -H 'Content-Type: application/json' -d '{
  "serviceName":"drill-frozen","routePath":"/v1/svc/drill-frozen","fieldSpace":"news",
  "fields":[{"name":"aum_band","cf":"cf","type":"string","cnName":"演练冻结态发布"}],
  "env":"prod","instance":"demo-bank","status":"draft","datasource":"redis",
  "oaTicket":"DRILL-FROZEN-001","operator":"drill"}')
grep -q WRITE_FROZEN "$EV/judge2-publish-frozen.json" || die "发布拒绝体无 WRITE_FROZEN"
[ "$pub_code" = 503 ] || die "发布拒绝码非 503（${pub_code}）"
ok "发布域写拒：POST /v1/admin/publish → 503 WRITE_FROZEN"
quota_code=$(curl -s -o "$EV/judge2-quota-frozen.json" -w '%{http_code}' -X POST \
  "http://127.0.0.1:$PORT_SVC/v1/admin/appkeys/$AK/quota" \
  -H 'Content-Type: application/json' -d '{"quota_qps":66,"quota_daily":6600}')
grep -q WRITE_FROZEN "$EV/judge2-quota-frozen.json" || die "凭证拒绝体无 WRITE_FROZEN"
[ "$quota_code" = 503 ] || die "凭证拒绝码非 503（${quota_code}）"
ok "凭证域写拒：POST /v1/admin/appkeys/{ak}/quota → 503 WRITE_FROZEN"
set +e
META_WRITER=db META_SNAPSHOT_DIR="$SNAP" META_MIGRATE_SRC_DIR="$SRC" \
  python3 -c "
import sys; sys.path.insert(0, 'service')
from app import meta_writer as mw
try:
    mw.matrix_save('$SRC/auth_matrix.yaml', {'matrix': []}, operator='drill')
    print('授权域直调意外成功（应为 MetaDbError）'); sys.exit(1)
except mw.MetaDbError as e:
    print('授权域写拒（mw.matrix_save → MetaDbError）：', str(e)[:90]); sys.exit(3)
"
mtx_rc=$?
set -e
[ "$mtx_rc" = 3 ] || die "授权域 mw.matrix_save 直调未按 MetaDbError 拒绝（rc=${mtx_rc}）"
ok "授权域写拒（等价实证）：mw.matrix_save db 入口直调 → MetaDbError 非零退出"
grep -q 'P1·写冻结' "$WORK/uvicorn.log" || die "服务日志无 P1·写冻结 告警"
grep 'P1·写冻结' "$WORK/uvicorn.log" | head -1 > "$EV/judge2-alert-line.txt"
ok "告警已发出：服务日志 P1·写冻结（runbook §2 模板口径，已截取留证）"
echo "  口径注记：grants_admin.save_matrix 已双模接 mw（META_WRITER=db → matrix_save 差量"
echo "  写库＋快照导出；缺省 yaml 态原文件写不变——6b8c02e 收口批项3 接线），拔库窗口 db"
echo "  态矩阵写同样撞 WRITE_FROZEN 拒绝——本步 mw.matrix_save 直调即其等价实证"
ok "判据2 √ 发布/凭证/授权写拒＋告警（拒绝形态与 runbook §2 影响面一致）"

# ── 步 4｜拉起 pg → 对账 → 解冻闭环（判据3） ──────────────────
step 4 "拉起 pg → 对账 → 解冻闭环"
"${COMPOSE[@]}" --env-file "$WORK/drill.env" start metadata-db >/dev/null
st=starting
for i in $(seq 1 45); do
  st=$(docker inspect oneserve-metadata-db --format '{{.State.Health.Status}}' 2>/dev/null || echo starting)
  [ "$st" = healthy ] && break; sleep 2
done
[ "$st" = healthy ] || die "恢复后 45×2s 未 healthy（st=${st}）"
ok "metadata-db 已拉起 healthy"
export META_WRITER=db META_SNAPSHOT_DIR="$SNAP"
META_MIGRATE_SRC_DIR="$SNAP" python3 scripts/meta_reconcile.py >/dev/null
ok "恢复对账清零：DB↔快照两方 exit 0（运行期口径＝源侧取快照，见头注）"
BEFORE_MT=$(stat -f %m "$SNAP/services.yaml" 2>/dev/null || stat -c %Y "$SNAP/services.yaml")
thaw_code=$(curl -s -o "$EV/judge3-thaw-publish.json" -w '%{http_code}' -X POST \
  http://127.0.0.1:$PORT_SVC/v1/admin/publish -H 'Content-Type: application/json' -d '{
  "serviceName":"drill-smoke","routePath":"/v1/svc/drill-smoke","fieldSpace":"news",
  "fields":[{"name":"aum_band","cf":"cf","type":"string","cnName":"演练解冻试写"}],
  "env":"prod","instance":"demo-bank","status":"draft","datasource":"redis",
  "oaTicket":"DRILL-THAW-001","operator":"drill-thaw"}')
[ "$thaw_code" = 200 ] || die "解冻试写非 200（${thaw_code}）"
ok "解冻＝试写一笔（冻结为派生态，无独立开关）：POST /v1/admin/publish → 200"
AFTER_MT=$(stat -f %m "$SNAP/services.yaml" 2>/dev/null || stat -c %Y "$SNAP/services.yaml")
[ "$AFTER_MT" -gt "$BEFORE_MT" ] || die "快照 mtime 未更新（导出未随动）"
ok "快照 mtime 已随试写更新"
"${COMPOSE[@]}" --env-file "$WORK/drill.env" exec -T metadata-db psql -U meta_migrate \
  -d oneserve_meta -A -t -c "SELECT operator||' | '||source||' | '||entity||' | '||occurred_at \
  FROM meta.version_history ORDER BY occurred_at DESC LIMIT 1;" | tee "$EV/judge3-version-history.txt"
grep -q drill-thaw "$EV/judge3-version-history.txt" || die "version_history 无解冻试写新行"
ok "version_history 出现解冻试写新行（drill-thaw）"
META_MIGRATE_SRC_DIR="$SNAP" python3 scripts/meta_reconcile.py >/dev/null
ok "复跑对账仍绿（exit 0）"
ok "判据3 √ 恢复→对账清零→解冻→写一笔快照更新闭环"

# ── 步 5｜证据归档 ────────────────────────────────────────────
step 5 "演练记录归档"
cp "$WORK/uvicorn.log" "$EV/uvicorn.log"
{ echo "判据1 数据面照跑：拔库窗口 ping/query 200；判据2 写拒 WRITE_FROZEN×2＋授权域直调拒＋P1 告警；判据3 对账清零→试写 200→快照随动→复跑绿"; } > "$EV/judges-summary.txt"
ok "证据落 $EV/（拒绝体 JSON、告警行、version_history、快照 md5、uvicorn 日志）"

# ── 步 6｜收尾与 dev 组零触碰核对 ─────────────────────────────
step 6 "收尾与零触碰核对"
for sig in TERM TERM KILL; do
  lsof -ti :"$PORT_SVC" 2>/dev/null | xargs kill -$sig 2>/dev/null || true
  for i in $(seq 1 10); do
    lsof -ti :"$PORT_SVC" >/dev/null 2>&1 || break; sleep 0.5
  done
  lsof -ti :"$PORT_SVC" >/dev/null 2>&1 || break
done
lsof -ti :"$PORT_SVC" >/dev/null 2>&1 && die "演练服务面杀不掉，端口 $PORT_SVC 仍被占（lsof -ti :$PORT_SVC | xargs kill -9）"
docker rm -f oneserve-drill-redis >/dev/null 2>&1 || true
"${COMPOSE[@]}" --env-file "$WORK/drill.env" down -v >/dev/null
NOW_PS=$(docker ps --format '{{.Names}}' | sort | grep -v '^oneserve-drill' || true)
if [ "$BASE_PS" != "$NOW_PS" ]; then
  diff <(echo "$BASE_PS") <(echo "$NOW_PS") || true
  die "dev 组容器清单变化（零触碰核对失败）"
fi
ok "演练栈已拆（down -v 卷零残留）；dev 组容器清单与基线一致（零触碰）"
echo
echo "════ 拔库演练通过：三判据全过（详见 $EV/）════"
