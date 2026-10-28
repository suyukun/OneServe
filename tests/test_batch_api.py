#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量任务框架单元测试（TASK-M15-1）——纯 python 直跑或 pytest 收集均可。

覆盖（门槛稿 A3 对本卡的判据＝本套件绿）：
  AC-1  合法名单提交→登记任务＋job_id＋排队位次＋状态=queued（含 FIFO 位次递增）；
  AC-2  空/超上限/键格式非法/未授权/字段越权/非法请求体→机读错误码拒绝，不产生任务；
  AC-4  状态端点：状态机＋processed/total 百分比＋所钉版本 data_as_of＋404；
  状态机全迁移：M15-1 的 25 组（5 态×5 目标）＋M15-4 加法扩展（cancelling 行、
        failed→queued、running→cancelling）逐组断言合法/非法；终态封闭；
  AC-6  取消端点（M15-4）：queued→cancelled（清队列残留）、running→cancelling
        （worker 收尾）、succeeded/failed 409、cancelled/cancelling 幂等同态；
  AC-7  任务级重试端点（M15-4）：failed→queued（重置进度/检查点＋原名单重入队列）；
        进行中/终态 409；
  配额（NC-4）：三口径默认值＋.env 覆盖＋单任务上限拒绝；
  审计（AC-8 前半·预埋）：每次端点调用落七元组（caller/endpoint/params_digest/status）。

