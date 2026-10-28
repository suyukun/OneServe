#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK-M2-4 发布台后端 API 套件（tests/test_admin_publish_api.py）——AC-4/6/7 机检面。

判据映射（门槛稿 AC-4／AC-6／AC-7／AC-8-a）：
  AC-4 数据源发现：HBase meta（namespace/表/列族/列）× 字段字典 CSV 双源校验，
    按业务域目录树分组展示；HBase 不可达＝如实 503（不退化成假数据）；
  AC-6 发布 hbase 型服务：backend.source=hbase＋backend.hbase{table,cf,key_type}＋
    field_space=wide297（ADR-0004）；加载期校验（字段∈297 列）写状态键之前拦下；
    运行时 services: 列表落条目＝加载器认它（可调前提）；同表重复发布＝service_id＋
    version 复合键新版本、旧版不覆盖（AC-8-a）；渲染器为其生成通配路由（零代码）；
  AC-7 校验拒绝：坏路由/键类型未定/必填缺失 → 拒绝且
    services.yaml 零变更（逐字节）；2026-09-29 起敏感概念移除，脱敏=手动显式配置。

HBase 未起容器时用 mock 连接池（照 tests/test_hbase_executor.py 既有 mock 风格）；
真实可达时的真链路证据归 scripts/m24_publish_e2e.sh（tests/evidence/ 留档）。

