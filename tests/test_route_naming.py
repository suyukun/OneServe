#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 路由命名规范机器闸套件（提案 v1 → fix/api-naming-enforce）。

覆盖三件事（对应任务卡）：
  1. 正则/条款机检：提案 §3 已实测的 21 用例边界集（正例 9 / 反例 12）＋条款号
     映射（R1 结构 / R3 保留词 / R4 动词 / R7 域码唯一与版本禁入）；R2 域码＝建议
     不强制（domain_hint 提示、不阻断——宽进严出）；
  2. 发布校验三路径：合规过 / 格式拒（400 invalid_route 人话）/ 占用拒
     （400 route_exists「该地址已被 <服务名> 占用」，防 G3 跨归属静默合并版本史）；
  3. 渲染收敛：登记 routePath → 网关对外路由原样渲染（proxy-rewrite 转内部通道
     /v1/query/<service_id>，兼容通道仍在）；无 routePath 存量条目回落 /v1/query；
     R8 兜底：不合规 routePath 拒渲染；
  4. 前后端同源门禁：admin/route-naming.js 由真源生成（漂移即红）、publish.js
     不再自写第二份正则（消提案 §5 G2 前后端口径漂移）。

被测对象 service/app/route_naming.py（单一事实来源）＋ops_config.publish_service
＋registry_render（纯逻辑，不 import FastAPI/redis，分层纪律同 tests/test_ops_config.py）。
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import ops_config as oc            # noqa: E402
from app import registry_render as rend     # noqa: E402
from app import route_naming as rn          # noqa: E402

FIELDS = [{"name": "pf_chnl_sts_41", "sample": "APP", "desc": "主触达渠道"},
          {"name": "if_acct_sts_01", "sample": "正常", "desc": "账户状态"}]


def _tmp_services(text: str = "services: []\n") -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "services.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# ======================================================================
# 1) 21 用例边界集（提案 §3 机检实现已实测口径：正例 9 / 反例 12）
# ======================================================================

CASES = [
    # —— 正例 9 ——
    ("/v1/svc/ret-usr-basic", None),          # 域码-业务名 标准形
    ("/v1/svc/news-tag-hot", None),           # 域码 news
    ("/v1/svc/com-claim-flow", None),          # 域码 com（status 是保留词，不用）
    ("/v1/svc/pub-notice-board", None),       # 域码 pub
    ("/v1/svc/com-region-code", None),        # 域码 com
    ("/v1/svc/abc", None),                    # 长度下边界（3）
    ("/v1/svc/x-y", None),                    # 两段极简
    ("/v1/svc/ret-5g-users", None),           # 实体段数字开头（提案允许）
    ("/v1/svc/ret-" + "a" * 59, None),        # 长度上边界（63）
    # —— 反例 12 ——
    ("/v1/svc/user_tag_query", "R1"),         # 下划线（存量坏名，提案 §5 G5）
    ("/v1/svc/9ret-usr", "R1"),               # 数字开头
    ("/v1/svc/a--b", "R1"),                   # 连续连字符
    ("/v1/svc/Ret-usr", "R1"),                # 大写
    ("/v1/svc/ret-usr-", "R1"),               # 连字符结尾
    ("/v1/svc/-ret-usr", "R1"),               # 连字符开头
    ("/v1/svc/ab", "R1"),                     # 过短（2 < 3）
    ("/v1/svc/ret-" + "a" * 60, "R1"),        # 过长（64 > 63）
    ("/v1/svc/com-ping", "R3"),               # 保留词 ping（与 /v1/ping 撞车）
    ("/v1/svc/ret-usr-get", "R4"),            # 动词 get
    ("/v1/svc/ret-v2-tags", "R7"),            # 版本段 v2（版本不进路径，Q3）
    ("/v1/svc/ret-news-x", "R7"),             # 双域码（域码唯一）
]


@pytest.mark.parametrize("route,expect", CASES)
def test_route_check_21_cases(route, expect):
    """提案边界集逐例机检：判定与条款号都按预期（R2 域码建议不在拦截内）。"""
    assert rn.check_route(route) == expect


def test_route_re_constants_match_proposal():
    """R1 正则与提案 §3 代码块逐字一致（防「实现时手滑改了正则」）。"""
    assert rn.ROUTE_RE_STR == r"^/v1/svc/(?!-)(?!.*--)[a-z][a-z0-9-]{1,61}[a-z0-9]$"
    assert set(rn.DOMAIN_CODES) == {"ret", "news", "pub", "com"}
    assert {"ping", "admin", "batch", "query", "proxy", "svc", "demo"} <= rn.RESERVED_SLUGS
    assert {"get", "query", "list", "create"} <= rn.VERB_TOKENS


