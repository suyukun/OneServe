#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 回归：HTTP 加载路径必须把对账结果写进 sync:recon:<版本>（与命令行口径逐字段一致）。

缺陷（docs/plans/遗留问题清单_20260923.md 问题 4）：发布台「加载到 Redis」端点
（admin_api.hbase_load_ep → app/hbase_load.load_into_redis → _reconcile）跑了对账
却没写 Redis——sync/switch.py 上线第二闸要求 sync:recon:<版本>.pass == 1，于是
永远判「未对账」拒切，版本切不过去。

命令行侧唯一写入方＝sync/reconcile.py 的 save_and_exit（grep 实证：hbase_to_redis.py
只写 sync:ver，不写 recon）。序列化口径已收口到 sync/common.recon_meta_set
（pass/checked_at/detail），命令行与 HTTP 两路共用同一实现，零第二份字段拼接。

判据：
  ① HTTP 路径加载后 sync:recon:<版本> 存在，字段名与命令行写的完全一致；
  ② 两路 pass 值形态一致（"1"/"0" 字符串）、checked_at 同为 ISO 时刻、detail 同为 JSON；
  ③ 对账红（有差异）也如实写 pass=0（switch 照样拒切——不吞不美化）；
  ④ 单一实现守卫：两路都必须经 common.recon_meta_set；switch 第二闸原样未动。

