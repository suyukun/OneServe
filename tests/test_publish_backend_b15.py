#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布台重设计·后端最小改动套件（SPEC v1.1 §七 B1-B5 · fix/publish-backend-b15）。

B1 发布字典按表：hbase 型发布校验按所选表取表级字典（sync/schema_<表名>.csv），
   缺表级 CSV 回落宽表 297（与 discovery 同款机制，零第二实现）；redis 型零变化。
B2 示例值自动生成：发布态缺示例值 → 从表样本行（discovery 同款 scan 形状）实取
   每字段首个非空值，显式配 band_mask 的字段经档位规则后填充；取不到 → SAMPLE_REQUIRED 如实拒绝。
B3 脱敏＝手动显式配置的独立机制（2026-09-29 裁决：敏感概念整体移除，授权是唯一
   访问控制层）：敏感级不再驱动任何发布行为；显式 desensitize=band_mask 才落
   运行时档位规则（A/B/C/U 档，机制复用 kv_executor 单一实现）。
B4/B5 测试见本文件下半部（切版三端点服务层＋表级中文名）。

被测对象：service/app/ops_config.py（纯逻辑，无 FastAPI/redis 依赖）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "sync"))

from app import kv_executor as kvx            # noqa: E402
from app import ops_config as oc              # noqa: E402

BIZ = "usr_basic_info"                          # 12 列业务表（4 敏感列）
HB = {"table": BIZ, "cf": "cf", "key_type": "phone_sm3"}


@pytest.fixture(autouse=True)
def _no_env_schema_csv(monkeypatch):
    """隔离环境覆盖：SCHEMA_CSV 外泄会让「按表取字典」测试失真。"""
    monkeypatch.delenv("SCHEMA_CSV", raising=False)


def _tmp_services() -> Path:
    """发布管线基板：真 services.yaml 头注释＋最小运行时服务条目（空表终态可追加）。"""
    d = Path(tempfile.mkdtemp())
    p = d / "services.yaml"
    real = (REPO / "service" / "services.yaml").read_text(encoding="utf-8")
    head = real.split("services:")[0] + "services:" + chr(10)
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
    p.write_text(head + seed, encoding="utf-8")
    return p


def _biz_fields(names, sample="x", desensitize=""):
    return [{"name": n, "sample": sample, "desc": "", "desensitize": desensitize}
            for n in names]


def _rows(vals: dict) -> list[dict]:
    """discovery 同款 scan 采样形状：[{row, columns:{'cf:列名': 值}}]。"""
    return [{"row": "a" * 64, "columns": {f"cf:{k}": v for k, v in vals.items()}}]


# ======================================================================
# B1 发布字典按表
# ======================================================================

def test_b1_biz_table_fields_publish_with_per_table_dict():
    """B1 主缺口：业务表 12 字段（不在宽表 297）经表级字典发布成功——不再 FIELD_NOT_IN_DICT。"""
    p = _tmp_services()
    d = oc.load_field_dict(table=BIZ)
    names = [n for n in d if n != "usr_id"]
    e, _ = oc.publish_service(p, name="用户基本信息", route="/v1/svc/biz-basic",
                              fields=_biz_fields(names), datasource="hbase", hbase=HB)
    assert e["status"] == "published" and len(e["fields"]) == 12
    # 字典外字段（表级字典 12 列也没有）仍拒——per-table 拦截不放松，消息按 12 列如实
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="n", route="/v1/svc/biz-bad",
                           fields=_biz_fields(["ghost_col"]), datasource="hbase", hbase=HB)
    assert ei.value.code == "field_not_in_dict" and "12 列" in ei.value.message


