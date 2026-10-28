#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用表级 schema 守卫（卡5 骨架 → 卡2 转真断言 · 装载与发布流程拆分设计_v1 §六）。

守卫口径（与表数量无关，新表/新服务零新增测试）：
  a) 已装载表必有 schema 元数据且字段非空（装载副产品，唯一写入路径＝write_space 尾部）；
  b) 注册服务出参字段 ⊆ 其所属表的 schema；
  c) auth_matrix 授权字段 ⊆ 对应服务所属表的 schema。

真源（卡2 落地）：Redis 键空间 sync:schema:<表>:<版本>＋sync:tables＋sync:tabver:<表>，
读写唯一实现＝sync/schema_meta.py。客户端两轨：
  - 环境变量 ONESERVE_GUARD_REDIS_URL 给出（隔离栈/演示栈真跑）→ 连真 Redis 断言；
  - 缺席（门禁环境无 Redis）→ 内存客户端，种子经**生产写入函数**（schema_meta_set＋
    tabver_index_add）从真仓表级字典 sync/schema_*.csv 构造——断言的仍是真源函数与
    真仓 schema 事实，非手拼假键。
卡5 时期的 skip 占位于此移除（AC-12：不新增 skipped）。

schema 口径：表 schema＝该表全部已装载版本字段名的**并集**（守卫目标是拦「字段不在
任何已装载 schema」的脏注册/脏授权；逐版本精确匹配归卡3 发布契约）。

运行：python3 -m pytest tests/test_table_schema_guard.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"), str(REPO / "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_meta as sm                    # noqa: E402


# ── 真源客户端（卡2 接线点：唯一需要随部署环境调整的函数） ──

def _guard_client():
    """守卫用的 Redis 客户端：env ONESERVE_GUARD_REDIS_URL＝真库；缺席＝内存种子。"""
    url = os.environ.get("ONESERVE_GUARD_REDIS_URL", "").strip()
    if url:
        import redis as redis_lib
        r = redis_lib.Redis.from_url(url, decode_responses=True)
        r.ping()
        return r, "redis:" + url
    return _seeded_memory_client(), "memory:seed-from-sync-csv"


def _seeded_memory_client():
    """内存客户端＋生产写入路径种子（真仓表级 CSV → schema 元数据＋tabver 索引）。"""
    import fnmatch

    class _Pipe:
        def __init__(self, r): self._r, self._ops = r, []

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

    class _FakeRedis:
        def __init__(self):
            self.kv, self.hashes, self.sets = {}, {}, {}

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

    r = _FakeRedis()
    seeded = 0
    for p in sorted((REPO / "sync").glob("schema_*.csv")):
        if p.name == "schema_usr_label_wide_297.csv":      # 297 宽表＝缓删旧形态，不入守卫真源
            continue
        domain = p.stem[len("schema_"):]
        observed = list(sm.dict_rows(domain))              # 真仓表级字典字段全集
        if not observed:
            continue
        fields, source = sm.build_fields(domain, "cf", observed)
        sm.schema_meta_set(r, domain, "guard-seed", fields, source=source)
        sm.tabver_index_add(r, domain, "guard-seed")
        seeded += 1
    if seeded == 0:
        raise AssertionError("守卫真源为空：sync/ 无表级 schema CSV（schema_*.csv）——"
                             "装载链路（卡2）或表级字典缺失，如实失败不静默")
    return r


def load_table_schemas(r) -> dict[str, set[str]]:
    """表级 schema 真源 → {表名: 字段名并集（全部已装载版本）}。"""
    out: dict[str, set[str]] = {}
    for t in sm.tabver_tables(r):
        names: set[str] = set()
        for v in sm.tabver_versions(r, t):
            names |= {f["name"] for f in (sm.schema_meta_get(r, t, v) or {}).get("fields", [])}
        out[t] = names
    return out


def load_loaded_tables(r) -> list[str]:
    """已装载表清单（sync:tables 索引）。"""
    return sm.tabver_tables(r)


# ── 既有真源装载（注册表/授权矩阵） ──

def _yaml():
    import yaml
    return yaml


def _services() -> list[dict]:
    doc = _yaml().safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8")) or {}
    return [s for s in (doc.get("services") or []) if isinstance(s, dict)]


def _service_id(svc: dict) -> str:
    """注册条目 → 服务 ID（route 去首斜杠、/ 换 -，与运行时口径同构）。"""
    route = str(svc.get("route") or "")
    return route.lstrip("/").replace("/", "-")


