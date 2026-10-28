#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 目录计数修复回归（tests/test_portal_catalog.py，2026-10-08）。

缺陷背景：/v1/portal/catalog 旧实现按 user_tag_query/news_tag_query 等历史
硬编码 service_id 拼业务域；2026-09-29 全下线＋发布台改发 v1-svc-* 命名后旧 id 均不在
注册表 → 端点恒回 domains:[]，目录页与首页搜索条恒显「0 个在线服务」，与发布台列表脱节。

修复口径：目录条目＝console_services 写区（与 GET /v1/admin/publish 的 services 列表
同一真源同一加载器），聚合收敛在 portal_api._catalog_view 纯函数。本套件直测该纯函数
（AST 抽取后 exec——宿主门禁无 fastapi 依赖，同 test_monitor.py 抽取先例），守四条：
  1. 目录服务总数 == console_services 条目数（发布台列表恒等，本卡验收口径）；
  2. serviceId 仅对运行时注册表在册服务下发（详情壳存在面），offline 条目不带；
  3. 状态映射：published→ok/正常、offline→warn/已下线；条目级 status 随目录下发；
  4. 空写区＝空目录（全下线合法终态，不造数）＋ 真库 services.yaml 一致性。

运行：python3 -m pytest tests/test_portal_catalog.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

# 与 services.yaml console_services 真实形态同构的最小条目集（1 published ＋ 2 offline）
ENTRIES = [
    {"id": "v1-svc-usr-basic-info", "name": "用户基本信息查询",
     "route": "/v1/svc/usr-basic-info", "method": "POST", "key": "usr_id 的 phone_sm3",
     "field_space": "wide297", "status": "published",
     "fields": [{"name": "age_band"}, {"name": "risk_level"}, {"name": "vip_flag"}]},
    {"id": "v1-svc-probe-no-source", "name": "探测无源服务",
     "route": "/v1/svc/probe-no-source", "method": "POST", "key": "usr_id",
     "field_space": "wide297", "status": "offline",
     "fields": [{"name": "ac_active_sts_01"}]},
    {"id": "v1-svc-probe-nosrc", "name": "探测无源",
     "route": "/v1/svc/probe-nosrc", "method": "POST", "key": "usr_id",
     "field_space": "news", "status": "offline", "fields": []},
]
RUNTIME_IDS = {"v1-svc-usr-basic-info"}   # 运行时注册表在册面（详情壳已构建期生成）


def _load_catalog_pures() -> dict:
    """从 portal_api.py 抽取目录纯函数族（无 IO、无 fastapi 依赖）并 exec 成可用命名空间。"""
    src = (REPO / "service" / "app" / "portal_api.py").read_text(encoding="utf-8")
    wanted = {"_FIELD_SPACE_DOMAIN", "_STATUS_VIEW", "_domain_of", "_endpoint_of", "_catalog_view"}
    chunks = []
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in wanted:
            chunks.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(src, node))
    assert len(chunks) == len(wanted), "portal_api.py 纯函数族抽取不全：%d/%d" % (len(chunks), len(wanted))
    ns: dict = {}
    exec(compile("\n\n".join(chunks), "portal_api_pures", "exec"), ns)  # noqa: S102
    return ns


_PURES = _load_catalog_pures()


def _view(entries=ENTRIES, runtime=RUNTIME_IDS, sync=None):
    domains, total = _PURES["_catalog_view"](entries, runtime, [], sync or {})
    return domains, total


# ── 1. 计数恒等（本卡验收口径） ──

def test_catalog_service_count_equals_console_list():
    """目录服务总数 == console_services 条目数（即发布台列表长度），字段数同源累加。"""
    domains, total_fields = _view()
    n = sum(d["services"] for d in domains)
    assert n == len(ENTRIES), "目录计数 %d ≠ 发布台列表 %d" % (n, len(ENTRIES))
    assert sum(len(d["items"]) for d in domains) == n, "items 与 services 计数不同源"
    assert total_fields == sum(len(e["fields"]) for e in ENTRIES), "totalFields 与注册表字段数不符"


