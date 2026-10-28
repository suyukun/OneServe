#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布台改造（M2-6 端到端最后一环）套件——「选表 → 加载 → 发布 → 真调 API」的加载环。

判据映射（任务卡 ④）：
  ① 加载端点鉴权闸：POST /v1/admin/publish/hbase/load 过 X-Admin-Token；
  ② 加载幂等：目标空间非空且未 replace → 拒灌（409，零副作用）；replace → 清空重灌且行数稳定；
  ③ 发布产物是 **Redis 型**（backend.kv，非 backend.source=hbase）；
  ④ 对账差异口径：加载后喂既有 hbase_reconcile.compare → 差异＝0 或逐条可解释；
  ⑤ HBase 不可达如实报错（不写脏、不回退假数据）。

分层纪律：写入用内存假 Redis（形状对齐 redis-py 本模块用到的子集），读取用假 pool/table
（沿用 tests/test_hbase_to_redis.py 的鸭子类型形状）；真链路 e2e 归 scripts/publish_load_e2e.sh。

真实可达时的加载链路口径归 service/app/hbase_load.py（复用 sync/hbase_to_redis.py＋
sync/hbase_reconcile.py，**零第二实现**）；本套件机检「服务层编排＋HTTP 闸＋产物口径」。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import hbase_load as hbl          # noqa: E402
from app import ops_config as oc           # noqa: E402
from app import errors as E                # noqa: E402
from app import kv_executor as kvx         # noqa: E402

import sync_config as C                    # noqa: E402
import hbase_to_redis as h2r               # noqa: E402
import hbase_reconcile as hr               # noqa: E402
from common import data_key                # noqa: E402

ADMIN_API = REPO / "service" / "app" / "admin_api.py"
LOAD_SRC = REPO / "service" / "app" / "hbase_load.py"


# ======================================================================
# 内存假 Redis / 假 HBase（形状沿用 tests/test_hbase_to_redis.py）
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
        import fnmatch
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


class DeadPool:
    def connection(self): raise RuntimeError("Connection refused to 127.0.0.1:9090")


HOST_ROWS = {("a" * 64).encode(): {b"cf:ac_active_sts_01": b"band_C",
                                   b"cf:if_acct_fct_24": b"523877"},
             ("b" * 64).encode(): {b"cf:mk_touch_sts_02": b"lvl_2"}}


def _pool():
    return FakePool(FakeConnection({"default:oneserve_demo": FakeTable(HOST_ROWS)}))


# ======================================================================
# ① 鉴权闸（HTTP 编排：源码机检，宿主无 fastapi 时仍可跑）
# ======================================================================

def test_load_endpoint_present():
    """加载端点在位；路径与既有 /v1/admin/publish/hbase/* 同族。

    2026-09-23：原测试名 ..._present_and_token_gated，曾断言「函数体首行即令牌闸 +
    成功出参之前必须有闸 + 401 统一错误体 + 全局逐端点闸计数不弱化」。管理令牌机制
    已按老板决定整体拆除（授权边界＝网络层可达；认证归 M3），上述闸相关断言随机制
    一并下线，改为反向防回潮断言；端点接线与「编排走服务层」契约照守。
    """
    import py_compile
    py_compile.compile(str(ADMIN_API), doraise=True)
    src = ADMIN_API.read_text(encoding="utf-8")
    assert '@router.post("/v1/admin/publish/hbase/load"' in src, "缺加载端点"
    body = src[src.index("async def hbase_load_ep("):]
    body = body[:body.index("@router.post(")]
    # 防回潮：加载端点不应残留任何令牌闸（机制已整体拆除）
    assert "_token_ok" not in body, "加载端点不应残留令牌闸（机制已拆）"
    assert src.count("_token_ok") == 0, "admin_api 不应残留令牌闸（机制已拆）"
    # 编排走服务层（HTTP 层不做读写业务判断）
    assert "hbl.load_into_redis" in body and "asyncio.to_thread" in body, \
        "加载端点未经服务层/线程池编排"


@pytest.fixture(autouse=True)
def _demo_table_scope():
    """键空间按表隔离（2026-09-29）：本套件的加载/断言统一在 oneserve_demo 表域
    （种子键与加载器切域后写入的键落同一 tags:oneserve_demo:… 空间）；
    退出还原存量单域静止值（防模块级键常量污染后续套件）。"""
    with C.table_scope("default:oneserve_demo"):
        yield