分层纪律：内存假 Redis/假 HBase（形状沿用 tests/test_publish_load.py）；不依赖
真 Redis/真 HBase（沿仓库既有手写替身惯例，未引入 fakeredis 依赖）。
"""
from __future__ import annotations

import fnmatch
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import hbase_load as hbl          # noqa: E402

import sync_config as C                    # noqa: E402
import hbase_to_redis as h2r               # noqa: E402

ADMIN_API = REPO / "service" / "app" / "admin_api.py"
LOAD_SRC = REPO / "service" / "app" / "hbase_load.py"


# ======================================================================
# 内存假 Redis / 假 HBase（形状沿用 tests/test_publish_load.py）
# ======================================================================

class _Pipe:
    def __init__(self, r):
        self._r, self._ops = r, []

    def set(self, k, v): self._ops.append(("set", k, v))
    def delete(self, k): self._ops.append(("del", k))
    def hset(self, k, mapping=None): self._ops.append(("hset", k, mapping))
    def get(self, k): self._ops.append(("get", k))

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set": out.append(self._r.set(op[1], op[2]))
            elif op[0] == "del": out.append(self._r.delete(op[1]))
            elif op[0] == "hset": out.append(self._r.hset(op[1], mapping=op[2]))
            elif op[0] == "get": out.append(self._r.get(op[1]))
        self._ops = []
        return out


class FakeRedis:
    def __init__(self, kv=None, hashes=None, sets=None):
        self.kv = dict(kv or {})
        self.hashes = {k: dict(v) for k, v in (hashes or {}).items()}
        self.sets = {k: set(v) for k, v in (sets or {}).items()}   # 卡2 schema/tabver 索引

    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v; return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
            n += 1 if self.hashes.pop(k, None) is not None else 0
        return n

    def hset(self, k, mapping=None):
        self.hashes.setdefault(k, {}).update(
            {f: v if isinstance(v, str) else str(v) for f, v in (mapping or {}).items()})
        return len(mapping or {})

    def hgetall(self, k): return dict(self.hashes.get(k, {}))

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

    def smembers(self, k): return set(self.sets.get(k, set()))

    def pipeline(self, transaction=False): return _Pipe(self)
    def ping(self): return True


class FakeTable:
    def __init__(self, rows=None): self.rows = dict(rows or {})
    def scan(self, limit=None, **kw):
        items = list(self.rows.items())
        return items[:limit] if limit is not None else items
    def row(self, rowkey, columns=None):
        data = dict(self.rows.get(rowkey, {}))
        if columns:
            wanted = {c.encode("utf-8") if isinstance(c, str) else c for c in columns}
            data = {c: v for c, v in data.items() if c in wanted}
        return data


class _Ctx:
    def __init__(self, conn): self._conn = conn
    def __enter__(self): return self._conn
    def __exit__(self, *exc): return False


class FakeConnection:
    def __init__(self, tables=None): self._tables = dict(tables or {})
    def tables(self): return list(self._tables)
    def table(self, name):
        if name in self._tables: return self._tables[name]
        return self._tables[name.split(":", 1)[1] if ":" in name else name]


class FakePool:
    def __init__(self, conn): self._conn = conn
    def connection(self): return _Ctx(self._conn)


HOST_ROWS = {("a" * 64).encode(): {b"cf:ac_active_sts_01": b"band_C",
                                   b"cf:if_acct_fct_24": b"523877"},
             ("b" * 64).encode(): {b"cf:mk_touch_sts_02": b"lvl_2"}}


def _pool():
    return FakePool(FakeConnection({"default:oneserve_demo": FakeTable(HOST_ROWS)}))


def _pool3():
    """三行表：rowkey 子集加载后 HBase 侧多出的行＝对账红（extra_in_hbase）。"""
    rows = dict(HOST_ROWS)
    rows[("c" * 64).encode()] = {b"cf:ac_active_sts_01": b"band_A"}
    return FakePool(FakeConnection({"default:oneserve_demo": FakeTable(rows)}))


# 命令行对账源 CSV（与假 HBase 同源；首列＝键列，空串＝无标签）
_CLI_CSV = ("usr_key_sm3,ac_active_sts_01,if_acct_fct_24,mk_touch_sts_02\n"
            + "a" * 64 + ",band_C,523877,\n"
            + "b" * 64 + ",,,lvl_2\n")


# ======================================================================
# ①② 新旧两路写出同一个键、同一种字段结构（逐字段一致）
# ======================================================================

def test_http_load_writes_recon_meta_field_by_field_like_cli(tmp_path, monkeypatch):
    """HTTP 端点加载流程（load_into_redis，端点仅线程池桥接）后：sync:recon:<版本>
    存在，且与命令行入口（sync/reconcile.py）写出的键/字段结构逐字段一致。"""
    # —— HTTP 路径：发布台「加载到 Redis」的业务核心（admin_api.hbase_load_ep 经
    #    asyncio.to_thread 调本函数；HTTP 编排已由 test_publish_load 机检）——
    r_http = FakeRedis()
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260924",
                              redis_client=r_http, pool=_pool())
    assert res["reconcile"]["pass"] is True and res["reconcile"]["diffCount"] == 0
    http_key = f"sync:recon:oneserve_demo:{'20260924'}"   # 按表键空间（表域＝oneserve_demo）
    http_recon = r_http.hgetall(http_key)
    assert http_recon, "P0-2 回归失败：HTTP 加载后 sync:recon:<版本> 未写入（switch 将永远拒切）"

    # —— 命令行路径：sync/reconcile.py（全仓唯一既有写入方，口径基准）——
    csv_path = tmp_path / "src_p02.csv"
    csv_path.write_text(_CLI_CSV, encoding="utf-8")
    r_cli = FakeRedis()
    # h2r.load 现按表隔离（写 tags:oneserve_demo:…/sync:ver:oneserve_demo:…），与本 CLI
    # 基准（存量单域 tags:user:…）不同键空间——基准侧按 loader.py 同款口径直接在存量域
    # 灌数（元信息＋非空标签数据键），reconcile.py 前置查/三查即可全绿：
    from common import data_key, sha256_file, ver_meta_set
    ver_meta_set(r_cli, "20260925", rows=2, tag_cols=3, source=str(csv_path),
                 source_sha256=sha256_file(str(csv_path)),
                 coverage_src="0.500000", data_as_of="2026-09-25")
    for k, tags in {"a" * 64: {"ac_active_sts_01": "band_C", "if_acct_fct_24": "523877"},
                    "b" * 64: {"mk_touch_sts_02": "lvl_2"}}.items():
        r_cli.set(data_key("20260925", k),
                  json.dumps(tags, ensure_ascii=False, separators=(",", ":")))
    import reconcile as rec_cli
    monkeypatch.setattr(rec_cli, "redis_conn", lambda: r_cli)
    monkeypatch.setattr(sys, "argv",
                        ["reconcile.py", "--csv", str(csv_path), "--version", "20260925"])
    assert rec_cli.main() == 0, "命令行对账应绿（夹具与加载同源）"
    cli_key = C.RECON_META.format(version="20260925")
    cli_recon = r_cli.hgetall(cli_key)
    assert cli_recon, "命令行口径基准失效（夹具问题，非被测代码）"

    # —— 字段结构逐字段一致（键规则：HTTP 路按表隔离 sync:recon:<表>:<版本>；CLI 路存量单域）——
    assert http_key == "sync:recon:oneserve_demo:20260924" and cli_key == "sync:recon:20260925"
    assert set(http_recon) == set(cli_recon) == {"pass", "checked_at", "detail"}
    assert http_recon["pass"] == cli_recon["pass"] == "1"   # 值形态："1"/"0" 字符串
    for recon in (http_recon, cli_recon):
        datetime.fromisoformat(recon["checked_at"])            # checked_at＝ISO 时刻
        assert isinstance(json.loads(recon["detail"]), dict)   # detail＝JSON 明细
    http_detail, cli_detail = json.loads(http_recon["detail"]), json.loads(cli_recon["detail"])
    assert http_detail["pass"] is True and cli_detail["pass"] is True
    assert http_detail["version"] == "20260924" and cli_detail["version"] == "20260925"
    # 加载绝不触碰指针（双缓冲红线；recon 落键≠切流量）
    assert r_http.get(C.ALIAS_KEY) is None


# ======================================================================
# ③ 对账红也如实写（pass=0）——不吞不美化，switch 照样拒切
# ======================================================================

def test_http_load_writes_recon_red_when_diff():
    """rowkey 子集加载 → HBase 侧多行（extra_in_hbase）→ 对账红：sync:recon 仍落键
    且 pass="0"（如实红；switch 第二闸据此拒切）。"""
    r = FakeRedis()
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260926",
                              rowkeys=["a" * 64], redis_client=r, pool=_pool3())
    assert res["loaded"] == 1
    assert res["reconcile"]["pass"] is False and res["reconcile"]["diffCount"] >= 1
    recon = r.hgetall("sync:recon:oneserve_demo:20260926")   # 按表键空间（表域＝oneserve_demo）
    assert recon and recon["pass"] == "0", "红对账也必须如实落键（pass=0）"
    detail = json.loads(recon["detail"])
    assert detail["pass"] is False and detail["diffCount"] >= 1
    assert any(d.get("kind") == "extra_in_hbase" for d in detail["diffs"]), "差异须逐条可解释"


# ======================================================================
# ④ 单一实现守卫 ＋ 设计红线（switch 三闸未动）
# ======================================================================

def test_recon_serialization_single_source_and_switch_gates_untouched():
    """序列化口径只许一份：两路都经 common.recon_meta_set，不得自拼字段；
    switch.py 第二闸原样在读 pass == "1"（本修复绝不改三闸）。"""
    com = (REPO / "sync" / "common.py").read_text(encoding="utf-8")
    assert "def recon_meta_fields(" in com and "def recon_meta_set(" in com, \
        "共享序列化实现缺失（sync/common.py）"
    rec = (REPO / "sync" / "reconcile.py").read_text(encoding="utf-8")
    hbl_src = LOAD_SRC.read_text(encoding="utf-8")
    assert "recon_meta_set(" in rec, "命令行入口未走共享序列化"
    assert "recon_meta_set" in hbl_src, "HTTP 服务层未走共享序列化"
    for src, name in ((rec, "sync/reconcile.py"), (hbl_src, "service/app/hbase_load.py")):
        assert "RECON_META" not in src, f"{name} 不得自拼对账键（第二实现）"
        assert '"checked_at"' not in src, f"{name} 不得自拼字段拼接（须走 common.recon_meta_set）"
    # 端点仍经服务层桥接（HTTP 编排不改口径）
    adm = ADMIN_API.read_text(encoding="utf-8")
    assert "hbl.load_into_redis" in adm, "加载端点未走服务层"
    # 设计红线：switch 第二闸判定原样（pass == "1" 才放行）
    sw = (REPO / "sync" / "switch.py").read_text(encoding="utf-8")
    assert 'recon.get("pass") != "1"' in sw, "switch 第二闸判定被改动（设计红线）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception:   # noqa: BLE001 —— 直跑模式聚合报错
            failed += 1
            import traceback
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
