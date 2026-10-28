#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe 元数据对账门禁（卡-1b7 步7 · 验收线②「对账差异清零」的守卫执行器）。

对账三件套＝四源 yaml vs meta 表 vs 最新快照，条数＋关键字段哈希（对账列口径与
meta_migrate.verify 完全同源——同一规划器＋同一 canon 序列化，杜绝第二实现漂移）：
  META_WRITER=yaml（缺省·基线态）  「源 vs 快照」两方——迁移未启用期守卫：
                                    表对账跳过（db 未启用不红，卡步7 口径）；
  META_WRITER=db                    三方全对：源 vs 表、快照 vs 表、源 vs 快照
                                    （任一差异 exit 2，差异明细逐表逐键打印）。

用法：
  python3 scripts/meta_reconcile.py                # 严格模式（CI/本地显式对账）
  python3 scripts/meta_reconcile.py --baseline     # 基线模式（run_gates 挂载形态）
  python3 scripts/meta_reconcile.py --refresh-baseline  # 合法源演进后重冻快照基线

基线模式（同 doc_ref_check 基线思路——存量冻结、新增漂移才红）：
  yaml 态快照缺失 ＝ 以当前四源冻结基线快照（exit 0，守卫就位）；
  db   态快照缺失 ＝ 以库内现状导出基线（库＝写权威）；
  其后源 yaml 漂移即红（双轨期手编漂移守卫）；合法演进跑 --refresh-baseline 重冻。

env（与 meta_migrate / meta_writer 同源钩子，生产缺省零变化）：
  META_WRITER（yaml|db）／META_SNAPSHOT_DIR（快照目录，缺省 ./metadata-snapshots）／
  META_MIGRATE_SRC_DIR（四源目录，缺省仓内 service/）／
  META_MIGRATE_PSQL_BASE（db 态表对账 psql 通道注入，缺省 compose -p oneserve-meta）。

退出码：0 全对（或基线冻结成功）；2 差异/快照缺失（严格模式）；
       1 环境性失败（yaml 不可用等）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_snapshot as ms  # noqa: E402

EXIT_OK, EXIT_MISMATCH, EXIT_ENV = 0, 2, 1
TABLES = ["field_space", "tenant", "service_catalog", "upstream_proxy", "credential", "grant"]



def writer_mode() -> str:
    mode = (os.environ.get("META_WRITER") or "yaml").strip().lower()
    return mode if mode in ("yaml", "db") else "yaml"


def digests(rows_by_table: dict) -> dict:
    """{table: (条数, 排序哈希)}——哈希口径＝meta_migrate.verify（行 canon→md5→聚合）。"""
    mig = _mig()
    out = {}
    for table in TABLES:
        rows = rows_by_table.get(table) or []
        cols = mig.RECON_COLUMNS.get(table) or mig.GRANT_RECON_COLUMNS
        hs = [hashlib.md5("|".join(mig._canon(r.get(n), k) for n, k in cols)
                          .encode("utf-8")).hexdigest() for r in rows]
        agg = hashlib.md5("".join(sorted(hs)).encode("utf-8")).hexdigest() if hs else "empty"
        out[table] = (len(rows), agg)
    return out


def db_rows() -> dict:
    """表内行集（jsonb_agg 通道；与 meta_snapshot.export_all 同款只读查询）。"""
    mig = _mig()
    runner = mig.ComposePsqlRunner()   # META_MIGRATE_PSQL_BASE 钩子同源（缺省 -p oneserve-meta）

    class _R:            # 复用 ComposePsqlRunner 协议 → meta_snapshot 通道形状
        @staticmethod
        def rows(query: str):
            out = runner.sql("SET TimeZone TO 'UTC';\n"
                             f"SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM ({query}) t;").strip()
            import json
            return json.loads(out.splitlines()[-1]) if out and out != "[]" else []

    rows = {t: _R.rows(q) for t, q in (
        ("service_catalog", "SELECT * FROM meta.service_catalog"),
        ("upstream_proxy", "SELECT * FROM meta.upstream_proxy"),
        ("credential", "SELECT * FROM meta.credential WHERE deleted_at IS NULL"),
        ("grant", 'SELECT * FROM meta."grant"'),
        ("tenant", "SELECT * FROM meta.tenant"),
        ("field_space", "SELECT * FROM meta.field_space"))}
    return rows


_mig_mod = None


def _mig():
    global _mig_mod
    if _mig_mod is None:
        _mig_mod = ms.load_migrate()
    return _mig_mod


def pk_sets(rows_by_table: dict, table: str) -> set:
    mig = _mig()
    pk = mig.RECON_PK.get(table, "id")
    return {str(r.get(pk)) for r in rows_by_table.get(table) or []}


