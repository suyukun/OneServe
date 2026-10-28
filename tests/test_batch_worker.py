#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量 worker 执行流水线单元测试（TASK-M15-2）——纯 python 直跑或 pytest 收集均可。

覆盖（门槛稿 A3 对本卡的判据＝本套件绿；AC 边界以稿为准）：
  AC-3  FIFO 消费：运行中 1 任务时其余停留 queued，按提交序被调度（单 worker 并发 1）；
  AC-7  键级重试≤2（幂等）→行 error 列，不拖垮整任务；值损坏同口径；全任务无一键
        可读→任务级 failed kv_unavailable（A4 Redis 不可用→可重提）；
  AC-11 钉版本：任务执行期间 T+1 切换全程同版本同 data_as_of；无记录键 found=false；
  AC-6（执行侧语义，端点归 M15-4）分片边界内停止；排队期取消的滞留队项跳过；
  A4   杀进程安全：崩溃→running 残留→重启按 Redis 分片检查点续跑（幂等可重入），
        未钉版本残留→明确置 failed；
  AC-9  白名单∩请求投影＋脱敏复用点查（白名单外字段名零出现，grep 机检）；
  胶水  batch_worker.py 可编译＋sidecar（docker-compose 同镜像）与门禁接线机检。

被测对象 service/app/batch_worker.py 不依赖 FastAPI/redis（store 注入，同 batch.py
纪律）；sidecar 进程级 E2E（python -m app.batch_worker 起真 Redis）归 TASK-M15-7
testkit/compose 场景。
"""

import asyncio
import copy
import json
import py_compile
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx                      # noqa: E402
from app import batch_worker as w                              # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
APPKEY = "oneserve-dev-appkey-001"            # demo_bank 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32, "90" * 32, "12" * 32]
VER = "20260922"                              # 假线上版本（meta 指针值）
AS_OF = "2026-09-22 00:00:00"
KV = SERVICES[batch.BATCH_SOURCE_SERVICE]["backend"]["kv"]
JOB = lambda jid: batch.JOB_KEY_PREFIX + jid   # noqa: E731


def dkey(version, sm3):
    return KV["data_key"].format(version=version, sm3=sm3)


class FakeWorkerRedis:
    """字典假 Redis：batch/worker 用到的全部方法＋故障/进度注入钩子。"""

    def __init__(self):
        self.strings: dict[str, str] = {}      # alias 指针＋数据键（get/mget）
        self.hashes: dict[str, dict[str, str]] = {}   # 任务 hash＋版本元信息
        self.lists: dict[str, list[str]] = {}  # FIFO 队列＋键名单
        # ---- 注入钩子 ----
        self.fail_mget_calls = 0               # 前 N 次 mget 抛错（pipeline 故障）
        self.fail_get_keys: set[str] = set()   # 这些键的 get 永远抛错（单键故障）
        self.get_calls: dict[str, int] = {}    # 单键 GET 计数（重试断言）
        self.mget_calls = 0
        self.mget_hook = None                  # f(调用序号)，每次 mget 开头执行
        self.progress_log: list[tuple[int, int]] = []   # update_progress 轨迹

    # ---------- 键值 ----------
    async def get(self, key):
        self.get_calls[key] = self.get_calls.get(key, 0) + 1
        if key in self.fail_get_keys:
            raise RuntimeError(f"kv unavailable: {key}")
        return self.strings.get(key)

    async def mget(self, *keys):
        await asyncio.sleep(0)                 # 让出事件循环（并发场景可交错）
        self.mget_calls += 1
        if self.mget_hook:
            self.mget_hook(self.mget_calls)
        if self.mget_calls <= self.fail_mget_calls:
            raise RuntimeError("pipeline unavailable")
        return [self.strings.get(k) for k in keys]

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(
            {k: str(v) for k, v in mapping.items()})
        if "processed" in mapping and "found" in mapping and "status" not in mapping:
            self.progress_log.append((int(mapping["processed"]),
                                      int(mapping["found"])))
        return len(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

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
        """LREM 语义（count=0 全清；>0 头部起清 N 个）：取消/重试清残留用（M15-4）。"""
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

    async def scan_iter(self, match=None):
        import fnmatch
        for k in sorted(self.hashes):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    # M15-5：submit 路径日配额计数（提交即计量，东八区日界换键）
    async def incrby(self, key, amount):
        value = int(self.strings.get(key) or 0) + int(amount)
        self.strings[key] = str(value)
        return value

    async def expire(self, key, seconds):
        return True


def seed_kv(st, version=VER, data_as_of=AS_OF):
    """预置线上版本：alias 指针＋版本元信息（data_as_of）。"""
    st.strings[KV["alias_key"]] = version
    st.hashes[KV["version_meta_key"].format(version=version)] = {
        "data_as_of": data_as_of, "row_count": "5"}


def put_user(st, sm3, tags, version=VER):
    """按 loader 存储格式写一个用户的非空标签紧凑 JSON。"""
    st.strings[dkey(version, sm3)] = json.dumps(tags, ensure_ascii=False)


async def submit_async(st, keys, *, fields=None):
    payload = {"keys": keys} if fields is None else {"keys": keys, "fields": fields}
    body, status = await batch.submit_job(st, SERVICES, MATRIX, APPKEY, payload)
    assert status == 202, body
    return body["job_id"]


def submit(st, keys, **kw):
    return asyncio.run(submit_async(st, keys, **kw))


def fresh(typed_keys=3):
    """新假 Redis＋预置线上版本；返回 (store, 前 N 个合法键)。"""
    st = FakeWorkerRedis()
    seed_kv(st)
    return st, KEYS[:typed_keys]


# ---------- 正常流：命中/未命中/投影/脱敏/分片/进度 ----------

def test_normal_flow_hits_misses_projection_desensitize():
    """正常流（2 命中 1 未命中，shard=2→两分片）：行 schema/投影脱敏/进度轨迹/终态。"""
    st, keys = fresh(3)
    put_user(st, keys[0], {"credit_limit_band": "1", "aum_band": "150000",
                           "ac_secret_99": "TOP-SECRET"})   # 白名单外字段
    put_user(st, keys[1], {"churn_risk_band": "9"})
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "succeeded" and job["error"] == ""
        assert job["live_version"] == VER and job["data_as_of"] == AS_OF
        assert (job["processed"], job["found"], job["missed"],
                job["row_errors"]) == (3, 2, 1, 0)
        parts = sorted(p.name for p in (Path(td) / jid).glob("part-*.jsonl"))
        assert parts == ["part-00000.jsonl", "part-00001.jsonl"]
        rows = w.read_rows(td, jid)
        assert [r["key"] for r in rows] == keys          # 行序=名单序
        for row in rows:                                 # AC-5 行 schema 与键序
            assert list(row.keys()) == ["key", "found", "fields",
                                        "data_as_of", "error"]
            assert row["data_as_of"] == AS_OF            # 全程同 data_as_of（AC-11）
        assert rows[0]["found"] and rows[0]["fields"] == {
            "credit_limit_band": "1", "aum_band": "A"}   # band_mask 脱敏
        assert rows[1]["found"] and rows[1]["fields"] == {"churn_risk_band": "9"}
        assert not rows[2]["found"] and rows[2]["fields"] is None
        assert rows[2]["error"] is None                  # 未命中＝如实 false，不伪造
        # AC-9 红线：白名单外字段名与原值零出现（grep 机检口径）
        blob = "\n".join(p.read_text(encoding="utf-8")
                          for p in (Path(td) / jid).glob("part-*.jsonl"))
        assert "ac_secret_99" not in blob and "TOP-SECRET" not in blob
        assert "150000" not in blob                      # 脱敏原值零出现
        # 进度按分片边界推进（processed/found 走 JobStore 接口）
        assert st.progress_log == [(2, 2), (3, 2)]
        # 注：出队归 drain（FIFO/并发用例断言）；process_job 只管单任务执行


def test_empty_result_all_misses():
    """空结果：全部未命中→succeeded、found=0、missed=N（不因无数据失败）。"""
    st, keys = fresh(2)
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"
        assert (job["processed"], job["found"], job["missed"],
                job["row_errors"]) == (2, 0, 2, 0)
        rows = w.read_rows(td, jid)
        assert len(rows) == 2 and all(not r["found"] and r["error"] is None
                                      for r in rows)


def test_requested_fields_subset_projection():
    """请求字段子集：行内只含请求字段（白名单∩请求，AC-9），其余授权字段零出现。"""
    st, keys = fresh(1)
    put_user(st, keys[0], {"credit_limit_band": "1", "aum_band": "150000"})
    jid = submit(st, keys, fields=["aum_band"])
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"
        blob = "\n".join(p.read_text(encoding="utf-8")
                          for p in (Path(td) / jid).glob("part-*.jsonl"))
        assert '"aum_band": "A"' in blob
        assert "credit_limit_band" not in blob            # 未请求＝零出现


# ---------- AC-3：FIFO 消费＋运行中其余停留 queued ----------

def test_fifo_serial_and_queued_while_running():
    """AC-3：单 worker 串行——job1 运行中时 job2/3 停留 queued；执行序=提交序。"""

    async def scenario():
        st = FakeWorkerRedis()
        seed_kv(st)
        for k in KEYS[:3]:
            put_user(st, k, {"credit_limit_band": "1"})
        jids = [await submit_async(st, [k]) for k in KEYS[:3]]

        entered, release = asyncio.Event(), asyncio.Event()
        orig_mget = st.mget

        async def gated_mget(*keys):                     # job1 首个（唯一）分片闸
            if st.mget_calls == 0:
                entered.set()
                await release.wait()
            return await orig_mget(*keys)
        st.mget = gated_mget

        with tempfile.TemporaryDirectory() as td:
            task = asyncio.create_task(
                w.drain(st, SERVICES, MATRIX, results_dir=td))
            await entered.wait()                         # job1 正在执行首分片读
            jobs = batch.JobStore(st)
            j1 = await jobs.get(jids[0])
            assert j1["status"] == "running"             # 运行中=并发已达上限 1
            for j in jids[1:]:                           # 其余必须停留 queued
                assert (await jobs.get(j))["status"] == "queued"
            assert st.lists[batch.QUEUE_KEY] == jids[1:]  # FIFO 队列余量=提交序
            release.set()
            done = await task
            assert done == jids                          # 调度序=提交序（AC-3）
            views = [await jobs.get(j) for j in jids]
            assert all(v["status"] == "succeeded" for v in views)
            stamps = [v["started_at"] for v in views]
            assert stamps == sorted(stamps)              # 开工时序不逆序

    asyncio.run(scenario())


def test_concurrent_drains_exactly_once():
    """并发取一：两个 drain 并发消费，LPOP 原子⇒每任务恰被消费一次、全量完成。"""

    async def scenario():
        st, _ = fresh()
        for k in KEYS[:4]:
            put_user(st, k, {"credit_limit_band": "1"})
        jids = [await submit_async(st, [k]) for k in KEYS[:4]]
        with tempfile.TemporaryDirectory() as td:
            a, b = await asyncio.gather(
                w.drain(st, SERVICES, MATRIX, results_dir=td),
                w.drain(st, SERVICES, MATRIX, results_dir=td))
            assert sorted(a + b) == sorted(jids)         # 各归其一，无重复消费
            assert len(a) + len(b) == len(set(a + b)) == 4
            jobs = batch.JobStore(st)
            for j in jids:
                v = await jobs.get(j)
                assert v["status"] == "succeeded" and v["processed"] == 1
                assert len(w.read_rows(td, j)) == 1      # 行数=名单键数，不重写
            assert st.lists[batch.QUEUE_KEY] == []

    asyncio.run(scenario())


# ---------- AC-7：键级重试与部分失败 ----------

def test_key_read_failure_retries_then_error_row():
    """AC-7：单键读失败重试≤2（GET 幂等，恰 3 次尝试）→行 error，不拖垮整任务。"""
    st, keys = fresh(3)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    put_user(st, keys[2], {"churn_risk_band": "9"})
    st.fail_mget_calls = 1                               # 分片 pipeline 失败→逐键回退
    st.fail_get_keys = {dkey(VER, keys[1])}              # 仅 keys[1] 单键故障
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"              # 不拖垮整任务
        assert (job["found"], job["missed"], job["row_errors"]) == (2, 0, 1)
        bad = w.read_rows(td, jid)[1]
        assert bad["key"] == keys[1] and not bad["found"]
        assert bad["error"] == w.ERR_KV_READ and bad["fields"] is None
        assert st.get_calls[dkey(VER, keys[1])] == 3     # 首读＋重试 2＝3 次尝试


def test_corrupt_value_reread_then_decode_error_row():
    """AC-7（值损坏口径）：非 JSON 值重读≤2 仍坏→row_decode_failed 行，任务照成。"""
    st, keys = fresh(3)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    st.strings[dkey(VER, keys[1])] = "{not-json"         # 值损坏（真 Redis 可复现的坏值）
    jid = submit(st, keys)                               # keys[2] 未命中
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "succeeded"
        assert (job["found"], job["missed"], job["row_errors"]) == (1, 1, 1)
        rows = w.read_rows(td, jid)
        assert rows[1]["error"] == w.ERR_DECODE
        assert st.get_calls[dkey(VER, keys[1])] == 2     # 重读恰 2 次（重试≤2）
        assert st.get_calls.get(dkey(VER, keys[0]), 0) == 0   # pipeline 命中不逐键


def test_all_keys_unreadable_fails_job_then_resubmit():
    """A4：全任务无一键可读→failed kv_unavailable（Redis 不可用口径）→同名单重提成功。"""
    st, keys = fresh(2)
    st.fail_mget_calls = 99                              # pipeline 全失败
    st.fail_get_keys = {dkey(VER, k) for k in keys}      # 逐键也全失败
    jid1 = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid1, results_dir=td))
        assert job["status"] == "failed" and job["error"] == w.ERR_JOB_KV
        assert (job["row_errors"], job["missed"]) == (2, 0)
        assert all(r["error"] == w.ERR_KV_READ for r in w.read_rows(td, jid1))
        # Redis 恢复后凭同名单重提（新 job_id）→成功；两任务互相独立
        st.fail_mget_calls = 0
        st.fail_get_keys.clear()
        for k in keys:
            put_user(st, k, {"credit_limit_band": "1"})
        jid2 = submit(st, keys)
        assert jid2 != jid1
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td))
        assert jid2 in done                                 # 新任务照常调度
        job2 = asyncio.run(batch.JobStore(st).get(jid2))
        assert job2["status"] == "succeeded" and job2["found"] == 2


# ---------- AC-11：钉版本 ----------

def test_version_pinned_across_t1_switch():
    """AC-11：执行中 T+1 切换（第 2 分片读前翻转 alias＋新版数据）→全程同版本同口径。"""
    st, keys = fresh(4)
    for k in keys:                                       # 旧版本：全部命中 A 档
        put_user(st, k, {"aum_band": "150000"})

    def flip(n):                                         # 第 2 次 mget（第 2 分片）前切换
        if n == 2:
            st.strings[KV["alias_key"]] = "20260923"
            put_user(st, keys[2], {"aum_band": "999999"}, version="20260923")
            put_user(st, keys[3], {"aum_band": "999999"}, version="20260923")
            st.hashes[KV["version_meta_key"].format(version="20260923")] = {
                "data_as_of": "2026-09-23 00:00:00", "row_count": "4"}
    st.mget_hook = flip
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert st.strings[KV["alias_key"]] == "20260923"  # 切换确已发生
        assert job["status"] == "succeeded"
        assert job["live_version"] == VER and job["data_as_of"] == AS_OF
        rows = w.read_rows(td, jid)
        assert len(rows) == 4 and all(r["data_as_of"] == AS_OF for r in rows)
        # 切换后读的第 2 分片仍取旧版本键（150000→A），不混入新版值（999999→C）
        assert all(r["fields"] == {"aum_band": "A"} for r in rows)
        assert "999999" not in "\n".join(json.dumps(r) for r in rows)


# ---------- AC-6 执行侧：分片边界内停止；滞留队项跳过 ----------

def test_endpoint_cancel_mid_shard_finalizes():
    """端点取消竞态（M15-4）：分片读中途标 cancelling→本分片落盘后收尾
    （update_progress 拒绝非 running→finalize_cancel），不卡 cancelling。"""
    st, keys = fresh(4)
    for k in keys:
        put_user(st, k, {"credit_limit_band": "1"})
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        asyncio.run(batch.JobStore(st).start(jid, live_version=VER,
                                             data_as_of=AS_OF))
        def cancel_mid_read(n):                  # 第 2 分片读中途：端点标意向
            if n == 2:
                st.hashes[JOB(jid)]["status"] = "cancelling"
        st.mget_hook = cancel_mid_read
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "cancelled"      # 已收尾成终态，不滞留 cancelling
        assert not (Path(td) / jid).exists()     # 分片产物已清
        assert st.lists[batch.QUEUE_KEY] == []   # 未重新入队


def test_cancel_mid_shard_stops_at_boundary():
    """AC-6 执行侧：分片读中途取消→本分片落盘后停，不再迁移状态（清理归 M15-4）。"""
    st, keys = fresh(4)
    for k in keys:
        put_user(st, k, {"credit_limit_band": "1"})
    jid = submit(st, keys)

    def cancel(n):                                       # 第 2 分片读取中：模拟取消端点
        if n == 2:
            st.hashes[JOB(jid)]["status"] = "cancelled"
            st.hashes[JOB(jid)]["finished_at"] = batch.now_iso()
    st.mget_hook = cancel
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "cancelled"              # worker 不越权迁移/不覆盖
        assert int(job["processed"]) == 2                # 检查点停在最后成功分片
        rows = w.read_rows(td, jid)
        assert len(rows) == 4                            # 在途分片按边界语义落盘
        assert sorted(p.name for p in (Path(td) / jid).glob("part-*")) == [
            "part-00000.jsonl", "part-00001.jsonl"]
        assert all(r["error"] is None for r in rows)     # 停止不是失败，行不记错


def test_cancelled_while_queued_stale_queue_entry_skipped():
    """排队期被取消的滞留队项：出队后跳过（终态不迁移），不产分片、不卡队列。"""
    st, keys = fresh(1)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    jid = submit(st, keys)
    asyncio.run(batch.JobStore(st).finish(jid, "cancelled"))   # queued→cancelled
    with tempfile.TemporaryDirectory() as td:
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td))
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "cancelled"
        assert not w.rows_dir(td, jid).exists() or not any(
            w.rows_dir(td, jid).iterdir())               # 未产任何分片
        assert st.lists[batch.QUEUE_KEY] == []           # 滞留项被清出队列


# ---------- AC-6 端点触发路径＋收尾（M15-4）：端点核心在 batch.cancel_job ----------

def test_endpoint_cancel_running_worker_finalizes_at_boundary():
    """端点触发取消（M15-4 集成）：cancel_job 标 stopping 意向→drain 在分片边界
    收尾＝cancelling→cancelled＋清理全部分片产物（AC-6 全链路）。"""
    st, keys = fresh(4)
    for k in keys:
        put_user(st, k, {"credit_limit_band": "1"})
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        # 以 running 模拟已启动：钉版本＋检查点 2（分片 0 已完成落盘）
        asyncio.run(batch.JobStore(st).start(jid, live_version=VER,
                                             data_as_of=AS_OF))
        asyncio.run(batch.JobStore(st).update_progress(jid, 2, 2))
        w._write_rows(w.part_path(td, jid, 0),
                      [{"key": k, "found": True, "fields": {}, "data_as_of": AS_OF,
                        "error": None} for k in KEYS[:2]])
        # 端点触发取消（M15-4 核心路径）：running→cancelling
        body, status = asyncio.run(batch.cancel_job(st, jid, caller=APPKEY))
        assert status == 200 and body["status"] == "cancelling"
        assert (Path(td) / jid / "part-00000.jsonl").exists()   # 产物此刻仍在
        # worker 下一轮消费（分片边界）：收尾＝终态＋清产物
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td,
                                   shard_size=2))
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "cancelled"                     # cancelling→cancelled
        assert not (Path(td) / jid / "part-00000.jsonl").exists()   # 分片产物已清
        assert not (Path(td) / jid).exists()                    # 任务目录已移走


def test_worker_cleanup_parts_removes_shards():
    """cleanup_parts（AC-6 清产物）：全清 part 文件＋移空目录；无目录＝0 不炸。"""
    st, keys = fresh(2)
    jid = submit(st, keys)
    assert w.cleanup_parts("/nonexistent-dir-xyz", jid) == 0    # 无目录幂等
    with tempfile.TemporaryDirectory() as td:
        d = w.rows_dir(td, jid)
        d.mkdir(parents=True)
        for i in range(3):
            w._write_rows(w.part_path(td, jid, i), [{"key": KEYS[0], "found": False,
                                                     "fields": None,
                                                     "data_as_of": AS_OF,
                                                     "error": None}])
        removed = w.cleanup_parts(td, jid)
        assert removed == 3 and not d.exists()                  # 全清＋目录移走


def test_retry_requeues_failed_job_full_pipeline():
    """任务级重试（M15-4 集成）：retry_job 重置检查点→drain 全新执行到 succeeded，
    钉住当期版本（重试后 start 重新读指针，非沿用失败前旧钉）。"""
    st, keys = fresh(2)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    jid = submit(st, keys)
    jobs = batch.JobStore(st)
    with tempfile.TemporaryDirectory() as td:
        # 第一轮：无版本指针（fresh 预置的 alias 摘掉）→start 后 failed data_not_ready
        del st.strings[KV["alias_key"]]
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=2))
        assert job["status"] == "failed" and job["error"] == w.ERR_NOT_READY
        assert int(job["processed"]) == 0 and int(job["found"]) == 0
        # 任务级重试（AC-7）：failed→queued＋重置＋重入队列
        body, status = asyncio.run(batch.retry_job(st, jid, caller=APPKEY))
        assert status == 202 and body["status"] == "queued"
        assert st.lists[batch.QUEUE_KEY] == [jid]
        # 第二轮：补上版本指针→同 job_id 全新执行到 succeeded
        seed_kv(st)
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td,
                                   shard_size=2))
        assert done == [jid]
        job = asyncio.run(jobs.get(jid))
        assert job["status"] == "succeeded" and job["error"] == ""
        assert job["live_version"] == VER and job["data_as_of"] == AS_OF
        assert (job["processed"], job["found"]) == (2, 1)       # 全量重跑口径
        rows = w.read_rows(td, jid)
        assert [r["key"] for r in rows] == keys                 # 原名单序执行


def test_crash_after_cancel_intent_resumes_to_finalized_cancel():
    """A4 边界（M15-4）：端点标 cancelling 后 worker 崩溃→重启 recover_running
    收尾＝清产物＋终态，不续跑。"""
    st, keys = fresh(2)
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        d = w.rows_dir(td, jid)
        d.mkdir(parents=True)
        w._write_rows(w.part_path(td, jid, 0), [{"key": KEYS[0], "found": False,
                                                 "fields": None,
                                                 "data_as_of": AS_OF,
                                                 "error": None}])
        asyncio.run(batch.JobStore(st).start(jid, live_version=VER,
                                             data_as_of=AS_OF))
        asyncio.run(batch.JobStore(st)._set(jid, "running", "cancelling"))
        assert asyncio.run(batch.JobStore(st).get(jid))["status"] == "cancelling"
        resumed = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                                results_dir=td))
        assert resumed == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "cancelled"                     # 意向→终态
        assert not d.exists()                                   # 产物已清
        assert st.lists[batch.QUEUE_KEY] == []                  # 未被重新入队


# ---------- A4：杀进程安全（崩溃续跑/幂等可重入/残留处置） ----------

def test_crash_mid_job_resumes_from_checkpoint():
    """中断恢复：分片途中杀进程（KeyboardInterrupt 穿透）→重启按检查点续跑，
    完成分片不重写、钉住版本与 started_at 不变（幂等可重入）。"""
    st, keys = fresh(4)
    for k in keys:
        put_user(st, k, {"aum_band": "150000"})
    jid = submit(st, keys)
    st.mget_hook = lambda n: (_ for _ in ()).throw(
        KeyboardInterrupt("kill -9 模拟")) if n == 2 else None
    with tempfile.TemporaryDirectory() as td:
        try:
            asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                      results_dir=td, shard_size=2))
            raise AssertionError("崩溃注入未生效")
        except KeyboardInterrupt:
            pass
        st.lists[batch.QUEUE_KEY].remove(jid)             # 崩溃前 worker 已出队该任务
        crashed = asyncio.run(batch.JobStore(st).get(jid))
        assert crashed["status"] == "running"            # 留在 running（A4 续跑态）
        assert crashed["processed"] == 2 and crashed["found"] == 2
        part0_before = (Path(td) / jid / "part-00000.jsonl").read_text("utf-8")
        started_before = crashed["started_at"]

        st.mget_hook = None                              # “重启”后环境恢复
        recovered = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                                  results_dir=td, shard_size=2))
        assert recovered == [jid]                        # 启动恢复接管续跑
        assert asyncio.run(w.drain(st, SERVICES, MATRIX,
                                   results_dir=td)) == []   # 队列已空
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded"
        assert (job["processed"], job["found"],
                job["missed"], job["row_errors"]) == (4, 4, 0, 0)
        assert job["live_version"] == VER                # 钉住的版本续用（不重读 alias）
        assert job["started_at"] == started_before       # 未重复 start
        assert (Path(td) / jid / "part-00000.jsonl").read_text(
            "utf-8") == part0_before                     # 完成分片不重写
        rows = w.read_rows(td, jid)
        assert [r["key"] for r in rows] == keys and all(
            r["fields"] == {"aum_band": "A"} for r in rows)


def test_prepare_resume_drops_stale_parts_beyond_checkpoint():
    """检查点分片序：起点=processed//shard_size；其后残留分片文件被清（整分片重做）。"""
    with tempfile.TemporaryDirectory() as td:
        jd = w.rows_dir(td, "j")
        jd.mkdir(parents=True)
        for name in ("part-00000.jsonl", "part-00001.jsonl", "part-00002.jsonl"):
            (jd / name).write_text("{}\n", encoding="utf-8")
        start = w._prepare_resume(jd, processed=2, shard_size=2)
        assert start == 1
        assert (jd / "part-00000.jsonl").exists()        # 完成分片保留
        assert (jd / "part-00001.jsonl").exists()        # 起始分片将由整分片重写
        assert not (jd / "part-00002.jsonl").exists()    # 检查点后残留清除


