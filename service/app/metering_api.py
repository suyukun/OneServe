# -*- coding: utf-8 -*-
"""计量/用量报表端点（P1 首批 ＋ 1b-9 报表专区一期）——FastAPI 薄胶水。

  GET /v1/admin/metering/summary?days=7      days＝1（今日）/7（近 7 天），默认 7
  GET /v1/admin/usage/overview?days=         报表专区·用量总览（1b-9，viewer 地板）
  GET /v1/admin/usage/subjects?days=&tenant=&registered=&service=&q=&sort=&order=&page=&page_size=
  GET /v1/admin/usage/services?days=&tenant=&subject=&q=&sort=&order=&page=&page_size=
  GET /v1/admin/usage/trend?days=&dim=caller|service

数据真源：审计 JSONL（app.state.audit 写入器文件）只读聚合——决策逻辑全在
app/metering.py（纯函数）；本模块只做 HTTP 编排（分层同 audit_api.py）。
只读面：不提供写操作；完整 appkey 不出端点（掩码口径见 metering.py）。
挂载说明：admin_api.py 有并行卡在改，本端点独立成文件、main.py 挂载，
路由域仍属 /v1/admin/*（运营面，经门户 nginx 反代，与 appkeys/usage 同暴露级）。

1b-9 报表专区四端点（设计稿 docs/design/报表专区一期设计_v1.md §3/§4）：
聚合/筛选/排序/分页逻辑全在 app/usage_report.py（纯函数，同分层纪律）；
**全部读 Redis 快照侧＋join 凭证登记运行态，现算禁令（ADR-0023 §5）**——
快照未响应＝降级态响应（status=degraded），绝不回落 JSONL 现算（旧
metering/summary 的降级路径为迁移期保留，收编时点随管道卡）。
三态：status ∈ ok｜empty｜degraded（空态文案见 usage_report.MSG_EMPTY；
加载态为前端本地态）。参数校验非法一律 400 bad_query（同 days 口径）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import errors
from app import metering as mt
from app import usage_report as ur

# 【v0.1.1 清理】W4 角色闸休眠挂点（矩阵域7「计量查看」viewer 地板，原
# Depends(auth_oidc.require_role(...))）已随 auth_oidc.py 删除摘除——off 态恒
# 放行，行为零变化（无登录模式决策 §7 兑现）。
router = APIRouter()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _bad_request(error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, 400)


@router.get("/v1/admin/metering/summary")
async def metering_summary_ep(request: Request, days: int = 7) -> JSONResponse:
    """按调用主体×服务×日的计量聚合（只读）：窗口总量＋逐凭证行＋日趋势＋口径。

    D20 落地：优先读 Redis 统计快照（metering_from_stats），消费者未部署时
    降级回 JSONL 倒序聚合（metering_scan）。days 非法（非 1..7）→ 400。
    迁移期保留（设计稿 §5：my.html 在读）；svc 维度补丁（ADR-0023 第一实施项）
    后本端点按服务视图（callers[].services）返回真实服务维度。
    """
    try:
        # ① 优先读 Redis 统计快照（D20：永不解析原始日志）
        scan = await mt.metering_from_stats(request.app.state.redis, days=days)
        if not scan["callers"]:
            # 统计键空→降级 JSONL 倒序聚合（消费者未部署/首日无数据）
            scan = mt.metering_scan(request.app.state.audit.jsonl, days=days)
        result = mt.metering_view({"keys": request.app.state.appkeys}, scan)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    result["generatedAt"] = _now_iso()
    return JSONResponse(result)


# ── 报表专区一期（1b-9 · /v1/admin/usage/*，viewer 地板；设计稿 §3/§4） ──

def _service_names(request: Request) -> dict:
    """审计 endpoint 值 → 登记服务名 索引（app.state.services 运行态只读）。

    A 型点查端点审计为 service:/v1/query/{service_id}；专用端点/代理为
    service:{route}。两形态都建索引；无登记名的服务出 None（前端以等宽码
    显路由原值＝技术标识豁免，设计稿 §6）。
    """
    idx: dict[str, str] = {}
    for sid, entry in (getattr(request.app.state, "services", None) or {}).items():
        name = str((entry or {}).get("name") or "")
        if not name:
            continue
        route = str((entry or {}).get("route") or "")
        if route:
            idx[f"service:{route}"] = name
        idx[f"service:/v1/query/{sid}"] = name
    return idx


async def _snapshot(request: Request, days: int) -> tuple[dict, str]:
    """读快照并判三态：ok（有数据）/empty（窗口内无调用）/degraded（快照未响应）。

    degraded 不回落 JSONL 现算（现算禁令）——返回空聚合＋degraded 态，
    端点出降级包络；旧 metering/summary 的 JSONL 降级路径此处不沿用。
    """
    try:
        scan = await mt.metering_from_stats(request.app.state.redis, days=days,
                                            raise_on_error=True)
    except Exception:
        return {"days": mt._window_days(None, days), "callers": {}}, "degraded"
    return scan, ("ok" if scan["callers"] else "empty")


def _envelope(scan: dict, status: str, items: list, *, total: int,
              page: int, page_size: int, **extra) -> dict:
    """统一响应包络：三态＋分页元数据＋口径随行（页面文案唯一来源）。"""
    env = {"status": status,
           "message": None if status == "ok" else
           (ur.MSG_DEGRADED if status == "degraded" else ur.MSG_EMPTY),
           "days": len(scan["days"]), "dayKeys": scan["days"],
           "total": total, "page": page, "pageSize": page_size, "items": items,
           "serviceDim": ur.has_service_dim(scan) if status == "ok" else None,
           "generatedAt": _now_iso(), "caliber": mt.METERING_CALIBER}
    env.update(extra)
    return env


@router.get("/v1/admin/usage/overview")
async def usage_overview_ep(request: Request, days: int = 7,
                            page: int = 1, page_size: int = None) -> JSONResponse:
    """用量总览（设计稿 §3.1）：指标卡四卡（窗口调用量/成功率/活跃调用方数/
    热门服务）＋近 7 日逐日柱线＋Top 调用方榜（服务端分页，默认调用量降序）。"""
    try:
        days = ur.parse_days(days)
        page, page_size = ur.parse_paging(page, page_size)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    scan, status = await _snapshot(request, days)
    doc = {"keys": request.app.state.appkeys}
    totals = ur.window_totals(scan)
    subjects = ur.subject_rows(scan, doc)
    total, items = ur.paginate(subjects, page, page_size)
    top_services = ur.sort_rows(ur.service_rows(scan, doc, _service_names(request)),
                                "calls", "desc")[:5]
    registered_n = sum(1 for s in subjects if s["registered"])
    return JSONResponse(_envelope(
        scan, status, items, total=total, page=page, page_size=page_size,
        totals=totals,
        activeCallers={"count": len(subjects),
                       "unregistered": len(subjects) - registered_n},
        topServices=top_services,
        daily=ur.daily_totals(scan)))


@router.get("/v1/admin/usage/subjects")
async def usage_subjects_ep(request: Request, days: int = 7, tenant: str = "",
                            registered: str = "", service: str = "", q: str = "",
                            sort: str = None, order: str = None,
                            page: int = 1, page_size: int = None) -> JSONResponse:
    """主体用量（设计稿 §3.2）：逐凭证行＋服务明细展开（svc 维度）；
    行字段沿 metering_view 既有名（callerMasked/appName/tenant/registered/…，
    迁移零改名）；q 前缀搜索出口仍掩码（完整凭证不出端点）。"""
    try:
        days = ur.parse_days(days)
        page, page_size = ur.parse_paging(page, page_size)
        sort, order = ur.parse_sort(sort, order)
        registered = ur.parse_registered(registered)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    scan, status = await _snapshot(request, days)
    doc = {"keys": request.app.state.appkeys}
    rows = ur.subject_rows(scan, doc)
    if service:
        # 服务筛选（服务页下钻预填）：行服务明细含该服务（路由原值精确匹配）
        rows = [r for r in rows if any(s["endpoint"] == service for s in r["services"])]
    rows = ur.q_filter(rows, scan, doc, q or "")
    rows = ur.apply_subject_filters(rows, tenant=tenant or "", registered=registered)
    rows = ur.sort_rows(rows, sort, order)
    total, items = ur.paginate(rows, page, page_size)
    return JSONResponse(_envelope(scan, status, items, total=total, page=page,
                                  page_size=page_size,
                                  tenants=ur.tenant_options(doc, scan)))


@router.get("/v1/admin/usage/services")
async def usage_services_ep(request: Request, days: int = 7, tenant: str = "",
                            subject: str = "", q: str = "",
                            sort: str = None, order: str = None,
                            page: int = 1, page_size: int = None) -> JSONResponse:
    """服务用量（设计稿 §3.3）：svc 维度按服务聚合行（调用量/成败/均值/调用方数）；
    行展开＝该服务 Top 调用方（前端拉 subjects?service=<路由原值> 预填）。"""
    try:
        days = ur.parse_days(days)
        page, page_size = ur.parse_paging(page, page_size)
        sort, order = ur.parse_sort(sort, order)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    scan, status = await _snapshot(request, days)
    doc = {"keys": request.app.state.appkeys}
    rows = ur.service_rows(scan, doc, _service_names(request),
                           tenant=tenant or "", subject=subject or "")
    if q:
        rows = [r for r in rows
                if r["service"].startswith(q) or (r.get("registeredName") or "").startswith(q)]
    rows = ur.sort_rows(rows, sort, order)
    total, items = ur.paginate(rows, page, page_size)
    return JSONResponse(_envelope(scan, status, items, total=total, page=page,
                                  page_size=page_size,
                                  tenants=ur.tenant_options(doc, scan)))


@router.get("/v1/admin/usage/trend")
async def usage_trend_ep(request: Request, days: int = 7,
                         dim: str = "caller") -> JSONResponse:
    """用量趋势（设计稿 §3.4）：日粒度折线，维度切换调用方/服务，Top10＋其余聚合；
    30/90 天窗属二期（随 usage_daily 上线，前端长窗入口置灰）。"""
    try:
        days = ur.parse_days(days)
        dim = ur.parse_dim(dim)
    except ValueError as exc:
        return _bad_request(errors.BAD_QUERY, str(exc))
    scan, status = await _snapshot(request, days)
    trend = ur.trend_series(scan, {"keys": request.app.state.appkeys}, dim)
    return JSONResponse(_envelope(scan, status, trend["series"],
                                  total=len(trend["series"]), page=1,
                                  page_size=ur.DEFAULT_PAGE_SIZE,
                                  dim=dim, series=trend["series"],
                                  totals=ur.window_totals(scan),
                                  others=trend["others"]))
