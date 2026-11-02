#!/usr/bin/env bash
# HBase 容器探针（TASK-M2-1）：起容器→9090 探活→建表→put/get 往返→结果留证。
# TASK-M2-3 扩展（＋e2e）：put 测试数据→经 hbase 型服务（demo_hbase_query）查询→投影断言
#   ——门槛稿 M2-3 验证口径「compose.hbase.yml 场景下端到端」：直连 thrift put 数据后，
#   起 service 容器经 /v1/hbase/query 真链路查询（鉴权/投影/脱敏/审计全链在服务内执行）。
# 用法：bash scripts/hbase_probe.sh            # 全链路（起容器→探活→put/get→e2e→down）
#       HBASE_PROBE_KEEP=1 bash scripts/hbase_probe.sh   # 验完不 down（调试用）
# 依据：门槛稿 NC-M2-8/镜像钉选小节——9090 实测探针＋HBASE_HEAPSIZE 内存限制；
# Docker VM 仅 7.748GiB，验完 down 让位内存（HBASE_PROBE_KEEP=1 显式保活除外）。
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER_BIN="${DOCKER_BIN:-docker}"
COMPOSE="$DOCKER_BIN compose -f docker-compose.yml -f compose.hbase.yml"
EVIDENCE_DIR="tests/evidence"
EVIDENCE="${HBASE_EVIDENCE:-$EVIDENCE_DIR/hbase-probe-$(date +%Y%m%d-%H%M%S).txt}"
PASS=0; FAIL=0