被测对象 service/app/batch.py 不依赖 FastAPI/redis（store 注入，同 kv_executor 纪律）；
batch_api.py 为 FastAPI 薄胶水（宿主机无 fastapi，另行语法机检；端点级 E2E 归
TASK-M15-2/7 的 compose/testkit 场景）。
"""

import asyncio
import copy
import hashlib
import json
import py_compile
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx  # noqa: E402
from app.audit import AuditWriter, digest_sha256  # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
GRANT = kvx.tenant_grant(MATRIX, batch.BATCH_SOURCE_SERVICE, tenant="demo_bank")   # 2026-10-15 随生产常量改指现役主标签服务（字面量已死，同 test_batch_worker 先例）
WHITELIST = GRANT["fields"]
APPKEY = "oneserve-dev-appkey-001"          # demo_bank 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32]    # 合法键（默认宽松模式：原 SM3 形态仍合法）


class FakeBatchRedis:
    """字典假 Redis：实现 batch 用到的 rpush/lpos/lrange/hset/hgetall（值皆 str）。

    M15-5 补：get/incrby/expire（日配额计数）＋scan_iter（并发配额 running 判定）
    ——submit 路径新增依赖，方法口径与 FakeWorkerRedis 同款。
    """

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def get(self, key):
        return self.strings.get(key)

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

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(str(v) for v in values)
        return len(self.lists[key])

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
        """LREM 语义（count=0 全清）：取消端点清队列残留用（M15-4）。"""
        vals = self.lists.get(key, [])
        kept = [v for v in vals if v != str(value)]
        self.lists[key] = kept
        return len(vals) - len(kept)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


def _submit(keys, *, caller=APPKEY, fields=None, store=None, quotas=None,
            audit=None, payload=None):
    """提交一次任务（默认合法口径）；返回 (body, status, store)。"""
    st = store or FakeBatchRedis()
    if payload is None:
        payload = {"keys": keys} if fields is None else {"keys": keys, "fields": fields}
    body, status = asyncio.run(
        batch.submit_job(st, SERVICES, MATRIX, caller, payload,
                         quotas=quotas, audit=audit))
    return body, status, st


def _one_job(store=None):
    """登记一个任务并返回 (job_id, store)。"""
    body, status, st = _submit(KEYS, store=store)
    assert status == 202, body
    return body["job_id"], st


# ---------- AC-1：合法提交→登记＋job_id＋排队位次 ----------

def test_submit_ok_registers_queued_job():
    """合法名单（含大写键归一化）→202：job_id/queued/位次/total/名单 sha256/白名单投影。"""
    upper = [KEYS[0].upper()] + KEYS[1:]                  # 大写 SM3 应归一化放行
    body, status, st = _submit(upper)
    assert status == 202
    normalized = [k.lower() for k in upper]
    assert body["status"] == "queued" and body["queue_position"] == 1
    assert body["total"] == 3 and body["tenant"] == "demo_bank"
    assert body["keys_sha256"] == hashlib.sha256(
        "\n".join(normalized).encode()).hexdigest()
    assert body["fields"] == sorted(WHITELIST)            # 缺省=全授权白名单
    job = st.hashes[batch.JOB_KEY_PREFIX + body["job_id"]]
    assert job["status"] == "queued" and int(job["total"]) == 3
    assert job["keys_sha256"] == body["keys_sha256"]      # 名单 sha256 落任务记录（NC-5 名单侧）
    keys_stored = asyncio.run(
        batch.JobStore(st).get_keys(body["job_id"]))
    assert keys_stored == normalized                      # 键名单完整落 Redis list
    assert st.lists[batch.QUEUE_KEY] == [body["job_id"]]


def test_submit_fifo_queue_positions():
    """FIFO：先提交位次 1、后提交位次 2；队列序=提交序（AC-3 的结构基础）。"""
    b1, s1, st = _submit(KEYS)
    b2, s2, _ = _submit(["12" * 32], store=st)
    assert s1 == s2 == 202
    assert b1["queue_position"] == 1 and b2["queue_position"] == 2
    assert st.lists[batch.QUEUE_KEY] == [b1["job_id"], b2["job_id"]]


def test_keys_digest_contract():
    """名单 sha256 口径：对归一化键集按序换行拼接取摘要（确定性契约）。"""
    assert batch.keys_digest(KEYS) == hashlib.sha256(
        "\n".join(KEYS).encode()).hexdigest()
    assert batch.keys_digest([]) == hashlib.sha256(b"").hexdigest()


# ---------- AC-2：拒绝路径（机读错误码＋不产生任务） ----------

def _assert_no_job(st):
    assert not st.hashes and not st.lists.get(batch.QUEUE_KEY), "拒绝时不得产生任务"


def test_reject_invalid_payload_shapes():
    """非法请求体：非 JSON 对象/keys 非数组/元素非字符串 → 400，不产生任务。"""
    for payload in [None, {"keys": "ab" * 32}, {"keys": [123]}, "[]", []]:
        body, status, st = _submit(None, payload=payload)
        assert status == 400 and body["error"] in ("invalid_json", "invalid_request"), (
            payload, body)
        _assert_no_job(st)


def test_reject_empty_keys():
    """空名单 → 400 empty_keys，不产生任务。"""
    body, status, st = _submit([])
    assert status == 400 and body["error"] == "empty_keys"
    _assert_no_job(st)


def test_reject_too_many_keys():
    """超单任务上限（NC-4 配额之一）→ 413 too_many_keys，不产生任务。"""
    keys = [format(i, "064x") for i in range(4)]
    body, status, st = _submit(keys, quotas={"max_keys_per_job": 3,
                                             "max_concurrent_jobs": 1,
                                             "max_daily_keys": 100})
    assert status == 413 and body["error"] == "too_many_keys"
    assert "BATCH_MAX_KEYS_PER_JOB" in body["message"]     # 指明可覆盖的配额项
    _assert_no_job(st)


def test_reject_invalid_key_format():
    """任一键格式非法（含名单全非法，A4）→ 400 invalid_key 报序号，不回显原键。

    2026-09-23 维护者 口径：默认模式放宽——"xyz" 这类合理字符串键已不算非法；
    改用真正越出 pattern 的键（空/含空白/含非法字符）验证拒绝口径。
    """
    for keys in [["has space"], [KEYS[0], "a/b"], ["有中文"], ["AB" * 31 + "!"]]:
        body, status, st = _submit(keys)
        assert status == 400 and body["error"] == "invalid_key", body
        assert "第" in body["message"] and keys[0][:4] not in body["message"]
        _assert_no_job(st)
    # 空键：仍 400（不掺「不回显原键」断言——空串无内容可回显）
    body, status, st = _submit([""])
    assert status == 400 and body["error"] == "invalid_key", body
    _assert_no_job(st)


def test_non_sm3_key_accepted_by_batch():
    """新增断言①：非 SM3 形态的键（UUID／数字串）经放宽校验被服务受理。"""
    for keys in [["123e4567-e89b-12d3-a456-426614174000"],
                 ["1234567890"], ["D41D8CD98F00B204E9800998ECF8427E"]]:
        body, status, st = _submit(keys)
        assert status == 202, (keys, body)
        assert body["status"] == "queued" and body["total"] == 1
        stored = asyncio.run(batch.JobStore(st).get_keys(body["job_id"]))
        assert stored == [k.lower() for k in keys]     # 归一化小写后受理


def test_reject_unauthorized_caller():
    """未登记 AppKey → 403 unknown_tenant（与点查同授权矩阵同错误码），不产生任务。"""
    body, status, st = _submit(KEYS, caller="no-such-appkey")
    assert status == 403 and body["error"] == "unknown_tenant"
    _assert_no_job(st)


def test_reject_service_not_granted():
    """已登记租户但未授权批量载体服务 → 403 service_not_granted。"""
    matrix = copy.deepcopy(MATRIX)
    matrix[0]["grants"] = [g for g in matrix[0]["grants"]
                           if g["service"] != batch.BATCH_SOURCE_SERVICE]
    st = FakeBatchRedis()
    body, status = asyncio.run(
        batch.submit_job(st, SERVICES, matrix, APPKEY, {"keys": KEYS}))
    assert status == 403 and body["error"] == "service_not_granted"
    _assert_no_job(st)


def test_reject_fields_not_granted():
    """请求字段越权（白名单外字段名）→ 403 fields_not_granted，不产生任务（AC-2）。
    2026-10-15：白名单随生产常量改指现役主标签服务（ac_* 系旧服务字段已不在名单），
    取「白名单外 usr_id × 白名单内 aum_band」验证报错只点名越权字段。"""
    body, status, st = _submit(KEYS, fields=["usr_id", "aum_band"])
    assert status == 403 and body["error"] == "fields_not_granted"
    assert "usr_id" in body["message"] and "aum_band" not in body["message"]
    _assert_no_job(st)


def test_submit_fields_subset_whitelist():
    """请求字段 ⊆ 白名单 → 放行并按请求字段（去重排序）落任务记录。
    2026-10-15：字段名随现役主标签服务白名单（同 test_reject_fields_not_granted 注）。"""
    body, status, st = _submit(KEYS, fields=["churn_risk_band", "aum_band"])
    assert status == 202
    assert body["fields"] == ["aum_band", "churn_risk_band"]


# ---------- AC-4：状态端点（状态机＋进度百分比＋data_as_of） ----------

def test_status_queued_view():
    """queued：进度 0%、data_as_of=None（未钉版本不伪造）、带排队位次。"""
    jid, st = _one_job()
    body, status = asyncio.run(batch.get_job(st, jid))
    assert status == 200
    assert body["status"] == "queued" and body["progress"] == 0
    assert body["processed"] == 0 and body["total"] == 3
    assert body["data_as_of"] is None and body["live_version"] is None
    assert body["queue_position"] == 1 and body["error"] is None


def test_status_running_progress_and_pinned_version():
    """running：钉住版本/data_as_of（AC-11 写入口）＋进度百分比向下取整。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="20260922", data_as_of="2026-09-22"))
    body, status = asyncio.run(batch.get_job(st, jid))
    assert status == 200 and body["status"] == "running"
    assert body["live_version"] == "20260922" and body["data_as_of"] == "2026-09-22"
    assert body["started_at"] and "queue_position" not in body   # 执行中不再排队
    asyncio.run(jobs.update_progress(jid, 2, 1))
    body, _ = asyncio.run(batch.get_job(st, jid))
    assert body["processed"] == 2 and body["found"] == 1
    assert body["progress"] == 66                                 # 2/3 → 66%（向下取整）
    asyncio.run(jobs.update_progress(jid, 3, 2))
    body, _ = asyncio.run(batch.get_job(st, jid))
    assert body["progress"] == 100


