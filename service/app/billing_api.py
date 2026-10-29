# -*- coding: utf-8 -*-
"""账单域端点（管理功能群一期 C 节·账单页＋CSV · 设计 v1.1 §3 已拍板，按 §0 共用口径施工）。

端点组（均 /v1/admin/billing/*；router 级挂 admin_api._admin_gate——原写方法过
auth_oidc.require_login，该挂点随 v0.1.1 登录清理摘除（ADR-0029 §7）；原
ROUTE_MIN_ROLE 域7 扩注：读/导出 viewer 地板、确认/补录 admin，矩阵同批退役）：
  GET  /v1/admin/billing/periods                        月账列表（四态＋三硬前置留痕三列，
                                                        分页 metering_api 先例＋指标卡 summary）
  GET  /v1/admin/billing/periods/{period}               详情（bill_line 四键行×双快照，分页）
  GET  /v1/admin/billing/periods/{period}/export        账期级 CSV（bills-<period>.csv）
  GET  /v1/admin/billing/periods/{period}/export-lines  明细级 CSV（bills-<period>-lines.csv）
  POST /v1/admin/billing/periods/{period}/confirm       确认（必填 OA 单号；admin）
  POST /v1/admin/billing/periods/{period}/register-oa   补录单号（仅 confirmed；admin）

【只读＋确认，无出账触发（拍板②）】出账是机器门禁任务＝CLI scripts/billing_run.py
（三硬前置逐条拦门），页面确认是人的决策点（§3.1 切面）；确认/补录/守卫与 CLI
同源同句走共享模块 app/billing_state.py（P1-⑬＋P0-6：状态跃迁事务内 UPDATE…WHERE
status 守卫核行，确认×rebill 并发竞态两侧封死）。欠费标记展示只读（标记动作走 CLI）。

【CSV 规格（§3.4，ADR-0024 决定第 5 条）】两级：账期级（每账期一行）＋明细级
（bill_line 四键＋双快照全列，中文表头沿术语净化表）；UTF-8 带 BOM（Excel 中文
兼容）；数字原值不格式化；分层口径（拍板④）：页面浏览面调用方掩码（metering
掩码唯一实现），明细级 CSV 调用方列带完整凭证编号（财务对账唯一键——完整串不落
页面仅落交付物），导出动作入 bill_export 审计留痕。

【通道】账单真源＝pg usage schema（0004，天生 pg-only，不受 META_WRITER 双模
管辖）；通道形态沿 billing_run 通道口径＋前置卡协议（P0-1）：BILLING_PSQL_BASE
（测试注入，优先级最高，协议同 USAGE_PSQL_BASE）＞ META_DB_HOST 直连（服务容器内
形态，五键注入）＞ USAGE_PSQL_BASE ＞ META_MIGRATE_PSQL_BASE ＞ compose 缺省；
meta_app 角色（0004 分域授权 usage_app 读写）。库不可达＝503 bill_db_unavailable
降级拒绝，不回落 yaml（账单无 yaml 形态，不为它发明）。

【审计双落（§3.7；§0 总口径）】①管线八元组（endpoint＝admin:<动作名>，复用
admin_api._audit_admin_action）；②meta.admin_event 窄表——确认/补录与状态跃迁
同事务落 ok（billing_state.confirm_with_event/register_oa_with_event，FOUND 守卫
未命中＝回滚且事件不落）；被拒（状态不符/缺单号）＝管线 denied＋窄表 denied
尽力而为落；导出（无状态变更动作）＝管线＋窄表 bill_export，尽力而为不阻断交付。

分层纪律（同 grants_admin/metering_api）：查询构造/CSV 组装纯函数（宿主机无
FastAPI 可单测）；HTTP 编排只在端点区。域界：不动 scripts/usage_rollup.py（他域）、
不动 service/app/main.py（挂载归收口批——挂载行 app.include_router(billing_api.router)）、
不碰 portal/ 产物与 admin/tenants* pricing*（他批）、migrations 不碰。
"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app import billing_state as bs      # 状态机/守卫共享模块（CLI 同源同句，P1-⑬）
from app import errors as E
from app import metering as mt           # masked_caller 掩码唯一实现
from app import usage_report as urp      # parse_paging（零第二套分页法，§0 总口径）
from app.admin_api import _admin_gate, _audit_admin_action   # 同闸同挂点＋管线审计复用

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── 页面文案唯一来源（§3.8 边界与降级，随响应下行）──
MSG_DB_DOWN = "账单库（pg）暂不可达：账单真源在 pg usage schema（不受 META_WRITER 管辖，无 yaml 回落）——稍后重试或联系平台值班。"
MSG_EMPTY = "暂无账期（出账走命令行，见上方引导卡；三硬前置未过会拒并说明原因）。"
CALIBER = ("双快照口径：逐行用量快照（usage_daily 四键月聚合整值）＋成交单价快照"
           "（出账时点，价目后续改动不改历史账，ADR-0024 决定第 4 条）；"
           "空价目试运行未计价行金额 0 元且不计应收（决定第 9 条）。")
STATUS_LABELS = {"draft": "草稿", "reconciled": "已对账", "confirmed": "已确认",
                 "written_off": "已核销"}

_PSQL_DEFAULT = ("docker compose -f compose.metadata.yml -p oneserve-meta exec -T "
                 "metadata-db psql -U meta_app -d oneserve_meta "
                 "-v ON_ERROR_STOP=1 -A -t -P footer=off")


class BillingDbError(RuntimeError):
    """账单库（pg usage schema）通道不可用/查询失败（人话 message 含 psql stderr 尾部）。"""


class BillingPsql:
    """账单域 psql 通道（通道优先级详模块头注；psql -A -t 单值文本协议）。"""

    def __init__(self, timeout_s: int = 60):
        custom = (os.environ.get("BILLING_PSQL_BASE", "").strip()
                  or os.environ.get("USAGE_PSQL_BASE", "").strip()
                  or os.environ.get("META_MIGRATE_PSQL_BASE", "").strip())
        host = (os.environ.get("META_DB_HOST") or "").strip()
        if custom:
            self.base = custom.split()
        elif host:
            port = (os.environ.get("META_DB_PORT") or "").strip() or "5432"
            user = (os.environ.get("META_DB_USER") or "").strip() or "meta_app"
            db = (os.environ.get("META_DB_DATABASE") or "").strip() or "oneserve_meta"
            self.base = ["psql", "-h", host, "-p", port, "-U", user, "-d", db,
                         "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]
        else:
            self.base = _PSQL_DEFAULT.split()
        self.timeout_s = timeout_s
        pw = (os.environ.get("META_DB_PASSWORD") or "").strip()
        self.env = {**os.environ, "PGPASSWORD": pw} if pw else None

    def sql(self, script: str) -> str:
        p = subprocess.run(self.base, input=script.encode("utf-8"), capture_output=True,
                           timeout=self.timeout_s, env=self.env, cwd=str(REPO_ROOT))
        if p.returncode != 0:
            raise BillingDbError("psql rc=%d：%s" % (p.returncode,
                                                     p.stderr.decode("utf-8", "replace")[-800:]))
        return p.stdout.decode("utf-8", "replace")

    def rows(self, query: str) -> list:
        """查询 → 行字典列表（jsonb_agg 单行渲染，TimeZone 钉 UTC；meta_writer 协议同源）。"""
        out = self.sql("SET TimeZone TO 'UTC';\n"
                       "SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM (" + query + ") t;").strip()
        last = out.splitlines()[-1] if out else "[]"
        return json.loads(last) if last and last != "[]" else []


# ── 纯函数区：查询构造／行组装／CSV（宿主机可单测） ──

def period_start_iso(period: str) -> str:
    """'YYYY-MM' → period_start ISO（'2026-09' → '2026-09-01'）；非法抛 ValueError。"""
    lo, _, _ = bs.parse_period(period)
    return lo.isoformat()


_PERIOD_COLS = ("to_char(period_start,'YYYY-MM') AS period, status,"
                " gate_t3_closed, gate_reconcile_clean, gate_lag_zero, gate_checked_at,"
                " amount_due::text AS amount_due, overdue, overdue_marked_at,"
                " coalesce(oa_ticket,'') AS oa_ticket, reconciled_at, confirmed_at")


def periods_query(limit: int, offset: int) -> str:
    return ("SELECT " + _PERIOD_COLS + " FROM usage.billing_period"
            " ORDER BY period_start DESC LIMIT %d OFFSET %d" % (limit, offset))


def period_query(period: str) -> str:
    return ("SELECT " + _PERIOD_COLS + " FROM usage.billing_period"
            " WHERE period_start = date '" + period_start_iso(period) + "'")


def summary_query() -> str:
    return ("SELECT count(*) AS periods, coalesce(sum(amount_due), 0)::text AS amount_due,"
            " count(*) FILTER (WHERE overdue) AS overdue_count,"
            " count(*) FILTER (WHERE status = 'confirmed') AS confirmed_count,"
            " (SELECT to_char(max(period_start), 'YYYY-MM') FROM usage.billing_period) AS latest_period,"
            " (SELECT status FROM usage.billing_period ORDER BY period_start DESC LIMIT 1) AS latest_status"
            " FROM usage.billing_period")


def lines_query(period: str, limit: int, offset: int) -> str:
    return ("SELECT to_char(bl.period_start, 'YYYY-MM') AS period, bl.tenant_id,"
            " coalesce(t.display_name, '') AS tenant_display_name, bl.caller, bl.endpoint,"
            " bl.calls, bl.success, bl.fail, bl.lat_sum, bl.lat_cnt, bl.source_event_count,"
            " bl.priced, coalesce(bl.price_item_id, '') AS price_item_id,"
            " coalesce(bl.unit_price::text, '') AS unit_price, bl.amount::text AS amount"
            " FROM usage.bill_line bl LEFT JOIN meta.tenant t ON t.tenant_id = bl.tenant_id"
            " WHERE bl.period_start = date '" + period_start_iso(period) + "'"
            " ORDER BY bl.tenant_id, bl.caller, bl.endpoint"
            " LIMIT %d OFFSET %d" % (limit, offset))


def lines_summary_query(period: str) -> str:
    return ("SELECT count(*) AS line_count, coalesce(sum(success), 0) AS success_sum,"
            " coalesce(sum(amount), 0)::text AS amount_sum,"
            " count(*) FILTER (WHERE NOT priced) AS unpriced_count"
            " FROM usage.bill_line WHERE period_start = date '" + period_start_iso(period) + "'")


def period_item(r: dict) -> dict:
    """账期行 → 列表/详情头部统一形状（三硬前置留痕三列＋核查时间，§3.2）。"""
    return {"period": r["period"], "status": r["status"],
            "gates": {"t3Closed": r["gate_t3_closed"], "reconcileClean": r["gate_reconcile_clean"],
                      "lagZero": r["gate_lag_zero"], "checkedAt": r["gate_checked_at"]},
            "amountDue": r["amount_due"], "overdue": r["overdue"],
            "overdueMarkedAt": r.get("overdue_marked_at"), "oaTicket": r.get("oa_ticket") or "",
            "reconciledAt": r.get("reconciled_at"), "confirmedAt": r.get("confirmed_at")}


def caller_index(appkeys_state) -> dict:
    """凭证登记索引 {appkey: rec}（读侧运行态只读；dict 文档取 keys[]，list 直用）。"""
    keys = appkeys_state.get("keys") if isinstance(appkeys_state, dict) else appkeys_state
    idx = {}
    for k in keys or []:
        if isinstance(k, dict) and k.get("appkey"):
            idx[str(k["appkey"])] = k
    return idx


def line_item(r: dict, callers: dict) -> dict:
    """bill_line 行 → 四键＋双快照两列组（§3.2）：调用方页面掩码（登记名优先＋
    凭证编号掩码副行——完整串不出页面端点，拍板④分层）；平均耗时＝lat_sum÷lat_cnt
    算术平均，无样本出 None（页面显示「—」）。"""
    rec = callers.get(str(r["caller"])) or {}
    avg = round(r["lat_sum"] / r["lat_cnt"], 3) if r["lat_cnt"] else None
    return {"period": r["period"], "tenantId": r["tenant_id"],
            "tenantDisplayName": r.get("tenant_display_name") or "",
            "caller": mt.masked_caller(str(r["caller"])),
            "callerName": str(rec.get("caller_name") or rec.get("app_name") or "") or None,
            "endpoint": r["endpoint"],
            "usageSnapshot": {"calls": r["calls"], "success": r["success"], "fail": r["fail"],
                              "avgLatencyMs": avg, "sourceEventCount": r["source_event_count"]},
            "priceSnapshot": {"priced": r["priced"], "unitPrice": r.get("unit_price") or None,
                              "amount": r.get("amount"), "priceItemId": r.get("price_item_id") or None}}


def _yn(b) -> str:
    return "true" if b else "false"


def _ts(v) -> str:
    return str(v) if v else ""


CSV_PERIOD_HEADER = ["账期", "状态", "前置①T+3账期关闭", "前置②计费面对账清零", "前置③覆盖齐零",
                     "前置核查时间", "应收合计", "欠费", "欠费标记时间", "OA单号", "确认时间"]
CSV_LINES_HEADER = ["账期", "租户ID", "租户显示名", "调用方", "调用方登记名", "服务",
                    "调用量", "成功量", "失败量", "平均耗时(毫秒)", "事件条数",
                    "是否计价", "成交单价", "金额", "价目ID"]


def csv_period_rows(items: list) -> list:
    """账期级 CSV 行集（每账期一行：§3.4 列序；布尔原值 true/false，不格式化）。"""
    return [[it["period"], it["status"],
             _yn(it["gates"]["t3Closed"]), _yn(it["gates"]["reconcileClean"]),
             _yn(it["gates"]["lagZero"]), _ts(it["gates"]["checkedAt"]),
             it["amountDue"], _yn(it["overdue"]), _ts(it["overdueMarkedAt"]),
             it["oaTicket"], _ts(it["confirmedAt"])] for it in items]


def csv_line_rows(lines: list) -> list:
    """明细级 CSV 行集（四键＋双快照全列；调用方列＝完整凭证编号，拍板④——
    财务对账唯一键，掩码有碰撞风险；完整串仅落交付物＋bill_export 审计留痕）。"""
    rows = []
    for ln in lines:
        u, p = ln["usageSnapshot"], ln["priceSnapshot"]
        rows.append([ln["period"], ln["tenantId"], ln["tenantDisplayName"],
                     ln["callerFull"], ln["callerName"] or "", ln["endpoint"],
                     u["calls"], u["success"], u["fail"],
                     ("" if u["avgLatencyMs"] is None else u["avgLatencyMs"]),
                     u["sourceEventCount"], _yn(p["priced"]),
                     p["unitPrice"] or "", p["amount"], p["priceItemId"] or ""])
    return rows


def csv_bytes(header: list, rows: list) -> bytes:
    """UTF-8 带 BOM CSV（Excel 中文兼容；§3.4）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def csv_response(filename: str, header: list, rows: list) -> Response:
    return Response(csv_bytes(header, rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % filename})


