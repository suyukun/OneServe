#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-1 审计查询套件（后端 · tests/test_audit_query.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：复用 my_access 日志读法扩条件＋limit/分页；SPEC §2-C1/§5-P1-1）：
  1. 多条件 AND 过滤：caller（部分匹配——C 侧合规检索，区别于 ③ 的 A 侧精确）、
     endpoint（部分匹配）、trace_id（精确）、status（2xx/4xx/5xx 类或精确码）；
  2. 时间窗：含端闭区间；纯日期按 UTC 全天（作上界＝含当日 23:59:59.999999）；
     带时区偏移的完整 ISO 原样比较；非法格式/起止颠倒 → ValueError（端点转 400）；
  3. 分页：新在前；offset 窗口切片正确（含越过末页→空页）；total 与页内容无关；
     limit clamp 1..100；
  4. 统计摘要（按全部命中行聚合，与翻页无关）：总量/成功/失败；成功率小基数口径
     （total<AUDIT_RATE_SAMPLE_MIN → None＝页面标「样本量不足」）；慢查询数
     （>200ms，SLA P99 同源）；慢查询 Top 端点（均耗时降序 ≤5 条）；
  5. 掩码纪律：items 全量 JSON 不含完整 sb_/ak_ 凭证（callerMasked 出参）；
  6. 容错：坏行跳过；存量七列行缺 trace_id 等价空串；
  7. 胶水机检（宿主机无 fastapi，同 test_my_access 口径）：audit_api.py py_compile
     ＋ AST 契约（路由路径/参数表/委托 my_access.audit_query/ValueError→400）
     ＋ main.py 挂载 audit_router。
