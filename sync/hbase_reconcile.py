# -*- coding: utf-8 -*-
"""HBase ↔ Redis 同源对账（TASK-M2-11.5）：差异报表（差异＝0 或逐条可解释）。

定位（同 hbase_loader）：开发期校验器，比对「同一份造数 CSV 灌进两条通道后的数据是否一致」——
  - Redis 侧：tags:<表>:<版本>:<键> 的键值（hbase_to_redis/loader 写入口径，JSON 紧凑序列化）；
  - HBase 侧：default:oneserve_demo 表 cf 列族的行值（sync/hbase_loader.py 写入口径）。
两条通道共用同一份 CSV 与同一套键，故正常情况差异必须为 0；非 0 即逐条列出可解释原因。

对账三查（沿 sync/reconcile.py 同款口径，阈值集中 sync_config）：
  查一 行数：Redis 键集 vs HBase 行集的双向差（仅 Redis 有＝漏灌 HBase；仅 HBase 有＝多灌/残留）；
  查二 抽样逐值：确定性抽样 RECON_SAMPLE_SIZE 条，逐字段比对 Redis 标签 JSON vs HBase 行值；
  查三 覆盖率：两通道填充密度相对偏差（同 RECON_COVERAGE_DEV_MAX 阈值）。

结果输出差异报表 JSON（stdout）并可落文件（--report）；exit 0＝差异 0 或全部可解释，1＝存在不可解释差异。
不可达口径：任一侧不可达即显式失败（exit 1，report 里写明 side），**不以假数据冒充**。

用法（宿主机；hbase 容器 9090 已映射宿主）：
  python3.13 sync/hbase_reconcile.py --version 20260920 --report tests/evidence/hbase-recon-<ts>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 宿主机现场＝<repo>/service；容器内镜像＝/app（Dockerfile WORKDIR）——两处可跑。
for _p in (str(REPO / "sync"), str(REPO / "service"), "/app"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C                                    # noqa: E402
from common import data_key, now_iso, redis_conn, sample_indices, scan_keys  # noqa: E402
from hbase_loader import (HBaseLoadError, connect, list_tables,        # noqa: E402
                          resolve_backend)


# ---------- 纯逻辑：差异比对（无 IO，单测全路径可覆盖） ----------

def hbase_row_to_tags(row: dict, cf: str) -> dict[str, str]:
    """HBase 行 → 标签集（b"cf:col"→col，非本列族列丢弃）：与在线查询同一反序列化口径。"""
    from app.hbase_service import row_to_fields
    return row_to_fields(row, cf)


def compare(redis_rows: dict[str, dict], hbase_rows: dict[str, dict], *,
            sample_size: int = C.RECON_SAMPLE_SIZE, seed: int = C.RECON_SAMPLE_SEED,
            coverage_dev_max: float = C.RECON_COVERAGE_DEV_MAX) -> dict:
    """两通道行集比对 → 差异报表 dict（纯函数：输入两个 {key: {col: value}} 映射）。

    返回 checks：row_count / orphan_hbase / value_sample / coverage，
    以及逐条 diff 明细（每条带 reason，便于「逐条可解释」判据）。
    """
    rk = set(redis_rows)
    hk = set(hbase_rows)
    only_redis = sorted(rk - hk)          # Redis 有、HBase 无 ＝ 漏灌
    only_hbase = sorted(hk - rk)          # HBase 有、Redis 无 ＝ 多灌/残留
    both = sorted(rk & hk)

    row_count = {
        "pass": not only_redis and not only_hbase,
        "rows_redis": len(rk), "rows_hbase": len(hk), "common": len(both),
        "rule": "双向差集为空（全等硬判，无容差）",
    }
    orphan = {
        "pass": not only_hbase,
        "only_hbase": len(only_hbase), "examples": [k[:16] + "…" for k in only_hbase[:3]],
        "rule": "HBase 存在而 Redis 无的行＝多灌/残留（reconcile 侧无 CSV 源，不自动删，逐条列出）",
    }

    idx = sample_indices(len(both), size=sample_size, seed=seed) if both else []
    mismatches = []
    for i in idx:
        key = both[i]
        r, h = redis_rows[key], hbase_rows[key]
        if r != h:
            mismatches.append({"key": key[:16] + "…", "reason": _first_diff(r, h)})
    value_sample = {
        "pass": not mismatches, "sampled": len(idx), "mismatch": len(mismatches),
        "rule": f"抽样 {min(sample_size, len(both))} 条逐字段比对（种子 {seed}），错 1 即红",
        "examples": mismatches[:3],
    }

    rf = sum(len(v) for v in redis_rows.values())
    hf = sum(len(v) for v in hbase_rows.values())
    denom_r = len(rk) or 1
    denom_h = len(hk) or 1
    cov_r = rf / denom_r
    cov_h = hf / denom_h
    dev = abs(cov_h - cov_r) / cov_r if cov_r else 0.0
    coverage = {
        "pass": dev <= coverage_dev_max,
        "cov_redis": round(cov_r, 6), "cov_hbase": round(cov_h, 6),
        "deviation": round(dev, 6), "threshold": coverage_dev_max,
        "rule": "两通道平均非空字段数的相对偏差 >20% 即红（抓系统性丢列/截断）",
    }

    diffs = ([{"kind": "missing_in_hbase", "key": k[:16] + "…"} for k in only_redis]
             + [{"kind": "extra_in_hbase", "key": k[:16] + "…"} for k in only_hbase]
             + [{"kind": "value_mismatch", "key": m["key"], "detail": m["reason"]} for m in mismatches])
    checks = {"row_count": row_count, "orphan_hbase": orphan,
              "value_sample": value_sample, "coverage": coverage}
    return {"checks": checks, "diff_count": len(diffs), "diffs": diffs,
            "pass": all(c["pass"] for c in checks.values())}


def _first_diff(expected: dict, actual: dict) -> dict:
    """首个差异描述（证据可读性；不影响判定）。"""
    for k in sorted(set(expected) | set(actual)):
        if expected.get(k) != actual.get(k):
            return {"field": k, "redis": expected.get(k), "hbase": actual.get(k)}
    return {"field": "<dict>", "redis": "…", "hbase": "…"}


def read_redis_rows(r, version: str, keys: list[str]) -> dict[str, dict]:
    """按数据键读 Redis 标签 JSON（紧凑序列化，loader 口径）。"""
    rows: dict[str, dict] = {}
    pipe = r.pipeline(transaction=False)
    for k in keys:
        pipe.get(data_key(version, k))
    for k, raw in zip(keys, pipe.execute()):
        if raw is not None:
            rows[k] = json.loads(raw)
    return rows


def read_hbase_rows(pool, table: str, cf: str) -> dict[str, dict]:
    """扫描 HBase 整表 → {rowkey: {col: value}}（str 口径）。"""
    out: dict[str, dict] = {}
    with pool.connection() as conn:
        for row_key, row_data in conn.table(table).scan():
            decoded = (row_key.decode("utf-8", "replace") if isinstance(row_key, bytes) else row_key)
            out[decoded] = hbase_row_to_tags(
                {k.decode("utf-8", "replace") if isinstance(k, bytes) else k:
                 v.decode("utf-8", "replace") if isinstance(v, bytes) else v
                 for k, v in row_data.items()}, cf)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HBase ↔ Redis 同源对账（差异报表；按表键空间）")
    ap.add_argument("--version", required=True, help="对账数据版本（Redis 空间 tags:<表>:<版本>）")
    ap.add_argument("--table", default=None, help="覆盖表名（缺省读 services.yaml backend.hbase；键空间按此表隔离）")
    ap.add_argument("--cf", default=None, help="覆盖列族（缺省读 services.yaml backend.hbase）")
    ap.add_argument("--report", default=None, help="差异报表输出 JSON 路径（可选，同时打印 stdout）")
    ap.add_argument("--service-id", default=None,
                    help="取表/列族的服务条目（须显式；原缺省 demo_hbase_query 已随 oneserve_demo "
                         "演示遗留清理删除 2026-10-08。或 --table+--cf 直传）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    host = args.host or os.environ.get("HBASE_HOST", "127.0.0.1")
    port = int(args.port or os.environ.get("HBASE_PORT", 9090))
    t0 = time.perf_counter()
    report = {"check": "hbase_vs_redis", "version": args.version,
              "host": f"{host}:{port}", "checked_at": now_iso()}

    def emit(ok: bool) -> int:
        report["pass"] = bool(ok)
        report["elapsed_s"] = round(time.perf_counter() - t0, 2)
        text = json.dumps(report, ensure_ascii=False)
        print(text)
        if args.report:
            p = Path(args.report)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8")
        return 0 if ok else 1

    try:
        if args.table and args.cf:
            table, cf = args.table, args.cf
        else:
            table, cf = resolve_backend(args.service_id)
        if args.table:
            C.use_table(table)   # 键空间按表归位：Redis 侧扫描/读取落 tags:<表>:<版本> 空间
        report["table"] = table
        report["cf"] = cf
        r = redis_conn(C.REDIS_URL)
        keys = [k.rsplit(":", 1)[1] for k in scan_keys(r, f"{C.NS}:{args.version}:*")]
        if not keys:
            report["error"] = f"Redis 空间 {C.NS}:{args.version}:* 无键（先跑 sync/loader.py 灌 Redis）"
            return emit(False)
        pool = connect(host, port)
        list_tables(pool, host, port)
        redis_rows = read_redis_rows(r, args.version, keys)
        hbase_rows = read_hbase_rows(pool, table, cf)
    except HBaseLoadError as exc:
        report["error"] = str(exc)
        report["fallback"] = "无（任一侧不可达即如实报错，不以假数据冒充）"
        return emit(False)
    except Exception as exc:  # noqa: BLE001 —— Redis 不可达等
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["side"] = "redis"
        return emit(False)

    report.update(compare(redis_rows, hbase_rows))
    return emit(report["pass"])


if __name__ == "__main__":
    sys.exit(main())
