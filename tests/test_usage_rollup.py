# -*- coding: utf-8 -*-
"""usage_rollup 落账管道测试（ADR-0023 实施卡）——隔离一次性 pg，禁碰 oneserve-* 现存容器。

起库模式对齐 tests/test_admin_api_db_e2e.py：docker run --rm postgres:17-alpine 一次性容器
（POSTGRES_USER=meta_migrate），按序灌 migrations/*.sql 全链（0001→0004 glob 全量），通道经
USAGE_PSQL_BASE 钩子注入；docker 不可达/镜像未就位＝pytest.skip 降级（hermetic）。

覆盖（任务卡验收线）：
  ① 0003 幂等可重放（二次 apply exit 0＋60 月分区齐）；
  ② 重放幂等（双跑同结果——行集逐列一致）；
  ③ T 窗整行覆盖（不增量合并：手插脏行被覆盖清除；分片变更后旧键不残留）；
  ④ 晚到事件窗内收敛（追加晚到行→同日重算收敛）＋window_days T+1~T+3 单测；
  ⑤ 四键分租户两行（同日同 caller 同 endpoint 转租户物理分两行）；
  ⑥ 沙箱与未登记主体归总量面（计费面 SQL 判定不含 sandbox/空串）；
  ⑦ JSONL 坏行容错（坏 JSON/非对象/脏 ts/空 caller/跨日行跳过，锚点只计有效行）；
  ⑧ 分区滚动预建（远期日自动补建月分区——DDL 只预建到 2027-12）；
  ⑨ 冻结账期跳过（usage.billing_period〔0004 真表〕confirmed 及核销后继态 written_off
     账期该月日跳过写且原数据原样保留；无冻继行＝未冻结照常覆盖写）。
测试日期全用固定历史日（2023-01-15 等）与远期日（2030-05-04）——--day 指定重放，
与真实时钟零耦合，无日期炸弹。
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

CONTAINER = "oneserve-usage-rollup-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
DAY = "2023-01-15"                   # 历史分区（DDL 预建覆盖内）
DAY2 = "2023-01-16"
FAR = "2030-05-04"                   # 超出 DDL 预建范围 → 验证滚动预建
STATE = {}


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _psql(sql: str, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "psql", "-U", "meta_migrate",
         "-d", "oneserve_meta", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"],
        input=(input_text if input_text is not None else sql),
        capture_output=True, text=True, timeout=120)


def _rows(day: str) -> list:
    """按四键序取整日行（不含 updated_at——重放幂等比较口径为业务列）。"""
    p = _psql("SELECT tenant_id, caller, endpoint, calls, success, fail, lat_sum, lat_cnt,"
              " source_event_count, source FROM usage.usage_daily WHERE day = '%s'"
              " ORDER BY tenant_id, caller, endpoint;" % day)
    assert p.returncode == 0, p.stderr
    return [ln for ln in p.stdout.strip().splitlines() if ln]


def _cli(script: str, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)


def _rollup(*args: str) -> subprocess.CompletedProcess:
    return _cli("usage_rollup.py", "--shard-dir", str(STATE["shard"]), *args,
                env_extra={"USAGE_PSQL_BASE": STATE["base"]})


def _ev(ts: str, caller: str, endpoint: str = "service:/v1/query/demo",
        status: int = 200, lat: int = 10, tenant: str | None = None) -> dict:
    row = {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "x",
           "status": status, "latency_ms": lat, "response_digest": "y", "trace_id": "t", "seq": 0}
    if tenant is not None:
        row["tenant_id"] = tenant
    return row


def _write_shard(day: str, rows: list) -> None:
    """写单日分片（审计追加序＝时间升序，对齐 audit_consumer 口径）。"""
    p = STATE["shard"] / ur.SHARD_FMT.format(day)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _append_shard(day: str, rows: list) -> None:
    p = STATE["shard"] / ur.SHARD_FMT.format(day)
    with p.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


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
    """一次性 pg ＋迁移全链（0001→0002→0003）＋USAGE_PSQL_BASE 注入（module 级）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=usage-rollup-test", PG_IMAGE],
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
                      / ("usage-rollup-" + uuid.uuid4().hex[:8])})
        STATE["shard"].mkdir(parents=True, exist_ok=True)
        yield STATE
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


