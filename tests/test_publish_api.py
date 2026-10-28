#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡3 套件③：发布端点契约（AC-9/T7 · B3.2 请求换源＋响应前向契约）。

判据映射：
  - 请求：扁平式 {serviceName, routePath, table(裸表名), source:'hbase',
    fields[{name,cf,type,cnName}], keyType, loadVersion} —— ops_config.normalize_publish_payload
    承接（flat source 新增；表/键/版本同旧式）；fields 带 type/cnName 不报 400；
  - 响应：前向契约 {ok, serviceId, routePath, action:created|updated, dataVersion,
    auditId(null 待卡4)}＋兼容保留 entry/before/namingHint；
  - 覆盖更新机制（updated 不追加条目）归卡4 实装——本套件断言「契约就位」：
    before 有值 → action=updated（端点映射层语义，注册表写语义不动）；
  - P2（2026-10-08）：错形请求 4xx＋标准错误信封——malformed/非对象体 →
    invalid_json 400；fields/columns/keys 等形状非法 → invalid_request 400
    （原直穿纯逻辑层未捕获异常 → 500 无信封；回归＝不 500 且零副作用）。

分层：fastapi 缺席环境 stub 后直调端点函数（沿 test_load_api 口径）；
opsc.publish_service 打桩捕获入参（真实写语义归 tests/test_admin_publish_api.py 既有覆盖）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "sync")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _install_fastapi_stub() -> list:
    """宿主门禁环境无 fastapi（容器内才有）——最小 stub 直调 admin_api 端点函数。"""
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
from app import ops_config as oc             # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS

FLAT_PAYLOAD = {
    "serviceName": "用户基本信息查询",
    "routePath": "/v1/svc/usr-basic-info",
    "table": "usr_basic_info",
    "source": "hbase",
    "fields": [{"name": "gender_cd", "cf": "cf",
                "type": "character varying(16)", "cnName": "性别代码"}],
    "keyType": "phone_sm3",
    "cf": "cf",
    "loadVersion": "20261001",
    "status": "published", "operator": "console",
}


class FakeRequest:
    def __init__(self, payload=None):
        self._p = payload if payload is not None else {}
        self.app = SimpleNamespace(state=SimpleNamespace(redis=None, sync_redis=None))

    async def json(self): return self._p


class FakeMalformedRequest(FakeRequest):
    """malformed JSON：request.json() 抛错（错形请求体；合法但非对象的体由
    FakeRequest([...]/"str") 直接给出——strict 解析两条路都要 400）。"""

    async def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def _run(coro): return asyncio.run(coro)


def _body(resp): return json.loads(resp.body.decode("utf-8"))


def test_normalize_accepts_flat_source_and_load_version():
    """B3.2 扁平式：source/table/keyType/loadVersion 归一进 hbase 通道（旧嵌套式零变化）。"""
    norm = oc.normalize_publish_payload(FLAT_PAYLOAD)
    assert norm["datasource"] == "hbase", "扁平 source:'hbase' 须归一为 hbase 通道"
    assert norm["hbase"]["table"] == "usr_basic_info"
    assert norm["hbase"]["cf"] == "cf" and norm["hbase"]["key_type"] == "phone_sm3"
    assert norm["load_version"] == "20261001"
    # 旧嵌套式兼容（既有适配层/手调试路径零变化）
    old = oc.normalize_publish_payload({
        "datasource": "hbase",
        "backend": {"source": "hbase",
                    "hbase": {"namespace": "default", "table": "usr_basic_info",
                              "cf": "cf", "key_type": "phone_sm3"}},
        "loadVersion": "20260930"})
    assert old["datasource"] == "hbase" and old["hbase"]["table"] == "usr_basic_info"


class _FakeScanClient:
    """样本采样打桩（hbase 型发布态示例值自动生成路径；真链路归既有 e2e）。"""

    def scan(self, table, **kw):
        return [{"row": "probe", "columns": {"cf:gender_cd": "0"}}]


def _with_stubs(entry_before_fn):
    """统一打桩：publish_service＋_hbase_client（绕 main/fastapi 依赖）→ 恢复件。"""
    orig_pub, orig_client = aa.opsc.publish_service, aa._hbase_client
    aa.opsc.publish_service = entry_before_fn
    aa._hbase_client = lambda request: _FakeScanClient()
    return orig_pub, orig_client


