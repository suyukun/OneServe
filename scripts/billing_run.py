#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""billing_run 月账出账任务（ADR-0024 实施卡·空价目试运行）：三硬前置门禁 → 四键双快照出账。

权威口径（ADR-0024 决定第 1/3/4/6/8/9 条，不越权改口径；流程＝设计 v1.1 §5.3）：
  - 出账对象＝租户；计费量＝成功量（2xx，usage_daily.success）；沙箱/未登记/归属空缺
    归总量面单列登记不入账（计费面判定与 scripts/usage_reconcile.py db_days 同源同句，
    改须同批——见 BILL_FACE_PREDICATE）。
  - 三硬前置写进状态机（不靠人记），任一不满足拒出账并人话报因：
      ①T+3 账期关闭：账期末日 D 的 rollup 滚动窗收口日＝D+3（window_days 在 X 日覆盖
        X-1~X-3），今日须晚于 D+3 方可出账；
      ②计费面三方对账清零：**调 usage_reconcile.reconcile 同源判定**（零第二实现漂移），
        红日非空即拒；快照侧偏差只告警不拦门（quiet 口径，与对账侧分层一致）；
      ③rollup 覆盖 lag 归零：账期内「有有效事件的分片日」必须全部落账（覆盖差集空）。
        当前可观测实现＝差集核查；Kafka 消费组真 lag 查询随 Kafka 实配批接入
        （M0 骨架 Kafka=stub 阶段的如实代理，口径不变）。
  - 空价目试运行：价目表为空（三模型全 0 元）也走完整链路——账单行照出（用量＋成功量
    快照），priced=false、金额 0 元且不计应收（决定第 9 条「金额栏空」口径，以 priced
    标记与「真 0 元价」区分）；价目命中则按 price_tier 区间行计价并留成交单价快照
    （决定第 2/4 条，价目后改不改历史账）。
  - 四键防重：bill_line PK＝(period_start,tenant_id,caller,endpoint)；同账期重跑拒绝，
    数据修正须显式 --rebill（仅限 draft/reconciled；confirmed/written_off 永久拒绝——
    底数冻结，ADR 决定第 4 条「不静默重跑、修正走调整单」）。
  - 确认冻结＋OA 登记：--confirm（reconciled→confirmed，必带 --oa-ticket 纯登记不校真伪，
    零系统集成，ADR 决定第 6 条）；--register-oa＝确认后单号补录/更正（同为纯登记）。
  - 欠费只算不限：--mark-overdue 仅置 overdue 标记，不做任何限流/停服/降配动作
    （ADR 决定第 8 条；配额联动启用时点阶段二后另批）。
  - 出账落库单事务：INSERT/UPDATE 账期（reconciled＋三前置留痕）＋账单行＋自检对账
    （bill_line ↔ usage_daily 计费面直查双向 EXCEPT ALL 零差异，不平即 RAISE 回滚——
    设计 §5.3「对账＝账单用量 vs usage_daily 直查」）。

【遗留如实（归阶段二）】价目管理页、真实价目、配额联动归阶段二（蓝图 §3.2-3）；
usage 侧冻结闸已接管（frozen_periods 冻结集含 confirmed/written_off）——原「冻结闸
未接管」过期口径作废（复核 P2 登记，2026-10-21 清）。账单页＋CSV 导出（决定第 5 条）
随管理功能群一期 C 节落地（app/billing_api.py＋admin/billing.html）；状态机/守卫下沉
共享模块 app/billing_state.py——本脚本 sys.path 引用同一文件，确认/补录/欠费标记/
rebill 守卫与页面端点同源同句零漂移（P1-⑬＋P0-6）。

用法：
  python3 scripts/billing_run.py --period 2026-09                       # 出账（缺省动作）
  python3 scripts/billing_run.py --period 2026-09 --rebill              # 显式重出（draft/reconciled）
  python3 scripts/billing_run.py --period 2026-09 --confirm --oa-ticket OA-2026-09-001
  python3 scripts/billing_run.py --period 2026-09 --register-oa --oa-ticket OA-2026-09-001R
  python3 scripts/billing_run.py --period 2026-09 --mark-overdue        # 欠费逾期标记（只算不限）
  python3 scripts/billing_run.py --period 2026-09 --show                # 查看账期与账单行

