# -*- coding: utf-8 -*-
"""billing_run 月账出账测试（ADR-0024 实施卡·空价目试运行）——隔离一次性 pg，禁碰 oneserve-* 现存容器。

起库模式对齐 tests/test_usage_rollup.py：docker run --rm postgres:17-alpine 一次性容器
（POSTGRES_USER=meta_migrate），按序灌 migrations/*.sql 全链（0001→0002→0003→0004），通道经
USAGE_PSQL_BASE 钩子注入；docker 不可达/镜像未就位＝pytest.skip 降级（hermetic）。

覆盖（任务卡验收线）：
  ① 0004 幂等可重放（二次 apply exit 0）＋四表在位＋分域授权（usage_app 读写/usage_ro 只读）
    ＋meta.billing_period 不建（0003 冻结闸接管为本卡遗留——tests/test_usage_rollup.py ⑨夹具占用该名）；
  ② 三前置全过出 0 元账（账单行/状态机 reconciled/三前置留痕/自检对账平）＋沙箱与未归属不进账
    （总量面单列报告行）＋用量/单价双快照列实证；
  ③ 前置缺一各自拒出账：①T+3（当月账期恒未收口）／②计费面对账清零（篡改计费面 calls 实证
    usage_reconcile 同源判定拦门）／③rollup 覆盖 lag（未落账分片日差集拦门）；
  ④ 四键防重（同账期重跑拒绝＋PK 物理防重＋--rebill 幂等重出同行集）；
  ⑤ 确认冻结后不可改（--rebill/--re-confirm 拒绝）＋OA 单号登记留痕（确认登记＋补录）；
  ⑥ 欠费只算不限（--mark-overdue 仅置标记：未确认拒标、确认后可标、无任何限流动作）；
  ⑦ 价目命中计价引擎实证（per_call/阶梯区间行；未定价行保持 0 元不计应收）；
  ⑧ P0-6 并发负例：--rebill 预检后事务前窗口内被确认抢先 → 事务内状态守卫拦截
    （一胜一拒：确认胜、rebill 拒 exit 2；confirmed 不被改回 reconciled，账单行原样）。
测试日期全用固定历史日（2023-01~2023-08）与「当前自然月」（T+3 恒未收口，判定与真实时钟
零耦合——当月账期按定义必违反 T+3，无日期炸弹）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "service"))

import usage_rollup as ur            # noqa: E402
import billing_run as br             # noqa: E402 —— P0-6 并发负例（进程内窗口钩子）
from app import billing_state as bs  # noqa: E402 —— 状态机共享模块（CLI/API 同源）

CONTAINER = "oneserve-billing-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
TZ_CN = _dt.timezone(_dt.timedelta(hours=8))
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


def _cli(script: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["USAGE_PSQL_BASE"] = STATE["base"]
    env.pop("META_MIGRATE_PSQL_BASE", None)
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), "--shard-dir",
                           str(STATE["shard"]), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=300)


def _billing(*args: str) -> subprocess.CompletedProcess:
    return _cli("billing_run.py", *args)


def _rollup(*args: str) -> subprocess.CompletedProcess:
    return _cli("usage_rollup.py", *args)


def _ev(ts: str, caller: str, endpoint: str = "service:/v1/query/a",
        status: int = 200, lat: int = 10, tenant: str | None = None) -> dict:
    row = {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "x",
           "status": status, "latency_ms": lat, "response_digest": "y", "trace_id": "t", "seq": 0}
    if tenant is not None:
        row["tenant_id"] = tenant
    return row


def _write_shard(day: str, rows: list) -> None:
    p = STATE["shard"] / ur.SHARD_FMT.format(day)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _append_shard(day: str, rows: list) -> None:
    p = STATE["shard"] / ur.SHARD_FMT.format(day)
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _ensure_tenants() -> None:
    r = _psql("INSERT INTO meta.tenant (tenant_id, display_name, tenant_type, oa_ticket_open)"
              " VALUES ('tenant-ok','正式一','external','OA-T1'),"
              " ('tenant-sb','沙箱演示','sandbox',''),"
              " ('tenant-price','计价租户','external','OA-TP')"
              " ON CONFLICT (tenant_id) DO NOTHING;")
    assert r.returncode == 0, r.stderr


def _seed_and_bill(ym: str, days: dict, *extra_billing_args: str) -> subprocess.CompletedProcess:
    """造分片→rollup→出账（各测试独立账期，互不污染）。"""
    _ensure_tenants()
    for day, evs in sorted(days.items()):
        _write_shard(day, evs)
        assert _rollup("--day", day).returncode == 0, day
    return _billing("--period", ym, *extra_billing_args)


def _period_row(ym: str) -> str:
    r = _psql("SELECT status, gate_t3_closed, gate_reconcile_clean, gate_lag_zero,"
              " amount_due, overdue FROM usage.billing_period"
              " WHERE period_start = date '%s-01';" % ym)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _bill_lines(ym: str) -> list:
    r = _psql("SELECT tenant_id, caller, endpoint, calls, success, fail, lat_sum, lat_cnt,"
              " source_event_count, priced, coalesce(price_item_id,'-'),"
              " coalesce(unit_price::text,'-'), amount::text"
              " FROM usage.bill_line WHERE period_start = date '%s-01'"
              " ORDER BY tenant_id, caller, endpoint;" % ym)
    assert r.returncode == 0, r.stderr
    return [ln for ln in r.stdout.strip().splitlines() if ln]


def _current_ym() -> str:
    return _dt.datetime.now(TZ_CN).strftime("%Y-%m")


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
def stack():
    """一次性 pg ＋迁移全链（0001→0004）＋USAGE_PSQL_BASE 注入（module 级）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=billing-run-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    import time
    saved = {k: os.environ.get(k) for k in ("USAGE_PSQL_BASE", "META_MIGRATE_PSQL_BASE")}
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
        os.environ["USAGE_PSQL_BASE"] = base
        os.environ.pop("META_MIGRATE_PSQL_BASE", None)
        r = _psql(BOOT_SQL)
        assert r.returncode == 0, r.stderr
        for mig in sorted((REPO / "migrations").glob("*.sql")):
            r = _psql(mig.read_text(encoding="utf-8"))
            assert r.returncode == 0, mig.name + ": " + r.stderr[-800:]
        STATE.update({"base": base, "shard": Path(os.environ.get("TMPDIR", "/tmp"))
                      / ("billing-run-" + uuid.uuid4().hex[:8])})
        STATE["shard"].mkdir(parents=True, exist_ok=True)
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


