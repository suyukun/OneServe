# -*- coding: utf-8 -*-
"""账号管理代理端点（W5 · 2026-10-15 登录墙与账号体系设计稿 B2/B3）。

平台只做壳：本模块持 env 部署通道凭证登录 Casdoor 管理会话，代理
/api/add-user|update-user|delete-user|set-password|get-users 五件管理 API；
平台零用户表（身份源单一事实＝Casdoor）。角色存 Casdoor 用户 tag
（admin/operator/viewer，缺省 viewer），裁决在后端 require_role（W4）。

端点组（均 /v1/admin/accounts/*，router 级挂 admin_api._admin_gate——写方法
过 auth_oidc.require_login，与 admin_api/monitor_api 同一挂点；W1 站点闸 on
时 GET 亦被墙，off 态现状不变）：
  GET  /v1/admin/accounts/users                用户列表（get-users 代理，软删行过滤）
  POST /v1/admin/accounts/users                创建（add-user；账号/显示名/初始密码/角色）
  POST /v1/admin/accounts/users/{name}/status  禁用/启用（update-user isForbidden）
  POST /v1/admin/accounts/users/{name}/role    角色分配（update-user tag）
  POST /v1/admin/accounts/users/{name}/password 管理员重置密码（set-password，临时密码）
  POST /v1/admin/accounts/users/{name}/delete  软删（update-user isDeleted；hard=true 走
                                               delete-user 物理删——默认软删，设计稿 B2）

角色闸（设计稿 B2：require_role('admin')）：
  · 优先接并行线 W4 的 auth_oidc.require_role('admin')（factory 在场即切换，
    off 恒放行；on 先登录再比秩，不足 raise RoleForbiddenError → main handler
    出 403 role_forbidden 信封）；router 级 _admin_gate 尾部还会按
    auth_oidc.ROUTE_MIN_ROLE 矩阵裁决（本组 6 路由已列 ROLE_ADMIN，域5）；
  · require_role 未就绪的旧工作树走本地后备：AUTH_OIDC_MODE=on 时验签 token
    （复用 auth_oidc.verify_access_token）＋ claims admin tag 校验
    （只认顶层 tag claim——收口批 2a 已按 W4 定稿收窄，与
    auth_oidc.role_from_claims/portal/site-gate.js 同口径），
    不符 403 role_forbidden；off 态恒放行（同 require_login 口径）。

配置（env；compose 注入口径同 auth_oidc/CASDOOR）：
  ONESERVE_CASDOOR_ORG      目标组织（默认 oneserve；_ensure_init 同款）
  ONESERVE_CASDOOR_APP      注册应用名（默认 oneserve-admin）
  ONESERVE_BOOTSTRAP_ADMIN_USER/PASSWORD   管理通道 seed admin 账号（v1.1 裁决#5：
                            原 CASDOOR_ADMIN_PASSWORD built-in 后门通道停用，改常规
                            admin 账号 API 通道；与 compose casdoor-init 同源注入）

安全口径：fail-closed——Casdoor 不可达 502 upstream_unreachable，不降级不缓存
用户数据；审计写 JSONL（复用 app.audit.make_entry 八元组，操作人＝JWT sub，
与 admin_api._audit_admin_action 同形态，参数/响应只存 sha256 摘要，密码不落
摘要明文以外的任何地方——审计摘要输入不含 password 字段值）。

【联调项清单（收口批 2a 2026-10-15 对真栈 Casdoor 容器逐端点核对后收口）】
  1. main.py include——收口批 2a 已合流（import＋include_router，见 main.py）；
  2. test_auth_oidc 清单 v2（W4）——W4 线已并入（ROUTE_MIN_ROLE 本组 6 路由
     已标 ROLE_ADMIN，auth_oidc.py 域5 段在案），合流前预期红不再存在；
  3. Casdoor 管理 API 出入参——已对真栈逐端点实测核对（2026-10-15，收口批 2a）：
     · get-users   GET /api/get-users?owner=<org> → {status:"ok", data:[用户]}（原实现即对）
     · add-user    POST JSON 全字段 → ok（原实现即对）
     · update-user POST /api/update-user?id=<owner>/<name>（**id 走 query 参数**）
       ＋ body＝**全量用户对象**（get-user 取回后打补丁回写）——最小 patch body
       （id 在 body）实测「Unauthorized operation」；query id＋缺字段 body 实测
       「Display name cannot be empty」；全量 body＋query id 实测 ok；
     · set-password POST /api/set-password，body＝**x-www-form-urlencoded**
       （userOwner/userName/newPassword）——JSON body 实测「The user: / doesn't
       exist」；form body 实测 ok 且新密码登录验证通过；
     · delete-user POST JSON body＝**全量用户对象**——body 只给 {id:"owner/name"}
       实测 status ok 但**静默 no-op**（用户仍在）；全量对象 body 实测真删；
     （代码已按上述实测契约修正；tests/test_accounts_admin.py fake Casdoor 同步）
  4. JWT 角色 claim——W4 定稿：tokenFormat=JWT 内嵌 User，顶层 tag claim 即角色
     （ADR-0016 Decision.2）；claims_is_admin 已收窄为 tag 单一口径（与
     auth_oidc.role_from_claims 同法：strip+lower 后只认 tag）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

try:   # 宿主机单测环境无 fastapi：纯逻辑（Casdoor 通道/校验）仍可导入直测
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    _HAS_FASTAPI = False
    APIRouter = Depends = JSONResponse = None   # type: ignore[assignment]
    Request = None   # type: ignore[assignment,misc]

from app import errors
from app import auth_oidc
from app.audit import make_entry

if _HAS_FASTAPI:
    from app.admin_api import _admin_gate   # 与 monitor_api 同款：router 级统一挂闸

logger = logging.getLogger("oneserve.accounts")

ROLES = ("admin", "operator", "viewer")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_MIN_PASSWORD_LEN = 6

# role_forbidden＝W4 负责错误码中心注册（设计稿 B1）；就绪前按设计稿字面码回 403
ROLE_FORBIDDEN_CODE = getattr(errors, "ROLE_FORBIDDEN", "role_forbidden")


# ---- 配置（每次读 env——测试/热切换零全局态，auth_oidc 同款纪律） ----


def _cfg() -> dict:
    return {
        "org": os.environ.get("ONESERVE_CASDOOR_ORG", "oneserve"),
        "app": os.environ.get("ONESERVE_CASDOOR_APP", "oneserve-admin"),
        # 管理通道账号（权限方案 v1.1 裁决#5·2026-10-16 isAdmin 收口）：改为常规
        # admin 账号 API 通道——用 bootstrap 建的组织内 seed admin（tag=admin＋
        # Casdoor org 级 isAdmin，组织作用域；built-in 全局 admin 后门通道停用，
        # 维护者 未来以正常注册账号自授管理员，本端点链路不变）。
        "admin_user": (os.environ.get("ONESERVE_CASDOOR_ADMIN_USER")
                       or os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_USER") or "admin"),
        "admin_password": os.environ.get("ONESERVE_BOOTSTRAP_ADMIN_PASSWORD", ""),
        # Casdoor 基址＝登录闸同款容器网内口径（auth_oidc._cfg internal 派生）。
        "base": auth_oidc._cfg()["internal"],
    }


# ---- 部署通道管理会话（tests/test_auth_oidc._admin_session 同款部署凭证用法） ----

_SESSION_TTL_S = 1800
_session: dict = {"opener": None, "at": 0.0}


def _reset_session() -> None:
    _session.update({"opener": None, "at": 0.0})


def _admin_session() -> urllib.request.OpenerDirector:
    """常规 admin 账号登录拿会话 cookie（进程内缓存；生产改口令走 env）。

    v1.1 裁决#5（2026-10-16）isAdmin 收口：原 built-in 全局 admin 后门通道停用，
    改走组织 oneserve 内 seed admin 账号（bootstrap_admin 建，tag=admin＋org 级
    isAdmin——组织作用域，非全局；live 实测该通道 get-users/add-user 全通）。
    不带 autoSignIn（test_auth_oidc live 实证：该版本带它反而 wrong-password）。"""
    hit = _session.get("opener")
    if hit is not None and (time.time() - _session.get("at", 0.0)) < _SESSION_TTL_S:
        return hit
    cfg = _cfg()
    if not cfg["admin_password"]:
        raise _CasdoorError(401, "账号管理通道未配置（ONESERVE_BOOTSTRAP_ADMIN_PASSWORD 缺失）")
    body = json.dumps({"type": "login", "username": cfg["admin_user"],
                       "password": cfg["admin_password"],
                       "application": cfg["app"], "organization": cfg["org"]}).encode()
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(cfg["base"] + "/api/login", data=body,
                                 headers={"Content-Type": "application/json"})
    with opener.open(req, timeout=8) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if out.get("status") != "ok":
        raise _CasdoorError(401, "账号管理通道登录被拒（部署通道凭证不符？）")
    _session.update({"opener": opener, "at": time.time()})
    return opener


class _CasdoorError(Exception):
    """Casdoor 侧拒绝（带 HTTP 形态码；人话化后映射既有错误码信封）。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


