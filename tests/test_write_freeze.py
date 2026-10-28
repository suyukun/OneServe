#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写冻结拒绝码套件（卡-1b7 步8 · WRITE_FROZEN 最小实现守卫，零 docker 依赖）。

覆盖：
  ① db_reachable 探针三态：通道 rc=0 可达 / rc≠0 不可达 / 命令不存在不炸（False）；
  ② MetaDbError 全局 handler 两分支：探针不可达＝503 WRITE_FROZEN（DR runbook §2
     拒绝码口径）＋P1 告警日志；探针可达＝500 META_DB_ERROR 人话（不冒充冻结）；
  ③ 注册在场机检：main.py add_exception_handler(mw.MetaDbError, …) 在场，且注册行
     在 Exception 兜底之前（专用 handler 优先＝防回潮）；
  ④ 探针单点收敛：写函数体不得引用 db_reachable（拒绝形态收敛在 handler，yaml
     模式写径零触碰）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_writer as mw          # noqa: E402
from app import main as svc_main           # noqa: E402


class _ReqStub:
    """handler 单测的最小 request 形状（只读 method 与 url.path 进日志）。"""

    method = "POST"

    class url:  # noqa: N801 —— Starlette URL 属性形状对齐
        path = "/v1/admin/publish"


@pytest.fixture()
def psql_base(monkeypatch):
    """META_WRITER_PSQL_BASE 注入器（探针通道协议＝命令行列表）。"""

    def _set(argv):
        monkeypatch.setenv(mw.ENV_PSQL_BASE, " ".join(argv))

    return _set


# ---------------------------------------------------------------- ① 探针三态

def test_db_reachable_true_when_channel_ok(psql_base):
    psql_base(["true"])                   # rc=0 ＝通道协议层可达
    assert mw.db_reachable() is True


def test_db_reachable_false_when_channel_fails(psql_base):
    psql_base(["false"])                  # rc=1 ＝库不可达（拔库态）
    assert mw.db_reachable() is False


def test_db_reachable_false_when_command_missing(psql_base):
    psql_base(["/nonexistent/psql-bridge-for-drill"])   # OSError 分支
    assert mw.db_reachable() is False


def test_db_reachable_never_raises_on_timeout(monkeypatch):
    # 探针自身超时只回 False，不上抛（拒绝响应不能被探针故障二次打断）
    monkeypatch.setenv(mw.ENV_PSQL_BASE, "sleep 30")
    assert mw.db_reachable(timeout_s=1) is False


# ------------------------------------------------------------ ② handler 两分支

def test_handler_write_frozen_when_db_down(psql_base, caplog):
    psql_base(["false"])
    resp = asyncio.run(svc_main._metadb_exc_handler(_ReqStub(), mw.MetaDbError("boom")))
    assert resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"] == "WRITE_FROZEN"
    assert "元数据库DR_runbook" in body["message"]
    assert any("写冻结" in r.message for r in caplog.records)   # P1·写冻结 告警在场


def test_handler_meta_db_error_when_db_up(psql_base):
    psql_base(["true"])                   # 库活着＝非冻结态，不得冒充 WRITE_FROZEN
    resp = asyncio.run(svc_main._metadb_exc_handler(_ReqStub(), mw.MetaDbError("约束炸了")))
    assert resp.status_code == 500
    body = json.loads(bytes(resp.body))
    assert body["error"] == "META_DB_ERROR"
    assert "约束炸了" in body["message"]


# ------------------------------------------------------------- ③ 注册在场机检

def test_handler_registered_before_exception_fallback():
    src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    reg = "app.add_exception_handler(mw.MetaDbError, _metadb_exc_handler)"
    assert reg in src, "MetaDbError 写冻结出口注册行丢失（卡-1b7 步8 防回潮）"
    assert src.index(reg) < src.index("app.add_exception_handler(Exception,"), \
        "MetaDbError 注册须在 Exception 兜底之前（专用 handler 优先）"


# ------------------------------------------------------------- ④ 探针单点收敛

def test_probe_not_called_by_write_paths():
    """探针只挂 handler：meta_writer 写函数体不得引用 db_reachable（yaml 零触碰）。"""
    src = (REPO / "service" / "app" / "meta_writer.py").read_text(encoding="utf-8")
    body = src.split("def db_reachable", 1)[1]
    assert "db_reachable(" not in body, "写径内部不得调用探针（拒绝形态收敛在 handler 单点）"
