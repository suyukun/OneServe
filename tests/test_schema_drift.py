#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""schema 漂移防护单测（TASK-M2-3.8 / AC-8-b）。

覆盖任务的四个缺口：
  ① 纯逻辑：删列→隔离、改名按删列、增列不触发、状态编解码往返、
     脏值/无键 fail-open（None→不隔离）、引用集提取；
  ② 双通道隔离闸：kv_executor 与 hbase_service 在 quarantined 时同抛
     TagQueryError(503, QUARANTINE_CODE)（AC-8-b「拒查并返明确错误码」）；
  ③ 「增列不泄露」机检：新增列名绝不进响应体/投影结果/状态键值；
  ④ 解除流程与对账脚本纯函数：recovery_decision、check_schema_drift、
     recover_services、load_dict_columns。

被测对象 service/app/schema_drift.py 零第三方依赖；kv/hbase 执行器的 store
经注入，字典假 store 即可覆盖全路径（与 test_kv_executor / test_hbase_executor 同口径）。
"""

import asyncio
import ast
import csv
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO / "sync"))

from app import hbase_service as hbsvc  # noqa: E402
from app import kv_executor as kvx  # noqa: E402
from app import schema_drift as sd  # noqa: E402

from synth_services import load_synth_services, synth_matrix  # noqa: E402  合成 fixture（全下线终态）

SERVICES = load_synth_services()          # 合成注册表（形状＝HEAD 真实条目）
MATRIX = synth_matrix()                   # 合成矩阵
SERVICE_V2 = SERVICES["user_tag_query"]                       # 卡4：service_id 单键
HBASE_SERVICE = SERVICES["usr_basic_info_query"]
WHITELIST = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")["fields"]


def _load_reconcile():
    """加载对账脚本模块（真实入口，非复制实现）。"""
    path = REPO / "scripts" / "reconcile_drift.py"
    spec = importlib.util.spec_from_file_location("reconcile_drift", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


recon = _load_reconcile()

ADDED_PROBE = "zz_added_col_probe_238b"     # 人为「增列」列名（字典多、无人引用）
ADDED_VALUE = "LEAK-SHOULD-NEVER-APPEAR"


# ======================================================================
# 假实现（异步 store 走 executor；同步 fake redis 走对账脚本）
# ======================================================================

class FakeStore:
    """字典假 KV：预置别名/数据键/哈希；覆盖 executor 的异步读口径。"""

    def __init__(self, data=None, hashes=None):
        self.data = dict(data or {})
        self.hashes = dict(hashes or {})

    async def get(self, key):
        return self.data.get(key)

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)


class FakeRedis:
    """字典假 Redis（同步 get/set）：覆盖 check_schema_drift / recover_services。"""

    def __init__(self):
        self.kv = {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value):
        self.kv[key] = value


class FakeHBaseClient:
    """假 HBase 客户端：run_get 鸭子类型（与 test_hbase_executor 同形）。"""

    def __init__(self, row=None):
        self.row = row or {"cf:ac_active_sts_01": "band_C"}
        self.calls = []

    async def run_get(self, client, table, rowkey, columns=None):
        self.calls.append(("get", table, rowkey, list(columns or [])))
        return {c: self.row[c] for c in (columns or []) if c in self.row}


# ======================================================================
# ① 编解码 / 判定纯逻辑
# ======================================================================

def test_state_encode_decode_roundtrip():
    """状态编解码往返：quarantined/ok 两态同值同回，确定性输出便于 diff。"""
    q = sd.make_quarantined(["b", "a", "a"])
    raw = sd.encode_state(q)
    assert raw == '{"state":"quarantined","missing":["a","b"]}'   # 排序去重＋紧凑
    assert sd.decode_state(raw) == q
    ok = sd.make_ok()
    assert sd.decode_state(sd.encode_state(ok)) == ok


def test_decode_dirty_or_none_fail_open():
    """脏值/无键 fail-open：None→None、坏 JSON→None、非 object JSON→None、bytes 可解。"""
    assert sd.decode_state(None) is None
    assert sd.decode_state("{not-json") is None
    assert sd.decode_state("[1,2,3]") is None                     # 非 dict
    assert sd.decode_state('"x"') is None
    assert sd.decode_state(sd.encode_state(sd.make_ok()).encode()) == sd.make_ok()


def test_is_quarantined_fail_open():
    """fail-open 口径：缺 state 键/未知态/None 一律不隔离（不误伤查询）。"""
    assert sd.is_quarantined(None) is False
    assert sd.is_quarantined({}) is False
    assert sd.is_quarantined({"state": "weird"}) is False
    assert sd.is_quarantined({"missing": ["a"]}) is False         # 缺 state 键
    assert sd.is_quarantined(sd.make_quarantined(["a"])) is True


def test_drift_state_key_shape():
    """状态键名：sync:drift:<service_id>（卡4：与注册表单键同构，@version 段已删）。"""
    assert sd.drift_state_key("user_tag_query") == "sync:drift:user_tag_query"
    assert sd.DRIFT_KEY_PREFIX == "sync:drift"


# ======================================================================
# ② 漂移判定三口径
# ======================================================================

def test_compute_drift_delete_triggers():
    """删列：引用了但字典没有 → missing 非空（隔离触发）。"""
    d = sd.compute_drift(["a", "b", "c"], ["a", "c"])
    assert d["missing"] == ["b"] and d["added"] == []


def test_compute_drift_rename_treated_as_delete():
    """改名：旧名进 missing、新名进 added → 按删列处理（隔离），不静默换名。"""
    d = sd.compute_drift(["ac_old_name"], ["ac_new_name"])
    assert d["missing"] == ["ac_old_name"]
    assert d["added"] == ["ac_new_name"]
    assert sd.is_quarantined(sd.make_quarantined(d["missing"])) is True


def test_compute_drift_add_does_not_trigger():
    """增列：字典多出、无人引用 → missing 空、added 有值，但不触发隔离。"""
    d = sd.compute_drift(["a"], ["a", ADDED_PROBE])
    assert d["missing"] == [] and d["added"] == [ADDED_PROBE]
    assert sd.is_quarantined(sd.make_ok()) is False               # 增列不改变状态


def test_quarantine_error_body_shape():
    """503 错误体：机读码＋missing_fields＋人话（逐个列出丢失列）。"""
    body = sd.quarantine_error_body(["b", "a", "a"])
    assert body["error"] == sd.QUARANTINE_CODE
    assert body["missing_fields"] == ["a", "b"]
    assert "a" in body["message"] and "b" in body["message"]
    assert "隔离" in body["message"]


def test_referenced_fields_union_single_key():
    """引用集提取（卡4 单键）：多租户并集、裸 service_id 一条路径、未注册忽略。"""
    services = [{"service_id": "svc"},
                {"service_id": "other"}]
    matrix = [
        {"grants": [{"service": "svc", "fields": ["a"]},
                    {"service": "svc", "fields": ["b"]},          # 同服务第二租户→并集
                    {"service": "ghost", "fields": ["x"]}]},      # 未注册→忽略
        {"grants": [{"service": "svc", "fields": ["c"]}]},
    ]
    refs = sd.referenced_fields(services, matrix)
    assert refs["svc"] == {"a", "b", "c"}                        # 并集
    assert refs["other"] == set()
    assert "ghost" not in refs


def test_field_space_default_wide_and_unknown_falls_back():
    """字段空间：缺省 wide297（向后兼容）；未知值防御式回落 wide297（宁判不误放）。"""
    assert sd.field_space_of({"service_id": "s"}) == sd.FIELD_SPACE_WIDE
    assert sd.field_space_of({"service_id": "s", "field_space": "wide297"}) == "wide297"
    assert sd.field_space_of({"service_id": "s", "field_space": "news"}) == "news"
    assert sd.field_space_of({"service_id": "s", "field_space": "typo"}) == sd.FIELD_SPACE_WIDE


def test_not_applicable_state_roundtrip_and_strictly_not_ok():
    """not_applicable 编解码往返，且与 ok 严格区分（不混同「不适用」与「健康」）。"""
    st = sd.make_not_applicable("news", reason="独立字段空间")
    assert st["state"] == sd.STATE_NOT_APPLICABLE and st["field_space"] == "news"
    assert sd.decode_state(sd.encode_state(st)) == st
    assert sd.is_not_applicable(st) is True
    assert sd.is_quarantined(st) is False                  # 不适用 ≠ 隔离
    ok = sd.make_ok()
    assert ok["state"] != st["state"]                     # 不适用 ≠ 健康
    assert sd.is_not_applicable(ok) is False
    assert sd.is_not_applicable(None) is False


def test_non_wide_spaces_excluded_from_referenced_fields():
    """非宽表字段空间（news/proxy）的语义占位符不得进引用集（假阳性根治机检）。"""
    services = [{"service_id": "wide_svc", "field_space": "wide297"},
                {"service_id": "news_svc", "field_space": "news"},
                {"service_id": "proxy_svc", "field_space": "proxy"},
                {"service_id": "legacy_svc"}]        # 缺省 wide297
    matrix = [{"grants": [
        {"service": "wide_svc", "fields": ["ac_active_sts_01"]},
        {"service": "news_svc", "fields": ["tags"]},                 # 语义占位符
        {"service": "proxy_svc", "fields": ["passthrough"]},         # 语义占位符
        {"service": "legacy_svc", "fields": ["mk_touch_sts_02"]},    # 缺省＝宽表域，仍计入
    ]}]
    refs = sd.referenced_fields(services, matrix)
    assert refs["wide_svc"] == {"ac_active_sts_01"}
    assert refs["news_svc"] == set(), "资讯域占位符混进宽表引用集（假阳性复发）"
    assert refs["proxy_svc"] == set(), "代理域占位符混进宽表引用集（假阳性复发）"
    assert refs["legacy_svc"] == {"mk_touch_sts_02"}, "缺省宽表域被漏判"
    # 即使语义占位符确实不在宽表字典中，也不得产生 missing（无隔离触发）
    d = sd.compute_drift(refs["news_svc"], {"ac_active_sts_01"})
    assert d["missing"] == []


def test_version_machinery_absent_from_schema_drift():
    """卡4 AC-1c 机检：_latest_version/_version_sort_key 零残留（版本归并已删）。"""
    src = (REPO / "service" / "app" / "schema_drift.py").read_text(encoding="utf-8")
    assert "_latest_version" not in src
    assert "_version_sort_key" not in src


# ======================================================================
# ③ 执行器隔离闸（AC-8-b：拒查＋明确错误码）
# ======================================================================

def _demo_store(drift_key=None, drift_value=None):
    """造一个可用 KV：别名/数据键/版本元信息；可选注入漂移状态键。"""
    tags = {"ac_active_sts_01": "band_C", ADDED_PROBE: ADDED_VALUE}
    data = {"tags:user:latest": "20260920",
            "tags:user:20260920:" + "ab" * 32: json.dumps(tags, separators=(",", ":"))}
    hashes = {"sync:ver:20260920": {"data_as_of": "2026-09-20"}}
    if drift_key:
        data[drift_key] = drift_value
    return FakeStore(data, hashes)


def _v2_drift_key():
    return sd.drift_state_key("user_tag_query")


def test_kv_hook_quarantined_raises_503():
    """kv 钩子：状态键 quarantined → TagQueryError(503, QUARANTINE_CODE)。"""
    store = _demo_store(_v2_drift_key(),
                        sd.encode_state(sd.make_quarantined(["ac_active_sts_01"])))
    grant = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")
    try:
        asyncio.run(kvx.execute(store, SERVICE_V2, grant, "ab" * 32))
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == sd.QUARANTINE_CODE
        assert "ac_active_sts_01" in exc.message                  # 人话含丢失列
    else:
        raise AssertionError("quarantined 应 503 拒查")


def test_kv_hook_no_key_or_dirty_fail_open():
    """fail-open：无键 / 脏值 / 非隔离态一律放行（不因基础设施抖动误伤）。"""
    grant = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")
    for raw in (None, "not-json", "[1]", sd.encode_state(sd.make_ok())):
        store = _demo_store(_v2_drift_key(), raw)
        out = asyncio.run(kvx.execute(store, SERVICE_V2, grant, "ab" * 32))
        assert out["found"] is True, f"raw={raw!r} 应放行"


def test_kv_hook_unregistered_fail_open_when_absent():
    """无漂移状态键（None）→ 不隔离：查询照常返回（fail-open 口径）。"""
    grant = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")
    out = asyncio.run(kvx.execute(_demo_store(), SERVICE_V2, grant, "ab" * 32))
    assert out["found"] is True and out["fields"]["ac_active_sts_01"] == "band_C"


def test_hbase_hook_quarantined_raises_503():
    """hbase 钩子：同一状态键口径 → TagQueryError(503, QUARANTINE_CODE)（AC-8-b 双通道）。"""
    key = sd.drift_state_key("usr_basic_info_query")
    store = FakeStore({key: sd.encode_state(sd.make_quarantined(["gender_cd"]))})
    grant = {"tenant": "demo_merchant", "oa_ticket": "x", "fields": {"gender_cd"}}
    try:
        asyncio.run(hbsvc.execute(FakeHBaseClient(), HBASE_SERVICE, grant,
                                  "ab" * 32, store=store))
    except kvx.TagQueryError as exc:
        assert exc.status == 503 and exc.code == sd.QUARANTINE_CODE
    else:
        raise AssertionError("hbase 通道 quarantined 应 503 拒查")


def test_hbase_hook_ok_passes_and_store_none_skips():
    """hbase 钩子：ok 态/未注入 store 均放行（既有直接调用方零改动）。"""
    grant = {"tenant": "demo_merchant", "oa_ticket": "x", "fields": {"ac_active_sts_01"}}
    out = asyncio.run(hbsvc.execute(FakeHBaseClient(), HBASE_SERVICE, grant, "ab" * 32))
    assert out["found"] is True                                   # store=None 跳过闸
    key = sd.drift_state_key("demo_hbase_query")
    store = FakeStore({key: sd.encode_state(sd.make_ok())})
    out2 = asyncio.run(hbsvc.execute(FakeHBaseClient(), HBASE_SERVICE, grant,
                                     "ab" * 32, store=store))
    assert out2["found"] is True and out2["datasource"] == "hbase"


# ======================================================================
# ④ 「增列不泄露」机检
# ======================================================================

def test_added_column_not_leaked_in_response():
    """增列不泄露：新增列即便真有值，也不出现在任何响应体/投影结果里。"""
    grant = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")
    out = asyncio.run(kvx.execute(_demo_store(), SERVICE_V2, grant, "ab" * 32))
    text = json.dumps(out, ensure_ascii=False)
    assert ADDED_PROBE not in text, "新增列名泄露进响应体（AC-8-b 红线）"
    assert ADDED_VALUE not in text, "新增列值泄露进响应体（AC-8-b 红线）"
    assert set(out["fields"]) <= WHITELIST, "投影结果混入白名单外字段"


def test_added_column_not_leaked_in_drift_state_or_projection():
    """增列不泄露：字典新增列只进报告计数，绝不写进状态键值；投影交集语义不变。"""
    refs = {"user_tag_query": set(WHITELIST)}
    dict_cols = set(WHITELIST) | {ADDED_PROBE}
    r = FakeRedis()
    report = recon.check_schema_drift(r, refs, dict_cols)
    key = sd.drift_state_key("user_tag_query")
    assert ADDED_PROBE not in r.get(key)                          # 状态键值无新列名
    assert ADDED_PROBE not in json.dumps(report, ensure_ascii=False)
    assert report["user_tag_query"]["added_count"] == 1          # 仅计数
    assert report["user_tag_query"]["state"] == sd.STATE_OK      # 增列不触发隔离
    # 投影白名单交集：新列即便在 tags 里也不出现
    tags = {ADDED_PROBE: ADDED_VALUE, "ac_active_sts_01": "band_C"}
    proj = kvx.project(tags, WHITELIST)
    assert ADDED_PROBE not in proj and proj == {"ac_active_sts_01": "band_C"}


# ======================================================================
# ⑤ 对账脚本纯函数 + 解除流程
# ======================================================================

def _temp_csv(columns):
    """写一份只含「字段名」结构列（＋干扰业务列）的字典真源临时 CSV。"""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with open(fd, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["attnum", "字段名", "数据类型", "cn_name", "业务口径", "敏感级"])
        for i, c in enumerate(columns, 1):
            wr.writerow([i, c, "text", f"备注{i}", "业务口径", "内部"])
    return path


def test_load_dict_columns_only_field_name():
    """字典字段集：只取「字段名」结构列，业务列内容变化不参与（列数无关）。"""
    path = _temp_csv(["a", "b", "c"])
    try:
        assert recon.load_dict_columns(path) == {"a", "b", "c"}
    finally:
        Path(path).unlink()


def test_check_schema_drift_writes_quarantine_state():
    """对账主体：删列 → 写 quarantined 状态键（missing 非空）。"""
    r = FakeRedis()
    refs = {"svc": {"a", "b"}}
    report = recon.check_schema_drift(r, refs, {"a"})
    assert report["svc"] == sd.make_quarantined(["b"])
    raw = r.get(sd.drift_state_key("svc"))
    assert sd.decode_state(raw)["state"] == sd.STATE_QUARANTINED


def test_check_schema_drift_not_applicable_for_non_wide_space():
    """对账主体：非宽表字段空间 → 写 not_applicable（不比对、不隔离、不当 ok）。"""
    r = FakeRedis()
    refs = {"wide_svc": {"a"}, "news_svc": set(), "proxy_svc": set()}
    spaces = {"wide_svc": "wide297", "news_svc": "news", "proxy_svc": "proxy"}
    report = recon.check_schema_drift(r, refs, {"a"}, spaces)
    assert report["wide_svc"]["state"] == sd.STATE_OK
    assert report["news_svc"]["state"] == sd.STATE_NOT_APPLICABLE
    assert report["proxy_svc"]["state"] == sd.STATE_NOT_APPLICABLE
    for sid in ("news_svc", "proxy_svc"):
        raw = sd.decode_state(r.get(sd.drift_state_key(sid)))
        assert raw["state"] == sd.STATE_NOT_APPLICABLE
        assert sd.is_quarantined(raw) is False         # 不适用态绝不触发执行器 503


def test_check_schema_drift_ok_writes_ok_state():
    """对账主体：全命中 → 写 ok 状态键（幂等覆盖）。"""
    r = FakeRedis()
    report = recon.check_schema_drift(r, {"svc": {"a"}}, {"a", "extra"})
    assert report["svc"]["state"] == sd.STATE_OK
    assert sd.decode_state(r.get(sd.drift_state_key("svc")))["state"] == sd.STATE_OK


def test_recover_decision_restored_clears():
    """解除①：人工确认 restored=True → 无条件转 ok（清隔离）。"""
    raw = sd.encode_state(sd.make_quarantined(["a"]))
    d = sd.recovery_decision(raw, True)
    assert d["action"] == "write" and d["state"] == sd.make_ok()
    assert d["reason"] == "restored_confirmed"


def test_recover_decision_machine_check():
    """解除②：未声明 restored → 仅当 missing 全部回归真源才转 ok，否则维持隔离。"""
    raw = sd.encode_state(sd.make_quarantined(["a", "b"]))
    d = sd.recovery_decision(raw, False, ["a"])
    assert d["state"] == sd.make_quarantined(["b"])                # 仍缺 b → 维持隔离
    assert d["reason"] == "missing_columns_remain" and d["still_missing"] == ["b"]
    d2 = sd.recovery_decision(raw, False, ["a", "b"])
    assert d2["state"] == sd.make_ok() and d2["reason"] == "all_missing_back"


def test_recover_decision_no_key_or_ok_noop():
    """解除边界：无键/非隔离态不写（补 ok 键会掩盖巡检缺口）。"""
    assert sd.recovery_decision(None, False)["action"] == "none"
    assert sd.recovery_decision(None, True)["action"] == "none"    # 无键也不凭空补
    assert sd.recovery_decision(sd.encode_state(sd.make_ok()), False)["action"] == "none"


def test_recover_services_roundtrip():
    """对账解除子命令：quarantined→(恢复真源)→ok，且幂等；未恢复则维持隔离。"""
    r = FakeRedis()
    refs = {"svc": {"a", "b"}}
    recon.check_schema_drift(r, refs, {"a"})                       # 删 b → quarantined
    key = sd.drift_state_key("svc")
    # 未恢复：机器复核 b 仍缺 → 维持隔离
    rep = recon.recover_services(r, refs, {"a"})
    assert rep["svc"]["state"]["state"] == sd.STATE_QUARANTINED
    assert sd.decode_state(r.get(key))["state"] == sd.STATE_QUARANTINED
    # 恢复 b：机器复核通过 → ok
    rep2 = recon.recover_services(r, refs, {"a", "b"})
    assert rep2["svc"]["state"] == sd.make_ok()
    assert sd.decode_state(r.get(key))["state"] == sd.STATE_OK
    # 幂等：再来一次仍是 ok
    assert recon.recover_services(r, refs, {"a", "b"})["svc"]["action"] == "none"


def test_recover_services_restored_override():
    """对账解除子命令：--restored 人工确认 → 无条件清隔离（覆盖写 ok）。"""
    r = FakeRedis()
    refs = {"svc": {"a", "b"}}
    recon.check_schema_drift(r, refs, {"a"})
    rep = recon.recover_services(r, refs, {"a"}, restored=True)
    assert rep["svc"]["reason"] == "restored_confirmed"
    assert sd.decode_state(r.get(sd.drift_state_key("svc")))["state"] == sd.STATE_OK


# ======================================================================
# ⑥ 全链路：删列→隔离→恢复→放行
# ======================================================================

def test_end_to_end_delete_quarantine_then_recover():
    """集成：真源删列→对账隔离→执行器 503→真源恢复→对账转 ok→执行器放行。"""
    full_cols = set(WHITELIST) | {ADDED_PROBE}
    path = _temp_csv(sorted(full_cols - {"ac_active_sts_01"}))     # 人为删列
    try:
        refs = sd.referenced_fields(list(SERVICES.values()), MATRIX)
        dict_cols = recon.load_dict_columns(path)
        r = FakeRedis()
        report = recon.check_schema_drift(r, refs, dict_cols)
        assert "user_tag_query" in report and \
            report["user_tag_query"]["state"] == sd.STATE_QUARANTINED
        assert report["user_tag_query"]["missing"] == ["ac_active_sts_01"]

        # 执行器读同一状态键 → 503 明确错误码
        store = _demo_store(_v2_drift_key(),
                            r.get(sd.drift_state_key("user_tag_query")))
        grant = kvx.tenant_grant(MATRIX, "user_tag_query", tenant="demo_merchant")
        try:
            asyncio.run(kvx.execute(store, SERVICE_V2, grant, "ab" * 32))
        except kvx.TagQueryError as exc:
            assert exc.status == 503 and exc.code == sd.QUARANTINE_CODE
        else:
            raise AssertionError("删列后应隔离拒查")

        # 恢复真源 → 对账 --recover → ok
        path2 = _temp_csv(sorted(full_cols))
        try:
            recon.recover_services(r, refs, recon.load_dict_columns(path2))
        finally:
            Path(path2).unlink()
        assert sd.decode_state(
            r.get(sd.drift_state_key("user_tag_query")))["state"] == sd.STATE_OK
        out = asyncio.run(kvx.execute(_demo_store(), SERVICE_V2, grant, "ab" * 32))
        assert out["found"] is True                                # 放行
    finally:
        Path(path).unlink()


# ======================================================================
# ⑦ 真实配置硬回归（本 bug 的根治防线）
# ======================================================================

def test_real_registry_against_real_dict_zero_false_quarantine():
    """硬回归：真 services.yaml ＋ 真 auth_matrix.yaml ＋ 真字段字典 CSV 跑对账。

    这是本假阳性 bug 的根治防线——若早写此用例，tags/passthrough 被误判删列
    根本出不来。断言（按字段空间新语义）：
      - 全部已注册服务无一被误隔离（quarantined 为空）；
      - 宽表域服务（user_tag_query）＝ ok；
      - 非宽表域（news_tag_query=资讯独立字段空间、partner_proxy=B 型整体透传）
        ＝ not_applicable（如实「不适用」，既不冒充 ok、更不得 quarantined）。
    """
    entries = list(SERVICES.values())
    refs = sd.referenced_fields(entries, MATRIX)
    spaces = sd.field_space_by_service(entries)
    dict_cols = recon.load_dict_columns()          # 真 sync/schema_usr_label_wide_297.csv
    r = FakeRedis()                                # 假 redis 只为观察写出的状态值
    report = recon.check_schema_drift(r, refs, dict_cols, spaces)

    quarantined = sorted(k for k, s in report.items() if s["state"] == sd.STATE_QUARANTINED)
    assert quarantined == [], f"真实配置下出现误隔离（假阳性回归）：{quarantined}"
    assert report["user_tag_query"]["state"] == sd.STATE_OK
    assert report["usr_basic_info_query"]["state"] == sd.STATE_NOT_APPLICABLE   # 业务表字典域（多表比对待接入）
    assert report["news_tag_query"]["state"] == sd.STATE_NOT_APPLICABLE
    assert report["partner_proxy"]["state"] == sd.STATE_NOT_APPLICABLE
    # 状态键落盘值同口径（执行器据此放行）：news/proxy 绝不能被写成 quarantined
    for sid in ("news_tag_query", "partner_proxy"):
        raw = sd.decode_state(r.get(sd.drift_state_key(sid)))
        assert raw["state"] == sd.STATE_NOT_APPLICABLE
        assert sd.is_quarantined(raw) is False


def test_real_services_declare_field_space_explicitly():
    """真实注册表每个条目显式声明 field_space（防漏声明退化成缺省宽表域而误判）。"""
    for sid, svc in SERVICES.items():
        assert svc.get("field_space") in sd.FIELD_SPACES, \
            f"{sid} 未显式声明合法 field_space"


# ======================================================================
# ⑧ 演练脚本护栏（真 Redis 链路，不再假 store 顶替）
# ======================================================================

def test_drill_script_runs_real_redis_not_fake_fallback():
    """护栏：演练脚本必须走真 Redis 受控链路，且不含「Docker 不可用→假 store」退化成主路径。

    本 bug 的漏检直接原因＝前一位开发者误判 Docker 不可用而退化成假 store 演练。
    断言：脚本含真 Redis 容器起停（docker run ... redis）与 CLI 全链路调用；
    且不存在把假 store 当环境的退化分支。
    """
    src = (REPO / "scripts" / "drill_schema_drift.sh").read_text(encoding="utf-8")
    assert "docker" in src and "redis" in src, "演练脚本缺真 Redis 容器链路"
    assert "reconcile_drift.py" in src, "演练脚本未调用真对账 CLI"
    # 不得存在假 store 类/实例充当链路（根因是假 store 演练盖住了真缺陷）
    assert "class FakeStore" not in src and "FakeStore(" not in src, \
        "演练脚本仍以假 store 充当链路（本 bug 漏检根因）"
    # Docker 不可用分支必须如实失败（exit 2），不得静默降级成假链路后报绿
    assert "exit 2" in src, "Docker 不可用时应如实退出 2，不得降级假链路"
    assert not re.search(r"退化为服务层|服务层演练", src), "演练脚本仍保留服务层假链路退化分支"


# ======================================================================
# ⑨ 薄胶水机检（宿主机无 fastapi，AST 契约口径同 test_quota_usage）
# ======================================================================

def _unparse_func(path, name):
    src = (REPO / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{path} 缺函数 {name}")


def test_main_hbase_branch_passes_drift_store():
    """main._run_query：hbase 分支注入 store（否则 HBase 通道无隔离闸）。"""
    src = _unparse_func("service/app/main.py", "_run_query")
    assert "hbsvc.execute" in src and "store=" in src, "hbase 查询未注入漂移 store"


def test_hbase_service_execute_has_drift_gate():
    """hbase_service.execute：签名带 store 且委托 kv_executor.check_drift_quarantine。"""
    src = _unparse_func("service/app/hbase_service.py", "execute")
    assert "store" in src and "check_drift_quarantine" in src


def test_reconcile_cli_exposes_recover_flags():
    """对账脚本 CLI：--recover/--restored 子命令存在，且 recover_services 函数在。"""
    src = (REPO / "scripts/reconcile_drift.py").read_text(encoding="utf-8")
    assert "--recover" in src and "--restored" in src
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert {"recover_services", "check_schema_drift", "load_dict_columns"} <= names


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== schema 漂移防护单测（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)}；失败：{'、'.join(failed)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
