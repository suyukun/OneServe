#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量快照 scope=snapshot 单元测试（TASK-M15-6，AC-12）——纯 python 直跑或 pytest 收集均可。

覆盖（卡 task-016 / 门槛稿 A3 AC-12；NC-2 已按建议口径批＝v0 全量快照，增量归 M2）：
  AC-12 小键集快照全流程：提交（无名单）→执行（SCAN 物化＋分片导出）→交付
        行数=版本元信息 row_count（reconcile 查一同族·全等硬判）；
  对账  快照行数 ≠ row_count / 空版本 / 版本元信息缺行数字段 → failed＋机读码，零导出；
  版本  版本切换后新快照取新版本（钉启动时 live_version，AC-11 同口径）；
  并发  快照运行中名单（点查名单）任务排队不互踩（单 worker FIFO，AC-3 同机制）；
  重试  snapshot 任务级重试：物化名单作废→按重试时点版本重扫（隐式名单语义）；
  续跑  物化后崩溃→按检查点续跑（名单已显式化，A4 同机制）；
  校验  提交校验：scope=snapshot 拒 keys／非法 scope／字段越权；不受单任务键上限约束。

被测对象 service/app/batch.py＋batch_worker.py 不依赖 FastAPI/redis（store 注入）。
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx                      # noqa: E402
from app import batch_delivery as bd                           # noqa: E402
from app import batch_worker as w                              # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
APPKEY = "oneserve-dev-appkey-001"            # demo_merchant 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32, "90" * 32]
VER = "20260922"                              # 假线上版本（meta 指针值）
VER2 = "20260923"                             # T+1 切换目标版本
AS_OF = "2026-09-22 00:00:00"
AS_OF2 = "2026-09-23 00:00:00"
KV = SERVICES[batch.BATCH_SOURCE_SERVICE]["backend"]["kv"]
JOB = lambda jid: batch.JOB_KEY_PREFIX + jid   # noqa: E731


def dkey(version, sm3):
    return KV["data_key"].format(version=version, sm3=sm3)


class FakeSnapshotRedis:
    """字典假 Redis：batch/worker/快照用到的全部方法（scan_iter 覆盖 strings）。"""

    def __init__(self):
        self.strings: dict[str, str] = {}      # alias 指针＋数据键（get/mget）
        self.hashes: dict[str, dict[str, str]] = {}   # 任务 hash＋版本元信息
        self.lists: dict[str, list[str]] = {}  # FIFO 队列＋键名单
        self.get_calls: dict[str, int] = {}
        self.mget_calls = 0
        self.mget_hook = None                  # f(调用序号)，每次 mget 开头执行

    # ---------- 键值 ----------
    async def get(self, key):
        self.get_calls[key] = self.get_calls.get(key, 0) + 1
        return self.strings.get(key)

    async def mget(self, *keys):
        await asyncio.sleep(0)                 # 让出事件循环（并发场景可交错）
        self.mget_calls += 1
        if self.mget_hook:
            self.mget_hook(self.mget_calls)
        return [self.strings.get(k) for k in keys]

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def delete(self, *keys):
        removed = 0
        for k in keys:
            removed += int(k in self.strings) + int(k in self.hashes) \
                + int(k in self.lists)
            self.strings.pop(k, None)
            self.hashes.pop(k, None)
            self.lists.pop(k, None)
        return removed

    # ---------- list ----------
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
        kept: list[str] = []
        removed = 0
        for v in vals:
            if str(value) == v and (count == 0 or removed < count):
                removed += 1
                continue
            kept.append(v)
        self.lists[key] = kept
        return removed

    async def incrby(self, key, amount):
        value = int(self.strings.get(key) or 0) + int(amount)
        self.strings[key] = str(value)
        return value

    async def expire(self, key, seconds):
        self.strings.setdefault("__ttl__:" + key, str(seconds))
        return True

    async def scan_iter(self, match=None):
        import fnmatch
        for k in sorted(set(self.hashes) | set(self.strings)):
            if match is None or fnmatch.fnmatch(k, match):
                yield k


