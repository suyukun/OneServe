#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""我的接入单元测试（P0-7 · SPEC §5 P0-7 行）——纯 python 直跑或 pytest 收集均可。

覆盖（任务卡口径：凭证过滤/日志合并/limit）：
  凭证过滤：按凭证身份（完整 appkey）精确匹配——租户 ID/错凭证不可见；
            视图只出掩码（完整 appkey 与 secret 摘要绝不透出）；
  到期倒计时：≤7 天标黄阈值边界（7 天标黄/8 天不标）、到期日即过期、无有效期不标；
  日志合并：审计 JSONL 按 caller/trace_id 过滤（AND）＋limit 尾窗；
            网关拒记（P0-5 旁路列表）视图化＋反查过滤；
            两源合并按 ts 降序（+00:00/Z 混排安全）＋limit 截断；
            存量七列行缺 trace_id 等价空串、坏行跳过。

被测对象 service/app/my_access.py 不依赖 FastAPI/redis（分层纪律同 appkeys.py）；
my_api.py/main.py 为 FastAPI 薄胶水（宿主机无 fastapi，语法机检口径同 test_appkeys）。
"""
import json
import py_compile
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import my_access as mya   # noqa: E402

TODAY = date(2026, 9, 22)


def _rec(**kw):
    """注册表记录便捷构造（默认沙箱、8 天后到期）。"""
    base = dict(
        appkey="sb_0123456789abcdef", secret_hash="sha256:" + "a" * 64,
        tenant="partner-demo-1", caller_name="演示调用方", app_name="电商标签点查",
        key_type="sandbox", oa_ticket="", tag_sets=[], quota_qps=50, quota_daily=10000,
        status="active", expires_at="2026-09-30", issued_at="2026-09-22T08:00:00+00:00",
        grants=[{"service": "user_tag_query", "endpoint": "/v1/tags/query",
                 "fields": ["f1", "f2", "f3"]}])
    base.update(kw)
    return base


# ---------- ① 凭证过滤：精确匹配＋掩码纪律 ----------

def test_credentials_exact_match_only():
    """按完整凭证精确匹配；租户 ID/别的凭证/空值一概查不到（不可越权）。"""
    doc = {"keys": [_rec(), _rec(appkey="ak_ffffffffffffffff", key_type="formal")]}
    assert len(mya.my_credentials(doc, "sb_0123456789abcdef", today=TODAY)) == 1
    assert mya.my_credentials(doc, "partner-demo-1", today=TODAY) == []          # 租户 ID ≠ 凭证
    assert mya.my_credentials(doc, "sb_0123456789abcdeg", today=TODAY) == []  # 近似错串
    assert mya.my_credentials(doc, "", today=TODAY) == []


def test_credentials_view_masked_no_secret():
    """视图只出掩码：完整 appkey 与 secret 摘要不得出现在输出 JSON。"""
    view = mya.my_credentials({"keys": [_rec()]}, "sb_0123456789abcdef", today=TODAY)[0]
    blob = json.dumps(view, ensure_ascii=False)
    assert view["appkeyMasked"] == "sb_0123****cdef"
    assert "sb_0123456789abcdef" not in blob
    assert "sha256" not in blob
    assert view["keyType"] == "sandbox" and view["status"] == "active"
    assert view["quotaDaily"] == 10000                     # ② 配额四要素的登记真值
    assert view["grants"] == [{"service": "user_tag_query", "fieldsCount": 3}]


def test_expiring_soon_threshold_and_expired():
    """倒计时标黄阈值：≤7 天标黄（7 标/8 不标）；到期日及之后＝已过期（不标黄）。"""
    def view(exp):
        return mya.credential_view(_rec(expires_at=exp), today=TODAY)

    assert mya.EXPIRING_SOON_DAYS == 7
    assert view("2026-09-29")["daysLeft"] == 7 and view("2026-09-29")["expiringSoon"] is True
    assert view("2026-09-30")["daysLeft"] == 8 and view("2026-09-30")["expiringSoon"] is False
    v0 = view("2026-09-22")                                 # 当天到期
    assert v0["status"] == "expired" and v0["expiringSoon"] is False
    v1 = view("2026-09-21")                                 # 已过 1 天
    assert v1["status"] == "expired" and v1["daysLeft"] == -1
    vlong = view("")                                        # 正式凭证可不填有效期
    assert vlong["daysLeft"] is None and vlong["expiringSoon"] is False
    assert vlong["status"] == "active" and vlong["expiresAt"] is None


def test_disabled_status_kept():
    """禁用态如实透出（statusLabel 交给页面人话化）；倒计时照算。"""
    v = mya.credential_view(_rec(status="disabled", expires_at="2026-09-25"), today=TODAY)
    assert v["status"] == "disabled" and v["daysLeft"] == 3
    assert v["expiringSoon"] is True   # 禁用不改变到期倒计时口径


# ---------- ③ 日志：审计尾读＋网关拒记＋合并 ----------

def _audit_line(seq, ts, caller, trace_id=""):
    row = {"ts": ts, "caller": caller, "endpoint": "service:/v1/tags/query",
           "params_digest": "p" * 8, "status": 200, "latency_ms": seq,
           "response_digest": "r" * 8, "seq": seq}
    if trace_id:
        row["trace_id"] = trace_id                          # 存量行可能没有该键
    return json.dumps(row, ensure_ascii=False)


def _write_audit(rows):
    d = tempfile.TemporaryDirectory()
    p = Path(d.name) / "audit.jsonl"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return d, p


def test_audit_tail_filter_by_caller():
    """按凭证过滤：只回该 caller 的行，文件序（旧→新）；traceId 透出＋caller 掩码。"""
    rows = [
        _audit_line(1, "2026-09-22T08:00:01.000+00:00", "ak_ffffffffffffffff"),
        _audit_line(2, "2026-09-22T08:00:02.000+00:00", "sb_0123456789abcdef", "t-002"),
        _audit_line(3, "2026-09-22T08:00:03.000+00:00", "ak_ffffffffffffffff"),
        _audit_line(4, "2026-09-22T08:00:04.000+00:00", "sb_0123456789abcdef", "t-004"),
    ]
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
        items = mya.audit_tail(p, caller="sb_0123456789abcdef", limit=20)
        assert [it["latencyMs"] for it in items] == [2, 4]
        assert items[0]["traceId"] == "t-002" and items[0]["source"] == "service"
        assert items[0]["callerMasked"] == "sb_0123****cdef"


def test_audit_tail_limit_and_trace_filter():
    """limit＝命中尾窗条数；trace_id 精确过滤；caller+trace_id 同给＝AND。"""
    rows = [
        _audit_line(i, f"2026-09-22T08:00:{i:02d}.000+00:00", "sb_0123456789abcdef", f"t-{i:03d}")
        for i in range(1, 6)
    ] + ["{not-json", ""]                                        # 坏行/空行：跳过不抛
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        p.write_text("\n".join(rows) + "\n", encoding="utf-8")
        tail = mya.audit_tail(p, caller="sb_0123456789abcdef", limit=2)
        assert [it["latencyMs"] for it in tail] == [4, 5]       # 尾窗两条，文件序
        hit = mya.audit_tail(p, trace_id="t-003", limit=20)
        assert len(hit) == 1 and hit[0]["traceId"] == "t-003"
        both = mya.audit_tail(p, caller="ak_ffffffffffffffff", trace_id="t-003", limit=20)
        assert both == []                                        # AND 语义
        # 空 trace_id 过滤＝不过滤：5 行全回（存量七列行场景由下行直测）
        assert len(mya.audit_tail(p, trace_id="", limit=20)) == 5


def test_audit_tail_legacy_row_without_trace_id():
    """存量七列行（无 trace_id 键）等价空串，读取不报错。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "audit.jsonl"
        p.write_text(_audit_line(1, "2026-09-22T08:00:01.000+00:00", "old-caller") + "\n",
                     encoding="utf-8")
        items = mya.audit_tail(p, caller="old-caller", limit=20)
        assert items[0]["traceId"] == ""
        assert items[0]["callerMasked"] == "old-caller…"      # 非 sb_/ak_ 截断＋…
    assert mya.audit_tail(Path("/nonexistent/audit.jsonl"), caller="x") == []


