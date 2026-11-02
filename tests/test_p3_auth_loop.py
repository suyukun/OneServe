#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 授权闭环组回归套件（tests/test_p3_auth_loop.py · 2026-10-08）。

覆盖（任务书两项验收判据的机器化部分）：
  A.【功能】AppKey 按服务签发/授权（新发布服务可获得授权，不再恒 403）：
     A1 端点层透传：POST /v1/admin/appkeys（admin_api.issue，stub 直调）携
        grants=[{service:新服务, fields}] → 200＋注册表落盘 grants；
        resolve_grant（main._caller_grant 的同一判定函数）对新服务返回授权字段
        ——查询链路授权闸放行（= 200 路径），新服务无需矩阵登记；
     A2 反例（修前缺陷口径）：签发不带 grants（继承试用集/矩阵）→ resolve_grant
        对新服务 403 service_not_granted——证明显式 grants 是新服务获权通路；
     A3 后端校验不放松：grants 指向未注册服务 → 400 unknown_service；
        字段为空 → 400 empty_fields；未授权服务仍 403（授权边界不变）；
     A4 前端纯函数（node 直调 O3）：normalizePublishedServices/publishedOnly/
        grantsFromForm——发布视图归一（id/serviceId 兼容、draft 剔除）与
        表单选择 → grants 请求体形状；表单/镜像 DOM 契约在场；
  B.【门面批 2026-10-08】试用台重设计为「接口调试」台（已发布服务在线调试）：
     B1 取数层（node 直调 TrialAPI.gatewayCall）：POST /v1/<route> passThrough——
        fetch 401/500 → {status, ok, json 错误信封, traceId} 原样带回，且只发
        一次请求（零 mock、零回落、零二次请求）；
     B2 组件逻辑（node 直调 trialPanel）：服务端错误 → state='error'、
        result=null（不渲染结果表）＋信封/追踪号入态；200 → state='ok'、
        resultRows 携 schema 中文释义、found:false 如实未命中；
     B3 产物 DOM（portal/trial.html）：三区骨架（控制行/接口信息/调用与结果）
        ＋错误信封区块在场；旧话术（自动降级/调试历史/SDK 包下载/明文手机号）
        不得残留；trial-tab-http/sdk 锚 id 保留（历史外链兼容锚；docs 深链已随
        docs 页 ux-polish 2026-11-02 下线失效，锚本身不删）；
     B4 源码契约：trial.js 走真实网关调用（postJsonRaw passThrough），无任何
        mock/trialDemoQuery.json 引用、无本地演示数据回落。

分层：fastapi 缺席环境 stub 后直调 admin_api 端点函数（沿 test_publish_api 口径）；
前端逻辑 node 直调（沿 test_quota_portal 口径）。
运行：python3 -m pytest tests/test_p3_auth_loop.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "service"), str(REPO / "tests")):
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

from app import admin_api as aa            # noqa: E402
from app import appkeys as ak              # noqa: E402
from app import errors as errc             # noqa: E402
from synth_services import synth_matrix, synth_services_dict  # noqa: E402

# 隔离修复（2026-10-15）：stub 只服务上方被测模块 import——装完即拆，防假 fastapi
# 残留 sys.modules 污染单进程全量跑的后续套件（实证：test_proxy_hub 的
# importorskip("fastapi") 被残留假模块误判）。admin_api 已缓存
# sys.modules['app.admin_api']，其内 stub 类引用随模块存活，本套件不受影响。
for _k in _STUB_KEYS:
    sys.modules.pop(_k, None)
del _STUB_KEYS

ADMIN_JS = REPO / "admin" / "appkeys.js"
TRIAL_JS = REPO / "portal" / "assets" / "trial.js"
SM3_JS = TRIAL_JS.parent / "sm3.min.js"
TRIAL_PAGE = REPO / "portal" / "trial.html"

