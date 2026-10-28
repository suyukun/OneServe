#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""meta_migrate 迁移工具套件（卡-1b7 步4 · 隔离一次性 pg 容器，禁碰 dev 容器组）。

隔离模式（项目首个真库测试套件；tests/ 此前无 pg 先例——本套件立先例）：
  ① 一次性 pg：docker run --rm postgres:17-alpine（同 casdoor-db tag，本机已有镜像），
     容器名带 pid 随机防撞；POSTGRES_USER=meta_migrate 即引导超户＝hermetic 简化
     （生产栈三角色隔离由 configs/metadata-db/init/ 守，本套件不测角色——test_meta_roles 归步 7）。
  ② 通道注入：META_MIGRATE_PSQL_BASE env → ComposePsqlRunner 改走 docker exec
     （协议同 compose exec psql；零代码分支，生产缺省仍 compose -p oneserve-meta）。
  ③ 源目录注入：META_MIGRATE_SRC_DIR env → 四 yaml 指向 tmp 副本（脏数据负例不改真文件）。
  ④ docker 不可达＝pytest.skip（CI/无 docker 环境降级跳过，不红）。

四正例＋一负例（任务单口径）：
  apply 全绿（装载＋对账 exit 0）／dry-run 零写入／幂等复跑（行数与台账不增）／
  哈希对账敏感（篡改一字段 verify exit 2）／脏租户拒（exit 2 零写入）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import yaml  # noqa: E402

CONTAINER = f"oneserve-meta-mig-test-{os.getpid()}-{uuid.uuid4().hex[:6]}"
PG_IMAGE = "postgres:17-alpine"
PSQL_BASE = (f"docker exec -i {CONTAINER} psql -U meta_migrate -d oneserve_meta "
             "-v ON_ERROR_STOP=1 -A -t -P footer=off")
SRC_FILES = ["services.yaml", "upstreams.yaml", "auth_matrix.yaml"]


def _docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="session")
def pg_container():
    """一次性 pg 容器（session 级：起一次、全套件共用、终态 rm）。"""
    if not _docker_available():
        pytest.skip("docker 不可达（hermetic 降级跳过）")
    img = subprocess.run(["docker", "images", "-q", PG_IMAGE], capture_output=True, text=True)
    if not img.stdout.strip():
        pytest.skip(f"镜像未就位：{PG_IMAGE}（离线环境不拉取，降级跳过）")
    up = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", CONTAINER,
         "-e", "POSTGRES_USER=meta_migrate", "-e", "POSTGRES_DB=oneserve_meta",
         "-e", "POSTGRES_PASSWORD=meta-migrate-test", PG_IMAGE],
        capture_output=True, text=True)
    assert up.returncode == 0, up.stderr
    # 就绪等待：pg_isready 轮询（上限 ~30s）
    import time
    for _ in range(30):
        probe = subprocess.run(["docker", "exec", CONTAINER, "pg_isready",
                                "-U", "meta_migrate", "-d", "oneserve_meta"],
                               capture_output=True, text=True)
        if probe.returncode == 0:
            break
        time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
        pytest.fail("一次性 pg 30s 未就绪")
    yield CONTAINER
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)  # --rm 兜底


@pytest.fixture()
def meta_env(tmp_path: Path, pg_container: str):
    """tmp 四 yaml 副本＋通道/源目录 env（每测试独立；session 容器内先清残留）。"""
    src = tmp_path / "src"
    (src / "registry").mkdir(parents=True)
    for name in SRC_FILES:
        (src / name).write_text((REPO / "service" / name).read_text(encoding="utf-8"))
    (src / "registry" / "appkeys.yaml").write_text(
        (REPO / "service" / "registry" / "appkeys.yaml").read_text(encoding="utf-8"))
    # 每测试干净库（session 容器共用：清前序测试残留；CASCADE 级联 FK）
    subprocess.run(
        ["docker", "exec", pg_container, "psql", "-U", "meta_migrate", "-d", "oneserve_meta",
         "-c", "TRUNCATE meta.field_space, meta.tenant, meta.service_catalog,"
               " meta.upstream_proxy, meta.credential, meta.\"grant\","
               " meta.admin_event, meta.version_history CASCADE;"],
        capture_output=True)
    env = dict(os.environ)
    env["META_MIGRATE_SRC_DIR"] = str(src)
    env["META_MIGRATE_PSQL_BASE"] = PSQL_BASE
    env["META_WRITER"] = "yaml"
    return {"env": env, "src": src}


