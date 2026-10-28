#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_daily 三方日对账（ADR-0023 实施卡·决定第 4 条；门禁风格对齐 scripts/meta_reconcile.py）。

三方（日粒度，窗口缺省 T+1 起三日＝rollup 滚动窗，--days/--day 可调）：
  ① usage_daily   日合计（总量面 calls／计费面 calls／source_event_count）——psql 通道；
  ② JSONL 分片    当日有效事件条数（解析容错口径与 usage_rollup.load_day 同源 import——
                  零第二实现漂移）；计费面收敛需 meta.tenant 的 tenant_type 映射；
  ③ Redis 快照    audit:stats:{YYYYMMDD}:* 的 calls 合计（TTL 8 天；只做热展示）。

判定（ADR 第 4 条，不越权改口径）：
  - 锚点：usage_daily.source_event_count（该日重放条数）↔ JSONL 当日条数，破＝表错；
  - 计费面（tenant_id 非空且 tenant_type 不为 sandbox——沙箱实体化后 tenant_id 亦非空，
    单看非空会把沙箱量误拦进出账门）三方清零＝出账硬前置：表 vs JSONL 破＝拦门红；
  - 总量面（含未登记主体、沙箱、归属空缺）条数单列登记、**不拦门**（常态流量非异常）；
  - 锚定：不一致以 JSONL 重放为准修表（--fix → usage_rollup.replay_day source=backfill，
    修后复验）；**快照侧偏差只告警，不反向修账**。

门禁接入（run_gates 挂载形态，对齐 meta_reconcile 基线约定「db 未启用不红」）：
  --check 模式下环境缺失（psql 通道不可达／usage_daily 表未建）＝绿行跳过 exit 0；
  环境在则真对账，计费面/锚点破 exit 2。显式运行（无 --check）环境缺失＝exit 1。

用法：
  python3 scripts/usage_reconcile.py --check           # run_gates 挂载形态
  python3 scripts/usage_reconcile.py                   # 显式对账（环境缺＝exit 1）
  python3 scripts/usage_reconcile.py --fix             # 对红日以 JSONL 重放修表后复验
  python3 scripts/usage_reconcile.py --day 2026-10-20  # 指定单日（可多次）

环境变量：
  AUDIT_SHARD_DIR／REDIS_URL（对齐 audit_consumer）；USAGE_PSQL_BASE（对齐 usage_rollup：
  USAGE_PSQL_BASE > META_MIGRATE_PSQL_BASE > compose -p oneserve-meta 缺省，禁碰 dev 容器组）。

退出码：0 全对（或 --check 环境缺失跳过）；2 差异；1 环境性失败。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import usage_rollup as ur          # noqa: E402 —— 解析/重放同源（零第二实现漂移）

EXIT_OK, EXIT_ENV, EXIT_MISMATCH = 0, 1, 2

# Redis 统计键口径（audit_consumer.py STATS_KEY_PREFIX 同源）
STATS_KEY_PREFIX = "audit:stats:"


# ----------------------------------------------------------------------
# ① usage_daily 侧（psql 通道；计费面判定 SQL 唯一实现）
# ----------------------------------------------------------------------

def db_days(psql: "ur.Psql", days: list) -> dict:
    """按日返回 {day: {"total_calls","billed_calls","events","rows"}}。

    计费面＝tenant_id 非空 且 meta.tenant 实存该租户 且 tenant_type<>sandbox
    （LEFT JOIN 后 tenant_type IS NOT NULL 即实存——查无此租户归总量面，
    「不得按当前凭证表翻判历史」口径的保守侧：宁入总量面不误拦计费面）。
    """
    dl = ",".join("'%s'" % d for d in days)
    # 锚点＝source_event_count（日级锚点：整日各行同值，ADR 第 3 条）——min/max 双取：
    # 行间不一致（min≠max）＝表被污染（手插行/部分写），同样判锚点破，不做 sum 假合计。
    out = psql.sql(
        "SELECT to_char(u.day,'YYYY-MM-DD'), sum(u.calls),"
        " sum(u.calls) FILTER (WHERE u.tenant_id <> '' AND t.tenant_type IS NOT NULL"
        "                       AND t.tenant_type <> 'sandbox'),"
        " min(u.source_event_count), max(u.source_event_count), count(*)"
        " FROM usage.usage_daily u LEFT JOIN meta.tenant t ON t.tenant_id = u.tenant_id"
        " WHERE u.day IN (%s) GROUP BY u.day ORDER BY u.day;" % dl)
    res = {}
    for line in out.strip().splitlines():
        if not line:
            continue
        cells = line.split("|")
        if len(cells) < 6:
            continue
        res[cells[0]] = {
            "total_calls": int(cells[1] or 0),
            "billed_calls": int(cells[2] or 0),
            "events_min": int(cells[3] or 0),
            "events_max": int(cells[4] or 0),
            "rows": int(cells[5] or 0),
        }
    return res


