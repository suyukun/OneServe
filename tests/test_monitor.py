#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-6 异常与批量监控套件（tests/test_monitor.py）——纯 python 直跑或 pytest 收集均可。

守卫口径（任务卡：异常列表＋按调用方/API 下钻＋错误样本＋trace 详情＋批量运行视角；
SPEC §5-P1-6／§2-B7）：
  1. 异常筛选与审计页同口径：只收 4xx/5xx；caller/endpoint 部分匹配；status 类/精确；
     trace_id 精确；时间窗含端闭（纯日期按东八区全天）——掩码/时间解析复用 my_access；
  2. 下钻聚合：按调用方/API 的调用量/异常数/异常率（小基数不出比率）；Top N 降序；
     维度键超上限＝dimTruncated 且其余并入「（其余）」（不静默丢）；
  3. 错误样本：条目携参数/响应 sha256 摘要（明文与完整凭证永不出端点——掩码纪律）；
  4. trace 详情：按 trace_id 跨「服务侧＋批量任务级」两文件精确反查；查无＝found=false；
  5. 批量运行视角（真源＝Redis 任务结构）：队列深度 LLEN／执行中 running+cancelling／
     五态计数／积压最长等待（无 queued＝None，不是 0）／队列滞留如实暴露／最近任务降序；
     Redis 读失败＝degraded 零值降级（不伪造在线）；
  6. 组件探活（维护者 追加：运维大盘四卡必须真）：Redis PING 失败→error；网关 TCP
     失败→error／成功→ok；服务＝本进程存活（uptime 真值）；审计＝注册配置；
  7. 只有 4xx/5xx 才计异常；输入非法抛 ValueError（端点转 400）；
  8. 胶水机检（宿主机无 fastapi，同 test_audit_query 口径）：monitor_api py_compile＋
     AST 契约（三端点路径/令牌闸/委托 monitor 纯函数）＋main 挂载 monitor_router＋
     portal_api 的 /v1/portal/status 携 components。

