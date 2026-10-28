# -*- coding: utf-8 -*-
"""报表专区一期聚合读侧（1b-9 · 设计稿 docs/design/报表专区一期设计_v1.md §3/§4）。

分层纪律（同 metering.py）：聚合/筛选/排序/分页/校验全在本模块纯函数，
不 import FastAPI/redis；metering_api.py 是 HTTP 薄胶水（四端点
/v1/admin/usage/{overview,subjects,services,trend}）。

数据真源＝Redis 统计快照（metering_from_stats，svc 维度含服务×日矩阵）＋
凭证登记运行态（appkeys 文档 join 登记名/租户/凭证编号）。**现算禁令**
（ADR-0023 §5）：快照缺失/未响应＝降级态响应，绝不回落 metering_scan
JSONL 现算——本模块从头到尾不 import metering_scan。

行字段名沿 metering_view 既有名（callerMasked/appName/tenant/registered/
calls/success/fail/avgMs/trend/services）——迁移零改名（设计稿 §4 无悔步②）。
一期读快照、二期切 usage_daily GROUP BY：端点契约（参数与响应包络）一期即
按终态设计，二期仅替换内部读函数，页面与调用方零改动（设计稿 §7 无悔步①）。
"""
from __future__ import annotations

from app import appkeys as appk
# 私有名跨模块取用系本仓既定纪律（metering.py 同款注释：读取器语义被测试
# 钉死，复制漂移比共享私有名更危险）——窗口/均值/掩码全部单一实现。
from app.metering import (
    METERING_CALIBER,
    METERING_MAX_DAYS,
    _avg_ms,
    masked_caller,
)

# 服务端分页（设计稿 §4）：page（1 起）/page_size∈{20,50,100} 默认 50
PAGE_SIZES = (20, 50, 100)
DEFAULT_PAGE_SIZE = 50

# 排序白名单（设计稿 §4）：非法值 400；默认 sort=calls&order=desc
SORT_FIELDS = ("calls", "success_rate", "avg_ms", "name")
SORT_ORDERS = ("asc", "desc")

# 登记状态筛选值域：全部（缺省）/已登记/未登记
REGISTERED_VALUES = ("", "true", "false")

# 三态文案（设计稿 §4 三态规格；前端与机检同源，不写第二份）
MSG_EMPTY = "窗口内暂无调用记录（未产生调用是真实状态，非缺数）"
MSG_DEGRADED = "用量数据暂不可用：统计快照未响应，请稍后重试"
MSG_SVC_PENDING = "—（待服务维度补丁）"


# ── 参数校验（ValueError → 端点 400 bad_query，同 days 非法口径） ──

def parse_days(days) -> int:
    """时间窗 1..7（东八区自然日，窗口上限与 metering 一致）；非法抛 ValueError。"""
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= METERING_MAX_DAYS:
        raise ValueError(f"时间窗只支持 1（今日）到 {METERING_MAX_DAYS}（近 {METERING_MAX_DAYS} 天）天")
    return days


def parse_dim(dim) -> str:
    """趋势维度：caller（调用方）/ service（服务）；非法抛 ValueError。"""
    if dim not in ("caller", "service"):
        raise ValueError("维度只支持 caller（调用方）或 service（服务）")
    return dim


def parse_paging(page, page_size) -> tuple[int, int]:
    """分页参数：page ≥1 整数；page_size∈{20,50,100}（缺省 50）；非法抛 ValueError。"""
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        raise ValueError("page 须为 1 起的整数")
    if page_size is None:
        page_size = DEFAULT_PAGE_SIZE
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size not in PAGE_SIZES:
        raise ValueError(f"page_size 只支持 {'/'.join(str(v) for v in PAGE_SIZES)}")
    return page, page_size


def parse_sort(sort, order) -> tuple[str, str]:
    """排序白名单校验；缺省 calls/desc；非法抛 ValueError。"""
    if sort is None:
        sort = "calls"
    if sort not in SORT_FIELDS:
        raise ValueError(f"排序字段只支持 {'｜'.join(SORT_FIELDS)}")
    if order is None:
        order = "desc"
    if order not in SORT_ORDERS:
        raise ValueError("排序方向只支持 asc（升序）或 desc（降序）")
    return sort, order


