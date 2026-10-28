#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_daily 落账任务（ADR-0023 实施卡·方案 B）：审计 JSONL 分片按日重放 → usage_daily。

权威口径（ADR-0023 决定第 1/2/3 条，不越权改口径）：
  - 账源唯一＝JSONL 不可变分片的按日重放投影 usage_daily；口径唯一＝METERING_CALIBER
    （app/metering.py：主体×端点×日、2xx 计成功、东八区自然日、网关即拒不计）。
  - T+1～T+3 三日滚动窗重算：消费者 lag、重启、晚到事件在窗内自动收敛（--window 缺省 3）。
  - 写入一律**按日整行覆盖 upsert**（不增量合并、不部分字段更新）＝事务内
    DELETE 该日全行＋INSERT 重放聚合；同源重放幂等一致（双跑同结果）。
  - 回填与 rollup 双跑同日以**按日写锁**串行化：pg_advisory_xact_lock(71023, yyyymmdd)。
  - source=rollup（滚动重算）／backfill（--backfill 回填与对账修正，usage_reconcile --fix 同通道）。
  - 冻结账期跳过（ADR-0024 口径）：frozen_periods() 查 usage.billing_period（0004 已建）
    status IN ('confirmed','written_off') 的账期月——账期确认及核销（confirmed 后继态，
    核销后底数同防回填覆盖）后此处自动接管，冻结账期内的 day 跳过写入并告警，
    修正走调整单；表未建（未 apply 0004 的实例）＝恒空集＋日志一条。
  - tenant_id＝九元组直取（ADR 第 6 条 A 案调用时点锚定；事件缺列前置兼容），
    未登记/缺列＝空串（总量面）；**不按当前凭证表翻判历史**——存量 as-of 解析
    工具归本 ADR 实施卡后续（version_history 时点重建），本脚本宁缺勿错。
  - 分区滚动预建（migrations/0003 头注同源说明）：DDL 预建 2023-01～2027-12；
    其后由本脚本 ensure_partition() 承担——写前检查目标日所在月分区，缺则
    CREATE TABLE IF NOT EXISTS ... PARTITION OF 自动补建（至少提前一个自然月）；
    建写前置保证「分区缺失的插入直接失败」不发生在日常链路。

部署形态：对齐 audit_consumer 的常驻件风格（本卡先交付脚本＋手动/定时触发，
常驻编排归后续卡）。定时触发示例（T+1 日终，宿主 crontab 口径）：
  30 1 * * * cd /path/to/OneServe && python3 scripts/usage_rollup.py >> log/usage_rollup.log 2>&1
分片来源＝audit_consumer 按日分片（AUDIT_PIPELINE=kafka|dual 管线，audit-YYYY-MM-DD.jsonl）；
local 单文件形态（audit.jsonl）非分片制，其入账接 OBS 归档卡一并落（如实标注限制）。

解析容错（同 metering_scan 坏行容错口径）：坏 JSON／非对象行／脏 ts／空 caller 跳过；
非数字状态只计 calls 不猜成败；缺 latency_ms 不计入均值素材；分片内行 ts 归日≠分片日的
异常行（正常链路恒一致——audit_consumer 以行 ts 定分片名）计数告警不入账，避免半日
覆盖毁账。坏行/异常计数随行打印，供三方对账（usage_reconcile）锚点核对。

用法：
  python3 scripts/usage_rollup.py                       # T+1~T+3 滚动窗（缺省）
  python3 scripts/usage_rollup.py --day 2026-10-20      # 指定单日重算（可多次）
  python3 scripts/usage_rollup.py --backfill --from 2023-01-01 --to 2026-10-19
  python3 scripts/usage_rollup.py --dry-run             # 只打印重放统计零写入

环境变量：
  AUDIT_SHARD_DIR        分片目录（缺省 /data/audit/shards，对齐 audit_consumer）
  USAGE_PSQL_BASE        psql 通道注入（缺省回落 META_MIGRATE_PSQL_BASE，再缺省
                         docker compose -f compose.metadata.yml -p oneserve-meta exec -T
                         metadata-db psql -U meta_migrate -d oneserve_meta …——禁碰 dev 容器组）

