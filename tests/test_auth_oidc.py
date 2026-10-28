#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OIDC 登录闸套件（2026-10-14 Casdoor 集成 · ADR-0010；2026-10-15 登录墙 W1/W4 升 v2 · ADR-0015/0016）。

守卫口径：
  1. token 验签单测（纯逻辑，离线）：自造 RSA 密钥对＋JWKS 注入——验签通过/篡改
     拒绝/错 issuer 拒绝/过期拒绝/未知 kid 拒绝/错 aud 拒绝·无 aud 放行记
     warning（P1-3 受众校验）；fail-closed（JWKS 不可达不降级）；
  2. 端点保护单测（需 fastapi/httpx，宿主机缺则如其它套件降级标注，不假绿）：
     AUTH_OIDC_MODE=on 时无 token POST → 401 auth_required 信封；伪造有效 token
     POST 过闸；W4 角色闸：viewer 过登录闸但越权 → 403 role_forbidden；
     mode=off 恒放行；
  3. live 集成（Casdoor 容器可达才跑；不可达 pytest.skip 显式统计可见
     （P1-1：ONE_SERVE_LIVE=1 时 skip 即 fail，CI 用））：幂等 init（组织
     oneserve/应用 oneserve-admin/测试用户 tester＋seed admin 形态用户）→
     密码流换真实 token → 验签通过 → 墙/角色语义活栈核验；
  4. 路由清单 v2（登录墙设计 A1/B5 · ADR-0015）：AST 全路由 ∈{墙域}∪{豁免}
     全覆盖分域断言（删豁免须回改地板，新端点必须分域）；每条 admin 写路由
     ROUTE_MIN_ROLE 必带 min_role 列（W4 防回潮）；活栈逐端点无 token 401 清单
     探测＋on 态站点墙/TestClient 真 app 401/403 探测＋前端 admin-gate.js 401
     拦截跳登录接线。

口径：错误码＝errors.AUTH_REQUIRED（错误码中心同源，AST 机检不散落字面量）；
测试不写共享栈 Redis；live 节只动 oneserve-casdoor 数据卷（幂等 ensure）。

运行：python3 tests/test_auth_oidc.py（或 python3 -m pytest tests/test_auth_oidc.py）。
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import os
import sys
import time
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
from app import auth_oidc as AO        # noqa: E402

_REAL_FETCH_JWKS = AO._fetch_jwks   # 测试卫生：mock 前留存真实现，live/还原用

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    _HAS_JWT = True
except Exception:                                            # noqa: BLE001
    _HAS_JWT = False

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi import APIRouter, Depends
    from app import admin_api
    _HAS_FASTAPI = True
except Exception:                                            # noqa: BLE001
    _HAS_FASTAPI = False

# live 环境口径（与 compose 缺省同源）
CASDOOR_BASE = os.environ.get("OIDC_ISSUER", "http://localhost:8000")   # 缺省随 auth_oidc 代码缺省对齐（1b 批·词条③：19000 旧口径已封死）
ORG, APP, APP_CLIENT_ID = "oneserve", "oneserve-admin", "oneserve-admin-client"
TEST_USER, TEST_PASSWORD = "tester", "***REMOVED***"
# 收口批续作（2026-10-15）：seed 缺省账号随 W6 线实修漂移——casdoor-init 按 .env 建
# oneserve/admin（tag=admin，实测密码流 200＋真栈 reload 200）；bootsadmin＝W6 前遗留、
# 已在账号管理台被禁用（isForbidden=True，密码流实测 400 invalid_grant）。缺省对齐现网。
SEED_ADMIN_USER = os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_USER", "admin") or "admin"
# 口令＝部署注入（.env，不入仓）：宿主测试进程不自动带 compose env——
# _seed_admin_password() 回落解析仓库根 .env（同值来源；文件缺失/无键时以
# 开发占位兜底，缺省账号被禁用/改密则走下方 isForbidden 显式 skip，不假绿）。
SEED_ADMIN_PASSWORD_FALLBACK = "OneserveAdmin456!"


def _dotenv_value(key: str) -> str:
    """从仓库根 .env 读一个键（行格式 KEY=VALUE，忽略注释；stdlib 零依赖）。"""
    p = Path(__file__).resolve().parents[1] / ".env"
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _seed_admin_password() -> str:
    return (os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_PASSWORD")
            or _dotenv_value("ONESERVE_BOOTSTRAP_ADMIN_PASSWORD")
            or SEED_ADMIN_PASSWORD_FALLBACK)


# ---------- 显式 SKIP（P1-1 复审修：print+return 假绿 → 统计可见的 skip） ----------

class _LiveSkipped(Exception):
    """裸跑（python3 直跑）口径的显式跳过信号：__main__ 统计为 skipped，不再计 passed。"""


def _live_skip(reason: str):
    """live 前置不满足时显式跳过（不再 print+return 假绿）。

    pytest 态：raise Skipped（-rs 可见、可统计、可 CI 区分）；裸跑态：raise
    _LiveSkipped（__main__ 归 skipped 计数）；ONE_SERVE_LIVE=1（CI live 模式）
    一律 AssertionError——live 前置缺失必须红，静默降级即回归不可拦截（P1-1）。"""
    if os.environ.get("ONE_SERVE_LIVE") == "1":
        raise AssertionError(f"[ONE_SERVE_LIVE=1] live 前置不满足，强制失败：{reason}")
    if os.environ.get("PYTEST_CURRENT_TEST"):   # pytest 运行态
        import pytest
        pytest.skip(reason)
    raise _LiveSkipped(reason)


# ---------- 1. token 验签（离线，RSA 自造密钥对） ----------

