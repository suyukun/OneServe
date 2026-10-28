# -*- coding: utf-8 -*-
"""对账查四：schema 漂移防护（TASK-M2-3.8 · SPEC §4.3 三查的延伸）。

三查管「数据对不对」（行数控/逐值/覆盖率），查四管「字典真源变了没有」——
比「各服务注册引用的字段集」（auth_matrix.yaml grants.fields ∩ services.yaml）
vs「字典真源当前字段集」（sync/schema_usr_label_wide_297.csv 的「字段名」列）。
TODO(卡2): 297 宽表字典换源为表级 schema 元数据（装载副产品）；CSV 缓删至卡2 交接（卡5 2026-09-30）。

漂移判定只看前 3 列结构位（口径见 service/app/schema_drift.py 头注）：比对只取
「字段名」这一结构列，新增业务列（cn_name/业务口径/敏感级这类）不触发。

结果写 Redis 状态键 sync:drift:<service_id>（卡4：版本后缀段已删；值编解码在 schema_drift：
quarantined:{missing:[...]} / ok）；kv_executor/hbase_service 每请求读该键执行
隔离闸。解除＝人工修正后重跑本脚本恢复 ok（幂等，无需重启服务）。

字段空间（TASK-M2-3.8 修复）：只对 services.yaml field_space: wide297（缺省）的服务
做宽表 297 列删列对账；资讯域（news）与 B 型代理（proxy）字段是语义占位符、不属于
宽表，结果记 not_applicable（如实：不适用，既非 ok 也非 quarantined，绝不把「不适用」
混进「健康」）。

自动化口径（如实，不得假称已生效）：本脚本是手工 CLI。AC-8-b 所称「自动转
quarantined」的自动触发器（compose 定时任务／cron／流水线后置钩子）属 M2 生产化，
本阶段未实现——删列后须人工执行本脚本才会写隔离状态键。

独立成脚本而非挂进 reconcile.py main 的取舍（回执注明）：reconcile 是「版本切换
前置闸」（按数据版本跑、红即拒切），查四是「服务×版本粒度的常态巡检」（与具体
数据版本无关、可随时单独重跑解除隔离）——两者生命周期不同，硬挂进三查 main 会
让「未起栈时想解除隔离」也要凑齐 CSV 指纹/行数条件，故只做函数级挂钩＋独立入口。

用法：
  python3.13 scripts/reconcile_drift.py                 # 全部已注册服务
  python3.13 scripts/reconcile_drift.py --only user_tag_query       # 指定服务（可重复）
  python3.13 scripts/reconcile_drift.py --recover                 # 解除：missing 回归真源即转 ok
  python3.13 scripts/reconcile_drift.py --recover --restored      # 解除：人工确认无条件清隔离
退出码：全 ok → 0；有隔离态 → 1（如实报告，非失败）；redis 不可达 → 2。
"""
import argparse
import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "sync"))
sys.path.insert(0, os.path.join(REPO, "service"))

import yaml  # noqa: E402

import sync_config as C  # noqa: E402
from app import kv_executor as kvx  # noqa: E402  （复用注册表/矩阵加载，零第二实现）
from app import schema_drift as sd  # noqa: E402  （纯逻辑单一事实来源）


def load_dict_columns(path=None):
    """字典真源当前字段集：只取「字段名」列（结构位口径，业务列变化不触发漂移）。

    不复用 common.load_schema()：其 297 列硬校验会把演练中间态（临时删一行）误炸成
    RuntimeError——查四的职责恰恰是容忍并检出任意列漂移。
    """
    with open(path or C.SCHEMA_CSV, encoding="utf-8", newline="") as f:
        return {row["字段名"] for row in csv.DictReader(f) if row.get("字段名")}


def check_schema_drift(r, refs, dict_cols, spaces=None):
    """查四主体：逐 service_id 比对引用集 vs 字典集，写状态键。

    r：redis 客户端（sync/common.redis_conn 同型）；refs：schema_drift.referenced_fields
    的输出；dict_cols：字典真源字段名集合；spaces：schema_drift.field_space_by_service
    的输出（{service_id: field_space}；缺省 None＝全部按宽表域，向后兼容）。
    返回 {service_id: 状态 dict}。
    增列（added）只记录不触发；删列/改名（missing 非空）→ quarantined；
    非宽表字段空间（news/proxy）→ not_applicable（不比对，如实标注不适用）。
    """
    report = {}
    for sid, fields in sorted(refs.items()):
        key = sd.drift_state_key(sid)
        space = (spaces or {}).get(sid, sd.FIELD_SPACE_DEFAULT)
        if space != sd.FIELD_SPACE_WIDE:
            state = sd.make_not_applicable(space, reason="非宽表字段空间，不参与宽表删列对账")
        else:
            drift = sd.compute_drift(fields, dict_cols)
            if drift["missing"]:
                state = sd.make_quarantined(drift["missing"])
            else:
                state = sd.make_ok()
            if drift["added"]:
                state["added_count"] = len(drift["added"])   # 仅观测值，不参与判定
        r.set(key, sd.encode_state(state))               # 覆盖式写＝幂等
        report[sid] = state
    return report