# ---- Casdoor 管理 API 代理（五件） ----


def _casdoor_call(opener, path: str, payload: dict | None = None,
                  form: dict | None = None) -> dict:
    """同步调 Casdoor 管理 API（urllib；端点内经 asyncio.to_thread 不堵事件环）。

    payload（JSON）与 form（x-www-form-urlencoded）二选一——set-password 实测
    只吃 form 体（JSON 体报 "The user: / doesn't exist"，收口批 2a 实测口径）。
    """
    cfg = _cfg()
    url = cfg["base"] + path
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(payload).encode() if payload is not None else None
        ctype = "application/json"
    req = urllib.request.Request(url, data=data, headers={"Content-Type": ctype})
    try:
        with opener.open(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        _reset_session()   # 401/403 大概率会话失效：下次调用重登
        if exc.code == 400:
            raise _CasdoorError(400, "Casdoor 拒绝请求（参数不合其契约）") from exc
        if exc.code in (401, 403):
            raise _CasdoorError(401, "账号管理通道权限不足（部署通道会话失效？）") from exc
        if exc.code == 404:
            raise _CasdoorError(404, "Casdoor 端点或对象不存在") from exc
        raise _CasdoorError(502, "Casdoor 返回 HTTP " + str(exc.code)) from exc
    except _CasdoorError:
        raise
    except Exception as exc:   # noqa: BLE001 —— 不可达 fail-closed
        _reset_session()
        raise _CasdoorError(502, "Casdoor 不可达（账号管理服务暂不可用）") from exc


def _casdoor_users() -> list[dict]:
    """GET /api/get-users?owner=<org> 代理；Casdoor 业务错（status=error）人话化。"""
    opener = _admin_session()
    cfg = _cfg()
    out = _casdoor_call(opener, "/api/get-users?owner=" + cfg["org"])
    if out.get("status") not in ("ok", "success", None):
        raise _CasdoorError(400, str(out.get("msg") or "Casdoor 拒绝查询"))
    return list(out.get("data") or [])


def _casdoor_user_action(path: str, payload: dict | None = None,
                         form: dict | None = None) -> dict:
    """写五件（add-user/update-user/delete-user/set-password）统一走此口。"""
    opener = _admin_session()
    out = _casdoor_call(opener, path, payload, form=form)
    if out.get("status") not in ("ok", "success"):
        raise _CasdoorError(400, str(out.get("msg") or "Casdoor 拒绝操作"))
    return out


def _casdoor_get_user(name: str) -> dict | None:
    """GET /api/get-user?id=<org>/<name> 取全量用户对象（不存在返回 None）。

    update-user / delete-user 均需全量对象做 body（收口批 2a 实测：update-user
    缺字段报 "Display name cannot be empty"；delete-user 只给 id 是静默 no-op）。
    """
    opener = _admin_session()
    out = _casdoor_call(opener, "/api/get-user?id=" + _cfg()["org"] + "/" + name)
    if out.get("status") == "error":
        raise _CasdoorError(400, str(out.get("msg") or "Casdoor 拒绝查询"))
    data = out.get("data")
    return dict(data) if isinstance(data, dict) and data else None


def _casdoor_update_user(name: str, patch: dict) -> dict:
    """update-user 代理（真栈契约：id 走 query 参数＋全量用户对象做 body）。

    实测（收口批 2a）：body 里放 id/最小 patch 一律 "Unauthorized operation"——
    该版本 Casdoor 从 query 读定位 id、按 body 全量替换列。故先 get-user 取
    全量、打补丁、再回写；读改写窗口内无并发守护（管理台单人操作口径）。
    """
    cfg = _cfg()
    user = _casdoor_get_user(name)
    if user is None:
        raise _CasdoorError(404, "用户 " + cfg["org"] + "/" + name + " 不存在")
    user.update(patch)
    qid = urllib.parse.quote(cfg["org"] + "/" + name, safe="")
    return _casdoor_user_action("/api/update-user?id=" + qid, user)


def _casdoor_delete_user(name: str) -> dict:
    """delete-user 代理（真栈契约：全量用户对象做 body——id-only body 静默 no-op）。"""
    cfg = _cfg()
    user = _casdoor_get_user(name)
    if user is None:
        raise _CasdoorError(404, "用户 " + cfg["org"] + "/" + name + " 不存在")
    return _casdoor_user_action("/api/delete-user", user)


# ---- 角色闸（W4 require_role 语义的本地形态；联调项见模块 docstring） ----


def claims_is_admin(claims: dict) -> bool:
    """claims admin 判定（已收窄·W4 定稿口径）：只认顶层 tag claim。

    ADR-0016 Decision.2：tokenFormat=JWT 内嵌 User，顶层 tag claim 即角色；
    与 auth_oidc.role_from_claims 同法（strip+lower 归一，admin 才算）——
    roles/role/isAdmin 等其余 claim 一律不信（联调项 4 兑现，2026-10-15 收口批 2a）。
    """
    return auth_oidc.role_from_claims(claims) == auth_oidc.ROLE_ADMIN


async def _require_admin(request) -> JSONResponse | None:
    """admin 角色闸：优先接并行线 W4 的 auth_oidc.require_role('admin')（本卡任务
    口径——其就绪即切换，401/403 信封由 main 的 handler 统一出）；require_role
    未就绪的旧工作树走本地 admin tag 后备（on 态验签＋tag 裁决，off 态恒放行，
    返回 403 信封）。两种形态语义一致（设计稿 B2：require_role('admin')）。"""
    factory = getattr(auth_oidc, "require_role", None)
    if factory is not None:   # W4 语义：off 恒放行；on 先登录再比秩，不足 raise 403
        await factory("admin")(request)
        return None
    claims = getattr(request.state, "oidc_claims", None)
    if not claims:
        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer "):
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "未登录（缺少 Bearer 令牌），请先登录"),
                status_code=401)
        try:
            claims = await asyncio.to_thread(auth_oidc.verify_access_token, authz[7:].strip())
            request.state.oidc_claims = claims
        except auth_oidc.AuthRequiredError:
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "登录令牌无效或已过期，请重新登录"),
                status_code=401)
    if not claims_is_admin(claims):
        logger.warning("accounts 端点非 admin 拒绝（sub=%s tag=%r）",
                       claims.get("sub"), claims.get("tag"))
        return JSONResponse(
            errors.envelope(ROLE_FORBIDDEN_CODE, "需要管理员角色（admin）"),
            status_code=403)
    return None


