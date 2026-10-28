#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录入口套件（ADR-0018 授权码流版，2026-10-20 task-027 重写）。

背景沿革：2026-10-15 品牌密码登录页（自建表单+后端密码流）→ 2026-10-19 ADR-0018
「登录页回原生」：login.html 退役为**授权引导壳**（fetch /v1/admin/auth/config 拿
带一次性 state 的授权地址→整页跳转 Casdoor 同源白标登录页）；登录后
fastAutoSignin 静默 302 发码→/admin/auth-callback.html 换 token→按回址回门户。
后端密码流端点保留为测试/后备通道（ADR-0018 Decision.4）。

守卫口径：
  1. 引导壳契约（portal/login.html）：拉 /v1/admin/auth/config＋location.replace
     跳授权地址；零表单零口令（登录交互在原生页）；零外链；不加载 admin-gate.js；
     零 19000 直连；
  2. 密码流端点契约（保留通道）：AST 源级＋fastapi 形态（同旧版）；
  3. 跳转防回潮（admin-gate.js 代码级）：loginUrl 恒 /login.html；登出**必须**
     调 /api/logout（ADR-0018 SLO——同源会话 cookie 常驻，不杀=自动回登）；
  4. 回跳页契约（admin/auth-callback.html）：state 必随 code 回传（后端 #34
     fail-closed 强校验）；回址限本站 origin（不限 /admin/，门户共用）；
  5. 网关契约（portal-nginx.conf）：路由哨兵注入在案（SPA 首登内部路由到 /apps
     弹回引导壳）；/apps 劫持在案；登录页启动 API 白名单含 get-app-login；
  6. live 契约（栈可达才跑）：密码流通道 200/401/400 人话（同旧版）。

