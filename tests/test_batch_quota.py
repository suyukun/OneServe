#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量配额 429 单元测试（TASK-M15-5，AC-10）——纯 python 直跑或 pytest 收集均可。

覆盖（门槛稿 A3 AC-10 的判据＝本套件绿）：
  三口径（NC-4 静态起步，.env/quotas 可覆盖）：
  - 并发（running/cancelling 判定）：租户执行中任务≥上限 → 429
    quota_concurrent_exceeded＋reason=concurrency＋retry_after（保守固定值）；
    queued 不占并发位（AC-3 FIFO 排队语义不变）；终态释放并发位；
  - 日键量：当日累计＋本次 > 上限 → 429 quota_daily_exceeded＋reason=daily
    ＋retry_after＝到下一东八区日界秒数（精确值，now 注入可测）；提交即计量、
    拒绝不计量；按日换键＝当日清零（次日自动恢复，无需人工重置）；
  - 单任务键上限（M15-1 已有 413）：reason=per_job 标注撞的是哪种配额；
  头口径（Stripe 式，batch.quota_headers 纯函数＋胶水挂头）：
  - 429：X-RateLimit-Reason＋Retry-After；413：仅 X-RateLimit-Reason；
  - 拒绝同样落审计七元组（AC-8 前半：429 也是一次调用）；
  胶水机检：batch_api.py 提交端点把 quota_headers 挂上 JSONResponse。

「提交过频」（AC-10 行文）由并发口径承接：执行中任务在即 429——同一租户
高频重复提交必然命中；独立的 QPS 级限流归网关（NC-4 批复后评估，见 init_routes
注记）。s4 点查回归（批量满载时点查不红）＝运行栈场景，归 TASK-M15-7 testkit。
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx                      # noqa: E402
from app.audit import AuditWriter, digest_sha256               # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
APPKEY = "oneserve-dev-appkey-001"            # demo_merchant 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32, "90" * 32, "12" * 32]
VER = "20260922"
AS_OF = "2026-09-22 00:00:00"
KV = SERVICES[batch.BATCH_SOURCE_SERVICE]["backend"]["kv"]

# 固定注入时刻：2026-09-21 14:13:20 UTC＝东八区 22:13:20（当日余 6400s）
NOW = 1790000000.0
QUOTAS = {"max_keys_per_job": 1_000_000,
          "max_concurrent_jobs": 1,
          "max_daily_keys": 5_000_000}


class FakeQuotaRedis:
    """字典假 Redis：submit＋配额路径用到的方法（值皆 str）。"""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}

    async def get(self, key):
        return self.strings.get(key)

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
        self.strings.setdefault("__ttl__:" + key, str(seconds))
        return True

    async def scan_iter(self, match=None):
        import fnmatch
        for k in sorted(self.hashes):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def seed_version(st):
    st.strings[KV["alias_key"]] = VER
    st.hashes[KV["version_meta_key"].format(version=VER)] = {
        "data_as_of": AS_OF, "row_count": "4"}


def call(st, keys, *, quotas=None, caller=APPKEY, now=NOW, **kw):
    return asyncio.run(batch.submit_job(
        st, SERVICES, MATRIX, caller, {"keys": keys},
        quotas=quotas or QUOTAS, now=now, **kw))


def submit_ok(st, keys, **kw):
    body, status = call(st, keys, **kw)
    assert status == 202, body
    return body["job_id"]


def job_hash_count(st):
    return sum(1 for k in st.hashes
               if k.startswith(batch.JOB_KEY_PREFIX)
               and not k.endswith(batch.JOB_KEYS_SUFFIX))


# ---------- 并发配额（AC-10：running 判定，queued 不占位） ----------

def test_concurrent_quota_429_while_running():
    """租户 running 任务在→再提交 429（code/reason/retry_after 齐备），不产生任务；
    终态后并发位释放→可再提交（「提交过频」由本口径承接）。"""
    st = FakeQuotaRedis()
    jid = submit_ok(st, KEYS[:2])
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version=VER, data_as_of=AS_OF))   # → running
    body, status = call(st, KEYS[:1])
    assert status == 429 and body["error"] == batch.QUOTA_CODE_CONCURRENT
    assert body["reason"] == batch.RATE_LIMIT_REASON_CONCURRENT
    assert body["retry_after"] == batch.RETRY_AFTER_CONCURRENCY_S
    assert "BATCH_MAX_CONCURRENT_JOBS" in body["message"]
    assert job_hash_count(st) == 1 and st.lists[batch.QUEUE_KEY] == [jid]
    asyncio.run(jobs.finish(jid, "succeeded"))              # 终态释放并发位
    body, status = call(st, KEYS[:1])
    assert status == 202, body


