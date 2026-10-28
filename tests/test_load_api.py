#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡2 套件②：数据装载域端点 /v1/admin/load/*（T4 · AC-2/3/4/5/8/10 机检面）。

判据映射（开工门槛-卡2 §A3）：
  AC-2  /tables 以真连接发现，链路不读 services.yaml（源码机检＋视图单测）；
  AC-3  不可达/认证失败/其他 → 卡1 三分类 {category, code, message} 原样透出；
  AC-4  POST /load 全量装载（columns 恒 None）委托 hbl.load_into_redis（零第二实现）；
  AC-5  同表同日冲突 → 409 LOAD_CONFLICT（端点信封含 table/version＋CLI 指引）；
  AC-8  请求带 columns/replace → 显式忽略并在响应注明（fullLoad/columnsIgnored）；
  AC-10 /assets 跨表枚举 tabver 清单（rows/tagCols/reconPass/schemaFieldCount/live）。

分层纪律：沿 test_admin_publish_api 口径——被测＝纯视图函数＋端点函数直调（FakeRequest），
不启 FastAPI 服务；hbl.load_into_redis 打桩捕获入参（真链路归隔离栈 e2e＋既有套件）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_fastapi_stub() -> list:
    """宿主门禁环境无 fastapi（容器内才有）——注入最小 stub 以便直测 admin_api 端点函数。

    仅本测试进程有效（run_gates 逐套件独立进程）；stub 只满足 admin_api 顶层 import
    与被测端点用到的 JSONResponse 形状（.status_code/.body）。与 test_admin_publish_api
    「不 import FastAPI」同目标（门禁零容器依赖），此处用 stub 换取端点函数直测覆盖。
    """
    if "fastapi" in sys.modules:
        return []
    import types as _types
    fastapi = _types.ModuleType("fastapi")

    class _APIRouter:
        def __init__(self, *a, **k):   # 2026-10-09 闸恢复：router 构造带 dependencies kwarg
            pass

        def get(self, *a, **k):
            return lambda fn: fn

        def post(self, *a, **k):
            return lambda fn: fn

    fastapi.APIRouter = _APIRouter
    # 2026-10-09 管理令牌闸恢复：admin_api 顶层新增 Depends/HTTPException import，
    # 桩环境同步补成员（直调端点函数不过路由层，_admin_gate 不在单测路径执行）。
    fastapi.Depends = lambda dep: dep

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=None):
            self.status_code = status_code
            self.detail = detail

    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    responses = _types.ModuleType("fastapi.responses")

    class _JSONResponse:
        def __init__(self, content, status_code=200, **k):
            self.status_code = status_code
            self.body = json.dumps(content, ensure_ascii=False).encode("utf-8")

    class _PlainTextResponse:
        def __init__(self, content, media_type=None, **k):
            self.status_code = 200
            self.body = str(content).encode("utf-8")
            self.media_type = media_type   # 2026-10-09 与 perf 桩统一（跨套件复用防属性缺失）

    responses.JSONResponse = _JSONResponse
    responses.PlainTextResponse = _PlainTextResponse
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    return ["fastapi", "fastapi.responses"]      # 隔离修复：登记注入键供调用方装完即拆


_STUB_KEYS = _install_fastapi_stub()

from app import admin_api as aa              # noqa: E402
from app import hbase_executor as hbx        # noqa: E402
from app import hbase_load as hbl            # noqa: E402
from app import load_tasks as lt              # noqa: E402  B2 异步装载任务表

import sync_config as C                      # noqa: E402
import schema_meta as sm                     # noqa: E402
import hbase_to_redis as h2r                 # noqa: E402
from common import recon_meta_set            # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS

ADMIN_API_SRC = REPO / "service" / "app" / "admin_api.py"
LOAD_DOMAIN_NEEDLE = "卡2：数据装载域"


# ── 内存假 Redis（沿 test_load_schema 同款） ──

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


class FakeRequest:
    """端点直调最小形状：只需 await request.json()（_payload 用）＋可选 sync_redis 注入。"""

    def __init__(self, payload=None, redis=None):
        self._p = payload or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis, sync_redis=redis))

    async def json(self): return self._p


def _run(coro): return asyncio.run(coro)


def _body(resp): return json.loads(resp.body.decode("utf-8"))


