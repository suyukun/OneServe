# -*- coding: utf-8 -*-
"""审计查询端点（P1-1 · C 面首卡，SPEC §2-C1/§5-P1-1）——FastAPI 薄胶水。

端点（经门户 nginx /v1/portal/ 前缀反代到本服务，与既有门户只读端点同暴露级）：
  GET /v1/portal/audit/logs?ts_from=…&ts_to=…&caller=…&endpoint=…&status=…&trace_id=…
      &limit=20&offset=0
  多条件 AND（不给条件＝全量浏览）；返回 {items,total,limit,offset,summary}：
  items＝八元组行（caller 掩码）；summary＝按全部命中行聚合的统计摘要卡数据
  （窗口总量/成功率/慢查询 top——今日总量由页面默认「今日」时间窗给出）。

数据真源：审计 JSONL（app.state.audit 写入器的文件，audit.py 八元组含 trace_id 第八列）。
决策逻辑全在 app/my_access.py ④ audit_query（纯函数）；本模块只做 HTTP 编排。

只读面口径（C1）：本端点不提供写操作；网关即拒记录（无八元组、无调用方身份）不
在本端点合并——那是「我的接入·traceId 反查」的能力，页面脚注如实指引。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import errors
from app import my_access as mya

# 【v0.1.1 清理】W4 角色闸休眠挂点（矩阵域6「审计查看」operator 地板，原
# Depends(auth_oidc.require_role(...))）已随 auth_oidc.py 删除摘除——off 态恒
# 放行，行为零变化（无登录模式决策 §7 兑现）。
router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bad_request(error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, 400)


@router.get("/v1/portal/audit/logs")
async def audit_logs_ep(request: Request, ts_from: str = "", ts_to: str = "",
                        caller: str = "", endpoint: str = "", status: str = "",
                        trace_id: str = "", limit: int = mya.AUDIT_QUERY_DEFAULT_LIMIT,
                        offset: int = 0) -> JSONResponse:
    """C1 审计查询：审计 JSONL 多条件过滤＋分页＋统计摘要（只读）。

    输入错误（时间格式/状态条件非法）就近 400 人话；limit 1..100 clamp；
    offset >=0。条件全空＝全量浏览（合规面口径；caller 出参始终掩码）。
    """
    try:
        result = mya.audit_query(
            request.app.state.audit.jsonl,
            ts_from=ts_from, ts_to=ts_to, caller=caller, endpoint=endpoint,
            status=status, trace_id=trace_id, limit=limit, offset=offset)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    result["generatedAt"] = _now_iso()
    return JSONResponse(result)