def test_concurrent_quota_queued_does_not_occupy():
    """queued 不占并发位（AC-3：新任务停留 queued 按 FIFO 被调度）——连续提交均 202。"""
    st = FakeQuotaRedis()
    submit_ok(st, KEYS[:1])
    submit_ok(st, KEYS[1:2])
    submit_ok(st, KEYS[2:3])
    assert len(st.lists[batch.QUEUE_KEY]) == 3


def test_concurrent_quota_cancelling_counts_until_finalized():
    """cancelling＝停止中仍占并发位；worker 收尾到 cancelled 终态后才释放。"""
    st = FakeQuotaRedis()
    jid = submit_ok(st, KEYS[:1])
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version=VER, data_as_of=AS_OF))
    asyncio.run(jobs.cancel(jid))                            # running→cancelling
    body, status = call(st, KEYS[1:2])
    assert status == 429 and body["error"] == batch.QUOTA_CODE_CONCURRENT
    asyncio.run(jobs.finish(jid, "cancelled"))               # 收尾终态
    body, status = call(st, KEYS[1:2])
    assert status == 202, body


# ---------- 日键量配额（AC-10：当日累计＋当日清零口径） ----------

def test_daily_quota_exceeded_429_with_exact_retry_after():
    """当日累计＋本次超限→429（reason=daily、retry_after=到下一日界精确秒数）；
    拒绝不计量（计数停在已提交量）、不产生任务；合规提交即累计入键。"""
    st = FakeQuotaRedis()
    quotas = {**QUOTAS, "max_daily_keys": 5}
    submit_ok(st, KEYS[:3], quotas=quotas)                   # 当日已计 3
    dkey = batch.daily_quota_key("demo_merchant", NOW)
    assert st.strings[dkey] == "3"
    body, status = call(st, KEYS[1:4], quotas=quotas)        # 3+3 > 5
    assert status == 429 and body["error"] == batch.QUOTA_CODE_DAILY
    assert body["reason"] == batch.RATE_LIMIT_REASON_DAILY
    assert body["retry_after"] == batch.seconds_to_next_day(NOW) == 6400
    assert "BATCH_MAX_DAILY_KEYS" in body["message"]
    assert st.strings[dkey] == "3"                           # 拒绝不计量
    assert job_hash_count(st) == 1
    body, status = call(st, KEYS[3:5], quotas=quotas)        # 3+2 = 5（贴上限放行）
    assert status == 202, body
    assert st.strings[dkey] == "5"


def test_daily_quota_resets_next_day():
    """当日清零＝按日换键：次日新键从 0 计，同一租户无需任何人工重置。"""
    st = FakeQuotaRedis()
    quotas = {**QUOTAS, "max_daily_keys": 3}
    submit_ok(st, KEYS[:3], quotas=quotas)                   # 当日打满
    body, status = call(st, KEYS[:1], quotas=quotas)
    assert status == 429 and body["error"] == batch.QUOTA_CODE_DAILY
    body, status = call(st, KEYS[:1], quotas=quotas, now=NOW + 86400)   # 次日
    assert status == 202, body
    assert st.strings[batch.daily_quota_key("demo_merchant", NOW + 86400)] == "1"


def test_daily_quota_key_uses_cn_business_day():
    """日键＝东八区业务日（与 stats.day_key 同口径）：UTC 2026-09-21 14:13→0921。"""
    assert batch.daily_quota_key("demo_merchant", NOW) == (
        "oneserve:batch:quota:daily:demo_merchant:20260921")
    # 东八区已过午夜（UTC+8=00:13 次日）→ 次日键
    assert batch.daily_quota_key("demo_merchant", NOW + 12 * 3600).endswith("20260922")