def table_exists(psql: "ur.Psql") -> bool:
    out = psql.sql("SELECT to_regclass('usage.usage_daily') IS NOT NULL;")
    return out.strip().splitlines()[-1].strip() == "t"


# ----------------------------------------------------------------------
# ② JSONL 分片侧（解析同源 import usage_rollup；计费面 tenant_type 映射查库收敛）
# ----------------------------------------------------------------------

def tenant_type_map(psql: "ur.Psql") -> dict:
    out = psql.sql("SELECT tenant_id, tenant_type FROM meta.tenant;")
    m = {}
    for line in out.strip().splitlines():
        if "|" in line:
            tid, ttype = line.split("|", 1)
            m[tid] = ttype
    return m


def jsonl_days(shard_dir: Path, days: list, ttypes: dict) -> dict:
    """按日返回 {day: {"total","billed"}}（有效事件条数／计费面条数）。"""
    res = {}
    for day in days:
        shard = shard_dir / ur.SHARD_FMT.format(day)
        if not shard.is_file():
            res[day] = {"total": 0, "billed": 0, "missing": True}
            continue
        _, events = ur.load_day(shard)
        st = ur.replay_stats(events)
        # 计费面收敛：tenant_id 非空 且 meta.tenant 实存 且 非 sandbox（与 db 侧 SQL 同口径）
        billed = sum(1 for e in events
                     if str(e[4] or "") and ttypes.get(str(e[4]), "sandbox") != "sandbox"
                     and str(e[4]) in ttypes)
        res[day] = {"total": st["total"], "billed": billed, "missing": False}
    return res


# ----------------------------------------------------------------------
# ③ Redis 快照侧（只告警不修账；不可达＝None 并告警）
# ----------------------------------------------------------------------

def snapshot_days(days: list, redis_url: str) -> dict:
    """按日返回 {day: total_calls | None}；None＝快照不可用（缺键/TTL 出窗/连不上）。"""
    try:
        import redis as redis_lib
    except ImportError:
        print("  [snap-warn] redis 库缺席：快照侧不可对（只告警不拦门）")
        return {d: None for d in days}
    try:
        r = redis_lib.Redis.from_url(redis_url, socket_connect_timeout=3,
                                     socket_timeout=5, decode_responses=True)
        r.ping()
    except Exception as exc:  # noqa: BLE001 —— 快照侧任何失败＝告警（ADR：快照只做热展示）
        print("  [snap-warn] Redis 不可达（%s）：快照侧不可对（只告警不拦门）" % exc)
        return {d: None for d in days}
    out = {}
    for day in days:
        ymd = day.replace("-", "")
        total = 0
        found = False
        try:
            cursor = 0
            while True:
                cursor, keys = r.scan(cursor, match="%s%s:*" % (STATS_KEY_PREFIX, ymd), count=500)
                pipe = r.pipeline(transaction=False)
                for k in keys:
                    pipe.hget(k, "calls")
                for v in pipe.execute():
                    if v is not None:
                        total += int(v or 0)
                        found = True
                if cursor == 0:
                    break
        except Exception as exc:  # noqa: BLE001
            print("  [snap-warn] %s 快照读取失败（%s）：只告警不拦门" % (day, exc))
            out[day] = None
            continue
        out[day] = total if found else None   # 无键＝TTL 出窗/无数据：告警口径，不计 0 冒充
    return out


# ----------------------------------------------------------------------
# 对账主判（锚点＋计费面拦门／总量面单列／快照告警）
# ----------------------------------------------------------------------

