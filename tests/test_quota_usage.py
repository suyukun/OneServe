#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-3 配额用量套件（后端 · tests/test_quota_usage.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：四要素接真＋B6 按调用方看用量；SPEC §5-P1-3）：
  1. 聚合正确性：多 caller 隔离（A 的用量不进 B）、跨日滚动（东八区日界：今日 23:59
     与次日 00:00 分属两日）、429 限流拒绝也计入已用（被拒也是一次真实调用）；
  2. 端点掩码纪律：caller 精确匹配——掩码片段（sb_aaa1****1111）/前缀/子串不得命中
     真凭证（配额是「我的」与运营台共用的真值，片段匹配会张冠李戴）；
  3. 四要素计算边界：0 已用（剩余＝上限、ratio=0）、满额（剩余=0、ratio=100、
     exceeded=False——刚好用完不算超）、超额（剩余下限 0、exceeded=True）、
     重置时刻＝东八区次日 00:00（跨 UTC 日界钉死）；
  4. 如实口径：未登记限额（quota_daily 缺失/非法）→ limit/remaining=None 而非 0；
     未知凭证/空 caller → found=False（查无此凭证 ≠ 今天没调用）；
  5. 容错：坏行/脏 ts 跳过不抛、文件不存在＝全零、趋势 7 日连续补 0；
  6. 胶水机检（宿主机无 fastapi，同 test_my_access 口径）：my_api.py/admin_api.py
     py_compile ＋ AST 契约（路由路径/参数/委托 my_access 纯函数）。
"""
from __future__ import annotations

import ast
import json
import py_compile
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import my_access as mya   # noqa: E402

_TZ_CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 23, 10, 30, 0, tzinfo=_TZ_CN)     # 东八区 2026-09-23 10:30

A = "sb_aaa1111bbbb2222"
B = "ak_bbb2222cccc3333"


def _line(ts, caller, status=200, latency=3, **extra):
    row = {"ts": ts, "caller": caller, "endpoint": "service:/v1/ping",
           "params_digest": "p" * 64, "status": status, "latency_ms": latency,
           "response_digest": "r" * 64, "trace_id": "t", "seq": 1}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def _fixture(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    f.write("\n".join(rows) + "\n")
    f.close()
    return Path(f.name)


def _doc(**over):
    base = [{"appkey": A, "app_name": "电商", "key_type": "sandbox", "status": "active",
             "quota_qps": 50, "quota_daily": 100},
            {"appkey": B, "app_name": "风控平台", "key_type": "formal", "status": "active",
             "quota_qps": 5, "quota_daily": 10}]
    return over.get("doc") or {"keys": base}


# ---------- 1. 聚合正确性 ----------

def test_multi_caller_isolated():
    """多 caller 隔离：A 五条、B 两条，互不串数；未登记的 caller 不建桶。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A) for _ in range(5)]
    rows += [_line("2026-09-23T03:00:00+00:00", B) for _ in range(2)]
    rows.append(_line("2026-09-23T04:00:00+00:00", "sb_unregistered9999"))
    p = _fixture(rows)
    got = mya.usage_scan(p, [A, B], now=NOW)
    assert got[A]["used"] == 5, got[A]
    assert got[B]["used"] == 2, got[B]
    assert "sb_unregistered9999" not in got        # 未要的 caller 不落内存


def test_day_rollover_at_cn_midnight():
    """跨日滚动：东八区日界钉死——00:29（东八区）归今日，昨日 23:59 归昨日。"""
    rows = [
        _line("2026-09-22T15:59:00+00:00", A),      # 东八区 09-22 23:59 → 昨日
        _line("2026-09-22T16:01:00+00:00", A),      # 东八区 09-23 00:01 → 今日
        _line("2026-09-23T01:00:00+00:00", A),      # 东八区 09-23 09:00 → 今日
    ]
    p = _fixture(rows)
    got = mya.usage_scan(p, [A], now=NOW)
    assert got[A]["day"] == "2026-09-23"
    assert got[A]["used"] == 2, got[A]
    trend = {d["date"]: d["used"] for d in got[A]["trend"]}
    assert trend["2026-09-22"] == 1 and trend["2026-09-23"] == 2


def test_429_and_failures_count_as_used():
    """429 也计入已用（被限流拒绝也是一次真实调用）；4xx/5xx 一并按条计。"""
    rows = [
        _line("2026-09-23T02:00:00+00:00", A, status=200),
        _line("2026-09-23T02:01:00+00:00", A, status=429),
        _line("2026-09-23T02:02:00+00:00", A, status=403),
        _line("2026-09-23T02:03:00+00:00", A, status=500),
    ]
    p = _fixture(rows)
    assert mya.usage_scan(p, [A], now=NOW)[A]["used"] == 4
    q = mya.my_quota(_doc(), A, p, now=NOW)["quota"]
    assert q["used"] == 4 and q["remaining"] == 96


# ---------- 2. 端点掩码纪律：caller 精确匹配 ----------