def test_status_terminal_states_and_not_found():
    """终态视图：finished_at 带 error 口径；未知任务 → 404 job_not_found。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
    asyncio.run(jobs.finish(jid, "failed", error="redis_unavailable"))
    body, status = asyncio.run(batch.get_job(st, jid))
    assert status == 200 and body["status"] == "failed"
    assert body["finished_at"] and body["error"] == "redis_unavailable"
    body, status = asyncio.run(batch.get_job(st, "nosuchjob"))
    assert status == 404 and body["error"] == "job_not_found"


# ---------- 状态机全迁移（M15-1 的 25 组＋M15-4 扩展行列逐组断言） ----------

def test_state_machine_full_transition_matrix():
    """合法迁移全通过、非法迁移全 409；对每个起点各建新任务驱动到该态再验证。

    M15-4 扩展后状态表为 6 态×6 目标＝36 组：原 25 组口径不变（加法扩展），
    新增 cancelling 行（仅→cancelled）与 cancelled/failed 目标列的合法增量。
    """
    for cur in batch.STATES:
        for target in batch.STATES:
            legal = target in batch.ALLOWED_TRANSITIONS[cur]
            try:
                batch.check_transition(cur, target)
                assert legal, f"{cur}->{target} 应判非法"
            except batch.BatchError as exc:
                assert not legal and exc.status == 409, (cur, target, exc)


def test_state_machine_transitions_persisted_end_to_end():
    """四条合法路径端到端落盘：q→r→succeeded／q→r→failed／q→r→cancelled／q→cancelled。"""
    for path in [("running", "succeeded"), ("running", "failed"),
                 ("running", "cancelled"), ("cancelled",)]:
        jid, st = _one_job()
        jobs = batch.JobStore(st)
        if path[0] == "running":
            asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
        asyncio.run(jobs.finish(jid, path[-1]))
        job = asyncio.run(jobs.get(jid))
        assert job["status"] == path[-1] and job["finished_at"], path


def test_state_machine_illegal_store_transitions_rejected():
    """落盘层非法迁移：queued→succeeded、running→queued、终态再迁移 → 409 且状态不变。"""
    for drive, attempt in [
            (None, ("queued", "succeeded")),
            ("running", ("running", "queued")),
            ("running", ("running", "running"))]:
        jid, st = _one_job()                                # 每场景全新任务
        jobs = batch.JobStore(st)
        if drive == "running":
            asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
        cur, target = attempt
        try:
            asyncio.run(jobs._set(jid, cur, target))
            raise AssertionError(f"{cur}->{target} 落盘应被拒绝")
        except batch.BatchError as exc:
            assert exc.status == 409 and exc.code == "illegal_transition"
        job = asyncio.run(jobs.get(jid))
        assert job["status"] == ("running" if drive == "running" else "queued")
    for terminal in ("succeeded", "cancelled"):
        jid2, st2 = _one_job()
        jobs2 = batch.JobStore(st2)
        asyncio.run(jobs2.start(jid2, live_version="v", data_as_of="d"))
        asyncio.run(jobs2.finish(jid2, terminal))
        try:
            asyncio.run(jobs2.finish(jid2, "running"))
            raise AssertionError("终态再迁移应被拒绝")
        except batch.BatchError as exc:
            assert exc.status == 409


def test_progress_only_in_running():
    """进度更新仅 running 允许：queued/succeeded 均拒绝（分片边界写口径）。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    try:
        asyncio.run(jobs.update_progress(jid, 1, 1))
        raise AssertionError("queued 态进度更新应被拒绝")
    except batch.BatchError as exc:
        assert exc.status == 409
    asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
    asyncio.run(jobs.update_progress(jid, 1, 1))               # running 放行
    asyncio.run(jobs.finish(jid, "succeeded"))
    try:
        asyncio.run(jobs.update_progress(jid, 2, 2))
        raise AssertionError("终态进度更新应被拒绝")
    except batch.BatchError as exc:
        assert exc.status == 409