# ── 端点区（HTTP 编排薄层） ──

router = APIRouter(dependencies=[Depends(_admin_gate)])


def _operator(request: Request) -> str:
    claims = getattr(request.state, "oidc_claims", None) or {}
    return str(claims.get("sub") or "console")


def _db_down() -> JSONResponse:
    return JSONResponse(E.envelope(E.BILL_DB_UNAVAILABLE, MSG_DB_DOWN), status_code=503)


def _bad(code: str, message: str) -> JSONResponse:
    return JSONResponse(E.envelope(code, message), status_code=E.http_status_of(code))


def _bad_paging(exc: ValueError) -> JSONResponse:
    # 分页参数非法沿 metering_api 先例（§0：零第二套分页法）
    return JSONResponse({"error": E.BAD_QUERY, "message": str(exc)}, 400)


async def _payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _period_or_response(period: str):
    """账期入参校验（400 bill_period_invalid）；返回 (period_start_iso, None) 或 (None, resp)。"""
    try:
        return period_start_iso(period), None
    except ValueError:
        return None, _bad(E.BILL_PERIOD_INVALID,
                          "账期参数非法（须为 YYYY-MM 自然月），实得 %r" % period)


def _guard_denied(exc: BillingDbError) -> bool:
    """状态机守卫拦截（billing_state BILL_STATE_GUARD）→ 422 拒绝而非环境失败。"""
    return bs.BILL_STATE_GUARD_MARKER in str(exc)


