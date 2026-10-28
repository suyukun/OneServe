#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 型代理中枢套件（2026-10-15 本地自转发闭环卡）——纯逻辑＋本地 http.server，无 docker 依赖。

覆盖口径（卡 §5：上游注册/转发/审计/错误路径）：
  ① 注册消费：upstreams.yaml 已发布条目→路由匹配（最长前缀/子路径/draft-offline 拒）；
  ② 转发：GET/POST＋白名单头＋体透传（状态/类型/体原样回传）、重试仅传输层失败；
  ③ 审计：透传调用留痕（endpoint＝service:<route>＋retention 后缀；参数＝子路径）；
  ④ 错误路径：上游不可达→502 upstream_unreachable 信封；超时→504；未发布→404
     proxy_not_found；鉴权未登记→403（grant 注入桩断言调用）；
  ⑤ 网关渲染：published→PUT（key-auth/request-id/错误信封/upstream_id 同源形状），
     offline/draft→DELETE；
  ⑥ 试连 probe：可达（含 4xx＝网通）／不可达／超时三态。

FastAPI 依赖的 HTTP 编排（create_router/TestClient）在宿主无 fastapi 时跳过
（同 test_error_envelope.py 口径；容器内全量跑）。
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

import yaml  # noqa: E402

from app import proxy_hub as hub  # noqa: E402
from app.kv_executor import TagQueryError  # noqa: E402

try:
    import pytest
except ImportError:  # 直跑口径：以类收集器替代 pytest.fixture/mark
    pytest = None


# ---------- 测试登记表（tmp 文件写入读出，走 ops_config 同一解析器） ----------

ENTRY = {
    "id": "proxy-local-usr-asset-tag", "service_name": "用户资产标签代理（本地自转发）",
    "route": "/proxy/local/usr-asset-tag", "method": "POST",
    "upstream_url": "http://127.0.0.1:1/v1/query/v1-svc-usr-asset-tag",  # 端口由用例改写
    "timeout_sec": 5, "retry_count": 1, "reuse_upstream": False,
    "upstream_name": "proxy-local-usr-asset-tag-up", "status": "published",
    "scope": {"env": "本机M0", "instance": "网关实例A"},
    "last_modified_by": "t", "last_modified_at": "2026-10-15T00:00:00+00:00",
    "published_at": "2026-10-15T00:00:00+00:00",
    "trace": {"action": "published", "by": "t", "at": "2026-10-15T00:00:00+00:00",
              "commit": None},
}

DRAFT = dict(ENTRY, id="proxy-local-draft", route="/proxy/local/draft",
             status="draft", service_name="草稿条目")


def write_registry(tmp_path, entries):
    p = tmp_path / "upstreams.yaml"
    p.write_text(yaml.safe_dump({"meta": {"updated": None, "updated_by": None},
                                 "proxies": entries}, allow_unicode=True), encoding="utf-8")
    return p


# ---------- ① 注册消费 ----------

def test_match_proxy_prefix_and_subpath(tmp_path):
    p = write_registry(tmp_path, [ENTRY, DRAFT])
    entries = hub.published_entries(p)
    assert len(entries) == 1, "draft 不得进数据面"
    e, sub = hub.match_proxy("/proxy/local/usr-asset-tag", entries)
    assert e is not None and sub == "", "1:1 端点形态：无子路径"
    e2, sub2 = hub.match_proxy("/proxy/local/usr-asset-tag/extra/path", entries)
    assert e2["id"] == ENTRY["id"] and sub2 == "extra/path", "子路径透传"


def test_match_proxy_longest_prefix_and_miss(tmp_path):
    longer = dict(ENTRY, id="proxy-local-usr-asset-tag-ext",
                  route="/proxy/local/usr-asset-tag/ext")
    p = write_registry(tmp_path, [ENTRY, longer])
    entries = hub.published_entries(p)
    e, sub = hub.match_proxy("/proxy/local/usr-asset-tag/ext/deep", entries)
    assert e["id"] == "proxy-local-usr-asset-tag-ext", "最长前缀优先"
    miss, _ = hub.match_proxy("/proxy/other/thing", entries)
    assert miss is None, "未登记路由＝无命中"


def test_build_target_url_empty_and_bad_sub():
    url = hub.build_target_url(ENTRY, "", "")
    assert url == ENTRY["upstream_url"], "空子路径＝登记 upstream_url 原样（1:1 端点）"
    assert hub.build_target_url(ENTRY, "a/b", "k=1").endswith("/a/b?k=1")
    for bad in ("a/../b", "a//b", "a@b", "http://x", "a b"):
        try:
            hub.build_target_url(ENTRY, bad, "")
            raise AssertionError(f"越界子路径 {bad!r} 应拒")
        except TagQueryError as exc:
            assert exc.status == 403 and exc.code == "proxy_traversal_blocked"


