# -*- coding: utf-8 -*-
"""OIDC 登录校验（ADR-0010 硬前提兑现起点 · Casdoor 集成，2026-10-14 最小闭环）。

调研报告 docs/research/登录权限选型_20261014.md Top1＝Casdoor；本模块按其落地
路径第 2 步实现：验 JWT（Casdoor 签发的 access token，JWKS 取公钥验签）＋
require_login 依赖（本期只挂 /v1/admin/* 写操作族端点——POST/PUT/DELETE/PATCH；
读端点与门户端点不动，全量角色细化另卡）。

混合模式（调研报告 §六）：门户公开只读直通；管理写操作经登录（401 auth_required
→ 前端跳 Casdoor 登录页 → 回跳 code → 后端 /v1/auth/callback 换 token → 前端存
localStorage → 请求带 Bearer）。授权码后端换 token＝简化路径，免前端存 client_secret。

配置（环境变量；compose 缺省＝开发占位，.env.example 同步注释）：
  AUTH_OIDC_MODE       on/off（默认 off：宿主机单测口径；compose 起栈置 on——
                       上线口径以 compose/部署环境为准，裸跑 service 不带该
                       变量＝无登录闸，风险口径同 ADR-0010 恒放行期）
  OIDC_ISSUER          Casdoor 地址（默认 http://localhost:8000＝dev 门户入口，issuer
                       校验口径；compose 恒注入＝PROD_PUBLIC_ORIGIN 派生——镜像生产化
                       1b 批·词条③自旧 19000 口径对齐，ADR-0018 同源部署后 19000 已封死）
  OIDC_CLIENT_ID       应用 client_id（默认 oneserve-admin-client）
  OIDC_CLIENT_SECRET   应用 client_secret（默认开发占位，生产改值不进仓）
  OIDC_JWKS_URL        JWKS 端点（默认 {issuer}/api/certs；可测注入覆盖）
  AUTH_OIDC_AUDIENCE   期望 aud 受众（P1-3；缺省=OIDC_CLIENT_ID——Casdoor JWT
                       aud＝clientId，2026-10-15 密码流实测 ["oneserve-admin-client"]）

安全口径：fail-closed——模式 on 时无 token/验签失败/JWKS 不可达一律 401
auth_required（不降级放行）；pyjwt/cryptography 缺失同 401（环境缺依赖不算放行理由）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

try:   # 宿主机单测环境无 fastapi：纯逻辑（验签/配置/异常）仍可导入直测（容器内全量）
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    _HAS_FASTAPI = False
    APIRouter = JSONResponse = None   # type: ignore[assignment]
    Request = None   # type: ignore[assignment,misc]

from app import errors

logger = logging.getLogger("oneserve.auth")


# ---- 配置（每次读 env——测试/热切换零全局态；compose 注入见 docker-compose.yml） ----


def _cfg() -> dict:
    issuer = os.environ.get("OIDC_ISSUER", "http://localhost:8000").rstrip("/")
    # 内网基址：容器网内 service 访问 casdoor 走 oneserve-casdoor:8000（浏览器口径
    # 的 issuer＝Casdoor origin＝部署地址 PROD_PUBLIC_ORIGIN，两口径分开——iss 校验
    # 按对外口径；缺省 8000＝dev 本机门户入口，与 compose 注入缺省同源，1b 批·词条③）
    internal = os.environ.get("OIDC_INTERNAL_BASE") or issuer
    client_id = os.environ.get("OIDC_CLIENT_ID", "oneserve-admin-client")
    return {
        "mode": os.environ.get("AUTH_OIDC_MODE", "off").strip().lower(),
        "issuer": issuer,
        "internal": internal.rstrip("/"),
        "client_id": client_id,
        # 期望 aud（P1-3 复审修）：AUTH_OIDC_AUDIENCE 显式优先，缺省=应用 clientId
        "audience": os.environ.get("AUTH_OIDC_AUDIENCE") or client_id,
        "client_secret": os.environ.get("OIDC_CLIENT_SECRET", ""),   # 真值来自 .env（compose 注入）；宿主直跑缺省空串＝off 态用不到（密钥出库批 2026-10-20）
        "jwks_url": os.environ.get("OIDC_JWKS_URL") or None,   # 缺省派生自 internal
    }


def auth_enabled() -> bool:
    return _cfg()["mode"] == "on"


# ---- 双模分派（ADR-0027 步1 · 登录本地化）：AUTH_PROVIDER=native|oidc，缺省 native ----
# native＝本地 users.yaml 验密＋HS256 自签 JWT（app/auth_native.py，本模块各入口
# 懒加载分派）；oidc＝本模块原 Casdoor RS256/JWKS 链路（代码与 git 史全保留，
# 未来要 SSO 时缝后重插）。登录闸总开关仍是 AUTH_OIDC_MODE（on/off 语义零变化）；
# 非法 provider 值按 native 兜底（缺省口径）并告警一次。

_PROVIDER_NATIVE = "native"
_PROVIDER_OIDC = "oidc"
_provider_warned = False


def _provider() -> str:
    global _provider_warned
    val = (os.environ.get("AUTH_PROVIDER") or "").strip().lower()
    if val not in (_PROVIDER_NATIVE, _PROVIDER_OIDC):
        if val and not _provider_warned:
            logger.warning("AUTH_PROVIDER=%r 非法（native|oidc），按缺省 native 兜底", val)
            _provider_warned = True
        return _PROVIDER_NATIVE
    return val


def _native() -> "object | None":
    """native 实现模块（懒加载：oidc 模式零导入开销，且避免模块级循环导入）。"""
    if _provider() != _PROVIDER_NATIVE:
        return None
    from app import auth_native
    return auth_native


# ---- 错误信封（错误码中心同源：AUTH_REQUIRED；AST 机检不散落字面量） ----


class AuthRequiredError(Exception):
    """管理写操作未登录/令牌无效（main 注册 handler → 401 auth_required 信封）。

    细分 reason（进 message 供前端与排障）：missing_token / bad_token /
    jwks_unavailable / dependency_missing。"""

    def __init__(self, reason: str = "missing_token"):
        self.reason = reason
        super().__init__(reason)


_REASONS = {
    "missing_token": "未登录（缺少 Bearer 令牌），请经统一登录页登录",
    "bad_token": "登录令牌无效或已过期，请重新登录",
    "jwks_unavailable": "登录服务不可达（JWKS/令牌端点拉取失败），稍后重试",
    "dependency_missing": "服务端登录校验依赖缺失（pyjwt/cryptography 未安装）",
    "secret_missing": "服务端登录密钥未配置（ONESERVE_AUTH_SECRET），已拒绝登录校验（native 模式）",
    "bad_state": "登录回跳校验失败（state 不符或已过期），请从管理页重新发起登录",
}


def auth_required_response(exc: AuthRequiredError):
    """401 auth_required 信封（错误码中心同源；handler/回调端点调用，fastapi 环境）。"""
    return JSONResponse(
        errors.envelope(errors.AUTH_REQUIRED, _REASONS.get(exc.reason, _REASONS["bad_token"])),
        status_code=401)


# ---- #34 OIDC state（login-CSRF 防护，2026-10-15 复审 P2-1） ----
# 最小实现＝无状态签名短时 cookie：config 发现时生成、callback 比对后清除。
# 值形态 "<ts_unix>.<sig>"，sig＝HMAC-SHA256(client_secret, ts) 前 16 hex——
# 密钥为后端私有（浏览器 JS 读得到 cookie 值也算不出合法 sig），无服务端会话表。
_STATE_TTL_S = 600   # 10 分钟：够人在 Casdoor 页输密码，又短到重放窗口可忽略

def _state_sig(secret: str, ts: str) -> str:
    return hmac.new((secret or "").encode(), ts.encode(), hashlib.sha256).hexdigest()[:16]


def make_state(secret: str) -> str:
    """一次性 state（时间戳＋HMAC 签名；同秒重复值由 TTL 内等值语义无害）。"""
    ts = str(int(time.time()))
    return f"{ts}.{_state_sig(secret, ts)}"


def check_state(got: str, cookie_val: str, secret: str) -> tuple[bool, str]:
    """query state × cookie state 校验（纯逻辑，宿主机可测）。

    返回 (ok, reason)：reason ∈ missing_cookie / bad_format / bad_signature /
    expired / mismatch（missing_cookie＝后备通道兼容口径，见 callback docstring）。"""
    if not cookie_val:
        return False, "missing_cookie"
    if "." not in (got or "") or got != cookie_val:
        return False, "mismatch"
    ts, _, sig = got.partition(".")
    try:
        age = time.time() - int(ts)
    except ValueError:
        return False, "bad_format"
    if not hmac.compare_digest(sig, _state_sig(secret, ts)):
        return False, "bad_signature"
    if age > _STATE_TTL_S or age < -_STATE_TTL_S:   # 负容差＝客户端时钟略偏
        return False, "expired"
    return True, ""


# ---- JWKS 缓存（kid → JWK；缓存过期或未知 kid 触发刷新） ----

_JWKS_TTL_S = 300
_jwks_cache: dict = {"keys": [], "at": 0.0}


def _fetch_jwks(url: str) -> dict:
    """同步拉 JWKS（urllib；require_login 经 asyncio.to_thread 调用，不堵事件环）。"""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def verify_access_token(token: str) -> dict:
    """验 Casdoor 签发的 access token（JWT·RS256）：JWKS 公钥验签＋iss/exp 校验。

    aud 受众校验（P1-3 复审修）：期望 aud＝AUTH_OIDC_AUDIENCE 或应用 clientId；
    token 带 aud 必须匹配（同 issuer 其他应用的 token 不再放行），不带 aud 的
    旧版 Casdoor 形态放行但记 warning（不静默）。实测当前 Casdoor 密码流 token
    aud=["oneserve-admin-client"]（数组形态）。失败抛 AuthRequiredError
    （fail-closed，不降级放行）。成功返回 claims dict。

    双模分派（ADR-0027 步1）：AUTH_PROVIDER=native（缺省）时改走本地 HS256
    验签（auth_native.verify_access_token，同 fail-closed 口径）；oidc 走本函数。
    """
    _nat = _native()
    if _nat is not None:
        return _nat.verify_access_token(token)
    try:
        import jwt   # PyJWT（懒加载：宿主机无依赖时 import 本模块不炸）
    except Exception as exc:   # noqa: BLE001
        raise AuthRequiredError("dependency_missing") from exc

    cfg = _cfg()
    # Casdoor 匿名可达的 JWKS＝/.well-known/jwks（discovery 同源；/api/certs 要鉴权，实测 2026-10-14）
    jwks_url = cfg["jwks_url"] or (cfg["internal"] + "/.well-known/jwks")
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        now = time.time()
        jwk = next((k for k in _jwks_cache["keys"] if k.get("kid") == kid), None)
        if jwk is None or (now - _jks_age()) > _JWKS_TTL_S:
            _jwks_cache.update(_fetch_jwks(jwks_url), at=now)
            jwk = next((k for k in _jwks_cache["keys"] if k.get("kid") == kid), None)
        if jwk is None:
            raise AuthRequiredError("bad_token")
        public = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        # aud 校验（P1-3 复审修）：带 aud 必须匹配期望受众；无 aud 放行＋warning
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception:   # noqa: BLE001 —— 预检失败留给正式 decode 统一裁决
            unverified = {}
        if unverified.get("aud"):
            claims = jwt.decode(token, public, algorithms=["RS256"],
                                issuer=cfg["issuer"], audience=cfg["audience"])
        else:
            logger.warning("OIDC token 无 aud claim，跳过受众校验（iss=%s 期望 aud=%s）",
                           cfg["issuer"], cfg["audience"])
            claims = jwt.decode(token, public, algorithms=["RS256"],
                                issuer=cfg["issuer"])
        return claims
    except AuthRequiredError:
        raise
    except jwt.ExpiredSignatureError as exc:
        raise AuthRequiredError("bad_token") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthRequiredError("bad_token") from exc
    except Exception as exc:   # noqa: BLE001 —— 网络错/解码错一律 fail-closed
        logger.warning("OIDC 验签失败（fail-closed）：%s", exc)
        raise AuthRequiredError("jwks_unavailable") from exc


def _jks_age() -> float:
    return _jwks_cache.get("at", 0.0)


# ---- require_login 依赖（admin 写操作族挂点；_admin_gate 委派到此） ----

WRITE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


async def require_login(request) -> None:
    """/v1/admin/* 写操作登录闸：Authorization: Bearer <casdoor jwt>。

    模式 off（宿主机单测/裸跑）恒放行——与 ADR-0010 恒放行形态同语义，开关随
    部署环境（compose 置 on）切换，代码零分叉。
    W1 起站点墙中间件先行验签并注入 request.state.oidc_claims——已注入即短路
    （同一请求同一进程，claims 只可能来自本模块验签成功之后），免重复 JWKS 验算。"""
    if not auth_enabled():
        return
    if getattr(request.state, "oidc_claims", None) is not None:
        return   # 站点墙（或前置依赖）已验过：state 有 claims 即验签成功的唯一写入口
    authz = request.headers.get("authorization", "")
    if not authz.lower().startswith("bearer "):
        raise AuthRequiredError("missing_token")
    claims = await asyncio.to_thread(verify_access_token, authz[7:].strip())
    request.state.oidc_claims = claims   # 端点内可取（本期未消费，角色卡接线位）


# ---- W1 站点墙分域清单（登录墙设计 A1，2026-10-15 · ADR-0015） ----
# 墙域＝需登录（on 态）：/v1/portal/*（13 条 GET 全墙内）、/v1/admin/*（含 GET，
# 「读公开」废除）、/demo（页面＋查询两条）。豁免最小集（判定顺序先于墙域）：
#   /v1/admin/auth/*  登录闭环三端点——pre-token，挂闸＝死锁（061b6a2 教训）；
#   /healthz、/v1/ping  探活（容器编排/端到端链路验证不可挂登录）；
#   /v1/query/、/v1/tags/query、/v1/batch/*、/proxy/  数据面 AppKey 自鉴权域
#   （机器调用方，网关 key-auth／grant 闸自管，不进站点墙）。
# 清单测试升 v2（tests/test_auth_oidc.py）：AST 全路由 ∈{墙域}∪{豁免} 全覆盖；
# 删豁免须回改地板，新端点必须分域（防漏挂是清单存在的意义）。
SITE_WALL_EXACT = frozenset({"/demo"})
SITE_WALL_PREFIXES = ("/v1/portal/", "/v1/admin/", "/demo/")
SITE_WALL_EXEMPT_EXACT = frozenset({"/healthz", "/v1/ping", "/v1/tags/query"})
SITE_WALL_EXEMPT_PREFIXES = (
    "/v1/admin/auth/",   # 登录闭环（login 密码流/config 发现/callback 换 token）：先于 token
    "/v1/query/",        # 数据面通用查询（AppKey 域，ADR-0008）
    "/v1/tags/query",    # 数据面标签点查（AppKey 域）
    "/v1/batch/",        # 批量任务提交/状态/结果（AppKey 域）
    "/proxy/",           # B 型代理数据面透传（grant 闸自鉴权，ADR-0013）
)


def site_wall_hit(path: str) -> bool:
    """站点墙分域判定（纯逻辑，宿主机可测）：True＝on 态需 Bearer 登录。

    豁免最小集优先于墙域判定（/v1/admin/auth/* 命中豁免即放行，不被
    /v1/admin/ 前缀吞掉——豁免清单若被误删由清单测试地板拦截）。"""
    if path in SITE_WALL_EXEMPT_EXACT:
        return False
    if any(path.startswith(p) for p in SITE_WALL_EXEMPT_PREFIXES):
        return False
    return path in SITE_WALL_EXACT or path.startswith(SITE_WALL_PREFIXES)


# ---- W4 角色闸（登录墙设计 B1 · ADR-0016）：Casdoor tag 单一事实，缺省 viewer ----
# 三角色存 Casdoor 用户 tag（#49），随 JWT 进入后端（tokenFormat=JWT 内嵌 User，
# 顶层 tag claim 即用户 tag——Casdoor token_jwt.go Claims.Tag，实测见 ADR-0016）；
# require_role 本地裁决，不回调 Casdoor、不自建用户表。tag 缺失/未知一率按
# viewer 兜底（最低权限）。tokenFields 注入口径：tokenFields 仅 JWT-Custom 格式
# 生效；本应用 tokenFormat=JWT 下角色已随 tag claim 在 token 内，等效达成设计
# 「tokenFields 注入角色进 JWT」的意图（ADR-0016 Decision.2 有记录）。
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)
ROLE_RANK = {ROLE_VIEWER: 0, ROLE_OPERATOR: 1, ROLE_ADMIN: 2}


def role_from_claims(claims) -> str:
    """JWT claims → 三角色（tag claim；缺/未知一率 viewer 最低权限兜底）。"""
    tag = str((claims or {}).get("tag") or "").strip().lower()
    return tag if tag in ROLE_RANK else ROLE_VIEWER


class RoleForbiddenError(Exception):
    """已登录但角色不足（main 注册 handler → 403 role_forbidden 信封，W4 新码）。

    细分进 message：需要 min_role，实际 actual（JWT tag 缺省 viewer）。"""

    def __init__(self, min_role: str, actual: str):
        self.min_role = min_role
        self.actual = actual
        super().__init__(f"min_role={min_role} actual={actual}")


def role_forbidden_response(exc: RoleForbiddenError):
    """403 role_forbidden 信封（错误码中心同源；handler 调用，fastapi 环境）。"""
    return JSONResponse(
        errors.envelope(errors.ROLE_FORBIDDEN,
                        f"当前账号角色（{exc.actual}）无权执行该操作，需要 {exc.min_role} 及以上"),
        status_code=403)


def _ensure_role_gate_handler(request) -> None:
    """角色闸拒绝前自举：当前 app 未注册 RoleForbiddenError→403 信封转换时当场补挂。

    main 注册链已挂同款（role_forbidden_response）＝幂等跳过、行为零变化；单测/
    最小 app（FastAPI()＋router 直挂、未走 main 注册链，如 test_accounts_admin 的
    _accounts_app）不补挂则 RoleForbiddenError 无 handler 接住，穿透到
    ServerErrorMiddleware 逃逸成 TestClient 裸异常（2026-10-15 回归实锤：登录墙/
    账号工程批引入，当时宿主缺 pyjwt 提前 skip 未暴露）。挂的是 scope 注入的
    handler 表引用（ExceptionMiddleware 持有、每请求注入），对本 app 即刻生效。
    """
    scope = getattr(request, "scope", None) or {}   # 依赖级直测 _FakeRequest 无 scope＝无处可挂，跳过
    pair = scope.get("starlette.exception_handlers")
    if pair is not None and RoleForbiddenError not in pair[0]:
        pair[0][RoleForbiddenError] = (   # 同 main 注册链形态：handler 收 (request, exc)
            lambda req, exc: role_forbidden_response(exc))


def _ensure_role(request, min_role: str) -> None:
    """角色裁决共用体：claims 角色与 min_role 比秩，不足 raise 403。"""
    actual = role_from_claims(getattr(request.state, "oidc_claims", None))
    if ROLE_RANK[actual] < ROLE_RANK[min_role]:
        _ensure_role_gate_handler(request)   # 未注册 handler 的最小 app 自举（幂等）
        raise RoleForbiddenError(min_role, actual)


def require_role(min_role: str):
    """依赖工厂：require_login 之上挂角色裁决（设计 B1：require_role 挂闸之上）。

    on 态：先登录（站点墙已验则短路）再比角色，不足 403 role_forbidden；
    off 态恒放行（单测/裸跑口径与 require_login 同源，零分叉）。
    request 须带 Request 注解：本依赖经 Depends() 进入 FastAPI 参数分析，裸参数
    名 request 会被误判为 query 参数（422）——宿主机无 fastapi 时注解为惰性
    字符串不求值（from __future__ annotations），纯逻辑直测不受影响。"""
    async def _dep(request: Request) -> None:
        await require_login(request)
        if auth_enabled():
            _ensure_role(request, min_role)
    return _dep


async def adjudicate_route_role(request) -> None:
    """按 ROUTE_MIN_ROLE 矩阵对当前路由做角色裁决（_admin_gate 尾部调用）。

    键＝"<METHOD> <路由模板>"（request.scope["route"].path；依赖在路由匹配后
    执行，scope 必有 route）。未列出＝只要求登录（墙域既有语义，写路由漏标
    min_role 由清单测试强制补表）；off 态 no-op。"""
    if not auth_enabled():
        return
    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    min_role = ROUTE_MIN_ROLE.get(f"{request.method} {template}")
    if min_role is None:
        return
    await require_login(request)   # GET 族此前不挂登录，矩阵行自带登录前置
    _ensure_role(request, min_role)


# 权限矩阵（设计 B1 · 7 能力域×3 角色）落地为路由级 min_role 单一事实表。
# 域归属：门户只读（域1）与计量查看（域7）全角色——域1 靠墙域登录即可、域7 标
# viewer 地板；发布装载 schema 上下线探测（域2）与凭证发放配额续期（域3）＝
# operator；凭证删除、reload/回滚、upstreams 增删（域4）与账号管理（域5，W5）＝
# admin；审计/monitor/perf 查看（域6）＝operator。运营台 GET 读＝各能力域控制
# 台视图，随域 operator 地板（viewer 只读门户 C 面与计量，不进运营台）。
ROUTE_MIN_ROLE: dict = {
    # 域3 凭证发放/配额/续期（operator+）
    "POST /v1/admin/appkeys": ROLE_OPERATOR,
    "POST /v1/admin/appkeys/{appkey}/status": ROLE_OPERATOR,
    "POST /v1/admin/appkeys/{appkey}/rotate": ROLE_OPERATOR,
    "POST /v1/admin/appkeys/{appkey}/quota": ROLE_OPERATOR,
    "POST /v1/admin/appkeys/{appkey}/expires": ROLE_OPERATOR,
    # 域4 凭证删除（admin）
    "POST /v1/admin/appkeys/{appkey}/delete": ROLE_ADMIN,
    # 域4 reload/回滚/upstreams 增删/版本清理（admin）
    "POST /v1/admin/reload": ROLE_ADMIN,
    # 域5 账号管理（admin；admin_accounts.py W5 卡路由，矩阵设计 B2 口径）
    "POST /v1/admin/accounts/users": ROLE_ADMIN,
    "POST /v1/admin/accounts/users/{name}/status": ROLE_ADMIN,
    "POST /v1/admin/accounts/users/{name}/role": ROLE_ADMIN,
    "POST /v1/admin/accounts/users/{name}/password": ROLE_ADMIN,
    "POST /v1/admin/accounts/users/{name}/delete": ROLE_ADMIN,
    "GET /v1/admin/accounts/users": ROLE_ADMIN,
    "POST /v1/admin/versions/delete": ROLE_ADMIN,
    "POST /v1/admin/publish/sync/rollback": ROLE_ADMIN,
    "POST /v1/admin/upstreams": ROLE_ADMIN,
    "POST /v1/admin/upstreams/{pid}/offline": ROLE_ADMIN,
    "POST /v1/admin/upstreams/{pid}/delete": ROLE_ADMIN,   # 删除草稿（2026-10-20；线上条目须先下线）
    # 域2 发布/装载/schema/上下线/探测（operator+）
    "POST /v1/admin/publish": ROLE_OPERATOR,
    "POST /v1/admin/publish/{sid}/offline": ROLE_OPERATOR,
    "POST /v1/admin/publish/sync/switch": ROLE_OPERATOR,
    "POST /v1/admin/publish/hbase/test": ROLE_OPERATOR,
    "POST /v1/admin/publish/hbase/load": ROLE_OPERATOR,
    "POST /v1/admin/load": ROLE_OPERATOR,
    "POST /v1/admin/upstreams/probe": ROLE_OPERATOR,
    "POST /v1/admin/perf/run": ROLE_OPERATOR,
    "POST /v1/admin/schema/parse": ROLE_OPERATOR,
    "POST /v1/admin/schema/save": ROLE_OPERATOR,
    # 域6 审计/monitor/perf 查看（operator+；portal 侧审计查询同域同地板）
    "GET /v1/portal/audit/logs": ROLE_OPERATOR,
    "GET /v1/admin/monitor/errors": ROLE_OPERATOR,
    "GET /v1/admin/monitor/trace": ROLE_OPERATOR,
    "GET /v1/admin/monitor/batch": ROLE_OPERATOR,
    "GET /v1/admin/perf/status": ROLE_OPERATOR,
    "GET /v1/admin/perf/reports": ROLE_OPERATOR,
    "GET /v1/admin/perf/report/{run}/{fname}": ROLE_OPERATOR,
    # 域7 计量查看（全角色：viewer 地板＝「仅要求登录」的显式声明）
    "GET /v1/admin/metering/summary": ROLE_VIEWER,
    # 域7 报表专区（1b-9 报表专区一期，设计稿 §1/§7-7）：四端点族全部入清单——
    # 明细已掩码、聚合只读面，viewer 地板（§9 拍板 4「主体明细 viewer 地板三处统一」）；
    # 漏标由 tests/test_auth_oidc.py 清单守卫用例拦截（W4 防回潮）
    "GET /v1/admin/usage/overview": ROLE_VIEWER,
    "GET /v1/admin/usage/subjects": ROLE_VIEWER,
    "GET /v1/admin/usage/services": ROLE_VIEWER,
    "GET /v1/admin/usage/trend": ROLE_VIEWER,
    # 域7 扩注·账单（管理功能群一期 C 节·账单页＋CSV，设计稿 §3.5）：读列表/详情/
    # 导出＝viewer（报表中心组专区先例：页面明细调用方掩码；CSV 分层口径详 §3.4/
    # 拍板点④——明细级 CSV 凭证完整串仅落交付物＋bill_export 审计留痕）；确认/
    # 补录单号＝admin（OA 登记权威面，ADR-0003 口径）。六行漏标由 tests/test_auth_oidc.py
    # 清单守卫用例拦截（W4 防回潮）
    "GET /v1/admin/billing/periods": ROLE_VIEWER,
    "GET /v1/admin/billing/periods/{period}": ROLE_VIEWER,
    "GET /v1/admin/billing/periods/{period}/export": ROLE_VIEWER,
    "GET /v1/admin/billing/periods/{period}/export-lines": ROLE_VIEWER,
    "POST /v1/admin/billing/periods/{period}/confirm": ROLE_ADMIN,
    "POST /v1/admin/billing/periods/{period}/register-oa": ROLE_ADMIN,
    # 域8 授权管理（1b-8 授权管理页）：只读详情地板＝viewer（设计稿拍板「下钻地板」
    # 三处统一裁定）；写操作＝admin（授权登记是租户级权威面，高于签发域 operator 地板）
    "GET /v1/admin/grants/tenants": ROLE_VIEWER,
    "GET /v1/admin/grants/tenants/{tenant}": ROLE_VIEWER,
    "POST /v1/admin/grants": ROLE_ADMIN,
    # 域8 扩注（管理功能群一期 A 节·租户管理页，设计稿 §1.4 拍板）：读地板＝viewer
    # （下钻地板三处统一同源）；写＝admin（租户是高于授权面的平台主体权威面，沿授权
    # 写面先例）。resync-consumers＝幂等对账摘挂写面（P1-③）归 admin；五行漏标由
    # tests/test_auth_oidc.py 清单守卫用例拦截（W4 防回潮）
    "GET /v1/admin/tenants": ROLE_VIEWER,
    "POST /v1/admin/tenants": ROLE_ADMIN,
    "POST /v1/admin/tenants/resync-consumers": ROLE_ADMIN,
    "POST /v1/admin/tenants/{tenant_id}/freeze": ROLE_ADMIN,
    "POST /v1/admin/tenants/{tenant_id}/unfreeze": ROLE_ADMIN,
    # 域9 计费管理（管理功能群一期 B 节·价目管理页，设计稿 §2.4 v1.1 拍板③）：价目读
    # 面＝viewer（账单页 bill_line.unit_price 快照本就含商务数值，保密口径不成立——
    # v1「operator 读」改判）；写（新建/停用）＝admin（计费权威面）。命中预演/覆盖率/
    # 选择器＝只读统计随读面 viewer。七行漏标由 tests/test_auth_oidc.py 清单守卫用例
    # 与 tests/test_pricing_api.py AST 契约守卫双重拦截（W4 防回潮）
    "GET /v1/admin/pricing/items": ROLE_VIEWER,
    "GET /v1/admin/pricing/items/{item_id}": ROLE_VIEWER,
    "GET /v1/admin/pricing/selector": ROLE_VIEWER,
    "GET /v1/admin/pricing/hit-preview": ROLE_VIEWER,
    "GET /v1/admin/pricing/coverage": ROLE_VIEWER,
    "POST /v1/admin/pricing/items": ROLE_ADMIN,
    "POST /v1/admin/pricing/items/{item_id}/retire": ROLE_ADMIN,
    # 运营台读（域2/3 控制台视图：装载/发布/凭证/上游清单＝operator+）
    "GET /v1/admin/appkeys": ROLE_OPERATOR,
    "GET /v1/admin/appkeys/usage": ROLE_OPERATOR,
    "GET /v1/admin/publish": ROLE_OPERATOR,
    "GET /v1/admin/publish/hbase/discovery": ROLE_OPERATOR,
    "GET /v1/admin/publish/hbase/selftest": ROLE_OPERATOR,
    "GET /v1/admin/publish/sync/status": ROLE_OPERATOR,
    "GET /v1/admin/load/sources": ROLE_OPERATOR,
    "GET /v1/admin/load/tables": ROLE_OPERATOR,
    "GET /v1/admin/load/tasks/{task_id}": ROLE_OPERATOR,
    "GET /v1/admin/load/assets": ROLE_OPERATOR,
    "GET /v1/admin/load/schema": ROLE_OPERATOR,
    "GET /v1/admin/versions": ROLE_OPERATOR,
    "GET /v1/admin/upstreams": ROLE_OPERATOR,
}

# ---- 登录闭环支撑端点（/v1/admin/auth/*：复用既有已代理前缀，免动网关路由；
#      本路由不挂 _admin_gate——登录前本来无 token，回调换 token 即登录本身） ----

router = APIRouter() if _HAS_FASTAPI else None   # 宿主机无 fastapi 时纯逻辑仍可测


def _redirect_uri() -> str:
    """Casdoor 应用登记的回跳地址（管理页静态回跳页；与 compose 口径同源）。"""
    return os.environ.get("OIDC_REDIRECT_URI", "http://localhost:8000/admin/auth-callback.html")


if _HAS_FASTAPI:   # 端点区（宿主机无 fastapi 不定义；main.py 仅容器态导入）

    @router.get("/v1/admin/auth/config", response_model=None)
    async def auth_config(request: Request) -> JSONResponse:
        """登录配置发现（前端拼授权 URL 用；mode 供前端区分是否启用登录）。

        #34（2026-10-15 复审 P2-1）：state 不再静态 "admin"——每次发现请求生成
        一次性随机值并下发签名短时 cookie（oneserve_oidc_state，HttpOnly＋SameSite=Lax，
        TTL＝_STATE_TTL_S），callback 校验比对后即刻清除（会话内一次性）。
        login-CSRF 面封堵：攻击者无法预知受害会话的 state，伪造回跳换 token 即 401。
        最小实现＝无状态签名 cookie（HMAC-SHA256，密钥复用 client_secret——后端
        私有、不进浏览器逻辑），零新增存储组件，不过度设计。

        双模分派（ADR-0027 步1）：native 时改走 auth_native.config_response
        （无授权 URL/state cookie；provider 字段供前端区分登录形态）。"""
        _nat = _native()
        if _nat is not None:
            return _nat.config_response()
        cfg = _cfg()
        state = make_state(cfg["client_secret"])
        params = urllib.parse.urlencode({
            "client_id": cfg["client_id"],
            "response_type": "code",
            "scope": "openid profile",
            "redirect_uri": _redirect_uri(),
            "state": state,
        })
        resp = JSONResponse({
            "mode": cfg["mode"],
            "loginUrl": cfg["issuer"] + "/login/oauth/authorize?" + params,
            "clientId": cfg["client_id"],
            "redirectUri": _redirect_uri(),
            # W1 站点墙标记（ADR-0015）：活栈清单探测据此识别栈代码是否含站点墙
            # （旧栈无此字段→墙语义探测显式 skip，不误报；前端零消费可忽略）。
            "siteWall": True,
        })
        resp.set_cookie("oneserve_oidc_state", state, max_age=_STATE_TTL_S,
                        httponly=True, samesite="lax", path="/")
        return resp

    @router.get("/v1/admin/auth/callback", response_model=None)
    async def auth_callback(request: Request, code: str = "",
                            state: str = "") -> JSONResponse:
        """授权码换 token（后端简化路径：client_secret 只在本端使用，不落前端）。

        Casdoor token 端点＝/api/login/oauth/access_token（form 表单，授权码流）。
        成功回 {accessToken, expiresIn, tokenType}；失败 401 auth_required 信封。

        #34（复审 P2-1 login-CSRF）：先验 state——query state 须与 config 发现时
        下发的签名 cookie 一致、签名有效且未过期（check_state 纯逻辑）；任何不符
        （含 cookie 缺失）fail-closed 401 bad_state（不发起 token 交换）；成功路径
        即刻清除 cookie（会话内一次性，防重放二次消费）。主用户流＝品牌密码登录页
        （POST /v1/admin/auth/login），本端点为授权码后备通道——浏览器闭环必经
        config 下发 cookie，无 cookie 即非正常回跳，拒绝对称安全。

        双模分派（ADR-0027 步1）：native 无授权码流——404 service_not_found
        信封（不静默试 Casdoor）。"""
        _nat = _native()
        if _nat is not None:
            return _nat.callback_response()
        if not code:
            return auth_required_response(AuthRequiredError("missing_token"))
        cfg = _cfg()
        ok, reason = check_state(state, request.cookies.get("oneserve_oidc_state"),
                                 cfg["client_secret"])
        if not ok:
            logger.warning("OIDC callback state 校验失败（reason=%s）——login-CSRF 拦截", reason)
            return auth_required_response(AuthRequiredError("bad_state"))
        data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "code": code,
            "redirect_uri": _redirect_uri(),
        }).encode()

        def _exchange() -> dict:
            req = urllib.request.Request(
                cfg["internal"] + "/api/login/oauth/access_token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            payload = await asyncio.to_thread(_exchange)
        except Exception as exc:   # noqa: BLE001 —— 换 token 失败 fail-closed 401
            logger.warning("OIDC 授权码换 token 失败：%s", exc)
            return auth_required_response(AuthRequiredError("jwks_unavailable"))
        access = payload.get("access_token") or ""
        if not access:
            return auth_required_response(AuthRequiredError("bad_token"))
        resp = JSONResponse({
            "accessToken": access,
            "expiresIn": payload.get("expires_in") or payload.get("expires_in_sec") or 0,
            "tokenType": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", ""),
            "state": state,
        })
        resp.delete_cookie("oneserve_oidc_state", path="/")   # 一次性：换到即焚
        return resp

    @router.post("/v1/admin/auth/login", response_model=None)
    async def auth_login(request: Request) -> JSONResponse:
        """账号密码登录（2026-10-15 品牌登录页后端：Casdoor 密码流换 token）。

        门户 /login.html（OneServe 门面）提交账号密码到本端点——后端走 Casdoor
        密码流（grant_type=password，应用 grantTypes 已开——live 闭环同款）换
        access token 返前端；client_secret 只在本端使用，浏览器全程不出 8000、
        不见 Casdoor 门面（19000 仅后端依赖）。成功口径与 /v1/admin/auth/callback
        同形（accessToken/expiresIn/tokenType）。失败人话（复用错误码中心既有
        码，不新增）：缺字段 400 invalid_request；凭据错 401 unauthorized；
        Casdoor 不可达 502 upstream_unreachable（fail-closed 不降级）。

        双模分派（ADR-0027 步1）：native 时改走 auth_native.login_response
        （users.yaml 本地验密＋HS256 签发；防爆破 5 连错冻 15 分钟）。"""
        try:
            body = await request.json()
        except Exception:                                    # noqa: BLE001 —— 非法 JSON 按缺字段口径
            body = {}
        username = str((body or {}).get("username") or "").strip()
        password = str((body or {}).get("password") or "")
        if not username or not password:
            return JSONResponse(
                errors.envelope(errors.INVALID_REQUEST, "请输入账号和密码"), status_code=400)
        _nat = _native()
        if _nat is not None:
            return _nat.login_response(username, password)
        cfg = _cfg()
        data = urllib.parse.urlencode({
            "grant_type": "password",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "username": username,
            "password": password,
        }).encode()

        def _password_grant() -> dict:
            req = urllib.request.Request(
                cfg["internal"] + "/api/login/oauth/access_token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            payload = await asyncio.to_thread(_password_grant)
        except urllib.error.HTTPError as exc:
            # Casdoor 4xx 拒绝。MFA Required（org mfaItems）下未绑定 TOTP 的用户，
            # ROPC 无 MFA 交互载体被拒（error_description 含 set up MFA）——给出可
            # 判别的 mfa_required 错误码＋指路统一登录页（2026-10-20 MFA 落地）；
            # 其余（密码错/用户禁用等）按凭据错口径人话回，不透传内部细节。
            try:
                detail = (exc.read() or b"").decode("utf-8", "ignore")
            except Exception:                                  # noqa: BLE001
                detail = ""
            if "MFA" in detail:
                logger.warning("OIDC 密码流登录被拒（MFA Required，用户未绑定）：HTTP %s", exc.code)
                return JSONResponse(
                    errors.envelope(errors.UNAUTHORIZED, "该账号已启用多因素认证：请经统一登录页完成绑定/验证"),
                    status_code=401, headers={"X-Error-Code": "mfa_required"})
            logger.warning("OIDC 密码流登录被拒（HTTP %s）", exc.code)
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "账号或密码不正确，请重试"), status_code=401)
        except Exception as exc:                             # noqa: BLE001 —— Casdoor 不可达 fail-closed
            logger.warning("OIDC 密码流登录失败（Casdoor 不可达？）：%s", exc)
            return JSONResponse(
                errors.envelope(errors.UPSTREAM_UNREACHABLE, "登录服务暂不可达，请稍后重试"),
                status_code=502)
        access = payload.get("access_token") or ""
        if not access:
            # 200 但无 token：Casdoor 部分版本把凭据错走此形态——同按凭据错口径
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "账号或密码不正确，请重试"), status_code=401)
        return JSONResponse({
            "accessToken": access,
            # 权限方案 v1.1 裁决#1（token 吊销档 2，2026-10-16）：TTL 缩至 2h 后
            # 需 refresh 自动续期——refresh_token 随登录下发（应用 grantTypes 已含
            # refresh_token，此前未用；前端 site-gate 过期前自动走 /v1/admin/auth/refresh）。
            "refreshToken": payload.get("refresh_token") or "",
            "expiresIn": payload.get("expires_in") or payload.get("expires_in_sec") or 0,
            "tokenType": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", ""),
        })

    @router.post("/v1/admin/auth/refresh", response_model=None)
    async def auth_refresh(request: Request) -> JSONResponse:
        """无感刷新（权限方案 v1.1 裁决#1·档 2）：refresh_token grant 换新 access。

        前端 site-gate.js 在 token 临期（≤5 分钟）/已过期且本地有 refreshToken 时
        调本端点；与 /v1/admin/auth/login 同通道（Casdoor 标准令牌端点，
        grant_type=refresh_token，轮换式——返回新 refresh_token）。client_secret
        只在本端使用（浏览器不出 8000 口径不变）。失败人话复用既有码：缺字段
        400 invalid_request；refresh_token 无效/被吊销 401 unauthorized（前端
        收 401 即清 token 跳登录页）；Casdoor 不可达 502 upstream_unreachable。

        双模分派（ADR-0027 步1）：native 时改走 auth_native.refresh_response
        （本地 HS256 验 refresh JWT＋轮换发新对）。"""
        try:
            body = await request.json()
        except Exception:                                    # noqa: BLE001 —— 非法 JSON 按缺字段口径
            body = {}
        refresh_token = str((body or {}).get("refreshToken") or "")
        if not refresh_token:
            return JSONResponse(
                errors.envelope(errors.INVALID_REQUEST, "缺少 refreshToken"), status_code=400)
        _nat = _native()
        if _nat is not None:
            return _nat.refresh_response(refresh_token)
        cfg = _cfg()
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "refresh_token": refresh_token,
        }).encode()

        def _refresh_grant() -> dict:
            req = urllib.request.Request(
                cfg["internal"] + "/api/login/oauth/access_token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            payload = await asyncio.to_thread(_refresh_grant)
        except urllib.error.HTTPError as exc:
            logger.warning("OIDC refresh 流被拒（HTTP %s）", exc.code)
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "登录态已失效，请重新登录"), status_code=401)
        except Exception as exc:                             # noqa: BLE001 —— Casdoor 不可达 fail-closed
            logger.warning("OIDC refresh 流失败（Casdoor 不可达？）：%s", exc)
            return JSONResponse(
                errors.envelope(errors.UPSTREAM_UNREACHABLE, "登录服务暂不可达，请稍后重试"),
                status_code=502)
        access = payload.get("access_token") or ""
        if not access:
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "登录态已失效，请重新登录"), status_code=401)
        return JSONResponse({
            "accessToken": access,
            "refreshToken": payload.get("refresh_token") or "",
            "expiresIn": payload.get("expires_in") or payload.get("expires_in_sec") or 0,
            "tokenType": payload.get("token_type", "Bearer"),
            "scope": payload.get("scope", ""),
        })