# ---------- 配额（NC-4）：默认值＋.env 覆盖 ----------

def test_quota_defaults_and_env_override():
    """三口径默认=并发1/单任务100万/日500万；环境变量逐项可覆盖。"""
    assert batch.quota_limits() == {
        "max_keys_per_job": 1_000_000,
        "max_concurrent_jobs": 1,
        "max_daily_keys": 5_000_000,
    }
    assert batch.quota_limits({"BATCH_MAX_KEYS_PER_JOB": "10",
                               "BATCH_MAX_CONCURRENT_JOBS": "2",
                               "BATCH_MAX_DAILY_KEYS": "20"}) == {
        "max_keys_per_job": 10, "max_concurrent_jobs": 2, "max_daily_keys": 20}


# ---------- 审计七元组（AC-8 前半·预埋：每次调用一条） ----------

def test_audit_seven_tuple_per_call():
    """提交/查询/拒绝各落一条七元组：caller 可溯租户、endpoint 口径、params 只落摘要。"""
    with tempfile.TemporaryDirectory() as td:
        writer = AuditWriter(td, flush_interval=0)     # 同步模式：无后台线程，即时可读
        st = FakeBatchRedis()
        b1, s1, _ = _submit(KEYS, store=st, audit=writer)
        assert s1 == 202
        asyncio.run(batch.get_job(st, b1["job_id"], caller=APPKEY, audit=writer))
        _submit(KEYS, caller="no-such-appkey", audit=writer)             # 拒绝也审计
        lines = (Path(td) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        entries = [json.loads(x) for x in lines]
        assert len(entries) == 3
        e_submit, e_status, e_reject = entries
        for e in entries:                                                # 七元组字段齐备
            assert set(e) >= {"ts", "caller", "endpoint", "params_digest",
                              "status", "latency_ms", "response_digest"}
            assert e["latency_ms"] >= 0
        assert e_submit["caller"] == APPKEY and e_submit["status"] == 202
        assert e_submit["endpoint"] == "service:/v1/batch/jobs"
        assert e_submit["params_digest"] == digest_sha256(json.dumps(
            {"total": 3, "keys_sha256": b1["keys_sha256"]}))            # 名单不落明文
        assert e_status["endpoint"] == "service:/v1/batch/jobs/status"
        assert e_status["status"] == 200 and e_status["caller"] == APPKEY
        assert e_reject["status"] == 403 and e_reject["caller"] == "no-such-appkey"
        writer.close()


# ---------- AC-6 取消端点（M15-4）：各态迁移/幂等/409 ----------

def test_cancel_queued_to_cancelled_clears_queue():
    """queued→cancelled（AC-6）：直接终态＋清队列残留；再查位次不在队。"""
    jid, st = _one_job()
    assert st.lists[batch.QUEUE_KEY] == [jid]                 # 排队残留先确认
    body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert status == 200 and body["status"] == "cancelled"
    assert st.lists[batch.QUEUE_KEY] == []                    # 队列残留已清
    job = asyncio.run(batch.JobStore(st).get(jid))
    assert job["status"] == "cancelled" and job["finished_at"]


def test_cancel_running_marks_cancelling_for_worker_finalize():
    """running→cancelling（AC-6）：只标停止意向，收尾（终态＋清产物）归 worker。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
    body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert status == 200 and body["status"] == "cancelling"
    assert body["finished_at"] is None                        # 终态未落：等 worker 收尾


def test_cancel_succeeded_rejected_409():
    """succeeded→409（AC-6 明文拒绝），状态保持 succeeded 不被迁移。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
    asyncio.run(jobs.finish(jid, "succeeded"))
    body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert status == 409 and body["error"] == "illegal_transition"
    assert body["status"] == "succeeded"
    assert asyncio.run(jobs.get(jid))["status"] == "succeeded"


def test_cancel_failed_rejected_409_retry_is_the_path():
    """failed→409：失败不在取消语义内（AC-6 只认 queued/running）；重试走 retry 端点。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
    asyncio.run(jobs.finish(jid, "failed", error="kv_unavailable"))
    body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert status == 409 and body["error"] == "illegal_transition"
    assert body["status"] == "failed"


def test_cancel_idempotent_cancelled_and_cancelling():
    """幂等（AC-6）：已 cancelled 重复 DELETE＝同态 200；cancelling 重复＝同态 200。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    first, s1 = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert s1 == 200 and first["status"] == "cancelled"
    again, s2 = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
    assert s2 == 200 and again["status"] == "cancelled"       # 已取消：同态 200
    jid2, st2 = _one_job()
    jobs2 = batch.JobStore(st2)
    asyncio.run(jobs2.start(jid2, live_version="v", data_as_of="d"))
    asyncio.run(jobs2.cancel(jid2))                           # running→cancelling
    mid, s3 = asyncio.run(batch.cancel_job(st2, jid2, caller=APPKEY))
    assert s3 == 200 and mid["status"] == "cancelling"        # 取消中：同态 200


def test_cancel_missing_job_404():
    """不存在的任务→404 job_not_found（机读错误码）。"""
    body, status = asyncio.run(batch.cancel_job(FakeBatchRedis(), "no-such-job"))
    assert status == 404 and body["error"] == "job_not_found"


# ---------- AC-7 任务级重试端点（M15-4）：failed→queued ----------

def test_retry_failed_to_queued_resets_and_requeues():
    """failed→queued（AC-7 任务级）：重置进度/检查点/版本钉＋凭原名单重入队列。"""
    jid, st = _one_job()
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version="v1", data_as_of="d1"))
    asyncio.run(jobs.finish(jid, "failed", error="kv_unavailable",
                            stats={"missed": 1, "row_errors": 2}))
    body, status = asyncio.run(batch.retry_job(st, jid, caller=APPKEY))
    assert status == 202 and body["status"] == "queued"
    assert body["job_id"] == jid                              # 同 job_id 语义（卡面）
    assert body["queue_position"] == 1 and body["retried_from"] == "failed"
    job = asyncio.run(jobs.get(jid))
    assert job["status"] == "queued"
    assert (job["processed"], job["found"], job["missed"],
            job["row_errors"]) == (0, 0, 0, 0)                # 进度/检查点重置
    assert job["live_version"] == "" and job["data_as_of"] == ""   # 版本钉重置
    assert job["error"] == "" and job["finished_at"] == ""
    assert st.lists[batch.QUEUE_KEY] == [jid]                 # 凭原名单重入 FIFO 队列
    keys_stored = asyncio.run(jobs.get_keys(jid))
    assert keys_stored == KEYS                                # 原名单不动（同名单重提）