say()  { echo "[probe] $*"; }
ok()   { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

mkdir -p "$EVIDENCE_DIR"
exec > >(tee "$EVIDENCE") 2>&1
say "===== OneServe HBase 探针 (TASK-M2-1/M2-3) $(date '+%F %T') ====="

# 0) 前置：docker 可用
$DOCKER_BIN info >/dev/null 2>&1 || { echo "docker daemon 不可用"; exit 2; }
ok "docker daemon 可用"

# 1) 镜像钉选复核：镜像在场（无则拉），记录 digest 与架构
IMAGE="openeuler/hbase:latest"
if ! $DOCKER_BIN image inspect "$IMAGE" >/dev/null 2>&1; then
  say "镜像不在本地，拉取 $IMAGE ..."
  $DOCKER_BIN pull "$IMAGE"
fi
DIGEST=$($DOCKER_BIN image inspect --format '{{index .RepoDigests 0}}' "$IMAGE" 2>/dev/null || echo "无 RepoDigest")
ARCH=$($DOCKER_BIN image inspect --format '{{.Architecture}}' "$IMAGE")
ok "镜像钉选：$IMAGE arch=$ARCH digest=$DIGEST"

# 2) 起容器（独立 override，不动主 compose）＋内存限制参数
# ── 共享 HBase 破坏闸（9-28 事故后立规）────────────────────────────────────
# compose.hbase.yml 起的是共享容器名 oneserve-hbase 且挂共享卷（P1-1 持久化）。
# 本探针自身只动独立探针表 oneserve_probe_test，但 5) 会调 scripts/hbase_e2e.sh
# 重建 default:oneserve_demo——须与被调脚本同闸：默认拒绝，E2E_ALLOW_SHARED=1 放行。
if [ "${E2E_ALLOW_SHARED:-0}" != "1" ]; then
  say "✗ 默认拒绝：本探针用 compose 起共享 oneserve-hbase 且 e2e 段会重建 oneserve_demo 表，有覆盖共享数据风险（9-28 事故实证）。"
  say "  - 需要跑：E2E_ALLOW_SHARED=1 bash scripts/hbase_probe.sh（探针表/oneserve_demo 均为独立表名，不碰业务表）"
  exit 2
fi
say "[共享闸放行] E2E_ALLOW_SHARED=1 → 允许起共享容器/跑 e2e 段（自担风险）"
say "起 hbase 容器（HBASE_HEAPSIZE=1024, mem_limit=2GiB）..."
$COMPOSE up -d hbase
sleep 2
MEM_LIMIT=$($DOCKER_BIN inspect oneserve-hbase --format '{{.HostConfig.Memory}}')
[ "$MEM_LIMIT" = "2147483648" ] && ok "容器 mem_limit=2GiB 生效（$MEM_LIMIT bytes）" || bad "mem_limit 异常: $MEM_LIMIT"
HEAP=$($DOCKER_BIN exec oneserve-hbase printenv HBASE_HEAPSIZE 2>/dev/null || echo missing)
[ "$HEAP" = "1024" ] && ok "HBASE_HEAPSIZE=1024 生效" || bad "HBASE_HEAPSIZE 异常: $HEAP"

# 3) Thrift 9090 探活（容器内先通，再验宿主映射）
# 注：探活/往返任何一步失败都要走到收尾 down（内存让位纪律），故只在
# 「起容器成功与否」上用 exit 短路，其余失败记 bad() 攒到末尾统一判。
say "等 Thrift 9090 就绪（容器内 /dev/tcp 探测，最长 90s）..."
READY=0; i=0
for i in $(seq 1 45); do
  if $DOCKER_BIN exec oneserve-hbase bash -c 'echo > /dev/tcp/127.0.0.1/9090' 2>/dev/null; then
    READY=1; break
  fi
  sleep 2
done
[ "$READY" = "1" ] && ok "容器内 9090 就绪（第 ${i} 轮）" || { bad "容器内 9090 90s 未就绪"; $DOCKER_BIN logs --tail 40 oneserve-hbase || true; DOWN_GUARD=1; }

# 收尾口径（TASK-M2-3 修正）：精确删 hbase 容器，不用 compose down——
# down 会连主栈一起停（compose 文件组合含 docker-compose.yml 全部服务），
# 误伤并行会话正在运行的主栈；内存让位只针对 hbase（两 JVM ≈2GiB）。
# 失败也要收尾（内存让位）；DOWN_GUARD=1 时走快速收尾分支
if [ "${DOWN_GUARD:-0}" = "1" ]; then
  $DOCKER_BIN rm -f oneserve-hbase >/dev/null 2>&1 || true
  ok "hbase 容器已删（失败路径收尾）"
  say "===== 探针结果: PASS=$PASS FAIL=$FAIL ====="
  exit 1
fi

HOST_OK=0
for i in $(seq 1 15); do
  if nc -z 127.0.0.1 9090 2>/dev/null; then ok "宿主 127.0.0.1:9090 映射可达"; HOST_OK=1; break; fi
  sleep 2
done
[ "$HOST_OK" = "1" ] || bad "宿主 9090 不可达"

# 3c) 堆限制实测：JVM 进程参数须见 -Xmx1024m（HBASE_HEAPSIZE 落地口径）
# 注：主 JVM＋Thrift JVM 起全有延迟（M2-3 实测：9090 通时 ps 可能还看不到两进程），
# 加 5 轮重试窗口；仍未见才判红（thrift 守护单独 -Xmx256m，与主堆 1024 并存）。
XMX_OK=0
for xi in 1 2 3 4 5; do
  XMX=$($DOCKER_BIN exec oneserve-hbase bash -c "ps aux | grep -o '\-Xmx[0-9]*[mMgG]' | sort -u | tr '\n' ' '" 2>/dev/null || echo "未知")
  if echo "$XMX" | grep -q "Xmx1024m"; then XMX_OK=1; break; fi
  sleep 4
done
[ "$XMX_OK" = "1" ] && ok "JVM 堆实测含 -Xmx1024m（HBASE_HEAPSIZE 落地；实测: ${XMX}）" || bad "未见 -Xmx1024m，实测: ${XMX}"

# 4) python/happybase 往返：建测试表→put→get→比对（协议级真验证）
say "happybase put/get 往返验证 ..."
PY_RC=0
python3 - <<'PYEOF' || PY_RC=$?
import sys, time
try:
    import happybase
except ImportError:
    print("  ✗ happybase 未安装（pip install -r service/requirements.txt）"); sys.exit(4)

TABLE = "oneserve_probe_test"
for attempt in range(3):
    try:
        # timeout=30s：create_table 服务端异步建表，8s 短超时会「客户端超时、服务端建成」
        conn = happybase.Connection(host="127.0.0.1", port=9090, timeout=30000)
        tables = [t.decode() for t in conn.tables()]   # 统一解码成 str 再比较
        print(f"  · 现存表: {tables or '（无）'}")
        if TABLE in tables:
            conn.disable_table(TABLE); conn.delete_table(TABLE)
            print(f"  · 已清理残留探针表 {TABLE}")
        conn.create_table(TABLE, {"cf": {"max_versions": 1}})
        t = conn.table(TABLE)
        t.put(b"row-sm3aabbcc", {b"cf:tag_v1": b"v1-probe", b"cf:tag_v297": b"v297-probe"})
        got = t.row(b"row-sm3aabbcc")
        assert got.get(b"cf:tag_v1") == b"v1-probe", f"put/get 往返失配: {got!r}"
        assert got.get(b"cf:tag_v297") == b"v297-probe", f"第二列失配: {got!r}"
        print(f"  ✓ put/get 往返一致: {got!r}")
        print(f"  ✓ 表列表含 {TABLE}: {[x.decode() for x in conn.tables()]}")
        sys.exit(0)
    except Exception as e:
        print(f"  · 第 {attempt+1} 次尝试失败: {type(e).__name__}: {e}")
        time.sleep(3)
print("  ✗ happybase 往返 3 次全败"); sys.exit(5)
PYEOF
if [ "$PY_RC" -eq 0 ]; then ok "put/get 往返验证绿"; else bad "put/get 往返验证红"; fi

# 5) TASK-M2-3 端到端：服务容器经 /v1/hbase/query 查 HBase（探针脚本扩展判据）
# 链路：put 演示数据（rowkey=SM3）→ 起 service（镜像在则用，缺则现场 build）→
#       curl /v1/hbase/query（key=<SM3>，鉴权走授权矩阵 demo_merchant 条目）→
#       断言 200＋found＋白名单字段集＋脱敏档位（原值/越权字段零出现）。
say "M2-3 e2e：经 demo_hbase_query 服务查 HBase ..."
E2E_RC=0
bash scripts/hbase_e2e.sh || E2E_RC=$?
if [ "$E2E_RC" -eq 0 ]; then ok "服务链 e2e（put→查询→投影断言）绿"; else bad "服务链 e2e 红 (rc=$E2E_RC)"; fi

# 6) 收尾：精确删 hbase 容器让位内存（保活开关显式豁免；不动主栈——见上方口径注释）
if [ "${HBASE_PROBE_KEEP:-0}" = "1" ]; then
  say "HBASE_PROBE_KEEP=1 → 容器保留（调试用；记得手动 down）"
else
  $DOCKER_BIN rm -f oneserve-hbase >/dev/null 2>&1 || true
  ok "hbase 容器已删（内存让位，Docker VM 7.748GiB 口径；主栈不受影响）"
fi

say "===== 探针结果: PASS=$PASS FAIL=$FAIL ====="
if [ "$FAIL" -eq 0 ] && [ "$PASS" -gt 0 ]; then say "探针全绿，证据已存 $EVIDENCE"; exit 0; fi
exit 1
