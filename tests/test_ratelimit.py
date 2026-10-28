#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-5 限流口径落地套件（tests/test_ratelimit.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：429 带 X-RateLimit-Reason 三值；门户上限与网关拦截值同源；
未登记限额服务行为如实；帮助中心写明口径；SPEC §4.6/§5-P1-5）：
  1. 网关限流插件渲染（app/ratelimit.py 纯逻辑）：
     - 阈值＝登记值：quota_qps → limit-count 速率规则、quota_daily → 日配额规则，
       网关 count 与门户四要素读的是同一个数（无「页面 100、网关 50」第二份口径）；
     - route×consumer 维度：限流键含 consumer_name 与 route_id 变量；
     - 原因头钩子随行（header_filter），仅在网关本地拒绝时补头，不覆盖服务层 429；
  2. 未登记限额的服务行为：两档皆缺/非法/≤0 → 返回空配置（只挂 key-auth，如实不限，
     不揣测 SPEC D7 尚未定案的默认额度）；仅登记一档时只挂对应规则；
  3. consumer 注册体（gw_consumer 单一构造点）：未登记＝与 P0-4 既有形状逐字段一致
     （存量调用方零行为变化）；
  4. 同源钩子：admin_api 四个下发点都把登记 quota 传进网关；init_routes 冷启动按
     注册表重建 consumer（含禁用过滤）——登记值＝网关拦截值在两条路径上都成立；
  5. 门户/文档/契约如实：my_access.QUOTA_CALIBER.limitEnforced=True（P1-3 的如实
     标注随 P1-5 更新）；my.html/my.js/docs.html 写明口径；gen_openapi rate_limit
     enabled=True＋三档原因；
  6. 真栈 e2e（tests/verify_ratelimit_e2e.py 存在性＋可导入；真栈结果落证据文件，
     宿主机无 docker 时不在此处起栈）。

被测对象 app/ratelimit.py 不 import FastAPI/redis（分层纪律，同 appkeys.py）。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import ratelimit as rl        # noqa: E402
from app import appkeys as appk        # noqa: E402
from app import gw_consumer as gw      # noqa: E402
from app import my_access as mya       # noqa: E402

MY_HTML = (REPO / "portal" / "src" / "my.html").read_text(encoding="utf-8")
MY_JS = (REPO / "portal" / "assets" / "my.js").read_text(encoding="utf-8")
DOCS_HTML = (REPO / "portal" / "src" / "docs.html").read_text(encoding="utf-8")


# ---------- 1. 网关限流插件渲染：阈值＝登记值 ----------

def test_reason_values_match_spec_and_batch_family():
    """三档原因取值＝SPEC §4.6 行文（global/endpoint/concurrent），与批量侧同族。"""
    assert rl.RATE_LIMIT_REASONS == ("global", "endpoint", "concurrent")
    assert rl.RATE_LIMIT_REASON_GLOBAL == "global"
    assert rl.RATE_LIMIT_REASON_ENDPOINT == "endpoint"
    assert rl.RATE_LIMIT_REASON_CONCURRENT == "concurrent"
    assert rl.REASON_HEADER == "X-RateLimit-Reason"


def test_quota_values_flow_into_gateway_counts():
    """登记值 → 网关 count 逐字对应：页面显示的 100/50 就是网关拦的 100/50。"""
    plugins = rl.consumer_plugins(quota_qps=50, quota_daily=100)
    rules = {r["key"]: r for r in plugins["limit-count"]["rules"]}
    assert rules[rl.ENDPOINT_KEY]["count"] == 50
    assert rules[rl.GLOBAL_KEY]["count"] == 100
    assert plugins["limit-count"]["rejected_code"] == 429
    # 限流键按 consumer×route 维度（SPEC §4.6「配额挂 route×consumer」）
    assert "$consumer_name" in rl.ENDPOINT_KEY and "$route_id" in rl.ENDPOINT_KEY
    assert "$consumer_name" in rl.GLOBAL_KEY


def test_source_of_truth_is_registry_value_not_hardcoded():
    """同源机检：登记值变 → 网关 count 跟着变（无第二份写死的拦截值）。"""
    a = rl.consumer_plugins(quota_qps=7, quota_daily=9)
    b = rl.consumer_plugins(quota_qps=70, quota_daily=90)
    ca = {r["key"]: r["count"] for r in a["limit-count"]["rules"]}
    cb = {r["key"]: r["count"] for r in b["limit-count"]["rules"]}
    assert ca == {rl.ENDPOINT_KEY: 7, rl.GLOBAL_KEY: 9}
    assert cb == {rl.ENDPOINT_KEY: 70, rl.GLOBAL_KEY: 90}