def parse_registered(registered) -> str:
    """登记状态筛选值域校验（''＝全部）；非法抛 ValueError。"""
    if registered is None:
        return ""
    if registered not in REGISTERED_VALUES:
        raise ValueError("登记状态只支持：全部（留空）/true（已登记）/false（未登记）")
    return registered


# ── 聚合与视图 ──

def _rate(success: int, calls: int) -> float | None:
    """成功率（百分比 1 位小数）；无调用＝None（不造 0 冒充比率）。"""
    return round(success / calls * 100, 1) if calls else None


def _registry(doc: dict) -> dict:
    """appkeys 文档 → appkey 原值 → 登记记录 索引（同 metering_view 口径）。"""
    return {str(k.get("appkey")): k for k in doc.get("keys") or [] if k.get("appkey")}


def subject_rows(scan: dict, doc: dict) -> list[dict]:
    """主体行（metering_view 同形＋成功率列）：掩码＋登记合并＋调用量降序。

    出参不含凭证原串（掩码纪律）；q 前缀搜索所需的原值匹配由 q_filter
    借 scan["callers"] 键与登记表完成，原串永不出本模块返回值。
    """
    reg = _registry(doc)
    day_keys = scan["days"]
    rows = []
    for caller, agg in scan["callers"].items():
        rec = reg.get(caller)
        rows.append({
            "callerMasked": masked_caller(caller),
            "kid": appk.record_kid(caller) if rec else None,
            "appName": (rec or {}).get("app_name"),
            "tenant": (rec or {}).get("tenant"),
            "registered": rec is not None,
            "calls": agg["calls"], "success": agg["success"], "fail": agg["fail"],
            "successRate": _rate(agg["success"], agg["calls"]),
            "avgMs": _avg_ms(agg["latSum"], agg["latCnt"]),
            # 趋势补零对齐窗口（连续轴不跳点，同 metering_view 口径）
            "trend": [{"date": d, "calls": (agg["days"].get(d) or [0, 0, 0])[0],
                       "success": (agg["days"].get(d) or [0, 0, 0])[1],
                       "fail": (agg["days"].get(d) or [0, 0, 0])[2]} for d in day_keys],
            # 服务明细（svc 维度聚合；旧键无 svc 字段＝空列表，页面降级「—」）
            "services": sorted(
                ({"endpoint": ep, "calls": c, "success": s, "fail": f,
                  "avgMs": _avg_ms(ls, lc)} for ep, (c, s, f, ls, lc) in agg["services"].items()),
                key=lambda x: (-x["calls"], x["endpoint"])),
        })
    rows.sort(key=lambda x: (-x["calls"], x["callerMasked"]))
    return rows


def has_service_dim(scan: dict) -> bool:
    """快照里是否出现过 svc 字段（False＝补丁前旧键，服务列降级「—（待服务维度补丁）」）。"""
    return any(c.get("services") for c in scan["callers"].values())


def q_filter(rows: list[dict], scan: dict, doc: dict, q: str) -> list[dict]:
    """q 前缀搜索：匹配登记名／凭证原串前缀（掩码串前缀也收——页面间跳转链路用），
    出口仍掩码：原串只用于匹配判定，绝不进返回行（完整凭证不出端点）。"""
    if not q:
        return rows
    reg = _registry(doc)
    masked_to_caller = {masked_caller(c): c for c in scan["callers"]}
    out = []
    for row in rows:
        original = masked_to_caller.get(row["callerMasked"], "")
        rec = reg.get(original) or {}
        name = str(row.get("appName") or rec.get("app_name") or "")
        if original.startswith(q) or name.startswith(q) or row["callerMasked"].startswith(q):
            out.append(row)
    return out


def apply_subject_filters(rows: list[dict], *, tenant: str = "",
                          registered: str = "") -> list[dict]:
    """主体行筛选：租户（登记租户串精确匹配）＋登记状态（true/false）。"""
    out = []
    for row in rows:
        if tenant and (row.get("tenant") or "") != tenant:
            continue
        if registered == "true" and not row["registered"]:
            continue
        if registered == "false" and row["registered"]:
            continue
        out.append(row)
    return out