def seed_version(st, version=VER, data_as_of=AS_OF, row_count=None, keys=()):
    """预置一个线上版本：alias 指针＋版本元信息＋数据键（row_count 缺省=键数）。"""
    st.strings[KV["alias_key"]] = version
    meta = {"data_as_of": data_as_of}
    if row_count is not None:
        meta["row_count"] = str(row_count)
    st.hashes[KV["version_meta_key"].format(version=version)] = meta
    for k in keys:
        st.strings[dkey(version, k)] = json.dumps({"credit_limit_band": "1"})


def switch_version(st, version, data_as_of, row_count, keys):
    """T+1 切换：翻转 alias 指针到新版本（版本空间独立写入）。"""
    st.strings[KV["alias_key"]] = version
    st.hashes[KV["version_meta_key"].format(version=version)] = {
        "data_as_of": data_as_of, "row_count": str(row_count)}
    for k in keys:
        st.strings[dkey(version, k)] = json.dumps({"churn_risk_band": "9"})


async def submit_async(st, payload, *, quotas=None):
    body, status = await batch.submit_job(st, SERVICES, MATRIX, APPKEY, payload,
                                          quotas=quotas)
    return body, status


def submit(st, payload, **kw):
    return asyncio.run(submit_async(st, payload, **kw))


def submit_snapshot(st, **kw):
    body, status = submit(st, {"scope": "snapshot"}, **kw)
    assert status == 202, body
    return body["job_id"]


def snapshot_keys(n):
    return sorted(KEYS[:n])


# ---------- AC-12：小键集快照全流程（提交→执行→交付行数=row_count） ----------

def test_snapshot_full_flow_rows_equal_row_count():
    """全流程：3 键版本→提交快照→SCAN 物化→分片导出→succeeded；交付行数=row_count。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=3, keys=KEYS[:3])
    jid = submit_snapshot(st)
    # 提交态：无名单落库、total=0、名单 sha256 空（物化后回填）
    job = asyncio.run(batch.JobStore(st).get(jid))
    assert job["scope"] == "snapshot" and job["status"] == "queued"
    assert job["total"] == 0 and job["keys_sha256"] == ""
    assert st.lists.get(JOB(jid) + batch.JOB_KEYS_SUFFIX) in (None, [])  # 未物化
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "succeeded" and job["error"] == ""
        assert job["live_version"] == VER and job["data_as_of"] == AS_OF
        assert (job["processed"], job["found"]) == (3, 3)      # 全键空间导出
        keys_sorted = snapshot_keys(3)
        assert st.lists[JOB(jid) + batch.JOB_KEYS_SUFFIX] == keys_sorted
        assert job["keys_sha256"] == batch.keys_digest(keys_sorted)  # 物化回填
        rows = w.read_rows(td, jid)
        assert [r["key"] for r in rows] == keys_sorted        # 行序=名单序（排序确定性）
        assert all(r["found"] and r["data_as_of"] == AS_OF for r in rows)
        assert len(rows) == 3 == int(asyncio.run(batch.JobStore(st).get(jid))["total"])
        # 交付口径（AC-5 同机制）：下载载荷行数=任务 total=版本元信息 row_count
        payload, status = asyncio.run(
            bd.results_download(st, jid, results_dir=td, caller=APPKEY))
        assert status == 200 and payload["row_count"] == 3    # 行数=row_count（AC-12）


def test_snapshot_takes_new_version_after_switch():
    """版本切换后新快照取新版本：全键集/data_as_of/行数对账均按新版本。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=3, keys=KEYS[:3])
    jid1 = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid1,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "succeeded" and job["live_version"] == VER
        # T+1 切换：新版本 2 键（子集+交叠键也换新值）
        switch_version(st, VER2, AS_OF2, row_count=2, keys=[KEYS[1], KEYS[2]])
        jid2 = submit_snapshot(st)
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid2,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "succeeded"
        assert job["live_version"] == VER2 and job["data_as_of"] == AS_OF2
        rows = w.read_rows(td, jid2)
        assert [r["key"] for r in rows] == sorted([KEYS[1], KEYS[2]])
        assert all(r["fields"] == {"churn_risk_band": "9"}   # 新版本数据
                   and r["data_as_of"] == AS_OF2 for r in rows)
        assert len(rows) == 2                                # 新版本 row_count=2


