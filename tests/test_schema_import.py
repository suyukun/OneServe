# -*- coding: utf-8 -*-
"""B1 表结构登记套件（2026-10-09）：parse_ddl / merge_rows / save_csv / load_rows 纯逻辑。

纯逻辑直测（无 FastAPI）：临时目录做真源（ONESERVE_SYNC_DIR），不碰仓库 sync/。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

import pytest  # noqa: E402

from app import schema_import as sim  # noqa: E402


@pytest.fixture()
def tmp_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("ONESERVE_SYNC_DIR", str(tmp_path))
    sim.SYNC_DIR = tmp_path          # 模块级常量同步（env 只在 import 时读一次）
    return tmp_path


DDL_OK = """
CREATE TABLE demo_tag (
  aum_band character varying(16) COMMENT 'AUM等级：管理资产规模分档',
  loan_flag tinyint COMMENT '贷款标志：0否 1是',
  dep_bal_band character varying(16)
)
"""


def test_parse_ddl_chinese_comment(tmp_sync):
    cols = sim.parse_ddl(DDL_OK)
    assert [c["name"] for c in cols] == ["aum_band", "loan_flag", "dep_bal_band"]
    assert cols[0]["comment"] == "AUM等级：管理资产规模分档"
    assert cols[1]["type"].upper().startswith("TINYINT")
    assert cols[2]["comment"] == ""            # 无 COMMENT → 空串（如实）


def test_parse_ddl_rejects_garbage(tmp_sync):
    with pytest.raises(sim.SchemaImportError) as ei:
        sim.parse_ddl("这不是 DDL ((( ")
    assert ei.value.code in ("DDL_PARSE_FAILED", "NO_COLUMNS")


def test_parse_ddl_rejects_empty(tmp_sync):
    with pytest.raises(sim.SchemaImportError) as ei:
        sim.parse_ddl("   ")
    assert ei.value.code == "EMPTY_DDL"


def test_merge_preserves_existing_and_flags_dropped(tmp_sync):
    sim.save_csv("demo_tag", [
        ["1", "aum_band", "VARCHAR(16)", "旧中文名", "管理资产规模分档", "内部"],
        ["2", "old_field", "INT", "将消失", "旧口径", "敏感"],
    ])
    existing = sim.load_rows("demo_tag")
    # DDL 里没有 old_field → 必须显式拒绝（不静默删）
    with pytest.raises(sim.SchemaImportError) as ei:
        sim.merge_rows(sim.parse_ddl(DDL_OK), existing)
    assert ei.value.code == "FIELDS_DROPPED"
    assert "old_field" in ei.value.message


def test_merge_keeps_biz_and_sens_new_field_blank(tmp_sync):
    sim.save_csv("demo_tag", [["1", "aum_band", "VARCHAR(16)", "旧中文名", "管理资产规模分档", "内部"]])
    rows = sim.merge_rows(sim.parse_ddl(DDL_OK), sim.load_rows("demo_tag"))
    by = {r[1]: r for r in rows}
    assert by["aum_band"][4] == "管理资产规模分档"   # 业务口径保留
    assert by["aum_band"][5] == "内部"               # 敏感级保留
    assert by["loan_flag"][4] == "" and by["loan_flag"][5] == ""   # 新字段留空（未判定≠内部）
    assert by["aum_band"][3] == "AUM等级：管理资产规模分档"          # cn_name 以 DDL 为准


def test_save_and_load_roundtrip(tmp_sync):
    rows = sim.merge_rows(sim.parse_ddl(DDL_OK), [])
    path = sim.save_csv("demo_tag", rows)
    assert path.name == "schema_demo_tag.csv"
    back = sim.load_rows("demo_tag")
    assert [r["字段名"] for r in back] == ["aum_band", "loan_flag", "dep_bal_band"]


def test_table_name_whitelist(tmp_sync):
    with pytest.raises(sim.SchemaImportError):
        sim._csv_path("../etc/passwd")
    with pytest.raises(sim.SchemaImportError):
        sim._csv_path("UPPER")
    assert sim.table_ok("usr_asset_tag1") and not sim.table_ok("")
