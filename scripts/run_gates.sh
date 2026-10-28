#!/usr/bin/env bash
# OneServe 治理门禁入口（task-009 建；task-011 接电：加跑 SM3 三 AC 套件）。
# 用法：bash scripts/run_gates.sh
# 退出码：全部套件绿 → 0；任一套件失败 → 1（退出码聚合，跑完所有套件再判）。
# git 状态检查只警告不阻断（S0 防误伤；阻断口径待门禁稳定后另议）。
set -euo pipefail
cd "$(dirname "$0")/.."   # 切到仓库根，脚本从任意目录调用均可

echo "== OneServe 治理门禁 =="

# 0) 解释器甄别（#31 修复，2026-10-15）：裸 python3 逐机漂移——本机默认解析到
#    homebrew Python 3.14（缺 yaml/pytest），门禁环境性全红，为历史 --no-verify
#    诱因之一。取第一个能 import yaml 的解释器；可用 ONESERVE_GATE_PYTHON 指定；
#    全部不可用 → fail-loud，不假装绿。
PY="${ONESERVE_GATE_PYTHON:-}"
if [ -z "$PY" ]; then
  for _cand in python3 /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /usr/bin/python3; do
    if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c 'import yaml' >/dev/null 2>&1; then
      PY="$_cand"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "== 门禁未过：找不到带 yaml 的 python3（可设 ONESERVE_GATE_PYTHON 指定解释器）=="
  exit 1
fi
echo "-- 解释器：$PY ($("$PY" --version 2>&1)) --"

# 1) 断言集：装有 pytest 则走 pytest，否则 python3 直跑（断言一致）。
#    task-011 接电：SM3 三 AC 套件一并入列；task-007 接电：资讯服务＋B 型代理两套件入列；
#    M15-1 接电：批量任务框架套件（tests/test_batch_api.py）入列；
#    M15-2 接电：批量 worker 流水线套件（tests/test_batch_worker.py）入列；
#    M15-3 接电：批量结果交付套件（tests/test_batch_delivery.py）入列；
#    M15-5 接电：任务级审计套件（tests/test_batch_audit.py）＋配额 429 套件
#    （tests/test_batch_quota.py）入列；
#    M15-6 接电：全量快照套件（tests/test_batch_snapshot.py）入列；
#    P0-5 接电：traceId 贯通套件（tests/test_trace_id.py）入列；

#    P0-4 接电：凭证发放基建套件（tests/test_appkeys.py）入列；
#    P0-7 接电：我的接入门户套件（tests/test_my_access.py）入列；
#    P0-9 接电：字段字典门户套件（tests/test_fields_portal.py）入列；
#    P0-10/11 接电：申请接入＋联系与支持门户套件（tests/test_apply_contact_portal.py）入列；


#    M2-1 接电：HBase 执行器骨架套件（tests/test_hbase_executor.py，连接池 mock）入列；
#    M2-3/M2-3.9/P0-6 接电：注册表/字典升级/admin gate 套件入列（套件清单已改动态枚举，此后不再逐卡登记）；

#    退出码聚合——任一套件失败，门禁最终非 0。

# 1.5) 契约产物前置再生（2026-10-08 复工清单第4项·假红根治：09-30 两起假红均因
#      数据/断言改动后 portal/openapi/ 产物未再生。先幂等再生产物再校验，断根）。
echo "-- 前置：契约产物再生（scripts/gen_openapi.py，幂等） --"
"$PY" scripts/gen_openapi.py || { echo "== 门禁未过：契约产物再生失败（先于任何套件）=="; exit 1; }

GATES_FAILED=0
for suite in $(ls tests/test_*.py | sort); do  # 动态枚举:新套件自动入列(2026-09-23 结构性修复,消除合并冲突源)

  if "$PY" -c 'import pytest' >/dev/null 2>&1; then
    "$PY" -m pytest "$suite" -q || GATES_FAILED=1
  else
    "$PY" "$suite" || GATES_FAILED=1
  fi
done


# 3) Design Gate（v1.1·静态设计门禁·10 项断言）
#    容错：node/npm 缺失时明确报「design_gate 未执行」而非静默绿
if command -v node >/dev/null 2>&1; then
  echo "-- Design Gate（scripts/design_gate.mjs） --"
  node scripts/design_gate.mjs || GATES_FAILED=1
else
  echo "⚠ design_gate 未执行：node 不可用（需安装 Node.js 以启用设计门禁）"
  GATES_FAILED=1
fi

# 4) 文档交叉引用存在性机检（doc_ref_check；2026-10-20 复盘改进第一条）。
#    基线模式：存量冻结于 scripts/doc_ref_baseline.txt（基线内不阻断，新增引用断裂才 fail）。
#    落位说明：本项按文档门禁顺位放在 Design Gate 之后；doc_fig_check 已按机检遗留裁决
#    （2026-10-21 裁定：挂）接入下节 4.2。
echo "-- 文档交叉引用机检（scripts/doc_ref_check.py，基线模式） --"
"$PY" scripts/doc_ref_check.py --baseline scripts/doc_ref_baseline.txt || GATES_FAILED=1

# 4.2) 图形数据源 ↔ 说明书机检（doc_fig_check；2026-10-21 机检遗留裁决：挂）。
#      断言 figures/*.yml 与说明书 §3.1/§11.1 表格逐字一致；真漂移改图不改文（文为定稿）。
echo "-- 图形数据源机检（scripts/doc_fig_check.mjs） --"
node scripts/doc_fig_check.mjs || GATES_FAILED=1

# 4.5) 报表专区术语净化机检（1b-9 · 设计稿 §6/§7-4）：报表四页可见文案剥离
#      script/aside/注释/details/code 后对禁用工程术语正则零命中（doc_ref_check 之后落位）。
echo "-- 报表专区术语净化机检（scripts/check_report_copy.py，§6 净化表） --"
"$PY" scripts/check_report_copy.py || GATES_FAILED=1

# 5) 元数据对账门禁（卡-1b7 步7；doc_ref_check 之后按任务单落位）。
#    基线模式同 doc_ref 思路——迁移未启用期的守卫：META_WRITER=yaml（缺省）跑
#    「源 vs 快照」两方（db 未启用跳表对账不红），快照缺失即冻结基线；
#    META_WRITER=db 后三方全对（源/表/快照，任一差异 exit 2）。合法源演进后用
#    --refresh-baseline 重冻。
echo "-- 元数据对账门禁（scripts/meta_reconcile.py，基线模式） --"
"$PY" scripts/meta_reconcile.py --baseline || GATES_FAILED=1

# 5.5) 用量三方对账门禁（ADR-0023 实施卡·决定第 4 条；挂点随本卡落位）。
#      --check 门禁形态：库未启用（psql 不可达/usage_daily 未建）＝绿行跳过不红
#      （对齐 meta_reconcile「db 未启用跳表对账」既有约定）；库在则真对账，
#      计费面/锚点破 exit 2 拦门；总量面单列不拦门；快照侧偏差只告警。
echo "-- 用量三方对账门禁（scripts/usage_reconcile.py，--check 模式） --"
"$PY" scripts/usage_reconcile.py --check || GATES_FAILED=1

# 2) git 状态检查：有未提交改动只警告，不阻断
if [ -n "$(git status --porcelain)" ]; then
  echo "⚠ 警告：工作区存在未提交改动（仅提醒，不阻断）："
  git status --porcelain
else
  echo "✓ git 工作区干净"
fi

if [ "$GATES_FAILED" -ne 0 ]; then
  echo "== 门禁未过：有套件失败（exit 1）=="
  exit 1
fi
echo "== 门禁全绿（exit 0）=="