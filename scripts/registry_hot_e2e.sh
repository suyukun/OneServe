#!/usr/bin/env bash
# TASK-M2-3.5 e2e：AC-6 判据实录——「新增一个服务＝只改配置，零代码改动，60s 内 curl 可调」。
# 链路：compose 起栈（REGISTRY_POLL_INTERVAL_S=15）→ init_routes 渲染路由 →
#       基线（既有服务走通配端点/B 型不进通配/401）→ 追加 services.yaml 条目（现场改文件）
#       → 热加载轮询生效（网关路由渲染下发＋注册表换新）→ 控制台签发凭证（P0-4 既有
#       配置面能力，显式授权新服务；授权矩阵文件容器内未挂载，登记走签发通道）
#       → 60s 内 curl /v1/query/demo_dynamic_query 200。
# 用法：bash scripts/registry_hot_e2e.sh   （断言全绿 exit 0；证据存 tests/evidence/）
set -euo pipefail
export PATH="$HOME/.docker/bin:$PATH"
cd "$(dirname "$0")/.."

EVIDENCE_DIR="tests/evidence"
EVIDENCE="${M235_EVIDENCE:-$EVIDENCE_DIR/m235-hot-reload.txt}"
PASS=0; FAIL=0
GW="http://localhost:9080"
PORTAL="http://localhost:8000"          # 门户 nginx：/v1/admin/* 反代服务层（P0-4）
ADMIN_TOKEN="admin321"

say() { echo "[m235-e2e] $*"; }
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR"
exec > >(tee "$EVIDENCE") 2>&1
say "===== TASK-M2-3.5 渲染器/热加载 e2e $(date '+%F %T') ====="

# 现场改动文件备份（判据＝运行期改配置；验完恢复仓库原状，改动不进 commit）
cp service/services.yaml /tmp/m235-services.yaml.bak
cp service/registry/appkeys.yaml /tmp/m235-appkeys.yaml.bak
cleanup() {
  cp /tmp/m235-services.yaml.bak service/services.yaml 2>/dev/null || true
  cp /tmp/m235-appkeys.yaml.bak service/registry/appkeys.yaml 2>/dev/null || true
}
trap cleanup EXIT

docker info >/dev/null 2>&1 || { echo "docker daemon 不可用"; exit 2; }

# ── 共享栈破坏闸（9-28 事故后立规）──────────────────────────────────────────
# 本脚本直写共享栈：compose up 重建共享容器、向共享 oneserve-redis SET
# tags:user:latest / tags:user:20260920:* 等键、改仓库 services.yaml（验后恢复）、
# 经共享网关加路由。Redis 有版本空间隔离但 latest 指针/限频键是共享的；默认拒绝，
# 须显式 E2E_ALLOW_SHARED=1 放行（脚本自身有备份恢复与幂等清零，风险可控时用）。
if [ "${E2E_ALLOW_SHARED:-0}" != "1" ]; then
  say "✗ 默认拒绝：本脚本直写共享栈（共享 Redis 指针键 tags:user:latest / services.yaml 现场改 / 共享网关路由），影响并行会话现场（9-28 事故后立规）。"
  say "  - 需要跑：E2E_ALLOW_SHARED=1 bash scripts/registry_hot_e2e.sh（脚本自带验后恢复）"
  exit 2
fi
say "[共享闸放行] E2E_ALLOW_SHARED=1 → 允许直写共享栈（自担风险；验后自动恢复 services.yaml/appkeys.yaml）"

# 0) 起栈：轮询间隔 15s（60s 判据的充裕口径；同时验证 env 覆盖通路）
say "① compose up（REGISTRY_POLL_INTERVAL_S=15）..."
REGISTRY_POLL_INTERVAL_S=15 docker compose up -d --build >/dev/null
for svc in etcd redis apisix service; do
  for i in $(seq 1 60); do
    st=$(docker inspect -f '{{.State.Health.Status}}' "oneserve-$svc" 2>/dev/null || echo starting)
    [ "$st" = "healthy" ] && break
    sleep 2
  done
  [ "$st" = "healthy" ] || { bad "$svc 未健康"; exit 1; }
done
ok "五组件健康（轮询间隔 15s 已注入 service 容器）"
# 签发限频计数清零（演示栈自有键 appkeys:rl:*；多轮 e2e 重跑会累计烧窗 5 次/小时）
docker compose exec -T redis redis-cli --scan --pattern 'appkeys:rl:*' 2>/dev/null \
  | while IFS= read -r k; do
      [ -n "$k" ] || continue
      docker compose exec -T redis redis-cli DEL "$k" >/dev/null
      echo "  · 已清演示栈签发限频计数（$k）"
    done || true