NOW = datetime.now(timezone.utc)   # 时间炸弹修复（2026-10-15）：原钉 2026-10-08＋7 天效期，跨日即 credential_expired 抢跑 A2 的 service_not_granted 断言（c3508bd 门禁实证）；动态取当前 → 效期恒为「今天+7」，时间无关。
NEW_SVC = "v1-svc-news-hots"           # 场景：签发后才发布上线的「新服务」
NEW_FIELDS = ["news_id", "title", "tags"]


def _services_with_new_svc() -> dict:
    """合成注册表＋一个新发布服务（形状沿 synth news 条目；仅键存在即可被判注册）。"""
    svcs = dict(synth_services_dict())
    entry = json.loads(json.dumps(svcs.get("news_tag_query") or {}))
    entry["service_id"] = NEW_SVC
    svcs[NEW_SVC] = entry
    return svcs


class _FakeRedis:
    async def incr(self, key): return 1

    async def expire(self, key, ttl): return True


class _Req:
    """stub 直调请求体（admin_api.issue 只用 app.state／headers／client／json）。"""

    def __init__(self, payload, *, services, matrix, path):
        self._p = payload
        self.headers = {}
        self.client = None
        self.app = SimpleNamespace(state=SimpleNamespace(
            redis=_FakeRedis(), services=services, auth_matrix=matrix, appkeys_path=Path(path)))

    async def json(self):
        return self._p


def _tmp_registry() -> Path:
    tmp = tempfile.mkdtemp(prefix="p3-auth-loop-")
    path = Path(tmp) / "appkeys.yaml"
    path.write_text("keys: []\n", encoding="utf-8")
    return path


def _with_admin_stubs():
    """网关 consumer＋热加载打桩（真链路归容器 e2e；本套件只验授权闭环判定）。"""
    orig_put = aa.gw_consumer.put_consumer
    orig_reload = aa._reload

    async def _put(appkey, quota_qps=None, quota_daily=None):
        return True

    aa.gw_consumer.put_consumer = _put
    aa._reload = lambda request: {}
    return orig_put, orig_reload


def _restore_admin_stubs(orig_put, orig_reload):
    aa.gw_consumer.put_consumer = orig_put
    aa._reload = orig_reload


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


# ── A.【功能】按服务签发/授权 ──────────────────────────────────────────

def test_issue_endpoint_grants_new_service_then_query_gate_allows():
    """A1：携 grants 签发 → 落盘 → resolve_grant 放行新服务（查询 200 的授权闸）。"""
    path = _tmp_registry()
    payload = {
        "tenant": "partner-demo-1", "caller_name": "演示调用方",
        "app_name": "资讯热点试用", "key_type": "sandbox",
        "tag_sets": ["行为域"], "quota_qps": 50, "quota_daily": 10000,
        "grants": [{"service": NEW_SVC, "endpoint": "/v1/svc/news-hots",
                    "fields": list(NEW_FIELDS)}],
    }
    st = _with_admin_stubs()
    try:
        resp = asyncio.run(aa.issue(_Req(payload, services=_services_with_new_svc(),
                                         matrix=synth_matrix(), path=path)))
    finally:
        _restore_admin_stubs(*st)
    assert resp.status_code == 200, f"按服务签发应 200：{_body(resp)}"
    body = _body(resp)
    assert body["appkey"].startswith("sb_") and body["secret"].startswith("sk_")
    # 权威源落盘：记录携显式 grants（端点透传未被丢弃）
    doc = ak.load_appkeys(path)
    rec = doc["keys"][0]
    assert [g["service"] for g in rec["grants"]] == [NEW_SVC]
    assert rec["grants"][0]["fields"] == NEW_FIELDS
    # 查询链路授权闸（main._caller_grant 的同一判定函数）：新服务放行＝200 路径
    grant = ak.resolve_grant(doc["keys"], body["appkey"], NEW_SVC)
    assert grant["fields"] == set(NEW_FIELDS) and grant["tenant"] == "partner-demo-1"


