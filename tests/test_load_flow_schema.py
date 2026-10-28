#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字段释义收权装载环节（2026-10-14 开工门槛-字段释义装载收权_v1 · 维护者 拍板）——AC-1/2/3/5 机检面。

判据映射（门槛稿 §A3）：
  AC-1 test_carryover     装载环节选中已装载过的表且字段集未变 → 字段清单必须默认带出
                          上次释义（零操作可继续）。③ initFieldDefs 读上次版本
                          sync:schema（GET /v1/admin/load/schema），submit 不以释义
                          确认态为前置；
  AC-2 test_ddl_override  装载环节贴 DDL 并确认 → 当次装载释义被新 DDL 覆盖（含新增
                          字段）。复用既有 B1 parse/save 端点与错误信封（错误码透出）；
                          字段消失按 B1 既有 FIELDS_DROPPED 显式拒绝（A4）；
  AC-3 test_schema_source 装载完成 sync:schema:<表>:<版本> 必须带当次确认释义
                          （write_space 唯一写入路径＝沿用卡2 链路）；发布向导/已发布页/
                          试查台字段说明与装载产物同源（同一 /v1/admin/load/schema）；
  AC-5 test_field_diff    字段集变化（新增字段）→ 清单标出无释义字段并引导补齐
                          （defMissing 判定＋「新增」标记＋引导文案），DDL 确认＝整表
                          按名替换，不静默沿用错位释义。

分层纪律：沿 test_load_api 口径——后端走真函数（sim/h2r/sm＋admin_api 端点直调
FakeRequest，不启服务）；临时目录做 schema CSV 沿用库真源（不碰仓库 sync/）；
前端交互为静态源码断言（同 test_load_page——门禁环境无浏览器，AC 文案/绑定/控制流
以源码为证）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_fastapi_stub() -> list:
    """宿主门禁环境无 fastapi（容器内才有）——最小 stub 供 admin_api 端点直调（同 test_load_api）。"""
    if "fastapi" in sys.modules:
        return []
    import types as _types
    fastapi = _types.ModuleType("fastapi")

    class _APIRouter:
        def __init__(self, *a, **k):
            pass

        def get(self, *a, **k):
            return lambda fn: fn

        def post(self, *a, **k):
            return lambda fn: fn

    fastapi.APIRouter = _APIRouter
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

    responses.JSONResponse = _JSONResponse
    responses.PlainTextResponse = _JSONResponse
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses
    return ["fastapi", "fastapi.responses"]      # 隔离修复：登记注入键供调用方装完即拆


_STUB_KEYS = _install_fastapi_stub()

from app import admin_api as aa              # noqa: E402
from app import schema_import as sim         # noqa: E402

import sync_config as C                     # noqa: E402
import schema_meta as sm                    # noqa: E402
import hbase_to_redis as h2r                # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS

SRC_PAGE = REPO / "admin" / "load.html"
SRC_JS = REPO / "admin" / "load.js"
OUT_PAGE = REPO / "portal" / "admin" / "load.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_sync_keyspace():
    """每测前后还原 sync_config 键空间 6 global（use_table 裸切副作用，同 test_load_schema）。"""
    saved = (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG)
    yield
    (C.NS, C.ALIAS_KEY, C.VER_META, C.RECON_META, C.SWITCH_LOG, C.DELETE_LOG) = saved


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
    """端点直调最小形状：await request.json()（_payload 用）＋sync_redis 注入。"""

    def __init__(self, payload=None, redis=None):
        self._p = payload or {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=redis, sync_redis=redis))

    async def json(self): return self._p


def _run(coro): return asyncio.run(coro)


def _body(resp): return json.loads(resp.body.decode("utf-8"))


# ══════════ AC-1：默认沿用上次释义（零操作可继续） ══════════

