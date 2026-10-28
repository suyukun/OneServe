# -*- coding: utf-8 -*-
"""billing 状态机共享模块（管理功能群一期 C 节 · 设计 v1.1 §3.3/§3.9-②，P1-⑬＋P0-6）。

CLI（scripts/billing_run.py）与服务端（app/billing_api.py）同源引用同一份
状态机/状态守卫/确认校验逻辑——本文件随 service/Dockerfile COPY app/ 进镜像，
CLI 侧 sys.path 引用同一文件，宿主与容器零第二实现漂移（设计 §3.3 v1.1 升级口径）。

【状态机（migrations/0004_billing_init.sql DDL CHECK 同值域）】
  draft -> reconciled -> confirmed -> written_off（核销可选默认不做）；
  确认冻结（ADR-0024 决定第 4 条）：confirmed/written_off 后账单底数不可改，
  rebill 永久拒绝，修正走调整单追加留痕。

【P0-6（对抗复核）：状态跃迁一律「事务内 UPDATE…WHERE status 守卫＋核行」，
判定不留在事务外——修复「确认与 rebill 并发竞态」】
  - 确认 reconciled->confirmed：单语句 UPDATE…AND status='reconciled' RETURNING
    （语句自身原子；RETURNING 空＝0 行 -> 调用方出 422/exit 2）；
  - rebill 重出：UPDATE 加 AND status IN ('draft','reconciled') 守卫＋事务内
    rebill_status_guard 守卫块核行（0 行命中 -> RAISE 回滚）。并发时序两侧皆封死：
    confirm 先提交 -> rebill 的守卫 UPDATE 撞 0 行 -> 守卫块拦截；rebill 先锁行 ->
    confirm 的守卫 UPDATE 阻塞至其提交后再判定（READ COMMITTED 重判 WHERE）。
    此前 UPDATE 无守卫且 confirmed 判定在事务外（billing_run.py:353-358/384-390
    实证），并发可把 confirmed 改回 reconciled。
  - OA 补录/欠费标记：仅 confirmed（同句守卫 RETURNING 核行）。

【OA 口径写死（设计 §0；ADR-0024 决定第 6 条）】零系统集成：单号纯手工登记，
  必填＝DDL billing_period_confirm_oa_check 同口径（本模块只锁非空）；格式提醒
  OA_TICKET_RE 归界面（admin/billing.html），服务端不校真伪、不做格式硬拦。

通道协议：本模块不建连接——各调用方传入带 .sql(script)->str 的执行器
（CLI＝scripts/usage_rollup.Psql；服务端＝app/billing_api 的通道），psql 通道
须带 ON_ERROR_STOP=1（守卫块 RAISE 后整体终止＝事务不达 COMMIT 即回滚）。
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re as _re
import uuid as _uuid

# -- 状态值域（0004 DDL billing_period_status_check 同源）--
STATUS_DRAFT = "draft"
STATUS_RECONCILED = "reconciled"
STATUS_CONFIRMED = "confirmed"
STATUS_WRITTEN_OFF = "written_off"
STATUS_VALUES = (STATUS_DRAFT, STATUS_RECONCILED, STATUS_CONFIRMED, STATUS_WRITTEN_OFF)
# 可重出状态（P0-6 rebill 守卫值域；confirmed/written_off 永久拒绝）
REBILLABLE_STATUSES = (STATUS_DRAFT, STATUS_RECONCILED)

# rebill 事务内守卫块 RAISE 标记：调用方据此把 psql 非零退出翻译成「拒绝」
# （CLI exit 2／API 422）而非环境失败（exit 1／503）
REBILL_GUARD_MARKER = "REBILL_STATUS_GUARD"

# API 面带留痕跃迁（confirm_with_event/register_oa_with_event）的守卫标记：
# UPDATE 未命中（不存在/状态不符/并发抢先）→ RAISE 回滚、事件不落，调用方出 422
BILL_STATE_GUARD_MARKER = "BILL_STATE_GUARD"

PERIOD_RE = _re.compile(r"^\d{4}-\d{2}$")

# OA 单号格式提醒（纯界面提醒；§0 口径写死，与 appkeys/grants_admin 同形同源）
OA_TICKET_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")


def q(s) -> str:
    """SQL 字面量安全引号（scripts/usage_rollup.q 同源同句；standard_conforming_strings=on
    下 \'\' 转义即充分）。共享模块自带一份——服务镜像无 scripts/，不反依赖。"""
    return "'" + str(s).replace("'", "''") + "'"


def returned_rows(out: str) -> list:
    """psql -t -A 的 RETURNING 行集（剔除 INSERT/UPDATE N 命令标签——0 行也有标签，
    不能当行集；billing_run._returned_rows 原样上移）。"""
    return [x for x in out.strip().splitlines()
            if x and not x.startswith(("UPDATE", "INSERT", "DELETE"))]


# ----------------------------------------------------------------------
# 账期解析（自然月，东八区月切；period_start＝当月首日，DDL PK 口径）
# ----------------------------------------------------------------------

