#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 契约生成器套件（scripts/gen_openapi.py 纯逻辑单测＋产物机检）。

守卫口径（SPEC §4.1/§4.2、任务卡 AC）：
  1. 契约由真源生成：每服务一个 path（授权矩阵登记端点），OpenAPI 3.1＋契约版本头；
  2. 详情九要素 P0 覆盖自检：前 5 项＋版本，缺项如实进 missing（不虚报完整）；
  3. 错误码逐条对照执行器真源（TagQueryError 调用点静态扫描 vs 契约码表）——不发明码；
  4. 路由方法与网关 init_routes 一致（点查 POST／代理 GET）；
  5. sidecar 与契约同源一致（details/<sid>.json、catalog.json）；
  6. 出参表含中文名列且缺名如实标「待补」；入参 pattern 来自 services.yaml；
  7. 幂等：重复运行产物逐字节一致。
运行：python3 -m pytest tests/test_gen_openapi.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))  # noqa: E402

import gen_openapi as gen  # noqa: E402

YAML = gen._yaml_module()

# 产物尚未生成时先补一次（保证套件可独立运行）
_ART = REPO / "portal" / "openapi" / "oneserve.openapi.yaml"
if not _ART.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "gen_openapi.py")],
                   check=True, capture_output=True)


def _sources():
    services = gen.load_services()
    endpoints = gen.load_auth_endpoints()
    routes = gen.load_gateway_routes()
    contract = gen.build_contract(services, endpoints, routes,
                                  gen.load_auth_fields(), gen.load_cn_desc(),
                                  gen.load_schema_columns())
    return services, endpoints, routes, contract


def _contract():
    return _sources()[3]


# 机制测试源：合成注册表/矩阵（全下线终态下线上真源为空表；形状＝HEAD 真实条目，
# hbase 型指向业务表 default:usr_basic_info）。产物/幂等类用例仍锚真实 artifacts。
import synth_services as _syn  # noqa: E402


def _syn_sources():
    services = gen.load_services(_syn.synth_services_path())
    endpoints = gen.load_auth_endpoints(_syn.synth_matrix_path())
    routes = gen.load_gateway_routes()
    contract = gen.build_contract(services, endpoints, routes,
                                  gen.load_auth_fields(_syn.synth_matrix_path()),
                                  gen.load_cn_desc(), gen.load_schema_columns())
    return services, endpoints, routes, contract


def _syn_contract():
    return _syn_sources()[3]


def _detail(contract, sid):
    return {s["service_id"]: s for s in contract["x-oneserve-details"]}[sid]


# ── 1. 真源生成：每服务一个 path ＋ OpenAPI 3.1 ＋ 版本头 ──

def test_every_service_has_registered_path():
    """机制：每服务一个 path＋矩阵端点全注册（合成源；现实态＝空表，由 sidecar 用例守卫）。"""
    services, endpoints, _, contract = _syn_sources()
    assert set(contract["paths"]) == {endpoints[sid] for sid in services}
    for sid, ep in endpoints.items():
        assert ep in contract["paths"], f"{sid} 端点 {ep} 未进契约 paths"
    real_services, _err = _syn.real_registry_state()
    assert not _err  # 2026-09-30 现实态口径：注册表空/非空均合法，只守 fail-loud 加载（path 机制由上方合成断言守卫）