def test_caller_exact_match_only():
    """掩码片段/前缀/子串一律不得命中真凭证（片段匹配＝张冠李戴）。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A) for _ in range(3)]
    p = _fixture(rows)
    for probe in (A[:7] + "****" + A[-4:], A[:7], "sb_", "aaa1111", A + "x", A[:-1]):
        scan = mya.usage_scan(p, [probe], now=NOW)
        assert scan[probe]["used"] == 0, (probe, scan)
        assert mya.my_quota(_doc(), probe, p, now=NOW)["found"] is False, probe
    assert mya.usage_scan(p, [A], now=NOW)[A]["used"] == 3      # 真凭证照常命中


# ---------- 3. 四要素计算边界 ----------

def test_quota_zero_used():
    """0 已用：剩余＝上限、ratio=0、exceeded=False（审计为空＝真的没调用，不是缺数）。"""
    q = mya.quota_view(100, {"used": 0, "trend": []})
    assert q["used"] == 0 and q["remaining"] == 100
    assert q["ratio"] == 0.0 and q["exceeded"] is False and q["registered"] is True


def test_quota_exact_limit_then_over():
    """满额（正好用完）不算超；超额一分即 exceeded，剩余下限 0 不显示负数。"""
    full = mya.quota_view(100, {"used": 100, "trend": []})
    assert full["remaining"] == 0 and full["ratio"] == 100.0 and full["exceeded"] is False
    over = mya.quota_view(100, {"used": 101, "trend": []})
    assert over["remaining"] == 0 and over["exceeded"] is True


def test_reset_at_is_next_cn_midnight():
    """重置时刻＝东八区次日 00:00（UTC 日界不等于业务日界，此处钉死）。"""
    r = mya.reset_at(datetime(2026, 9, 23, 23, 59, 0, tzinfo=_TZ_CN))
    assert r.isoformat() == "2026-09-24T00:00:00+08:00"
    # UTC 侧 09-23T15:00 ＝ 东八区 09-23T23:00：UTC 日界与业务日界分离处最易错——
    # 重置须按东八区算（1 小时后），不是按 UTC 日界（9 小时后）。
    r2 = mya.reset_at(datetime(2026, 9, 23, 15, 0, 0, tzinfo=timezone.utc))
    assert r2.isoformat() == "2026-09-24T00:00:00+08:00"
    assert (r2 - datetime(2026, 9, 23, 15, 0, 0, tzinfo=timezone.utc)) == timedelta(hours=1)
    # 反向：UTC 侧已是 09-24T02:00（东八区 09-24T10:00）→ 重置指向 09-25
    r3 = mya.reset_at(datetime(2026, 9, 24, 2, 0, 0, tzinfo=timezone.utc))
    assert r3.isoformat() == "2026-09-25T00:00:00+08:00"


def test_my_quota_four_elements_boundary():
    """四要素端到端：登记 100，当日真实 3 条 → 已用 3 / 剩余 97 / 重置次日 00:00。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A) for _ in range(3)]
    p = _fixture(rows)
    v = mya.my_quota(_doc(), A, p, now=NOW)
    assert v["found"] is True and v["callerMasked"] == "sb_aaa1****2222"
    assert v["quota"]["limit"] == 100 and v["quota"]["used"] == 3
    assert v["quota"]["remaining"] == 97 and v["quota"]["ratio"] == 3.0
    assert v["resetAt"] == "2026-09-24T00:00:00+08:00"
    assert A not in json.dumps(v, ensure_ascii=False)       # 完整凭证不出视图


# ---------- 4. 如实口径：未登记/未知凭证 ----------

def test_unregistered_limit_is_null_not_zero():
    """限额未登记 → limit/remaining 为 None（不是 0）；已用仍按真实条数给。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A)]
    p = _fixture(rows)
    for bad in (None, "", "abc"):
        q = mya.quota_view(bad, {"used": 1, "trend": []})
        assert q["limit"] is None and q["remaining"] is None and q["registered"] is False
        assert q["used"] == 1                                # 用量是真值，不受登记与否影响
    doc = {"keys": [{"appkey": A, "quota_daily": None}]}
    assert mya.my_quota(doc, A, p, now=NOW)["quota"]["limit"] is None


def test_unknown_or_empty_caller_is_empty_state():
    """未知/空 caller＝found=False 如实空态（不回「0 已用」伪装成今天没调用）。"""
    p = _fixture([_line("2026-09-23T02:00:00+00:00", A)])
    assert mya.my_quota(_doc(), "", p, now=NOW)["found"] is False
    unknown = mya.my_quota(_doc(), "sb_zzzz9999", p, now=NOW)
    assert unknown["found"] is False and unknown["quota"] is None
    assert "caliber" in unknown                              # 空态也带口径说明


# ---------- 5. 容错与趋势 ----------

def test_scan_fault_tolerance_and_trend_zero_fill():
    """坏行/脏 ts 跳过不抛；文件不存在＝全零；趋势 7 日连续补 0（迷你趋势不跳点）。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A), "{broken", "",
            _line("not-a-time", A), _line("2026-09-23T02:05:00+00:00", B)]
    p = _fixture(rows)
    got = mya.usage_scan(p, [A, B], now=NOW)
    assert got[A]["used"] == 1 and got[B]["used"] == 1       # 脏 ts 行不归入任何一日
    assert len(got[A]["trend"]) == mya.QUOTA_TREND_DAYS == 7
    assert [d["date"] for d in got[A]["trend"]][-1] == "2026-09-23"   # 升序，今日在末
    assert mya.usage_scan(Path("/nonexistent/audit.jsonl"), [A], now=NOW)[A]["used"] == 0
    assert mya.usage_scan(p, [], now=NOW) == {}