退出码：0 成功（含 --dry-run）；1 环境性失败（psql 不可达等）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re as _re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 东八区（METERING_CALIBER 口径：自然日按东八区切；与 app/my_access.py _TZ_CN 同值）
TZ_CN = _dt.timezone(_dt.timedelta(hours=8))

# 分片文件名口径（audit_consumer._shard_path 同源）
SHARD_FMT = "audit-{}.jsonl"

# pg advisory lock classid（usage 域专用常量；二参 lock=(USG_LOCK, yyyymmdd)）
USG_LOCK = 71023

# 滚动预建领先月数：写窗口内任意日前，至少提前一个自然月补齐分区
PARTITION_LEAD_MONTHS = 1

# tenant_id 值域（ADR-0023 第 3 条 DDL CHECK 同源——appkeys.py TENANT_ID_RE / 0001 / 0003 三方同源）
TENANT_ID_RE = _re.compile(r"^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$")

EXIT_OK, EXIT_ENV = 0, 1


# ----------------------------------------------------------------------
# psql 通道（对齐 meta_migrate.ComposePsqlRunner 协议；测试注入同款 env 钩子）
# ----------------------------------------------------------------------

class PsqlError(Exception):
    pass


class Psql:
    """psql 通道：USAGE_PSQL_BASE > META_MIGRATE_PSQL_BASE > compose -p oneserve-meta 缺省。"""

    def __init__(self, timeout_s: int = 180):
        custom = (os.environ.get("USAGE_PSQL_BASE", "").strip()
                  or os.environ.get("META_MIGRATE_PSQL_BASE", "").strip())
        if custom:
            self.base = custom.split()
        else:
            self.base = ["docker", "compose", "-f", "compose.metadata.yml",
                         "-p", "oneserve-meta", "exec", "-T", "metadata-db",
                         "psql", "-U", "meta_migrate", "-d", "oneserve_meta",
                         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]
        self.timeout_s = timeout_s

    def sql(self, script: str) -> str:
        p = subprocess.run(self.base, input=script.encode("utf-8"),
                           capture_output=True, timeout=self.timeout_s)
        if p.returncode != 0:
            raise PsqlError("psql rc=%d：%s" % (p.returncode,
                                                p.stderr.decode("utf-8", "replace")[-1500:]))
        return p.stdout.decode("utf-8", "replace")


def q(s) -> str:
    """SQL 字面量安全引号（standard_conforming_strings=on 下 \'\' 转义即充分）。"""
    return "'" + str(s).replace("'", "''") + "'"


# ----------------------------------------------------------------------
# 事件解析（容错口径与 metering_scan 同源——唯一口径，不建第二实现）
# ----------------------------------------------------------------------

def parse_ts(ts_str) -> _dt.datetime | None:
    """ISO8601 容错解析；失败返回 None（脏 ts）。"""
    if not ts_str:
        return None
    try:
        s = str(ts_str).replace("Z", "+00:00")
        dt = _dt.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def day_key_cn(dt: _dt.datetime) -> str:
    """东八区日期键 YYYY-MM-DD。"""
    return dt.astimezone(TZ_CN).strftime("%Y-%m-%d")