POLL=$(docker compose exec -T service printenv REGISTRY_POLL_INTERVAL_S)
[ "$POLL" = "15" ] && ok "容器内 REGISTRY_POLL_INTERVAL_S=$POLL" || bad "轮询间隔 env 未生效: $POLL"

# 1) init_routes：路由由 services.yaml 渲染（通配＋兼容期专用）
say "② init_routes 渲染下发..."
INIT_OUT=$(docker compose exec -T service python /init/init_routes.py)
echo "$INIT_OUT"
echo "$INIT_OUT" | grep -q "routes/oneserve-query-user_tag_query" && ok "渲染路由含通配 user_tag_query" || bad "渲染路由缺通配条目"
echo "$INIT_OUT" | grep -q "routes/oneserve-v1-tags-query" && ok "兼容期专用路由保留" || bad "兼容期路由缺失"
echo "$INIT_OUT" | grep -qE "APISIX 初始化完成" && ok "init_routes 全部 PUT 成功" || bad "init_routes 存在失败项"

# 2) 基线：既有服务经通配端点可调（灌 user_tag_query 演示数据）
say "③ 基线：既有服务走通配端点..."
KEY=$(python3 -c "import sys; sys.path.insert(0,'service'); from app.kv_executor import sm3_hex; print(sm3_hex('13800138000'))")
docker compose exec -T redis redis-cli SET tags:user:latest 20260920 >/dev/null
docker compose exec -T redis redis-cli SET "tags:user:20260920:$KEY" '{"ac_active_sts_01":"band_C","if_acct_fct_24":"523877","mk_touch_sts_02":"lvl_2"}' >/dev/null
docker compose exec -T redis redis-cli HSET sync:ver:20260920 data_as_of 2026-09-20 rows 1 >/dev/null
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/user_tag_query" \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}")
[ "$CODE" = "200" ] && ok "既有服务经通配端点 200（/v1/query/user_tag_query）" || bad "通配端点基线应 200，实得 $CODE"
BODY=$(curl -s -X POST "$GW/v1/query/user_tag_query" -H "apikey: oneserve-dev-appkey-001" \
  -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}")
echo "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["found"] is True and d["fields"]["if_acct_fct_24"]=="B"' \
  && ok "通配端点响应与专用端点同链路（投影＋脱敏）" || bad "通配端点响应异常: $BODY"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/partner_proxy" \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" -d "{}")
[ "$CODE" = "404" ] && ok "B 型代理不在通配路由（404＝渲染器判定正确）" || bad "partner_proxy 通配应 404，实得 $CODE"
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/user_tag_query" \
  -H "Content-Type: application/json" -d "{}")
[ "$CODE" = "401" ] && ok "通配路由 key-auth 生效（无 key 401）" || bad "无 key 应 401，实得 $CODE"

# 3) AC-6 判据主体：追加服务条目（现场改 yaml，零代码）→ 热加载 → 60s 内 curl 200
say "④ 新增服务 demo_dynamic_query（只改 services.yaml，零代码）..."
T0=$(date +%s)
cat >> service/services.yaml <<'YAML'

  # —— TASK-M2-3.5 e2e：热加载判据样本（现场追加；验后恢复，不进 commit）——
  - service_id: demo_dynamic_query
    version: v1
    archetype: kv_lookup
    backend:
      source: redis
      kv:
        namespace: tags:demo
        alias_key: tags:demo:latest
        data_key: "tags:demo:{version}:{sm3}"
        version_meta_key: "sync:ver:demo:{version}"
        storage_key: as_received
        entity: user
    params:
      - name: key
        type: string(64)
        key_type: phone_sm3
        pattern: '^[0-9a-f]{64}$'
    response:
      projection: auth_matrix
      meta: [data_as_of, live_version]
    guard:
      timeout_ms: 3000
      max_items_per_call: 1
    audit:
      enabled: true
      log_params: hash_only
      log_response_digest: sha256_of_desensitized_body
YAML
ok "services.yaml 追加 demo_dynamic_query 条目（文件改动即全部改动）"
docker compose exec -T redis redis-cli SET tags:demo:latest 20260923 >/dev/null
docker compose exec -T redis redis-cli SET "tags:demo:20260923:$KEY" '{"ac_active_sts_01":"band_A","mk_touch_sts_02":"lvl_1"}' >/dev/null
docker compose exec -T redis redis-cli HSET sync:ver:demo:20260923 data_as_of 2026-09-23 rows 1 >/dev/null
ok "新服务演示数据已灌（tags:demo 键空间）"

