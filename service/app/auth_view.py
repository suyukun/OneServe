# -*- coding: utf-8 -*-
"""授权登记全景（P1-8 · SPEC §2-C2/§5-P1-8）——纯逻辑，只读 auth_matrix.yaml。

职责（分层纪律同 my_access.py / appkeys.py：本模块不 import FastAPI/redis）：
  ① 全景：租户 × appkey × 服务 × 字段权限登记全表（一行＝一条 grants 登记条目）；
  ② 反查：「这个字段给了谁」——给定字段名，列出持有它的租户/服务/单号（与「字段字典」
     页同源：auth_matrix grants[].fields 即投影红线，字段页标「已对外开放」的口径来源）；
  ③ 溯源：OA 单号 → 它授权了哪些租户/服务/字段（tenant 级与 grant 级两个登记位合并）；
  ④ 审计口径：缺 OA 单号如实标 missing、占位单号（OA-PENDING-* 等）如实标 placeholder，
     不代填假单号也不假装已合规（ADR-0003：登记须填 OA 单号；存量数据可能不一致→如实展示）。

只读：本模块不写任何配置文件；授权登记的写侧归运营台/CLI，不在本卡范围。

掩码纪律（与 appkeys.list_view / my_access.admin_usage 同源）：完整 appkey 不出端点，
出 appkeyMasked（mask_appkey）＋ kid（sha256 前 12 位）供运营台对照——auth_matrix.yaml
内虽有明文 appkey（网关 key-auth 凭证，git 留痕口径），门户侧一律掩码展示。

可测性：全部函数无副作用、无 IO（matrix 由调用方注入），空/脏数据如实降级不抛错。
"""
from __future__ import annotations

from app import appkeys as appk

# 口径说明（端点随行 caliber 字段＝页面就地文案的唯一来源；改口径只改这一处）
AUTH_CALIBER = {
    "source": "service/auth_matrix.yaml（授权登记唯一权威源，本页只读展示）",
    "oaRule": "ADR-0003：审批走工单系统，平台只做授权登记；登记条目须填 OA 单号",
    "oaMissing": "存量登记可能缺单号或仍是占位单号——本页如实标注「缺失 / 占位待回填」，不代填",
    "writeSide": "本页只读；授权登记的写侧归运营台与 auth_admin CLI，不在 P1-8 范围",
    "appkeyMasked": "appkey 按掩码展示（前 7 位＋后 4 位），kid＝sha256 前 12 位供运营台对照",
    "fieldLink": "字段反查与表级 schema 元数据（装载副产品）同源：auth_matrix grants[].fields 即字段投影红线，字段开放明细见各服务详情页出参表",
}

# 占位单号前缀（演示/待回填）：如 OA-PENDING-TASK008——不是真实 OA 单号，如实归类
OA_PLACEHOLDER_PREFIXES = ("OA-PENDING", "PENDING", "TODO", "TBD")

OA_STATE_LABELS = {"present": "已登记单号", "placeholder": "占位待回填", "missing": "缺失"}


def oa_state(ticket) -> str:
    """OA 单号状态：present（有真实单号）｜placeholder（占位待回填）｜missing（缺失）。

    空/空白＝missing；OA-PENDING-* / PENDING-* / TODO* / TBD* ＝placeholder——占位串不等于
    合规单号，如实单列一类，避免把演示占位当成已登记（ADR-0003 的合规面口径）。
    """
    t = str(ticket or "").strip()
    if not t:
        return "missing"
    if t.upper().startswith(OA_PLACEHOLDER_PREFIXES):
        return "placeholder"
    return "present"


def _mask(appkey: str) -> tuple[str, str]:
    """appkey → （掩码, kid）；空 appkey 如实留空（不伪造指纹）。"""
    if not appkey:
        return "", ""
    return appk.mask_appkey(appkey), appk.record_kid(appkey)


def grant_rows(matrix) -> list[dict]:
    """登记矩阵 → 扁平全景行（一行＝一个租户的一条 grants 登记）。

    容错只读：matrix/entry/grants 非预期类型一律跳过，不抛错——合规全景不该被
    脏数据打断（同 my_access 日志读法的容错口径）。
    有效单号＝grant.oa_ticket 优先，其次 tenant.oa_ticket（两级登记位，grant 级更细）；
    oaSource 记明取位，页面可区分「本服务单独批」与「租户级统一批」。
    """
    rows: list[dict] = []
    for entry in matrix or []:
        if not isinstance(entry, dict):
            continue
        tenant = str(entry.get("tenant") or "").strip()
        appkey = str(entry.get("appkey") or "").strip()
        masked, kid = _mask(appkey)
        tenant_ticket = str(entry.get("oa_ticket") or "").strip()
        tenant_state = oa_state(tenant_ticket)
        for g in entry.get("grants") or []:
            if not isinstance(g, dict):
                continue
            service = str(g.get("service") or "").strip()
            fields = [str(f).strip() for f in (g.get("fields") or []) if str(f).strip()]
            g_ticket = str(g.get("oa_ticket") or "").strip()
            effective = g_ticket or tenant_ticket
            rows.append({
                "tenant": tenant,
                "appkeyMasked": masked,
                "kid": kid,
                "service": service,
                "serviceId": service,   # 卡4 AC-3d：裸 service_id（版本位已删，版本视图字段不再输出）
                "endpoint": str(g.get("endpoint") or "").strip(),
                "fields": fields,
                "fieldsCount": len(fields),
                "tenantOaTicket": tenant_ticket,
                "tenantOaState": tenant_state,
                "grantOaTicket": g_ticket,
                "oaTicket": effective,
                "oaState": oa_state(effective),
                "oaSource": "grant" if g_ticket else ("tenant" if tenant_ticket else "none"),
            })
    return rows


