# -*- coding: utf-8 -*-
"""HBase 执行器雏形（TASK-M2-1）：Thrift 连接池＋get/scan 最小接口。

范围界定（门槛稿 TASK-M2-1 行）：
  - 本卡只做「连接层」：happybase 连接池＋get/scan 接口雏形＋健康检查探活；
  - 不接业务路由/投影/脱敏/审计——那是 TASK-M2-3（hbase 执行器＋数据源抽象）
    沿 kv_executor 的 TagQueryError→services.yaml 模式接电；
  - Py3.13 实测 happybase 可装（纯 python，依赖 thrift 未实测——见探针留证）。

设计约束（沿 kv_executor 同款纪律）：
  - 连接池经参数注入（鸭子类型），单测用假连接池覆盖全路径，无容器也能跑；
  - 表名/列族零硬编码，配置驱动留给 M2-3 的 services.yaml 扩展；
  - happybase 延迟导入：无容器环境（CI/单测）不强制安装。
"""
from __future__ import annotations

import asyncio
import time

from app import errors
from typing import Any

# 默认 Thrift1 端口（happybase 兼容口径；NC-M2-1 真集群端口待答后由配置覆盖）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9090
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_POOL_SIZE = 4


class HBaseQueryError(Exception):
    """HBase 执行器可控错误：携带 HTTP 状态码与机读错误码（端点层直接映射响应）。

    口径沿 kv_executor.TagQueryError——M2-3 接电时端点层复用同一映射路径。
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class HBaseClient:
    """happybase 连接池封装：get/scan 最小接口雏形（M2-3 再扩投影/脱敏/审计）。

    pool 参数鸭子类型注入：需要 with_connection() 上下文方法返回 (conn, None)
    （或 happybase.ConnectionPool 实例本身可作上下文管理器）。单测用假池即可
    覆盖取数/限行/超时/表缺失全路径，无容器、无 happybase 也能跑。
    """

    def __init__(
        self,
        pool: Any,
        *,
        default_limit: int = 100,
        max_limit: int = 1000,
        query_timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._pool = pool
        self._default_limit = default_limit
        self._max_limit = max_limit
        self._query_timeout_s = query_timeout_s

    # ---------- 连接层 ----------

    def _acquire(self) -> Any:
        """从池取连接：happybase.ConnectionPool 直接可作上下文管理器。"""
        return self._pool.connection()

    # ---------- 执行接口（雏形） ----------

    @staticmethod
    def _to_bytes(value: str | bytes) -> bytes:
        """行键/前缀边界编码：接口层说 str，Thrift 层说 bytes（happybase 文档惯例）。"""
        return value.encode("utf-8") if isinstance(value, str) else value

    def get(self, table: str, rowkey: str, columns: list[str] | None = None) -> dict[str, str]:
        """单行点查：rowkey → {column: value(str)}；行不存在＝空 dict（端点层决定 404 语义）。

        columns 为 None 时取整行；否则仅取白名单列（M2-3 在此挂授权投影）。
        注（M2-3 实测修正）：happybase 的 row() 在 Table 对象上而非 Connection——
        连接层须先 conn.table(name)（M2-1 mock 假 API 自证通过、真容器 e2e 暴露）。
        """
        with self._acquire() as conn:
            try:
                row = conn.table(table).row(self._to_bytes(rowkey), columns=columns)
            except Exception as exc:  # happybase 异常族宽，M2-3 收窄
                raise HBaseQueryError(502, errors.HBASE_UPSTREAM_ERROR, f"HBase 点查失败: {exc}") from exc
        # happybase 返回 bytes 值 → 统一转 str（KV 执行器同口径：值即 str）
        return {k.decode("utf-8", "replace"): v.decode("utf-8", "replace") for k, v in row.items()}

    def scan(self, table: str, *, row_prefix: str | None = None, limit: int | None = None,
             columns: list[str] | None = None) -> list[dict[str, Any]]:
        """前缀扫描雏形：返回 [{row, columns:{col: value}}]，limit 封顶防拖库。

        max_limit 硬上限（默认 1000）服务层强制——网关级熔断/限流之外的第二道闸。
        """
        effective_limit = self._default_limit if limit is None else min(int(limit), self._max_limit)
        if effective_limit <= 0:
            raise HBaseQueryError(400, errors.HBASE_BAD_LIMIT, "limit 须为正整数")
        kwargs: dict[str, Any] = {"limit": effective_limit}
        if columns:
            kwargs["columns"] = columns
        if row_prefix is not None:
            kwargs["row_prefix"] = self._to_bytes(row_prefix)
        out: list[dict[str, Any]] = []
        with self._acquire() as conn:
            try:
                scanner = conn.table(table).scan(**kwargs)   # M2-3：scan 同在 Table 对象上
                for row_key, row_data in scanner:
                    out.append({
                        "row": row_key.decode("utf-8", "replace"),
                        "columns": {k.decode("utf-8", "replace"): v.decode("utf-8", "replace")
                                    for k, v in row_data.items()},
                    })
            except Exception as exc:
                raise HBaseQueryError(502, errors.HBASE_UPSTREAM_ERROR, f"HBase 扫描失败: {exc}") from exc
        return out

    # ---------- 元数据发现（TASK-M2-4 / AC-4） ----------

    @staticmethod
    def _decode(value: Any) -> str:
        """Thrift bytes → str（表名/列族名/列名统一口径）。"""
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

    def tables(self) -> list[str]:
        """列全部表名（HBase meta：list_tables）；不可达→503 HBASE_UNAVAILABLE（如实报错）。"""
        with self._acquire() as conn:
            try:
                return sorted(self._decode(t) for t in conn.tables())
            except HBaseQueryError:
                raise
            except Exception as exc:  # happybase/thrift 异常族宽
                raise HBaseQueryError(503, "HBASE_UNAVAILABLE",
                                      f"HBase 不可达（list_tables 失败）: {exc}") from exc

    def discover(self, *, sample_limit: int = 1) -> dict[str, Any]:
        """HBase meta 发现（AC-4）：namespace → 表 → 列族 → 列。

        真源＝HBase 自身 meta：
          - 表清单＝Connection.tables()（getTableNames）；
          - 列族＝Table.families()（getColumnDescriptors）；
          - 列＝对每表取 sample_limit 行样本反推（HBase meta 不提供列级清单，
            空表/无权则列清单为空＝如实，不伪造列名）。
        namespace 由表名 `ns:table` 解析（happybase 1.3 无 list_namespaces API，
        如实标注；无冒号＝default）。

        返回 {"namespaces": {ns: {table: {"families": {cf: [cols]}}}}, "table_count": n}。
        HBase 不可达→HBaseQueryError(503/HBASE_UNAVAILABLE)；元数据读取失败→502。
        """
        with self._acquire() as conn:
            try:
                names = sorted(self._decode(t) for t in conn.tables())
            except HBaseQueryError:
                raise
            except Exception as exc:
                raise HBaseQueryError(503, "HBASE_UNAVAILABLE",
                                      f"HBase 不可达（元数据发现失败）: {exc}") from exc
            out: dict[str, dict] = {}
            for full in names:
                try:
                    table = conn.table(full)
                    fams = [self._decode(f) for f in table.families()]
                except Exception as exc:
                    raise HBaseQueryError(502, "HBASE_UPSTREAM_ERROR",
                                          f"列族读取失败 {full}: {exc}") from exc
                cols: dict[str, list[str]] = {cf: [] for cf in fams}
                if sample_limit > 0:
                    try:
                        for _row, data in table.scan(limit=sample_limit):
                            for col in data:
                                cf, _, name = self._decode(col).partition(":")
                                cols.setdefault(cf, [])
                                if name and name not in cols[cf]:
                                    cols[cf].append(name)
                    except Exception:  # noqa: BLE001 —— 空表/无读权：列清单留空，如实不伪造
                        pass
                ns, _, tbl = full.partition(":")
                if not tbl:
                    ns, tbl = "default", full
                out.setdefault(ns, {})[tbl] = {"families": {cf: sorted(v) for cf, v in cols.items()}}
            return {"namespaces": out, "table_count": len(names)}

    # ---------- 健康检查 ----------

    def health(self, probe_table: str = "oneserve_healthcheck") -> dict[str, Any]:
        """连通性自测：list_tables（表元数据在 Connection 上，与行级操作区分）。

        probe_table 参数保留（调用方兼容），当前实现只做表列表探活——行级读写
        通不通由业务查询路径即时暴露（502 口径），不在健康检查里建删表。
        """
        started = time.monotonic()
        try:
            with self._acquire() as conn:
                tables = [t.decode() if isinstance(t, bytes) else t for t in conn.tables()]
            return {"ok": True, "tables": len(tables), "latency_ms": round((time.monotonic() - started) * 1000, 1)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}


# ---------- 阻塞→异步桥（沿 service 层 asyncio 端点口径） ----------

async def run_get(client: HBaseClient, table: str, rowkey: str,
                  columns: list[str] | None = None) -> dict[str, str]:
    """线程池里跑阻塞 Thrift 调用，包超时——FastAPI 端点层直接 await。"""
    return await asyncio.wait_for(
        asyncio.to_thread(client.get, table, rowkey, columns), timeout=client._query_timeout_s
    )


async def run_scan(client: HBaseClient, table: str, *, row_prefix: str | None = None,
                   limit: int | None = None, columns: list[str] | None = None) -> list[dict[str, Any]]:
    """scan 的异步桥：同 run_get 口径。"""
    return await asyncio.wait_for(
        asyncio.to_thread(client.scan, table, row_prefix=row_prefix, limit=limit, columns=columns),
        timeout=client._query_timeout_s,
    )


# ---------- 失败诊断三分类（卡1：不可达〔含未启动〕/ 认证失败 / 其他） ----------

# 认证失败信号（连接建立后被拒）：异常类型名/消息命中即归认证类。
# 注意与不可达互斥的先验：纯网络错误几乎不含这些词，先判认证更稳。
_AUTH_MARKERS = ("auth", "credential", "gssapi", "sasl", "kerberos", "keytab",
                 "unauthorized", "unauthenticated", "permission denied",
                 "access denied", "401", "403")
# 不可达信号（含容器未启动/端口不通/DNS 解析失败/超时）：thrift/socket 断连族。
_UNREACHABLE_MARKERS = ("could not connect", "connection refused", "connection reset",
                        "connection aborted", "connection closed", "broken pipe",
                        "read 0 bytes", "timed out", "timeout", "temporarily unavailable",
                        "name or service not known", "nodename nor servname provided",
                        "no route to host", "network is unreachable", "network is down",
                        "errno 111", "errno 61", "errno 64",
                        # 本模块自包消息的中文标记（「HBase 不可达（list_tables 失败）…」族）
                        "不可达", "拒绝连接", "连接被拒", "无法连接")


def classify_hbase_failure(exc: BaseException, *, endpoint: str = "") -> dict:
    """把一次 HBase 连接/调用失败归入三分类（卡1 诊断口径；纯函数可单测）。

    分类（先认证后不可达）:
      unreachable —— 不可达（含容器未启动、地址/端口不通、DNS 失败、超时）；
                     文案带行动指引「开发态一键起：bash scripts/up_hbase.sh」；
      auth_failed —— 认证失败（凭据/主体/密钥文件不可用）；文案指向 connections.yaml
                     认证配置与密钥注入（值走环境变量/密钥文件，永不落仓）；
      other       —— 其他（如实带原始异常摘要；happybase 驱动缺失等环境问题在此类）。
    返回 {category, code, status, message}；发现端点（admin_api）原样透出，
    「不返回假元数据」口径不变。HBaseQueryError 自带的非通用机读码
    （如 HBASE_DRIVER_MISSING）原样保留，不吞成三分类码。
    """
    text = f"{type(exc).__name__} {exc}".lower()
    reason = f"{type(exc).__name__}: {str(exc)[:160]}".strip()
    where = endpoint or "配置的 HBase 地址"
    # 已登记的非通用机读码原样保留（驱动缺失等环境问题自有码表语义）
    preserve = None
    if isinstance(exc, HBaseQueryError) and exc.code not in ("HBASE_UNAVAILABLE",
                                                             "HBASE_UPSTREAM_ERROR"):
        preserve = (exc.code, exc.status)

    def _out(category: str, code: str, status: int, message: str) -> dict:
        if preserve is not None:
            code, status = preserve
        return {"category": category, "code": code, "status": status, "message": message}

    if any(m in text for m in _AUTH_MARKERS):
        return _out("auth_failed", "HBASE_AUTH_FAILED", 503,
                    f"HBase 认证失败（{reason}）——连接已建立但被拒。请核对 "
                    "config/connections.yaml 的 auth_mode/user/principal 与注入的密钥"
                    "（密钥值经环境变量或密钥文件提供，永不落仓）。不返回假元数据。")
    if isinstance(exc, TimeoutError) or any(m in text for m in _UNREACHABLE_MARKERS):
        return _out("unreachable", "HBASE_UNAVAILABLE", 503,
                    f"HBase 不可达（{where}，{reason}）——容器未启动、地址或网络不通都会"
                    "走到这里。开发态一键起：bash scripts/up_hbase.sh（起后稍候再刷新）；"
                    "生产态请核对 MRS 地址/端口与网络策略。不返回假元数据。")
    return _out("other", "HBASE_UPSTREAM_ERROR", 502,
                f"HBase 连接异常（{reason}）——非「不可达/认证失败」两类；"
                "请查看服务端日志定位。不返回假元数据。")


# ---------- happybase 池工厂（延迟导入；无容器环境不强制装 happybase） ----------

def make_pool(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
              size: int = DEFAULT_POOL_SIZE, timeout_s: float = DEFAULT_TIMEOUT_S,
              *, transport: str = "buffered", protocol: str = "binary") -> Any:
    """建 happybase.ConnectionPool；happybase 缺席时抛可读错误（探针/端点层转 503）。"""
    try:
        import happybase  # 延迟导入：单测/CI 不强制安装
    except ImportError as exc:
        raise HBaseQueryError(
            503, errors.HBASE_DRIVER_MISSING,
            "happybase 未安装（requirements 已列；容器环境 pip install -r service/requirements.txt）",
        ) from exc
    return happybase.ConnectionPool(
        size=size, host=host, port=port, timeout=timeout_s * 1000,  # happybase 超时单位 ms
        transport=transport, protocol=protocol,
    )