def test_error_messages_are_human_with_fix():
    """违规报错＝人话＋改法（不是条款号黑话）：每条消息带示例或具体改法。"""
    assert "小写" in rn.route_error_message("/v1/svc/Hello_World")
    assert "3~63" in rn.route_error_message("/v1/svc/ab")
    assert "保留词" in rn.route_error_message("/v1/svc/com-ping")
    assert "动词" in rn.route_error_message("/v1/svc/ret-usr-get")
    assert "版本" in rn.route_error_message("/v1/svc/ret-v2-tags")
    assert rn.route_error_message("/v1/svc/ret-usr-basic") is None


def test_domain_hint_advisory_only():
    """R2 域码建议不强制：命中域码表→提示；未命中→None 放行（宽进严出）。"""
    assert rn.domain_hint("/v1/svc/ret-usr-basic") is not None
    assert "ret" in rn.domain_hint("/v1/svc/ret-usr-basic")
    assert rn.domain_hint("/v1/svc/retail-usr-basic") is None   # 非表内域码不提示
    assert rn.check_route("/v1/svc/retail-usr-basic") is None   # retail- 开头放行（R2 降建议）
    assert rn.domain_hint("/v1/svc/Hello_World") is None        # 不合规不提示


# ======================================================================
# 2) 发布校验三路径（ops_config.publish_service 机器闸）
# ======================================================================

def test_publish_compliant_route_passes():
    """合规过：/v1/svc/ret-usr-basic 发布成功，登记 routePath 即条目 route。"""
    p = _tmp_services()
    entry, prev = oc.publish_service(p, name="零售用户基础", route="/v1/svc/ret-usr-basic",
                                     fields=FIELDS, status="published")
    assert entry["routePath"] == "/v1/svc/ret-usr-basic"
    assert entry["id"] == "v1-svc-ret-usr-basic"
    assert prev is None
    assert "console_services" in p.read_text(encoding="utf-8")


def test_publish_format_rejected_with_human_message():
    """格式拒：/v1/svc/Hello_World → 400 invalid_route＋人话（含改法），文件零变更。"""
    p = _tmp_services()
    before = p.read_text(encoding="utf-8")
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="坏名", route="/v1/svc/Hello_World",
                           fields=FIELDS, status="draft")
    assert ei.value.status == 400 and ei.value.code == "invalid_route"
    assert "小写" in ei.value.message
    assert p.read_text(encoding="utf-8") == before   # 校验失败文件零变更


def test_publish_occupied_route_rejected():
    """占用拒：两个服务同地址 → 第二个 400 route_exists「该地址已被 X 占用」（G3）。"""
    p = _tmp_services()
    oc.publish_service(p, name="服务A", route="/v1/svc/ret-usr-basic",
                       fields=FIELDS, status="draft")
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="服务B", route="/v1/svc/ret-usr-basic",
                           fields=FIELDS, status="draft")
    assert ei.value.status == 400 and ei.value.code == "route_exists"
    assert "服务A" in ei.value.message and "占用" in ei.value.message


def test_publish_same_service_republish_overwrites():
    """卡4 AC-2a：同服务（同名）重发＝原地覆盖（无版本升位），不触发占用闸。"""
    p = _tmp_services()
    oc.publish_service(p, name="服务A", route="/v1/svc/ret-usr-basic",
                       fields=FIELDS, status="draft")
    e2, before = oc.publish_service(p, name="服务A", route="/v1/svc/ret-usr-basic",
                                    fields=FIELDS, status="draft")
    assert before is not None and before["id"] == "v1-svc-ret-usr-basic"  # 前值视图带回
    rows = oc.load_console_services(p)
    assert len([r for r in rows if r["id"] == "v1-svc-ret-usr-basic"]) == 1   # 仍单条目


def test_publish_occupied_by_runtime_region_entry():
    """占用者也查运行时 services: 区（手编条目 service_id 与派生不同 → 同地址拒发）。"""
    p = _tmp_services(
        "services:\n"
        "  - service_id: legacy-holder\n"
        "    name: 存量占位服务\n"
        "    route: /v1/svc/ret-usr-basic\n"
        "    backend:\n"
        "      kv:\n"
        "        namespace: tags:x\n"
        "        alias_key: tags:x:latest\n"
        "        data_key: 'tags:x:{version}:{sm3}'\n"
        "        version_meta_key: 'sync:ver:x:{version}'\n"
    )
    with pytest.raises(oc.OpsConfigError) as ei:
        oc.publish_service(p, name="新服务", route="/v1/svc/ret-usr-basic",
                           fields=FIELDS, status="draft")
    assert ei.value.code == "route_exists" and "存量占位服务" in ei.value.message


def test_ops_config_no_second_regex():
    """单一实现纪律：ops_config 不再持有第二份路由正则（消双口径源头）。"""
    assert not hasattr(oc, "ROUTE_SVC_RE")


# ======================================================================
# 3) 渲染收敛（登记 routePath → 网关路由一致；/v1/query 兼容仍在）
# ======================================================================

