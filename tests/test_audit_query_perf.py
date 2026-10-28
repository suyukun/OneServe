#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计查询提速语义测试（2026-10-14 轻量版改造）——tests/test_audit_query_perf.py。

背景（盘点实证）：审计 JSONL 392MB/105 万行，audit_query 原先每次全文件正序线性扫；
本批改造＝倒序分块读（_read_chunks_reverse，4MB 块）＋带 ts_from 时提前终止。
本套件钉死三条语义，防回归也防「提速改坏口径」：
  1. 倒序正确性：产出顺序＝时间新→旧（最旧/最新可辨）；翻页与过滤结果
     与正序语义完全一致（对照「文件正序逐行过滤」的朴素参考实现，逐页比对）；
  2. 提前终止证据：monkeypatch _read_chunks_reverse 计数读取字节数——
     带 ts_from 的近窗查询读取量 < 全量（严格小于），且时间窗越近读得越少；
     无 ts_from 的查询为保证 total 全量口径，仍读全文件（读取量＝全量）；
  3. total/summary 口径不变：提前终止只发生在「更旧行不可能再命中」之后，
     带时间窗的 total/summary 与朴素全扫参考完全一致（不许默默变语义）。

纯 python 直跑或 pytest 收集均可（同 test_audit_query.py 口径，无三方依赖）。
"""
from __future__ import annotations

import json
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
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    f.write(chr(10).join(rows) + chr(10))
    f.close()
    return Path(f.name)


def _seed_lines(n):
    """n 行时间严格递增样例：第 i 行 ts＝09-01 起 i 小时整点（跨天递增，日期始终合法，
    时间序＝文件序——倒读正确性与时间窗过滤的前提）。"""
    return [_line(i + 1, f"2026-09-{1 + i // 24:02d}T{i % 24:02d}:00:00.000+00:00",
                  f"ak_key{i:04d}", f"service:/v1/ep{i % 3}", 200 if i % 4 else 403,
                  5 + i % 250) for i in range(n)]


def _naive_query(path, **kw):
    """朴素参考：正序逐行全扫＋原语义（offset 尾窗反切）——改造前的行为基线。"""
    import collections
    limit = min(max(int(kw.get("limit", mya.AUDIT_QUERY_DEFAULT_LIMIT)), 1), mya.AUDIT_QUERY_MAX_LIMIT)
    offset = max(int(kw.get("offset", 0)), 0)
    lo = mya._norm_ts_bound(kw["ts_from"]) if kw.get("ts_from") else None
    hi = mya._norm_ts_bound(kw.get("ts_to", ""), day_end=True) if kw.get("ts_to") else None
    caller_q = (kw.get("caller") or "").strip()
    endpoint_q = (kw.get("endpoint") or "").strip()
    trace_q = (kw.get("trace_id") or "").strip()
    window = collections.deque(maxlen=offset + limit)
    total = success = fail = slow = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if caller_q and caller_q not in str(row.get("caller") or ""):
            continue
        if endpoint_q and endpoint_q not in str(row.get("endpoint") or ""):
            continue
        if trace_q and str(row.get("trace_id") or "") != trace_q:
            continue
        if not mya._match_status(row.get("status"), (kw.get("status") or "")):
            continue
        ts = mya._parse_ts(str(row.get("ts") or ""))
        if lo is not None and ts < lo:
            continue
        if hi is not None and ts > hi:
            continue
        total += 1
        st = row.get("status")
        if isinstance(st, int):
            if 200 <= st < 300:
                success += 1
            else:
                fail += 1
        lat = row.get("latency_ms")
        if isinstance(lat, int):
            if lat > mya.AUDIT_SLOW_MS:
                slow += 1
        window.append(row.get("ts"))
    return {"items": list(window)[::-1][offset:offset + limit],
            "total": total, "success": success, "fail": fail, "slow": slow}


def _spy_reads():
    """monkeypatch mya._read_chunks_reverse：包装计数累计读取字节数。"""
    box = {"bytes": 0}
    orig = mya._read_chunks_reverse

    def counting(fh, size, *, chunk=mya.AUDIT_QUERY_CHUNK):
        box["bytes"] = 0
        for ln in orig(fh, size, chunk=chunk):
            box["bytes"] += len(ln.encode("utf-8")) + 1   # +1 还原换行符
            yield ln
    mya._read_chunks_reverse = counting
    return box, (lambda: mya.__dict__.update({"_read_chunks_reverse": orig}))


# ── 1. 倒序正确性 ──

def test_reverse_order_oldest_last_newest_first():
    """无过滤默认页：items[0] 最新、items[-1] 较旧（时间新→旧产出）。"""
    p = _fixture(_seed_lines(50))              # 最新行＝09-24T01:00（第 49 小时）
    r = mya.audit_query(p, limit=5)
    ts_list = [it["ts"] for it in r["items"]]
    want = [f"2026-09-{1 + (49 - i) // 24:02d}T{(49 - i) % 24:02d}:00:00.000+00:00"
            for i in range(5)]
    assert ts_list == want, (ts_list, want)


def test_pagination_and_filter_match_forward_semantics():
    """翻页×过滤逐页比对朴素正序参考：第 0/1/2 页与全量 total 完全一致。"""
    p = _fixture(_seed_lines(60))
    for kwargs in ({}, {"caller": "ak_key00"}, {"endpoint": "ep0", "status": "2xx"},
                   {"trace_id": ""}):
        ref_total = _naive_query(p, **kwargs)["total"]
        for offset in (0, 5, 10, 55, 60):
            r = mya.audit_query(p, limit=5, offset=offset, **kwargs)
            ref = _naive_query(p, limit=5, offset=offset, **kwargs)
            assert [it["ts"] for it in r["items"]] == ref["items"], (kwargs, offset)
            assert r["total"] == ref["total"] == ref_total, (kwargs, offset)


def test_time_window_results_match_naive():
    """带时间窗（含端闭）的查询：结果与朴素全扫参考一致（提前终止不改内容）。"""
    p = _fixture(_seed_lines(40))
    kw = {"ts_from": "2026-09-01T10:00:00.000+00:00", "ts_to": "2026-09-01T20:00:00.000+00:00"}
    r = mya.audit_query(p, **kw)
    ref = _naive_query(p, **kw)
    assert r["total"] == ref["total"] == 11
    assert [it["ts"] for it in r["items"]] == ref["items"]
    assert r["summary"]["success"] == ref["success"] and r["summary"]["fail"] == ref["fail"]
    assert r["summary"]["slowCount"] == ref["slow"]


def test_reverse_reader_chunk_boundaries():
    """_read_chunks_reverse 直接机检：小 chunk 跨块拼行不丢不重，顺序严格倒序。"""
    p = _fixture(_seed_lines(30))
    size = p.stat().st_size
    with p.open("rb") as fh:
        got = [ln.strip() for ln in mya._read_chunks_reverse(fh, size, chunk=137)]
    want = [ln.strip() for ln in reversed(p.read_text(encoding="utf-8").splitlines()) if ln]
    assert got == want
    # 完整性：倒读拼回来的行集合＝正读行集合（一行不多一行不少）
    assert sorted(got) == sorted(want)


# ── 2. 提前终止证据（读取量 < 全量） ──

def test_early_stop_reads_less_than_full_file():
    """带 ts_from 的近窗查询：读取字节数严格小于全文件（monkeypatch 读取函数计数）。"""
    p = _fixture(_seed_lines(400))          # ~110B/行 × 400 行 ≈ 44KB
    full = p.stat().st_size
    box, restore = _spy_reads()
    try:
        mya.audit_query(p, ts_from="2026-09-01T20:00:00.000+00:00", limit=20)
        near = box["bytes"]
        mya.audit_query(p, ts_from="2026-09-01T00:00:00.000+00:00", limit=20)
        wide = box["bytes"]
    finally:
        restore()
    assert near < full, f"近窗读取量 {near} 应 < 全量 {full}"
    assert near < wide, f"近窗 {near} 应 < 全时段窗 {wide}"


def test_no_ts_from_still_scans_full_for_total():
    """无 ts_from（全量浏览）：total 须全量——读取量＝全文件（口径不变优先）。"""
    p = _fixture(_seed_lines(200))
    full = p.stat().st_size
    box, restore = _spy_reads()
    try:
        r = mya.audit_query(p, limit=20)
        read = box["bytes"]
    finally:
        restore()
    assert r["total"] == 200 and read >= full, (r["total"], read, full)


def test_ts_to_only_recent_runs_fast_but_exact():
    """只给 ts_to（上界）不带下界：不能提前终止（旧行仍可能命中 total）。"""
    p = _fixture(_seed_lines(300))
    full = p.stat().st_size
    box, restore = _spy_reads()
    try:
        r = mya.audit_query(p, ts_to="2026-09-01T10:00:00.000+00:00")
        read = box["bytes"]
    finally:
        restore()
    assert r["total"] == 11 and read >= full, (r["total"], read)


# ── 3. total/summary 口径不变 ──

def test_window_semantics_with_offset_beyond_recent():
    """大 offset 翻页（窗口 > 命中数的一半）：头窗＋切片仍给出正确旧页。"""
    p = _fixture(_seed_lines(25))
    r = mya.audit_query(p, limit=5, offset=20)
    ref = _naive_query(p, limit=5, offset=20)
    assert [it["ts"] for it in r["items"]] == ref["items"] == [
        "2026-09-01T04:00:00.000+00:00", "2026-09-01T03:00:00.000+00:00",
        "2026-09-01T02:00:00.000+00:00", "2026-09-01T01:00:00.000+00:00",
        "2026-09-01T00:00:00.000+00:00"]
    r2 = mya.audit_query(p, limit=5, offset=25)
    assert r2["items"] == [] and r2["total"] == 25


def test_summary_caliber_unchanged_with_time_window():
    """summary 按全部命中行聚合（与翻页无关）——带窗查询口径与朴素参考逐项对齐。"""
    p = _fixture(_seed_lines(120))
    kw = {"ts_from": "2026-09-01T05:00:00.000+00:00"}
    r = mya.audit_query(p, limit=3, **kw)
    ref = _naive_query(p, **kw)
    s = r["summary"]
    assert s["total"] == ref["total"] == 115
    assert s["success"] == ref["success"] and s["fail"] == ref["fail"]
    assert s["slowCount"] == ref["slow"]


# ── 4. #38 倒读提前终止的单调性守护（2026-10-15 复审 P2-5） ──

def test_monotonic_guard_fail_safe_on_clock_skew():
    """audit_query 守护：倒读方向先遇 ts 跳升（回拨后旧行落在新行之后的形态）——
    无守护时其后首条窗外旧行即 floor break，漏计更早的窗内命中；有守护＝跳变关
    终止闸全扫，total 与朴素全扫参考一致。同窗有序文件仍享受提前终止。"""
    ordered = _seed_lines(240)                       # 时间严格递增（正常追加序）
    skewed = list(ordered)
    # 物理 idx150 插未来行（ts 大于全部种子行）：倒读 idx≈90 先见此跳变 → 关终止闸；
    # idx120 的窗外旧行不再触发 break → 前半段窗内命中不漏。
    future = _line(999, "2026-12-01T12:00:00.000+00:00", "ak_key9999",
                   "service:/v1/future", 200, 7)
    old = _line(998, "2026-01-01T00:00:00.000+00:00", "ak_key9998",
                "service:/v1/old", 200, 7)
    skewed.insert(150, future)
    skewed.insert(120, old)

    ts_from = "2026-09-05T00:00:00+00:00"            # 下界之下＝idx120 前的窗内行会被漏
    path_o = _fixture(ordered)
    path_s = _fixture(skewed)
    try:
        naive_s = _naive_query(path_s, ts_from=ts_from)
        assert naive_s["total"] > 0, "夹具须保证乱序点后仍有窗内命中"

        box, restore = _spy_reads()
        r_s = mya.audit_query(path_s, ts_from=ts_from)
        skewed_bytes = box["bytes"]
        restore()
        assert r_s["total"] == naive_s["total"], \
            f"乱序须 fail-safe 不漏计：{r_s['total']} vs {naive_s['total']}"

        box2, restore2 = _spy_reads()
        r_o = mya.audit_query(path_o, ts_from=ts_from)
        stop_bytes = box2["bytes"]
        restore2()
        assert stop_bytes < skewed_bytes, \
            f"有序应享受提前终止（读得更少）：{stop_bytes} >= {skewed_bytes}"
        assert r_o["total"] == _naive_query(path_o, ts_from=ts_from)["total"], \
            "有序自身语义不变（对照朴素全扫）"
    finally:
        path_o.unlink(missing_ok=True)
        path_s.unlink(missing_ok=True)


def test_monotonic_guard_metering_no_undercount_on_skew():
    """metering_scan 同款守护：尾段插未来行（单调断点）＋中段插窗外旧行——
    无守护时旧行即 floor break 漏计其前窗内行；有守护＝跳变先关闸，全扫不漏账。"""
    from app import metering as mt
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)

    def iso(dt_obj):
        return dt_obj.strftime("%Y-%m-%dT%H:%M:%S.000+00:00")

    lines = []
    for i in range(60):
        t = now - _dt.timedelta(minutes=2 * (60 - i))     # 递增时间，全部落今日窗内
        lines.append(_line(i + 1, iso(t), "ak_new", "service:/e", 200, 5))
        if i == 30:   # 中段窗外旧行：无守护时倒读到它即 break（漏前段 30 条）
            lines.append(_line(998, iso(now - _dt.timedelta(days=200)),
                               "ak_old", "service:/e", 200, 5))
    # 尾段未来行（物理 idx45＞插旧行处）：倒读更早遇到＝单调断点，先关终止闸
    lines.insert(45, _line(999, iso(now + _dt.timedelta(days=2)),
                           "ak_future", "service:/e", 200, 5))
    pth = _fixture(lines)
    try:
        scan = mt.metering_scan(pth, days=1, now=now)
        got = scan["callers"].get("ak_new", {}).get("calls", 0)
        assert got == 60, f"乱序窗内聚合不漏账：实得 {got}/60（提前 break＝漏前段）"
    finally:
        pth.unlink(missing_ok=True)


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
