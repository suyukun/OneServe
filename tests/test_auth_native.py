#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""native 登录模式套件（ADR-0027 步1 · 登录本地化——后端双模，2026-10-27）。

守卫口径（步1 验收判据的测试面；隔离栈 curl 判据另跑真容器验证）：
  1. users.yaml 用户表：原子写 roundtrip（tmp＋rename 无残留）＋明文密码不落盘
     （写前断言）＋首启种子幂等（二次启动不重复建、不改密；两 env 任一缺＝no-op）；
  2. login 端点（auth_router 直挂）：成功 200 契约同形（accessToken/refreshToken/
     expiresIn/tokenType）；claims 带 tag 三角色与 Casdoor token 结构对齐
     （sub/name/tag/iss/aud）；错密/未知用户 401 unauthorized 人话（不泄露存在性）；
  3. 防爆破复刻：5 连错冻结 15 分钟（第 5 次即冻结人话＋X-Error-Code=account_frozen；
     冻结期正确密码也拒；成功登录清零计数；冻结到期恢复）；
  4. refresh：轮换发新对；垃圾/access 冒充/用户禁用后 401；
  5. 站点墙 e2e（真 main.app TestClient）：on+native 无 token 401 auth_required；
     native 登录真 token GET /v1/admin/appkeys 200（admin）／viewer 403；
  6. 双模分派回归：AUTH_PROVIDER=oidc 时 config 回授权 URL 形态、native HS256
     token 不被 oidc 通道放行；缺 ONESERVE_AUTH_SECRET fail-closed（401 人话指路）；
  7. 配置面：.env.example 三键在册（CHANGE_ME 占位）＋users.yaml 模板随仓。

运行：python3 tests/test_auth_native.py（或 python3 -m pytest tests/test_auth_native.py -q）。
口径：测试不写共享栈 Redis；users.yaml 一律走临时文件（ONESERVE_USERS_YAML 注入）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import errors as E            # noqa: E402
from app import auth_oidc as AO        # noqa: E402
from app import auth_native as AN      # noqa: E402

try:
    import jwt
    _HAS_JWT = True
except Exception:                                            # noqa: BLE001
    _HAS_JWT = False

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except Exception:                                            # noqa: BLE001
    _HAS_FASTAPI = False

try:
    import bcrypt   # noqa: F401
    _HAS_BCRYPT = True
except Exception:                                            # noqa: BLE001
    _HAS_BCRYPT = False

SECRET = "native-test-secret-0123456789abcdef-STEP1"
PW = "StepOneAdmin456!"

_ENV_KEYS = ("AUTH_PROVIDER", "ONESERVE_AUTH_SECRET", "ONESERVE_USERS_YAML",
             "ONESERVE_SEED_ADMIN_USER", "ONESERVE_SEED_ADMIN_PASSWORD",
             "AUTH_OIDC_MODE")


@contextmanager
def _native_env(users_yaml: Path, *, secret: str | None = SECRET, mode: str = "on"):
    """注入 native 双模环境（try/finally 全量还原——测试卫生，防污染并行套件）。"""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["AUTH_PROVIDER"] = "native"
    os.environ["AUTH_OIDC_MODE"] = mode
    os.environ["ONESERVE_USERS_YAML"] = str(users_yaml)
    for k in ("ONESERVE_SEED_ADMIN_USER", "ONESERVE_SEED_ADMIN_PASSWORD"):
        os.environ.pop(k, None)
    if secret is None:
        os.environ.pop("ONESERVE_AUTH_SECRET", None)
    else:
        os.environ["ONESERVE_AUTH_SECRET"] = secret
    AN.reset_fail_state()
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        AN.reset_fail_state()


def _client():
    """auth_router 直挂最小 app（登录闭环豁免域，免整栈装配）。"""
    app = FastAPI()
    app.include_router(AO.router)
    return TestClient(app)


