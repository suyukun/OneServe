# -*- coding: utf-8 -*-
"""批量查询 worker 执行流水线（TASK-M15-2，开工门槛-M1.5批量查询_v0 §B2/B3/C1）。

单 worker 进程（并发=1，B3）的 FIFO 消费循环，覆盖 AC-3/7/11：
  出队（LPOP 队头，提交序）→ queued 转 running（钉住启动时 live_version＋data_as_of，
  AC-11）→ 名单分片（分批 pipeline 读，防 Redis 阻塞）→ 复用点查读路径
  （kv_executor.project＋apply_desensitize，白名单外字段零出现，AC-9 口径同源）
  → 分片落盘（JSONL part 文件，行 schema 同 AC-5：key/found/fields/data_as_of/error）
  → 进度更新（processed/found 走 JobStore.update_progress，分片边界写）
  → 完成迁移（succeeded；附写 missed/row_errors 计数＝部分失败语义）。

失败与中断语义（按稿 A3/A4）：
  - 键级：单键读取失败重试≤2（GET 幂等），仍败记入该行 error 列，不拖垮整任务（AC-7）；
    值损坏（非 JSON）同口径重读后仍坏记 row_decode_failed；
  - 任务级：无线上版本指针→failed data_not_ready（不读脏）；全任务无一键可读→
    failed kv_unavailable（Redis 不可用，凭同名单重提，A4）；执行时点授权复核
    fail-closed（矩阵撤销→failed）；
  - 取消（AC-6 执行侧＋收尾，端点核心在 batch.cancel_job＝M15-4）：分片边界查
    状态——cancelling＝端点已标停止意向：即收尾（cancelling→cancelled、清理全部分
    片产物，finalize_cancel）；分片读中途收到取消意向：本分片写完落盘后同样收尾；
    其余非 running（queued 期取消的 cancelled 等）即停不迁移；
  - 杀进程安全（A4）：分片先落盘后写进度检查点（processed/found 在任务 hash），
    重启后 recover_running 按 Redis 分片状态续跑（钉住版本续用，整分片重做，
    幂等可重入）；未钉版本/检查点不对齐的 running 残留→明确置 failed
    job_state_inconsistent（注：row_errors 计数不跨会话累计，权威口径＝
    结果文件 error 行，M15-3 交付以文件行数对账）。

设计约束（同 batch.py 纪律）：本模块不 import FastAPI/redis——KV 访问经 store
参数注入（鸭子类型：在 JobStore 四方法外另需异步 get/hget/mget/lpop/scan_iter，
redis.asyncio 客户端原生满足），单元测试用字典假实现即可全路径覆盖；redis 仅在
sidecar 入口 main() 延迟导入。

范围红线（后续卡，本卡不做）：下载/分页交付端点（M15-3 batch_delivery.py，
读侧；结果 sha256 已随成功落任务记录）。M15-4 并入本卡：取消端点核心
（batch.cancel_job）＋本模块收尾（finalize_cancel/cleanup_parts）。
M15-5 并入：任务级终态审计（AC-8 后半——本模块经 audit 参数注入 AuditWriter，
终态迁移处落一条，快照分支同口径贯通；写入器与 service 分目录
/BATCH_AUDIT_DIR，同卷不同文件，保 WAL 单写者纪律）＋提交端配额 429
（batch.submit_job）。M15-6 并入：scope=snapshot 全量快照——名单隐式＝线上
版本全键集（SCAN 物化＋row_count 对账见 _materialize_snapshot；快照键量在
物化 total 后计入日配额）。AC-3 的并发上限强制＝本 worker 串行消费本身
（单进程并发 1 ⇒ 每租户运行中任务≤1；提交侧 429 限流在 batch.submit_job）。


配额/参数（.env 可覆盖）：BATCH_SHARD_SIZE 分片批大小（默认 1000，对齐 sync 链
PIPE_BATCH 口径）；BATCH_RESULTS_DIR 分片产物根目录（默认 /data/batch-results）；
BATCH_WORKER_POLL_INTERVAL 队列空轮询间隔秒（默认 1）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from app import batch
from app import errors
from app import kv_executor as kvx

logger = logging.getLogger("oneserve.batch-worker")

# 分片批大小（B3：分批 pipeline 防 Redis 阻塞；对齐 sync 链 PIPE_BATCH=1000）
DEFAULT_SHARD_SIZE = 1000
# 键级重试上限（AC-7：重试≤2＝首读后至多再试 2 次；GET 幂等）
KEY_RETRY_LIMIT = 2
# 分片文件名：part-<5位分片序号>.jsonl（M15-3 拼接交付；M15-4 取消清理）
PART_FILE_FMT = "part-{idx:05d}.jsonl"

RESULTS_DIR_ENV = "BATCH_RESULTS_DIR"
DEFAULT_RESULTS_DIR = "/data/batch-results"
SHARD_SIZE_ENV = "BATCH_SHARD_SIZE"
POLL_INTERVAL_ENV = "BATCH_WORKER_POLL_INTERVAL"
DEFAULT_POLL_INTERVAL = 1.0
# 任务级终态审计写入器目录（M15-5）：与 service 的 AUDIT_DIR 同 named volume、
# 不同子目录（service=/data/audit，本进程=/data/audit/batch）——audit.jsonl/WAL
# 各写各的文件，AuditWriter 的 WAL 单写者纪律不被跨进程破坏。
BATCH_AUDIT_DIR_ENV = "BATCH_AUDIT_DIR"
DEFAULT_BATCH_AUDIT_DIR = "/data/audit/batch"

# 机读错误码
# 码名统一定义在 app/errors.py（P1-2 错误码中心；单一事实来源，消除散落字面量）
ERR_KV_READ = errors.KV_READ_FAILED            # 单键读取重试耗尽（行 error 列，AC-7）
ERR_DECODE = errors.ROW_DECODE_FAILED          # 值损坏重读后仍不可解析（行 error 列）
ERR_JOB_KV = errors.KV_UNAVAILABLE             # 全任务无一键可读（A4：置 failed 可重提）
ERR_NOT_READY = errors.DATA_NOT_READY          # 启动时无线上版本指针（A4：不读脏）
ERR_INCONSISTENT = errors.JOB_STATE_INCONSISTENT  # running 但未钉版本（异常残留→明确失败）
ERR_SNAPSHOT_EMPTY = errors.SNAPSHOT_EMPTY_VERSION  # M15-6：空版本（row_count=0）
ERR_ROWCOUNT_MISSING = errors.ROWCOUNT_MISSING     # M15-6：版本元信息缺行数字段（无法对账）
ERR_ROWCOUNT_MISMATCH = errors.ROWCOUNT_MISMATCH   # M15-6：快照行数 ≠ 版本元信息 row_count
ERR_TENANT_FROZEN = errors.TENANT_FROZEN           # P1-⑮：执行时点核 frozen——冻结租户任务不执行


def _tenant_lifecycle(matrix, tenant: str) -> str:
    """租户生命周期态（执行时点核，P1-⑮）：矩阵行 tenant_status 戳（db 态快照随迁
    位，非 active 才输出）；无戳＝active（yaml 态无生命周期源，门不生效如实标注）。
    与 grants_admin.tenant_status_of 同口径就地实现——worker 不 import 授权写面。"""
    for row in matrix or []:
        if isinstance(row, dict) and str(row.get("tenant", "")) == tenant:
            s = str(row.get("tenant_status", "")).strip()
            return s if s in ("active", "frozen", "terminating", "cancelled") else "active"
    return "active"


# ---------- 分片产物落盘（整分片重写＝崩溃重入幂等） ----------

def rows_dir(results_dir: str | Path, job_id: str) -> Path:
    """单任务分片目录：<results_dir>/<job_id>/。"""
    return Path(results_dir) / job_id


def part_path(results_dir: str | Path, job_id: str, shard_index: int) -> Path:
    """单分片文件路径（序号＝键名单起点 ÷ 分片批大小，确定性可重算）。"""
    return rows_dir(results_dir, job_id) / PART_FILE_FMT.format(idx=shard_index)


def _part_index(path: Path) -> int:
    return int(path.stem.split("-")[1])


def _prepare_fresh(job_dir: Path) -> int:
    """全新任务从分片 0 起（job_id 每次提交新生成，queued 态必无残留分片）。"""
    job_dir.mkdir(parents=True, exist_ok=True)
    return 0


def _prepare_resume(job_dir: Path, processed: int, shard_size: int) -> int:
    """崩溃续跑起点（A4）：processed 只在整分片完成后落检查点 ⇒ 起始分片＝
    processed//shard_size；清掉该分片之后的残留文件（整分片重做，幂等可重入）。"""
    start = processed // shard_size
    if job_dir.is_dir():
        for path in sorted(job_dir.glob("part-*.jsonl")):
            if _part_index(path) > start:
                path.unlink()
    return start


