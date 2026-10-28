# -*- coding: utf-8 -*-
"""价目管理页测试（管理功能群一期 B 节 · 设计 v1.1 §2.10 验收线全可跑）。

三段结构：
  A. 纯逻辑（宿主机零依赖可跑）：P0-4 归一（与 proxy_executor.annotate_endpoint 对拍）、
     三模型分流、区间三不变量负例（人话报因）、月初约束（P1-⑦）、展示态推算、
     item_id 规则、事务脚本形态（advisory lock＋互斥＋admin_event 同发）、
     AST 契约守卫（路由清单/角色地板/挂闸模块/引擎匹配键锚点）。
  B. API 契约（TestClient 自挂 router——main.py 挂载归收口批，沿 test_accounts_admin
     自挂先例；auth off 态）：422 人话信封、降级态、404 信封、分页非法。
  C. pg 集成（隔离一次性 pg，0001~0004 全链 apply，沿 tests/test_billing_run.py 模式；
     docker 不可达/镜像未就位＝pytest.skip 降级）：CRUD 全流程、重叠窗并发负例（P1-⑨）、
     停用按展示态分派（P1-⑧）、命中预演＋priced 覆盖率＋引擎匹配实证（P0-4：造
     usage_daily 命中数据→billing_run.load_price 原函数按落库 service_ref 命中计价）、
     角色矩阵（§2.10-7：viewer GET 200／operator GET 200 POST 403／admin POST 200）。

隔离纪律：一次性 pg 容器 --rm 用毕清零；现存 oneserve-* 容器零起停；禁碰
scripts/billing_run.py（引擎只 import 复用）与 service/app/main.py。
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "scripts"))

# ADR-0027 步1（登录双模）：本套件＝oidc 回归——钉住 AUTH_PROVIDER=oidc。缺省
# native 会让 verify_access_token 分派本地 HS256，本套件自造的 RS256/JWKS 桩
# token 将全部被拒；oidc 链路原语义在本钉住下逐条守卫（native 面归 test_auth_native）。
os.environ["AUTH_PROVIDER"] = "oidc"

from app import pricing_api as pa          # noqa: E402
from app import auth_oidc as AO            # noqa: E402

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

# ── 固定相对日期（与真实时钟零耦合的推算基点；月初约束用真日历推） ──────────
TODAY = pa.today_cn()
FIRST_THIS = TODAY.replace(day=1)
NEXT_MONTH = _dt.date(FIRST_THIS.year + (FIRST_THIS.month == 12), FIRST_THIS.month % 12 + 1, 1)
LAST_MONTH_END = FIRST_THIS - _dt.timedelta(days=1)
PREV_MONTH_FIRST = LAST_MONTH_END.replace(day=1)
PREV_MONTH_LAST = NEXT_MONTH - _dt.timedelta(days=1) if False else FIRST_THIS - _dt.timedelta(days=1)


def _first_of_month(d: _dt.date) -> _dt.date:
    return d.replace(day=1)


def _shift_months(d: _dt.date, n: int) -> _dt.date:
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return _dt.date(y, m, 1)


# ════════════════ A. 纯逻辑 ════════════════

def test_normalize_service_ref_p0_4():
    """P0-4 单点归一：剥 ?retention=on|off（幂等）；A 型原值不动；与审计标注对拍。"""
    from app import proxy_executor as pxe
    assert pa.normalize_service_ref("service:/v1/query/alpha") == "service:/v1/query/alpha"
    base = "service:/proxy/beta"
    for enabled in (True, False):
        annotated = pxe.annotate_endpoint(base, enabled)   # 审计侧原函数（真实形态）
        assert annotated in (base + "?retention=on", base + "?retention=off")
        assert pa.normalize_service_ref(annotated) == base, "标注后归一必须回到原值（引擎匹配键）"
    assert pa.normalize_service_ref(base + "?retention=on?retention=off") == base, "重复标注幂等剥除"
    assert pa.normalize_service_ref(" service:/x ") == "service:/x"
    assert pa.normalize_service_ref("") == ""
    assert pa.B_TYPE_CREATE_OPEN is True, "收口批已翻闸：引擎侧归一匹配落地（billing_run.load_price 同源复用 normalize_service_ref）"


def test_item_id_and_display_state():
    """item_id＝pi_＋16 位小写 hex；展示态三态按今日推算（不入库）。"""
    for _ in range(20):
        assert re.fullmatch(r"pi_[0-9a-f]{16}", pa.new_item_id())
    assert pa.new_item_id() != pa.new_item_id()
    today = _dt.date(2026, 10, 21)
    assert pa.display_state(_dt.date(2026, 10, 1), None, today) == pa.STATE_IN_WINDOW
    assert pa.display_state(_dt.date(2026, 10, 1), _dt.date(2026, 12, 31), today) == pa.STATE_IN_WINDOW
    assert pa.display_state(_dt.date(2026, 11, 1), None, today) == pa.STATE_PENDING
    assert pa.display_state(_dt.date(2026, 9, 1), _dt.date(2026, 9, 30), today) == pa.STATE_EXPIRED


def test_derive_tiers_three_models():
    """三模型表单分流：按次单档／套餐两档（额度内 0 价＋超额档）／阶梯行式。"""
    assert pa.derive_tiers({"model": "per_call", "unit_price": "0.5"}) == [(0, None, "0.500000")]
    assert pa.derive_tiers({"model": "monthly_package", "included_quota": 10000,
                            "overage_price": "0.8"}) == [(0, 10000, "0"), (10000, None, "0.800000")]
    tiers = pa.derive_tiers({"model": "tiered", "tiers": [
        {"lower_bound": 10000, "upper_bound": None, "unit_price": "0.8"},
        {"lower_bound": 0, "upper_bound": 10000, "unit_price": "0"}]})
    assert tiers == [(0, 10000, "0.000000"), (10000, None, "0.800000")], "行序无关，按 lower 排序"
    assert pa.tier_summary([(0, None, "0.5")]) == "[0,∞)@0.500000"
    assert pa.tier_summary([(0, 10000, "0"), (10000, None, "0.8")]) == \
        "[0,10000)@0.000000 ＋ [10000,∞)@0.800000"


def test_tier_invariant_rejections_human_reasons():
    """区间三不变量负例：人话报因对齐设计 §2.3 示例（缺档明细）。"""
    def tiers(*rows):
        return {"model": "tiered", "tiers": [r for r in rows]}
    with pytest.raises(pa.PricingValidationError, match=r"缺 \[5000,10000\)"):
        pa.derive_tiers(tiers({"lower_bound": 0, "upper_bound": 5000, "unit_price": "0"},
                              {"lower_bound": 10000, "upper_bound": None, "unit_price": "0.8"}))
    with pytest.raises(pa.PricingValidationError, match="首档下界须为 0"):
        pa.derive_tiers(tiers({"lower_bound": 100, "upper_bound": None, "unit_price": "1"},
                              {"lower_bound": 200, "upper_bound": None, "unit_price": "2"}))
    with pytest.raises(pa.PricingValidationError, match="末档上界须留空"):
        pa.derive_tiers(tiers({"lower_bound": 0, "upper_bound": 100, "unit_price": "1"},
                              {"lower_bound": 100, "upper_bound": 200, "unit_price": "2"}))
    with pytest.raises(pa.PricingValidationError, match="仅末档可留空上界"):
        pa.derive_tiers(tiers({"lower_bound": 0, "upper_bound": None, "unit_price": "1"},
                              {"lower_bound": 100, "upper_bound": None, "unit_price": "2"}))
    with pytest.raises(pa.PricingValidationError, match="上界须大于下界"):
        pa.derive_tiers(tiers({"lower_bound": 0, "upper_bound": 0, "unit_price": "1"},
                              {"lower_bound": 100, "upper_bound": None, "unit_price": "2"}))
    with pytest.raises(pa.PricingValidationError, match="至少需要两档"):
        pa.derive_tiers(tiers({"lower_bound": 0, "upper_bound": None, "unit_price": "1"}))
    with pytest.raises(pa.PricingValidationError, match="6 位小数"):
        pa.derive_tiers({"model": "per_call", "unit_price": "0.1234567"})
    with pytest.raises(pa.PricingValidationError, match="整数部分 ≤8 位"):
        pa.derive_tiers({"model": "per_call", "unit_price": "999999999"})
    with pytest.raises(pa.PricingValidationError, match="须为 ≥0 且最多 6 位小数"):
        pa.derive_tiers({"model": "per_call", "unit_price": "-1"})
    with pytest.raises(pa.PricingValidationError, match="须为非负数字"):
        pa.derive_tiers({"model": "per_call", "unit_price": ["x"]})


def test_create_validation_month_start_and_btype():
    """生效起约束到月初（P1-⑦拍板推荐）；B 型创建放行（收口批翻闸反转）；币种/备注校验。"""
    body = {"service_ref": "service:/v1/query/a", "model": "per_call", "unit_price": "1"}
    with pytest.raises(pa.PricingValidationError, match="每月 1 日"):
        pa.validate_create(dict(body, effective_from=_dt.date(2026, 10, 15).isoformat()))
    # B 型开闸反转（收口批 2026-10-21）：proxy 目标放行；带审计后缀原值落库前归一剥除
    prep_b = pa.validate_create(dict(body, service_ref="service:/proxy/b", target_kind="proxy",
                                     effective_from=_shift_months(FIRST_THIS, 1).isoformat()))
    assert prep_b["service_ref"] == "service:/proxy/b", "B 型创建放行（闸已翻）"
    prep_b2 = pa.validate_create(dict(body, service_ref="service:/proxy/b?retention=on",
                                      effective_from=_shift_months(FIRST_THIS, 1).isoformat()))
    assert prep_b2["service_ref"] == "service:/proxy/b", "原值带审计后缀＝落库前归一剥除（P0-4 单点）"
    with pytest.raises(pa.PricingValidationError, match="三位大写字母"):
        pa.validate_create(dict(body, currency="cnyx",
                                effective_from=_shift_months(FIRST_THIS, 1).isoformat()))
    prep = pa.validate_create(dict(body, effective_from=_shift_months(FIRST_THIS, 1).isoformat(),
                                   remark="备注'带单引号\"\n控制符"))
    assert prep["service_ref"] == "service:/v1/query/a"
    assert "\n" not in prep["remark"] and "'" in prep["remark"], "控制符清洗但保留原文语义（引号由 _lit 加倍）"
    script = pa.build_create_script(prep, "pi_" + "0" * 16, "tester")
    assert prep["remark"].replace("'", "''") in script, "备注落脚本＝单引号加倍转义形态"


def test_build_scripts_shape():
    """事务脚本形态：advisory lock＋事务内互斥（P1-⑨）＋admin_event 同发＋停用分派（P1-⑧）。"""
    prep = pa.validate_create({"service_ref": "service:/v1/query/a", "model": "per_call",
                               "unit_price": "0.5", "remark": "it's ok",
                               "effective_from": _shift_months(FIRST_THIS, 1).isoformat()})
    item_id = pa.new_item_id()
    script = pa.build_create_script(prep, item_id, "oneserve/tester")
    assert script.startswith("BEGIN;\n") and script.rstrip().endswith("COMMIT;")
    assert "pg_advisory_xact_lock(hashtext('service:/v1/query/a'))" in script
    assert "DO $$" in script and "已有生效区间与之重叠的价目" in script
    assert "INSERT INTO usage.price_item" in script and "INSERT INTO usage.price_tier" in script
    assert "INSERT INTO meta.admin_event" in script and "'price_create'" in script
    assert "'it''s ok'" in script, "单引号加倍防注入"
    ret = pa.build_retire_script(item_id, "oneserve/tester", _dt.date(2026, 10, 21))
    assert "FOR UPDATE" in ret and "'price_retire'" in ret
    assert "DELETE FROM usage.price_tier" in ret, "未生效撤回＝删档位行"
    assert "SET effective_to = '2026-10-21'::date" in ret.replace("'2026-10-21'::date", "today") or \
        "effective_to = today" in ret
    assert "已过期（生效止" in ret, "已过期拒绝分支在 SQL 内判定"


def _enumerate_pricing_routes():
    import ast
    tree = ast.parse((REPO / "service" / "app" / "pricing_api.py").read_text(encoding="utf-8"))
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                    and dec.args and isinstance(dec.args[0], ast.Constant)
                    and dec.func.attr in ("get", "post")):
                rows.append((dec.func.attr.upper(), str(dec.args[0].value)))
    return rows


def test_ast_contract_guard_routes_and_roles():
    """AST 契约守卫：七路由全在 ROUTE_MIN_ROLE（读 viewer／写 admin，§2.4 v1.1 拍板③）。"""
    rows = _enumerate_pricing_routes()
    assert len(rows) == 7, f"端点族应恰 7 条（设计 4 条＋命中预演/覆盖率/选择器 3 条只读）：{rows!r}"
    floors = {"GET": AO.ROLE_VIEWER, "POST": AO.ROLE_ADMIN}
    for method, path in rows:
        key = f"{method} {path}"
        assert key in AO.ROUTE_MIN_ROLE, f"{key} 缺 min_role 列（W4 防回潮）"
        assert AO.ROUTE_MIN_ROLE[key] == floors[method], f"{key} 地板错：{AO.ROUTE_MIN_ROLE[key]!r}"


def test_ast_contract_guard_gate_and_engine_anchor():
    """守卫续：挂闸模块清单含 pricing_api.py；引擎匹配键锚点＋归一同源复用锚点（收口批）。"""
    guard_src = (REPO / "tests" / "test_auth_oidc.py").read_text(encoding="utf-8")
    assert '"pricing_api.py"' in guard_src, "pricing_api.py 须入 _GATED_WRITE_MODULES（写路由挂闸域）"
    src = (REPO / "service" / "app" / "pricing_api.py").read_text(encoding="utf-8")
    assert "router = APIRouter(dependencies=[Depends(_admin_gate)])" in src, "router 级挂 _admin_gate"
    engine_src = (REPO / "scripts" / "billing_run.py").read_text(encoding="utf-8")
    assert "WHERE service_ref = %s AND effective_from <= %s" in engine_src, \
        "P0-4 锚点：引擎匹配键＝service_ref 精确匹配（匹配值经归一）——锚点防漂移"
    assert "pa.normalize_service_ref" in engine_src, \
        "收口批锚点：引擎匹配侧同源复用 normalize_service_ref（B 型开闸前提，禁第二实现）"


def test_build_selector_endpoint_values():
    """选择器＝服务∪代理合并命名空间；值＝endpoint 原值（P0-4）；B 型角标随开闸更新（收口批反转）。"""
    services = {"alpha": {"service_id": "alpha", "name": "标签查询", "route": ""},
                "beta": {"service_id": "beta", "name": "外部代理", "route": "/proxy/beta", "kind": "proxy"}}
    out = pa.build_selector(services)
    by_id = {i["id"]: i for i in out["items"]}
    assert by_id["alpha"]["endpoint"] == "service:/v1/query/alpha", "A 型点查 endpoint 原值"
    assert by_id["beta"]["endpoint"] == "service:/proxy/beta", "B 型归一形（无审计后缀）"
    assert by_id["beta"]["kind"] == "proxy" and "归一形" in by_id["beta"]["note"], \
        "B 型角标随开闸更新（延后文案随翻闸退役）"
    assert out["bTypeOpen"] is True, "收口批已翻闸"
    assert pa.build_selector(None)["status"] == "ok", "登记表缺席＝空选择器降级不抛"

# ════════════════ B. API 契约（TestClient 自挂 router；main.py 挂载归收口批） ════════════════

def _pricing_app():
    app = FastAPI()
    app.include_router(pa.router)
    return app


def test_api_validation_422_and_paging_400_and_degraded(monkeypatch):
    """API 契约：422 人话信封（校验先于 db）／分页非法 400／hit-preview 缺参 400／降级态。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（API 契约段）")
    monkeypatch.setenv("AUTH_OIDC_MODE", "off")
    monkeypatch.setenv("PRICING_PSQL_BASE", "false")   # 必败通道（真子进程 rc=1 → PricingDbError）
    client = TestClient(_pricing_app())
    r = client.post("/v1/admin/pricing/items", json={"service_ref": "service:/x", "model": "per_call",
                                                     "unit_price": "1", "effective_from": "2026-10-15"})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"] == "pricing_validation" and "每月 1 日" in body["message"], body
    r = client.get("/v1/admin/pricing/items?page=0")
    assert r.status_code == 400 and r.json()["error"] == "bad_query", r.text
    r = client.get("/v1/admin/pricing/hit-preview")
    assert r.status_code == 400, r.text
    r = client.get("/v1/admin/pricing/items")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "degraded" and "计费数据暂不可用" in body["message"] and body["items"] == [], body
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/x", "model": "per_call", "unit_price": "1",
        "effective_from": _shift_months(FIRST_THIS, 1).isoformat()})
    assert r.status_code == 503 and r.json()["error"] == "pricing_db_unavailable", \
        "校验全过才撞库：库不可达＝写面 503 信封"