# ---------- #37 防穿越白名单（2026-10-15 复审 P2-4：黑名单正则→白名单字符集） ----------

def test_subpath_whitelist_accepts():
    """合法形态全放行：普通段/多段/数字点位/下划线连字符点号。"""
    for ok in ("extra/path", "v1/query/x", "a_b-c.d", "seg/12.3", "tag1014"):
        assert hub.subpath_ok(ok), f"合法子路径 {ok!r} 须放行"


def test_subpath_whitelist_rejects_traversal_variants():
    """负路径矩阵：../、..%2f 解码后、编码变体裸串、scheme/host/@/:/空白/反斜杠/双斜线。"""
    bad = [
        "..", "../etc/passwd", "a/../../etc/passwd", "a/../b",
        "..%2f..%2fetc", "%2e%2e/%2e%2e/etc",          # 未解码形态（含 % 即白名单外）
        "..\..\windows", "http://evil.com/x",
        "a b", "a\u0009b", "user@host", "localhost:8080",
        "a//b", "?query=1", "#frag", "a%00b",           # NUL/控制编码变体
    ]
    for s in bad:
        assert not hub.subpath_ok(s.strip("/")), f"越界形态 {s!r} 须拒"
    # 前导斜杠：build_target_url 先 strip 再判——"/../../etc/passwd" 剥成上跳段仍拒（不因 strip 漏网）
    try:
        hub.build_target_url(ENTRY, "/../../etc/passwd", "")
        raise AssertionError("前导斜杠上跳须拒")
    except TagQueryError as exc:
        assert exc.status == 403 and exc.code == "proxy_traversal_blocked"


# ---------- ②③④ 转发/审计/错误路径（本地 http.server 真桩） ----------

class _Upstream(BaseHTTPRequestHandler):
    seen = []          # (method, path, headers dict, body)

    def _respond(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _canon_headers(self):
        # HTTP/1.1 头大小写不敏感（RFC 7230 §3.2）：留痕按小写规范键——客户端送
        # apikey/Apikey/APIKEY 皆同值命中；同名多行折叠（罕见，够用口径）。
        return {k.lower(): v for k, v in self.headers.items()}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        type(self).seen.append((self.command, self.path, self._canon_headers(), body))
        if self.path.endswith("/boom"):          # 上游应答 500＝透传（非传输层，不重试）
            self._respond(500, b'{"error":"upstream_five_hundred"}')
        elif self.path.endswith("/all-405"):     # POST 也 405（无 Allow）＝双 405 失配
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path.endswith("/validate-400"):  # 参数校验拒绝空探测（查询型 POST 常态）
            self._respond(400, b'{"detail":"body required"}')
        elif self.path.endswith("/validate-422"):  # FastAPI 校验拒绝＝422
            self._respond(422, b'{"detail":[{"loc":["body"],"msg":"field required"}]}')
        else:
            self._respond(200, b'{"code":"0","data":{"record":{"id":"PT-1"}}}')

    def do_GET(self):
        type(self).seen.append((self.command, self.path, self._canon_headers(), b""))
        if self.path.endswith("/post-only"):     # bug7：405 + Allow 头 → probe 建议改用 POST
            self.send_response(405)
            self.send_header("Allow", "POST, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith(("/strict-post", "/all-405",
                               "/validate-400", "/validate-422")):
            # 2026-10-21 复刻 维护者 实测：FastAPI 4xx 不带 Allow 头（探测误报缺陷链）
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.endswith("/gone"):          # 404：可达但不存在＝unreachable（不伪装通过）
            self._respond(404, b'{"detail":"Not Found"}')
            return
        self._respond(200, b'{"ok":true}')

    def log_message(self, *a):  # 静默
        pass


