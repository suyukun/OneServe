# -*- coding: utf-8 -*-
"""批量查询异步任务框架核心（TASK-M15-1，开工门槛-M1.5批量查询_v0 §B4/C1）。

本模块＝「任务登记＋状态机＋Redis 任务结构」的纯逻辑核心，覆盖 AC-1/2/4：
  - 提交：键名单校验→授权→登记任务→入 FIFO 队列→返回 job_id＋排队位次；
  - 状态机：queued/running/succeeded/failed/cancelled 五态＋合法迁移表（全迁移可测）；
    M15-4 加法扩展：cancelling 中间态（取消端点→worker 分片边界收尾）＋failed→queued
    （任务级重试），扩法见状态机块注释（M15-1 契约：迁移合法性集中一处可机检）；
  - 查询：状态/进度（processed/total 百分比）＋所钉版本 data_as_of。

设计约束（同 kv_executor 纪律）：
  - 本模块不 import FastAPI/pydantic/redis——KV 访问经 store 参数注入（鸭子类型：
    需要异步 rpush/lpos/hset/hgetall 四个方法，redis.asyncio 客户端原生满足），
    单元测试用字典假实现即可全路径覆盖；
  - 授权与键校验复用 kv_executor（tenant_grant / normalize_key），口径与点查同源；
  - 审计七元组经 audit 参数注入（audit.py 纯 stdlib，可注入真 AuditWriter 做断言）。

配额（NC-4 静态口径起步，.env/环境变量可覆盖，§D NC-4）：
  BATCH_MAX_KEYS_PER_JOB    单任务键上限，默认 100 万（本卡在提交校验强制＝AC-2 超上限）；
  BATCH_MAX_CONCURRENT_JOBS 每租户并发任务上限，默认 1（worker 调度强制＝AC-3，TASK-M15-2）；
  BATCH_MAX_DAILY_KEYS      每租户日键量上限，默认 500 万（429 限流＝AC-10，TASK-M15-5）。

范围红线（后续卡，本卡不做）：worker 执行流水线/分片落盘（M15-2）、结果文件＋分页
交付（M15-3）。M15-4 并入：取消端点（DELETE /v1/batch/jobs/{id}，AC-6）与
任务级重试（POST /v1/batch/jobs/{id}/retry，AC-7）。M15-5 并入：任务级完成
审计（AC-8 后半——终态经 JobStore.finish 落一条，worker 侧 audit 注入见
batch_worker）、提交端配额 429（AC-10——并发/日键量两口径；单任务上限 413
已有）、端点审计第八列 trace_id 接线（P0-5 注记落点）、网关 /v1/batch/* 路由。
M15-6 并入：全量快照 scope=snapshot（提交端 scope 分支＋worker SCAN 物化，
快照不受单任务键上限、日配额物化后计量——两口径咬合见 submit_job/worker）。
本卡预埋件沿用：名单 sha256 落任务记录（双 sha256 的名单侧）、每次端点调用
落审计（SPEC §3.2 同构）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from app import errors
from app import kv_executor as kvx
from app.audit import AuditWriter, make_entry
from app.trace import new_trace_id   # M15-5：任务级审计 trace_id 按任务终态生成

# 批量任务的读路径载体：worker（M15-2）按现役主标签服务的授权白名单投影执行（B2）。
# 2026-10-15 实战首跑修复：原值 user_tag_query 已随 09-29 全服务下线删除（批量链路
# 当时未接线漏更名），任何提交都 403 service_not_granted、队列永空——对齐 appkeys.py
# TRIAL_SOURCE_SERVICE / main.py /v1/tags/query 的同名修复先例，改指现役主标签服务。
BATCH_SOURCE_SERVICE = "v1-svc-usr-asset-tag"

# 任务状态机（AC-4 五态；M15-4 加法扩展 cancelling 中间态）
STATES = ("queued", "running", "cancelling", "succeeded", "failed", "cancelled")
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
# 合法迁移表：排队→执行/取消；执行→完成/失败/取消中；终态不再迁移。
# M15-4 对 M15-1 契约的两处加法扩展（只增不改，原 25 组迁移全部保留）：
#   ① running→cancelling（取消端点对 running 任务只标记停止意向；worker 在分片
#      边界发现 cancelling 后迁 cancelling→cancelled 并清理分片产物——把「停」与
#      「收尾」分离，避免端点与执行侧竞争同一状态字段）；
#   ② failed→queued（任务级重试端点，AC-7：凭原名单重入队列；键名单/口径语义
#      不变，仅重置进度/检查点字段；键级重试≤2 仍在 worker 内，本卡只做任务级）。
#   queued→cancelled（queued 直接取消）为 M15-1 既有迁移，本卡端点沿用。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "cancelling"}),
    "cancelling": frozenset({"cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset({"queued"}),
    "cancelled": frozenset(),
}

# Redis 任务结构键（B4 契约：任务 hash＋键名单 list＋FIFO 队列 list）
QUEUE_KEY = "oneserve:batch:queue"
JOB_KEY_PREFIX = "oneserve:batch:job:"
JOB_KEYS_SUFFIX = ":keys"

# 审计端点名（七元组 endpoint 字段口径：静态名，job_id/名单摘要入 params）
AUDIT_SUBMIT_ENDPOINT = "service:/v1/batch/jobs"
AUDIT_STATUS_ENDPOINT = "service:/v1/batch/jobs/status"
AUDIT_CANCEL_ENDPOINT = "service:/v1/batch/jobs/cancel"     # M15-4：DELETE 取消
AUDIT_RETRY_ENDPOINT = "service:/v1/batch/jobs/retry"       # M15-4：POST 重试
AUDIT_JOB_FINISH_ENDPOINT = "service:/v1/batch/jobs/finish"  # M15-5：任务级终态审计

# NC-4 配额默认值（估算起步，.env 可覆盖）
DEFAULT_MAX_KEYS_PER_JOB = 1_000_000      # 单任务 ≤ 100 万键
DEFAULT_MAX_CONCURRENT_JOBS = 1           # 并发 1 / 租户（worker 并发=1，B3）
DEFAULT_MAX_DAILY_KEYS = 5_000_000        # 日 ≤ 500 万键 / 租户

# 任务口径（M15-6，AC-12）：list=显式键名单（AC-1 既有）；snapshot=全量快照——
# 无名单，隐式名单＝执行时点线上版本全键集（worker SCAN 物化，batch_worker）。
# 配额口径（M15-5×M15-6 咬合）：snapshot 不受单任务键上限约束（提交时无名单
# 可数，AC-12 要求按全键空间导出）；日键量配额照计——键量在 worker 物化 total
# 后计量（提交端无从数起），见 batch_worker.process_job。
SCOPE_LIST = "list"
SCOPE_SNAPSHOT = "snapshot"
SCOPES = (SCOPE_LIST, SCOPE_SNAPSHOT)

# 配额 429 机读码与响应头口径（AC-10，M15-5；NC-4 三口径静态起步）
QUOTA_CODE_CONCURRENT = errors.QUOTA_CONCURRENT_EXCEEDED  # 并发：租户已有执行中任务
QUOTA_CODE_DAILY = errors.QUOTA_DAILY_EXCEEDED            # 日键量：当日累计超上限
# X-RateLimit-Reason（Stripe 式：指明撞了哪种配额）三值：
#   concurrency＝租户并发（全局口径）/ per_job＝单任务键上限 / daily＝日键量配额
RATE_LIMIT_REASON_CONCURRENT = "concurrency"
RATE_LIMIT_REASON_PER_JOB = "per_job"
RATE_LIMIT_REASON_DAILY = "daily"
# 并发 429 的 Retry-After＝保守固定提示（任务完成时间不可知）；日配额精确到下一日界
RETRY_AFTER_CONCURRENCY_S = 30
DAILY_QUOTA_KEY_PREFIX = "oneserve:batch:quota:daily:"   # +tenant:+YYYYMMDD（东八区）
DAILY_QUOTA_TTL_S = 172800      # 计数键 48h 清理（当日清零＝按日换键，不是 TTL 清零）
# 并发口径＝「执行中」任务：running＋cancelling（queued 只排队不占并发位，AC-3 FIFO）
IN_FLIGHT_STATES = frozenset({"running", "cancelling"})
# 任务级终态审计的 status 映射（七元组同构；499＝nginx「客户端关闭」惯例口径）
TASK_AUDIT_STATUS = {"succeeded": 200, "failed": 500, "cancelled": 499}


class BatchError(Exception):
    """批量任务可控错误：携带 HTTP 状态码与机读错误码（端点层直接映射响应）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def quota_limits(env: Mapping[str, str] = os.environ) -> dict[str, int]:
    """读配额三口径（NC-4）：环境变量覆盖，缺省用硬编码起步值。"""
    return {
        "max_keys_per_job": int(env.get("BATCH_MAX_KEYS_PER_JOB",
                                        DEFAULT_MAX_KEYS_PER_JOB)),
        "max_concurrent_jobs": int(env.get("BATCH_MAX_CONCURRENT_JOBS",
                                           DEFAULT_MAX_CONCURRENT_JOBS)),
        "max_daily_keys": int(env.get("BATCH_MAX_DAILY_KEYS",
                                      DEFAULT_MAX_DAILY_KEYS)),
    }