@router.get("/v1/admin/billing/periods")
async def billing_periods_ep(request: Request, page: int = 1, page_size: int = None) -> JSONResponse:
    """月账列表（§3.2）：行＝账期，四态＋三硬前置留痕三列＋应收合计＋欠费徽标＋
    OA 单号＋确认时间；指标卡四张数据随 summary（最近账期状态/应收合计/欠费账期数/
    已确认账期数）。分页沿 metering_api 信封（total/page/pageSize/items）。"""
    try:
        page, page_size = urp.parse_paging(page, page_size)
    except ValueError as exc:
        return _bad_paging(exc)
    ch = BillingPsql()
    try:
        total_rows = ch.rows("SELECT count(*) AS total FROM usage.billing_period")
        total = int(total_rows[0]["total"]) if total_rows else 0
        items = [period_item(r) for r in ch.rows(periods_query(page_size, (page - 1) * page_size))]
        s = ch.rows(summary_query())[0] if total else None
    except BillingDbError:
        return _db_down()
    summary = ({"periods": int(s["periods"]), "amountDue": s["amount_due"],
                "overdueCount": int(s["overdue_count"]), "confirmedCount": int(s["confirmed_count"]),
                "latestPeriod": s["latest_period"],
                "latestStatusLabel": STATUS_LABELS.get(s["latest_status"], s["latest_status"])}
               if s else None)
    return JSONResponse({"status": "ok" if total else "empty",
                         "message": None if total else MSG_EMPTY,
                         "total": total, "page": page, "pageSize": page_size, "items": items,
                         "summary": summary, "caliber": CALIBER})


