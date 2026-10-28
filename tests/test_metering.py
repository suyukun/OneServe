#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计量报表首批测试（P1 · 映射 20261015 #9/#10）——纯 python 直跑或 pytest 收集均可。

覆盖三段：
  1. 聚合逻辑单测（metering.metering_scan）：小样本构造——caller×endpoint×日
     三维计数/成败/平均耗时；东八区日界；倒读提前终止（窗口外旧行不入账）；
     脏 ts/坏行/未来行/非数字状态容错；days 非法拒绝；
  2. 视图（metering.metering_view）：掩码纪律（完整 appkey 不出参）、登记名合并、
     趋势补零对齐、调用量降序、窗口合计；
  3. 端点契约（metering_api）：GET /v1/admin/metering/summary 形状＋days 越界 400
     ＋参数类型 422；页面接线（appkeys.html 计量区块＋appkeys.js 取数路径）。
"""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import metering as mt            # noqa: E402

# fastapi 宿主缺装时端点契约降级跳过（同 tests/test_error_envelope.py 守卫口径；
# 契约另有活体验证：recreate 后 curl /v1/admin/metering/summary 实测）
try:
    from fastapi.testclient import TestClient   # noqa: E402
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001
    TestClient = None  # type: ignore
    _HAS_FASTAPI = False

NOW = datetime(2026, 10, 15, 1, 0, tzinfo=timezone.utc)   # 北京时间 10-15 09:00
DEMO_KEY = "sb_c49d18ab72e03f55"                   # 登记凭证样例（中性演示值；掩码 sb_c49d****3f55 自洽）
GHOST = "sb_zzzz0000zzzz0000"                             # 未登记凭证样例
DEMO = "demo-page"                                        # 非凭证标识（演示通道）


def _write_audit(rows):
    """rows＝dict 或裸文本行（坏行样例）；按真实审计口径**时间升序**追加。"""
    d = tempfile.mkdtemp(prefix="metering-")
    p = Path(d) / "audit.jsonl"
    p.write_text("".join((json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r) + "\n"
                         for r in rows), encoding="utf-8")
    return p


def _row(caller, ts, endpoint="service:/v1/query/demo", status=200, lat=10):
    return {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "x",
            "status": status, "latency_ms": lat, "response_digest": "y",
            "trace_id": "t", "seq": 0}


# ── 1. 聚合逻辑 ──

def test_scan_aggregates_by_caller_service_day():
    """小样本三维聚合：调用量/成败/平均耗时按 caller×endpoint×日各自正确。"""
    p = _write_audit([
        _row(DEMO_KEY, "2026-10-15T00:30:00+00:00", "service:/v1/query/a", 200, 10),   # CN 今日 08:30
        _row(DEMO_KEY, "2026-10-15T00:40:00+00:00", "service:/v1/query/a", 403, 20),   # 今日失败
        _row(DEMO_KEY, "2026-10-14T02:00:00+00:00", "service:/v1/query/b", 200, 30),   # CN 昨日 10:00
        _row(DEMO, "2026-10-15T00:20:00+00:00", "service:/v1/query/a", 202, 4),    # 2xx 计成功
        _row(DEMO, "2026-10-15T00:21:00+00:00", "service:/v1/query/a", 500, 6),
    ])
    scan = mt.metering_scan(p, days=7, now=NOW)
    assert scan["days"][-1] == "2026-10-15" and len(scan["days"]) == 7
    j = scan["callers"][DEMO_KEY]
    assert j["calls"] == 3 and j["success"] == 2 and j["fail"] == 1
    assert (j["latSum"], j["latCnt"]) == (60, 3)                        # 均值素材：(10+20+30)/3
    assert j["services"]["service:/v1/query/a"] == [2, 1, 1, 30, 2]
    assert j["services"]["service:/v1/query/b"] == [1, 1, 0, 30, 1]
    assert j["days"]["2026-10-15"] == [2, 1, 1] and j["days"]["2026-10-14"] == [1, 1, 0]
    d = scan["callers"][DEMO]
    assert d["calls"] == 2 and d["success"] == 1 and d["fail"] == 1     # 202 计成功
    assert (d["latSum"], d["latCnt"]) == (10, 2)                        # 均值 5.0


def test_scan_early_stop_and_dirty_rows_tolerated():
    """窗口外旧行（提前终止）不入账；脏 ts/坏行跳过不计数；未来日不入窗；
    非数字状态只计总量不猜成败；缺 latency 不计入均值。"""
    p = _write_audit([
        _row(GHOST, "2026-10-01T00:00:00+00:00", status=200, lat=1),   # 早于窗口下界（倒读到它之前即停）
        _row(DEMO_KEY, "2026-10-08T23:00:00+00:00", status=200, lat=1),    # CN 10-09 07:00→窗口首日（东八区日界）入账
        _row(DEMO_KEY, "2026-10-09T00:00:01+00:00", status=200, lat=2),    # 恰在窗口首日（CN 08:00:01）
        {"ts": "garbage", "caller": DEMO_KEY, "endpoint": "e", "status": 200,
         "latency_ms": 1, "trace_id": "t"},                            # 脏 ts：跳过
        {"ts": "2026-10-15T00:10:00+00:00", "caller": DEMO_KEY, "endpoint": "e",
         "status": "xxx", "latency_ms": 5},                            # 非数字状态：只计总量
        _row(DEMO_KEY, "2026-10-16T00:00:00+00:00", status=200, lat=1),    # 未来日（时钟偏移）→不入窗
        "{坏行不是json",                                                   # 坏行：跳过
        _row(DEMO_KEY, "2026-10-15T00:50:00+00:00", status=200, lat=99),   # 今日（文件尾＝最新）
    ])
    scan = mt.metering_scan(p, days=7, now=NOW)
    j = scan["callers"][DEMO_KEY]
    # 入账＝窗口首日 2 条（10-08T23Z/10-09T00Z 均 CN 10-09）＋今日 2 条（非数字状态/正常）
    assert j["calls"] == 4 and j["success"] == 3 and j["fail"] == 0
    assert j["latCnt"] == 4 and j["latSum"] == 107                       # 脏 ts 行（lat=1）不入均值；非数字状态行 latency=5 正常入均值
    assert GHOST not in scan["callers"]                                  # 提前终止：更旧 caller 不入账
    assert "2026-10-16" not in j["days"]


def test_scan_days_validation_and_today_window():
    """days 只认 1..7；days=1 窗口＝仅今日（东八区）。"""
    p = _write_audit([
        _row(DEMO_KEY, "2026-10-14T02:00:00+00:00"),                        # CN 昨日（时间升序在前）
        _row(DEMO_KEY, "2026-10-15T00:30:00+00:00"),
    ])
    for bad in (0, 8, -1, "7"):
        try:
            mt.metering_scan(p, days=bad, now=NOW)
            raise AssertionError(f"days={bad!r} 应被拒绝")
        except ValueError:
            pass
    today = mt.metering_scan(p, days=1, now=NOW)
    assert today["days"] == ["2026-10-15"]
    assert today["callers"][DEMO_KEY]["calls"] == 1                          # 昨日不入今日窗
    assert mt.metering_scan(Path("/nonexistent/audit.jsonl"), days=7, now=NOW)["callers"] == {}


# ── 2. 视图（掩码＋登记合并＋趋势对齐） ──

def test_view_masks_joins_and_sorts():
    """完整 appkey 不出参；登记凭证带 kid/appName/tenant；未登记如实标注；
    趋势补零对齐窗口；按调用量降序；合计行正确。"""
    doc = {"keys": [{"appkey": DEMO_KEY, "app_name": "演示签发", "tenant": "demo-trial"}]}
    scan = mt.metering_scan(_write_audit([
        _row(DEMO_KEY, "2026-10-15T00:30:00+00:00"),
        _row(GHOST, "2026-10-15T00:31:00+00:00"),
        _row(DEMO, "2026-10-15T00:32:00+00:00"),
        _row(DEMO, "2026-10-15T00:33:00+00:00"),
    ]), days=7, now=NOW)
    view = mt.metering_view(doc, scan)
    assert [c["calls"] for c in view["callers"]] == [2, 1, 1]            # 降序
    by_masked = {c["callerMasked"]: c for c in view["callers"]}
    demo = by_masked["sb_c49d****3f55"]
    assert demo["registered"] and demo["appName"] == "演示签发" and demo["tenant"] == "demo-trial"
    assert demo["kid"] and len(demo["trend"]) == 7 and demo["trend"][-1]["calls"] == 1
    assert all(t["calls"] == 0 for t in demo["trend"][:-1])              # 补零对齐
    assert by_masked["sb_zzzz****0000"]["registered"] is False          # 未登记凭证：掩码＋如实标注
    assert "demo-page…" in by_masked                                    # 非凭证标识：截 12 位＋…
    import json as _j
    assert DEMO_KEY not in _j.dumps(view, ensure_ascii=False)                # 完整 appkey 不出参
    assert view["total"]["calls"] == 4 and view["caliber"]["usageSource"]


# ── 3. 端点契约 ──

def _client(audit_path, keys):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import metering_api
    app = FastAPI()
    app.state.appkeys = keys
    app.state.audit = SimpleNamespace(jsonl=audit_path)
    # #76 Phase 2：metering_api 优先读 Redis 统计快照，测试注入空 mock
    # （stats 键为空→降级回 JSONL 倒序聚合，行为与改造前一致）
    from unittest.mock import AsyncMock, MagicMock
    mock_redis = MagicMock()
    mock_redis.scan = AsyncMock(return_value=(0, []))
    mock_redis.pipeline = MagicMock()
    mock_pipe = MagicMock()
    mock_pipe.hgetall = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(return_value=[])
    mock_redis.pipeline.return_value = mock_pipe
    app.state.redis = mock_redis
    app.include_router(metering_api.router)
    return TestClient(app)


def test_endpoint_contract_ok_and_errors():
    """GET /v1/admin/metering/summary：形状契约＋掩码＋days 越界 400/类型错 422。"""
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（计量端点契约；另有活体 curl 验证）")
    doc_keys = [{"appkey": DEMO_KEY, "app_name": "演示签发", "tenant": "demo-trial"}]
    # 端点内部用真实时钟聚合（metering_api 未注入时钟）——夹具日期必须相对 now 往回推：
    # 昨日/今日两条，跨日且恒落 7 天窗内。原硬编码 2026-10-14/15 实证为时间炸弹：
    # 随今日漂移出窗后，倒读首行触窗界提前终止，2026-10-21 爆（total 0≠2）；
    # 且原夹具两行时间降序违反分片升序口径（倒读正确性前提）。修后恒升序。
    _now = datetime.now(timezone.utc)
    p = _write_audit([
        _row(DEMO_KEY, (_now - timedelta(hours=26)).isoformat()),   # 昨日（相差 24h 恒跨日，窗内恒真）
        _row(DEMO, (_now - timedelta(hours=2)).isoformat()),    # 今日
    ])
    c = _client(p, doc_keys)
    r = c.get("/v1/admin/metering/summary", params={"days": 7})
    assert r.status_code == 200
    j = r.json()
    for k in ("dayKeys", "total", "callers", "caliber", "generatedAt", "days"):
        assert k in j, f"缺字段 {k}"
    assert j["days"] == 7 and len(j["dayKeys"]) == 7
    assert j["total"]["calls"] == 2
    assert {x["callerMasked"] for x in j["callers"]} >= {"sb_c49d****3f55", "demo-page…"}
    assert DEMO_KEY not in r.text                                            # 完整 appkey 不出端点
    assert c.get("/v1/admin/metering/summary", params={"days": 8}).status_code == 400
    assert c.get("/v1/admin/metering/summary", params={"days": 0}).status_code == 400
    bad = c.get("/v1/admin/metering/summary", params={"days": "abc"})
    assert bad.status_code == 422                                        # 参数类型错：框架 422
    assert c.get("/v1/admin/metering/summary", params={"days": 1}).json()["days"] == 1


def test_endpoint_serves_real_service_dimension_from_stats():
    """ADR-0023 第一实施项实证：快照带 svc 字段 → /v1/admin/metering/summary 的
    callers[].services 出真实服务维度（缺陷前快照路径恒空、仅 JSONL 降级路径可得）。

    JSONL 降级源特意放另一 caller（GHOST）——若误降级会出现 GHOST 行，
    断言不出现即证明本响应确实来自快照路径而非回退现算。
    """
    if not _HAS_FASTAPI:
        pytest.skip("依赖缺席：fastapi（计量端点契约；另有活体 curl 验证）")
    from types import SimpleNamespace
    from datetime import timedelta, timezone as _tz
    from unittest.mock import AsyncMock, MagicMock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import metering_api
    tz_cn = _tz(timedelta(hours=8))
    day_str = datetime.now(tz_cn).strftime("%Y%m%d")
    key = f"{mt.STATS_KEY_PREFIX}{day_str}:{DEMO_KEY}"
    snapshot_hash = {
        "calls": "3", "success": "2", "fail": "1", "lat_sum": "40", "lat_cnt": "3",
        "svc:service:/v1/query/a:calls": "2", "svc:service:/v1/query/a:success": "1",
        "svc:service:/v1/query/a:fail": "1", "svc:service:/v1/query/a:lat_sum": "30",
        "svc:service:/v1/query/a:lat_cnt": "2",
        "svc:service:/v1/query/b:calls": "1", "svc:service:/v1/query/b:success": "1",
        "svc:service:/v1/query/b:lat_sum": "10", "svc:service:/v1/query/b:lat_cnt": "1",
    }
    mock_redis = MagicMock()
    mock_redis.scan = AsyncMock(return_value=(0, [key]))
    mock_pipe = MagicMock()
    mock_pipe.hgetall = MagicMock(return_value=mock_pipe)
    mock_pipe.execute = AsyncMock(return_value=[snapshot_hash])
    mock_redis.pipeline.return_value = mock_pipe
    app = FastAPI()
    app.state.appkeys = [{"appkey": DEMO_KEY, "app_name": "演示签发", "tenant": "demo-trial"}]
    app.state.audit = SimpleNamespace(jsonl=_write_audit(
        # 误降级哨兵：本测试 days=1（今日窗）——哨兵行取真实时钟 5 分钟前恒在窗内，
        # 误降级 JSONL 时 GHOST 行必然现形（原硬编码日期出窗后哨兵失效）。
        [_row(GHOST, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
              "service:/v1/fallback-only")]))
    app.state.redis = mock_redis
    app.include_router(metering_api.router)
    c = TestClient(app)
    r = c.get("/v1/admin/metering/summary", params={"days": 1})
    assert r.status_code == 200
    j = r.json()
    assert len(j["callers"]) == 1
    row = j["callers"][0]
    assert row["calls"] == 3 and row["success"] == 2 and row["fail"] == 1
    assert row["services"] == [
        {"endpoint": "service:/v1/query/a", "calls": 2, "success": 1, "fail": 1, "avgMs": 15.0},
        {"endpoint": "service:/v1/query/b", "calls": 1, "success": 1, "fail": 0, "avgMs": 10.0},
    ]
    assert "sb_zzzz" not in r.text        # 哨兵：未发生 JSONL 降级
    assert DEMO_KEY not in r.text             # 完整 appkey 不出端点（掩码纪律随行）


def test_metering_block_migrated_to_reports_zone():
    """1b-9 迁移（设计稿 §5/§7-5）：appkeys 页计量区块整块迁出——#metering-section 与
    计量表不复存在；轻量指引卡在位（一行说明＋「前往报表中心」链接指向主体用量页）；
    appkeys.js 零计量取数（不发计量请求、不载计量函数）。旧端点本身迁移期保留
    （my.html 在读），其契约由 test_endpoint_contract_ok_and_errors 继续守。"""
    html = (REPO / "admin" / "appkeys.html").read_text(encoding="utf-8")
    js = (REPO / "admin" / "appkeys.js").read_text(encoding="utf-8")
    assert 'id="metering-section"' not in html, "计量区块应已整块迁出（设计稿 §7-5）"
    assert 'id="metering-table"' not in html and "mt-bars" not in html
    assert 'id="metering-gone-card"' in html, "缺轻量指引卡占位"
    assert "reports/subjects" in html, "指引卡应含「前往报表中心」链接（主体用量页）"
    assert "loadMetering" not in js, "appkeys.js 计量取数应已撤除"
    assert "/v1/admin/metering/summary" not in js, "appkeys.js 不应再发计量请求"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"{len(fns)} tests passed")