环境变量（通道对齐 usage_rollup/usage_reconcile）：
  USAGE_PSQL_BASE > META_MIGRATE_PSQL_BASE > compose -p oneserve-meta 缺省（禁碰 dev 容器组）
  AUDIT_SHARD_DIR（分片目录）／REDIS_URL（对账快照侧，仅告警不拦门）

退出码：0 成功；1 环境性失败（psql 不可达/计费表未建）；2 拒绝（前置未过/四键防重/冻结/状态不符）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import re as _re
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "service"))

import usage_rollup as ur          # noqa: E402 —— psql 通道/分片口径同源（零第二实现）
import usage_reconcile as rc       # noqa: E402 —— 出账前置②同源判定（零第二实现漂移）
from app import billing_state as bs   # noqa: E402 —— 状态机/守卫共享模块（C 节 P1-⑬：
   # CLI 与 app/billing_api.py 同源同句；Dockerfile COPY app/ 进镜像，宿主/容器零漂移）
from app import pricing_api as pa    # noqa: E402 —— P0-4 单点归一（load_price 匹配侧剥 B 型
   # 审计后缀，与落库侧同源零第二实现；收口批 2026-10-21 翻闸配套，billing_state 同 path 先例）

EXIT_OK, EXIT_ENV, EXIT_REFUSE = 0, 1, 2

# 东八区（账期月切与日切同源，ur.TZ_CN）
TZ_CN = ur.TZ_CN

# 计费面判定（ADR-0024 决定第 9 条；与 usage_reconcile.db_days 同源同句——改须同批）
BILL_FACE_PREDICATE = ("u.tenant_id <> '' AND t.tenant_type IS NOT NULL"
                       " AND t.tenant_type <> 'sandbox'")

PERIOD_RE = _re.compile(r"^\d{4}-\d{2}$")

# T+3 收口：账期末日 D 的 rollup 末班覆盖日＝D+3，出账最早 D+4（今日 > D+3）
T3_CLOSE_DAYS = 3


# RETURNING 行集/账期解析已上移共享模块（bs.returned_rows/bs.parse_period）；
# 别名保留原调用点不改（行为零变化迁移）。
_returned_rows = bs.returned_rows


class Refuse(Exception):
    """拒绝出账/操作（exit 2）——人话报因。"""


# ----------------------------------------------------------------------
# 账期解析（自然月，东八区月切；period_start＝当月首日，DDL PK 口径）
# ----------------------------------------------------------------------

parse_period = bs.parse_period   # 'YYYY-MM' → (period_start, hi_exclusive, period_end)
PERIOD_RE = bs.PERIOD_RE


def period_days(lo: _dt.date, hi: _dt.date) -> list:
    """账期内全部自然日（东八区日切口径）。"""
    out, d = [], lo
    while d < hi:
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def today_cn() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).astimezone(TZ_CN).date()


# ----------------------------------------------------------------------
# psql 基础
# ----------------------------------------------------------------------

def tables_ready(psql: "ur.Psql") -> bool:
    out = psql.sql("SELECT to_regclass('usage.bill_line') IS NOT NULL"
                   " AND to_regclass('usage.billing_period') IS NOT NULL;")
    return out.strip().splitlines()[-1].strip() == "t"


def fetch_period(psql: "ur.Psql", lo: _dt.date) -> dict | None:
    out = psql.sql(
        "SELECT status, gate_t3_closed, gate_reconcile_clean, gate_lag_zero,"
        " amount_due, overdue, coalesce(oa_ticket,''),"
        " coalesce(reconciled_at::text,'-'), coalesce(confirmed_at::text,'-')"
        " FROM usage.billing_period WHERE period_start = %s;" % ur.q(lo.isoformat()))
    lines = [x for x in out.strip().splitlines() if x]
    if not lines:
        return None
    c = lines[-1].split("|")
    return {"status": c[0], "g_t3": c[1] == "t", "g_rec": c[2] == "t",
            "g_lag": c[3] == "t", "amount_due": c[4], "overdue": c[5] == "t",
            "oa_ticket": c[6], "reconciled_at": c[7], "confirmed_at": c[8]}