def parse_period(s: str) -> tuple:
    """'YYYY-MM' -> (period_start, hi_exclusive, period_end)；非法即 ValueError
    （CLI 用法错误 exit 1／API 入参 400，消息同源）。"""
    if not PERIOD_RE.match(s or ""):
        raise ValueError("--period 须为 YYYY-MM（自然月），实得 %r" % s)
    y, m = int(s[:4]), int(s[5:7])
    lo = _dt.date(y, m, 1)
    end = _dt.date(y, m, calendar.monthrange(y, m)[1])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return lo, _dt.date(ny, nm, 1), end


# ----------------------------------------------------------------------
# 状态守卫/确认校验（事务内判定；SQL 同 billing_run 原句——行为零变化迁移）
# ----------------------------------------------------------------------

def confirm_period(sql_exec, period_start: str, oa_ticket: str) -> list:
    """确认跃迁 reconciled->confirmed（ADR-0024 决定第 6 条：单号纯登记必填）。

    事务内判定：UPDATE…WHERE period_start=<x> AND status='reconciled' RETURNING
    period_start——0 行（不存在/已确认/已核销/并发抢先）＝返回空表，调用方出
    422/exit 2；判定不留在事务外（P0-6）。调用方须先校验单号非空
    （DDL billing_period_confirm_oa_check 同口径）。"""
    out = sql_exec.sql(
        "UPDATE usage.billing_period SET status = 'confirmed', oa_ticket = %s,"
        " confirmed_at = now(), updated_at = now()"
        " WHERE period_start = %s AND status = 'reconciled' RETURNING period_start;"
        % (q(oa_ticket), q(period_start)))
    return returned_rows(out)


def register_oa(sql_exec, period_start: str, oa_ticket: str) -> list:
    """OA 单号补录/更正（仅 confirmed；＝CLI --register-oa 语义，纯登记不校真伪）。

    事务内判定同上：0 行＝返回空表（不存在或未确认）。"""
    out = sql_exec.sql(
        "UPDATE usage.billing_period SET oa_ticket = %s, updated_at = now()"
        " WHERE period_start = %s AND status = 'confirmed' RETURNING period_start;"
        % (q(oa_ticket), q(period_start)))
    return returned_rows(out)


def mark_overdue(sql_exec, period_start: str) -> list:
    """欠费逾期标记（只算不限：不做限流/停服/降配；ADR-0024 决定第 8 条）。

    仅 confirmed（欠费以确认后应收为前提）；0 行＝返回空表。"""
    out = sql_exec.sql(
        "UPDATE usage.billing_period SET overdue = true, overdue_marked_at = now(),"
        " updated_at = now() WHERE period_start = %s AND status = 'confirmed'"
        " RETURNING period_start;" % q(period_start))
    return returned_rows(out)


def rebill_status_guard(period_start: str) -> str:
    """rebill 事务内状态守卫块（P0-6）：守卫 UPDATE（AND status IN
    ('draft','reconciled')）之后、破坏性 DELETE 之前执行——

    - 守卫 UPDATE 命中（draft/reconciled）→ 行已置 reconciled → 守卫块静默放行；
    - 守卫 UPDATE 0 行（confirmed/written_off/不存在，含并发窗口内被抢先确认）
      → 行状态非 reconciled → RAISE 回滚，psql ON_ERROR_STOP 终止，COMMIT 不到达
      ＝整事务回滚，错误消息带 REBILL_STATUS_GUARD 标记供调用方翻译成拒绝。

    消息文本不含引号/百分号（RAISE 字面量直拼安全；period_start 已过
    parse_period 值域且经 q() 书写）。"""
    msg = (REBILL_GUARD_MARKER + "：账期 " + period_start
           + " 无可重出对象（状态非 draft/reconciled——确认/核销后账单底数冻结，"
             "修正走调整单，ADR-0024 决定第 4 条）；事务已回滚")
    return ("DO $rebill_guard$ BEGIN"
            " IF NOT EXISTS (SELECT 1 FROM usage.billing_period"
            " WHERE period_start = " + q(period_start) +
            " AND status IN ('draft','reconciled')) THEN"
            " RAISE EXCEPTION '%s', " + q(msg) + ";"
            " END IF; END $rebill_guard$;")


# ----------------------------------------------------------------------
# API 面跃迁＋admin_event 窄表留痕（§0 审计双落②：写库事务内同发；§3.7）
# ----------------------------------------------------------------------

def _lit(v) -> str:
    """SQL 字面量：None → NULL（窄表可空列），其余走 q()。"""
    return "NULL" if v is None else q(v)