@router.get("/v1/admin/billing/periods/{period}")
async def billing_period_detail_ep(request: Request, period: str, page: int = 1,
                                   page_size: int = None) -> JSONResponse:
    """账期详情（§3.2）：头部账期卡（状态/三前置/OA/金额/欠费）＋bill_line 四键行×
    双快照两列组（分页）＋汇总行（Σ成功量/Σ金额/未计价行数——空价目试运行面）。"""
    ps, resp = _period_or_response(period)
    if resp:
        return resp
    try:
        page, page_size = urp.parse_paging(page, page_size)
    except ValueError as exc:
        return _bad_paging(exc)
    ch = BillingPsql()
    try:
        head = ch.rows(period_query(period))
        if not head:
            return _bad(E.BILL_PERIOD_NOT_FOUND, "账期 %s 不存在（出账走命令行 billing_run）" % period)
        callers = caller_index(getattr(request.app.state, "appkeys", None))
        lines = [line_item(r, callers)
                 for r in ch.rows(lines_query(period, page_size, (page - 1) * page_size))]
        s = ch.rows(lines_summary_query(period))[0]
    except BillingDbError:
        return _db_down()
    return JSONResponse({"status": "ok", "message": None,
                         "period": period_item(head[0]), "caliber": CALIBER,
                         "total": int(s["line_count"]), "page": page, "pageSize": page_size,
                         "items": lines,
                         "summary": {"lineCount": int(s["line_count"]),
                                     "successSum": int(s["success_sum"]),
                                     "amountSum": s["amount_sum"],
                                     "unpricedCount": int(s["unpriced_count"])}})