def test_real_registry_consistency():
    """真库口径：真实 services.yaml 的 console_services 走同一聚合，计数恒等。"""
    data = yaml.safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8"))
    entries = data.get("console_services") or []
    runtime = {s.get("service_id") for s in (data.get("services") or [])}
    domains, _ = _view(entries=entries, runtime=runtime)
    assert sum(d["services"] for d in domains) == len(entries)
    # 空写区（全下线合法终态）＝空目录，不造数
    domains_empty, tf_empty = _view(entries=[], runtime=runtime)
    assert domains_empty == [] and tf_empty == 0


# ── 2. serviceId 门控（详情壳存在面；offline 不下发防死链） ──

def test_service_id_only_for_runtime_registered():
    domains, _ = _view()
    items = {it["name"]: it for d in domains for it in d["items"]}
    assert items["用户基本信息查询"].get("serviceId") == "v1-svc-usr-basic-info"
    assert "serviceId" not in items["探测无源服务"], "offline 条目不得带 serviceId（无详情壳，防死链）"


# ── 3. 状态映射（域级＋条目级） ──

def test_status_mapping_published_and_offline():
    domains, _ = _view()
    by_id = {d["id"]: d for d in domains}
    assert (by_id["user"]["status"], by_id["user"]["statusText"]) == ("ok", "正常")
    assert (by_id["news"]["status"], by_id["news"]["statusText"]) == ("warn", "已下线")
    statuses = {it["name"]: it["status"] for d in domains for it in d["items"]}
    assert statuses == {"用户基本信息查询": "published",
                        "探测无源服务": "offline", "探测无源": "offline"}


def test_domain_grouping_by_field_space():
    """业务域按 field_space 派生：wide297→用户标签域（缺省回落），news→资讯标签域。"""
    domains, _ = _view()
    by_id = {d["id"]: d for d in domains}
    assert by_id["user"]["name"] == "用户标签域" and by_id["user"]["services"] == 2
    assert by_id["news"]["name"] == "资讯标签域" and by_id["news"]["services"] == 1


# ── 4. 同步时间：命中真值则下发，未命中如实"—" ──

def test_last_sync_at_from_real_value_or_dash():
    domains, _ = _view(sync={"v1-svc-usr-basic-info": "2026-10-08T03:00:00+00:00"})
    by_id = {d["id"]: d for d in domains}
    assert by_id["user"]["lastSyncAt"] == "2026-10-08T03:00:00+00:00"
    assert by_id["news"]["lastSyncAt"] == "—"


# ── 5. N5（2026-10-14 门槛稿 AC-1）：目录每卡业务描述 ──

def test_item_desc_business_text_or_honest_placeholder():
    """条目 desc＝业务描述（services.yaml 条目 desc 字段）；未登记如实「暂无描述」。

    旧实现把查询键口径（key，如「usr_id 的 phone_sm3」）塞进 desc 冒充业务描述——
    目录五卡全是同一句技术口径，用户视角审视判定「卡片只有 API 路径+字段名」。
    """
    entries = [
        dict(ENTRIES[0], desc="按查询键返回用户基础画像等基本信息。"),   # 登记了描述
        dict(ENTRIES[1]),                                              # 未登记 desc
        dict(ENTRIES[2], desc="   "),                                  # 登记了空白描述
    ]
    domains, _ = _view(entries=entries)
    items = {it["name"]: it for d in domains for it in d["items"]}
    assert items["用户基本信息查询"]["desc"] == "按查询键返回用户基础画像等基本信息。", "已登记描述未下发"
    assert items["探测无源服务"]["desc"] == "暂无描述", "未登记描述须如实「暂无描述」"
    assert items["探测无源"]["desc"] == "暂无描述", "空白描述须如实「暂无描述」（不透传空白）"
    # 每张卡 desc 恒非空（前端无须兜底）；查询键口径不再冒充业务描述
    for d in domains:
        for it in d["items"]:
            assert it["desc"].strip(), "目录条目 desc 不得为空"
    assert all(it["desc"] != "usr_id 的 phone_sm3" for d in domains for it in d["items"]), \
        "查询键口径不得再冒充业务描述"


