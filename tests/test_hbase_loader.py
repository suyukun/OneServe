#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HBase 灌数/同源对账单测（TASK-M2-11.5）。

红线（门槛稿 M2-11.5 判据「同源对账差异报表，差异＝0 或逐条可解释」）：
  ① loader 幂等：目标表非空且未 --replace → 拒灌（HBaseLoadError）；--replace → 清空重灌；
  ② 同源对账口径：纯逻辑 compare 覆盖「差异＝0」与「漏行/多行/值错/覆盖率塌陷」逐条可解释；
  ③ HBase 不可达如实报错：连接层异常→HBaseLoadError（绝不回退假数据、绝不静默成功）。

分层纪律（沿 test_hbase_executor.py）：写入原语用鸭子类型假 table/pool 覆盖全路径，
单测无容器、无 happybase 也能跑；真容器链路单列 test_hbase_reachable_integration，
容器不起时 pytest.skip 并如实说明——不用假 stub 冒充真链路。
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"), str(REPO / "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hbase_loader as hl        # noqa: E402
import hbase_reconcile as hr     # noqa: E402


# ======================================================================
# 假实现：happybase Table/Batch/Connection/Pool 的鸭子类型形状
# ======================================================================

class FakeBatch:
    """假 Batch：put/delete 累积，__exit__ 时落进 table._rows（与 happybase 上下文口径同形）。"""

    def __init__(self, table):
        self._t = table
        self._puts = []
        self._dels = []

    def put(self, row, data):
        self._puts.append((row, dict(data)))

    def delete(self, row, columns=None):
        self._dels.append(row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for row in self._dels:
            self._t.rows.pop(row, None)
        for row, data in self._puts:
            self._t.rows.setdefault(row, {}).update(data)
        return False


class FakeTable:
    """假 Table：rows＝{bytes_rowkey: {bytes_col: bytes_val}}。"""

    def __init__(self, rows=None, families=("cf",)):
        self.rows = dict(rows or {})
        self._families = families

    def scan(self, limit=None, **kw):
        items = list(self.rows.items())
        if limit is not None:
            items = items[:limit]
        return items

    def batch(self, batch_size=None):
        return FakeBatch(self)

    def _column_family_names(self):
        return list(self._families)


class FakeConnection:
    def __init__(self, tables_map=None):
        self._tables = dict(tables_map or {})

    def tables(self):
        return list(self._tables)

    def create_table(self, name, families):
        self._tables[name] = FakeTable(families=tuple(families))

    def table(self, name):
        # 真 happybase 实测：conn.table("default:x") 与 conn.table("x") 都可取到同一表
        # （default 命名空间剥名）；假实现同形容忍，免测试被剥名差异误伤。
        if name in self._tables:
            return self._tables[name]
        short = name.split(":", 1)[1] if ":" in name else name
        return self._tables[short]


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _Ctx(self._conn)


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


def _sample_data():
    return {"keys": ["a" * 64, "b" * 64],
            "rows": [{"ac_active_sts_01": "band_C", "if_acct_fct_24": "523877"},
                     {"mk_touch_sts_02": "lvl_2"}],
            "tag_cols": ["ac_active_sts_01", "if_acct_fct_24", "mk_touch_sts_02"],
            "nonempty_cells": 3, "sha256": "0" * 64}


# ======================================================================
# ① loader 幂等
# ======================================================================

def test_loader_creates_table_and_writes_rows():
    """首次装载：建表＋写行，rowkey＝SM3，列＝cf:<标签名>（bytes 口径）。"""
    conn = FakeConnection()
    res = hl.load(FakePool(conn), "default:usr_basic_info", "cf", _sample_data())
    assert res["created"] is True and res["written"] == 2 and res["rows_in_table"] == 2
    t = conn.table("default:usr_basic_info")
    rowkey = ("a" * 64).encode("utf-8")
    assert t.rows[rowkey][b"cf:ac_active_sts_01"] == b"band_C"
    assert b"cf:if_acct_fct_24" in t.rows[rowkey]


def test_loader_idempotent_rejects_nonempty_without_replace():
    """幂等闸：目标表非空且未 --replace → 拒灌（防误覆盖），异常可读。"""
    conn = FakeConnection({"default:usr_basic_info": FakeTable(rows={b"x": {b"cf:a": b"1"}})})
    with pytest.raises(hl.HBaseLoadError) as ei:
        hl.load(FakePool(conn), "default:usr_basic_info", "cf", _sample_data())
    assert "--replace" in str(ei.value)
    # 原行未被触碰（拒灌＝零副作用）
    assert b"x" in conn.table("default:usr_basic_info").rows


def test_loader_replace_clears_then_writes():
    """幂等可重跑：--replace 先清空旧行再灌，最终行数＝源行数（旧残留不叠加）。"""
    old = FakeTable(rows={b"old1": {b"cf:a": b"1"}, b"old2": {b"cf:a": b"2"}})
    conn = FakeConnection({"default:usr_basic_info": old})
    res = hl.load(FakePool(conn), "default:usr_basic_info", "cf", _sample_data(), replace=True)
    assert res["cleared"] == 2 and res["written"] == 2
    assert set(old.rows) == {b"a" * 64, b"b" * 64}, "旧行未被清干净"


def test_loader_repeat_run_replace_is_stable():
    """同源重复装载（--replace）结果稳定：两跑后行集完全相同（幂等）。"""
    conn = FakeConnection()
    pool = FakePool(conn)
    hl.load(pool, "default:usr_basic_info", "cf", _sample_data(), replace=True)
    first = dict(conn.table("default:usr_basic_info").rows)
    hl.load(pool, "default:usr_basic_info", "cf", _sample_data(), replace=True)
    assert conn.table("default:usr_basic_info").rows == first


def test_loader_table_exists_tolerates_namespace_stripping():
    """真容器实测口径：tables() 对 default 命名空间剥名（"usr_basic_info"），
    等值比较会把已建表误判不存在→重跑 AlreadyExists。_table_exists 须两种写法都认。"""
    conn = FakeConnection({"usr_basic_info": FakeTable()})   # 剥名形态
    assert hl._table_exists(conn, "default:usr_basic_info") is True
    conn2 = FakeConnection({"default:usr_basic_info": FakeTable()})   # 全名形态
    assert hl._table_exists(conn2, "default:usr_basic_info") is True
    assert hl._table_exists(FakeConnection(), "default:usr_basic_info") is False


def test_loader_rerun_without_replace_on_existing_table_rejects():
    """重跑幂等：表已存在（剥名形态）且未 --replace → 拒灌，而非 create_table AlreadyExists。"""
    conn = FakeConnection({"usr_basic_info": FakeTable(rows={b"x": {b"cf:a": b"1"}})})
    with pytest.raises(hl.HBaseLoadError) as ei:
        hl.load(FakePool(conn), "default:usr_basic_info", "cf", _sample_data())
    assert "--replace" in str(ei.value)


def test_loader_resolve_backend_from_services_yaml():
    """表/列族只认注册表单一真源（零硬编码）：合成 usr_basic_info_query → 业务表＋cf。"""
    import synth_services as _syn  # 合成注册表（全下线终态；线上真源为空表）
    table, cf = hl.resolve_backend("usr_basic_info_query", services=_syn.load_synth_services())
    assert table == "default:usr_basic_info" and cf == "cf"


def test_loader_read_csv_rejects_bad_key(tmp_path):
    """坏键显式拒（非 64 位 SM3）：不静默跳过，不写半截。"""
    bad = tmp_path / "bad.csv"
    bad.write_text("usr_key_sm3,tag_a\nshortkey,1\n", encoding="utf-8")
    with pytest.raises(hl.HBaseLoadError):
        hl.read_csv(bad)


# ======================================================================
# ③ HBase 不可达如实报错（不回退假数据）
# ======================================================================

def test_loader_unreachable_raises_not_silent():
    """不可达：连接层抛异常 → HBaseLoadError（绝不静默成功、绝不回退假数据）。"""

    class DeadPool:
        def connection(self):
            raise RuntimeError("Connection refused to 127.0.0.1:9090")

    with pytest.raises(hl.HBaseLoadError) as ei:
        hl.list_tables(DeadPool(), "127.0.0.1", 9090)
    assert "不可达" in str(ei.value)


def test_loader_connect_wraps_raw_pool_exception(monkeypatch):
    """真容器实测口径：happybase.ConnectionPool 构造期即时连首个连接，HBase 不可达时
    抛原始 TTransportException（非 HBaseQueryError）——connect 必须一并转 HBaseLoadError。"""
    class _TTransportException(Exception):
        pass

    def boom(*a, **k):
        raise _TTransportException("TTransportException(type=1, message=\"Could not connect\")")

    monkeypatch.setattr(hl, "make_pool", boom)
    with pytest.raises(hl.HBaseLoadError) as ei:
        hl.connect("127.0.0.1", 9090)
    assert "不可达" in str(ei.value)


def test_loader_cli_unreachable_reports_failure(tmp_path, capsys, monkeypatch):
    """CLI 不可达：退出码 1＋JSON 写明 fallback=无（不回退假数据）。"""
    csv = tmp_path / "src.csv"
    csv.write_text("usr_key_sm3,tag_a\n" + "a" * 64 + ",1\n", encoding="utf-8")
    monkeypatch.setattr(hl, "connect", lambda *a, **k: FakePool(FakeConnection()))
    monkeypatch.setattr(hl, "list_tables", lambda *a, **k: (_ for _ in ()).throw(
        hl.HBaseLoadError("HBase 不可达（127.0.0.1:9090）：ConnectionError")))
    rc = hl.main(["--csv", str(csv), "--version", "20260920",
                  "--table", "default:usr_basic_info"])   # 直传业务表：本用例守不可达诚实口径，表名解析另有专测
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and "不回退假数据" in out["fallback"]


def test_reconcile_unreachable_reports_failure(tmp_path, capsys, monkeypatch):
    """对账不可达：HBase 侧失败 → exit 1，report 写明 error 且不产假报表。"""
    monkeypatch.setattr(hr, "connect", lambda *a, **k: (_ for _ in ()).throw(
        hl.HBaseLoadError("HBase 不可达（127.0.0.1:9090）：ConnectionError")))
    monkeypatch.setattr(hr, "redis_conn", lambda *a: _StubRedis(keys=["a" * 64]))
    monkeypatch.setattr(hr, "resolve_backend", lambda *a, **k: ("default:usr_basic_info", "cf"))   # 全下线终态：真源空表，表名解析另有专测
    report = tmp_path / "recon.json"
    rc = hr.main(["--version", "20260920", "--report", str(report)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["pass"] is False and "不可达" in out["error"]
    assert json.loads(report.read_text(encoding="utf-8"))["pass"] is False


class _StubRedis:
    """极简 Redis 假实现：只支持对账用到的 scan/get/pipeline。"""

    def __init__(self, keys):
        self._kv = {f"tags:user:20260920:{k}": json.dumps({"ac_active_sts_01": "band_C"}) for k in keys}

    def scan(self, cursor=0, match=None, count=1000):
        import fnmatch
        keys = [k for k in self._kv if fnmatch.fnmatchcase(k, match or "*")]
        return 0, keys

    def pipeline(self, transaction=False):
        return _StubPipe(self._kv)


class _StubPipe:
    def __init__(self, kv):
        self._kv = kv
        self._ops = []

    def get(self, k):
        self._ops.append(k)

    def execute(self):
        return [self._kv.get(k) for k in self._ops]


# ======================================================================
# ② 同源对账口径（纯逻辑 compare；差异＝0 或逐条可解释）
# ======================================================================

def test_compare_zero_diff_pass():
    """差异＝0：两通道行集/值/覆盖率全等 → pass=True、diff_count=0。"""
    rows = {"a" * 64: {"ac_active_sts_01": "band_C", "if_acct_fct_24": "523877"},
            "b" * 64: {"mk_touch_sts_02": "lvl_2"}}
    rep = hr.compare(dict(rows), dict(rows))
    assert rep["pass"] is True and rep["diff_count"] == 0
    assert all(c["pass"] for c in rep["checks"].values())


def test_compare_missing_in_hbase_is_explained():
    """漏灌 HBase：only_redis 命中 → 行数红＋差异逐条 kind=missing_in_hbase。"""
    redis_rows = {"a" * 64: {"t": "1"}, "b" * 64: {"t": "2"}}
    hbase_rows = {"a" * 64: {"t": "1"}}
    rep = hr.compare(redis_rows, hbase_rows)
    assert rep["pass"] is False
    assert rep["checks"]["row_count"]["rows_redis"] == 2
    assert rep["checks"]["row_count"]["rows_hbase"] == 1
    assert [d["kind"] for d in rep["diffs"]] == ["missing_in_hbase"]


def test_compare_extra_in_hbase_is_explained():
    """多灌/残留：only_hbase 命中 → orphan_hbase 红，逐条 kind=extra_in_hbase。"""
    redis_rows = {"a" * 64: {"t": "1"}}
    hbase_rows = {"a" * 64: {"t": "1"}, "c" * 64: {"t": "9"}}
    rep = hr.compare(redis_rows, hbase_rows)
    assert rep["pass"] is False and rep["checks"]["orphan_hbase"]["only_hbase"] == 1
    assert any(d["kind"] == "extra_in_hbase" for d in rep["diffs"])


def test_compare_value_mismatch_sample_explained():
    """值错：抽样逐值红，差异给出 field/redis/hbase 原值（逐条可解释）。"""
    redis_rows = {"a" * 64: {"if_acct_fct_24": "523877"}}
    hbase_rows = {"a" * 64: {"if_acct_fct_24": "999"}}
    rep = hr.compare(redis_rows, hbase_rows, sample_size=10)
    assert rep["checks"]["value_sample"]["mismatch"] == 1
    d = [x for x in rep["diffs"] if x["kind"] == "value_mismatch"][0]
    assert d["detail"] == {"field": "if_acct_fct_24", "redis": "523877", "hbase": "999"}


def test_compare_coverage_collapse_detected():
    """覆盖率塌陷：HBase 侧系统性丢列 → coverage 红（行数/抽样可仍绿＝独立见红）。"""
    redis_rows = {f"{i:064d}": {f"c{j}": "1" for j in range(10)} for i in range(50)}
    hbase_rows = {k: {"c0": "1"} for k in redis_rows}      # 每行只剩 1 列
    rep = hr.compare(redis_rows, hbase_rows)
    assert rep["checks"]["row_count"]["pass"] is True
    assert rep["checks"]["coverage"]["pass"] is False
    assert rep["checks"]["coverage"]["deviation"] > rep["checks"]["coverage"]["threshold"]


def test_compare_deterministic_same_seed():
    """抽样确定性：同输入同种子两跑差异报表逐字节一致（可复跑复算）。"""
    redis_rows = {f"{i:064d}": {"t": str(i)} for i in range(100)}
    hbase_rows = dict(redis_rows)
    hbase_rows[list(redis_rows)[0]] = {"t": "DIFFERENT"}
    a = hr.compare(redis_rows, hbase_rows, sample_size=50, seed=42)
    b = hr.compare(redis_rows, hbase_rows, sample_size=50, seed=42)
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
           json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_hbase_row_to_tags_filters_other_family():
    """反序列化口径：只取本列族列，非本列族显式丢弃（与 hbase_service.row_to_fields 同源）。"""
    row = {"cf:a": "1", "other:b": "2", "cf:": "3"}
    assert hr.hbase_row_to_tags(row, "cf") == {"a": "1"}


# ======================================================================
# 真容器链路（容器不起 → pytest.skip 并如实说明，不用假 stub 冒充）
# ======================================================================

def _hbase_reachable():
    """探活本机 HBase 替身（HBASE_HOST/HBASE_PORT，缺省 127.0.0.1:9090）。"""
    import os
    import socket
    host = os.environ.get("HBASE_HOST", "127.0.0.1")
    port = int(os.environ.get("HBASE_PORT", 9090))
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _hbase_is_expendable():
    """本机 HBase 是否可随便清空（本用例会 replace 全表，必须显式确认）。

    【2026-09-23 事故与修复】原用例的跳过条件只有「HBase 可达吗」——只要替身容器
    在场就真跑，且体内是 hl.load(..., replace=True)：先清空目标表再灌一行夹具。
    这在「替身默认不起」的前提下无害，但当天 HBase 替身一直在跑（维护者 用系统讲
    架构），于是 bash scripts/run_gates.sh（动态枚举全部 tests/）一跑，就把真表
    default:usr_basic_info 里 200,000 行演示数据清空成 1 行夹具。
    该缺陷极隐蔽：用例本身「诚实」（不用假 stub、真跑真链路），恰恰因此更危险。

    修复：破坏性集成用例必须显式 opt-in —— 要求环境变量
    ONESERVE_ALLOW_DESTRUCTIVE_HBASE_TESTS=1 才执行。默认 skip，既保住
    「真链路可验证」，又杜绝「跑个测试把生产数据清了」。
    """
    import os
    return os.environ.get("ONESERVE_ALLOW_DESTRUCTIVE_HBASE_TESTS") == "1"


@pytest.mark.skipif(not (_hbase_reachable() and _hbase_is_expendable()),
                    reason="破坏性集成用例：需 HBase 替身在场 且 显式设 "
                           "ONESERVE_ALLOW_DESTRUCTIVE_HBASE_TESTS=1（本用例会 replace "
                           "全表；默认跳过，防清空真实演示数据。2026-09-23 事故后加）")
def test_hbase_reachable_integration_roundtrip(tmp_path):
    """真容器在场且显式放行时：装载→读回→对账差异＝0（最小真链路冒烟）。

    警告：本用例 replace=True 会清空目标表。仅在可牺牲的替身上跑，且必须显式设
    ONESERVE_ALLOW_DESTRUCTIVE_HBASE_TESTS=1。
    """
    import os
    host = os.environ.get("HBASE_HOST", "127.0.0.1")
    port = int(os.environ.get("HBASE_PORT", 9090))
    table, cf = "default:loader_roundtrip_smoke", "cf"   # 独立可牺牲冒烟表（oneserve_demo 已删，2026-10-08）
    data = {"keys": ["a" * 64], "rows": [{"ac_active_sts_01": "band_C"}],
            "tag_cols": ["ac_active_sts_01"], "nonempty_cells": 1, "sha256": "0" * 64}
    pool = hl.connect(host, port)
    hl.load(pool, table, cf, data, replace=True)
    got = hr.read_hbase_rows(pool, table, cf)
    assert got.get("a" * 64, {}).get("ac_active_sts_01") == "band_C"
    rep = hr.compare({"a" * 64: {"ac_active_sts_01": "band_C"}}, got)
    assert rep["pass"] is True and rep["diff_count"] == 0


def test_hbase_row_to_tags_uses_online_serializer():
    """单一事实来源：反序列化直接调 hbase_service.row_to_fields（不复制第二实现）。"""
    from app import hbase_service
    row = {"cf:x": "1"}
    assert hr.hbase_row_to_tags(row, "cf") == hbase_service.row_to_fields(row, "cf")
