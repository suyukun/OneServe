# -*- coding: utf-8 -*-
"""报表专区一期端点测试（1b-9 · /v1/admin/usage/* 四端点族；设计稿 §3/§4/§7）。

覆盖：
  - 契约形状：三态包络（ok/empty/degraded）＋分页元数据＋serviceDim＋口径随行；
  - 分页：page_size 白名单 {20,50,100} 默认 50、越界页 items 空 total 不变；
  - 参数边界：days=8 / sort=foo / order=foo / page=0 / page_size=30 → 400；
  - 现算禁令（ADR-0023 §5）：快照未响应＝降级态，绝不回落 JSONL 现算；
  - svc 维度：services/serviceDim 真实值（补丁实证）；掩码纪律（完整 appkey 不出端点）；
  - 筛选/排序/搜索：租户、登记状态、q 前缀（登记名/原串/掩码串，出口仍掩码）、排序白名单；
  - 趋势：caller/service 双维＋Top10＋其余聚合（svc×日矩阵）；
  - （角色矩阵守卫随 tests/test_auth_oidc.py 于 v0.1.1 登录清理退役，无登录模式决策 §7）。

Kafka/Redis 未起时全部测试仍绿（mock 覆盖所有外部依赖，同 test_audit_consumer 口径）。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as appk          # noqa: E402
from app import metering as mt           # noqa: E402
from app import usage_report as ur       # noqa: E402

try:
    from fastapi.testclient import TestClient   # noqa: E402
    from fastapi import FastAPI                 # noqa: E402
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    TestClient = None  # type: ignore
    _HAS_FASTAPI = False

_TZ_CN = timezone(timedelta(hours=8))
DEMO_KEY = "sb_c49d18ab72e03f55"           # 登记凭证样例（中性演示值；掩码与断言自洽）
PARTNER = "ak_1122334455667788"              # 登记正式凭证样例
GHOST = "sb_zzzz0000zzzz0000"                # 未登记凭证样例
EP_A = "service:/v1/query/a"
EP_B = "service:/v1/query/b"

_SVC_A = {f"svc:{EP_A}:calls": "2", f"svc:{EP_A}:success": "1", f"svc:{EP_A}:fail": "1",
          f"svc:{EP_A}:lat_sum": "30", f"svc:{EP_A}:lat_cnt": "2",
          f"svc:{EP_B}:calls": "1", f"svc:{EP_B}:success": "1",
          f"svc:{EP_B}:lat_sum": "10", f"svc:{EP_B}:lat_cnt": "1"}


def _today_cn() -> str:
    return datetime.now(_TZ_CN).strftime("%Y%m%d")


def _hashes(days=1, svc=True):
    """构造 N 日 × 3 caller 快照 hash（DEMO_KEY 2 服务、PARTNER 1 服务、GHOST 1 服务）。"""
    out = {}
    base = datetime.now(_TZ_CN)
    for i in range(days):
        day = (base - timedelta(days=i)).strftime("%Y%m%d")
        h_demo = {"calls": "3", "success": "2", "fail": "1",
                  "lat_sum": "40", "lat_cnt": "3"}
        if svc:
            h_demo.update(_SVC_A)
        out[f"{mt.STATS_KEY_PREFIX}{day}:{DEMO_KEY}"] = h_demo
        h_partner = {"calls": "5", "success": "5", "fail": "0",
                     "lat_sum": "50", "lat_cnt": "5"}
        if svc:
            h_partner.update({f"svc:{EP_A}:calls": "5", f"svc:{EP_A}:success": "5",
                              f"svc:{EP_A}:lat_sum": "50", f"svc:{EP_A}:lat_cnt": "5"})
        out[f"{mt.STATS_KEY_PREFIX}{day}:{PARTNER}"] = h_partner
        out[f"{mt.STATS_KEY_PREFIX}{day}:{GHOST}"] = {
            "calls": "1", "success": "0", "fail": "1", "lat_sum": "9", "lat_cnt": "1",
            **({f"svc:{EP_B}:calls": "1", f"svc:{EP_B}:fail": "1",
                f"svc:{EP_B}:lat_sum": "9", f"svc:{EP_B}:lat_cnt": "1"} if svc else {})}
    return out


KEYS_DOC = [
    {"appkey": DEMO_KEY, "app_name": "演示签发", "tenant": "demo-trial"},
    {"appkey": PARTNER, "app_name": "合作行代付", "tenant": "partner-demo"},
]

SERVICES_REG = {
    "v1-svc-a": {"name": "演示服务甲", "route": "/v1/query/a"},
}


class StatsRedis:
    """模拟 aioredis：按 audit:stats:{day}:* 前缀返回预设 hash；error=True 模拟快照未响应。"""

    def __init__(self, hashes=None, error=False):
        self._hashes = hashes or {}
        self._error = error

    async def scan(self, cursor, match="", count=100):
        if self._error:
            raise RuntimeError("snapshot unavailable")
        prefix = match.rstrip("*")
        keys = [k for k in self._hashes if k.startswith(prefix)]
        return (0, keys)

    def pipeline(self, transaction=False):
        return _StatsPipe(self)


class _StatsPipe:
    def __init__(self, r):
        self._r = r
        self._keys = []

    def hgetall(self, key):
        self._keys.append(key)
        return self

    async def execute(self):
        return [dict(self._r._hashes[k]) for k in self._keys]


def _client(hashes=None, keys=None, services=None, redis_error=False):
    from app import metering_api
    app = FastAPI()
    app.state.appkeys = KEYS_DOC if keys is None else keys
    app.state.audit = SimpleNamespace(jsonl=Path("/nonexistent/audit.jsonl"))
    app.state.redis = StatsRedis(hashes=hashes if hashes is not None else _hashes(),
                                 error=redis_error)
    app.state.services = SERVICES_REG if services is None else services
    app.include_router(metering_api.router)
    return TestClient(app)


# ── 契约形状 ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_subjects_contract_shape_and_masking():
    """/v1/admin/usage/subjects：包络字段＋svc 服务明细真实值＋掩码纪律＋默认 page_size=50。"""
    c = _client()
    r = c.get("/v1/admin/usage/subjects", params={"days": 1})
    assert r.status_code == 200
    j = r.json()
    for k in ("status", "message", "days", "dayKeys", "total", "page", "pageSize",
              "items", "serviceDim", "tenants", "generatedAt", "caliber"):
        assert k in j, f"缺字段 {k}"
    assert j["status"] == "ok" and j["message"] is None
    assert j["pageSize"] == 50 and j["page"] == 1
    assert j["serviceDim"] is True                     # svc 补丁生效实证
    assert sorted(j["tenants"]) == ["demo-trial", "partner-demo"]
    masked_demo = appk.mask_appkey(DEMO_KEY)
    row = next(x for x in j["items"] if x["callerMasked"] == masked_demo)
    assert row["registered"] is True and row["appName"] == "演示签发"
    assert row["tenant"] == "demo-trial" and row["kid"]
    assert row["services"] == [
        {"endpoint": EP_A, "calls": 2, "success": 1, "fail": 1, "avgMs": 15.0},
        {"endpoint": EP_B, "calls": 1, "success": 1, "fail": 0, "avgMs": 10.0},
    ]
    assert row["successRate"] == 66.7 and row["calls"] == 3
    assert DEMO_KEY not in r.text and PARTNER not in r.text   # 完整 appkey 不出端点


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_legacy_hash_without_svc_reports_service_dim_false():
    """兼容口径：快照全是补丁前旧键（无 svc 字段）→ serviceDim=False（服务列降级「—」），他列照常。"""
    c = _client(hashes=_hashes(svc=False))
    j = c.get("/v1/admin/usage/subjects", params={"days": 1}).json()
    assert j["serviceDim"] is False
    row = j["items"][0]
    assert row["services"] == [] and row["calls"] > 0


# ── 分页与参数边界（设计稿 §7-3 命令级口径） ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_paging_boundaries_and_400s():
    """分页：page_size 白名单/越界页 items 空且 total 不变；days=8、sort=foo、order=foo、
    page=0、page_size=30、registered=maybe 均 400；days=abc 类型错 422。"""
    hashes = {}
    day = _today_cn()
    for i in range(60):   # 60 个 caller（每行 calls=i+1）
        caller = f"sb_{i:016d}"
        hashes[f"{mt.STATS_KEY_PREFIX}{day}:{caller}"] = {
            "calls": str(i + 1), "success": str(i + 1), "fail": "0",
            "lat_sum": "10", "lat_cnt": "1"}
    c = _client(hashes=hashes, keys=[])
    r1 = c.get("/v1/admin/usage/subjects", params={"days": 1, "page_size": 20})
    assert r1.status_code == 200
    j1 = r1.json()
    assert len(j1["items"]) == 20 and j1["total"] == 60
    r2 = c.get("/v1/admin/usage/subjects", params={"days": 1, "page_size": 20, "page": 2})
    j2 = r2.json()
    assert j2["total"] == j1["total"]                     # total 翻页不变
    assert {x["callerMasked"] for x in j1["items"]}.isdisjoint(
        {x["callerMasked"] for x in j2["items"]})         # 翻页不重不漏
    r_far = c.get("/v1/admin/usage/subjects", params={"days": 1, "page_size": 20, "page": 99})
    assert r_far.json()["items"] == [] and r_far.json()["total"] == 60   # 越界页幂等
    for bad in ({"days": 8}, {"days": 1, "sort": "foo"}, {"days": 1, "order": "up"},
                {"days": 1, "page": 0}, {"days": 1, "page_size": 30},
                {"days": 1, "registered": "maybe"}, {"days": 0}):
        resp = c.get("/v1/admin/usage/subjects", params=bad)
        assert resp.status_code == 400, f"{bad} 应 400：{resp.status_code}"
        assert resp.json()["error"] == "bad_query"
    assert c.get("/v1/admin/usage/subjects", params={"days": "abc"}).status_code == 422
    for ps in (20, 50, 100):                              # 合法 page_size 白名单双侧
        assert c.get("/v1/admin/usage/subjects",
                     params={"days": 1, "page_size": ps}).status_code == 200


# ── 三态与现算禁令 ──

def _forbidden_scan(*a, **k):
    raise AssertionError("现算禁令（ADR-0023 §5）：报表端点不得触发 JSONL 现算")


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_empty_state_when_no_snapshot(monkeypatch):
    """空态：快照键缺失（消费者未部署/首日无数据）→ status=empty＋空态文案，不报错不造假。"""
    monkeypatch.setattr(mt, "metering_scan", _forbidden_scan)
    c = _client(hashes={})
    j = c.get("/v1/admin/usage/subjects", params={"days": 7}).json()
    assert j["status"] == "empty" and j["message"] == ur.MSG_EMPTY
    assert j["items"] == [] and j["total"] == 0


@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_degraded_no_jsonl_fallback(monkeypatch):
    """降级态＋现算禁令：快照未响应 → status=degraded＋降级文案；
    JSONL 降级路径绝不触发（metering_scan 被调即红），四端点一致。"""
    monkeypatch.setattr(mt, "metering_scan", _forbidden_scan)
    c = _client(redis_error=True)
    for path in ("/v1/admin/usage/subjects", "/v1/admin/usage/overview",
                 "/v1/admin/usage/services", "/v1/admin/usage/trend"):
        r = c.get(path, params={"days": 7})
        assert r.status_code == 200, path
        j = r.json()
        assert j["status"] == "degraded", path
        assert j["message"] == ur.MSG_DEGRADED
        assert j["items"] == []


# ── 筛选/排序/搜索 ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_filters_sort_and_q_search():
    """筛选（租户/登记状态）、排序白名单（calls/success_rate/avg_ms/name）、q 前缀搜索
    （登记名/凭证原串前缀/掩码串前缀都能命中；出口仍掩码，原串不出现）。"""
    c = _client()
    j = c.get("/v1/admin/usage/subjects", params={"days": 1, "tenant": "partner-demo"}).json()
    assert [x["tenant"] for x in j["items"]] == ["partner-demo"]
    j = c.get("/v1/admin/usage/subjects", params={"days": 1, "registered": "false"}).json()
    assert len(j["items"]) == 1 and j["items"][0]["registered"] is False
    j = c.get("/v1/admin/usage/subjects", params={"days": 1, "registered": "true"}).json()
    assert all(x["registered"] for x in j["items"]) and len(j["items"]) == 2
    # 排序：success_rate 升序 → GHOST(0%) 在前
    j = c.get("/v1/admin/usage/subjects",
              params={"days": 1, "sort": "success_rate", "order": "asc"}).json()
    rates = [x["successRate"] for x in j["items"]]
    assert rates == sorted(rates) and rates[0] == 0.0
    # name 排序（排序键＝登记名，缺登记名回落掩码串；按同一键提取断言）
    j = c.get("/v1/admin/usage/subjects",
              params={"days": 1, "sort": "name", "order": "asc"}).json()
    names = [x["appName"] or x["callerMasked"] for x in j["items"]]
    assert names == sorted(names)
    # q 登记名前缀
    j = c.get("/v1/admin/usage/subjects", params={"days": 1, "q": "合作行"}).json()
    assert len(j["items"]) == 1 and j["items"][0]["appName"] == "合作行代付"
    # q 凭证原串前缀——出口仍掩码
    j = c.get("/v1/admin/usage/subjects", params={"days": 1, "q": DEMO_KEY[:6]}).json()
    assert len(j["items"]) == 1
    assert j["items"][0]["callerMasked"] == appk.mask_appkey(DEMO_KEY)
    assert DEMO_KEY not in str(j["items"])
    # q 掩码串前缀（页面跳转链路）
    j = c.get("/v1/admin/usage/subjects",
              params={"days": 1, "q": appk.mask_appkey(GHOST)[:7]}).json()
    assert len(j["items"]) == 1 and j["items"][0]["registered"] is False
    # q 无命中
    assert c.get("/v1/admin/usage/subjects", params={"days": 1, "q": "不存在的名字"}).json()["items"] == []


# ── 服务页 ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_services_aggregation_names_and_filters():
    """/v1/admin/usage/services：跨 caller 按服务聚合（调用量/调用方数去重）＋登记服务名合并
    （route 与 /v1/query/{sid} 双形态）＋subject/tenant 筛选＋q 搜索命中登记名。"""
    c = _client()
    j = c.get("/v1/admin/usage/services", params={"days": 1}).json()
    by_ep = {x["service"]: x for x in j["items"]}
    assert by_ep[EP_A]["calls"] == 7 and by_ep[EP_A]["callers"] == 2      # DEMO_KEY 2＋PARTNER 5
    assert by_ep[EP_A]["registeredName"] == "演示服务甲"                   # /v1/query/a 形态命中
    assert by_ep[EP_B]["calls"] == 2 and by_ep[EP_B]["callers"] == 2
    assert by_ep[EP_B]["registeredName"] is None                          # 无登记名＝路由原值
    assert by_ep[EP_B]["successRate"] == 50.0
    # subject 筛选（凭证原串前缀）→ 只剩该 caller 的服务
    j = c.get("/v1/admin/usage/services", params={"days": 1, "subject": PARTNER[:5]}).json()
    assert len(j["items"]) == 1 and j["items"][0]["service"] == EP_A
    assert j["items"][0]["calls"] == 5
    # tenant 筛选同效
    j = c.get("/v1/admin/usage/services", params={"days": 1, "tenant": "demo-trial"}).json()
    assert {x["service"] for x in j["items"]} == {EP_A, EP_B}
    assert sum(x["calls"] for x in j["items"]) == 3
    # q 搜索命中登记名
    j = c.get("/v1/admin/usage/services", params={"days": 1, "q": "演示服务"}).json()
    assert len(j["items"]) == 1 and j["items"][0]["service"] == EP_A
    # 非法参数同 subjects 口径
    assert c.get("/v1/admin/usage/services", params={"days": 9}).status_code == 400
    assert c.get("/v1/admin/usage/services", params={"days": 1, "sort": "foo"}).status_code == 400


# ── 总览页 ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_overview_cards_and_top_services():
    """/v1/admin/usage/overview：窗口总量/成功率/活跃调用方（含未登记副注）/热门服务 Top5/逐日柱线。"""
    c = _client()
    j = c.get("/v1/admin/usage/overview", params={"days": 1}).json()
    assert j["status"] == "ok"
    assert j["totals"]["calls"] == 9 and j["totals"]["success"] == 7 and j["totals"]["fail"] == 2
    assert j["totals"]["successRate"] == 77.8
    assert j["activeCallers"] == {"count": 3, "unregistered": 1}   # 未登记副注数据源
    assert len(j["topServices"]) == 2
    assert j["topServices"][0]["service"] == EP_A and j["topServices"][0]["calls"] == 7
    assert len(j["daily"]) == 1 and j["daily"][0]["calls"] == 9
    assert j["items"][0]["calls"] == 5                              # 榜默认调用量降序
    assert c.get("/v1/admin/usage/overview", params={"days": 8}).status_code == 400


# ── 趋势页 ──

@pytest.mark.skipif(not _HAS_FASTAPI, reason="依赖缺席：fastapi")
def test_trend_dims_top10_and_others():
    """/v1/admin/usage/trend：caller 维（逐日补零＋登记名 label）／service 维（svc×日矩阵）；
    超过 10 实体 → Top10＋其余聚合；dim/days 非法 400。"""
    c = _client(hashes=_hashes(days=2))
    j = c.get("/v1/admin/usage/trend", params={"days": 2, "dim": "caller"}).json()
    assert j["dim"] == "caller" and len(j["dayKeys"]) == 2
    assert len(j["series"]) == 3 and j["others"] is None
    demo = next(s for s in j["series"] if s["key"] == appk.mask_appkey(DEMO_KEY))
    assert demo["label"] == "演示签发"
    assert sum(p["calls"] for p in demo["data"]) == 6               # 两日窗口合计（每日 3 次）
    assert all(len(s["data"]) == 2 for s in j["series"])            # 逐日补零对齐
    # service 维：svc×日矩阵聚合
    j = c.get("/v1/admin/usage/trend", params={"days": 2, "dim": "service"}).json()
    keys = {s["key"] for s in j["series"]}
    assert keys == {EP_A, EP_B}
    ep_a = next(s for s in j["series"] if s["key"] == EP_A)
    assert sum(p["calls"] for p in ep_a["data"]) == 14              # 每日 7 次 × 2 日
    # Top10＋其余聚合：11 个 caller
    hashes = {}
    day = _today_cn()
    for i in range(11):
        hashes[f"{mt.STATS_KEY_PREFIX}{day}:sb_{i:016d}"] = {
            "calls": str(i + 1), "success": str(i + 1), "fail": "0",
            "lat_sum": "1", "lat_cnt": "1"}
    c2 = _client(hashes=hashes, keys=[])
    j = c2.get("/v1/admin/usage/trend", params={"days": 1, "dim": "caller"}).json()
    assert len(j["series"]) == 10 and j["others"]["count"] == 1
    assert sum(s["calls"] for s in j["series"]) + j["others"]["calls"] == 66
    assert c.get("/v1/admin/usage/trend", params={"days": 1, "dim": "foo"}).status_code == 400
    assert c.get("/v1/admin/usage/trend", params={"days": 8}).status_code == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
