# -*- coding: utf-8 -*-
"""我的接入端点（P0-7 · 面向接入方的自助视图）——FastAPI 薄胶水。

端点（经门户 nginx /v1/portal/ 前缀反代到本服务，与既有门户只读端点同暴露级）：
  GET /v1/portal/my/credentials?caller=<完整凭证>   我的凭证＋配额登记值（掩码口径）
  GET /v1/portal/my/quota?caller=<完整凭证>         我的配额四要素真值（P1-3：
      日限额＝登记值／已用＝当日审计真实调用聚合／剩余／重置＝东八区次日 00:00；
      附近 7 日逐日用量与 caliber 口径说明随行）
  GET /v1/portal/my/logs?caller=<完整凭证>          我的日志（审计聚合＋网关拒记合并）
      /v1/portal/my/logs?trace_id=<追踪号>          traceId 反查（含被网关拒绝的调用）

caller 参数＝凭证身份（D1=b 凭证兼身份，无登录）：即 sb_/ak_ 开头的完整 AppKey——
查询框由页面明示「按凭证查询」；不含管理令牌（只读自助视图，数据一行不出
完整凭证与 secret 摘要——掩码纪律与 admin 列表同源）。

数据真源：
  凭证 → service/registry/appkeys.yaml（读盘即时值，签发/禁用后立即可见）；
  日志 → 审计 JSONL（app.state.audit 写入器的文件，八元组含 trace_id）＋
         Redis 列表 portal:trace:gw-rejected（P0-5 网关旁路，最近 1000 条）。
决策逻辑全在 app/my_access.py（纯函数）；本模块只做 HTTP 编排。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import appkeys as appk
from app import errors
from app import my_access as mya

router = APIRouter()

# P0-5 网关拒记旁路列表（configs/apisix/init_routes.py 写入；LPUSH 头端最新）
GW_REJECTED_KEY = "portal:trace:gw-rejected"
GW_REJECTED_MAX = 1000


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bad_request(error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, 400)


@router.get("/v1/portal/my/credentials")
async def my_credentials_ep(request: Request, caller: str = "") -> JSONResponse:
    """① 我的凭证＋配额登记值：按凭证身份查权威源（精确匹配，0/1 条）。

    未查询到不是错误（found=false）——凭证不存在/输错由页面如实提示；
    配额四要素真值走 /v1/portal/my/quota（P1-3）。
    """
    caller = caller.strip()
    if not caller:
        return _bad_request(errors.MISSING_CALLER, "请提供要查询的凭证（caller 参数，以 sb_ 或 ak_ 开头）")
    doc = appk.load_appkeys(request.app.state.appkeys_path)   # 读盘即时值
    creds = mya.my_credentials(doc, caller)
    return JSONResponse({"found": bool(creds), "credentials": creds,
                         "expiringSoonDays": mya.EXPIRING_SOON_DAYS,
                         "rotationOverlapDays": appk.ROTATION_OVERLAP_DAYS,  # P1-4 倒计时文案口径
                         "generatedAt": _now_iso()})


@router.get("/v1/portal/my/quota")
async def my_quota_ep(request: Request, caller: str = "") -> JSONResponse:
    """② 我的配额四要素真值（P1-3）：日限额（登记）× 当日审计真实用量。

    口径随行在 caliber 字段（页面就地文案的唯一来源）：
      上限＝appkeys.yaml quota_daily 登记值（网关 limit-count 同取此值强制；P1-5）；
      已用＝当日（东八区）审计 JSONL 按 caller **精确**聚合的真实调用条数
            （成败与 429 都计入——429 也是一次真实调用，不因为它被拒就不算）；
      剩余＝上限−已用（下限 0）；重置＝东八区次日 00:00。
    附近 7 日逐日用量（同一遍扫描聚合）供页面迷你趋势。

    空 caller＝400 人话（同 credentials）；未知 caller＝found=false 如实空态
    （不回「0 已用」——查无此凭证 ≠ 今天没调用）。出参一律掩码。
    """
    caller = caller.strip()
    if not caller:
        return _bad_request(errors.MISSING_CALLER, "请提供要查询的凭证（caller 参数，以 sb_ 或 ak_ 开头）")
    doc = appk.load_appkeys(request.app.state.appkeys_path)   # 读盘即时值
    view = mya.my_quota(doc, caller, request.app.state.audit.jsonl)
    view["generatedAt"] = _now_iso()
    return JSONResponse(view)


@router.get("/v1/portal/my/logs")
async def my_logs_ep(request: Request, caller: str = "", trace_id: str = "",
                     limit: int = mya.DEFAULT_LOG_LIMIT) -> JSONResponse:
    """③ 我的日志：审计 JSONL（按 caller 过滤）＋网关拒记合并，ts 降序取前 limit。

    trace_id 给定时进入反查模式：两处真源都按 traceId 精确检索（网关拒记
    无调用方身份，反查是它唯一的自助定位途径）；caller/trace_id 至少给一个。
    """
    caller, trace_id = caller.strip(), trace_id.strip()
    if not caller and not trace_id:
        return _bad_request(errors.MISSING_QUERY, "请提供凭证（caller）或追踪号（trace_id）至少一项")
    limit = min(max(limit, 1), mya.MAX_LOG_LIMIT)

    audit_path = request.app.state.audit.jsonl
    service_items = mya.audit_tail(audit_path, caller=caller, trace_id=trace_id,
                                   limit=limit)

    # 网关拒记：反查模式扫全列表精确匹配；列表模式取头端最近 limit 条
    if trace_id:
        raw = await request.app.state.redis.lrange(GW_REJECTED_KEY, 0, GW_REJECTED_MAX - 1)
    else:
        raw = await request.app.state.redis.lrange(GW_REJECTED_KEY, 0, limit - 1)
    gw_items = mya.gw_rejected_view(raw, trace_id=trace_id, limit=limit)

    items = mya.merge_logs(service_items, gw_items, limit=limit)
    return JSONResponse({
        "caller": caller, "traceId": trace_id, "limit": limit,
        "items": items,
        "gwRejectedNote": (
            "网关拒绝记录不含调用方身份（请求未通过认证），不能按凭证过滤，"
            "按时间并入参考；可用错误响应头 X-Trace-Id 反查"),
        "generatedAt": _now_iso(),
    })