def test_resume_with_changed_shard_size_fails_inconsistent():
    """检查点对齐守卫：分片大小被改（检查点不对齐）→明确置 failed，不静默错账。"""
    st, keys = fresh(4)
    for k in keys:
        put_user(st, k, {"credit_limit_band": "1"})
    jid = submit(st, keys)
    asyncio.run(batch.JobStore(st).start(jid, live_version=VER, data_as_of=AS_OF))
    asyncio.run(batch.JobStore(st).update_progress(jid, 2, 2))   # shard=2 时留的检查点
    with tempfile.TemporaryDirectory() as td:
        done = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                             results_dir=td, shard_size=3))  # 改配额重启
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "failed" and job["error"] == w.ERR_INCONSISTENT


def test_running_unpinned_marked_failed_inconsistent():
    """未钉版本的 running 残留（异常态）→明确置 failed job_state_inconsistent（A4）。"""
    st, keys = fresh(1)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    jid = submit(st, keys)
    asyncio.run(batch.JobStore(st)._set(jid, "queued", "running",
                                      {"live_version": "", "data_as_of": ""}))
    with tempfile.TemporaryDirectory() as td:
        done = asyncio.run(w.recover_running(st, SERVICES, MATRIX,
                                             results_dir=td))
        assert done == [jid]
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "failed"
        assert job["error"] == w.ERR_INCONSISTENT