def test_carryover():
    """后端：上次版本 sync:schema 经 GET /v1/admin/load/schema 可读（③ 沿用取数源，
    字段带 cnName）；前端：选表即 initFieldDefs（不待用户动作），carry 态直取
    body.fields，submit 不以释义态为前置——同字段集零操作可继续。"""
    r = FakeRedis()
    fields = [
        {"name": "aum_band", "cf": "cf", "type": "VARCHAR(16)", "cnName": "AUM等级",
         "desc": "管理资产规模分档"},
        {"name": "dep_bal_band", "cf": "cf", "type": "VARCHAR(16)", "cnName": "存款余额档",
         "desc": "存款余额分档"},
    ]
    sm.schema_meta_set(r, "usr_asset_tag", "20261014", fields, source="hbase+dict")
    resp = _run(aa.load_schema_ep(FakeRequest(redis=r), table="usr_asset_tag",
                                  version="20261014"))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["ok"] is True and body["fieldCount"] == 2
    by = {f["name"]: f for f in body["fields"]}
    assert by["aum_band"]["cnName"] == "AUM等级" and by["dep_bal_band"]["cnName"] == "存款余额档"

    js = _read(SRC_JS)
    pick = js[js.index("pick(t) {"):js.index("async submit(")]
    assert "initFieldDefs(t)" in pick, "选表即带出释义清单（AC-1 零操作，不待用户动作）"
    init = js[js.index("async initFieldDefs("):js.index("fieldStateText()")]
    assert "API.schema" in init, "沿用取数须走 /v1/admin/load/schema（装载产物同源）"
    assert "this.fieldState = 'carry'" in init and "body.fields.map" in init,         "schema 可读须落 carry 态并直取上次字段"
    submit = js[js.index("async submit("):js.index("async overwrite(")]
    assert "fieldState" not in submit and "fieldRows" not in submit,         "提交不得以释义确认态为前置（同字段集零操作可继续）"


# ══════════ AC-2：贴 DDL 覆盖当次装载释义（复用 B1 端点与错误信封） ══════════

def test_ddl_override(tmp_path, monkeypatch):
    """行为链：贴 DDL（含新增字段与无 COMMENT 字段）→ B1 parse/merge/save 写沿用库
    → 装载（write_space 唯一写入路径）→ sync:schema 带新 DDL 释义；parse 失败＝
    错误信封原样（DDL_PARSE_FAILED）；字段消失＝B1 既有 FIELDS_DROPPED 显式拒绝。"""
    # 端点错误信封（复用 B1 parse 端点；不落盘）
    resp = _run(aa.schema_parse_ep(FakeRequest(
        payload={"table": "t_ddl_x", "ddl": "THIS IS NOT A CREATE TABLE"})))
    assert resp.status_code == 400
    assert _body(resp)["error"] == "DDL_PARSE_FAILED"

    # 前端：parse/save 端点复用＋失败人话透出（错误信封 message）
    js = _read(SRC_JS)
    assert "API.schemaParse" in js and "API.schemaSave" in js, "须复用 B1 parse/save 端点"
    parse_seg = js[js.index("async ddlParse("):js.index("async ddlConfirm(")]
    assert "解析失败：" in parse_seg, "parse 失败须错误信封人话透出（不裸抛）"
    assert "this.picked.table" in parse_seg, "表名＝当前选中表（装载环节绑定）"

    # 行为链（临时目录做沿用库真源，不碰仓库 sync/）
    monkeypatch.setattr(sim, "SYNC_DIR", tmp_path)     # sim 写源（模块常量 import 时定格）
    monkeypatch.setattr(sm, "_sync_dir", lambda: tmp_path)  # 装载富化读源（dict_rows）
    table = "t_ddl_x"
    ddl = ("CREATE TABLE t_ddl_x ("
           "aum_band VARCHAR(16) COMMENT 'AUM等级', "
           "new_flag TINYINT COMMENT '新标志', "
           "plain_col VARCHAR(8))")
    rows = sim.merge_rows(sim.parse_ddl(ddl), sim.load_rows(table))   # 首次登记
    sim.save_csv(table, rows)

    r = FakeRedis()
    data = {"u1": {"aum_band": "A", "new_flag": "1", "plain_col": "x"}}
    res = h2r.write_space(r, "20261014", data, table=table, cf="cf")
    assert res["schema"]["field_count"] == 3
    got = sm.schema_meta_get(r, table, "20261014")
    by = {f["name"]: f for f in got["fields"]}
    assert by["aum_band"]["cnName"] == "AUM等级"       # DDL 释义入装载产物
    assert by["new_flag"]["cnName"] == "新标志"          # 新增字段：COMMENT 即释义
    assert by["plain_col"]["cnName"] == "待补"           # 无 COMMENT：如实待补（AC-5 前置）
    assert got["source"] == "hbase+dict"

    # 字段消失＝显式拒绝（A4：按 B1 既有语义，不静默删）
    shrink = "CREATE TABLE t_ddl_x (aum_band VARCHAR(16) COMMENT 'AUM等级')"
    with pytest.raises(sim.SchemaImportError) as ei:
        sim.merge_rows(sim.parse_ddl(shrink), sim.load_rows(table))
    assert ei.value.code == "FIELDS_DROPPED"


