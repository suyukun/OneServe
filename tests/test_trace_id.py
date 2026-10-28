#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-5 traceId 贯通单元测试——纯 python 直跑或 pytest 收集均可。

覆盖（纯逻辑层；FastAPI 中间件/网关链路为薄胶水，E2E 归 tests/verify_trace_e2e.py）：
  - trace.py：透传优先级（X-Trace-Id > X-OneServe-Trace-Id > 自生成）、大小写不敏感、
    空白容错、生成形态（32 位小写十六进制，与 APISIX uuid 同形态口径）；
  - audit.py 八元组：make_entry 带 trace_id 落第八列、缺省空串＝存量七元组口径；
  - 存量兼容：旧七列 JSONL 行（无 trace_id 键）在新代码下可读可写可恢复，缺列等价空串；
  - main.py 薄胶水机检：语法编译通过（宿主机无 fastapi，同 test_batch_api 口径）。
"""

import json
import py_compile
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import trace  # noqa: E402
from app.audit import AuditEntry, AuditWriter, make_entry  # noqa: E402


# ---------- trace.py：解析与生成 ----------

def test_resolve_prefers_trace_header():
    """网关透传的 X-Trace-Id 优先原样透传（不重造）。"""
    assert trace.resolve_trace_id({"X-Trace-Id": "abc123"}) == "abc123"


def test_resolve_case_insensitive_and_alt_header():
    """header 大小写不敏感；无 X-Trace-Id 时兼容读 SPEC 行文名 X-OneServe-Trace-Id。"""
    assert trace.resolve_trace_id({"x-trace-id": "low"}) == "low"
    assert trace.resolve_trace_id({"X-OneServe-Trace-Id": "spec-name"}) == "spec-name"
    # 两个都在：正式 header 名优先
    assert trace.resolve_trace_id(
        {"x-oneserve-trace-id": "alt", "x-trace-id": "main"}) == "main"


def test_resolve_blank_stripped_then_generate():
    """空白值视同缺失；全缺时用 maker 生成。"""
    assert trace.resolve_trace_id({"X-Trace-Id": "  "}, maker=lambda: "gen") == "gen"
    assert trace.resolve_trace_id({}, maker=lambda: "gen") == "gen"


def test_new_trace_id_shape():
    """生成形态：标准 UUID（36 位带连字符，与 APISIX request-id 默认算法同格式）。"""
    tid = trace.new_trace_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid)
    assert trace.new_trace_id() != tid  # 逐次不同


# ---------- audit.py：八元组与存量兼容 ----------

def test_make_entry_with_trace_id():
    """make_entry 带 trace_id → 第八列落值（其余七列口径不动）。"""
    e = make_entry(caller="c", endpoint="service:/v1/ping", params="[]",
                   status=200, latency_ms=1, response_body="{}", trace_id="t-001")
    assert e.trace_id == "t-001"
    rec = json.loads(e.to_json_line())
    assert rec["trace_id"] == "t-001"
    # 原七列仍在且含义不变
    for k in ("ts", "caller", "endpoint", "params_digest", "status",
              "latency_ms", "response_digest"):
        assert k in rec


def test_make_entry_trace_id_default_empty():
    """缺省 trace_id＝空串（P0-5 之前与 batch 等未接链路同口径）。"""
    e = make_entry(caller="c", endpoint="e", params="p",
                   status=200, latency_ms=1, response_body="r")
    assert e.trace_id == ""


def test_legacy_seven_column_line_compatible():
    """存量兼容：旧七列 JSONL 行（无 trace_id 键）读取/续写/恢复均容错。"""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # 手工造一条 P0-5 之前的存量行（七列＋seq，无 trace_id）
        legacy = {"ts": "2026-01-01T00:00:00+00:00", "caller": "old",
                  "endpoint": "service:/v1/ping", "params_digest": "x",
                  "status": 200, "latency_ms": 2, "response_digest": "y", "seq": 1}
        (d / "audit.jsonl").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        # 新写入器打开同一目录：seq 不冲突，新行带 trace_id
        w = AuditWriter(str(d))
        e = w.append(make_entry(caller="new", endpoint="service:/v1/ping",
                                params="[]", status=200, latency_ms=1,
                                response_body="{}", trace_id="t-new"))
        w.close()
        assert e.seq == 2  # 存量行 seq=1 被识别，新行顺延
        lines = [json.loads(l) for l in
                 (d / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "trace_id" not in lines[0]          # 存量行保持原样（不回填不报错）
        assert lines[1]["trace_id"] == "t-new"
        # 旧行按 key 读取可直接构造（缺列走默认值＝空串口径）
        assert AuditEntry(**lines[0]).trace_id == ""


def test_legacy_wal_line_recover_compatible():
    """存量兼容：WAL 里残留旧格式行，recover() 照常补齐（缺 trace_id 不炸）。"""
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        legacy = {"ts": "2026-01-01T00:00:00+00:00", "caller": "old",
                  "endpoint": "service:/v1/ping", "params_digest": "x",
                  "status": 200, "latency_ms": 2, "response_digest": "y", "seq": 7}
        (d / "audit.wal").write_text(json.dumps(legacy) + "\n", encoding="utf-8")
        w = AuditWriter(str(d))
        assert w.recover() == 1
        w.close()
        line = json.loads((d / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert line["caller"] == "old" and line["seq"] == 7


# ---------- main.py 薄胶水机检（宿主机无 fastapi，同 test_batch_api 口径） ----------

def test_main_module_compiles():
    """中间件改动语法机检：py_compile 通过（行为验证归 E2E verify_trace_e2e.py）。"""
    py_compile.compile(str(REPO / "service" / "app" / "main.py"), doraise=True)


def test_trace_middleware_reinject_keeps_endpoint_headers():
    """真栈回归补丁（TASK-M15-7 s5 场景发现）：trace_middleware 对 ≥400 JSON
    重建响应注入 traceId 时只带 X-Trace-Id——端点自挂的 X-RateLimit-Reason/
    Retry-After（M15-5 AC-10 Stripe 式头）被整体丢弃。源码机检：重建分支必须
    先原样随行原响应头（仅 content-length 例外——体注入后长度重算）。"""
    src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    # P1-2：注入改为 errors 常量（单一事实来源），字段名同值不变
    assert "errors.FIELD_TRACE_ID, trace_id" in src
    assert "for name, value in response.headers.items():" in src, \
        "重建响应须遍历随行原响应头（业务头不丢）"
    assert 'injected.headers[name] = value' in src
    assert 'name.lower() != "content-length"' in src, \
        "content-length 须排除（体已变，长度重算）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} 项全过")