def test_b1_demo_table_falls_back_to_wide_dict():
    """回落路径：demo 表（无表级 CSV）仍按宽表 297 校验——业务列不在宽表即拒（兼容零变化）。"""
    p = _tmp_services()
    hb = {"table": "oneserve_demo", "cf": "cf", "key_type": "phone_sm3"}
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="n", route="/v1/svc/dmo-bad",
                           fields=_biz_fields(["gender_cd"]), datasource="hbase", hbase=hb)
    assert ei.value.code == "field_not_in_dict" and "297" in ei.value.message
    # 宽表真列照旧放行
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/dmo-ok",
                              fields=_biz_fields(["ac_active_sts_01"]), datasource="hbase", hbase=hb)
    assert e["status"] == "published"


def test_b1_wide_loopback_gate_unchanged_for_redis():
    """redis 型回环防线零变化：宽表域语义占位符 tags 仍拒（与既有 test_ops_config 同口径）。"""
    p = _tmp_services()
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="n", route="/v1/svc/backdoor", fields=_biz_fields(["tags"]))
    assert ei.value.code == "field_not_in_dict" and "宽表" in ei.value.message


# ======================================================================
# B2 示例值自动生成
# ======================================================================

def test_b2_samples_auto_filled_from_rows_and_masked():
    """B2：无示例值输入 → 从样本行实取；敏感级列（字典纯数据）不再驱动分档——
    未配置字段原值实取；显式配 band_mask 的字段经档位规则（A/B/C/U，原值零出现）。"""
    d = oc.load_field_dict(table=BIZ)
    sens = sorted(n for n in d if n != "usr_id" and d[n]["sensitivity"] == "敏感")
    norm = [n for n in d if n != "usr_id" and d[n]["sensitivity"] != "敏感"][:2]
    vals = {norm[0]: "1", norm[1]: "",                       # 一行空值、次行补上
            sens[0]: "5000", sens[1]: "50000", sens[2]: "2500000", sens[3]: "married"}
    rows = _rows(vals) + _rows({norm[1]: "2"})               # 跨行取首个非空值
    p = _tmp_services()
    fields = ([{"name": n, "sample": "", "desc": ""} for n in norm + sens[:3]]
              + [{"name": sens[3], "sample": "", "desc": "", "desensitize": "band_mask"}])
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/auto-sample",
                              fields=fields, datasource="hbase", hbase=HB, sample_rows=rows)
    fs = {f["name"]: f for f in oc.load_console_services(p)[0]["fields"]}
    assert fs[norm[0]]["sample"] == "1" and fs[norm[1]]["sample"] == "2"
    # 未配置（含敏感级=敏感的列）→ 原值实取，敏感级零消费
    assert fs[sens[0]]["sample"] == "5000" and fs[sens[1]]["sample"] == "50000"
    assert fs[sens[2]]["sample"] == "2500000"
    # 显式 band_mask → 非数值走 unknown 档 U（原值零出现）
    assert fs[sens[3]]["sample"] == "U"



def test_b2_sample_gate_fails_closed_without_rows():
    """fail-closed：无样本行（redis 型/未注入）→ SAMPLE_REQUIRED 原门禁不变；
    有样本行但该列全空 → 拒绝并如实说明「自动生成未取到」。"""
    p = _tmp_services()
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="n", route="/v1/svc/no-rows",
                           fields=[{"name": "gender_cd", "sample": "", "desc": ""}],
                           datasource="hbase", hbase=HB)
    assert ei.value.code == "sample_required"
    with pytest.raises(oc.OpsConfigError) as ei2:
        oc.publish_service(p, name="n", route="/v1/svc/empty-col",
                           fields=[{"name": "gender_cd", "sample": "", "desc": ""}],
                           datasource="hbase", hbase=HB, sample_rows=_rows({"cf:city_code": "x"}))
    assert ei2.value.code == "sample_required" and "自动生成" in ei2.value.message
    # 草稿不卡示例值（既有语义保留）
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/draft",
                              fields=[{"name": "gender_cd", "sample": "", "desc": ""}],
                              status="draft", datasource="hbase", hbase=HB)
    assert e["status"] == "draft"