# ----------------------------------------------------------------------
# 三硬前置（ADR-0024 决定第 3 条；①②③逐条核，全列人话，任一不过拒出账）
# ----------------------------------------------------------------------

def gate_t3(end: _dt.date, today: _dt.date) -> tuple:
    """①T+3 账期关闭：今日须晚于 末日+T3_CLOSE_DAYS。"""
    close_day = end + _dt.timedelta(days=T3_CLOSE_DAYS)
    ok = today > close_day
    msg = ("账期末日 %s，T+3 收口 %s，今日 %s" % (end, close_day, today))
    return ok, msg


def gate_reconcile(shard_dir: Path, days: list, psql: "ur.Psql", redis_url: str) -> tuple:
    """②计费面三方对账清零：usage_reconcile.reconcile 同源判定（快照侧只告警已静默）。"""
    bad, lines = rc.reconcile(shard_dir, days, psql, redis_url, quiet_snap_warn=True)
    for ln in lines:
        print("    " + ln)
    return (not bad), ("红日 %d：%s" % (len(bad), ",".join(bad)) if bad else "红日 0（窗口 %d 日）" % len(days))


def gate_rollup_lag(shard_dir: Path, days: list, psql: "ur.Psql") -> tuple:
    """③rollup 覆盖 lag 归零：有有效事件的分片日必须全部落账（覆盖差集空）。

    注：分片存在但全坏行/空日不在此列（rollup 本就不产行，对账侧两方皆空同口径）；
    Kafka 消费组真 lag 查询随 Kafka 实配批接入（M0 Kafka=stub 阶段如实代理）。
    """
    uncovered = []
    with_events = 0
    for day in days:
        shard = Path(shard_dir) / ur.SHARD_FMT.format(day)
        if not shard.is_file():
            continue
        _, events = ur.load_day(shard)
        if not events:
            continue
        with_events += 1
        out = psql.sql("SELECT EXISTS (SELECT 1 FROM usage.usage_daily WHERE day = %s);"
                       % ur.q(day))
        if out.strip().splitlines()[-1].strip() != "t":
            uncovered.append(day)
    if uncovered:
        return False, ("有事件分片日未落账 %d 日：%s（先跑 scripts/usage_rollup.py --day …）"
                       % (len(uncovered), ",".join(uncovered)))
    return True, ("有事件分片日 %d 全部落账" % with_events)


def check_gates(shard_dir: Path, lo: _dt.date, hi: _dt.date, end: _dt.date,
                psql: "ur.Psql", redis_url: str) -> tuple:
    """逐条核查三硬前置；返回 (failures, flags)；每条打印人话。"""
    days = period_days(lo, hi)
    ok1, msg1 = gate_t3(end, today_cn())
    print("  [gate-1] T+3 账期关闭：%s（%s）" % ("PASS" if ok1 else "FAIL", msg1))
    ok2, msg2 = gate_reconcile(shard_dir, days, psql, redis_url)
    print("  [gate-2] 计费面三方对账清零：%s（%s）" % ("PASS" if ok2 else "FAIL", msg2))
    ok3, msg3 = gate_rollup_lag(shard_dir, days, psql)
    print("  [gate-3] rollup 覆盖 lag 归零：%s（%s）" % ("PASS" if ok3 else "FAIL", msg3))
    flags = (ok1, ok2, ok3)
    failures = [i + 1 for i, ok in enumerate(flags) if not ok]
    return failures, flags


# ----------------------------------------------------------------------
# 计费面聚合＋价目（成功量口径；三模型统一 price_tier 区间行算法）
# ----------------------------------------------------------------------

