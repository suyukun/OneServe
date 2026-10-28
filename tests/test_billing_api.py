# -*- coding: utf-8 -*-
"""账单页＋CSV 端到端套件（管理功能群一期 C 节 · 设计 v1.1 §3.10 验收判据，本卡新建）。

起库模式沿 tests/test_billing_run.py：docker run --rm postgres:17-alpine 一次性容器，
按序灌 migrations/*.sql 全链（0001→0004）；API 通道经 BILLING_PSQL_BASE 注入
（meta_app 角色），CLI 造账通道经 USAGE_PSQL_BASE（meta_migrate）；docker 不可达/
镜像未就位＝pytest.skip 降级（hermetic）。app＝FastAPI() 自挂 billing router
（main.py 挂载归收口批，本卡不碰 main——router 自挂单测口径沿 test_grants_admin）。

覆盖（§3.10-5/6 全判据＋§3.9-⑥）：
  ① AST 契约守卫：六端点装饰器枚举＝§3.5 清单（多/少端点即红）；router 字面量挂
    _admin_gate；ROUTE_MIN_ROLE 六行在位（GET viewer×4／POST admin×2）；「只读＋
    确认，无出账触发」（拍板②：无 billing_run 依赖、无 bill_line 写句）；页面真源
    admin/billing.html 线框要素在位（§3.2）；
  ② 隔离一次性 pg 全链 e2e（off 态）：出 0 元账→列表见 reconciled＋三前置全 true→
    页面确认（带单号）→confirmed→重复确认 422 人话→缺单号 400→补录单号生效→
    CSV 首字节 EF BB BF＋中文表头命中；明细级 CSV 调用方列＝完整凭证编号（拍板④
    分层：页面掩码、CSV 全串）；admin_event 窄表 ok/denied 留痕对账（§3.7）；
  ③ 角色矩阵（on 态自造 RSA/JWT）：无令牌 401、viewer GET/导出 200、viewer POST
    confirm 403 role_forbidden、admin POST 200；欠费标记展示只读（CLI --mark-overdue
    后列表 overdue 徽标位翻转；无标记端点——①的端点枚举即守卫）；
  ④ 并发负例（§3.10-5）：同账期「API 确认 × CLI --rebill」窗口竞态——确认胜、
    rebill 事务内守卫拒绝 exit 2（P0-6，一胜一拒）；
  ⑤ 降级：库不可达＝读 503／确认 503（bill_db_unavailable，无 yaml 回落）。
测试日期全用固定历史日（2023-06/07/09），T+3 恒过，与真实时钟零耦合。
"""
from __future__ import annotations

import ast
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "service"))

# ADR-0027 步1（登录双模）：本套件＝oidc 回归——钉住 AUTH_PROVIDER=oidc。缺省
# native 会让 verify_access_token 分派本地 HS256，本套件自造的 RS256/JWKS 桩
# token 将全部被拒；oidc 链路原语义在本钉住下逐条守卫（native 面归 test_auth_native）。
os.environ["AUTH_PROVIDER"] = "oidc"

import usage_rollup as ur            # noqa: E402 —— 分片/rollup 造账（CLI 同链路）
import billing_run as br             # noqa: E402 —— ④ 并发负例（进程内窗口钩子）
from app import billing_state as bs  # noqa: E402
from app import errors as E          # noqa: E402

try:
    from fastapi import FastAPI            # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

try:
    import jwt                              # noqa: E402
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
    _HAS_JWT = True
except Exception:  # noqa: BLE001
    _HAS_JWT = False

CONTAINER = "oneserve-billing-api-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
TZ_CN = _dt.timezone(_dt.timedelta(hours=8))
STATE = {}

PERIOD_MAIN = "2023-06"     # ② e2e 主账期（确认/补录/CSV）
PERIOD_ROLE = "2023-07"     # ③ 角色矩阵账期（admin 确认＋欠费标记展示）
PERIOD_RACE = "2023-09"     # ④ 并发负例账期（进程内自造）

CALLER = "sb_billingdemo01"
CALLER_NAME = "财务对账凭证一号"
TENANT = "tenant-ok"