def test_snapshot_pinned_version_across_mid_run_switch():
    """快照执行中 T+1 切换（物化后分片读前）：钉住启动版本，不混入新版本键值。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=4, keys=KEYS[:4])
    jid = submit_snapshot(st)

    def flip(n):                                   # 第 2 分片读前切换（AC-11 同口径）
        if n == 2:
            switch_version(st, VER2, AS_OF2, row_count=4, keys=KEYS[:4])
    st.mget_hook = flip
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "succeeded"
        assert job["live_version"] == VER and job["data_as_of"] == AS_OF
        rows = w.read_rows(td, jid)
        assert len(rows) == 4 and all(
            r["fields"] == {"credit_limit_band": "1"}       # 全程旧版本口径
            and r["data_as_of"] == AS_OF for r in rows)


# ---------- 对账失败口径（不等=failed+机读码；空版本 failed） ----------

def test_snapshot_empty_version_row_count_zero_failed():
    """空版本（row_count=0）→failed snapshot_empty_version，零导出。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=0, keys=())
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed"
        assert job["error"] == w.ERR_SNAPSHOT_EMPTY
        assert job["row_count_expected"] == 0 and job["keys_scanned"] == 0
        assert not w.part_files(td, jid)                    # 零导出


def test_snapshot_empty_key_space_mismatch_failed():
    """键空间空但 row_count>0 → 行数不等=failed row_count_mismatch（机读码）。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=5, keys=())
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed"
        assert job["error"] == w.ERR_ROWCOUNT_MISMATCH
        assert job["row_count_expected"] == 5 and job["keys_scanned"] == 0
        assert not w.part_files(td, jid)


def test_snapshot_row_count_mismatch_failed():
    """行数不等（3 键 vs row_count=4）→failed row_count_mismatch＋对账明细，零导出。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=4, keys=KEYS[:3])   # 元信息多记 1 行
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed"
        assert job["error"] == w.ERR_ROWCOUNT_MISMATCH
        assert job["row_count_expected"] == 4 and job["keys_scanned"] == 3
        assert not w.part_files(td, jid)                    # 不等即零导出（不交脏账）


def test_snapshot_row_count_missing_failed():
    """版本元信息缺行数字段（无法对账）→failed row_count_missing。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, keys=KEYS[:2])             # 只写 data_as_of
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed"
        assert job["error"] == w.ERR_ROWCOUNT_MISSING
        assert not w.part_files(td, jid)


def test_snapshot_accepts_loader_rows_field_alias():
    """loader 实写 rows 字段（sync/loader.py ver_meta_set）——同口径对账不误杀。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, keys=KEYS[:2])
    st.hashes[KV["version_meta_key"].format(version=VER)]["rows"] = "2"  # loader 口径
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"
        assert job["processed"] == 2


def test_snapshot_no_live_pointer_failed_data_not_ready():
    """无线上版本指针→failed data_not_ready（A4 不读脏，与名单任务同口径）。"""
    st = FakeSnapshotRedis()                                 # 不 seed：无 alias
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed" and job["error"] == w.ERR_NOT_READY


# ---------- 并发：快照运行中名单任务排队不互踩（单 worker FIFO） ----------