# ── ① DDL 幂等可重放＋四表＋分域授权＋meta.billing_period 不建 ──

def test_ddl_replayable_tables_grants(stack):
    r = _psql((REPO / "migrations" / "0004_billing_init.sql").read_text(encoding="utf-8"))
    assert r.returncode == 0, r.stderr[-800:]
    p = _psql("SELECT to_regclass('usage.price_item') IS NOT NULL,"
              " to_regclass('usage.price_tier') IS NOT NULL,"
              " to_regclass('usage.billing_period') IS NOT NULL,"
              " to_regclass('usage.bill_line') IS NOT NULL,"
              " to_regclass('meta.billing_period') IS NULL;")
    assert p.stdout.strip() == "t|t|t|t|t", "四表在位且 meta.billing_period 不建（冻结闸遗留），实得 " + p.stdout
    p = _psql("SELECT has_table_privilege('usage_app','usage.bill_line','INSERT'),"
              " has_table_privilege('usage_app','usage.billing_period','UPDATE'),"
              " has_table_privilege('usage_ro','usage.bill_line','SELECT'),"
              " has_table_privilege('usage_ro','usage.bill_line','INSERT');")
    assert p.stdout.strip() == "t|t|t|f", "分域授权：usage_app 读写／usage_ro 只读，实得 " + p.stdout
    p = _psql("INSERT INTO usage.billing_period (period_start, status) VALUES ('2023-09-01','bogus');")
    assert p.returncode != 0, "状态机值域应拦非法状态"
    assert "billing_period_status_check" in p.stderr


# ── ② 三前置全过出 0 元账＋沙箱/未归属不进账＋双快照 ──