BOOT_SQL = (
    "DO $$ BEGIN"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_app') THEN CREATE ROLE meta_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_ro') THEN CREATE ROLE meta_ro LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_app') THEN CREATE ROLE usage_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_ro') THEN CREATE ROLE usage_ro LOGIN; END IF;"
    " END $$;"
    "CREATE SCHEMA IF NOT EXISTS meta AUTHORIZATION meta_migrate;"
    "CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;")

PSQL_MIGRATE_BASE = ("docker exec -i " + CONTAINER +
                     " psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
PSQL_APP_BASE = ("docker exec -i " + CONTAINER +
                 " psql -U meta_app -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-U", "meta_migrate",
                           "-d", "oneserve_meta", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"],
                          input=sql, capture_output=True, text=True, timeout=120)


def _ev(ts: str, status: int = 200, lat: int = 10) -> dict:
    return {"ts": ts, "caller": CALLER, "endpoint": "service:/v1/query/a", "params_digest": "x",
            "status": status, "latency_ms": lat, "response_digest": "y", "trace_id": "t",
            "seq": 0, "tenant_id": TENANT}


def _bill_period(ym: str, day: str) -> None:
    """造账全链（CLI 同链路）：租户→分片→rollup→billing_run 出账（0 元空价目）。"""
    r = _psql("INSERT INTO meta.tenant (tenant_id, display_name, tenant_type, oa_ticket_open)"
              " VALUES ('%s','正式一','external','OA-T1') ON CONFLICT (tenant_id) DO NOTHING;" % TENANT)
    assert r.returncode == 0, r.stderr
    shard = STATE["shard"] / ur.SHARD_FMT.format(day)
    shard.write_text(json.dumps(_ev(day + "T01:00:00+00:00")) + "\n"
                     + json.dumps(_ev(day + "T02:00:00+00:00", status=500)) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["USAGE_PSQL_BASE"] = PSQL_MIGRATE_BASE
    env.pop("META_MIGRATE_PSQL_BASE", None)
    for script, args in (("usage_rollup.py", ["--day", day]),
                         ("billing_run.py", ["--period", ym])):
        p = subprocess.run([sys.executable, str(REPO / "scripts" / script), "--shard-dir",
                            str(STATE["shard"]), *args],
                           capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300)
        assert p.returncode == 0, script + ": " + p.stdout + p.stderr


def _app():
    """最小账单面 app：billing router 原闸（_admin_gate）＋缺省 state＋异常 handler 自举。"""
    from app import auth_oidc as AO
    from app import billing_api
    app = FastAPI()
    app.add_exception_handler(AO.AuthRequiredError,
                              lambda req, exc: AO.auth_required_response(exc))
    app.add_exception_handler(AO.RoleForbiddenError,
                              lambda req, exc: AO.role_forbidden_response(exc))
    app.state.redis = None
    app.state.services = {}
    app.state.appkeys = {"meta": {}, "keys": [
        {"appkey": CALLER, "caller_name": CALLER_NAME, "app_name": "月账单演示",
         "tenant": TENANT, "key_type": "formal"}]}
    app.include_router(billing_api.router)
    return app


@pytest.fixture(scope="module")
def stack():
    """一次性 pg ＋迁移全链（0001→0004）＋通道 env 注入（module 级）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=billing-api-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    saved = {k: os.environ.get(k) for k in
             ("BILLING_PSQL_BASE", "USAGE_PSQL_BASE", "META_MIGRATE_PSQL_BASE")}
    try:
        for _ in range(30):
            if subprocess.run(["docker", "exec", CONTAINER, "pg_isready",
                               "-U", "meta_migrate", "-d", "oneserve_meta"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("一次性 pg 30s 未就绪")
        r = _psql(BOOT_SQL)
        assert r.returncode == 0, r.stderr
        for mig in sorted((REPO / "migrations").glob("*.sql")):
            r = _psql(mig.read_text(encoding="utf-8"))
            assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
        os.environ["BILLING_PSQL_BASE"] = PSQL_APP_BASE
        os.environ["USAGE_PSQL_BASE"] = PSQL_MIGRATE_BASE
        os.environ.pop("META_MIGRATE_PSQL_BASE", None)
        STATE.update({"shard": Path(os.environ.get("TMPDIR", "/tmp"))
                      / ("billing-api-" + uuid.uuid4().hex[:8])})
        STATE["shard"].mkdir(parents=True, exist_ok=True)
        _bill_period(PERIOD_MAIN, "2023-06-10")
        _bill_period(PERIOD_ROLE, "2023-07-10")
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


@pytest.fixture()
def client():
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi/httpx")
    os.environ["AUTH_OIDC_MODE"] = "off"
    return TestClient(_app())


# ── ① AST 契约守卫（纯静态，宿主机可跑） ──

_EXPECTED_ROUTES = {
    ("GET", "/v1/admin/billing/periods"),
    ("GET", "/v1/admin/billing/periods/{period}"),
    ("GET", "/v1/admin/billing/periods/{period}/export"),
    ("GET", "/v1/admin/billing/periods/{period}/export-lines"),
    ("POST", "/v1/admin/billing/periods/{period}/confirm"),
    ("POST", "/v1/admin/billing/periods/{period}/register-oa"),
}


def test_ast_contract_routes_gate_matrix_and_readonly():
    """端点六条＝§3.5 清单＋挂闸字面量＋ROUTE_MIN_ROLE 六行＋无出账触发＋页面线框。"""
    from app import auth_oidc as AO
    src = (REPO / "service" / "app" / "billing_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    routes = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                routes.add((dec.func.attr.upper(), str(dec.args[0].value)))
    assert routes == _EXPECTED_ROUTES, "端点清单漂移（§3.5 六条）：实得 " + repr(sorted(routes))
    assert 'APIRouter(dependencies=[Depends(_admin_gate)])' in src, "router 须路由级挂 _admin_gate"
    matrix = {("GET", "/v1/admin/billing/periods"): AO.ROLE_VIEWER,
              ("GET", "/v1/admin/billing/periods/{period}"): AO.ROLE_VIEWER,
              ("GET", "/v1/admin/billing/periods/{period}/export"): AO.ROLE_VIEWER,
              ("GET", "/v1/admin/billing/periods/{period}/export-lines"): AO.ROLE_VIEWER,
              ("POST", "/v1/admin/billing/periods/{period}/confirm"): AO.ROLE_ADMIN,
              ("POST", "/v1/admin/billing/periods/{period}/register-oa"): AO.ROLE_ADMIN}
    for (method, path), role in matrix.items():
        assert AO.ROUTE_MIN_ROLE.get("%s %s" % (method, path)) == role, \
            "ROUTE_MIN_ROLE 缺行/错档：%s %s" % (method, path)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    assert not any("billing_run" in m or "usage_rollup" in m for m in imports), \
        "页面只读＋确认，无出账触发（拍板②）：命中出账模块 " + repr(imports)
    strings = [n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("INSERT INTO usage.bill_line" in s for s in strings), \
        "无 bill_line 写句（出账归 CLI 机器门禁任务，页面只读＋确认）"
    assert not any("mark_overdue" in s or "mark-overdue" in s for s in strings), \
        "无欠费标记触发（标记动作走 CLI，ADR-0024 决定第 8 条）"
    page = (REPO / "admin" / "billing.html").read_text(encoding="utf-8")
    for marker in ("月账列表", "账期详情", "双快照", "导出 CSV", "确认", "出账引导", "OA 单号",
                   "badge", "admin-gate.js"):
        assert marker in page, "页面线框缺要素（§3.2）：" + marker
    assert bs.PERIOD_RE.match("2026-09") and not bs.PERIOD_RE.match("2026-9")


# ── ② 隔离一次性 pg 全链 e2e（§3.10-5 主链） ──

def test_zero_yuan_e2e_confirm_register_csv(stack, client):
    """出 0 元账→列表 reconciled＋三前置全 true→确认→422 重复→400 缺单号→补录生效
    →CSV BOM/中文表头/分层口径→admin_event ok/denied 留痕对账。"""
    # 列表：reconciled＋三前置全 true＋应收 0 元
    r = client.get("/v1/admin/billing/periods")
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["status"] == "ok" and d["total"] >= 2, d.get("message")
    row = next(x for x in d["items"] if x["period"] == PERIOD_MAIN)
    assert row["status"] == "reconciled", row
    assert row["gates"]["t3Closed"] and row["gates"]["reconcileClean"] and row["gates"]["lagZero"]
    assert row["amountDue"] == "0.00" and row["overdue"] is False
    assert d["summary"]["amountDue"] is not None and d["summary"]["confirmedCount"] >= 0
    # 详情：双快照两列组＋调用方页面掩码（登记名优先）
    r = client.get("/v1/admin/billing/periods/%s" % PERIOD_MAIN)
    assert r.status_code == 200, r.text[:300]
    det = r.json()
    assert det["period"]["status"] == "reconciled"
    assert len(det["items"]) == 1 and det["summary"]["unpricedCount"] == 1
    ln = det["items"][0]
    assert ln["tenantId"] == TENANT and ln["tenantDisplayName"] == "正式一"
    assert ln["endpoint"] == "service:/v1/query/a"
    assert ln["caller"] != CALLER and "****" in ln["caller"], "页面调用方必须掩码（拍板④）"
    assert ln["callerName"] == CALLER_NAME
    assert ln["usageSnapshot"] == {"calls": 2, "success": 1, "fail": 1, "avgLatencyMs": 10.0,
                                   "sourceEventCount": 2}
    assert ln["priceSnapshot"]["priced"] is False and ln["priceSnapshot"]["amount"] == "0.00"
    # 确认：缺单号 400 → 带单号 200 → 重复确认 422（状态机不跳步，人话）
    r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_MAIN, json={})
    assert r.status_code == 400 and r.json()["error"] == E.INVALID_OA_TICKET, r.text[:200]
    r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_MAIN,
                    json={"oa_ticket": "OA-2023-06-001"})
    assert r.status_code == 200, r.text[:300]
    r = client.get("/v1/admin/billing/periods")
    assert next(x for x in r.json()["items"] if x["period"] == PERIOD_MAIN)["status"] == "confirmed"
    r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_MAIN,
                    json={"oa_ticket": "OA-2023-06-001R"})
    assert r.status_code == 422 and r.json()["error"] == E.BILL_STATUS_REFUSED, r.text[:300]
    assert "仅已对账" in r.json()["message"], r.text[:300]
    # 补录单号（confirmed 行）生效
    r = client.post("/v1/admin/billing/periods/%s/register-oa" % PERIOD_MAIN,
                    json={"oa_ticket": "OA-2023-06-001R2"})
    assert r.status_code == 200, r.text[:300]
    r = client.get("/v1/admin/billing/periods")
    row = next(x for x in r.json()["items"] if x["period"] == PERIOD_MAIN)
    assert row["oaTicket"] == "OA-2023-06-001R2" and row["confirmedAt"], row
    # 404：未知账期
    r = client.get("/v1/admin/billing/periods/1999-01")
    assert r.status_code == 404 and r.json()["error"] == E.BILL_PERIOD_NOT_FOUND
    r = client.get("/v1/admin/billing/periods/1999-1/export-lines")
    assert r.status_code == 400 and r.json()["error"] == E.BILL_PERIOD_INVALID
    # CSV 账期级：BOM＋中文表头＋行内容（数字原值/布尔原值）
    r = client.get("/v1/admin/billing/periods/%s/export" % PERIOD_MAIN)
    assert r.status_code == 200
    assert r.content[:3] == b"\xef\xbb\xbf", "CSV 首字节须 UTF-8 BOM（§3.4）"
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("账期,状态,前置①T+3账期关闭"), text[:120]
    line1 = text.splitlines()[1]
    assert line1.startswith("2023-06,confirmed,true,true,true,") and "0.00" in line1 \
        and "OA-2023-06-001R2" in line1, line1
    assert 'filename="bills-2023-06.csv"' in r.headers.get("content-disposition", "")
    # CSV 明细级：四键＋双快照全列；调用方列＝完整凭证编号（拍板④分层）
    r = client.get("/v1/admin/billing/periods/%s/export-lines" % PERIOD_MAIN)
    assert r.status_code == 200 and r.content[:3] == b"\xef\xbb\xbf"
    ltext = r.content.decode("utf-8-sig")
    assert ltext.splitlines()[0].startswith("账期,租户ID,租户显示名,调用方,调用方登记名,服务,"), ltext[:140]
    assert ",".join(["2023-06", TENANT, "正式一", CALLER, CALLER_NAME,
                     "service:/v1/query/a", "2", "1", "1"]) in ltext, ltext.splitlines()[1]
    assert "****" not in ltext, "明细级 CSV 须完整凭证编号（掩码有碰撞风险）"
    assert 'filename="bills-2023-06-lines.csv"' in r.headers.get("content-disposition", "")
    # admin_event 窄表留痕对账（§3.7：ok/denied 双态在位）
    ev = _psql("SELECT action, result FROM meta.admin_event WHERE target = '%s'"
               " ORDER BY occurred_at;" % PERIOD_MAIN)
    assert ev.returncode == 0, ev.stderr
    pairs = {tuple(ln.split("|")) for ln in ev.stdout.strip().splitlines() if ln}
    assert ("bill_confirm", "ok") in pairs and ("bill_register_oa", "ok") in pairs
    assert ("bill_export", "ok") in pairs and ("bill_confirm", "denied") in pairs, pairs


# ── ③ 角色矩阵（§3.10-6；on 态自造 RSA/JWT） ──

def _make_jwks_env():
    """自造 RSA＋JWKS（test_grants_admin 同款最小实现）：返回 token 签发函数。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization  # noqa: F401（形状对齐）
    from app import auth_oidc as AO
    pub = key.public_key().public_numbers()

    def b64u(n: int) -> str:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        import base64
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": "test-kid-1", "alg": "RS256", "n": b64u(pub.n), "e": b64u(pub.e)}
    orig_fetch = AO._fetch_jwks
    AO._jwks_cache.update({"keys": [jwk], "at": time.time()})
    AO._fetch_jwks = lambda url: {"keys": [jwk]}

    def issue(**overrides) -> str:
        claims = {"iss": "http://localhost:8000", "sub": "oneserve/tester",
                  "aud": "oneserve-admin-client", "exp": int(time.time()) + 600,
                  "iat": int(time.time())}
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid-1"})
    return issue, lambda: setattr(AO, "_fetch_jwks", orig_fetch)


@pytest.mark.skipif(not (_HAS_FASTAPI and _HAS_JWT), reason="依赖缺席：fastapi/httpx＋pyjwt")
def test_role_matrix_viewer_read_export_admin_confirm(stack):
    """无令牌 401／viewer GET·导出 200、POST confirm 403 role_forbidden／admin POST 200；
    欠费标记展示只读（CLI 标记后列表 overdue 翻转；端点枚举①已守卫无标记端点）。"""
    issue, restore = _make_jwks_env()
    client = TestClient(_app())
    os.environ["AUTH_OIDC_MODE"] = "on"
    try:
        r = client.get("/v1/admin/billing/periods")
        assert r.status_code == 401, "GET 也过登录地板（读公开已废除）"
        vtok = {"Authorization": "Bearer " + issue(tag="viewer")}
        atok = {"Authorization": "Bearer " + issue(tag="admin")}
        r = client.get("/v1/admin/billing/periods", headers=vtok)
        assert r.status_code == 200, r.text[:200]
        r = client.get("/v1/admin/billing/periods/%s" % PERIOD_ROLE, headers=vtok)
        assert r.status_code == 200 and r.json()["period"]["status"] == "reconciled"
        r = client.get("/v1/admin/billing/periods/%s/export" % PERIOD_ROLE, headers=vtok)
        assert r.status_code == 200 and r.content[:3] == b"\xef\xbb\xbf"
        r = client.get("/v1/admin/billing/periods/%s/export-lines" % PERIOD_ROLE, headers=vtok)
        assert r.status_code == 200
        r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_ROLE, headers=vtok,
                        json={"oa_ticket": "OA-2023-07-001"})
        assert r.status_code == 403 and r.json()["error"] == E.ROLE_FORBIDDEN, r.text[:200]
        r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_ROLE, headers=atok,
                        json={"oa_ticket": "OA-2023-07-001"})
        assert r.status_code == 200, r.text[:300]
        r = client.get("/v1/admin/billing/periods", headers=vtok)
        row = next(x for x in r.json()["items"] if x["period"] == PERIOD_ROLE)
        assert row["status"] == "confirmed" and row["overdue"] is False
    finally:
        os.environ["AUTH_OIDC_MODE"] = "off"
        restore()
    # 欠费标记展示只读：CLI --mark-overdue 后列表 overdue 徽标位翻转（viewer 视角）
    env = dict(os.environ)
    env["USAGE_PSQL_BASE"] = PSQL_MIGRATE_BASE
    p = subprocess.run([sys.executable, str(REPO / "scripts" / "billing_run.py"), "--shard-dir",
                        str(STATE["shard"]), "--period", PERIOD_ROLE, "--mark-overdue"],
                       capture_output=True, text=True, env=env, cwd=str(REPO), timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    client = TestClient(_app())
    r = client.get("/v1/admin/billing/periods")
    row = next(x for x in r.json()["items"] if x["period"] == PERIOD_ROLE)
    assert row["overdue"] is True and row["overdueMarkedAt"], "欠费标记只读展示（决定第 8 条）"


# ── ④ 并发负例（§3.10-5：同账期并发「确认×--rebill」一胜一拒，P0-6） ──

def test_race_api_confirm_x_cli_rebill_one_wins_one_refused(stack, monkeypatch):
    """--rebill 预检后、写账事务前窗口内 API 确认抢先 → 确认胜（200）、rebill 拒
    （exit 2，事务内守卫回滚）；confirmed 底数与账单行原样，事件留痕 ok。"""
    _bill_period(PERIOD_RACE, "2023-09-10")
    client = TestClient(_app())
    orig = br.aggregate_billed_face

    def confirm_via_api_in_window(psql, lo, hi):
        rows = orig(psql, lo, hi)
        r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_RACE,
                        json={"oa_ticket": "OA-RACE-2023-09"})
        assert r.status_code == 200, "窗口内 API 确认应命中（reconciled→confirmed）：" + r.text[:200]
        return rows

    monkeypatch.setattr(br, "aggregate_billed_face", confirm_via_api_in_window)
    try:
        rc = br.main(["--period", PERIOD_RACE, "--rebill", "--shard-dir", str(STATE["shard"])])
    finally:
        monkeypatch.undo()
    assert rc == br.EXIT_REFUSE, "P0-6：确认后 rebill 须拒绝（exit 2），实得 rc=%s" % rc
    r = client.get("/v1/admin/billing/periods")
    row = next(x for x in r.json()["items"] if x["period"] == PERIOD_RACE)
    assert row["status"] == "confirmed" and row["oaTicket"] == "OA-RACE-2023-09", row
    ev = _psql("SELECT count(*) FROM meta.admin_event WHERE action='bill_confirm'"
               " AND result='ok' AND target='%s';" % PERIOD_RACE)
    assert ev.stdout.strip().endswith("1"), ev.stdout


# ── ⑤ 降级（pg 未起＝503，无 yaml 回落） ──

def test_db_down_degraded_503(stack, client, monkeypatch):
    monkeypatch.setenv("BILLING_PSQL_BASE", "docker exec -i no-such-container-xyz psql -U x -d x")
    r = client.get("/v1/admin/billing/periods")
    assert r.status_code == 503 and r.json()["error"] == E.BILL_DB_UNAVAILABLE, r.text[:200]
    assert "无 yaml 回落" in r.json()["message"]
    r = client.post("/v1/admin/billing/periods/%s/confirm" % PERIOD_MAIN,
                    json={"oa_ticket": "OA-X"})
    assert r.status_code == 503 and r.json()["error"] == E.BILL_DB_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