def _load_domain_source() -> str:
    """装载域源码段（端点链 AC-2 机检范围）。

    三批（2026-10-08）注：终点从 POST /v1/admin/publish 收窄为 P3 版本空间管理域
    段头——版本管理域（versions/delete 登记同步引 SERVICES_YAML）插在装载域与
    发布 POST 之间，非装载端点链，不再扫入；「装载域端点链零 services.yaml
    依赖」的断言语义不变。
    """
    text = ADMIN_API_SRC.read_text(encoding="utf-8")
    start = text.index(LOAD_DOMAIN_NEEDLE)
    end = text.index("P3（2026-10-08）：版本空间管理域", start)
    return text[start:end]


# ── AC-2：发现＝真连接，不读 services.yaml ──

def test_tables_endpoint_uses_hbase_only():
    """装载端点链零 services.yaml 依赖（无 load_services/resolve_backend/SERVICES_YAML）。"""
    seg = _load_domain_source()
    for banned in ("load_services", "resolve_backend", "SERVICES_YAML"):
        assert banned not in seg, f"装载域端点链不得引用 {banned}（发现/装载是数据面的事）"


def test_load_tables_view_shapes_rows():
    """B3.1-2 视图：meta×tables_meta×快照 → full/namespace/cn 名/列族样本/loaded 段。"""
    meta = {"namespaces": {"default": {"usr_basic_info": {"families": {"cf": ["gender_cd"]}}},
                           "ods": {"usr_asset_tag": {"families": {"cf": []}}}},
            "table_count": 2}
    tables_meta = {"usr_basic_info": {"cn_name": "用户基本信息", "desc": "人口属性"}}
    snapshot = {"usr_basic_info": {"versions": ["20260930"], "latestVersion": None},
                "ods:usr_asset_tag": {"versions": [], "latestVersion": "20260929"}}
    view = aa._load_tables_view(meta, tables_meta, snapshot)
    assert view["ok"] is True and view["reachable"] is True and view["tableCount"] == 2
    rows = {t["full"]: t for t in view["tables"]}
    u = rows["usr_basic_info"]
    assert u["namespace"] == "default" and u["table"] == "usr_basic_info"
    assert u["tableCnName"] == "用户基本信息" and u["tableDesc"] == "人口属性"
    assert u["families"] == [{"name": "cf", "sampleColumns": ["gender_cd"]}]
    assert u["loaded"] == {"versions": ["20260930"], "latestVersion": None}
    a = rows["ods:usr_asset_tag"]
    assert a["tableCnName"] == ""                     # 缺登记＝空串（如实，不猜）
    assert a["loaded"]["latestVersion"] == "20260929"


# ── AC-3：三分类透出 ──

def test_tables_error_classification():
    """探活异常 → {category, code, message} 三分类（不可达带 up_hbase 指引；认证/其他各归位）。"""
    view = aa._load_sources_view("none", "h:9090", None, 3)
    s = view["sources"][0]
    assert s["status"] == "ok" and s["category"] is None and s["tableCount"] == 3

    unreach = aa._load_sources_view("none", "h:9090",
                                    ConnectionError("could not connect to h:9090"), None)
    s = unreach["sources"][0]
    assert s["status"] == "unreachable" and s["code"] == "HBASE_UNAVAILABLE"
    assert "up_hbase.sh" in s["message"]              # 行动指引（卡1 口径）

    authed = aa._load_sources_view("basic", "h:9090",
                                   RuntimeError("TTransport: sasl negotiation failed"), None)
    assert authed["sources"][0]["status"] == "auth_failed"

    other = aa._load_sources_view("none", "h:9090", KeyError("weird"), None)
    assert other["sources"][0]["status"] == "other"


# ── AC-4/AC-8：POST /load 全量委托＋显式忽略 ──

def _patch_loader(retval=None, raises=None):
    captured = {}
    orig = hbl.load_into_redis

    def fake(**kwargs):
        captured.update(kwargs)
        if raises is not None:
            raise raises
        return retval or {}

    hbl.load_into_redis = fake
    return captured, orig