def test_b2_pick_field_samples_qualifier_semantics():
    """取值语义：按 qualifier 末段匹配（跨列族有效）；首个非空值胜出；采满即停。"""
    rows = [
        {"row": "r1", "columns": {"cf:a": "", "meta:a": "from_meta", "cf:b": "b1"}},
        {"row": "r2", "columns": {"cf:a": "a2", "cf:b": "b2"}},
    ]
    got = oc.pick_field_samples([{"name": "a"}, {"name": "b"}], rows)
    assert got == {"a": "from_meta", "b": "b1"}
    assert oc.pick_field_samples([{"name": "a"}], []) == {}


# ======================================================================
# B3 脱敏＝手动显式配置的独立机制（敏感概念已移除：授权是唯一访问控制层）
# ======================================================================

def test_b3_no_config_means_empty_desensitize():
    """不变式：无脱敏配置时发布产物 response.desensitize 为空（敏感级不再驱动任何
    发布行为——字典里敏感级=敏感的列照常发布，零自动 band_mask、零拦截）。"""
    d = oc.load_field_dict(table=BIZ)
    sens = sorted(n for n in d if n != "usr_id" and d[n]["sensitivity"] == "敏感")
    assert sens, "字典应仍带敏感级纯数据列（CSV 保留）"
    p = _tmp_services()
    rows = _rows({n: "123456" for n in sens})
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/biz-plain",
                              fields=[{"name": n} for n in sens],
                              datasource="hbase", hbase=HB, sample_rows=rows)
    fs = oc.load_console_services(p)[0]["fields"]
    assert all(f["desensitize"] == "" for f in fs), "未配置字段不应出现自动脱敏标记"
    rt = kvx.load_services(p)["v1-svc-biz-plain"]
    assert not rt["response"].get("desensitize"), "无配置时 response.desensitize 必须为空"


def test_b3_band_mask_rule_bands_and_unknown():
    """机制保留（与敏感概念无关）：显式 band_mask 档位钉死——A<1万、B<100万、
    C≥100万、非数值 U（机制＝kv_executor._band_mask）。"""
    rule = oc.BAND_MASK_RULE
    cases = {"9999": "A", "10000": "B", "999999": "B", "1000000": "C", "9.9": "A",
             "abc": "U", "": "U"}
    for raw, expect in cases.items():
        assert kvx._band_mask(raw, rule) == expect, raw


def test_b3_explicit_band_mask_lands_executable_rules():
    """显式配置真实生效：desensitize=band_mask 的字段落运行时 response.desensitize
    （kv_executor 按此分档，原值零出现）；自定义标记原样保留、不映射内建规则。"""
    d = oc.load_field_dict(table=BIZ)
    sens = sorted(n for n in d if n != "usr_id" and d[n]["sensitivity"] == "敏感")
    p = _tmp_services()
    rows = _rows({sens[0]: "123456"})
    e, _ = oc.publish_service(p, name="n", route="/v1/svc/biz-rule",
                              fields=[{"name": sens[0], "sample": "", "desensitize": "band_mask"},
                                      {"name": sens[1], "sample": "x", "desensitize": "custom_rule"}],
                              datasource="hbase", hbase=HB, sample_rows=rows)
    fs = {f["name"]: f for f in oc.load_console_services(p)[0]["fields"]}
    assert fs[sens[0]]["desensitize"] == "band_mask"         # 显式标记原样保留
    assert fs[sens[1]]["desensitize"] == "custom_rule"       # 自定义标记不映射内建规则
    rt = kvx.load_services(p)["v1-svc-biz-rule"]
    rules = rt["response"]["desensitize"]
    assert set(rules) == {sens[0]} and rules[sens[0]] == oc.BAND_MASK_RULE
    out = kvx.apply_desensitize({sens[0]: "123456"}, rules)  # 执行器真跑：123456 → B 档
    assert out[sens[0]] == "B"