def _svc_entry(sid, route=None):
    e = {"service_id": sid, "archetype": "kv_lookup",
         "name": "n", "backend": {"kv": {"namespace": "tags:user"}}}
    if route:
        e["route"] = route
    return {sid: e}


def test_render_uses_registered_routepath_as_uri():
    """对外路由＝注册条目 routePath 原样渲染（登记什么就是什么）。"""
    specs = dict(rend.render_route_specs(_svc_entry("v1-svc-ret-usr-basic", "/v1/svc/ret-usr-basic")))
    body = specs["routes/oneserve-svc-v1-svc-ret-usr-basic"]
    assert body["uri"] == "/v1/svc/ret-usr-basic"
    # 内部通道仍在：proxy-rewrite 把对外地址转入 /v1/query/<service_id>（存量 handler 零改动）
    assert body["plugins"]["proxy-rewrite"]["regex_uri"] == \
        ["^/v1/svc/ret-usr-basic$", "/v1/query/v1-svc-ret-usr-basic"]


def test_render_falls_back_to_query_channel_without_routepath():
    """无 routePath 的存量条目：回落 /v1/query/<service_id> 渲染（存量逻辑零破坏）。"""
    specs = dict(rend.render_query_routes(_svc_entry("user_tag_query")))
    assert specs["routes/oneserve-query-user_tag_query"]["uri"] == "/v1/query/user_tag_query"


def test_render_legacy_compat_channels_always_present():
    """兼容期专用通道不删：/v1/tags|news|hbase/query 三条仍在。"""
    uris = [b["uri"] for _, b in rend.render_route_specs({})]
    assert set(rend.LEGACY_ROUTE_URIS) <= set(uris)


def test_render_r8_skips_noncompliant_routepath():
    """R8 兜底：手编绕过发布端点的不合规 routePath → 拒渲染（fail-closed 少路由不错路由）。"""
    specs = dict(rend.render_query_routes(_svc_entry("v1-svc-com-ping", "/v1/svc/com-ping")))
    assert "routes/oneserve-svc-v1-svc-com-ping" not in specs


def test_render_gateway_uri_equals_registry_routepath():
    """验收②口径：发布落运行时区的条目（hbase 型），网关渲染 uri 与登记 routePath 全等。

    redis 型 console 条目本就不落运行时区（登记态，既有语义）——渲染真源是
    load_services()，故用 hbase 型发布（落 services: 区）验证全链路：发布 → 注册表 →
    渲染 uri ＝ routePath（消灭「登记 /v1/svc、实渲染 /v1/query」双口径）。
    """
    p = _tmp_services()
    oc.publish_service(p, name="零售用户基础", route="/v1/svc/ret-usr-basic",
                       fields=FIELDS, status="published", datasource="hbase",
                       hbase={"table": "oneserve_demo", "cf": "cf", "key_type": "phone_sm3"},
                       load_version="20260929")
    from app.kv_executor import load_services
    specs = dict(rend.render_route_specs(load_services(p)))
    assert "routes/oneserve-svc-v1-svc-ret-usr-basic" in specs
    assert specs["routes/oneserve-svc-v1-svc-ret-usr-basic"]["uri"] == "/v1/svc/ret-usr-basic"


# ======================================================================
# 4) 前后端同源门禁（导出一份，禁止两处各写）
# ======================================================================

def _generator_module():
    spec = importlib.util.spec_from_file_location(
        "gen_route_naming_js", REPO / "scripts" / "gen_route_naming_js.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_frontend_constants_generated_from_single_source():
    """admin/route-naming.js 与真源生成物逐字节一致（改真源忘再生成＝红）。"""
    mod = _generator_module()
    expected = mod.TEMPLATE.replace("__PAYLOAD__", mod.build_payload())
    actual = (REPO / "admin" / "route-naming.js").read_text(encoding="utf-8")
    assert actual == expected
    # 门户产物同步（build_portal copytree 携带，无陈旧拷贝）
    synced = (REPO / "portal" / "admin" / "route-naming.js").read_text(encoding="utf-8")
    assert synced == expected


def test_frontend_uses_shared_regex_not_own_copy():
    """publish.js ④步用同源常量：不再自写第二份 /v1/svc 正则（消 G2 前后端漂移）。"""
    src = (REPO / "admin" / "publish.js").read_text(encoding="utf-8")
    assert "root.ROUTE_NAMING" in src and "RN.routePattern" in src
    assert "A-Za-z0-9_-" not in src                 # 旧手写正则字符集已删
    html = (REPO / "admin" / "publish.html").read_text(encoding="utf-8")
    i_gate, i_rn, i_js = (html.index("admin-gate.js"), html.index("route-naming.js"),
                          html.index("publish-hbase-adapter.js"))
    assert i_gate < i_rn < i_js                      # 常量先于业务 js 加载
    built = (REPO / "portal" / "admin" / "publish.html").read_text(encoding="utf-8")
    assert built.index("route-naming.js") < built.index("publish.js")