# ---- 请求校验＋审计（admin_api._audit_admin_action 同形态本地实现） ----


def _validate_new_user(body: dict) -> tuple[str, str, str, str]:
    name = str(body.get("name") or "").strip()
    display = str(body.get("displayName") or "").strip() or name
    password = str(body.get("password") or "")
    role = str(body.get("role") or "viewer").strip().lower()
    if not _NAME_RE.fullmatch(name):
        raise _CasdoorError(400, "账号须为 2~64 位字母/数字/下划线/连字符")
    if len(password) < _MIN_PASSWORD_LEN:
        raise _CasdoorError(400, f"初始密码至少 {_MIN_PASSWORD_LEN} 位")
    if role not in ROLES:
        raise _CasdoorError(400, "角色须为 admin/operator/viewer 三选一")
    return name, display, password, role


def _validate_reset_password(body: dict) -> str:
    password = str(body.get("password") or "")
    if len(password) < _MIN_PASSWORD_LEN:
        raise _CasdoorError(400, f"临时密码至少 {_MIN_PASSWORD_LEN} 位")
    return password


def _audit(request, action: str, operator: str, params: dict,
           status: int, response: dict, latency_ms: int) -> str:
    """管理动作审计（JSONL 八元组；writer 缺位＝如实跳过不阻断，admin_api 同款）。"""
    writer = getattr(getattr(request, "app", None), "state", None)
    writer = getattr(writer, "audit", None)
    if writer is None:
        return ""
    safe_params = {k: v for k, v in params.items() if k not in ("password",)}
    try:
        entry = make_entry(caller=operator or "admin", endpoint=f"admin:accounts.{action}",
                           params=json.dumps(safe_params, ensure_ascii=False, sort_keys=True),
                           status=status, latency_ms=latency_ms,
                           response_body=json.dumps(response, ensure_ascii=False,
                                                    sort_keys=True),
                           trace_id=str(getattr(request.state, "trace_id", "") or ""))
        writer.append(entry)
        return entry.trace_id or ""
    except Exception:   # noqa: BLE001 —— 审计写失败不阻断业务返回
        return ""