def test_snapshot_running_list_job_queued_no_interference():
    """快照执行中后到的名单任务停留 queued（FIFO）；两者各自完成、行不互踩。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=3, keys=KEYS[:3])
    snap_jid = submit_snapshot(st)
    list_keys = [KEYS[0], KEYS[3]]                           # 与快照交叠+独有
    list_jid = asyncio.run(submit_async(st, {"keys": list_keys}))[0]["job_id"]
    seen = {}
    # 快照分片读中同步窥探名单任务态（假 store 直读 hash；不可在事件循环内 asyncio.run）
    st.mget_hook = lambda n: seen.setdefault(
        n, {"status": st.hashes[JOB(list_jid)]["status"],
            "total": st.hashes[JOB(list_jid)]["total"],
            "scope": st.hashes[JOB(list_jid)].get("scope")})
    with tempfile.TemporaryDirectory() as td:
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td,
                                   shard_size=1))           # 3 分片快照→再名单任务
        assert done == [snap_jid, list_jid]                 # FIFO：快照先完成后名单才执行
        mid = seen.get(2)                                   # 快照第 2 分片时名单仍排队
        assert mid is not None and mid["status"] == "queued"
        assert int(mid["total"]) == 2 and mid["scope"] == "list"
        snap = asyncio.run(batch.JobStore(st).get(snap_jid))
        lst = asyncio.run(batch.JobStore(st).get(list_jid))
        assert snap["status"] == "succeeded" and snap["processed"] == 3
        assert lst["status"] == "succeeded" and lst["processed"] == 2
        snap_rows = w.read_rows(td, snap_jid)
        list_rows = w.read_rows(td, list_jid)
        assert [r["key"] for r in snap_rows] == snapshot_keys(3)   # 互不踩：各行其名单
        assert [r["key"] for r in list_rows] == list_keys
        assert st.lists[batch.QUEUE_KEY] == []               # 队列消费干净


# ---------- 任务级重试：物化名单作废→按重试时点版本重扫 ----------

def test_snapshot_retry_rematerializes_current_version():
    """快照失败重试：物化名单作废（total=0/sha 空/名单清空）→按重试时点版本重扫。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=4, keys=KEYS[:3])   # 错账版本
    jid = submit_snapshot(st)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed" and job["error"] == w.ERR_ROWCOUNT_MISMATCH
        body, status = asyncio.run(batch.retry_job(st, jid, caller=APPKEY))
        assert status == 202 and body["total"] == 0
        reset = asyncio.run(batch.JobStore(st).get(jid))
        assert reset["keys_sha256"] == "" and reset["total"] == 0
        assert st.lists.get(JOB(jid) + batch.JOB_KEYS_SUFFIX) in (  # 名单已作废
            None, [])
        # 版本修复（切到对账一致的新版本）→重提按新版本全键空间重扫
        switch_version(st, VER2, AS_OF2, row_count=2, keys=[KEYS[0], KEYS[2]])
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td))
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded"
        assert job["live_version"] == VER2 and job["processed"] == 2
        assert st.lists[JOB(jid) + batch.JOB_KEYS_SUFFIX] == sorted(
            [KEYS[0], KEYS[2]])
        assert [r["key"] for r in w.read_rows(td, jid)] == sorted(
            [KEYS[0], KEYS[2]])


# ---------- A4：物化后崩溃→按检查点续跑（名单已显式化） ----------