# ════════════════ C. pg 集成（隔离一次性 pg；docker 不可达＝skip 降级） ════════════════

CONTAINER = "oneserve-pricing-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
STATE = {}


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "meta_migrate",
         "-d", "oneserve_meta", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"],
        input=sql, capture_output=True, text=True, timeout=120)


def _rollup(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["USAGE_PSQL_BASE"] = STATE["base"]
    env.pop("META_MIGRATE_PSQL_BASE", None)
    return subprocess.run([sys.executable, str(REPO / "scripts" / "usage_rollup.py"), "--shard-dir",
                           str(STATE["shard"]), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300)


def _ev(ts: str, caller: str, endpoint: str = "service:/v1/query/a",
        status: int = 200, lat: int = 10, tenant: str = "tenant-price") -> dict:
    return {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "x",
            "status": status, "latency_ms": lat, "response_digest": "y", "trace_id": "t",
            "seq": 0, "tenant_id": tenant}


BOOT_SQL = (
    "DO $$ BEGIN"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_app') THEN CREATE ROLE meta_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_ro') THEN CREATE ROLE meta_ro LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_app') THEN CREATE ROLE usage_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_ro') THEN CREATE ROLE usage_ro LOGIN; END IF;"
    " END $$;"
    "CREATE SCHEMA IF NOT EXISTS meta AUTHORIZATION meta_migrate;"
    "CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;")