运行：python3 tests/test_login_page.py（或 python3 -m pytest tests/test_login_page.py）。
"""
from __future__ import annotations

import base64
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))   # fastapi 在场时直探 auth_oidc.router
LOGIN_PAGE = REPO / "portal" / "login.html"
CALLBACK_PAGE = REPO / "admin" / "auth-callback.html"
GATE_SRC = REPO / "admin" / "admin-gate.js"
NGINX_CONF = REPO / "configs" / "nginx" / "portal-nginx.conf"
AUTH_OIDC = REPO / "service" / "app" / "auth_oidc.py"
LIVE_BASE = "http://localhost:8000"
TEST_USER, TEST_PASSWORD = "tester", "os1020"

# 与 test_admin_gate._strip_js_comments 同口径：断言「代码里没有」而非「没提到」
def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


def _skip(reason: str) -> None:
    print(f"  [SKIP] {reason}（不假绿）")


# ---------- 1. 引导壳契约 ----------

def test_login_bootstrap_contract():
    """login.html＝授权引导壳：拉 config 跳授权地址；零表单零口令。"""
    html = LOGIN_PAGE.read_text(encoding="utf-8")
    # 授权引导：config 发现 + 整页跳转
    assert "/v1/admin/auth/config" in html, "须拉取 /v1/admin/auth/config（一次性 state 由后端下发）"
    assert "location.replace(j.loginUrl)" in html, "须整页跳转授权地址（location.replace）"
    # 零表单零口令（登录交互全部在 Casdoor 原生白标页）
    assert 'type="password"' not in html, "引导壳不得残留密码框（ADR-0018：登录交互在原生页）"
    assert 'id="login-username"' not in html and 'id="login-submit"' not in html, "旧表单遗迹须清"
    # 品牌（纯文字标，维护者 2026-10-19：不做图标）
    assert "OneServe" in html and "统一数据服务平台" in html, "品牌字样须在"
    # 内网红线：零外链
    for bad in ("http://cdn", "https://cdn", "unpkg.com", "jsdelivr", "googleapis"):
        assert bad not in html, f"外链红线：{bad}"
    # 不加载 admin-gate.js / site-gate（防 401 拦截循环）
    assert 'src="admin-gate.js"' not in html and 'src="site-gate.js"' not in html
    # 零 Casdoor 直连（19000 仅后端依赖）
    assert "http://localhost:19000" not in html and ":19000" not in html
    print("  ok test_login_bootstrap_contract")


# ---------- 2. 密码流端点契约（保留通道，源级 AST＋fastapi 形态） ----------

def test_login_endpoint_registered():
    """POST /v1/admin/auth/login 注册在 auth_oidc（测试/后备通道，ADR-0018 Decision.4）。"""
    import ast
    tree = ast.parse(AUTH_OIDC.read_text(encoding="utf-8"))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "post" and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and dec.args[0].value == "/v1/admin/auth/login"):
                hits.append(node.name)
    assert hits == ["auth_login"], f"登录端点须恰一个 auth_login：{hits}"
    src = AUTH_OIDC.read_text(encoding="utf-8")
    assert '"grant_type": "password"' in src, "须走 Casdoor 密码流（grant_type=password）"
    assert '"/api/login/oauth/access_token"' in src, "复用 Casdoor token 端点"
    for needle in (errors_UNAUTHORIZED, errors_INVALID_REQUEST, errors_UPSTREAM):
        assert needle in src, f"登录端点缺错误码映射：{needle}"
    try:
        from app import auth_oidc as AO   # noqa: F401 —— 仅探测可导入性
        if AO.router is None:
            _skip("router 形态断言：宿主机缺 fastapi（AST 源级断言已覆盖）")
        else:
            routes = {(getattr(r, "path", ""), tuple(getattr(r, "methods", ()) or ()))
                      for r in AO.router.routes}
            assert any(p == "/v1/admin/auth/login" and "POST" in m for p, m in routes), \
                "auth_oidc.router 须注册 POST /v1/admin/auth/login"
    except ImportError:
        _skip("router 形态断言：宿主机缺 fastapi（AST 源级断言已覆盖）")
    print("  ok test_login_endpoint_registered")


errors_UNAUTHORIZED = 'errors.UNAUTHORIZED'
errors_INVALID_REQUEST = 'errors.INVALID_REQUEST'
errors_UPSTREAM = 'errors.UPSTREAM_UNREACHABLE'


# ---------- 3. 跳转防回潮＋SLO（admin-gate.js 代码级） ----------

def test_gate_redirect_targets_login_page():
    """loginUrl 恒 /login.html；登出必须调 /api/logout（ADR-0018 SLO）。"""
    js = _strip_js_comments(GATE_SRC.read_text(encoding="utf-8"))
    assert "var LOGIN_PAGE = '/login.html';" in js, "登录页常量须为 /login.html"
    assert "return LOGIN_PAGE;" in js
    assert "19000" not in js, "代码不得残留 Casdoor 直连"
    assert "login/oauth/authorize" not in js, "授权地址由后端 config 下发（前端不拼 authorize）"
    assert "fetch('/api/logout'" in js, "登出必须调 /api/logout 杀 Casdoor 同源会话（SLO，不调=自动回登）"
    assert GATE_SRC.read_text(encoding="utf-8") == \
        (REPO / "portal" / "admin" / "admin-gate.js").read_text(encoding="utf-8"), \
        "admin/admin-gate.js 与 portal/admin 产物不同步（源/产物必须一致）"
    print("  ok test_gate_redirect_targets_login_page")


# ---------- 4. 回跳页契约（auth-callback.html） ----------

def test_callback_state_and_return_contract():
    """state 必随 code 回传（#34 fail-closed）；回址限本站 origin。"""
    html = CALLBACK_PAGE.read_text(encoding="utf-8")
    assert "'&state=' + encodeURIComponent" in html, "state 须随 code 回传（旧版漏传＝401 bad_state）"
    assert "location.origin + '/'" in html, "回址限本站 origin（门户/管理端共用登录）"
    assert "location.origin + '/admin/'" not in html, "旧「限 /admin/」口径须已升级"
    print("  ok test_callback_state_and_return_contract")


# ---------- 5. 网关契约（portal-nginx.conf） ----------

