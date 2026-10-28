# -*- coding: utf-8 -*-
"""native 登录模式（ADR-0027 步1 · 登录本地化——去 Casdoor，2026-10-27）。

背景：Casdoor 差异化能力全关，现存价值仅「验密码＋发 JWT＋存用户」三件小事，
却扛 3 容器＋全部部署坑（ADR-0027）。本模块＝AUTH_PROVIDER=native 时的完整
登录后端；oidc 模式（service/app/auth_oidc.py 原 RS256/JWKS 链路）不动，双模
经 AUTH_PROVIDER 在 auth_oidc 各入口分派（verify/login/refresh/config/callback）。

职责（步1 范围；账号页 CRUD＝步3、登录审计直写＝步4）：
  - users.yaml 用户表（service/users.yaml）：tmp＋os.replace 原子写（先例＝
    upstreams.yaml/appkeys.yaml）；结构为步3 账号页 CRUD 铺垫——
    users.<name> -> name/password_hash/role/disabled/created_at/updated_at；
  - 首启种子：ONESERVE_SEED_ADMIN_USER/PASSWORD 都设置且 users.yaml 无该用户时
    建 admin（bcrypt 哈希；明文不落盘不进日志；已存在即跳过不改密＝幂等）；
  - login：本地验密（bcrypt）＋签发 HS256 JWT（ONESERVE_AUTH_SECRET 签名；
    claims 带 tag 三角色与 Casdoor token 对齐——sub/name/tag/iss/aud/exp/iat，
    role_from_claims 零改动消费）；/v1/admin/auth/* 契约字段不变
    （accessToken/refreshToken/expiresIn/tokenType/scope）；
  - verify：本地 HS256 验签（fail-closed：坏签名/过期/错 type 一律
    AuthRequiredError bad_token→401 auth_required 同形信封；密钥缺失＝
    secret_missing，依赖缺失＝dependency_missing）；
  - refresh：轮换式发新对（access 2h／refresh 7d，对齐权限方案 v1.1 裁决#1
    Casdoor TTL 档）；禁用/删除用户旧 refresh 不续命；
  - 防爆破：复刻「5 连错冻结 15 分钟」（内存计数，重启即清——Casdoor 原语义
    同为进程内计数；冻结时人话报错，成功登录清零）。

安全口径：密码明文永不落盘、不落日志（写盘前断言同 appkeys.save_appkeys）；
HS256 对称密钥＝ONESERVE_AUTH_SECRET（.env 注入，轮换＝换值重启、旧 token 全失效）。
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app import errors

logger = logging.getLogger("oneserve.auth")

SERVICE_DIR = Path(__file__).resolve().parent.parent          # service/
USERS_YAML = SERVICE_DIR / "users.yaml"                        # 用户表权威源（步3 CRUD 同源）

# ---- 常量（token 口径；与 Casdoor 既有档对齐——权限方案 v1.1 裁决#1 TTL 2h） ----
ACCESS_TTL_S = 2 * 60 * 60            # access token 2 小时
REFRESH_TTL_S = 7 * 24 * 60 * 60      # refresh token 7 天（轮换式，随每次刷新换新）
NATIVE_ISSUER = "oneserve"            # native 自签 token 的 iss（本地验签同源校验）
NATIVE_AUDIENCE = "oneserve-admin"    # aud 受众（对齐 Casdoor「token aud=应用」语义）
TYPE_ACCESS = "access"
TYPE_REFRESH = "refresh"

# ---- 防爆破（复刻 Casdoor「5 连错冻结 15 分钟」语义；内存计数） ----
MAX_LOGIN_FAILS = 5
FREEZE_SECONDS = 15 * 60
WRONG_PW_MESSAGE = "账号或密码不正确，请重试"
FROZEN_MESSAGE = "密码连续错误次数过多，账号已临时锁定，请 15 分钟后再试"
DISABLED_MESSAGE = "账号已被禁用，请联系管理员"

# 冻结计数（进程内；键=提交的用户名——含不存在用户名同样计数，防在线枚举）
_FAIL_STATE: dict[str, dict] = {}

_HEADER_COMMENT = """\
# ============================================================
# OneServe 用户表（service/users.yaml）——native 登录模式唯一权威源（ADR-0027 步1）
# 写入方：首启种子（ONESERVE_SEED_ADMIN_*，幂等）＋账号管理页 CRUD（步3 同契约换芯）；
# 原子写（tmp＋os.replace，先例＝upstreams.yaml/appkeys.yaml），本文件随仓库 git 留痕。
# 安全纪律：password_hash 只存 bcrypt 哈希（$2b$...），明文密码永不落盘、不落日志。
# 结构：users.<name> -> name / password_hash / role(admin|operator|viewer)
#       / disabled(bool) / created_at / updated_at（disabled=true 拒登录）
# ============================================================"""