def _export_common(request: Request, period: str, action: str, lines_mode: bool):
    """导出共用：账期存在性＋明细行集（明细级调用方列＝完整凭证编号，拍板④）＋
    bill_export 审计留痕（管线＋窄表，尽力而为不阻断交付——无状态变更动作）。"""
    ps, resp = _period_or_response(period)
    if resp:
        return None, resp
    ch = BillingPsql()
    try:
        head = ch.rows(period_query(period))
        if not head:
            return None, _bad(E.BILL_PERIOD_NOT_FOUND,
                              "账期 %s 不存在（出账走命令行 billing_run）" % period)
        if lines_mode:
            raw_lines = ch.rows(lines_query(period, 100000, 0))
            callers = caller_index(getattr(request.app.state, "appkeys", None))
            lines = [line_item(r, callers) for r in raw_lines]
            for ln, raw in zip(lines, raw_lines):
                ln["callerFull"] = str(raw["caller"])
            rows = csv_line_rows(lines)
        else:
            rows = csv_period_rows([period_item(head[0])])
        ev = {"operator": _operator(request), "action": action, "target": period,
              "result": "ok", "oa_ticket": None, "tenant_id": None}
        try:
            bs.write_event(ch, ev)
        except Exception:  # noqa: BLE001 —— 留痕失败不阻断交付（管线事件仍下行）
            pass
    except BillingDbError:
        return None, _db_down()
    _audit_admin_action(request, action, ev["operator"], {"period": period},
                        {"export": action, "rows": len(rows)},
                        latency_ms=0, status=200)
    return rows, None