"""
from __future__ import annotations

import ast
import json
import py_compile
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import my_access as mya   # noqa: E402


def _line(seq, ts, caller, endpoint, status, latency, trace_id="", **extra):
    row = {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "p" * 64,
           "status": status, "latency_ms": latency, "response_digest": "r" * 64,
           "trace_id": trace_id, "seq": seq}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def _fixture(rows):
    """写临时审计 JSONL，返回路径（复用 my_access 读法口径）。"""
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    f.write("\n".join(rows) + "\n")
    f.close()
    return Path(f.name)


def _seed():
    """时间递增的六行样例：两调用方×两端点×成败×快慢（第 6 行慢 300ms）。"""
    return [
        _line(1, "2026-09-22T01:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/tags/query", 200, 5),
        _line(2, "2026-09-22T02:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/news/query", 403, 3),
        _line(3, "2026-09-22T03:00:00.000+00:00", "ak_deadbeefdeadbeef", "service:/v1/tags/query", 200, 8, trace_id="T3"),
        _line(4, "2026-09-23T04:00:00.000+00:00", "demo-page", "service:/demo/query", 400, 2),
        _line(5, "2026-09-23T05:00:00.000+00:00", "ak_deadbeefdeadbeef", "service:/v1/tags/query", 500, 250, trace_id="T5"),
        _line(6, "2026-09-23T06:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/tags/query", 200, 300, trace_id="T6"),
    ]


# ── 1. 多条件 AND 过滤 ──

def test_filter_caller_substring_c_side():
    """caller 部分匹配（C 侧合规检索）：凭片段搜人；A 侧 ③ 的精确匹配语义不受影响。"""
    p = _fixture(_seed())
    r = mya.audit_query(p, caller="sb_aaaa")
    assert r["total"] == 3 and all("sb_aaaa****1111" == it["callerMasked"] for it in r["items"])
    r2 = mya.audit_query(p, caller="deadbeef")
    assert r2["total"] == 2
    r3 = mya.audit_query(p, caller="nobody")
    assert r3["total"] == 0 and r3["items"] == []


def test_filter_endpoint_substring_and_trace_exact():
    p = _fixture(_seed())
    assert mya.audit_query(p, endpoint="news") ["total"] == 1
    assert mya.audit_query(p, endpoint="/v1/tags/query")["total"] == 4
    assert mya.audit_query(p, trace_id="T5")["total"] == 1
    assert mya.audit_query(p, trace_id="T")["total"] == 0      # 精确，不做包含
    # AND 组合：caller×endpoint×状态类
    r = mya.audit_query(p, caller="sb_aaaa", endpoint="tags", status="2xx")
    assert r["total"] == 2


def test_filter_status_class_and_exact():
    p = _fixture(_seed())
    assert mya.audit_query(p, status="2xx")["total"] == 3
    assert mya.audit_query(p, status="4xx")["total"] == 2
    assert mya.audit_query(p, status="5xx")["total"] == 1
    assert mya.audit_query(p, status="403")["total"] == 1
    assert mya.audit_query(p, status="")["total"] == 6         # 空＝不过滤


# ── 2. 时间窗 ──

def test_time_window_inclusive_and_date_only():
    p = _fixture(_seed())
    # 含端闭：[01:00, 03:00] 命中 1..3 行
    r = mya.audit_query(p, ts_from="2026-09-22T01:00:00.000+00:00",
                        ts_to="2026-09-22T03:00:00.000+00:00")
    assert r["total"] == 3
    # 纯日期按东八区全天（与 stats.py「今日」同口径）：东八区 09-22 一天＝
    # UTC [09-21T16:00, 09-22T16:00) → 行 1..3
    assert mya.audit_query(p, ts_from="2026-09-22", ts_to="2026-09-22")["total"] == 3
    # 带时区偏移完整 ISO：东八区 [09-22T08:00, 09-23T08:00] ＝ UTC [09-22T00, 09-23T00]
    # → 行 1..3（09-23 的行都在 UTC 09-23T04 点后，窗外）
    assert mya.audit_query(p, ts_from="2026-09-22T08:00:00+08:00",
                           ts_to="2026-09-23T08:00:00+08:00")["total"] == 3


def test_time_bad_format_and_reversed_raise():
    p = _fixture(_seed())
    for bad in ("2026-13-99", "not-a-time"):
        try:
            mya.audit_query(p, ts_from=bad)
            raise AssertionError("非法时间应抛 ValueError：" + bad)
        except ValueError:
            pass
    try:
        mya.audit_query(p, ts_from="2026-09-23", ts_to="2026-09-22")
        raise AssertionError("起止颠倒应抛 ValueError")
    except ValueError:
        pass


def test_date_only_cn_day_boundary():
    """纯日期边界＝东八区日界（非 UTC）：09-23T18:00Z 已是东八区 09-24 凌晨，窗不含。

    （UTC 口径会把该行算进 09-23——此测试钉死与 stats.py「今日」同源的东八区滚动。）
    """
    rows = [
        _line(1, "2026-09-23T15:30:00.000+00:00", "ak_k", "service:/v1/ping", 200, 1),   # 东八区 23:30
        _line(2, "2026-09-23T18:00:00.000+00:00", "ak_k", "service:/v1/ping", 200, 1),   # 东八区 09-24 02:00
    ]
    p = _fixture(rows)
    assert mya.audit_query(p, ts_from="2026-09-23", ts_to="2026-09-23")["total"] == 1
    assert mya.audit_query(p, ts_from="2026-09-24", ts_to="2026-09-24")["total"] == 1


def test_status_bad_raises():
    p = _fixture(_seed())
    try:
        mya.audit_query(p, status="ok")
        raise AssertionError("非法状态条件应抛 ValueError")
    except ValueError:
        pass


# ── 3. 分页 ──

def test_pagination_newest_first_and_slices():
    p = _fixture(_seed())
    r0 = mya.audit_query(p, limit=2, offset=0)
    assert r0["total"] == 6
    assert [it["ts"] for it in r0["items"]] == ["2026-09-23T06:00:00.000+00:00",
                                                 "2026-09-23T05:00:00.000+00:00"]
    r1 = mya.audit_query(p, limit=2, offset=2)
    assert [it["ts"] for it in r1["items"]] == ["2026-09-23T04:00:00.000+00:00",
                                                 "2026-09-22T03:00:00.000+00:00"]
    # 越过末页：空页但 total 完整（分页导航口径）
    r5 = mya.audit_query(p, limit=2, offset=6)
    assert r5["items"] == [] and r5["total"] == 6
    # 尾窗有界：大 offset 下页内容仍正确（deque 尾窗切片语义）
    r6 = mya.audit_query(p, limit=4, offset=4)
    assert [it["ts"] for it in r6["items"]] == ["2026-09-22T02:00:00.000+00:00",
                                                 "2026-09-22T01:00:00.000+00:00"]


def test_limit_clamped():
    p = _fixture(_seed())
    assert mya.audit_query(p, limit=0)["limit"] == 1
    assert mya.audit_query(p, limit=999)["limit"] == mya.AUDIT_QUERY_MAX_LIMIT
    assert mya.audit_query(p, offset=-5)["offset"] == 0


# ── 4. 统计摘要 ──

def test_summary_counts_over_all_matches_not_page():
    p = _fixture(_seed())
    r = mya.audit_query(p, limit=2, offset=0)         # 只看第 1 页，摘要仍是全量口径
    s = r["summary"]
    assert s["total"] == 6 and s["success"] == 3 and s["fail"] == 3
    assert s["successRate"] is None                    # 6 < 100：小基数不出比率
    assert s["rateSampleMin"] == mya.AUDIT_RATE_SAMPLE_MIN == 100


def test_summary_success_rate_when_sample_enough():
    rows = [_line(i, f"2026-09-22T0{i}:00:00.000+00:00", "ak_kk", "service:/v1/ping",
                  200 if i % 2 else 500, 1) for i in range(1, 101)]
    p = _fixture(rows)
    s = mya.audit_query(p)["summary"]
    assert s["total"] == 100 and s["successRate"] == 50.0


def test_summary_slow_and_top_endpoints():
    p = _fixture(_seed())
    s = mya.audit_query(p)["summary"]
    assert s["slowMs"] == mya.AUDIT_SLOW_MS == 200
    assert s["slowCount"] == 2                          # 250ms 与 300ms 两行
    # tags/query 均值 (5+8+250+300)/4=140.75 > demo 2 > news 3？news=3>demo=2 → 次序断言
    assert [t["endpoint"] for t in s["slowTop"]] == [
        "service:/v1/tags/query", "service:/v1/news/query", "service:/demo/query"]
    top = s["slowTop"][0]
    assert top["count"] == 4 and top["avgMs"] == 140.8 and top["maxMs"] == 300


def test_summary_slow_top_capped_at_five():
    rows = [_line(i, f"2026-09-22T01:00:0{i}.000+00:00", "ak_k", f"service:/v1/ep{i}", 200, i)
            for i in range(1, 9)]
    p = _fixture(rows)
    top = mya.audit_query(p)["summary"]["slowTop"]
    assert len(top) == mya.AUDIT_SLOW_TOP_N == 5
    assert [t["endpoint"] for t in top] == [f"service:/v1/ep{i}" for i in range(8, 3, -1)]


# ── 5. 掩码纪律 ──

def test_items_masked_no_full_credential():
    p = _fixture(_seed())
    blob = json.dumps(mya.audit_query(p), ensure_ascii=False)
    assert "sb_aaaabbbbcccc1111" not in blob
    assert "ak_deadbeefdeadbeef" not in blob
    assert "sb_aaaa****1111" in blob and "ak_dead****beef" in blob   # 掩码可辨识


# ── 6. 容错 ──

def test_bad_lines_skipped_and_legacy_seven_cols():
    rows = _seed()
    legacy = json.dumps({"ts": "2026-09-23T07:00:00.000+00:00", "caller": "old-caller-xx",
                         "endpoint": "service:/v1/ping", "params_digest": "p", "status": 200,
                         "latency_ms": 1, "response_digest": "r", "seq": 7})   # 无 trace_id
    p = _fixture(rows + ["{broken json", "", legacy])
    r = mya.audit_query(p, endpoint="ping")
    assert r["total"] == 1 and r["items"][0]["traceId"] == ""   # 存量七列＝空串口径
    assert r["items"][0]["callerMasked"] == "old-caller-x…"


def test_missing_file_returns_empty():
    r = mya.audit_query(Path("/nonexistent/audit.jsonl"))
    assert r["total"] == 0 and r["items"] == []
    assert r["summary"]["successRate"] is None


# ── 7. 胶水机检：audit_api.py（宿主机无 fastapi，同 test_my_access 口径） ──

def test_audit_api_module_compiles():
    py_compile.compile(str(REPO / "service" / "app" / "audit_api.py"), doraise=True)


def test_audit_api_contract_by_ast():
    """AST 契约：路由路径/八参签名/委托 my_access.audit_query/ValueError→400/补 generatedAt。"""
    src = (REPO / "service" / "app" / "audit_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    def _is_router_get(d) -> bool:
        return (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                and d.func.attr == "get" and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "router")
    routes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and any(_is_router_get(d) for d in n.decorator_list)]
    assert len(routes) == 1, "audit_api 应只暴露一个 GET 端点"
    fn = routes[0]
    route_path = next(d.args[0].value for d in fn.decorator_list if _is_router_get(d))
    assert route_path == "/v1/portal/audit/logs"
    params = {a.arg for a in fn.args.args} - {"request"}
    assert {"ts_from", "ts_to", "caller", "endpoint", "status",
            "trace_id", "limit", "offset"} <= params
    blob = src
    assert "mya.audit_query" in blob and "request.app.state.audit.jsonl" in blob
    assert "ValueError" in blob and "400" in blob and "generatedAt" in blob


def test_main_registers_audit_router():
    src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.audit_api import router as audit_router" in src
    assert "app.include_router(audit_router)" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except Exception as exc:   # noqa: BLE001
            failed += 1
            print(f"  ✗ {fn.__name__}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
