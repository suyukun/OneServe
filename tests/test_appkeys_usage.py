#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AppKey 用量聚合套件（2026-10-14 门槛稿 §C TASK-4 / AC-6）——纯 python 直跑或 pytest 收集均可。

数据源（NC-1 最省路径）：审计 JSONL 既有真源一次扫描——今日次数（CN 日界，与
my_access.usage_scan 同口径）/最近调用时间（lastUsedAt）/累计条数（total，兼作
受限删的调用计数）。不建第二份 Redis 计数器（冷启动清零会假阴）。

覆盖：scan_usage 三口径＋CN 日界；文件缺失＝全零；usage_view（AC-6）增补
lastUsedAt/everUsed 且今日次数/上限与门户同口径；「从未使用」标记；删除闸门联动。
"""
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import appkeys as ak            # noqa: E402

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)   # 北京时间 18:00（周三）
CALLER_A = "sb_aaaa1111aaaa1111"
CALLER_B = "sb_bbbb2222bbbb2222"


def _write_audit(lines):
    d = tempfile.mkdtemp(prefix="ak-usage-")
    p = Path(d) / "audit.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in lines) + "\n", encoding="utf-8")
    return p


def test_scan_usage_today_last_and_total_with_cn_boundary():
    """今日次数按北京时间日界；lastUsedAt＝可解析 ts 的最大值；total＝累计条数；
    坏行/脏 ts 跳过不造数；无关 caller 不命中。"""
    p = _write_audit([
        {"caller": CALLER_A, "ts": "2026-10-14T09:00:00+00:00"},        # 今日（CN 17:00）
        {"caller": CALLER_A, "ts": "2026-10-14T09:30:00.123+00:00"},    # 今日（毫秒格式）
        {"caller": CALLER_A, "ts": "2026-10-13T01:00:00+00:00"},        # 昨日（CN 10-13 09:00）
        {"caller": CALLER_A, "ts": "2026-10-14T17:30:00+00:00"},        # CN 10-15 01:30 → 非今日
        {"caller": CALLER_A, "ts": "garbage"},                          # 脏 ts：不计任何口径
        {"caller": "sb_other", "ts": "2026-10-14T09:00:00+00:00"},      # 无关 caller
    ])
    p.write_text(p.read_text(encoding="utf-8") + "{坏行不是json\n", encoding="utf-8")
    out = ak.scan_usage(p, [CALLER_A, CALLER_B], now=NOW)
    a = out[CALLER_A]
    assert a["today"] == 2                       # 昨日行与跨 CN 日界行都不入今日
    assert a["total"] == 4                       # 脏 ts/坏行/无关 caller 不计
    assert a["lastUsedAt"] == "2026-10-14T17:30:00+00:00"   # 可解析最大 ts
    b = out[CALLER_B]
    assert b == {"today": 0, "lastUsedAt": None, "total": 0}


def test_scan_usage_missing_file_is_all_zero():
    """审计文件不存在＝全零（未产生调用，非错误——受限删/从未使用判定的依据）。"""
    out = ak.scan_usage(Path("/nonexistent/audit.jsonl"), [CALLER_A], now=NOW)
    assert out[CALLER_A] == {"today": 0, "lastUsedAt": None, "total": 0}


def test_usage_view_items_match_ac6():
    """usage_view（AC-6）：items 按 kid 索引带 used/limit（门户同口径）＋lastUsedAt/everUsed；
    从未调用的凭证 used=0、lastUsedAt=None、everUsed=False（「从未使用」可判）。"""
    from synth_services import synth_matrix, synth_services_dict   # noqa: E402
    services = synth_services_dict()
    matrix = synth_matrix()
    now = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
    doc = {"keys": []}
    rec_a, _ = ak.issue_appkey(doc, matrix, services, tenant="partner-demo-1",
                               app_name="A", key_type="sandbox", now=now)
    rec_b, _ = ak.issue_appkey(doc, matrix, services, tenant="partner-demo-2",
                               app_name="B", key_type="sandbox", now=now)
    doc["keys"] += [rec_a, rec_b]
    p = _write_audit([
        {"caller": rec_a["appkey"], "ts": "2026-10-14T01:00:00+00:00"},   # CN 09:00 今日
        {"caller": rec_a["appkey"], "ts": "2026-10-14T02:00:00+00:00"},   # CN 10:00 今日
        {"caller": rec_a["appkey"], "ts": "2026-10-11T01:00:00+00:00"},   # 三天前（只入 total/lastUsed 候选）
    ])
    view = ak.usage_view(doc, p, now=NOW)
    by_kid = {it["kid"]: it for it in view["items"]}
    a = by_kid[ak.record_kid(rec_a["appkey"])]
    b = by_kid[ak.record_kid(rec_b["appkey"])]
    assert a["used"] == 2 and a["limit"] == rec_a["quota_daily"]      # 今日次数＋登记上限（同门口径）
    assert a["lastUsedAt"] == "2026-10-14T02:00:00+00:00"
    assert a["everUsed"] is True
    assert b["used"] == 0 and b["everUsed"] is False and b["lastUsedAt"] is None   # 从未使用
    assert view.get("caliber")                                        # 口径说明随行


def test_delete_gate_feeds_from_scan_total():
    """AC-2 联动：受限删的调用计数取 scan_usage 的 total——聚合>0 → 删除拒绝。"""
    from synth_services import synth_matrix, synth_services_dict   # noqa: E402
    services = synth_services_dict()
    matrix = synth_matrix()
    now = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
    doc = {"keys": []}
    rec, _ = ak.issue_appkey(doc, matrix, services, tenant="partner-demo-1",
                             app_name="A", key_type="sandbox", now=now)
    doc["keys"].append(rec)
    p = _write_audit([{"caller": rec["appkey"], "ts": "2026-10-10T01:00:00+00:00"}])  # 仅历史调用
    total = ak.scan_usage(p, [rec["appkey"]], now=NOW)[rec["appkey"]]["total"]
    assert total == 1                                                 # 历史调用也断链风险＝计入
    try:
        ak.delete_appkey(doc, rec["appkey"], used_count=total)
        raise AssertionError("有历史调用记录应拒绝删除")
    except ak.AppkeyIssueError as exc:
        assert exc.status == 409 and exc.code == "appkey_has_calls"
