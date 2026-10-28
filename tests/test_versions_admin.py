#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 套件：版本空间管理域 /v1/admin/versions*（2026-10-08 · 版本管理页后端）。

判据（卡口径逐条）：
  - 列表聚合：SCAN tags:* 按「(表, 版本)」聚键数（latest 指针不计入）；
    isLive＝比对 tags:<ns>:latest 指针值；registered＝console_services 写区
    backend.load.table 登记态（未登记＝孤儿）；verMeta/schemaMeta 在场性如实；
  - 删除闸①：version == latest 指针值 → 409 PROTECTED_LIVE_VERSION（人话文案）＋零删除；
  - 删除闸②：namespace+version 组合不存在（数据键与元信息全不在场）→ 404；
  - 删除本体：该空间数据键全删（deletedKeys＝实际删到数）＋sync:ver:<ns>:<version>＋
    sync:schema:<ns>:<version> 一并删（有则删）＋audit_append 留痕
    （sync:delete:<ns>:log LPUSH JSON）；别的空间/线上指针零影响；
  - 修三①（三批 2026-10-08 维护者 实测·幽灵版本）：删除成功后同步 services.yaml
    console_services 登记——backend.load.{table,version} 命中 → loaded:false
    （version 保留＝留痕口径不变）；未命中/已 false＝零写盘；响应带 registrySynced。

分层纪律：沿 test_load_api 口径——fastapi stub＋内存 FakeRedis＋端点函数直调
（FakeRequest）；种子经真 h2r.write_space（唯一写入路径，非手拼键）。