def test_issue_without_grants_cannot_reach_new_service():
    """A2（修前缺陷口径）：不带 grants 只继承试用集 → 新服务 403 service_not_granted。"""
    rec, _ = ak.issue_appkey({"keys": []}, synth_matrix(), _services_with_new_svc(),
                             tenant="partner-demo-1", app_name="资讯热点试用",
                             key_type="sandbox", now=NOW)
    try:
        ak.resolve_grant([rec], rec["appkey"], NEW_SVC)
        raise AssertionError("继承试用集的凭证不应能调新服务（授权边界必须仍拒）")
    except ak.TagQueryError as exc:
        assert exc.status == 403 and exc.code == errc.SERVICE_NOT_GRANTED


def test_explicit_grants_validation_intact():
    """A3：显式授权校验不放松——未注册服务 400；空字段 400；未授权服务仍 403。"""
    try:
        ak.issue_appkey({"keys": []}, synth_matrix(), synth_services_dict(),
                        tenant="partner-demo-1", app_name="x", key_type="sandbox",
                        grants=[{"service": "no-such-svc", "fields": ["a"]}], now=NOW)
        raise AssertionError("未注册服务应 400 unknown_service")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 400 and exc.code == errc.UNKNOWN_SERVICE
    try:
        ak.issue_appkey({"keys": []}, synth_matrix(), _services_with_new_svc(),
                        tenant="partner-demo-1", app_name="x", key_type="sandbox",
                        grants=[{"service": NEW_SVC, "fields": []}], now=NOW)
        raise AssertionError("空字段应 400 empty_fields")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 400 and exc.code == errc.EMPTY_FIELDS


def test_grants_view_resolves_proxy_namespace():
    """C4（2026-10-20）：授权列表按 kind 双源解析——代理显示服务名/路由而非裸 id。

    proxy 命名空间来自 upstreams.yaml 已发布条目（kind=proxy、fields=[]）；
    _grants_view 收到合并后的命名空间后应解析 name/route，字段数如实 0。
    """
    doc = {"keys": [{"appkey": "sb_1234567890abcdef",
                     "grants": [{"service": "proxy-local-x", "endpoint": None, "fields": []},
                                {"service": "news_tag_query", "endpoint": None,
                                 "fields": ["news_id"]}]}]}
    services = {"proxy-local-x": {"kind": "proxy", "name": "资产标签代理",
                                  "route": "/proxy/local/x", "fields": []},
                "news_tag_query": {"name": "资讯标签", "route": "/v1/svc/news-tag"}}
    view = aa._grants_view(doc, services)
    rows = list(view.values())[0]
    proxy = [r for r in rows if r["service"] == "proxy-local-x"][0]
    normal = [r for r in rows if r["service"] == "news_tag_query"][0]
    assert proxy == {"service": "proxy-local-x", "serviceName": "资产标签代理",
                     "endpoint": "/proxy/local/x", "kind": "proxy", "fieldCount": 0}
    assert normal == {"service": "news_tag_query", "serviceName": "资讯标签",
                     "endpoint": "/v1/svc/news-tag", "fieldCount": 1}
    assert "kind" not in normal, "普通服务保持旧形状（兼容既有视图契约）"