def users_path() -> Path:
    """用户表路径（每次读 env——测试/隔离栈注入 ONESERVE_USERS_YAML 零全局态）。"""
    return Path(os.environ.get("ONESERVE_USERS_YAML") or USERS_YAML)


def load_users(path: Path | None = None) -> dict:
    """读用户表；文件不存在＝空表（首启种子前的正常态）。坏文件如实抛（fail-closed）。"""
    p = path or users_path()
    if not p.is_file():
        return {"users": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    users = data.get("users") or {}
    if not isinstance(users, dict):
        raise RuntimeError(f"{p.name} users 须为映射（name -> 用户条目）")
    return {"users": users}


def dump_users(doc: dict) -> str:
    body = yaml.safe_dump(doc, sort_keys=True, allow_unicode=True, default_flow_style=False)
    return _HEADER_COMMENT + "\n" + body


def save_users(path: Path, doc: dict, *, password_plain: str | None = None) -> None:
    """原子写盘（tmp＋rename，目录内 rename 安全）；写前断言明文密码不出现（安全红线，
    同 appkeys.save_appkeys）。"""
    text = dump_users(doc)
    if password_plain:
        assert password_plain not in text, "安全红线：密码明文不得写入用户表"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- bcrypt（懒加载：宿主机缺依赖时 import 本模块不炸；缺依赖＝验密 fail-closed） ----

def hash_password(password: str) -> str:
    import bcrypt   # 懒加载（镜像 requirements 有；宿主单测环境缺则 login 路径如实报依赖缺失）
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    import bcrypt
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False   # 坏哈希（手改文件等）＝验不过，不炸登录端点


_DUMMY_HASH: list[str] = []   # 惰性算一次：不存在用户名也做一次同代价比对（防时序枚举）


def _dummy_hash() -> str:
    if not _DUMMY_HASH:
        _DUMMY_HASH.append(hash_password("oneserve-timing-equalizer"))
    return _DUMMY_HASH[0]


# ---- 首启种子（幂等：两 env 任一未设置＝no-op；用户已存在＝跳过不改密） ----

def seed_admin(path: Path | None = None) -> bool:
    """ONESERVE_SEED_ADMIN_USER/PASSWORD 都设置且该用户不存在时建 admin（bcrypt）。

    返回是否新建；密码明文不落日志。已存在即跳过（二次启动不重复建、不改密——
    语义对齐 casdoor-init bootstrap_admin.py 先例）。"""
    name = (os.environ.get("ONESERVE_SEED_ADMIN_USER") or "").strip()
    password = os.environ.get("ONESERVE_SEED_ADMIN_PASSWORD") or ""
    if not name or not password:
        return False
    p = path or users_path()
    doc = load_users(p)
    if name in doc["users"]:
        return False
    now = _now_iso()
    doc["users"][name] = {
        "name": name,
        "password_hash": hash_password(password),
        "role": "admin",
        "disabled": False,
        "created_at": now,
        "updated_at": now,
    }
    save_users(p, doc, password_plain=password)
    return True


# ---- HS256 token（claims 结构与 Casdoor token 对齐：sub/name/tag/iss/aud/exp/iat） ----

def _secret() -> str:
    return (os.environ.get("ONESERVE_AUTH_SECRET") or "").strip()


def has_secret() -> bool:
    return bool(_secret())


def issue_token_pair(username: str, role: str) -> dict:
    """签发 access＋refresh（HS256）；响应字段与 /v1/admin/auth/login 契约同形。"""
    import jwt
    from app.auth_oidc import ROLES   # 懒加载（auth_oidc 分派点反向懒加载本模块，避免环）
    secret = _secret()
    if not secret:
        raise _auth_required("secret_missing")
    tag = role if role in ROLES else "viewer"   # 未知角色一率 viewer（role_from_claims 同口径）
    now = int(time.time())

    def _claims(ttl: int, token_type: str) -> dict:
        return {"iss": NATIVE_ISSUER, "aud": NATIVE_AUDIENCE, "sub": username,
                "name": username, "tag": tag, "type": token_type,
                "iat": now, "exp": now + ttl, "jti": secrets.token_hex(8)}

    return {
        "accessToken": jwt.encode(_claims(ACCESS_TTL_S, TYPE_ACCESS), secret, algorithm="HS256"),
        "refreshToken": jwt.encode(_claims(REFRESH_TTL_S, TYPE_REFRESH), secret, algorithm="HS256"),
        "expiresIn": ACCESS_TTL_S,
        "tokenType": "Bearer",
        "scope": "",
    }


def _auth_required(reason: str):
    """构造 AuthRequiredError（懒加载同环避口径；reason 进 401 auth_required 信封 message）。"""
    from app.auth_oidc import AuthRequiredError
    return AuthRequiredError(reason)


def _decode(token: str, expected_type: str) -> dict:
    """本地 HS256 验签（签名/exp/iss/aud/type 全校验）；失败一律 AuthRequiredError。"""
    try:
        import jwt
    except Exception as exc:   # noqa: BLE001
        raise _auth_required("dependency_missing") from exc
    secret = _secret()
    if not secret:
        raise _auth_required("secret_missing")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"],
                            issuer=NATIVE_ISSUER, audience=NATIVE_AUDIENCE)
    except Exception as exc:   # noqa: BLE001 —— 签名/过期/iss/aud 任一不符＝坏 token
        raise _auth_required("bad_token") from exc
    if claims.get("type") != expected_type:
        raise _auth_required("bad_token")
    return claims