def _wait_task(task_id, timeout_s=5.0):
    """B2 异步契约：轮询任务表至终态（done/failed），超时 AssertionError（fake 秒回）。"""
    import time as _t
    t0 = _t.monotonic()
    while _t.monotonic() - t0 < timeout_s:
        v = lt.view(task_id)
        if v and v["status"] in ("done", "failed"):
            return v
        _t.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout_s}s 内到终态")


def _load_and_wait(payload, retval=None, raises=None):
    """提交装载→等终态→返回 (202 提交体, 终态视图, captured)。"""
    captured, orig = _patch_loader(retval=retval, raises=raises)
    try:
        resp = _run(aa.load_ep(FakeRequest(payload)))
    finally:
        hbl.load_into_redis = orig
    submit = _body(resp)
    assert resp.status_code == 202 and submit.get("taskId"), submit
    final = _wait_task(submit["taskId"])
    return submit, final, captured


def test_load_full_table_reuses_existing_chain():
    """AC-4（B2 异步契约）：202+taskId→后台委托（columns 恒 None/replace 恒 False）→
    进度端点 done 响应含 B3.1 全部契约字段。"""
    retval = {"loaded": 2, "tag_cols": 2, "nonempty_cells": 3, "coverage_src": 0.75,
              "source_sha256": "abc", "data_as_of": "2026-09-30", "elapsedMs": 12,
              "reconcile": {"pass": True, "diffCount": 0, "checks": {"rows": "ok"},
                            "diffs": [], "error": ""},
              "schema": {"field_count": 2, "source": "hbase+dict", "generated_at": "t"}}
    submit, final, captured = _load_and_wait(
        {"source": "hbase", "table": "default:usr_basic_info", "cf": "cf", "version": "20260930"},
        retval=retval)
    assert submit["mode"] == "hbase->redis-load-async" and submit["pollUrl"].endswith(submit["taskId"])
    assert final["status"] == "done"
    resp = _run(aa.load_task_view_ep(submit["taskId"]))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and body["status"] == "done" and body["progress"] == 100
    assert body["mode"] == "hbase->redis-load"
    assert body["table"] == "usr_basic_info" and body["version"] == "20260930"
    assert body["loaded"] == 2 and body["tagCols"] == 2 and body["nonemptyCells"] == 3
    assert body["coverageSrc"] == 0.75 and body["dataAsOf"] == "2026-09-30"
    assert body["reconcile"]["pass"] is True and body["reconcile"]["diffCount"] == 0
    assert body["schema"] == {"fieldCount": 2, "source": "hbase+dict", "generatedAt": "t"}
    # 委托口径：全量（columns=None）、严格幂等（replace=False）——AC-4/NC-2
    assert captured["table"] == "default:usr_basic_info" and captured["cf"] == "cf"
    assert captured["version"] == "20260930"
    assert captured["columns"] is None and captured["replace"] is False


def test_columns_ignored_full_load():
    """AC-8（B2 异步契约）：请求带 columns/replace → 202 响应显式注明忽略；装载器收到 None/False。"""
    submit, final, captured = _load_and_wait(
        {"table": "usr_basic_info", "columns": ["gender_cd", "age_band"], "replace": True},
        retval={"reconcile": {}, "schema": {}})
    assert submit["columnsIgnored"] is True and submit["replaceIgnored"] is True
    assert submit["fullLoad"] is True
    assert final["status"] == "done"
    assert captured["columns"] is None and captured["replace"] is False
    assert captured["version"] and captured["version"].isdigit() and len(captured["version"]) == 8


# ── AC-5：同表同日 409 ──

def test_same_table_same_day_rejected():
    """LOAD_CONFLICT（B2 异步契约）：提交 202 成功→任务终态 failed→进度端点 409 信封
    带 table/version＋「不提供覆盖」口径与 CLI 指引。"""
    err = hbl.HBaseLoadServiceError("LOAD_CONFLICT", "目标空间非空（2 键），重灌须 --replace", 409)
    submit, final, _ = _load_and_wait({"table": "usr_basic_info", "version": "20260930"}, raises=err)
    assert final["status"] == "failed"
    resp = _run(aa.load_task_view_ep(submit["taskId"]))
    assert resp.status_code == 409
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "LOAD_CONFLICT"
    assert body["table"] == "usr_basic_info" and body["version"] == "20260930"
    assert "如实拒绝" in body["message"] and "--replace" in body["message"]


