#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HBase 执行器单测（TASK-M2-1）——连接池 mock，无容器也能跑。

红线断言（门槛稿 TASK-M2-1 判据「test_hbase_executor 骨架」）：
  ① get/scan 雏形走通：值 bytes→str 统一、limit 封顶（max_limit 服务层硬闸）；
  ② 错误语义可控：表/上游异常→HBaseQueryError(502/HBASE_UPSTREAM_ERROR)，
     limit<=0→400，happybase 缺席→503/HBASE_DRIVER_MISSING；
  ③ 连接池经注入（鸭子类型），无 happybase、无容器全路径可测（M2-3 接电同口径）。

被测对象 service/app/hbase_executor.py 只依赖标准库（happybase 延迟导入）。
"""

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import hbase_executor as hbx  # noqa: E402


# ======================================================================
# TASK-M2-3 接电：hbase 数据源服务执行器（hbase_service.execute）
# ======================================================================

import json  # noqa: E402
import yaml  # noqa: E402

from app import audit as audit_mod  # noqa: E402
from app import hbase_service as hbsvc  # noqa: E402
from app import kv_executor as kvx  # noqa: E402

import synth_services as _syn  # noqa: E402  合成 fixture（全下线终态；hbase 型指向业务表）

SERVICE_PATH = _syn.synth_services_path()   # 合成注册表（线上真源已清空 services: []）
SERVICES = {s["service_id"]: s for s in
            yaml.safe_load(SERVICE_PATH.read_text(encoding="utf-8"))["services"]}
HBASE_SERVICE = SERVICES["usr_basic_info_query"]   # 业务表 default:usr_basic_info（demo 表已删）


class FakeConnection:
    """假连接：happybase 真实 API 形状——Connection.tables()/table(name)，
    行级 row()/scan() 在 Table 对象上（M2-1 假 API 自证通过、M2-3 e2e 真容器暴露）。"""

    def __init__(self, tables=None, rows=None, scan_rows=None, fail_row=False):
        self._tables = tables or []
        self.rows = rows or {}
        self.scan_rows = scan_rows or []
        self.fail_row = fail_row
        self.calls = []
        self._table = FakeTable(self)

    def tables(self):
        return self._tables

    def table(self, name):
        self.calls.append(("table", name))
        return self._table


class FakeTable:
    """假 Table：happybase Table.row()/scan() 形状（调用参数记录在 connection.calls）。"""

    def __init__(self, conn):
        self._conn = conn

    def row(self, rowkey, columns=None):
        c = self._conn
        c.calls.append(("row", rowkey, columns))
        if c.fail_row:
            raise RuntimeError("boom: thrift 上游炸了")
        return c.rows.get(rowkey, {})

    def scan(self, **kwargs):
        c = self._conn
        c.calls.append(("scan", kwargs))
        return c.scan_rows[: kwargs.get("limit", len(c.scan_rows))]


class FakePool:
    """假连接池：connection() 上下文管理器口径（与 happybase.ConnectionPool 同形）。"""

    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def connection(self):
        self.acquired += 1
        return _Ctx(self.conn)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


# ---------- get 雏形 ----------

def test_get_roundtrip_bytes_to_str():
    """put/get 往返口径：bytes 进 str 出（KV 执行器同口径：值即 str）。"""
    conn = FakeConnection(rows={b"row-1": {b"cf:tag_v1": b"v1-probe", b"cf:tag_v297": b"v297-probe"}})
    client = hbx.HBaseClient(FakePool(conn))
    out = client.get("t", "row-1")
    assert out == {"cf:tag_v1": "v1-probe", b"cf:tag_v297".decode(): "v297-probe"}


def test_get_columns_passthrough():
    """columns 白名单原样传给连接层；str 行键边界编码为 bytes（Thrift 层口径）。"""
    conn = FakeConnection(rows={b"r": {b"cf:a": b"1"}})
    client = hbx.HBaseClient(FakePool(conn))
    client.get("t", "r", columns=["cf:a"])
    assert conn.calls[-2] == ("table", "t")            # 先取 Table（真实 API 形状）
    assert conn.calls[-1] == ("row", b"r", ["cf:a"])   # 行级调用在 Table 上


def test_get_missing_row_empty_dict():
    """行不存在＝空 dict（端点层决定 404 语义，连接层不擅自抛 404）。"""
    client = hbx.HBaseClient(FakePool(FakeConnection()))
    assert client.get("t", "nope") == {}


def test_get_upstream_error_wraps_502():
    """上游异常→可控错误 502/HBASE_UPSTREAM_ERROR（端点层直接映射响应）。"""
    conn = FakeConnection(fail_row=True)
    client = hbx.HBaseClient(FakePool(conn))
    try:
        client.get("t", "r")
        assert False, "须抛 HBaseQueryError"
    except hbx.HBaseQueryError as exc:
        assert exc.status == 502 and exc.code == "HBASE_UPSTREAM_ERROR"


# ---------- scan 雏形 ----------

def test_scan_limit_cap_max_limit():
    """limit 封顶：请求超 max_limit 被硬闸拦下（防拖库第二道闸）。"""
    scan_rows = [(f"row-{i}".encode(), {b"cf:v": b"x"}) for i in range(10)]
    conn = FakeConnection(scan_rows=scan_rows)
    client = hbx.HBaseClient(FakePool(conn), max_limit=5)
    out = client.scan("t", limit=999)
    assert len(out) == 5
    assert conn.calls[-1][1]["limit"] == 5
    assert out[0]["row"] == "row-0" and out[0]["columns"] == {"cf:v": "x"}


def test_scan_default_limit_and_row_prefix():
    """默认 limit＋row_prefix 传递口径（前缀同样边界编码为 bytes）。"""
    conn = FakeConnection(scan_rows=[(b"row-a", {b"cf:v": b"1"})])
    client = hbx.HBaseClient(FakePool(conn), default_limit=3)
    client.scan("t", row_prefix="row-")
    kwargs = conn.calls[-1][1]
    assert kwargs == {"limit": 3, "row_prefix": b"row-"}


def test_scan_bad_limit_400():
    """limit<=0→400/HBASE_BAD_LIMIT（入参校验前置，不打到上游）。"""
    client = hbx.HBaseClient(FakePool(FakeConnection()))
    try:
        client.scan("t", limit=0)
        assert False, "须抛 HBaseQueryError"
    except hbx.HBaseQueryError as exc:
        assert exc.status == 400 and exc.code == "HBASE_BAD_LIMIT"


# ---------- 异步桥 ----------

def test_run_get_bridge_with_timeout():
    """run_get：to_thread 桥＋asyncio.wait_for 超时（端点层直接 await 口径）。"""
    conn = FakeConnection(rows={b"r": {b"cf:v": b"ok"}})
    client = hbx.HBaseClient(FakePool(conn), query_timeout_s=2.0)
    out = asyncio.run(hbx.run_get(client, "t", "r"))
    assert out == {"cf:v": "ok"}


def test_run_get_timeout_raises():
    """阻塞调用超时→asyncio.TimeoutError（网关 408/504 语义留给端点层）。"""
    class SlowTable(FakeTable):
        def row(self, *a, **k):
            import time
            time.sleep(0.3)
            return {}

    class SlowConn(FakeConnection):
        def table(self, name):
            super().table(name)
            return SlowTable(self)

    client = hbx.HBaseClient(FakePool(SlowConn()), query_timeout_s=0.05)
    try:
        asyncio.run(hbx.run_get(client, "t", "r"))
        assert False, "须抛 TimeoutError"
    except asyncio.TimeoutError:
        pass


# ---------- 健康检查与池工厂 ----------

def test_health_ok_and_fail():
    """health：list_tables 通→ok:True；池炸→ok:False（探针/端点共用口径）。"""
    conn = FakeConnection(tables=[b"t1", b"t2"])
    ok_res = hbx.HBaseClient(FakePool(conn)).health()
    assert ok_res["ok"] is True and ok_res["tables"] == 2 and "latency_ms" in ok_res

    class DeadPool:
        def connection(self):
            raise RuntimeError("conn refused")

    bad_res = hbx.HBaseClient(DeadPool()).health()
    assert bad_res["ok"] is False and "error" in bad_res


def test_make_pool_driver_missing_503():
    """happybase 缺席→503/HBASE_DRIVER_MISSING（CI/无容器环境不误报上游错误）。"""

    def fake_import(name, *a, **k):
        if name == "happybase":
            raise ImportError("No module named 'happybase'")
        return _real_import(name, *a, **k)

    import builtins
    _real_import = builtins.__import__
    saved = builtins.__import__
    builtins.__import__ = fake_import
    try:
        hbx.make_pool()
        assert False, "须抛 HBaseQueryError(503)"
    except hbx.HBaseQueryError as exc:
        assert exc.status == 503 and exc.code == "HBASE_DRIVER_MISSING"
    finally:
        builtins.__import__ = saved

# ---------- AC-1/AC-2/AC-14：hbase 数据源服务执行器（hbase_service.execute） ----------

def _grant(fields):
    """授权条目（fields ⊆ 合成矩阵 usr_basic_info_query 白名单：usr_basic_info 12 列字典）。"""
    return {"tenant": "demo_merchant", "oa_ticket": "OA-PENDING-TASKM23", "fields": set(fields)}


class FakeHBaseClient:
    """假 HBase 客户端：预置整行数据（含未授权列），记录 columns 传递口径。"""

    def __init__(self, row=None, fail=False):
        self.row = row if row is not None else {
            "cf:gender_cd": "1",
            "cf:age_band": "C",
            "cf:city_code": "0755",
            "cf:open_date": "2013-05-14",         # 库内存在但未授权列（投影红线）
            "cf:cust_level": "L3",
            "cf:risk_level": "2",                 # 原值 2 → band_mask 落 R12 档（≤3）
            "cf:edu_level": "4",                  # 未授权列
            "cf:occupation_cd": "21",             # 敏感未授权列（投影红线）
            "cf:marital_sts": "20",               # 敏感未授权列
            "cf:channel_cd": "APP",               # 未授权列
            "cf:vip_flag": "1",
            "cf:blacklist_flag": "0",             # 敏感未授权列
        }
        self.fail = fail
        self.calls = []

    async def run_get(self, client, table, rowkey, columns=None):
        self.calls.append(("get", table, rowkey, list(columns or [])))
        if self.fail:
            raise hbx.HBaseQueryError(502, "HBASE_UPSTREAM_ERROR", "boom")
        if rowkey != "ab" * 32:
            return {}
        return {c: self.row[c] for c in (columns or []) if c in self.row}


def _rowkey():
    return "ab" * 32          # 64 位小写十六进制（SM3 键口径）


def test_hbase_execute_found_path_with_projection_and_masking():
    """AC-1/AC-2：rowkey 取行→投影→脱敏→响应（fields ⊆ 授权白名单）。"""
    grant = _grant({"gender_cd", "risk_level", "vip_flag"})
    fake = FakeHBaseClient()
    out = asyncio.run(hbsvc.execute(fake, HBASE_SERVICE, grant, _rowkey()))
    assert out["found"] is True
    assert set(out["fields"]) <= grant["fields"], "执行结果混入未授权字段（红线）"
    assert out["fields"] == {"gender_cd": "1", "risk_level": "R12", "vip_flag": "1"}   # 2 → R12（≤3）
    assert '"risk_level": "2"' not in json.dumps(out["fields"]), "脱敏字段原值出现（红线）"
    assert out["datasource"] == "hbase"


def test_hbase_projection_red_line_zero_leak():
    """AC-2 判据：白名单外字段名零出现——即使整行里真有值（grep 机检同口径断言）。"""
    grant = _grant({"gender_cd"})                          # 白名单只授 1 列
    fake = FakeHBaseClient()
    out = asyncio.run(hbsvc.execute(fake, HBASE_SERVICE, grant, _rowkey()))
    body_text = json.dumps(out, ensure_ascii=False)
    assert out["fields"] == {"gender_cd": "1"}
    for leaked in ("risk_level", "occupation_cd", "blacklist_flag", "marital_sts",
                   "open_date", "channel_cd", "cust_level", "2013-05-14"):
        assert leaked not in body_text, f"越权字段/值泄露：{leaked}"


def test_hbase_column_level_projection_pushdown():
    """列级投影前置：Thrift 取数只取白名单列（未授权列的值不进内存，AC-2 第一道闸）。"""
    grant = _grant({"gender_cd", "risk_level"})
    fake = FakeHBaseClient()
    asyncio.run(hbsvc.execute(fake, HBASE_SERVICE, grant, _rowkey()))
    op, table, rowkey, columns = fake.calls[0]
    assert table == "default:usr_basic_info" and rowkey == "ab" * 32   # rowkey＝上游供数键
    assert set(columns) == {"cf:gender_cd", "cf:risk_level"}  # 仅白名单列下发


def test_hbase_missing_row_honest_empty():
    """行不存在→found=false＋fields=None（与 KV 通道诚实空值同构）。"""
    fake = FakeHBaseClient()
    out = asyncio.run(hbsvc.execute(fake, HBASE_SERVICE, _grant({"gender_cd"}), "cd" * 32))
    assert out["found"] is False and out["fields"] is None


def test_hbase_invalid_key_400():
    """键格式非法→400 invalid_key（沿 kv_executor.normalize_key 口径）。

    2026-09-23 维护者 口径：默认模式放宽——"not-a-key" 这类合理字符串键
    （含连字符）已放行，不再以 SM3 形态判定非法；改用真正越出 pattern
    的键（含空白）验证 400 口径。
    """
    for bad in ["", "has space", "a/b"]:
        try:
            asyncio.run(hbsvc.execute(FakeHBaseClient(), HBASE_SERVICE,
                                      _grant({"gender_cd"}), bad))
        except kvx.TagQueryError as exc:
            assert exc.status == 400 and exc.code == "invalid_key"
        else:
            raise AssertionError(f"非法键 {bad!r} 应 400")
    # 放宽口径：含连字符的合理键放行（不再被当非法）
    out = asyncio.run(hbsvc.execute(FakeHBaseClient(), HBASE_SERVICE,
                                    _grant({"gender_cd"}), "not-a-key"))
    assert out["found"] is False                     # 键合法但行不存在＝诚实空值


def test_hbase_guard_timeout_503():
    """guard 超时→503 guard_timeout（服务层口径：超时预算内必返机读错误，AC-3 前半）。"""
    import copy as _copy
    svc = _copy.deepcopy(HBASE_SERVICE)
    svc["guard"]["timeout_ms"] = 30

    class SlowClient(FakeHBaseClient):
        async def run_get(self, *a, **k):
            await asyncio.sleep(0.2)
            return {}

    try:
        asyncio.run(hbsvc.execute(SlowClient(), svc, _grant({"gender_cd"}), _rowkey()))
    except hbx.HBaseQueryError as exc:
        assert exc.status == 503 and exc.code == "guard_timeout"
    else:
        raise AssertionError("HBase 读超时应 503 guard_timeout")


def test_hbase_upstream_error_propagates_502():
    """上游异常→HBaseQueryError(502) 原样传播（端点层同 TagQueryError 口径映射）。"""
    try:
        asyncio.run(hbsvc.execute(FakeHBaseClient(fail=True), HBASE_SERVICE,
                                  _grant({"gender_cd"}), _rowkey()))
    except hbx.HBaseQueryError as exc:
        assert exc.status == 502 and exc.code == "HBASE_UPSTREAM_ERROR"
    else:
        raise AssertionError("上游异常应传播 HBaseQueryError(502)")


def test_hbase_audit_entry_shape_distinct_datasource():
    """AC-14：审计七元组结构不动＋endpoint 可区分数据源（datasource=hbase 标注）。"""
    entry = audit_mod.make_entry(caller="oneserve-dev-appkey-001",
                                 endpoint=hbsvc.AUDIT_ENDPOINT,
                                 params=json.dumps({"key": _rowkey()}),
                                 status=200, latency_ms=5,
                                 response_body=json.dumps({"found": True}),
                                 trace_id="trace-m23")
    assert hbsvc.AUDIT_ENDPOINT.startswith("service:/v1/hbase/query")
    assert "datasource=hbase" in hbsvc.AUDIT_ENDPOINT
    assert entry.endpoint == hbsvc.AUDIT_ENDPOINT and entry.status == 200


def test_hbase_service_registry_entry_shape():
    """注册条目结构：source=hbase＋backend.hbase{table,cf,key_type}＋params 键模式（B3 契约）。"""
    hb = HBASE_SERVICE["backend"]["hbase"]
    assert HBASE_SERVICE["backend"]["source"] == "hbase"
    assert hb["table"] == "default:usr_basic_info" and hb["cf"] and hb["key_type"] == "phone_sm3"
    # 2026-09-23 维护者 口径：默认模式放宽（不再把 64 位十六进制当默认口径）
    assert HBASE_SERVICE["params"][0]["pattern"] == "^[A-Za-z0-9_.-]{1,128}$"
    hbsvc.validate_backend(HBASE_SERVICE)      # 不抛＝结构合格


def test_hbase_validate_backend_rejects_bad_config():
    """坏配置显式拒（source=hbase 缺 backend.hbase / 缺必填字段 → RuntimeError）。"""
    import copy as _copy
    bad = _copy.deepcopy(HBASE_SERVICE)
    del bad["backend"]["hbase"]
    try:
        hbsvc.validate_backend(bad)
        raise AssertionError("缺 backend.hbase 应拒")
    except RuntimeError:
        pass
    bad2 = _copy.deepcopy(HBASE_SERVICE)
    del bad2["backend"]["hbase"]["cf"]
    try:
        hbsvc.validate_backend(bad2)
        raise AssertionError("缺 cf 应拒")
    except RuntimeError:
        pass


# ---------- 卡1：失败诊断三分类（classify_hbase_failure） ----------

def test_classify_unreachable_with_dev_action_guidance():
    """不可达（含未启动）→ unreachable/503/HBASE_UNAVAILABLE＋行动指引文案。"""
    d = hbx.classify_hbase_failure(RuntimeError("TSocket read 0 bytes"),
                                   endpoint="oneserve-hbase:9090")
    assert d["category"] == "unreachable" and d["code"] == "HBASE_UNAVAILABLE"
    assert d["status"] == 503
    assert "bash scripts/up_hbase.sh" in d["message"]       # 开发态行动指引
    assert "oneserve-hbase:9090" in d["message"]            # 带当前生效端点
    assert "不返回假元数据" in d["message"]                  # 诚信口径保持
    # 容器未启动（连接拒绝）同归不可达
    d2 = hbx.classify_hbase_failure(ConnectionRefusedError(61, "Connection refused"),
                                    endpoint="oneserve-hbase:9090")
    assert d2["category"] == "unreachable"
    # 超时同归不可达（含 asyncio.TimeoutError）
    d3 = hbx.classify_hbase_failure(TimeoutError(), endpoint="h:1")
    assert d3["category"] == "unreachable"
    # 本模块自包中文消息（tables/discover 的 HBASE_UNAVAILABLE 包装）同归不可达
    wrapped = hbx.HBaseQueryError(503, "HBASE_UNAVAILABLE", "HBase 不可达（list_tables 失败）: oops")
    d4 = hbx.classify_hbase_failure(wrapped, endpoint="h:1")
    assert d4["category"] == "unreachable" and d4["code"] == "HBASE_UNAVAILABLE"


def test_classify_auth_failed_and_other():
    """认证失败→auth_failed/503/HBASE_AUTH_FAILED（指向双轨配置）；其余→other/502。"""
    d = hbx.classify_hbase_failure(RuntimeError("TTransport: SASL authentication failed"))
    assert d["category"] == "auth_failed" and d["code"] == "HBASE_AUTH_FAILED"
    assert d["status"] == 503 and "connections.yaml" in d["message"]
    assert "不返回假元数据" in d["message"]
    # 其他：非网络非认证的未预期异常，如实归 other（不猜类别）
    d2 = hbx.classify_hbase_failure(ValueError("boom"))
    assert d2["category"] == "other" and d2["code"] == "HBASE_UPSTREAM_ERROR"
    assert d2["status"] == 502 and "boom" in d2["message"]
    # 已登记的非通用机读码原样保留（驱动缺失不吞成三分类码）
    exc = hbx.HBaseQueryError(503, "HBASE_DRIVER_MISSING", "happybase 未安装")
    d3 = hbx.classify_hbase_failure(exc, endpoint="h:1")
    assert d3["category"] == "other" and d3["code"] == "HBASE_DRIVER_MISSING"
    assert d3["status"] == 503

