# -*- coding: utf-8 -*-
"""admin_api 端到端切 db 验证（卡-1b7 收口批项3/4 · META_WRITER=db 隔离栈，禁碰 dev 容器组）。

隔离模式沿 test_meta_writer_db 先例：docker run --rm postgres:17-alpine 一次性容器
（LOGIN 角色版），通道经 META_WRITER_PSQL_BASE／META_MIGRATE_PSQL_BASE 钩子注入；
源/快照目录均 tmp（真 service/ 四源零触碰）；docker 不可达＝pytest.skip 降级。

覆盖（收口批验收线「db 模式四域端点」口径）：
  ① 发布域 POST /v1/admin/publish      → service_catalog 行＋admin_event/version_history；
  ② 签发域 POST /v1/admin/appkeys      → credential/grant 行＋网关下发假件（consumer
    put/delete monkeypatch，同 test_p3_auth_loop 口径——真网关不在隔离栈面）；
  ③ 授权域 POST /v1/admin/grants       → matrix_save 落库（grants_admin.save_matrix
    接线 mw.matrix_save 的端点级实证）＋grant 扩展列 effective_from/to（项2）；
  ④ 上游域 POST /v1/admin/upstreams    → upstream_proxy 行；
  ⑤ 沙箱空壳投影（项4）：tenant_upsert 纯空壳沙箱租户 → 快照 auth_matrix 沙箱段；
  ⑥ 三方对账绿（源=快照 vs 表 vs 快照）＋逆向对账（快照 → 空库 apply 行数一致，
    含扩展列与空壳租户往返）。
因果链：每例后 version_history↔admin_event event_id 对偶零悬空（FK 钉死）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_writer as mw          # noqa: E402
from app import appkeys as appk            # noqa: E402
from app import ops_config as opsc         # noqa: E402
from app import kv_executor as kvx         # noqa: E402

try:
    from fastapi import FastAPI            # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from unittest.mock import AsyncMock, MagicMock
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

CONTAINER = "oneserve-meta-e2e-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
SRC_FILES = ["services.yaml", "upstreams.yaml", "auth_matrix.yaml"]
STATE = {}

BOOT_SQL = (
    "DO $$ BEGIN"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_app') THEN"
    " CREATE ROLE meta_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_ro') THEN"
    " CREATE ROLE meta_ro LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_app') THEN"
    " CREATE ROLE usage_app LOGIN; END IF;"
    " IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='usage_ro') THEN"
    " CREATE ROLE usage_ro LOGIN; END IF; END $$;"
    "CREATE SCHEMA IF NOT EXISTS meta AUTHORIZATION meta_migrate;"
    "CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;")


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _psql(db: str, sql: str, *, input_text: str | None = None):
    return subprocess.run(["docker", "exec", "-i", CONTAINER, "psql", "-U", "meta_migrate",
                           "-d", db, "-v", "ON_ERROR_STOP=1", "-A", "-t"],
                          input=(input_text if input_text is not None else sql),
                          capture_output=True, text=True, timeout=120)


def _count(db: str, table: str, where: str = "") -> int:
    sql = "SELECT count(*) FROM meta." + table + ((" WHERE " + where) if where else "") + ";"
    p = _psql(db, sql)
    assert p.returncode == 0, p.stderr
    return int((p.stdout or "0").strip().splitlines()[-1] or 0)


def _one(db: str, sql: str) -> str:
    p = _psql(db, sql)
    assert p.returncode == 0, p.stderr
    return (p.stdout or "").strip().splitlines()[-1] if (p.stdout or "").strip() else ""


def _cli(script: str, *args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)


@pytest.fixture(scope="module")
def stack():
    """一次性 pg 容器＋迁移链＋快照基线＋META_WRITER=db env（module 级全链就绪）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=meta-e2e-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    saved = {k: os.environ.get(k) for k in
             ("META_WRITER", "META_WRITER_PSQL_BASE", "META_MIGRATE_PSQL_BASE",
              "META_MIGRATE_SRC_DIR", "META_SNAPSHOT_DIR")}
    try:
        for _ in range(30):
            if subprocess.run(["docker", "exec", CONTAINER, "pg_isready",
                               "-U", "meta_migrate", "-d", "oneserve_meta"],
                              capture_output=True).returncode == 0:
                break
            time_sleep(1)
        else:
            pytest.fail("一次性 pg 30s 未就绪")
        _boot_and_load()
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