def load_day(shard: Path) -> tuple[dict, list]:
    """读单日分片 → (stats, events)。

    stats＝容错计数 {ok, bad_json, dirty_ts, empty_caller, cross_day}；
    events＝[(caller, endpoint, status, latency_ms, tenant_raw)]（有效行）。
    行 ts 归日≠分片日的异常行计入 cross_day 不入账（防半日覆盖毁账，见头注）。
    """
    stats = {"ok": 0, "bad_json": 0, "dirty_ts": 0, "empty_caller": 0, "cross_day": 0}
    events: list = []
    shard_day = shard.name[len("audit-"):-len(".jsonl")]
    with shard.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                stats["bad_json"] += 1
                continue
            if not isinstance(row, dict):
                stats["bad_json"] += 1
                continue
            ts = parse_ts(row.get("ts") or "")
            if ts is None:
                stats["dirty_ts"] += 1
                continue
            caller = str(row.get("caller") or "")
            if not caller:
                stats["empty_caller"] += 1
                continue
            if day_key_cn(ts) != shard_day:
                stats["cross_day"] += 1
                continue
            stats["ok"] += 1
            events.append((caller,
                           str(row.get("endpoint") or "—"),   # metering_scan 占位口径同源
                           row.get("status"),
                           row.get("latency_ms"),
                           row.get("tenant_id")))
    return stats, events


def aggregate(events: list) -> dict:
    """按 (tenant_id, caller, endpoint) 聚合 → {key: [calls,success,fail,lat_sum,lat_cnt]}。

    口径＝METERING_CALIBER：2xx 计成功；非数字状态只计总量不猜成败；缺 latency 不计均值。
    """
    agg: dict = {}
    for caller, endpoint, status, latency, tenant_raw in events:
        tenant_id = str(tenant_raw or "")
        if tenant_id and not TENANT_ID_RE.match(tenant_id):
            tenant_id = ""          # 脏 tenant 值归总量面（与 DDL CHECK 拒绝口径一致，不丢量）
        key = (tenant_id, caller, endpoint)
        row = agg.setdefault(key, [0, 0, 0, 0, 0])
        row[0] += 1
        ok = 1 if isinstance(status, int) and 200 <= status < 300 else 0
        bad = 1 if isinstance(status, int) and not (200 <= status < 300) else 0
        row[1] += ok
        row[2] += bad
        if isinstance(latency, int):
            row[3] += latency
            row[4] += 1
    return agg


def replay_stats(events: list) -> dict:
    """JSONL 侧对账口径（usage_reconcile 同源 import）：总量/计费面条数。

    计费面粗口径＝tenant_id 非空（sandbox 判定在对账侧 join meta.tenant 收敛，
    本函数只做事件侧可判部分——对账侧以本函数输出为准，同源零漂移）。
    """
    total = len(events)
    billed = sum(1 for c in events
                 if str(c[4] or "") and TENANT_ID_RE.match(str(c[4])))
    return {"total": total, "billed": billed}


# ----------------------------------------------------------------------
# 分区滚动预建 + 冻结账期留位
# ----------------------------------------------------------------------

