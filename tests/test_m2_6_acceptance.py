#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK-M2-6 收口卡机检套件——端到端验收脚本与操作手册的「交付物在位＋口径不漂移」。

判据映射（任务卡 ①②；③操作手册断言已随 docs/plans 收缩移除，2026-10-28）：
  ① 端到端验收脚本：scripts/m26_e2e_acceptance.sh 存在，且一条命令覆盖
     「起 HBase 替身 → 造数灌 HBase → 经 HTTP 加载进 Redis → 同源对账差异=0 →
      真调 API（含 data_as_of/live_version）→ 收尾清场」；
  ② 隔离纪律机检：脚本用 COMPOSE_PROJECT_NAME=oneserve-m26 ＋ 专属网络/容器/卷前缀，
     且**绝不用 down -v**、**不写共享栈 Redis**（AGENTS.md 硬纪律）。

分层纪律：本套件只读文本断言（不起容器、不连网）；真链路验证＝
scripts/m26_e2e_acceptance.sh 实跑（脚本自含 PASS/FAIL 计数与全链判据，
运行产物证据不随开源树分发）。
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "m26_e2e_acceptance.sh"
ISO = REPO / "compose.m26-iso.yml"
DRIFT_DRILL = REPO / "scripts" / "drill_schema_drift.sh"


def _read(path: Path) -> str:
    assert path.is_file(), "文件不存在：" + str(path.relative_to(REPO))
    return path.read_text(encoding="utf-8")


# ---------- ① 端到端验收脚本：一条命令覆盖全链 ----------

def test_e2e_script_covers_full_chain():
    """脚本一条命令覆盖：起 HBase → 造数灌 HBase → HTTP 加载 → 对账 → 真调 API。"""
    text = _read(SCRIPT)
    for anchor in (
        "compose.hbase.yml",                            # ① 起 HBase 开发替身容器
        "sync/gen_mock_source.py",                      # ② 造数（与既有链路同源）
        "sync/hbase_loader.py",                         # ② 灌 HBase
        "/v1/admin/publish/hbase/discovery",             # ③ 经 HTTP 选表
        "/v1/admin/publish/hbase/load",                  # ③ 经 HTTP 加载进 Redis
        "hbase_reconcile",                              # ④ 同源对账口径
        "/v1/admin/publish",                            # ⑤ 发布
        "/sync/reconcile.py",                           # ⑥ 上线闸一（源对账）
        "/sync/switch.py",                              # ⑥ 上线闸二（原子切指针）
        "/v1/query/",                                   # ⑦ 真调 API（经网关）
        "live_version",                                 # ⑦ 真数据元信息
        "data_as_of",                                   # ⑦ 时点诚实
    ):
        assert anchor in text, "端到端脚本缺环节：" + anchor
    # 漂移演练复用既有脚本（本卡不重写，只编排调用）
    assert "drill_schema_drift.sh" in text, "端到端脚本未编排既有 schema 漂移演练"
    assert DRIFT_DRILL.is_file(), "既有 scripts/drill_schema_drift.sh 缺失（不许重写，须复用）"


def test_e2e_script_isolation_discipline():
    """隔离纪律硬断言：专属项目名/网络/容器/卷 + 绝不用 down -v + 不写共享栈 Redis。"""
    text = _read(SCRIPT)
    assert 'PROJECT="oneserve-m26"' in text, "未用隔离项目名 oneserve-m26"
    assert "compose.m26-iso.yml" in text, "未显式叠加隔离 override"
    assert "down --remove-orphans" in text, "拆栈未走 down --remove-orphans"
    assert "绝不 down -v" in text, "未声明「绝不 down -v」纪律"
    # 「down -v」只允许出现在说明性文本里（say/echo/注释/cmd/step），不得出现在实际命令中
    narrating = ("say ", "echo ", "#", "step ", "ok ", "bad ", "cmd ")
    for line in text.splitlines():
        s = line.strip()
        if "down -v" not in s and "down --volumes" not in s:
            continue
        assert s.startswith(narrating), "脚本实际执行了打共享卷的拆栈：" + s
    assert "oneserve-m26-" in text, "未按前缀精确删本栈卷"
    assert "写共享栈 Redis" in text, "未声明「不写共享栈 Redis」纪律"


def test_isolation_override_is_self_contained():
    """隔离 override：专属网络 + 专属卷名 + 三处必要的网络别名（单一渲染入口的默认名）。"""
    text = _read(ISO)
    assert "name: oneserve-m26" in text, "override 项目名不是 oneserve-m26"
    assert "oneserve-m26-net" in text, "未起专属网络"
    assert "172.29.0.0/16" in text, "专属网络未与共享栈 172.28.0.0/16 错开"
    for vol in ("oneserve-m26-etcd-data", "oneserve-m26-redis-data",
                "oneserve-m26-audit-data", "oneserve-m26-retention-data",
                "oneserve-m26-batch-results"):
        assert vol in text, "缺专属卷名：" + vol
    # 别名是必要的：init_routes 上游名 / 网关统计钩子 Redis 名 / nginx 反代名
    for alias in ("oneserve-service", "oneserve-apisix", "oneserve-redis"):
        assert alias in text, "缺必要的网络别名（单一渲染入口的默认上游名）：" + alias


# ---------- 验收自含：脚本自带判据与计数，不依赖运行产物留档 ----------

def test_e2e_acceptance_script_is_self_contained():
    """验收脚本自含：PASS/FAIL 计数器与全链判据词内置，运行产物证据不随树分发。"""
    text = _read(SCRIPT)
    assert "PASS=0; FAIL=0" in text, "脚本缺 PASS/FAIL 计数器初始化"
    assert "PASS=$((PASS+1))" in text and "FAIL=$((FAIL+1))" in text, "脚本缺 ok/bad 计数钩子"
    for anchor in ("选表", "加载到 Redis", "同源对账", "真调", "schema 漂移",
                   "共享栈"):
        assert anchor in text, "脚本缺端到端判据环节：" + anchor
    assert "drill_schema_drift.sh" in text, "脚本未编排既有 schema 漂移演练"
