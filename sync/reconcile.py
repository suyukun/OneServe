# -*- coding: utf-8 -*-
"""对账三查（task-006 · SPEC §4.3）：新空间 vs 模拟导出源，绿才许切。

前置查：源文件指纹——加载时记录的 sha256 必须等于本次对账 CSV 的 sha256（防拿错文件对账）。
查一 行数：Redis 新空间键数 === CSV 行数（全等硬判，无容差）。
查二 抽样逐值：确定性抽样 1000 条（种子固定，可复算），每条全字段与非空标签 JSON 逐值比对，错 1 条即红。
查三 覆盖率偏差：新空间实际填充密度 vs 源 CSV 填充密度，相对偏差 >20% 即红（抓系统性丢列/截断）。

结果落 sync:recon:<版本>（pass/checked_at/明细；--table 时按表键空间
sync:recon:<表>:<版本>），switch.py 据此放行；退出码 0＝绿 / 1＝红。

用法：
  python3.13 sync/reconcile.py --csv sync/out/src_20260921.csv --version 20260921
  python3.13 sync/reconcile.py --csv sync/out/src.csv --version 20260921 --table usr_basic_info
"""
import argparse
import csv
import json
import sys
import time

import sync_config as C
from common import (data_key, recon_meta_set, redis_conn, sample_indices,
                    scan_keys, sha256_file, ver_meta_get)


def main():
    ap = argparse.ArgumentParser(description="对账三查（新空间 vs 源文件）")
    ap.add_argument("--csv", required=True, help="与加载同源的导出 CSV")
    ap.add_argument("--version", required=True, help="待对账的新空间版本")
    ap.add_argument("--table", default=C.DEFAULT_TABLE,
                    help="表名（键空间域段 tags:<表>:*；缺省不切域＝存量单域口径）")
    args = ap.parse_args()

    if args.table:
        C.use_table(args.table)   # 键空间按表归位（三查判定零改动：闸体键名随域段参数化）
    t0 = time.perf_counter()
    r = redis_conn()
    report = {"version": args.version, "csv": args.csv, "checks": {}, "pass": False}

    def save_and_exit(ok):
        report["pass"] = ok
        report["elapsed_s"] = round(time.perf_counter() - t0, 2)
        detail = {k: v for k, v in report.items() if k not in ("csv",)}
        # 序列化口径唯一实现＝common.recon_meta_set（HTTP 端点 app/hbase_load.py 同款）。
        recon_meta_set(r, args.version, ok, detail)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if ok else 1

    # —— 前置查：版本元信息存在 + 源文件指纹一致 ——
    meta = ver_meta_get(r, args.version)
    if meta is None:
        report["checks"]["pre"] = {"pass": False, "error": f"版本元信息缺失：未加载或加载失败"}
        return save_and_exit(False)
    sha = sha256_file(args.csv)
    if sha != meta.get("source_sha256"):
        report["checks"]["pre"] = {"pass": False,
                                   "error": "源文件指纹不一致（对账 CSV ≠ 加载 CSV），拒绝对账"}
        return save_and_exit(False)
    report["checks"]["pre"] = {"pass": True, "source_sha256": sha[:16] + "…"}

    # 读源 CSV（全量行进内存：1 万行×297 列量级可承受；键列与非空标签一次读齐）
    src_keys, src_rows = [], []
    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        tag_cols = [c for c in reader.fieldnames if c != C.KEY_COLUMN]
        for row in reader:
            src_keys.append(row.pop(C.KEY_COLUMN))
            src_rows.append({k: v for k, v in row.items() if v != ""})
    n_csv = len(src_rows)

    # —— 查一：行数全等 ——
    redis_keys = scan_keys(r, f"{C.NS}:{args.version}:*")
    n_redis = len(redis_keys)
    report["checks"]["row_count"] = {
        "pass": n_redis == n_csv, "rows_csv": n_csv, "rows_redis": n_redis,
        "rule": "全等硬判（无容差）",
    }

    # —— 查二：抽样 1000 条逐值 ——
    idx = sample_indices(n_csv)
    mismatches = []
    pipe = r.pipeline(transaction=False)
    for i in idx:
        pipe.get(data_key(args.version, src_keys[i]))
    actuals = pipe.execute()
    for i, raw in zip(idx, actuals):
        expected = src_rows[i]
        actual = json.loads(raw) if raw is not None else None
        if actual != expected:
            why = "键不存在" if actual is None else _first_diff(expected, actual)
            mismatches.append({"row": i, "key": src_keys[i][:16] + "…", "diff": why})
    report["checks"]["sample"] = {
        "pass": not mismatches, "sampled": len(idx), "mismatch": len(mismatches),
        "rule": f"抽样 {C.RECON_SAMPLE_SIZE} 条逐值比对（种子 {C.RECON_SAMPLE_SEED}），错 1 即红",
        "examples": mismatches[:3],
    }

    # —— 查三：覆盖率相对偏差（全空间实测 vs 源） ——
    cov_csv = sum(len(t) for t in src_rows) / (n_csv * len(tag_cols))
    pipe = r.pipeline(transaction=False)
    for k in redis_keys:
        pipe.get(k)
    nonempty = sum(len(json.loads(v)) for v in pipe.execute() if v is not None)
    cov_redis = nonempty / (n_redis * len(tag_cols)) if n_redis else 0.0
    dev = abs(cov_redis - cov_csv) / cov_csv if cov_csv else 0.0
    report["checks"]["coverage"] = {
        "pass": dev <= C.RECON_COVERAGE_DEV_MAX,
        "cov_source": round(cov_csv, 6), "cov_redis": round(cov_redis, 6),
        "deviation": round(dev, 6), "threshold": C.RECON_COVERAGE_DEV_MAX,
        "rule": "相对偏差 >20% 拒切（抓系统性丢列/截断）",
    }

    ok = all(c["pass"] for c in report["checks"].values())
    return save_and_exit(ok)


def _first_diff(expected, actual):
    """给出首个差异描述（证据可读性；不影响判定）。"""
    for k in sorted(set(expected) | set(actual)):
        if expected.get(k) != actual.get(k):
            return {"field": k, "expected": expected.get(k), "actual": actual.get(k)}
    return {"field": "<dict>", "expected": "…", "actual": "…"}


if __name__ == "__main__":
    sys.exit(main())