# ── ① DDL 幂等可重放 ──

def test_ddl_replayable_and_partitions(stack):
    """0003 二次 apply exit 0（幂等可重放）；usage_daily＋60 月分区齐（2023-01~2027-12）。"""
    r = _psql((REPO / "migrations" / "0003_usage_init.sql").read_text(encoding="utf-8"))
    assert r.returncode == 0, r.stderr[-800:]
    p = _psql("SELECT count(*) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
              " JOIN pg_namespace n ON n.oid = c.relnamespace"
              " WHERE n.nspname='usage' AND (SELECT relname FROM pg_class WHERE oid = i.inhparent)"
              " = 'usage_daily';")
    assert p.stdout.strip() == "60", "分区应为 60 个月（2023-01~2027-12），实得 " + p.stdout
    p = _psql("SELECT has_table_privilege('usage_app', 'usage.usage_daily', 'INSERT'),"
              " has_table_privilege('usage_ro', 'usage.usage_daily', 'SELECT'),"
              " has_table_privilege('usage_ro', 'usage.usage_daily', 'INSERT');")
    assert p.stdout.strip().startswith("t|t|f"), "分域授权：usage_app 读写／usage_ro 只读，实得 " + p.stdout


# ── ② 重放幂等 ──

def test_replay_idempotent_double_run_same_result(stack):
    """同源重放幂等：--day 双跑，业务列行集逐字节一致（ADR 第 2 条）。"""
    _write_shard(DAY, [
        _ev("2023-01-15T02:00:00+00:00", "sb_k1", tenant="tenant-a"),
        _ev("2023-01-15T03:00:00+00:00", "sb_k1", status=500, lat=20, tenant="tenant-a"),
        _ev("2023-01-15T04:00:00+00:00", "demo-page", status=202, lat=5),
    ])
    r1 = _rollup("--day", DAY)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    rows1 = _rows(DAY)
    r2 = _rollup("--day", DAY)
    assert r2.returncode == 0, r2.stdout + r2.stderr
    rows2 = _rows(DAY)
    assert rows1 == rows2 and len(rows1) == 2
    assert "rollup" in rows1[0]


# ── ③ T 窗整行覆盖（不增量合并）──

def test_full_row_overwrite_clears_stale_keys(stack):
    """整行覆盖：手插脏行（非本日重放产物）被覆盖清除；分片变更后旧键不残留增量。"""
    _write_shard(DAY2, [_ev("2023-01-16T02:00:00+00:00", "sb_k2", "service:/v1/query/a")])
    assert _rollup("--day", DAY2).returncode == 0
    # 手插两条假行：既有日内的陌生键、也有已存在键的错误放大值
    r = _psql("INSERT INTO usage.usage_daily (day, tenant_id, caller, endpoint, calls,"
              " source_event_count) VALUES ('%s','ghost-t','sb_ghost','e',99,99)," % DAY2
              + "('%s','tenant-x','sb_k2','service:/v1/query/a',77,77);" % DAY2)
    assert r.returncode == 0, r.stderr
    # 分片追加同键第二端点事件 → 重算
    _append_shard(DAY2, [_ev("2023-01-16T03:00:00+00:00", "sb_k2", "service:/v1/query/b")])
    assert _rollup("--day", DAY2).returncode == 0
    rows = _rows(DAY2)
    assert len(rows) == 2, "覆盖后应只剩分片重放真实两行：\n" + "\n".join(rows)
    assert all("ghost" not in ln and "|77|" not in ln for ln in rows)


# ── ④ 晚到事件窗内收敛 ──