def ensure_partition(psql: Psql, day: str) -> None:
    """目标日所在月分区缺失即补建（含领先 PARTITION_LEAD_MONTHS 个月）；幂等。"""
    d = _dt.date.fromisoformat(day)
    ym_list = [d.strftime("%Y-%m")]
    lead_m = d.month - 1 + PARTITION_LEAD_MONTHS
    lead = _dt.date(d.year + lead_m // 12, lead_m % 12 + 1, 1)
    ym_list.append(lead.strftime("%Y-%m"))
    for ym in sorted(set(ym_list)):
        part = "usage_daily_y%sm%s" % (ym[:4], ym[5:7])
        lo = ym + "-01"
        ny, nm = (int(ym[:4]) + 1, 1) if ym[5:7] == "12" else (int(ym[:4]), int(ym[5:7]) + 1)
        hi = "%04d-%02d-01" % (ny, nm)
        out = psql.sql("SELECT CASE WHEN to_regclass('usage.%s') IS NULL THEN 1 ELSE 0 END;" % part)
        if out.strip().splitlines()[-1].strip() == "1":
            psql.sql("CREATE TABLE IF NOT EXISTS usage.%s PARTITION OF usage.usage_daily "
                     "FOR VALUES FROM ('%s') TO ('%s');" % (part, lo, hi))
            print("  [partition] 已滚动预建 usage.%s（%s~%s）" % (part, lo, hi))


def frozen_periods(psql: Psql) -> set:
    """已冻结账期集合（ADR-0024 口径：账期「已确认」后冻结，修正走调整单）。

    查询点＝usage.billing_period（ADR-0024 实施卡 0004 已建表，rollup 通道 meta_migrate
    为其属主）：status IN ('confirmed','written_off') 的账期月集合（written_off 为
    confirmed 后继态，核销后底数同防回填覆盖）；表不存在（未 apply 0004 的旧实例）＝
    无冻结账期返回空集（首查时日志一条）。
    """
    out = psql.sql("SELECT to_regclass('usage.billing_period') IS NULL;")
    if out.strip().splitlines()[-1].strip() == "t":
        if not frozen_periods._logged:
            print("  [freeze] 冻结账期闸留位：usage.billing_period 未建（0004 未 apply），"
                  "本期无冻结账期")
            frozen_periods._logged = True
        return set()
    rows = psql.sql("SELECT coalesce(string_agg(DISTINCT period, ','), '') FROM ("
                    " SELECT to_char(period_start, 'YYYY-MM') AS period FROM usage.billing_period"
                    " WHERE status IN ('confirmed', 'written_off')) t;").strip()
    last = rows.splitlines()[-1] if rows else ""
    return {x for x in last.split(",") if x}


frozen_periods._logged = False


# ----------------------------------------------------------------------
# 按日整行覆盖 upsert（DELETE+INSERT 单事务；按日 advisory lock 串行化双跑）
# ----------------------------------------------------------------------

def write_day(psql: Psql, day: str, agg: dict, event_count: int, source: str) -> int:
    """整日覆盖写入，返回写入行数。同源重放幂等：同分片重放结果恒一致。"""
    selects = []
    for (tenant_id, caller, endpoint), (calls, success, fail, lat_sum, lat_cnt) in sorted(agg.items()):
        selects.append(
            "SELECT %s::date, %s, %s, %s, %d, %d, %d, %d, %d, %d, %s"
            % (q(day), q(tenant_id), q(caller), q(endpoint),
               calls, success, fail, lat_sum, lat_cnt, event_count, q(source)))
    script = ["BEGIN;",
              # 按日写锁：回填与 rollup 双跑同日串行化（ADR 第 2 条）
              "SELECT pg_advisory_xact_lock(%d, %s);" % (USG_LOCK, day.replace("-", "")),
              "DELETE FROM usage.usage_daily WHERE day = %s;" % q(day)]
    if selects:
        script.append(
            "INSERT INTO usage.usage_daily (day, tenant_id, caller, endpoint, calls, success,"
            " fail, lat_sum, lat_cnt, source_event_count, source) %s;" % "\nUNION ALL ".join(selects))
    script.append("COMMIT;")
    psql.sql("\n".join(script))
    return len(selects)


def replay_day(shard_dir: Path, day: str, psql: Psql, source: str, dry_run: bool = False) -> dict:
    """重放单日：读分片 → 聚合 →（冻结检查）→ 分区预建 → 整行覆盖。分片缺失＝跳过。

    返回 {"day", "status": ok|skip|frozen|dryrun, "rows", "events", "stats"}。
    """
    shard = Path(shard_dir) / SHARD_FMT.format(day)
    if not shard.is_file():
        print("  [skip] %s：分片不存在（%s）" % (day, shard))
        return {"day": day, "status": "skip", "rows": 0, "events": 0}
    stats, events = load_day(shard)
    if stats["cross_day"]:
        print("  [warn] %s：%d 行 ts 归日≠分片日（时钟偏移类异常），不入账防半日覆盖"
              % (day, stats["cross_day"]))
    agg = aggregate(events)
    if day[:7] in frozen_periods(psql):
        print("  [frozen] %s：已冻结账期，跳过写入（修正走调整单，ADR-0024 口径）" % day)
        return {"day": day, "status": "frozen", "rows": 0, "events": len(events)}
    if dry_run:
        print("  [dry-run] %s：有效事件 %d → %d 行；容错 %s"
              % (day, len(events), len(agg), _stats_str(stats)))
        return {"day": day, "status": "dryrun", "rows": len(agg), "events": len(events)}
    ensure_partition(psql, day)
    rows = write_day(psql, day, agg, len(events), source)
    print("  [ok] %s：有效事件 %d → 覆盖 %d 行（source=%s）；容错 %s"
          % (day, len(events), rows, source, _stats_str(stats)))
    return {"day": day, "status": "ok", "rows": rows, "events": len(events)}


def _stats_str(stats: dict) -> str:
    parts = []
    for k in ("bad_json", "dirty_ts", "empty_caller", "cross_day"):
        if stats.get(k):
            parts.append("%s=%d" % (k, stats[k]))
    return " ".join(parts) if parts else "无"


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def window_days(window: int, now: _dt.datetime | None = None) -> list:
    """T+1 起往回 window 天（东八区；ADR 第 2 条 T+1~T+3 滚动窗）。"""
    today = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(TZ_CN).date()
    return [(today - _dt.timedelta(days=i)).isoformat() for i in range(1, window + 1)]


def _shard_days(shard_dir: Path, lo: str, hi: str) -> list:
    """分片目录里落 [lo, hi] 的日期清单（回填模式的日集合来源）。"""
    days = []
    for p in sorted(Path(shard_dir).glob("audit-*.jsonl")):
        stem = p.name[len("audit-"):-len(".jsonl")]
        try:
            _dt.date.fromisoformat(stem)
        except ValueError:
            continue                          # 非分片命名：跳过不猜
        if lo <= stem <= hi:
            days.append(stem)
    return days


def run(args) -> int:
    shard_dir = Path(args.shard_dir)
    if args.day:
        days = sorted(set(args.day))
    elif args.backfill and args.from_day and args.to_day:
        days = _shard_days(shard_dir, args.from_day, args.to_day)
        print("== 回填重放：%s ~ %s → 命中分片 %d 日（source=backfill） =="
              % (args.from_day, args.to_day, len(days)))
    else:
        days = list(window_days(max(1, args.window)))
        print("== 滚动重算：T+1~T+%d（%s ~ %s；source=rollup） =="
              % (args.window, days[-1], days[0]))
    if not days:
        print("  无目标日（分片缺失或范围空）——零写入")
        return EXIT_OK
    psql = Psql()
    try:
        frozen = frozen_periods(psql)
    except PsqlError as exc:
        print("环境性失败：psql 通道不可用：%s" % exc, file=sys.stderr)
        return EXIT_ENV
    source = "backfill" if args.backfill else "rollup"
    ok = 0
    for day in days:
        try:
            r = replay_day(shard_dir, day, psql, source, dry_run=args.dry_run)
            ok += 1 if r["status"] in ("ok", "dryrun") else 0
        except PsqlError as exc:
            print("环境性失败：%s 写入失败：%s" % (day, exc), file=sys.stderr)
            return EXIT_ENV
    print("== 完成：%d 日重算（%s） ==" % (ok, args.dry_run and "dry-run" or "已写库"))
    return EXIT_OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="usage_daily 落账：JSONL 分片按日重放（ADR-0023）")
    ap.add_argument("--shard-dir", default=os.environ.get("AUDIT_SHARD_DIR", "/data/audit/shards"))
    ap.add_argument("--window", type=int, default=3,
                    help="滚动窗天数：T+1 起往回 N 天（缺省 3＝ADR 的 T+1~T+3）")
    ap.add_argument("--day", action="append", default=[],
                    help="指定重算日 YYYY-MM-DD（可多次；给出则忽略滚动窗）")
    ap.add_argument("--backfill", action="store_true",
                    help="回填模式（source=backfill；与 --from/--to 或 --day 配合）")
    ap.add_argument("--from", dest="from_day", default="", help="回填起始日（含）")
    ap.add_argument("--to", dest="to_day", default="", help="回填结束日（含）")
    ap.add_argument("--dry-run", action="store_true", help="只打印重放统计，零写入")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