def recover_services(r, refs, dict_cols, *, restored=False):
    """解除流程：把漂移修复后的隔离服务转回 ok（写回状态键，幂等）。

    逐 service_id 读当前状态键 → schema_drift.recovery_decision 判定：
      - 非隔离/无键 → 原样不动（补 ok 键会掩盖真实巡检缺口）；
      - 隔离且 restored=True（人工确认字典已恢复）→ 覆盖写 ok；
      - 隔离且 restored=False → 只当 missing 字段全部重新出现在当前真源才写 ok，
        否则把状态键的 missing 收敛为「仍未回归」的清单并维持 quarantined。
    返回 {service_id: decision}；与 check_schema_drift 同为薄胶水（判定在纯函数）。
    """
    report = {}
    for sid in sorted(refs):
        key = sd.drift_state_key(sid)
        raw = r.get(key)
        decision = sd.recovery_decision(raw, restored, dict_cols)
        if decision["action"] == "write":
            r.set(key, sd.encode_state(decision["state"]))   # 覆盖式写＝幂等
        report[sid] = decision
    return report


def main():
    ap = argparse.ArgumentParser(description="对账查四：schema 漂移防护（引用集 vs 字典真源）")
    ap.add_argument("--csv", default=None, help="字典真源 CSV（缺省 sync/schema_usr_label_wide_297.csv）")
    ap.add_argument("--only", action="append", default=[], metavar="SID",
                    help="只查指定 service_id（可重复；缺省查全部已注册服务）")
    ap.add_argument("--recover", action="store_true",
                    help="解除流程：漂移修复后转回 ok（默认机器复核 missing 是否回归真源）")
    ap.add_argument("--restored", action="store_true",
                    help="配合 --recover：人工确认字典真源已恢复，无条件清隔离（覆盖写 ok）")
    args = ap.parse_args()
    if args.restored and not args.recover:
        print("错误：--restored 须与 --recover 同用（解除流程专用）", file=sys.stderr)
        return 2

    try:
        from common import redis_conn
        r = redis_conn()
    except Exception as exc:  # noqa: BLE001 —— redis 不可达：显式失败，不误写状态
        print(f"错误：Redis 不可达（{exc}）。查四拒绝在盲态下改写漂移状态键。", file=sys.stderr)
        return 2

    registry = kvx.load_services()                       # 卡4：service_id 单键注册表
    matrix = kvx.load_auth_matrix()
    entries = list(registry.values())
    refs = sd.referenced_fields(entries, matrix)
    spaces = sd.field_space_by_service(entries)   # 字段空间（wide297 才参与宽表对账）
    dict_cols = load_dict_columns(args.csv)

    only = set(args.only)
    if only:
        unknown = only - set(refs)
        if unknown:
            print(f"错误：--only {sorted(unknown)} 不在注册表中", file=sys.stderr)
            return 2
        refs = {k: v for k, v in refs.items() if k in only}

    if args.recover:
        report = recover_services(r, refs, dict_cols, restored=args.restored)
        out = {"check": "schema_drift_recover", "dict_source": str(args.csv or C.SCHEMA_CSV),
               "restored": bool(args.restored), "services": report,
               "still_quarantined": sorted(
                   k for k, d in report.items()
                   if (d.get("state") or {}).get("state") == sd.STATE_QUARANTINED)}
        print(json.dumps(out, ensure_ascii=False))
        return 1 if out["still_quarantined"] else 0

    report = check_schema_drift(r, refs, dict_cols, spaces)
    out = {"check": "schema_drift", "dict_source": str(args.csv or C.SCHEMA_CSV),
           "services": report,
           "quarantined": sorted(k for k, s in report.items()
                                 if s.get("state") == sd.STATE_QUARANTINED),
           "not_applicable": sorted(k for k, s in report.items()
                                    if s.get("state") == sd.STATE_NOT_APPLICABLE),
           "note": ("隔离当前为手工 CLI 触发（本脚本）；自动触发器归 M2 生产化。"
                    "not_applicable＝非宽表字段空间（资讯/代理域），不参与宽表删列对账。")}
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["quarantined"] else 0


if __name__ == "__main__":
    sys.exit(main())