@pytest.fixture(scope="module")
def pg():
    """一次性 pg ＋迁移全链（0001→0004）＋PRICING_PSQL_BASE/USAGE_PSQL_BASE 注入（module 级）。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（pg 集成段）")
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=pricing-api-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    saved = {k: os.environ.get(k) for k in ("PRICING_PSQL_BASE", "USAGE_PSQL_BASE", "META_MIGRATE_PSQL_BASE")}
    base = ("docker exec -i " + CONTAINER +
            " psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
    try:
        for _ in range(30):
            if subprocess.run(["docker", "exec", CONTAINER, "pg_isready",
                               "-U", "meta_migrate", "-d", "oneserve_meta"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("一次性 pg 30s 未就绪")
        os.environ["PRICING_PSQL_BASE"] = base
        os.environ["USAGE_PSQL_BASE"] = base
        os.environ.pop("META_MIGRATE_PSQL_BASE", None)
        r = _psql(BOOT_SQL)
        assert r.returncode == 0, r.stderr
        for mig in sorted((REPO / "migrations").glob("*.sql")):
            r = _psql(mig.read_text(encoding="utf-8"))
            assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
        shard = Path(os.environ.get("TMPDIR", "/tmp")) / ("pricing-api-" + uuid.uuid4().hex[:8])
        shard.mkdir(parents=True, exist_ok=True)
        STATE.update({"base": base, "shard": shard})
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def _client() -> "TestClient":
    os.environ["AUTH_OIDC_MODE"] = "off"
    return TestClient(_pricing_app())


def test_crud_flow_create_list_detail_retire(pg):
    """§2.10-4：新建 per_call＋tiered → 不连续负例 422 → 列表展示态 → 详情 → 在窗停用置止日。"""
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/alpha", "model": "per_call", "unit_price": "0.5",
        "effective_from": _first_of_month(TODAY).isoformat(), "remark": "crud-flow 在窗条目"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert re.fullmatch(r"pi_[0-9a-f]{16}", body["itemId"])
    alpha_id = body["itemId"]
    tiers_3 = [{"lower_bound": 0, "upper_bound": 10000, "unit_price": "0"},
               {"lower_bound": 10000, "upper_bound": 50000, "unit_price": "0.8"},
               {"lower_bound": 50000, "upper_bound": None, "unit_price": "0.5"}]
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/gamma", "model": "tiered",
        "effective_from": _shift_months(FIRST_THIS, 1).isoformat(), "tiers": tiers_3})
    assert r.status_code == 200, r.text
    gamma_id = r.json()["itemId"]
    # 不连续负例（人话报因）
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/gamma", "model": "tiered",
        "effective_from": _shift_months(FIRST_THIS, 2).isoformat(),
        "tiers": [{"lower_bound": 0, "upper_bound": 5000, "unit_price": "0"},
                  {"lower_bound": 10000, "upper_bound": None, "unit_price": "0.8"}]})
    assert r.status_code == 422, r.text
    assert "缺 [5000,10000)" in r.json()["message"], r.json()
    # 列表：展示态按今日推算（alpha 在窗／gamma 未生效）＋档位摘要
    r = client.get("/v1/admin/pricing/items")
    items = {i["serviceRef"]: i for i in r.json()["items"]}
    assert items["service:/v1/query/alpha"]["state"] == "in_window"
    assert items["service:/v1/query/gamma"]["state"] == "pending"
    assert items["service:/v1/query/gamma"]["tierSummary"] == \
        "[0,10000)@0.000000 ＋ [10000,50000)@0.800000 ＋ [50000,∞)@0.500000"
    # 详情：档位全览＋历史账脚注
    r = client.get("/v1/admin/pricing/items/" + gamma_id)
    detail = r.json()
    assert len(detail["tiers"]) == 3 and "bill_line.unit_price 快照" in detail["footnote"]
    # 在窗停用＝置止日（P1-⑧ 分派一）
    r = client.post("/v1/admin/pricing/items/%s/retire" % alpha_id, json={})
    assert r.status_code == 200 and r.json()["action"] == "set_end_date", r.text
    assert r.json()["effectiveTo"] == TODAY.isoformat()
    r = client.get("/v1/admin/pricing/items/" + alpha_id)
    assert r.json()["item"]["effectiveTo"] == TODAY.isoformat()
    # 404 信封（未知 item）
    r = client.get("/v1/admin/pricing/items/pi_" + "f" * 16)
    assert r.status_code == 404 and r.json()["error"] == "price_item_not_found", r.text


def test_overlap_window_sequential_and_concurrent(pg):
    """§2.10-5（P1-⑨）：重叠窗顺序拒绝（同日起/跨窗皆 422 人话）＋无缝后窗可建＋并发恰一成一败。"""
    ref = "service:/v1/query/overlap"
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": ref, "model": "per_call", "unit_price": "1",
        "effective_from": _shift_months(FIRST_THIS, 3).isoformat(),
        "effective_to": (_shift_months(FIRST_THIS, 4) - _dt.timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": ref, "model": "per_call", "unit_price": "2",
        "effective_from": _shift_months(FIRST_THIS, 3).isoformat()})
    assert r.status_code == 422, r.text
    assert "UNIQUE" not in r.json()["message"], "同日起走事务内互斥人话，不裸抛 UNIQUE"
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": ref, "model": "per_call", "unit_price": "2",
        "effective_from": _shift_months(FIRST_THIS, 2).isoformat(),
        "effective_to": _shift_months(FIRST_THIS, 4).isoformat()})
    assert r.status_code == 422 and "已有生效区间与之重叠" in r.json()["message"], r.json()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": ref, "model": "per_call", "unit_price": "2",
        "effective_from": _shift_months(FIRST_THIS, 4).isoformat()})
    assert r.status_code == 200, r.text
    # 并发负例：同服务两路并发（各自通道/会话），advisory lock 串行化 → 恰一成一败
    def _create(window_start):
        ch = pa.PricingDbChannel()
        try:
            return "ok", pa.create_item(ch, {"service_ref": "service:/v1/query/par",
                                             "model": "per_call", "unit_price": "1",
                                             "effective_from": window_start.isoformat()}, "tester")
        except pa.PricingDbError as exc:
            return "fail", str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_create, _shift_months(FIRST_THIS, 5))
        f2 = pool.submit(_create, _shift_months(FIRST_THIS, 6))   # 6 号窗起＜5 号窗止＝重叠
        results = [f1.result(), f2.result()]
    outcomes = sorted(o for o, _ in results)
    assert outcomes == ["fail", "ok"], "并发两路应恰一成一败：" + str([m[:200] for _, m in results])
    assert any("已有生效区间与之重叠" in m for o, m in results if o == "fail"), "败因＝事务内互斥（P1-⑨）"


def test_retire_state_dispatch(pg):
    """P1-⑧：未生效→撤回删除（404 后证）；已过期→409 禁用；在窗分派见 crud_flow。"""
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/pending-x", "model": "per_call", "unit_price": "1",
        "effective_from": _shift_months(FIRST_THIS, 2).isoformat()})
    pending_id = r.json()["itemId"]
    r = client.post("/v1/admin/pricing/items/%s/retire" % pending_id, json={})
    assert r.status_code == 200 and r.json()["action"] == "withdrawn", r.text
    assert client.get("/v1/admin/pricing/items/" + pending_id).status_code == 404, "撤回＝条目连档位删除"
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/expired-x", "model": "per_call", "unit_price": "1",
        "effective_from": _shift_months(FIRST_THIS, -2).isoformat(),
        "effective_to": LAST_MONTH_END.isoformat()})
    expired_id = r.json()["itemId"]
    assert client.get("/v1/admin/pricing/items/" + expired_id).json()["item"]["state"] == "expired"
    r = client.post("/v1/admin/pricing/items/%s/retire" % expired_id, json={})
    assert r.status_code == 409, r.text
    assert "已过期（生效止" in r.json()["message"], r.json()


def test_denied_create_writes_narrow_table(pg):
    """被拒提交（校验拒绝）＝窄表 admin_event result=denied 尽力而为落（§0 审计总口径）。"""
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": "service:/v1/query/denied-x", "model": "per_call", "unit_price": "1",
        "effective_from": "2026-10-15"})   # 非月初＝拒
    assert r.status_code == 422
    rows = pa.PricingDbChannel().rows(
        "SELECT action, target, result FROM meta.admin_event WHERE result = 'denied'"
        " AND target = 'service:/v1/query/denied-x' ORDER BY occurred_at DESC LIMIT 1")
    assert rows and rows[0]["action"] == "price_create" and rows[0]["result"] == "denied", rows


def test_hit_preview_coverage_and_engine_priced(pg):
    """§2.10-6（P0-4 核心）：造 usage_daily 命中数据 → 命中预演报告 → priced 覆盖率报告
    → billing_run 引擎原函数按落库 service_ref 精确命中计价（只 import 复用，零改动）。"""
    import usage_rollup as ur
    import billing_run as br
    lo, hi_ex = pa._prev_complete_month()
    end = hi_ex - _dt.timedelta(days=1)
    # ① 造分片→rollup→usage_daily（A 型原值／B 型带审计后缀／无关 endpoint 三行；
    #    独立 endpoint pricedemo——与其余用例的价目零耦合，测试序无关）
    demo = "service:/v1/query/pricedemo"
    days = {
        (lo + _dt.timedelta(days=1)).isoformat(): [
            _ev((lo + _dt.timedelta(days=1)).isoformat() + "T01:00:00+00:00", "sb_k1",
                endpoint=demo, status=200),
            _ev((lo + _dt.timedelta(days=1)).isoformat() + "T02:00:00+00:00", "sb_k1",
                endpoint=demo, status=200),
            _ev((lo + _dt.timedelta(days=1)).isoformat() + "T03:00:00+00:00", "sb_k1",
                endpoint=demo, status=500),
        ],
        (lo + _dt.timedelta(days=2)).isoformat(): [
            _ev((lo + _dt.timedelta(days=2)).isoformat() + "T01:00:00+00:00", "sb_k2",
                endpoint="service:/proxy/beta?retention=on", status=200),
            _ev((lo + _dt.timedelta(days=2)).isoformat() + "T02:00:00+00:00", "sb_k2",
                endpoint="service:/v1/query/other", status=200),
        ],
    }
    for day, evs in sorted(days.items()):
        p = STATE["shard"] / ur.SHARD_FMT.format(day)
        p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs), encoding="utf-8")
        r = _rollup("--day", day)
        assert r.returncode == 0, day + ": " + r.stdout + r.stderr
    # ② API 建价目：service_ref＝endpoint 原值（P0-4 口径），生效窗罩住该账期
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": demo, "model": "per_call", "unit_price": "0.5",
        "effective_from": lo.isoformat(), "remark": "P0-4 实证价目"})
    assert r.status_code == 200, r.text
    item_id = r.json()["itemId"]
    # ③ 命中预演：demo 命中行数报告＋未覆盖样本列出代理/无关 endpoint
    r = client.get("/v1/admin/pricing/hit-preview", params={"service_ref": demo})
    assert r.status_code == 200, r.text
    hp = r.json()
    assert hp["period"] == lo.strftime("%Y-%m") and hp["willHit"] is True
    assert hp["hitRows"] == 1 and hp["hitSuccess"] == 2 and hp["hitCalls"] == 3, \
        "行数＝usage_daily 日行数（同日同键聚合为一行）：calls 3/success 2/fail 1"
    unhit_eps = {u["endpoint"] for u in hp["unhitTop"]}
    assert "service:/proxy/beta?retention=on" in unhit_eps and "service:/v1/query/other" in unhit_eps
    # 归一形与原值等效（B 型剥后缀后同键）
    r = client.get("/v1/admin/pricing/hit-preview", params={"service_ref": demo + "?retention=on"})
    assert r.json()["hitRows"] == 1, "预演入参带审计后缀也归一命中（P0-4）"
    # ④ priced 覆盖率报告（只读统计）：covered＝demo；uncovered＝代理/无关
    r = client.get("/v1/admin/pricing/coverage", params={"period": lo.strftime("%Y-%m")})
    cov = r.json()
    assert cov["status"] == "ok" and cov["totalEndpoints"] == 3, cov
    covered_eps = {c["endpoint"]: c for c in cov["covered"]}
    uncovered_eps = {u["endpoint"] for u in cov["uncovered"]}
    assert covered_eps[demo]["itemId"] == item_id
    assert uncovered_eps == {"service:/proxy/beta?retention=on", "service:/v1/query/other"}
    assert 0 < cov["coverageRatio"] < 1
    # ⑤ 引擎实证：billing_run.load_price 原函数按落库 service_ref 命中＋区间计价
    psql = ur.Psql()
    hit = br.load_price(psql, demo, lo, end, {})
    assert hit is not None and hit[0] == item_id, hit
    assert hit[1] == [(0, None, "0.500000")]
    assert br.price_for(7, hit[1]) == _dt_dec("3.5"), "7 次 × 0.5＝3.50"
    assert br.load_price(psql, "service:/v1/query/other", lo, end, {}) is None
    assert br.load_price(psql, "service:/proxy/beta?retention=on", lo, end, {}) is None, \
        "beta 无价目＝None（B 型带价命中实证另见 test_btype_proxy_price_engine_priced）"


def test_btype_proxy_price_engine_priced(pg):
    """收口批 B 型开闸实证（§2.8 延后条件兑现）：B 型价目创建放行（带后缀原值落库归一形）
    → usage_daily 带审计后缀行 → billing_run.load_price 匹配侧归一命中 → apply_pricing
    出账 priced（on/off 双后缀同归一键，不再 0 元少收）。"""
    import usage_rollup as ur
    import billing_run as br
    lo, hi_ex = pa._prev_complete_month()
    end = hi_ex - _dt.timedelta(days=1)
    bref = "service:/proxy/beta-close"   # 独立 endpoint＝与其余用例价目零耦合（测试序无关）
    day = (lo + _dt.timedelta(days=3)).isoformat()
    evs = [
        _ev(day + "T01:00:00+00:00", "sb_b1", endpoint=bref + "?retention=on", status=200),
        _ev(day + "T02:00:00+00:00", "sb_b1", endpoint=bref + "?retention=on", status=200),
        _ev(day + "T03:00:00+00:00", "sb_b2", endpoint=bref + "?retention=off", status=200),
    ]
    p = STATE["shard"] / ur.SHARD_FMT.format(day)
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in evs), encoding="utf-8")
    r = _rollup("--day", day)
    assert r.returncode == 0, day + ": " + r.stdout + r.stderr
    # ① B 型创建放行（闸已翻）：带审计后缀原值创建 → 落库＝归一形（P0-4 单点）
    client = _client()
    r = client.post("/v1/admin/pricing/items", json={
        "service_ref": bref + "?retention=on", "target_kind": "proxy", "model": "per_call",
        "unit_price": "0.2", "effective_from": lo.isoformat(), "remark": "B 型开闸实证"})
    assert r.status_code == 200, r.text
    item_id = r.json()["itemId"]
    r = client.get("/v1/admin/pricing/items/" + item_id)
    assert r.status_code == 200 and r.json()["item"]["serviceRef"] == bref, \
        "带后缀原值创建落库＝归一形（收口批翻闸后 B 型放行）"
    # ② 引擎匹配侧归一：带后缀行命中同一价目（on/off 双后缀＋归一形三路同键）
    psql = ur.Psql()
    hit_on = br.load_price(psql, bref + "?retention=on", lo, end, {})
    hit_off = br.load_price(psql, bref + "?retention=off", lo, end, {})
    hit_bare = br.load_price(psql, bref, lo, end, {})
    assert hit_on is not None and hit_on[0] == item_id, "带审计后缀行命中 B 型价目（引擎归一落地）"
    assert hit_off is not None and hit_off[0] == item_id, "off 后缀同归一键（裂键回归已根治）"
    assert hit_bare is not None and hit_bare[0] == item_id, "归一形原值直接命中"
    # ③ 出账实证：apply_pricing 原函数对带后缀行 priced＋金额（10 次 × 0.2＝2.00 元）
    rows = [{"endpoint": bref + "?retention=on", "tenant_id": "tenant-price", "caller": "sb_b1",
             "calls": 10, "success": 10, "fail": 0, "lat_sum": 0, "lat_cnt": 0, "events": 10}]
    due = br.apply_pricing(psql, rows, lo, end)
    assert rows[0]["priced"] is True and rows[0]["item_id"] == item_id, rows[0]
    assert rows[0]["amount"] == _dt_dec("2.00") and due == _dt_dec("2.00"), \
        "出账 priced 实证：10 次 × 0.2＝2.00 元（收口批翻闸闭环）"


def _dt_dec(s):
    from decimal import Decimal
    return Decimal(s)


def test_role_matrix_viewer_operator_admin(pg):
    """§2.10-7：viewer GET 200／operator GET 200 POST 403／admin POST 200（域9 矩阵）。"""
    try:
        import jwt  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric import rsa
    except Exception:  # noqa: BLE001
        pytest.skip("依赖缺席：pyjwt/cryptography（角色矩阵段）")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    pub = key.public_key().public_numbers()

    def b64u_int(n):
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    jwk = {"kty": "RSA", "kid": "test-kid-1", "alg": "RS256",
           "n": b64u_int(pub.n), "e": b64u_int(pub.e)}
    orig_fetch, orig_cache = AO._fetch_jwks, dict(AO._jwks_cache)
    AO._jwks_cache.update({"keys": [jwk], "at": time.time()})
    AO._fetch_jwks = lambda url: {"keys": [jwk]}

    def issue(tag):
        claims = {"iss": "http://localhost:8000", "sub": "oneserve/" + tag,
                  "aud": "oneserve-admin-client", "exp": int(time.time()) + 600,
                  "iat": int(time.time()), "tag": tag}
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-kid-1"})

    saved_mode = os.environ.get("AUTH_OIDC_MODE")
    os.environ["AUTH_OIDC_MODE"] = "on"
    try:
        client = TestClient(_pricing_app())
        h = {"Authorization": "Bearer " + issue("viewer")}
        assert client.get("/v1/admin/pricing/items", headers=h).status_code == 200, "viewer 读 200"
        assert client.get("/v1/admin/pricing/selector", headers=h).status_code == 200, "viewer 选择器 200"
        r = client.post("/v1/admin/pricing/items", headers=h,
                        json={"service_ref": "service:/v1/query/rolecheck", "model": "per_call",
                              "unit_price": "1", "effective_from": _shift_months(FIRST_THIS, 1).isoformat()})
        assert r.status_code == 403 and r.json()["error"] == "role_forbidden", "viewer 写 403"
        h2 = {"Authorization": "Bearer " + issue("operator")}
        assert client.get("/v1/admin/pricing/items", headers=h2).status_code == 200, "operator 读 200"
        r = client.post("/v1/admin/pricing/items", headers=h2,
                        json={"service_ref": "service:/v1/query/rolecheck", "model": "per_call",
                              "unit_price": "1", "effective_from": _shift_months(FIRST_THIS, 1).isoformat()})
        assert r.status_code == 403 and r.json()["error"] == "role_forbidden", "operator 写 403"
        h3 = {"Authorization": "Bearer " + issue("admin")}
        r = client.post("/v1/admin/pricing/items", headers=h3,
                        json={"service_ref": "service:/v1/query/rolecheck", "model": "per_call",
                              "unit_price": "1", "effective_from": _shift_months(FIRST_THIS, 1).isoformat()})
        assert r.status_code == 200, "admin 写 200：" + r.text
        assert r.json()["item"]["createdAt"], "admin 建价目落库"
    finally:
        if saved_mode is None:
            os.environ.pop("AUTH_OIDC_MODE", None)
        else:
            os.environ["AUTH_OIDC_MODE"] = saved_mode
        AO._fetch_jwks = orig_fetch
        AO._jwks_cache.clear()
        AO._jwks_cache.update(orig_cache)