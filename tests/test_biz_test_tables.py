#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""业务化测试表套件（2026-09-29 · 维护者 走查定调：3 张带中文注释的业务表）。

覆盖三块：
  ① 字典多表化·命中路径：resolve_schema_csv(table) 优先 sync/schema_<表名>.csv，
     load_field_dict(table=…) 读出各表自己的 cn_name/业务口径/敏感级；
  ② 字典多表化·回落路径：无表级 CSV（demo oneserve_demo 等）→ 回落既有
     wide297 单字典，与 table 缺省时完全一致（demo 行为零变化＝验收 2 的机检锚点）；
  ③ loader --table：与 --service-id 互斥；直传绕过 services.yaml（cf 缺省 cf）；
     不指定时仍走 resolve_backend（行为不变）。

被测对象：service/app/ops_config.py、sync/hbase_loader.py（纯逻辑/mock，无容器依赖）。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for _p in (str(REPO / "sync"), str(REPO / "service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import ops_config as oc            # noqa: E402
import hbase_loader as hl                   # noqa: E402

BIZ_TABLES = ("usr_basic_info", "usr_asset_tag", "card_acct_summary")
BIZ_COLS = {  # 与 scripts/gen_biz_tables.py 规格一致（钉死抽查锚点）
    "usr_basic_info": {"n": 12, "sensitive": ["risk_level", "occupation_cd", "marital_sts",
                                             "blacklist_flag"],
                       "cn": {"gender_cd": "性别代码", "open_date": "开户日期"}},
    "usr_asset_tag": {"n": 18, "sensitive": ["credit_limit_band", "overdue_cnt_band"],
                      "cn": {"aum_band": "AUM等级", "churn_risk_band": "流失风险档"}},
    "card_acct_summary": {"n": 14, "sensitive": ["txn_amt_m1_band", "install_cnt_flag",
                                                "cash_adv_flag"],
                          "cn": {"card_sts": "卡状态", "last_txn_days": "距上次交易天数"}},
}


@pytest.fixture(autouse=True)
def _no_env_schema_csv(monkeypatch):
    """隔离环境覆盖：SCHEMA_CSV 一旦外泄会让「按表找字典」测试失真。"""
    monkeypatch.delenv("SCHEMA_CSV", raising=False)


# ======================================================================
# ① 字典多表化：命中路径（真交付物 sync/schema_<表名>.csv 在场）
# ======================================================================

def test_resolve_schema_csv_table_hit():
    """表名（含 ns: 前缀）→ 命中 sync/schema_<表名>.csv。"""
    for t in BIZ_TABLES:
        assert oc.resolve_schema_csv(t).name == f"schema_{t}.csv"
        assert oc.resolve_schema_csv(f"default:{t}").name == f"schema_{t}.csv"   # 剥 ns 前缀
        assert oc.resolve_schema_csv(t).is_file()


def test_load_field_dict_per_table_cn_and_sensitivity():
    """每表自己的字典：列数/cn_name/业务口径/敏感级（发布台中文注释真源）。"""
    for t, spec in BIZ_COLS.items():
        d = oc.load_field_dict(table=t)
        assert len(d) == spec["n"], f"{t} 应 {spec['n']} 列"
        for name, cn in spec["cn"].items():
            assert d[name]["cnName"] == cn
            assert d[name]["definition"]                     # 业务口径非空
        for s in spec["sensitive"]:
            assert d[s]["sensitivity"] == "敏感"  # 敏感级列保留为纯数据（CSV 列不删），2026-09-29 起不再作为 AC-7 依据


def test_load_field_dict_table_hit_in_tmp_layout(monkeypatch, tmp_path):
    """命中路径不依赖仓库真文件：tmp 布局 service/../sync/schema_<t>.csv 同样命中。"""
    (tmp_path / "sync").mkdir()
    (tmp_path / "sync" / "schema_foo.csv").write_text(
        "attnum,字段名,数据类型,cn_name,业务口径,敏感级\n1,f_col,character varying(16),中文名,口径A,内部\n",
        encoding="utf-8")
    monkeypatch.setattr(oc, "SERVICE_DIR", tmp_path / "service")
    assert oc.resolve_schema_csv("foo").name == "schema_foo.csv"
    d = oc.load_field_dict(table="foo")
    assert d == {"f_col": {"cnName": "中文名", "type": "character varying(16)",
                           "sensitivity": "内部", "definition": "口径A"}}


# ======================================================================
# ② 字典多表化：回落路径（demo 表零变化）
# ======================================================================

def test_resolve_schema_csv_fallback_for_table_without_own_csv():
    """无表级 CSV 的表（demo）→ 回落 wide297；与 table 缺省同一文件。"""
    demo = oc.resolve_schema_csv("oneserve_demo")
    assert demo.name == oc.SCHEMA_CSV_NAME and demo == oc.resolve_schema_csv()
    # 非法表名（路径字符）整体回落，不猜、不报错（防御式：与缺省同字典）
    assert oc.resolve_schema_csv("../etc/passwd") == oc.resolve_schema_csv()


def test_load_field_dict_fallback_equals_default():
    """demo/未知表与缺省调用读同一份宽表字典（逐字段相等＝兼容验收的机检形态）。"""
    base = oc.load_field_dict()
    assert len(base) == 297
    assert oc.load_field_dict(table="oneserve_demo") == base
    assert oc.load_field_dict(table="no_such_biz_table") == base


def test_resolve_schema_csv_env_override_still_wins(monkeypatch, tmp_path):
    """SCHEMA_CSV 环境变量仍是最高优先（既有语义保留，csv_path/env 兼容口径）。"""
    env_csv = tmp_path / "env_schema.csv"
    env_csv.write_text("attnum,字段名,数据类型,cn_name,业务口径,敏感级\n1,e,text,环境字典,,\n",
                       encoding="utf-8")
    monkeypatch.setenv("SCHEMA_CSV", str(env_csv))
    assert oc.resolve_schema_csv("usr_basic_info") == env_csv
    assert oc.load_field_dict(table="usr_basic_info") == {
        "e": {"cnName": "环境字典", "type": "text", "sensitivity": "", "definition": ""}}


# ======================================================================
# 发布台发现视图：每表各的字典（命中）＋ demo 回落（兼容）
# ======================================================================

def _biz_meta():
    """假 HBase meta：demo＋3 张业务表，families.cf 列＝各表样本实见列（前 3 列）。"""
    fams = {"oneserve_demo": ["ac_active_sts_01"]}
    for t in BIZ_TABLES:
        d = oc.load_field_dict(table=t)
        fams[t] = sorted(d)[:3]
    return {"namespaces": {"default": {t: {"families": {"cf": cols}}
                                        for t, cols in fams.items()}},
            "table_count": len(fams)}


def test_build_datasource_view_per_table_dict_and_demo_unchanged():
    """发现视图：业务表用表级字典（中文注释）；demo 回落宽表 296 列且形状不变。"""
    view = oc.build_datasource_view(_biz_meta())
    tables = {t["table"]: t for d in view["domains"] for t in d["tables"]}
    # demo：回落路径——宽表字典 296 出参列、旧 cnName 通道照旧
    demo_cols = tables["oneserve_demo"]["families"][0]["columns"]
    assert len(demo_cols) == 296 and "usr_id" not in {c["name"] for c in demo_cols}
    assert {c["name"] for c in demo_cols} == {n for n in oc.load_field_dict() if n != "usr_id"}
    # 业务表：各表各的列清单＋中文 cnName＋口径 desc
    for t, spec in BIZ_COLS.items():
        cols = {c["name"]: c for c in tables[t]["families"][0]["columns"]}
        assert len(cols) == spec["n"]
        for name, cn in spec["cn"].items():
            assert cols[name]["cnName"] == cn and cols[name]["cf"] == "cf"
            assert cols[name]["desc"]                          # 业务口径进 desc
        for s in spec["sensitive"]:
            assert cols[s]["sensitive"] == "敏感"  # 纯数据透传（发布行为零消费，仅字典数据通道保留）
        seen = {c["name"] for c in tables[t]["families"][0]["columns"] if c["sampleSeen"]}
        assert seen <= set(cols) and seen                       # 样本实见列如实标注
    # 双源校验：业务表样本列 ∈ 「回落字典 ∪ 表级字典」→ 不误报 mismatch
    assert view["dualSource"]["status"] == "ok", view["dualSource"]["unknownColumns"]
    # 顶层契约不动：dict.columnCount 仍＝宽表 297（M2-5 适配层锚点）
    assert view["dict"]["columnCount"] == 297 and view["dict"]["source"].endswith(
        oc.SCHEMA_CSV_NAME)


def test_publish_ignores_sensitivity_level_entirely():
    """敏感级列存在但不驱动任何发布行为：usr_basic_info 全字段（含 4 个敏感级列）
    发布成功，字段零自动脱敏标记、运行时条目 response.desensitize 为空。"""
    import tempfile
    from app import kv_executor as kvx
    real = (REPO / "service" / "services.yaml").read_text(encoding="utf-8")
    seed = """  - service_id: seed_kv_query            # 测试基板合成条目（非线上服务）
    version: v1
    archetype: kv_lookup
    backend:
      kv:
        namespace: tags:seed
        alias_key: tags:seed:latest
        data_key: 'tags:seed:{version}:{sm3}'
        version_meta_key: 'sync:ver:seed:{version}'
        storage_key: as_received
        entity: seed
    params:
      - name: key
        type: string(1..128)
        key_type: seed_key
        pattern: '^[A-Za-z0-9_.-]{1,128}$'
    response:
      projection: auth_matrix
      meta: [data_as_of, live_version]
    guard:
      timeout_ms: 3000
      max_items_per_call: 1
    audit:
      enabled: true
      log_params: hash_only
      log_response_digest: sha256_of_desensitized_body
"""
    p = Path(tempfile.mkdtemp()) / "services.yaml"
    p.write_text(real.split("services:")[0] + "services:" + chr(10) + seed, encoding="utf-8")
    d = oc.load_field_dict(table="usr_basic_info")
    rows = [{"row": "a" * 64, "columns": {f"cf:{n}": "1" for n in d}}]
    fields = [{"name": n, "sample": "", "desc": ""} for n in d if n != "usr_id"]
    e, _ = oc.publish_service(p, name="用户基本信息", route="/v1/svc/biz-nosens",
                              fields=fields, datasource="hbase",
                              hbase={"table": "usr_basic_info", "cf": "cf", "key_type": "phone_sm3"},
                              sample_rows=rows)
    fs = oc.load_console_services(p)[0]["fields"]
    assert e["status"] == "published" and len(fs) == len(fields) == 12
    assert all(f["desensitize"] == "" for f in fs), "敏感级不得再驱动自动脱敏"
    rt = kvx.load_services(p)["v1-svc-biz-nosens"]
    assert not rt["response"].get("desensitize"), "无显式配置时 response.desensitize 必须为空"


def test_build_datasource_view_ghost_column_still_mismatch():
    """回落回归锚点：无表级字典的表出现字典外列 → 仍 mismatch（既有 AC-4 语义不放松）。"""
    meta = {"namespaces": {"default": {"t": {"families": {"cf": ["ghost_col"]}}}}}
    view = oc.build_datasource_view(meta)
    assert view["dualSource"]["status"] == "mismatch"
    assert view["dualSource"]["unknownColumns"] == ["ghost_col"]


# ======================================================================
# ③ loader --table：直传绕过 services.yaml；互斥；缺省不变
# ======================================================================

class _Recorder:
    def __init__(self):
        self.calls = {}

    def __call__(self, *a, **kw):
        self.calls.setdefault((a, tuple(sorted(kw.items()))), 0)
        self.calls[(a, tuple(sorted(kw.items())))] += 1
        return self


def _fake_load_side(captured):
    def _load(pool, table, cf, data, *, replace=False, batch_size=None):
        captured.update(table=table, cf=cf, replace=replace)
        return {"created": True, "cleared": 0, "written": len(data["keys"]),
                "rows_in_table": len(data["keys"])}
    return _load


def _patch_loader(monkeypatch, captured, backend_calls):
    data = {"keys": ["a" * 64], "rows": [{"x": "1"}], "tag_cols": ["x"],
            "nonempty_cells": 1, "sha256": "0" * 8}
    monkeypatch.setattr(hl, "read_csv", lambda p: data)
    monkeypatch.setattr(hl, "connect", lambda host, port: object())
    monkeypatch.setattr(hl, "list_tables", lambda pool, host, port: [])
    monkeypatch.setattr(hl, "load", _fake_load_side(captured))

    def _resolve_backend(service_id, services=None):
        backend_calls.append(service_id)
        return "default:usr_basic_info", "cf"   # 假注册表（oneserve_demo 已删，2026-10-08）
    monkeypatch.setattr(hl, "resolve_backend", _resolve_backend)


def test_loader_table_bypasses_services_yaml(monkeypatch, capsys):
    """--table 直传：表/列族不经 services.yaml（cf 缺省 cf），输出带 resolved_via。"""
    captured, backend_calls = {}, []
    _patch_loader(monkeypatch, captured, backend_calls)
    rc = hl.main(["--csv", "x.csv", "--version", "20260929",
                  "--table", "default:usr_basic_info", "--replace"])
    assert rc == 0 and backend_calls == []                    # 绝不经 services.yaml
    assert captured["table"] == "default:usr_basic_info" and captured["cf"] == "cf"
    assert captured["replace"] is True
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["table"] == "default:usr_basic_info"
    assert out["cf"] == "cf" and "--table" in out["resolved_via"]


def test_loader_table_and_service_id_are_mutually_exclusive(monkeypatch, capsys):
    """--table × --service-id 同给 → 拒绝（JSON 报错退出 1，不进连接层）。"""
    boom = _Recorder()
    monkeypatch.setattr(hl, "connect", boom)
    rc = hl.main(["--csv", "x.csv", "--version", "20260929",
                  "--table", "usr_basic_info", "--service-id", "some_service"])
    assert rc == 1 and not boom.calls                        # 未触达连接层
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "互斥" in out["error"]


def test_loader_explicit_service_id_uses_resolve_backend(monkeypatch, capsys):
    """显式 --service-id：走 resolve_backend（注册表解析）；resolved_via 带服务名。"""
    captured, backend_calls = {}, []
    _patch_loader(monkeypatch, captured, backend_calls)
    rc = hl.main(["--csv", "x.csv", "--version", "20260929", "--service-id", "some_service"])
    assert rc == 0 and backend_calls == ["some_service"]
    assert captured["table"] == "default:usr_basic_info" and captured["cf"] == "cf"
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and "services.yaml" in out["resolved_via"]


def test_loader_requires_explicit_table_or_service_id(monkeypatch, capsys):
    """--table 与 --service-id 都不给 → 拒绝（demo_hbase_query 缺省已随 oneserve_demo
    演示遗留清理删除，2026-10-08；不再有隐式缺省，未触达连接层）。"""
    boom = _Recorder()
    monkeypatch.setattr(hl, "connect", boom)
    rc = hl.main(["--csv", "x.csv", "--version", "20260929"])
    assert rc == 1 and not boom.calls                        # 未触达连接层
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "须显式 --service-id 或 --table" in out["error"]