def test_late_event_converges_and_window_days(stack):
    """分片追加晚到事件→同日重算收敛（晚到量入账）；window_days＝T+1~T+3。"""
    _write_shard(DAY, [
        _ev("2023-01-15T05:00:00+00:00", "sb_k3", tenant="tenant-c"),
    ])
    assert _rollup("--day", DAY).returncode == 0
    before = [ln for ln in _rows(DAY) if "sb_k3" in ln]
    _append_shard(DAY, [_ev("2023-01-15T06:00:00+00:00", "sb_k3", lat=30, tenant="tenant-c")])
    assert _rollup("--day", DAY).returncode == 0
    after = [ln for ln in _rows(DAY) if "sb_k3" in ln]
    assert after != before and "|2|" in after[0], "晚到事件窗内收敛：calls 1→2"
    assert "|40|2|" in after[0], "lat_sum 10+30=40、lat_cnt=2：" + after[0]
    # window_days 单测：固定 now 的 T+1~T+3（东八区）
    now = _dt.datetime(2026, 10, 21, 4, 0, tzinfo=_dt.timezone.utc)   # CN 10-21 12:00
    assert ur.window_days(3, now) == ["2026-10-20", "2026-10-19", "2026-10-18"]
    assert ur.window_days(1, now) == ["2026-10-20"]


# ── ⑤ 四键分租户两行 ──

def test_four_key_tenant_split_two_rows(stack):
    """同日同 caller 同 endpoint 转租户：事件直取 tenant_id，物理分两行（ADR 第 3 条）。"""
    d = "2023-02-10"
    _write_shard(d, [
        _ev("2023-02-10T01:00:00+00:00", "sb_k4", "service:/v1/query/x", tenant="tenant-a"),
        _ev("2023-02-10T02:00:00+00:00", "sb_k4", "service:/v1/query/x", tenant="tenant-b"),
        _ev("2023-02-10T03:00:00+00:00", "sb_k4", "service:/v1/query/x", tenant="tenant-a"),
    ])
    assert _rollup("--day", d).returncode == 0
    rows = [ln for ln in _rows(d) if "sb_k4" in ln]
    assert len(rows) == 2, "四键唯一：转租户分属两行，实得\n" + "\n".join(rows)
    a_row = next(ln for ln in rows if ln.startswith("tenant-a|"))
    b_row = next(ln for ln in rows if ln.startswith("tenant-b|"))
    assert a_row.split("|")[3] == "2" and b_row.split("|")[3] == "1"
    assert "|3|" in a_row, "锚点＝该日重放事件条数（整日 3 条，非单行事件数）"


# ── ⑥ 沙箱与未登记主体归总量面 ──

def test_sandbox_and_unregistered_in_total_plane(stack):
    """沙箱租户（tenant_type=sandbox）与未登记主体（tenant_id 空串）：行照落，
    计费面判定（reconcile db_days 同款 SQL）两者均不计入——不进计费面。"""
    import usage_reconcile as rc
    d = "2023-03-10"
    _write_shard(d, [
        _ev("2023-03-10T01:00:00+00:00", "sb_k5", tenant="sandbox-x"),
        _ev("2023-03-10T02:00:00+00:00", "sb_k6"),                       # 未登记：无 tenant_id
        _ev("2023-03-10T03:00:00+00:00", "sb_k7", tenant="tenant-real"),
    ])
    r = _psql("INSERT INTO meta.tenant (tenant_id, display_name, tenant_type, oa_ticket_open)"
              " VALUES ('sandbox-x','沙箱演示','sandbox',''),"
              " ('tenant-real','正式租户','external','OA-2023-001');")
    assert r.returncode == 0, r.stderr
    assert _rollup("--day", d).returncode == 0
    rows = _rows(d)
    assert len(rows) == 3, "沙箱/未登记/正式三主体各一行：\n" + "\n".join(rows)
    psql = ur.Psql()
    db = rc.db_days(psql, [d])
    assert db[d]["billed_calls"] == 1, "计费面只含正式租户 1 次：\n" + str(db[d])
    assert db[d]["total_calls"] == 3 and db[d]["events_min"] == db[d]["events_max"] == 3


# ── ⑦ JSONL 坏行容错 ──

def test_bad_lines_tolerated(stack):
    """坏 JSON/非对象行/脏 ts/空 caller/跨日行：跳过不炸，锚点只计有效行。"""
    d = "2023-04-10"
    shard = STATE["shard"] / ur.SHARD_FMT.format(d)
    good = json.dumps(_ev("2023-04-10T01:00:00+00:00", "sb_k8"))
    shard.write_text("\n".join([
        "{坏行不是json",
        json.dumps(["array", "not", "dict"]),
        json.dumps({"ts": "garbage", "caller": "sb_k8"}),
        json.dumps({"ts": "2023-04-10T02:00:00+00:00", "caller": ""}),
        json.dumps(_ev("2023-04-11T01:00:00+00:00", "sb_k8")),   # ts 归日≠分片日：异常行不入账
        good,
    ]) + "\n", encoding="utf-8")
    assert _rollup("--day", d).returncode == 0
    rows = _rows(d)
    assert len(rows) == 1 and "|1|" in rows[0], "只入账唯一有效行：\n" + "\n".join(rows)
    cells = rows[0].split("|")
    assert cells[-1] == "rollup" and cells[-2] == "1", "source_event_count=1（锚点只计有效行）：" + rows[0]