# ---------- 任务级失败口径（fail-closed） ----------

def test_no_live_pointer_fails_data_not_ready():
    """无线上版本指针→failed data_not_ready（A4：不读脏），零分片产出。"""
    st = FakeWorkerRedis()                               # 不 seed：无 alias 指针
    jid = submit(st, KEYS[:1])
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid, results_dir=td))
        assert job["status"] == "failed" and job["error"] == w.ERR_NOT_READY
        assert not w.rows_dir(td, jid).exists()


def test_grant_revoked_before_execution_fails_closed():
    """执行时点授权复核：提交后矩阵撤销→failed（机读码同点查），零分片产出。"""
    st, keys = fresh(1)
    jid = submit(st, keys)
    matrix = copy.deepcopy(MATRIX)
    matrix[0]["grants"] = [g for g in matrix[0]["grants"]
                           if g["service"] != batch.BATCH_SOURCE_SERVICE]
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, matrix, jid, results_dir=td))
        assert job["status"] == "failed" and job["error"] == "service_not_granted"
        assert not w.rows_dir(td, jid).exists()


def test_drain_skips_missing_job_queue_entry():
    """队项指向不存在任务（脏队列）→记日志跳过，不卡后续消费。"""
    st, keys = fresh(1)
    put_user(st, keys[0], {"credit_limit_band": "1"})
    jid = submit(st, keys)
    st.lists[batch.QUEUE_KEY].insert(0, "ghost-job")     # 队头塞脏项
    with tempfile.TemporaryDirectory() as td:
        done = asyncio.run(w.drain(st, SERVICES, MATRIX, results_dir=td))
        assert done == [jid]                             # 脏项跳过后正常消费真任务
        job = asyncio.run(batch.JobStore(st).get(jid))
        assert job["status"] == "succeeded"