def _make_jwks_env(monkey=None):
    """造 RSA 密钥对＋JWKS，注入 _fetch_jwks；返回 (token签发函数, cleanup)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pub = key.public_key().public_numbers()
    def b64u_int(n: int) -> str:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    jwk = {"kty": "RSA", "kid": "test-kid-1", "alg": "RS256",
           "n": b64u_int(pub.n), "e": b64u_int(pub.e)}
    orig_fetch = AO._fetch_jwks
    AO._jwks_cache.update({"keys": [jwk], "at": time.time()})
    AO._fetch_jwks = lambda url: {"keys": [jwk]}

    def issue(**overrides) -> str:
        claims = {"iss": "http://localhost:8000", "sub": "oneserve/tester",   # 与 auth_oidc._cfg 缺省同源（1b 批·词条③对齐，勿再手分叉）
                  "aud": "oneserve-admin-client", "exp": int(time.time()) + 600,
                  "iat": int(time.time())}
        for k, v in overrides.items():   # None 值＝删除 claim（造「无 aud」旧版形态）
            if v is None:
                claims.pop(k, None)
            else:
                claims[k] = v
        # PyJWT 直接接受 cryptography 私钥对象（RS256 签发；验签走 JWKS 公钥路径）
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid-1"})
    return issue


def test_verify_token_roundtrip_and_rejects():
    """验签单测：好 token 过；篡改/错 iss/过期/未知 kid 拒（AuthRequiredError）。"""
    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（token 验签单测）")
    issue = _make_jwks_env()
    try:
        claims = AO.verify_access_token(issue())
        assert claims["sub"] == "oneserve/tester", "好 token 须验出 claims"

        for bad_kw, why in (
            ({"iss": "http://evil.example"}, "错 issuer 须拒"),
            ({"exp": int(time.time()) - 10}, "过期须拒"),
        ):
            try:
                AO.verify_access_token(issue(**bad_kw))
                raise AssertionError(why)
            except AO.AuthRequiredError as exc:
                assert exc.reason == "bad_token", why

        try:  # 未知 kid：JWKS 刷新后仍无此 kid → bad_token
            AO._jwks_cache.update({"keys": [], "at": 0.0})
            orig = AO._fetch_jwks
            AO._fetch_jwks = lambda url: {"keys": []}   # 刷新也拿不到该 kid
            try:
                AO.verify_access_token(issue())
                raise AssertionError("未知 kid 须拒")
            finally:
                AO._fetch_jwks = orig
        except AO.AuthRequiredError:
            pass

        try:  # 乱串非 JWT → bad_token（不裸抛解码异常）
            AO.verify_access_token("not-a-jwt")
            raise AssertionError("非 JWT 串须拒")
        except AO.AuthRequiredError:
            pass
    finally:
        AO._fetch_jwks = _REAL_FETCH_JWKS   # 测试卫生：还原真实现（防 mock 泄漏污染 live）
    print("  ok test_verify_token_roundtrip_and_rejects")


def test_audience_validation():
    """aud 受众校验（P1-3 复审修）：带 aud 必须匹配期望（AUTH_OIDC_AUDIENCE 显式
    优先，缺省=应用 clientId）；错 aud 拒（bad_token→401，同 issuer 跨应用 token
    不得过闸）；无 aud 放行（旧版 Casdoor 兼容）但服务端须记 warning（不静默）；
    数组形态 aud（当前 Casdoor 密码流实测 ["oneserve-admin-client"]）匹配过。"""
    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（aud 校验单测）")
    issue = _make_jwks_env()
    records: list = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())
    handler = _Cap()
    AO.logger.addHandler(handler)
    AO.logger.setLevel(logging.WARNING)
    try:
        # 默认期望（clientId）＋token aud 同值 → 过
        claims = AO.verify_access_token(issue())
        assert claims["sub"] == "oneserve/tester"
        # 错 aud（同 issuer 其他应用 token 形态）→ 拒 bad_token
        try:
            AO.verify_access_token(issue(aud="other-app-client"))
            raise AssertionError("错 aud 须拒（跨应用 token 不得过闸）")
        except AO.AuthRequiredError as exc:
            assert exc.reason == "bad_token", exc.reason
        # AUTH_OIDC_AUDIENCE 显式改期望：新值过、旧 clientId 拒
        os.environ["AUTH_OIDC_AUDIENCE"] = "custom-aud"
        try:
            assert AO.verify_access_token(issue(aud="custom-aud"))["sub"] == "oneserve/tester"
            try:
                AO.verify_access_token(issue())   # aud=oneserve-admin-client 已非期望
                raise AssertionError("期望 aud 切换后原 clientId 须拒")
            except AO.AuthRequiredError:
                pass
        finally:
            os.environ.pop("AUTH_OIDC_AUDIENCE", None)
        # 无 aud（旧版 Casdoor 形态）→ 放行＋warning 落日志（不静默）
        records.clear()
        assert AO.verify_access_token(issue(aud=None))["sub"] == "oneserve/tester"
        assert any("aud" in m for m in records), "无 aud 放行须记 warning（不静默）"
        # 数组形态 aud（真 Casdoor 实测形态）→ 匹配过
        assert AO.verify_access_token(issue(aud=["oneserve-admin-client"]))["sub"] == "oneserve/tester"
    finally:
        AO.logger.removeHandler(handler)
        AO._fetch_jwks = _REAL_FETCH_JWKS
    print("  ok test_audience_validation")


def test_verify_fail_closed_when_jwks_down():
    """fail-closed：JWKS 不可达（网络错）→ AuthRequiredError(jwks_unavailable)，不降级放行。"""
    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（fail-closed 单测）")
    issue = _make_jwks_env()
    tok = issue()   # 合法形态 token；但让 JWKS 通道炸掉（缓存空＋拉取抛错）
    AO._jwks_cache.update({"keys": [], "at": 0.0})
    orig = AO._fetch_jwks
    def _boom(url):
        raise OSError("jwks unreachable")
    AO._fetch_jwks = _boom
    try:
        AO.verify_access_token(tok)
        raise AssertionError("JWKS 不可达须 fail-closed 拒绝")
    except AO.AuthRequiredError as exc:
        assert exc.reason == "jwks_unavailable", exc.reason
    finally:
        AO._fetch_jwks = orig
    print("  ok test_verify_fail_closed_when_jwks_down")


# ---------- 1.5 #34 OIDC state（login-CSRF 防护；纯逻辑＋端点级） ----------

def test_state_make_and_check_roundtrip():
    """state 生成→校验通过；篡改/伪造签名/过期/缺失各拒（纯逻辑宿主机可跑）。"""
    secret = "test-secret"
    s = AO.make_state(secret)
    ok, reason = AO.check_state(s, s, secret)
    assert ok and reason == "", (ok, reason)
    # 攻击者不知密钥：同 ts 造不出合法 sig
    ts = s.partition(".")[0]
    forged = ts + "." + "0" * 16
    assert AO.check_state(forged, forged, secret) == (False, "bad_signature")
    # 不匹配（query≠cookie）＝login-CSRF 主拦截面
    assert AO.check_state("attacker-state", s, secret) == (False, "mismatch")
    assert AO.check_state("", s, secret) == (False, "mismatch")
    assert AO.check_state(s, "", secret)[1] == "missing_cookie"
    # 过期：ts 挪到 TTL 之外（签名用同一密钥算——只验过期分支）
    old_ts = str(int(time.time()) - AO._STATE_TTL_S - 30)
    old_sig = AO._state_sig(secret, old_ts)
    old = old_ts + "." + old_sig
    assert AO.check_state(old, old, secret) == (False, "expired")
    # 无点值＝mismatch 分支（got 不含 "."）；非数字 ts＝bad_format 分支
    assert AO.check_state("abc", "abc", secret) == (False, "mismatch")
    badfmt = "notanum." + "0" * 16
    assert AO.check_state(badfmt, badfmt, secret) == (False, "bad_format")
    print("  ok test_state_make_and_check_roundtrip")


def test_state_config_callback_closed_loop():
    """端点级闭环（TestClient）：config 下发一次性 state cookie → callback 正验 200、
    错 state 401 bad_state、成功换 token 后 cookie 即焚（会话内一次性）。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi/httpx（state 闭环端点测试）")
    import re as _re
    app = FastAPI()
    app.include_router(AO.router)
    client = TestClient(app)
    # ① config：state 不再静态 admin，且带 Set-Cookie（HttpOnly）
    r = client.get("/v1/admin/auth/config")
    assert r.status_code == 200, r.text[:120]
    login_url = r.json()["loginUrl"]
    m = _re.search(r"[?&]state=([^&]+)", login_url)
    assert m and m.group(1) != "admin", f"state 须为一次性随机值：{login_url[-60:]}"
    state = m.group(1)
    sc = r.headers.get("set-cookie", "")
    assert "oneserve_oidc_state=" + state in sc and "HttpOnly" in sc, sc
    # ② 重放面：真 state 但浏览器无 cookie（fail-closed）→ 401 bad_state
    client.cookies.clear()
    r = client.get("/v1/admin/auth/callback?code=fake-code&state=" + state)
    body = r.json()
    assert r.status_code == 401 and body.get("error") == E.AUTH_REQUIRED \
        and "state" in body.get("message", ""), \
        f"无 cookie 携 state 回跳（重放面）须 401 bad_state：{r.status_code} {r.text[:150]}"
    # cookie 在位但 query state 不符＝login-CSRF 核心拦截
    client.get("/v1/admin/auth/config")   # 重新下发 cookie
    r = client.get("/v1/admin/auth/callback?code=fake-code&state=attacker-value")
    assert r.status_code == 401 and "state" in r.json().get("message", ""), \
        f"state 不符须 401 bad_state 人话：{r.status_code} {r.text[:150]}"
    # ③ 正向闭环：真 Casdoor 在场则走完整 code 交换（活栈缺位时以 token 端点桩验证），
    #    成功后 cookie 必须被清除（一次性）
    cfg = AO._cfg()
    real_casdoor = False
    try:
        import urllib.request as _u
        _u.urlopen(cfg["internal"] + "/.well-known/jwks", timeout=2)
        real_casdoor = True
    except Exception:
        pass
    orig_fetch = AO._fetch_jwks
    try:
        if real_casdoor:
            # 授权码不可复用（一次性），真栈下假 code 换 token 应失败 401——
            # 但仍证明 state 校验已通过（否则错误信封是 bad_state 而非 jwks/bad_token 通道）
            r = client.get("/v1/admin/auth/callback?code=nonexistent-code&state=" + state)
            body = r.json()
            assert r.status_code == 401 and "state" not in body.get("message", ""), \
                f"state 相符时不得再报 state 错（应走到换 token 层）：{body}"
        else:
            pytest.skip("Casdoor 不可达：正向闭环以 ② 的 mismatch 拦截＋纯逻辑覆盖为准")
    finally:
        AO._fetch_jwks = orig_fetch
    print("  ok test_state_config_callback_closed_loop（一次性 state＋CSRF 拦截＋相符放行）")