@router.get("/v1/admin/billing/periods/{period}/export")
async def billing_export_ep(request: Request, period: str) -> Response:
    """账期级 CSV 导出（bills-<period>.csv；每账期一行：§3.4 列序）。"""
    rows, resp = _export_common(request, period, "bill_export", lines_mode=False)
    if resp:
        return resp
    return csv_response("bills-%s.csv" % period, CSV_PERIOD_HEADER, rows)


@router.get("/v1/admin/billing/periods/{period}/export-lines")
async def billing_export_lines_ep(request: Request, period: str) -> Response:
    """明细级 CSV 导出（bills-<period>-lines.csv；bill_line 四键＋双快照全列，
    调用方列带完整凭证编号——财务对账唯一键，拍板④）。"""
    rows, resp = _export_common(request, period, "bill_export", lines_mode=True)
    if resp:
        return resp
    return csv_response("bills-%s-lines.csv" % period, CSV_LINES_HEADER, rows)


def _confirm_common(request: Request, period: str, payload: dict, register_only: bool):
    """确认/补录共用编排：必填单号（DDL billing_period_confirm_oa_check 同口径）→
    共享模块跃迁＋ok 留痕同事务（§0 双落②）；未命中（不存在/状态不符/并发抢先）
    ＝422 bill_status_refused＋denied 留痕（管线＋窄表尽力而为）。返回 (resp, ok)。"""
    ps, resp = _period_or_response(period)
    if resp:
        return resp, False
    action = "bill_register_oa" if register_only else "bill_confirm"
    operator = _operator(request)
    ticket = str(payload.get("oa_ticket") or "").strip()
    if not ticket:
        msg = ("OA 单号必填（确认结果与单号一并登记留痕，纯登记不校真伪，零系统集成）"
               if not register_only else "OA 单号必填（补录/更正同为纯登记留痕）")
        resp = _bad(E.INVALID_OA_TICKET, msg)
        _audit_admin_action(request, action, operator, {"period": period},
                            {"error": E.INVALID_OA_TICKET}, latency_ms=0, status=400)
        _try_denied_event(period, action, operator)
        return resp, False
    ch = BillingPsql()
    t0 = time.monotonic()
    ev = {"operator": operator, "action": action, "target": period,
          "result": "ok", "oa_ticket": ticket, "tenant_id": None}
    try:
        if register_only:
            bs.register_oa_with_event(ch, ps, ticket, ev)
        else:
            bs.confirm_with_event(ch, ps, ticket, ev)
    except BillingDbError as exc:
        if _guard_denied(exc):
            head = ch.rows(period_query(period))
            status = head[0]["status"] if head else "不存在"
            label = STATUS_LABELS.get(status, status)
            msg = ("仅已对账（reconciled）账期可确认：账期 %s 当前状态 %s"
                   "（状态机 reconciled→confirmed 不跳步）" % (period, label)
                   if not register_only else
                   "仅已确认（confirmed）账期可补录单号：账期 %s 当前状态 %s" % (period, label))
            resp = _bad(E.BILL_STATUS_REFUSED, msg)
            _audit_admin_action(request, action, operator, {"period": period, "oa_ticket": ticket},
                                {"error": E.BILL_STATUS_REFUSED},
                                latency_ms=int((time.monotonic() - t0) * 1000), status=422)
            _try_denied_event(period, action, operator)
            return resp, False
        _audit_admin_action(request, action, operator, {"period": period},
                            {"error": E.BILL_DB_UNAVAILABLE},
                            latency_ms=int((time.monotonic() - t0) * 1000), status=503)
        return _db_down(), False
    resp = JSONResponse({"period": period, "status": "confirmed", "oaTicket": ticket,
                         "message": ("账期 %s 已确认冻结（底数不可改，修正走调整单）；"
                                     "OA 单号已登记留痕" % period)
                         if not register_only else
                         ("账期 %s OA 单号补录留痕（纯登记不校真伪）" % period)})
    _audit_admin_action(request, action, operator, {"period": period, "oa_ticket": ticket},
                        {"period": period, "status": "confirmed"},
                        latency_ms=int((time.monotonic() - t0) * 1000), status=200)
    return resp, True