def _restore(orig_pub, orig_client):
    aa.opsc.publish_service = orig_pub
    aa._hbase_client = orig_client


def test_fields_with_type_cnname_accepted_no_400():
    """请求字段带 type/cnName（B3.2 投影对象）→ 不报 400（写语义/持久化归卡4）。"""
    captured = {}

    def fake(path, *, name, route, fields, **kw):
        captured.update(name=name, route=route, fields=fields, **kw)
        return ({"id": "v1-svc-usr-basic-info", "serviceId": "v1-svc-usr-basic-info",
                 "name": name, "route": route}, None)

    st = _with_stubs(fake)
    try:
        resp = _run(aa.publish_service_ep(FakeRequest(dict(FLAT_PAYLOAD))))
    finally:
        _restore(*st)
    assert resp.status_code == 200, f"type/cnName 字段不得 400：{_body(resp)}"
    assert captured["fields"][0]["name"] == "gender_cd"


def test_response_forward_contract_action_created_then_updated():
    """响应前向契约：ok/serviceId/routePath/dataVersion/auditId；action 随 before 映射
    （覆盖更新注册表写语义归卡4——本断言＝端点映射层契约）。"""
    entry = {"id": "v1-svc-usr-basic-info", "serviceId": "v1-svc-usr-basic-info",
             "name": "用户基本信息查询", "route": "/v1/svc/usr-basic-info"}
    st = _with_stubs(lambda path, **kw: (entry, None))
    try:
        created = _run(aa.publish_service_ep(FakeRequest(dict(FLAT_PAYLOAD))))
    finally:
        _restore(*st)
    st = _with_stubs(lambda path, **kw: (entry, {"id": "v1-svc-usr-basic-info"}))
    try:
        updated = _run(aa.publish_service_ep(FakeRequest(dict(FLAT_PAYLOAD))))
    finally:
        _restore(*st)
    c, u = _body(created), _body(updated)
    for body, action in ((c, "created"), (u, "updated")):
        assert body["ok"] is True
        assert body["serviceId"] == "v1-svc-usr-basic-info"
        assert body["routePath"] == "/v1/svc/usr-basic-info"
        assert body["dataVersion"] == "20261001"
        assert body["auditId"] is None, "auditId 待卡4 接 audit.py 八元组（如实 null）"
        assert body["action"] == action
    # 兼容保留（既有适配层/测试）
    assert "entry" in c and "before" in c and "namingHint" in c


# ======================================================================
# P2（2026-10-08）：错形请求 4xx＋标准错误信封（原 500 无信封 → invalid_json/
# invalid_request 400；信封字段 error/message 与 appkeys 端点同口径，traceId 等
# 由 trace_middleware 运行期注入，容器内已验证）
# ======================================================================

def test_publish_malformed_json_400_invalid_json_envelope():
    """malformed JSON / 合法但非对象体 → 400 invalid_json（不落进误导性 invalid_name）。"""
    calls = []
    st = _with_stubs(lambda *a, **k: calls.append(1) or ({}, None))
    try:
        malformed = _run(aa.publish_service_ep(FakeMalformedRequest()))
        nonobj = _run(aa.publish_service_ep(FakeRequest([1, 2, 3])))
    finally:
        _restore(*st)
    for r in (malformed, nonobj):
        assert r.status_code == 400, _body(r)
        body = _body(r)
        assert body["error"] == "invalid_json", body
        assert body["message"], "信封须带人话 message"
    assert not calls, "错形请求不得触达发布写路径"


def test_publish_fields_wrong_shape_400_invalid_request():
    """fields 非对象数组（字符串/数字列表/散对象）→ 400 invalid_request，零副作用。"""
    calls = []
    st = _with_stubs(lambda *a, **k: calls.append(1) or ({}, None))
    try:
        resps = [_run(aa.publish_service_ep(FakeRequest({**FLAT_PAYLOAD, "fields": bad})))
                 for bad in ("abc", [1, 2], {"name": "x"})]
    finally:
        _restore(*st)
    for r in resps:
        assert r.status_code == 400, _body(r)
        assert _body(r)["error"] == "invalid_request", _body(r)
    assert not calls, "形状闸须在采样/发布之前拦下（零副作用）"