def test_snapshot_crash_after_materialize_resumes():
    """物化后分片途中崩溃→重启续跑：物化名单复用、版本钉住、完成分片不重写。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=4, keys=KEYS[:4])
    jid = submit_snapshot(st)
    st.mget_hook = lambda n: (_ for _ in ()).throw(
        KeyboardInterrupt("kill -9 模拟")) if n == 2 else None
    with tempfile.TemporaryDirectory() as td:
        try:
            asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                      results_dir=td, shard_size=2))
            raise AssertionError("崩溃注入未生效")
        except KeyboardInterrupt:
            pass
        st.lists[batch.QUEUE_KEY].remove(jid)
        crashed = asyncio.run(batch.JobStore(st).get(jid))
        assert crashed["status"] == "running" and crashed["total"] == 4
        assert st.lists[JOB(jid) + batch.JOB_KEYS_SUFFIX] == snapshot_keys(4)
        part0_before = (Path(td) / jid / "part-00000.jsonl").read_text("utf-8")
        st.mget_hook = None
        recovered = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                                  results_dir=td, shard_size=2))
        assert recovered == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded" and job["processed"] == 4
        assert job["live_version"] == VER                      # 钉住版本续用
        assert (Path(td) / jid / "part-00000.jsonl").read_text(
            "utf-8") == part0_before                          # 完成分片不重写
        assert len(w.read_rows(td, jid)) == 4                  # 行数=row_count=4


def test_snapshot_crash_during_materialize_rematerializes():
    """物化中途崩溃（名单半写、total 未回填）→续跑整体重物化（total=0 判据）。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=4, keys=KEYS[:4])
    jid = submit_snapshot(st)
    st.lists[JOB(jid) + batch.JOB_KEYS_SUFFIX] = KEYS[:1]    # 模拟半写的残留名单
    asyncio.run(batch.JobStore(st).start(jid, live_version=VER, data_as_of=AS_OF))
    with tempfile.TemporaryDirectory() as td:
        recovered = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                                  results_dir=td, shard_size=2))
        assert recovered == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded" and job["processed"] == 4
        assert st.lists[JOB(jid) + batch.JOB_KEYS_SUFFIX] == snapshot_keys(4)


# ---------- 提交校验（AC-2 同族）＋配额口径 ----------

def test_snapshot_submit_accepted_shape():
    """{scope:"snapshot"} →202：scope/total=0/名单 sha 空/字段缺省全白名单。"""
    st = FakeSnapshotRedis()
    body, status = submit(st, {"scope": "snapshot"})
    assert status == 202
    assert body["scope"] == "snapshot" and body["total"] == 0
    assert body["keys_sha256"] == "" and body["queue_position"] == 1
    grant = kvx.tenant_grant(MATRIX, batch.BATCH_SOURCE_SERVICE, tenant="demo_merchant")
    assert body["fields"] == sorted(grant["fields"])
    view, _ = asyncio.run(batch.get_job(st, body["job_id"]))
    assert view["scope"] == "snapshot" and view["total"] == 0


def test_snapshot_submit_with_keys_rejected():
    """scope=snapshot 携带 keys（名单隐式=全键集）→400 invalid_request，不产生任务。"""
    st = FakeSnapshotRedis()
    body, status = submit(st, {"scope": "snapshot", "keys": KEYS[:1]})
    assert status == 400 and body["error"] == "invalid_request"
    assert st.lists.get(batch.QUEUE_KEY, []) == []           # 未产生任务


def test_snapshot_submit_invalid_scope_rejected():
    """未知 scope→400 invalid_scope（机读码），不产生任务。"""
    st = FakeSnapshotRedis()
    body, status = submit(st, {"scope": "incremental", "keys": KEYS[:1]})
    assert status == 400 and body["error"] == "invalid_scope"
    body, status = submit(st, {"keys": KEYS[:1], "scope": "diff"})
    assert status == 400 and body["error"] == "invalid_scope"


def test_snapshot_submit_fields_not_granted_rejected():
    """快照字段越权→403 fields_not_granted（与名单任务同口径）。"""
    st = FakeSnapshotRedis()
    body, status = submit(st, {"scope": "snapshot",
                               "fields": ["ac_secret_99"]})
    assert status == 403 and body["error"] == "fields_not_granted"