def event_insert_sql(event: dict) -> str:
    """meta.admin_event 窄表插入句（0001 DDL：event_id/operator/action/target/
    result/oa_ticket/tenant_id/occurred_at；occurred_at 走缺省 now()）。

    event 键：event_id（缺省 uuid4 hex）/operator/action/target/result/
    oa_ticket/tenant_id（可空）。导出、denied 等独立语句场景直接 .sql() 执行
    （无状态变更动作无同事务对偶义务，§0 定稿说明①）。"""
    return ("INSERT INTO meta.admin_event (event_id, operator, action, target,"
            " result, oa_ticket, tenant_id) VALUES (%s, %s, %s, %s, %s, %s, %s);"
            % (_lit(event.get("event_id") or _uuid.uuid4().hex),
               _lit(event.get("operator") or ""),
               _lit(event.get("action") or ""),
               _lit(event.get("target")),
               _lit(event.get("result") or "ok"),
               _lit(event.get("oa_ticket")),
               _lit(event.get("tenant_id"))))


def _audited_op(update_sql: str, event_sql: str) -> str:
    """同事务脚本：BEGIN → DO 块（UPDATE→FOUND 判定→命中才 INSERT 事件）→ COMMIT。

    UPDATE 未命中（NOT FOUND）＝状态机守卫拦截 → RAISE（BILL_STATE_GUARD 标记）
    回滚整事务、事件不落；psql ON_ERROR_STOP 终止，调用方据 marker 出 422 并
    另行落 denied 留痕（拒绝零变更，无同事务对偶义务）。UPDATE 句与裸跃迁同一
    构造器（_confirm_sql/_register_sql，SET/WHERE 同句），仅 RETURNING 尾缀按
    通道形态取舍（DO 块内以 FOUND 判行；RETURNING 由裸路径供行集判定）。"""
    return ("BEGIN;\n"
            "DO $bill_state_op$ BEGIN\n" + update_sql + "\n"
            " IF NOT FOUND THEN RAISE EXCEPTION '" + BILL_STATE_GUARD_MARKER +
            "：账期状态跃迁未命中（状态机守卫拦截，事务回滚，事件不落）'; END IF;\n"
            + event_sql + "\n"
            "END $bill_state_op$;\n"
            "COMMIT;")


def _confirm_sql(period_start: str, oa_ticket: str, returning: bool) -> str:
    """确认跃迁句（reconciled→confirmed；确认必带单号 DDL CHECK 同口径）。"""
    return ("UPDATE usage.billing_period SET status = 'confirmed', oa_ticket = %s,"
            " confirmed_at = now(), updated_at = now()"
            " WHERE period_start = %s AND status = 'reconciled'%s"
            % (q(oa_ticket), q(period_start),
               " RETURNING period_start;" if returning else ";"))


def _register_sql(period_start: str, oa_ticket: str, returning: bool) -> str:
    """OA 补录句（仅 confirmed；纯登记不校真伪）。"""
    return ("UPDATE usage.billing_period SET oa_ticket = %s, updated_at = now()"
            " WHERE period_start = %s AND status = 'confirmed'%s"
            % (q(oa_ticket), q(period_start),
               " RETURNING period_start;" if returning else ";"))


def confirm_period(sql_exec, period_start: str, oa_ticket: str) -> list:
    """确认跃迁 reconciled→confirmed（裸路径＝CLI --confirm；ADR-0024 决定第 6 条）。

    事务内判定：UPDATE…WHERE period_start=<x> AND status='reconciled' RETURNING
    period_start——0 行（不存在/已确认/已核销/并发抢先）＝返回空表，调用方出
    422/exit 2；判定不留在事务外（P0-6）。调用方须先校验单号非空
    （DDL billing_period_confirm_oa_check 同口径）。"""
    return returned_rows(sql_exec.sql(_confirm_sql(period_start, oa_ticket, True)))


def register_oa(sql_exec, period_start: str, oa_ticket: str) -> list:
    """OA 单号补录/更正（裸路径＝CLI --register-oa；仅 confirmed）。

    事务内判定同上：0 行＝返回空表（不存在或未确认）。"""
    return returned_rows(sql_exec.sql(_register_sql(period_start, oa_ticket, True)))


def confirm_with_event(sql_exec, period_start: str, oa_ticket: str, event: dict) -> bool:
    """API 面：确认跃迁＋admin_event ok 留痕同事务（§3.7 bill_confirm）。

    命中＝True（跃迁与 ok 事件一并提交）；未命中＝False（BILL_STATE_GUARD
    回滚、事件不落——调用方出 422 并另行落 denied）。"""
    sql_exec.sql(_audited_op(_confirm_sql(period_start, oa_ticket, False),
                             event_insert_sql({**event, "result": "ok"})))
    return True


def register_oa_with_event(sql_exec, period_start: str, oa_ticket: str, event: dict) -> bool:
    """API 面：OA 补录＋留痕同事务（§3.7 bill_register_oa；语义同 register_oa）。"""
    sql_exec.sql(_audited_op(_register_sql(period_start, oa_ticket, False),
                             event_insert_sql({**event, "result": "ok"})))
    return True


def write_event(sql_exec, event: dict) -> None:
    """独立窄表事件（导出 bill_export／拒绝 denied——无状态变更动作，单语句）。"""
    sql_exec.sql(event_insert_sql(event))
