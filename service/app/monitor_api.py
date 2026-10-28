# -*- coding: utf-8 -*-
"""异常与批量监控端点（P1-6 · SPEC §5-P1-6／§2-B7 B7 运营页）——FastAPI 薄胶水。

端点（与 /v1/admin/* 同族：门户 nginx /v1/admin/ 反代到本服务；
本卡只做可看，不做送达——无任何写操作）：
  GET /v1/admin/monitor/errors    异常列表＋按调用方/API 下钻＋错误样本（审计真源）
  GET /v1/admin/monitor/trace     按 trace_id 精确反查 trace 详情（跨服务/批量两审计）
  GET /v1/admin/monitor/batch     批量运行视角（队列深度/进度/五态/积压，真源 JobStore）

分层纪律：决策全在 app/monitor.py（纯逻辑，可脱离 FastAPI 单测）；本模块只做
HTTP 解析（条件透传/limit clamp 在纯逻辑内）、app.state 依赖取用与响应序列化。
安全：管理令牌闸三段决策史——2026-09-23 拆除→2026-10-09 恢复→2026-10-14 再拆除
（维护者 亲测两轮被卡后拍板：现在不需要管理令牌，未来统一做系统登录页）。当前
_admin_gate（自 admin_api 导入的同一挂点）恒放行，三只读端点不再校验 X-Admin-Token；
恢复路径见 ADR-0010。出参一律掩码（caller 掩码、参数/响应只出 sha256 摘要，明文
与完整凭证不出端点）。errors.UNAUTHORIZED 等错误码常量仍保留（错误码中心 91 个码
口径契约，不得删）。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import errors
from app import monitor as mon

from app.admin_api import _admin_gate   # 同一挂点（2026-10-14 起恒放行，见 ADR-0010；恢复闸期两端同族复用）


router = APIRouter(dependencies=[Depends(_admin_gate)])

# 2026-10-09：管理令牌闸恢复——三只读监控端点与 /v1/admin/* 同族过闸（见模块 docstring）。
# 网关数据面探活目标（compose 网内服务名；本机/异构部署可经环境变量覆盖）
GATEWAY_PROBE_HOST = os.environ.get("APISIX_PROBE_HOST", "oneserve-apisix")
GATEWAY_PROBE_PORT = int(os.environ.get("APISIX_PROBE_PORT", "9080"))
# 批量任务级审计（M15-5，batch-worker 独立目录）——trace 反查的第二来源
BATCH_AUDIT_DIR = Path(os.environ.get("BATCH_AUDIT_DIR", "/data/audit/batch"))


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _audit_sources(request: Request) -> list[tuple[str, Path]]:
    """trace 反查的两处审计真源：服务侧（app.state.audit）＋批量任务级（独立目录）。"""
    return [
        ("service", Path(request.app.state.audit.jsonl)),
        ("batch", BATCH_AUDIT_DIR / "audit.jsonl"),
    ]


@router.get("/v1/admin/monitor/errors")
async def monitor_errors(request: Request, ts_from: str = "", ts_to: str = "",
                         caller: str = "", endpoint: str = "", error_class: str = "",
                         status: str = "", trace_id: str = "",
                         limit: int = mon.ERROR_DEFAULT_LIMIT, offset: int = 0) -> JSONResponse:
    """异常列表＋下钻（只读）：审计 JSONL 筛 4xx/5xx，按调用方/API 聚合。

    空条件＝全量窗口（页面默认给「今日」窗）；无异常＝total=0＋空列表（页面如实
    「暂无异常」，不填假样本）。输入错误（时间格式/状态条件/异常类非法）就近 400 人话。
    """
    try:
        result = mon.error_query(request.app.state.audit.jsonl,
                                 ts_from=ts_from, ts_to=ts_to, caller=caller,
                                 endpoint=endpoint, error_class=error_class,
                                 status=status, trace_id=trace_id,
                                 limit=limit, offset=offset)
    except ValueError as exc:
        return JSONResponse({"error": errors.BAD_QUERY, "message": str(exc)}, 400)
    result["generatedAt"] = _now_iso()
    return JSONResponse(result)


@router.get("/v1/admin/monitor/trace")
async def monitor_trace(request: Request, trace_id: str = "") -> JSONResponse:
    """trace 详情（只读）：按 trace_id 精确反查审计八元组（服务侧＋批量任务级）。

    无 trace_id 或查无命中＝found=false（页面如实「未找到该追踪号」，不编造链路）。
    """
    result = mon.trace_lookup(_audit_sources(request), trace_id)
    result["generatedAt"] = _now_iso()
    return JSONResponse(result)


@router.get("/v1/admin/monitor/batch")
async def monitor_batch(request: Request) -> JSONResponse:
    """批量运行视角（只读）：队列深度/执行中/五态计数/积压/最近任务。

    真源＝Redis 任务结构（JobStore 口径，batch.py 单一事实来源）；Redis 读失败＝
    degraded 附错误摘要并零值降级（页面标「读取失败」，不伪造在线）。
    """
    result = await mon.batch_overview(request.app.state.redis)
    return JSONResponse(result)


@router.get("/v1/admin/monitor/containers")
async def monitor_containers(request: Request) -> JSONResponse:
    """常驻容器健康一屏（只读；蓝图 1a 批·监控最小集 2026-10-20）。

    九常驻容器＋可选 hbase 的聚合探活：探活原语单一来源＝app/healthz
    （TCP/PING/HTTP OIDC 发现端点，与各容器 compose healthcheck 同判据）；
    无网络探针的容器如实 off。Redis 读失败＝error（对齐 /healthz 编排口径）。
    """
    result = await mon.containers_view(request.app.state.redis)
    result["generatedAt"] = _now_iso()
    return JSONResponse(result)


async def components(request: Request) -> JSONResponse:
    """运维大盘四组件探活出参（/v1/portal/status 的 components 字段，portal_api 调用）。

    供门户运行视图状态带渲染——探活真做（Redis PING／网关 TCP／本进程存活／审计配置），
    探不到＝error，前端显示「未知」，不回退「正常」。
    """
    result = await mon.components_view(
        request.app.state.redis, request.app.state.services,
        version=str(request.app.version or ""),
        started_monotonic=getattr(request.app.state, "started_monotonic", 0.0),
        gateway_host=GATEWAY_PROBE_HOST, gateway_port=GATEWAY_PROBE_PORT)
    return JSONResponse({"components": result, "generatedAt": _now_iso()})