def _write_rows(path: Path, rows: list[dict]) -> None:
    """整分片写盘＋fsync（杀进程安全序：先落盘，后写进度检查点）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def part_files(results_dir: str | Path, job_id: str) -> list[Path]:
    """按分片序列出已落盘文件（M15-3 交付读入口：下载/分页与 read_rows 同源同序）。"""
    job_dir = rows_dir(results_dir, job_id)
    return sorted(job_dir.glob("part-*.jsonl")) if job_dir.is_dir() else []


def read_rows(results_dir: str | Path, job_id: str) -> list[dict]:
    """按分片序拼接读回全部行（worker 自检/测试/M15-3 拼接口径同源）。"""
    out: list[dict] = []
    for path in part_files(results_dir, job_id):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                out.append(json.loads(line))
    return out


def sha256_results(results_dir: str | Path, job_id: str) -> str:
    """结果文件 sha256（NC-5 双 sha256 之结果侧）：按分片序流式拼接取摘要＝交付字节摘要。

    口径：对「分片文件按序拼接的原始字节」取摘要——下载端点流式返回的正是这些
    字节，故客户端对下载内容自算的 sha256 必与本值一致（防抵赖/防传输损坏）。
    """
    digest = hashlib.sha256()
    for path in part_files(results_dir, job_id):
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()

def cleanup_parts(results_dir: str | Path, job_id: str) -> int:
    """清理单任务全部分片产物（AC-6：取消即清；返回删除的分片文件数）。

    只删 part-*.jsonl 分片文件后移走任务目录：整目录 rmtree 会越权删掉
    M15-3 之后落在同目录的交付物（结果文件/manifest），不做。
    """
    job_dir = rows_dir(results_dir, job_id)
    removed = 0
    if job_dir.is_dir():
        for path in sorted(job_dir.glob("part-*.jsonl")):
            path.unlink()
            removed += 1
        try:
            job_dir.rmdir()          # 空目录才移得动（有交付物则保留）
        except OSError:
            pass
    return removed


async def finalize_cancel(store: Any, results_dir: str | Path,
                          job_id: str, *,
                          audit: Any = None) -> dict:
    """取消收尾（M15-4 契约）：cancelling→cancelled＋清理全部分片产物。

    取消端点只标停止意向（running→cancelling，batch.JobStore.cancel）；
    本函数在 worker 分片边界被调用，把「停」落成终态并清产物
    （AC-6「清理分片产物」的执行侧落点）。audit≠None 时终态迁移处落一条
    任务级终态审计（AC-8 后半，M15-5）。非 cancelling 重入＝直接读回
    不迁移（幂等：竞态下收尾已发生过）。
    """
    jobs = batch.JobStore(store)
    job = await jobs.get(job_id)
    if job is None:
        raise batch.BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
    if job["status"] == "cancelling":
        await jobs.dequeue_stale(job_id)   # 清队列残留（防御；running 取消不该在队）
        await jobs.finish(job_id, "cancelled", audit=audit)
        cleanup_parts(results_dir, job_id)
        job = await jobs.get(job_id)
    return job





# ---------- scope=snapshot 隐式名单物化（M15-6，AC-12） ----------

async def scan_version_keys(store: Any, kv: dict, version: str) -> list[str]:
    """SCAN 遍历线上版本全键空间（禁 KEYS，同 sync/common.scan_keys 口径），
    返回排序后的键名单（确定性：分片序/名单 sha256 可复算）。"""
    prefix = kv["data_key"].format(version=version, sm3="")
    keys: list[str] = []
    async for data_key_str in store.scan_iter(match=prefix + "*"):
        keys.append(data_key_str[len(prefix):])
    return sorted(keys)


async def _materialize_snapshot(store: Any, jobs: batch.JobStore, job_id: str,
                                kv: dict, version: str, *,
                                shard_size: int,
                                audit: Any = None) -> tuple[list[str], int] | str:
    """快照隐式名单物化＋行数对账（AC-12，reconcile 查一同族·全等硬判）：

    版本元信息 row_count（loader 写 rows 字段、批链惯例 row_count——两口径都认）
    ↔ SCAN 全键集行数逐一对账：不等＝版本空间与元信息错账，任务置 failed
    （机读码）零导出，不交付脏账。通过后名单按 shard_size 分批 RPUSH 入库
    （既有分片机制的名单位），total＋名单 sha256 回填任务记录——此后与显式
    名单任务同机制执行（分片/检查点/崩溃续跑/交付/取消）。

    崩溃安全序：先清残留名单→分批写入→全量完成后才回填 total（total=0 即
    「名单未权威」，续跑据此整体重物化，幂等可重入）。失败路径返回机读码。
    对账失败的终态经 audit 参数落任务级终态审计（M15-5×M15-6：快照终态
    审计与名单任务同口径，一条不漏）。
    """
    meta_key = kv["version_meta_key"].format(version=version)
    raw_count = await store.hget(meta_key, "row_count")
    if raw_count is None:
        raw_count = await store.hget(meta_key, "rows")
    if raw_count is None:
        await jobs.finish(job_id, "failed", error=ERR_ROWCOUNT_MISSING,
                          audit=audit)
        return ERR_ROWCOUNT_MISSING
    row_count = int(raw_count)
    keys = await scan_version_keys(store, kv, version)
    stats = {"row_count_expected": row_count, "keys_scanned": len(keys)}
    if row_count == 0:      # 空版本（loader 不产 0 行版本；键空而 row_count>0 走 mismatch）
        await jobs.finish(job_id, "failed", error=ERR_SNAPSHOT_EMPTY, stats=stats,
                          audit=audit)
        return ERR_SNAPSHOT_EMPTY
    if len(keys) != row_count:
        await jobs.finish(job_id, "failed", error=ERR_ROWCOUNT_MISMATCH,
                           stats=stats, audit=audit)
        return ERR_ROWCOUNT_MISMATCH
    keys_list = batch.JOB_KEY_PREFIX + job_id + batch.JOB_KEYS_SUFFIX
    await store.delete(keys_list)                  # 清物化中途崩溃的残留
    for i in range(0, len(keys), shard_size):      # 分批入库（防巨名单单命令阻塞）
        await store.rpush(keys_list, *keys[i:i + shard_size])
    await jobs.set_snapshot_manifest(job_id, total=len(keys),
                                     keys_sha256=batch.keys_digest(keys))
    return keys, len(keys)

# ---------- 键级读路径（pipeline 批读＋单键重试≤2，AC-7） ----------

async def _get_with_retry(store: Any, key: str) -> tuple[str | None, str | None]:
    """单键 GET＋键级重试≤KEY_RETRY_LIMIT（GET 幂等）；耗尽返回 (None, 错误码)。"""
    for attempt in range(KEY_RETRY_LIMIT + 1):
        try:
            return await store.get(key), None
        except Exception:  # noqa: BLE001 —— 单键读失败进重试，不外抛
            if attempt == KEY_RETRY_LIMIT:
                return None, ERR_KV_READ
    return None, ERR_KV_READ  # 不可达（保险）


async def _decode_tags(store: Any, key: str,
                       raw: str) -> tuple[dict | None, str | None]:
    """解析存储值（loader 紧凑 JSON）；坏值重读≤2 再试，仍坏返回行错误码。"""
    for attempt in range(KEY_RETRY_LIMIT + 1):
        try:
            return json.loads(raw), None
        except ValueError:
            if attempt == KEY_RETRY_LIMIT:
                return None, ERR_DECODE
            raw, err = await _get_with_retry(store, key)   # 重读一次再解析
            if raw is None:
                return None, err or ERR_KV_READ
    return None, ERR_DECODE  # 不可达（保险）


async def _read_shard_rows(store: Any, kv: dict, version: str, keys: list[str],
                           whitelist: set[str], desensitize: dict,
                           data_as_of: str) -> tuple[list[dict], int, int, int]:
    """读一个分片并组行：pipeline MGET 一次批量读（防阻塞），失败退逐键 GET。

    返回 (行列表, 命中数, kv_read_failed 数, row_decode_failed 数)。
    行 schema 同 AC-5：key/found/fields/data_as_of/error（fields 与点查同口径：
    白名单∩非空标签→脱敏；空投影→null；未命中→null，不伪造）。
    """
    data_keys = [kv["data_key"].format(version=version, sm3=k) for k in keys]
    raws: list[str | None] | None
    try:
        raws = await store.mget(*data_keys)
    except Exception:  # noqa: BLE001 —— 分片 pipeline 失败退逐键（键级重试兜底）
        logger.warning("分片 pipeline 读失败（%d 键），退逐键 GET＋键级重试", len(keys))
        raws = None
    rows: list[dict] = []
    found = read_fail = decode_fail = 0
    for i, (key, dkey) in enumerate(zip(keys, data_keys)):
        error: str | None = None
        tags: dict | None = None
        if raws is not None:
            raw = raws[i]
        else:
            raw, error = await _get_with_retry(store, dkey)
        if raw is not None:
            tags, error = await _decode_tags(store, dkey, raw)
        if error is not None:
            row = {"key": key, "found": False, "fields": None,
                   "data_as_of": data_as_of, "error": error}
            if error == ERR_KV_READ:
                read_fail += 1
            else:
                decode_fail += 1
        elif tags is None:                            # 未命中（raw=None）：如实 false
            row = {"key": key, "found": False, "fields": None,
                   "data_as_of": data_as_of, "error": None}
        else:                                         # 命中：投影→脱敏（AC-9 红线）
            fields = kvx.apply_desensitize(kvx.project(tags, whitelist),
                                           desensitize)
            row = {"key": key, "found": True, "fields": fields or None,
                   "data_as_of": data_as_of, "error": None}
            found += 1
        rows.append(row)
    return rows, found, read_fail, decode_fail


# ---------- 单任务流水线（幂等可重入：queued 全新执行；running 崩溃续跑） ----------

async def process_job(store: Any, services: dict, matrix: list[dict], job_id: str,
                      *, results_dir: str | Path,
                      shard_size: int = DEFAULT_SHARD_SIZE,
                      audit: Any = None) -> dict:
    """执行/续跑一个任务到终态；返回最终任务记录。

    audit≠None 时所有终态迁移处各落一条任务级终态审计（AC-8 后半，M15-5；
    状态机保证终态只迁一次＝每任务恰好一条）。
    入口状态决定路径：queued＝全新（授权复核→钉版本→start）；
    running＝崩溃续跑（续用任务记录里钉住的版本，不重读 alias——AC-11 跨重启）；
    cancelling＝端点已标停止意向，入口即收尾（finalize_cancel：清产物＋终态）；
    终态＝直接返回（幂等重入，如排队期被取消的滞留队项）。
    """
    jobs = batch.JobStore(store)
    job = await jobs.get(job_id)
    if job is None:
        raise batch.BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
    if job["status"] in batch.TERMINAL_STATES:
        return job                                    # 已终态：跳过不迁移
    if job["status"] == "cancelling":
        # 端点已标停止意向（M15-4）：入口即收尾，不续跑（AC-6 边界语义）
        return await finalize_cancel(store, results_dir, job_id, audit=audit)
    resume = job["status"] == "running"

    service = services[batch.BATCH_SOURCE_SERVICE]
    kv = service["backend"]["kv"]
    if resume:
        # A4 崩溃续跑：续用任务记录里钉住的版本（不重读 alias——AC-11 跨重启）
        version = job.get("live_version") or ""
        data_as_of = job.get("data_as_of") or ""
        if not version:                               # 未钉版本的 running 残留
            return await jobs.finish(job_id, "failed", error=ERR_INCONSISTENT,
                                      audit=audit)
    else:
        # 钉版本（AC-11）：启动读一次指针＋版本元信息，此后全程不再读 alias。
        # 状态机纪律（M15-1 契约）：failed 只能从 running 迁出——先 start 再判致命错，
        # 无指针时以空钉（视图＝None，不伪造）进 running 后置 failed（A4：不读脏）。
        version = await store.get(kv["alias_key"])
        data_as_of = ""
        if version is not None:
            data_as_of = await store.hget(
                kv["version_meta_key"].format(version=version), "data_as_of") or ""
        await jobs.start(job_id, live_version=version or "",
                         data_as_of=data_as_of)
        if version is None:
            return await jobs.finish(job_id, "failed", error=ERR_NOT_READY,
                                      audit=audit)
    # 执行时点授权复核（fail-closed：提交后矩阵撤销/缩权→任务失败不执行）
    try:
        grant = kvx.tenant_grant(matrix, batch.BATCH_SOURCE_SERVICE,
                                 tenant=job["tenant"])
    except kvx.TagQueryError as exc:
        return await jobs.finish(job_id, "failed", error=exc.code, audit=audit)
    # 执行时点冻结复核（P1-⑮，管理功能群一期 §1.6 计量面：冻结即新增调用拒）——
    # 提交后冻结的任务在此拦下置 failed（可重提），不靠调用面网关拒绝兜底
    if _tenant_lifecycle(matrix, job["tenant"]) != "active":
        return await jobs.finish(job_id, "failed", error=ERR_TENANT_FROZEN, audit=audit)
    # 白名单∩请求（AC-9：白名单外字段名零出现；矩阵缩权后在执行端再截一次）
    whitelist = set(json.loads(job.get("fields") or "[]")) & grant["fields"]
    desensitize = service["response"].get("desensitize")

    # scope=snapshot（AC-12，M15-6）：名单隐式＝钉住版本全键集，首次执行/名单
    # 未权威（total=0，含物化中途崩溃）时 SCAN 物化＋row_count 对账；此后（含
    # 崩溃续跑）与显式名单任务同机制——物化名单即「名单位」。
    if (job.get("scope") or batch.SCOPE_LIST) == batch.SCOPE_SNAPSHOT \
            and (not resume or int(job.get("total") or 0) == 0):
        outcome = await _materialize_snapshot(store, jobs, job_id, kv, version,
                                              shard_size=shard_size, audit=audit)
        if isinstance(outcome, str):                  # 机读错误码：已置 failed
            return await jobs.get(job_id)
        keys, total = outcome
        # 快照键量计入日配额（M15-5×M15-6 咬合：提交时无名单可数→物化 total 后
        # 计量；单任务上限已在提交端跳过。重试重扫＝再计量，与名单任务重提同
        # 口径；崩溃续跑不重物化（total≠0）故不重复计量）。
        dkey = batch.daily_quota_key(job["tenant"], time.time())
        await store.incrby(dkey, total)
        await store.expire(dkey, batch.DAILY_QUOTA_TTL_S)
    else:
        keys = await jobs.get_keys(job_id)
        total = len(keys)
    processed = int(job.get("processed") or 0)        # 检查点（崩溃续跑起点）
    found = int(job.get("found") or 0)
    read_fail = decode_fail = 0
    job_dir = rows_dir(results_dir, job_id)
    if resume:
        # 检查点对齐守卫：processed 只在整分片边界落盘 ⇒ 对 shard_size 取余必为 0
        # （除已全部完成）；不对齐＝分片大小被改过（部署常量纪律被破坏）→明确置失败
        if processed < total and processed % shard_size:
            return await jobs.finish(job_id, "failed", error=ERR_INCONSISTENT,
                                      audit=audit)
        start_shard = _prepare_resume(job_dir, processed, shard_size)
    else:
        start_shard = _prepare_fresh(job_dir)

    for shard_index in range(start_shard, (total + shard_size - 1) // shard_size):
        # AC-6 执行侧＋收尾语义（M15-4）：分片边界查状态——
        #   cancelling＝端点已标停止意向：即收尾（cancelling→cancelled＋清产物），
        #   不再读下一个分片；其余非 running（排队期取消的 cancelled 等）即停不迁移。
        current = await jobs.get(job_id)
        if current["status"] == "cancelling":
            logger.info("任务 %s 分片边界收尾取消（状态=cancelling）", job_id)
            return await finalize_cancel(store, results_dir, job_id, audit=audit)
        if current["status"] != "running":
            logger.info("任务 %s 在分片边界停（状态=%s，不再迁移）",
                        job_id, current["status"])
            return current
        shard = keys[shard_index * shard_size:(shard_index + 1) * shard_size]
        rows, hit, rf, df = await _read_shard_rows(
            store, kv, version, shard, whitelist, desensitize, data_as_of)
        _write_rows(part_path(results_dir, job_id, shard_index), rows)
        processed += len(rows)
        found += hit
        read_fail += rf
        decode_fail += df
        try:
            await jobs.update_progress(job_id, processed, found)
        except batch.BatchError as exc:
            if exc.code == "illegal_transition":
                # 分片落盘后任务已被迁走状态：cancelling＝端点取消→收尾
                # （清产物＋终态，M15-4）；其余（cancelled 直写等）保留产物即停。
                if (await jobs.get(job_id))["status"] == "cancelling":
                    return await finalize_cancel(store, results_dir, job_id, audit=audit)
                return await jobs.get(job_id)
            raise

    missed = processed - found - read_fail - decode_fail
    stats = {"missed": missed, "row_errors": read_fail + decode_fail}
    if total > 0 and read_fail == total:
        # 全任务无一键可读 ⇒ Redis 不可用（A4：置 failed，凭同名单重提）
        return await jobs.finish(job_id, "failed", error=ERR_JOB_KV, stats=stats,
                                  audit=audit)
    # 成功即汇总结果文件 sha256（M15-3：落任务记录＝任务级审计双 sha256 的
    # 「结果 sha256」位，任务级审计条目正式归 M15-5；失败/取消不产结果摘要）
    stats["results_sha256"] = sha256_results(results_dir, job_id)
    return await jobs.finish(job_id, "succeeded", stats=stats, audit=audit)


# ---------- FIFO 消费循环（单 worker 串行；AC-3 结构强制） ----------

async def pop_next(store: Any) -> str | None:
    """队头出队（LPOP 原子：多 worker 并存也各取各的，不重复消费）。"""
    return await store.lpop(batch.QUEUE_KEY)


async def recover_running(store: Any, services: dict, matrix: list[dict], *,
                          results_dir: str | Path,
                          shard_size: int = DEFAULT_SHARD_SIZE,
                          audit: Any = None) -> list[str]:
    """启动恢复（A4 杀进程安全）：扫出 running 任务逐个续跑/置失败；返回 job_id 列表。"""
    resumed: list[str] = []
    jobs = batch.JobStore(store)
    async for key in store.scan_iter(match=batch.JOB_KEY_PREFIX + "*"):
        if key.endswith(batch.JOB_KEYS_SUFFIX):
            continue                                  # 键名单 list 不是任务 hash
        job_id = key[len(batch.JOB_KEY_PREFIX):]
        job = await jobs.get(job_id)
        if job and job.get("status") == "cancelling":
            # 取消意向残留（端点标记后 worker 崩溃，A4 边界）：收尾＝清产物＋终态
            logger.info("收尾 cancelling 任务 %s（取消端点已标记）", job_id)
            await finalize_cancel(store, results_dir, job_id, audit=audit)
            resumed.append(job_id)
        elif job and job.get("status") == "running":
            logger.info("恢复 running 任务 %s（检查点 processed=%d）",
                        job_id, job.get("processed", 0))
            await process_job(store, services, matrix, job_id,
                              results_dir=results_dir, shard_size=shard_size,
                              audit=audit)
            resumed.append(job_id)
    return resumed


async def drain(store: Any, services: dict, matrix: list[dict], *,
                results_dir: str | Path,
                shard_size: int = DEFAULT_SHARD_SIZE,
                audit: Any = None) -> list[str]:
    """一轮 FIFO 消费：循环出队执行到队列空；返回处理过的 job_id（按执行序）。

    注意：恢复（recover_running）是 worker 启动期动作，不在每轮 drain 里做——
    避免与在途任务竞争（单 worker 纪律下恢复只应发生在进程刚启动时）。"""
    done: list[str] = []
    while True:
        job_id = await pop_next(store)
        if job_id is None:
            return done
        try:
            await process_job(store, services, matrix, job_id,
                              results_dir=results_dir, shard_size=shard_size,
                              audit=audit)
            done.append(job_id)
        except batch.BatchError as exc:
            # 可控错误（如滞留队项的 job_not_found）：记日志继续消费，不卡队列
            logger.warning("任务 %s 跳过：%s %s", job_id, exc.code, exc.message)


async def run_forever(store: Any, services: dict, matrix: list[dict], *,
                      results_dir: str | Path,
                      shard_size: int = DEFAULT_SHARD_SIZE,
                      poll_interval: float = DEFAULT_POLL_INTERVAL,
                      audit: Any = None) -> None:
    """sidecar 主循环：启动先恢复遗留 running（A4）→drain 到空→休眠轮询；
    周期异常记日志后休眠重试（半途任务留在 running，重启由恢复逻辑续跑）。"""
    recovered = await recover_running(store, services, matrix,
                                     results_dir=results_dir, shard_size=shard_size,
                                     audit=audit)
    if recovered:
        logger.info("启动恢复 %d 个 running 任务：%s", len(recovered), recovered)
    while True:
        try:
            n = await drain(store, services, matrix, results_dir=results_dir,
                            shard_size=shard_size, audit=audit)
            if n == 0:
                await asyncio.sleep(poll_interval)
        except Exception:  # noqa: BLE001 —— 周期失败不退出进程（恢复逻辑接管）
            logger.exception("worker 周期失败（休眠后重试）")
            await asyncio.sleep(poll_interval)


# ---------- sidecar 入口（同镜像 python -m app.batch_worker；B3 并发=1） ----------

def main() -> int:
    """进程入口：读环境变量→连 Redis（延迟导入）→消费循环（Ctrl-C 退出）。

    任务级终态审计写入器（M15-5，AC-8 后半）：独立目录 BATCH_AUDIT_DIR（默认
    /data/audit/batch，与 service 同 named volume 不同子目录——WAL 单写者纪律）；
    启动先 recover 兜底半行，退出兜底 close（atexit 亦兜底，幂等）。
    """
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    results_dir = os.environ.get(RESULTS_DIR_ENV, DEFAULT_RESULTS_DIR)
    shard_size = int(os.environ.get(SHARD_SIZE_ENV, DEFAULT_SHARD_SIZE))
    poll_interval = float(os.environ.get(POLL_INTERVAL_ENV, DEFAULT_POLL_INTERVAL))
    audit_dir = os.environ.get(BATCH_AUDIT_DIR_ENV, DEFAULT_BATCH_AUDIT_DIR)
    import redis.asyncio as aioredis   # 延迟导入：纯逻辑层不依赖 redis（同 batch.py）
    from app.audit import AuditWriter  # 延迟导入：入口层才落 audit 依赖（同纪律）

    services = kvx.load_services()
    matrix = kvx.load_auth_matrix()
    store = aioredis.from_url(
        os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0"),
        decode_responses=True)
    audit = AuditWriter(audit_dir)
    recovered = audit.recover()
    logger.info("batch-worker 就绪：results_dir=%s shard_size=%d poll=%.1fs audit_dir=%s"
                "（任务级审计 WAL 恢复 %d 条）",
                results_dir, shard_size, poll_interval, audit_dir, recovered)
    try:
        asyncio.run(run_forever(store, services, matrix,
                                results_dir=results_dir, shard_size=shard_size,
                                poll_interval=poll_interval, audit=audit))
    except KeyboardInterrupt:
        logger.info("batch-worker 退出（KeyboardInterrupt）")
    finally:
        audit.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