# ======================================================================
# B4 切版三端点（服务层纯逻辑；HTTP 编排按仓内惯例走源码机检——宿主无 fastapi）
# ======================================================================

class _FakeSyncRedis:
    """sync common 口径假连接（get/hgetall/lrange；状态读取只此三面）。"""

    def __init__(self, live="20260929", meta=None, log=None):
        self._live, self._meta, self._log = live, meta, log or []

    def get(self, k):
        return self._live

    def hgetall(self, k):
        return self._meta

    def lrange(self, k, a, b):
        return self._log[a:b + 1]


def test_b4_sync_status_reads_pointer_meta_and_log():
    """status：指针→版本、版本元信息、最近切换日志（键规则经 sync/common 单一实现）；
    无指针如实 null；坏 JSON 条目跳过并计数。"""
    from app import sync_ops as so
    import json as _json
    log = [_json.dumps({"at": "t1", "action": "switch", "from": "20260920", "to": "20260929"}),
           "{bad json",
           _json.dumps({"at": "t2", "action": "rollback", "from": "20260929", "to": "20260920"})]
    view = so.sync_status(_FakeSyncRedis(meta={"rows": "10000"}, log=log))
    assert view["ok"] and view["liveVersion"] == "20260929"
    assert view["versionMeta"] == {"rows": "10000"}
    assert view["recentSwitches"][0]["to"] == "20260929"
    assert len(view["recentSwitches"]) == 2 and "1 条坏 JSON" in view["note"]
    empty = so.sync_status(_FakeSyncRedis(live=None))
    assert empty["liveVersion"] is None and empty["versionMeta"] is None
    assert "未上线" in empty["note"]


def test_b4_switch_and_rollback_http_mapping(monkeypatch):
    """CLI 结果→HTTP 映射全路径：成功 200/幂等 200/闸拒绝 409（人话 reason 原样带出）/
    进程故障 502/缺参 400——失败不吞不美化。"""
    from app import sync_ops as so
    canned = {}
    monkeypatch.setattr(so, "_run_cli", lambda script, args, **kw: canned[script])
    ok_out = {"ok": True, "switched": True, "from": "20260920", "to": "20260929",
              "switch_elapsed_ms": 1.2}
    canned[so.SWITCH_SCRIPT] = {"rc": 0, "out": ok_out}
    st, body = so.run_switch("20260929")
    assert st == 200 and body["switched"] and body["version"] == "20260929"
    assert body["from"] == "20260920"
    # 幂等无动作（指针已指向目标）→ 200（note 如实）
    canned[so.SWITCH_SCRIPT] = {"rc": 0, "out": {"ok": True, "switched": False,
                                                   "note": "指针已指向 20260929，幂等无动作"}}
    st2, body2 = so.run_switch("20260929")
    assert st2 == 200 and body2["switched"] is False
    # 三闸拒绝：rc=1＋reason → 409＋人话 reason（线上仍旧版）
    canned[so.SWITCH_SCRIPT] = {"rc": 1, "out": {"ok": False, "switched": False,
                                                   "reason": "对账未通过或未对账（pass=0），拒切，线上仍旧版"}}
    st3, body3 = so.run_switch("20260930")
    assert st3 == 409 and body3["error"] == "switch_rejected" and "对账" in body3["reason"]
    # 进程级故障（非 JSON 输出）→ 502，stderr 尾部如实带出
    canned[so.SWITCH_SCRIPT] = {"rc": 2, "out": None, "stdout": "", "stderr": "Traceback ... boom"}
    st4, body4 = so.run_switch("20260930")
    assert st4 == 502 and body4["error"] == "SWITCH_CLI_CRASH" and "boom" in body4["message"]
    # 缺参 → 400（不进子进程）
    st5, body5 = so.run_switch("  ")
    assert st5 == 400 and body5["error"] == "invalid_version"
    # 回滚：显式 to / 缺省自动上一版（args 断言：缺省不带 --to，判定留给 CLI）
    seen = {}

    def _fake_run(script, args, **kw):
        seen["args"] = args
        return {"rc": 0, "out": {"ok": True, "rolled_back": True, "from": "20260929",
                                  "to": "20260920", "target_rows": 100}}

    monkeypatch.setattr(so, "_run_cli", _fake_run)
    st6, body6 = so.run_rollback("20260920")
    assert st6 == 200 and body6["rolled_back"] and body6["to"] == "20260920"
    so.run_rollback("")
    assert seen["args"] == []                       # 缺省＝自动上一版＋不切域（判定归 CLI）
    # 带表语境（按表隔离）：CLI 收 --table <裸表名>，三闸在表键空间判定
    so.run_switch("20260929", "default:usr_basic_info")
    assert seen["args"] == ["--table", "usr_basic_info", "--version", "20260929"]
    assert body6.get("table") is None               # 缺省不切域：回显 table=None（如实）
    so.run_rollback("20260920", "usr_asset_tag")
    assert seen["args"] == ["--table", "usr_asset_tag", "--to", "20260920"]
    # 非法表名 → SyncOpsError(400)（HTTP 层如实映射；不进子进程）
    with pytest.raises(so.SyncOpsError) as ei:
        so.run_switch("20260929", "ns:bad-table!")
    assert ei.value.status == 400 and ei.value.code == "INVALID_TABLE"
    st8, body8 = so.run_rollback()
    # 回显键不覆盖 CLI 真实字段：CLI 报 to=20260920（自动解析的上一版）须原样带出
    assert st8 == 200 and body8["to"] == "20260920" and body8["target_rows"] == 100
    # 闸拒绝路径（回滚安全闸）
    monkeypatch.setattr(so, "_run_cli",
                        lambda script, args, **kw: {"rc": 1, "out": {"ok": False, "rolled_back": False,
                                                                    "reason": "目标空间 20260919 为空/不存在，拒回滚（防线上空数据）"}})
    st9, body9 = so.run_rollback("20260919")
    assert st9 == 409 and body9["error"] == "rollback_rejected" and "拒回滚" in body9["reason"]