# ---------- 2. 端点保护（TestClient；无 fastapi 则降级标注） ----------

def _protected_app():
    """最小受护 app：POST/GET 走真挂点 _admin_gate（admin_api 同款依赖）。

    依赖须在 router 构造期挂（新版 fastapi 的 APIRoute 在 __init__ 把 dependencies
    烘进 dependant，事后改 route.dependencies 列表不生效——2026-10-15 容器实测）。"""
    app = FastAPI()
    # 最小 app 需注册与 main 同款闸异常 handler（依赖 raise 短路 → 401/403 信封；
    # 不注册＝异常重抛，401 断言红——2026-10-15 容器实测补齐）。
    app.add_exception_handler(AO.AuthRequiredError,
                              lambda req, exc: AO.auth_required_response(exc))
    app.add_exception_handler(AO.RoleForbiddenError,
                              lambda req, exc: AO.role_forbidden_response(exc))
    router = APIRouter(dependencies=[Depends(admin_api._admin_gate)])

    @router.post("/v1/admin/appkeys")
    @router.get("/v1/admin/appkeys")
    async def probe():
        return {"ok": True}

    app.include_router(router)
    return app


class _FakeRequest:
    """依赖级测试用最小 Request 替身（require_login 只读 headers/state）。"""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.state = type("S", (), {})()


def test_require_login_dependency_gate():
    """依赖级（免 fastapi，处处可跑）：on 无 Bearer→missing_token；有效 Bearer 过；
    off 恒放行。（_admin_gate 的方法过滤 POST/PUT/DELETE 归 test_admin_gate AST 断言。）"""
    import asyncio
    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（依赖级闸单测）")
    issue = _make_jwks_env()
    os.environ["AUTH_OIDC_MODE"] = "on"
    try:
        try:
            asyncio.run(AO.require_login(_FakeRequest({})))
            raise AssertionError("on 无 Bearer 须拒")
        except AO.AuthRequiredError as exc:
            assert exc.reason == "missing_token"
        try:
            asyncio.run(AO.require_login(_FakeRequest({"authorization": "Bearer bad.token"})))
            raise AssertionError("坏 Bearer 须拒")
        except AO.AuthRequiredError as exc:
            assert exc.reason == "bad_token"
        req = _FakeRequest({"authorization": "Bearer " + issue()})
        asyncio.run(AO.require_login(req))
        assert getattr(req.state, "oidc_claims", {}).get("sub") == "oneserve/tester"
    finally:
        os.environ["AUTH_OIDC_MODE"] = "off"
    asyncio.run(AO.require_login(_FakeRequest({})))   # off 恒放行
    print("  ok test_require_login_dependency_gate")