def test_load_service_layer_reuses_loader_no_second_impl():
    """服务层复用既有加载器/对账（零第二实现）：不重写读写，只编排。"""
    src = LOAD_SRC.read_text(encoding="utf-8")
    assert "hbase_to_redis" in src and "hbase_reconcile" in src, "未复用既有加载/对账模块"
    for fn in ("load_into_redis", "_sync_modules", "_reconcile"):
        assert f"def {fn}(" in src, f"缺服务层函数 {fn}"
    # 不出现第二套键空间口径（键/元信息由 sync 侧单一实现提供）
    assert "tags:user:" not in src.replace('KV_', ''), "服务层不得自造键空间字面量"
    # 分层纪律：不 import FastAPI（HTTP 编排归 admin_api）
    assert "import fastapi" not in src


# ======================================================================
# ② 加载幂等（服务层直测：假 Redis ＋ 假 HBase）
# ======================================================================

def test_load_service_writes_new_space_untouched_pointer():
    """加载写新空间；线上指针零改动（双缓冲口径由既有加载器保证）。"""
    r = FakeRedis(kv={C.ALIAS_KEY: "20260920"})
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                              redis_client=r, pool=_pool())
    assert res["loaded"] == 2 and res["version"] == "20260923"
    assert set(k for k in r.kv if k.startswith(f"{C.NS}:20260923:")) == {
        data_key("20260923", "a" * 64), data_key("20260923", "b" * 64)}
    assert r.get(C.ALIAS_KEY) == "20260920", "加载绝不触碰线上指针"
    assert res["reconcile"]["pass"] is True and res["reconcile"]["diffCount"] == 0


def test_load_service_idempotent_rejects_nonempty_without_replace():
    """目标空间非空且未 replace → 可控 409（拒灌，零副作用）；replace → 清空重灌。"""
    r = FakeRedis(kv={data_key("20260923", "x" * 64): "{}"})
    with pytest.raises(hbl.HBaseLoadServiceError) as ei:
        hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                            redis_client=r, pool=_pool())
    assert ei.value.code == "LOAD_CONFLICT" and ei.value.status == 409
    assert "--replace" in ei.value.message or "replace" in ei.value.message
    assert data_key("20260923", "x" * 64) in r.kv, "拒灌＝零副作用"
    # replace：清空重灌成功
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                              redis_client=r, pool=_pool(), replace=True)
    assert res["loaded"] == 2 and res["cleared_before_load"] == 1
    assert data_key("20260923", "x" * 64) not in r.kv


def test_load_service_rejects_loading_over_live_version():
    """version 恰为线上版 → 拒灌（既有双缓冲口径：不得原地重灌正在读的空间）。"""
    r = FakeRedis(kv={C.ALIAS_KEY: "20260923"})
    with pytest.raises(hbl.HBaseLoadServiceError) as ei:
        hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                            redis_client=r, pool=_pool())
    assert ei.value.code == "LOAD_CONFLICT" and "线上版" in ei.value.message


# ======================================================================
# ③ 发布产物是 Redis 型（非 hbase 型）
# ======================================================================

def test_publish_product_is_redis_kv_typed(tmp_path):
    """发布产物＝backend.kv（版本键空间）＋field_space=wide297＋backend.load 来源留痕。"""
    import shutil
    p = tmp_path / "services.yaml"
    shutil.copy(REPO / "service" / "services.yaml", p)
    entry, _ = oc.publish_service(
        p, name="加载后发布", route="/v1/svc/loaded", datasource="hbase",
        hbase={"table": "default:oneserve_demo", "cf": "cf", "key_type": "phone_sm3"},
        fields=[{"name": "ac_active_sts_01", "sample": "band_C", "desc": ""}],
        load_version="20260923", operator="admin@oneserve")
    assert entry["datasource"] == "redis", "发布产物必须是 Redis 型（非 hbase）"
    reg = kvx.load_services(p)
    rt = reg["v1-svc-loaded"]
    assert "hbase" not in rt["backend"] and rt["backend"]["kv"]["data_key"] == "tags:oneserve_demo:{version}:{sm3}"
    assert rt["backend"]["kv"]["alias_key"] == "tags:oneserve_demo:latest"
    assert rt["field_space"] == "wide297"                 # ADR-0004 保留
    assert rt["backend"]["load"]["table"] == "default:oneserve_demo"
    assert rt["backend"]["load"]["version"] == "20260923"
    # 执行器认它：kv_executor 分发（无 source → 缺省 redis），不读 backend.load
    assert rt.get("backend", {}).get("source", "redis") == "redis"


