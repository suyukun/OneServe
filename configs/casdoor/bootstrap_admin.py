#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""W6 seed 引导（登录墙设计 B4 · ADR-0016）：bootstrap admin 幂等创建。

口径：
- 触发＝compose 服务 casdoor-init（起栈自动跑一次；docker compose up/down 全程
  幂等，重复执行「已存在即跳过」不报错不重复建）。
- 输入＝环境变量（密码不进代码不进仓；.env.example 留占位）：
    ONESERVE_BOOTSTRAP_ADMIN_USER       必填——未设置＝整段跳过（no-op 退出 0，
                                        供不需要 seed 的环境静默复用同一 compose）
    ONESERVE_BOOTSTRAP_ADMIN_PASSWORD   必填（与 USER 同设才生效；日志永不回显）
    CASDOOR_INTERNAL_BASE               Casdoor 容器网内基址（缺省 http://oneserve-casdoor:8000）
    CASDOOR_ADMIN_PASSWORD              built-in admin 口令（与 app.conf initAdminPassword
                                        同源；缺省开发占位，生产必改）
    PROD_PUBLIC_ORIGIN                  部署地址（应用 redirectUris 派生源；缺省
                                        http://localhost:8000＝dev 本机口径）
- 行为＝oneserve 组织＋oneserve-admin 应用（tokenFormat=JWT——JWT 格式内嵌
  User，tag 顶层 claim 即角色，见 ADR-0016 Decision.2）不存在则建；然后确保
  bootstrap admin 用户在位并挂 tag=admin。已存在的用户**不改密码**（幂等跳过
  ——轮换密码走账号管理/W5 或 Casdoor 控制台，不在起栈通道）。
- 失败＝非 0 退出（fail loud；compose logs 可见），不静默吞错。