def print_diff(tag_a: str, rows_a: dict, tag_b: str, rows_b: dict) -> bool:
    """逐表打印两方对账结果；任一差异 True。差异明细：条数/哈希/PK 差集（≤5）。"""
    mig = _mig()
    da, db_ = digests(rows_a), digests(rows_b)
    any_diff = False
    for table in TABLES:
        ca, ha = da[table]
        cb, hb = db_[table]
        ok = ca == cb and ha == hb
        any_diff = any_diff or not ok
        mark = "OK " if ok else "DIFF"
        print(f"  [{mark}] {table}: {tag_a} {ca} 行 vs {tag_b} {cb} 行；"
              f"hash {tag_a}={ha[:12]} {tag_b}={hb[:12]}")
        if ok:
            continue
        pka, pkb = pk_sets(rows_a, table), pk_sets(rows_b, table)
        only_a, only_b = sorted(pka - pkb)[:5], sorted(pkb - pka)[:5]
        if only_a:
            print(f"         仅在 {tag_a}：{only_a}")
        if only_b:
            print(f"         仅在 {tag_b}：{only_b}")
        if not only_a and not only_b:     # 同键不同值：给首例字段级差异样例
            pk = mig.RECON_PK.get(table, "id")
            cols = mig.RECON_COLUMNS.get(table) or mig.GRANT_RECON_COLUMNS
            rows_a_by = {str(r.get(pk)): r for r in rows_a.get(table) or []}
            shown = 0
            for r in rows_b.get(table) or []:
                ra = rows_a_by.get(str(r.get(pk)))
                if ra is None:
                    continue
                for n, k in cols:
                    if mig._canon(ra.get(n), k) != mig._canon(r.get(n), k):
                        print(f"         例 {r.get(pk)}.{n}: {tag_a}={mig._canon(ra.get(n), k)!r} "
                              f"≠ {tag_b}={mig._canon(r.get(n), k)!r}")
                        shown += 1
                        break
                if shown >= 3:
                    break
    return any_diff


def cmd(args) -> int:
    mode = writer_mode()
    mig = _mig()
    src_dir = Path(os.environ.get("META_MIGRATE_SRC_DIR") or mig.SERVICE_DIR)
    snap_dir = Path(args.snapshot_dir) if args.snapshot_dir else ms.snapshots_dir()
    print(f"== 元数据对账（模式={mode}；源={src_dir}；快照={snap_dir}） ==")
    source_rows = ms.planned_rows_from_dir(mig, src_dir)

    # 重冻（合法源演进后的显式动作）：yaml 态以四源冻结；db 态以库现状导出（库＝写权威）
    if args.refresh_baseline:
        ms.write_snapshots(db_rows(), snap_dir) if mode == "db"             else ms.write_snapshots(source_rows, snap_dir)
        print(f"  基线快照已重冻（{'库' if mode == 'db' else '四源'} → {snap_dir}）")
        return EXIT_OK

    snaps_exist = all((snap_dir / n).is_file() for n in
                      (ms.SERVICES_NAME, ms.UPSTREAMS_NAME, ms.APPKEYS_NAME, ms.MATRIX_NAME))
    if not snaps_exist:
        if args.baseline and mode == "yaml":
            ms.write_snapshots(source_rows, snap_dir)
            print(f"  基线快照已冻结（四源 → {snap_dir}）；其后源漂移即红，"
                  "合法演进用 --refresh-baseline 重冻")
            return EXIT_OK
        if args.baseline and mode == "db":
            ms.write_snapshots(db_rows(), snap_dir)
            print(f"  基线快照已导出（库 → {snap_dir}，db 态库＝写权威）")
            return EXIT_OK
        print(f"  快照缺失：{snap_dir}（严格模式拒绝；基线模式请加 --baseline）",
              file=sys.stderr)
        return EXIT_MISMATCH
    snap_rows = ms.planned_rows_from_dir(mig, snap_dir)

    if mode == "yaml":
        print("-- 两方对账（源 vs 快照；基线态跳表对账——db 未启用不红） --")
        return EXIT_MISMATCH if print_diff("源", source_rows, "快照", snap_rows) else EXIT_OK

    print("-- 三方对账（源 vs 表 vs 快照；META_WRITER=db） --")
    db = db_rows()
    rc = EXIT_OK
    print("  · 源 vs 表：")
    if print_diff("源", source_rows, "表", db):
        rc = EXIT_MISMATCH
    print("  · 快照 vs 表：")
    if print_diff("快照", snap_rows, "表", db):
        rc = EXIT_MISMATCH
    print("  · 源 vs 快照：")
    if print_diff("源", source_rows, "快照", snap_rows):
        rc = EXIT_MISMATCH
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OneServe 元数据对账门禁（卡-1b7 步7）")
    ap.add_argument("--baseline", action="store_true",
                    help="基线模式：快照缺失即冻结基线（run_gates 挂载形态）")
    ap.add_argument("--refresh-baseline", action="store_true",
                    help="重冻基线：以当前源（yaml 态）/库（db 态）重写快照后退出 0")
    ap.add_argument("--snapshot-dir", default="", help="快照目录（缺省 META_SNAPSHOT_DIR/仓内回落）")
    args = ap.parse_args(argv)
    try:
        return cmd(args)
    except Exception as exc:  # noqa: BLE001 —— 环境性失败给人话
        print(f"对账环境性失败：{exc}", file=sys.stderr)
        return EXIT_ENV


if __name__ == "__main__":
    sys.exit(main())