# 排序白名单字段 → 行键名（出参字段沿 metering_view 驼峰：successRate/avgMs）
SORT_KEY_MAP = {"calls": "calls", "success_rate": "successRate",
                "avg_ms": "avgMs", "name": "name"}


def sort_rows(rows: list[dict], sort: str, order: str) -> list[dict]:
    """白名单排序：calls/success_rate/avg_ms 数值列（None 沉底）；name＝登记名
    （主体页，缺登记名回落掩码串；服务页行自带 name 键＝登记名或路由原值）。"""
    reverse = order == "desc"
    key_field = SORT_KEY_MAP[sort]

    def _key(row: dict):
        if key_field == "name":
            return str(row.get("name") or row.get("appName") or row.get("callerMasked") or "")
        v = row.get(key_field)
        return (1, 0) if v is None else (0, v)   # None（无样本均值/比率）恒沉底

    return sorted(rows, key=_key, reverse=reverse)


def service_rows(scan: dict, doc: dict, service_names: dict | None = None,
                 *, tenant: str = "", subject: str = "") -> list[dict]:
    """服务行：svc 维度按服务聚合（调用量/成败/均值/调用方数）＋登记服务名合并。

    service_names＝路由路径 → 登记服务名 索引（端点层由 app.state.services
    构建）；无登记名的服务主列留路由原值（前端等宽码呈现＝技术标识豁免）。
    tenant/subject 筛选先裁 caller 集再聚合（服务→主体下钻链路反方向）。
    """
    reg = _registry(doc)
    names = service_names or {}
    allowed: set[str] | None = None
    if tenant or subject:
        allowed = set()
        for caller in scan["callers"]:
            rec = reg.get(caller) or {}
            if tenant and str(rec.get("tenant") or "") != tenant:
                continue
            if subject:
                m = masked_caller(caller)
                if not (caller.startswith(subject) or m.startswith(subject)
                        or str(rec.get("app_name") or "").startswith(subject)):
                    continue
            allowed.add(caller)
    rows: dict[str, dict] = {}
    for caller, agg in scan["callers"].items():
        if allowed is not None and caller not in allowed:
            continue
        for ep, (c, s, f, ls, lc) in agg["services"].items():
            row = rows.setdefault(ep, {"service": ep, "registeredName": names.get(ep),
                                       "callers": 0, "calls": 0, "success": 0, "fail": 0,
                                       "latSum": 0, "latCnt": 0})
            row["calls"] += c
            row["success"] += s
            row["fail"] += f
            row["latSum"] += ls
            row["latCnt"] += lc
            row["callers"] += 1        # 外层每 caller 至多进一次＝去重调用方数
    out = []
    for ep, row in rows.items():
        out.append({
            "service": ep,
            "registeredName": row["registeredName"],
            "callers": row["callers"],
            "calls": row["calls"], "success": row["success"], "fail": row["fail"],
            "successRate": _rate(row["success"], row["calls"]),
            "avgMs": _avg_ms(row["latSum"], row["latCnt"]),
            "name": row["registeredName"] or ep,   # 排序键（白名单 name→service 同义）
        })
    return out


def daily_totals(scan: dict) -> list[dict]:
    """逐日全平台调用量（总览页柱图／趋势页总量线）：补零对齐窗口。"""
    totals = {d: [0, 0, 0] for d in scan["days"]}
    for agg in scan["callers"].values():
        for d, row in (agg.get("days") or {}).items():
            if d in totals:
                t = totals[d]
                t[0] += row[0]
                t[1] += row[1]
                t[2] += row[2]
    return [{"date": d, "calls": t[0], "success": t[1], "fail": t[2]}
            for d, t in totals.items()]


def window_totals(scan: dict) -> dict:
    """窗口总量（调用/成败/平均耗时/成功率）——口径同 METERING_CALIBER。"""
    calls = sum(a["calls"] for a in scan["callers"].values())
    success = sum(a["success"] for a in scan["callers"].values())
    fail = sum(a["fail"] for a in scan["callers"].values())
    lat_sum = sum(a["latSum"] for a in scan["callers"].values())
    lat_cnt = sum(a["latCnt"] for a in scan["callers"].values())
    return {"calls": calls, "success": success, "fail": fail,
            "avgMs": _avg_ms(lat_sum, lat_cnt), "successRate": _rate(success, calls)}


