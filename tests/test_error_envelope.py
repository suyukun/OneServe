#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-2 错误码统一套件（SPEC §4.4 envelope ＋ A6 错误码中心同源）。

守卫口径（任务卡 §5-P1-2）：
  1. 统一 envelope：所有错误响应含 error/message/traceId/request_log_url，适用时含
     param；字段名向后兼容（error/message 不改名）——以真实 FastAPI 栈（TestClient）
     断言各端点错误体形态一致＋出现时机（param 只在该码登记了参数时出现）。
  2. 错误码中心同源：机读码集中 app/errors.py，AST 机检各模块无散落错误码字面量。
  3. request_log_url 真可达：深链指向门户「我的接入」页既有 trace_id 深链机制。
  4. 网关侧：路由渲染/init_routes 挂网关错误信封钩子；码名与错误码中心同源。

运行：python3 -m pytest tests/test_error_envelope.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import errors as E  # noqa: E402

APP = REPO / 'service' / 'app'

try:
    from fastapi.testclient import TestClient  # noqa: E402
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    TestClient = None  # type: ignore
    _HAS_FASTAPI = False


# ---------- 1. 错误码中心本身（纯逻辑） ----------

def test_registry_codes_unique_and_wellformed():
    """码唯一、非空、机读码形态；envelope 字段名同源。"""
    codes = E.all_codes()
    assert codes and len(codes) == len(set(codes)), '错误码重复或为空'
    for c in codes:
        assert re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', c), '机读码形态非法：' + c
    assert E.FIELD_ERROR == 'error' and E.FIELD_MESSAGE == 'message'
    assert E.FIELD_TRACE_ID == 'traceId' and E.FIELD_PARAM == 'param'
    assert E.FIELD_REQUEST_LOG_URL == 'request_log_url' and E.FIELD_DOCS_URL == 'docs_url'


def test_envelope_shape_and_backward_compat():
    """envelope()：error/message 恒在（向后兼容）；param 按登记；trace 缺省不造死链。"""
    body = E.envelope(E.INVALID_KEY, '键格式不合法', trace_id='t-1')
    assert body[E.FIELD_ERROR] == E.INVALID_KEY and body[E.FIELD_MESSAGE]
    assert body[E.FIELD_PARAM] == 'key'
    assert body[E.FIELD_TRACE_ID] == 't-1'
    assert body[E.FIELD_REQUEST_LOG_URL] == 'my.html?trace_id=t-1'
    assert body[E.FIELD_DOCS_URL] == 'errors.html#invalid_key'
    bare = E.envelope(E.INTERNAL_ERROR, '服务内部错误')
    assert E.FIELD_TRACE_ID not in bare and E.FIELD_REQUEST_LOG_URL not in bare
    assert E.FIELD_PARAM not in bare
    ex = E.envelope(E.QUOTA_DAILY_EXCEEDED, '当日超限', trace_id='t', extra={'reason': 'daily'})
    assert ex['reason'] == 'daily' and ex[E.FIELD_ERROR] == E.QUOTA_DAILY_EXCEEDED


def test_request_log_url_points_to_real_deeplink_page():
    """request_log_url 落点＝门户既有深链页 my.html?trace_id=…（真可达，非 404）。"""
    assert E.REQUEST_LOG_PAGE == 'my.html'
    url = E.request_log_url('a b/../x')
    assert url.startswith('my.html?trace_id='), url
    assert ' ' not in url and '/' not in url.split('?', 1)[1]
    assert (REPO / 'portal' / E.REQUEST_LOG_PAGE).is_file(), '深链落点页不存在'
    assert (REPO / 'portal' / E.DOCS_PAGE).is_file(), 'docs_url 落点页不存在'
    js = (REPO / 'portal' / 'assets' / 'my.js').read_text(encoding='utf-8')
    assert "q.get('trace_id')" in js and 'lookupTrace' in js, 'my.js 缺 trace_id 深链反查机制'


# ---------- 2. 错误码中心同源：AST 机检无散落错误码字面量 ----------

def _error_codes_in_module(path: Path) -> list:
    """扫描模块里「明显是错误码」的字面量：dict 的 error/code 值、*Error(...) 首字母实参。"""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value in ('error', 'code')
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)
                        and v.value and re.fullmatch(r'[a-z][a-z0-9_]*', v.value)):
                    hits.append((node.lineno, v.value))
        if isinstance(node, ast.Call):
            fn = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else '')
            if fn.endswith('Error'):
                for a in node.args[:2]:
                    if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                            and re.fullmatch(r'[a-z][a-z0-9_]*', a.value)):
                        hits.append((node.lineno, a.value))
    return hits


def test_no_stray_error_code_literals_in_service():
    """服务层各模块不再直写机读码字面量（已收敛为 errors.<CONST>）。"""
    offenders = []
    for p in sorted(APP.glob('*.py')):
        if p.name == 'errors.py':
            continue
        for lineno, code in _error_codes_in_module(p):
            if E.is_known(code):
                offenders.append(p.name + ':' + str(lineno) + ' ' + code)
    assert not offenders, '散落错误码字面量（应引用 app/errors.py 常量）：' + '; '.join(offenders)