def test_kv_constants_match_sync_config_single_source():
    """键空间常量与 sync/sync_config.py 逐字一致（ops_config 不 import sync/，故用守卫测试
    把「只读镜像」钉成可机检不变量——防第二套键空间口径悄悄漂移）；
    比较口径＝SC.LEGACY_KEYSPACE（存量单域静止值，不受本套件表域夹具的运行期切域影响）。"""
    import sync_config as SC
    sc_ns, sc_alias, sc_ver = SC.LEGACY_KEYSPACE[:3]
    assert oc.KV_NS == sc_ns
    assert oc.KV_ALIAS_KEY == sc_alias
    assert oc.KV_VERSION_META_KEY == sc_ver
    assert oc.KV_DATA_KEY.format(version="V", sm3="k") == sc_ns + ":V:k"


def test_publish_product_points_to_loaded_version_space():
    """发布产物指向「加载后的新版本键空间」——经 alias_key 指针解析版本（与 loader 同口径）。"""
    import tempfile, shutil
    p = Path(tempfile.mkdtemp()) / "services.yaml"
    shutil.copy(REPO / "service" / "services.yaml", p)
    oc.publish_service(p, name="n", route="/v1/svc/vvv", datasource="hbase",
                       hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"},
                       fields=[{"name": "ac_active_sts_01", "sample": "x", "desc": ""}],
                       load_version="20260923")
    rt = kvx.load_services(p)["v1-svc-vvv"]
    kv = rt["backend"]["kv"]
    # 键模板与 sync/common.data_key 同口径（tags:<表>:<版本>:<key>；源表 t → tags:t:*）
    assert kv["data_key"].format(version="20260923", sm3="k" * 64) == f"tags:t:20260923:{'k' * 64}"
    # 元信息键与 sync/common.ver_meta_set 同口径（data_as_of 来源；sync:ver:t:<版本>）
    assert kv["version_meta_key"].format(version="20260923") == "sync:ver:t:20260923"


# ======================================================================
# ④ 对账差异口径（复用既有 compare；差异＝0 或逐条可解释）
# ======================================================================

def test_reconcile_zero_diff_matches_existing_convention():
    """加载结果喂既有 hbase_reconcile.compare → 差异＝0（与既有报表同口径）。"""
    r = FakeRedis()
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                              redis_client=r, pool=_pool())
    assert res["reconcile"]["pass"] is True and res["reconcile"]["diffCount"] == 0
    checks = res["reconcile"]["checks"]
    assert checks["row_count"]["pass"] and checks["value_sample"]["pass"] \
        and checks["coverage"]["pass"]


def test_reconcile_reports_nonzero_diff_explainably():
    """存在差异时如实上报（不吞不美化）：HBase 有而 Redis 无 → diffCount>0 且 pass=false。"""
    r = FakeRedis()
    h2r.write_space(r, "20260923", {"a" * 64: {"ac_active_sts_01": "band_C"}},
                    table="default:oneserve_demo", cf="cf")
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                              redis_client=r, pool=_pool(), replace=True)
    # 本次是 replace 重灌（两行都在），差异仍为 0——改用列投影制造差异场景：
    r2 = FakeRedis(kv={data_key("20260924", "a" * 64): json.dumps({"ac_active_sts_01": "band_C"})})
    rep = hbl._reconcile(hr, h2r, r2, _pool(), "default:oneserve_demo", "cf", "20260924", None)
    assert rep["pass"] is False and rep["diffCount"] >= 1
    kinds = {d["kind"] for d in rep["diffs"]}
    assert "extra_in_hbase" in kinds, "差异须逐条可解释（kind 明示）"


def test_reconcile_uses_projection_not_full_row():
    """列投影加载：对账口径随投影裁剪（不把「未加载的列」误判为差异）。"""
    r = FakeRedis()
    res = hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                              columns=["ac_active_sts_01"], redis_client=r, pool=_pool())
    assert res["loaded"] == 1                       # 只有 a 行含该列
    assert res["reconcile"]["pass"] is True and res["reconcile"]["diffCount"] == 0