def _node(script: str, *files: Path) -> str:
    r = subprocess.run(["node", "-e", script, *map(str, files)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_admin_form_grants_pure_functions():
    """A4a：签发表单纯函数——发布视图归一（draft 剔除/id 兼容）＋ grants 请求体形状。"""
    script = (
        "const fs=require('fs');\n"
        "(0,eval)(fs.readFileSync(process.argv[1],'utf8'));\n"
        "const O3=globalThis.O3;\n"
        "const svcs=O3.normalizePublishedServices({services:[\n"
        " {serviceId:'v1-svc-a',serviceName:'A',routePath:'/v1/svc/a',fieldSpace:'news',status:'published',fields:['f1','f2']},\n"
        " {id:'v1-svc-b',name:'B',route:'/v1/svc/b',status:'draft',fields:['g1']},\n"
        " {serviceId:'',serviceName:'坏行',fields:[]},\n"
        "]});\n"
        "const pub=O3.publishedOnly(svcs);\n"
        "console.log(JSON.stringify({\n"
        " ids:svcs.map(s=>s.id), pubIds:pub.map(s=>s.id),\n"
        " g1:O3.grantsFromForm(['v1-svc-a'],{'v1-svc-a':['f2']},pub),\n"
        " g2:O3.grantsFromForm(['v1-svc-a'],{'v1-svc-a':[]},pub)\n"
        "}));\n"
    )
    out = json.loads(_node(script, ADMIN_JS))
    assert out["ids"] == ["v1-svc-a", "v1-svc-b"], "空 id 行须剔除；id/serviceId 双口径兼容"
    assert out["pubIds"] == ["v1-svc-a"], "draft 服务不得进入签发授权列表"
    assert out["g1"] == [{"service": "v1-svc-a", "endpoint": "/v1/svc/a", "fields": ["f2"]}]
    assert out["g2"] == [], "未勾字段的服务须从 grants 剔除（后端 empty_fields 前置拦）"


def test_admin_form_and_mirror_dom_contract():
    """A4b：表单 DOM 契约（源页＋门户镜像）——按服务授权区块与提交通路在场。"""
    html = (REPO / "admin" / "appkeys.html").read_text(encoding="utf-8")
    assert 'id="svc-grant-section"' in html and 'id="svc-grant-list"' in html
    assert 'id="svc-grant-note"' in html and "toggleField(" in html
    js = ADMIN_JS.read_text(encoding="utf-8")
    assert "publish: '/v1/admin/publish'" in js, "签发表单须读已发布服务列表"
    assert "body.grants = grants" in js, "grants 须随表单提交（live 签发请求体）"
    mirror = (REPO / "portal" / "admin" / "appkeys.html").read_text(encoding="utf-8")
    assert 'id="svc-grant-section"' in mirror, "门户镜像未同步（须重跑 build_portal.py）"
    # P3 弹窗卡（维护者 4）：签发成功可见落点补配额口径（弹窗内行＋留痕条句）
    assert 'id="secret-quota-value"' in html and "quotaText" in js, "签发弹窗须有配额行"
    assert "如需调整联系管理员" in js, "签发成功文案须带配额提示句"
    assert 'id="secret-quota-value"' in mirror, "门户镜像缺配额行（须重跑 build_portal.py）"
    assert 'class="modal"' not in html, "弹窗面板类不得叫 .modal（vendor 碰撞锁死回归）"


# ── B.【门面批 2026-10-08】接口调试台（原试用台重设计） ────────────────────

_TRIAL_HARNESS = """
// trial.js 以真模块加载（module.exports = {TrialAPI, trialPanel}；浏览器全局由页面挂）。
globalThis.window = globalThis;
globalThis.document = { addEventListener: function(){} };
const mod = require(process.argv[1]);
globalThis.TrialAPI = mod.TrialAPI;
const out = {};
(async () => {
  // 场景1：取数层 passThrough——401 错误信封原样带回＋只发一次请求
  let calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push([String(url), opts && opts.method, opts && opts.headers && opts.headers.apikey]);
    return { ok: false, status: 401,
      headers: { get: (h) => h.toLowerCase() === 'x-trace-id' ? 'tr-401' : null },
      json: async () => ({ error: 'auth_failed', message: '未携带或无效的 apikey 请求头',
                           docs_url: 'errors.html#auth_failed' }) };
  };
  const r1 = await globalThis.TrialAPI.gatewayCall('/v1/svc/usr-basic-info', { key: 'a'.repeat(64) }, { apikey: 'sb_test' });
  out.api = { status: r1.status, ok: r1.ok, err: r1.json && r1.json.error,
              msg: r1.json && r1.json.message, docs: r1.json && r1.json.docs_url,
              tid: r1.traceId, latMs: typeof r1.latencyMs === 'number',
              fetchCount: calls.length, method: calls[0] && calls[0][1],
              sentKey: calls[0] && calls[0][2] };

  // 组件工厂直调（不依赖 Alpine/DOM）：注入一个已发布服务＋schema，再走 invoke
  const P = mod.trialPanel;
  const svc = { id: 'v1-svc-x', serviceName: 'X', routePath: '/v1/svc/x',
                status: 'published', keyType: 'phone_sm3',
                fields: ['age_band', 'city_code'],
                loadSource: { table: 't', cf: 'cf', keyType: 'phone_sm3', version: '20261008' } };

  // 场景2：服务端 403 → 错误态＋result 置空＋信封入态
  const p1 = P();
  p1.services = [svc]; p1.serviceId = svc.id; p1.schemaFields = [];
  p1.schemaState = 'error';
  p1.apikey = 'sb_test'; p1.keyInput = 'b'.repeat(64); p1.state = 'idle';
  globalThis.fetch = async () => ({ ok: false, status: 403, headers: { get: () => 'tr-403' },
    json: async () => ({ error: 'service_not_granted', message: '租户未获服务授权',
                         docs_url: 'errors.html#service_not_granted' }) });
  p1.invoke();
  await new Promise(r => setTimeout(r, 30));
  out.err403 = { state: p1.state, result: p1.result, status: p1.httpStatus,
                 errCode: p1.err && p1.err.error, hasMsg: !!(p1.err && p1.err.message),
                 hasDocs: !!(p1.err && p1.err.docs_url), tid: p1.traceId,
                 rows: p1.resultRows().length };

  // 场景3：200 命中 → 结果行携 schema 中文释义；无 schema 字段如实留空
  const p2 = P();
  p2.services = [svc]; p2.serviceId = svc.id;
  p2.schemaFields = [{ name: 'age_band', type: 'varchar', cnName: '年龄段', desc: '' }];
  p2.schemaState = 'ok';
  p2.apikey = 'sb_test'; p2.keyInput = 'c'.repeat(64); p2.state = 'idle';
  globalThis.fetch = async () => ({ ok: true, status: 200, headers: { get: () => null },
    json: async () => ({ service_id: 'v1-svc-x', found: true,
                         fields: { age_band: 'D', extra_no_schema: '1' },
                         data_as_of: '2026-10-08', live_version: '20261008' }) });
  p2.invoke();
  await new Promise(r => setTimeout(r, 30));
  const rows = p2.resultRows();
  out.ok200 = { state: p2.state, status: p2.httpStatus, rows: rows.length,
                cn: (rows.find(x => x.name === 'age_band') || {}).cnName,
                noSchemaCn: (rows.find(x => x.name === 'extra_no_schema') || {}).cnName };

  // 场景4：draft 服务不得进入下拉（published() 过滤）＋出参表形状＋curl 随路由生成
  const p3 = P();
  p3.services = [svc, { id: 'draft-1', serviceName: 'D', routePath: '/v1/svc/d',
                        status: 'draft', fields: ['z'] }];
  p3.serviceId = svc.id;
  p3.schemaFields = [{ name: 'age_band', type: 'varchar(16)', cnName: '年龄段', desc: '' }];
  p3.schemaState = 'ok';
  out.view = { pub: p3.published().map(s => s.id),
               outFields: p3.outFields(),
               endpoint: p3.endpointPath(),
               curl: p2.curlCmd().split('\\n')[0] };
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""

TRIAL_OUT = json.loads(_node(_TRIAL_HARNESS, TRIAL_JS).strip())


def test_trial_gateway_call_passthrough_no_fallback():
    """B1：真实网关调用 passThrough——401 错误信封原样带回；只发一次请求、零 mock 回落。"""
    api = TRIAL_OUT["api"]
    assert api["status"] == 401 and api["ok"] is False, "非 2xx 应答须原样带回（passThrough）"
    assert api["err"] == "auth_failed" and api["msg"], "错误信封 error/message 须原样带回"
    assert api["docs"] == "errors.html#auth_failed", "docs_url（错误码中心链接）须原样带回"
    assert api["tid"] == "tr-401" and api["latMs"], "traceId 与耗时须带回"
    assert api["fetchCount"] == 1, "调用只发一次请求（不得二次请求取替代数据）"
    assert api["method"] == "POST" and api["sentKey"] == "sb_test", "POST＋apikey 请求头须随调用发出"


def test_trial_component_error_state_no_fake_table():
    """B2a：服务端 403 → error 态＋result 置空（结果表零行）＋信封/追踪号入态。"""
    e = TRIAL_OUT["err403"]
    assert e["state"] == "error", "服务端错误须进错误态（不得按 ok 渲染结果表）"
    assert e["result"] is None, "result 必须置空（不渲染假数据）"
    assert e["status"] == 403 and e["errCode"] == "service_not_granted"
    assert e["hasMsg"] and e["hasDocs"], "错误信封 message/docs_url 须入态"
    assert e["tid"] == "tr-403" and e["rows"] == 0, "追踪号入态＋结果表零行"


def test_trial_component_ok_renders_with_schema_cn():
    """B2b：200 命中 → 结果行带 schema 中文释义；无 schema 字段如实留空（不编造）。"""
    o = TRIAL_OUT["ok200"]
    assert o["state"] == "ok" and o["status"] == 200 and o["rows"] == 2
    assert o["cn"] == "年龄段", "结果行中文释义须来自 schema 元数据"
    assert o["noSchemaCn"] == "", "schema 缺字段时释义留空（不得编造）"


def test_trial_component_view_contract():
    """B2c：下拉只列已发布服务；出参表=字段/类型/中文释义；curl 随服务路由生成。"""
    v = TRIAL_OUT["view"]
    assert v["pub"] == ["v1-svc-x"], "draft 服务不得进入下拉（status=published 过滤）"
    assert v["outFields"] == [   # schema 命中→类型/释义；缺字段→'—' 如实占位（不编造）
        {"name": "age_band", "type": "varchar(16)", "cnName": "年龄段"},
        {"name": "city_code", "type": "—", "cnName": "—"},
    ]
    assert v["endpoint"] == "/v1/svc/x", "路由由所选服务带出"
    assert "curl" in v["curl"] and "-X POST" in v["curl"], "curl 示例随所选服务生成"


def test_trial_page_console_dom():
    """B3：产物 DOM——三区骨架＋错误信封区块在场；旧话术与旧区块零残留；深链锚保留。"""
    html = TRIAL_PAGE.read_text(encoding="utf-8")
    for block in ("trial-controls", "trial-service-select", "trial-apikey",
                  "trial-api-info", "trial-endpoint", "trial-param-table",
                  "trial-fields-table", "trial-invoke", "trial-key-input",
                  "trial-invoke-btn", "trial-result", "trial-error-envelope",
                  "trial-error-code", "trial-error-message", "trial-error-docs-link",
                  "trial-raw-json"):
        assert f'id="{block}"' in html, f"接口调试台缺关键区块 id：{block}"
    for stale in ("自动降级", "调试历史", "SDK 包", "手机号", "demo-badge", "演示数据"):
        assert stale not in html, f"旧话术/旧区块残留：{stale}"
    assert 'id="trial-tab-http"' in html and 'id="trial-tab-sdk"' in html, \
        "历史兼容锚（#trial-tab-http/#trial-tab-sdk）须保留（docs 深链已随页下线）"


def test_trial_js_real_gateway_contract():
    """B4：源码契约——真实网关调用（postJsonRaw passThrough），零 mock/演示回落。"""
    src = TRIAL_JS.read_text(encoding="utf-8")
    assert "gatewayCall" in src and "postJsonRaw" in src, "调用须走 passThrough 取数层"
    assert "/v1/admin/publish'" in src, "服务下拉数据源须为 GET /v1/admin/publish"
    assert "/v1/admin/load/schema" in src, "出参表数据源须为表级 schema 元数据端点"
    for banned in ("trialDemoQuery", "trialTagsQuery", "mock/", "channel === 'demo'",
                   "localStorage"):
        assert banned not in src, f"trial.js 残留旧演示/历史机制：{banned}"

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