def test_b4_reuse_cli_zero_gate_duplication_and_zero_modification():
    """只包不改的机器断言：①sync_ops 以子进程调用既有 switch.py/rollback.py（复用）；
    ②闸实现零复制（闸判定标记一概没有；键常量仅经 sync_common.C 单一事实来源**展示**，
    2026-09-29 按表隔离起 status note 引用 C.ALIAS_KEY——读自唯一实现，非第二套口径）；
    ③两闸脚本的**闸体区块**相对 HEAD 逐字节未改（按表隔离只允许参数化键名——
    argparse 加 --table 与切域调用，放行/安全闸判定行不得出现在改动里）。"""
    import subprocess as _sp
    from app import sync_ops as so
    src = (REPO / "service" / "app" / "sync_ops.py").read_text(encoding="utf-8")
    assert so.SWITCH_SCRIPT in src and so.ROLLBACK_SCRIPT in src and "subprocess" in src
    for marker in ("RECON_META", "count_keys"):
        assert marker not in src, f"sync_ops 出现闸实现标记 {marker}（涉嫌第二实现）"
    assert ".set(" not in src.replace("setattr", ""), "sync_ops 不应直接 SET 指针（切换归 CLI）"

    def _section(text, start, end):
        i, j = text.index(start), text.index(end)
        assert 0 <= i < j, f"闸体区块标记缺失（{start!r}…{end!r}）"
        return text[i:j]

    # 闸体区块（放行三闸 / 回滚安全闸）与 HEAD 逐字节一致：按表参数化不得触碰判定逻辑
    gate_marks = (("sync/switch.py", "    # —— 放行三闸 ——", "    # —— 原子切：单条 SET ——"),
                  ("sync/rollback.py", "    target = args.to or _previous_version(r, live)",
                   "    # —— 原子回滚：单条 SET ——"))
    for script, start, end in gate_marks:
        cur = (REPO / script).read_text(encoding="utf-8")
        head = _sp.run(["git", "-C", str(REPO), "show", "HEAD:" + script],
                       capture_output=True).stdout.decode("utf-8")
        assert _section(cur, start, end) == _section(head, start, end), \
            f"{script} 闸体区块相对 HEAD 被改动（验收③：闸逻辑零改动，只参数化键名）"