def _user_view(u: dict) -> dict:
    """Casdoor 用户行 → 列表视图（只出展示字段；不回密码散列等内部字段）。"""
    return {
        "name": u.get("name"),
        "displayName": u.get("displayName"),
        "email": u.get("email"),
        "role": u.get("tag") or ("admin" if u.get("isAdmin") else "viewer"),
        "isForbidden": bool(u.get("isForbidden")),
        "isDeleted": bool(u.get("isDeleted")),
        "createdTime": u.get("createdTime"),
    }


def _error_response(exc: _CasdoorError) -> JSONResponse:
    """Casdoor 拒绝 → 既有错误码人话信封（4xx→既有码；不可达 502 fail-closed）。"""
    if exc.status == 400:
        return JSONResponse(errors.envelope(errors.INVALID_REQUEST, exc.message),
                            status_code=400)
    if exc.status in (401, 403):
        return JSONResponse(errors.envelope(errors.UNAUTHORIZED, exc.message),
                            status_code=401)
    if exc.status == 404:
        return JSONResponse(errors.envelope(errors.INVALID_REQUEST, exc.message),
                            status_code=404)
    return JSONResponse(errors.envelope(errors.UPSTREAM_UNREACHABLE, exc.message),
                        status_code=502)


# ---- 端点组（router 级挂 _admin_gate；未挂 main.py 前不激活——联调项 1） ----

