# -*- coding: utf-8 -*-
"""授权登记全景端点（P1-8 · C 面 C2，SPEC §2-C2/§5-P1-8）——FastAPI 薄胶水。

端点（经门户 nginx /v1/portal/ 前缀反代到本服务，与既有门户只读端点同暴露级）：
  GET /v1/portal/grants/panorama                 租户×appkey×服务×字段全景＋OA 溯源列表
  GET /v1/portal/grants/field?field=<字段名>     反查「这个字段给了谁」
  GET /v1/portal/grants/oa?ticket=<OA 单号>      OA 单号 → 授权范围溯源

数据真源：service/auth_matrix.yaml（app.state.auth_matrix，lifespan 加载、热加载刷新）。
决策逻辑全在 app/auth_view.py（纯函数，只读）；本模块只做 HTTP 编排。

只读面口径（C2）：不提供任何写操作；授权登记的写侧归运营台与 auth_admin CLI。
appkey 一律掩码出参（完整 appkey 不出端点，掩码纪律同 my_access/admin_api）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import auth_view as av

router = APIRouter()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bad_request(error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, 400)


@router.get("/v1/portal/grants/panorama")
async def grants_panorama(request: Request) -> JSONResponse:
    """① 授权全景：登记全表＋筛选项＋汇总＋OA 单号索引（只读，appkey 掩码）。

    空矩阵＝全零空态如实返回（页面对应空态文案），不造演示行。
    """
    view = av.panorama(request.app.state.auth_matrix)
    view["generatedAt"] = _now_iso()
    return JSONResponse(view)


@router.get("/v1/portal/grants/field")
async def grants_field(request: Request, field: str = "") -> JSONResponse:
    """② 字段反查「这个字段给了谁」：字段名精确匹配（与字段字典页同源口径）。

    空 field＝400 人话（先选字段再反查）；无命中＝found=false 如实空态
    （字段未授权＝经任何接口零出现，不编造持有方）。
    """
    field = field.strip()
    if not field:
        return _bad_request("missing_field", "请提供要反查的字段名（field 参数，如 ac_active_sts_01）")
    view = av.field_lookup(request.app.state.auth_matrix, field)
    view["generatedAt"] = _now_iso()
    return JSONResponse(view)


@router.get("/v1/portal/grants/oa")
async def grants_oa(request: Request, ticket: str = "") -> JSONResponse:
    """③ OA 单号溯源：单号 → 它授权了哪些租户/服务/字段（tenant 级＋grant 级两登记位）。

    空 ticket＝400 人话；未命中＝found=false 如实空态（本登记里查无此单号）。
    """
    ticket = ticket.strip()
    if not ticket:
        return _bad_request("missing_ticket", "请提供要溯源的 OA 单号（ticket 参数，如 OA-2026-0001）")
    view = av.oa_trace(request.app.state.auth_matrix, ticket)
    view["generatedAt"] = _now_iso()
    return JSONResponse(view)
