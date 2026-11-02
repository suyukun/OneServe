#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量结果交付单元测试（TASK-M15-3）——纯 python 直跑或 pytest 收集均可。

覆盖（门槛稿 A3 对本卡的判据＝本套件绿；AC 边界以稿为准）：
  AC-5 下载：任务成功→完整结果文件 JSONL（Content-Disposition 附件名＋sha256 头；
        行 schema key/found/fields/data_as_of/error，行数=名单键数）；
  AC-5 分页：cursor/page/limit——行数据＋next_cursor＋has_more，直接读分片文件，
        跨分片边界翻页与下载字节/read_rows 三方同源同口径；
  完整性：worker 成功即汇总结果文件 sha256 落任务记录（NC-5 双 sha256 结果侧，
        任务级审计条目归 M15-5）；下载头/分页体携带，与 hashlib 独立口径一致；
  A4/语义：404 不存在｜409 未完成（queued/running）｜409 终态无结果（failed/
        cancelled）｜410 结果文件过期/被删；空结果任务（全未命中）照常交付；
  审计（AC-8 预埋口径）：下载/分页/参数拒绝各落一条七元组（同 M15-1 口径）。

被测对象 service/app/batch_delivery.py 不依赖 FastAPI/redis（store 注入，同 batch.py
纪律；文件读复用 batch_worker 分片布局）；batch_api.py 为 FastAPI 薄胶水（宿主机无
fastapi，另行语法机检；网关/compose 级 curl 证据归 TASK-M15-7 testkit 场景）。
"""

import asyncio
import contextlib
import hashlib
import json
import py_compile
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import batch, kv_executor as kvx                      # noqa: E402
from app import batch_delivery as d                            # noqa: E402
from app import batch_worker as w                              # noqa: E402
from app.audit import AuditWriter                              # noqa: E402

from synth_services import load_synth_services, synth_matrix, synth_services_dict  # noqa: E402  合成 fixture（全服务下线，见模块头注）
SERVICES = load_synth_services()   # 线上 services.yaml 已清空（全下线）；机制测试用合成注册表
MATRIX = synth_matrix()
APPKEY = "oneserve-dev-appkey-001"            # demo_merchant 网关凭证（auth_matrix.yaml）
KEYS = ["ab" * 32, "cd" * 32, "ef" * 32, "90" * 32, "12" * 32]
VER = "20260922"                              # 假线上版本（meta 指针值）
AS_OF = "2026-09-22 00:00:00"
KV = SERVICES[batch.BATCH_SOURCE_SERVICE]["backend"]["kv"]
ROW_SCHEMA = ["key", "found", "fields", "data_as_of", "error"]   # AC-5 行 schema


def dkey(version, sm3):
    return KV["data_key"].format(version=version, sm3=sm3)


class FakeDeliveryRedis:
    """字典假 Redis：submit/process_job/delivery 用到的全部方法（值皆 str）。"""

    def __init__(self):
        self.strings: dict[str, str] = {}      # alias 指针＋数据键
        self.hashes: dict[str, dict[str, str]] = {}   # 任务 hash＋版本元信息
        self.lists: dict[str, list[str]] = {}  # FIFO 队列＋键名单

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


def seed_kv(st):
    """预置线上版本：alias 指针＋版本元信息（data_as_of）。"""
    st.strings[KV["alias_key"]] = VER
    st.hashes[KV["version_meta_key"].format(version=VER)] = {
        "data_as_of": AS_OF, "row_count": "5"}


def put_user(st, sm3, tags, version=VER):
    st.strings[dkey(version, sm3)] = json.dumps(tags, ensure_ascii=False)


def submit(st, keys, **kw):
    payload = {"keys": keys} if not kw.get("fields") else {
        "keys": keys, "fields": kw["fields"]}
    body, status = asyncio.run(
        batch.submit_job(st, SERVICES, MATRIX, APPKEY, payload))
    assert status == 202, body
    return body["job_id"]


@contextlib.contextmanager
def delivered_job(keys, tags_map=None, *, shard_size=2):
    """提交并跑成一个小任务；tags_map={键序号: 标签}，未列键＝未命中（空结果口径）。"""
    st = FakeDeliveryRedis()
    seed_kv(st)
    for i, k in enumerate(keys):
        if tags_map and i in tags_map:
            put_user(st, k, tags_map[i])
    jid = submit(st, keys)
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, jid,
                                        results_dir=td, shard_size=shard_size))
        assert job["status"] == "succeeded", job
        yield st, jid, job, td


def page(st, jid, td, **kw):
    """分页查询便捷入口（默认 results_dir=td）。"""
    return asyncio.run(d.results_page(st, jid, results_dir=td, **kw))


def download(st, jid, td, **kw):
    return asyncio.run(d.results_download(st, jid, results_dir=td, **kw))


# ---------- AC-5 下载：完整文件＋头载荷＋行 schema ----------

def test_download_full_file_payload_sha_and_schema():
    """下载载荷：分片序字节拼接＝响应体；sha256 头值与 hashlib/任务记录一致；
    行 schema 键序精确、行数=名单键数、全程同 data_as_of。"""
    tags = {0: {"ac_active_sts_01": "1", "if_acct_fct_24": "150000"},
            1: {"ac_login_sts_02": "9"}}                     # 2 命中 3 未命中，3 分片
    with delivered_job(KEYS[:5], tags, shard_size=2) as (st, jid, job, td):
        payload, status = download(st, jid, td)
        assert status == 200
        assert payload["filename"] == jid + ".jsonl"         # Content-Disposition 用
        assert payload["media_type"] == d.MEDIA_TYPE_NDJSON
        assert payload["row_count"] == 5                      # X-Results-Rows 用
        blob = b"".join(d.iter_result_chunks(payload["paths"]))
        parts = sorted((Path(td) / jid).glob("part-*.jsonl"))
        assert blob == b"".join(p.read_bytes() for p in parts)  # 分片序拼接=交付字节
        rows = [json.loads(x) for x in blob.decode("utf-8").splitlines()]
        assert len(rows) == 5 == job["total"]                 # 行数=名单键数（AC-5）
        for row in rows:
            assert list(row.keys()) == ROW_SCHEMA             # 行 schema 键序精确
            assert row["data_as_of"] == AS_OF
        # sha256 三方一致：下载头载荷＝任务记录＝独立 hashlib（防抵赖/防传输损坏）
        digest = hashlib.sha256(blob).hexdigest()
        assert payload["sha256"] == digest == job["results_sha256"]


def test_download_same_source_as_read_rows_and_pages():
    """同源同口径三方对账：下载字节解析行＝batch_worker.read_rows＝分页全页拼接。"""
    tags = {i: {"ac_active_sts_01": "1"} for i in range(4)}  # 4 命中 1 未命中
    with delivered_job(KEYS[:5], tags, shard_size=2) as (st, jid, job, td):
        payload, status = download(st, jid, td)
        assert status == 200
        blob = b"".join(d.iter_result_chunks(payload["paths"]))
        from_file = [json.loads(x) for x in blob.decode("utf-8").splitlines()]
        assert from_file == w.read_rows(td, jid)              # 与 worker 读回同源
        paged, cursor = [], "0"
        while cursor:                                          # 逐页翻完（limit=2）
            body, s = page(st, jid, td, cursor=cursor, limit="2")
            assert s == 200
            paged.extend(body["rows"])
            cursor = body["next_cursor"]
        assert paged == from_file                              # 分页=文件同口径


# ---------- AC-5 分页：跨分片边界＋cursor/page 别名＋越界宽容 ----------

def test_pagination_boundaries_across_shards():
    """跨分片翻页（5 键 shard=2→3 分片）：limit=2 三页到尾；limit=3 跨分片；
    page 别名=cursor 等价；offset 越过文件尾=空页 has_more=false。"""
    tags = {i: {"ac_active_sts_01": "1"} for i in range(3)}   # 3 命中 2 未命中
    with delivered_job(KEYS[:5], tags, shard_size=2) as (st, jid, job, td):
        all_rows = w.read_rows(td, jid)
        # limit=2：页界 [0:2][2:4][4:5]，第 2 页起点恰在第 2 分片（跨分片边界）
        b1, s1 = page(st, jid, td, cursor="0", limit="2")
        assert s1 == 200 and b1["rows"] == all_rows[0:2]
        assert b1["next_cursor"] == "2" and b1["has_more"] is True
        b2, _ = page(st, jid, td, cursor=b1["next_cursor"], limit="2")
        assert b2["rows"] == all_rows[2:4] and b2["has_more"] is True
        b3, _ = page(st, jid, td, cursor=b2["next_cursor"], limit="2")
        assert b3["rows"] == all_rows[4:5]
        assert b3["has_more"] is False and b3["next_cursor"] is None
        assert b3["total"] == 5 and b3["status"] == "succeeded"
        # limit=3 首页即跨第 1+2 分片；limit>总数单页收尾
        bwide, _ = page(st, jid, td, limit="3")
        assert bwide["rows"] == all_rows[0:3] and bwide["has_more"] is True
        ball, _ = page(st, jid, td, limit="7")
        assert ball["rows"] == all_rows and ball["has_more"] is False
        # page 别名：page=2&limit=2 等价 cursor=2；page=1 等价 cursor=0
        bp, _ = page(st, jid, td, page="2", limit="2")
        assert bp["rows"] == b2["rows"] and bp["next_cursor"] == b2["next_cursor"]
        assert page(st, jid, td, page="1", limit="2")[0]["rows"] == b1["rows"]
        # 越过文件尾：空页收尾（宽容口径，不 400）
        bvoid, svoid = page(st, jid, td, cursor="99", limit="2")
        assert svoid == 200 and bvoid["rows"] == [] and not bvoid["has_more"]
        assert bvoid["next_cursor"] is None
        # 分页体携带结果 sha256（与下载头同值同源）
        assert b1["results_sha256"] == job["results_sha256"]


def test_empty_results_all_misses_still_deliverable():
    """空结果任务（全未命中）：succeeded 照常交付——下载 200 有 N 行 found=false，
    分页翻完同样 N 行（空结果≠无结果，如实不伪造）。"""
    with delivered_job(KEYS[:3], None) as (st, jid, job, td):
        assert (job["found"], job["missed"]) == (0, 3)
        payload, status = download(st, jid, td)
        assert status == 200 and payload["row_count"] == 3
        blob = b"".join(d.iter_result_chunks(payload["paths"]))
        rows = [json.loads(x) for x in blob.decode("utf-8").splitlines()]
        assert len(rows) == 3
        assert all(not r["found"] and r["fields"] is None and r["error"] is None
                  for r in rows)
        body, s = page(st, jid, td, limit="2")
        assert s == 200 and len(body["rows"]) == 2 and body["has_more"] is True
        assert all(not r["found"] for r in body["rows"])


def test_unfinished_missing_and_terminal_semantics():
    """404/409 语义（两模式同口径）：不存在→404；queued/running→409 job_not_finished；
    failed/cancelled→409 job_not_succeeded（终态永不产结果，凭同名单重提）。"""
    st = FakeDeliveryRedis()
    seed_kv(st)
    put_user(st, KEYS[0], {"ac_active_sts_01": "1"})
    with tempfile.TemporaryDirectory() as td:
        for body, status in (download(st, "nosuchjob", td),
                             page(st, "nosuchjob", td, limit="2")):
            assert status == 404 and body["error"] == "job_not_found"
        q = submit(st, [KEYS[0]])                            # queued（未跑）
        for body, status in (download(st, q, td), page(st, q, td, limit="2")):
            assert status == 409 and body["error"] == "job_not_finished"
        q2 = submit(st, [KEYS[1]])                           # running（已 start 未完）
        asyncio.run(batch.JobStore(st).start(q2, live_version=VER, data_as_of=AS_OF))
        for body, status in (download(st, q2, td), page(st, q2, td, limit="2")):
            assert status == 409 and body["error"] == "job_not_finished"
        st2 = FakeDeliveryRedis()                            # failed：无版本指针
        f = submit(st2, [KEYS[0]])
        job = asyncio.run(w.process_job(st2, SERVICES, MATRIX, f, results_dir=td))
        assert job["status"] == "failed"
        for body, status in (download(st2, f, td), page(st2, f, td, limit="2")):
            assert status == 409 and body["error"] == "job_not_succeeded"
        # M15-5：q2 仍 running，原 store 再提交会被并发配额 429 拒——cancelled
        # 场景换新 store 构造（同上面 failed 场景的 fresh-store 模式，意图不变：
        # 终态 cancelled 的交付语义）。
        st3 = FakeDeliveryRedis()
        c = submit(st3, [KEYS[2]])                           # cancelled（排队期取消）
        asyncio.run(batch.JobStore(st3).finish(c, "cancelled"))
        for body, status in (download(st3, c, td), page(st3, c, td, limit="2")):
            assert status == 409 and body["error"] == "job_not_succeeded"


def test_results_gone_410():
    """A4：succeeded 但结果文件被删/过期→410 results_gone（目录在文件无、整目录无）。"""
    with delivered_job(KEYS[:2], {0: {"ac_active_sts_01": "1"}}) as (st, jid, _, td):
        for p in w.part_files(td, jid):
            p.unlink()                                   # 目录在、分片无
        for body, status in (download(st, jid, td), page(st, jid, td, limit="2")):
            assert status == 410 and body["error"] == "results_gone"
    with delivered_job(KEYS[:2], {0: {"ac_active_sts_01": "1"}}) as (st, jid, _, td):
        shutil.rmtree(Path(td) / jid)                    # 整目录无
        for body, status in (download(st, jid, td), page(st, jid, td, cursor="0")):
            assert status == 410 and body["error"] == "results_gone"


# ---------- 完整性：sha256 落任务记录（NC-5 结果侧） ----------

def test_worker_records_results_sha256_on_success_only():
    """worker 成功即写 results_sha256（＝分片拼接字节摘要、确定性）；失败不写。"""
    with delivered_job(KEYS[:4], {0: {"ac_active_sts_01": "1"}},
                       shard_size=2) as (st, jid, job, td):
        blob = b"".join(p.read_bytes() for p in w.part_files(td, jid))
        assert job["results_sha256"] == hashlib.sha256(blob).hexdigest()
        assert job["results_sha256"] == w.sha256_results(td, jid)  # 复算一致
    st = FakeDeliveryRedis()                             # 失败任务：无结果摘要
    f = submit(st, KEYS[:1])
    with tempfile.TemporaryDirectory() as td:
        job = asyncio.run(w.process_job(st, SERVICES, MATRIX, f, results_dir=td))
    assert job["status"] == "failed" and not job.get("results_sha256")


# ---------- 分页参数校验（机读错误码） ----------

def test_invalid_pagination_params_rejected():
    """非法分页参数→400 逐参数机读码；limit 单独提供＝首页（合法）。"""
    with delivered_job(KEYS[:3], {0: {"ac_active_sts_01": "1"}}) as (st, jid, _, td):
        cases = [("abc", None, None, "invalid_cursor"),
                 ("-1", None, None, "invalid_cursor"),
                 (None, "0", None, "invalid_page"),
                 (None, "x", None, "invalid_page"),
                 (None, None, "0", "invalid_limit"),
                 (None, None, str(d.MAX_PAGE_SIZE + 1), "invalid_limit"),
                 (None, None, "abc", "invalid_limit"),
                 ("1", "1", None, "invalid_request")]      # cursor+page 互斥
        for cursor, pg, lim, code in cases:
            body, status = page(st, jid, td, cursor=cursor, page=pg, limit=lim)
            assert status == 400 and body["error"] == code, (cursor, pg, lim, body)
        body, status = page(st, jid, td, limit="2")        # limit 单独＝首页
        assert status == 200 and body["has_more"] is True
        assert body["next_cursor"] == "2"


# ---------- 审计预埋（AC-8 前半口径，同 M15-1） ----------

def test_audit_seven_tuple_per_delivery_call():
    """下载/分页/参数拒绝各落一条七元组：endpoint 静态名、caller 可溯、成败皆记。"""
    with delivered_job(KEYS[:2], {0: {"ac_active_sts_01": "1"}}) as (st, jid, _, td):
        with tempfile.TemporaryDirectory() as ad:
            writer = AuditWriter(ad, flush_interval=0)     # 同步模式：即时可读
            download(st, jid, td, caller=APPKEY, audit=writer)
            page(st, jid, td, limit="1", caller=APPKEY, audit=writer)
            page(st, jid, td, cursor="bad", caller=APPKEY, audit=writer)  # 拒绝也审计
            entries = [json.loads(x) for x in
                       (Path(ad) / "audit.jsonl").read_text(
                           encoding="utf-8").splitlines()]
            assert len(entries) == 3
            for e in entries:
                assert set(e) >= {"ts", "caller", "endpoint", "params_digest",
                                  "status", "latency_ms", "response_digest"}
                assert e["endpoint"] == d.AUDIT_RESULTS_ENDPOINT
                assert e["caller"] == APPKEY
            assert [e["status"] for e in entries] == [200, 200, 400]
            writer.close()


# ---------- 胶水机检（宿主机无 fastapi：语法＋路由/头声明＋接线） ----------

def test_delivery_glue_compiles_with_route_and_headers():
    """batch_delivery.py 纯逻辑（无 fastapi/redis import）；batch_api.py 可编译且
    声明 results 路由＋流式下载头（Content-Disposition/X-Results-Sha256）。"""
    core = REPO / "service" / "app" / "batch_delivery.py"
    glue = REPO / "service" / "app" / "batch_api.py"
    py_compile.compile(str(core), doraise=True)
    py_compile.compile(str(glue), doraise=True)
    csrc = core.read_text(encoding="utf-8")
    assert "import fastapi" not in csrc and "from fastapi" not in csrc
    assert "import redis" not in csrc and "from redis" not in csrc
    gsrc = glue.read_text(encoding="utf-8")
    route = '@router.get("/v1/batch/jobs/{job_id}/results")'
    assert route in gsrc
    for token in ("StreamingResponse", "Content-Disposition",
                  "X-Results-Sha256", "X-Results-Rows", "batch_delivery"):
        assert token in gsrc, token


def test_batch_api_every_route_returns_response():
    """真栈回归补丁（TASK-M15-7 s5 场景发现）：get_batch_results 分页分支算完
    body 未 return——FastAPI 对无 return 的路由函数回 HTTP 200＋null 体；单测此前
    只盖纯逻辑核心（results_page）与文本令牌，HTTP 胶水分支无人断言。AST 机检：
    batch_api.py 每个 @router 路由函数顶层必须有 return（防「丢了 return」这一
    合并缝隙类再犯）。"""
    """真栈回归补丁（TASK-M15-7 s5 场景发现）：get_batch_results 分页分支算完
    body 未 return——FastAPI 对无 return 的路由函数回 HTTP 200＋null 体；单测此前
    只盖纯逻辑核心（results_page）与文本令牌，HTTP 胶水分支无人断言。AST 机检：
    batch_api.py 每个 @router 路由函数顶层必须有 return（防「丢了 return」这一
    合并缝隙类再犯）。"""
    import ast

    glue = REPO / "service" / "app" / "batch_api.py"
    tree = ast.parse(glue.read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and isinstance(dec.func.value, ast.Name)
            and dec.func.value.id == "router"
            for dec in node.decorator_list)
        if not is_route:
            continue
        checked += 1
        assert any(isinstance(stmt, ast.Return) for stmt in node.body), \
            f"路由 {node.name} 顶层缺 return（FastAPI 将回 null）"
    assert checked >= 5, f"路由数异常：只识别到 {checked} 个 @router 端点"


def test_service_container_shares_results_volume_and_gates_wired():
    """接线：service 容器挂 batch-results 卷＋BATCH_RESULTS_DIR（与 worker 同值）；
    run_gates 已接本套件（pre-commit 阻断口径）。"""
    compose = yaml.safe_load(
        (REPO / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["service"]
    assert "BATCH_RESULTS_DIR=/data/batch-results" in svc["environment"]
    assert any(v.startswith("oneserve-batch-results:") for v in svc["volumes"])
    assert any("oneserve-batch-results" in v
               for v in compose["services"]["batch-worker"]["volumes"])  # 同卷
    gates = (REPO / "scripts" / "run_gates.sh").read_text(encoding="utf-8")
    assert "tests/test_batch_delivery.py" in gates


def main():
    """无 pytest 直跑入口：逐条运行 test_*，全绿退出 0。"""
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    print("== TASK-M15-3 批量结果交付单元测试（%d 条）==" % len(tests))
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  ✓ %s" % name)
        except AssertionError as exc:
            print("  ✗ %s：%s" % (name, exc))
            failed.append(name)
        except Exception as exc:  # noqa: BLE001 —— 非断言异常也计失败（直跑兜底）
            print("  ✗ %s：%s: %s" % (name, type(exc).__name__, exc))
            failed.append(name)
    if failed:
        print("== 未过：%d/%d；失败：%s ==" % (len(tests) - len(failed),
              len(tests), "、".join(failed)))
        return 1
    print("== 全绿：%d/%d ==" % (len(tests), len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