# ── ⑧ 分区滚动预建 ──

def test_partition_auto_rollout_beyond_ddl(stack):
    """远期日（超出 DDL 预建 2027-12）：ensure_partition 自动补建月分区后落账成功。"""
    d = FAR
    _write_shard(d, [_ev(d + "T01:00:00+00:00", "sb_k9", tenant="tenant-far")])
    assert _rollup("--day", d).returncode == 0, "滚动预建失败则插入直接报错"
    p = _psql("SELECT to_regclass('usage.usage_daily_y2030m05') IS NOT NULL;")
    assert p.stdout.strip() == "t"
    assert len(_rows(d)) == 1


# ── ⑨ 冻结账期跳过（usage.billing_period 真表接管）──

def test_frozen_period_skip(stack):
    """usage.billing_period（0004 真表）confirmed 或核销后继态 written_off 含该月→该日
    跳过写且原数据原样保留（ADR-0024 口径：修正走调整单，核销后底数同防回填覆盖）；
    冻继行清除＝未冻结，同日重算照常覆盖。"""
    d = "2023-05-10"
    _write_shard(d, [_ev(d + "T01:00:00+00:00", "sb_k10", tenant="tenant-fr")])
    assert _rollup("--day", d).returncode == 0
    before = _rows(d)
    r = _psql("INSERT INTO usage.billing_period (period_start, status, oa_ticket)"
              " VALUES ('2023-05-01','confirmed','OA-ROLLUP-FROZEN-TEST');")
    assert r.returncode == 0, r.stderr
    try:
        _append_shard(d, [_ev(d + "T02:00:00+00:00", "sb_k10", tenant="tenant-fr")])
        out = _rollup("--day", d)
        assert out.returncode == 0
        assert "frozen" in out.stdout, "应打印冻结跳过日志：\n" + out.stdout
        assert _rows(d) == before, "冻结账期跳过写入：原行原样保留"
    finally:
        _psql("DELETE FROM usage.billing_period WHERE period_start = '2023-05-01';")
    # 未冻结照常：confirmed 行清除后同日重算恢复覆盖写（追加的晚到事件照常入账）
    assert _rollup("--day", d).returncode == 0
    after = _rows(d)
    assert len(after) == len(before), "行数应不变（同键覆盖，非增量加行）：\n" + "\n".join(after)
    assert after != before, "解除冻结后照常覆盖写（calls/source_event_count 随晚到事件更新）"

    # 核销态同冻结（ADR-0024 状态机 confirmed→written_off 后继态：核销后底数同防回填覆盖）。
    # 造数直插 written_off 即最小合法：0004 库层只锁值域（status CHECK 含 written_off）＋
    # confirmed 必带 OA（billing_period_confirm_oa_check），迁移路径锁在 billing_run 应用层，
    # 无 CHECK 拦直插；written_off 承接 confirmed 故带 oa_ticket。
    r = _psql("INSERT INTO usage.billing_period (period_start, status, oa_ticket)"
              " VALUES ('2023-05-01','written_off','OA-ROLLUP-FROZEN-TEST');")
    assert r.returncode == 0, r.stderr
    try:
        _append_shard(d, [_ev(d + "T03:00:00+00:00", "sb_k10", tenant="tenant-fr")])
        out = _rollup("--day", d)
        assert out.returncode == 0
        assert "frozen" in out.stdout, "written_off 账期应打印冻结跳过日志：\n" + out.stdout
        assert _rows(d) == after, "written_off 账期日同样跳过写入：原行原样保留"
    finally:
        _psql("DELETE FROM usage.billing_period WHERE period_start = '2023-05-01';")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")