say "⑤ 等热加载生效（60s 判据窗；轮询 2s；403＝注册表已生效待授权，随即经管理面签发）..."
NEWKEY=""
CURKEY="oneserve-dev-appkey-001"
ELAPSED=""
ISSUED=0
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/demo_dynamic_query" \
    -H "apikey: $CURKEY" -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}" || echo 000)
  if [ "$CODE" = "403" ] && [ "$ISSUED" = "0" ]; then
    # 注册表热加载已生效（403＝service_not_granted，新服务已被识别）；
    # 授权登记走既有配置面：控制台签发凭证（P0-4 admin API，显式授权新服务，零代码）
    ISSUE=$(curl -s -X POST "$PORTAL/v1/admin/appkeys" \
      -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{
        "tenant": "demo_bank", "app_name": "m235-hot-reload-e2e",
        "key_type": "sandbox", "caller_name": "demo_bank",
        "oa_ticket": "OA-PENDING-M235",
        "grants": [{"service": "demo_dynamic_query",
                    "fields": ["ac_active_sts_01", "mk_touch_sts_02"]}]}')
    NEWKEY=$(echo "$ISSUE" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("appkey",""))' 2>/dev/null || true)
    ISSUED=1                          # 单次签发尝试：限频 5 次/小时（P0-4），不重试烧窗
    if [ -n "$NEWKEY" ]; then
      CURKEY="$NEWKEY"
      ok "控制台签发凭证成功（appkey=${NEWKEY:0:10}…，显式授权 demo_dynamic_query）"
    else
      echo "  · 签发失败: $ISSUE"
    fi
  fi
  if [ "$CODE" = "200" ]; then ELAPSED=$(( $(date +%s) - T0 )); break; fi
  sleep 2
done
if [ -n "$ELAPSED" ]; then
  ok "热加载生效：/v1/query/demo_dynamic_query 200（耗时 ${ELAPSED}s ≤ 60s，判据 AC-6）"
  [ "$ELAPSED" -le 60 ] && ok "60s 判据达标" || bad "超窗：${ELAPSED}s"
else
  bad "60s+ 内未生效（判据红）"
fi
BODY=$(curl -s -X POST "$GW/v1/query/demo_dynamic_query" \
  -H "apikey: $CURKEY" -H "Content-Type: application/json" -d "{\"key\": \"$KEY\"}")
echo "  · 新服务响应体: $BODY"
echo "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d["service_id"]=="demo_dynamic_query" and d["found"] is True and "data_as_of" in d' \
  && ok "新服务执行链路完整（service_id/found/data_as_of）" || bad "新服务响应异常: $BODY"

# 4) 收尾复核：网关路由幂等重跑＋热加载日志
say "⑥ 幂等与日志复核..."
R2=$(docker compose exec -T service python /init/init_routes.py | grep -cE " -> 20[01]")
[ "$R2" -ge 9 ] && ok "init_routes 重跑幂等（$R2 条 PUT 全部 200/201）" || bad "重跑 PUT 异常: $R2"
HL=$(docker compose logs service 2>&1 | grep -acE "注册表热加载完成" || true)
WM=$(docker compose logs service 2>&1 | grep -acE "热加载看门启动" || true)
echo "  · 日志计数：热加载完成=$HL 看门启动=$WM"
[ "${HL:-0}" -ge 1 ] && ok "service 日志含「注册表热加载完成」（mtime 轮询触发实录）" \
  || { [ "${WM:-0}" -ge 1 ] && ok "service 日志含「热加载看门启动」（看门在跑；完成行见 ${HL:-0}）" || bad "未见热加载日志"; }
UNREG=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/query/nonexistent_svc" \
  -H "apikey: oneserve-dev-appkey-001" -H "Content-Type: application/json" -d "{}")
[ "$UNREG" = "404" ] && ok "未注册服务 404（网关无路由即拒）" || bad "未注册服务应 404，实得 $UNREG"

say "===== e2e 结果: PASS=$PASS FAIL=$FAIL ====="
if [ "$FAIL" -eq 0 ] && [ "$PASS" -ge 8 ]; then say "AC-6 判据全绿，证据已存 $EVIDENCE"; exit 0; fi
exit 1