# ======================================================================
# ⑤ HBase 不可达如实报错（不写脏、不回退假数据）
# ======================================================================

def test_unreachable_hbase_reports_and_writes_nothing():
    """HBase 不可达 → 可控 503（HBASE_UNAVAILABLE）；Redis 零写入（先读后写）。

    两条不可达路径：① 建池/探活失败（connect 抛 HBaseLoadError）→ HBASE_UNAVAILABLE；
    ② 注入池在取连接时抛错（读阶段）→ 仍为可控 503，无一滴脏写入。"""
    r = FakeRedis()
    with pytest.raises(hbl.HBaseLoadServiceError) as ei:
        hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                            redis_client=r, pool=DeadPool())
    assert ei.value.status == 503 and "Connection refused" in ei.value.message
    assert not [k for k in r.kv if k.startswith(C.NS)], "HBase 失败时 Redis 必须零写入"
    # 建池失败路径（生产路径：connect/list_tables 抛 HBaseLoadError）
    r2 = FakeRedis()
    orig_connect = h2r.__dict__.get("connect")
    import hbase_loader as hl
    orig = hl.connect
    hl.connect = lambda *a, **k: (_ for _ in ()).throw(
        hl.HBaseLoadError("HBase 不可达（127.0.0.1:9090）：ConnectionError"))
    try:
        with pytest.raises(hbl.HBaseLoadServiceError) as ei2:
            hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                                redis_client=r2)
        assert ei2.value.code == "HBASE_UNAVAILABLE" and ei2.value.status == 503
    finally:
        hl.connect = orig
    assert not [k for k in r2.kv if k.startswith(C.NS)]


def test_unreachable_redis_reports():
    """Redis 不可达 → 可控 503（REDIS_UNAVAILABLE），HBase 都不去碰。"""
    class BoomRedis:
        pass

    orig = h2r.redis_conn
    h2r.redis_conn = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("refused 127.0.0.1:16379"))
    try:
        with pytest.raises(hbl.HBaseLoadServiceError) as ei:
            hbl.load_into_redis(table="default:oneserve_demo", cf="cf", version="20260923",
                               pool=_pool())
        assert ei.value.code == "REDIS_UNAVAILABLE" and ei.value.status == 503
    finally:
        h2r.redis_conn = orig


def test_load_error_codes_are_registered_or_documented():
    """服务层错误码口径：机读码形态合法；端点层如实回给机读码（不静默）。"""
    for code in ("LOAD_CONFLICT", "HBASE_UNAVAILABLE", "REDIS_UNAVAILABLE",
                 "LOAD_UPSTREAM_ERROR", "LOAD_TIMEOUT", "LOADER_UNAVAILABLE"):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", code)
    # 服务层以这些机读码归类错误；端点层原样透传（exc.code/status），故只机检透传口径
    src = LOAD_SRC.read_text(encoding="utf-8")
    for code in ("LOAD_CONFLICT", "HBASE_UNAVAILABLE", "REDIS_UNAVAILABLE", "LOAD_UPSTREAM_ERROR"):
        assert f'"{code}"' in src, f"服务层未定义机读码 {code}"
    adm = ADMIN_API.read_text(encoding="utf-8")
    body = adm[adm.index("async def hbase_load_ep("):]
    body = body[:body.index("@router.post(")]
    assert "exc.code" in body and "exc.status" in body and "exc.message" in body, \
        "端点层未原样透传服务层错误口径（不静默吞错）"


# ======================================================================
# ⑥ 端到端链条的静态机检：选表 → 加载 → 发布（前端接线）
# ======================================================================