被测对象 service/app/ops_config.py（纯逻辑）＋service/app/hbase_executor.py 元数据发现；
不 import FastAPI（HTTP 编排以源码机检，端到端归 e2e 脚本）。
"""

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import hbase_executor as hbx        # noqa: E402
from app import kv_executor as kvx           # noqa: E402
from app import ops_config as oc             # noqa: E402
from app import registry_render as rend     # noqa: E402

REAL_SERVICES = REPO / "service" / "services.yaml"


# ======================================================================
# mock 连接池（happybase API 形状；不依赖容器）
# ======================================================================

class FakeTable:
    """假 Table：happybase Table.families()/scan() 形状。"""

    def __init__(self, families=None, rows=None):
        self._families = families or [b"cf"]
        self._rows = rows or []

    def families(self):
        return self._families

    def scan(self, **kwargs):
        limit = kwargs.get("limit", len(self._rows))
        return self._rows[:limit]


class FakeConnection:
    def __init__(self, tables=None, families=None, rows=None):
        self._tables = tables or []
        self._families = families
        self._rows = rows
        self.calls = []

    def tables(self):
        self.calls.append(("tables",))
        if isinstance(self._tables, Exception):
            raise self._tables
        return self._tables

    def table(self, name):
        self.calls.append(("table", name))
        if isinstance(self._families, Exception):
            raise self._families
        return FakeTable(self._families, self._rows)


class FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.acquired = 0

    def connection(self):
        self.acquired += 1
        return _Ctx(self.conn)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _client(conn):
    return hbx.HBaseClient(FakePool(conn))


def _services_copy() -> Path:
    """发布管线基板：真 services.yaml 头注释＋一个最小运行时服务条目。

    全下线终态下真文件是 services: []（真加载器 fail-loud），发布后的注册表
    断言需要非空基板——合成种子条目充当（与线上状态解耦，形状＝真实条目）。
    """
    d = Path(tempfile.mkdtemp())
    p = d / "services.yaml"
    real = REAL_SERVICES.read_text(encoding="utf-8")
    head = real.split("services:")[0] + "services:" + chr(10)
    seed = (
        "  - service_id: seed_kv_query            # 测试基板合成条目（非线上服务）" + chr(10) +
        "    version: v1" + chr(10) +
        "    archetype: kv_lookup" + chr(10) +
        "    backend:" + chr(10) +
        "      kv:" + chr(10) +
        "        namespace: tags:seed" + chr(10) +
        "        alias_key: tags:seed:latest" + chr(10) +
        "        data_key: 'tags:seed:{version}:{sm3}'" + chr(10) +
        "        version_meta_key: 'sync:ver:seed:{version}'" + chr(10) +
        "        storage_key: as_received" + chr(10) +
        "        entity: seed" + chr(10) +
        "    params:" + chr(10) +
        "      - name: key" + chr(10) +
        "        type: string(1..128)" + chr(10) +
        "        key_type: seed_key" + chr(10) +
        "        pattern: '^[A-Za-z0-9_.-]{1,128}$'" + chr(10) +
        "    response:" + chr(10) +
        "        projection: auth_matrix" + chr(10) +
        "        meta: [data_as_of, live_version]" + chr(10) +
        "    guard:" + chr(10) +
        "        timeout_ms: 3000" + chr(10) +
        "        max_items_per_call: 1" + chr(10) +
        "    audit:" + chr(10) +
        "        enabled: true" + chr(10) +
        "        log_params: hash_only" + chr(10) +
        "        log_response_digest: sha256_of_desensitized_body" + chr(10)
    )
    p.write_text(head + seed, encoding="utf-8")
    return p


def _hbase_fields(names, sample="x"):
    return [{"name": n, "sample": sample, "desc": ""} for n in names]


# ======================================================================
# AC-4：HBase meta 发现（mock 连接池）
# ======================================================================

def test_discover_lists_namespace_table_family_column():
    """discover：namespace→表→列族→列 逐层可读（HBase meta 真源；列由样本行反推）。"""
    conn = FakeConnection(
        tables=[b"default:oneserve_demo", b"ods:usr_label_wide"],
        families=[b"cf", b"meta"],
        rows=[(b"row-1", {b"cf:ac_active_sts_01": b"band_C",
                         b"cf:mk_touch_sts_02": b"lvl_2",
                         b"meta:src": b"dw"})])
    out = _client(conn).discover()
    assert out["table_count"] == 2
    ns = out["namespaces"]
    assert set(ns) == {"default", "ods"}
    assert set(ns["default"]) == {"oneserve_demo"}
    assert set(ns["default"]["oneserve_demo"]["families"]) == {"cf", "meta"}
    assert ns["default"]["oneserve_demo"]["families"]["cf"] == ["ac_active_sts_01", "mk_touch_sts_02"]
    assert ns["default"]["oneserve_demo"]["families"]["meta"] == ["src"]


def test_discover_unreachable_is_honest_503():
    """HBase 不可达 → 503/HBASE_UNAVAILABLE（如实报错，不静默回退假数据）。"""
    client = _client(FakeConnection(tables=RuntimeError("conn refused")))
    try:
        client.discover()
        raise AssertionError("不可达须抛 HBaseQueryError")
    except hbx.HBaseQueryError as exc:
        assert exc.status == 503 and exc.code == "HBASE_UNAVAILABLE"
        assert "不可达" in exc.message


def test_discover_without_colon_table_falls_to_default_namespace():
    """无 namespace 前缀的表名归 default（HBase 表名口径）。"""
    conn = FakeConnection(tables=[b"plain_table"], families=[b"cf"], rows=[])
    out = _client(conn).discover()
    assert set(out["namespaces"]["default"]) == {"plain_table"}
    assert out["namespaces"]["default"]["plain_table"]["families"] == {"cf": []}


def test_datasource_view_m25_contract_namespace_table_family_columns():
    """AC-4（M2-5 契约）：namespace→表→列族→列树；列来自字段字典 CSV（meta 无列级 API）。"""
    meta = {"namespaces": {"default": {"oneserve_demo": {"families": {
        "cf": ["ac_active_sts_01"]}}}}, "table_count": 1}
    view = oc.build_datasource_view(meta)
    assert view["datasource"] == "hbase" and view["hbase"]["reachable"] is True
    assert view["checkedAt"]                                     # 校验时点（双源校验时间）
    assert view["dict"]["columnCount"] == 297
    assert [d["id"] for d in view["domains"]] == ["default"]
    tbl = view["domains"][0]["tables"][0]
    assert tbl["namespace"] == "default" and tbl["table"] == "oneserve_demo"
    assert tbl["id"] == "oneserve_demo" and tbl["path"] == "oneserve_demo"
    assert set(tbl["keyTypes"]) == {"phone_sm3", "usr_id"}
    assert tbl["keySamples"]["phone_sm3"] == ""                  # P0-1：无 scan 样本＝留空（不造演示键）
    assert tbl["keySamples"]["usr_id"] == ""                     # 真库示例须运营者填，不编造
    fams = tbl["families"]
    assert [f["name"] for f in fams] == ["cf"]
    assert any(c["sampleSeen"] for c in fams[0]["columns"])     # 真库实见列如实标注
    cols = {c["name"]: c for c in fams[0]["columns"]}
    assert len(cols) == 296                                      # 297 列减键列 usr_id
    assert cols["ac_active_sts_01"]["cf"] == "cf"
    assert cols["ac_active_sts_01"]["cnName"] and cols["ac_active_sts_01"]["type"]
    assert cols["ac_active_sts_01"]["sensitive"] in ("公开", "内部", "敏感")
    assert "usr_id" not in cols                                  # 键列不入出参字段表


def test_collect_rowkey_samples_scan_paths():
    """P0-1 示例键真源：有数据＝真 rowkey；空库/无表/单表失败＝空＋如实标注（绝不造假键）。"""
    meta = {"namespaces": {"default": {"oneserve_demo": {"families": {"cf": []}}},
                           "ods": {"usr_label_wide": {"families": {"cf": []}}}},
            "table_count": 2}

    class _Ok:                                  # 有数据：每表 scan(limit=1) 实取
        def scan(self, table, limit=None, **kw):
            assert limit == 1
            return [{"row": f"rk-{table}", "columns": {"cf:x": "1"}}]

    samples, note = oc.collect_rowkey_samples(meta, _Ok())
    assert samples == {"oneserve_demo": "rk-oneserve_demo",
                       "ods:usr_label_wide": "rk-ods:usr_label_wide"}
    assert "scan(limit=1)" in note and "2/2" in note

    class _Empty:                               # 空库：scan 空 → 空＋空库原因
        def scan(self, table, limit=None, **kw):
            return []

    samples2, note2 = oc.collect_rowkey_samples(meta, _Empty())
    assert samples2 == {} and "空库" in note2

    class _Half:                                # 单表失败：原因入注，不拖垮其它表
        def scan(self, table, limit=None, **kw):
            if ":" in table:
                raise RuntimeError("thrift broken")
            return [{"row": "rk-live", "columns": {}}]

    samples3, note3 = oc.collect_rowkey_samples(meta, _Half())
    assert samples3 == {"oneserve_demo": "rk-live"}
    assert "thrift broken" in note3

    samples4, note4 = oc.collect_rowkey_samples({"namespaces": {}, "table_count": 0}, _Ok())
    assert samples4 == {} and "无表" in note4    # 无表：如实标注


def test_datasource_view_rowkey_samples_honest_paths():
    """P0-1 发现视图：示例键＝scan 真键或留空＋原因；readonly 放开（可粘贴真键）。

    验收两路径（mock 注入，不依赖真库）：有数据→真 rowkey；空库→空＋标注。"""
    meta = {"namespaces": {"default": {"oneserve_demo": {"families": {"cf": []}}}},
            "table_count": 1}
    # 路径 1（有数据）：真 rowkey 进 phone_sm3 槽；usr_id 明文示例仍不编造
    v = oc.build_datasource_view(meta, rowkey_samples={"oneserve_demo": "ab12cd34ef"},
                                 sample_key_note="示例键来自真库 scan(limit=1)：1/1 表取到")
    tbl = v["domains"][0]["tables"][0]
    assert tbl["keySamples"]["phone_sm3"] == "ab12cd34ef"
    assert tbl["keySamples"]["usr_id"] == ""
    assert v["key"]["sampleKey"] == "ab12cd34ef"
    assert v["key"]["sampleKeySource"] == "hbase-scan" and v["key"]["readonly"] is False
    assert v["key"]["sampleUsrId"] == ""            # 假用户演示键不再出现
    # 路径 2（空库）：键留空＋原因标注（不造键、可粘贴）
    v2 = oc.build_datasource_view(
        meta, rowkey_samples={},
        sample_key_note="示例键留空：空库（scan(limit=1) 未取到任何行），可粘贴真实 rowkey 试查")
    tbl2 = v2["domains"][0]["tables"][0]
    assert tbl2["keySamples"]["phone_sm3"] == "" and tbl2["keySamples"]["usr_id"] == ""
    assert v2["key"]["sampleKey"] == "" and v2["key"]["sampleKeySource"] == "none"
    assert "空库" in v2["key"]["note"] and "粘贴" in v2["key"]["note"]
    # 旧假键绝不回流（SM3(DEMO-USR-0001) 字面值与假用户号）
    for view in (v, v2):
        assert "291eded30fd68c4cf423fe14bb7d53e5c076de2494d7f5fa2a21abf2649de96f" not in str(view)
        assert "DEMO-USR-0001" not in str(view)


def test_datasource_view_dual_source_mismatch_blocks():
    """AC-4：HBase 有字典外列 → dualSource.status=mismatch＋mismatches[]（发布拦截依据）。"""
    meta = {"namespaces": {"default": {"t": {"families": {"cf": ["ghost_col"]}}}},
            "table_count": 1}
    view = oc.build_datasource_view(meta)
    ds = view["dualSource"]
    assert ds["status"] == "mismatch" and ds["unknownColumns"] == ["ghost_col"]
    assert ds["mismatches"] and ds["mismatches"][0]["field"] == "ghost_col"
    # 两源一致（无字典外列）→ status=ok
    ok_view = oc.build_datasource_view({"namespaces": {"default": {"t": {"families": {"cf": []}}}},
                                        "table_count": 1})
    assert ok_view["dualSource"]["status"] == "ok" and ok_view["dualSource"]["mismatches"] == []


def test_selftest_assertions_reachable_and_unreachable():
    """AC-5 机检：可达时断言全绿；不可达时 hbase-reachable/dual-source 如实标红（不谎报）。"""
    meta = {"namespaces": {"default": {"t": {"families": {"cf": []}}}}, "table_count": 1}
    good = oc.hbase_selftest_assertions(meta, reachable=True)
    assert {a["id"] for a in good} >= {"dict-present", "hbase-reachable", "dual-source",
                                       "domain-tree", "reject-not-in-dict", "route-render"}
    assert all(a["ok"] for a in good), [a for a in good if not a["ok"]]
    bad = oc.hbase_selftest_assertions(None, reachable=False, reach_error="conn refused")
    by_id = {a["id"]: a for a in bad}
    assert by_id["hbase-reachable"]["ok"] is False
    assert by_id["dual-source"]["ok"] is False and "不可达" in by_id["dual-source"]["detail"]
    assert by_id["dict-present"]["ok"] is True                   # 纯逻辑断言照跑，不因不可达跳过


def test_verify_datasource_blocks_unknown_hbase_column():
    """AC-4 双源校验：HBase 有而字典无 → ok=false＋unknownColumns（拦截依据）。"""
    dict_cols = {"ac_active_sts_01": {"sensitivity": "内部"}}
    meta = {"namespaces": {"default": {"t": {"families": {"cf": ["ac_active_sts_01", "ghost"]}}}}}
    v = oc.verify_datasource(meta, dict_cols)
    assert v["ok"] is False and v["unknown_columns"] == ["ghost"]
    assert v["dict_not_in_meta"] == [] and v["dict_present"] is True


def test_verify_datasource_missing_dict_is_not_ok():
    """字典真源不可读 → ok=false（宁拦不放；不谎称一致，not_applicable≠ok）。"""
    v = oc.verify_datasource({"namespaces": {}, "table_count": 0}, {})
    assert v["ok"] is False and v["dict_present"] is False


def test_verify_datasource_consistent_columns_ok():
    """两源一致（HBase 列全 ∈ 字典）→ ok=true；字典列未被样本覆盖只作如实提示。"""
    dict_cols = {f"c{i}": {"sensitivity": "内部"} for i in range(3)}
    meta = {"namespaces": {"default": {"t": {"families": {"cf": ["c0"]}}}}}
    v = oc.verify_datasource(meta, dict_cols)
    assert v["ok"] is True and v["unknown_columns"] == []
    assert v["dict_not_in_meta"] == ["c1", "c2"]        # 如实：样本未见（非不存在）


# ======================================================================
# AC-6：发布 hbase 型服务（写区＋运行时注册区）
# ======================================================================

def test_publish_hbase_lands_redis_entry_with_field_space():
    """本卡（ADR-0005）：HBase 选表发布产物＝**加载后 Redis 型**（backend.kv），
    **不再落 backend.source=hbase**；保留 field_space=wide297（ADR-0004）＋backend.load 来源留痕。"""
    p = _services_copy()
    entry, before = oc.publish_service(
        p, name="HBase 宽表标签点查", route="/v1/svc/hbase-labels",
        fields=_hbase_fields(["ac_active_sts_01"]),
        datasource="hbase",
        hbase={"table": "default:oneserve_demo", "cf": "cf", "key_type": "phone_sm3"},
        load_version="20260923",
        operator="admin@oneserve", oa_ticket="OA-TEST-M24", now=oc.datetime(2026, 9, 23, tzinfo=oc.timezone.utc))
    assert before is None
    # 产物是 Redis 型（datasource 回显 redis；非 hbase）
    assert entry["datasource"] == "redis" and entry["fieldSpace"] == "wide297"
    assert entry["keyType"] == "phone_sm3"
    assert entry["loadSource"] == {"table": "default:oneserve_demo", "cf": "cf",
                                   "keyType": "phone_sm3", "version": "20260923"}

    reg = kvx.load_services(p)
    rt = reg["v1-svc-hbase-labels"]                       # latest 指针可查＝加载器认它
    assert "hbase" not in rt["backend"], "发布产物不得再落 HBase 直连后端（ADR-0005）"
    assert rt["backend"].get("source", "redis") == "redis"   # 缺省 source＝redis 向后兼容
    assert rt["backend"]["kv"] == {
        # 按表隔离（2026-09-29）：发布条目键空间随源表（oneserve_demo → tags:oneserve_demo:*）
        "namespace": "tags:oneserve_demo", "alias_key": "tags:oneserve_demo:latest",
        "data_key": "tags:oneserve_demo:{version}:{sm3}",
        "version_meta_key": "sync:ver:oneserve_demo:{version}",
        "storage_key": "as_received", "entity": "user"}
    # 来源留痕（执行器不读）：从哪张 HBase 表加载、落在哪个 Redis 版本空间
    assert rt["backend"]["load"]["table"] == "default:oneserve_demo"
    assert rt["backend"]["load"]["version"] == "20260923"
    assert rt["field_space"] == "wide297"                 # ADR-0004 硬要求
    # 2026-09-23 维护者 口径：hbase 型键模式也放宽为默认宽松模式
    assert rt["params"][0]["pattern"] == "^[A-Za-z0-9_.-]{1,128}$"
    assert rt["trace"]["oa_ticket"] == "OA-TEST-M24"       # 操作者＋OA 单号引用
    assert rt["trace"]["commit"] is None                   # git 留痕占位：不伪造 hash
    # 端到端可调前提（kv_executor 认 backend.kv）：注册条目不带 source 亦按 redis 分发
    assert rt["archetype"] == "kv_lookup" and rt["response"]["projection"] == "auth_matrix"


def test_publish_product_is_never_hbase_typed():
    """本卡核心判据：发布产物不是 HBase 直连型（backend.source=hbase 不出现在任何发布产物）。

    build_hbase_entry 保留代码（M3 候选）但**不在发布链路被调用**——机检发布入口只调
    build_redis_entry_from_hbase。"""
    src = (REPO / "service" / "app" / "ops_config.py").read_text(encoding="utf-8")
    body = src[src.index("def publish_service("):]
    assert "build_redis_entry_from_hbase(" in body, "发布链路未走 Redis 型构造"
    assert "build_hbase_entry(" not in body.split("def offline_service(")[0], \
        "发布链路不得再调 build_hbase_entry（HBase 直连保留代码归 M3）"
    assert "build_hbase_entry" in src, "HBase 直连构造能力须保留代码（维护者：已做的保留不删）"


def test_publish_hbase_preserves_runtime_comments_and_entries():
    """纯追加：运行时注册区既有条目与全部注释逐字节保留（写区外零接触）。"""
    p = _services_copy()
    orig = p.read_text(encoding="utf-8")
    oc.publish_service(p, name="n", route="/v1/svc/hbase-x",
                       fields=_hbase_fields(["ac_active_sts_01"]), datasource="hbase",
                       hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
    text = p.read_text(encoding="utf-8")
    head_now = text.split(oc.CONSOLE_MARKER)[0]
    head_orig = orig.split(oc.CONSOLE_MARKER)[0] if oc.CONSOLE_MARKER in orig else orig
    # 既有注释逐条仍在（顶格注释未被吞、未被重排掉）
    # 锚点自相对（2026-09-29 全下线后历史注释已随清理改写，硬编码历史锚点会漂移；
    # 「纯追加零接触」不变式本身不变）：取基板头部真实顶格注释行作锚。
    import re as _re
    anchors = [ln for ln in head_orig.splitlines()
               if ln.startswith("#") and len(ln.strip()) > 20][:3]
    assert anchors, "基板无注释可守（异常态）"
    for anchor in anchors:
        assert anchor in head_now, f"运行时区注释丢失：{anchor}"
    assert head_now.startswith(head_orig.rstrip("\n"))     # 既有文本是前缀（纯追加）


def test_publish_hbase_requires_field_in_dict_297():
    """AC-6 加载期校验：字段不在字典真源 297 列 → 拒绝（写状态键之前拦下）＋零变更。"""
    p = _services_copy()
    before_text = p.read_text(encoding="utf-8")
    try:
        oc.publish_service(p, name="n", route="/v1/svc/hbase-bad",
                           fields=_hbase_fields(["not_a_real_col"]), datasource="hbase",
                           hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
        raise AssertionError("字典外字段应拒")
    except oc.OpsConfigError as exc:
        assert exc.code == "field_not_in_dict" and "297" in exc.message
    assert p.read_text(encoding="utf-8") == before_text


def test_publish_hbase_sensitivity_level_drives_nothing():
    """2026-09-29 裁决机检：敏感级零消费——usr_id（宽表字典敏感级=敏感）未配置时
    发布原样通过（零自动 band_mask、零拦截）；显式 desensitize 原样保留真实生效。"""
    p = _services_copy()
    hb = {"table": "t", "cf": "cf", "key_type": "phone_sm3"}
    # 未配置 → 敏感级不驱动任何行为：字段 desensitize 落空、运行时条目无脱敏规则
    oc.publish_service(p, name="n", route="/v1/svc/hbase-plain",
                       fields=_hbase_fields(["usr_id"]), datasource="hbase", hbase=hb)
    f = oc.load_console_services(p)[0]["fields"][0]
    assert f["desensitize"] == "", f"敏感级不得再自动配脱敏，实得 {f['desensitize']!r}"
    rt = kvx.load_services(p)["v1-svc-hbase-plain"]
    assert not rt["response"].get("desensitize"), "无配置时 response.desensitize 必须为空"
    # 直调校验同样零敏感拦截（字典外字段仍拒——AC-6 主体不变）
    oc.validate_against_dict([{"name": "usr_id", "desensitize": ""}], oc.load_field_dict())
    # 显式配置优先：自定义标记原样保留；band_mask 落可执行规则（手动显式能力保留）
    oc.publish_service(p, name="n", route="/v1/svc/hbase-explicit",
                       fields=[{"name": "usr_id", "sample": "x", "desc": "",
                                "desensitize": "my_custom_mask"}],
                       datasource="hbase", hbase=hb)
    f2 = oc.load_console_services(p)[1]["fields"][0]
    assert f2["desensitize"] == "my_custom_mask"


def test_publish_hbase_route_renders_svc_route_zero_code():
    """AC-6：运行时可调前提＝渲染器为新服务生成网关路由（零代码）。

    命名规范 v1（Q1 收敛）：对外出口＝登记 routePath **原样**渲染（/v1/svc/<slug>，
    消灭「登记 /v1/svc、实渲染 /v1/query」双口径）；网关侧 proxy-rewrite 把对外地址
    转入 /v1/query/<sid> 内部转发通道（service 层 handler 零改动，存量兼容）。
    """
    p = _services_copy()
    oc.publish_service(p, name="n", route="/v1/svc/hbase-x",
                       fields=_hbase_fields(["ac_active_sts_01"]), datasource="hbase",
                       hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
    specs = dict(rend.render_query_routes(kvx.load_services(p)))
    body = specs["routes/oneserve-svc-v1-svc-hbase-x"]
    assert body["uri"] == "/v1/svc/hbase-x"                       # 对外＝登记地址原样
    assert body["plugins"]["proxy-rewrite"]["regex_uri"] == \
        ["^/v1/svc/hbase-x$", "/v1/query/v1-svc-hbase-x"]          # 内部通道仍在


def test_republish_overwrites_no_second_entry():
    """卡4 AC-2a：同 route/service_id 再发布＝原地覆盖，console 仍单条目且无版本键。"""
    p = _services_copy()
    kw = dict(datasource="hbase",
              hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
    oc.publish_service(p, name="n", route="/v1/svc/hbase-v",
                       fields=_hbase_fields(["ac_active_sts_01"]), **kw)
    e2, prev = oc.publish_service(p, name="n", route="/v1/svc/hbase-v",
                                  fields=_hbase_fields(["ac_active_sts_01", "mk_touch_sts_02"]), **kw)
    assert prev is not None and prev["fields"] == ["ac_active_sts_01"]  # 前值视图供只读 diff
    rows = oc.load_console_services(p)
    assert len([r for r in rows if r["id"] == "v1-svc-hbase-v"]) == 1      # 单条目（判据原文）
    assert all("version" not in r and "versions" not in r for r in rows)  # 无版本键
    # 运行时注册表：service_id 单键可查、无版本键（不产生 v2 条目）
    reg = kvx.load_services(p)
    assert not any(isinstance(k, tuple) for k in reg)
    svc = reg["v1-svc-hbase-v"]
    assert "version" not in svc and "versions" not in svc
    assert [f["name"] for f in svc["fields"]] == ["ac_active_sts_01", "mk_touch_sts_02"]


def test_republish_leaves_audit_trace():
    """卡4 AC-2b/NC-2c：覆盖前旧值留痕——条目 trace.history 保底＋HTTP 层审计八元组。"""
    p = _services_copy()
    kw = dict(datasource="hbase",
              hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
    oc.publish_service(p, name="n", route="/v1/svc/hbase-v",
                       fields=_hbase_fields(["ac_active_sts_01"]), **kw)
    oc.publish_service(p, name="n", route="/v1/svc/hbase-v",
                       fields=_hbase_fields(["ac_active_sts_01", "mk_touch_sts_02"]), **kw)
    stored = next(r for r in oc.load_console_services(p) if r["id"] == "v1-svc-hbase-v")
    hist = stored["trace"]["history"]
    assert len(hist) == 2                                   # 首发布＋覆盖更新两记录
    assert hist[0]["status"] == "published" and hist[0]["delta"] == "+1"
    assert hist[1]["status"] == "published" and hist[1]["delta"] == "+1"
    # NC-2c 正式审计：HTTP 层已接 audit.py 八元组（源码机检；真链路归 S6 e2e 实证）
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    assert "from app.audit import make_entry" in src           # 接通既有审计 writer
    assert 'endpoint=f"admin:{action}"' in src                 # endpoint 语义=admin:<action>
    assert 'request, "publish"' in src and 'request, "offline"' in src  # 两动作均挂审计
    assert src.count('_audit_admin_action(') >= 3              # 定义＋publish/offline 两调用


def test_normalize_publish_payload_m25_backend_shape():
    """AC-6（M2-5 契约）：前端发布请求 backend{source,hbase{namespace,table,cf,key_type}}
    归一为 publish_service 入参（表名统一 namespace:table；url 路径名缺省 default 不前缀）。"""
    norm = oc.normalize_publish_payload({
        "serviceName": "n", "routePath": "/v1/svc/xxx",
        "keyType": "phone_sm3",
        "backend": {"source": "hbase",
                    "hbase": {"namespace": "default", "table": "oneserve_demo",
                              "cf": "cf", "key_type": "phone_sm3"}},
        "oaTicket": "OA-M25"})
    assert norm["datasource"] == "hbase"
    assert norm["hbase"] == {"table": "oneserve_demo", "cf": "cf", "key_type": "phone_sm3"}
    assert norm["oa_ticket"] == "OA-M25"
    # 非 default namespace → 表名统一 namespace:table（services.yaml 口径）
    norm2 = oc.normalize_publish_payload({
        "backend": {"source": "hbase",
                    "hbase": {"namespace": "ods", "table": "wide", "cf": "cf",
                              "key_type": "phone_sm3"}}})
    assert norm2["hbase"]["table"] == "ods:wide"
    # 扁平式兼容（本卡早期/手调口径）
    norm3 = oc.normalize_publish_payload({
        "datasource": "hbase", "hbaseTable": "default:oneserve_demo",
        "hbaseCf": "cf", "keyType": "phone_sm3"})
    assert norm3["hbase"] == {"table": "default:oneserve_demo", "cf": "cf",
                              "key_type": "phone_sm3"}
    # 缺省＝redis（存量行为零改动）
    assert oc.normalize_publish_payload({})["datasource"] == "redis"


def test_redis_publish_behaviour_unchanged():
    """向后兼容：datasource 缺省（redis）时写区仍按 id 就地更新（不新增行）。"""
    p = _services_copy()
    oc.publish_service(p, name="n", route="/v1/svc/rrr",
                       fields=_hbase_fields(["ac_active_sts_01"]))
    oc.publish_service(p, name="n", route="/v1/svc/rrr",
                       fields=_hbase_fields(["ac_active_sts_01", "mk_touch_sts_02"]))
    entries = oc.load_console_services(p)
    assert len(entries) == 1                                  # 卡4：覆盖更新仍单条目
    assert [f["name"] for f in entries[0]["fields"]] == ["ac_active_sts_01", "mk_touch_sts_02"]
    assert not [s for s in kvx.load_services(p).values()
                if isinstance(s, dict) and s.get("backend", {}).get("source") == "hbase"
                and s.get("service_id") == "v1-svc-rrr"]


# ======================================================================
# AC-7：校验拒绝（文件零变更）
# ======================================================================

def test_publish_validation_rejects_keeps_file_zero_change():
    """AC-7：坏路由/键类型未定/必填缺失/坏表名/坏列族 → 拒绝且逐字节零变更。"""
    p = _services_copy()
    baseline = p.read_text(encoding="utf-8")
    hb_ok = {"table": "t", "cf": "cf", "key_type": "phone_sm3"}
    cases = [
        (dict(name="n", route="/v1/SVC/X", fields=_hbase_fields(["ac_active_sts_01"]),
              datasource="hbase", hbase=hb_ok), "invalid_route"),
        (dict(name="n", route="/v1/svc/xxx", fields=_hbase_fields(["ac_active_sts_01"]),
              datasource="hbase", hbase={"table": "t", "cf": "cf"}), "invalid_key_type"),
        (dict(name="n", route="/v1/svc/xxx", fields=[], datasource="hbase", hbase=hb_ok),
         "empty_fields"),
        (dict(name="n", route="/v1/svc/xxx", fields=_hbase_fields(["ac_active_sts_01"]),
              datasource="hbase", hbase={"table": "bad table!", "cf": "cf",
                                         "key_type": "phone_sm3"}), "invalid_hbase_table"),
        (dict(name="n", route="/v1/svc/xxx", fields=_hbase_fields(["ac_active_sts_01"]),
              datasource="hbase", hbase={"table": "t", "cf": "", "key_type": "phone_sm3"}),
         "invalid_hbase_cf"),
        (dict(name="n", route="/v1/svc/xxx", fields=_hbase_fields(["ac_active_sts_01"]),
              datasource="hbase", hbase=hb_ok, status="offline"), "invalid_status"),
        (dict(name="n", route="/v1/svc/xxx",
              fields=[{"name": "ac_active_sts_01", "sample": "", "desc": ""}],
              datasource="hbase", hbase=hb_ok), "sample_required"),
    ]
    for kwargs, code in cases:
        try:
            oc.publish_service(p, **kwargs)
            raise AssertionError(f"应拒绝 {kwargs}（期望 {code}）")
        except oc.OpsConfigError as exc:
            assert exc.code == code, (kwargs, exc.code)
    assert p.read_text(encoding="utf-8") == baseline
    assert not list(p.parent.glob("*.tmp"))                 # 原子写无残件


def test_publish_draft_hbase_not_registered_runtime():
    """草稿：写区登记但 **不** 落运行时服务条目（未发布不上线；加载器不认它）。"""
    p = _services_copy()
    oc.publish_service(p, name="n", route="/v1/svc/hbase-draft",
                       fields=[{"name": "ac_active_sts_01", "sample": "", "desc": ""}],
                       status="draft", datasource="hbase",
                       hbase={"table": "t", "cf": "cf", "key_type": "phone_sm3"})
    assert oc.load_console_services(p)[0]["status"] == "draft"
    assert "v1-svc-hbase-draft" not in kvx.load_services(p)


# ======================================================================
# HTTP 编排机检（宿主无 fastapi：源码断言；端到端归 e2e 脚本）
# ======================================================================

def test_admin_api_uses_pure_normalizer_not_inline_branching():
    """分层纪律：HTTP 层不做数据源业务判断——归一在 ops_config 纯函数（可机检）。"""
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    assert "opsc.normalize_publish_payload(p)" in src
    assert "norm[\"datasource\"]" in src and "norm[\"hbase\"]" in src
    # 旧的内联 namespace 归一分支已删（判定单点）
    assert 'if tbl and ":" not in tbl and ns and ns != "default"' not in src


def test_admin_api_m25_contract_endpoints_wired():
    """AC-4/AC-5（M2-5 契约）：discovery/test/selftest 三端点在位。

    2026-09-23：原函数名 ..._wired_and_gated，并断言「每端点前有 if not _token_ok(request)」。
    管理令牌机制已按老板决定整体拆除（授权边界＝网络层可达，认证归 M3），故去掉 gated 断言；
    wired（端点接线）与其余契约断言（如实 503／连接池／归一／OA／出参）全部保留。
    """
    import py_compile
    src_path = REPO / "service" / "app" / "admin_api.py"
    py_compile.compile(str(src_path), doraise=True)
    src = src_path.read_text(encoding="utf-8")
    for route in ('@router.get("/v1/admin/publish/hbase/discovery"',
                  '@router.post("/v1/admin/publish/hbase/test"',
                  '@router.get("/v1/admin/publish/hbase/selftest"',
                  '@router.post("/v1/admin/publish"'):
        assert route in src, f"缺 M2-5 契约端点 {route}"
    assert "HBASE_UNAVAILABLE" in src                      # 不可达如实 503
    assert "app.state.hbase_client" in src                 # 取运行期连接池
    # （原「每端点须过令牌闸」断言随机制拆除下线；端点可达性由本套件其余断言 + 实机 curl 守）
    assert "normalize_publish_payload" in src              # M2-5 backend{source,hbase} 归一（纯逻辑）
    assert "oa_ticket" in src                              # OA 单号引用透传
    assert '"passed"' in src and '"assertions"' in src     # test/selftest 契约出参
    # 契约常量与 M2-5 适配层同源（不另造第二套路径）
    assert "hbase/discovery" in src and "hbase/test" in src and "hbase/selftest" in src


def test_main_wires_hbase_pool_decoupled_from_registry():
    """卡1：连接池生命周期与 services.yaml 解耦——启动即建、注册表热加载不动池。

    旧判定（「无 hbase 型服务在场→不建池」＋热加载补池）已删；池常在，
    发现/装载/试查随时可用，服务上下线不影响池（AC-6「发布后可调」由池常在
    直接满足，不再依赖注册表状态）。宿主无 fastapi，按本套件惯例源码机检。
    """
    import py_compile
    src_path = REPO / "service" / "app" / "main.py"
    py_compile.compile(str(src_path), doraise=True)
    src = src_path.read_text(encoding="utf-8")
    assert "app.state.hbase_client = hbase_client" in src  # 发现端点取运行期连接池
    assert "hbase_client = _build_hbase_client(" in src    # lifespan 启动即建池（无条件）
    assert "load_hbase_connection()" in src                # 配置经 connections.py 双轨装配
    assert "app.state.hbase_connection = hbase_connection" in src  # 诊断文案取当前端点
    # 旧判定无残留（卡1 验收口径：注册表不再门控连接池）
    assert "_hbase_source_services" not in src             # 「hbase 型服务在场」判定已删
    assert "_ensure_hbase_client" not in src               # 热加载补池（注册表→池）已删
    assert "无 hbase 型服务在场" not in src                 # 旧文案残留即失败
    # 热加载回调不触碰连接池：函数体内无 hbase 字样
    body = src[src.index('def _apply_registry_state('):src.index('@app.post("/v1/query/')]
    assert "hbase" not in body, "热加载不应触碰连接池（服务上下线不影响池）"
    # 地址不再直读 os.environ 默认值（走 connections.py：env > yaml > 默认）
    assert 'os.environ.get("HBASE_HOST"' not in src


def test_admin_api_hbase_failure_diagnostics_three_categories():
    """卡1：发现端点失败诊断三分类接线（不可达〔含未启动〕/认证失败/其他）＋行动指引。

    分类逻辑本体在 hbase_executor.classify_hbase_failure（单测见
    tests/test_hbase_executor.py）；本测试机检 HTTP 层接线与文案口径。
    """
    src_path = REPO / "service" / "app" / "admin_api.py"
    src = src_path.read_text(encoding="utf-8")
    assert "classify_hbase_failure" in src                     # 三分类判定接入发现端点
    assert "bash scripts/up_hbase.sh" in src                   # 不可达文案带开发态行动指引
    assert "category" in src                                   # 分类随响应透出（前端可判）
    # 旧文案（注册表口径的「无 hbase 型服务在场」）不得残留
    assert "无 hbase 型服务在场" not in src

def test_ops_config_pure_logic_no_fastapi_redis():
    """分层纪律：发现/发布决策在 ops_config（不 import FastAPI/redis）。"""
    src = (REPO / "service" / "app" / "ops_config.py").read_text(encoding="utf-8")
    assert "import fastapi" not in src and "import redis" not in src
    assert "def verify_datasource(" in src and "def load_field_dict(" in src
    assert "def build_datasource_view(" in src


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("[ok] " + name)
            except Exception:
                failed += 1
                print("[FAIL] " + name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
