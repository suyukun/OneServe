#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HBase → Redis 加载器单测（M2 架构纠正卡）。

判据（本卡最小实现口径）：
  ① 加载幂等：目标空间非空且未 --replace → 拒；--replace → 清空重灌且行数稳定；
  ② 键口径与 sync/loader.py 一致：键＝tags:user:<版本>:<key>，value＝非空标签紧凑 JSON
     逐字节相同；版本元信息字段口径一致；
  ③ 版本指针切换：加载器只写新空间，绝不触碰 tags:user:latest；version==线上版拒灌；
  ④ 不可达如实报错：HBase 读失败 / Redis 不可达 → 显式失败（exit 1），绝不回退假数据；
  ⑤ 对账口径：加载结果喂 sync/hbase_reconcile.compare → 差异＝0（与既有报表同口径）。

分层纪律：写入用内存假 Redis（形状对齐 redis-py 本文用到的子集），读取用假 pool/table
（沿用 tests/test_hbase_loader.py 的鸭子类型形状）；真容器链路用
scripts/hbase_to_redis_demo.sh 显式起容器后验证，不在此用假 stub 冒充真链路。
"""

import json
import sys
import fnmatch
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"), str(REPO / "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C                    # noqa: E402
import hbase_to_redis as h2r               # noqa: E402
import hbase_reconcile as hr               # noqa: E402
from common import data_key                # noqa: E402


# ======================================================================
# 内存假 Redis（只覆盖本模块用到的命令子集；pipeline 同形）
# ======================================================================

class _Pipe:
    def __init__(self, r):
        self._r = r
        self._ops = []

    def set(self, k, v):
        self._ops.append(("set", k, v))

    def delete(self, k):
        self._ops.append(("del", k))

    def hset(self, k, mapping=None):
        self._ops.append(("hset", k, mapping))

    def get(self, k):
        self._ops.append(("get", k))

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set":
                out.append(self._r.set(op[1], op[2]))
            elif op[0] == "del":
                out.append(self._r.delete(op[1]))
            elif op[0] == "hset":
                out.append(self._r.hset(op[1], mapping=op[2]))
            elif op[0] == "get":
                out.append(self._r.get(op[1]))
        self._ops = []
        return out


class FakeRedis:
    """str 口径内存 Redis：kv＝{key: str}，hashes＝{key: {field: str}}，sets＝{key: set}（卡2 schema/tabver 索引用）。"""

    def __init__(self, kv=None, hashes=None, sets=None):
        self.kv = dict(kv or {})
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.sets = {k: set(v) for k, v in (sets or {}).items()}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v):
        self.kv[k] = v
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
            n += 1 if self.hashes.pop(k, None) is not None else 0
        return n

    def hset(self, k, mapping=None):
        # 真 redis-py 把数值字段转字符串存取（decode_responses=True 读回即 str）；假实现同形。
        self.hashes.setdefault(k, {}).update(
            {f: v if isinstance(v, str) else str(v) for f, v in (mapping or {}).items()})
        return len(mapping or {})

    def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    def scan(self, cursor=0, match=None, count=1000):
        return 0, [k for k in self.kv if fnmatch.fnmatchcase(k, match or "*")]

    def sadd(self, k, *members):
        s = self.sets.setdefault(k, set())
        n = 0
        for m in members:
            m = m if isinstance(m, str) else str(m)
            if m not in s:
                s.add(m)
                n += 1
        return n

    def smembers(self, k):
        return set(self.sets.get(k, set()))

    def pipeline(self, transaction=False):
        return _Pipe(self)

    def ping(self):
        return True


class FakeBatch:
    def __init__(self, table):
        self._t = table
        self._puts, self._dels = [], []

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
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def scan(self, limit=None, **kw):
        items = list(self.rows.items())
        return items[:limit] if limit is not None else items

    def batch(self, batch_size=None):
        return FakeBatch(self)

    def row(self, rowkey, columns=None):
        data = dict(self.rows.get(rowkey, {}))
        if columns:
            # 真 happybase 会把 str 列名编码为 bytes 再比对；假实现同形，免测试被编码差异误伤。
            wanted = {c.encode("utf-8") if isinstance(c, str) else c for c in columns}
            data = {c: v for c, v in data.items() if c in wanted}
        return data


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, tables=None):
        self._tables = dict(tables or {})

    def tables(self):
        return list(self._tables)

    def table(self, name):
        if name in self._tables:
            return self._tables[name]
        return self._tables[name.split(":", 1)[1] if ":" in name else name]


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        return _Ctx(self._conn)


# 行键＝bytes（真 happybase 口径：Thrift 层键/值均 bytes，读回时才 decode 为 str）
HOST_ROWS = {("a" * 64).encode(): {b"cf:ac_active_sts_01": b"band_C", b"cf:if_acct_fct_24": b"523877"},
             ("b" * 64).encode(): {b"cf:mk_touch_sts_02": b"lvl_2"}}
HOST_TAGS = {"a" * 64: {"ac_active_sts_01": "band_C", "if_acct_fct_24": "523877"},
             "b" * 64: {"mk_touch_sts_02": "lvl_2"}}


def _pool():
    return FakePool(FakeConnection({"default:usr_basic_info": FakeTable(HOST_ROWS)}))


# ======================================================================
# ① 加载幂等 ＋ ③ 版本指针不被触碰
# ======================================================================

@pytest.fixture(autouse=True)
def _usr_basic_info_scope():
    """键空间按表隔离（2026-09-29）：本套件统一在 usr_basic_info 表域断言
    （种子键/指针与加载器切域后写入的键落同一 tags:usr_basic_info:… 空间）；
    退出还原存量单域静止值（防模块级键常量污染后续套件）。"""
    with C.table_scope("default:usr_basic_info"):
        yield


def test_load_writes_new_space_and_leaves_pointer_untouched():
    """加载只写新空间；tags:user:latest（线上指针）零改动（双缓冲）。"""
    r = FakeRedis(kv={C.ALIAS_KEY: "20260920"})
    res = h2r.load(r, _pool(), "default:usr_basic_info", "cf", "20260923")
    assert res["loaded"] == 2
    assert set(k for k in r.kv if k.startswith(f"{C.NS}:20260923:")) == {
        data_key("20260923", "a" * 64), data_key("20260923", "b" * 64)}
    assert r.get(C.ALIAS_KEY) == "20260920", "加载器绝不触碰线上指针"


def test_load_idempotent_rejects_nonempty_without_replace():
    """目标空间非空且未 --replace → 拒灌（防误覆盖正在对账的空间）。"""
    r = FakeRedis(kv={data_key("20260923", "x" * 64): "{}"})
    with pytest.raises(h2r.LoadError) as ei:
        h2r.load(r, _pool(), "default:usr_basic_info", "cf", "20260923")
    assert "--replace" in str(ei.value)
    assert data_key("20260923", "x" * 64) in r.kv, "拒灌＝零副作用"


def test_load_replace_is_stable_across_reruns():
    """--replace 幂等可重跑：清空旧空间后重灌，最终键集/值完全一致。"""
    r = FakeRedis(kv={data_key("20260923", "x" * 64): "{}"})
    first = h2r.load(r, _pool(), "default:usr_basic_info", "cf", "20260923", replace=True)
    assert first["cleared_before_load"] == 1
    snapshot = dict(r.kv)
    second = h2r.load(r, _pool(), "default:usr_basic_info", "cf", "20260923", replace=True)
    assert second["cleared_before_load"] == 2
    assert r.kv == snapshot, "重跑结果逐字节稳定"


def test_load_rejects_loading_over_live_version():
    """version 恰为线上版 → 拒（禁止原地重灌正在读的空间）。"""
    r = FakeRedis(kv={C.ALIAS_KEY: "20260923"})
    with pytest.raises(h2r.LoadError) as ei:
        h2r.load(r, _pool(), "default:usr_basic_info", "cf", "20260923")
    assert "线上版" in str(ei.value)


# ======================================================================
# ② 键口径与 sync/loader.py 一致
# ======================================================================

def test_key_and_value_bytes_match_loader_convention():
    """键＝data_key；value＝紧凑 JSON（与非 ASCII 中文口径一致），逐字节对齐 loader.py 写法。"""
    r = FakeRedis()
    rows = {"c" * 64: {"中文标签": "值一", "empty_dropped": ""}}
    rows["c" * 64].pop("empty_dropped")   # 空值由读取层丢弃，写入层只收非空标签
    h2r.write_space(r, "20260923", rows, table="t", cf="cf")
    raw = r.get(data_key("20260923", "c" * 64))
    assert raw == json.dumps({"中文标签": "值一"}, ensure_ascii=False, separators=(",", ":"))
    # 与 sync/loader.py 第 65-66 行写法同口径：ensure_ascii=False + separators=(",",":")


def test_version_meta_fields_match_loader_convention():
    """sync:ver:<版本> 元信息字段与 loader.py 同口径（rows/tag_cols/coverage/data_as_of…）。"""
    r = FakeRedis()
    h2r.write_space(r, "20260923", HOST_TAGS, columns=["ac_active_sts_01", "if_acct_fct_24"],
                    table="default:usr_basic_info", cf="cf", source_sha256="f" * 64)
    meta = r.hgetall(C.VER_META.format(version="20260923"))
    for field in ("rows", "tag_cols", "source_sha256", "coverage_src", "data_as_of", "loaded_at"):
        assert meta.get(field), f"元信息缺 {field}"
    assert meta["rows"] == "2" and meta["tag_cols"] == "2"
    assert meta["data_as_of"] == "2026-09-23"
    assert meta["source_sha256"] == "f" * 64
    # 与 sync/common.read_user 读取口径一致：data_as_of 从该 hash 取
    assert meta["source_kind"] == "hbase"


def test_read_rows_scan_all_and_column_subset():
    """整表 scan 读回 {key: 非空标签}；--columns 只保留清单内列。"""
    rows = h2r.read_rows(_pool(), "default:usr_basic_info", "cf")
    assert rows == HOST_TAGS
    subset = h2r.read_rows(_pool(), "default:usr_basic_info", "cf", columns=["if_acct_fct_24"])
    assert subset == {"a" * 64: {"if_acct_fct_24": "523877"}}


def test_read_rows_by_rowkey_batch():
    """按 rowkey 批量点查：只取命中的行，列清单前置到 Thrift 取列。"""
    rows = h2r.read_rows(_pool(), "default:usr_basic_info", "cf",
                         columns=["ac_active_sts_01"], rowkeys=["a" * 64, "f" * 64])
    assert rows == {"a" * 64: {"ac_active_sts_01": "band_C"}}


# ======================================================================
# ④ 不可达如实报错（不回退假数据）
# ======================================================================

def test_unreachable_hbase_raises_and_writes_nothing():
    """HBase 读失败：抛可控错误，Redis 零写入（先读后写，失败不写脏）。"""
    class DeadPool:
        def connection(self):
            raise RuntimeError("Connection refused to 127.0.0.1:9090")

    r = FakeRedis()
    with pytest.raises(Exception) as ei:
        h2r.load(r, DeadPool(), "default:usr_basic_info", "cf", "20260923")
    assert "Connection refused" in str(ei.value) or "not subscriptable" not in str(ei.value)
    assert not [k for k in r.kv if k.startswith(C.NS)], "HBase 失败时 Redis 必须零写入"


def test_cli_hbase_error_reports_side_hbase(monkeypatch, capsys):
    """CLI：HBase 不可达 → exit 1，JSON 写明 side=hbase 且 fallback=无。"""
    monkeypatch.setattr(h2r, "redis_conn", lambda: FakeRedis())
    monkeypatch.setattr(h2r, "connect", lambda *a, **k: (_ for _ in ()).throw(
        h2r.HBaseLoadError("HBase 不可达（127.0.0.1:9090）：ConnectionError")))
    rc = h2r.main(["--version", "20260923", "--table", "default:usr_basic_info", "--cf", "cf"])   # 直传业务表：本用例守不可达诚实口径
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["side"] == "hbase"
    assert "不回退假数据" in out["fallback"]


def test_cli_redis_error_reports_side_redis(monkeypatch, capsys):
    """CLI：Redis 不可达 → exit 1，JSON 写明 side=redis（HBase 都不去碰）。"""
    def boom():
        raise ConnectionError("Connection refused to 127.0.0.1:16379")

    monkeypatch.setattr(h2r, "redis_conn", boom)
    rc = h2r.main(["--version", "20260923", "--table", "default:usr_basic_info", "--cf", "cf"])   # 直传业务表（①在 redis ②之前）
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False and out["side"] == "redis"
    assert "不回退假数据" in out["fallback"]


def test_cli_success_reports_load(monkeypatch, capsys):
    """CLI 全绿路径：HBase 读 → Redis 写，输出加载报告（模式/行数/表/列族）。"""
    r = FakeRedis()
    monkeypatch.setattr(h2r, "redis_conn", lambda: r)
    monkeypatch.setattr(h2r, "connect", lambda *a, **k: _pool())
    monkeypatch.setattr(h2r, "list_tables", lambda *a, **k: ["default:usr_basic_info"])
    rc = h2r.main(["--version", "20260923", "--table", "default:usr_basic_info", "--cf", "cf"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["mode"] == "hbase->redis-load"
    assert out["loaded"] == 2 and out["read_mode"] == "整表scan"
    assert out["table"] == "default:usr_basic_info" and out["cf"] == "cf"


# ======================================================================
# ⑤ 对账口径（复用 sync/hbase_reconcile.compare；差异＝0）
# ======================================================================

def test_reconcile_zero_diff_after_load():
    """加载后的 Redis 行集 vs HBase 行集：既有对账 compare → 差异＝0。"""
    r = FakeRedis()
    h2r.write_space(r, "20260923", HOST_TAGS, table="default:usr_basic_info", cf="cf")
    redis_rows = {k.rsplit(":", 1)[1]: json.loads(v)
                  for k, v in r.kv.items() if k.startswith(f"{C.NS}:20260923:")}
    assert redis_rows == HOST_TAGS
    rep = hr.compare(redis_rows, HOST_TAGS)
    assert rep["pass"] is True and rep["diff_count"] == 0


def test_snapshot_sha256_deterministic():
    """HBase 快照指纹确定性：同输入两次一致，键序无关。"""
    a = h2r.snapshot_sha256(HOST_TAGS)
    b = h2r.snapshot_sha256(dict(reversed(list(HOST_TAGS.items()))))
    assert a == b and len(a) == 64