def _try_denied_event(period: str, action: str, operator: str) -> None:
    """denied 窄表留痕（尽力而为：拒绝零变更无同事务对偶义务；库不可达即跳过）。"""
    try:
        bs.write_event(BillingPsql(), {"operator": operator, "action": action,
                                       "target": period, "result": "denied",
                                       "oa_ticket": None, "tenant_id": None})
    except Exception:  # noqa: BLE001
        pass


@router.post("/v1/admin/billing/periods/{period}/confirm")
async def billing_confirm_ep(request: Request, period: str) -> JSONResponse:
    """确认（人的决策点，§3.3-1）：必填 OA 单号（格式提醒归界面 OA_TICKET_RE）→
    服务层状态机跃迁 reconciled→confirmed（共享模块事务内守卫核行，P0-6）→
    留痕（oa_ticket/confirmed_at＋admin_event）→前端刷新。非 reconciled 行→422 人话。"""
    payload = await _payload(request)
    resp, _ok = _confirm_common(request, period, payload, register_only=False)
    return resp


@router.post("/v1/admin/billing/periods/{period}/register-oa")
async def billing_register_oa_ep(request: Request, period: str) -> JSONResponse:
    """补录单号（§3.3-2）：＝--register-oa 语义，登记/更正 OA 单号（仅 confirmed）。"""
    payload = await _payload(request)
    resp, _ok = _confirm_common(request, period, payload, register_only=True)
    return resp