def _ddl(container: str) -> None:
    """最小等价 initdb：分域角色＋meta/usage schema（02_schemas.sql 语义），再按序落 migrations/*.sql。

    角色重复建报错不致命（pre 步不挂 ON_ERROR_STOP，逐语句容错）；schema IF NOT EXISTS 幂等；
    迁移链按文件名序全量执行（0001 十表 → 0002 扩展列……新迁移入列即自动跟随）。
    """
    boot = (
        "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='meta_app') THEN"
        " CREATE ROLE meta_app NOLOGIN; END IF; IF NOT EXISTS"
        " (SELECT FROM pg_roles WHERE rolname='meta_ro') THEN"
        " CREATE ROLE meta_ro NOLOGIN; END IF; IF NOT EXISTS"
        " (SELECT FROM pg_roles WHERE rolname='usage_app') THEN"
        " CREATE ROLE usage_app NOLOGIN; END IF; IF NOT EXISTS"
        " (SELECT FROM pg_roles WHERE rolname='usage_ro') THEN"
        " CREATE ROLE usage_ro NOLOGIN; END IF; END $$;"
        "CREATE SCHEMA IF NOT EXISTS meta AUTHORIZATION meta_migrate;"
        "CREATE SCHEMA IF NOT EXISTS usage AUTHORIZATION meta_migrate;")
    subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "meta_migrate",
                    "-d", "oneserve_meta"], input=boot.encode(), capture_output=True)
    for mig in sorted((REPO / "migrations").glob("*.sql")):
        p = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "meta_migrate",
                            "-d", "oneserve_meta", "-v", "ON_ERROR_STOP=1"],
                           input=mig.read_text(encoding="utf-8").encode(), capture_output=True)
        assert p.returncode == 0, mig.name + ": " + p.stderr.decode()[-800:]


def _cli(meta_env, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "scripts" / "meta_migrate.py"), *args],
                          capture_output=True, text=True, cwd=str(REPO), env=meta_env["env"],
                          timeout=120)


def _count(container: str, table: str) -> int:
    p = subprocess.run(["docker", "exec", container, "psql", "-U", "meta_migrate", "-d",
                        "oneserve_meta", "-A", "-t", "-c",
                        f"SELECT count(*) FROM meta.{table};"],
                       capture_output=True, text=True)
    return int(p.stdout.strip() or "0")


# ----------------------------------------------------------------------
# 正例一：apply 装载＋对账 exit 0（隔离容器全链）
# ----------------------------------------------------------------------

