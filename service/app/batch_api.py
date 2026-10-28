# -*- coding: utf-8 -*-
"""批量查询任务 API 路由（TASK-M15-1；门槛稿 §B4 契约骨架的 FastAPI 落地）。

端点（M15-1 两枚＋M15-3 结果交付一枚；DELETE 取消=M15-4）：
  POST /v1/batch/jobs        提交键名单任务（AC-1/2；202 登记＋排队位次）
  GET  /v1/batch/jobs/{id}   任务状态＋进度（AC-4；状态机五态＋百分比＋data_as_of）
  GET  /v1/batch/jobs/{id}/results  结果交付（AC-5；无分页参数=下载完整 JSONL，
                             Content-Disposition＋X-Results-Sha256 头；携 cursor=/page=
                             /limit= 任意一个=分页查询，与文件同源同口径）

端点（M15-1 两枚＋M15-4 两枚；results 分页=M15-3）：
  POST   /v1/batch/jobs            提交键名单任务（AC-1/2；202 登记＋排队位次）
  GET    /v1/batch/jobs/{id}       任务状态＋进度（AC-4；状态机＋百分比＋data_as_of）
  DELETE /v1/batch/jobs/{id}       取消（AC-6，M15-4：queued 直接取消清队列残留；
                                   running 标停止意向→worker 分片边界收尾；
                                   succeeded/failed 409；cancelled/cancelling 幂等 200）
  POST   /v1/batch/jobs/{id}/retry 任务级重试（AC-7，M15-4：failed→queued 凭原名单
                                   重入队列并重置进度；非 failed 409）


胶水纪律：业务逻辑全部在 app.batch（可脱离 FastAPI 单测）；本模块只做
HTTP 解析、Pydantic 契约定型（B4 完成标志：可 import）与响应序列化。
鉴权与点查同通道：网关 key-auth 后透传 apikey 头→授权矩阵（B2）；
每次调用落审计八元组（经 app.state.audit 注入，SPEC §3.2 同构；trace_id
自 request.state 注入＝P0-5 注记的批量链路接线点，M15-5）。
配额响应（AC-10）：429/413 挂 X-RateLimit-Reason；429 另挂 Retry-After
（映射逻辑 batch.quota_headers，Stripe 式头口径集中一处）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from app import batch
from app import batch_delivery
from app import errors

router = APIRouter()


# ---------- Pydantic 契约（B4：job 状态机＋端点 schema 定型） ----------

class BatchSubmitRequest(BaseModel):
    """提交请求：keys=键名单（scope=list 必填）；scope=snapshot 不携带 keys
    （名单隐式=线上版本全键集，M15-6）；fields=请求投影字段（可选，⊆ 授权白名单）。"""
    keys: list[str] | None = None
    fields: list[str] | None = None
    scope: str | None = None


class BatchSubmitAccepted(BaseModel):
    """提交成功（202）：job_id＋排队位次＋名单 sha256（NC-5 名单侧）。
    snapshot：total=0/名单 sha256 空（执行时 SCAN 全键集后回填，M15-6）。"""
    job_id: str
    status: str
    queue_position: int
    scope: str
    total: int
    keys_sha256: str
    fields: list[str]
    tenant: str
    created_at: str


class BatchResultsRow(BaseModel):
    """结果行（AC-5 JSONL 行 schema 定型；fields=投影脱敏后键值，空投影/未命中=null）。"""
    key: str
    found: bool
    fields: dict[str, object] | None
    data_as_of: str
    error: str | None


class BatchResultsPage(BaseModel):
    """分页查询（200）：行数据＋next_cursor＋has_more（与结果文件同源同口径）。"""
    job_id: str
    status: str
    total: int
    rows: list[BatchResultsRow]
    next_cursor: str | None
    has_more: bool
    results_sha256: str | None


class BatchJobStatus(BaseModel):
    """状态查询（200）：状态机＋processed/total 百分比＋所钉版本 data_as_of。"""
    job_id: str
    tenant: str
    status: str
    scope: str
    total: int
    processed: int
    progress: int
    found: int
    keys_sha256: str
    fields: list[str]
    live_version: str | None
    data_as_of: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    queue_position: int | None = None


class BatchCancelAccepted(BaseModel):
    """取消受理（200）：queued 直取＝终态 cancelled；running＝停止意向 cancelling。"""
    job_id: str
    tenant: str
    status: str
    scope: str
    total: int
    processed: int
    progress: int
    found: int
    keys_sha256: str
    fields: list[str]
    live_version: str | None
    data_as_of: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    queue_position: int | None = None


class BatchRetryAccepted(BaseModel):
    """重试受理（202，AC-7 任务级）：同 job_id 凭原名单重入队列（failed→queued）。"""
    job_id: str
    status: str
    queue_position: int
    total: int
    keys_sha256: str
    tenant: str
    retried_from: str


@router.post("/v1/batch/jobs")
async def submit_batch_job(request: Request) -> JSONResponse:
    """提交批量任务（AC-1/2/10）：校验失败机读错误码且不产生任务；成功 202＋位次；
    超配额 429/413（并发/日键量/单任务）挂 Stripe 式限流头。"""
    caller = request.headers.get("apikey", "")
    trace_id = getattr(request.state, "trace_id", "")   # trace_middleware 注入（P0-5）
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 —— 非法 JSON
        payload = None
    # Pydantic 第一道形状闸（错误映射为本端点机读码；语义校验在 app.batch）
    if isinstance(payload, dict):
        try:
            BatchSubmitRequest.model_validate(payload)
        except ValidationError:
            body = {"error": errors.INVALID_REQUEST,
                    "message": "请求体须为 {keys: [..], fields: [..]?} 或 "
                               "{scope: 'snapshot', fields: [..]?}"}
            batch.audit_call(request.app.state.audit, caller or "unknown",
                         batch.AUDIT_SUBMIT_ENDPOINT,
                         '{"error": "invalid_request"}', 400, 0, body,
                         trace_id=trace_id)
            return JSONResponse(body, 400)
    body, status = await batch.submit_job(
        request.app.state.redis, request.app.state.services,
        request.app.state.auth_matrix, caller, payload,
        audit=request.app.state.audit, trace_id=trace_id)
    if status == 202:
        body = BatchSubmitAccepted.model_validate(body).model_dump()
    return JSONResponse(body, status_code=status,
                        headers=batch.quota_headers(body, status))


@router.get("/v1/batch/jobs/{job_id}")
async def get_batch_job(request: Request, job_id: str) -> JSONResponse:
    """查任务状态（AC-4）：queued/running/succeeded/failed/cancelled＋进度＋data_as_of。"""
    body, status = await batch.get_job(
        request.app.state.redis, job_id, caller=request.headers.get("apikey", ""),
        audit=request.app.state.audit,
        trace_id=getattr(request.state, "trace_id", ""))
    if status == 200:
        body = BatchJobStatus.model_validate(body).model_dump()
    return JSONResponse(body, status_code=status)

@router.get("/v1/batch/jobs/{job_id}/results")
async def get_batch_results(request: Request, job_id: str, cursor: str | None = None,
                            page: str | None = None,
                            limit: str | None = None) -> Response:
    """结果交付（AC-5）：下载完整结果文件 JSONL（含 sha256 头）或 cursor/page 分页。

    无分页参数＝下载（StreamingResponse：Content-Disposition 附件＋X-Results-Sha256
    ＋X-Results-Rows 头；流式输出不整读 GB 级文件）；携 cursor=/page=/limit= 任一＝
    分页查询（行数据＋next_cursor＋has_more，直接读分片文件，与下载字节同源同口径）。
    参数原样传 str，语义校验与机读错误码在 app.batch_delivery（胶水纪律）。
    """
    caller = request.headers.get("apikey", "")
    trace_id = getattr(request.state, "trace_id", "")   # trace_middleware 注入（P0-5）
    results_dir = batch_delivery.results_dir_from_env()
    if cursor is None and page is None and limit is None:
        payload, status = await batch_delivery.results_download(
            request.app.state.redis, job_id, results_dir=results_dir,
            caller=caller, audit=request.app.state.audit, trace_id=trace_id)
        if status != 200:
            return JSONResponse(payload, status_code=status)
        return StreamingResponse(
            batch_delivery.iter_result_chunks(payload["paths"]),
            media_type=payload["media_type"],
            headers={"Content-Disposition":
                         f'attachment; filename="{payload["filename"]}"',
                     "X-Results-Sha256": payload["sha256"],
                     "X-Results-Rows": str(payload["row_count"])})
    body, status = await batch_delivery.results_page(
        request.app.state.redis, job_id, results_dir=results_dir,
        cursor=cursor, page=page, limit=limit,
        caller=caller, audit=request.app.state.audit, trace_id=trace_id)
    if status == 200:
        body = BatchResultsPage.model_validate(body).model_dump()
    return JSONResponse(body, status_code=status)


@router.delete("/v1/batch/jobs/{job_id}")
async def cancel_batch_job(request: Request, job_id: str) -> JSONResponse:
    """取消任务（AC-6，M15-4）：见 batch.cancel_job 状态语义。"""
    body, status = await batch.cancel_job(
        request.app.state.redis, job_id,
        caller=request.headers.get("apikey", ""),
        audit=request.app.state.audit,
        trace_id=getattr(request.state, "trace_id", ""))
    if status == 200:
        body = BatchCancelAccepted.model_validate(body).model_dump()
    return JSONResponse(body, status_code=status)


@router.post("/v1/batch/jobs/{job_id}/retry")
async def retry_batch_job(request: Request, job_id: str) -> JSONResponse:
    """任务级重试（AC-7，M15-4）：failed→queued 凭原名单重入队列。"""
    body, status = await batch.retry_job(
        request.app.state.redis, job_id,
        caller=request.headers.get("apikey", ""),
        audit=request.app.state.audit,
        trace_id=getattr(request.state, "trace_id", ""))
    if status == 202:
        body = BatchRetryAccepted.model_validate(body).model_dump()

    return JSONResponse(body, status_code=status)