运行：python3 -m pytest tests/test_versions_admin.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_fastapi_stub() -> list:
    """宿主门禁环境无 fastapi（容器内才有）——注入最小 stub 以便直测 admin_api 端点函数。

    与 test_load_api 同款：stub 只满足 admin_api 顶层 import 与被测端点用到的
    JSONResponse 形状（.status_code/.body）。
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

import sync_config as C                      # noqa: E402
import hbase_to_redis as h2r                 # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS


# ── 内存假 Redis（沿 test_load_api 同款；本域另需 exists/lpush/lrange） ──

class _Pipe:
    def __init__(self, r):
        self._r, self._ops = r, []

    def set(self, k, v): self._ops.append(("set", k, v))
    def delete(self, k): self._ops.append(("del", k))

    def execute(self):
        out = []
        for op in self._ops:
            if op[0] == "set":
                out.append(self._r.set(op[1], op[2]))
            elif op[0] == "del":
                out.append(self._r.delete(op[1]))
        self._ops = []
        return out


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.hashes = {}
        self.sets = {}
        self.lists = {}

    def get(self, k): return self.kv.get(k)
    def set(self, k, v): self.kv[k] = v; return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            n += 1 if self.kv.pop(k, None) is not None else 0
            n += 1 if self.hashes.pop(k, None) is not None else 0
            n += 1 if self.sets.pop(k, None) is not None else 0
            n += 1 if self.lists.pop(k, None) is not None else 0
        return n

    def exists(self, k):
        return int(k in self.kv or k in self.hashes or k in self.sets or k in self.lists)

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

    def lpush(self, k, v):
        self.lists.setdefault(k, []).insert(0, v)
        return len(self.lists[k])

    def lrange(self, k, start, end):
        items = self.lists.get(k, [])
        if end == -1:
            return list(items[start:])
        return list(items[start:end + 1])

    def scan(self, cursor=0, match=None, count=1000):
        return 0, [k for k in self.kv if fnmatch.fnmatchcase(k, match or "*")]

    def pipeline(self, transaction=False): return _Pipe(self)
    def ping(self): return True


class FakeRequest:
    """端点直调最小形状：await request.json()＋state.sync_redis 注入。"""

    def __init__(self, payload=None, redis=None):
        self._p = payload or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis, sync_redis=redis))

    async def json(self): return self._p


def _run(coro): return asyncio.run(coro)


def _body(resp): return json.loads(resp.body.decode("utf-8"))


def _seed(r, table, version, n_rows=3, live=False) -> int:
    """经真 write_space 落一套已装载版本（数据键＋ver/schema 元信息＋tabver 索引）。"""
    rows = {f"rk-{i:03d}": {"gender_cd": "0", "age_band": "C"} for i in range(n_rows)}
    with C.table_scope(table):
        h2r.write_space(r, version, rows, table=table, cf="cf")
        if live:
            r.set(C.ALIAS_KEY, version)
    return len(rows)


def _patch_registered(tables):
    """注册表桩：console_services 写区按给定表名登记（读取处 opsc.load_console_services）。"""
    orig = aa.opsc.load_console_services
    aa.opsc.load_console_services = lambda path=None: [
        {"id": f"svc-{t}", "backend": {"load": {"table": t}}} for t in tables]
    return orig


@contextlib.contextmanager
def _registry_yaml(entries):
    """把 console_services 写区指到临时 services.yaml（hermetic：删除端点的登记
    同步会写 SERVICES_YAML，测试绝不碰真 service/services.yaml——工作树上另有
    并行卡改动）。种子文本沿生产结构：services: [] 运行时区＋CONSOLE_MARKER 写区。
    """
    d = tempfile.mkdtemp(prefix="oneserve-versions-")
    p = Path(d) / "services.yaml"
    p.write_text("services: []\n" + aa.opsc._dump_console_block(entries), encoding="utf-8")
    orig = aa.opsc.SERVICES_YAML
    aa.opsc.SERVICES_YAML = p
    try:
        yield p
    finally:
        aa.opsc.SERVICES_YAML = orig
        shutil.rmtree(d, ignore_errors=True)


def _registry_entry(table="default:usr_basic_info", version="20261001", loaded=True):
    """一条带 backend.load 来源留痕的写区条目（build_redis_entry_from_hbase 产物形状）。"""
    return {
        "id": "v1-svc-usr-basic", "name": "用户基本信息查询", "route": "/v1/svc/usr-basic",
        "status": "published", "fields": [{"name": "age_band", "sample": "C"}],
        "backend": {
            "kv": {"alias_key": "tags:usr_basic_info:latest"},
            "load": {"source": "hbase", "table": table, "cf": "cf",
                     "key_type": "phone_sm3", "version": version, "loaded": loaded},
        },
        "trace": {"action": "published", "by": "console", "at": "2026-10-08T00:00:00+00:00",
                  "commit": None},
    }


# ── 列表聚合 ──

def test_list_aggregates_spaces_and_flags():
    """SCAN tags:* 聚合：键数（指针不计入）/isLive/registered（孤儿）/verMeta/schemaMeta。"""
    r = FakeRedis()
    n_live = _seed(r, "usr_basic_info", "20261008", n_rows=3, live=True)
    n_old = _seed(r, "usr_basic_info", "20261001", n_rows=2)
    n_orph = _seed(r, "card_acct_summary", "20261007", n_rows=4)
    orig = _patch_registered(["usr_basic_info"])
    try:
        view = aa._versions_view(r)
    finally:
        aa.opsc.load_console_services = orig
    assert view["ok"] is True
    assert view["totalKeys"] == n_live + n_old + n_orph
    rows = {(s["namespace"], s["version"]): s for s in view["spaces"]}
    assert sorted(rows) == [("card_acct_summary", "20261007"),
                            ("usr_basic_info", "20261001"),
                            ("usr_basic_info", "20261008")]
    live_row = rows[("usr_basic_info", "20261008")]
    assert live_row["keys"] == n_live            # latest 指针键不计入键数
    assert live_row["isLive"] is True and live_row["registered"] is True
    assert live_row["verMeta"] is True and live_row["schemaMeta"] is True
    old_row = rows[("usr_basic_info", "20261001")]
    assert old_row["keys"] == n_old and old_row["isLive"] is False
    orphan = rows[("card_acct_summary", "20261007")]
    assert orphan["registered"] is False         # 未登记＝孤儿徽标
    assert orphan["isLive"] is False


def test_list_endpoint_reads_state_sync_redis():
    """GET /v1/admin/versions 端点直调：经 state.sync_redis 取视图（不触真 Redis）。"""
    r = FakeRedis()
    _seed(r, "usr_basic_info", "20261008", n_rows=1, live=True)
    orig = _patch_registered(["usr_basic_info"])
    try:
        resp = _run(aa.versions_list_ep(FakeRequest(redis=r)))
    finally:
        aa.opsc.load_console_services = orig
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and len(body["spaces"]) == 1
    assert body["spaces"][0]["namespace"] == "usr_basic_info"


# ── 删除闸①：线上版拒删 ──

def test_delete_live_version_rejected_409():
    """version == tags:<ns>:latest 指针值 → 409 PROTECTED_LIVE_VERSION＋人话文案＋零删除。"""
    r = FakeRedis()
    n = _seed(r, "usr_basic_info", "20261008", n_rows=3, live=True)
    resp = _run(aa.versions_delete_ep(FakeRequest(
        {"namespace": "usr_basic_info", "version": "20261008"}, redis=r)))
    assert resp.status_code == 409
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "PROTECTED_LIVE_VERSION"
    assert "线上版不可删除" in body["message"] and "切换上线" in body["message"]
    # 零副作用：数据键/元信息/指针原封不动，无审计条目
    assert sum(1 for k in r.kv if k.startswith("tags:usr_basic_info:20261008:")) == n
    assert r.exists("sync:ver:usr_basic_info:20261008") == 1
    assert r.get("tags:usr_basic_info:latest") == "20261008"
    assert r.lists.get("sync:delete:usr_basic_info:log") is None


# ── 删除本体：数据键＋ver/schema 元信息一并删＋审计留痕 ──

def test_delete_removes_keys_and_meta_with_audit():
    """正常删除：数据键全删（deletedKeys 对）＋sync:ver/sync:schema 一并删＋审计留痕。"""
    r = FakeRedis()
    _seed(r, "usr_basic_info", "20261008", n_rows=3, live=True)      # 线上版：不受影响
    n_old = _seed(r, "usr_basic_info", "20261001", n_rows=5)         # 旧版：删除目标
    with _registry_yaml([]):      # 修三①后删除会写 SERVICES_YAML → 指到临时文件（hermetic）
        resp = _run(aa.versions_delete_ep(FakeRequest(
            {"namespace": "usr_basic_info", "version": "20261001"}, redis=r)))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and body["deletedKeys"] == n_old
    assert body["deletedVerMeta"] is True and body["deletedSchemaMeta"] is True
    assert body["registrySynced"] == 0                # 写区无登记＝登记面本就干净
    # 目标空间清干净（数据键＋两类元信息）；线上空间与指针零影响
    assert sum(1 for k in r.kv if k.startswith("tags:usr_basic_info:20261001:")) == 0
    assert r.exists("sync:ver:usr_basic_info:20261001") == 0
    assert r.exists("sync:schema:usr_basic_info:20261001") == 0
    assert sum(1 for k in r.kv if k.startswith("tags:usr_basic_info:20261008:")) == 3
    assert r.exists("sync:ver:usr_basic_info:20261008") == 1
    assert r.get("tags:usr_basic_info:latest") == "20261008"
    # 审计留痕：sync:delete:<ns>:log LPUSH JSON（沿 audit_append 既有口径）
    entries = [json.loads(x) for x in r.lrange("sync:delete:usr_basic_info:log", 0, -1)]
    assert len(entries) == 1
    e = entries[0]
    assert e["action"] == "version_space_delete"
    assert e["namespace"] == "usr_basic_info" and e["version"] == "20261001"
    assert e["deleted_keys"] == n_old and e["deleted_meta"] == 2
    assert e["at"]


def test_delete_meta_only_space_succeeds_with_zero_keys():
    """元信息残留（数据键已不在）也可清：有则删，deletedKeys=0 如实。"""
    r = FakeRedis()
    r.hset("sync:ver:card_acct_summary:20260901", mapping={"rows": "7"})
    with _registry_yaml([]):
        resp = _run(aa.versions_delete_ep(FakeRequest(
            {"namespace": "card_acct_summary", "version": "20260901"}, redis=r)))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["deletedKeys"] == 0 and body["deletedVerMeta"] is True
    assert r.exists("sync:ver:card_acct_summary:20260901") == 0


# ── 修三①（三批 2026-10-08 维护者 实测：幽灵版本）——删除同步 console_services 登记 ──

def test_delete_syncs_console_registry_loaded_flag():
    """被删 (namespace,version) 命中登记条目 backend.load → 置 loaded:false。

    幽灵版本的登记面根因：删除只清 Redis，services.yaml console_services 的
    version/loaded 残留。回归口径：命中条目 loaded:false＋version 原样保留
    （来源留痕口径不变）；未命中条目（别的版本/别的表）零改动；运行时
    services: 区与响应 registrySynced=1。
    """
    r = FakeRedis()
    _seed(r, "usr_basic_info", "20261008", n_rows=3, live=True)
    _seed(r, "usr_basic_info", "20261001", n_rows=5)          # 删除目标
    entries = [
        _registry_entry("default:usr_basic_info", "20261001", loaded=True),   # 命中
        _registry_entry("default:usr_basic_info", "20261008", loaded=True),   # 线上版：不命中
        _registry_entry("default:card_acct_summary", "20261001", loaded=True),  # 别的表：不命中
    ]
    with _registry_yaml(entries) as reg:
        resp = _run(aa.versions_delete_ep(FakeRequest(
            {"namespace": "usr_basic_info", "version": "20261001"}, redis=r)))
        assert resp.status_code == 200
        body = _body(resp)
        assert body["registrySynced"] == 1
        after = aa.opsc.load_console_services(reg)
        text = reg.read_text(encoding="utf-8")
    assert after[0]["backend"]["load"]["loaded"] is False
    assert after[0]["backend"]["load"]["version"] == "20261001"   # version 保留＝留痕口径
    assert after[1]["backend"]["load"]["loaded"] is True          # 未命中条目零改动
    assert after[2]["backend"]["load"]["loaded"] is True
    assert text.startswith("services: []")                        # 运行时区原样
    assert aa.opsc.CONSOLE_MARKER in text


def test_delete_registry_sync_skips_when_no_match():
    """登记无命中（别的版本/已 loaded:false）＝零写盘（文件字节不变）＋registrySynced=0。"""
    r = FakeRedis()
    _seed(r, "usr_basic_info", "20261008", n_rows=2, live=True)
    _seed(r, "usr_basic_info", "20261001", n_rows=1)
    entries = [
        _registry_entry("default:usr_basic_info", "20261008", loaded=True),   # 线上版不删
        _registry_entry("default:usr_basic_info", "20261001", loaded=False),  # 已是 false：幂等跳过
    ]
    with _registry_yaml(entries) as reg:
        before = reg.read_bytes()
        resp = _run(aa.versions_delete_ep(FakeRequest(
            {"namespace": "usr_basic_info", "version": "20261001"}, redis=r)))
        assert resp.status_code == 200
        assert _body(resp)["registrySynced"] == 0
        assert reg.read_bytes() == before                          # 零写盘（幂等）


# ── 删除闸②：不存在 → 404 ──

def test_delete_unknown_space_404():
    """namespace+version 组合不存在（数据键与元信息全不在场）→ 404。"""
    r = FakeRedis()
    _seed(r, "usr_basic_info", "20261008", n_rows=1, live=True)
    resp = _run(aa.versions_delete_ep(FakeRequest(
        {"namespace": "usr_basic_info", "version": "20991231"}, redis=r)))
    assert resp.status_code == 404
    body = _body(resp)
    assert body["ok"] is False and body["error"] == "VERSION_NOT_FOUND"
    # 别的表同版本号不受影响（按表隔离）
    resp2 = _run(aa.versions_delete_ep(FakeRequest(
        {"namespace": "card_acct_summary", "version": "20261008"}, redis=r)))
    assert resp2.status_code == 404


# ── 入参闸 ──

def test_delete_input_validation():
    """缺 namespace/坏表名/坏版本号 → 400 且零删除。"""
    r = FakeRedis()
    n = _seed(r, "usr_basic_info", "20261008", n_rows=2, live=True)
    r1 = _run(aa.versions_delete_ep(FakeRequest({"version": "20261008"}, redis=r)))
    assert r1.status_code == 400 and _body(r1)["error"] == "INVALID_TABLE"
    r2 = _run(aa.versions_delete_ep(FakeRequest(
        {"namespace": "bad-table!", "version": "20261008"}, redis=r)))
    assert r2.status_code == 400 and _body(r2)["error"] == "INVALID_TABLE"
    r3 = _run(aa.versions_delete_ep(FakeRequest(
        {"namespace": "usr_basic_info", "version": "2026-10-08"}, redis=r)))
    assert r3.status_code == 400 and _body(r3)["error"] == "INVALID_VERSION"
    assert sum(1 for k in r.kv if k.startswith("tags:usr_basic_info:")) == n + 1  # 数据键＋指针


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