def _seeded_users_yaml(path: Path, extra: dict | None = None) -> Path:
    """造一份带 admin（＋可选他角色用户）的 users.yaml（bcrypt 真哈希）。"""
    doc = {"users": {"admin": {"name": "admin", "password_hash": AN.hash_password(PW),
                               "role": "admin", "disabled": False,
                               "created_at": "2026-10-27T00:00:00+00:00",
                               "updated_at": "2026-10-27T00:00:00+00:00"}}}
    for name, role in (extra or {}).items():
        doc["users"][name] = {"name": name, "password_hash": AN.hash_password(PW + name),
                              "role": role, "disabled": False,
                              "created_at": "2026-10-27T00:00:00+00:00",
                              "updated_at": "2026-10-27T00:00:00+00:00"}
    AN.save_users(path, doc)
    return path


def _login(client, username: str, password: str):
    return client.post("/v1/admin/auth/login", json={"username": username, "password": password})


# ---------- 1. 用户表与种子 ----------

def test_users_store_roundtrip_atomic(tmp_path):
    """原子写 roundtrip：tmp 无残留、头注在册、明文密码写前断言拦截。"""
    p = tmp_path / "users.yaml"
    doc = {"users": {"admin": {"name": "admin", "password_hash": AN.hash_password(PW),
                               "role": "admin", "disabled": False,
                               "created_at": "t", "updated_at": "t"}}}
    AN.save_users(p, doc)
    assert p.is_file() and not p.with_suffix(".yaml.tmp").exists(), "原子写须无 .tmp 残留"
    loaded = AN.load_users(p)
    assert loaded["users"]["admin"]["role"] == "admin"
    text = p.read_text(encoding="utf-8")
    assert "OneServe 用户表" in text and "bcrypt" in text, "头注（结构＋安全纪律）须随文件"
    assert PW not in text, "明文密码不得落盘"
    try:
        AN.save_users(p, doc, password_plain=PW)   # 明文恰为哈希源时＝断言拦截（安全红线）
        raise AssertionError("明文密码写前断言未拦截")
    except AssertionError as exc:
        assert "明文" in str(exc)
    print("  ok test_users_store_roundtrip_atomic")


def test_seed_admin_idempotent(tmp_path):
    """首启种子：建 admin（bcrypt，role=admin）；二次启动不重复建、不改密；env 缺一＝no-op。"""
    if not _HAS_BCRYPT:
        pytest.skip("依赖缺席：bcrypt（种子单测）")
    p = tmp_path / "users.yaml"
    saved = {k: os.environ.get(k) for k in ("ONESERVE_SEED_ADMIN_USER",
                                            "ONESERVE_SEED_ADMIN_PASSWORD",
                                            "ONESERVE_USERS_YAML")}
    try:
        os.environ["ONESERVE_USERS_YAML"] = str(p)
        # 缺一（密码空）＝no-op，不建文件
        os.environ["ONESERVE_SEED_ADMIN_USER"] = "admin"
        os.environ["ONESERVE_SEED_ADMIN_PASSWORD"] = ""
        assert AN.seed_admin() is False and not p.exists(), "密码空＝整段跳过"
        # 首启：建号
        os.environ["ONESERVE_SEED_ADMIN_PASSWORD"] = PW
        assert AN.seed_admin() is True
        doc = AN.load_users(p)
        u = doc["users"]["admin"]
        assert u["role"] == "admin" and u["disabled"] is False
        assert u["password_hash"].startswith("$2b$") and PW not in p.read_text()
        assert AN.verify_password(PW, u["password_hash"])
        hash1 = u["password_hash"]
        # 二次启动：不重复建、不改密（幂等）
        assert AN.seed_admin() is False
        assert AN.load_users(p)["users"]["admin"]["password_hash"] == hash1, "二次启动不得改密"
        assert len(AN.load_users(p)["users"]) == 1, "不重复建号"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  ok test_seed_admin_idempotent（首启建号＋二次幂等不改密＋缺 env no-op）")


# ---------- 2. login 端点契约 ----------