def test_snapshot_exempt_from_per_job_key_cap():
    """快照不受单任务键上限约束（AC-12 全键空间导出；提交时无名单可数）。"""
    st = FakeSnapshotRedis()
    quotas = {"max_keys_per_job": 1, "max_concurrent_jobs": 1,
              "max_daily_keys": 5}
    body, status = submit(st, {"scope": "snapshot"}, quotas=quotas)
    assert status == 202                                       # 同限额下名单任务会 413
    body, status = submit(st, {"keys": KEYS[:2]}, quotas=quotas, )
    assert status == 413 and body["error"] == "too_many_keys"  # 名单任务仍受限


def test_snapshot_keys_counted_into_daily_quota_on_materialize():
    """快照键量计入日配额（M15-5×M15-6 咬合口径）：提交时不计量（无名单可数）、
    不受单任务上限；worker 物化 total 后 INCRBY 当日键＋TTL——此后提交端日配额
    判定含快照已计量部分（名单任务贴限即 429）。"""
    st = FakeSnapshotRedis()
    seed_version(st, VER, AS_OF, row_count=3, keys=KEYS[:3])
    jid = submit_snapshot(st)
    dkey = batch.daily_quota_key("demo_merchant", time.time())
    assert st.strings.get(dkey) in (None, "0")              # 提交时不计量
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"
        assert st.strings[dkey] == "3"                      # 物化 total 后计 3 键
        assert st.strings["__ttl__:" + dkey] == str(batch.DAILY_QUOTA_TTL_S)
    # 已计 3 键进入提交端判定：日配额 4 下名单任务再提 2 键 → 429（拒绝不计量）
    body, status = submit(st, {"keys": KEYS[:2]},
                          quotas={"max_keys_per_job": 10,
                                  "max_concurrent_jobs": 1,
                                  "max_daily_keys": 4})
    assert status == 429 and body["error"] == batch.QUOTA_CODE_DAILY
    assert st.strings[dkey] == "3"                          # 拒绝不重复计量


def test_list_submit_explicit_scope_backward_compatible():
    """显式 scope=list 与缺省 scope 行为一致（旧契约零回归）。"""
    st = FakeSnapshotRedis()
    body1, s1 = submit(st, {"keys": KEYS[:1]})
    body2, s2 = submit(st, {"scope": "list", "keys": KEYS[:1]})
    assert s1 == s2 == 202
    assert body1["scope"] == "list" and body2["scope"] == "list"
    assert body1["total"] == body2["total"] == 1


def test_snapshot_unauthorized_caller_rejected():
    """未授权调用方提交快照→403（与名单任务同一授权矩阵）。"""
    st = FakeSnapshotRedis()
    body, status = submit(st, {"scope": "snapshot"})
    assert status == 202                                       # 前置：合法 AppKey
    st2 = FakeSnapshotRedis()
    body, status = asyncio.run(
        batch.submit_job(st2, SERVICES, MATRIX, "no-such-appkey",
                         {"scope": "snapshot"}))
    assert status == 403 and body["error"] == "unknown_tenant"


# ---------- 胶水机检 ----------

def test_snapshot_suite_wired_in_gates():
    """run_gates 已接本套件（门禁阻断口径，同 M15-2/3 接电方式）。"""
    gates = (REPO / "scripts" / "run_gates.sh").read_text(encoding="utf-8")
    assert "tests/test_batch_snapshot.py" in gates


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== TASK-M15-6 全量快照 scope=snapshot 单元测试（{len(tests)} 条）==")
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
        except AssertionError as exc:
            print(f"  ✗ {name}：{exc}")
            failed.append(name)
        except Exception as exc:  # noqa: BLE001 —— 非断言异常也计失败（直跑无 pytest 兜底）
            print(f"  ✗ {name}：{type(exc).__name__}: {exc}")
            failed.append(name)
    if failed:
        print(f"== 未过：{len(tests) - len(failed)}/{len(tests)}；"
              f"失败：{'、'.join(failed)} ==")
        return 1
    print(f"== 全绿：{len(tests)}/{len(tests)} ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