def aggregate_billed_face(psql: "ur.Psql", lo: _dt.date, hi: _dt.date) -> list:
    """计费面月聚合（四键粒度）：[(tenant_id, caller, endpoint, calls, success, fail,
    lat_sum, lat_cnt, source_event_count)]——bill_line 用量快照与自检对账的双向同源。"""
    out = psql.sql(
        "SELECT u.tenant_id, u.caller, u.endpoint, sum(u.calls), sum(u.success), sum(u.fail),"
        " sum(u.lat_sum), sum(u.lat_cnt), sum(u.source_event_count)"
        " FROM usage.usage_daily u LEFT JOIN meta.tenant t ON t.tenant_id = u.tenant_id"
        " WHERE u.day >= %s AND u.day < %s AND " % (ur.q(lo.isoformat()), ur.q(hi.isoformat()))
        + BILL_FACE_PREDICATE +
        " GROUP BY u.tenant_id, u.caller, u.endpoint ORDER BY u.tenant_id, u.caller, u.endpoint;")
    rows = []
    for line in out.strip().splitlines():
        if not line:
            continue
        c = line.split("|")
        rows.append({"tenant_id": c[0], "caller": c[1], "endpoint": c[2],
                     "calls": int(c[3]), "success": int(c[4]), "fail": int(c[5]),
                     "lat_sum": int(c[6]), "lat_cnt": int(c[7]), "events": int(c[8]),
                     "priced": False, "item_id": None, "unit_price": None,
                     "amount": Decimal("0.00")})
    return rows


def total_plane_report(psql: "ur.Psql", lo: _dt.date, hi: _dt.date) -> dict:
    """总量面单列（ADR 决定第 9 条：未归属/沙箱/登记缺失 不入账，报告行登记＋告警补录）。"""
    out = psql.sql(
        "SELECT coalesce(sum(u.calls) FILTER (WHERE u.tenant_id = ''), 0),"
        " coalesce(sum(u.calls) FILTER (WHERE u.tenant_id <> '' AND t.tenant_type = 'sandbox'), 0),"
        " coalesce(sum(u.calls) FILTER (WHERE u.tenant_id <> '' AND t.tenant_type IS NULL), 0)"
        " FROM usage.usage_daily u LEFT JOIN meta.tenant t ON t.tenant_id = u.tenant_id"
        " WHERE u.day >= %s AND u.day < %s;" % (ur.q(lo.isoformat()), ur.q(hi.isoformat())))
    c = out.strip().splitlines()[-1].split("|")
    return {"unassigned": int(c[0]), "sandbox": int(c[1]), "unknown": int(c[2])}


def _dec(s) -> Decimal:
    return Decimal(str(s))


def price_for(qty: int, tiers: list) -> Decimal:
    """qty（成功次数）按阶梯区间行计价：tiers=[(lower, upper|None, unit_price_str)] 升序。

    三模型统一算法（ADR 决定第 2 条）：按次＝单区间单价；套餐＝额度内 0 价档＋超额档；
    阶梯＝多档。区间有洞时洞内量不计价（防少收不多收的保守侧；区间连续性归登记应用层）。
    """
    amount = Decimal("0")
    remaining = qty
    for lower, upper, unit in tiers:
        if remaining <= 0:
            break
        cap = qty if upper is None else min(upper, qty)
        take = min(max(cap - lower, 0), remaining)
        if take > 0:
            amount += _dec(unit) * take
            remaining -= take
    return amount


