# -*- coding: utf-8 -*-
"""usage_reconcile 三方对账测试（ADR-0023 决定第 4 条）——隔离一次性 pg＋临时 JSONL 分片。

起库模式对齐 tests/test_admin_api_db_e2e.py（docker run --rm 一次性容器，禁碰
oneserve-* 现存容器；docker 不可达/镜像未就位＝pytest.skip）。Redis 快照侧按 ADR
「只告警不拦门」注入假值（monkeypatch snapshot_days），不起 redis 容器。

覆盖（任务卡验收线）：
  ① rollup 后即对账＝绿（锚点/计费面清零）；
  ② 改 JSONL 模拟偏差 → strict/--check 红 exit 2 → --fix 以 JSONL 重放修平 → 绿
     且修表行 source=backfill（ADR 锚定：以 JSONL 重放为准修表）；
  ③ 分片缺失日＝MISS 登记（留存窗核对口径），表有账才拦门；
  ④ 快照侧偏差只告警（exit 0，不反向修账）；
  ⑤ 沙箱与未登记主体归总量面不进计费面（billed 只数正式租户；全未登记日 billed=0 绿）；
  ⑥ --check 无库环境绿行跳过 exit 0（run_gates 挂载约定，对齐 meta_reconcile 基线态）。
"""
from __future__ import annotations

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
import usage_reconcile as rc         # noqa: E402

CONTAINER = "oneserve-usage-recon-test-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:6]
PG_IMAGE = "postgres:17-alpine"
DAY = "2023-06-10"                   # 固定历史日（DDL 预建覆盖内），与真实时钟零耦合
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


def _cli(script: str, *args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(REPO / "scripts" / script), *args],
                          capture_output=True, text=True, env=env, cwd=str(REPO), timeout=180)


def _recon(*args: str) -> subprocess.CompletedProcess:
    return _cli("usage_reconcile.py", "--shard-dir", str(STATE["shard"]),
                "--redis-url", "redis://127.0.0.1:1/0",   # 立即拒连：快照侧告警口径
                *args, env_extra={"USAGE_PSQL_BASE": STATE["base"]})


