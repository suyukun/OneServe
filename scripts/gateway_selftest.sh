#!/usr/bin/env bash
# ============================================================
# 网关层只读自检（1.10-6 归 up.sh 链路 · 晚间批卡4 · 2026-10-21）
#
#   出处：docs/design/管理功能群一期设计_v1.md §1.10-6（验收命令）
#         ＋ docs/plans/BACKLOG.md 挂账「1.10-6 网关层 curl 实测（归 up.sh 链路）」
#   验证面＝APISIX 路由/凭证链路（与 configs/apisix/init_routes.py 同源）：
#     a. /v1/ping        无 key     → 401   key-auth 生效（AGENTS.md 常用命令口径）
#     b. /v1/ping        带 dev key → 200   凭证有效、路由在位（端到端全链）
#     c. /v1/tags/query  带 key     → 过网关认证达服务（命令＝ecs_seed.sh 自检 c 同款；
#                                     网关层断言非 401 且路由命中；数据命中 found 断言
#                                     归灌数自检 c，此处不依赖种子数据）
#     d. /demo           无 key     → 200 或服务层 auth_required 401
#                                   （网关层不挂 key-auth；2026-10-15 起统一登录 Bearer 门禁
#                                     覆盖演示通道——401 信封来自服务层而非 APISIX，路由在位）
#   只读承诺：全部 GET／既有业务查询，无任何建删改；dev key 只运行时从 .env 读，
#   不写进本脚本、证据里只记掩码（密钥出库批 2026-10-20 同口径）。
#
# 用法：
#   bash scripts/gateway_selftest.sh                    # 打印 ✓/✗，exit 0=全过 / 1=有败
#   GW_BASE=http://localhost:9080 bash scripts/gateway_selftest.sh
#   bash scripts/gateway_selftest.sh --evidence <dir>   # 追加落证据（命令/状态码/片段/时间戳）
#   APISIX_APPKEY=xxx bash scripts/gateway_selftest.sh  # 覆盖 .env（仅测试本脚本用）
# 环境变量：GW_BASE（默认 http://localhost:9080）
# 证据惯例：tests/evidence/<目录>/（沿 hbase_e2e 等既有落法）
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

GW_BASE=${GW_BASE:-http://localhost:9080}
EVID_DIR=""
if [ "${1:-}" = "--evidence" ]; then EVID_DIR="${2:?--evidence 需要目录参数}"; mkdir -p "$EVID_DIR"; fi

# key 出库：环境变量优先，缺省读 .env（不回显全值）
if [ -z "${APISIX_APPKEY:-}" ] && [ -f .env ]; then
  APISIX_APPKEY=$(sed -n 's/^APISIX_APPKEY=//p' .env | head -1)
fi
if [ -z "${APISIX_APPKEY:-}" ]; then echo "✗ APISIX_APPKEY 未设置且 .env 缺该键" >&2; exit 2; fi
KEY_MASKED="${APISIX_APPKEY:0:6}***"

PASS=0; FAIL=0
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); if [ -n "$EVID_DIR" ]; then printf '%s\n' "$*" >> "$EVID_DIR/fails.txt"; fi; }

# req <label> <curl args...>：执行请求，回填 CODE/BODY；网络失败得 000 不中断逐项
CODE="000"; BODY=""
req() {
  local label="$1"; shift
  local tmp; tmp=$(mktemp)
  CODE=$(curl -sS -m 8 -o "$tmp" -w '%{http_code}' "$@" 2>"$tmp.err" || true)
  BODY=$(head -c 400 "$tmp" 2>/dev/null | tr '\n' ' ' || true)
  rm -f "$tmp" "$tmp.err"
  if [ -n "$EVID_DIR" ]; then
    local a disp=""
    for a in "$@"; do case "$a" in apikey:*) a="apikey:***";; esac; disp="$disp '$a'"; done
    {
      echo "### $label"
      echo "- 时间: $(date '+%F %T %z')"
      echo "- 命令: curl$disp"
      echo "- 状态码: ${CODE}"
      echo "- 片段: ${BODY:0:200}"
      echo
    } >> "$EVID_DIR/evidence.md"
  fi
}

echo "== 网关层只读自检（1.10-6 实测序列） GW_BASE=$GW_BASE key=$KEY_MASKED =="

# a. 无 key → 401（key-auth 生效）
req "a.ping无key" "$GW_BASE/v1/ping"
if [ "${CODE}" = "401" ]; then ok "a /v1/ping 无key → 401（key-auth 生效）"
else bad "a /v1/ping 无key 期望 401 实得 ${CODE}（snippet: ${BODY:0:120}）"; fi

# b. 带 dev key → 200
req "b.ping带key" -H "apikey: $APISIX_APPKEY" "$GW_BASE/v1/ping"
if [ "${CODE}" = "200" ]; then ok "b /v1/ping 带key → 200（凭证有效、路由在位）"
else bad "b /v1/ping 带key 期望 200 实得 ${CODE}（snippet: ${BODY:0:120}）"; fi

# c. 正式通道带 key：非 401 且非网关 404（业务层语义不在本卡验证面，snippet 留痕）
req "c.tags/query带key" -X POST -H "apikey: $APISIX_APPKEY" \
  -H "Content-Type: application/json" -d '{"key":"gateway-selftest-probe"}' \
  "$GW_BASE/v1/tags/query"
if [ "${CODE}" != "000" ] && [ "${CODE}" != "401" ] && ! grep -q "404 Route Not Found" <<<"$BODY"; then
  ok "c /v1/tags/query 带key → ${CODE}（过网关认证达服务；数据命中以 ecs_seed 自检 c 为准）"
else
  bad "c /v1/tags/query 带key 实得 ${CODE}（snippet: ${BODY:0:120}）——401=凭证/路由挂错，404 Route Not Found=路由缺"
fi

# d. 演示通道：路由在位且达服务层（200＝开放口径；401 auth_required＝统一登录门禁口径，
#    信封来自服务层 Bearer 校验而非 APISIX key-auth——两者都证明网关路由在位）
req "e.demo无key" "$GW_BASE/demo"
if [ "${CODE}" = "200" ] || grep -q "auth_required" <<<"$BODY"; then
  ok "e /demo 无key → ${CODE}（路由在位；服务层统一登录门禁或开放口径，snippet: ${BODY:0:60}）"
else
  bad "e /demo 无key 期望 200 或服务层 auth_required 401，实得 ${CODE}（snippet: ${BODY:0:120}）"
fi

echo "== 网关层自检结果: PASS=$PASS FAIL=$FAIL =="
if [ -n "$EVID_DIR" ]; then
  printf '汇总 %s GW_BASE=%s PASS=%d FAIL=%d\n' "$(date '+%F %T %z')" "$GW_BASE" "$PASS" "$FAIL" > "$EVID_DIR/summary.txt"
fi
[ "$FAIL" -eq 0 ] || exit 1
exit 0