def test_login_success_contract_and_claims(tmp_path):
    """成功 200：契约字段同形；access claims 带 tag/sub/name/iss/aud（Casdoor 结构对齐）。"""
    if not (_HAS_FASTAPI and _HAS_JWT and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt＋bcrypt（login 契约单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users):
        r = _login(_client(), "admin", PW)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        for key in ("accessToken", "refreshToken", "expiresIn", "tokenType"):
            assert body.get(key), f"契约字段缺失：{key}"
        assert body["tokenType"] == "Bearer" and body["expiresIn"] == AN.ACCESS_TTL_S == 7200
        claims = jwt.decode(body["accessToken"], SECRET, algorithms=["HS256"],
                            issuer=AN.NATIVE_ISSUER, audience=AN.NATIVE_AUDIENCE)
        assert claims["sub"] == "admin" and claims["name"] == "admin" and claims["tag"] == "admin"
        assert claims["type"] == AN.TYPE_ACCESS and claims["exp"] > claims["iat"]
        # 签发即验签（后端验签入口分派到本地 HS256）
        assert AO.verify_access_token(body["accessToken"])["sub"] == "admin"
    print("  ok test_login_success_contract_and_claims")


def test_login_wrong_password_and_unknown_user(tmp_path):
    """错密/未知用户 401 unauthorized 同人话（不泄露账号存在性）；缺字段 400。"""
    if not (_HAS_FASTAPI and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋bcrypt（错密单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users):
        client = _client()
        r1 = _login(client, "admin", "totally-wrong!")
        r2 = _login(client, "no-such-user", "whatever-pass!")
        for r in (r1, r2):
            assert r.status_code == 401 and r.json()["error"] == E.UNAUTHORIZED
            assert r.json()["message"] == AN.WRONG_PW_MESSAGE, "凭据错人话"
            assert r.json().get("docs_url"), "错误信封恒带 docs_url"
        r3 = client.post("/v1/admin/auth/login", json={"username": "admin"})
        assert r3.status_code == 400 and r3.json()["error"] == E.INVALID_REQUEST
    print("  ok test_login_wrong_password_and_unknown_user")