def now_iso() -> str:
    """UTC ISO8601 毫秒（与审计七元组 ts 同款式）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def keys_digest(keys: list[str]) -> str:
    """名单 sha256（NC-5 双 sha256 之名单侧）：对归一化后键集按序换行拼接取摘要。"""
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def daily_quota_key(tenant: str, now_ts: float) -> str:
    """日配额计数键：tenant＋东八区日期（与 stats.day_key 同业务日口径；按日换键
    即「当日清零」——新日新键从 0 计，无需定时任务）。"""
    day = datetime.fromtimestamp(now_ts + 8 * 3600, tz=timezone.utc)
    return f"{DAILY_QUOTA_KEY_PREFIX}{tenant}:{day.strftime('%Y%m%d')}"


def seconds_to_next_day(now_ts: float) -> int:
    """距下一东八区日界的秒数（日配额 429 的 Retry-After 精确值；下限 1s 防零值）。"""
    return max(1, ((int(now_ts) + 8 * 3600) // 86400 + 1) * 86400 - 8 * 3600
                  - int(now_ts))


async def count_tenant_active_jobs(store: Any, tenant: str) -> int:
    """数租户执行中（running/cancelling）任务数（提交端并发配额判定，AC-10）。

    权威口径＝任务 hash 里的状态字段（与 worker.recover_running 同款 scan：键名单
    list 跳过）。M1.5 任务量级（日≤500 万键 ≈ 日均数十任务）全量 scan 足够，
    不另建计数器（崩溃自愈：状态即计数，无第二份真相）。
    """
    n = 0
    async for key in store.scan_iter(match=JOB_KEY_PREFIX + "*"):
        if key.endswith(JOB_KEYS_SUFFIX):
            continue                                  # 键名单 list 不是任务 hash
        raw = await store.hgetall(key)
        if raw.get("tenant") == tenant and raw.get("status") in IN_FLIGHT_STATES:
            n += 1
    return n


def check_transition(current: str, target: str) -> None:
    """状态机合法性：非法迁移抛 409（含未知状态）。"""
    if current not in ALLOWED_TRANSITIONS:
        raise BatchError(409, errors.ILLEGAL_TRANSITION, f"未知状态 {current!r}")
    if target not in ALLOWED_TRANSITIONS[current]:
        raise BatchError(409, errors.ILLEGAL_TRANSITION,
                         f"不允许迁移 {current}→{target}（合法目标："
                         f"{sorted(ALLOWED_TRANSITIONS[current]) or ['无（终态）']}）")


# ---------- Redis 任务结构（store 注入：rpush/lpos/hset/hgetall） ----------

# 任务 hash 里的整型字段（Redis hash 值皆 str，读出时还原）
# missed/row_errors＝M15-2 worker 完成时附写的未命中/行错计数（部分失败语义，AC-7）；
# row_count_expected/keys_scanned＝M15-6 快照对账失败附写明细（failed 机读码佐证）
_INT_FIELDS = ("total", "processed", "found", "missed", "row_errors",
               "row_count_expected", "keys_scanned")


class JobStore:
    """批量任务 Redis 结构封装（队列 hash/键名单/FIFO 队列；单 worker 串行口径）。

    store：redis.asyncio 客户端（decode_responses=True）或等价假实现；
    并发口径＝提交端 RPUSH 入队、worker 端 LPOP 出队（FIFO，AC-3 结构基础）。
    """

    def __init__(self, store: Any):
        self._s = store

    async def create(self, job: dict, keys: list[str]) -> int:
        """登记任务：写任务 hash＋键名单 list＋入队；返回排队位次（1 起，队头=1）。"""
        jid = job["job_id"]
        await self._s.hset(JOB_KEY_PREFIX + jid, mapping=job)
        if keys:
            await self._s.rpush(JOB_KEY_PREFIX + jid + JOB_KEYS_SUFFIX, *keys)
        await self._s.rpush(QUEUE_KEY, jid)
        pos = await self._s.lpos(QUEUE_KEY, jid)
        return (pos if pos is not None else 0) + 1

    async def get(self, job_id: str) -> dict | None:
        """读任务记录（整型字段还原）；不存在返回 None。"""
        raw = await self._s.hgetall(JOB_KEY_PREFIX + job_id)
        if not raw:
            return None
        job = dict(raw)
        for f in _INT_FIELDS:
            if f in job:
                job[f] = int(job[f])
        return job

    async def get_keys(self, job_id: str) -> list[str]:
        """读任务键名单（worker M15-2 消费；store 需实现 lrange）。"""
        return list(await self._s.lrange(JOB_KEY_PREFIX + job_id + JOB_KEYS_SUFFIX, 0, -1))

    async def queue_position(self, job_id: str) -> int | None:
        """排队位次（1 起）；不在队列返回 None。"""
        pos = await self._s.lpos(QUEUE_KEY, job_id)
        return None if pos is None else pos + 1

    async def _set(self, job_id: str, current: str, target: str,
                   updates: dict | None = None) -> dict:
        """带状态机校验的落盘迁移：非法迁移 409；通过则 HSET 状态＋附带字段。"""
        check_transition(current, target)
        fields = {"status": target, **(updates or {})}
        await self._s.hset(JOB_KEY_PREFIX + job_id,
                           mapping={k: str(v) for k, v in fields.items()})
        job = await self.get(job_id) or {}
        return job

    async def start(self, job_id: str, *, live_version: str,
                    data_as_of: str | None) -> dict:
        """queued→running：钉住启动时 live_version＋data_as_of（AC-11 钉版本的写入口）。"""
        job = await self.get(job_id)
        if job is None:
            raise BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
        return await self._set(job_id, job["status"], "running",
                               {"live_version": live_version or "",
                                "data_as_of": data_as_of or "",
                                "started_at": now_iso()})

    async def finish(self, job_id: str, target: str, *,
                     error: str | None = None,
                     stats: dict | None = None,
                     audit: AuditWriter | None = None) -> dict:
        """→终态（succeeded/failed/cancelled）：记 finished_at；失败带机读 error；
        stats＝完成统计附写字段（M15-2 worker 写 missed/row_errors，部分失败语义）；
        audit≠None 时另落一条任务级终态审计（AC-8 后半，M15-5——迁移成功即落一条：
        双 sha256＋四计数＋caller，写失败不阻断迁移；状态机保证终态只迁一次＝恰好一条）。"""
        if target not in TERMINAL_STATES:
            raise BatchError(409, errors.ILLEGAL_TRANSITION,
                             f"finish 目标必须是终态，收到 {target!r}")
        job = await self.get(job_id)
        if job is None:
            raise BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
        updates = {"finished_at": now_iso(), "error": error or ""}
        if stats:
            updates.update(stats)
        job = await self._set(job_id, job["status"], target, updates)
        audit_job_finish(audit, job)
        return job

    async def update_progress(self, job_id: str, processed: int,
                              found: int) -> dict:
        """进度更新：仅 running 态允许（worker 分片边界写，M15-2 消费）。"""
        job = await self.get(job_id)
        if job is None:
            raise BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
        if job["status"] != "running":
            raise BatchError(409, errors.ILLEGAL_TRANSITION,
                             f"进度更新仅 running 态允许，当前 {job['status']}")
        await self._s.hset(JOB_KEY_PREFIX + job_id,
                           mapping={"processed": str(processed), "found": str(found)})
        job["processed"], job["found"] = processed, found
        return job

    async def set_snapshot_manifest(self, job_id: str, *, total: int,
                                    keys_sha256: str) -> None:
        """快照名单物化回执（M15-6）：total＋名单 sha256 回填（隐式名单→显式化）。

        全量物化完成后由 worker 调用（batch_worker._materialize_snapshot）：
        total 由 0 回填为全键集行数＝此后进度百分比/交付行数对账的口径。
        """
        await self._s.hset(JOB_KEY_PREFIX + job_id,
                           mapping={"total": str(total),
                                    "keys_sha256": keys_sha256})

    async def cancel(self, job_id: str, *,
                     audit: AuditWriter | None = None) -> dict:
        """取消迁移（AC-6 端点写入口，M15-4）：

        queued→cancelled：排队直接取消（终态，附 finished_at；audit 透传 finish
        落任务级终态审计——M15-5：排队取消同样是一条「任务完成」，不漏审计）；
        running→cancelling：只标记停止意向，分片产物清理与 →cancelled 终态
        由 worker 在分片边界收尾（batch_worker.finalize_cancel，M15-4 契约）。
        其余起点态由状态机表统一拒绝（409）。
        """
        job = await self.get(job_id)
        if job is None:
            raise BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
        if job["status"] == "queued":
            return await self.finish(job_id, "cancelled", audit=audit)
        return await self._set(job_id, job["status"], "cancelling")

    async def retry(self, job_id: str) -> dict:
        """任务级重试迁移（AC-7 端点写入口，M15-4）：failed→queued。

        凭原名单重入 FIFO 队列（同 job_id：卡面「同名单重提」语义），仅重置
        进度/检查点与版本钉字段（worker start 时会重新钉住当期版本）；键级
        重试≤2 仍在 worker 内（M15-2），本方法只负责任务级迁移与入队。
        入队前先 LREM 清残留（防御异常滞留重复项：至多一个队列项）。

        snapshot 任务（M15-6）：物化的隐式名单一并作废——重提后按重试时点
        线上版本全键空间重新 SCAN 物化（隐式名单的「原名单」语义＝执行时点
        全键集，沿用旧物化名单会在新版空间下错账），total 归 0 待重扫回填。
        """
        job = await self.get(job_id)
        if job is None:
            raise BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
        is_snapshot = (job.get("scope") or SCOPE_LIST) == SCOPE_SNAPSHOT
        if is_snapshot:      # 物化名单作废（DEL 整表；store 需实现 delete）
            await self._s.delete(JOB_KEY_PREFIX + job_id + JOB_KEYS_SUFFIX)
        job = await self._set(job_id, job["status"], "queued",
                              {"total": 0 if is_snapshot else job.get("total", 0),
                               "keys_sha256": "" if is_snapshot else job["keys_sha256"],
                               "processed": 0,
                               "found": 0, "missed": 0, "row_errors": 0,
                               "live_version": "", "data_as_of": "",
                               "started_at": "", "finished_at": "",
                               "error": ""})
        await self._s.lrem(QUEUE_KEY, 0, job_id)  # 防御：清残留重复项再入队
        await self._s.rpush(QUEUE_KEY, job_id)   # 凭原名单重入 FIFO 队列
        return job

    async def dequeue_stale(self, job_id: str) -> None:
        """从 FIFO 队列摘除指定任务（取消端点清理排队残留；不在队列＝无操作）。

        LREM 语义按出现次数全清（历史滞留重复项一并清）；store 需实现 lrem。
        """
        await self._s.lrem(QUEUE_KEY, 0, job_id)


def job_view(job: dict, position: int | None = None) -> dict:
    """任务记录→对外状态视图（AC-4：状态＋processed/total 百分比＋data_as_of）。"""
    total = job.get("total", 0)
    processed = job.get("processed", 0)
    return {
        "job_id": job["job_id"],
        "tenant": job["tenant"],
        "status": job["status"],
        "scope": job.get("scope") or SCOPE_LIST,   # 旧任务无 scope＝list（缺省口径）
        "total": total,
        "processed": processed,
        "progress": (processed * 100 // total) if total else 0,
        "found": job.get("found", 0),
        "keys_sha256": job["keys_sha256"],
        "fields": json.loads(job.get("fields") or "[]"),
        "live_version": job.get("live_version") or None,
        "data_as_of": job.get("data_as_of") or None,   # 未钉版本（queued）＝None，不伪造
        "created_at": job["created_at"],
        "started_at": job.get("started_at") or None,
        "finished_at": job.get("finished_at") or None,
        "error": job.get("error") or None,
        **({"queue_position": position} if position is not None else {}),
    }


# ---------- 端点核心逻辑（batch_api.py 薄胶水调用；返回 body＋HTTP 状态） ----------

def audit_call(audit: AuditWriter | None, caller: str, endpoint: str, params: str,
           status: int, latency_ms: int, body: dict,
           trace_id: str = "") -> None:
    """落一条审计八元组（trace_id 第八列，P0-5；写失败不阻断业务返回；口径同
    main._audit_call。trace_id 由端点胶水从 request.state 注入——批量链路接线点
    是本参数，此前空串即 P0-5 注记的「接路由时一行接上」）。"""
    if audit is None:
        return
    try:
        audit.append(make_entry(caller=caller, endpoint=endpoint, params=params,
                                status=status, latency_ms=latency_ms,
                                response_body=json.dumps(body, ensure_ascii=False),
                                trace_id=trace_id))
    except Exception:  # noqa: BLE001
        pass  # 审计故障不阻断业务（与点查口径一致）


def job_finish_params(job: dict) -> str:
    """任务级审计 params 明文（审计只落其 sha256 摘要，不落键明文）：
    任务标识七要素（job_id/tenant/caller/status/total＋error）＋双 sha256
    （keys_sha256＋results_sha256）＋四计数（processed/found/missed/row_errors）。
    results_sha256 仅 succeeded 携值（M15-3 口径），failed/cancelled 空串＝不伪造。
    """
    return json.dumps({
        "job_id": job.get("job_id", ""),
        "tenant": job.get("tenant", ""),
        "caller": job.get("caller", ""),
        "status": job.get("status", ""),
        "total": int(job.get("total") or 0),
        "processed": int(job.get("processed") or 0),
        "found": int(job.get("found") or 0),
        "missed": int(job.get("missed") or 0),
        "row_errors": int(job.get("row_errors") or 0),
        "keys_sha256": job.get("keys_sha256", ""),
        "results_sha256": job.get("results_sha256", ""),
        "error": job.get("error") or "",
    }, ensure_ascii=False)


def _job_latency_ms(job: dict) -> int:
    """任务执行耗时毫秒：started_at→finished_at（queued 直取消自 created_at 起，
    即排队到取消的滞留时长；解析失败如实返回 0，不伪造）。"""
    start = job.get("started_at") or job.get("created_at") or ""
    try:
        t0 = datetime.fromisoformat(start)
        t1 = datetime.fromisoformat(job.get("finished_at") or "")
        return max(0, int((t1 - t0).total_seconds() * 1000))
    except ValueError:
        return 0


def audit_job_finish(audit: AuditWriter | None, job: dict) -> None:
    """任务终态任务级审计（AC-8 后半，M15-5）：复用 audit.py 通道另落一条八元组。

    - caller＝提交时落任务记录的 appkey（caller 可溯租户：params 另带 tenant）；
    - trace_id 按任务终态新生成（任务完成是 worker 侧事件、不在任何请求链路内，
      独立 trace 才可独立反查；网关侧请求 trace 归端点七元组列）；
    - status：succeeded=200/failed=500/cancelled=499；latency_ms＝执行耗时；
    - 写失败不阻断终态迁移（与端点审计同口径，JobStore.finish 调用处兜底）。
    """
    if audit is None:
        return
    try:
        audit.append(make_entry(
            caller=job.get("caller") or job.get("tenant") or "unknown",
            endpoint=AUDIT_JOB_FINISH_ENDPOINT,
            params=job_finish_params(job),
            status=TASK_AUDIT_STATUS.get(job.get("status", ""), 200),
            latency_ms=_job_latency_ms(job),
            response_body=json.dumps(
                {"job_id": job.get("job_id", ""), "status": job.get("status", "")},
                ensure_ascii=False),
            trace_id=new_trace_id()))
    except Exception:  # noqa: BLE001
        pass


def quota_headers(body: dict, status: int) -> dict[str, str]:
    """配额响应头映射（AC-10，Stripe 式；batch_api 胶水层直接挂 JSONResponse）。

    X-RateLimit-Reason＝撞了哪种配额（concurrency/per_job/daily）；Retry-After
    仅时间窗型 429 携带（并发＝保守固定提示、日配额＝到下一日界秒数）——413 单任务
    上限不随时间恢复（拆名单重提即可），只带 Reason 不带 Retry-After。
    """
    headers: dict[str, str] = {}
    if status in (413, 429) and body.get("reason"):
        headers["X-RateLimit-Reason"] = str(body["reason"])
    if status == 429 and body.get("retry_after") is not None:
        headers["Retry-After"] = str(body["retry_after"])
    return headers


def _validate_payload(payload: Any) -> tuple[str, list[str] | None, list[str] | None]:
    """请求体形状校验（M15-6 加 scope）：{keys: [...], fields: [...]?} 或
    {scope: "snapshot", fields: [...]?}；返回（scope，keys|None，fields|None）。"""
    if not isinstance(payload, dict):
        raise BatchError(400, errors.INVALID_JSON, "请求体须为 JSON 对象")
    scope = payload.get("scope") or SCOPE_LIST
    if scope not in SCOPES:
        raise BatchError(400, errors.INVALID_SCOPE,
                         f"scope 须为 {'/'.join(SCOPES)}，收到 {scope!r}")
    keys = payload.get("keys")
    if scope == SCOPE_SNAPSHOT:
        # 快照名单隐式＝线上版本全键集（AC-12）：携带 keys 即形状冲突，整单拒绝
        if keys is not None:
            raise BatchError(400, errors.INVALID_REQUEST,
                             "scope=snapshot 不携带 keys（名单隐式=当前线上版本全键集）")
    elif not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise BatchError(400, errors.INVALID_REQUEST, "keys 须为字符串数组（键名单）")
    fields = payload.get("fields")
    if fields is not None and (not isinstance(fields, list)
                               or not all(isinstance(f, str) for f in fields)):
        raise BatchError(400, errors.INVALID_REQUEST, "fields 须为字符串数组（可选）")
    return scope, keys, fields


def _validate_keys(keys: list[str], pattern: str,
                   max_keys: int) -> list[str]:
    """名单校验（AC-1/2/A4）：空/超上限/任一键格式非法→整单拒绝，不产生任务。"""
    if not keys:
        raise BatchError(400, errors.EMPTY_KEYS, "键名单为空")
    if len(keys) > max_keys:
        raise BatchError(413, errors.TOO_MANY_KEYS,
                         f"单任务键数 {len(keys)} 超上限 {max_keys}（BATCH_MAX_KEYS_PER_JOB）")
    normalized: list[str] = []
    for i, raw in enumerate(keys):
        try:
            normalized.append(kvx.normalize_key(raw, pattern))   # 与点查同口径：小写+fullmatch
        except kvx.TagQueryError:
            # 不回显原键（可能携带任意内容）；只报序号，键本身已是调用方资产
            raise BatchError(400, errors.INVALID_KEY,
                             f"第 {i} 个键格式非法（须匹配 {pattern}），整单拒绝") from None
    return normalized


def _validate_fields(fields: list[str] | None,
                     whitelist: set[str]) -> list[str]:
    """请求字段校验（AC-2 越权拒绝）：请求字段必须 ⊆ 授权白名单；缺省=全白名单。"""
    if fields is None:
        return sorted(whitelist)
    not_granted = sorted(set(fields) - whitelist)
    if not_granted:
        raise BatchError(403, errors.FIELDS_NOT_GRANTED,
                         f"请求字段超出授权白名单：{not_granted}")
    return sorted(set(fields))


async def submit_job(store: Any, services: dict, matrix: list[dict], caller: str,
                     payload: Any, *, quotas: dict | None = None,
                     audit: AuditWriter | None = None, trace_id: str = "",
                     now: float | None = None) -> tuple[dict, int]:
    """POST /v1/batch/jobs 核心（AC-1/2/10）：校验→授权→配额→登记→入队→job_id＋位次。

    配额三口径（AC-10，NC-4 静态起步）：单任务键上限→413（携 reason）；并发/日键量
    →429 机读码＋reason＋retry_after（胶水层映射 X-RateLimit-Reason/Retry-After 头）。
    超限均不产生任务。日配额提交即计量（东八区日界清零；取消/失败不退还——计量口径
    简单可审计，NC-4 批复前不引入退还复杂度）。now 可注入＝日界判定可测。
    """
    started = time.monotonic()
    quotas = quotas or quota_limits()
    now_ts = time.time() if now is None else now
    who = caller or "unknown"

    def done(body: dict, status: int, params: str) -> tuple[dict, int]:
        audit_call(audit, who, AUDIT_SUBMIT_ENDPOINT, params, status,
               int((time.monotonic() - started) * 1000), body,
               trace_id=trace_id)
        return body, status

    # 1) 形状校验（AC-2：非法请求体/非法 scope 拒绝，不产生任务）
    try:
        scope, keys, fields = _validate_payload(payload)
    except BatchError as exc:
        return done({"error": exc.code, "message": exc.message}, exc.status,
                    json.dumps({"error": exc.code}))

    # 2) 授权（AC-2：服务未授权拒绝；与点查同一授权矩阵/错误码）
    try:
        grant = kvx.tenant_grant(matrix, BATCH_SOURCE_SERVICE, appkey=caller)
    except kvx.TagQueryError as exc:
        return done({"error": exc.code, "message": exc.message}, exc.status,
                    json.dumps({"error": exc.code}))

    # 3) 名单＋字段校验（AC-2：空/超上限/键格式非法/字段越权；均不产生任务；
    #    超上限＝413 携 per_job reason——单任务键上限即配额三口径之一）。
    #    snapshot 无名单可校（隐式=全键集，M15-6）：跳过名单校验（含单任务键
    #    上限——AC-12 按全键空间导出，不因键数截断；日键量配额＝物化后计量）。
    pattern = services[BATCH_SOURCE_SERVICE]["params"][0]["pattern"]
    try:
        if scope == SCOPE_SNAPSHOT:
            keys = None
        else:
            keys = _validate_keys(keys, pattern, quotas["max_keys_per_job"])
        fields = _validate_fields(fields, grant["fields"])
    except BatchError as exc:
        body = {"error": exc.code, "message": exc.message}
        if exc.code == errors.TOO_MANY_KEYS:
            body["reason"] = RATE_LIMIT_REASON_PER_JOB
        # 名单任务 params 带 total（M15-5 口径）；snapshot 无名单可数→带 scope
        detail = {"total": len(keys)} if keys is not None else {"scope": scope}
        return done(body, exc.status, json.dumps({"error": exc.code, **detail}))

    # 4) 并发配额（AC-10，M15-5）：租户执行中（running/cancelling）任务数≥上限
    #    →429；queued 不占并发位（AC-3：新任务停留 queued 按 FIFO 被调度）。
    #    snapshot 同受并发约束（执行中即占位，与 scope 无关）。
    tenant = grant["tenant"]
    active = await count_tenant_active_jobs(store, tenant)
    if active >= quotas["max_concurrent_jobs"]:
        body = {"error": QUOTA_CODE_CONCURRENT,
                "reason": RATE_LIMIT_REASON_CONCURRENT,
                "retry_after": RETRY_AFTER_CONCURRENCY_S,
                "message": f"租户 {tenant} 已有 {active} 个执行中批量任务，"
                           f"并发上限 {quotas['max_concurrent_jobs']}"
                           f"（BATCH_MAX_CONCURRENT_JOBS），待其完成或取消后再提交"}
        detail = {"total": len(keys)} if keys is not None else {"scope": scope}
        return done(body, 429, json.dumps({"error": QUOTA_CODE_CONCURRENT,
                                           **detail}))

    # 5) 日键量配额（AC-10，M15-5）：当日累计＋本次 > 上限→429（Retry-After=到
    #    下一东八区日界秒数——按日换键即当日清零，次日语义自动恢复）。snapshot
    #    不在提交端拦截/计量（M15-6 咬合：提交时无名单可数）——键量在 worker
    #    物化 total 后计入日配额（batch_worker），单任务上限已在上面跳过。
    if keys is not None:
        dkey = daily_quota_key(tenant, now_ts)
        used = int(await store.get(dkey) or 0)
        if used + len(keys) > quotas["max_daily_keys"]:
            body = {"error": QUOTA_CODE_DAILY, "reason": RATE_LIMIT_REASON_DAILY,
                    "retry_after": seconds_to_next_day(now_ts),
                    "message": f"租户 {tenant} 当日已提交 {used} 键，加本次 {len(keys)} 键"
                               f"将超日配额 {quotas['max_daily_keys']}"
                               f"（BATCH_MAX_DAILY_KEYS），次日自动恢复"}
            return done(body, 429, json.dumps({"error": QUOTA_CODE_DAILY,
                                               "total": len(keys)}))

    # 6) 登记任务＋入 FIFO 队列（AC-1：返回 job_id＋排队位次，状态=queued）；
    #    caller 落任务记录＝任务级终态审计的 caller 来源（AC-8 可溯租户）；
    #    snapshot：total=0（执行时 SCAN 全键集后回填）、名单 sha256 空（物化后回填）。
    job_id = uuid.uuid4().hex
    sha = keys_digest(keys) if keys else ""
    total = len(keys) if keys else 0
    jobs = JobStore(store)
    job = {"job_id": job_id, "tenant": tenant, "caller": who, "scope": scope,
           "status": "queued", "total": total, "processed": 0, "found": 0,
           "keys_sha256": sha, "fields": json.dumps(fields, ensure_ascii=False),
           "live_version": "", "data_as_of": "", "error": "",
           "created_at": now_iso(), "started_at": "", "finished_at": ""}
    position = await jobs.create(job, keys or [])
    if keys is not None:
        # 日配额计量（提交即累计；TTL 48h 兜底清理隔日死键，清零口径在键名日期
        # 里）。snapshot 不在此计量——键量未知，物化 total 后由 worker 计入。
        await store.incrby(dkey, len(keys))
        await store.expire(dkey, DAILY_QUOTA_TTL_S)

    body = {"job_id": job_id, "status": "queued", "queue_position": position,
            "scope": scope, "total": total, "keys_sha256": sha,
            "fields": fields, "tenant": tenant,
            "created_at": job["created_at"]}
    params = {"scope": scope} if scope == SCOPE_SNAPSHOT else {
        "total": total, "keys_sha256": sha}
    return done(body, 202, json.dumps(params))


async def get_job(store: Any, job_id: str, *, caller: str = "unknown",
                  audit: AuditWriter | None = None,
                  trace_id: str = "") -> tuple[dict, int]:
    """GET /v1/batch/jobs/{id} 核心（AC-4）：状态机＋进度百分比＋所钉版本 data_as_of。"""
    started = time.monotonic()
    jobs = JobStore(store)
    job = await jobs.get(job_id)
    if job is None:
        body = {"error": errors.JOB_NOT_FOUND, "message": f"任务 {job_id} 不存在"}
        audit_call(audit, caller or "unknown", AUDIT_STATUS_ENDPOINT,
               json.dumps({"job_id": job_id}), 404,
               int((time.monotonic() - started) * 1000), body,
               trace_id=trace_id)
        return body, 404
    position = await jobs.queue_position(job_id) if job["status"] == "queued" else None
    body = job_view(job, position)
    audit_call(audit, caller or "unknown", AUDIT_STATUS_ENDPOINT,
               json.dumps({"job_id": job_id}), 200,
               int((time.monotonic() - started) * 1000), body,
               trace_id=trace_id)
    return body, 200


async def cancel_job(store: Any, job_id: str, *, caller: str = "unknown",
                     audit: AuditWriter | None = None,
                     trace_id: str = "") -> tuple[dict, int]:
    """DELETE /v1/batch/jobs/{id} 核心（AC-6，M15-4）。

    queued→cancelled：直接终态并清队列残留（滞留队项跳过语义已有执行侧测试）；
    running→cancelling：停止意向标记，worker 分片边界收尾（→cancelled＋清理
    分片产物，batch_worker.finalize_cancel）；cancelling 幂等＝同态 200；
    cancelled 幂等＝同态 200（AC-6「幂等同态」）；succeeded/failed→409
    （succeeded 拒绝是 AC-6 明文；failed 不在取消语义内，同样 409——重试走
    POST retry）；404 job_not_found。每次调用落审计七元组（AC-8 预埋）。
    """
    started = time.monotonic()
    jobs = JobStore(store)

    def done(body: dict, status: int) -> tuple[dict, int]:
        audit_call(audit, caller or "unknown", AUDIT_CANCEL_ENDPOINT,
                   json.dumps({"job_id": job_id}), status,
                   int((time.monotonic() - started) * 1000), body,
                   trace_id=trace_id)
        return body, status

    try:
        current = (await jobs.get(job_id)) or {}
        # audit 透传：queued→cancelled 终态在本调用内落成，任务级终态审计
        # （AC-8 后半）随之落一条；running→cancelling 非终态，终态审计归 worker 收尾。
        target = await jobs.cancel(job_id, audit=audit)
    except BatchError as exc:
        if exc.code == errors.ILLEGAL_TRANSITION:
            # 幂等优先：已取消/取消中（重复请求或竞态）＝同态 200；其余终态拒绝。
            # 重读一次（竞态窗口内状态可能已被 worker/并发请求迁走，以实时为准）。
            live = ((await jobs.get(job_id)) or current).get("status")
            if live in ("cancelling", "cancelled"):
                return done(job_view(await jobs.get(job_id) or current), 200)
            return done({"error": exc.code, "message": exc.message,
                         "status": live}, exc.status)
        return done({"error": exc.code, "message": exc.message}, exc.status)
    if current.get("status") == "queued":
        await jobs.dequeue_stale(job_id)     # 清队列残留（LREM 全清，幂等）
    body = job_view(target)
    return done(body, 200)


async def retry_job(store: Any, job_id: str, *, caller: str = "unknown",
                    audit: AuditWriter | None = None,
                    trace_id: str = "") -> tuple[dict, int]:
    """POST /v1/batch/jobs/{id}/retry 核心（AC-7 任务级，M15-4）。

    failed→queued：凭原名单重入 FIFO 队列（同 job_id），重置进度/检查点/
    版本钉字段，状态=queued＋排队位次；queued/running/cancelling→409
    job_not_retryable（进行中不可重试）；终态 succeeded/cancelled→409
    （卡面语义：仅任务级失败可重提；键级重试≤2 在 worker 内＝M15-2）；
    404 job_not_found。每次调用落审计七元组。
    """
    started = time.monotonic()
    jobs = JobStore(store)

    def done(body: dict, status: int) -> tuple[dict, int]:
        audit_call(audit, caller or "unknown", AUDIT_RETRY_ENDPOINT,
                   json.dumps({"job_id": job_id}), status,
                   int((time.monotonic() - started) * 1000), body,
                   trace_id=trace_id)
        return body, status

    try:
        job = await jobs.retry(job_id)
    except BatchError as exc:
        return done({"error": ("job_not_retryable"
                               if exc.code == errors.ILLEGAL_TRANSITION else exc.code),
                     "message": exc.message}, exc.status)
    position = await jobs.queue_position(job_id)
    body = {"job_id": job_id, "status": "queued",
            "queue_position": position, "total": job.get("total", 0),
            "keys_sha256": job["keys_sha256"], "tenant": job["tenant"],
            "retried_from": "failed"}
    return done(body, 202)
