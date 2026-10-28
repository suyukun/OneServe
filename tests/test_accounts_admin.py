#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号管理代理套件（W5 · 2026-10-15 设计稿 B2/B3/B5，tests/test_accounts_admin.py）。

守卫口径（设计稿 B5：契约 AST＋Casdoor fake 单测＋live 节真建/禁/删 _live_skip 口径）：
  1. 契约 AST（纯 stdlib 宿主机可跑）：/v1/admin/accounts/* 六端点注册在案；
     router 级挂 admin_api._admin_gate（与 admin_api/monitor_api 同一挂点——W1 墙
     on 时写方法 401、写闸双保险保留）；角色闸 require_role('admin') 语义在位
     （W4 合流前为本地 admin tag 校验，联调项）；
  2. 纯逻辑：claims admin tag 宽容识别（tag/roles/role/isAdmin 四形态）；新建校验
     （账号正则/密码长度/角色三选一）；角色标签人话；
  3. Casdoor fake 单测（fastapi/httpx 在场才跑；缺则如实降级标注不假绿）：
     五件代理（get-users/add-user/update-user/set-password/delete-user）逐件出
     入参契约＋审计触发＋软删默认/物理删显式＋fail-closed（不可达 502
     upstream_unreachable，不降级不出假数据）＋角色闸（on 态 viewer 403
     role_forbidden、admin 过、off 态放行）；
  4. live 节（栈可达才跑；main.py 尚未挂载本 router＝联调项——404 显式 skip
     「待后端线合流」，ONE_SERVE_LIVE=1 强制 fail）。

口径：错误码复用既有码（invalid_request/unauthorized/upstream_unreachable）；
role_forbidden 归 W4 注册（就绪前按设计稿字面码）；密码只在代理转发中出现，
不落审计（审计参数剔除 password 字段）。
运行：python3 tests/test_accounts_admin.py（或 python3 -m pytest tests/test_accounts_admin.py）。
"""
from __future__ import annotations

import ast
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

# ADR-0027 步1（登录双模）：本套件＝oidc 回归——钉住 AUTH_PROVIDER=oidc。缺省
# native 会让 verify_access_token 分派本地 HS256，本套件自造的 RS256/JWKS 桩
# token 将全部被拒；oidc 链路原语义在本钉住下逐条守卫（native 面归 test_auth_native）。
os.environ["AUTH_PROVIDER"] = "oidc"

from app import errors as E            # noqa: E402
from app import admin_accounts as AA   # noqa: E402

MODULE = REPO / "service" / "app" / "admin_accounts.py"
if not MODULE.is_file():   # 容器态复跑（./service 挂载为 /app，模块位于 /app/app/）
    MODULE = REPO / "app" / "admin_accounts.py"

try:
    from fastapi import FastAPI
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    _HAS_FASTAPI = False

try:
    from fastapi.testclient import TestClient   # TestClient 需 httpx（容器/宿主均可能缺）
    _HAS_TESTCLIENT = _HAS_FASTAPI
except Exception:   # noqa: BLE001
    _HAS_TESTCLIENT = False

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    _HAS_JWT = True
except Exception:   # noqa: BLE001
    _HAS_JWT = False

BASE = os.environ.get("ONESERVE_LIVE_BASE", "http://localhost:8000")


class _LiveSkipped(Exception):
    """裸跑口径的显式跳过信号（test_auth_oidc 同款）。"""


def _live_skip(reason: str):
    if os.environ.get("ONE_SERVE_LIVE") == "1":
        raise AssertionError(f"[ONE_SERVE_LIVE=1] live 前置不满足，强制失败：{reason}")
    if os.environ.get("PYTEST_CURRENT_TEST"):
        import pytest
        pytest.skip(reason)
    raise _LiveSkipped(reason)


# ---------- 1. 契约 AST ----------

def test_accounts_contract_ast():
    """六端点注册在 admin_accounts.router；router 级挂 _admin_gate；审计/角色闸在位。"""
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    routes: dict[str, set] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.value.id == "router" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                routes.setdefault(str(dec.args[0].value), set()).add(dec.func.attr.upper())
    expect = {
        "/v1/admin/accounts/users": {"GET", "POST"},
        "/v1/admin/accounts/users/{name}/status": {"POST"},
        "/v1/admin/accounts/users/{name}/role": {"POST"},
        "/v1/admin/accounts/users/{name}/password": {"POST"},
        "/v1/admin/accounts/users/{name}/delete": {"POST"},
    }
    for path, methods in expect.items():
        got = routes.get(path, set())
        assert methods <= got, f"缺端点 {methods - got} {path}（实得 {got}）"
    src = MODULE.read_text(encoding="utf-8")
    # Casdoor 五件代理全部在案（设计稿 B3；get-users 为前缀拼接形态按前缀匹配）
    for api in ("/api/get-users", "/api/add-user", "/api/update-user",
                "/api/set-password", "/api/delete-user"):
        assert api in src, f"缺 Casdoor 代理：{api}"
    # router 级挂闸（新端点自动带闸的防漏挂设计，admin_api 同款）
    assert "APIRouter(dependencies=[Depends(_admin_gate)])" in src, "router 须路由级挂 _admin_gate"
    # 角色闸：优先接 W4 require_role（factory 在场即切换）＋本地后备在位
    assert "def _require_admin" in src and "claims_is_admin" in src
    assert 'getattr(auth_oidc, "require_role", None)' in src, \
        "须优先接 W4 require_role('admin')（本卡任务口径）"
    assert 'factory("admin")(request)' in src, "接通形态：require_role('admin') 依赖直调"
    # fail-closed：不可达映射 502 upstream_unreachable；审计操作人＝JWT sub
    assert "errors.UPSTREAM_UNREACHABLE" in src
    assert 'claims.get("sub")' in src
    print(f"  ok test_accounts_contract_ast（6 端点＋Casdoor 五件＋挂闸/角色闸形态）")


# ---------- 2. 纯逻辑 ----------

def test_claims_is_admin_tag_only():
    """claims admin 判定＝tag 单一口径（收口批 2a 收窄·W4 定稿 ADR-0016 Decision.2：
    tokenFormat=JWT 内嵌 User，顶层 tag claim 即角色；与 auth_oidc.role_from_claims
    同法 strip+lower 归一）——roles/role/isAdmin 等其余 claim 一律不信（联调项 4 收口）。"""
    for claims in ({"tag": "admin"}, {"tag": " ADMIN "}, {"tag": "Admin"}):
        assert AA.claims_is_admin(claims), f"tag=admin（归一后）须识别：{claims}"
    for claims in ({}, {"tag": "viewer"}, {"tag": "operator"}, {"tag": "role:admin"},
                   {"tag": None}, {"roles": ["admin"]}, {"role": "admin"},
                   {"isAdmin": True}, {"isAdmin": False}):
        assert not AA.claims_is_admin(claims), f"非 tag 口径须不信（收窄后）：{claims}"


def test_validate_new_user():
    """新建校验：账号正则/密码长度/角色三选一；缺省角色 viewer、显示名回退账号名。"""
    name, display, password, role = AA._validate_new_user(
        {"name": "zhangsan", "password": "secret123", "role": "operator"})
    assert (name, display, password, role) == ("zhangsan", "zhangsan", "secret123", "operator")
    for bad, why in (({"name": "a", "password": "secret123"}, "账号过短须拒"),
                     ({"name": "坏名字", "password": "secret123"}, "非法字符须拒"),
                     ({"name": "zhangsan", "password": "12345"}, "密码过短须拒"),
                     ({"name": "zhangsan", "password": "secret123", "role": "root"}, "角色越界须拒")):
        try:
            AA._validate_new_user(bad)
            raise AssertionError(why)
        except AA._CasdoorError as exc:
            assert exc.status == 400, why
    assert AA._validate_reset_password({"password": "abc123"}) == "abc123"
    try:
        AA._validate_reset_password({})
        raise AssertionError("重置密码缺字段须拒")
    except AA._CasdoorError as exc:
        assert exc.status == 400


def test_user_view_and_audit_hygiene():
    """列表视图只出展示字段；审计参数剔除 password（审计摘要不含明文口令）。"""
    view = AA._user_view({"name": "tester", "displayName": "测试", "email": "t@x",
                          "tag": "operator", "isForbidden": True, "isDeleted": False,
                          "createdTime": "2026-10-15T00:00:00+08:00", "password": "x"})
    assert view["role"] == "operator" and view["isForbidden"] is True and "password" not in view
    assert AA._user_view({"name": "n", "isAdmin": True})["role"] == "admin"
    assert AA._operator({"sub": "oneserve/tester"}) == "oneserve/tester"
    assert AA._operator({}) == "anonymous"


# ---------- 3. Casdoor fake 单测（TestClient） ----------

class _FakeCasdoor:
    """Casdoor 管理通道替身：按真栈实测契约（收口批 2a 2026-10-15）应答。

    线上契约（对真栈容器逐端点核对所得，与生产代理同规）：
      get-user     GET  /api/get-user?id=<owner>/<name>          → data＝全量对象|None
      get-users    GET  /api/get-users?owner=<org>               → data＝[全量对象]
      add-user     POST JSON 全字段
      update-user  POST /api/update-user?id=<owner>/<name>（id 走 query）＋body＝全量对象
      set-password POST form 体（userOwner/userName/newPassword；JSON 体不识别）
      delete-user  POST body＝全量用户对象（id-only body 在真栈＝静默 no-op）
    记录：calls＝[(api, body|form)]（线序）；queries＝对齐的 query id（未编码）。
    """

    def __init__(self, fail=False):
        self.calls: list[tuple[str, dict]] = []
        self.queries: list[str] = []
        self.users: list[dict] = [{"name": "tester", "displayName": "测试用户", "email": "t@x",
                                   "tag": "viewer", "isForbidden": False, "isDeleted": False,
                                   "createdTime": "2026-10-14T00:00:00+08:00"}]
        self.fail = fail

    def opener(self):
        fake = self

        class _Resp:
            def __init__(self, obj):
                self._obj = obj

            def read(self):
                return json.dumps(self._obj).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, req, timeout=8):
                if fake.fail:
                    raise urllib.error.URLError("casdoor unreachable")
                url = str(req.get_full_url())
                path, _, qs = url.partition("?")
                q = dict(urllib.parse.parse_qsl(qs))
                if path.endswith("/api/get-users"):
                    fake.calls.append(("/api/get-users", {}))
                    fake.queries.append(q.get("owner", ""))
                    return _Resp({"status": "ok", "data": fake.users})
                if path.endswith("/api/get-user"):
                    hit = next((dict(u) for u in fake.users
                                if u.get("name") == str(q.get("id", "")).split("/")[-1]), None)
                    return _Resp({"status": "ok", "data": hit})
                if path.endswith("/api/add-user"):
                    body = json.loads((req.data or b"{}").decode())
                    fake.calls.append(("/api/add-user", body))
                    fake.queries.append("")
                    fake.users.append(dict(body, isForbidden=False, isDeleted=False))
                    return _Resp({"status": "ok"})
                if path.endswith("/api/update-user"):
                    body = json.loads((req.data or b"{}").decode())
                    fake.calls.append(("/api/update-user", body))
                    fake.queries.append(q.get("id", ""))
                    name = str(q.get("id", "")).split("/")[-1]
                    for u in fake.users:
                        if u.get("name") == name:
                            u.update(body)
                    return _Resp({"status": "ok"})
                if path.endswith("/api/set-password"):
                    form = dict(urllib.parse.parse_qsl((req.data or b"").decode()))
                    fake.calls.append(("/api/set-password", form))
                    fake.queries.append("")
                    return _Resp({"status": "ok"})
                if path.endswith("/api/delete-user"):
                    body = json.loads((req.data or b"{}").decode())
                    fake.calls.append(("/api/delete-user", body))
                    fake.queries.append(q.get("id", ""))
                    fake.users = [u for u in fake.users if u.get("name") != body.get("name")]
                    return _Resp({"status": "ok"})
                return _Resp({"status": "error", "msg": "unknown path " + url})

        return _Opener()


def _accounts_app():
    app = FastAPI()
    app.include_router(AA.router)
    return app


def _install_fake(fake: _FakeCasdoor, monkey_env: bool = True):
    """替换进程内会话缓存为 fake opener（测试卫生：套件尾还原）。"""
    AA._session.update({"opener": fake.opener(), "at": time.time()})
    return lambda: AA._reset_session()


def test_accounts_fake_casdoor_end_to_end():
    """五件代理契约（fake Casdoor）：列表/建/禁/角色/改密/软删·硬删＋审计触发。"""
    if not _HAS_TESTCLIENT:
        pytest.skip("依赖缺席：fastapi.testclient/httpx（fake Casdoor 端到端）")
    fake = _FakeCasdoor()
    cleanup = _install_fake(fake)
    client = TestClient(_accounts_app())
    os.environ["AUTH_OIDC_MODE"] = "off"
    try:
        # 列表：get-users 代理＋软删过滤＋视图字段
        r = client.get("/v1/admin/accounts/users")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] >= 1 and body["org"] == "oneserve"
        assert body["users"][0]["name"] == "tester" and "password" not in body["users"][0]

        # 创建：add-user 载荷（owner/organization/tag=role/isAdmin/密码在转发中）
        r = client.post("/v1/admin/accounts/users",
                        json={"name": "zhangsan", "displayName": "张三",
                              "password": "secret123", "role": "operator"})
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        api, payload = fake.calls[-1]
        assert api == "/api/add-user"
        assert payload["owner"] == "oneserve" and payload["tag"] == "operator"
        assert payload["isAdmin"] is False and payload["password"] == "secret123"
        assert payload["signupApplication"] == "oneserve-admin"

        # 禁用→update-user isForbidden（真栈契约：id 走 query＋全量对象 body）
        r = client.post("/v1/admin/accounts/users/tester/status", json={"forbidden": True})
        assert r.status_code == 200 and r.json()["action"] == "disable"
        api, payload = fake.calls[-1]
        assert api == "/api/update-user" and payload["isForbidden"] is True
        assert payload["name"] == "tester" and payload.get("displayName"), \
            "body 须为全量用户对象（真栈缺字段拒改）"
        assert fake.queries[-1] == "oneserve/tester", "定位 id 须走 query 参数"

        # 角色分配→update-user tag（admin 随 isAdmin=true）
        r = client.post("/v1/admin/accounts/users/tester/role", json={"role": "admin"})
        assert r.status_code == 200 and r.json()["action"] == "set_role"
        api, payload = fake.calls[-1]
        assert payload["tag"] == "admin" and payload["isAdmin"] is True

        # 改密→set-password（form 体：userOwner/userName/newPassword；真栈 JSON 体不识别）
        r = client.post("/v1/admin/accounts/users/tester/password",
                        json={"password": "temp-pass-9"})
        assert r.status_code == 200 and r.json()["action"] == "password_reset"
        api, payload = fake.calls[-1]
        assert api == "/api/set-password"
        assert payload == {"userOwner": "oneserve", "userName": "tester",
                           "newPassword": "temp-pass-9"}

        # 软删（默认）→update-user isDeleted，delete-user 不被调用
        delete_calls = [c for c, _ in fake.calls if c == "/api/delete-user"]
        r = client.post("/v1/admin/accounts/users/zhangsan/delete", json={})
        assert r.status_code == 200 and r.json()["action"] == "soft_delete"
        api, payload = fake.calls[-1]
        assert api == "/api/update-user" and payload["isDeleted"] is True
        assert fake.queries[-1] == "oneserve/zhangsan"
        assert [c for c, _ in fake.calls if c == "/api/delete-user"] == delete_calls

        # 硬删（显式）→delete-user 代理（全量对象 body；真栈 id-only body＝静默 no-op）
        r = client.post("/v1/admin/accounts/users/zhangsan/delete", json={"hard": True})
        assert r.status_code == 200 and r.json()["action"] == "purge"
        assert fake.calls[-1][0] == "/api/delete-user"
        assert fake.calls[-1][1]["name"] == "zhangsan", "delete-user 须全量对象 body"
        assert all(u["name"] != "zhangsan" for u in fake.users), "硬删须真删（防静默 no-op 回潮）"

        # 校验拒绝：400 invalid_request（不触 Casdoor）
        before = len(fake.calls)
        r = client.post("/v1/admin/accounts/users",
                        json={"name": "x", "password": "secret123"})
        assert r.status_code == 400 and r.json()["error"] == E.INVALID_REQUEST
        assert len(fake.calls) == before, "校验拒绝不得触达 Casdoor"
        print(f"  ok fake Casdoor 五件代理端到端（{len(fake.calls)} 次代理调用）")
    finally:
        cleanup()
        os.environ["AUTH_OIDC_MODE"] = "off"


def test_accounts_fail_closed_and_role_gate():
    """fail-closed（不可达 502 upstream_unreachable）＋角色闸（on viewer 403
    role_forbidden／on admin 过闸／off 放行）。on 态 token 用自造 RSA（无 pyjwt
    降级标注——闸语义与 test_auth_oidc 同款，不假绿）。"""
    if not _HAS_TESTCLIENT:
        pytest.skip("依赖缺席：fastapi.testclient/httpx（fail-closed/角色闸 TestClient 层）")
    os.environ["AUTH_OIDC_MODE"] = "off"

    # fail-closed：Casdoor 不可达 → 502 upstream_unreachable（off 态闸放行仍不可达）
    fake = _FakeCasdoor(fail=True)
    cleanup = _install_fake(fake)
    try:
        client = TestClient(_accounts_app())
        r = client.get("/v1/admin/accounts/users")
        assert r.status_code == 502 and r.json()["error"] == E.UPSTREAM_UNREACHABLE, r.text
        print("  ok fail-closed（Casdoor 不可达 → 502 upstream_unreachable）")
    finally:
        cleanup()

    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（角色闸 on 态）")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def issue(tag="viewer", sub="oneserve/tester"):
        import base64 as b64
        from cryptography.hazmat.primitives import serialization
        pub = key.public_key().public_numbers()
        raw = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
        jwk = {"kty": "RSA", "kid": "k1", "alg": "RS256",
               "n": b64.urlsafe_b64encode(raw).rstrip(b"=").decode(), "e": "AQAB"}
        AA._jwks_patched = True
        orig = AA.auth_oidc._fetch_jwks
        AA.auth_oidc._jwks_cache.update({"keys": [jwk], "at": time.time()})
        AA.auth_oidc._fetch_jwks = lambda url: {"keys": [jwk]}
        return orig, jwt.encode({"iss": "http://localhost:8000", "sub": sub, "tag": tag,   # iss 与 auth_oidc._cfg 缺省同源（1b 批·词条③对齐）
                                 "aud": "oneserve-admin-client",
                                 "exp": int(time.time()) + 600}, key, algorithm="RS256",
                                headers={"kid": "k1"})

    fake2 = _FakeCasdoor()
    cleanup2 = _install_fake(fake2)
    client = TestClient(_accounts_app())
    os.environ["AUTH_OIDC_MODE"] = "on"
    orig_fetch = None
    try:
        orig_fetch, tok_v = issue("viewer")
        h = {"Authorization": "Bearer " + tok_v}
        r = client.get("/v1/admin/accounts/users", headers=h)
        assert r.status_code == 403, r.text
        assert r.json()["error"] == AA.ROLE_FORBIDDEN_CODE, r.json()
        assert "admin" in r.json()["message"]

        orig_fetch2, tok_a = issue("admin")
        r = client.get("/v1/admin/accounts/users",
                       headers={"Authorization": "Bearer " + tok_a})
        assert r.status_code == 200, r.text
        print("  ok 角色闸（on 态 viewer 403 role_forbidden／admin 过闸）")
    finally:
        AA.auth_oidc._fetch_jwks = orig_fetch or AA.auth_oidc._fetch_jwks
        cleanup2()
        os.environ["AUTH_OIDC_MODE"] = "off"

    # off 态匿名放行（单测/裸跑口径，与 require_login 同语义）
    fake3 = _FakeCasdoor()
    cleanup3 = _install_fake(fake3)
    try:
        r = TestClient(_accounts_app()).get("/v1/admin/accounts/users")
        assert r.status_code == 200, "off 态须恒放行"
        print("  ok off 态恒放行")
    finally:
        cleanup3()


def test_accounts_endpoints_dependency_level():
    """依赖级端点直测（免 fastapi/httpx，处处可跑——test_auth_oidc._FakeRequest 同款）：
    直接 await 端点函数，验编排契约（载荷塑形/错误映射/软删默认），路由形态由
    AST 断言覆盖；端点函数定义在 fastapi 分支内——宿主机缺 fastapi 时如实降级
    标注（容器内复跑；httpx 缺席不受影响）。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（依赖级端点直测）")
    import asyncio

    class _FakeState:
        oidc_claims = {}
        trace_id = ""

    class _FakeApp:
        class state:
            audit = None

    class _FakeRequest:
        def __init__(self, body=None, params=None, headers=None):
            self._body = body or {}
            self.headers = headers or {}
            self.state = _FakeState()
            self.app = _FakeApp()
            self.query_params = params or {}

        async def json(self):
            if self._body is None:
                raise ValueError("bad json")
            return self._body

    fake = _FakeCasdoor()
    cleanup = _install_fake(fake)
    os.environ["AUTH_OIDC_MODE"] = "off"
    try:
        # 列表
        resp = asyncio.run(AA.accounts_list_users(_FakeRequest()))
        body = json.loads(resp.body)
        assert resp.status_code == 200 and body["users"][0]["name"] == "tester", body
        # 创建载荷塑形
        resp = asyncio.run(AA.accounts_create_user(
            _FakeRequest({"name": "zhangsan", "displayName": "张三",
                          "password": "secret123", "role": "operator"})))
        assert resp.status_code == 200, resp.body
        api, payload = fake.calls[-1]
        assert api == "/api/add-user" and payload["tag"] == "operator"
        assert payload["password"] == "secret123" and payload["owner"] == "oneserve"
        # 禁用/角色/改密/软删/硬删
        resp = asyncio.run(AA.accounts_set_status("tester", _FakeRequest({"forbidden": True})))
        assert resp.status_code == 200 and fake.calls[-1][0] == "/api/update-user"
        assert fake.calls[-1][1]["isForbidden"] is True
        resp = asyncio.run(AA.accounts_set_role("tester", _FakeRequest({"role": "admin"})))
        assert resp.status_code == 200 and fake.calls[-1][1]["tag"] == "admin"
        resp = asyncio.run(AA.accounts_reset_password(
            "tester", _FakeRequest({"password": "temp-pass-9"})))
        assert resp.status_code == 200 and fake.calls[-1][0] == "/api/set-password"
        assert fake.calls[-1][1] == {"userOwner": "oneserve", "userName": "tester",
                                     "newPassword": "temp-pass-9"}
        n_delete = len([c for c, _ in fake.calls if c == "/api/delete-user"])
        resp = asyncio.run(AA.accounts_delete_user("zhangsan", _FakeRequest({})))
        assert resp.status_code == 200 and json.loads(resp.body)["action"] == "soft_delete"
        assert fake.calls[-1][0] == "/api/update-user" and fake.calls[-1][1]["isDeleted"] is True
        assert len([c for c, _ in fake.calls if c == "/api/delete-user"]) == n_delete
        resp = asyncio.run(AA.accounts_delete_user("zhangsan", _FakeRequest({"hard": True})))
        assert json.loads(resp.body)["action"] == "purge" and fake.calls[-1][0] == "/api/delete-user"
        # 校验拒绝：400 人话信封且不触 Casdoor
        n = len(fake.calls)
        resp = asyncio.run(AA.accounts_create_user(_FakeRequest({"name": "x", "password": "s"})))
        assert resp.status_code == 400 and json.loads(resp.body)["error"] == E.INVALID_REQUEST
        assert len(fake.calls) == n
        # 非法 JSON 体：按缺字段 400（不炸 500）
        resp = asyncio.run(AA.accounts_create_user(_FakeRequest(None)))
        assert resp.status_code == 400
        # 角色闸依赖级：on 态 viewer token → 403（无 pyjwt 则降级标注）
        if _HAS_JWT:
            import base64 as b64
            from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
            key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
            from cryptography.hazmat.primitives import serialization
            pub = key.public_key().public_numbers()
            raw = pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")
            jwk = {"kty": "RSA", "kid": "k1", "alg": "RS256",
                   "n": b64.urlsafe_b64encode(raw).rstrip(b"=").decode(), "e": "AQAB"}
            orig = AA.auth_oidc._fetch_jwks
            AA.auth_oidc._jwks_cache.update({"keys": [jwk], "at": time.time()})
            AA.auth_oidc._fetch_jwks = lambda url: {"keys": [jwk]}
            tok = jwt.encode({"iss": "http://localhost:8000", "sub": "oneserve/tester",   # iss 与 auth_oidc._cfg 缺省同源（1b 批·词条③对齐）
                              "tag": "viewer", "aud": "oneserve-admin-client",
                              "exp": int(time.time()) + 600}, key, algorithm="RS256",
                             headers={"kid": "k1"})
            os.environ["AUTH_OIDC_MODE"] = "on"
            try:
                req = _FakeRequest({}, headers={"authorization": "Bearer " + tok})
                # W4 require_role 就绪＝异常形态（RoleForbiddenError→main handler
                # 出 403 信封）；旧树后备＝直接返 403 信封。两形态语义一致，皆可。
                caught_403 = None
                try:
                    resp = asyncio.run(AA._require_admin(req))
                    if resp is not None:
                        caught_403 = resp
                except AA.auth_oidc.RoleForbiddenError as exc:
                    assert exc.min_role == "admin", exc.min_role
                if caught_403 is not None:
                    assert caught_403.status_code == 403, "viewer 须 403"
                    assert json.loads(caught_403.body)["error"] == AA.ROLE_FORBIDDEN_CODE
            finally:
                AA.auth_oidc._fetch_jwks = orig
                os.environ["AUTH_OIDC_MODE"] = "off"
        print(f"  ok 依赖级端点直测（编排/载荷/错误映射；{len(fake.calls)} 次代理调用）")
    finally:
        cleanup()
        os.environ["AUTH_OIDC_MODE"] = "off"


def test_role_forbidden_code_alignment():
    """role_forbidden 取码口径：W4 注册 errors.ROLE_FORBIDDEN 后自动跟随；未注册按
    设计稿字面码（联调项）。信封形态恒带 error/message。"""
    assert AA.ROLE_FORBIDDEN_CODE == getattr(E, "ROLE_FORBIDDEN", "role_forbidden")
    body = E.envelope(AA.ROLE_FORBIDDEN_CODE, "需要管理员角色（admin）")
    assert body["error"] == AA.ROLE_FORBIDDEN_CODE and body["message"]
    print(f"  ok role_forbidden 取码口径（当前＝{AA.ROLE_FORBIDDEN_CODE}）")


# ---------- 4. live 节（栈可达才跑；router 未挂载＝联调项显式 skip） ----------

def _http_get(url: str, headers: dict | None = None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        except Exception:   # noqa: BLE001
            return exc.code, {}


def test_accounts_live_contract():
    """live 契约：端点已挂载（200/401/403）才断言；404＝main 未挂 router（联调项 1）
    显式 skip。ONE_SERVE_LIVE=1 时 skip 即 fail。"""
    try:
        status, _ = _http_get(BASE + "/v1/admin/auth/config", timeout=5)
    except Exception:   # noqa: BLE001
        _live_skip(f"live 契约：栈不可达（{BASE}；容器未起）")
    status, body = _http_get(BASE + "/v1/admin/accounts/users", timeout=8)
    if status == 404:
        _live_skip("live 契约：/v1/admin/accounts/* 未挂载（main.py 尚未 include "
                   "admin_accounts.router——待后端线合流后验，联调项 1）")
    if status == 401:
        _live_skip("live 契约：端点已挂载且墙 on（无 token 401 auth_required 形态正确）；"
                   "带 token 断言待合流后以 admin 真账号补跑")
    if status == 403:
        assert body.get("error") == AA.ROLE_FORBIDDEN_CODE, body
        _live_skip("live 契约：角色闸 403 形态正确（tester 非 admin）；admin 真账号断言待合流补跑")
    assert status == 200, f"意外状态 {status}：{body}"
    assert isinstance(body.get("users"), list) and "total" in body, body
    print(f"  ok live 列表契约（total={body.get('total')}）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = skipped = 0
    for fn in fns:
        try:
            fn()
        except _LiveSkipped as exc:
            skipped += 1
            print("  SKIP " + fn.__name__ + ": " + str(exc))
        except Exception as exc:   # noqa: BLE001
            failed += 1
            import traceback
            print("  FAIL " + fn.__name__ + ": " + str(exc))
            traceback.print_exc()
    print(f"{len(fns) - failed - skipped}/{len(fns)} passed, {skipped} skipped")
    sys.exit(1 if failed else 0)
