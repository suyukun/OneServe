#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡3 套件②：发布向导 API 契约机检（AC-2/4/5/7/8/9 前端侧）。

判据映射（开工门槛-卡3 §A3）：
  AC-2 ①步表源＝GET /v1/admin/load/assets（发布链路不再引用 discovery）；
  AC-5 ②步「设为线上/切回」复用 POST /v1/admin/publish/sync/{switch,rollback}（三闸不动）；
  AC-7 ④步字段源＝GET /v1/admin/load/schema（normalizeSchema；unknown/待补 原样透出 NC-7）；
  AC-8 投影白名单 ⊆ schema（checkedFieldsOf 纯函数＋发布体 fields ⊆ schema）；
  AC-9 发布体＝B3.2 扁平契约（含 type/cnName；不带 columns/装载语义）。

JS 纯函数经 node 直调（加载序同页面：route-naming → adapter → publish.js）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
PUB_JS = ADMIN / "publish.js"
ADAPTER = ADMIN / "publish-hbase-adapter.js"

HAS_NODE = shutil.which("node") is not None


def _code_only(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)//.*$", "", js)


def _node_json(script: str):
    assert HAS_NODE, "需要 node 运行向导纯函数 headless 断言"
    pre = ("var __rn = require(" + json.dumps(str(ADMIN / "route-naming.js")) + ");\n"
           "globalThis.ROUTE_NAMING = __rn.ROUTE_NAMING || __rn;\n")
    pre += "".join("require(" + json.dumps(str(p)) + ");\n" for p in (ADAPTER, PUB_JS))
    r = subprocess.run(["node", "-e", pre + script], capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return json.loads(r.stdout.strip())


# ── AC-2：①步表源＝assets（发布链路零 discovery 引用） ──

def test_step1_uses_load_assets():
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    assert "HB.CONTRACT.loadAssets" in js, "①步表源须为 /v1/admin/load/assets（B3.1）"
    assert not re.search(r"HB\.CONTRACT\.load(?!Assets|Schema)", js), \
        "发布链路不得引用旧装载端点（迁移清单；loadAssets/loadSchema 除外）"
    assert "HB.CONTRACT.discovery" not in js and "HB.CONTRACT.test" not in js, \
        "发布链路不得引用 discovery/test（AC-2）"


# ── AC-5：切版复用 sync 三闸 ──

def test_switch_live_version_reuses_sync_switch():
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    seg = js[js.index("async switchNow()"):]
    seg = seg[:seg.index("async rollbackNow()")]
    assert "HB.CONTRACT.syncSwitch" in seg, "设为线上须复用 POST /v1/admin/publish/sync/switch"
    assert "HB.buildSwitchPayload" in seg, "切换载荷须走适配层单一构造（table+version）"
    seg2 = js[js.index("async rollbackNow()"):]
    seg2 = seg2[:seg2.index("validateRoute()")]
    assert "HB.CONTRACT.syncRollback" in seg2 and "HB.buildRollbackPayload" in seg2, \
        "切回上一版须复用 sync/rollback（按表隔离）"
    # 载荷形状：裸表名＋版本（与 sync CLI 同口径）
    out = _node_json("""
var p = O1HBase.buildSwitchPayload({ table: { table: 'default:usr_basic_info' }, version: '20261001' });
var r = O1HBase.buildRollbackPayload({ table: { table: 'usr_basic_info' } });
console.log(JSON.stringify({ p: p, r: r }));
""")
    assert out["p"] == {"version": "20261001", "table": "usr_basic_info"}, out["p"]
    assert out["r"] == {"table": "usr_basic_info"}


# ── AC-7：④步字段源＝schema 端点（归一如实） ──

def test_step4_fields_from_schema_endpoint():
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    assert "HB.loadSchemaUrl" in js, "④步字段源须为 /v1/admin/load/schema（B3.1）"
    out = _node_json("""
var s1 = O1HBase.normalizeSchema({ ok: true, table: 'usr_basic_info', version: '20261001',
  fieldCount: 2, source: 'hbase+dict',
  fields: [ { name: 'gender_cd', cf: 'cf', type: 'character varying(16)', cnName: '性别代码', desc: 'x' },
            { name: 'mystery_col', cf: 'cf', cnName: '' } ] });
var s2 = O1HBase.normalizeSchema({ ok: false, error: 'SCHEMA_NOT_FOUND' });
console.log(JSON.stringify({ s1: s1, s2: s2 }));
""")
    s1 = out["s1"]
    assert s1["ok"] is True and s1["fieldCount"] == 2
    g = [f for f in s1["fields"] if f["name"] == "gender_cd"][0]
    assert g["cnName"] == "性别代码" and g["type"].startswith("character varying")
    m = [f for f in s1["fields"] if f["name"] == "mystery_col"][0]
    assert m["type"] == "unknown" and m["cnName"] == "待补", "缺元数据须原样 unknown/待补（NC-7）"
    assert out["s2"]["ok"] is False and s1["fields"], "404 归一如实不造假字段"


# ── AC-8：投影白名单 ⊆ schema ──

def test_projection_whitelist_subset_of_schema():
    out = _node_json("""
var schema = [ { name: 'gender_cd', cf: 'cf', type: 't', cnName: '性别代码' },
               { name: 'age_band', cf: 'cf', type: 't', cnName: '年龄段' } ];
var checks = { gender_cd: true, age_band: false };
var picked = WIZ.checkedFieldsOf(schema, checks);
var payload = O1HBase.buildPublishPayload({
  serviceName: '用户基本信息查询', routePath: '/v1/svc/usr-basic-info',
  table: { table: 'usr_basic_info' }, keyType: 'phone_sm3', cf: 'cf', loadVersion: '20261001',
  fields: picked, status: 'published', operator: 'console' });
console.log(JSON.stringify({ picked: picked, payload: payload }));
""")
    assert [f["name"] for f in out["picked"]] == ["gender_cd"], "勾选集＝投影白名单"
    p = out["payload"]
    names = {f["name"] for f in p["fields"]}
    schema_names = {"gender_cd", "age_band"}
    assert names <= schema_names, "发布字段必须 ⊆ 表 schema（AC-8）"
    assert "columns" not in p, "发布体不得携带装载语义 columns"
    assert p["fields"][0]["cnName"] == "性别代码" and p["fields"][0]["type"] == "t"


# ── AC-9：发布体＝B3.2 扁平契约（覆盖更新语义＝契约就位，机制待卡4） ──

def test_publish_payload_contract_b32():
    out = _node_json("""
var p = O1HBase.buildPublishPayload({
  serviceName: '用户基本信息查询', routePath: '/v1/svc/usr-basic-info',
  table: { table: 'usr_basic_info' }, keyType: 'phone_sm3', cf: 'cf', loadVersion: '20261001',
  fields: [ { name: 'gender_cd', cf: 'cf', type: 'character varying(16)', cnName: '性别代码' } ],
  status: 'published', operator: 'console' });
console.log(JSON.stringify(p));
""")
    p = out
    for key in ("serviceName", "routePath", "table", "source", "fields", "keyType",
                "loadVersion", "status", "operator"):
        assert key in p, f"发布体缺 B3.2 契约字段：{key}"
    assert p["table"] == "usr_basic_info" and p["source"] == "hbase"
    assert p["loadVersion"] == "20261001"
    assert set(p["fields"][0].keys()) == {"name", "cf", "type", "cnName"}, \
        "字段对象＝{name,cf,type,cnName}（B3.2）"


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
