#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meta_writer db 模式套件（卡-1b7 步6 · 隔离一次性 pg 容器，禁碰 dev 容器组）。

隔离模式沿 test_meta_migrate 先例：docker run --rm postgres:17-alpine 一次性容器
（角色建 LOGIN 版——meta_app 走本地 trust 直连，即写通道角色）；通道经
META_WRITER_PSQL_BASE／META_MIGRATE_PSQL_BASE 两钩子注入；源/快照目录均 tmp
（真 service/ 四源零触碰）；docker 不可达＝pytest.skip 降级。

覆盖（任务单步6 口径）：
  ① 基线：迁移 apply＋快照基线导出（reconcile db 态 --bootstrap 路径）＋行数锚点；
  ② 失败全回滚：quota_qps=-1 撞 DDL CHECK → 整事务回滚零写入＋abort 台账行在案
    （DR runbook 口径）；
  ③ 六实体写各一例：建租户／发凭证（issue＋save 两拍端点流）／改授权（matrix_save）／
    上下线服务（publish＋offline 两入口）／存代理草稿（upstream_save）／发布
    （publish）——表内断言＋version_history/admin_event 断言＋快照生成与格式还原；
  ④ 逆向对账：快照 yaml 再 apply 回另一空库（oneserve_meta2）行数一致；
  ⑤ 三方对账绿：META_WRITER=db 源(=快照) vs 表 vs 快照 exit 0；
  ⑥ 幂等重存（credential_save 复跑零台账）＋yaml 缺省回归（字节级 disable 等价）。
已知边界（模块头注同款，不在此重复）：renewed_at 不入库【遗留】；纯沙箱空壳租户
已落地投影载体（收口批项4：单键沙箱行，remark 归位边界沿 meta_writer 头注）。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_writer as mw          # noqa: E402
from app import meta_snapshot as ms        # noqa: E402  （锚点测试用规划器解析源副本行数）
from app import appkeys as appk            # noqa: E402
from app import ops_config as opsc         # noqa: E402

CONTAINER = "oneserve-meta-writer-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
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