def load_price(psql: "ur.Psql", endpoint: str, lo: _dt.date, end: _dt.date,
               cache: dict) -> list | None:
    """endpoint 归一后精确匹配生效价目（service_ref＝endpoint 归一原值，P0-4 单点口径）。

    B 型代理的审计后缀 ?retention=on|off（proxy_executor.annotate_endpoint）在匹配
    侧剥除——与落库侧同用 pricing_api.normalize_service_ref（收口批 2026-10-21
    落地＋B 型翻闸配套），同一代理 on/off 不再裂成两键；cache 按归一键共享。
    """
    ref = pa.normalize_service_ref(endpoint)
    if ref in cache:
        return cache[ref]
    out = psql.sql(
        "SELECT item_id FROM usage.price_item"
        " WHERE service_ref = %s AND effective_from <= %s"
        "   AND (effective_to IS NULL OR effective_to >= %s)"
        " ORDER BY effective_from DESC LIMIT 1;"
        % (ur.q(ref), ur.q(end.isoformat()), ur.q(lo.isoformat())))
    lines = [x for x in out.strip().splitlines() if x]
    if not lines:
        cache[ref] = None
        return None
    item_id = lines[-1].split("|")[0]
    t_out = psql.sql("SELECT lower_bound, coalesce(upper_bound::text,''), unit_price"
                     " FROM usage.price_tier WHERE item_id = %s ORDER BY lower_bound;"
                     % ur.q(item_id))
    tiers = []
    for line in t_out.strip().splitlines():
        if not line:
            continue
        lo_s, hi_s, p_s = line.split("|")
        tiers.append((int(lo_s), int(hi_s) if hi_s.strip() else None, p_s))
    cache[ref] = (item_id, tiers)
    return cache[ref]


def apply_pricing(psql: "ur.Psql", rows: list, lo: _dt.date, end: _dt.date) -> Decimal:
    """就地计价：命中价目 → priced=true＋成交单价快照＋金额；未命中 → 空价目 0 元不计应收。"""
    cache: dict = {}
    due = Decimal("0.00")
    for r in rows:
        hit = load_price(psql, r["endpoint"], lo, end, cache)
        if not hit or not hit[1]:
            continue
        item_id, tiers = hit
        amt = price_for(r["success"], tiers).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        r["priced"], r["item_id"], r["amount"] = True, item_id, amt
        r["unit_price"] = (amt / r["success"]).quantize(Decimal("0.000001"),
                                                        rounding=ROUND_HALF_UP) if r["success"] > 0 else None
        due += amt
    return due.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ----------------------------------------------------------------------
# 出账落库（单事务：账期＋账单行＋自检对账，不平即 RAISE 回滚）
# ----------------------------------------------------------------------

def _line_values(lo: _dt.date, r: dict) -> str:
    up = "NULL" if r["unit_price"] is None else str(r["unit_price"])
    return ("(%s, %s, %s, %s, %d, %d, %d, %d, %d, %d, %s, %s, %s, %s)"
            % (ur.q(lo.isoformat()), ur.q(r["tenant_id"]), ur.q(r["caller"]), ur.q(r["endpoint"]),
               r["calls"], r["success"], r["fail"], r["lat_sum"], r["lat_cnt"], r["events"],
               "true" if r["priced"] else "false",
               ur.q(r["item_id"]) if r["item_id"] else "NULL",
               up, str(r["amount"])))


def _selfcheck_sql(lo: _dt.date, hi: _dt.date, period: str) -> str:
    """自检对账（设计 §5.3：账单用量 vs usage_daily 直查）——双向 EXCEPT ALL 零差异。"""
    agg = ("SELECT u.tenant_id, u.caller, u.endpoint, sum(u.calls), sum(u.success), sum(u.fail),"
           " sum(u.lat_sum), sum(u.lat_cnt), sum(u.source_event_count)"
           " FROM usage.usage_daily u LEFT JOIN meta.tenant t ON t.tenant_id = u.tenant_id"
           " WHERE u.day >= '%s' AND u.day < '%s' AND " % (lo, hi) + BILL_FACE_PREDICATE +
           " GROUP BY u.tenant_id, u.caller, u.endpoint")
    line = ("SELECT tenant_id, caller, endpoint, calls, success, fail, lat_sum, lat_cnt,"
            " source_event_count FROM usage.bill_line WHERE period_start = '%s'" % lo)
    return ("DO $$ DECLARE bad bigint; BEGIN"
            " SELECT count(*) INTO bad FROM ((" + line + " EXCEPT ALL " + agg + ")"
            " UNION ALL (" + agg + " EXCEPT ALL " + line + ")) d;"
            " IF bad > 0 THEN RAISE EXCEPTION"
            " '账单自检不平：bill_line 与 usage_daily 计费面直查差异 %% 行（账期 %s）——回滚，核数据后重跑', bad;"
            " END IF; END $$;" % period)