def test_gw_rejected_view_and_trace_filter():
    """网关拒记视图：source=gateway、无调用方身份；列表模式截 limit、反查模式精确过滤。"""
    raw = [
        json.dumps({"ts": "2026-09-22T07:59:58Z", "status": 401,
                    "trace_id": "gw-1", "uri": "/v1/tags/query"}),
        json.dumps({"ts": "2026-09-22T07:59:59Z", "status": 401,
                    "trace_id": "gw-2", "uri": "/v1/news/query"}),
        "{broken",
    ]
    items = mya.gw_rejected_view(raw, limit=20)
    assert len(items) == 2 and items[0]["traceId"] == "gw-1"    # 入参序（LPUSH 头端最新）
    assert items[0]["source"] == "gateway" and items[0]["callerMasked"] == ""
    assert items[0]["latencyMs"] is None and items[0]["endpoint"] == "/v1/tags/query"
    limited = mya.gw_rejected_view(raw, limit=1)
    assert len(limited) == 1
    hit = mya.gw_rejected_view(raw, trace_id="gw-2")
    assert len(hit) == 1 and hit[0]["endpoint"] == "/v1/news/query"


def test_merge_logs_mixed_ts_desc_and_limit():
    """合并：审计（+00:00）与网关（Z）混排按时间降序；limit 跨源截断。"""
    service = [
        {"ts": "2026-09-22T08:00:03.000+00:00", "endpoint": "service:/v1/tags/query",
         "status": 200, "latencyMs": 3, "traceId": "t-3", "callerMasked": "sb_0123****cdef",
         "source": "service"},
        {"ts": "2026-09-22T08:00:01.000+00:00", "endpoint": "service:/v1/ping",
         "status": 200, "latencyMs": 1, "traceId": "t-1", "callerMasked": "sb_0123****cdef",
         "source": "service"},
    ]
    gw = [
        {"ts": "2026-09-22T08:00:04Z", "endpoint": "/v1/tags/query", "status": 401,
         "latencyMs": None, "traceId": "gw-4", "callerMasked": "", "source": "gateway"},
        {"ts": "2026-09-22T08:00:02Z", "endpoint": "/v1/news/query", "status": 401,
         "latencyMs": None, "traceId": "gw-2", "callerMasked": "", "source": "gateway"},
    ]
    merged = mya.merge_logs(service, gw, limit=20)
    assert [it["traceId"] for it in merged] == ["gw-4", "t-3", "gw-2", "t-1"]  # 全降序
    assert merged[0]["source"] == "gateway"
    assert [it["traceId"] for it in mya.merge_logs(service, gw, limit=2)] == ["gw-4", "t-3"]
    # 脏 ts 沉底不抛错
    odd = [{"ts": "garbage", "traceId": "odd", "source": "service"}]
    assert [it["traceId"] for it in mya.merge_logs(odd, gw, limit=10)][-1] == "odd"


# ---------- 薄胶水机检（宿主机无 fastapi，同 test_appkeys 口径） ----------

def test_my_api_and_main_compile():
    """my_api.py/main.py 语法机检（FastAPI 编排层，逻辑全在纯函数已覆盖）。"""
    py_compile.compile(str(REPO / "service" / "app" / "my_api.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "main.py"), doraise=True)


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