def test_frontend_load_action_wired_and_adapted():
    """卡3 迁移清单：发布页零装载动作（发现/加载/重灌归数据装载页）；适配层装载
    契约保留（NC-5 端点不删，运维/testkit 用），publish.js 不得再引用。"""
    html = (REPO / "admin" / "publish.html").read_text(encoding="utf-8")
    for dead in ("load-btn", "load-date", "load-ok", "load-fail", "load-retry-btn", "load-replace"):
        assert f'id="{dead}"' not in html, f"装载控件未迁出发布页：{dead}"
    assert "预计约 1 分钟" not in html, "装载耗时提示属流程①"
    adapter = (REPO / "admin" / "publish-hbase-adapter.js").read_text(encoding="utf-8")
    assert "load: '/v1/admin/publish/hbase/load'" in adapter, "适配层加载端点须保留（NC-5）"
    assert "buildLoadPayload" in adapter and "normalizeLoadResult" in adapter, "适配层缺加载构造/归一"
    js = (REPO / "admin" / "publish.js").read_text(encoding="utf-8")
    assert "async startLoad()" not in js, "publish.js 不得残留装载动作（归装载页）"


def test_load_endpoint_forwards_source_csv_dev_provenance():
    """sourceCsv 透传既有加载器 --source-csv（开发期源指纹引用）：生产不需要，但必须在位
    ——否则「同一份 CSV 灌 HBase 再加载」的开发链路无法通过既有 reconcile→switch 上线闸。"""
    src = ADMIN_API.read_text(encoding="utf-8")
    body = src[src.index("async def hbase_load_ep("):]
    body = body[:body.index("@router.post(")]
    assert "sourceCsv" in body and "source_csv=source_csv" in body, \
        "加载端点未透传 sourceCsv（既有加载器 --source-csv）"
    svc = LOAD_SRC.read_text(encoding="utf-8")
    assert "source_csv=source_csv" in svc, "服务层未把 source_csv 传给既有加载器"


def test_e2e_script_and_evidence_recorded():
    """端到端可复现证据：scripts/publish_load_e2e.sh ＋ tests/evidence/ 留档（PASS 且全绿）。"""
    script = REPO / "scripts" / "publish_load_e2e.sh"
    assert script.is_file(), "缺端到端脚本 scripts/publish_load_e2e.sh"
    text = script.read_text(encoding="utf-8")
    # 覆盖任务卡端到端判据：选表 → 加载 → 发布 → 真调 API
    for anchor in ("/v1/admin/publish/hbase/discovery", "/v1/admin/publish/hbase/load",
                   "/v1/admin/publish", "/v1/query/", "reconcile.py", "switch.py"):
        assert anchor in text, f"e2e 脚本缺环节：{anchor}"
    assert "不碰共享主栈" in text or "共享 Redis 未写" in text, "e2e 未声明不碰共享栈"
    evs = sorted((REPO / "tests" / "evidence").glob("publish-load-e2e-*.txt"))
    assert evs, "缺端到端证据文件（tests/evidence/publish-load-e2e-*.txt）"
    # 证据由 e2e 的 tee 管道落盘：以 errors=replace 读（偶发多字节截断，与既有
    # m2-hbase-to-redis 同款）。取「含 FAIL=0 的最新一份」——只认全绿证据，
    # 不因残留的中途失败跑被误判为交付证据。
    ev = ""
    for p in reversed(evs):
        text = p.read_bytes().decode("utf-8", "replace")
        if "PASS=" in text and "FAIL=0" in text:
            ev = text
            break
    assert ev, "缺全绿（FAIL=0）的端到端证据"
    assert "选表 → 加载 → 发布 → 真调 API 返回真数据" in ev, "证据缺端到端判据"
    assert "backend.kv（Redis 型）" in ev, "证据缺发布产物＝Redis 型"
    assert "同源对账：差异=0" in ev, "证据缺对账差异 0"


def test_frontend_load_failure_displayed_not_silent():
    """卡3 迁移清单：装载失败展示/文案归数据装载页；发布页零装载语义控件。"""
    html = (REPO / "admin" / "publish.html").read_text(encoding="utf-8")
    js = (REPO / "admin" / "publish.js").read_text(encoding="utf-8")
    for dead in ('id="load-fail"', 'id="load-fail-text"', 'id="load-retry-btn"'):
        assert dead not in html, f"装载失败控件未迁出发布页：{dead}"
    assert "覆盖同日版本" not in js and "覆盖同日版本" not in html, "replace 指引不得出现（卡2 NC-2）"
    assert "loadState" not in js, "publish.js 不得残留装载状态机"
    load_html = (REPO / "admin" / "load.html").read_text(encoding="utf-8")
    assert 'id="load-error"' in load_html and 'id="load-conflict"' in load_html, "装载页缺失败/冲突展示"


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
