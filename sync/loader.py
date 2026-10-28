# -*- coding: utf-8 -*-
"""加载器：模拟导出 CSV → Redis 双缓冲新空间 tags:user:<版本>（task-006 · SPEC §4.3）。

- 每用户一个数据键 tags:user:<版本>:<键>，value＝非空标签 JSON（紧凑序列化）；
- pipeline 批量灌入（批大小见 sync_config.PIPE_BATCH）；
- 写版本元信息 sync:ver:<版本>（行数/源文件指纹/覆盖率/data_as_of），供对账与读取方使用；
- 幂等：目标空间非空时拒绝，需 --replace 显式清空重灌（防误覆盖正在对账的空间）。

用法：
  python3.13 sync/loader.py --csv sync/out/src_20260920.csv --version 20260920
"""
import argparse
import csv
import json
import sys
import time

import sync_config as C
from common import data_key, now_iso, redis_conn, scan_keys, sha256_file, ver_meta_set


def main():
    ap = argparse.ArgumentParser(description="CSV → Redis 双缓冲新空间")
    ap.add_argument("--csv", required=True, help="模拟导出 CSV 路径")
    ap.add_argument("--version", required=True, help="目标版本（YYYYMMDD，非当前线上版）")
    ap.add_argument("--replace", action="store_true", help="目标空间非空时先清空重灌")
    args = ap.parse_args()

    t0 = time.perf_counter()
    r = redis_conn()

    # 双缓冲约束：只灌「新空间」，绝不直写当前线上版本空间
    from common import get_live
    live = get_live(r)
    if args.version == live:
        print(json.dumps({"ok": False, "error": f"版本 {args.version} 正是当前线上版，禁止原地重灌"},
                         ensure_ascii=False))
        return 1

    pattern = f"{C.NS}:{args.version}:*"
    exist = scan_keys(r, pattern)
    if exist:
        if not args.replace:
            print(json.dumps({"ok": False, "error": f"目标空间非空（{len(exist)} 键），重灌须 --replace"},
                             ensure_ascii=False))
            return 1
        pipe = r.pipeline(transaction=False)
        for k in exist:
            pipe.delete(k)
        pipe.execute()

    # data_as_of：数据日期（版本号即数据日期口径），可用参数覆盖
    data_as_of = f"{args.version[:4]}-{args.version[4:6]}-{args.version[6:8]}"

    rows = 0
    nonempty_cells = 0
    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        tag_cols = [c for c in reader.fieldnames if c != C.KEY_COLUMN]
        pipe = r.pipeline(transaction=False)
        for row in reader:
            sm3 = row.pop(C.KEY_COLUMN)
            tags = {k: v for k, v in row.items() if v != ""}
            nonempty_cells += len(tags)
            pipe.set(data_key(args.version, sm3),
                     json.dumps(tags, ensure_ascii=False, separators=(",", ":")))
            rows += 1
            if rows % C.PIPE_BATCH == 0:
                pipe.execute()
                pipe = r.pipeline(transaction=False)
        pipe.execute()

    if rows == 0:
        print(json.dumps({"ok": False, "error": "CSV 无数据行"}, ensure_ascii=False))
        return 1

    coverage = nonempty_cells / (rows * len(tag_cols))
    ver_meta_set(
        r, args.version,
        rows=rows,
        tag_cols=len(tag_cols),
        source=args.csv,
        source_sha256=sha256_file(args.csv),
        coverage_src=f"{coverage:.6f}",
        nonempty_per_user_avg=f"{nonempty_cells / rows:.2f}",
        data_as_of=data_as_of,
        loaded_at=now_iso(),
        key_algo="上游供数侧生成",
    )

    print(json.dumps({
        "ok": True, "version": args.version, "loaded": rows, "tag_cols": len(tag_cols),
        "coverage_src": round(coverage, 6), "nonempty_per_user_avg": round(nonempty_cells / rows, 2),
        "pipe_batch": C.PIPE_BATCH, "elapsed_s": round(time.perf_counter() - t0, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