# ---------- 胶水机检（宿主机无 fastapi/redis：语法＋sidecar/门禁接线） ----------

def test_worker_module_compiles_with_entrypoint():
    """batch_worker.py 语法可编译＋声明 sidecar 入口（进程级 E2E 归 M15-7 场景）。"""
    mod = REPO / "service" / "app" / "batch_worker.py"
    py_compile.compile(str(mod), doraise=True)
    src = mod.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src
    assert "async def run_forever" in src and "async def process_job" in src
    head = src.split("def main()", 1)[0]                # main() 之前＝纯逻辑层
    assert "import redis" not in head and "from redis" not in head  # 延迟导入纪律


def test_sidecar_wired_in_compose_and_gates():
    """同镜像 sidecar（B3/D13）：compose 定义 batch-worker（同镜像/重启续跑/产物卷）
    ＋run_gates 已接本套件（pre-commit 阻断口径）。"""
    compose = yaml.safe_load(
        (REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    svc, worker = compose["services"]["service"], compose["services"]["batch-worker"]
    assert worker["image"] == svc["image"]               # 同镜像（D13）
    assert worker["command"] == ["python", "-m", "app.batch_worker"]
    assert worker["restart"] == "unless-stopped"         # 杀进程安全：崩溃→重启→续跑
    mounts = " ".join(worker["volumes"])
    assert "batch-results" in mounts                     # 分片产物 named volume
    assert any("batch-results" in v for v in compose["volumes"])
    gates = (REPO / "scripts" / "run_gates.sh").read_text(encoding="utf-8")
    assert "tests/test_batch_worker.py" in gates


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print(f"== TASK-M15-2 批量 worker 执行流水线单元测试（{len(tests)} 条）==")
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
