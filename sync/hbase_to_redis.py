# -*- coding: utf-8 -*-
"""HBase → Redis 加载器（M2 架构纠正口径：HBase 冷存储 → Redis 热存储）。

定位（docs/plans/M2范围裁定_v2-架构纠正_20260923.md 第一节）：
  HBase＝冷存储，Redis＝热存储；本器只做**一件事**——把 HBase（页面选定的表）的行
  数据加载进 Redis **既有键空间**，供 /v1/tags/query 等从 Redis 发 API；**不产出
  HBase 直连服务**（维护者 明示现阶段不做）。

复用优先（零第二实现）：
  - 读取：连接层复用 service/app/hbase_executor.make_pool（经 sync/hbase_loader.connect，
    不可达统一转可控错误）；整表 scan 复用 sync/hbase_reconcile.read_hbase_rows
    （其反序列化经 app.hbase_service.row_to_fields，与在线查询同一份口径）；
    按 rowkey 批量则复用 hbase_executor.HBaseClient.get（同一列投影口径）。
  - 写入：键＝sync/common.data_key（tags:<表>:<版本>:<key>，键空间按表隔离），value＝
    非空标签紧凑 JSON（ensure_ascii=False, separators=(",", ":")）——与 sync/loader.py
    逐字节同口径；版本元信息 sync:ver:<表>:<版本>（rows/tag_cols/source/sha256/coverage/data_as_of…）。
  - 双缓冲：只灌「本表新版本空间」，绝不触碰 tags:<表>:latest（指针切换另经 sync/switch.py，
    受对账三闸放行）；目标空间非空且未 --replace 即拒灌。
  - 失败不写脏：**先整表读入内存**（HBase 失败 → 零 Redis 写入），再写新空间；
    写中途失败尽力清掉本次已写键；新空间未切指针上线，读方仍读旧版。

不可达口径：HBase / Redis 任一不可达一律显式报错（exit 1），**不回退假数据**。

用法（宿主机；hbase 容器 9090 已映射宿主）：
  python3.13 sync/hbase_to_redis.py --version 20260923 \
      --source-csv sync/out/src_hbase_20260923.csv
  python3.13 sync/hbase_to_redis.py --version 20260923 --columns ac_active_sts_01,if_acct_fct_24
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 宿主机现场＝<repo>/sync＋<repo>/service；容器内镜像＝/app（Dockerfile WORKDIR）——两处可跑。
for _p in (str(REPO / "sync"), str(REPO / "service"), "/app"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C                                              # noqa: E402
from common import (data_key, get_live, now_iso, redis_conn,         # noqa: E402
                    scan_keys, sha256_file, ver_meta_set)
from hbase_loader import (HBaseLoadError, connect, list_tables,      # noqa: E402
                          resolve_backend)
import hbase_reconcile as hr                                         # noqa: E402
from app.hbase_executor import HBaseClient                           # noqa: E402


class LoadError(RuntimeError):
    """加载器可控错误：配置/源/目标不合法一律显式抛，不静默降级、不写半截脏数据。"""


# ---------- HBase 读取（复用既有实现，不复制第二套） ----------

def read_rows(pool, table: str, cf: str, *, columns: list[str] | None = None,
              rowkeys: list[str] | None = None) -> dict[str, dict]:
    """从 HBase 读行 → {rowkey: {列名: 值}}（str 口径，与 Redis value 同形）。

    - rowkeys 给出：按 rowkey 批量点查（复用 hbase_executor.HBaseClient.get，列级投影前置）；
    - 否则：整表 scan（复用 sync/hbase_reconcile.read_hbase_rows，见其 docstring）；
    - columns 给出时只保留清单内列（加载清单＝页面选定字段）。
    """
    if rowkeys:
        client = HBaseClient(pool, default_limit=len(rowkeys), max_limit=max(1, len(rowkeys)))
        out: dict[str, dict] = {}
        for rk in rowkeys:
            cols = [f"{cf}:{c}" for c in columns] if columns else None
            tags = hr.hbase_row_to_tags(client.get(table, rk, cols), cf)
            if tags:
                out[rk] = tags
        return out
    rows = hr.read_hbase_rows(pool, table, cf)
    if columns:
        allow = set(columns)
        rows = {k: {c: v for c, v in t.items() if c in allow} for k, t in rows.items()}
    # 值口径＝非空标签 JSON（与 sync/loader.py 同款）：过滤后无任何选中标签的行不落键。
    return {k: t for k, t in rows.items() if t}


def snapshot_sha256(rows: dict[str, dict]) -> str:
    """HBase 快照指纹（确定性：按 rowkey 排序后对行值紧凑 JSON 逐行哈希）。"""
    h = hashlib.sha256()
    for key in sorted(rows):
        h.update(key.encode("utf-8"))
        h.update(b"\x00")
        h.update(json.dumps(rows[key], ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


# ---------- Redis 写入（与 sync/loader.py 完全同口径） ----------

def write_space(r, version: str, rows: dict[str, dict], *, columns: list[str] | None = None,
                replace: bool = False, data_as_of: str | None = None, source: str | None = None,
                source_sha256: str | None = None, table: str = "", cf: str = "",
                progress_cb=None) -> dict:
    """把行集写进 Redis 新版本空间（双缓冲；失败不写脏）。返回加载报告 dict。

    约束与 sync/loader.py 一致：
      - version 恰为该表当前线上版 → 拒（禁止原地重灌正在对账/线上的空间，按表判定：
        别的表同版本号不受影响，换一个版本日期即可重灌）；
      - 目标空间非空且未 replace → 拒；
      - value＝非空标签紧凑 JSON；写完全部数据键后，最后写 sync:ver:<表>:<版本> 元信息
        （写中途失败即无完整元信息，switch 的对账闸不会放行）。
    键空间按表隔离：本函数先把 sync_config 键域切到 table（tags:<表>:…），后续
    get_live/scan_keys/ver_meta_set 全部落在本表键空间（2026-09-29 按表隔离改造）。
    """
    try:
        C.use_table(table)   # 键空间按表归位（NS/ALIAS_KEY/VER_META/RECON_META/SWITCH_LOG）
    except ValueError as exc:
        raise LoadError(str(exc)) from exc
    live = get_live(r)
    if version == live:
        raise LoadError(f"版本 {version} 已是表 {table} 当前线上版，禁止原地重灌（换一个版本日期再灌；其他表不受影响）")

    pattern = f"{C.NS}:{version}:*"
    exist = scan_keys(r, pattern)
    if exist and not replace:
        raise LoadError(f"目标空间非空（{len(exist)} 键），重灌须 --replace")
    cleared = 0
    if exist and replace:
        pipe = r.pipeline(transaction=False)
        for k in exist:
            pipe.delete(k)
        pipe.execute()
        cleared = len(exist)

    tag_cols = list(columns) if columns else sorted({c for t in rows.values() for c in t})
    n_rows = len(rows)
    nonempty = sum(len(t) for t in rows.values())

    written: list[str] = []
    try:
        pipe = r.pipeline(transaction=False)
        n = 0
        for key in sorted(rows):
            dk = data_key(version, key)
            pipe.set(dk, json.dumps(rows[key], ensure_ascii=False, separators=(",", ":")))
            written.append(dk)
            n += 1
            if n % C.PIPE_BATCH == 0:
                pipe.execute()
                pipe = r.pipeline(transaction=False)
                if progress_cb:
                    try:
                        progress_cb(n, n_rows)   # 批级回调（B2 进度；异常不阻断装载）
                    except Exception:  # noqa: BLE001
                        pass
        pipe.execute()
        if progress_cb:
            try:
                progress_cb(n_rows, n_rows)
            except Exception:  # noqa: BLE001
                pass
    except Exception:
        # 失败不写脏：尽力清掉本次已写键（本空间尚未上线；清干净便于重跑）
        try:
            cp = r.pipeline(transaction=False)
            for k in written:
                cp.delete(k)
            cp.execute()
        except Exception:  # noqa: BLE001 —— 清理失败不掩盖原始异常
            pass
        raise

    data_as_of = data_as_of or f"{version[:4]}-{version[4:6]}-{version[6:8]}"
    coverage = nonempty / (n_rows * len(tag_cols)) if tag_cols else 0.0
    meta = {
        "rows": n_rows, "tag_cols": len(tag_cols),
        "source": source or f"hbase:{table}:{cf}",
        "source_kind": "hbase", "table": table, "cf": cf,
        "source_sha256": source_sha256 or snapshot_sha256(rows),
        "hbase_snapshot_sha256": snapshot_sha256(rows),
        "coverage_src": f"{coverage:.6f}",
        "nonempty_per_user_avg": f"{nonempty / n_rows:.2f}",
        "data_as_of": data_as_of, "loaded_at": now_iso(),
    }
    ver_meta_set(r, version, **meta)   # 最后一步：元信息＝空间完成的标记

    # 卡2 装载副产品：表级 schema 元数据＋tabver 索引（B3.2 写入顺序 schema→tables→tabver，
    # 任一失败＝装载报错不静默半成品；ver_meta 已在则如实保留，由重跑/换版本处理）。
    # 字段清单＝本次装载观测列 tag_cols（数据面事实）；类型/中文名富化自表级字典 CSV
    # （缺则如实 unknown/待补）。CLI 与 HTTP 两入口同经本函数＝唯一写入路径——卡5 守卫
    # 「已装载表必有 schema」据此对全部装载路径成立。
    import schema_meta
    schema_fields, schema_source = schema_meta.build_fields(table, cf, tag_cols)
    schema_info = schema_meta.schema_meta_set(r, table, version, schema_fields,
                                              source=schema_source)
    schema_meta.tabver_index_add(r, table, version)

    return {"loaded": n_rows, "cleared_before_load": cleared, "tag_cols": len(tag_cols),
            "nonempty_cells": nonempty, "coverage_src": round(coverage, 6),
            "source_sha256": meta["source_sha256"],
            "hbase_snapshot_sha256": meta["hbase_snapshot_sha256"],
            "data_as_of": data_as_of, "schema": schema_info}


def load(r, pool, table: str, cf: str, version: str, *, columns: list[str] | None = None,
         rowkeys: list[str] | None = None, replace: bool = False,
         source_csv: str | None = None, progress_cb=None) -> dict:
    """一次加载：HBase 读（先）→ Redis 新空间写（后）。HBase 失败时零 Redis 写入。

    外层按表键空间作用域（table_scope）：本函数返回后模块键常量还原——防进程内
    连续多表加载/后续他域操作读到被本表污染的键常量（CLI 单进程无所谓，进程内复用
    时是实义正确性）。
    """
    with C.table_scope(table):
        rows = read_rows(pool, table, cf, columns=columns, rowkeys=rowkeys)
        if not rows:
            raise LoadError(f"HBase 表 {table}（列族 {cf}）无数据行（先灌数或核对表/列族）")
        source = f"hbase:{table}:{cf}"
        source_sha256 = None
        if source_csv:
            # 开发期：HBase 侧数据来源＝同一份造数 CSV；记录其指纹供既有 reconcile.py 前置查复用。
            source_sha256 = sha256_file(source_csv)
            source = source_csv
        return write_space(r, version, rows, columns=columns, replace=replace,
                           source=source, source_sha256=source_sha256, table=table, cf=cf,
                           progress_cb=progress_cb)


# ---------- CLI ----------

def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HBase → Redis 加载器（HBase 冷存储 → Redis 热存储）")
    ap.add_argument("--version", required=True, help="目标版本（YYYYMMDD，非当前线上版）")
    ap.add_argument("--service-id", default=None,
                    help="取表/列族的服务条目（须显式；原缺省 demo_hbase_query 已随 oneserve_demo "
                         "演示遗留清理删除 2026-10-08。或 --table+--cf 直传）")
    ap.add_argument("--table", default=None, help="覆盖表名（缺省读 services.yaml backend.hbase）")
    ap.add_argument("--cf", default=None, help="覆盖列族（缺省读 services.yaml backend.hbase）")
    ap.add_argument("--columns", default=None, help="列清单（逗号分隔；缺省＝该列族全部列）")
    ap.add_argument("--keys", default=None, help="按 rowkey 批量点查（逗号分隔；缺省＝整表 scan）")
    ap.add_argument("--replace", action="store_true", help="目标空间非空时先清空重灌")
    ap.add_argument("--source-csv", default=None, help="源指纹引用（开发期同源造数 CSV；只取 sha256）")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args(argv)

    host = args.host or os.environ.get("HBASE_HOST", "127.0.0.1")
    port = int(args.port or os.environ.get("HBASE_PORT", 9090))
    columns = _split(args.columns)
    rowkeys = _split(args.keys)
    t0 = time.perf_counter()

    # ① 配置解析（表/列族缺省读 services.yaml 单一真源）
    try:
        if args.table and args.cf:
            table, cf = args.table, args.cf
        else:
            table, cf = resolve_backend(args.service_id)
    except HBaseLoadError as exc:
        print(json.dumps({"ok": False, "side": "config", "error": str(exc)}, ensure_ascii=False))
        return 1

    # ② Redis 连接（不可达即如实报错，不进入任何写入）
    try:
        r = redis_conn()
    except Exception as exc:  # noqa: BLE001 —— redis 连接异常族
        print(json.dumps({"ok": False, "side": "redis",
                          "error": f"Redis 不可达：{type(exc).__name__}: {exc}",
                          "fallback": "无（Redis 不可达即如实报错，不回退假数据）"},
                         ensure_ascii=False))
        return 1

    # ③ HBase 读（先读全，再写；读失败 → 零 Redis 写入）
    try:
        pool = connect(host, port)
        list_tables(pool, host, port)     # 探活
        res = load(r, pool, table, cf, args.version, columns=columns, rowkeys=rowkeys,
                   replace=args.replace, source_csv=args.source_csv)
    except (LoadError, HBaseLoadError) as exc:
        print(json.dumps({"ok": False, "side": "hbase", "version": args.version,
                          "host": f"{host}:{port}", "table": table, "cf": cf,
                          "error": str(exc),
                          "fallback": "无（HBase 不可达/源非法即如实报错，不回退假数据）"},
                         ensure_ascii=False))
        return 1

    print(json.dumps({
        "ok": True, "mode": "hbase->redis-load", "version": args.version,
        "host": f"{host}:{port}", "table": table, "cf": cf,
        "columns_from": "清单" if columns else "列族全部列",
        "read_mode": "rowkey批量" if rowkeys else "整表scan",
        **res, "elapsed_s": round(time.perf_counter() - t0, 2),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