def _ev(ts: str, caller: str, endpoint: str = "service:/v1/query/demo",
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


def _source_col(day: str) -> list:
    p = _psql("SELECT source FROM usage.usage_daily WHERE day='%s' ORDER BY caller;" % day)
    assert p.returncode == 0, p.stderr
    return [ln.strip() for ln in p.stdout.strip().splitlines() if ln.strip()]


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
    """一次性 pg ＋迁移全链＋租户种子＋分片目录（module 级）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip("镜像未就位：" + PG_IMAGE + "（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=usage-recon-test", PG_IMAGE],
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
        r = _psql("INSERT INTO meta.tenant (tenant_id, display_name, tenant_type, oa_ticket_open)"
                  " VALUES ('tenant-real','正式租户','external','OA-2023-001'),"
                  " ('sandbox-x','沙箱演示','sandbox','');")
        assert r.returncode == 0, r.stderr
        shard = Path(os.environ.get("TMPDIR", "/tmp")) / ("usage-recon-" + uuid.uuid4().hex[:8])
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


def _seed(stack) -> None:
    """基线分片＋rollup：2 正式租户事件＋1 未登记＋1 沙箱。"""
    _write_shard(DAY, [
        _ev(DAY + "T01:00:00+00:00", "sb_k1", tenant="tenant-real"),
        _ev(DAY + "T02:00:00+00:00", "sb_k2", tenant="tenant-real"),
        _ev(DAY + "T03:00:00+00:00", "demo-page"),            # 未登记主体：总量面
        _ev(DAY + "T04:00:00+00:00", "sb_k3", tenant="sandbox-x"),  # 沙箱：总量面
    ])
    r = _cli("usage_rollup.py", "--day", DAY, "--shard-dir", str(STATE["shard"]),
             env_extra={"USAGE_PSQL_BASE": STATE["base"]})
    assert r.returncode == 0, r.stdout + r.stderr


# ── ① 一致即绿 ──

def test_reconcile_green_after_rollup(stack):
    _seed(stack)
    r = _recon("--day", DAY)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[OK ]" in r.stdout
    assert _source_col(DAY) == ["rollup"] * 4, "四键四行全部 rollup（2 正式＋1 未登记＋1 沙箱）"


# ── ② 偏差检出→--fix 修平（source=backfill）──

def test_drift_detected_fix_repairs_with_backfill_source(stack):
    _seed(stack)
    # 模拟偏差：分片追加晚到事件（真实世界＝消费者 lag/晚到），表已陈旧
    _append_shard(DAY, [_ev(DAY + "T05:00:00+00:00", "sb_k2", tenant="tenant-real")])
    strict = _recon("--day", DAY)
    assert strict.returncode == 2, "偏差日应拦门 exit 2：\n" + strict.stdout
    assert "计费面破" in strict.stdout or "锚点破" in strict.stdout
    # --check（无 --fix）同样红：门禁挂载形态真判定
    assert _recon("--day", DAY, "--check").returncode == 2
    fixed = _recon("--day", DAY, "--fix")
    assert fixed.returncode == 0, "修表后应绿：\n" + fixed.stdout + fixed.stderr
    assert _source_col(DAY) == ["backfill"] * 4, "修表通道 source=backfill（ADR 锚定，四键四行）"
    p = _psql("SELECT sum(calls) FROM usage.usage_daily WHERE day='%s';" % DAY)
    assert p.stdout.strip() == "5", "修后总量=5（基线 4＋晚到 1）"


# ── ③ 分片缺失＝MISS 登记 ──

def test_missing_shard_reported(stack):
    _seed(stack)
    ghost = "2023-06-11"
    r = _psql("INSERT INTO usage.usage_daily (day, tenant_id, caller, endpoint, calls,"
              " source_event_count) VALUES ('%s','tenant-real','sb_x','e',5,5);" % ghost)
    assert r.returncode == 0, r.stderr
    out = _recon("--day", ghost)
    assert out.returncode == 2, "表有账而分片缺＝锚定不了，拦门核对留存窗"
    assert "[MISS]" in out.stdout


# ── ④ 快照侧偏差只告警 ──

def test_snapshot_deviation_warns_not_blocks(stack, monkeypatch):
    _seed(stack)
    monkeypatch.setattr(rc, "snapshot_days", lambda days, url: {d: 999 for d in days})
    bad, lines = rc.reconcile(STATE["shard"], [DAY], ur.Psql(), "unused", quiet_snap_warn=False)
    assert bad == [], "快照偏差不拦门（ADR：快照只做热展示，不反向修账）"
    assert any("snap" in ln and "999" in ln for ln in lines), "偏差应打印告警行：\n" + "\n".join(lines)


def test_sandbox_and_unregistered_not_in_billing_plane(stack):
    _seed(stack)
    psql = ur.Psql()
    ttypes = rc.tenant_type_map(psql)
    jl = rc.jsonl_days(STATE["shard"], [DAY], ttypes)
    assert jl[DAY]["total"] == 4 and jl[DAY]["billed"] == 2, \
        "分片侧计费面只数正式租户（沙箱/未登记归总量面）：" + str(jl[DAY])
    db = rc.db_days(psql, [DAY])
    assert db[DAY]["billed_calls"] == 2 and db[DAY]["total_calls"] == 4, str(db[DAY])
    bad, _ = rc.reconcile(STATE["shard"], [DAY], psql, "unused")
    assert bad == []


# ── ⑥ --check 无库环境绿行跳过 ──

def test_check_mode_skips_without_db(monkeypatch):
    """run_gates 挂载约定：psql 通道不可达＝绿行跳过 exit 0（对齐 meta_reconcile 基线态）。"""
    dead = "docker exec -i oneserve-no-such-container-xyz psql -U x -d x"
    monkeypatch.setenv("USAGE_PSQL_BASE", dead)
    monkeypatch.setenv("META_MIGRATE_PSQL_BASE", "")   # 显式清空回落链
    r = _cli("usage_reconcile.py", "--check", env_extra={"USAGE_PSQL_BASE": dead})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "跳过" in r.stdout
    # 非 check 模式同环境＝环境性失败 exit 1
    r2 = _cli("usage_reconcile.py", "--day", DAY, env_extra={"USAGE_PSQL_BASE": dead})
    assert r2.returncode == 1, r2.stdout + r.stderr


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"{len(fns)} tests passed")
