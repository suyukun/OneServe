# -*- coding: utf-8 -*-
"""批量结果交付核心（TASK-M15-3，开工门槛-M1.5批量查询_v0 §B2/B4/B5，覆盖 AC-5）。

结果交付两形态，同一读源（分片 JSONL 文件，M15-2 worker 落盘）同一口径：
  - 下载：GET /v1/batch/jobs/{id}/results（无分页参数）→ 完整结果文件 JSONL
    （StreamingResponse，Content-Disposition 附件名＋X-Results-Sha256 头）；
  - 分页：同路径携 cursor= 或 page=（或 limit=）→ 行数据＋next_cursor＋has_more，
    直接读分片文件按序切片，不另造数据结构/索引（与文件严格同源同口径）。

语义（AC-5＋A4 边界，端点层机读码）：
  - 404 job_not_found      任务不存在；
  - 409 job_not_finished   queued/running——结果未生成，稍后重试（不读在途半成品）；
  - 409 job_not_succeeded  failed/cancelled——终态永不产结果（凭同名单重提）；
  - 410 results_gone       已成功但结果文件过期/被删（A4）；
  - 400 invalid_cursor/invalid_page/invalid_limit/invalid_request  分页参数非法。

cursor 口径：不透明续读令牌＝下一行全局序号（十进制字符串）。结果文件在任务
succeeded 后不可变（worker 只在 running 写、终态封口），故序号切片稳定且与
下载字节严格同源；翻页从分片文件头顺序扫到 offset（O(offset) 线性 IO，
M1.5 百万键级可接受——建索引即「另造数据结构」，本卡红线不做）。
page N（1 起）＝cursor (N-1)*limit 的人读便利别名，两者互斥。

完整性（NC-5 双 sha256 之结果侧）：worker 成功时汇总分片拼接字节的 sha256
落任务记录（batch_worker.sha256_results，本卡先落文件哈希位）；下载头与分页
体都带该值，客户端对下载内容自算摘要即可核验。任务级完成审计条目正式归 M15-5。

设计约束（同 batch.py 纪律）：本模块不 import FastAPI/redis——JobStore 经 store
参数注入（hgetall 即可），文件路径复用 batch_worker 的分片布局函数（同源纪律）；
每次调用落审计七元组（AC-8 预埋口径，同 M15-1：best-effort，不阻断业务）。

配额/参数（.env 可覆盖）：BATCH_RESULTS_DIR 分片产物根目录（与 worker 同名同值，
compose 内共 named volume）；分页页大小缺省 1000（对齐分片口径）、上限 10000。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from app import batch
from app import errors
from app import batch_worker as w

# 审计端点名（七元组 endpoint 字段口径：静态名；模式/游标入 params）
AUDIT_RESULTS_ENDPOINT = "service:/v1/batch/jobs/results"

# 结果文件媒体类型（JSONL 惯用类型）与下载附件名后缀
MEDIA_TYPE_NDJSON = "application/x-ndjson"

# 分页页大小（缺省对齐 sync 链 PIPE_BATCH/分片批大小口径；上限防大响应拖网关）
DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 10000

# 读盘块大小（下载流式返回；1MB 对齐 sha256_results 同款分块）
CHUNK_SIZE = 1 << 20


def results_dir_from_env(env: Mapping[str, str] = os.environ) -> str:
    """交付端点读的结果根目录（与 worker 同一环境变量名，compose 共 named volume）。"""
    return env.get(w.RESULTS_DIR_ENV, w.DEFAULT_RESULTS_DIR)


def iter_result_chunks(paths: list[Path] | list[str]) -> Iterator[bytes]:
    """按分片序流式产出结果字节（下载端点响应体；与 sha256 摘要字节严格一致）。"""
    for path in paths:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                yield chunk


def _iter_rows(paths: list[Path]) -> Iterator[dict]:
    """按分片序逐行产出（JSONL 行→dict；与 batch_worker.read_rows 同解析口径）。"""
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                yield json.loads(line)


async def _check_ready(store: Any, job_id: str,
                       results_dir: str | Path) -> tuple[dict, list[Path]]:
    """交付前置：任务存在→已成功→结果文件在。违者抛 BatchError（404/409/410）。"""
    jobs = batch.JobStore(store)
    job = await jobs.get(job_id)
    if job is None:
        raise batch.BatchError(404, errors.JOB_NOT_FOUND, f"任务 {job_id} 不存在")
    if job["status"] in ("queued", "running"):
        raise batch.BatchError(409, errors.JOB_NOT_FINISHED,
                               f"任务 {job['status']} 尚未完成，结果未生成（稍后重试）")
    if job["status"] != "succeeded":
        raise batch.BatchError(409, errors.JOB_NOT_SUCCEEDED,
                               f"任务终态 {job['status']}，不产出结果文件（可凭同名单重提）")
    paths = w.part_files(results_dir, job_id)
    if not paths:
        raise batch.BatchError(410, errors.RESULTS_GONE,
                               "结果文件不存在或已被清理（留存期口径见部署配置）")
    return job, paths


def _parse_pagination(cursor: str | None, page: str | None,
                      limit: str | None) -> tuple[int, int]:
    """解析分页参数→（offset, limit）；非法即 400（机读码逐参数区分）。"""
    if cursor is not None and page is not None:
        raise batch.BatchError(400, errors.INVALID_REQUEST,
                               "cursor 与 page 不可同时提供（二选一）")
    try:
        size = DEFAULT_PAGE_SIZE if limit is None else int(limit)
    except ValueError:
        raise batch.BatchError(
            400, errors.INVALID_LIMIT, f"limit 须为整数（1~{MAX_PAGE_SIZE}）") from None
    if not 1 <= size <= MAX_PAGE_SIZE:
        raise batch.BatchError(
            400, errors.INVALID_LIMIT, f"limit 须在 1~{MAX_PAGE_SIZE}（收到 {limit}）")
    if cursor is not None:
        try:
            offset = int(cursor)
        except ValueError:
            raise batch.BatchError(
                400, errors.INVALID_CURSOR, "cursor 须为上次响应返回的 next_cursor 值") from None
        if offset < 0:
            raise batch.BatchError(400, errors.INVALID_CURSOR, "cursor 不可为负")
    elif page is not None:
        try:
            no = int(page)
        except ValueError:
            raise batch.BatchError(400, errors.INVALID_PAGE, "page 须为 ≥1 的整数") from None
        if no < 1:
            raise batch.BatchError(400, errors.INVALID_PAGE, "page 须为 ≥1 的整数")
        offset = (no - 1) * size
    else:
        offset = 0
    return offset, size


def _results_sha(job: dict, results_dir: str | Path, job_id: str) -> str:
    """结果 sha256：任务记录值为权威；历史/异常缺失时按同口径现算兜底。"""
    return job.get("results_sha256") or w.sha256_results(results_dir, job_id)


async def results_download(store: Any, job_id: str, *, results_dir: str | Path,
                           caller: str = "unknown",
                           audit: Any = None,
                           trace_id: str = "") -> tuple[dict, int]:
    """下载模式核心（AC-5）：校验→定位分片；200 返回交付载荷（胶水层流式输出）。

    载荷字段：paths（分片序文件列表，iter_result_chunks 流式拼出响应体）、
    filename/media_type（Content-Disposition 用）、sha256（响应头 X-Results-Sha256）、
    row_count（＝任务 total，行数=名单键数对账口径，不为此重扫文件）。
    """
    started = time.monotonic()
    try:
        job, paths = await _check_ready(store, job_id, results_dir)
        payload = {"job_id": job_id,
                   "sha256": _results_sha(job, results_dir, job_id),
                   "row_count": int(job.get("total") or 0),
                   "filename": f"{job_id}.jsonl",
                   "media_type": MEDIA_TYPE_NDJSON,
                   "paths": [str(p) for p in paths]}
        status, body = 200, {"job_id": job_id, "rows": payload["row_count"],
                             "sha256": payload["sha256"]}   # 审计只落摘要
    except batch.BatchError as exc:
        payload, status, body = {"error": exc.code, "message": exc.message}, exc.status, None
    batch.audit_call(audit, caller or "unknown", AUDIT_RESULTS_ENDPOINT,
                     json.dumps({"job_id": job_id, "mode": "download"}), status,
                     int((time.monotonic() - started) * 1000),
                     body or payload,
                     trace_id=trace_id)
    return payload, status


async def results_page(store: Any, job_id: str, *, results_dir: str | Path,
                       cursor: str | None = None, page: str | None = None,
                       limit: str | None = None, caller: str = "unknown",
                       audit: Any = None,
                       trace_id: str = "") -> tuple[dict, int]:
    """分页模式核心（AC-5）：行数据＋next_cursor＋has_more（与文件同源同口径）。

    多读一行判定 has_more（不依赖任务 total——文件行数才是交付权威口径）；
    offset 越过文件尾＝空页 has_more=false（宽容口径，客户端自行收手）。
    """
    started = time.monotonic()
    try:
        offset, size = _parse_pagination(cursor, page, limit)
        job, paths = await _check_ready(store, job_id, results_dir)
        rows: list[dict] = []
        for i, row in enumerate(_iter_rows(paths)):
            if i < offset:
                continue
            rows.append(row)
            if len(rows) > size:                    # 多读一行＝has_more 证据
                break
        has_more = len(rows) > size
        body = {"job_id": job_id, "status": "succeeded",
                "total": int(job.get("total") or 0), "rows": rows[:size],
                "next_cursor": str(offset + size) if has_more else None,
                "has_more": has_more,
                "results_sha256": _results_sha(job, results_dir, job_id)}
        status = 200
    except batch.BatchError as exc:
        body, status = {"error": exc.code, "message": exc.message}, exc.status
    batch.audit_call(audit, caller or "unknown", AUDIT_RESULTS_ENDPOINT,
                     json.dumps({"job_id": job_id, "mode": "page",
                                 "cursor": cursor, "page": page, "limit": limit}),
                     status, int((time.monotonic() - started) * 1000), body,
                     trace_id=trace_id)
    return body, status
