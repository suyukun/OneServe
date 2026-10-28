# -*- coding: utf-8 -*-
"""HBase 开发期造数装载器（TASK-M2-11.5）。

⚠️ 定位声明（架构前提，不得误读；见 docs/plans/阶段范围裁定_20260923.md）：
本器是【开发期造数工具】，只在本机 HBase 替身容器（compose.hbase.yml，默认不起、
显式叠加才起）上使用，用完删容器让位内存。生产/测试环境的 HBase 是示例 HBase 集群
现役组件、运维归 MRS 团队；数据由数仓 T+1 直写（Data Studio/Flink），**平台不承担
生产写入**。本器绝不接入 up.sh / 主 docker-compose.yml，也绝不是生产写入路径。

数据同源：造数复用 sync/gen_mock_source.py（297 列真结构＋假值，键＝SM3 真算），
与 sync/loader.py 灌 Redis 用的是同一份 CSV——本器只负责把同一份数据写进 HBase。

写入口径（显式二选一，2026-10-08 起无隐式缺省）：
  - --service-id：表/列族从 services.yaml 的 backend.hbase 读（与在线查询同一份配置）；
  - --table：直传表名（cf 缺省 cf）——原缺省服务 demo_hbase_query 已随 oneserve_demo
    演示遗留清理删除（P3-数据工程 2026-10-08），两者都不给即报错；
  - rowkey ＝ 上游供数键（造数工具实算；与 Redis 通道同口径）；
  - 列 ＝ cf:<标签名>，值 ＝ 非空标签值字符串；
  - 幂等：目标表非空时拒绝，须 --replace 显式清空重灌（沿 sync/loader.py 同款口径）。

不可达口径：连接/建表/写失败一律显式抛 HBaseLoadError，退出码 1，**不回退假数据**。

用法（宿主机自测；hbase 容器 9090 已映射宿主）：
  python3.13 sync/hbase_loader.py --csv sync/out/src_20260920.csv --version 20260920 --table usr_asset_tag --replace
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 宿主机现场＝<repo>/service；容器内镜像＝/app（Dockerfile WORKDIR，app 包所在）。
# 两候选都注入，同一脚本 host/容器两处可跑（不复制第二实现）。
for _p in (str(REPO / "sync"), str(REPO / "service"), "/app"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C                                  # noqa: E402
from common import sha256_file                           # noqa: E402
from app import kv_executor as kvx                       # noqa: E402
from app.hbase_executor import HBaseQueryError, make_pool  # noqa: E402

DEFAULT_TABLE_CF = "cf"   # --table 直传时的缺省列族（显式绕过 services.yaml；原缺省服务
#                         #  demo_hbase_query 已随 oneserve_demo 演示遗留清理删除，2026-10-08）


class HBaseLoadError(RuntimeError):
    """HBase 装载/读取可控错误：不可达或表操作失败一律显式抛，不静默降级、不回退假数据。"""


# ---------- 配置解析（表/列族只认 services.yaml 单一真源） ----------

def resolve_backend(service_id: str | None = None, services=None) -> tuple[str, str]:
    """从注册表取 (table, cf)：与在线查询用同一份 backend.hbase，避免第二份表名。

    service_id 须显式传入（无缺省）：原缺省 demo_hbase_query 已随 oneserve_demo
    演示遗留清理删除（2026-10-08）；None/未登记一律显式报错，不猜表名。
    """
    if not service_id:
        raise HBaseLoadError("--service-id 未指定：原缺省服务 demo_hbase_query 已删（2026-10-08），"
                             "须显式 --service-id 或改用 --table 直传表名")
    services = services if services is not None else kvx.load_services()
    entry = services.get(service_id)
    if entry is None:
        raise HBaseLoadError(f"services.yaml 无服务 {service_id}")
    hb = (entry.get("backend") or {}).get("hbase") or {}
    if not hb.get("table") or not hb.get("cf"):
        raise HBaseLoadError(f"服务 {service_id} 缺 backend.hbase.table/cf")
    return hb["table"], hb["cf"]


# ---------- 源数据（与 Redis 链路同源：同一份 CSV） ----------

def read_csv(path: str | Path) -> dict:
    """读造数 CSV：键列＝造数键（小写 64 位，本造数工具实算），其余列为非空标签（空串即缺省）。"""
    keys: list[str] = []
    rows: list[dict] = []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or C.KEY_COLUMN not in reader.fieldnames:
            raise HBaseLoadError(f"CSV 缺键列 {C.KEY_COLUMN}：{path}")
        tag_cols = [c for c in reader.fieldnames if c != C.KEY_COLUMN]
        for raw in reader:
            key = (raw.pop(C.KEY_COLUMN) or "").strip().lower()
            if len(key) != 64:
                raise HBaseLoadError(f"键非 64 位（本造数工具口径）：{key[:16]}…（{path}）")
            keys.append(key)
            rows.append({k: v for k, v in raw.items() if v != ""})
    if not keys:
        raise HBaseLoadError(f"CSV 无数据行：{path}")
    return {"keys": keys, "rows": rows, "tag_cols": tag_cols,
            "nonempty_cells": sum(len(r) for r in rows),
            "sha256": sha256_file(path)}


# ---------- 连接层（复用 app.hbase_executor.make_pool） ----------

def connect(host: str, port: int, *, size: int = 2, timeout_s: float = 30.0):
    """建 happybase 连接池（hbase_executor.make_pool＝同一连接层，不复制第二实现）。

    注：happybase.ConnectionPool.__init__ 会**即时建立首个连接**，HBase 不可达时在此
    就抛 TTransportException（而非等到第一次取连接）。故这里捕获所有异常并统一转
    HBaseLoadError——不可达必须如实报错，绝不静默回退假数据。
    """
    try:
        return make_pool(host=host, port=int(port), size=size, timeout_s=timeout_s)
    except HBaseQueryError as exc:
        raise HBaseLoadError(f"HBase 驱动/连接池不可用（{host}:{port}）：{exc.message}") from exc
    except Exception as exc:  # noqa: BLE001 —— thrift/happybase 连接异常族
        raise HBaseLoadError(f"HBase 不可达（{host}:{port}）：{type(exc).__name__}: {exc}") from exc


def list_tables(pool, host: str, port: int) -> list[str]:
    """探活：列全部表；不可达即抛 HBaseLoadError（绝不吞错、绝不回退假数据）。"""
    try:
        with pool.connection() as conn:
            return _decode_tables(conn.tables())
    except Exception as exc:  # noqa: BLE001 —— happybase/thrift 异常族宽，统一转可控错误
        raise HBaseLoadError(f"HBase 不可达（{host}:{port}）：{type(exc).__name__}: {exc}") from exc


def _decode_tables(raw) -> list[str]:
    """HBase Thrift tables() 返回 bytes 表名；统一解码为 str。"""
    return [t.decode() if isinstance(t, bytes) else t for t in raw]


def _table_exists(conn, table: str, tables=None) -> bool:
    """表是否存在——**必须容忍 Thrift tables() 对 default 命名空间的剥名**。

    实测（M2-11.5 真容器）：create_table("default:oneserve_demo") 后 tables() 返回
    "oneserve_demo"（不带 default: 前缀）；故直接等值比较会把已建的表误判为不存在，
    重跑即 create_table → AlreadyExists。此处同时匹配全名与命名空间剥离名（仅 default）。
    """
    names = _decode_tables(conn.tables()) if tables is None else list(tables)
    short = table.split(":", 1)[1] if ":" in table else table
    return table in names or short in names


# ---------- 写入原语（接受鸭子类型 table，便于无容器单测） ----------

def table_has_rows(table, *, limit: int = 1) -> bool:
    """表内是否已有数据行（幂等闸：非空即拒，除非 --replace）。"""
    return next(iter(table.scan(limit=limit)), None) is not None


def clear_rows(table, *, batch_size: int = C.PIPE_BATCH) -> int:
    """清空表内全部行（scan＋batch delete；HBase 行级删除）。返回删除行数。"""
    n = 0
    with table.batch(batch_size=batch_size) as batch:
        for row_key, _ in table.scan():
            batch.delete(row_key)
            n += 1
    return n


def write_rows(table, keys, rows, cf: str, *, batch_size: int = C.PIPE_BATCH) -> int:
    """批量写入行：rowkey＝键，列＝cf:<标签名>，值＝标签值（bytes 口径）。返回写入行数。"""
    n = 0
    with table.batch(batch_size=batch_size) as batch:
        for key, tags in zip(keys, rows):
            batch.put(key.encode("utf-8"),
                      {f"{cf}:{c}".encode("utf-8"): v.encode("utf-8") for c, v in tags.items()})
            n += 1
    return n


def load(pool, table: str, cf: str, data: dict, *, replace: bool = False,
         batch_size: int = C.PIPE_BATCH) -> dict:
    """一次完整装载（单连接内完成建表/幂等闸/清空/写入/复核）。"""
    try:
        with pool.connection() as conn:
            created = False
            if not _table_exists(conn, table):
                conn.create_table(table, {cf: {"max_versions": 1}})
                created = True
            t = conn.table(table)
            if table_has_rows(t) and not replace:
                raise HBaseLoadError(f"目标表 {table} 非空，重灌须 --replace（防误覆盖）")
            cleared = clear_rows(t, batch_size=batch_size) if replace else 0
            written = write_rows(t, data["keys"], data["rows"], cf, batch_size=batch_size)
            rows_in_table = sum(1 for _ in t.scan())
    except HBaseLoadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HBaseLoadError(f"装载 HBase 表 {table} 失败：{type(exc).__name__}: {exc}") from exc
    return {"created": created, "cleared": cleared, "written": written,
            "rows_in_table": rows_in_table}


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开发期造数 → HBase（非生产写入路径）")
    ap.add_argument("--csv", required=True, help="与 Redis 链路同源的造数 CSV")
    ap.add_argument("--version", required=True, help="数据版本（YYYYMMDD；仅溯源，rowkey 无版本）")
    ap.add_argument("--replace", action="store_true", help="目标表非空时先清空重灌")
    ap.add_argument("--service-id", default=None,
                    help="取表/列族的服务条目（须显式；与 --table 互斥。原缺省 demo_hbase_query "
                         "已随 oneserve_demo 演示遗留清理删除，2026-10-08）")
    ap.add_argument("--table", default=None,
                    help="直接指定表名（namespace:table 或 table；与 --service-id 互斥，"
                         "绕过 services.yaml 解析，cf 缺省 cf）——业务化测试表直传用")
    ap.add_argument("--host", default=None, help="HBase Thrift 主机（缺省 HBASE_HOST 或 127.0.0.1）")
    ap.add_argument("--port", type=int, default=None, help="HBase Thrift 端口（缺省 HBASE_PORT 或 9090）")
    args = ap.parse_args(argv)

    host = args.host or os.environ.get("HBASE_HOST", "127.0.0.1")
    port = int(args.port or os.environ.get("HBASE_PORT", 9090))
    t0 = time.perf_counter()
    try:
        if args.table and args.service_id:
            raise HBaseLoadError("--table 与 --service-id 互斥：表名直传时不再经 services.yaml 解析")
        if not args.table and not args.service_id:
            raise HBaseLoadError("须显式 --service-id 或 --table：原缺省服务 demo_hbase_query 已随 "
                                 "oneserve_demo 演示遗留清理删除（2026-10-08），不再有隐式缺省")
        data = read_csv(args.csv)
        if args.table:
            table, cf, via = args.table, DEFAULT_TABLE_CF, "--table 直传（绕过 services.yaml）"
        else:
            table, cf = resolve_backend(args.service_id)
            via = f"services.yaml（{args.service_id}.backend.hbase）"
        pool = connect(host, port)
        list_tables(pool, host, port)          # 探活：不可达即抛，不进入假写入
        res = load(pool, table, cf, data, replace=args.replace)
    except HBaseLoadError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "host": f"{host}:{port}",
                          "fallback": "无（HBase 不可达即如实报错，不回退假数据）"},
                         ensure_ascii=False))
        return 1

    print(json.dumps({
        "ok": True, "version": args.version, "mode": "dev-load-only(非生产写入)",
        "host": f"{host}:{port}", "table": table, "cf": cf, "resolved_via": via,
        "rows": res["written"], "tag_cols": len(data["tag_cols"]),
        "coverage_src": round(data["nonempty_cells"] / (len(data["keys"]) * len(data["tag_cols"])), 6),
        "source_sha256": data["sha256"], "created_table": res["created"],
        "cleared_before_load": res["cleared"], "rows_in_table": res["rows_in_table"],
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "key_algo": "上游供数侧生成(与 sync/gen_mock_source.py 造数同源)",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