def _start_upstream():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_forward_passthrough_and_audit_shape(tmp_path):
    if pytest is not None:
        pytest.importorskip("fastapi")
    srv = _start_upstream()
    try:
        port = srv.server_address[1]
        entry = dict(ENTRY, upstream_url=f"http://127.0.0.1:{port}/v1/query/x",
                     retry_count=0)
        audits = []

        async def fake_audit(caller, endpoint, params, status, latency_ms, body, trace_id=""):
            audits.append(dict(caller=caller, endpoint=endpoint, params=params,
                               status=status, body=body, trace_id=trace_id))

        async def ok_grant(caller, sid):
            return {"tenant": "demo_bank"}

        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(hub.create_router(
            grant_check=ok_grant, audit_call=fake_audit, upstreams_path=write_registry(tmp_path, [entry])))
        client = TestClient(app)
        resp = client.post("/proxy/local/usr-asset-tag",
                           headers={"apikey": "sb_test", "X-Trace-Id": "trace-1",
                                    "Cookie": "leak=1"},
                           json={"key": "abc"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["code"] == "0", "上游体原样透传"
        # 上游侧：方法/路径/白名单头到达；Cookie 不转发（防头注入通道）
        method, path, headers, body = _Upstream.seen[-1]
        assert method == "POST" and path == "/v1/query/x"
        assert headers.get("apikey") == "sb_test"
        assert headers.get("x-trace-id") == "trace-1"
        assert "cookie" not in headers
        assert json.loads(body)["key"] == "abc", "请求体透传"
        # 审计：透传留痕（endpoint=service:<route>?retention=off；caller=apikey）
        assert len(audits) == 1
        a = audits[0]
        assert a["endpoint"] == "service:/proxy/local/usr-asset-tag?retention=off"
        assert a["caller"] == "sb_test" and a["status"] == 200
        assert a["body"]["upstream_status"] == 200
        # 留存开关缺省关＝零落盘
        assert a["body"]["retained_path"] is None
    finally:
        srv.shutdown()


def test_forward_errors_404_403_502(tmp_path):
    if pytest is not None:
        pytest.importorskip("fastapi")
    dead_port = _free_port()
    entry = dict(ENTRY, upstream_url=f"http://127.0.0.1:{dead_port}/x", retry_count=0)
    p = write_registry(tmp_path, [entry])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def audit(*a, **k):
        pass

    def denied(caller, sid):
        raise TagQueryError(403, "service_not_granted", "未登记授权")

    app = FastAPI()
    app.include_router(hub.create_router(
        grant_check=lambda c, s: ({}), audit_call=audit, upstreams_path=p))
    client = TestClient(app)
    # 未发布路由 → 404 proxy_not_found 信封
    r = client.post("/proxy/nope/thing", headers={"apikey": "k"})
    assert r.status_code == 404 and r.json()["error"] == "proxy_not_found"
    # 鉴权未登记 → 403（grant 桩拒绝路径）
    app2 = FastAPI()
    app2.include_router(hub.create_router(grant_check=denied, audit_call=audit,
                                          upstreams_path=p))
    r2 = TestClient(app2).post("/proxy/local/usr-asset-tag", headers={"apikey": "k"})
    assert r2.status_code == 403 and r2.json()["error"] == "service_not_granted"
    # 上游不可达（死端口）→ 502 upstream_unreachable 信封
    r3 = client.post("/proxy/local/usr-asset-tag", headers={"apikey": "k"}, json={"key": "1"})
    assert r3.status_code == 502, r3.text
    assert r3.json()["error"] == "upstream_unreachable"


def test_forward_retries_transport_only(tmp_path):
    """重试口径：传输层失败（502）重试 1+retry 次；上游已应答 5xx 不重试。"""
    srv = _start_upstream()
    try:
        port = srv.server_address[1]
        entry = dict(ENTRY, upstream_url=f"http://127.0.0.1:{port}/boom", retry_count=2)
        calls = []

        async def flaky(url, t, **kw):
            calls.append(url)
            if len(calls) < 3:
                raise TagQueryError(502, "upstream_unreachable", "x")
            return {"status": 200, "content_type": "application/json", "body": b"{}"}

        async def always_dead(url, t, **kw):
            calls.append(url)
            raise TagQueryError(502, "upstream_unreachable", "dead")

        result = asyncio.run(hub.forward(flaky, entry, "u", "POST", {}, b""))
        assert result["status"] == 200 and len(calls) == 3, "第 3 次成功（1+2 次尝试）"
        calls.clear()
        try:
            asyncio.run(hub.forward(always_dead, entry, "u", "POST", {}, b""))
            raise AssertionError("持续不可达应熔断抛出")
        except TagQueryError as exc:
            assert exc.status == 502 and len(calls) == 3
    finally:
        srv.shutdown()


# ---------- ⑤ 网关渲染（纯函数＋桩 Admin API） ----------

def test_proxy_route_shape():
    r = hub.proxy_route(ENTRY)
    assert r["name"] == "oneserve-px-proxy-local-usr-asset-tag"
    assert r["uri"] == "/proxy/local/usr-asset-tag"
    assert r["methods"] == ["POST"] and r["upstream_id"] == "oneserve-service"
    assert "key-auth" in r["plugins"] and "request-id" in r["plugins"]
    assert "serverless-post-function" in r["plugins"], "网关错误信封钩子同源"


def test_push_proxy_routes_put_and_delete(tmp_path):
    offline = dict(ENTRY, id="proxy-local-off", route="/proxy/local/off",
                   status="offline")
    p = write_registry(tmp_path, [ENTRY, DRAFT, offline])
    ops = []

    def fake_admin(admin, key, method, path, body):
        ops.append((method, path))
        return 200

    async def run():
        # 桩掉 to_thread 的真网络：直接替换 rhot._admin_req
        orig = hub.rhot._admin_req
        hub.rhot._admin_req = fake_admin
        try:
            async def fake_to_thread(fn, *a):
                return fn(*a)
            orig_to_thread = hub.asyncio.to_thread
            hub.asyncio.to_thread = fake_to_thread
            try:
                return await hub.push_proxy_routes(p)
            finally:
                hub.asyncio.to_thread = orig_to_thread
        finally:
            hub.rhot._admin_req = orig

    ok, bad = asyncio.run(run())
    assert (ok, bad) == (3, 0), "published PUT＋draft/offline DELETE 各自幂等"
    puts = [o for o in ops if o[0] == "PUT"]
    dels = {o[1] for o in ops if o[0] == "DELETE"}
    assert puts == [("PUT", "routes/oneserve-px-proxy-local-usr-asset-tag")], "仅已发布 PUT"
    assert "routes/oneserve-px-proxy-local-off" in dels, "下线条目摘流"
    assert any("draft" in d for d in dels), "草稿同样不占网关"


# ---------- ⑥ 试连 probe ----------

def test_probe_tristate_2xx_method_mismatch_and_dead():
    """bug7（2026-10-20）三态＋2026-10-21 显式方法单步口径。

    发布闸「ok 且通过态」：ok_2xx／ok_post_confirmed 放行；5xx 保留原状态码但
    ok=false（可达不等于可用，发布要求 2xx）。
    """
    srv = _start_upstream()
    try:
        port = srv.server_address[1]
        ok = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/v1/query/x", timeout_sec=3))
        assert ok["ok"] is True and ok["verdict"] == "ok_2xx"
        assert ok["status"] == 200 and ok["latencyMs"] >= 0
        assert ok["methodUsed"] == "GET" and ok["forwardMethod"] == "GET"
        assert ok["probeSeq"] == "GET"
        # 非 2xx（500）：保留状态码与耗时，但不再算通过（显式 POST 单步）
        err = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/boom", method="POST",
                                    timeout_sec=3))
        assert err["ok"] is False and err["verdict"] == "unreachable"
        assert err["status"] == 500 and err["latencyMs"] >= 0, "原状态码/耗时须保留"
        # 显式 GET 探 405 + Allow: POST, OPTIONS → 方法不匹配并建议 POST（单步，不擅自复探）
        mm = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/post-only", method="GET",
                                   timeout_sec=3))
        assert mm["ok"] is False and mm["verdict"] == "method_mismatch"
        assert mm["status"] == 405 and mm["suggestedMethod"] == "POST"
        assert "上游只接受 POST" in mm["message"]
        assert mm["probeSeq"] == "GET", "显式方法单步，序列不得含 POST"
    finally:
        srv.shutdown()
    dead = asyncio.run(hub.probe(f"http://127.0.0.1:{_free_port()}/x", timeout_sec=2))
    assert dead["ok"] is False and dead["verdict"] == "unreachable"
    assert dead["errorKind"] == "upstream_unreachable"


