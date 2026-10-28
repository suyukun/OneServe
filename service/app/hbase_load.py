# -*- coding: utf-8 -*-
"""加载服务层：HBase（冷存储）→ Redis（热存储）——复用既有加载器，不重造读写。

卡：发布台改造（M2-6 端到端最后一环）。
定位（docs/plans/M2范围裁定_v2-架构纠正_20260923.md 第一节 / ADR-0005）：
  「页面选表 → 自动同步加载到 Redis」这一步的可调用入口。**读写全复用**
  sync/hbase_to_redis.py（HBase 读 + Redis 写 + 双缓冲 + 失败不写脏）与
  sync/hbase_reconcile.py（同源对账 compare/read_*）——本模块只做编排与口径归一，
  **零第二实现**。

分层纪律：本模块是纯逻辑（无 FastAPI），HTTP 编排归 app/admin_api.py；与
ops_config.py 同款（admin 端点只做 token/JSON/线程池桥）。

不可达口径（沿加载器）：HBase / Redis 任一不可达一律显式抛可控错误，**不写脏、不回退**。
幂等：目标空间非空且未 replace → LoadError（拒灌）；version == 线上版 → 拒灌。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]      # <repo>/service/app/hbase_load.py → <repo>


class HBaseLoadServiceError(RuntimeError):
    """加载服务层可控错误（不可达/源非法）。端点层映射为如实错误响应。"""

    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _sync_dir() -> Path | None:
    """sync/ 脚本目录定位：env SYNC_DIR > 容器布局 /sync > 宿主布局 <repo>/sync。

    与 ops_config.resolve_schema_csv 同款双布局纪律（宿主/容器两处可跑）。
    """
    env = os.environ.get("SYNC_DIR")
    if env and (Path(env) / "hbase_to_redis.py").is_file():
        return Path(env)
    ctr = Path("/sync")
    if (ctr / "hbase_to_redis.py").is_file():
        return ctr
    host = REPO / "sync"
    if (host / "hbase_to_redis.py").is_file():
        return host
    return None


def _sync_modules():
    """延迟导入既有加载/对账模块（唯一一份实现；host/容器两处同源）。"""
    d = _sync_dir()
    if d is None:
        raise HBaseLoadServiceError(
            "LOADER_UNAVAILABLE",
            "加载器未就位：sync/（hbase_to_redis.py / hbase_reconcile.py）不可见——"
            "请确认 <repo>/sync 存在，或容器内已挂载 /sync")
    p = str(d)
    if p not in sys.path:
        sys.path.insert(0, p)
    import hbase_to_redis as h2r      # noqa: E402 —— 既有加载器（读写唯一实现）
    import hbase_reconcile as hr      # noqa: E402 —— 既有同源对账（compare/read_*）
    return h2r, hr


def _import(name: str):
    """取既有模块顶层符号（延迟导入；不复制第二实现）。"""
    d = _sync_dir()
    if d is None:
        raise HBaseLoadServiceError("LOADER_UNAVAILABLE", "加载器未就位：sync/ 不可见")
    p = str(d)
    if p not in sys.path:
        sys.path.insert(0, p)
    return __import__(name)


def _default_endpoint() -> tuple[str, int]:
    """卡1 双轨缺省端点：host/port 未显式给出时经 connections.load_hbase_connection()
    装配（优先级链 env HBASE_* > config/connections.yaml > 代码默认）——装载链路与
    发现链路同源，不再各自读环境变量（卡2 T3 修复的卡1 尾巴）。
    """
    from app import connections as _cc
    conn = _cc.load_hbase_connection()
    return conn.host, conn.port


def load_into_redis(*, table: str, cf: str, version: str,
                    columns: list[str] | None = None, rowkeys: list[str] | None = None,
                    replace: bool = False, host: str | None = None, port: int | None = None,
                    source_csv: str | None = None,
                    redis_client=None, pool=None, progress_cb=None) -> dict:
    """一次「HBase → Redis 新版本空间」加载（复用既有加载器）＋同源对账。

    入参：表（namespace:table）/列族/版本/列清单（缺省＝列族全部列）/rowkey 清单（缺省＝整表 scan）；
         replace＝目标空间非空时先清空重灌（幂等闸，沿既有口径）。
    redis_client / pool 仅供单测注入假实现（生产走既有连接层）。
    返回：{loaded, tag_cols, nonempty_cells, coverage_src, source_sha256, data_as_of,
          cleared_before_load, reconcile:{pass,diffCount,checks,diffs}, elapsedMs}。
    副作用：对账结果按命令行同款口径落 sync:recon:<表>:<版本>（common.recon_meta_set，
          P0-2：不写则 switch.py 第二闸永远判「未对账」拒切）。
    失败：HBaseLoadServiceError（不可达/源非法/幂等拒灌）——不写脏、不回退。
    """
    h2r, hr = _sync_modules()
    hl = _import("hbase_loader")
    import time as _time
    t0 = _time.perf_counter()

    # ① Redis 连接（不可达即如实报错，不进入任何写入）
    if redis_client is None:
        try:
            redis_client = h2r.redis_conn()
        except Exception as exc:  # noqa: BLE001 —— redis 连接异常族
            raise HBaseLoadServiceError(
                "REDIS_UNAVAILABLE",
                f"Redis 不可达：{type(exc).__name__}: {exc}（无一滴脏写入）") from exc

    # ② HBase 连接（不可达即如实报错；读在写之前）——host/port 缺省走卡1 双轨装配
    #    （env > connections.yaml > 默认；与发现链路 main._build_hbase_client 同源）
    if pool is None:
        if host is None or port is None:
            dh, dp = _default_endpoint()
            host = host or dh
            port = int(port or dp)
        try:
            pool = hl.connect(host, int(port))
            hl.list_tables(pool, host, int(port))   # 探活
        except hl.HBaseLoadError as exc:
            raise HBaseLoadServiceError("HBASE_UNAVAILABLE", str(exc)) from exc

    # ③④⑤ 按表键空间作用域（2026-09-29 按表隔离）：加载/对账/落对账结果全部落在
    #    tags:<表>:… 本表空间（sync_config.table_scope 切域＋加锁防多表并发串台，退出恢复）；
    #    别的表的指针/空间零影响。
    cfg = _import("sync_config")
    try:
        with cfg.table_scope(table):
            # ③ 加载（读→写；幂等/双缓冲/失败不写脏全在既有加载器内）
            try:
                res = h2r.load(redis_client, pool, table, cf, version,
                               columns=columns, rowkeys=rowkeys, replace=replace,
                               source_csv=source_csv, progress_cb=progress_cb)
            except h2r.LoadError as exc:                 # 幂等/双缓冲口径：拒灌
                raise HBaseLoadServiceError("LOAD_CONFLICT", str(exc), 409) from exc
            except hl.HBaseLoadError as exc:
                raise HBaseLoadServiceError("HBASE_UNAVAILABLE", str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 —— thrift/redis 异常族宽：如实上报，不静默
                raise HBaseLoadServiceError(
                    "LOAD_UPSTREAM_ERROR",
                    f"加载失败（HBase/Redis 链路异常）：{type(exc).__name__}: {exc}") from exc

            # ④ 同源对账（复用既有 read_* + compare；无第二套比对逻辑）
            reconcile = _reconcile(hr, h2r, redis_client, pool, table, cf, version, columns)

            # ⑤ 对账结果落 Redis（P0-2 修复）：与命令行入口（sync/reconcile.py）**同一口径**写
            #    sync:recon:<表>:<版本>——switch.py 上线第二闸据此放行（按表判定）。序列化口径
            #    唯一实现＝sync/common.recon_meta_set（pass/checked_at/detail，零第二份字段拼接）；
            #    对账红/对账自身失败也如实写（pass=0），switch 照样拒切——不吞不美化。
            import common as sync_common   # noqa: E402 —— sync/ 目录已由 _sync_modules 挂载
            recon_detail = {"version": version, "table": table, "cf": cf,
                            "pass": bool(reconcile.get("pass")),
                            "diffCount": reconcile.get("diffCount", 0),
                            "checks": reconcile.get("checks", {}),
                            "diffs": reconcile.get("diffs", [])}
            if reconcile.get("error"):
                recon_detail["error"] = reconcile["error"]
            sync_common.recon_meta_set(redis_client, version, recon_detail["pass"], recon_detail)
    except ValueError as exc:   # 表名非法（key_domain 校验）：如实 400 口径拒收
        raise HBaseLoadServiceError("INVALID_TABLE", str(exc), 400) from exc

    res.update({"version": version, "table": table, "cf": cf,
                "elapsedMs": int((_time.perf_counter() - t0) * 1000),
                "reconcile": reconcile})
    return res


def _reconcile(hr, h2r, r, pool, table: str, cf: str, version: str,
               columns: list[str] | None) -> dict:
    """加载后同源对账（HBase 行集 vs Redis 键集）：复用既有 compare，差异逐条可解释。

    columns 给出时对 HBase 侧同样裁剪——比对口径＝本次加载的投影（同为选中列），
    否则会把「未加载的列」误判为差异（对账口径本身不改造，仍是 hr.compare）。
    """
    try:
        import sync_config as C
        keys = [k.rsplit(":", 1)[1] for k in h2r.scan_keys(r, f"{C.NS}:{version}:*")]
        if not keys:
            return {"pass": False, "diffCount": 0, "checks": {},
                    "diffs": [], "error": f"Redis 空间 {C.NS}:{version}:* 无键（加载未落键）"}
        redis_rows = hr.read_redis_rows(r, version, keys)
        hbase_rows = hr.read_hbase_rows(pool, table, cf)
        if columns:
            # 与加载读取层同口径：列投影后无任何非空标签的行不落键（否则对账会把
            # 「按投影过滤掉的空行」误判为 extra_in_hbase＝假差异）。
            allow = set(columns)
            hbase_rows = {k: {c: v for c, v in t.items() if c in allow}
                          for k, t in hbase_rows.items()}
            hbase_rows = {k: t for k, t in hbase_rows.items() if t}
        rep = hr.compare(redis_rows, hbase_rows)
        return {"pass": bool(rep.get("pass")), "diffCount": rep.get("diff_count", 0),
                "checks": rep.get("checks", {}), "diffs": rep.get("diffs", [])[:20]}
    except Exception as exc:  # noqa: BLE001 —— 对账自身失败如实上报，不改写「加载成功」事实
        return {"pass": False, "diffCount": 0, "checks": {}, "diffs": [],
                "error": f"对账失败：{type(exc).__name__}: {exc}"}