def test_admin_usage_view_masks_appkey():
    """B6 运营台：一次扫描全表，出参只带 kid＋掩码（完整 appkey 不出端点）。"""
    rows = [_line("2026-09-23T02:00:00+00:00", A) for _ in range(12)]   # 超 B 的 10 次限额
    rows += [_line("2026-09-23T03:00:00+00:00", B)]
    p = _fixture(rows)
    out = mya.admin_usage(_doc(), p, now=NOW)
    blob = json.dumps(out, ensure_ascii=False)
    assert A not in blob and B not in blob
    by_kid = {i["kid"]: i for i in out["items"]}
    assert len(by_kid) == 2
    a_item = by_kid[mya.appk.record_kid(A)]
    assert a_item["used"] == 12 and a_item["limit"] == 100 and a_item["exceeded"] is False
    assert a_item["appkeyMasked"] == "sb_aaa1****2222"
    b_item = by_kid[mya.appk.record_kid(B)]
    assert b_item["used"] == 1 and b_item["remaining"] == 9
    assert out["resetAt"] == "2026-09-24T00:00:00+08:00"
    assert out["caliber"]["limitEnforced"] is True           # P1-5：网关已按登记值强制拦截


def test_caliber_fields_present():
    """口径说明字段齐备（页面文案唯一来源；改口径只改后端一处）。"""
    c = mya.QUOTA_CALIBER
    for k in ("usageSource", "usageIncludes", "limitSource", "limitEnforced", "resetAt"):
        assert k in c, k
    assert c["limitEnforced"] is True                        # P1-5：登记值＝网关拦截值（同源）
    assert "gatewayEnforcedBy" in c
    assert c["trendDays"] == 7


# ---------- 6. 薄胶水机检（宿主机无 fastapi，同 test_my_access 口径） ----------

def _routes(path: str):
    src = (REPO / "service" / "app" / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "get" and dec.args
                    and isinstance(dec.args[0], ast.Constant)):
                out[dec.args[0].value] = node
    return out


def test_my_quota_glue_contract():
    """my_api.py：GET /v1/portal/my/quota 存在且委托 my_access.my_quota 纯函数。"""
    py_compile.compile(str(REPO / "service" / "app" / "my_api.py"), doraise=True)
    fn = _routes("my_api.py").get("/v1/portal/my/quota")
    assert fn is not None, "缺 GET /v1/portal/my/quota 路由"
    assert [a.arg for a in fn.args.args] == ["request", "caller"]
    src = ast.unparse(fn)
    assert "mya.my_quota(" in src, "端点须委托纯函数 my_access.my_quota"
    # P1-2：误机敲码收敏到错误码中心（app/errors.py），这里改撮常量引用不做字面量断言
    assert "errors.MISSING_CALLER" in src, "空 caller 须 400 人话就近提示（误码取中心常量）"


def test_admin_usage_glue_contract():
    """admin_api.py：GET /v1/admin/appkeys/usage 存在、委托同一纯函数。

    2026-09-23：原断言还有一条「_token_ok in src（须过 X-Admin-Token 闸）」。
    管理令牌机制已按老板决定**整体拆除**（本机/内网阶段网络层即边界，认证归 M3 接入
    公司内网时统一做），故该断言随机制一并下线；本测试保留的契约是「运营台与门户
    复用同一个纯函数 admin_usage（同口径）」，这条与令牌无关、必须继续守。
    2026-10-14 凭证管理补全（AC-6）：路由改委托 appkeys.usage_view——包装函数内部
    仍调 mya.admin_usage（同口径不动），只增补 lastUsedAt/everUsed 两字段；本测试
    双侧守卫：路由委托 usage_view ＋ 包装体确实调 admin_usage（口径不旁路）。
    """
    py_compile.compile(str(REPO / "service" / "app" / "admin_api.py"), doraise=True)
    fn = _routes("admin_api.py").get("/v1/admin/appkeys/usage")
    assert fn is not None, "缺 GET /v1/admin/appkeys/usage 路由"
    src = ast.unparse(fn)
    assert "appk.usage_view(" in src, "运营台须委托 appkeys.usage_view（AC-6 增补口径）"
    wrap = (REPO / "service" / "app" / "appkeys.py").read_text(encoding="utf-8")
    assert "mya.admin_usage(" in wrap, "usage_view 内部须仍走 my_access.admin_usage（同口径不旁路）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except AssertionError:
            failed += 1
            import traceback
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