def _service_fields(svc: dict) -> list[str]:
    return [str((f or {}).get("name") or "") for f in (svc.get("fields") or [])]


def _service_table(svc: dict) -> str | None:
    """服务 → 其所属表（backend.load.table；契约随卡3 定稿校正）。"""
    backend = svc.get("backend") or {}
    load = backend.get("load") or {}
    return str(load.get("table") or "") or None


def _upstream_proxy_ids() -> set[str]:
    """upstreams.yaml 在册代理 id（ADR-0013：B 型代理单一权威源，proxy-local-* 命名空间）。

    proxy_hub 真实消费本表——授权在场即可调；代理域无表归属，字段⊆表 schema
    检查不适用（字段核验归目标服务注册条目）。
    """
    doc = _yaml().safe_load((REPO / "service" / "upstreams.yaml").read_text(encoding="utf-8")) or {}
    return {str(p["id"]) for p in (doc.get("proxies") or [])
            if isinstance(p, dict) and p.get("id")}


def _auth_grants() -> list[tuple[str, list[str]]]:
    doc = _yaml().safe_load((REPO / "service" / "auth_matrix.yaml").read_text(encoding="utf-8")) or {}
    out: list[tuple[str, list[str]]] = []
    for row in doc.get("matrix") or []:
        for g in row.get("grants") or []:
            sid = str(g.get("service") or "")
            fields = [str(f) for f in (g.get("fields") or [])]
            if sid:
                out.append((sid, fields))
    return out


# ── a) 已装载表必有 schema 且字段非空 ──

def test_a_loaded_tables_have_schema_with_fields():
    r, origin = _guard_client()
    schemas = load_table_schemas(r)
    tables = load_loaded_tables(r)
    print("  真源＝" + origin + "；表 " + str(len(tables)) + " 张：" + ", ".join(tables))
    assert tables, "已装载表清单为空（sync:tables）——真源异常或从未装载"
    missing = [t for t in tables if t not in schemas]
    assert not missing, "已装载表缺 schema 元数据：" + str(missing)
    empty = [t for t in tables if not schemas.get(t)]
    assert not empty, "已装载表 schema 字段集为空：" + str(empty)


# ── b) 注册服务出参字段 ⊆ 其表 schema ──

def test_b_service_fields_subset_of_table_schema():
    r, _origin = _guard_client()
    schemas = load_table_schemas(r)
    skipped, checked = [], 0
    for svc in _services():
        sid = _service_id(svc) or "<无 route>"
        table = _service_table(svc)
        fields = [f for f in _service_fields(svc) if f]
        if table is None:
            if fields:
                skipped.append(sid)
            continue
        assert table in schemas, f"{sid} 所属表 {table} 不在 schema 真源（先装载再发布）"
        extra = sorted(set(fields) - schemas[table])
        assert not extra, f"{sid} 出参字段越出表 {table} schema：{extra}"
        checked += 1
    print(f"  断言 b：{checked} 服务受检，{len(skipped)} 服务无表归属跳过：{skipped}")


# ── c) auth_matrix 字段 ⊆ 对应服务表 schema ──

def test_c_auth_matrix_fields_subset_of_table_schema():
    r, _origin = _guard_client()
    schemas = load_table_schemas(r)
    services = _services()
    # 2026-10-15 ADR-0013：B 型代理登记于 upstreams.yaml 且 proxy_hub 真实消费——
    # 在册判定＝services.yaml ∪ upstreams.yaml（授权在场即可调）。
    known_ids = {_service_id(s) for s in services if _service_id(s)} | _upstream_proxy_ids()

    def _table_of(sid: str) -> str | None:
        """同 id 多条目（运行时区＋console 写区并存）时任一条目的表归属即生效——
        守卫取并集口径，防写区残条目稀释断言。"""
        tables = [t for t in (_service_table(s) for s in services if _service_id(s) == sid) if t]
        return tables[0] if tables else None

    for sid, fields in _auth_grants():
        assert sid in known_ids, f"授权矩阵登记了未注册服务：{sid}"
        table = _table_of(sid)
        if table is None:
            print(f"  跳过（无表归属，契约待卡3）：{sid}")
            continue
        assert table in schemas, f"{sid} 所属表 {table} 不在 schema 真源"
        extra = sorted(set(fields) - schemas[table])
        assert not extra, f"{sid} 授权字段越出表 {table} schema：{extra}（白名单越界＝脏授权）"


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[ok] {name}")
            except Exception:
                failed += 1
                print(f"[FAIL] {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
