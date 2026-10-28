#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计量简版报表（task-008 · SPEC §5⑤：按租户×服务的日/月调用计数，CSV）。

数据源：审计七元组 JSONL（task-002 service/app/audit.py 的落盘格式，
参数直接传文件路径——本机/WAL 兜底产物或未来从 Kafka/OBS 导出的同构文件）。

口径（简版，不做 BI）：
  - 维度＝租户×服务（×日 / ×月），计数＝审计条目数（不区分成败，全量计入）；
  - appkey→租户、endpoint→服务 的映射取自唯一权威授权矩阵
    service/auth_matrix.yaml（task-008 起管理面读写同一文件，
    003 的 demo_bank 与新登记条目同源）；
  - 矩阵里查不到的 caller/endpoint：以原值直接作为维度名继续计数并在
    stderr 警告——保证「汇总行 == 审计总条数」恒成立（AC-2 对账口径）；
  - 日期/月份取自审计 ts（UTC ISO8601）前 10 / 前 7 个字符。

输出（--out-dir，默认 reports/）：
  metering_daily.csv    tenant_id,service_id,date,call_count（+ 末行 ALL 汇总）
  metering_monthly.csv  tenant_id,service_id,month,call_count（+ 末行 ALL 汇总）

用法：
  python3 scripts/metering_report.py path/to/audit.jsonl \
      [--matrix service/auth_matrix.yaml] [--out-dir reports]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth_admin import load_matrix_file  # noqa: E402  复用授权矩阵的受限 YAML 解码

ALL = "ALL"


def _endpoint_path(endpoint: str) -> str:
    """审计 endpoint 归一化：七元组格式为「来源前缀:路径」（task-002 audit.py 造的
    "service:/v1/ping"），矩阵登记的是路径；取最后一个「:」之后的部分。"""
    return endpoint.rsplit(":", 1)[-1] if ":" in endpoint else endpoint


def _load_mappings(matrix_path: Path) -> tuple[dict, dict]:
    """从授权矩阵取 appkey→租户 与 endpoint→服务 两个映射（003 基底 schema）。"""
    data = load_matrix_file(matrix_path)
    appkey_tenant: dict[str, str] = {}
    endpoint_service: dict[str, str] = {}
    for row in data.get("matrix", []):
        tid = str(row.get("tenant", "?"))
        if row.get("appkey"):
            appkey_tenant[str(row["appkey"])] = tid
        for grant in row.get("grants", []):
            if grant.get("endpoint"):
                endpoint_service[str(grant["endpoint"])] = str(grant.get("service"))
    return appkey_tenant, endpoint_service


def _read_audit(jsonl_path: Path) -> tuple[list[dict], int]:
    """读审计 JSONL，返回 (记录列表, 坏行数)。坏行只警告不中断（半行容错同 audit.py）。"""
    records: list[dict] = []
    bad = 0
    for line_no, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict) or "ts" not in rec or "caller" not in rec:
                raise ValueError("缺少 ts/caller 字段")
        except (json.JSONDecodeError, ValueError) as exc:
            bad += 1
            print(f"警告：第 {line_no} 行跳过（{exc}）", file=sys.stderr)
            continue
        records.append(rec)
    return records, bad


def _aggregate(records: list[dict], matrix_path: Path) -> tuple[Counter, Counter, int]:
    """按 租户×服务×(日|月) 聚合计数。返回 (daily, monthly, 总条数)。"""
    appkey_tenant, endpoint_service = _load_mappings(matrix_path)
    daily: Counter = Counter()
    monthly: Counter = Counter()
    unknown_callers: set[str] = set()
    unknown_endpoints: set[str] = set()
    for rec in records:
        caller = str(rec.get("caller", ""))
        tenant = appkey_tenant.get(caller)
        if tenant is None:
            tenant = caller  # 未知调用方：以原值入表，保证计数守恒
            unknown_callers.add(caller)
        endpoint = str(rec.get("endpoint", ""))
        path = _endpoint_path(endpoint)
        service = endpoint_service.get(path)
        if service is None:
            service = endpoint  # 未知端点：同上
            unknown_endpoints.add(endpoint)
        ts = str(rec.get("ts", ""))
        day, month = ts[:10], ts[:7]
        daily[(tenant, service, day)] += 1
        monthly[(tenant, service, month)] += 1
    for c in sorted(unknown_callers):
        print(f"警告：caller {c!r} 不在授权矩阵中，按原值计入（请核对登记）", file=sys.stderr)
    for e in sorted(unknown_endpoints):
        print(f"警告：endpoint {e!r} 不在授权矩阵中，按原值计入（请核对登记）", file=sys.stderr)
    return daily, monthly, len(records)


def _write_csv(path: Path, dim: str, counter: Counter, total: int) -> None:
    """写单个报表 CSV：明细按维度排序，末行 ALL 汇总（合计==审计总条数）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["tenant_id", "service_id", dim, "call_count"])
        for (tenant, service, bucket), count in sorted(counter.items()):
            writer.writerow([tenant, service, bucket, count])
        writer.writerow([ALL, ALL, ALL, total])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="计量简版报表：审计 JSONL → 租户×服务 日/月计数 CSV")
    parser.add_argument("audit_jsonl", type=Path, help="审计 JSONL 文件路径（task-002 七元组格式）")
    parser.add_argument("--matrix", type=Path, default=REPO_ROOT / "service" / "auth_matrix.yaml",
                        help="授权矩阵路径（appkey/endpoint 维度映射来源）")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "reports", help="CSV 输出目录（默认 reports/）")
    args = parser.parse_args(argv)

    if not args.audit_jsonl.is_file():
        print(f"错误：审计文件不存在：{args.audit_jsonl}", file=sys.stderr)
        return 1

    records, bad = _read_audit(args.audit_jsonl)
    daily, monthly, total = _aggregate(records, args.matrix)

    daily_path = args.out_dir / "metering_daily.csv"
    monthly_path = args.out_dir / "metering_monthly.csv"
    _write_csv(daily_path, "date", daily, total)
    _write_csv(monthly_path, "month", monthly, total)

    print(f"审计条目：{total} 条（坏行 {bad} 跳过）｜日格 {len(daily)} ｜月格 {len(monthly)}")
    print(f"日报表：{daily_path}")
    print(f"月报表：{monthly_path}")
    print(f"对账：汇总行（ALL）={total}，与审计条数{'一致 ✓' if bad == 0 else '差坏行数（坏行未计入）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