def write_bill(psql: "ur.Psql", period: tuple, rows: list, due: Decimal,
               flags: tuple, rebill: bool) -> None:
    lo, hi, end = period
    script = ["BEGIN;"]
    if rebill:
        # P0-6：重出 UPDATE 带可重出状态守卫（confirmed/written_off 撞 0 行不改任何列），
        # 紧随事务内守卫块核行——0 行命中（含并发窗口内被抢先确认）即 RAISE 回滚
        # （REBILL_STATUS_GUARD 标记由 action_bill 翻译成拒绝 exit 2；判定不留在事务外）。
        script.append(
            "UPDATE usage.billing_period SET status = 'reconciled',"
            " gate_t3_closed = %s, gate_reconcile_clean = %s, gate_lag_zero = %s,"
            " gate_checked_at = now(), amount_due = %s, reconciled_at = now(), updated_at = now()"
            " WHERE period_start = %s AND status IN ('draft','reconciled');"
            % ("true" if flags[0] else "false", "true" if flags[1] else "false",
               "true" if flags[2] else "false", str(due), ur.q(lo.isoformat())))
        script.append(bs.rebill_status_guard(lo.isoformat()))
        script.append("DELETE FROM usage.bill_line WHERE period_start = %s;" % ur.q(lo.isoformat()))
    else:
        script.append(
            "INSERT INTO usage.billing_period (period_start, status, gate_t3_closed,"
            " gate_reconcile_clean, gate_lag_zero, gate_checked_at, amount_due, reconciled_at)"
            " VALUES (%s, 'reconciled', %s, %s, %s, now(), %s, now());"
            % (ur.q(lo.isoformat()), "true" if flags[0] else "false",
               "true" if flags[1] else "false", "true" if flags[2] else "false", str(due)))
    if rows:
        script.append("INSERT INTO usage.bill_line (period_start, tenant_id, caller, endpoint,"
                      " calls, success, fail, lat_sum, lat_cnt, source_event_count,"
                      " priced, price_item_id, unit_price, amount) VALUES\n"
                      + ",\n".join(_line_values(lo, r) for r in rows) + ";")
    script.append(_selfcheck_sql(lo, hi, "%04d-%02d" % (lo.year, lo.month)))
    script.append("COMMIT;")
    psql.sql("\n".join(script))


# ----------------------------------------------------------------------
# 动作
# ----------------------------------------------------------------------