def test_endpoint_protection_gate():
    """TestClient 全栈层（需 fastapi/httpx；宿主机缺则降级标注，不假绿——
    依赖级语义已由上一用例覆盖，本层验信封形态/GET 直通/mode=off）。"""
    if not (_HAS_FASTAPI and _HAS_JWT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt（TestClient 端点保护）")
    issue = _make_jwks_env()
    app = _protected_app()
    client = TestClient(app)

    os.environ["AUTH_OIDC_MODE"] = "on"
    try:
        r = client.post("/v1/admin/appkeys")
        assert r.status_code == 401, f"无 token 写操作须 401：{r.status_code}"
        body = r.json()
        assert body.get("error") == E.AUTH_REQUIRED, body
        assert body.get("message"), "信封须带人话 message"
        assert body.get("docs_url") == "errors.html#auth_required", "docs_url 锚点同源"

        r = client.post("/v1/admin/appkeys",
                        headers={"Authorization": "Bearer " + issue()})
        # W4：无 tag＝缺省 viewer，签发域地板 operator → 越权 403（墙/登录已过）
        assert r.status_code == 403 and r.json().get("error") == E.ROLE_FORBIDDEN, \
            f"viewer 写操作须 403 role_forbidden：{r.status_code} {r.text[:120]}"
        r = client.post("/v1/admin/appkeys",
                        headers={"Authorization": "Bearer " + issue(tag=AO.ROLE_OPERATOR)})
        assert r.status_code == 200 and r.json()["ok"] is True, "operator 级 token 须过闸+矩阵"

        r = client.post("/v1/admin/appkeys",
                        headers={"Authorization": "Bearer broken.token.here"})
        assert r.status_code == 401 and r.json().get("error") == E.AUTH_REQUIRED

        # W1 起 GET 也在墙域（读公开废除）；依赖级矩阵地板：无 token → 401
        # （adjudicate_route_role 矩阵行自带登录前置）。
        r = client.get("/v1/admin/appkeys")
        assert r.status_code == 401 and r.json().get("error") == E.AUTH_REQUIRED, \
            f"GET 运营台读不挂登录/角色地板（读公开已废除）：{r.status_code} {r.text[:120]}"
        # W4 角色闸：viewer（tag 缺省）过登录闸但越权 → 403 role_forbidden 新码
        r = client.get("/v1/admin/appkeys",
                       headers={"Authorization": "Bearer " + issue(tag=AO.ROLE_VIEWER)})
        assert r.status_code == 403 and r.json().get("error") == E.ROLE_FORBIDDEN, \
            f"viewer 越权须 403 role_forbidden：{r.status_code} {r.text[:120]}"
        assert "viewer" in r.json().get("message", "") and "operator" in r.json().get("message", ""), \
            "403 人话须含实际角色与所需角色"
        # admin 过矩阵（rank 足）→ 200
        r = client.get("/v1/admin/appkeys",
                       headers={"Authorization": "Bearer " + issue(tag=AO.ROLE_ADMIN)})
        assert r.status_code == 200, f"admin 过角色闸须 200：{r.status_code} {r.text[:120]}"
    finally:
        os.environ["AUTH_OIDC_MODE"] = "off"

    r = client.get("/v1/admin/appkeys")
    assert r.status_code == 200, "mode=off GET 恒放行（矩阵裁决 no-op）"

    r = client.post("/v1/admin/appkeys")
    assert r.status_code == 200, "mode=off 恒放行（ADR-0010 恒放行语义同源）"
    print("  ok test_endpoint_protection_gate")


def test_error_code_registered():
    """错误码中心同源：auth_required 注册在案（401）；信封字段齐全。"""
    assert E.AUTH_REQUIRED == "auth_required"
    body = E.envelope(E.AUTH_REQUIRED, "x")
    assert body[E.FIELD_ERROR] == "auth_required"
    print("  ok test_error_code_registered")


# ---------- 2.5 W4 角色模型纯逻辑（宿主机可跑；免 fastapi） ----------

def test_role_model_units():
    """W4 角色闸模型（ADR-0016）：tag→角色缺省 viewer、秩比较、403 新码登记、
    adjudicate_route_role 矩阵裁决（off＝no-op／未列＝只登录／越权＝403）。"""
    # tag claim 解析：缺/空/未知一率 viewer（最低权限兜底）；大小写归一
    assert AO.role_from_claims(None) == AO.ROLE_VIEWER
    assert AO.role_from_claims({}) == AO.ROLE_VIEWER
    assert AO.role_from_claims({"tag": ""}) == AO.ROLE_VIEWER
    assert AO.role_from_claims({"tag": "weird-role"}) == AO.ROLE_VIEWER
    assert AO.role_from_claims({"tag": "admin"}) == AO.ROLE_ADMIN
    assert AO.role_from_claims({"tag": "ADMIN"}) == AO.ROLE_ADMIN
    assert AO.role_from_claims({"tag": "operator"}) == AO.ROLE_OPERATOR
    # 三角色秩：admin > operator > viewer
    assert (AO.ROLE_RANK[AO.ROLE_ADMIN] > AO.ROLE_RANK[AO.ROLE_OPERATOR]
            > AO.ROLE_RANK[AO.ROLE_VIEWER])
    # 403 新码在错误码中心登记（403＋信封同源）
    assert E.ROLE_FORBIDDEN == "role_forbidden"
    assert E.is_known(E.ROLE_FORBIDDEN), "role_forbidden 须在错误码中心登记"
    body = E.envelope(E.ROLE_FORBIDDEN, "x")
    assert body[E.FIELD_ERROR] == "role_forbidden"
    # 矩阵表：值域全在三角色内；admin 写族（域4）与 operator 写族（域2/3）各有在位行
    assert all(v in AO.ROLE_RANK for v in AO.ROUTE_MIN_ROLE.values())
    assert AO.ROUTE_MIN_ROLE["POST /v1/admin/reload"] == AO.ROLE_ADMIN
    assert AO.ROUTE_MIN_ROLE["POST /v1/admin/appkeys"] == AO.ROLE_OPERATOR
    assert AO.ROUTE_MIN_ROLE["GET /v1/admin/metering/summary"] == AO.ROLE_VIEWER
    for _u in ("overview", "subjects", "services", "trend"):   # 1b-9 报表专区四端点族（域7）
        assert AO.ROUTE_MIN_ROLE.get(f"GET /v1/admin/usage/{_u}") == AO.ROLE_VIEWER, \
            f"GET /v1/admin/usage/{_u} 缺矩阵行或非 viewer 地板"
    # adjudicate_route_role：FakeRequest（scope.route 模板；state 预置 claims 短路）
    import asyncio

    class _R:
        def __init__(self, method, template, tag):
            self.method = method
            self.headers = {}
            self.scope = {"route": type("RT", (), {"path": template})()}
            self.state = type("S", (), {})()
            self.state.oidc_claims = {"tag": tag}   # 站点墙已验形态（短路 require_login）
            self.url = type("U", (), {"path": template})()

    os.environ["AUTH_OIDC_MODE"] = "on"
    try:
        # viewer 撞 admin 地板 → RoleForbiddenError(min=admin, actual=viewer)
        try:
            asyncio.run(AO.adjudicate_route_role(_R("POST", "/v1/admin/reload", "viewer")))
            raise AssertionError("viewer 撞 admin 地板须拒")
        except AO.RoleForbiddenError as exc:
            assert exc.min_role == "admin" and exc.actual == "viewer"
        # operator 过 operator 地板；viewer 过 viewer 地板；未列路由只登录不裁角色
        asyncio.run(AO.adjudicate_route_role(_R("POST", "/v1/admin/appkeys", "operator")))
        asyncio.run(AO.adjudicate_route_role(_R("GET", "/v1/admin/metering/summary", "viewer")))
        asyncio.run(AO.adjudicate_route_role(_R("GET", "/v1/portal/overview", "viewer")))
    finally:
        os.environ["AUTH_OIDC_MODE"] = "off"
    # off 态 no-op（viewer 撞 admin 地板也放行——恒放行口径）
    asyncio.run(AO.adjudicate_route_role(_R("POST", "/v1/admin/reload", "viewer")))
    print("  ok test_role_model_units")


def test_usage_report_routes_min_role_guard():
    """1b-9 报表专区四端点族守卫（设计稿 §7-7）：AST 枚举全部 /v1/admin/usage/* 路由，
    每条都必须在 ROUTE_MIN_ROLE 有 viewer 地板矩阵行——新增 usage 端点漏标＝红（W4 防回潮）。"""
    rows = [r for r in _enumerate_all_routes() if r[3].startswith("/v1/admin/usage/")]
    assert len(rows) >= 4, f"报表专区端点族盘点不足 4 条（overview/subjects/services/trend）：{rows!r}"
    for mod, fn, method, path in rows:
        key = f"{method} {path}"
        assert key in AO.ROUTE_MIN_ROLE, \
            f"{key}（{mod}.{fn}）缺 min_role 列（设计稿 §7-7）：在 auth_oidc.ROUTE_MIN_ROLE 域7 补矩阵行"
        assert AO.ROUTE_MIN_ROLE[key] == AO.ROLE_VIEWER, \
            f"{key} 地板非 viewer（报表专区只读面拍板＝viewer）：{AO.ROUTE_MIN_ROLE[key]!r}"


# ---------- 2.6 【无登录模式 ADR-0029·已删】W1 站点墙 on 态真栈探测 ----------
# 站点墙中间件已随无登录模式拆除（main.py 不再注册；全放行零拦截），原
# test_site_wall_on_state_probe（墙域 401/豁免放行/角色 200-403/off 恒放行）随机制
# 退役移除；依赖级语义仍由上方 require_login/角色闸用例覆盖（保留模块的休眠契约）。
# 历史实现见 git 史。


# ---------- 3. live 集成（Casdoor 容器可达才跑；不可达 SKIP 标注） ----------

def _http(url: str, data=None, headers=None, timeout=8):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _casdoor_reachable() -> bool:
    try:
        _http(CASDOOR_BASE + "/.well-known/openid-configuration")
        return True
    except Exception:                                        # noqa: BLE001
        return False


def _admin_session():
    """built-in admin 登录拿会话 cookie（开发口径，app.conf initAdminPassword 占位；生产必改）。"""
    body = json.dumps({"type": "login", "username": "admin",
                       "password": os.environ.get("CASDOOR_ADMIN_PASSWORD", "***REMOVED***"),
                       "application": "app-built-in", "organization": "built-in",
                       # 不带 autoSignIn：实测该版本带它反而 wrong-password（2026-10-14）
                       }).encode()
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(CASDOOR_BASE + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=8) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    assert out.get("status") == "ok", f"admin 登录失败：{out}"
    return opener


def _ensure_init(opener) -> None:
    """幂等 init：组织 oneserve＋应用 oneserve-admin（复制 app-built-in 改字段）＋测试用户。"""
    def api(path, payload):
        req = urllib.request.Request(
            CASDOOR_BASE + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # 组织
    with opener.open(CASDOOR_BASE + "/api/get-organization?id=admin/" + ORG, timeout=8) as r:
        got = json.loads(r.read().decode("utf-8"))
    if got.get("status") == "error" or not got.get("data"):
        out = api("/api/add-organization", {
            "owner": "admin", "name": ORG, "displayName": "OneServe",
            "websiteUrl": "https://oneserve.local", "passwordType": "bcrypt",
            "passwordOptions": ["AtLeast6"], "countryCodes": ["CN"],
            "languages": ["zh", "en"], "theme": "default",
        })
        assert out.get("status") in ("ok", "success"), f"建组织失败：{out}"

    # 应用（以 app-built-in 为模板改字段，继承默认值）
    with opener.open(CASDOOR_BASE + "/api/get-application?id=admin/app-built-in", timeout=8) as r:
        tpl = json.loads(r.read().decode("utf-8"))
    app_obj = dict(tpl.get("data") or tpl)
    app_obj.update({
        "owner": "admin", "name": APP, "organization": ORG,
        "displayName": "OneServe 管理端", "clientId": APP_CLIENT_ID,
        "clientSecret": os.environ.get("OIDC_CLIENT_SECRET", "***REMOVED***"),
        "redirectUris": ["http://localhost:8000/admin/auth-callback.html"],
        "grantTypes": ["authorization_code", "refresh_token", "password"],
        # v1.1 裁决#1（2026-10-16）：TTL 168h→2h（token 吊销档 2；与 bootstrap_admin 同源）
        "responseTypes": ["code"], "tokenFormat": "JWT", "expireInHours": 2,
        "enablePassword": True, "enableSignUp": False,
    })
    with opener.open(CASDOOR_BASE + "/api/get-application?id=admin/" + APP, timeout=8) as r:
        got = json.loads(r.read().decode("utf-8"))
    if got.get("status") == "error" or not got.get("data"):
        out = api("/api/add-application", app_obj)
        assert out.get("status") in ("ok", "success"), f"建应用失败：{out}"

    # 测试用户（tag 不设＝W4 缺省 viewer 形态）
    with opener.open(CASDOOR_BASE + "/api/get-user?id=" + ORG + "/tester", timeout=8) as r:
        got = json.loads(r.read().decode("utf-8"))
    if got.get("status") == "error" or not got.get("data"):
        out = api("/api/add-user", {
            "owner": ORG, "name": TEST_USER, "displayName": "测试用户",
            "password": TEST_PASSWORD, "organization": ORG, "type": "normal-user",
            "isAdmin": False, "isForbidden": False, "isDeleted": False,
            "signupApplication": APP, "email": "tester@oneserve.local",
        })
        assert out.get("status") in ("ok", "success"), f"建用户失败：{out}"

    # seed admin 形态用户（W6 bootstrap_admin.py 同参缩小版：tag=admin 挂平台角色；
    # 只动 oneserve 组织数据卷，幂等 ensure 口径同 tester）
    with opener.open(CASDOOR_BASE + "/api/get-user?id=" + ORG + "/" + SEED_ADMIN_USER, timeout=8) as r:
        got = json.loads(r.read().decode("utf-8"))
    if got.get("status") == "error" or not got.get("data"):
        out = api("/api/add-user", {
            "owner": ORG, "name": SEED_ADMIN_USER, "displayName": "seed admin（测试）",
            "password": _seed_admin_password(), "organization": ORG, "type": "normal-user",
            "isAdmin": False, "isForbidden": False, "isDeleted": False, "tag": "admin",
            "signupApplication": APP, "email": SEED_ADMIN_USER + "@oneserve.local",
        })
        assert out.get("status") in ("ok", "success"), f"建 seed admin 失败：{out}"


def test_live_casdoor_closed_loop():
    """live 闭环（真栈 urllib，免 fastapi）：Casdoor 可达 → 幂等 init → 密码流真
    token → 验签过（含 aud 受众匹配，P1-3）→ 写闸 200；栈未起（8000 不可达或未
    开闸）/Casdoor 不可达时显式 skip（_live_skip：可统计；ONE_SERVE_LIVE=1 强制
    fail——P1-1，不再 print 假绿）。"""
    if os.environ.get("AUTH_OIDC_LIVE") == "0":
        _live_skip("live 闭环：AUTH_OIDC_LIVE=0 显式关闭")
    if not _casdoor_reachable():
        _live_skip(f"live 闭环：Casdoor 不可达（{CASDOOR_BASE}，容器未起）")
    if not _HAS_JWT:
        pytest.skip("依赖缺席：pyjwt/cryptography（live 闭环验签）")

    opener = _admin_session()
    _ensure_init(opener)

    # 密码流拿真实 token（grantTypes 已开 password）
    data = urllib.parse.urlencode({
        "grant_type": "password", "client_id": APP_CLIENT_ID,
        "client_secret": os.environ.get("OIDC_CLIENT_SECRET", "***REMOVED***"),
        "username": TEST_USER, "password": TEST_PASSWORD,
    }).encode()
    status, payload = _http(CASDOOR_BASE + "/api/login/oauth/access_token", data=data,
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
    access = payload.get("access_token") or ""
    assert access, f"密码流换 token 失败：{payload}"

    # 本进程内真 JWKS 验签（离线语义级）
    os.environ["OIDC_JWKS_URL"] = CASDOOR_BASE + "/.well-known/jwks"   # /api/certs 要鉴权（实测），匿名 JWKS＝/.well-known/jwks
    os.environ["AUTH_OIDC_MODE"] = "on"
    AO._fetch_jwks = _REAL_FETCH_JWKS   # 防前置用例 mock 泄漏（pytest 按定义序执行）
    AO._jwks_cache.update({"keys": [], "at": 0.0})
    try:
        claims = AO.verify_access_token(access)
        assert claims.get("iss") == CASDOOR_BASE.rstrip("/"), claims.get("iss")   # iss＝Casdoor origin＝部署地址（1b 批·词条③对齐，勿写死本机地址）
        # P1-3 实测落账：真 token aud 形态断言（数组含 clientId；验签过＝aud 匹配过）
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud]
        assert APP_CLIENT_ID in aud_list, f"真 token aud 须含 {APP_CLIENT_ID}：{aud!r}"
        print("  ok live 验签（真 JWKS＋真 token＋aud 匹配）")

        # 栈级（8000 门户反代）：栈未起/未开闸时降级标注，不假绿
        def _post(url, headers):
            req = urllib.request.Request(url, data=b"{}", method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        st, body = _post("http://localhost:8000/v1/admin/reload",
                         {"Content-Type": "application/json"})
        if st != 401:
            _live_skip(f"栈级断言：无 token POST 期望 401 实得 {st}（栈未起或未开闸）")
        else:
            assert json.loads(body).get("error") == E.AUTH_REQUIRED, body[:150]
            # 栈代码是否含 W1/W4（auth/config 新增 siteWall 标记；旧栈无 → 墙/角色
            # 语义探测显式 skip，不误报旧栈；W2/W3 前端线联调期两态都合法）
            _, cfg = _http(_LIVE_BASE + "/v1/admin/auth/config", timeout=5)
            if not cfg.get("siteWall"):
                _live_skip("栈级墙/角色断言：活栈代码未含 W1 站点墙（config 无 siteWall 标记）")
            # W4 活栈：tester（无 tag＝缺省 viewer）真 token → 过墙越权 → 403 role_forbidden
            st1, body1 = _post("http://localhost:8000/v1/admin/reload",
                               {"Content-Type": "application/json",
                                "Authorization": "Bearer " + access})
            assert st1 == 403 and json.loads(body1).get("error") == E.ROLE_FORBIDDEN, \
                f"viewer 真 token 越权写须 403 role_forbidden：{st1} {body1[:200]}"
            # seed admin（tag=admin）真 token → 过墙＋矩阵 → 端点真 200。
            # 环境态前置（同「栈未起 skip」口径，P1-1 不假绿也不误红）：账号在场但被
            # 禁用＝账号管理台运维裁量结果（实测回执：bootsadmin 禁用留 维护者 裁）；
            # invalid_grant＝.env 凭据未读到或已轮换（部署注入态）。两者非代码回归→
            # 显式 skip；ONE_SERVE_LIVE=1 强制态下 _live_skip 一律红，回归仍可拦截。
            try:
                with opener.open(CASDOOR_BASE + "/api/get-user?id=" + ORG + "/" + SEED_ADMIN_USER,
                                 timeout=8) as r:
                    seed_user = (json.loads(r.read().decode("utf-8")).get("data") or {})
            except Exception:                       # noqa: BLE001 —— 查询失败交下游分类
                seed_user = {}
            if seed_user.get("isForbidden"):
                _live_skip(f"seed admin 活栈断言前置：{ORG}/{SEED_ADMIN_USER} "
                           "isForbidden=True（账号管理台已禁用）")
            seed_data = urllib.parse.urlencode({
                "grant_type": "password", "client_id": APP_CLIENT_ID,
                "client_secret": os.environ.get("OIDC_CLIENT_SECRET", "***REMOVED***"),
                "username": SEED_ADMIN_USER, "password": _seed_admin_password(),
            }).encode()
            try:
                _, seed_payload = _http(CASDOOR_BASE + "/api/login/oauth/access_token",
                                        data=seed_data,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", "replace")
                if exc.code == 400 and "invalid_grant" in err_body:
                    _live_skip(
                        f"seed admin 活栈断言前置：{ORG}/{SEED_ADMIN_USER} 密码流 "
                        "invalid_grant（.env 凭据未读到或已轮换——部署注入属环境态）")
                raise
            seed_access = seed_payload.get("access_token") or ""
            assert seed_access, f"seed admin 密码流换 token 失败：{seed_payload}"
            st2, body2 = _post("http://localhost:8000/v1/admin/reload",
                               {"Content-Type": "application/json",
                                "Authorization": "Bearer " + seed_access})
            assert st2 == 200, f"seed admin 真 token 须过墙+矩阵 200：{st2} {body2[:200]}"
            print("  ok test_live_casdoor_closed_loop（init＋真 token：viewer 403／seed admin 200）")
    finally:
        os.environ["AUTH_OIDC_MODE"] = "off"
        os.environ.pop("OIDC_JWKS_URL", None)


# ---------- 4. 管理写端点全覆盖清单（决策清单02 · 2026-10-15） ----------
# 闸形态：admin_api/monitor_api 的 router 路由级统一挂 Depends(_admin_gate)——写方法
# （POST/PUT/DELETE/PATCH）委派 auth_oidc.require_login，GET 读端点放行。本组从
# 三层锁「全覆盖」，新端点自动进清单（防漏挂是清单存在的意义）：
#   A. 路由盘点（AST 枚举装饰器，纯 stdlib 宿主机可跑）：全部写端点须分域——
#      /v1/admin/* 必须在挂闸模块，其余必须在豁免清单（batch 提交=外部调用方
#      AppKey 自鉴权；/v1/query、/v1/tags/query=数据面查询（上游调用方，网关
#      key-auth 域）；/demo/query=演示查询通道——皆非管理面，任务卡口径不动）；
#   B. 活栈清单（:8000 门户反代真链路，urllib 免 fastapi/httpx）：mode=on 无
#      token 时每个 /v1/admin 写端点 → 401 auth_required 信封；GET 保持公开；
#   C. 前端 401 拦截闭环：admin-gate.js 全局 fetch 包装器识别 auth_required →
#      跳门户品牌登录页 /login.html（2026-10-15 起，用户不见 Casdoor 门面）；抽 3
#      管理页验证闸脚本先载＋写请求头统一走
#      adminHeaders()（Bearer 单点携带，401 信封即跳转触发条件）。

_GATED_WRITE_MODULES = {"admin_api.py", "monitor_api.py", "admin_accounts.py",
                        "tenants_api.py", "pricing_api.py", "billing_api.py"}   # router 路由级挂 _admin_gate（accounts＝W5 并行卡，同闸同挂点；tenants＝管理功能群一期 A 节；pricing＝B 节价目管理页；billing＝C 节账单页＋CSV）
_AUTH_PRE_AUTH_PREFIX = "/v1/admin/auth/"   # 登录闭环端点（login 密码流）：先于 token
#   存在（拿账号密码换 token 本身＝登录），挂 token 闸＝死锁——豁免且不进 401 清单
# v2（ADR-0015）：豁免单一事实源＝auth_oidc.SITE_WALL_EXEMPT_*（分域判定与中间件
# 同源，测试不另抄第二份清单）；/demo/query 已随整站墙进墙域（W1）。
_EXEMPT_WRITE_PREFIXES = AO.SITE_WALL_EXEMPT_PREFIXES


def _route_exempt(path: str) -> bool:
    """路由是否属豁免最小集（与 auth_oidc.site_wall_hit 同源判定，反查形态）。"""
    return (path in AO.SITE_WALL_EXEMPT_EXACT
            or any(path.startswith(p) for p in AO.SITE_WALL_EXEMPT_PREFIXES))


def _enumerate_all_routes():
    """AST 枚举 service/app/*.py 全部路由装饰器（读写＋api_route）：
    [(模块, 函数, 方法, 路径)]。纯 stdlib（宿主机免 fastapi 可跑）；动态注册
    （add_api_route/mount）已核实零使用（2026-10-15 grep），装饰器扫描即完备。"""
    import ast
    rows = []
    for py in sorted((REPO / "service" / "app").glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id in ("router", "app")
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    continue
                path = str(dec.args[0].value)
                if dec.func.attr in ("get", "post", "put", "delete", "patch"):
                    rows.append((py.name, node.name, dec.func.attr.upper(), path))
                elif dec.func.attr == "api_route":
                    methods = []
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple, ast.Set)):
                            methods = [str(e.value).upper() for e in kw.value.elts if isinstance(e, ast.Constant)]
                    for mth in methods or ["POST"]:
                        rows.append((py.name, node.name, mth, path))
    return rows


def _enumerate_write_routes():
    """v1 兼容入口：全路由枚举的写方法子集（GET 供 v2 墙域清单用）。"""
    return [r for r in _enumerate_all_routes() if r[2] != "GET"]
_LIVE_BASE = os.environ.get("ONESERVE_LIVE_BASE", "http://localhost:8000")
_FRONTEND_SAMPLE_PAGES = ("appkeys", "publish", "upstreams")   # 管理写按钮最集中 3 页


def test_admin_write_route_inventory():
    """清单断言 A（v2）：全部写端点分域＋每条 admin 写路由带 min_role 列（W4）。

    分域（ADR-0015 墙域口径）：admin 写在挂闸模块；其余 ∈{豁免}∪{墙域}
    （/demo/query 演示通道随整站墙进墙域）。W4 防回潮：每条 admin 写路由必须在
    ROUTE_MIN_ROLE 有 min_role 列（合法三角色值），新写端点漏标＝红。
    基线地板（2026-10-15 v2）：全仓写路由 ≥27＝admin ≥20＋非 admin ≥6（豁免
    数据面 5＋墙域 demo 1；另＋登录闭环 pre-auth 写端点 POST /v1/admin/auth/login
    ——登录前无 token，豁免 token 闸，契约归 test_login_page）；删端点须回改地板。
    结构双保险：两挂闸模块 router 须路由级挂 _admin_gate、admin_api 写方法须委派
    auth_oidc.require_login、闸尾须接角色裁决 adjudicate_route_role。"""
    rows = _enumerate_write_routes()
    admin_rows = [r for r in rows if r[3].startswith("/v1/admin/")
                  and not r[3].startswith(_AUTH_PRE_AUTH_PREFIX)]
    other_rows = [r for r in rows if not r[3].startswith("/v1/admin/")]
    assert len(rows) >= 27 and len(admin_rows) >= 20 and len(other_rows) >= 6, \
        f"盘点数低于基线地板（27 总/20 admin/6 豁免域）：{rows!r}"
    for mod, fn, method, path in admin_rows:
        assert mod in _GATED_WRITE_MODULES, \
            f"{method} {path}（{mod}.{fn}）不在挂闸 router 上——漏挂：并入 admin_api 或明确分域进豁免清单"
        key = f"{method} {path}"
        assert key in AO.ROUTE_MIN_ROLE, \
            f"{key}（{mod}.{fn}）缺 min_role 列（W4 防回潮）：在 auth_oidc.ROUTE_MIN_ROLE 补矩阵行"
        assert AO.ROUTE_MIN_ROLE[key] in AO.ROLE_RANK, \
            f"{key} min_role 非法（须 admin/operator/viewer）：{AO.ROUTE_MIN_ROLE[key]!r}"
    for mod, fn, method, path in other_rows:
        assert _route_exempt(path) or AO.site_wall_hit(path), \
            f"{method} {path}（{mod}.{fn}）既非 admin 挂闸域，也不在豁免/墙域清单——新写端点必须分域（v2 全覆盖）"
    for mod in sorted(_GATED_WRITE_MODULES):
        src = (REPO / "service" / "app" / mod).read_text(encoding="utf-8")
        assert "APIRouter(dependencies=[Depends(_admin_gate)])" in src, \
            f"{mod} router 须路由级挂 _admin_gate（GET 放行/写方法过闸由闸内方法分派）"
    admin_src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    assert "request.method in auth_oidc.WRITE_METHODS" in admin_src, "闸内须按写方法分派"
    assert "await auth_oidc.require_login(request)" in admin_src, "写方法须委派 require_login"
    assert "await auth_oidc.adjudicate_route_role(request)" in admin_src, "闸尾须接 W4 角色裁决"
    print(f"  ok test_admin_write_route_inventory（写路由 {len(rows)}＝admin {len(admin_rows)}"
          f"＋非admin {len(other_rows)}，min_role 列 {len(admin_rows)}/{len(admin_rows)} 全标）")


def test_site_wall_route_classification_v2():
    """清单断言 A2（W1 新增）：AST 全路由（读＋写）∈{墙域}∪{豁免} 全覆盖。

    地板（2026-10-15）：总路由 ≥69＝墙域 ≥56（admin 41 含 monitor/metering＋
    portal 13＋demo 2）＋豁免 ≥13（auth 3＋healthz/ping 2＋数据面 AppKey 域
    query 1＋tags 1＋batch 5＋proxy 1）。删路由须回改地板；新路由未分域＝红
    （防漏挂是清单存在的意义）。墙域再分层断言：portal GET 13 条全墙内、
    admin 域含 GET（「读公开」废除）、/demo 两条全墙内。"""
    rows = _enumerate_all_routes()
    wall = [r for r in rows if AO.site_wall_hit(r[3])]
    exempt = [r for r in rows if _route_exempt(r[3])]
    unclassified = [r for r in rows if r not in wall and r not in exempt]
    assert not unclassified, \
        f"路由未分域（v2 全覆盖红）：{unclassified!r}——新端点必须落墙域或豁免最小集"
    assert len(rows) >= 69 and len(wall) >= 56 and len(exempt) >= 13, \
        f"盘点低于地板（69 总/56 墙/13 豁免）：总 {len(rows)} 墙 {len(wall)} 豁免 {len(exempt)}"
    # 墙域分域地板：portal 13（6 门户＋audit 1＋grants 3＋my 3）、admin 域含 GET、demo 2
    portal_wall = [r for r in wall if r[3].startswith("/v1/portal/")]
    admin_wall = [r for r in wall if r[3].startswith("/v1/admin/")]
    demo_wall = [r for r in wall if r[3] == "/demo" or r[3].startswith("/demo/")]
    assert len(portal_wall) >= 13, f"portal 墙域低于 13 条地板：{len(portal_wall)}"
    assert len(admin_wall) >= 41, f"admin 墙域低于 41 条地板（含 monitor/metering GET）：{len(admin_wall)}"
    assert any(r[2] == "GET" for r in admin_wall), "admin 域须含 GET（读公开废除）"
    assert len(demo_wall) == 2, f"/demo 两条（页面＋查询）须全墙内：{demo_wall!r}"
    # 豁免地板：auth 闭环 3＋healthz/ping＋数据面（query/tags/batch/proxy）
    auth_exempt = [r for r in exempt if r[3].startswith("/v1/admin/auth/")]
    # v1.1 裁决#1（2026-10-16 token 吊销档 2）：+1＝POST /v1/admin/auth/refresh
    # （refresh_token 无感续期，pre-token 豁免——login 同通道口径），3→4。
    assert len(auth_exempt) == 4, f"登录闭环豁免恰 4 条（含 refresh）：{auth_exempt!r}"
    data_exempt = [r for r in exempt if r[3].startswith(("/v1/query/", "/v1/tags/", "/v1/batch/", "/proxy/"))]
    assert len(data_exempt) >= 8, f"数据面 AppKey 域豁免低于 8 条地板：{len(data_exempt)}"
    print(f"  ok test_site_wall_route_classification_v2（全路由 {len(rows)}＝墙 {len(wall)}＋豁免 {len(exempt)}，"
          f"portal {len(portal_wall)}/admin {len(admin_wall)}/demo {len(demo_wall)} 全覆盖）")


def _live_stack_mode():
    """活栈鉴权模式（GET /v1/admin/auth/config 发现；None＝栈不可达）。"""
    try:
        _, payload = _http(_LIVE_BASE + "/v1/admin/auth/config", timeout=5)
    except Exception:                                        # noqa: BLE001
        return None
    return str(payload.get("mode") or "")


def test_admin_write_endpoints_401_live():
    """清单断言 B：活栈 mode=on 无 token，每个 /v1/admin 写端点 → 401 auth_required。

    探测安全设计：请求体一律空 JSON {}＋路径参数填 0——即使某端点闸失效（断言
    必挂的场景），handler 也先撞参数校验 400/不存在 404，不触发真实写。栈未起或
    mode!=on 显式 skip（_live_skip：可统计；ONE_SERVE_LIVE=1 强制 fail——P1-1；
    on 态语义已由依赖级/TestClient 用例覆盖）。"""
    mode = _live_stack_mode()
    if mode is None:
        _live_skip(f"活栈清单：栈不可达（{_LIVE_BASE}；容器未起）")
    if mode != "on":
        _live_skip(f"活栈清单：栈 AUTH_OIDC_MODE={mode} 未开闸（on 态语义单测已覆盖）")
    import re as _re

    def _probe(method: str, path: str):
        url = _LIVE_BASE + _re.sub(r"\{[^}]+\}", "0", path)
        data = None if method == "GET" else b"{}"
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    rows = [(method, path) for mod, fn, method, path
            in _enumerate_write_routes()
            if path.startswith("/v1/admin/") and not path.startswith(_AUTH_PRE_AUTH_PREFIX)]
    assert rows, "admin 写路由盘点为空（枚举损坏？）"
    probed = 0
    for method, path in rows:
        status, body = _probe(method, path)
        if status == 404:
            # 404＝活栈代码落后于工作树（并行卡新路由未随栈重启加载；墙若在位该
            # 探测先 401 后路由）。跳过并留痕，不让旧栈拖红新路由；栈升级后自动全探。
            print(f"  SKIP(栈落后) {method} {path}: 404（活栈未含本路由）")
            continue
        assert status == 401 and json.loads(body).get("error") == E.AUTH_REQUIRED, \
            f"{method} {path} 无 token 须 401 auth_required（探测填 0/空体）：实得 {status} {body[:120]}"
        probed += 1
        print(f"  ok 401 {method} {path}")
    assert probed >= 1, "活栈写端点 401 探测全被跳过（栈不可用？）"
    # W1（ADR-0015）：栈代码含站点墙（auth/config siteWall 标记）→ 墙域 GET 同 401
    # （读公开废除），并扩测 portal GET 族与豁免地板；旧栈（无标记）保持读公开语义
    # ——skip 不误报，栈升级后自动转严。
    _, cfg = _http(_LIVE_BASE + "/v1/admin/auth/config", timeout=5)
    st, body = _probe("GET", "/v1/admin/appkeys")
    if not cfg.get("siteWall"):
        assert st != 401, f"旧栈 GET 读端点不得被登录闸拦下：{st} {body[:120]}"
        print(f"  ok 旧栈 GET 读端点公开（{st}）——写端点 {len(rows)} 条全 401（站点墙语义探测待栈升级）")
    else:
        assert st == 401 and json.loads(body).get("error") == E.AUTH_REQUIRED, \
            f"W1 墙域 GET 无 token 须 401：{st} {body[:120]}"
        for path in ("/v1/portal/overview", "/v1/portal/catalog", "/v1/portal/status",
                     "/v1/portal/my/credentials", "/v1/portal/grants/panorama",
                     "/v1/portal/audit/logs", "/demo"):
            p_st, _p_body = _probe("GET", path)
            assert p_st == 401, f"墙域 GET {path} 无 token 须 401：实得 {p_st}"
        assert _probe("GET", "/v1/admin/auth/config")[0] != 401, "登录闭环豁免不得拦"
        assert _probe("GET", "/healthz")[0] != 401, "探活豁免不得拦"
        # /v1/ping＝数据面（SPEC＋main.py「经网关 key-auth 保护」）：墙豁免的语义是
        # 「不需 Bearer 登录」，其防护层是网关 AppKey 而非匿名可达——收口批 2a
        # 2026-10-15 墙 on 后 W1 分支首跑实证：无 AppKey 经网关得 key-auth 401
        # （WWW-Authenticate: apikey，非 auth_required）。按数据面口径带 dev
        # AppKey 探测（与 .env.example 公开开发值同源）。
        _ping_req = urllib.request.Request(_LIVE_BASE + "/v1/ping",
                                           headers={"apikey": "oneserve-dev-appkey-001"})
        try:
            with urllib.request.urlopen(_ping_req, timeout=8) as _r:
                _ping_st = _r.status
        except urllib.error.HTTPError as _exc:
            _ping_st = _exc.code
        assert _ping_st != 401, "端到端验证豁免不得拦（数据面 AppKey 口径；auth_required 才是墙拦）"
        print(f"  ok W1 墙域 GET 全 401（admin＋portal 7 族抽样）＋豁免放行——写端点 {len(rows)} 条全 401，清单绿")


def test_frontend_401_redirect_wiring():
    """清单断言 C（无登录模式 ADR-0029 改锚）：admin-gate.js 为空壳——不得含 401
    拦截/登录跳转机制；管理页仍保持「闸脚本先于业务 js 加载、写请求头统一走
    AdminGate.adminHeaders()（空壳下恒空对象）」的接线契约。历史拦截闭环见 git 史。"""
    gate_src = (REPO / "portal" / "admin" / "admin-gate.js").read_text(encoding="utf-8")
    assert (REPO / "admin" / "admin-gate.js").read_text(encoding="utf-8") == gate_src, \
        "admin/admin-gate.js 与 portal/admin 产物不同步（源/产物必须一致）"
    for needle in ("resp.status === 401", "auth_required",
                   "redirectToLogin()", "/login.html", "root.fetch = function"):
        assert needle not in gate_src, f"admin-gate.js 空壳不得含登录拦截链：{needle}"
    for page in _FRONTEND_SAMPLE_PAGES:
        html = (REPO / "portal" / "admin" / f"{page}.html").read_text(encoding="utf-8")
        # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（只放宽匹配不改序契约）
        pos_gate = re.search(r'src="admin-gate\.js(\?v=[0-9a-f]+)?"', html)
        pos_js = re.search(rf'src="{page}\.js(\?v=[0-9a-f]+)?"', html)
        assert pos_gate and pos_js and pos_js.start() > pos_gate.start(), \
            f"{page}.html 须 admin-gate.js 先于 {page}.js 加载（组件工厂引用 AdminGate）"
        js = (REPO / "portal" / "admin" / f"{page}.js").read_text(encoding="utf-8")
        assert "AdminGate.adminHeaders" in js, f"{page}.js 写请求头须统一走 AdminGate.adminHeaders()"
        assert "fetch(" in js, f"{page}.js 须走全局 fetch（页面取数链路不回退）"
    print(f"  ok test_frontend_401_redirect_wiring（空壳无拦截＋{len(_FRONTEND_SAMPLE_PAGES)} 页接线）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = skipped = 0
    for fn in fns:
        try:
            fn()
        except _LiveSkipped as exc:                          # P1-1：显式跳过，不再计 passed
            skipped += 1
            print("  SKIP " + fn.__name__ + ": " + str(exc))
        except Exception as exc:                             # noqa: BLE001
            failed += 1
            import traceback
            print("  FAIL " + fn.__name__ + ": " + str(exc))
            traceback.print_exc()
    print(f"{len(fns) - failed - skipped}/{len(fns)} passed, {skipped} skipped")
    sys.exit(1 if failed else 0)