def _entry(row: dict) -> dict:
    """全景行 → 反查/溯源视图条目（同字段，语义命名；完整 appkey 永不出现）。"""
    return {
        "tenant": row["tenant"],
        "appkeyMasked": row["appkeyMasked"],
        "kid": row["kid"],
        "service": row["service"],
        "serviceId": row["serviceId"],
        "endpoint": row["endpoint"],
        "fields": row["fields"],
        "fieldsCount": row["fieldsCount"],
        "oaTicket": row["oaTicket"],
        "oaState": row["oaState"],
        "oaSource": row["oaSource"],
    }


def oa_tickets(matrix) -> list[dict]:
    """OA 单号索引（溯源列表）：单号 → 覆盖的租户/服务/字段数（tenant 级＋grant 级合并）。

    同一单号出现在多个登记位时聚合为一条（一个批件批一片授权是常态）；计数按登记条目
    去重后如实给。缺单号条目不在本列表（列进 missing）——缺就是缺，不塞空串占位。
    """
    buckets: dict[str, dict] = {}
    for r in grant_rows(matrix):
        if r["oaState"] == "missing":
            continue
        b = buckets.setdefault(r["oaTicket"], {
            "ticket": r["oaTicket"], "state": r["oaState"],
            "count": 0, "tenants": [], "services": [], "fields": []})
        b["count"] += 1
        if r["tenant"] not in b["tenants"]:
            b["tenants"].append(r["tenant"])
        if r["service"] not in b["services"]:
            b["services"].append(r["service"])
        for f in r["fields"]:
            if f not in b["fields"]:
                b["fields"].append(f)
    return sorted(buckets.values(), key=lambda b: (b["state"] != "present", b["ticket"]))


def panorama(matrix) -> dict:
    """① 全景视图：登记全表＋筛选项＋汇总（含 OA 缺失/占位如实计数）。

    返回 rows（每行含 oaState，页面据此逐行标「缺失/占位」）＋ facets（租户/服务/字段
    选项，供页面筛选控件）＋ summary（租户数/凭证数/服务数/登记条目/字段引用与去重数/
    OA 缺失条数）＋ oaTickets（单号溯源列表）。空矩阵＝全零空态，如实不造行。
    """
    rows = grant_rows(matrix)
    tenants = sorted({r["tenant"] for r in rows if r["tenant"]})
    services = sorted({r["service"] for r in rows if r["service"]})
    fields = sorted({f for r in rows for f in r["fields"]})
    kids = {r["kid"] for r in rows if r["kid"]}
    return {
        "rows": rows,
        "facets": {"tenants": tenants, "services": services, "fields": fields},
        "summary": {
            "tenants": len(tenants),
            "appkeys": len(kids),
            "services": len(services),
            "grants": len(rows),
            "fieldRefs": sum(r["fieldsCount"] for r in rows),
            "distinctFields": len(fields),
            "oaMissing": sum(1 for r in rows if r["oaState"] == "missing"),
            "oaPlaceholder": sum(1 for r in rows if r["oaState"] == "placeholder"),
        },
        "oaTickets": oa_tickets(matrix),
        "caliber": AUTH_CALIBER,
    }


def field_lookup(matrix, field_name: str) -> dict:
    """② 反查「这个字段给了谁」：字段名精确匹配（登记字段名区分大小写，原样比对）。

    空字段名＝found=false 如实空态（不是错误，页面提示先选字段）；无命中同样
    found=false——「没开放给任何人」与「查无此字段」在平台口径下等价（字段未授权
    即经任何接口零出现），页面统一按空态展示，不编造持有方。
    """
    name = str(field_name or "").strip()
    if not name:
        return {"field": "", "found": False, "entries": [], "summary": None,
                "caliber": AUTH_CALIBER}
    entries = [_entry(r) for r in grant_rows(matrix) if name in r["fields"]]
    return {
        "field": name,
        "found": bool(entries),
        "entries": entries,
        "summary": {
            "tenants": len({e["tenant"] for e in entries}),
            "services": len({e["service"] for e in entries}),
            "grants": len(entries),
            "oaMissing": sum(1 for e in entries if e["oaState"] == "missing"),
            "oaPlaceholder": sum(1 for e in entries if e["oaState"] == "placeholder"),
        },
        "caliber": AUTH_CALIBER,
    }


def oa_trace(matrix, ticket: str) -> dict:
    """③ OA 单号溯源：按单号精确检索它授权了哪些租户/服务/字段。

    tenant 级与 grant 级两个登记位都参与匹配（同一单号可同时出现在两级）；
    空单号＝found=false 空态；未命中＝found=false（如实「本登记里查无此单号」）。
    """
    t = str(ticket or "").strip()
    if not t:
        return {"ticket": "", "found": False, "state": "missing", "entries": [],
                "caliber": AUTH_CALIBER}
    entries = [_entry(r) for r in grant_rows(matrix)
               if t in (r["tenantOaTicket"], r["grantOaTicket"])]
    return {"ticket": t, "found": bool(entries), "state": oa_state(t),
            "entries": entries, "caliber": AUTH_CALIBER}