def test_real_registry_every_service_has_desc():
    """真库口径：services.yaml 五条目已登记 desc；目录聚合后每卡一句中文业务描述。"""
    data = yaml.safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8"))
    entries = data.get("console_services") or []
    domains, _ = _view(entries=entries, runtime={s.get("service_id") for s in (data.get("services") or [])})
    items = [it for d in domains for it in d["items"]]
    assert len(items) == len(entries)
    assert all(it["desc"] and it["desc"] != "暂无描述" for it in items), \
        "真库五服务均已登记描述，不应出现「暂无描述」（若新增服务未登记，请补 services.yaml desc）"


# ── 6. 维护者 2026-10-15 批「按建议」：目录模板属性——更新频率/责任主体 ──

def test_item_update_frequency_and_owner_or_honest_placeholder():
    """条目 update_frequency/owner → 目录卡下发 updateFrequency/owner；缺值如实「未标注」。

    口径同 desc（N5）：未登记/空白＝「未标注」占位，不拿默认节奏或平台名冒充真值。
    """
    entries = [
        dict(ENTRIES[0], update_frequency="每日（T+1）", owner="平台运营组"),   # 已登记
        dict(ENTRIES[1]),                                                      # 未登记
        dict(ENTRIES[2], update_frequency="  ", owner=""),                     # 空白
    ]
    domains, _ = _view(entries=entries)
    items = {it["name"]: it for d in domains for it in d["items"]}
    assert items["用户基本信息查询"]["updateFrequency"] == "每日（T+1）", "已登记更新频率未下发"
    assert items["用户基本信息查询"]["owner"] == "平台运营组", "已登记责任主体未下发"
    assert items["探测无源服务"]["updateFrequency"] == "未标注", "未登记更新频率须如实「未标注」"
    assert items["探测无源服务"]["owner"] == "未标注", "未登记责任主体须如实「未标注」"
    assert items["探测无源"]["updateFrequency"] == "未标注" and items["探测无源"]["owner"] == "未标注", \
        "空白登记须如实「未标注」（不透传空白）"
    for d in domains:   # 每卡两属性恒非空（前端无须兜底）
        for it in d["items"]:
            assert it["updateFrequency"].strip() and it["owner"].strip(), "两属性不得为空"


def test_real_registry_every_service_has_frequency_owner():
    """真库口径：services.yaml 五现役条目均已登记两属性真值（不落「未标注」占位）。"""
    data = yaml.safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8"))
    entries = data.get("console_services") or []
    domains, _ = _view(entries=entries,
                       runtime={s.get("service_id") for s in (data.get("services") or [])})
    items = [it for d in domains for it in d["items"]]
    assert len(items) == len(entries)
    for it in items:
        assert it["updateFrequency"] != "未标注", \
            f"{it['name']} 未登记 update_frequency（请补 services.yaml 真值）"
        assert it["owner"] != "未标注", \
            f"{it['name']} 未登记 owner（请补 services.yaml 真值）"


def test_catalog_page_displays_frequency_owner():
    """页面展示：目录卡带「更新频率/责任主体」两行元信息，缺值兜底「未标注」不造假。

    真源（src）与构建产物（portal/catalog.html）须同步持有（MPA 纪律：产物由
    scripts/build_portal.py 从 src 生成，禁止手编产物）。
    """
    for path in (REPO / "portal" / "src" / "catalog.html", REPO / "portal" / "catalog.html"):
        html = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO)
        assert "更新频率" in html and "责任主体" in html, f"{rel} 缺中文标签"
        assert "it.updateFrequency" in html and "it.owner" in html, f"{rel} 未绑定两属性"
        assert "未标注" in html, f"{rel} 缺缺值兜底「未标注」（mock 降级路径会显空）"
        assert "svc-meta" in html, f"{rel} 缺元信息行容器"


if __name__ == "__main__":

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] " + fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print("[FAIL] " + fn.__name__ + ": " + str(exc))
    print("%d/%d passed" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)