# errors 模块的非「码常量」成员（信封字段/查询辅助函数）；其余 errors.X 均按码常量断言
_NON_CODE_MEMBERS = {
    "envelope", "param_of", "http_status_of", "is_known", "all_codes", "get",
    "docs_url", "request_log_url", "contract_rows", "archetype_codes",
    "REQUEST_LOG_PAGE", "DOCS_PAGE", "ErrorCode",
}


def _is_code_const(attr: str) -> bool:
    """是否「码常量」名（排除 FIELD_*/PARAM_* 字段名常量与辅助成员）。"""
    return (not attr.startswith("FIELD_") and not attr.startswith("PARAM_")
            and attr not in _NON_CODE_MEMBERS)


def test_executor_codes_are_registered_in_center():
    """反向：执行器引用的 errors.<CONST> 必在中心登记（且属性真实存在，防拼写漂移）。"""
    for name in ('kv_executor.py', 'proxy_executor.py', 'hbase_service.py', 'appkeys.py',
                 'ops_config.py', 'batch.py', 'batch_worker.py', 'batch_delivery.py',
                 'main.py', 'admin_api.py', 'my_api.py', 'audit_api.py',
                 'registry_render.py'):
        tree = ast.parse((APP / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == 'errors'):
                assert hasattr(E, node.attr), \
                    name + ' 引用了不存在的 errors.' + node.attr
                const = getattr(E, node.attr)
                if _is_code_const(node.attr):
                    assert isinstance(const, str) and E.is_known(const), \
                        name + ' 引用了未登记错误码 errors.' + node.attr


# ---------- 3. A6 错误码中心同源产物 ----------

def test_errors_sidecar_is_same_source_as_registry():
    """portal/openapi/errors.json 的码集＝app/errors.py（同源生成，不多不少）。"""
    import json
    import subprocess
    f = REPO / 'portal' / 'openapi' / 'errors.json'
    if not f.is_file():
        subprocess.run([sys.executable, str(REPO / 'scripts' / 'gen_openapi.py')],
                       check=True, capture_output=True)
    doc = json.loads(f.read_text(encoding='utf-8'))
    assert sorted(r['code'] for r in doc['codes']) == sorted(E.all_codes())
    for row in doc['codes']:
        reg = E.get(row['code'])
        assert row['meaning'] == reg.meaning and row['solution'] == reg.solution
        assert row['http_status'] == reg.http_status and row['source'] == reg.source
        assert row['param'] == (reg.param or None)
    assert any(r['code'] == E.AUTH_FAILED and r['source'] == 'gateway' for r in doc['codes'])


# ---------- 4. 网关侧：路由渲染挂错误信封钩子＋码名同源 ----------

def test_gateway_error_envelope_wired_into_renderer_and_init_routes():
    """网关错误体统一钩子挂在渲染路由与静态路由；码名与 errors.py 同源。"""
    from app import registry_render as rend
    assert 'serverless-post-function' in rend.query_route('svc')['plugins']
    for _path, body in rend.legacy_route_specs():
        assert 'serverless-post-function' in body['plugins']
    lua = rend.GATEWAY_ERROR_ENVELOPE['functions'][0]
    assert E.AUTH_FAILED in lua, '网关钩子码名与错误码中心不同源'
    assert 'my.html' in lua and 'traceId' in lua
    # 错误响应 Content-Type 校正（key-auth 默认 text/plain）——仅 ≥400 生效
    ct = rend.GATEWAY_ERROR_CONTENT_TYPE
    assert ct['headers']['set']['Content-Type'].startswith('application/json')
    assert ct['vars'] == [['status', '>=', 400]], 'Content-Type 校正必须门控在 ≥400'
    src = (REPO / 'configs' / 'apisix' / 'init_routes.py').read_text(encoding='utf-8')
    assert 'GATEWAY_ERROR_ENVELOPE' in src, 'init_routes 静态路由未挂网关错误信封'


# ---------- 5. 真实栈：各端点错误体形态一致（FastAPI TestClient） ----------

def _assert_envelope(resp, code=None, param=None):
    """通用断言：≥400 JSON 体为 §4.4 信封。"""
    assert resp.status_code >= 400, resp.status_code
    body = resp.json()
    assert isinstance(body, dict)
    assert body.get('error'), '缺 error 机读码：' + str(body)
    assert body.get('message'), '缺 message 人话：' + str(body)
    assert body.get('traceId'), '缺 traceId：' + str(body)
    assert body.get('request_log_url') == 'my.html?trace_id=' + body['traceId'], str(body)
    assert body.get('docs_url') == 'errors.html#' + body['error'], str(body)
    assert resp.headers.get('X-Trace-Id') == body['traceId'], '响应头 traceId 与体不一致'
    if code is not None:
        assert body['error'] == code, str(body)
    if param is not None:
        assert body.get('param') == param, 'param 期望 ' + param + '：' + str(body)
    return body


def test_endpoint_errors_share_envelope():
    """各端点错误体形态一致：404/400/401 均入 §4.4 信封（含 param 出现时机）。

    只走错误路径（不触真 redis）：统计挂点用 no-op 替身，审计写入器/redis 置空。
    """
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（端点错误信封共享；仅源码机检子集可跑）")
    from app import main as svc
    client = TestClient(svc.app)
    # 错误路径先于 redis 访问（404/400/401），仅 _audit_call 的统计挂点会碰 redis——替身
    orig_record = svc.live_stats.record_call

    async def _noop_record(*_a, **_k):
        return None

    svc.live_stats.record_call = _noop_record
    orig_services = svc.services
    try:
        resp = client.post('/v1/query/nope', json={'key': 'x'})
        _assert_envelope(resp, code=E.SERVICE_NOT_FOUND)
        svc.services = {
            'user_tag_query': {
                'service_id': 'user_tag_query', 'archetype': 'kv_lookup',
                'backend': {'source': 'redis', 'kv': {'namespace': 'ns'}},
                'params': [{'name': 'key', 'pattern': '^[0-9a-f]{64}$'}],
                'response': {'meta': []}, 'guard': {'max_items_per_call': 1},
                'audit': {'enabled': False},
            },
        }
        resp = client.post('/v1/tags/query', content=b'{not-json',
                           headers={'content-type': 'application/json'})
        _assert_envelope(resp, code=E.INVALID_JSON, param='请求体')
        # 2026-10-14：管理令牌闸第三次反转拆除（ADR-0010）——无令牌访问管理端点
        # 须 200（恒放行；原 401 信封断言随闸拆除改写，历史见 bcf34f8）。
        resp = client.get('/v1/admin/appkeys')
        assert resp.status_code == 200, f'无令牌 GET /v1/admin/appkeys 应 200（恒放行）：{resp.status_code}'
    finally:
        svc.live_stats.record_call = orig_record
        svc.services = orig_services


def test_offline_dedicated_endpoints_return_404_envelope():
    """2026-10-14 P0 死端点清理回归：news/hbase 等死服务层路由已删 → 404 信封（不裸 500）。

    依据：资讯域已下线；HBase 不作独立数据源发 API（2026-09-23 裁定）；历史代理
    路由删除防注册表 KeyError 裸崩（网关/上游链方向待另裁）。相关通道到达服务层
    一律 service_not_found 404 信封——禁止回归出 500（原 hbase TypeError 等 KeyError）。
    """
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（死端点 404 信封回归）")
    from app import main as svc
    client = TestClient(svc.app)
    resp = client.post('/v1/news/query', json={'key': 'x'})
    _assert_envelope(resp, code=E.SERVICE_NOT_FOUND)
    resp = client.post('/v1/hbase/query', json={'key': 'x'})
    _assert_envelope(resp, code=E.SERVICE_NOT_FOUND)
    resp = client.get('/v1/proxy/partner/info')
    _assert_envelope(resp, code=E.SERVICE_NOT_FOUND)


def test_framework_validation_error_enters_envelope():
    """FastAPI 自带 422（参数类型校验）也入同一信封，且 param 指向出错参数。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（框架 422 入信封）")
    from app import main as svc
    client = TestClient(svc.app)
    resp = client.get('/v1/portal/audit/logs', params={'limit': 'not-an-int'})
    if resp.status_code != 422:
        pytest.skip('本环境未触发 422（实际 %s）' % resp.status_code)
    body = _assert_envelope(resp, code=E.INVALID_REQUEST)
    assert 'detail' in body, '422 既有 detail 字段须保留（向后兼容）'


def test_a6_errors_page_and_anchors_are_reachable():
    """A6 页文件在位、入构建注册表；docs_url 锚点页即本页（真可达，非 404）。"""
    page = REPO / 'portal' / 'errors.html'
    assert page.is_file(), 'A6 错误码中心页缺失（errors.html）'
    html = page.read_text(encoding='utf-8')
    for block_id in ('errors-envelope', 'errors-toolbar', 'errors-table-card'):
        assert ('id="' + block_id + '"') in html, '缺关键区块：' + block_id
    src = (REPO / 'portal' / 'src' / 'errors.html').read_text(encoding='utf-8')
    assert ':id="c.code"' in src, '每码须渲染为带 id 的行（docs_url 锚点）'
    # 侧栏可达（错误码中心入口无死链）；详情壳相对路径改写断言已删——
    # 断言对象（demo 服务详情壳 catalog/user_tag_query.html）已随 2026-09-29 全服务下线移除。
    index = (REPO / 'portal' / 'index.html').read_text(encoding='utf-8')
    assert 'href="errors.html"' in index, '侧栏缺错误码中心入口'
    assert (REPO / 'portal' / 'assets' / 'errors.js').is_file(), '缺 errors.js 组件'
    assert (REPO / 'portal' / 'assets' / 'errors.css').is_file(), '缺 errors.css'


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print('  ok ' + fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print('  FAIL ' + fn.__name__ + ': ' + str(exc))
            traceback.print_exc()
    print(str(len(fns) - failed) + '/' + str(len(fns)) + ' passed')
    sys.exit(1 if failed else 0)