router = APIRouter(dependencies=[Depends(_admin_gate)]) if _HAS_FASTAPI else None


def _register(app_router) -> None:   # main.py 合流时的挂载入口（联调项 1）
    app_router.include_router(router)


if _HAS_FASTAPI:   # 端点区（宿主机无 fastapi 不定义；纯逻辑/通道函数仍可测）

    @router.get("/v1/admin/accounts/users", response_model=None)
    async def accounts_list_users(request: Request) -> JSONResponse:
        """用户列表（get-users 代理；软删行默认过滤，?include_deleted=1 查看）。"""
        t0 = time.time()
        denied = await _require_admin(request)
        if denied is not None:
            return denied
        try:
            rows = await asyncio.to_thread(_casdoor_users)
        except _CasdoorError as exc:
            return _error_response(exc)
        include_deleted = request.query_params.get("include_deleted") in ("1", "true")
        users = [_user_view(u) for u in rows
                 if include_deleted or not u.get("isDeleted")]
        body = {"users": users, "total": len(users), "org": _cfg()["org"]}
        _audit(request, "list",
               _operator(claims=getattr(request.state, "oidc_claims", {})), {},
               200, {"total": len(users)}, int((time.time() - t0) * 1000))
        return JSONResponse(body)

    @router.post("/v1/admin/accounts/users", response_model=None)
    async def accounts_create_user(request: Request) -> JSONResponse:
        """创建账号（add-user；角色＝Casdoor tag，缺省 viewer）。"""
        t0 = time.time()
        denied = await _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001
            body = {}
        try:
            name, display, password, role = _validate_new_user(body or {})
        except _CasdoorError as exc:
            return _error_response(exc)
        cfg = _cfg()
        payload = {
            "owner": cfg["org"], "name": name, "displayName": display,
            "password": password, "organization": cfg["org"], "type": "normal-user",
            "tag": role, "isAdmin": role == "admin",
            "isForbidden": False, "isDeleted": False,
            "signupApplication": cfg["app"],
            "email": str(body.get("email") or "").strip() or f"{name}@{cfg['org']}.local",
        }
        try:
            await asyncio.to_thread(_casdoor_user_action, "/api/add-user", payload)
        except _CasdoorError as exc:
            return _error_response(exc)
        resp = {"ok": True, "user": {"name": name, "displayName": display, "role": role}}
        _audit(request, "create", _operator(claims=getattr(request.state, "oidc_claims", {})),
               {"name": name, "role": role}, 200, resp, int((time.time() - t0) * 1000))
        return JSONResponse(resp)

    @router.post("/v1/admin/accounts/users/{name}/status", response_model=None)
    async def accounts_set_status(name: str, request: Request) -> JSONResponse:
        """禁用/启用（update-user isForbidden；禁用即 Casdoor 侧拒绝其登录）。"""
        return await _update_user_field(request, name, "status")

    @router.post("/v1/admin/accounts/users/{name}/role", response_model=None)
    async def accounts_set_role(name: str, request: Request) -> JSONResponse:
        """角色分配（update-user tag；admin/operator/viewer 三选一）。"""
        return await _update_user_field(request, name, "role")

    @router.post("/v1/admin/accounts/users/{name}/password", response_model=None)
    async def accounts_reset_password(name: str, request: Request) -> JSONResponse:
        """管理员重置密码（set-password；临时密码口径，首次登录改密归后续卡）。"""
        t0 = time.time()
        denied = await _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001
            body = {}
        try:
            password = _validate_reset_password(body or {})
        except _CasdoorError as exc:
            return _error_response(exc)
        cfg = _cfg()
        form = {"userOwner": cfg["org"], "userName": name, "newPassword": password}
        try:
            # 真栈契约（收口批 2a 实测）：set-password 只吃 form 体，JSON 体不识别
            await asyncio.to_thread(_casdoor_user_action, "/api/set-password",
                                    form=form)
        except _CasdoorError as exc:
            return _error_response(exc)
        resp = {"ok": True, "user": name, "action": "password_reset"}
        _audit(request, "reset_password",
               _operator(claims=getattr(request.state, "oidc_claims", {})),
               {"name": name}, 200, resp, int((time.time() - t0) * 1000))
        return JSONResponse(resp)

    @router.post("/v1/admin/accounts/users/{name}/delete", response_model=None)
    async def accounts_delete_user(name: str, request: Request) -> JSONResponse:
        """删除：默认软删（update-user isDeleted=true）；body hard=true 物理删
        （delete-user 代理，设计稿 B2 软删为默认、硬删显式）。"""
        t0 = time.time()
        denied = await _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001
            body = {}
        hard = bool((body or {}).get("hard"))
        try:
            if hard:
                # 真栈契约：delete-user 须全量用户对象做 body（id-only body 实测
                # status ok 但静默 no-op，收口批 2a 已核）——helper 内先 get-user
                await asyncio.to_thread(_casdoor_delete_user, name)
                action = "purge"
            else:
                # 软删＝update-user isDeleted=true（query id＋全量 body，helper 内取全量）
                await asyncio.to_thread(_casdoor_update_user, name,
                                        {"isDeleted": True})
                action = "soft_delete"
        except _CasdoorError as exc:
            return _error_response(exc)
        resp = {"ok": True, "user": name, "action": action}
        _audit(request, action,
               _operator(claims=getattr(request.state, "oidc_claims", {})),
               {"name": name, "hard": hard}, 200, resp, int((time.time() - t0) * 1000))
        return JSONResponse(resp)

    async def _update_user_field(request: Request, name: str, kind: str) -> JSONResponse:
        """禁用/启用（isForbidden）与角色（tag）共用的 update-user 代理。"""
        t0 = time.time()
        denied = await _require_admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:   # noqa: BLE001
            body = {}
        if not _NAME_RE.fullmatch(name):
            return _error_response(_CasdoorError(400, "账号名不合规范"))
        if kind == "status":
            forbidden = bool((body or {}).get("forbidden"))
            patch = {"isForbidden": forbidden}
            action = "disable" if forbidden else "enable"
        else:
            role = str((body or {}).get("role") or "").strip().lower()
            if role not in ROLES:
                return _error_response(_CasdoorError(400, "角色须为 admin/operator/viewer 三选一"))
            patch = {"tag": role, "isAdmin": role == "admin"}
            action = "set_role"
        try:
            # 真栈契约（收口批 2a 实测）：update-user＝query id＋全量用户对象 body
            await asyncio.to_thread(_casdoor_update_user, name, patch)
        except _CasdoorError as exc:
            return _error_response(exc)
        resp = {"ok": True, "user": name, "action": action}
        _audit(request, action,
               _operator(claims=getattr(request.state, "oidc_claims", {})),
               {"name": name, **({"forbidden": patch.get("isForbidden")}
                                 if kind == "status" else {"role": patch.get("tag")})},
               200, resp, int((time.time() - t0) * 1000))
        return JSONResponse(resp)


def _operator(claims=None) -> str:
    """审计操作人＝JWT sub（设计稿 B2）；无 claims（off 态联调）如实标 anonymous。"""
    claims = claims or {}
    return str(claims.get("sub") or "anonymous")