def time_sleep(s: float) -> None:
    import time
    time.sleep(s)


def _boot_and_load():
    base = Path(os.environ.get("TMPDIR", "/tmp")) / ("metae2e-" + uuid.uuid4().hex[:8])
    src = base / "src"
    (src / "registry").mkdir(parents=True)
    for name in SRC_FILES:
        (src / name).write_text((REPO / "service" / name).read_text(encoding="utf-8"))
    (src / "registry" / "appkeys.yaml").write_text(
        (REPO / "service" / "registry" / "appkeys.yaml").read_text(encoding="utf-8"))
    snap = base / "snap"
    psql_base = ("docker exec -i " + CONTAINER +
                 " psql -U meta_app -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
    mig_base = ("docker exec -i " + CONTAINER +
                " psql -U meta_migrate -d oneserve_meta -v ON_ERROR_STOP=1 -A -t -P footer=off")
    os.environ["META_WRITER"] = "db"
    os.environ["META_WRITER_PSQL_BASE"] = psql_base
    os.environ["META_MIGRATE_PSQL_BASE"] = mig_base
    os.environ["META_MIGRATE_SRC_DIR"] = str(src)
    os.environ["META_SNAPSHOT_DIR"] = str(snap)
    mw._channel_inst = None
    mw._svc_ids_cache = None
    mw._proxy_ids_cache = None
    r = _psql("oneserve_meta", "", input_text=BOOT_SQL)
    assert r.returncode == 0, r.stderr
    for mig in sorted((REPO / "migrations").glob("*.sql")):
        r = _psql("oneserve_meta", mig.read_text(encoding="utf-8"))
        assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
    r = _cli("meta_migrate.py", "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _cli("meta_reconcile.py", "--baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    STATE.update({"src": src, "snap": snap, "base": base,
                  "counts0": {t: _count("oneserve_meta", t) for t in
                              ("field_space", "tenant", "service_catalog", "upstream_proxy",
                               "credential", "grant", "admin_event", "version_history")}})


def _causal_link_intact() -> None:
    """version_history↔admin_event event_id 对偶零悬空（FK 因果链机检）。"""
    n = _count("oneserve_meta",
               "version_history vh LEFT JOIN meta.admin_event ae ON ae.event_id = vh.event_id",
               "ae.event_id IS NULL")
    assert n == 0, "version_history 悬空 event_id"


@pytest.fixture(scope="module")
def client(stack):
    """TestClient：admin_api 路由＋db 模式运行态（读侧状态指向快照＝库镜像）。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（端到端契约；另有活体验证）")
    from app import admin_api
    snap = STATE["snap"]
    app = FastAPI()
    mock_redis = MagicMock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)
    app.state.redis = mock_redis
    # 注册表读侧对齐生产口径（main.py lifespan / meta_snapshot.hot_read_path 同款）：
    # services.yaml 读侧不切快照——快照运行时区按 origin 还原、console 覆盖行落写区后
    # 变薄（本 fork 6/6 服务被 console 覆盖→快照运行时区恒空），load_services 只读运行时区，
    # 指快照会使签发域服务校验恒 unknown_service；仓内 services.yaml 才是全集权威源。
    app.state.services = kvx.load_services(kvx.SERVICES_YAML)
    app.state.auth_matrix = kvx.load_auth_matrix(snap / "auth_matrix.yaml")
    app.state.appkeys = appk.load_appkeys(snap / "registry" / "appkeys.yaml")["keys"]
    app.state.appkeys_path = snap / "registry" / "appkeys.yaml"
    app.state.auth_matrix_path = snap / "auth_matrix.yaml"
    app.include_router(admin_api.router)
    orig_put, orig_del = admin_api.gw_consumer.put_consumer, admin_api.gw_consumer.delete_consumer
    admin_api.gw_consumer.put_consumer = AsyncMock(return_value=True)
    admin_api.gw_consumer.delete_consumer = AsyncMock(return_value=True)
    yield TestClient(app)
    admin_api.gw_consumer.put_consumer = orig_put
    admin_api.gw_consumer.delete_consumer = orig_del


def test_01_publish_domain_endpoint_writes_db(client, stack):
    """发布域：POST /v1/admin/publish → service_catalog 行＋双台账＋快照。"""
    c0 = STATE["counts0"]
    r = client.post("/v1/admin/publish", json={
        "serviceName": "元数据库e2e探针", "routePath": "/v1/svc/meta-e2e-1b7-probe",
        "fields": [{"name": "user_name", "sample": "D", "desc": "", "desensitize": ""}],
        "fieldSpace": "proxy", "status": "published", "env": "测试", "instance": "实例A"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    sid = r.json()["serviceId"]
    assert sid == "v1-svc-meta-e2e-1b7-probe"
    assert _count("oneserve_meta", "service_catalog", f"service_id = '{sid}'") == 1
    assert _count("oneserve_meta", "admin_event",
                  f"action = 'service_publish' AND target = '{sid}'") == 1
    assert _count("oneserve_meta", "service_catalog") == c0["service_catalog"] + 1
    _causal_link_intact()
    # 快照双区还原含执行字段（项1 验收：写径走合并后规划面，孪生执行字段不回退）
    from app import meta_snapshot as ms
    console = opsc.load_console_services(STATE["snap"] / "services.yaml")
    assert any(e["id"] == sid for e in console)
    mig = ms.load_migrate()
    rows = mig.plan_service_catalog(STATE["snap"] / "services.yaml")[0]
    twin = next(x for x in rows if x["service_id"] == "v1-svc-usr-basic-info")
    assert twin["guard"] and twin["audit"] and twin["response"] and twin["archetype"], \
        "写径后孪生执行字段回退丢失"


def test_02_issue_domain_endpoint_writes_db(client, stack):
    """签发域：POST /v1/admin/appkeys → credential/grant 行＋网关假件下发＋台账。"""
    c0 = STATE["counts0"]
    # demo_bank＝登记面恰 1 把（上限 2：正常＋轮转各一把；partner-demo 已满额拒发）。
    # 原内网版签发租户 demo-trial 系出仓剥离对象，fork 授权矩阵/demo 注册表中不存在，
    # 改用矩阵在册 demo 租户（meta.tenant 行在场是 credential 落库外键前提）
    r = client.post("/v1/admin/appkeys", json={
        "tenant": "demo_bank", "app_name": "e2e签发探针", "key_type": "sandbox",
        "caller_name": "e2e-probe", "quota_qps": 30, "quota_daily": 5000,
        "grants": [{"service": "v1-svc-usr-basic-info",
                    "endpoint": "/v1/svc/usr-basic-info",
                    "fields": ["user_name"]}]})
    assert r.status_code == 200, r.text
    ak = r.json()["appkey"]
    assert ak.startswith("sb_")
    assert _count("oneserve_meta", "credential", f"appkey = '{ak}'") == 1
    assert _count("oneserve_meta", "grant",
                  f"scope = 'credential' AND credential_appkey = '{ak}'") == 1
    assert _count("oneserve_meta", "admin_event",
                  f"action = 'credential_issue' AND target = '{ak}'") == 1
    assert _count("oneserve_meta", "credential") == c0["credential"] + 1
    _causal_link_intact()
    keys = appk.load_appkeys(STATE["snap"] / "registry" / "appkeys.yaml")["keys"]
    assert ak in {k["appkey"] for k in keys}, "快照注册表未随签发更新"


def test_03_grants_domain_endpoint_writes_db(client, stack):
    """授权域：POST /v1/admin/grants（含生效区间）→ grant 行＋matrix_save 台账（项2/项3）。"""
    c0 = STATE["counts0"]
    # 授权目标＝已登记代理（整路由授权 fields 空集合法——服务类须字段在册校验，
    # 注册表条目 fields 为投影声明面，1b-8 授权页对代理免字段清单）
    # 授权租户＝partner-demo（demo_bank/demo-1020 存量数据凭证级⊆租户级本就违例，
    # 全行不变量校验如实拦下——运行态脏数据归其卡，非本批范围；partner-demo 子集干净）
    r = client.post("/v1/admin/grants", json={
        "action": "upsert", "tenant": "partner-demo", "service": "proxy-ext-asset-tag-1020",
        "fields": [],
        "effective_from": "2026-10-21", "effective_to": "2027-10-21"})
    assert r.status_code == 200, r.text
    assert _count("oneserve_meta", "grant",
                  "scope = 'tenant' AND tenant_id = 'partner-demo' "
                  "AND proxy_id = 'proxy-ext-asset-tag-1020'") == 1
    row = _one("oneserve_meta",
               "SELECT effective_from || '|' || effective_to || '|' || oa_ticket "
               "FROM meta.\"grant\" WHERE tenant_id = 'partner-demo' "
               "AND proxy_id = 'proxy-ext-asset-tag-1020';")
    assert row == "2026-10-21|2027-10-21|DEMO-20260929-002", "扩展列/单号回落不符：" + row
    assert _count("oneserve_meta", "grant") == c0["grant"] + 2  # 签发域已 +1 凭证级，本域 +1 租户级
    assert _count("oneserve_meta", "admin_event",
                  "action = 'matrix_save' AND operator = 'admin' "
                  "AND target = 'grant'") == 1, "授权域台账（operator=端点登录态回退）缺席"
    _causal_link_intact()
    # 快照授权段随写（grant 级扩展字段带出：生效区间与行级同值单号省略）
    mx = yaml.safe_load((STATE["snap"] / "auth_matrix.yaml").read_text(encoding="utf-8"))["matrix"]
    drow = next(x for x in mx if x.get("tenant") == "partner-demo")
    g = next(g for g in drow["grants"] if g.get("service") == "proxy-ext-asset-tag-1020")
    assert g.get("effective_from") == "2026-10-21" and g.get("effective_to") == "2027-10-21"
    assert "oa_ticket" not in g, "与租户级同值单号不应重复输出（往返省略口径）"
    assert drow.get("tenant_type") is None, "external 戳应省略（往返省略口径）"
    assert next(x for x in mx if x.get("tenant") == "demo_bank").get("tenant_type") \
        == "internal", "tenant_type 戳未随快照还原（项2）"


def test_04_upstream_domain_endpoint_writes_db(client, stack):
    """上游域：POST /v1/admin/upstreams → upstream_proxy 行＋台账。"""
    c0 = STATE["counts0"]
    r = client.post("/v1/admin/upstreams", json={
        "serviceName": "元数据库e2e代理", "routePath": "/proxy/metae2e/probe",
        "method": "POST", "upstreamUrl": "http://oneserve-service:8000/v1/svc/usr-basic-info",
        "status": "draft", "env": "测试", "instance": "实例A"})
    assert r.status_code == 200, r.text
    pid = r.json()["entry"]["id"]
    assert _count("oneserve_meta", "upstream_proxy", f"id = '{pid}'") == 1
    assert _count("oneserve_meta", "upstream_proxy") == c0["upstream_proxy"] + 1
    assert _count("oneserve_meta", "admin_event",
                  f"action = 'upstream_save' AND target = '{pid}'") == 1
    _causal_link_intact()
    assert "/proxy/metae2e/probe" in (STATE["snap"] / "upstreams.yaml").read_text(encoding="utf-8")


def test_05_sandbox_shell_projection(stack):
    """沙箱空壳投影（项4）：纯空壳沙箱租户 → 快照 auth_matrix 沙箱段载体。"""
    row = mw.tenant_upsert(tenant_id="e2e-shell-tenant", display_name="e2e空壳沙箱",
                           tenant_type="sandbox", status="active",
                           oa_ticket_open=None, remark="auth_matrix 登记",
                           operator="e2e-probe")
    assert row["tenant_id"] == "e2e-shell-tenant"
    assert _count("oneserve_meta", "tenant",
                  "tenant_id = 'e2e-shell-tenant' AND tenant_type = 'sandbox'") == 1
    mx = yaml.safe_load((STATE["snap"] / "auth_matrix.yaml").read_text(encoding="utf-8"))["matrix"]
    shell = next(x for x in mx if x.get("tenant") == "e2e-shell-tenant")
    assert shell.get("tenant_type") == "sandbox" and "appkey" not in shell \
        and "oa_ticket" not in shell, "沙箱空壳投影段形状不符：" + str(shell)


def test_06_three_way_reconcile_green(stack):
    """三方对账绿：源(=快照) vs 表 vs 快照 exit 0（四域写＋空壳租户后）。"""
    snap = str(STATE["snap"])
    r = _cli("meta_reconcile.py",
             env_extra={"META_WRITER": "db", "META_MIGRATE_SRC_DIR": snap,
                        "META_SNAPSHOT_DIR": snap})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DIFF]" not in r.stdout


def test_07_reverse_apply_extension_and_shell_roundtrip(stack):
    """逆向对账：快照 → 空库 apply 行数一致＋扩展列与空壳租户往返（项2/项4）。"""
    r = _psql("oneserve_meta", "DROP DATABASE IF EXISTS oneserve_meta2;")
    assert r.returncode == 0, r.stderr
    r = _psql("oneserve_meta", "CREATE DATABASE oneserve_meta2;")
    assert r.returncode == 0, r.stderr
    r = _psql("oneserve_meta2", "", input_text=BOOT_SQL)
    assert r.returncode == 0, r.stderr
    for mig in sorted((REPO / "migrations").glob("*.sql")):
        r = _psql("oneserve_meta2", mig.read_text(encoding="utf-8"))
        assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
    mig_base = ("docker exec -i " + CONTAINER +
                " psql -U meta_migrate -d oneserve_meta2 -v ON_ERROR_STOP=1"
                " -A -t -P footer=off")
    r = _cli("meta_migrate.py", "apply", "--repeat-safe",
             env_extra={"META_MIGRATE_PSQL_BASE": mig_base,
                        "META_MIGRATE_SRC_DIR": str(STATE["snap"])})
    assert r.returncode == 0, r.stdout + r.stderr
    for table in ("field_space", "tenant", "service_catalog", "upstream_proxy",
                  "credential", "grant"):
        n1 = _count("oneserve_meta", table)
        n2 = _count("oneserve_meta2", table)
        assert n1 == n2, f"逆向对账行数不一致：{table} {n1} != {n2}"
    # 扩展列往返（项2）：新授权的生效区间在新库原样在列
    row = _one("oneserve_meta2",
               "SELECT effective_from || '|' || effective_to FROM meta.\"grant\" "
               "WHERE tenant_id = 'partner-demo' AND proxy_id = 'proxy-ext-asset-tag-1020';")
    assert row == "2026-10-21|2027-10-21", "生效区间逆向漂移：" + row
    # 沙箱空壳租户往返（项4）：投影载体还原不丢租户行
    assert _count("oneserve_meta2", "tenant",
                  "tenant_id = 'e2e-shell-tenant' AND tenant_type = 'sandbox'") == 1
    # 六表哈希级一致（条数之上再钉字段级；table_hash_snapshot＝对账同款聚合）
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "oneserve_meta_migrate_e2e", REPO / "scripts" / "meta_migrate.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    class _R:
        def __init__(self, db):
            self.db = db

        def sql(self, script):
            p = _psql(self.db, script)
            assert p.returncode == 0, p.stderr
            return p.stdout

    from app import meta_snapshot as ms
    with ms.planner_dir(mig, STATE["snap"]):
        h1 = mig.table_hash_snapshot(_R("oneserve_meta"))
        h2 = mig.table_hash_snapshot(_R("oneserve_meta2"))
    assert h1 == h2, "六表哈希不一致：\n" + h1 + "\n" + h2