def test_openapi_skeleton_and_version():
    c = _contract()
    assert c["openapi"] == "3.1.0"
    assert c["info"]["version"] == gen.CONTRACT_VERSION == "2026-09-23"
    assert c["x-oneserve-contract-version"] == gen.CONTRACT_VERSION
    assert c["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "apikey"


def test_yaml_artifact_parses_and_matches():
    """落盘 yaml 可解析且与 build_contract 重建结果语义一致（info/paths/details 三处）。"""
    doc = YAML.safe_load(_ART.read_text(encoding="utf-8"))
    c = _contract()
    assert doc["openapi"] == c["openapi"] and doc["info"] == c["info"]
    assert doc["paths"] == c["paths"]
    assert doc["x-oneserve-details"] == c["x-oneserve-details"]


# ── 2. 九要素 P0 自检 ──

def test_elements_p0_complete_for_all_services():
    c = _contract()
    for s in c["x-oneserve-details"]:
        e = s["elements"]
        assert e["complete"] is True, f"{s['service_id']} 缺项：{e['missing']}"
        for k in ("identification", "params_table", "response_table", "errors", "example", "version"):
            assert e["items"][k] is True


def test_missing_elements_are_honest():
    """自检器不是橡皮章：砍掉示例后必须如实报缺（防虚报完整的回归；合成源）。"""
    services, endpoints, routes = _syn_sources()[:3]
    orig = gen._example_for
    gen._example_for = lambda arch: {"kind": "curl", "command": []}
    try:
        spec = gen.build_spec(services["user_tag_query"], endpoints["user_tag_query"],
                              gen._route_for(routes, endpoints["user_tag_query"]), {}, {}, {})
    finally:
        gen._example_for = orig
    assert spec["elements"]["complete"] is False
    assert "报文示例" in spec["elements"]["missing"]


# ── 3. 错误码对照执行器真源（静态扫描 TagQueryError 调用点，不发明码） ──

def _executor_error_codes() -> dict:
    """扫描执行器/端点真源实际引用的机读码（P1-2：码名取自 app/errors.py 单一真源）。

    不再扫描字符串字面量——错误码已收敛为 errors.<CONST> 常量引用（P1-2 消除散落
    字面量）；本函数 AST 扫描各执行器的 errors.<NAME> 属性访问，用错误码中心反查
    该常量对应的码字符串与 HTTP 状态，得「真源真的会抛哪些码」清单。契约表若发明
    了执行器从不引用的码，本断言即红（非橡皮章）。"""
    import ast as _ast
    import sys as _sys
    _sys.path.insert(0, str(REPO / "service"))
    from app import errors as _errors
    sources = ["kv_executor.py", "proxy_executor.py", "main.py", "hbase_service.py",
               "appkeys.py", "batch.py"]
    known = set(_errors.all_codes())
    truth: dict = {}
    for name in sources:
        tree = _ast.parse((REPO / "service" / "app" / name).read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if (isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name)
                    and node.value.id == "errors"):
                const = getattr(_errors, node.attr, None)
                if isinstance(const, str) and const in known:
                    truth[const] = _errors.http_status_of(const)
    return truth


def test_service_error_codes_match_executor_truth():
    c = _contract()
    truth = _executor_error_codes()
    for s in c["x-oneserve-details"]:
        for e in s["errors"]:
            if e["source"] != "service":
                continue
            assert e["code"] in truth, f"{s['service_id']} 发明了执行器不存在的码：{e['code']}"
            assert truth[e["code"]] == e["http_status"], (
                f"{e['code']} 状态码不符：契约 {e['http_status']} vs 真源 {truth[e['code']]}")


def test_gateway_401_present_and_ratelimit_honest():
    """网关 401（key-auth）必须出现；P1-5 限流已落地时如实标 enabled＋三档原因。

    限流不逐服务罗列 429 码（按凭证登记配额触发，非无条件）——429 统一在
    rate_limit 段说明，errors 表仍不得虚设 429 条目。
    """
    c = _contract()
    for s in c["x-oneserve-details"]:
        gw = [e for e in s["errors"] if e["source"] == "gateway"]
        assert [e["code"] for e in gw] == ["auth_failed"] and gw[0]["http_status"] == 401
        codes = [e["code"] for e in s["errors"]]
        assert "429" not in codes
        rl = s["rate_limit"]
        assert rl["enabled"] is True
        assert rl["header"] == "X-RateLimit-Reason"
        assert rl["reasons"] == ["global", "endpoint", "concurrent"]
        assert "route×consumer" in rl["note"] and "quota_daily" in rl["note"]


def test_error_rows_have_solution_and_param():
    c = _contract()
    for s in c["x-oneserve-details"]:
        for e in s["errors"]:
            assert e["meaning"] and e["solution"], f"{e['code']} 缺含义或解决方案"
            assert "related" in e


# ── 4. 路由方法与网关一致 ──

def test_methods_match_gateway_routes():
    services, endpoints, routes, c = _syn_sources()
    for sid in services:
        spec = _detail(c, sid)
        route = gen._route_for(routes, endpoints[sid])
        if spec["archetype"] == "external_proxy":
            # B 型代理路由经 upstreams.yaml 由 push_proxy_routes 运行期下发（ADR-0013）——
            # 无静态 init_routes 路由（2026-10-28 开源剥离后口径）；methods 按发布口径
            # GET；gateway.key_auth 如实 False（静态路由不存在，鉴权在运行期路由上）。
            assert route is None
            assert spec["methods"] == ["GET"]
            assert spec["gateway"]["key_auth"] is False
            continue
        assert route is not None
        assert sorted(spec["methods"]) == sorted(route["methods"])
        assert spec["gateway"]["key_auth"] is True
    kv = [s for s in c["x-oneserve-details"] if s["archetype"] == "kv_lookup"]
    assert kv and all(s["methods"] == ["POST"] for s in kv)
    proxy = [s for s in c["x-oneserve-details"] if s["archetype"] == "external_proxy"]
    assert proxy and all(s["methods"] == ["GET"] for s in proxy)


# ── 5. sidecar 同源一致 ──

def test_sidecars_consistent_with_contract():
    c = _contract()
    for s in c["x-oneserve-details"]:
        f = REPO / "portal" / "openapi" / "details" / (s["service_id"] + ".json")
        assert json.loads(f.read_text(encoding="utf-8")) == s
    cat = json.loads((REPO / "portal" / "openapi" / "catalog.json").read_text(encoding="utf-8"))
    assert cat["contractVersion"] == gen.CONTRACT_VERSION
    assert sorted(x["service_id"] for x in cat["services"]) == sorted(
        s["service_id"] for s in c["x-oneserve-details"])


# ── 6. 表格口径：中文名/待补、pattern、授权白名单、脱敏标注 ──

def test_response_table_has_cn_and_honest_pending():
    c = _syn_contract()
    s = _detail(c, "user_tag_query")
    rows = s["response"]["auth_fields"]
    assert len(rows) == 10          # auth_matrix.yaml 白名单条目数（demo_merchant 真源）
    assert all(r["cn"] and r["type"] for r in rows)
    assert any("待补" in r["cn"] for r in rows), "缺中文名的字段应如实标「待补」"
    masked = {r["name"] for r in rows if r["desensitize"]}
    assert masked == {"if_acct_fct_24", "if_bse_fct_04"}   # services.yaml 脱敏登记


def test_params_pattern_from_services_yaml():
    c = _syn_contract()
    # 2026-09-23 维护者 口径：默认键模式放宽（平台不关注键的加密算法）
    assert _detail(c, "user_tag_query")["params"][0]["pattern"] == "^[A-Za-z0-9_.-]{1,128}$"
    assert _detail(c, "news_tag_query")["params"][0]["pattern"] == "^[a-z0-9]{4,64}$"


def test_notfound_shape_is_honest_null():
    """未命中分支如实入表：found=false＋fields=null（不伪造空标签集；合成源）。"""
    c = _syn_contract()
    nf = _detail(c, "user_tag_query")["response"]["not_found_shape"]
    notes = {r["name"]: r["note"] for r in nf}
    assert notes["fields"].startswith("固定 null")
    assert notes["found"].startswith("固定 false")


def test_example_uses_real_format_key_and_curl_copyable():
    c = _syn_contract()
    ex = _detail(c, "user_tag_query")["example"]["command"]
    body = [ln for ln in ex if '"key"' in ln]
    assert len(body) == 2
    for ln in body:
        assert ln.startswith("  -d '{\"key\": \""), f"curl 缺单引号包裹或转义不符：{ln}"
    keys = [re.search(r'"key": "([0-9a-fn0-9]+)"', ln).group(1) for ln in body]
    assert re.fullmatch(r"[0-9a-f]{64}", keys[0])            # 真实 SM3 格式
    assert keys[1] == "n000001"                               # 同步造数编号段
    assert _detail(c, "partner_proxy")["example"]["command"][1].startswith("curl -sS ")


# ── 7. 幂等 ──

def test_generation_is_idempotent():
    outs = ["portal/openapi/oneserve.openapi.yaml", "portal/openapi/catalog.json"]
    outs += [f"portal/openapi/details/{p.name}" for p in
             sorted((REPO / "portal" / "openapi" / "details").glob("*.json"))]   # 全下线终态为零个
    before = [bytes((REPO / o).read_bytes()) for o in outs]
    subprocess.run([sys.executable, str(REPO / "scripts" / "gen_openapi.py")],
                   check=True, capture_output=True)
    after = [bytes((REPO / o).read_bytes()) for o in outs]
    assert before == after


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"[ok] {name}")
            except Exception:
                failed += 1
                print(f"[FAIL] {name}")
                traceback.print_exc()
    sys.exit(1 if failed else 0)