# ══════════ AC-3：装载完成 sync:schema 带当次确认释义；发布/凭证同源 ══════════

def test_schema_source(tmp_path, monkeypatch):
    """后端：write_space 落的 sync:schema ＝ load_schema_ep 读出的字段（同一真源，
    逐字段一致）——装载确认释义经沿用库富化固化；前端同源：发布向导（publish.js 经
    HB.loadSchemaUrl）／已发布页 published.js／试查台 trial.js 字段说明全部取
    /v1/admin/load/schema（与装载页沿用同端点，零第二取数路径）。"""
    monkeypatch.setattr(sim, "SYNC_DIR", tmp_path)
    monkeypatch.setattr(sm, "_sync_dir", lambda: tmp_path)
    table = "t_src_x"
    ddl = "CREATE TABLE t_src_x (sts VARCHAR(16) COMMENT '状态', amt DECIMAL(16,2) COMMENT '金额')"
    sim.save_csv(table, sim.merge_rows(sim.parse_ddl(ddl), sim.load_rows(table)))

    r = FakeRedis()
    h2r.write_space(r, "20261015", {"k1": {"sts": "1", "amt": "9.9"}},
                    table=table, cf="cf")
    written = sm.schema_meta_get(r, table, "20261015")
    resp = _run(aa.load_schema_ep(FakeRequest(redis=r), table=table, version="20261015"))
    body = _body(resp)
    assert resp.status_code == 200 and body["ok"] is True
    assert body["fields"] == written["fields"],         "端点读出须与 write_space 写入逐字段一致（同源）"
    by = {f["name"]: f for f in body["fields"]}
    assert by["sts"]["cnName"] == "状态" and by["amt"]["cnName"] == "金额"

    # 前端消费方：发布向导/已发布页/试查台字段说明＝同一装载产物端点
    adapter = _read(REPO / "admin" / "publish-hbase-adapter.js")
    assert "'/v1/admin/load/schema'" in adapter and "loadSchemaUrl" in adapter
    assert "loadSchemaUrl" in _read(REPO / "admin" / "publish.js"),         "发布向导④字段勾选须经 HB.loadSchemaUrl（装载产物）"
    assert "HB.loadSchemaUrl" in _read(REPO / "admin" / "published.js"),         "已发布页字段中文名须读装载产物（经 HB.loadSchemaUrl 同端点）"
    assert "/v1/admin/load/schema" in _read(REPO / "portal" / "assets" / "trial.js"),         "试查台出参释义须读装载产物"

    # 装载请求体不带字段清单（AC-8 口径不破坏）：释义经沿用库随版本固化，不进 payload
    m = re.search(r"var payload = \{[^}]*\}", _read(SRC_JS), re.S)
    assert m and "rows" not in m.group(0) and "ddl" not in m.group(0),         "装载请求体不得夹带字段清单（沿用链路，不另起炉灶）"


# ══════════ AC-5：字段集变化标出无释义字段引导补齐（不静默沿用错位） ══════════

def test_field_diff():
    """前端：未释义判定（cnName 空/待补）＋新增标记（parse diff.added）＋琥珀引导
    文案；DDL 确认＝fieldRows 整表按名替换（s.rows.map），无按位沿用路径。"""
    js = _read(SRC_JS)
    # 未释义判定纯函数（挂 O2LOAD 可直调）
    assert "function defMissing(r)" in js and "defMissing: defMissing" in js
    assert "cn === '待补'" in js, "「待补」（build_fields 如实口径）须判未释义"
    # 新增标记：parse 存 diff.added → 确认行标 added → UI「新增」chip
    assert "s.added = d.added || []" in js
    confirm = js[js.index("async ddlConfirm("):js.index("stopLoadTimer()")]
    assert "added.indexOf(r['字段名']) >= 0" in confirm, "确认行须带 parse 新增标记"
    assert "this.fieldRows = s.rows.map" in confirm,         "DDL 确认＝整表替换当次清单（按名映射，无按位沿用）"
    # 引导文案与计数
    assert "个字段未释义" in js and "补齐后再装载" in js, "缺未释义引导文案"
    assert "get missingCount()" in js, "未释义计数须为派生属性（面板 x-show）"
    # UI：未释义/新增 chip＋琥珀引导面板在场（源页与产物）
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        sec = html[html.index('id="load-fields"'):html.index('id="load-ddl"')]
        assert "未释义" in sec and "新增" in sec, f"{p.name} 缺未释义/新增标记"
        assert 'id="load-fields-missing"' in sec and "missingText()" in sec,             f"{p.name} 缺未释义引导面板"


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