def _cli(script: str, *args, env_extra=None):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)
@pytest.fixture(scope="module")
def stack():
    """一次性 pg 容器＋角色/schema/DDL＋迁移装载＋快照基线（module 级全链就绪）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=meta-migrate-test", PG_IMAGE],
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
            time.sleep(1)
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


def _boot_and_load():
    base = Path(os.environ.get("TMPDIR", "/tmp")) / ("metadb-" + uuid.uuid4().hex[:8])
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
    mw._channel_inst = None                 # 白盒：通道按本套件 env 重建
    mw._svc_ids_cache = None
    mw._proxy_ids_cache = None
    # ① 角色（LOGIN 版）＋schema ＋ 迁移链按序全量（0001 十表 → 0002 扩展列……）
    r = _psql("oneserve_meta", "", input_text=BOOT_SQL)
    assert r.returncode == 0, r.stderr
    for mig in sorted((REPO / "migrations").glob("*.sql")):
        r = _psql("oneserve_meta", mig.read_text(encoding="utf-8"))
        assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
    # ② meta_migrate apply（四 yaml 全量装载；步4 工具）
    r = _cli("meta_migrate.py", "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    # ③ 快照基线导出（reconcile db 态基线路径＝从库导出）
    r = _cli("meta_reconcile.py", "--baseline")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "基线快照已导出" in r.stdout
    STATE.update({"src": src, "snap": snap, "base": base,
                  "bootstrap_out": r.stdout,
                  "counts0": {t: _count("oneserve_meta", t) for t in
                              ("field_space", "tenant", "service_catalog",
                               "upstream_proxy", "credential", "grant",
                               "admin_event", "version_history")}})


def test_00_stack_ready_and_baseline_anchors(stack):
    """基线就绪：四快照在场＋行数锚点（锚＝套件自拷四源副本，不随工作树运行态漂移）。

    收口小批（2026-10-21）纠偏：原锚 8/4/12/31/10/7 写死旧工作树形态（运行态
    services.yaml 收口恢复 HEAD 态后 service_catalog 7≠8 恒红）。改锚＝fixture
    装载的同一批 tmp 四源副本经迁移规划器解析的行数（DB↔源对表）——「栈就绪＋
    基线锚」语义不变，锚点真源从工作树快照改为随装载源自洽；快照读侧可解析锚
    同步改锚源侧读数（快照＝源的导出，两侧读数应恒等）。
    """
    snap = STATE["snap"]
    for rel in ("services.yaml", "upstreams.yaml", "registry/appkeys.yaml",
                "auth_matrix.yaml"):
        assert (snap / rel).is_file(), "基线快照缺文件：" + rel
    rows = ms.planned_rows_from_dir(ms.load_migrate(), STATE["src"])
    c0 = STATE["counts0"]
    assert c0["service_catalog"] == len(rows["service_catalog"])
    assert c0["upstream_proxy"] == len(rows["upstream_proxy"])
    assert c0["credential"] == len(rows["credential"])
    assert c0["grant"] == len(rows["grant"])
    assert c0["tenant"] == len(rows["tenant"])
    assert c0["field_space"] == len(rows["field_space"])
    # 快照可被既有读方原样解析，读数＝源侧读数（读侧零切换的前提）
    assert "console_services" in (snap / "services.yaml").read_text(encoding="utf-8")
    assert len(opsc.load_console_services(snap / "services.yaml")) == \
        len(opsc.load_console_services(STATE["src"] / "services.yaml"))
    assert len(appk.load_appkeys(snap / "registry" / "appkeys.yaml")["keys"]) == \
        len(appk.load_appkeys(STATE["src"] / "registry" / "appkeys.yaml")["keys"])
def test_01_rollback_negative_and_abort_ledger(stack):
    """失败全回滚：quota_qps=-1 撞 DDL CHECK → 零写入＋abort 台账（version_history+admin_event）。"""
    ak_path = STATE["snap"] / "registry" / "appkeys.yaml"
    before = _count("oneserve_meta", "credential")
    abort_before = _count("oneserve_meta", "version_history",
                          "delta_after ->> 'abort' IS NOT NULL")
    doc = appk.load_appkeys(ak_path)
    doc["keys"][0]["quota_qps"] = -1
    with pytest.raises(mw.MetaDbError) as ei:
        mw.credential_save(ak_path, doc, operator="demo-test")
    assert "quota_qps_check" in str(ei.value), "回滚非 DDL CHECK 所致：" + str(ei.value)
    assert _count("oneserve_meta", "credential") == before, "失败事务竟有行写入"
    abort_after = _count("oneserve_meta", "version_history",
                         "delta_after ->> 'abort' IS NOT NULL")
    assert abort_after == abort_before + 1, "version_history 未记 abort 行（DR runbook 口径）"
    assert _count("oneserve_meta", "admin_event", "result = 'abort'") >= 1
def test_02_six_entity_writes_end_to_end(stack):
    """六实体写各一例：建租户／发凭证／改授权／上下线服务／存代理草稿／发布。"""
    snap = STATE["snap"]
    svc_path = snap / "services.yaml"
    ups_path = snap / "upstreams.yaml"
    ak_path = snap / "registry" / "appkeys.yaml"
    mx_path = snap / "auth_matrix.yaml"
    fixed = datetime.datetime(2026, 10, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)

    # ① 建租户（外部登记；remark 用规划器常量防 matrix_save 归位差量——已知边界）
    row = mw.tenant_upsert(tenant_id="meta1b7ext", display_name="元数据库1b7外部租户",
                           tenant_type="external", status="active",
                           oa_ticket_open="OA-20261020-0001", remark="auth_matrix 登记",
                           operator="demo-test", oa_ticket="OA-20261020-0001")
    assert row["tenant_id"] == "meta1b7ext"
    assert _count("oneserve_meta", "tenant", "tenant_id = 'meta1b7ext'") == 1

    # ② 发凭证（issue 纯逻辑→save 库事务；端点「签发→下发→save」两拍流逐字保留）
    doc = appk.load_appkeys(ak_path)
    matrix = yaml.safe_load(mx_path.read_text(encoding="utf-8"))["matrix"]
    rec, secret = mw.credential_issue(
        doc, matrix, {"v1-svc-usr-basic-info": {}}, tenant="meta1b7ext",
        app_name="1b7-db写入验证", key_type="sandbox", caller_name="1b7-probe",
        grants=[{"service": "v1-svc-usr-basic-info", "endpoint": "/v1/svc/usr-basic-info",
                 "fields": ["user_name"]}], now=fixed)
    assert rec["appkey"].startswith("sb_") and secret
    doc["keys"].append(rec)   # 端点两拍流原样：issue 不追加，append 归端点（admin_api 同款）
    mw.credential_save(ak_path, doc, operator="demo-test", secret_plain=secret)
    assert _count("oneserve_meta", "credential",
                  "appkey = '" + rec["appkey"] + "'") == 1
    assert _count("oneserve_meta", "grant",
                  "scope = 'credential' AND credential_appkey = '" + rec["appkey"] + "'") == 1
    # 幂等重存：同 doc 复跑＝零写零台账（upsert 幂等语义含台账）
    ae_before = _count("oneserve_meta", "admin_event")
    mw.credential_save(ak_path, doc, operator="demo-test")
    assert _count("oneserve_meta", "admin_event") == ae_before, "幂等重存竟记台账"

    # ③ 改授权（矩阵写径：grants_admin 双层校验原样发生＋grant 全删重插差量）
    data = yaml.safe_load(mx_path.read_text(encoding="utf-8"))
    drow = next(r for r in data["matrix"] if r.get("tenant") == "demo_bank")
    drow.setdefault("grants", []).append(
        {"service": "proxy-local-usr-asset-tag", "fields": []})   # 整路由授权（fields 空仅代理合法）
    mw.matrix_save(mx_path, data, operator="demo-test")
    assert _count("oneserve_meta", "grant",
                  "scope = 'tenant' AND tenant_id = 'demo_bank'"
                  " AND proxy_id = 'proxy-local-usr-asset-tag'") >= 1

    # ④ 发布＋下线服务（publish/offline 两入口；视图＝副本演练产物原样返回）
    entry, prev = mw.service_publish(
        svc_path, name="元数据库1b7探针服务", route="/v1/svc/meta-db-1b7-probe",
        fields=[{"name": "user_name", "sample": "A", "desc": "", "desensitize": ""}],
        field_space="proxy", status="published", operator="demo-test",
        env="测试", instance="实例A")
    SID = "v1-svc-meta-db-1b7-probe"     # _slug_from_route＝整条路由 slug 化（既有口径）
    assert entry and entry["id"] == SID
    off_entry, off_prev = mw.service_offline(svc_path, SID, operator="demo-test")
    assert off_entry["status"] == "offline"

    # ⑤ 存代理草稿
    px, px_prev = mw.upstream_save(
        ups_path, service_name="元数据库1b7代理", route="/proxy/metadb/probe-draft",
        method="POST", upstream_url="http://localhost:8000/v1/svc/usr-basic-info",
        status="draft", operator="demo-test", env="测试", instance="实例A")
    assert px["status"] == "draft"

    # —— 表内断言（六实体各 +N） ——
    assert _count("oneserve_meta", "tenant") == STATE["counts0"]["tenant"] + 1
    assert _count("oneserve_meta", "credential") == STATE["counts0"]["credential"] + 1
    assert _count("oneserve_meta", "service_catalog") == STATE["counts0"]["service_catalog"] + 1
    assert _count("oneserve_meta", "upstream_proxy") == STATE["counts0"]["upstream_proxy"] + 1
    assert _count("oneserve_meta", "grant") == STATE["counts0"]["grant"] + 2

    # —— 台账断言：六动作齐全、vh↔ae 因果链闭合（event_id FK 对偶）、delta 形状 ——
    actions = _psql("oneserve_meta", "SELECT DISTINCT action FROM meta.admin_event "
                    "WHERE result = 'ok' AND operator = 'demo-test';").stdout
    for a in ("tenant_upsert", "credential_issue", "matrix_save",
              "service_publish", "service_offline", "upstream_save"):
        assert a in actions, "admin_event 缺动作：" + a
    assert _count("oneserve_meta",
                  "version_history vh LEFT JOIN meta.admin_event ae"
                  " ON ae.event_id = vh.event_id",
                  "ae.event_id IS NULL") == 0, "version_history 悬空 event_id"
    assert _count("oneserve_meta", "version_history",
                  "entity = 'tenant' AND entity_id = 'meta1b7ext'"
                  " AND delta_before IS NULL") == 1
    assert _count("oneserve_meta", "version_history",
                  "entity = 'grant' AND entity_id = '*'") >= 1
    src = (REPO / "service" / "app" / "meta_writer.py").read_text(encoding="utf-8")
    assert "FOR UPDATE" in src, "行锁语句缺失（卡步6 口径）"

    # —— 快照生成与格式还原（双区结构＋三表按原头注结构；读方原样解析） ——
    svc_text = (snap / "services.yaml").read_text(encoding="utf-8")
    assert opsc.CONSOLE_MARKER in svc_text and "services:" in svc_text
    console = opsc.load_console_services(snap / "services.yaml")
    probe = next(e for e in console if e["id"] == "v1-svc-meta-db-1b7-probe")
    assert probe["status"] == "offline"
    keys = appk.load_appkeys(snap / "registry" / "appkeys.yaml")["keys"]
    assert rec["appkey"] in {k["appkey"] for k in keys}
    assert "/proxy/metadb/probe-draft" in (snap / "upstreams.yaml").read_text(encoding="utf-8")
    STATE["last_issued"] = rec["appkey"]
def test_03_reverse_apply_snapshot_row_counts_match(stack):
    """逆向对账：快照 yaml → 另一空库（oneserve_meta2）apply → 行数与源库一致。"""
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
    for table, where in (("field_space", ""), ("tenant", ""), ("service_catalog", ""),
                         ("upstream_proxy", ""), ("credential", "deleted_at IS NULL"),
                         ("grant", "")):
        n1 = _count("oneserve_meta", table, where)
        n2 = _count("oneserve_meta2", table, where)
        assert n1 == n2, "逆向对账行数不一致：" + table + " " + str(n1) + " != " + str(n2)


def test_04_three_way_reconcile_green(stack):
    """三方对账绿：META_WRITER=db 下 源(=快照) vs 表 vs 快照 exit 0（验收线②库后继形态）。"""
    snap = str(STATE["snap"])
    r = _cli("meta_reconcile.py",
             env_extra={"META_WRITER": "db", "META_MIGRATE_SRC_DIR": snap,
                        "META_SNAPSHOT_DIR": snap})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[DIFF]" not in r.stdout
    assert "三方对账" in r.stdout


def test_05_yaml_default_regression(tmp_path, stack, monkeypatch):
    """yaml 缺省回归：META_WRITER 不设＝yaml 模式（db 套件 env 不污染缺省行为）。"""
    monkeypatch.delenv("META_WRITER", raising=False)
    assert mw.writer_mode() == "yaml"
    fixed = datetime.datetime(2026, 10, 20, 1, 0, 0)
    rec = {"appkey": "sb_writeryamlreg01", "secret_hash": "sha256:" + "a" * 64,
           "tenant": "yamlreg01", "caller_name": "yamlreg01", "app_name": "回归应用",
           "key_type": "sandbox", "oa_ticket": "", "tag_sets": [], "quota_qps": 50,
           "quota_daily": 10000, "status": "active", "expires_at": "2099-01-01",
           "issued_at": "2026-10-20T00:00:00+00:00",
           "grants": [{"service": "v1-svc-usr-basic-info", "endpoint": "/x",
                       "fields": ["user_name"]}]}
    import yaml as _y
    body = ("# yaml 回归头注\n" + _y.safe_dump(
        {"meta": {"updated": "2026-10-20T00:00:00+00:00", "updated_by": "console"},
         "keys": [rec]}, sort_keys=False, allow_unicode=True, default_flow_style=False))
    pa, pb = tmp_path / "a.yaml", tmp_path / "b.yaml"
    pa.write_text(body, encoding="utf-8")
    pb.write_text(body, encoding="utf-8")
    da, db_ = appk.load_appkeys(pa), appk.load_appkeys(pb)
    appk.set_status(da, "sb_writeryamlreg01", "disable", now=fixed)
    appk.save_appkeys(pa, da)
    mw.credential_set_status(db_, "sb_writeryamlreg01", "disable", now=fixed)
    mw.credential_save(pb, db_)
    assert pa.read_bytes() == pb.read_bytes(), "yaml 模式回归：抽层后字节级不等价"