built-in admin 仅部署通道（app.conf initAdminPassword）；本脚本不触碰它。
只用 stdlib urllib（与 tests/test_auth_oidc.py live 节同款通道，零新依赖）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORG = "oneserve"
APP = "oneserve-admin"
# 权限方案 v1.1 裁决#1（2026-10-16）：token TTL 168h→2h（吊销档 2 的 A 路；refresh 自动续期在 portal/site-gate.js）
TOKEN_EXPIRE_IN_HOURS = 2
APP_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "oneserve-admin-client")
APP_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")   # 真值来自 .env（compose 注入；密钥出库批 2026-10-20，空缺省＝登录失败即报错）
BASE = os.environ.get("CASDOOR_INTERNAL_BASE", "http://oneserve-casdoor:8000").rstrip("/")
# 部署地址（2026-10-20 部署地址批）：应用 redirectUris 派生源——与 service 侧授权回调
# （compose OIDC_REDIRECT_URI）严格同值；缺省 dev 本机口径，行为与历史硬编码一致。
PUBLIC_ORIGIN = os.environ.get("PROD_PUBLIC_ORIGIN", "http://localhost:8000").rstrip("/")
REDIRECT_URI = PUBLIC_ORIGIN + "/admin/auth-callback.html"
ADMIN_PASSWORD = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")  # 真值来自 .env；BOOT 变量未设＝main() 先 no-op，不会用到空口令
BOOT_USER = (os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_USER") or "").strip()
BOOT_PASSWORD = os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_PASSWORD") or ""


def _log(msg: str) -> None:
    print(f"[bootstrap-admin] {msg}", flush=True)


def _http(path: str, data=None, opener=None, timeout=10):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with (opener or urllib.request.build_opener()).open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_casdoor(deadline_s: int = 120) -> None:
    """等 Casdoor 就绪（depends_on healthcheck 已保证；双保险再等一轮）。"""
    end = time.time() + deadline_s
    while time.time() < end:
        try:
            with urllib.request.urlopen(BASE + "/.well-known/openid-configuration", timeout=5):
                return
        except Exception:                       # noqa: BLE001 —— 未就绪继续等
            time.sleep(3)
    raise RuntimeError(f"Casdoor {deadline_s}s 内未就绪（{BASE}）")


def _admin_opener():
    """built-in admin 登录会话（cookie jar；与 test_auth_oidc._admin_session 同款）。

    收口批 2a 实修（2026-10-15）：cookie jar 必须先于登录挂上——原实现登录用裸
    opener、登录后才建空 jar，会话 cookie 全丢，后续 get-organization 等一律
    匿名（实测回 "Unauthorized operation"，seed 必失败）。"""
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    out = _http("/api/login", {"type": "login", "username": "admin",
                               "password": ADMIN_PASSWORD, "application": "app-built-in",
                               "organization": "built-in"}, opener=opener)
    if out.get("status") != "ok":
        raise RuntimeError(f"built-in admin 登录失败（CASDOOR_ADMIN_PASSWORD 与 app.conf 不一致？）")
    return opener


def main() -> int:
    if not BOOT_USER or not BOOT_PASSWORD:
        _log("ONESERVE_BOOTSTRAP_ADMIN_USER/PASSWORD 未设置——按契约整段跳过（no-op）")
        return 0
    _wait_casdoor()
    opener = _admin_opener()

    def api(path: str, payload) -> dict:
        return _http(path, payload, opener=opener)

    def get_ok(path: str) -> bool:
        with opener.open(BASE + path, timeout=10) as resp:
            got = json.loads(resp.read().decode("utf-8"))
        return not (got.get("status") == "error" or not got.get("data"))

    # 组织（不存在则建；与 test_auth_oidc._ensure_init 同参）
    if not get_ok(f"/api/get-organization?id=admin/{ORG}"):
        out = api("/api/add-organization", {
            "owner": "admin", "name": ORG, "displayName": "OneServe",
            "websiteUrl": "https://oneserve.local", "passwordType": "bcrypt",
            "passwordOptions": ["AtLeast6"], "countryCodes": ["CN"],
            "languages": ["zh", "en"], "theme": "default",
        })
        if out.get("status") not in ("ok", "success"):
            raise RuntimeError(f"建组织失败：{out}")
        _log(f"组织 {ORG} 已创建")
    else:
        _log(f"组织 {ORG} 已在位（跳过）")

    # 应用（以 app-built-in 为模板改字段；tokenFormat=JWT——tag 角色 claim 随
    # JWT 内嵌 User 顶层字段下发，ADR-0016 Decision.2）
    # 权限方案 v1.1 裁决#1（2026-10-16 token 吊销档 2）：expireInHours 168→2
    # （TTL 1~2h 档取 2h），前端 refresh_token 无感续期补齐（site-gate.js +
    # /v1/admin/auth/refresh）。存量子按新档校正（update-application 幂等收敛）。
    app = None
    if get_ok(f"/api/get-application?id=admin/{APP}"):
        with opener.open(BASE + f"/api/get-application?id=admin/{APP}", timeout=10) as r:
            app = json.loads(r.read().decode("utf-8")).get("data") or {}
    if app:
        if int(app.get("expireInHours") or 0) != TOKEN_EXPIRE_IN_HOURS:
            app["expireInHours"] = TOKEN_EXPIRE_IN_HOURS
            out = api("/api/update-application?id=" + urllib.parse.quote(f"admin/{APP}", safe=""), app)
            if out.get("status") not in ("ok", "success"):
                raise RuntimeError(f"应用 {APP} TTL 校正失败：{out}")
            _log(f"应用 {APP} expireInHours 校正为 {TOKEN_EXPIRE_IN_HOURS}（token 吊销档 2）")
        else:
            _log(f"应用 {APP} 已在位且 TTL={TOKEN_EXPIRE_IN_HOURS}h（跳过）")
    else:
        with opener.open(BASE + f"/api/get-application?id=admin/app-built-in", timeout=10) as r:
            tpl = json.loads(r.read().decode("utf-8"))
        app_obj = dict(tpl.get("data") or tpl)
        app_obj.update({
            "owner": "admin", "name": APP, "organization": ORG,
            "displayName": "OneServe 管理端", "clientId": APP_CLIENT_ID,
            "clientSecret": APP_CLIENT_SECRET,
            "redirectUris": [REDIRECT_URI],   # PROD_PUBLIC_ORIGIN 派生（缺省 dev 本机口径）
            "grantTypes": ["authorization_code", "refresh_token", "password"],
            "responseTypes": ["code"], "tokenFormat": "JWT",
            "expireInHours": TOKEN_EXPIRE_IN_HOURS,
            "enablePassword": True, "enableSignUp": False,
        })
        out = api("/api/add-application", app_obj)
        if out.get("status") not in ("ok", "success"):
            raise RuntimeError(f"建应用失败：{out}")
        _log(f"应用 {APP}（{APP_CLIENT_ID}）已创建（tokenFormat=JWT，TTL={TOKEN_EXPIRE_IN_HOURS}h，角色随 tag claim）")

    # bootstrap admin 用户（幂等：已存在即跳过，不改密码；tag=admin 挂平台角色）
    if not get_ok(f"/api/get-user?id={ORG}/{BOOT_USER}"):
        out = api("/api/add-user", {
            "owner": ORG, "name": BOOT_USER, "displayName": "平台管理员（seed）",
            "password": BOOT_PASSWORD, "organization": ORG, "type": "normal-user",
            # v1.1 裁决#5：isAdmin=True＝Casdoor 组织级管理员（org 作用域，非全局
            # built-in）——service 账号管理通道（admin_accounts._admin_session）以
            # 本账号走常规 admin API 通道，built-in 后门停用（live 实测 get/add/update 全通）。
            "isAdmin": True, "isForbidden": False, "isDeleted": False,
            "tag": "admin",
            "signupApplication": APP,
            "email": f"{BOOT_USER}@oneserve.local",
        })
        if out.get("status") not in ("ok", "success"):
            raise RuntimeError(f"建 bootstrap admin 失败：{out}")
        # 读回验证（收口批续作件 B，2026-10-15）：add-user 回 status=ok 不等于落库——
        # get-user 确认真在且 tag=admin 才算成功（fail loud 口径；防静默 no-op 回潮，
        # 同 update-user 契约实测教训）。第三任清账批补全：在位确认之外再读回实体
        # 核对 tag=admin（原实现只验 get_ok 不看 tag＝与注释口径不符，补齐）。
        if not get_ok(f"/api/get-user?id={ORG}/{BOOT_USER}"):
            raise RuntimeError(f"add-user 报 ok 但读回缺失：{ORG}/{BOOT_USER}")
        with opener.open(BASE + f"/api/get-user?id={ORG}/{BOOT_USER}", timeout=10) as r:
            created = json.loads(r.read().decode("utf-8"))["data"]
        if str(created.get("tag") or "") != "admin":
            raise RuntimeError(
                f"add-user 读回在位但 tag!=admin（实际={created.get('tag')!r}）：{ORG}/{BOOT_USER}")
        _log(f"bootstrap admin 用户 {ORG}/{BOOT_USER} 已创建并读回确认在位（tag=admin；密码不落日志）")
    else:
        # 在位即确保角色挂对（update-user 只改 tag；密码不动）
        with opener.open(BASE + f"/api/get-user?id={ORG}/{BOOT_USER}", timeout=10) as r:
            user = json.loads(r.read().decode("utf-8"))["data"]
        # v1.1 裁决#5：tag=admin 之外还须 org 级 isAdmin=True（service 常规 admin
        # API 通道的前提；存量 False 一并幂等校正）
        if str(user.get("tag") or "") != "admin" or not user.get("isAdmin"):
            user["tag"] = "admin"
            user["isAdmin"] = True
            # 真栈契约（收口批 2a 实测）：update-user 定位 id 走 query 参数＋全量对象 body
            out = api("/api/update-user?id=" + urllib.parse.quote(ORG + "/" + BOOT_USER, safe=""), user)
            if out.get("status") not in ("ok", "success"):
                raise RuntimeError(f"校正 bootstrap admin 角色失败：{out}")
            _log(f"用户 {ORG}/{BOOT_USER} 已在位，校正 tag=admin＋org 级 isAdmin（密码未动）")
        else:
            _log(f"bootstrap admin 用户 {ORG}/{BOOT_USER} 已在位且 tag=admin＋isAdmin（跳过）")

    _log("seed 引导完成（幂等）")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                    # noqa: BLE001 —— fail loud，不静默
        _log(f"FAIL：{exc}")
        sys.exit(1)