def action_bill(args, psql: "ur.Psql", period: tuple) -> int:
    lo, hi, end = period
    ym = "%04d-%02d" % (lo.year, lo.month)
    row = fetch_period(psql, lo)
    if args.rebill:
        if row is None:
            print("[refuse] --rebill 无对象：账期 %s 不存在（先出账）" % ym)
            return EXIT_REFUSE
        if row["status"] in ("confirmed", "written_off"):
            print("[refuse] 确认冻结：账期 %s 已 %s——确认后账单不可改（修正走调整单，"
                  "ADR-0024 决定第 4 条）；--rebill 永久拒绝" % (ym, row["status"]))
            return EXIT_REFUSE
    elif row is not None:
        print("[refuse] 四键防重：账期 %s 已存在（status=%s）——同账期重跑拒绝；"
              "数据修正须显式 --rebill（仅限 draft/reconciled）" % (ym, row["status"]))
        return EXIT_REFUSE

    print("== 出账前置核查（三硬前置，ADR-0024 决定第 3 条：任一不过拒出账）==")
    failures, flags = check_gates(Path(args.shard_dir), lo, hi, end, psql, args.redis_url)
    if failures:
        print("== 拒绝出账：前置未过 %s（人话报因见上）==" % "、".join("①②③"[i - 1] for i in failures))
        return EXIT_REFUSE

    rows = aggregate_billed_face(psql, lo, hi)
    due = apply_pricing(psql, rows, lo, end)
    face = total_plane_report(psql, lo, hi)
    try:
        write_bill(psql, period, rows, due, flags, rebill=args.rebill)
    except ur.PsqlError as exc:
        if bs.REBILL_GUARD_MARKER in str(exc):
            # P0-6 事务内守卫拦截（pre-check 后并发窗口内被抢先确认/核销）：拒绝而非环境失败
            print("[refuse] 重出状态守卫拦截：账期 %s 已不在可重出状态（draft/reconciled）——"
                  "确认后账单不可改（修正走调整单，ADR-0024 决定第 4 条）；--rebill 拒绝" % ym)
            return EXIT_REFUSE
        raise

    priced_n = sum(1 for r in rows if r["priced"])
    succ = sum(r["success"] for r in rows)
    print("== 出账完成：账期 %s（%s~%s）==%s" % (ym, lo, end, args.rebill and "〔--rebill 重出〕" or ""))
    print("  bill_line %d 行／计费量（success）%d／应收合计 %s 元"
          % (len(rows), succ, due))
    print("  空价目试运行口径：未定价行 %d（金额栏 0 元且不计应收，ADR-0024 决定第 9 条）"
          % (len(rows) - priced_n) + ("；已定价行 %d" % priced_n if priced_n else ""))
    print("  总量面单列（不入账）：未归属主体 %d 次／沙箱租户 %d 次／登记缺失 %d 次"
          "——告警触发登记补录（决定第 9 条）" % (face["unassigned"], face["sandbox"], face["unknown"]))
    print("  状态：reconciled（三前置留痕①②③全 true；自检对账平：bill_line ↔ usage_daily 直查零差异）")
    return EXIT_OK


def action_confirm(args, psql: "ur.Psql", period: tuple, register_only: bool) -> int:
    lo = period[0]
    ym = "%04d-%02d" % (lo.year, lo.month)
    ticket = (args.oa_ticket or "").strip()
    if not ticket:
        print("[refuse] --%s 必带 --oa-ticket（确认结果与单号一并登记留痕，ADR-0024 决定第 6 条）"
              % ("register-oa" if register_only else "confirm"))
        return EXIT_REFUSE
    if register_only:
        rows = bs.register_oa(psql, lo.isoformat(), ticket)
        if not rows:
            row = fetch_period(psql, lo)
            print("[refuse] OA 登记无对象：账期 %s 不存在或未确认（status=%s）——"
                  "单号登记随确认动作或事后补录（仅 confirmed）"
                  % (ym, row["status"] if row else "不存在"))
            return EXIT_REFUSE
        print("[ok] OA 单号补录留痕：账期 %s → %s（纯登记不校真伪，零系统集成）" % (ym, ticket))
        return EXIT_OK
    rows = bs.confirm_period(psql, lo.isoformat(), ticket)
    if not rows:
        row = fetch_period(psql, lo)
        print("[refuse] 确认无对象或状态不符：账期 %s（status=%s）——状态机 reconciled→confirmed"
              " 不跳步" % (ym, row["status"] if row else "不存在"))
        return EXIT_REFUSE
    print("[ok] 账期 %s 已确认冻结：OA 单号 %s 已登记留痕（零系统集成不校真伪）；"
          "此后 --rebill/改账永久拒绝" % (ym, ticket))
    return EXIT_OK


def action_mark_overdue(args, psql: "ur.Psql", period: tuple) -> int:
    lo = period[0]
    ym = "%04d-%02d" % (lo.year, lo.month)
    rows = bs.mark_overdue(psql, lo.isoformat())
    if not rows:
        row = fetch_period(psql, lo)
        print("[refuse] 欠费标记无对象：账期 %s 不存在或未确认（status=%s）——"
              "欠费以确认后应收为前提" % (ym, row["status"] if row else "不存在"))
        return EXIT_REFUSE
    print("[ok] 账期 %s 欠费逾期标记 overdue=true——只算不限：不做限流/停服/降配"
          "（ADR-0024 决定第 8 条；配额联动阶段二后另批）" % ym)
    return EXIT_OK


