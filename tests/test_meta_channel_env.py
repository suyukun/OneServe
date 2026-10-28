# -*- coding: utf-8 -*-
"""meta_writer db 通道 env 组装冒烟套件（2026-10-21 前置卡·对抗复核 P0-1 根治，零 docker 依赖）。

覆盖（MetaDbChannel 通道选型优先级与口令透传，钉住防回潮）：
  ① 缺省态：META_DB_HOST/META_WRITER_PSQL_BASE 全空 ＝ PSQL_BASE_DEFAULT（宿主
     docker compose exec 通道现状，dev 零影响口径）；
  ② 容器内直连：META_DB_HOST 非空 ＝ psql -h 直连数组（META_DB_PORT/USER/DATABASE
     缺省补全 5432/meta_app/oneserve_meta；参数尾段同缺省通道口径）；
  ③ 测试钩子最高优先：META_WRITER_PSQL_BASE 在场压过 META_DB_HOST（既有测试注入
     协议不变＝全量套件零新增红的前提）；
  ④ 口令透传：META_DB_PASSWORD 进程级 PGPASSWORD 透传（不进 base 数组＝不落
     SQL/日志）；缺席时 env=None（subprocess 继承父 env＝现状行为）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import meta_writer as mw          # noqa: E402

_ENV_KEYS = ("META_WRITER_PSQL_BASE", "META_DB_HOST", "META_DB_PORT",
             "META_DB_USER", "META_DB_PASSWORD", "META_DB_DATABASE",
             "META_WRITER_TIMEOUT_S")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """七键全清＝隔离进程环境残留（组合态自证，不吃宿主 env）。"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_default_host_channel_unchanged():
    """① env 全空＝宿主 docker compose 通道（现状逐字节）。"""
    ch = mw.MetaDbChannel()
    assert ch.base == mw.PSQL_BASE_DEFAULT.split()
    assert ch.env is None


def test_container_direct_form():
    """② META_DB_HOST 非空＝容器内 psql -h 直连数组（缺省补全）。"""
    os.environ["META_DB_HOST"] = "oneserve-metadata-db"
    ch = mw.MetaDbChannel()
    assert ch.base == ["psql", "-h", "oneserve-metadata-db", "-p", "5432",
                       "-U", "meta_app", "-d", "oneserve_meta",
                       "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]


def test_container_direct_overrides():
    """② 续：PORT/USER/DATABASE 可覆盖（compose.prod.yml prod 形态同构）。"""
    os.environ.update({"META_DB_HOST": "meta-db.internal", "META_DB_PORT": "6543",
                       "META_DB_USER": "meta_app", "META_DB_DATABASE": "oneserve_meta"})
    ch = mw.MetaDbChannel()
    assert ch.base[1:9] == ["-h", "meta-db.internal", "-p", "6543",
                            "-U", "meta_app", "-d", "oneserve_meta"]


def test_test_hook_beats_db_env():
    """③ META_WRITER_PSQL_BASE 最高优先（既有测试注入协议不变）。"""
    os.environ["META_WRITER_PSQL_BASE"] = "docker exec somewhere psql -U x"
    os.environ["META_DB_HOST"] = "oneserve-metadata-db"
    ch = mw.MetaDbChannel()
    assert ch.base == "docker exec somewhere psql -U x".split()


def test_password_passthrough_to_pgpassword():
    """④ 口令进程级 PGPASSWORD 透传；不进 base 数组（不落命令行/SQL/日志面）。"""
    os.environ["META_DB_HOST"] = "oneserve-metadata-db"
    os.environ["META_DB_PASSWORD"] = "pw-secret-value"
    ch = mw.MetaDbChannel()
    assert all("pw-secret-value" not in str(p) for p in ch.base)
    assert ch.env is not None
    assert ch.env["PGPASSWORD"] == "pw-secret-value"


def test_password_absent_env_none():
    """④ 口令缺席＝env None（subprocess 继承父 env＝现状行为不变）。"""
    os.environ["META_DB_HOST"] = "oneserve-metadata-db"
    ch = mw.MetaDbChannel()
    assert ch.env is None