def test_zero_yuan_full_chain_and_total_plane(stack):
    out = _seed_and_bill("2023-01", {
        "2023-01-10": [
            _ev("2023-01-10T01:00:00+00:00", "sb_k1", tenant="tenant-ok"),
            _ev("2023-01-10T02:00:00+00:00", "sb_k1", status=500, tenant="tenant-ok"),
            _ev("2023-01-10T03:00:00+00:00", "sb_ks", tenant="tenant-sb"),
            _ev("2023-01-10T04:00:00+00:00", "sb_ks", tenant="tenant-sb"),
            _ev("2023-01-10T05:00:00+00:00", "sb_ku"),                      # 未登记主体
        ],
        "2023-01-11": [
            _ev("2023-01-11T01:00:00+00:00", "sb_k1", lat=30, tenant="tenant-ok"),
        ],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    assert "出账完成" in out.stdout and "gate-1] T+3 账期关闭：PASS" in out.stdout
    assert "gate-2] 计费面三方对账清零：PASS" in out.stdout
    assert "gate-3] rollup 覆盖 lag 归零：PASS" in out.stdout
    assert "沙箱租户 2 次" in out.stdout and "未归属主体 1 次" in out.stdout
    assert "空价目试运行" in out.stdout and "不计应收" in out.stdout
    assert _period_row("2023-01") == "reconciled|t|t|t|0.00|f", _period_row("2023-01")
    lines = _bill_lines("2023-01")
    assert len(lines) == 1, "沙箱/未归属不入账，只出正式租户一行：\n" + "\n".join(lines)
    # 用量快照逐列（月聚合：两日合并 calls4/success3/fail1/lat40/latcnt3/锚3）＋成交单价快照（空价目）
    assert lines[0] == ("tenant-ok|sb_k1|service:/v1/query/a|3|2|1|50|3|6|f|-|-|0.00"), lines[0]


# ── ③a 前置① T+3 账期关闭（当月账期恒未收口，与真实时钟零耦合）──

def test_gate_t3_blocks_current_month(stack):
    ym = _current_ym()
    out = _billing("--period", ym)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "gate-1] T+3 账期关闭：FAIL" in out.stdout
    assert "拒绝出账" in out.stdout
    assert "gate-2] 计费面三方对账清零：PASS" in out.stdout, "缺 shard 缺账日不构成②红（只有①拦）"
    y, m = int(ym[:4]), int(ym[5:7])
    p = _psql("SELECT count(*) FROM usage.billing_period WHERE period_start = date '%04d-%02d-01';" % (y, m))
    assert p.stdout.strip() == "0", "拒出账不得落任何账期行"


# ── ③b 前置② 计费面对账清零（usage_reconcile 同源判定拦门）──

def test_gate_reconcile_blocks_on_billed_face_break(stack):
    out = _seed_and_bill("2023-02", {
        "2023-02-10": [_ev("2023-02-10T01:00:00+00:00", "sb_k2", tenant="tenant-ok"),
                       _ev("2023-02-10T02:00:00+00:00", "sb_k2", tenant="tenant-ok")],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    before = _period_row("2023-02")
    # 篡改计费面 calls（对账 billed 面）→ usage_reconcile 判计费面破
    r = _psql("UPDATE usage.usage_daily SET calls = calls + 1 WHERE day = '2023-02-10'"
              " AND tenant_id = 'tenant-ok';")
    assert r.returncode == 0, r.stderr
    out = _billing("--period", "2023-02", "--rebill")
    assert out.returncode == 2, out.stdout + out.stderr
    assert "gate-2] 计费面三方对账清零：FAIL" in out.stdout
    assert "计费面破" in out.stdout, "人话报因应指认计费面偏差：\n" + out.stdout
    assert "gate-1] T+3 账期关闭：PASS" in out.stdout and "gate-3] rollup 覆盖 lag 归零：PASS" in out.stdout
    assert _period_row("2023-02") == before, "拒出账不得动已对账数据"


# ── ③c 前置③ rollup 覆盖 lag（未落账分片日差集拦门）──

def test_gate_rollup_lag_blocks_on_uncovered_day(stack):
    out = _seed_and_bill("2023-03", {
        "2023-03-10": [_ev("2023-03-10T01:00:00+00:00", "sb_k3", tenant="tenant-ok")],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    _append_shard("2023-03-20", [_ev("2023-03-20T01:00:00+00:00", "sb_k3", tenant="tenant-ok")])
    out = _billing("--period", "2023-03", "--rebill")
    assert out.returncode == 2, out.stdout + out.stderr
    assert "gate-3] rollup 覆盖 lag 归零：FAIL" in out.stdout
    assert "2023-03-20" in out.stdout and "未落账" in out.stdout, out.stdout


# ── ④ 四键防重（重跑拒绝＋PK 物理防重＋--rebill 幂等同行集）──

def test_four_key_dedup_and_rebill_idempotent(stack):
    out = _billing("--period", "2023-01")
    assert out.returncode == 2 and "四键防重" in out.stdout, out.stdout + out.stderr
    r = _psql("INSERT INTO usage.bill_line (period_start, tenant_id, caller, endpoint)"
              " VALUES ('2023-01-01','tenant-ok','sb_k1','service:/v1/query/a');")
    assert r.returncode != 0 and "bill_line_pk" in r.stderr, "四键 PK 物理防重（schema 约束）"
    rows_before = _bill_lines("2023-01")
    out = _billing("--period", "2023-01", "--rebill")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "重出" in out.stdout
    assert _bill_lines("2023-01") == rows_before, "--rebill 幂等：同源重放同行集"


# ── ⑤ 确认冻结后不可改＋OA 单号登记留痕 ──

def test_confirm_freeze_and_oa_registration(stack):
    out = _seed_and_bill("2023-05", {
        "2023-05-10": [_ev("2023-05-10T01:00:00+00:00", "sb_k5", "service:/v1/query/c",
                           tenant="tenant-ok"),
                       _ev("2023-05-10T02:00:00+00:00", "sb_k5", "service:/v1/query/c",
                           tenant="tenant-ok")],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    out = _billing("--period", "2023-05", "--confirm")
    assert out.returncode == 2 and "--oa-ticket" in out.stdout, "确认必带 OA 单号"
    out = _billing("--period", "2023-05", "--confirm", "--oa-ticket", "OA-2023-05-001")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "确认冻结" in out.stdout and "OA-2023-05-001" in out.stdout and "留痕" in out.stdout
    assert _period_row("2023-05").startswith("confirmed|"), _period_row("2023-05")
    r = _psql("SELECT oa_ticket, confirmed_at IS NOT NULL FROM usage.billing_period"
              " WHERE period_start = '2023-05-01';")
    assert r.stdout.strip() == "OA-2023-05-001|t", r.stdout
    out = _billing("--period", "2023-05", "--rebill")
    assert out.returncode == 2 and "确认冻结" in out.stdout, "确认后 --rebill 永久拒绝"
    out = _billing("--period", "2023-05", "--confirm", "--oa-ticket", "OA-X")
    assert out.returncode == 2 and "状态不符" in out.stdout, "状态机不跳步：重复确认拒绝"
    out = _billing("--period", "2023-05", "--register-oa", "--oa-ticket", "OA-2023-05-001-R2")
    assert out.returncode == 0 and "补录留痕" in out.stdout, out.stdout + out.stderr
    r = _psql("SELECT oa_ticket FROM usage.billing_period WHERE period_start = '2023-05-01';")
    assert r.stdout.strip() == "OA-2023-05-001-R2", "OA 补录留痕生效"


# ── ⑥ 欠费只算不限（仅标记，未确认拒标）──

def test_mark_overdue_only_marks(stack):
    out = _seed_and_bill("2023-07", {
        "2023-07-10": [_ev("2023-07-10T01:00:00+00:00", "sb_k7", "service:/v1/query/g",
                           tenant="tenant-ok")],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    out = _billing("--period", "2023-07", "--mark-overdue")
    assert out.returncode == 2 and "未确认" in out.stdout, "欠费以确认后应收为前提"
    out = _billing("--period", "2023-05", "--mark-overdue")   # ⑤ 已确认账期
    assert out.returncode == 0 and "只算不限" in out.stdout, out.stdout + out.stderr
    assert "不做限流" in out.stdout
    r = _psql("SELECT overdue, overdue_marked_at IS NOT NULL FROM usage.billing_period"
              " WHERE period_start = '2023-05-01';")
    assert r.stdout.strip() == "t|t", r.stdout


# ── ⑦ 价目命中计价引擎（per_call／阶梯区间行；未定价行保持 0 元不计应收）──

def test_priced_tiers_engine(stack):
    r = _psql(
        "INSERT INTO usage.price_item (item_id, service_ref, model, effective_from)"
        " VALUES ('prc-per','service:/v1/query/p1','per_call', date '2023-01-01'),"
        " ('prc-tier','service:/v1/query/p2','tiered', date '2023-01-01');"
        "INSERT INTO usage.price_tier (item_id, lower_bound, upper_bound, unit_price) VALUES"
        " ('prc-per', 0, NULL, 0.010000),"
        " ('prc-tier', 0, 10, 0), ('prc-tier', 10, NULL, 0.500000);")
    assert r.returncode == 0, r.stderr
    out = _seed_and_bill("2023-08", {
        "2023-08-10": [
            _ev("2023-08-10T01:00:00+00:00", "sb_kp1", "service:/v1/query/p1", tenant="tenant-price"),
            _ev("2023-08-10T02:00:00+00:00", "sb_kp1", "service:/v1/query/p1", status=500,
                tenant="tenant-price"),
            _ev("2023-08-10T03:00:00+00:00", "sb_kp1", "service:/v1/query/p1", tenant="tenant-price"),
            _ev("2023-08-10T04:00:00+00:00", "sb_kp2", "service:/v1/query/p2", tenant="tenant-price"),
        ] + [_ev("2023-08-10T%02d:00:00+00:00" % h, "sb_kp2", "service:/v1/query/p2",
                 tenant="tenant-price") for h in range(5, 16)],   # p2 success 12（UTC 时≤15 防跨日拒入）
        "2023-08-11": [
            _ev("2023-08-11T01:00:00+00:00", "sb_kp3", "service:/v1/query/p3", tenant="tenant-price"),
        ],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    assert "已定价行 2" in out.stdout and "未定价行 1" in out.stdout, out.stdout
    lines = {ln.split("|")[2]: ln for ln in _bill_lines("2023-08")}
    # p1：per_call 计成功量 2 次 ×0.01＝0.02（失败 1 次不入价）；锚点＝日全量事件合计 15
    assert lines["service:/v1/query/p1"] == (
        "tenant-price|sb_kp1|service:/v1/query/p1|3|2|1|30|3|15|t|prc-per|0.010000|0.02"), lines["service:/v1/query/p1"]
    # p2：阶梯 [0,10)@0＋[10,∞)@0.5，success 12 → 10×0＋2×0.5＝1.00；加权均价 1.00/12
    l2 = lines["service:/v1/query/p2"].split("|")
    assert l2[9:13] == ["t", "prc-tier", "0.083333", "1.00"], lines["service:/v1/query/p2"]
    # p3：未命中价目 → 空价目 0 元不计应收
    l3 = lines["service:/v1/query/p3"].split("|")
    assert l3[9:13] == ["f", "-", "-", "0.00"], lines["service:/v1/query/p3"]
    # 账期应收合计＝1.02
    r = _psql("SELECT amount_due FROM usage.billing_period WHERE period_start = '2023-08-01';")
    assert r.stdout.strip() == "1.02", r.stdout

# ── ⑧ P0-6 并发负例：确认×--rebill 竞态（事务内状态守卫核 rowcount）──

def test_rebill_confirmed_race_in_transaction_guard(stack, monkeypatch, capsys):
    """--rebill 预检后、写账事务前的并发窗口内被确认抢先 → 事务内守卫 RAISE 回滚。

    确定性重现 TOCTOU 窗口：钩住 aggregate_billed_face（四键防重预检与三前置核查
    之后、write_bill 事务之前执行），窗口内先经共享模块入口落一笔确认
    （bs.confirm_period＝reconciled→confirmed，同 CLI/API 同句），再放行原聚合——
    旧代码此处 UPDATE 无 status 守卫（billing_run.py:353-358），会把 confirmed
    改回 reconciled 并静默重写账单行；现代码事务内守卫块（P0-6）核行 0 命中即
    拦截：拒绝 exit 2（非环境失败 exit 1），confirmed 底数与账单行原样。"""
    out = _seed_and_bill("2023-06", {
        "2023-06-10": [_ev("2023-06-10T01:00:00+00:00", "sb_k6", "service:/v1/query/r",
                           tenant="tenant-ok")],
    })
    assert out.returncode == 0, out.stdout + out.stderr
    before = _bill_lines("2023-06")
    orig = br.aggregate_billed_face

    def confirm_in_window(psql, lo, hi):
        rows = orig(psql, lo, hi)
        hit = bs.confirm_period(psql, lo.isoformat(), "OA-RACE-2023-06")
        assert hit, "并发窗口内确认应命中（reconciled→confirmed）"
        return rows

    monkeypatch.setattr(br, "aggregate_billed_face", confirm_in_window)
    try:
        rc = br.main(["--period", "2023-06", "--rebill",
                      "--shard-dir", str(STATE["shard"])])
    finally:
        monkeypatch.undo()
    assert rc == br.EXIT_REFUSE, "P0-6：确认后 rebill 须拒绝（exit 2），实得 rc=%s" % rc
    assert "重出状态守卫拦截" in capsys.readouterr().out
    r = _psql("SELECT status, oa_ticket, confirmed_at IS NOT NULL"
              " FROM usage.billing_period WHERE period_start = '2023-06-01';")
    assert r.stdout.strip() == "confirmed|OA-RACE-2023-06|t",         "确认结果不被并发 rebill 回退，实得 " + r.stdout
    assert _bill_lines("2023-06") == before, "守卫回滚：确认后账单行不得被重出改写"