def test_sync_switch_rollback_malformed_400_no_cli():
    """切版/回滚 malformed JSON → 400 invalid_json 且不执行 CLI（曾把空体当合法触发回滚）。"""
    ran = []
    orig_sw, orig_rb = aa.syo.run_switch, aa.syo.run_rollback
    aa.syo.run_switch = lambda *a, **k: ran.append("switch") or (200, {"ok": True})
    aa.syo.run_rollback = lambda *a, **k: ran.append("rollback") or (200, {"ok": True})
    try:
        sw = _run(aa.publish_sync_switch_ep(FakeMalformedRequest()))
        rb = _run(aa.publish_sync_rollback_ep(FakeMalformedRequest()))
    finally:
        aa.syo.run_switch, aa.syo.run_rollback = orig_sw, orig_rb
    for r in (sw, rb):
        assert r.status_code == 400, _body(r)
        assert _body(r)["error"] == "invalid_json", _body(r)
    assert not ran, "错形请求不得触发切版/回滚 CLI"


def test_publish_offline_malformed_400():
    """下线端点 malformed JSON → 400 invalid_json（不触 offline_service）。"""
    off = []
    orig = aa.opsc.offline_service
    aa.opsc.offline_service = lambda *a, **k: off.append(1) or ({}, None)
    try:
        r = _run(aa.publish_offline("some-sid", FakeMalformedRequest()))
    finally:
        aa.opsc.offline_service = orig
    assert r.status_code == 400 and _body(r)["error"] == "invalid_json", _body(r)
    assert not off


def test_hbase_load_and_test_wrong_shape_4xx():
    """hbase load 的 columns/keys 非数组、test 的 fields 非对象数组 → 400（原直穿 500）。"""
    load_cols = _run(aa.hbase_load_ep(FakeRequest({"table": "usr_basic_info", "columns": 123})))
    load_keys = _run(aa.hbase_load_ep(FakeRequest({"table": "usr_basic_info", "keys": {"a": 1}})))
    load_bad = _run(aa.hbase_load_ep(FakeMalformedRequest()))
    test_fields = _run(aa.hbase_test(FakeRequest(
        {"fields": "abc", "sampleKey": "13800138000", "table": "t"})))
    test_bad = _run(aa.hbase_test(FakeMalformedRequest()))
    for r, code in ((load_cols, "invalid_request"), (load_keys, "invalid_request"),
                    (test_fields, "invalid_request"), (load_bad, "invalid_json"),
                    (test_bad, "invalid_json")):
        assert r.status_code == 400, _body(r)
        assert _body(r)["error"] == code, _body(r)


# ======================================================================
# P3 二批（2026-10-08 维护者 六条②）：重复 route → 409 冲突＋结构化 error。
# 占用拦截本体在纯逻辑层 route_naming.find_route_occupant（R6：同 route 异名拒、
# 同名重发＝覆盖更新 200），端点层只做窄状态映射（route_exists 400→409）。
# ======================================================================

def test_publish_duplicate_route_409_conflict_envelope():
    """重复 route（异名占用）→ 409 + {error: route_exists, message 含占用者}。"""
    def fake(path, *, name, route, fields, **kw):
        raise oc.OpsConfigError(400, "route_exists",
                                "该地址已被服务「用户资产标签查询」占用（/v1/svc 地址全网唯一）。"
                                "请换一个地址后重试")
    st = _with_stubs(fake)
    try:
        resp = _run(aa.publish_service_ep(FakeRequest(dict(FLAT_PAYLOAD))))
    finally:
        _restore(*st)
    assert resp.status_code == 409, f"重复 route 须 409 冲突：{_body(resp)}"
    body = _body(resp)
    assert body["error"] == "route_exists", body
    assert "用户资产标签查询" in body["message"] and "占用" in body["message"], \
        "结构化 message 须含占用者与人话指引"


def test_publish_other_ops_errors_keep_own_status():
    """窄映射回归：非占用类 OpsConfigError 状态码不受影响（invalid_name 仍 400）。"""
    def fake(path, *, name, route, fields, **kw):
        raise oc.OpsConfigError(400, "invalid_name", "服务名必填")
    st = _with_stubs(fake)
    try:
        resp = _run(aa.publish_service_ep(FakeRequest(dict(FLAT_PAYLOAD))))
    finally:
        _restore(*st)
    assert resp.status_code == 400 and _body(resp)["error"] == "invalid_name", _body(resp)


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