def test_load_task_not_found():
    """B2：不存在的任务 → 404（服务重启任务态即失，如实口径）。"""
    resp = _run(aa.load_task_view_ep("no-such-task"))
    assert resp.status_code == 404
    body = _body(resp)
    assert body["error"] == "TASK_NOT_FOUND" and "不持久化" in body["message"]


def test_load_input_validation():
    """入参闸：非 hbase 源/缺 table/坏版本号/坏表名 → 400 且零委托。"""
    called = {"n": 0}
    orig = hbl.load_into_redis

    def fail(**kw):      # 装载器被调到＝测试失败（校验必须前置）
        called["n"] += 1
        raise AssertionError("校验失败不得进入装载")

    hbl.load_into_redis = fail
    try:
        r1 = _run(aa.load_ep(FakeRequest({"source": "mysql"})))
        assert r1.status_code == 400 and _body(r1)["error"] == "UNSUPPORTED_SOURCE"
        r2 = _run(aa.load_ep(FakeRequest({})))
        assert r2.status_code == 400 and _body(r2)["error"] == "INVALID_TABLE"
        r3 = _run(aa.load_ep(FakeRequest({"table": "usr_basic_info", "version": "2026-09-30"})))
        assert r3.status_code == 400 and _body(r3)["error"] == "INVALID_VERSION"
        r4 = _run(aa.load_ep(FakeRequest({"table": "a:b:c:d"})))
        assert r4.status_code == 400 and _body(r4)["error"] == "INVALID_TABLE"
    finally:
        hbl.load_into_redis = orig
    assert called["n"] == 0


# ── AC-10：/assets 跨表枚举 ──

def _seed_loaded(r, table, version, live=None):
    """经真 write_space＋recon 落一套已装载版本（唯一写入路径，非手拼键）。"""
    rows = {"rk-1": {"gender_cd": "0", "age_band": "C"}}
    with C.table_scope(table):
        h2r.write_space(r, version, rows, table=table, cf="cf")
        recon_meta_set(r, version, True, {"diffCount": 0})
        if live:
            r.set(C.ALIAS_KEY, version)


def test_tabver_listing_after_load():
    """装载后 /assets 枚举：表×版本×rows/tagCols/reconPass/schemaFieldCount/live。"""
    r = FakeRedis()
    _seed_loaded(r, "usr_basic_info", "20260930", live="20260930")
    _seed_loaded(r, "card_acct_summary", "20260929")
    view = aa._load_assets_view(r)
    assert view["ok"] is True
    tables = {t["table"]: t for t in view["tables"]}
    assert sorted(tables) == ["card_acct_summary", "usr_basic_info"]
    u = tables["usr_basic_info"]
    assert u["tableCnName"] == "用户基本信息" and u["domain"] == "usr_basic_info"
    v = u["versions"][0]
    assert v["version"] == "20260930" and v["rows"] == 1 and v["tagCols"] == 2
    assert v["reconPass"] is True and v["schemaFieldCount"] == 2 and v["live"] is True
    c = tables["card_acct_summary"]["versions"][0]
    assert c["live"] is False and c["reconPass"] is True


def test_schema_endpoint_found_and_not_found():
    """B3.1-5：schema 查询 200（装载副产品字段）与 404（未装载/版本不存在）。"""
    r = FakeRedis()
    _seed_loaded(r, "usr_basic_info", "20260930")
    resp = _run(aa.load_schema_ep(FakeRequest(redis=r), table="usr_basic_info", version="20260930"))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and body["fieldCount"] == 2
    names = sorted(f["name"] for f in body["fields"])
    assert names == ["age_band", "gender_cd"]
    g = [f for f in body["fields"] if f["name"] == "gender_cd"][0]
    assert g["cnName"] == "性别代码" and g["type"].upper().startswith("VARCHAR")

    miss = _run(aa.load_schema_ep(FakeRequest(redis=r), table="usr_basic_info", version="20991231"))
    assert miss.status_code == 404 and _body(miss)["error"] == "SCHEMA_NOT_FOUND"
    bad = _run(aa.load_schema_ep(FakeRequest(redis=r), table="usr_basic_info"))
    assert bad.status_code == 400 and _body(bad)["error"] == "SCHEMA_QUERY_INVALID"


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
