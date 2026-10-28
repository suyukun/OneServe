#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡2 套件①：表级 schema 元数据＋tabver 索引（T1/T2 · AC-6/AC-10 机检面）。

判据映射（开工门槛-卡2 §A3/§B3.2）：
  AC-6  装载成功 ⇒ sync:schema:<表>:<版本> 落盘（字段名/类型/中文注释；字段数>0），
        且经**唯一写入路径** hbase_to_redis.write_space 尾部（CLI/HTTP 同源）；
  AC-10 装载成功 ⇒ sync:tables＋sync:tabver:<表> 索引登记（跨表枚举，卡3 消费）；
  如实口径：无表级字典 CSV ⇒ type=unknown/cnName=待补（source=hbase）；
        命中 sync/schema_<表>.csv ⇒ 富化（source=hbase+dict）——不发明数据。

分层：内存假 Redis（形状沿 tests/test_publish_load.py，补 SET 族）＋真仓表级 CSV；
write_space 集成用例走真函数（FakeRedis），不 mock 被测逻辑本身。
"""
from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"), str(REPO / "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_config as C               # noqa: E402
import schema_meta as sm              # noqa: E402
import hbase_to_redis as h2r          # noqa: E402


@pytest.fixture(autouse=True)
def _restore_sync_keyspace():
    """每测前后保存/还原 sync_config 键空间 6 global，隔离 use_table 的裸切副作用。"""
    saved = (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG)
    yield
    (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG) = saved


# ── 内存假 Redis（沿 test_publish_load.FakeRedis；补 SET 族） ──

class _Pipe:
    def __init__(self, r):
        self._r, self._ops = r, []

    def set(self, k, v): self._ops.append(("set", k, v))
    def delete(self, k): self._ops.append(("del", k))
    def hset(self, k, mapping=None): self._ops.append(("hset", k, mapping))

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set": out.append(self._r.set(op[1], op[2]))
            elif op[0] == "del": out.append(self._r.delete(op[1]))
            elif op[0] == "hset": out.append(self._r.hset(op[1], mapping=op[2]))
        self._ops = []
        return out


class FakeRedis:
    def __init__(self, kv=None, hashes=None, sets=None):
        self.kv = dict(kv or {})
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.sets = {k: set(v) for k, v in (sets or {}).items()}

    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v; return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
            n += 1 if self.hashes.pop(k, None) is not None else 0
            n += 1 if self.sets.pop(k, None) is not None else 0
        return n

    def hset(self, k, mapping=None):
        self.hashes.setdefault(k, {}).update(
            {f: v if isinstance(v, str) else str(v) for f, v in (mapping or {}).items()})
        return len(mapping or {})

    def hgetall(self, k): return dict(self.hashes.get(k, {}))

    def sadd(self, k, *members):
        s = self.sets.setdefault(k, set())
        n = 0
        for m in members:
            m = m if isinstance(m, str) else str(m)
            if m not in s:
                s.add(m); n += 1
        return n

    def smembers(self, k): return set(self.sets.get(k, set()))

    def scan(self, cursor=0, match=None, count=1000):
        return 0, [k for k in self.kv if fnmatch.fnmatchcase(k, match or "*")]

    def pipeline(self, transaction=False): return _Pipe(self)
    def ping(self): return True


# ── T1：build_fields 富化口径（真仓表级 CSV） ──

def test_build_fields_enriched_from_table_dict_csv():
    """usr_basic_info 命中表级 CSV：type/cnName/desc 富化，source=hbase+dict，按名排序。"""
    fields, source = sm.build_fields("usr_basic_info", "cf",
                                     ["age_band", "gender_cd"])
    assert source == "hbase+dict"
    assert [f["name"] for f in fields] == ["age_band", "gender_cd"]   # 排序稳定
    g = fields[1]
    assert g["cf"] == "cf" and g["type"].upper().startswith("VARCHAR")
    assert g["cnName"] == "性别代码" and g["desc"]                     # 业务口径非空（真源）


def test_build_fields_honest_when_no_dict():
    """无表级 CSV 的表：type=unknown/cnName=待补/desc 空串（source=hbase，不发明）。"""
    fields, source = sm.build_fields("no_such_biz_table", "cf", ["col_a", "col_b"])
    assert source == "hbase"
    assert all(f["type"] == "unknown" and f["cnName"] == "待补" and f["desc"] == ""
               for f in fields)
    assert [f["name"] for f in fields] == ["col_a", "col_b"]


def test_build_fields_dict_partial_hit_honest():
    """CSV 命中但观测列不在 CSV：该列如实 unknown/待补；任一命中 ⇒ source=hbase+dict。"""
    fields, source = sm.build_fields("usr_basic_info", "cf",
                                     ["gender_cd", "col_not_in_dict"])
    assert source == "hbase+dict"
    by_name = {f["name"]: f for f in fields}
    assert by_name["gender_cd"]["cnName"] == "性别代码"
    assert by_name["col_not_in_dict"]["type"] == "unknown"
    assert by_name["col_not_in_dict"]["cnName"] == "待补"


def test_build_fields_namespaced_table_resolves_bare_domain():
    """namespace:table 入参＝剥前缀取裸表名定位 CSV（与 key_domain 同口径）。"""
    fields, source = sm.build_fields("default:usr_basic_info", "cf", ["gender_cd"])
    assert source == "hbase+dict" and fields[0]["name"] == "gender_cd"


# ── T1：schema/tabver 读写 roundtrip ──

def test_schema_meta_set_get_roundtrip():
    r = FakeRedis()
    fields, source = sm.build_fields("usr_basic_info", "cf", ["gender_cd"])
    info = sm.schema_meta_set(r, "usr_basic_info", "20260930", fields, source=source)
    assert info["field_count"] == 1 and info["source"] == "hbase+dict" and info["generated_at"]
    assert info["key"] == C.SCHEMA_META_KEY.format(table="usr_basic_info", version="20260930")

    got = sm.schema_meta_get(r, "usr_basic_info", "20260930")
    assert got is not None
    assert got["table"] == "usr_basic_info" and got["version"] == "20260930"
    assert got["field_count"] == 1 and got["source"] == "hbase+dict"
    assert got["fields"][0]["name"] == "gender_cd" and got["fields"][0]["cf"] == "cf"
    # 不存在＝None（端点 404 / 守卫口径），不抛
    assert sm.schema_meta_get(r, "usr_basic_info", "20991231") is None
    assert sm.schema_meta_get(r, "never_loaded", "20260930") is None


def test_tabver_index_add_and_listing():
    """SADD 双索引：sync:tables 跨表全集＋sync:tabver:<表> 版本清单（排序稳定）。"""
    r = FakeRedis()
    sm.tabver_index_add(r, "usr_basic_info", "20260929")
    sm.tabver_index_add(r, "usr_basic_info", "20260930")
    sm.tabver_index_add(r, "card_acct_summary", "20260930")
    assert sm.tabver_tables(r) == ["card_acct_summary", "usr_basic_info"]
    assert sm.tabver_versions(r, "usr_basic_info") == ["20260929", "20260930"]
    assert sm.tabver_versions(r, "default:usr_basic_info") == ["20260929", "20260930"]
    assert sm.tabver_versions(r, "never_loaded") == []


def test_tabver_keys_never_touch_latest_pointer():
    """键空间契约：索引/schema 键绝不涉及 tags:<表>:latest（指针归 switch 三闸）。"""
    r = FakeRedis()
    sm.tabver_index_add(r, "usr_basic_info", "20260930")
    sm.schema_meta_set(r, "usr_basic_info", "20260930",
                       *[sm.build_fields("usr_basic_info", "cf", ["gender_cd"])][0][:1],
                       source="hbase+dict")
    written = set(r.sets) | set(r.hashes)
    assert not any(":latest" in k for k in written), written


# ── T2：唯一写入路径（write_space 尾部）集成 ──

def test_write_space_writes_schema_and_tabver_ac6_ac10():
    """AC-6/AC-10：write_space 成功 ⇒ schema hash＋双索引齐落；响应带 schema 段。"""
    r = FakeRedis()
    rows = {
        "rk-1": {"gender_cd": "0", "age_band": "C"},
        "rk-2": {"gender_cd": "1", "age_band": "D"},
    }
    res = h2r.write_space(r, "20260930", rows, table="usr_basic_info", cf="cf")
    # 响应 schema 段（端点 AC 契约同源）
    assert res["schema"]["field_count"] == 2
    assert res["schema"]["source"] == "hbase+dict"
    # 键空间按 B3.2 契约
    sch = sm.schema_meta_get(r, "usr_basic_info", "20260930")
    assert sch["field_count"] == 2
    assert sorted(f["name"] for f in sch["fields"]) == ["age_band", "gender_cd"]
    assert all(f["type"] != "unknown" for f in sch["fields"])       # CSV 富化命中
    assert sm.tabver_tables(r) == ["usr_basic_info"]
    assert sm.tabver_versions(r, "usr_basic_info") == ["20260930"]
    # 数据键照旧（tags:<表>:<版本>:<键>），元信息照旧（sync:ver:<表>:<版本>）
    assert sorted(k for k in r.kv if k.startswith("tags:usr_basic_info:20260930:"))         == ["tags:usr_basic_info:20260930:rk-1", "tags:usr_basic_info:20260930:rk-2"]
    assert "rows" in r.hashes["sync:ver:usr_basic_info:20260930"]


def test_write_space_schema_fields_are_observed_cols_only():
    """schema 字段清单＝本次装载观测列（数据面事实）——字典有而未观测的列不入 schema。"""
    r = FakeRedis()
    rows = {"rk-1": {"gender_cd": "0"}}          # 只观测到 1 列（CSV 有 12 列）
    h2r.write_space(r, "20260930", rows, table="usr_basic_info", cf="cf")
    sch = sm.schema_meta_get(r, "usr_basic_info", "20260930")
    assert [f["name"] for f in sch["fields"]] == ["gender_cd"]


def test_write_space_rejects_then_no_schema_or_index():
    """幂等闸在 schema/索引之前：目标空间非空未 replace ⇒ LoadError，零 schema/索引写入。"""
    r = FakeRedis()
    rows = {"rk-1": {"gender_cd": "0"}}
    h2r.write_space(r, "20260930", rows, table="usr_basic_info", cf="cf")
    r2 = FakeRedis(kv=dict(r.kv), hashes={}, sets={})   # 只留数据键（模拟残留空间）
    # 把数据键放回 r2，让空间非空
    try:
        h2r.write_space(r2, "20260930", rows, table="usr_basic_info", cf="cf")
        raise AssertionError("应拒灌（目标空间非空）")
    except h2r.LoadError:
        pass
    assert sm.tabver_tables(r2) == []
    assert sm.schema_meta_get(r2, "usr_basic_info", "20260930") is None


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("[ok] " + name)
            except Exception:
                failed += 1
                print("[FAIL] " + name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
