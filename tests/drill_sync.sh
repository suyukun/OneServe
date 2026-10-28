#!/usr/bin/env bash
# OneServe task-006 · 同步链路四场演练（SPEC §4.3 / AC-1~AC-3）
#
# ①正常全流程：加载→对账绿→原子切换→读方经 meta 指针拿新版且 data_as_of 更新；旧空间保留
# ②失败注入：a)行数不一致 b)抽样值错 c)覆盖率塌陷 → 三查各自见红 → 拒切 → 线上仍旧版
# ③回滚：切回旧版单条 SET，秒级生效；指向空版本被安全闸拒绝
# ④删除清单：清单文件→两版空间删键→读方返回 null→审计可查
#
# 用法：bash tests/drill_sync.sh [--keep]   # --keep＝收尾不 down（调试用）
# 依赖：docker（~/.docker/bin）、python3.13＋gmssl/redis-py、compose.sync.yml（Redis 16379）
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.docker/bin:$PATH"
export SYNC_REDIS_URL="redis://127.0.0.1:16379/0"
PY=python3.13
OUT=sync/out
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1
mkdir -p "$OUT"

PASS=0
banner() { printf '\n\033[1m===== %s =====\033[0m\n' "$*"; }
step()   { printf -- '--- %s\n' "$*"; }
ok()     { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()    { printf '  \033[31m✗ %s\033[0m\n' "$*" >&2; printf '\n[演练失败] %s\n' "$*" >&2; exit 1; }

# 断言：JSON 文件上的表达式为真（经环境变量传参，免引号转义问题）
assert_json() { # $1=json文件 $2=表达式(可用 d) $3=描述
  ASSERT_FILE="$1" ASSERT_EXPR="$2" ASSERT_MSG="$3" $PY -c '
import json, os
d = json.load(open(os.environ["ASSERT_FILE"]))
assert eval(os.environ["ASSERT_EXPR"]), os.environ["ASSERT_MSG"]' \
    || bad "$3（断言：$2）"
  ok "$3"
}

# 期待命令非零退出（拒切/拒回滚类负向断言）；stdout 落第 2 个参数文件供证据与后续断言
expect_fail() { # $1=描述 $2=输出文件 剩余=命令
  local desc="$1" outfile="$2"; shift 2
  set +e
  "$@" >"$outfile" 2>&1
  local rc=$?
  set -e
  [ "$rc" -ne 0 ] || bad "预期失败却成功：$desc"
  ok "$desc"
}

wait_redis() {
  for _ in $(seq 1 30); do
    if $PY -c 'import os, redis; redis.Redis.from_url(os.environ["SYNC_REDIS_URL"]).ping()' 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  bad "Redis 30 秒未就绪（16379）"
}

# 打印某版本空间键数（版本经环境变量传入，避免多层引号）
count_space() {
  SPACE_VER="$1" $PY -c '
import os, sys
sys.path.insert(0, "sync")
import sync_config as C
from common import count_keys, redis_conn
print(count_keys(redis_conn(), C.NS + ":" + os.environ["SPACE_VER"] + ":*"))'
}

# ============ 第 0 步：起演练 Redis ============
banner "第 0 步 · 起演练 Redis（compose.sync.yml，宿主 16379，noeviction＋AOF everysec）"
docker compose -f compose.sync.yml -p oneserve-sync up -d
wait_redis
ok "Redis 就绪（oneserve-sync-redis @ 16379）"

# 演练专用库（该容器 db0 只服务本演练）：清残留，保证复跑幂等
$PY -c 'import os, redis; redis.Redis.from_url(os.environ["SYNC_REDIS_URL"]).flushdb()'
ok "演练库已清（复跑幂等）"

# ============ 演练 ①：正常全流程 ============
banner "演练 ① · 正常全流程（AC-1：对账绿→切换→读方拿新版，data_as_of 更新）"

step "①-1 生成 v1 模拟导出（1 万用户，SM3 键，填充 15%＝中性档）"
$PY sync/gen_mock_source.py --date 20260920 --users 10000 --fill 0.15 --seed 42 \
    --out "$OUT/src_20260920.csv" | tee "$OUT/gen1.json"
assert_json "$OUT/gen1.json" 'd["users"]==10000 and d["tag_cols"]==296 and len(d["sha256"])==64' "v1 源生成：10000 用户 × 296 标签列（键=SM3 真算）"
KEY0=$(sed -n '2p' "$OUT/src_20260920.csv" | cut -d, -f1)  # sed 单行打印，避开 tail|head 的 SIGPIPE×pipefail 坑

step "①-2 加载 v1 → 双缓冲新空间 tags:user:20260920（pipeline 批量）"
$PY sync/loader.py --csv "$OUT/src_20260920.csv" --version 20260920 | tee "$OUT/load1.json"
assert_json "$OUT/load1.json" 'd["ok"] and d["loaded"]==10000' "v1 加载：10000 键入新空间"

step "①-3 对账 v1（三查：行数/抽样逐值/覆盖率）"
$PY sync/reconcile.py --csv "$OUT/src_20260920.csv" --version 20260920 | tee "$OUT/recon1.json"
assert_json "$OUT/recon1.json" 'd["pass"] and all(c["pass"] for c in d["checks"].values())' "v1 对账三查全绿"

step "①-4 首次上线：原子切 meta 指针 → 20260920"
$PY sync/switch.py --version 20260920 | tee "$OUT/switch1.json"
assert_json "$OUT/switch1.json" 'd["switched"] and d["to"]=="20260920"' "v1 切换上线（from=null）"

step "①-5 读方验证（经 meta 指针两跳）"
$PY sync/read_user.py "$KEY0" | tee "$OUT/read1.json"
assert_json "$OUT/read1.json" 'd["found"] and d["live_version"]=="20260920" and d["data_as_of"]=="2026-09-20" and d["n_tags"]>0' "读方：v1 可读，data_as_of=2026-09-20"

step "①-6 生成并加载 v2（同基线 +50 新用户，值随数据日期演化）"
$PY sync/gen_mock_source.py --date 20260921 --users 10050 --fill 0.15 --seed 42 \
    --out "$OUT/src_20260921.csv" | tee "$OUT/gen2.json"
$PY sync/loader.py --csv "$OUT/src_20260921.csv" --version 20260921 | tee "$OUT/load2.json"
assert_json "$OUT/load2.json" 'd["ok"] and d["loaded"]==10050' "v2 加载：10050 键入新空间"

step "①-7 对账 v2 全绿 → 切换 → 读方无感升级"
$PY sync/reconcile.py --csv "$OUT/src_20260921.csv" --version 20260921 | tee "$OUT/recon2.json"
assert_json "$OUT/recon2.json" 'd["pass"]' "v2 对账三查全绿"
$PY sync/switch.py --version 20260921 | tee "$OUT/switch2.json"
assert_json "$OUT/switch2.json" 'd["switched"] and d["from"]=="20260920" and d["to"]=="20260921" and d["switch_elapsed_ms"]<1000' "v2 原子切换（单条 SET，<1s）"
$PY sync/read_user.py "$KEY0" | tee "$OUT/read2.json"
assert_json "$OUT/read2.json" 'd["found"] and d["live_version"]=="20260921" and d["data_as_of"]=="2026-09-21" and d["n_tags"]>0' "读方：data_as_of 已更新为 2026-09-21（无感升级）"

step "①-8 旧空间保留（双缓冲口径，回滚底气）"
N_OLD=$(count_space 20260920)
[ "$N_OLD" = "10000" ] || bad "旧空间键数应 10000，实得 $N_OLD"
ok "旧空间 tags:user:20260920 保留 10000 键"

# ============ 演练 ②：失败注入 → 拒切 ============
banner "演练 ② · 失败注入（AC-2：对账红→拒切→线上仍旧版）"

step "②-1 生成并加载 v3（20260922）"
$PY sync/gen_mock_source.py --date 20260922 --users 10060 --fill 0.15 --seed 42 \
    --out "$OUT/src_20260922.csv" | tee "$OUT/gen3.json"
$PY sync/loader.py --csv "$OUT/src_20260922.csv" --version 20260922 | tee "$OUT/load3.json"
assert_json "$OUT/load3.json" 'd["loaded"]==10060' "v3 加载"

step "②-2 注入 a：偷删 37 键（模拟加载丢行）"
$PY tests/drill_tools.py drop-keys --version 20260922 --n 37 | tee "$OUT/inj_a.json"
assert_json "$OUT/inj_a.json" 'd["dropped"]==37 and d["remaining"]==10023' "注入 a：10060→10023 键"

step "②-3 对账 v3 → 查一行数红（其余查照跑，全面报告）"
expect_fail "对账 v3(a) 非零退出＝红" "$OUT/recon3a.json" \
    $PY sync/reconcile.py --csv "$OUT/src_20260922.csv" --version 20260922
assert_json "$OUT/recon3a.json" 'not d["pass"] and not d["checks"]["row_count"]["pass"] and d["checks"]["row_count"]["rows_csv"]==10060 and d["checks"]["row_count"]["rows_redis"]==10023' "查一捕获行数不一致（10060 vs 10023）"

step "②-4 拒切：switch.py 三闸不放行"
expect_fail "switch 拒切（对账未过）" "$OUT/switch3a.json" $PY sync/switch.py --version 20260922
cat "$OUT/switch3a.json"
assert_json "$OUT/switch3a.json" 'd["ok"]==False and "拒切" in d["reason"]' "拒切理由可读（对账未通过）"

step "②-5 读方：线上仍旧版 20260921"
$PY sync/read_user.py "$KEY0" | tee "$OUT/read3a.json"
assert_json "$OUT/read3a.json" 'd["live_version"]=="20260921" and d["data_as_of"]=="2026-09-21"' "线上仍 20260921 旧版（失败不切换）"

step "②-6 重灌恢复 → 注入 b：篡改 3 个抽样内用户的标签值"
$PY sync/loader.py --csv "$OUT/src_20260922.csv" --version 20260922 --replace > "$OUT/load3b.json"
$PY tests/drill_tools.py tamper-sampled --csv "$OUT/src_20260922.csv" --version 20260922 --n-keys 3 | tee "$OUT/inj_b.json"
assert_json "$OUT/inj_b.json" 'len(d["tampered"])==3' "注入 b：3 个抽样内用户被篡改"
expect_fail "对账 v3(b) 非零退出＝红" "$OUT/recon3b.json" \
    $PY sync/reconcile.py --csv "$OUT/src_20260922.csv" --version 20260922
assert_json "$OUT/recon3b.json" 'not d["pass"] and d["checks"]["row_count"]["pass"] and not d["checks"]["sample"]["pass"] and d["checks"]["sample"]["mismatch"]==3' "查二捕获抽样值错（行数仍绿，错 3/1000）"
expect_fail "switch 拒切（注入 b 后）" "$OUT/switch3b.json" $PY sync/switch.py --version 20260922
$PY sync/read_user.py "$KEY0" > "$OUT/read3b.json"
assert_json "$OUT/read3b.json" 'd["live_version"]=="20260921"' "线上仍旧版（注入 b 后）"

step "②-7 重灌恢复 → 注入 c：非抽样用户剥离 75 个 ac_ 列（覆盖率塌陷；65 列实测偏差 19.8% 差 0.2pp 不够）"
$PY sync/loader.py --csv "$OUT/src_20260922.csv" --version 20260922 --replace > "$OUT/load3c.json"
$PY tests/drill_tools.py strip-columns --csv "$OUT/src_20260922.csv" --version 20260922 --n-cols 75 | tee "$OUT/inj_c.json"
assert_json "$OUT/inj_c.json" 'd["stripped_fields"]==75 and d["stripped_keys"]>0' "注入 c：剥离 75 列（仅非抽样键）"
expect_fail "对账 v3(c) 非零退出＝红" "$OUT/recon3c.json" \
    $PY sync/reconcile.py --csv "$OUT/src_20260922.csv" --version 20260922
assert_json "$OUT/recon3c.json" 'd["checks"]["row_count"]["pass"] and d["checks"]["sample"]["pass"] and not d["checks"]["coverage"]["pass"] and d["checks"]["coverage"]["deviation"]>d["checks"]["coverage"]["threshold"]' "查三捕获覆盖率偏差超阈（行数/抽样仍绿＝独立见红）"
expect_fail "switch 拒切（注入 c 后）" "$OUT/switch3c.json" $PY sync/switch.py --version 20260922
$PY sync/read_user.py "$KEY0" > "$OUT/read3c.json"
assert_json "$OUT/read3c.json" 'd["live_version"]=="20260921"' "线上仍旧版（注入 c 后）"

step "②-8 清理演练版本 20260922（不留脏空间）"
$PY tests/drill_tools.py purge-version --version 20260922 | tee "$OUT/purge3.json"
assert_json "$OUT/purge3.json" 'd["purged_keys"]==10060' "v3 空间已清"

# ============ 演练 ③：回滚 ============
banner "演练 ③ · 回滚（AC-3：指回旧版秒级生效）"

step "③-1 自动回滚（从切换日志取上一版 20260920）"
$PY sync/rollback.py | tee "$OUT/rb1.json"
assert_json "$OUT/rb1.json" 'd["rolled_back"] and d["from"]=="20260921" and d["to"]=="20260920" and d["rollback_elapsed_ms"]<1000' "回滚完成：20260921→20260920，单条 SET <1s"

step "③-2 读方立即读到旧版"
$PY sync/read_user.py "$KEY0" | tee "$OUT/read_rb.json"
assert_json "$OUT/read_rb.json" 'd["found"] and d["live_version"]=="20260920" and d["data_as_of"]=="2026-09-20"' "读方：data_as_of 回到 2026-09-20（秒级生效）"

step "③-3 安全闸：回滚到不存在的版本被拒"
expect_fail "拒回滚到空版本" "$OUT/rb2.json" $PY sync/rollback.py --to 20990101
assert_json "$OUT/rb2.json" 'd["ok"]==False and "拒回滚" in d["reason"]' "拒回滚理由可读（目标空间为空）"

# ============ 演练 ④：删除清单 ============
banner "演练 ④ · 删除清单生效（删除权传导最小版）"

step "④-1 构造清单：2 个在库用户（两版空间都有）＋1 个不存在键"
KEYD1=$(sed -n '3p' "$OUT/src_20260920.csv" | cut -d, -f1)
KEYD2=$(sed -n '4p' "$OUT/src_20260920.csv" | cut -d, -f1)
MISSING=$($PY -c 'print("ab"*32)')
printf 'usr_key_sm3\n%s\n%s\n%s\n' "$KEYD1" "$KEYD2" "$MISSING" > "$OUT/delete_list.csv"
ok "清单就绪（3 条，其中 1 条必然 miss）"

step "④-2 执行删除（scope=all 默认：所有现存版本空间，防旧缓冲翻回复活）"
$PY sync/delete_apply.py --list "$OUT/delete_list.csv" | tee "$OUT/del.json"
assert_json "$OUT/del.json" 'd["ok"] and d["listed"]==3 and d["per_version_hit"]["20260920"]==2 and d["per_version_hit"]["20260921"]==2' "两版空间各命中删除 2 键（miss 不计）"

step "④-3 读方：已删用户返回 null"
$PY sync/read_user.py "$KEYD1" | tee "$OUT/read_del.json"
assert_json "$OUT/read_del.json" 'd["found"]==False and d["live_version"]=="20260920"' "已删用户 found=false（对外 null）"

step "④-4 审计可查（sync:delete:log 最新一条）"
$PY -c '
import json, sys
sys.path.insert(0, "sync")
import sync_config as C
from common import redis_conn
e = json.loads(redis_conn().lrange(C.DELETE_LOG, 0, 0)[0])
print(json.dumps(e, ensure_ascii=False))' | tee "$OUT/del_audit.json"
assert_json "$OUT/del_audit.json" 'd["listed"]==3 and set(d["per_version_hit"])=={"20260920","20260921"}' "删除审计已留痕（LRANGE 可查，含文件指纹）"

# ============ 收尾 ============
if [ "$KEEP" = "1" ]; then
  printf '\n[--keep] 跳过 down，Redis 留在 16379 供调试\n'
else
  banner "收尾 · docker compose down（留证据不留进程；卷 oneserve-sync-data 保留可复跑）"
  docker compose -f compose.sync.yml -p oneserve-sync down | tee "$OUT/down.txt"
fi

printf '\n\033[1m四场演练全部通过（%d 组断言全绿）\033[0m\n' "$PASS"
printf '  ① 正常全流程：对账绿→切换→data_as_of 更新→旧空间保留\n'
printf '  ② 失败注入：行数/抽样/覆盖率三注入各自见红→拒切→线上仍旧版\n'
printf '  ③ 回滚：单条 SET 指回旧版，<1s 生效；空版本被拒\n'
printf '  ④ 删除清单：两版空间删键→读方 null→审计留痕\n'