def test_b4_admin_api_three_sync_endpoints_wired():
    """HTTP 编排机检（宿主无 fastapi，同 test_admin_api_endpoints_wired 口径)：
    三端点在位、调 sync_ops、SyncOpsError 按其 status 如实映射。"""
    import py_compile
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    for route in ('"/v1/admin/publish/sync/status"', '"/v1/admin/publish/sync/switch"',
                  '"/v1/admin/publish/sync/rollback"'):
        assert route in src, f"缺端点 {route}"
    for call in ("syo.sync_status", "syo.run_switch", "syo.run_rollback", "syo.SyncOpsError"):
        assert call in src, f"端点未接服务层 {call}"
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "sync_ops.py"), doraise=True)


# ======================================================================
# B5 表级中文名（tables_meta.yaml → discovery tableCnName/tableDesc）
# ======================================================================

def _biz_meta():
    fams = {"oneserve_demo": ["ac_active_sts_01"], "tmp_demo_unregistered": ["f0"]}
    for t in ("usr_basic_info", "usr_asset_tag", "card_acct_summary"):
        d = oc.load_field_dict(table=t)
        fams[t] = sorted(d)[:3]
    return {"namespaces": {"default": {t: {"families": {"cf": cols}} for t, cols in fams.items()}},
            "table_count": len(fams)}


def test_b5_tables_meta_registered_and_missing_fallback():
    """已登记表 → tableCnName/tableDesc 取 tables_meta；未登记 → 空串（如实，不猜）。

    oneserve_demo 元数据已随该表删除一并清理（2026-10-08）——其在发现视图里
    与 tmp_demo_unregistered 同样落「未登记 → 空串」桶（删登记生效验证）。"""
    view = oc.build_datasource_view(_biz_meta())
    tables = {t["table"]: t for d in view["domains"] for t in d["tables"]}
    assert tables["usr_basic_info"]["tableCnName"] == "用户基本信息"
    assert tables["usr_basic_info"]["tableDesc"].startswith("客户人口属性")
    assert tables["usr_asset_tag"]["tableCnName"] and tables["card_acct_summary"]["tableCnName"]
    assert tables["oneserve_demo"]["tableCnName"] == ""
    assert tables["oneserve_demo"]["tableDesc"] == ""
    assert tables["tmp_demo_unregistered"]["tableCnName"] == ""
    assert tables["tmp_demo_unregistered"]["tableDesc"] == ""


def test_b5_load_tables_meta_degrades_honestly(tmp_path):
    """装载降级：缺文件＝{}（发现视图全空串）；坏结构＝空表；部分字段补空串；注入口径可用。"""
    assert oc.load_tables_meta(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just" + chr(10) + "- a list" + chr(10), encoding="utf-8")
    assert oc.load_tables_meta(bad) == {}
    partial = tmp_path / "partial.yaml"
    partial.write_text("t1:" + chr(10) + "  cn_name: 名字" + chr(10) + "  desc:" + chr(10), encoding="utf-8")
    assert oc.load_tables_meta(partial) == {"t1": {"cn_name": "名字", "desc": ""}}
    view = oc.build_datasource_view(_biz_meta(), tables_meta={})
    tables = {t["table"]: t for d in view["domains"] for t in d["tables"]}
    assert all(t["tableCnName"] == "" for t in tables.values())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:   # noqa: BLE001 —— 直跑模式聚合报错（pytest 模式各自独立）
            failed += 1
            print(f"[FAIL] {fn.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