def test_login_disabled_user_rejected(tmp_path):
    """disabled=true 拒登录（人话指路联系管理员；不计入爆破计数）。"""
    if not (_HAS_FASTAPI and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋bcrypt（禁用单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    doc = AN.load_users(users)
    doc["users"]["admin"]["disabled"] = True
    AN.save_users(users, doc)
    with _native_env(users):
        r = _login(_client(), "admin", PW)
        assert r.status_code == 401 and r.json()["message"] == AN.DISABLED_MESSAGE
        assert r.headers.get("x-error-code") == "account_disabled"
    print("  ok test_login_disabled_user_rejected")


# ---------- 3. 防爆破（5 连错冻结 15 分钟，复刻 Casdoor 语义） ----------

def test_bruteforce_freeze_semantics(tmp_path):
    """4 连错仍「密码不正确」；第 5 次触发冻结（人话＋X-Error-Code）；冻结期正确密码
    也拒；成功登录清零；冻结到期（时间推进）恢复可登录。"""
    if not (_HAS_FASTAPI and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋bcrypt（防爆破单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users):
        client = _client()
        for i in range(4):
            r = _login(client, "admin", "wrong-" + str(i))
            assert r.status_code == 401 and r.json()["message"] == AN.WRONG_PW_MESSAGE, \
                f"第 {i + 1} 次错密不应冻结"
        r5 = _login(client, "admin", "wrong-5th")
        assert r5.status_code == 401, "冻结仍 401"
        assert r5.json()["message"] == AN.FROZEN_MESSAGE and "15 分钟" in r5.json()["message"]
        assert r5.headers.get("x-error-code") == "account_frozen", "冻结须可机判"
        # 冻结期：正确密码也拒（爆破者不得借冻结窗口试探成功路径）
        r_ok = _login(client, "admin", PW)
        assert r_ok.status_code == 401 and r_ok.json()["message"] == AN.FROZEN_MESSAGE
        # 冻结到期（frozen_until 拨回过去＝时间推进）→ 恢复登录
        AN._FAIL_STATE["admin"]["frozen_until"] = time.time() - 1
        r_back = _login(client, "admin", PW)
        assert r_back.status_code == 200, "冻结到期须恢复可登录"
        # 成功登录清零计数：再连错 4 次不触发冻结
        for i in range(4):
            _login(client, "admin", "wrong-again-" + str(i))
        r6 = _login(client, "admin", PW)
        assert r6.status_code == 200, "成功登录清零后 4 连错不得冻结"
    print("  ok test_bruteforce_freeze_semantics（5 连错冻 15 分钟＋冻结期正确密码也拒＋清零）")


# ---------- 4. refresh（轮换式） ----------

def test_refresh_rotation_and_rejections(tmp_path):
    """refresh 200 换新对（轮换式）；垃圾串/access 冒充/禁用用户 401 人话。"""
    if not (_HAS_FASTAPI and _HAS_JWT and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt＋bcrypt（refresh 单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users):
        client = _client()
        pair = _login(client, "admin", PW).json()
        r = client.post("/v1/admin/auth/refresh", json={"refreshToken": pair["refreshToken"]})
        assert r.status_code == 200, r.text[:150]
        new_pair = r.json()
        assert new_pair["accessToken"] != pair["accessToken"], "轮换式须发新对"
        assert jwt.decode(new_pair["accessToken"], SECRET, algorithms=["HS256"],
                          issuer=AN.NATIVE_ISSUER, audience=AN.NATIVE_AUDIENCE)["tag"] == "admin"
        # 垃圾串 / access 冒充 refresh → 401
        for bad in ("not-a-jwt", pair["accessToken"]):
            rb = client.post("/v1/admin/auth/refresh", json={"refreshToken": bad})
            assert rb.status_code == 401 and rb.json()["error"] == E.UNAUTHORIZED, bad[:12]
        # 用户禁用后旧 refresh 不续命
        doc = AN.load_users(users)
        doc["users"]["admin"]["disabled"] = True
        AN.save_users(users, doc)
        rd = client.post("/v1/admin/auth/refresh", json={"refreshToken": new_pair["refreshToken"]})
        assert rd.status_code == 401, "禁用用户旧 refresh 不得续期"
        r_empty = client.post("/v1/admin/auth/refresh", json={})
        assert r_empty.status_code == 400 and r_empty.json()["error"] == E.INVALID_REQUEST
    print("  ok test_refresh_rotation_and_rejections")


# ---------- 5. 三角色 tag（与 ROUTE_MIN_ROLE 矩阵的缝） ----------

def test_three_role_tags_align(tmp_path):
    """三角色用户登录各得 tag；role_from_claims（角色闸消费口径）零改动正确映射。"""
    if not (_HAS_FASTAPI and _HAS_JWT and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt＋bcrypt（三角色单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml",
                               {"op1": "operator", "viewer1": "viewer"})
    with _native_env(users):
        client = _client()
        for name, expected, pw in (("admin", AO.ROLE_ADMIN, PW),
                                   ("op1", AO.ROLE_OPERATOR, PW + "op1"),
                                   ("viewer1", AO.ROLE_VIEWER, PW + "viewer1")):
            r = _login(client, name, pw)
            assert r.status_code == 200, f"{name} 登录须 200：{r.text[:120]}"
            claims = jwt.decode(r.json()["accessToken"], SECRET, algorithms=["HS256"],
                                issuer=AN.NATIVE_ISSUER, audience=AN.NATIVE_AUDIENCE)
            assert claims["tag"] == expected, f"{name} tag 须 {expected}"
            assert AO.role_from_claims(claims) == expected, "角色闸消费口径零改动正确"
    print("  ok test_three_role_tags_align（admin/operator/viewer 三角色 tag 正确）")


# ---------- 6. 【无登录模式 ADR-0029·已删】站点墙 e2e（真 main.app） ----------
# 站点墙中间件与登录端点（/v1/admin/auth/login）已随无登录模式摘除接线，原
# test_native_wall_appkeys_e2e（无 token 401／native 登录 200／viewer 403）随机制
# 退役移除；auth_native 模块纯函数级用例（验密/签发/验签/角色）仍在本套件覆盖。
# 历史实现见 git 史。


# ---------- 7. 双模分派回归（oidc 通道不动＋native 优先级） ----------

def test_provider_dispatch_config_and_verify(tmp_path):
    """config：native 回 provider=native 无授权 URL；oidc 回 loginUrl 授权形态。
    verify：native HS256 token 在 oidc 分支不被放行（分派互斥）。"""
    if not (_HAS_FASTAPI and _HAS_JWT and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt＋bcrypt（分派回归）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users):
        client = _client()
        pair = _login(client, "admin", PW).json()
        cfg = client.get("/v1/admin/auth/config").json()
        assert cfg["provider"] == "native" and cfg["siteWall"] is True
        assert "loginUrl" not in cfg, "native 模式不得下发 Casdoor 授权地址"
        r_cb = client.get("/v1/admin/auth/callback?code=x&state=y")
        assert r_cb.status_code == 404, "native 无授权码流（404 信封）"
        native_token = pair["accessToken"]
    # 切 oidc：config 回授权形态；native token 走 JWKS 分支不被放行（互斥证据）
    saved_provider = os.environ.get("AUTH_PROVIDER")
    os.environ["AUTH_PROVIDER"] = "oidc"
    try:
        cfg2 = _client().get("/v1/admin/auth/config").json()
        assert "loginUrl" in cfg2 and "/login/oauth/authorize" in cfg2["loginUrl"]
        try:
            AO.verify_access_token(native_token)
            raise AssertionError("oidc 分支不得放行 native HS256 token")
        except AO.AuthRequiredError:
            pass
    finally:
        if saved_provider is None:
            os.environ.pop("AUTH_PROVIDER", None)
        else:
            os.environ["AUTH_PROVIDER"] = saved_provider
    print("  ok test_provider_dispatch_config_and_verify（native/oidc 互斥分派）")


def test_secret_missing_fail_closed(tmp_path):
    """ONESERVE_AUTH_SECRET 缺失：login 401 auth_required 人话指路；verify 抛
    secret_missing（fail-closed，不降级放行）。"""
    if not (_HAS_FASTAPI and _HAS_JWT and _HAS_BCRYPT):
        pytest.skip("依赖缺席：fastapi/httpx＋pyjwt＋bcrypt（密钥缺失单测）")
    users = _seeded_users_yaml(tmp_path / "users.yaml")
    with _native_env(users, secret=None):
        r = _login(_client(), "admin", PW)
        assert r.status_code == 401 and r.json()["error"] == E.AUTH_REQUIRED
        assert "ONESERVE_AUTH_SECRET" in r.json()["message"], "密钥缺失须指路环境变量"
        try:
            AN.verify_access_token("any-token")
            raise AssertionError("缺密钥须 fail-closed")
        except AO.AuthRequiredError as exc:
            assert exc.reason == "secret_missing"
    print("  ok test_secret_missing_fail_closed")


# ---------- 8. 配置面守卫 ----------

def test_env_example_documents_native_keys():
    """无登录模式（ADR-0029）改锚：登录链三键已从 .env.example 移除（防回潮）。
    历史口径（三键在册＋双模说明）见 git 史。"""
    text = (REPO / ".env.example").read_text(encoding="utf-8")
    for needle in ("AUTH_PROVIDER=native", "ONESERVE_AUTH_SECRET=CHANGE_ME_AUTH_SECRET",
                   "ONESERVE_SEED_ADMIN_USER", "ONESERVE_SEED_ADMIN_PASSWORD=CHANGE_ME_SEED_ADMIN_PASSWORD"):
        assert needle not in text, f".env.example 不得残留登录链键（ADR-0029 已删）：{needle}"
    assert "无登录模式" in text, ".env.example 须留无登录模式碑注"
    print("  ok test_env_example_documents_native_keys（登录链键零残留）")


def test_users_yaml_template_shipped():
    """service/users.yaml 模板随仓（空表＋头注）：步3 CRUD 落点在册、结构自文档。"""
    p = REPO / "service" / "users.yaml"
    assert p.is_file(), "users.yaml 模板须随仓"
    text = p.read_text(encoding="utf-8")
    assert "password_hash" in text and "role" in text and "disabled" in text
    assert "明文密码永不落盘" in text
    import yaml as _yaml
    assert _yaml.safe_load(text)["users"] == {}, "模板须为空表（种子运行期写入）"
    print("  ok test_users_yaml_template_shipped")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