def reconcile(shard_dir: Path, days: list, psql: "ur.Psql", redis_url: str,
              quiet_snap_warn: bool = False) -> tuple:
    """返回 (bad_days, lines)；bad_days＝锚点或计费面破的日清单（--fix 修表对象）。"""
    lines = []
    bad = []
    ttypes = tenant_type_map(psql)
    db = db_days(psql, days)
    jl = jsonl_days(shard_dir, days, ttypes)
    snap = snapshot_days(days, redis_url) if not quiet_snap_warn else {d: None for d in days}
    for day in days:
        d = db.get(day)
        j = jl.get(day, {"total": 0, "billed": 0, "missing": True})
        if j.get("missing"):
            lines.append("  [MISS]  %s：分片不存在——表 %s vs 分片缺；留存窗核对见 ADR-0023 前提声明"
                         % (day, "有账" if d else "无账"))
            if d and (d["events_min"] or d["events_max"] or d["total_calls"]):
                bad.append(day)
            continue
        if not d:
            if j["total"]:
                lines.append("  [DIFF]  %s：表无账 vs 分片 %d 条（计费 %d）→ 以 JSONL 重放修表"
                             % (day, j["total"], j["billed"]))
                bad.append(day)
            else:
                lines.append("  [OK ]   %s：两方皆空（无调用日）" % day)
            continue
        anchor_ok = (d["events_min"] == d["events_max"] == j["total"])
        billed_ok = d["billed_calls"] == j["billed"]
        total_ok = d["total_calls"] == j["total"]
        mark = "OK " if (anchor_ok and billed_ok) else "DIFF"
        lines.append(
            "  [%s] %s：表(总 %d/计费 %d/锚 %d~%d/%d 行) vs 分片(总 %d/计费 %d)"
            % (mark, day, d["total_calls"], d["billed_calls"], d["events_min"],
               d["events_max"], d["rows"], j["total"], j["billed"]))
        if not anchor_ok:
            lines.append("         锚点破：source_event_count %d~%d ≠ 分片条数 %d → 以 JSONL 重放修表"
                         % (d["events_min"], d["events_max"], j["total"]))
        if not billed_ok:
            lines.append("         计费面破：%d ≠ %d → 以 JSONL 重放修表（出账硬前置）"
                         % (d["billed_calls"], j["billed"]))
        if anchor_ok and billed_ok and not total_ok:
            # 锚点齐但总量差＝归属空缺口径漂移（理论不可达，登记型防线）
            lines.append("         总量面差：表 %d ≠ 分片 %d（登记，不拦门）" % (d["total_calls"], j["total"]))
        if anchor_ok and billed_ok:
            pass
        else:
            bad.append(day)
        s = snap.get(day)
        if s is None:
            if not quiet_snap_warn:
                lines.append("  [snap ] %s：快照侧无可用合计（TTL 出窗/无键）——告警不拦门" % day)
        elif s != j["total"]:
            lines.append("  [snap ] %s：快照 %d ≠ 分片 %d——快照侧偏差只告警，不反向修账"
                         % (day, s, j["total"]))
    return bad, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="usage_daily 三方日对账（ADR-0023 第 4 条）")
    ap.add_argument("--check", action="store_true",
                    help="门禁模式：环境缺失＝绿行跳过 exit 0（run_gates 挂载形态）")
    ap.add_argument("--fix", action="store_true",
                    help="对不一致日以 JSONL 重放修表（source=backfill）后复验")
    ap.add_argument("--days", type=int, default=3,
                    help="对账窗口天数（T+1 起往回 N 天，缺省 3＝rollup 滚动窗）")
    ap.add_argument("--day", action="append", default=[],
                    help="指定对账日 YYYY-MM-DD（可多次；给出则忽略 --days）")
    ap.add_argument("--shard-dir", default=os.environ.get("AUDIT_SHARD_DIR", "/data/audit/shards"))
    ap.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0"))
    args = ap.parse_args(argv)

    days = sorted(set(args.day)) if args.day else sorted(ur.window_days(max(1, args.days)))
    print("== 用量三方对账（%s）== 日窗 %s ~ %s；分片=%s"
          % ("check" if args.check else "strict", days[0], days[-1], args.shard_dir))
    psql = ur.Psql()
    try:
        if not table_exists(psql):
            if args.check:
                print("  跳过（绿行）：usage_daily 未建（0003 未 apply 或库未启用）——对齐 "
                      "meta_reconcile 基线约定「db 未启用不红」")
                return EXIT_OK
            print("  环境性失败：usage_daily 未建（先 apply migrations/0003_usage_init.sql）",
                  file=sys.stderr)
            return EXIT_ENV
    except Exception as exc:  # noqa: BLE001
        if args.check:
            print("  跳过（绿行）：psql 通道不可达（%s）——db 未启用不红" % exc)
            return EXIT_OK
        print("  环境性失败：psql 通道不可达：%s" % exc, file=sys.stderr)
        return EXIT_ENV

    bad, lines = reconcile(Path(args.shard_dir), days, psql, args.redis_url)
    for ln in lines:
        print(ln)

    if bad and args.fix:
        print("-- --fix：对 %d 红日以 JSONL 重放修表（source=backfill）--" % len(bad))
        for day in bad:
            try:
                ur.replay_day(Path(args.shard_dir), day, psql, "backfill")
            except Exception as exc:  # noqa: BLE001
                print("  环境性失败：%s 修表失败：%s" % (day, exc), file=sys.stderr)
                return EXIT_ENV
        bad2, lines2 = reconcile(Path(args.shard_dir), days, psql, args.redis_url,
                                 quiet_snap_warn=True)
        for ln in lines2:
            print(ln)
        bad = bad2

    if bad:
        print("== 对账未过：锚点/计费面破 %d 日（%s）——--fix 以 JSONL 重放修表 ==" 
              % (len(bad), ",".join(bad)))
        return EXIT_MISMATCH
    print("== 对账绿：计费面/锚点清零（总量面单列见上；快照侧只告警） ==")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
