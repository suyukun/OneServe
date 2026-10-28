#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量任务级审计单元测试（TASK-M15-5，AC-8 后半）——纯 python 直跑或 pytest 收集均可。

覆盖（门槛稿 A3 AC-8 后半的判据＝本套件绿）：
  任务级终态审计（JobStore.finish 落一条，经 audit 参数注入真 AuditWriter）：
  - succeeded：worker 全流水线跑成 → 恰好一条（endpoint=jobs/finish、caller=提交
    appkey、params 摘要含任务七要素＋双 sha256＋四计数、status=200、trace_id
    按任务终态生成非空）；终态重入不重复落（状态机保证恰好一条）；
  - failed：无版本指针 → data_not_ready → status=500、results_sha256 空串不伪造；
  - cancelled（worker 收尾）：running→cancelling→finalize_cancel → status=499；
  - cancelled（queued 端点直取消）：cancel_job 内终态 → 任务级一条＋端点一条，两读可分；
  - caller 可溯租户：任务记录落 caller（appkey），params 摘要含 tenant（digest 对账）；
  trace_id 贯通（AC-8 前半＋P0-5 注记落点）：
  - submit/status/cancel/retry/results 五端点核心函数 trace_id 透传到审计第八列；
  - 胶水机检：batch_api.py 五端点均接 request.state.trace_id；网关路由
    /v1/batch/*（init_routes.py）带 key-auth＋request-id（宿主机无 fastapi/docker，
    E2E 的 401/200 归 M15-7 testkit/compose 场景，同 house 口径）。

被测对象 service/app/batch.py＋batch_worker.py 不依赖 FastAPI/redis（store 注入）。
"""

import asyncio
import json
import py_compile
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx                      # noqa: E402
from app import batch_worker as w                              # noqa: E402
from app import batch_delivery as d                            # noqa: E402
from app.audit import AuditWriter, digest_sha256               # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
APPKEY = "oneserve-dev-appkey-001"            # demo_bank 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32]
VER = "20260922"
AS_OF = "2026-09-22 00:00:00"
KV = SERVICES[batch.BATCH_SOURCE_SERVICE]["backend"]["kv"]


def dkey(version, sm3):
    return KV["data_key"].format(version=version, sm3=sm3)


class FakeAuditRedis:
    """字典假 Redis：submit/worker/配额/审计全链路用到的方法（值皆 str）。"""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key):
        return self.strings.get(key)

    async def mget(self, *keys):
        return [self.strings.get(k) for k in keys]

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(str(v) for v in values)
        return len(self.lists[key])

    async def lpop(self, key):
        vals = self.lists.get(key)
        return vals.pop(0) if vals else None

    async def lpos(self, key, value):
        try:
            return self.lists.get(key, []).index(value)
        except ValueError:
            return None

    async def lrange(self, key, start, end):
        vals = self.lists.get(key, [])
        if end == -1:
            end = len(vals) - 1
        return vals[start:end + 1]

    async def lrem(self, key, count, value):
        vals = self.lists.get(key, [])
        kept = [v for v in vals if v != str(value)]
        self.lists[key] = kept
        return len(vals) - len(kept)

    async def incrby(self, key, amount):
        value = int(self.strings.get(key) or 0) + int(amount)
        self.strings[key] = str(value)
        return value

    async def expire(self, key, seconds):
        return True

    async def scan_iter(self, match=None):
        import fnmatch
        for k in sorted(self.hashes):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def seed_kv(st, version=VER, data_as_of=AS_OF):
    """预置线上版本：alias 指针＋版本元信息（data_as_of）。"""
    st.strings[KV["alias_key"]] = version
    st.hashes[KV["version_meta_key"].format(version=version)] = {
        "data_as_of": data_as_of, "row_count": "5"}


def put_user(st, sm3, tags, version=VER):
    st.strings[dkey(version, sm3)] = json.dumps(tags, ensure_ascii=False)


def submit(st, keys, **kw):
    payload = {"keys": keys} if "fields" not in kw else {
        "keys": keys, "fields": kw["fields"]}
    body, status = asyncio.run(
        batch.submit_job(st, SERVICES, MATRIX, APPKEY, payload,
                         audit=kw.get("audit"), trace_id=kw.get("trace_id", "")))
    assert status == 202, body
    return body["job_id"]


def read_entries(td):
    """读审计 JSONL（writer 用同步模式 flush_interval=0，即时可读）。"""
    lines = (Path(td) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(x) for x in lines]


# ---------- 任务级终态审计：succeeded（双 sha256＋四计数全量对账） ----------

def test_task_audit_succeeded_one_entry_full_payload():
    """成功终态恰好一条：caller/双 sha256/计数/trace 全对账（params_digest 机检）。"""
    st = FakeAuditRedis()
    seed_kv(st)
    put_user(st, KEYS[0], {"ac_active_sts_01": "1"})
    put_user(st, KEYS[1], {"ac_login_sts_02": "9"})
    jid = submit(st, KEYS)
    with tempfile.TemporaryDirectory() as td:
        writer = AuditWriter(td, flush_interval=0)
        with tempfile.TemporaryDirectory() as rd:
            job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                            results_dir=rd, audit=writer))
            assert job["status"] == "succeeded"
            assert (job["processed"], job["found"], job["missed"],
                    job["row_errors"]) == (3, 2, 1, 0)
            results_sha = w.sha256_results(rd, jid)
            assert job["results_sha256"] == results_sha
        entries = read_entries(td)
        assert len(entries) == 1, "任务级终态审计恰好一条"
        e = entries[0]
        assert e["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        assert e["caller"] == APPKEY                        # caller=提交 appkey
        assert e["status"] == 200 and e["latency_ms"] >= 0
        assert e["trace_id"]                                # 终态自生成 trace
        # params 摘要全量对账：任务七要素＋双 sha256＋四计数（digest_sha256 同口径）
        expected = {
            "job_id": jid, "tenant": "demo_bank", "caller": APPKEY,
            "status": "succeeded", "total": 3, "processed": 3, "found": 2,
            "missed": 1, "row_errors": 0,
            "keys_sha256": batch.keys_digest(KEYS),
            "results_sha256": results_sha, "error": "",
        }
        assert e["params_digest"] == digest_sha256(
            json.dumps(expected, ensure_ascii=False))
        assert e["response_digest"] == digest_sha256(json.dumps(
            {"job_id": jid, "status": "succeeded"}, ensure_ascii=False))
        writer.close()


def test_task_audit_terminal_reentry_no_duplicate():
    """终态重入（process_job 幂等再跑）不再落任务级审计（状态机保证恰好一条）。"""
    st = FakeAuditRedis()
    seed_kv(st)
    jid = submit(st, KEYS[:2])
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        writer = AuditWriter(td, flush_interval=0)
        for _ in range(2):                                  # 第二次＝终态直接返回
            job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                            results_dir=rd, audit=writer))
            assert job["status"] == "succeeded"
        assert len(read_entries(td)) == 1
        writer.close()


# ---------- 任务级终态审计：failed / cancelled（不伪造结果摘要） ----------

def test_task_audit_failed_status_500_no_results_sha():
    """failed（data_not_ready）：status=500、results_sha256 空串、error 机读码入 params。"""
    st = FakeAuditRedis()          # 不 seed：无版本指针 → failed data_not_ready（A4）
    jid = submit(st, KEYS[:1])
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        writer = AuditWriter(td, flush_interval=0)
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=rd, audit=writer))
        assert job["status"] == "failed" and job["error"] == "data_not_ready"
        entries = read_entries(td)
        assert len(entries) == 1
        e = entries[0]
        assert e["status"] == 500 and e["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        expected = {
            "job_id": jid, "tenant": "demo_bank", "caller": APPKEY,
            "status": "failed", "total": 1, "processed": 0, "found": 0,
            "missed": 0, "row_errors": 0,
            "keys_sha256": batch.keys_digest(KEYS[:1]),
            "results_sha256": "", "error": "data_not_ready",
        }
        assert e["params_digest"] == digest_sha256(
            json.dumps(expected, ensure_ascii=False))
        writer.close()


def test_task_audit_cancelled_via_worker_finalize():
    """worker 收尾取消：running→cancelling→finalize_cancel→cancelled＝status 499 一条。"""
    st = FakeAuditRedis()
    seed_kv(st)
    jid = submit(st, KEYS[:1])
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version=VER, data_as_of=AS_OF))
    asyncio.run(jobs.cancel(jid))                           # → cancelling（非终态）
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        writer = AuditWriter(td, flush_interval=0)
        job = asyncio.run(w.finalize_cancel(st, rd, jid, audit=writer))
        assert job["status"] == "cancelled"
        entries = read_entries(td)
        assert len(entries) == 1
        assert entries[0]["status"] == 499
        assert entries[0]["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        assert entries[0]["trace_id"]
        writer.close()


def test_task_audit_queued_cancel_via_endpoint_two_entries():
    """queued 端点直取消：任务级终态一条（先）＋取消端点七元组一条（后），两读可分。"""
    st = FakeAuditRedis()
    jid = submit(st, KEYS[:1])
    with tempfile.TemporaryDirectory() as td:
        writer = AuditWriter(td, flush_interval=0)
        body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY,
                                                    audit=writer))
        assert status == 200 and body["status"] == "cancelled"
        entries = read_entries(td)
        assert len(entries) == 2
        task_e, endpoint_e = entries
        assert task_e["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        assert task_e["status"] == 499 and task_e["caller"] == APPKEY
        expected = {
            "job_id": jid, "tenant": "demo_bank", "caller": APPKEY,
            "status": "cancelled", "total": 1, "processed": 0, "found": 0,
            "missed": 0, "row_errors": 0,
            "keys_sha256": batch.keys_digest(KEYS[:1]),
            "results_sha256": "", "error": "",
        }
        assert task_e["params_digest"] == digest_sha256(
            json.dumps(expected, ensure_ascii=False))
        assert endpoint_e["endpoint"] == batch.AUDIT_CANCEL_ENDPOINT
        assert endpoint_e["status"] == 200
        writer.close()


def test_task_audit_requires_injected_writer():
    """audit=None（缺省）不落任务级审计：既有调用方（M15-2 测试等）口径不变。"""
    st = FakeAuditRedis()
    seed_kv(st)
    jid = submit(st, KEYS[:1])
    with tempfile.TemporaryDirectory() as rd:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=rd))
        assert job["status"] == "succeeded"


def test_job_record_stores_caller_for_traceability():
    """任务记录落 caller（AC-8「caller 可追溯到租户」的数据基础）：审计 caller 与
    任务记录 caller 同源；tenant 已在 params（上面各 digest 对账已证）。"""
    st = FakeAuditRedis()
    jid = submit(st, KEYS[:1])
    job = asyncio.run(batch.JobStore(st).get(jid))
    assert job["caller"] == APPKEY and job["tenant"] == "demo_bank"


# ---------- trace_id 贯通批量链路（AC-8 前半＋P0-5 注记落点） ----------

def test_endpoint_audit_carries_trace_id_through_batch_chain():
    """五端点核心函数 trace_id → 审计第八列：submit/status/cancel/retry/results 全透传。"""
    st = FakeAuditRedis()
    seed_kv(st)
    jid = submit(st, KEYS[:1], trace_id="trace-submit-1")   # 提交（202 审计一条）
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as rd:
        writer = AuditWriter(td, flush_interval=0)
        # 提交/查询/取消-重试链（取消后重试不合状态机，各自独立任务演示）
        asyncio.run(batch.submit_job(st, SERVICES, MATRIX, APPKEY,
                                     {"keys": KEYS[:1]}, audit=writer,
                                     trace_id="trace-submit-2"))
        asyncio.run(batch.get_job(st, jid, caller=APPKEY, audit=writer,
                                  trace_id="trace-status-1"))
        jid2 = submit(st, KEYS[:2])
        asyncio.run(batch.cancel_job(st, jid2, caller=APPKEY, audit=writer,
                                     trace_id="trace-cancel-1"))
        jobs = batch.JobStore(st)
        jid3 = submit(st, KEYS[:3])
        asyncio.run(jobs.start(jid3, live_version=VER, data_as_of=AS_OF))
        asyncio.run(jobs.finish(jid3, "failed", error="kv_unavailable"))
        asyncio.run(batch.retry_job(st, jid3, caller=APPKEY, audit=writer,
                                    trace_id="trace-retry-1"))
        entries = read_entries(td)
        by_trace = {e["trace_id"]: e for e in entries}
        assert by_trace["trace-submit-2"]["endpoint"] == batch.AUDIT_SUBMIT_ENDPOINT
        assert by_trace["trace-status-1"]["endpoint"] == batch.AUDIT_STATUS_ENDPOINT
        assert by_trace["trace-cancel-1"]["endpoint"] == batch.AUDIT_CANCEL_ENDPOINT
        assert by_trace["trace-retry-1"]["endpoint"] == batch.AUDIT_RETRY_ENDPOINT
        # queued 取消在此链内落了一条任务级终态审计：trace 为终态自生成值
        # （不与任何请求 trace 混用——任务完成事件独立可反查）
        known = {"trace-submit-2", "trace-status-1", "trace-cancel-1",
                 "trace-retry-1"}
        finish_e = [e for e in entries
                    if e["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT]
        assert len(finish_e) == 1 and finish_e[0]["trace_id"] not in known
        writer.close()


def test_results_delivery_audit_carries_trace_id():
    """结果交付两模式（下载/分页）trace_id 透传到审计第八列（M15-3 端点接线上）。"""
    st = FakeAuditRedis()
    seed_kv(st)
    put_user(st, KEYS[0], {"ac_active_sts_01": "1"})
    jid = submit(st, KEYS[:2])
    with tempfile.TemporaryDirectory() as rd:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=rd))
        assert job["status"] == "succeeded"
        with tempfile.TemporaryDirectory() as td:
            writer = AuditWriter(td, flush_interval=0)
            asyncio.run(d.results_download(st, jid, results_dir=rd, caller=APPKEY,
                                           audit=writer, trace_id="trace-dl-1"))
            asyncio.run(d.results_page(st, jid, results_dir=rd, caller=APPKEY,
                                       audit=writer, trace_id="trace-pg-1"))
            entries = read_entries(td)
            assert {e["trace_id"] for e in entries} == {"trace-dl-1", "trace-pg-1"}
            writer.close()


# ---------- 胶水机检（宿主机无 fastapi/docker：语法＋接线存在性；E2E 归 M15-7） ----------

def test_batch_api_glue_wires_trace_and_quota_headers():
    """batch_api.py：五端点均接 request.state.trace_id；提交端点挂 quota_headers。"""
    glue = REPO / "service" / "app" / "batch_api.py"
    py_compile.compile(str(glue), doraise=True)
    src = glue.read_text(encoding="utf-8")
    assert src.count('getattr(request.state, "trace_id"') >= 5   # 五端点全接线
    assert "trace_id=trace_id" in src or "trace_id=getattr" in src
    assert "batch.quota_headers(body, status)" in src            # 429/413 头映射
    # 提交 Pydantic 拒绝路径也带 trace（400 同链路）
    assert "trace_id=trace_id)" in src


def test_gateway_route_declares_batch_with_keyauth_and_trace():
    """init_routes.py：/v1/batch/* 路由挂 key-auth＋request-id（trace 注入）＋统计钩子。"""
    routes = REPO / "configs" / "apisix" / "init_routes.py"
    py_compile.compile(str(routes), doraise=True)
    src = routes.read_text(encoding="utf-8")
    assert '"/v1/batch/*"' in src
    assert '"routes/oneserve-v1-batch"' in src
    # 批量路由条目三插件齐备（key-auth=401/200 口径；request-id=trace 接线）
    block = src.split('"routes/oneserve-v1-batch"')[1].split("\n    ]")[0]
    assert '"key-auth": {}' in block and '"request-id": REQUEST_ID' in block
    assert "serverless-pre-function" in block
    assert '"DELETE"' in block and '"POST"' in block and '"GET"' in block


def test_worker_sidecar_declares_audit_dir():
    """compose：batch-worker 挂审计卷＋BATCH_AUDIT_DIR（与 service 同卷不同文件）。"""
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    assert "BATCH_AUDIT_DIR=/data/audit/batch" in compose
    worker_block = compose.split("batch-worker:")[1].split("portal:")[0]
    assert "oneserve-audit-data:/data/audit" in worker_block


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== TASK-M15-5 任务级审计单元测试（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)}；失败：{'、'.join(failed)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