def test_daily_quota_counter_ttl_set():
    """计量键挂 48h TTL（隔日死键清理；清零口径在键名日期里，与 TTL 无关）。"""
    st = FakeQuotaRedis()
    submit_ok(st, KEYS[:1])
    dkey = batch.daily_quota_key("demo_merchant", NOW)
    assert st.strings["__ttl__:" + dkey] == str(batch.DAILY_QUOTA_TTL_S)


# ---------- 单任务键上限（M15-1 已有 413）＋reason 标注 ----------

def test_per_job_limit_413_with_reason():
    """超单任务上限→413 too_many_keys（既有行为）＋reason=per_job（M15-5 头口径）。"""
    st = FakeQuotaRedis()
    body, status = call(st, KEYS, quotas={**QUOTAS, "max_keys_per_job": 3})
    assert status == 413 and body["error"] == "too_many_keys"
    assert body["reason"] == batch.RATE_LIMIT_REASON_PER_JOB
    assert job_hash_count(st) == 0


# ---------- 头口径（Stripe 式）：quota_headers 纯函数 ----------

def test_quota_headers_mapping():
    """429→Reason+Retry-After；413→仅 Reason；200/无 reason→空头（不误挂）。"""
    assert batch.quota_headers(
        {"reason": "daily", "retry_after": 6400}, 429) == {
        "X-RateLimit-Reason": "daily", "Retry-After": "6400"}
    assert batch.quota_headers(
        {"reason": "concurrency", "retry_after": 30}, 429) == {
        "X-RateLimit-Reason": "concurrency", "Retry-After": "30"}
    assert batch.quota_headers({"reason": "per_job"}, 413) == {
        "X-RateLimit-Reason": "per_job"}
    assert batch.quota_headers({"reason": "per_job", "retry_after": 1}, 413) == {
        "X-RateLimit-Reason": "per_job"}          # 413 不随时间恢复：不挂 Retry-After
    assert batch.quota_headers({"reason": "daily"}, 429) == {
        "X-RateLimit-Reason": "daily"}            # 缺 retry_after 不硬造
    assert batch.quota_headers({}, 429) == {}
    assert batch.quota_headers({"reason": "daily"}, 200) == {}


# ---------- 拒绝路径审计（AC-8 前半：429 也是一次调用） ----------

def test_quota_rejections_are_audited():
    """并发/日配额 429 各落一条七元组（status=429、机读码入 params 摘要）。"""
    st = FakeQuotaRedis()
    jid = submit_ok(st, KEYS[:2])
    jobs = batch.JobStore(st)
    asyncio.run(jobs.start(jid, live_version=VER, data_as_of=AS_OF))
    with tempfile.TemporaryDirectory() as td:
        writer = AuditWriter(td, flush_interval=0)
        body, status = call(st, KEYS[:1], audit=writer)
        assert status == 429
        body, status = call(st, KEYS[:1],
                            quotas={**QUOTAS, "max_concurrent_jobs": 5,
                                    "max_daily_keys": 1},
                            audit=writer, caller=APPKEY)
        # 第一个 429 已并发受限；放开并发但日配额=1（已计 2）→ 仍 429（日配额）
        assert status == 429 and body["error"] == batch.QUOTA_CODE_DAILY
        lines = (Path(td) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        entries = [json.loads(x) for x in lines]
        assert len(entries) == 2
        assert all(e["status"] == 429 and e["caller"] == APPKEY
                   and e["endpoint"] == batch.AUDIT_SUBMIT_ENDPOINT
                   for e in entries)
        assert {e["params_digest"] for e in entries} == {
            digest_sha256(json.dumps(
                {"error": batch.QUOTA_CODE_CONCURRENT, "total": 1})),
            digest_sha256(json.dumps(
                {"error": batch.QUOTA_CODE_DAILY, "total": 1})),
        }
        writer.close()


# ---------- 胶水机检（宿主机无 fastapi：接线存在性；E2E 归 M15-7） ----------

def test_batch_api_glue_mounts_quota_headers():
    """提交端点把 quota_headers 挂上 JSONResponse（429/413 响应头出得去）。"""
    src = (REPO / "service" / "app" / "batch_api.py").read_text(encoding="utf-8")
    assert "headers=batch.quota_headers(body, status)" in src


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== TASK-M15-5 配额 429 单元测试（{len(tests)} 条）==")
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