def action_show(args, psql: "ur.Psql", period: tuple) -> int:
    lo, hi, end = period
    ym = "%04d-%02d" % (lo.year, lo.month)
    row = fetch_period(psql, lo)
    if row is None:
        print("[show] 账期 %s：无账期记录" % ym)
        return EXIT_OK
    print("[show] 账期 %s：status=%s 三前置①②③=%s/%s/%s 应收=%s 元 overdue=%s"
          " OA=%s reconciled_at=%s confirmed_at=%s"
          % (ym, row["status"], row["g_t3"], row["g_rec"], row["g_lag"], row["amount_due"],
             row["overdue"], row["oa_ticket"] or "-", row["reconciled_at"], row["confirmed_at"]))
    out = psql.sql(
        "SELECT count(*), coalesce(sum(success),0), coalesce(sum(amount),0),"
        " coalesce(sum(amount) FILTER (WHERE priced),0)"
        " FROM usage.bill_line WHERE period_start = %s;" % ur.q(lo.isoformat()))
    c = out.strip().splitlines()[-1].split("|")
    print("[show] 账单行 %s 行／计费量 %s／金额合计 %s（其中已定价计应收 %s）" % (c[0], c[1], c[2], c[3]))
    det = psql.sql(
        "SELECT tenant_id, caller, endpoint, calls, success, priced, amount"
        " FROM usage.bill_line WHERE period_start = %s ORDER BY tenant_id, caller, endpoint"
        " LIMIT 20;" % ur.q(lo.isoformat()))
    for line in [x for x in det.strip().splitlines() if x]:
        print("    " + line)
    return EXIT_OK


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="月账出账（ADR-0024 空价目试运行）：三硬前置门禁→四键双快照")
    ap.add_argument("--period", required=True, help="账期 YYYY-MM（自然月，东八区月切）")
    ap.add_argument("--rebill", action="store_true",
                    help="显式重出账期草稿（仅限 draft/reconciled；confirmed 永久拒绝）")
    ap.add_argument("--confirm", action="store_true",
                    help="确认冻结（reconciled→confirmed，必带 --oa-ticket）")
    ap.add_argument("--register-oa", dest="register_oa", action="store_true",
                    help="OA 单号补录/更正（仅 confirmed；纯登记不校真伪）")
    ap.add_argument("--mark-overdue", dest="mark_overdue", action="store_true",
                    help="欠费逾期标记（只算不限：不做限流/停服/降配）")
    ap.add_argument("--show", action="store_true", help="查看账期与账单行")
    ap.add_argument("--oa-ticket", dest="oa_ticket", default="", help="确认 OA 单号（纯登记留痕）")
    ap.add_argument("--shard-dir", default=os.environ.get("AUDIT_SHARD_DIR", "/data/audit/shards"))
    ap.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0"))
    args = ap.parse_args(argv)

    actions = [bool(args.rebill), args.confirm, args.register_oa, args.mark_overdue, args.show]
    if sum(actions) > 1:
        print("用法错误：--rebill/--confirm/--register-oa/--mark-overdue/--show 互斥（缺省＝出账）",
              file=sys.stderr)
        return EXIT_ENV
    try:
        period = parse_period(args.period)
    except ValueError as exc:
        print("用法错误：%s" % exc, file=sys.stderr)
        return EXIT_ENV

    psql = ur.Psql()
    try:
        if not tables_ready(psql):
            print("环境性失败：计费表未建（先 apply migrations/0004_billing_init.sql）", file=sys.stderr)
            return EXIT_ENV
        if args.show:
            return action_show(args, psql, period)
        if args.confirm:
            return action_confirm(args, psql, period, register_only=False)
        if args.register_oa:
            return action_confirm(args, psql, period, register_only=True)
        if args.mark_overdue:
            return action_mark_overdue(args, psql, period)
        return action_bill(args, psql, period)
    except ur.PsqlError as exc:
        print("环境性失败：psql 通道不可用：%s" % exc, file=sys.stderr)
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