def test_reason_hook_is_header_filter_and_coexists_with_limits():
    """原因头钩子（header_filter）随限流插件一起下发，且不覆盖服务层 429。"""
    plugins = rl.consumer_plugins(quota_qps=1, quota_daily=1)
    hook = plugins["serverless-post-function"]
    assert hook["phase"] == "header_filter"
    assert rl.REASON_HEADER in hook["functions"][0]
    # 只在网关本地拒绝（upstream_status 为空）时补头——服务层 429（如批量配额）不覆盖
    assert "ngx.var.upstream_status" in hook["functions"][0]
    assert "rate_limiting_info" in hook["functions"][0]
    # 并发兜底值在场（limit-conn 不写 rate_limiting_info）
    assert "concurrent" in hook["functions"][0]


def test_reason_hook_is_fresh_dict_each_call():
    """钩子每次返回新 dict（防调用方就地改写共享常量污染后续 consumer）。"""
    a, b = rl.reason_hook(), rl.reason_hook()
    assert a == b and a is not b
    a["phase"] = "changed"
    assert rl.reason_hook()["phase"] == "header_filter"


# ---------- 2. 未登记限额的服务行为（如实不限，不揣测默认值） ----------

def test_unregistered_quota_yields_no_rate_limit():
    """两档皆缺/非法/≤0＝未登记 → 空配置（只挂 key-auth，如实「不限」）。"""
    for qps, daily in ((None, None), ("", ""), (0, 0), ("abc", "xyz"), (-1, None), (None, 0)):
        assert rl.consumer_plugins(qps, daily) == {}, (qps, daily)


def test_partial_quota_only_installs_registered_rule():
    """仅登记一档 → 只挂对应规则＋并发闸（不无中生有另一档）。"""
    only_qps = rl.consumer_plugins(quota_qps=5, quota_daily=None)
    keys = [r["key"] for r in only_qps["limit-count"]["rules"]]
    assert keys == [rl.ENDPOINT_KEY]
    only_daily = rl.consumer_plugins(quota_qps=None, quota_daily=5)
    keys = [r["key"] for r in only_daily["limit-count"]["rules"]]
    assert keys == [rl.GLOBAL_KEY]
    # 并发闸与原因头钩子在两档下都在（否则 429 无原因头）
    for p in (only_qps, only_daily):
        assert "limit-conn" in p and "serverless-post-function" in p


# ---------- 3. consumer 注册体（gw_consumer 单一构造点） ----------

def test_consumer_body_unregistered_matches_p0_4_shape():
    """未登记：与 P0-4 既有 consumer 形状逐字段一致（存量调用方零行为变化）。"""
    assert rl.consumer_body("sb_x") == {
        "username": "sb_x", "plugins": {"key-auth": {"key": "sb_x"}}}
    assert rl.consumer_body("sb_x", quota_qps=None, quota_daily=None) == rl.consumer_body("sb_x")


def test_consumer_body_registered_includes_limits():
    """已登记：key-auth＋limit-count＋limit-conn＋原因头钩子齐备。"""
    body = rl.consumer_body("ak_y", quota_qps=10, quota_daily=1000)
    assert body["username"] == "ak_y"
    assert body["plugins"]["key-auth"] == {"key": "ak_y"}
    assert body["plugins"]["limit-count"]["rules"]
    assert body["plugins"]["limit-conn"]["conn"] == rl.DEFAULT_MAX_CONCURRENT
    assert body["plugins"]["serverless-post-function"]["phase"] == "header_filter"


def test_gw_put_consumer_passes_quota_to_gateway():
    """gw_consumer.put_consumer：把登记 quota 透到 consumer 体（假传输捕获）。"""
    import asyncio
    captured = []

    def fake(method, path, body=None):
        captured.append((method, path, body))
        return 200, {}

    asyncio.run(gw.put_consumer("ak_z", quota_qps=3, quota_daily=30, _req=fake))
    assert captured[0][1] == "consumers/ak_z"
    rules = {r["key"]: r["count"] for r in
             captured[0][2]["plugins"]["limit-count"]["rules"]}
    assert rules == {rl.ENDPOINT_KEY: 3, rl.GLOBAL_KEY: 30}
    # 未给 quota：仍只挂 key-auth（P0-4 兼容）
    captured.clear()
    asyncio.run(gw.put_consumer("sb_plain", _req=fake))
    assert captured[0][2] == {"username": "sb_plain",
                              "plugins": {"key-auth": {"key": "sb_plain"}}}