def test_retry_only_from_failed():
    """非 failed 态重试→409 job_not_retryable：进行中/成功/取消均不可重试。"""
    for drive, target in [("queued", None), ("running", None),
                          ("succeeded", None), ("cancelled", None)]:
        jid, st = _one_job()
        jobs = batch.JobStore(st)
        if drive in ("running", "succeeded", "cancelled"):
            asyncio.run(jobs.start(jid, live_version="v", data_as_of="d"))
        if drive in ("succeeded", "cancelled"):
            asyncio.run(jobs.finish(jid, drive))
        body, status = asyncio.run(batch.retry_job(st, jid, caller=APPKEY))
        assert status == 409 and body["error"] == "job_not_retryable", drive
        assert asyncio.run(jobs.get(jid))["status"] == drive   # 状态不被迁移


def test_retry_missing_job_404():
    """不存在的任务重试→404 job_not_found。"""
    body, status = asyncio.run(batch.retry_job(FakeBatchRedis(), "no-such-job"))
    assert status == 404 and body["error"] == "job_not_found"


def test_cancel_retry_audit_trail():
    """取消/重试各落一条审计（AC-8 同构口径：endpoint 静态名＋摘要）。

    M15-5 起 queued 取消在端点内即落终态——先落一条任务级终态审计
    （jobs/finish，status=499）再落取消端点条目；本测试顺带断言该条目。
    """
    with tempfile.TemporaryDirectory() as td:
        writer = AuditWriter(td, flush_interval=0)
        jid, st = _one_job()
        b1, s1 = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY, audit=writer))
        assert s1 == 200
        jid2, st2 = _one_job()
        jobs2 = batch.JobStore(st2)
        asyncio.run(jobs2.start(jid2, live_version="v", data_as_of="d"))
        asyncio.run(jobs2.finish(jid2, "failed", error="kv_unavailable"))
        b2, s2 = asyncio.run(batch.retry_job(st2, jid2, caller=APPKEY,
                                             audit=writer))
        assert s2 == 202
        lines = (Path(td) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        entries = [json.loads(x) for x in lines]
        assert len(entries) == 3
        assert entries[0]["endpoint"] == batch.AUDIT_JOB_FINISH_ENDPOINT
        assert entries[0]["status"] == 499 and entries[0]["caller"] == APPKEY
        assert entries[0]["trace_id"]                    # 任务终态自生成 trace
        assert entries[1]["endpoint"] == batch.AUDIT_CANCEL_ENDPOINT
        assert entries[1]["status"] == 200 and entries[1]["caller"] == APPKEY
        assert entries[2]["endpoint"] == batch.AUDIT_RETRY_ENDPOINT
        assert entries[2]["status"] == 202
        assert entries[1]["params_digest"] == digest_sha256(json.dumps({"job_id": jid}))
        writer.close()


# ---------- 胶水机检（宿主机无 fastapi：语法＋路由声明存在性） ----------

def test_batch_api_glue_compiles_with_routes():
    """batch_api.py 语法可编译且声明两路由（完整 import=E2E，归 M15-2/7 场景）。"""
    glue = REPO / "service" / "app" / "batch_api.py"
    py_compile.compile(str(glue), doraise=True)
    src = glue.read_text(encoding="utf-8")
    assert '@router.post("/v1/batch/jobs")' in src
    assert '@router.get("/v1/batch/jobs/{job_id}")' in src
    assert '@router.delete("/v1/batch/jobs/{job_id}")' in src          # M15-4 取消
    assert '@router.post("/v1/batch/jobs/{job_id}/retry")' in src     # M15-4 重试


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== TASK-M15-1 批量任务框架单元测试（{len(tests)} 条）==")
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