def trend_series(scan: dict, doc: dict, dim: str) -> dict:
    """趋势页数据：维度实体 Top10 逐日曲线＋其余聚合（设计稿 §3.4）。

    dim=caller：逐主体（掩码出口）；dim=service：逐服务（svc×日矩阵，
    metering_from_stats["svcDays"]——快照侧专供，JSONL 路径无此矩阵）。
    """
    day_keys = scan["days"]
    entities: dict[str, dict] = {}
    if dim == "caller":
        reg = _registry(doc)
        for caller, agg in scan["callers"].items():
            rec = reg.get(caller) or {}
            masked = masked_caller(caller)
            entities[caller] = {
                "key": masked,
                "label": str(rec.get("app_name") or "") or masked,
                "days": agg.get("days") or {},
            }
    else:
        for agg in scan["callers"].values():
            for day_d, per_svc in (agg.get("svcDays") or {}).items():
                for ep, row in per_svc.items():
                    e = entities.setdefault(ep, {"key": ep, "label": ep, "days": {}})
                    t = e["days"].setdefault(day_d, [0, 0, 0])
                    t[0] += row[0]
                    t[1] += row[1]
                    t[2] += row[2]
    series = []
    for e in entities.values():
        calls = sum(v[0] for v in e["days"].values())
        success = sum(v[1] for v in e["days"].values())
        fail = sum(v[2] for v in e["days"].values())
        series.append({
            "key": e["key"], "label": e["label"],
            "calls": calls, "success": success, "fail": fail,
            "successRate": _rate(success, calls),
            "data": [{"date": d, "calls": (e["days"].get(d) or [0, 0, 0])[0],
                      "success": (e["days"].get(d) or [0, 0, 0])[1],
                      "fail": (e["days"].get(d) or [0, 0, 0])[2]} for d in day_keys],
        })
    series.sort(key=lambda x: (-x["calls"], x["key"]))
    top = series[:10]
    rest = series[10:]
    others = None
    if rest:
        agg = {"calls": 0, "success": 0, "fail": 0}
        per_day = {d: [0, 0, 0] for d in day_keys}
        for r in rest:
            agg["calls"] += r["calls"]
            agg["success"] += r["success"]
            agg["fail"] += r["fail"]
            for point in r["data"]:
                t = per_day[point["date"]]
                t[0] += point["calls"]
                t[1] += point["success"]
                t[2] += point["fail"]
        others = {"label": f"其余 {len(rest)} 项", "count": len(rest),
                  "calls": agg["calls"], "success": agg["success"], "fail": agg["fail"],
                  "data": [{"date": d, "calls": t[0], "success": t[1], "fail": t[2]}
                           for d, t in per_day.items()]}
    return {"dim": dim, "dayKeys": day_keys, "series": top, "others": others}


def tenant_options(doc: dict, scan: dict) -> list[str]:
    """租户下拉：凭证登记运行态租户串去重排序（沙箱自造串如实展示，类型标注随二期）。"""
    reg = _registry(doc)
    tenants = {str((reg.get(c) or {}).get("tenant") or "")
               for c in scan["callers"] if reg.get(c)}
    tenants.discard("")
    return sorted(tenants)


def paginate(rows: list[dict], page: int, page_size: int) -> tuple[int, list[dict]]:
    """服务端切片：越界页返回空 items、total 不变（幂等翻页，设计稿 §4）。"""
    total = len(rows)
    start = (page - 1) * page_size
    return total, rows[start:start + page_size]


def degrade_envelope(scan_days: list[str], **extra) -> dict:
    """降级态包络（快照未响应）：不回落 JSONL 现算，前端按 status 渲染降级横幅。"""
    env = {"status": "degraded", "message": MSG_DEGRADED, "days": len(scan_days),
           "dayKeys": scan_days, "total": 0, "page": 1,
           "pageSize": DEFAULT_PAGE_SIZE, "items": [], "generatedAt": None,
           "caliber": METERING_CALIBER}
    env.update(extra)
    return env