被测对象 service/app/monitor.py 不依赖 FastAPI/redis（store/ping/连接器注入）。
"""
from __future__ import annotations

import ast
import asyncio
import json
import py_compile
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import monitor as mon   # noqa: E402


def _line(seq, ts, caller, endpoint, status, latency, trace_id="", **extra):
    row = {"ts": ts, "caller": caller, "endpoint": endpoint, "params_digest": "p" * 64,
           "status": status, "latency_ms": latency, "response_digest": "r" * 64,
           "trace_id": trace_id, "seq": seq}
    row.update(extra)
    return json.dumps(row, ensure_ascii=False)


def _fixture(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    f.write("\n".join(rows) + "\n")
    f.close()
    return Path(f.name)


def _seed():
    """六行样例：两调用方×两端点×成/败×4xx/5xx（与审计页套件同构，口径可比对）。"""
    return [
        _line(1, "2026-09-22T01:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/tags/query", 200, 5),
        _line(2, "2026-09-22T02:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/news/query", 403, 3),
        _line(3, "2026-09-22T03:00:00.000+00:00", "ak_deadbeefdeadbeef", "service:/v1/tags/query", 200, 8, trace_id="T3"),
        _line(4, "2026-09-23T04:00:00.000+00:00", "demo-page", "service:/demo/query", 400, 2),
        _line(5, "2026-09-23T05:00:00.000+00:00", "ak_deadbeefdeadbeef", "service:/v1/tags/query", 500, 250, trace_id="T5"),
        _line(6, "2026-09-23T06:00:00.000+00:00", "sb_aaaabbbbcccc1111", "service:/v1/tags/query", 200, 300, trace_id="T6"),
    ]


# ── 1. 异常筛选（只收 4xx/5xx；与审计页同口径） ──

def test_only_failure_records_are_errors():
    """异常＝4xx/5xx；2xx/3xx 只进窗口总量、不进异常列表（分类正确）。"""
    p = _fixture(_seed())
    r = mon.error_query(p)
    assert r["total"] == 3, "样例含 403/400/500 三条异常"
    assert [i["status"] for i in r["items"]] == [500, 400, 403], "新在前"
    assert r["summary"]["windowTotal"] == 6, "窗口总量含全部命中记录"
    assert r["summary"]["byClass"] == {"4xx": 2, "5xx": 1}
    assert all(i["errorClass"] in ("4xx", "5xx") for i in r["items"])


def test_error_class_and_status_filters():
    p = _fixture(_seed())
    assert mon.error_query(p, error_class="4xx")["total"] == 2
    assert mon.error_query(p, error_class="5xx")["total"] == 1
    assert mon.error_query(p, status="403")["total"] == 1
    assert mon.error_query(p, status="4xx")["total"] == 2
    # 状态条件若指向成功类：异常列表口径下无命中（本端点只列异常）
    assert mon.error_query(p, status="2xx")["total"] == 0


def test_caller_and_endpoint_substring_and_trace_exact():
    p = _fixture(_seed())
    assert mon.error_query(p, caller="sb_aaaa")["total"] == 1
    assert mon.error_query(p, caller="deadbeef")["total"] == 1
    assert mon.error_query(p, endpoint="news")["total"] == 1
    assert mon.error_query(p, trace_id="T5")["total"] == 1
    assert mon.error_query(p, trace_id="T")["total"] == 0        # 精确，不做包含
    # AND 组合：调用方×异常类
    assert mon.error_query(p, caller="sb_aaaa", error_class="4xx")["total"] == 1


def test_time_window_inclusive_and_cn_day():
    p = _fixture(_seed())
    r = mon.error_query(p, ts_from="2026-09-22", ts_to="2026-09-22")
    assert r["total"] == 1, "东八区 09-22 全天只含 09-22T02:00Z 的 403 行"


def test_bad_inputs_raise_value_error():
    p = _fixture(_seed())
    for kwargs in ({"error_class": "6xx"}, {"status": "ok"}, {"ts_from": "2026-13-99"},
                   {"ts_from": "2026-09-23", "ts_to": "2026-09-22"}):
        try:
            mon.error_query(p, **kwargs)
            raise AssertionError("应抛 ValueError：" + str(kwargs))
        except ValueError:
            pass


def test_bad_lines_skipped_and_missing_file_empty():
    p = _fixture(_seed() + ["{broken json", "", '{"ts": "x"}'])
    assert mon.error_query(p)["total"] == 3
    r = mon.error_query(Path("/nonexistent/audit.jsonl"))
    assert r["total"] == 0 and r["items"] == []
    assert r["summary"]["errorRate"] is None


# ── 2. 下钻聚合 ──

def test_drill_by_caller_and_api_shape():
    p = _fixture(_seed())
    r = mon.error_query(p)
    assert [c["callerMasked"] for c in r["byCaller"]] == [
        "sb_aaaa****1111", "ak_dead****beef", "demo-page…"]
    top = r["byCaller"][0]
    assert top["total"] == 3 and top["errors"] == 1 and top["topStatus"] == 403
    assert top["errorRate"] is None, "小基数（<100）不出比率"
    apis = {a["endpoint"]: a for a in r["byApi"]}
    assert apis["service:/v1/tags/query"]["total"] == 4
    assert apis["service:/v1/tags/query"]["errors"] == 1
    assert apis["service:/v1/tags/query"]["topStatus"] == 500


def test_drill_rate_when_sample_enough():
    """总量 ≥ 小基数阈值时给出异常率（100 行 4xx/5xx 全异常＝100.0）。"""
    rows = [_line(i, f"2026-09-22T00:00:{i % 60:02d}.000+00:00", "ak_kk",
                  "service:/v1/ping", 500 if i % 2 else 400, 1) for i in range(1, 101)]
    r = mon.error_query(_fixture(rows))
    assert r["summary"]["errorRate"] == 100.0
    assert r["byApi"][0]["errorRate"] == 100.0


def test_drill_dim_truncation_merges_other():
    """维度键超上限：多余键并入「（其余）」且 dimTruncated=true（不静默丢）。"""
    rows = [_line(i, "2026-09-22T01:00:00.000+00:00", f"ak_{i:04d}", f"service:/v1/ep{i}", 500, 1)
            for i in range(mon.MAX_DIM_KEYS + 20)]
    r = mon.error_query(_fixture(rows))
    assert r["dimTruncated"] is True
    assert any(a["endpoint"] == mon.DIM_OTHER for a in r["byApi"])
    assert r["total"] == mon.MAX_DIM_KEYS + 20, "其余行仍计入异常总数（只并展览、不丢计数）"


# ── 3. 分页＋错误样本＋掩码纪律 ──

def test_pagination_and_samples():
    p = _fixture(_seed())
    r = mon.error_query(p, limit=2, offset=0)
    assert r["total"] == 3 and len(r["items"]) == 2
    assert all(i["paramsDigest"] == "p" * 64 and i["responseDigest"] == "r" * 64
               for i in r["items"]), "错误样本＝参数/响应 sha256 摘要"
    assert r["summary"]["sampleLimit"] == mon.ERROR_SAMPLE_LIMIT
    assert mon.error_query(p, limit=0)["limit"] == 1
    assert mon.error_query(p, limit=999)["limit"] == mon.ERROR_MAX_LIMIT
    assert mon.error_query(p, offset=-5)["offset"] == 0


def test_masked_no_full_credential():
    p = _fixture(_seed())
    blob = json.dumps(mon.error_query(p), ensure_ascii=False)
    assert "sb_aaaabbbbcccc1111" not in blob and "ak_deadbeefdeadbeef" not in blob
    assert "sb_aaaa****1111" in blob
    # 下钻用「可辨识前缀」而非掩码全文（掩码尾部 **** 匹配不上真实 caller）
    assert mon.drill_caller("sb_aaaabbbbcccc1111") == "sb_aaaa"
    assert mon.drill_caller("ak_deadbeefdeadbeef") == "ak_dead"
    assert mon.drill_caller("demo-page") == "demo-page"
    assert mon.drill_caller("") == ""


# ── 4. trace 详情（跨两文件精确反查） ──

def test_trace_lookup_across_sources():
    p1 = _fixture(_seed())
    p2 = _fixture([_line(1, "2026-09-23T07:00:00.000+00:00", "ak_deadbeefdeadbeef",
                         "service:/v1/batch/jobs/finish", 500, 12, trace_id="T5")])
    r = mon.trace_lookup([("service", p1), ("batch", p2)], "T5")
    assert r["found"] is True and len(r["entries"]) == 2
    assert sorted(e["source"] for e in r["entries"]) == ["batch", "service"]
    assert {e["errorClass"] for e in r["entries"]} == {"5xx"}
    assert "ak_deadbeefdeadbeef" not in json.dumps(r), "trace 详情同样掩码"


def test_trace_lookup_empty_states():
    p = _fixture(_seed())
    assert mon.trace_lookup([("service", p)], "")["found"] is False
    assert mon.trace_lookup([("service", p)], "nope")["entries"] == []
    assert mon.trace_lookup([("service", Path("/nope.jsonl"))], "T5")["sources"] == []


# ── 5. 批量运行视角（真源＝Redis 任务结构） ──

def _job_hash(jid, status, **extra):
    """任务 hash 假体（值皆 str，同 Redis 口径）；extra 覆盖缺省字段。"""
    row = {"job_id": jid, "tenant": "demo_merchant", "scope": "list", "status": status,
           "total": "100", "processed": "50", "created_at": "2026-09-23T06:00:00+00:00",
           "keys_sha256": "k" * 64, "fields": "[]"}
    row.update(extra)
    return row


class FakeBatchStore:
    """字典假 Redis：实现 batch_overview 用到的 scan_iter/hgetall/llen（值皆 str）。"""

    def __init__(self, jobs=None, queue=None, broken=False):
        self.jobs = jobs if jobs is not None else {}
        self.queue = list(queue or [])
        self.broken = broken

    async def scan_iter(self, match=None):
        if self.broken:
            raise ConnectionError("redis down")
        for k in sorted(self.jobs):
            yield k

    async def hgetall(self, key):
        return dict(self.jobs.get(key, {}))

    async def llen(self, key):
        return len(self.queue)


def _store_three():
    jid = "oneserve:batch:job:"
    return FakeBatchStore(jobs={
        jid + "aaa": _job_hash("aaa", "queued"),
        jid + "bbb": _job_hash("bbb", "running"),
        jid + "ccc": _job_hash("ccc", "failed", error="kv_unavailable"),
        jid + "ddd": _job_hash("ddd", "succeeded"),
        jid + "aaa:keys": {},                       # 键名单 list 不是任务 hash（须跳过）
    }, queue=["aaa"])


def test_batch_overview_real_counts():
    o = asyncio.run(mon.batch_overview(_store_three(), now_ts=1758600000.0))
    assert o["queueDepth"] == 1 and o["inFlight"] == 1 and o["total"] == 4
    assert o["counts"] == {"queued": 1, "running": 1, "cancelling": 0,
                           "succeeded": 1, "failed": 1, "cancelled": 0}
    assert o["backlog"]["queued"] == 1 and o["backlog"]["staleQueueItems"] == 0
    assert isinstance(o["backlog"]["oldestWaitSec"], int)
    assert o["degraded"] is None
    assert o["caliber"]["notConnected"], "未接入项须随行如实标注"


def test_batch_overview_no_queued_backlog_is_none():
    """无 queued 任务＝oldestWaitSec None（不是 0 秒——「没有排队」与「等了 0 秒」不同）。"""
    st = FakeBatchStore(jobs={"oneserve:batch:job:x": _job_hash("x", "running")}, queue=[])
    o = asyncio.run(mon.batch_overview(st, now_ts=1.0))
    assert o["backlog"]["oldestWaitSec"] is None
    assert o["backlog"]["queued"] == 0


def test_batch_overview_stale_queue_item_exposed():
    """队列项多于 queued 记录＝滞留，如实暴露（不静默）。"""
    st = FakeBatchStore(jobs={}, queue=["ghost-1", "ghost-2"])
    o = asyncio.run(mon.batch_overview(st, now_ts=1.0))
    assert o["queueDepth"] == 2 and o["backlog"]["staleQueueItems"] == 2


def test_batch_overview_progress_none_when_total_zero():
    """快照任务物化前 total=0：progress=None（不伪造 0%）。"""
    st = FakeBatchStore(jobs={"oneserve:batch:job:s":
                              _job_hash("s", "running", total="0", processed="0",
                                        scope="snapshot")})
    o = asyncio.run(mon.batch_overview(st, now_ts=1.0))
    row = o["recent"][0]
    assert row["progress"] is None and row["scope"] == "snapshot"
    assert o["recent"][0]["waitSec"] is None, "非 queued 不给等待时长"


def test_batch_overview_degraded_on_redis_failure():
    o = asyncio.run(mon.batch_overview(FakeBatchStore(broken=True)))
    assert o["degraded"] and "redis" in o["degraded"].lower()
    assert o["queueDepth"] == 0 and o["total"] == 0
    assert o["backlog"]["staleQueueItems"] is None, "降级态下滞留数不猜"


def test_batch_overview_recent_sorted_desc():
    jid = "oneserve:batch:job:"
    st = FakeBatchStore(jobs={
        jid + "old": _job_hash("old", "succeeded", created_at="2026-09-23T01:00:00+00:00"),
        jid + "new": _job_hash("new", "succeeded", created_at="2026-09-23T09:00:00+00:00"),
    })
    o = asyncio.run(mon.batch_overview(st, now_ts=1.0))
    assert [r["jobId"] for r in o["recent"]] == ["new", "old"]


# ── 6. 组件探活（失败如实 error，不回退「正常」） ──

class _OkRedis:
    async def ping(self):
        return True


class _DownRedis:
    async def ping(self):
        raise ConnectionError("redis down")


class _FakeWriter:
    def close(self):
        pass

    async def wait_closed(self):
        return None


async def _ok_conn(host, port):
    return (object(), _FakeWriter())


async def _bad_conn(host, port):
    raise OSError("connection refused")


def test_probe_redis_ok_and_error():
    ok = asyncio.run(mon.probe_redis(_OkRedis()))
    assert ok["status"] == "ok" and "PONG" in ok["detail"]
    bad = asyncio.run(mon.probe_redis(_DownRedis()))
    assert bad["status"] == "error", "Redis PING 失败须如实 error（不得降级成 ok）"
    assert "ConnectionError" in bad["detail"]


def test_probe_gateway_ok_and_error():
    ok = asyncio.run(mon.probe_gateway(connector=_ok_conn))
    assert ok["status"] == "ok" and "9080" in ok["detail"]
    bad = asyncio.run(mon.probe_gateway(connector=_bad_conn))
    assert bad["status"] == "error" and "OSError" in bad["detail"]


def test_probe_gateway_custom_target():
    seen = {}

    async def _spy(host, port):
        seen["host"], seen["port"] = host, port
        return (object(), _FakeWriter())

    asyncio.run(mon.probe_gateway(host="apisix.internal", port=9999, connector=_spy))
    assert seen == {"host": "apisix.internal", "port": 9999}


def test_probe_service_uptime_truth():
    r = mon.probe_service(started_monotonic=100.0, version="0.2.0", now_monotonic=3700.0)
    assert r["status"] == "ok" and "1h00m" in r["detail"] and "0.2.0" in r["detail"]


def test_components_view_four_cards_real():
    """四卡齐备且 id 与前端 COMPONENT_ORDER 对齐；审计关闭＝off（不是 error）。"""
    view = asyncio.run(mon.components_view(
        _OkRedis(), {"v1-svc-usr-asset-tag": {"audit": {"enabled": True, "log_params": "hash_only"}}},
        version="0.2.0", started_monotonic=0.0, gateway_connector=_ok_conn))
    ids = [c["id"] for c in view]
    assert ids == ["ops-comp-gateway", "ops-comp-service", "ops-comp-redis", "ops-comp-audit"]
    assert all(c["status"] in ("ok", "warn", "error", "off") for c in view)
    assert all(c["checkedAt"] and c["detail"] for c in view)
    off = asyncio.run(mon.components_view(
        _OkRedis(), {"v1-svc-usr-asset-tag": {"audit": {"enabled": False}}},
        version="", started_monotonic=0.0, gateway_connector=_ok_conn))
    assert off[3]["status"] == "off"
    down = asyncio.run(mon.components_view(
        _DownRedis(), {}, version="", started_monotonic=0.0, gateway_connector=_bad_conn))
    assert down[0]["status"] == "error" and down[2]["status"] == "error", "探不到＝error 如实返回"


# ── 7. 胶水机检（宿主机无 fastapi） ──

def test_monitor_api_module_compiles():
    py_compile.compile(str(REPO / "service" / "app" / "monitor_api.py"), doraise=True)
    py_compile.compile(str(REPO / "service" / "app" / "monitor.py"), doraise=True)


def _routes(src_name: str) -> dict:
    tree = ast.parse((REPO / "service" / "app" / src_name).read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                    and isinstance(dec.args[0], ast.Constant)):
                out[dec.args[0].value] = node
    return out


def test_monitor_api_contract_by_ast():
    """四只读端点路径固定、委托 monitor 纯函数。

    2026-09-23：原断言为「三端点各过一个 _token_ok 管理令牌闸」+「hmac.compare_digest
    防时序侧信道」。管理令牌机制已按老板决定**整体拆除**（本机/内网阶段授权边界＝网络层
    可达；认证归 M3 接入公司内网时统一做），该两条断言随机制一并下线，改为反向的
    防回潮断言：monitor_api 不应再出现任何令牌闸痕迹。端点契约与纯函数委托照旧守。
    2026-10-20（蓝图 1a 监控最小集）：新增第四只读端点 /v1/admin/monitor/containers
    （常驻容器健康一屏，委托 mon.containers_view，探活原语单一来源 app/healthz）。
    """
    src = (REPO / "service" / "app" / "monitor_api.py").read_text(encoding="utf-8")
    routes = _routes("monitor_api.py")
    assert set(routes) == {"/v1/admin/monitor/errors", "/v1/admin/monitor/trace",
                           "/v1/admin/monitor/batch",
                           "/v1/admin/monitor/containers"}
    assert src.count("_token_ok") == 0, "monitor_api 不应残留令牌闸（机制已拆）"
    assert "hmac.compare_digest" not in src, "令牌比较已随机制拆除（不该残留）"
    assert "mon.error_query" in src and "mon.trace_lookup" in src and "mon.batch_overview" in src
    assert "mon.containers_view" in src, "容器健康端点须委托 monitor.containers_view（分层纪律）"
    assert "ValueError" in src and "400" in src, "非法输入须就近 400"


def test_portal_status_resilient_when_redis_down():
    """大盘自保：Redis 故障时 /v1/portal/status 仍须可用（否则拿不到 Redis=error）。"""
    src = (REPO / "service" / "app" / "portal_api.py").read_text(encoding="utf-8")
    fn = ast.unparse(_routes("portal_api.py")["/v1/portal/status"])
    assert "except Exception" in fn, "同步域读取须容错降级（故障时端点仍可读）"
    assert "components_view" in fn
    assert "components" in fn


def test_main_registers_monitor_router_and_uptime():
    src = (REPO / "service" / "app" / "main.py").read_text(encoding="utf-8")
    assert "from app.monitor_api import router as monitor_router" in src
    assert "app.include_router(monitor_router)" in src
    assert "started_monotonic" in src, "服务组件 uptime 需要真起点（不得前端伪造）"


def test_portal_status_carries_real_components():
    """运维大盘四卡接真：/v1/portal/status 出参含 components（monitor.components_view）。"""
    src = (REPO / "service" / "app" / "portal_api.py").read_text(encoding="utf-8")
    assert "mon.components_view" in src
    assert '"components": components' in src
    fn = _routes("portal_api.py")["/v1/portal/status"]
    assert "components" in ast.unparse(fn)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:   # noqa: BLE001 —— 直跑模式聚合报错
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {fn.__name__}: {exc}")
    if failed:
        print(f"== {failed} 组失败 ==")
    sys.exit(1 if failed else 0)