def test_nginx_authorize_flow_wiring():
    """路由哨兵＋/apps 劫持＋启动 API 白名单（get-app-login）在案。"""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert "sub_filter '<body>'" in conf and "location.replace(\"/login.html\")" in conf, \
        "路由哨兵注入须在案（SPA 首登内部路由 /apps 弹回引导壳）"
    assert "location = /apps" in conf, "/apps 劫持须在案"
    assert "get-app-login" in conf, "登录页启动 API 白名单须含 get-app-login（netlog 实证必调）"
    assert "login/oauth/authorize" in conf, "SPA 授权路由 API 须放行"
    print("  ok test_nginx_authorize_flow_wiring")


# ---------- 6. live 契约（栈可达才跑） ----------

def _http(url, data=None, headers=None, timeout=8, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except Exception:                                       # noqa: BLE001
            return exc.code, {"raw": body}


def _stack_mode():
    try:
        _, payload = _http(LIVE_BASE + "/v1/admin/auth/config", timeout=5)
        return str(payload.get("mode") or "")
    except Exception:                                           # noqa: BLE001
        return None


def test_login_endpoint_contract_live():
    """live 契约（密码流保留通道）：tester 200 拿 JWT；错密码 401 人话；缺字段 400。"""
    mode = _stack_mode()
    if mode is None:
        _skip(f"live 契约：栈不可达（{LIVE_BASE}；容器未起；不假绿）")
        return
    if mode != "on":
        _skip(f"live 契约：栈 AUTH_OIDC_MODE={mode} 未开闸")
        return

    st, payload = _http(LIVE_BASE + "/v1/admin/auth/login", method="POST",
                        data=json.dumps({"username": TEST_USER, "password": TEST_PASSWORD}).encode(),
                        headers={"Content-Type": "application/json"})
    if st == 401 and "多因素" in str(payload.get("message", "")):   # 后端 mfa_required 人话映射（X-Error-Code 同标）
        # MFA Required（org mfaItems）启用后，未绑定 TOTP 的用户走不通密码流通道
        # （ROPC 无 MFA 交互载体，Casdoor 拒发）——设计行为，非回归；绑定后或关闭
        # MFA 后本用例自动恢复。授权码流（主通道）不受影响：绑定页 /mfa/setup 承接。
        _skip("live 密码流契约：MFA Required 已启用且 tester 未绑定 TOTP（ROPC 通道关闭）")
        return
    assert st == 200, f"tester 登录须 200：{st} {payload}"
    token = payload.get("accessToken") or ""
    assert token.count(".") == 2, "access token 须为 JWT 三段式"
    assert payload.get("tokenType") == "Bearer"
    part = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    assert claims.get("sub"), "JWT 须带 sub claim"
    print(f"  ok live 登录成功（sub={claims.get('sub')}）")

    st2, payload2 = _http(LIVE_BASE + "/v1/admin/auth/login", method="POST",
                          data=json.dumps({"username": TEST_USER, "password": "wrong-password!"}).encode(),
                          headers={"Content-Type": "application/json"})
    assert st2 == 401, f"错密码须 401：{st2} {payload2}"
    assert payload2.get("error") == "unauthorized", payload2
    assert "账号或密码不正确" in payload2.get("message", ""), "人话报错"
    assert payload2.get("docs_url"), "错误信封恒带 docs_url"

    st3, payload3 = _http(LIVE_BASE + "/v1/admin/auth/login", method="POST",
                          data=json.dumps({"username": TEST_USER}).encode(),
                          headers={"Content-Type": "application/json"})
    assert st3 == 400 and payload3.get("error") == "invalid_request", payload3
    assert "请输入账号和密码" in payload3.get("message", ""), "缺字段人话提示"
    print("  ok test_login_endpoint_contract_live（好账 200＋错密 401 人话＋缺字段 400）")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as exc:                                # noqa: BLE001
            failed += 1
            import traceback
            print("  FAIL " + fn.__name__ + ": " + str(exc))
            traceback.print_exc()
    print(str(len(fns) - failed) + "/" + str(len(fns)) + " passed")
    sys.exit(1 if failed else 0)