def test_probe_auto_two_step_get405_then_post():
    """2026-10-21 两步判定（维护者 实测误报缺陷链修复）：AUTO GET 405 → POST 复探。

    - POST 2xx ＝ 通过（ok_2xx，methodUsed 如实记两步 "GET→POST"）；
    - POST 400/422（参数校验拒绝空探测）＝ ok_post_confirmed：上游在线、POST 方法
      已确认，发布闸放行；状态码保留真值不造假；
    - POST 也 405（无 Allow）才判 method_mismatch（「上游对 GET/POST 均 405」）；
    - 404／超时仍 unreachable；建议方法仅在真有 Allow 头时给出。
    """
    srv = _start_upstream()
    try:
        port = srv.server_address[1]
        # POST 2xx：GET 405（无 Allow）→ POST 复探 200 ＝ 通过，将用 POST 转发
        ok = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/strict-post",
                                   method="AUTO", timeout_sec=3))
        assert ok["ok"] is True and ok["verdict"] == "ok_2xx"
        assert ok["status"] == 200
        assert ok["methodUsed"] == "GET→POST" and ok["forwardMethod"] == "POST"
        assert ok["probeSeq"] == "GET→405→POST", "技术详情须如实显示两步序列"
        assert ok["allow"] == "" and ok["suggestedMethod"] == "", "无 Allow 头不得造建议"
        # POST 400：参数校验拒绝空探测 ＝ 上游在线、POST 已确认（放行，状态码保真）
        c400 = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/validate-400",
                                     method="AUTO", timeout_sec=3))
        assert c400["ok"] is True and c400["verdict"] == "ok_post_confirmed"
        assert c400["status"] == 400 and c400["forwardMethod"] == "POST"
        assert "参数校验拒绝" in c400["message"] and "在线" in c400["message"]
        # POST 422：同上（FastAPI 校验拒绝形态）
        c422 = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/validate-422",
                                     method="AUTO", timeout_sec=3))
        assert c422["ok"] is True and c422["verdict"] == "ok_post_confirmed"
        assert c422["status"] == 422 and c422["forwardMethod"] == "POST"
        # POST 也 405（无 Allow）：双 405 才判失配，文案「上游对 GET/POST 均 405」
        mm = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/all-405",
                                   method="AUTO", timeout_sec=3))
        assert mm["ok"] is False and mm["verdict"] == "method_mismatch"
        assert mm["suggestedMethod"] == "", "无 Allow 头不给建议"
        assert "均 405" in mm["message"]
        assert mm["probeSeq"] == "GET→405→POST"
        # 404：可达但不存在＝unreachable（不伪装通过）
        nf = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/gone",
                                   method="AUTO", timeout_sec=3))
        assert nf["ok"] is False and nf["verdict"] == "unreachable"
        assert nf["status"] == 404
        # 405 带 Allow（历史 /post-only）：AUTO 两步下 POST 复探 200 直接转通过
        auto = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/post-only",
                                     method="AUTO", timeout_sec=3))
        assert auto["ok"] is True and auto["verdict"] == "ok_2xx"
        assert auto["forwardMethod"] == "POST" and auto["probeSeq"] == "GET→405→POST"
    finally:
        srv.shutdown()
    # 连不上／超时：unreachable 口径不变
    dead = asyncio.run(hub.probe(f"http://127.0.0.1:{_free_port()}/strict-post",
                                 method="AUTO", timeout_sec=2))
    assert dead["ok"] is False and dead["verdict"] == "unreachable"
    assert dead["errorKind"] == "upstream_unreachable"
    assert dead["methodUsed"] == "GET", "第一步未过未复探，序列如实记 GET"