def test_apply_green_and_reconcile_zero(pg_container, meta_env):
    _ddl(pg_container)
    r = _cli(meta_env, "apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "对账不一致" not in r.stdout
    # 统计行格式锚点（具体 N 随源数据形态走——六表对账 OK 即条数与哈希双侧实证；
    # 当前真源形态实证：8/4/12/31/10/7，见卡-1b7 步4 施工报告迁移对照表）
    import re
    for pat in (r"service_catalog \d+ 行", r"upstream_proxy \d+ 行", r"credential 合并 \d+ 行",
                r"grant \d+ 行", r"tenant 合并 \d+ 行", r"七域种子 7 行"):
        assert re.search(pat, r.stdout), f"统计缺锚点：{pat}"
    v = _cli(meta_env, "verify")
    assert v.returncode == 0, v.stdout + v.stderr
    s = _cli(meta_env, "status")
    assert s.returncode == 0
    assert "application: 表位登记未建表（卡红线）" in s.stdout


# ----------------------------------------------------------------------
# 正例二：dry-run 只打印统计零写入
# ----------------------------------------------------------------------

def test_dry_run_zero_writes(pg_container, meta_env):
    _ddl(pg_container)
    r = _cli(meta_env, "apply", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "零写入" in r.stdout and "dry-run 完成" in r.stdout
    assert _count(pg_container, "tenant") == 0, "dry-run 竟然写了行"


# ----------------------------------------------------------------------
# 正例三：幂等复跑（行数收敛＋台账零增长）
# ----------------------------------------------------------------------

def test_idempotent_rerun(pg_container, meta_env):
    _ddl(pg_container)
    assert _cli(meta_env, "apply").returncode == 0
    before = {t: _count(pg_container, t) for t in
              ("field_space", "tenant", "service_catalog", "upstream_proxy", "credential",
               "grant", "admin_event", "version_history")}
    r2 = _cli(meta_env, "apply", "--repeat-safe")
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert "零变化（幂等重跑）" in r2.stdout, "重跑不应再记台账"
    after = {t: _count(pg_container, t) for t in before}
    assert after == before, f"幂等破坏：{before} -> {after}"


# ----------------------------------------------------------------------
# 正例四：哈希对账敏感（篡改一字段 → verify exit 2）
# ----------------------------------------------------------------------

def test_hash_reconcile_detects_drift(pg_container, meta_env):
    _ddl(pg_container)
    assert _cli(meta_env, "apply").returncode == 0
    # 篡改 tmp services.yaml console 写区生效条目（字段级合并——收口批项1：console 值
    # 覆盖运行时区，运行时独有执行字段保留；篡改运行时区同 id 条目不影响合并结果＝
    # 合并语义正证，须改条目级生效值才该红。注：键名改名/条目缺键＝「未表达」不清除
    # 运行时字段，正是项1 合并语义，不属生效面篡改）
    svc = meta_env["src"] / "services.yaml"
    text = svc.read_text(encoding="utf-8")
    head, tail = text.split("# ==== console_services", 1)
    # 动态取写区首个生效条目的条目级 name 值做篡改（先定位首个 "- id:" 行再取其后
    # 条目级 name——直接匹配 "- name:" 会命中 params/fields 列表项的 name 键）
    import re
    m_id = re.search(r"^\s*- id: ", tail, re.M)
    assert m_id, "console 写区无条目可篡改"
    m = re.search(r"^\s*name: (.+)$", tail[m_id.end():], re.M)
    assert m, "console 写区无 name 字段可篡改"
    orig = m.group(1).strip()
    at = m_id.end() + m.start(1)
    svc.write_text(head + "# ==== console_services"
                   + tail[:at] + tail[at:].replace(orig, orig + "（被篡改）", 1),
                   encoding="utf-8")
    v = _cli(meta_env, "verify")
    assert v.returncode == 2, f"哈希对账未抓到漂移：{v.stdout}"
    assert "[DIFF] service_catalog" in v.stdout


# ----------------------------------------------------------------------
# 负例：脏租户拒（exit 2 零写入）
# ----------------------------------------------------------------------

def test_dirty_tenant_rejected_zero_write(pg_container, meta_env):
    _ddl(pg_container)
    mx = meta_env["src"] / "auth_matrix.yaml"
    data = yaml.safe_load(mx.read_text(encoding="utf-8"))
    data["matrix"].append({"tenant": "-bad-tenant", "appkey": None,
                           "oa_ticket": "OA-TEST-BAD", "grants": []})
    mx.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r = _cli(meta_env, "apply")
    assert r.returncode == 2
    assert "租户 ID 非法" in (r.stdout + r.stderr)
    assert _count(pg_container, "tenant") == 0, "脏数据校验前竟有写入"
    rd = _cli(meta_env, "apply", "--dry-run")
    assert rd.returncode == 2, "dry-run 也必须拒载脏数据"