# ---------- 4. 同源钩子：登记路径与冷启动路径都把登记值送到网关 ----------

def _fn_src(path: str, name: str) -> str:
    tree = ast.parse((REPO / "service" / "app" / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{path} 缺函数 {name}")


def test_admin_api_passes_registered_quota_on_issue_enable_rotate_quota():
    """admin_api 四个下发点都带登记 quota（签发/启用/轮换/调额——登记值即拦截值）。"""
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    assert src.count("gw_consumer.put_consumer(") == 4
    for fn in ("issue", "set_appkey_status", "rotate_appkey_ep", "set_appkey_quota"):
        body = _fn_src("admin_api.py", fn)
        assert "quota_qps=" in body and "quota_daily=" in body, f"{fn} 未把登记配额下发网关"


def test_init_routes_cold_start_syncs_registry_consumers():
    """init_routes 冷启动按注册表重建 consumer（含配额插件），并过滤禁用凭证。"""
    src = (REPO / "configs" / "apisix" / "init_routes.py").read_text(encoding="utf-8")
    assert "def _load_appkeys(" in src
    assert "rl.consumer_body(" in src
    assert 'status") != "disabled"' in src          # 禁用凭证不重建（禁用即时 401 语义保持）
    assert 'f"consumers/{rec[\'appkey\']}"' in src or "consumers/{rec[" in src


# ---------- 5. 门户/文档/契约如实（P1-3 标注随 P1-5 更新） ----------

def test_limit_enforced_now_true_and_sourced():
    """P1-3 的如实标注 limitEnforced 由 false 更新为 true（网关已强制）。"""
    c = mya.QUOTA_CALIBER
    assert c["limitEnforced"] is True
    assert "gatewayEnforcedBy" in c and "limit-count" in c["gatewayEnforcedBy"]
    assert "同取此值" in c["limitSource"] or "单一事实来源" in c["limitSource"]
    # 两个消费口径（门户卡 / 运营台）共用同一常量
    assert mya.QUOTA_CALIBER is c


def test_portal_copy_reflects_enforcement():
    """门户文案如实：超限被拦、429＋原因；不再写「并未强制拦截」。"""
    assert "并未强制拦截" not in MY_HTML
    assert "X-RateLimit-Reason" in MY_HTML and "429" in MY_HTML
    assert "c.limitEnforced === true" in MY_JS
    assert "gatewayEnforcedBy" in MY_JS


def test_docs_center_documents_caliber():
    """帮助中心写明口径：三档原因＋route×consumer＋同源。"""
    assert 'id="docs-ratelimit"' in DOCS_HTML
    for reason in ("global", "endpoint", "concurrent"):
        assert reason in DOCS_HTML
    assert "X-RateLimit-Reason" in DOCS_HTML
    assert "route×consumer" in DOCS_HTML or "调用方（AppKey）× 路由" in DOCS_HTML


def test_contract_rate_limit_artifact_honest():
    """契约 rate_limit 段诚实性（enabled＋三档原因＋note）：合成源重建 spec 断言——
    全下线终态下无落盘详情 sidecar（零服务＝零产物），机制红线不随产物消亡。"""
    import sys as _sys
    _sys.path.insert(0, str(REPO / "scripts"))
    _sys.path.insert(0, str(REPO / "tests"))
    import gen_openapi as gen
    import synth_services as _syn
    services = gen.load_services(_syn.synth_services_path())
    endpoints = gen.load_auth_endpoints(_syn.synth_matrix_path())
    routes = gen.load_gateway_routes()
    det = gen.build_spec(services["user_tag_query"], endpoints["user_tag_query"],
                         gen._route_for(routes, endpoints["user_tag_query"]), {}, {}, {})
    rlsec = det["rate_limit"]
    assert rlsec["enabled"] is True
    assert rlsec["header"] == "X-RateLimit-Reason"
    assert rlsec["reasons"] == ["global", "endpoint", "concurrent"]
    assert "quota_daily" in rlsec["note"]


# ---------- 6. e2e 脚本存在性（真栈结果落证据；宿主机无栈时不在此起栈） ----------

def test_e2e_script_exists_and_compiles():
    import py_compile
    p = REPO / "tests" / "verify_ratelimit_e2e.py"
    assert p.is_file(), "缺真栈 e2e 脚本"
    py_compile.compile(str(p), doraise=True)


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== P1-5 限流口径套件（{len(tests)} 条）==")
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