def test_probe_respects_explicit_configured_method():
    """配置流已选定方法时 probe 尊重配置方法，不擅自换方法复探（修复要求3）。"""
    srv = _start_upstream()
    try:
        port = srv.server_address[1]
        # 配置 POST：单步直探 2xx ＝ 通过，序列无 GET 步
        ok = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/strict-post",
                                   method="POST", timeout_sec=3))
        assert ok["ok"] is True and ok["verdict"] == "ok_2xx"
        assert ok["methodUsed"] == "POST" and ok["probeSeq"] == "POST"
        assert ok["forwardMethod"] == "POST"
        # 配置 GET 探 POST-only（无 Allow）：单步判失配，无建议、不擅自复探 POST
        mm = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/strict-post",
                                   method="GET", timeout_sec=3))
        assert mm["ok"] is False and mm["verdict"] == "method_mismatch"
        assert mm["suggestedMethod"] == "" and "未返回 Allow 头" in mm["message"]
        assert mm["probeSeq"] == "GET", "显式方法不得擅自复探另一方法"
        # 显式 GET 探 405 带 Allow：suggest_method 仅在真有 Allow 头时用
        mm2 = asyncio.run(hub.probe(f"http://127.0.0.1:{port}/post-only",
                                    method="GET", timeout_sec=3))
        assert mm2["verdict"] == "method_mismatch" and mm2["suggestedMethod"] == "POST"
    finally:
        srv.shutdown()


# ---------- 宿主直跑入口（同套件口径：断言一致） ----------

if __name__ == "__main__":
    import tempfile
    failures = 0
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        try:
            if "tmp_path" in fn.__code__.co_varnames[:fn.__code__.co_argcount]:
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"PASS {name}")
        except ImportError as exc:
            print(f"SKIP {name}（{exc}）")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__} {exc}")
    sys.exit(1 if failures else 0)