def verify_access_token(token: str) -> dict:
    """native access token 验签（站点墙/require_login 经 auth_oidc 分派到此）。"""
    return _decode(token, TYPE_ACCESS)


def verify_refresh_token(token: str) -> dict:
    return _decode(token, TYPE_REFRESH)


# ---- 防爆破（内存计数；复刻 Casdoor「5 连错冻结 15 分钟」） ----

def _frozen_remaining(username: str) -> int:
    st = _FAIL_STATE.get(username)
    if not st or not st.get("frozen_until"):
        return 0
    return max(0, int(st["frozen_until"] - time.time()))


def _record_fail(username: str) -> tuple[int, bool]:
    """记一次失败；连错满 MAX_LOGIN_FAILS 置 frozen_until。返回 (累计失败数, 是否刚触发冻结)。"""
    st = _FAIL_STATE.setdefault(username, {"fails": 0, "frozen_until": 0.0})
    st["fails"] += 1
    if st["fails"] >= MAX_LOGIN_FAILS and not st.get("frozen_until"):
        st["frozen_until"] = time.time() + FREEZE_SECONDS
        logger.warning("native 登录防爆破：用户 %s 连续错误 %d 次，冻结 %d 分钟",
                       username, st["fails"], FREEZE_SECONDS // 60)
        return st["fails"], True
    return st["fails"], False


def _record_success(username: str) -> None:
    _FAIL_STATE.pop(username, None)   # 成功登录清零（Casdoor 同语义）


def reset_fail_state() -> None:
    """测试卫生：清冻结计数（生产无调用方）。"""
    _FAIL_STATE.clear()


# ---- /v1/admin/auth/* native 分派实现（auth_oidc 端点内分派调用；契约字段不变） ----

def config_response():
    """config 发现（native）：mode 供前端区分是否启用登录；provider 标识双模。"""
    from fastapi.responses import JSONResponse
    from app.auth_oidc import _cfg
    return JSONResponse({"mode": _cfg()["mode"], "provider": "native", "siteWall": True})


def callback_response():
    """授权码回调（native 无授权码流）：404 service_not_found 信封（不静默试 Casdoor）。"""
    from fastapi.responses import JSONResponse
    return JSONResponse(
        errors.envelope(errors.SERVICE_NOT_FOUND,
                        "授权码回调仅在 oidc 模式提供（当前 AUTH_PROVIDER=native）"),
        status_code=404)


def login_response(username: str, password: str):
    """login（native）：本地验密＋签发 HS256 token 对。人话契约与 oidc 通道同形：
    凭据错 401 unauthorized；冻结 401（X-Error-Code=account_frozen）；禁用 401；
    密钥/依赖缺失 fail-closed 401 auth_required；意外错误 500 不裸炸。"""
    from fastapi.responses import JSONResponse
    from app.auth_oidc import auth_required_response
    if _frozen_remaining(username) > 0:
        return JSONResponse(errors.envelope(errors.UNAUTHORIZED, FROZEN_MESSAGE),
                            status_code=401, headers={"X-Error-Code": "account_frozen"})
    try:
        users = load_users()["users"]
        user = users.get(username)
        if user is None:
            verify_password(password, _dummy_hash())   # 同代价比对（防时序枚举）
            _fails, _frozen = _record_fail(username)
            if _frozen:
                return JSONResponse(errors.envelope(errors.UNAUTHORIZED, FROZEN_MESSAGE),
                                    status_code=401, headers={"X-Error-Code": "account_frozen"})
            return JSONResponse(errors.envelope(errors.UNAUTHORIZED, WRONG_PW_MESSAGE),
                                status_code=401)
        if user.get("disabled"):
            return JSONResponse(errors.envelope(errors.UNAUTHORIZED, DISABLED_MESSAGE),
                                status_code=401, headers={"X-Error-Code": "account_disabled"})
        if not verify_password(password, str(user.get("password_hash") or "")):
            _fails, _frozen = _record_fail(username)
            if _frozen:
                return JSONResponse(errors.envelope(errors.UNAUTHORIZED, FROZEN_MESSAGE),
                                    status_code=401, headers={"X-Error-Code": "account_frozen"})
            return JSONResponse(errors.envelope(errors.UNAUTHORIZED, WRONG_PW_MESSAGE),
                                status_code=401)
        _record_success(username)
        return JSONResponse(issue_token_pair(username, str(user.get("role") or "")))
    except Exception as exc:   # noqa: BLE001 —— fail-closed：信封化，不裸炸
        from app.auth_oidc import AuthRequiredError
        if isinstance(exc, AuthRequiredError):
            return auth_required_response(exc)   # secret/依赖缺失：401 auth_required 同形信封
        logger.exception("native 登录内部错误（fail-closed）")
        return JSONResponse(errors.envelope(errors.INTERNAL_ERROR, "登录服务内部错误，请稍后重试"),
                            status_code=500)


def refresh_response(refresh_token: str):
    """refresh（native）：验 refresh JWT→轮换发新对。无效/过期/用户已删已禁用一律
    401 unauthorized（前端收 401 即清 token 跳登录页——与 oidc 通道同语义）。"""
    from fastapi.responses import JSONResponse
    from app.auth_oidc import AuthRequiredError, auth_required_response
    try:
        claims = verify_refresh_token(refresh_token)
    except AuthRequiredError:
        return JSONResponse(
            errors.envelope(errors.UNAUTHORIZED, "登录态已失效，请重新登录"), status_code=401)
    try:
        users = load_users()["users"]
        user = users.get(str(claims.get("sub") or ""))
        if user is None or user.get("disabled"):
            return JSONResponse(
                errors.envelope(errors.UNAUTHORIZED, "登录态已失效，请重新登录"), status_code=401)
        return JSONResponse(issue_token_pair(str(claims["sub"]), str(user.get("role") or "")))
    except Exception as exc:   # noqa: BLE001
        if isinstance(exc, AuthRequiredError):
            return auth_required_response(exc)
        logger.exception("native refresh 内部错误（fail-closed）")
        return JSONResponse(errors.envelope(errors.INTERNAL_ERROR, "登录服务内部错误，请稍后重试"),
                            status_code=500)
