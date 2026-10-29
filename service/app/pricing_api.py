# -*- coding: utf-8 -*-
"""价目管理端点与服务层（管理功能群一期 B 节 · 设计 v1.1 §2 已拍板，按 §0 共用口径施工）。

端点组（均 /v1/admin/pricing/*；router 级挂 admin_api._admin_gate——原写方法过
auth_oidc.require_login，该挂点随 v0.1.1 登录清理摘除（ADR-0029 §7）；原
ROUTE_MIN_ROLE 域9 计费管理：读面 viewer 地板、写面 admin，矩阵同批退役）：
  GET  /v1/admin/pricing/items                     价目列表（分页 metering_api 先例＋展示态推算）
  GET  /v1/admin/pricing/items/{item_id}           价目详情（档位全览＋快照脚注）
  GET  /v1/admin/pricing/selector                  计价对象选择器（服务∪已发布代理合并命名空间；
                                                   选择器值＝endpoint 原值——P0-4）
  GET  /v1/admin/pricing/hit-preview?service_ref=  命中预演（P0-4 配套：选定 service_ref 对
                                                   最近一个完整账期 usage_daily 的命中行数报告）
  GET  /v1/admin/pricing/coverage?period=          priced 覆盖率报告（出账前只读统计；未计价
                                                   endpoint 归一清单；billing_run 引擎零改动）
  POST /v1/admin/pricing/items                     新建价目（三模型表单分流；preview=true＝预演
                                                   不落库：档位全览＋生效窗冲突检测）；admin
  POST /v1/admin/pricing/items/{item_id}/retire    停用按展示态分派（P1-⑧）；admin

【P0-4 口径（本模块单点）】计价引擎匹配键＝usage_daily.endpoint 精确匹配
（scripts/billing_run.py load_price），故 service_ref 落库＝endpoint 归一原值；
B 型代理的审计后缀 ?retention=on|off（proxy_executor annotate_endpoint）会裂键，
落库前经 normalize_service_ref 剥除。引擎侧归一匹配已随收口批（2026-10-21）落地：
load_price 匹配侧同源复用本函数（billing_state.py 先例 sys.path 引 service/），
on/off 后缀不再裂键；B_TYPE_CREATE_OPEN 随之翻闸＝True（§2.8「有条件延后」的
前提——引擎侧归一匹配落地前开闸＝静默 0 元少收回归——已兑现解除）。

【校验三不变量（完备约束归应用层，0004 头注既定口径）】首档下界＝0／区间连续
（下一档下界＝上一档上界）／末档上界空＝不封顶；DDL 只锁单行合法
（price_tier_bounds_check）与 UNIQUE(service_ref, effective_from)。生效窗互斥
检测在事务内执行＋pg_advisory_xact_lock（键＝归一 endpoint）串行化同服务并发
创建（P1-⑨——UNIQUE 只防同日起效，防不了重叠窗「检查—写入」竞态）。
生效起约束到月初（P1-⑦拍板推荐：价目按自然月整月生效，与账期粒度对齐）。
item_id＝pi_＋16 位随机小写 hex（secrets.token_hex(8)，对齐 appkey 先例，P2）。

【通道】价目真源＝pg usage schema（0004，天生 pg-only，不受 META_WRITER 双模
管辖）；通道形态如实沿 meta_writer：宿主 docker compose exec／服务容器内直连
（META_DB_* 五键，前置卡协议同源）／测试注入 PRICING_PSQL_BASE（优先级最高，
协议同 USAGE_PSQL_BASE）。库不可达＝读面降级态响应＋写面 503，不回落 yaml
（价目无 yaml 形态，不为它发明）。

【审计双落】①管线八元组（endpoint＝admin:<动作名>，复用 admin_api
._audit_admin_action 同形态）；②同事务 meta.admin_event 窄表（result=ok）；
被拒（校验拒绝）＝管线事件 result=denied＋窄表 result=denied 尽力而为落。

分层纪律（同 grants_admin/metering_api）：校验/归一/SQL 构造全在本模块纯函数
（宿主机无 FastAPI 可单测）；HTTP 编排只在端点区（_HAS_FASTAPI 守卫）。域界
（0be2351 原口径，收口批按任务书解除两项）：billing_run
引擎匹配与 main.py 挂载已由收口批（2026-10-21）接线；portal/ 与 admin/tenants*
仍归各批。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import subprocess
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── 域常量 ──────────────────────────────────────────────────────────────
MODELS = ("per_call", "monthly_package", "tiered")
MODEL_LABELS = {"per_call": "按次", "monthly_package": "套餐", "tiered": "阶梯"}

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# numeric(14,6)：整数 8 位＋小数 6 位；表单校验 ≥0、6 位小数上限（§2.8）
PRICE_RE = re.compile(r"^(0|[1-9][0-9]{0,7})(\.[0-9]{1,6})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")
# endpoint 原值安全字符集（A 型 service:前缀／B 型 /proxy 路由＋审计后缀；注入面收口）
SERVICE_REF_RE = re.compile(r"^[A-Za-z0-9:/._?&=\-]{1,256}$")
RETENTION_SUFFIX_RE = re.compile(r"\?retention=(on|off)$")

ITEM_ID_PREFIX = "pi_"
B_TYPE_CREATE_OPEN = True    # §2.8 延后条件已兑现：引擎侧归一匹配落地（billing_run.load_price 同源复用 normalize_service_ref）——收口批 2026-10-21 翻闸
B_TYPE_NOTE = "整路由代理：选择器值＝归一形 endpoint（审计留存后缀由引擎同源剥除，不再裂键）"
MSG_PG_DOWN = ("计费数据暂不可用：计费库未启用或未响应（compose.metadata.yml，项目名 "
               "oneserve-meta）。价目为空期间出账走空价目试运行口径，不受影响。")
FOOTNOTE_SNAPSHOT = "已出账单的成交单价存在 bill_line.unit_price 快照，与本表后续改动无关"
CALIBER_NOTE = "价目变更不追溯历史账单——已出账单按出账时点成交单价快照固化（ADR-0024 决定第 4 条）"

# 展示态（按今日推算，不入库——§2.6）
STATE_IN_WINDOW = "in_window"
STATE_PENDING = "pending"
STATE_EXPIRED = "expired"
STATE_LABELS = {STATE_IN_WINDOW: "在生效窗内", STATE_PENDING: "未生效", STATE_EXPIRED: "已过期"}

TZ_CN = _dt.timezone(_dt.timedelta(hours=8))

# 分页（零第二套分页法：metering_api/usage_report 先例同源）
from app.usage_report import parse_paging   # noqa: E402  （page≥1／page_size∈{20,50,100}）


class PricingValidationError(ValueError):
    """价目校验拒绝（人话 message 直出 422）。"""


class PricingDbError(RuntimeError):
    """计费库通道错误（含 psql stderr 尾部；读面降级/写面 503 的唯一来源）。"""


# ── P0-4 单点：service_ref 归一 ──────────────────────────────────────────
def normalize_service_ref(ref: str) -> str:
    """剥 B 型 ?retention=on|off 审计后缀（幂等），余者原值保留。

    引擎匹配键＝usage_daily.endpoint 精确匹配（billing_run load_price）；审计
    后缀（proxy_executor.annotate_endpoint）会把同一代理裂成 on/off 两键——落库
    与引擎侧同用本函数（同源单点，P0-4）。A 型值不含该后缀＝原样返回。
    """
    out = str(ref or "").strip()
    while True:
        stripped = RETENTION_SUFFIX_RE.sub("", out)
        if stripped == out:
            return out
        out = stripped


def new_item_id() -> str:
    """item_id＝pi_＋16 位随机小写 hex（P2 拍板口径，对齐 appkey 生成先例）。"""
    return ITEM_ID_PREFIX + secrets.token_hex(8)


def today_cn() -> _dt.date:
    """业务「今日」＝东八区当日（展示态推算与停用分派共用单点）。"""
    return _dt.datetime.now(TZ_CN).date()


def display_state(effective_from: _dt.date, effective_to: _dt.date | None,
                  today: _dt.date | None = None) -> str:
    """展示态三态推算（存储只有 effective_from/to，展示态不入库——§2.6）。"""
    today = today or today_cn()
    if effective_from > today:
        return STATE_PENDING
    if effective_to is not None and effective_to < today:
        return STATE_EXPIRED
    return STATE_IN_WINDOW


def _parse_date(s, field: str) -> _dt.date:
    if not isinstance(s, str) or not DATE_RE.match(s.strip()):
        raise PricingValidationError(f"{field} 须为 YYYY-MM-DD 日期（实得 {s!r}）")
    try:
        return _dt.date.fromisoformat(s.strip())
    except ValueError as exc:
        raise PricingValidationError(f"{field} 不是合法日期：{s!r}") from exc


def _parse_price(v, field: str) -> str:
    """单价校验：≥0、≤6 位小数、numeric(14,6) 值域内；返回规范化文本（落库原样）。"""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise PricingValidationError(f"{field} 须为非负数字（实得 {v!r}）")
    s = str(v).strip()
    if not PRICE_RE.match(s):
        raise PricingValidationError(
            f"{field} 须为 ≥0 且最多 6 位小数的数值、整数部分 ≤8 位（实得 {s!r}）")
    try:
        d = Decimal(s)
    except InvalidOperation as exc:
        raise PricingValidationError(f"{field} 不是合法数值：{s!r}") from exc
    if d > Decimal("99999999.999999"):
        raise PricingValidationError(f"{field} 超出 numeric(14,6) 值域上限 99999999.999999")
    return format(d.quantize(Decimal("0.000001")), "f")


def fmt_range(lower: int, upper: int | None) -> str:
    return f"[{lower},{upper})" if upper is not None else f"[{lower},∞)"


def _validate_tier_rows(rows, model: str) -> list:
    """阶梯行式校验（区间三不变量）→ [(lower:int, upper:int|None, price:str)] 升序。

    人话报因对齐 §2.3 示例：「档位 [0,5000) 与 [10000,∞) 不连续：缺 [5000,10000)」。
    """
    if not isinstance(rows, list) or len(rows) < 2:
        raise PricingValidationError("阶梯价目至少需要两档（单档请选「按次」模型）")
    parsed = []
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise PricingValidationError(f"第 {i} 档须为对象（lowerBound/upperBound/unitPrice）")
        lower = row.get("lower_bound", row.get("lowerBound"))
        upper = row.get("upper_bound", row.get("upperBound"))
        price = row.get("unit_price", row.get("unitPrice"))
        if isinstance(lower, bool) or not isinstance(lower, int) or lower < 0:
            raise PricingValidationError(f"第 {i} 档下界（含）须为 ≥0 整数（实得 {lower!r}）")
        if upper is not None and (isinstance(upper, bool) or not isinstance(upper, int)):
            raise PricingValidationError(f"第 {i} 档上界（不含）须为整数或留空＝不封顶（实得 {upper!r}）")
        if upper is not None and upper <= lower:
            raise PricingValidationError(
                f"第 {i} 档区间非法：上界须大于下界（{fmt_range(lower, upper)}）")
        parsed.append((lower, upper, _parse_price(price, f"第 {i} 档单价")))
    parsed.sort(key=lambda t: t[0])
    if parsed[0][0] != 0:
        raise PricingValidationError(f"首档下界须为 0（实得 {parsed[0][0]}）")
    for i in range(len(parsed) - 1):
        lower, upper, _ = parsed[i]
        nxt = parsed[i + 1]
        if upper is None:
            raise PricingValidationError(
                f"仅末档可留空上界（不封顶）：档 {fmt_range(lower, None)} 后还有后续档")
        if nxt[0] != upper:
            hole = fmt_range(upper, nxt[0])
            raise PricingValidationError(
                f"档位 {fmt_range(lower, upper)} 与 {fmt_range(nxt[0], nxt[1])} 不连续：缺 {hole}")
    if parsed[-1][1] is not None:
        raise PricingValidationError(
            f"末档上界须留空（不封顶，DDL：price_tier.upper_bound NULL）；"
            f"封顶区间请扩为末档 {fmt_range(parsed[-1][0], None)}")
    return parsed


def derive_tiers(body: dict) -> list:
    """三模型表单分流（§2.2 第二步）：按次单档／套餐两档／阶梯行式 → 统一区间行。

    套餐＝[0,N)@0（额度内 0 价档）＋[N,∞)@P（超额单价档）——ADR-0024 决定第 2 条。
    """
    model = body.get("model")
    if model not in MODELS:
        raise PricingValidationError(
            f"计价模型须为 {'/'.join(MODELS)}（按次/套餐/阶梯；实得 {model!r}）")
    if model == "per_call":
        price = _parse_price(body.get("unit_price", body.get("unitPrice")), "单价")
        return [(0, None, price)]
    if model == "monthly_package":
        quota = body.get("included_quota", body.get("includedQuota"))
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 1:
            raise PricingValidationError(f"月含额度 N 须为 ≥1 整数（实得 {quota!r}）")
        over = _parse_price(body.get("overage_price", body.get("overagePrice")), "超额单价")
        return [(0, quota, format(Decimal(0), "f")), (quota, None, over)]
    return _validate_tier_rows(body.get("tiers"), model)


def validate_create(body: dict) -> dict:
    """新建校验（P0-4 归一＋三模型分流＋月初约束）→ 规范化 dict（含派生 tiers）。

    校验拒绝统一 PricingValidationError（端点区转 422 人话信封）；DB 侧重叠窗
    校验在事务内（P1-⑨），此处不做。
    """
    if not isinstance(body, dict):
        raise PricingValidationError("请求体须为 JSON 对象")
    raw_ref = body.get("service_ref", body.get("serviceRef"))
    if not isinstance(raw_ref, str) or not SERVICE_REF_RE.match(raw_ref.strip()):
        raise PricingValidationError(
            "计价对象 endpoint 原值非法：仅允许字母数字与 :/._?&=- 且长度 1..256")
    ref = normalize_service_ref(raw_ref)
    if not ref:
        raise PricingValidationError("计价对象 endpoint 原值不能为空")
    kind = str(body.get("target_kind", body.get("targetKind")) or "service").strip()
    raw_is_annotated = bool(RETENTION_SUFFIX_RE.search(raw_ref.strip()))
    if (kind == "proxy" or raw_is_annotated) and not B_TYPE_CREATE_OPEN:
        raise PricingValidationError(f"B 型（整路由代理）价目暂不开放创建：{B_TYPE_NOTE}")
    currency = str(body.get("currency") or "CNY").strip().upper()
    if not CURRENCY_RE.match(currency):
        raise PricingValidationError(f"币种须为三位大写字母（ISO 4217；实得 {currency!r}）")
    eff_from = _parse_date(body.get("effective_from", body.get("effectiveFrom")), "生效起")
    if eff_from.day != 1:
        raise PricingValidationError(
            f"生效起须为每月 1 日（价目按自然月整月生效，与账期粒度对齐——拍板口径 P1-⑦；"
            f"实得 {eff_from.isoformat()}）")
    raw_to = body.get("effective_to", body.get("effectiveTo"))
    eff_to = None if raw_to in (None, "") else _parse_date(raw_to, "生效止")
    if eff_to is not None and eff_to < eff_from:
        raise PricingValidationError(
            f"生效止（{eff_to.isoformat()}）不得早于生效起（{eff_from.isoformat()}）")
    remark = body.get("remark")
    if remark is None:
        remark = ""
    remark = re.sub(r"[\x00-\x1f\x7f]", " ", str(remark)).strip()
    if len(remark) > 200:
        raise PricingValidationError("备注最长 200 字")
    return {"service_ref": ref, "target_kind": kind if kind in ("service", "proxy") else "service",
            "model": body.get("model"), "currency": currency,
            "effective_from": eff_from, "effective_to": eff_to,
            "remark": remark, "tiers": derive_tiers(body)}


def tier_summary(tiers: list) -> str:
    """档位摘要（列表列）：如「[0,∞)@0.500000」或「[0,10000)@0 ＋ [10000,∞)@0.800000」。"""
    parts = []
    for lower, upper, price in tiers:
        p = Decimal(str(price)).quantize(Decimal("0.000001"))
        parts.append(f"{fmt_range(int(lower), None if upper is None else int(upper))}@{format(p, 'f')}")
    return " ＋ ".join(parts)


# ── pg 通道（宿主 docker compose／容器内直连／测试注入，协议同 meta_writer） ──
PSQL_BASE_DEFAULT = ("docker compose -f compose.metadata.yml -p oneserve-meta exec -T "
                     "metadata-db psql -U meta_app -d oneserve_meta "
                     "-v ON_ERROR_STOP=1 -A -t -P footer=off")
ENV_PSQL_BASE = "PRICING_PSQL_BASE"   # 测试注入钩子（优先级最高）


def _psql_base_from_env() -> list:
    """META_DB_* 五键 → 容器内直连 psql 数组（协议同 meta_writer._psql_base_from_env）。"""
    host = (os.environ.get("META_DB_HOST") or "").strip()
    if not host:
        return []
    port = (os.environ.get("META_DB_PORT") or "").strip() or "5432"
    user = (os.environ.get("META_DB_USER") or "").strip() or "meta_app"
    db = (os.environ.get("META_DB_DATABASE") or "").strip() or "oneserve_meta"
    return ["psql", "-h", host, "-p", port, "-U", user, "-d", db,
            "-v", "ON_ERROR_STOP=1", "-A", "-t", "-P", "footer=off"]


def _billing_env_base() -> list:
    """域钩子回落：USAGE_PSQL_BASE > META_MIGRATE_PSQL_BASE（billing CLI 同协议）。"""
    for name in ("USAGE_PSQL_BASE", "META_MIGRATE_PSQL_BASE"):
        custom = (os.environ.get(name) or "").strip()
        if custom:
            return custom.split()
    return []


def _lit(v) -> str:
    """SQL 字面量（None→NULL；单引号加倍；conforming strings 下安全）。"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


class PricingDbChannel:
    """计费库通道（meta_app 角色；psql 每脚本一会话一事务——BEGIN…COMMIT 整体成败）。"""

    def __init__(self, base: list | None = None, timeout_s: int | None = None):
        custom = (os.environ.get(ENV_PSQL_BASE) or "").strip()
        self.base = list(base) if base else (custom.split() if custom
                                             else (_billing_env_base()
                                                   or _psql_base_from_env()
                                                   or PSQL_BASE_DEFAULT.split()))
        self.timeout_s = int(timeout_s or os.environ.get("PRICING_TIMEOUT_S") or "60")
        pw = (os.environ.get("META_DB_PASSWORD") or "").strip()
        self.env = {**os.environ, "PGPASSWORD": pw} if pw else None

    def script(self, sql: str) -> str:
        p = subprocess.run(self.base, input=sql.encode("utf-8"), capture_output=True,
                           cwd=str(REPO_ROOT), timeout=self.timeout_s, env=self.env)
        if p.returncode != 0:
            raise PricingDbError(
                "计费库事务失败 rc=%d：\n%s" % (p.returncode,
                                               p.stderr.decode("utf-8", "replace")[-1200:]))
        return p.stdout.decode("utf-8", "replace")

    def rows(self, query: str) -> list:
        """查询 → 行字典列表（jsonb_agg 单行渲染；数值/日期列由调用方 ::text 保真）。"""
        out = self.script("SET TimeZone TO 'UTC';\n"
                          f"SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM ({query}) t;").strip()
        if not out:
            return []
        last = out.splitlines()[-1]
        data = json.loads(last)
        return data if isinstance(data, list) else []


def db_reachable(channel: PricingDbChannel | None = None) -> bool:
    """计费库可达性轻探（SELECT 1；读面降级判定）。"""
    try:
        (channel or PricingDbChannel()).script("SELECT 1;")
        return True
    except Exception:  # noqa: BLE001
        return False


def _prev_complete_month(today: _dt.date | None = None) -> tuple:
    """最近一个完整账期 → (首日, 次月首日) 半开窗（东八区今日推算）。"""
    today = today or today_cn()
    first_this = today.replace(day=1)
    last_prev = first_this - _dt.timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, first_this

# ── 读面（列表/详情/选择器/命中预演/覆盖率——全部只读） ────────────────────
def _effective_window_sql(eff_from_col: str, eff_to_col: str, lo: str, hi_ex: str) -> str:
    """引擎同源生效窗谓词（billing_run.load_price 原式：from <= 期末 AND (to IS NULL OR to >= 期首)）。"""
    return (f"{eff_from_col} <= {hi_ex} AND ({eff_to_col} IS NULL OR {eff_to_col} >= {lo})")


def list_items(channel: PricingDbChannel, page: int = 1, page_size: int = 50,
               service: str | None = None, state: str | None = None,
               today: _dt.date | None = None) -> dict:
    """价目列表（分页信封 total/page/pageSize/items）＋展示态推算＋档位摘要。"""
    page, page_size = parse_paging(page, page_size)
    today = today or today_cn()
    where = "TRUE"
    if service:
        where += " AND service_ref = " + _lit(normalize_service_ref(service))
    total_rows = channel.rows(
        "SELECT count(*)::text AS n FROM usage.price_item WHERE " + where)
    total = int(total_rows[0]["n"]) if total_rows else 0
    rows = channel.rows(
        "SELECT item_id::text AS item_id, service_ref::text AS service_ref, model::text AS model,"
        " currency::text AS currency, effective_from::text AS effective_from,"
        " effective_to::text AS effective_to, remark::text AS remark,"
        " created_at::text AS created_at, updated_at::text AS updated_at"
        f" FROM usage.price_item WHERE {where}"
        " ORDER BY service_ref, effective_from DESC"
        f" LIMIT {int(page_size)} OFFSET {int((page - 1) * page_size)}")
    ids = [r["item_id"] for r in rows]
    tiers_by_item = {}
    if ids:
        id_list = ", ".join(_lit(i) for i in ids)
        for t in channel.rows(
                "SELECT item_id::text AS item_id, lower_bound::text AS lower_bound,"
                " upper_bound::text AS upper_bound, unit_price::text AS unit_price"
                f" FROM usage.price_tier WHERE item_id IN ({id_list})"
                " ORDER BY item_id, lower_bound"):
            tiers_by_item.setdefault(t["item_id"], []).append(
                (int(t["lower_bound"]),
                 None if t["upper_bound"] in (None, "") else int(t["upper_bound"]),
                 t["unit_price"]))
    items = []
    for r in rows:
        eff_to = None if r["effective_to"] in (None, "") else _dt.date.fromisoformat(r["effective_to"])
        st = display_state(_dt.date.fromisoformat(r["effective_from"]), eff_to, today)
        items.append({
            "itemId": r["item_id"], "serviceRef": r["service_ref"],
            "model": r["model"], "modelLabel": MODEL_LABELS.get(r["model"], r["model"]),
            "currency": r["currency"], "effectiveFrom": r["effective_from"],
            "effectiveTo": r["effective_to"] or None, "remark": r["remark"] or "",
            "state": st, "stateLabel": STATE_LABELS[st],
            "tierSummary": tier_summary(tiers_by_item.get(r["item_id"], [])),
            "createdAt": r["created_at"], "updatedAt": r["updated_at"],
        })
    if state:
        items = [i for i in items if i["state"] == state]
    return {"status": "ok", "total": total, "page": page, "pageSize": page_size,
            "items": items, "today": today.isoformat(), "states": dict(STATE_LABELS)}


def get_item(channel: PricingDbChannel, item_id: str):
    """价目详情（档位全览＋历史账脚注）；不存在返回 None（端点区转 404）。"""
    if not re.fullmatch(r"pi_[0-9a-f]{16}", str(item_id or "")):
        return None
    rows = channel.rows(
        "SELECT item_id::text AS item_id, service_ref::text AS service_ref, model::text AS model,"
        " currency::text AS currency, effective_from::text AS effective_from,"
        " effective_to::text AS effective_to, remark::text AS remark,"
        " created_at::text AS created_at, updated_at::text AS updated_at"
        " FROM usage.price_item WHERE item_id = " + _lit(item_id))
    if not rows:
        return None
    r = rows[0]
    tiers = [(int(t["lower_bound"]),
              None if t["upper_bound"] in (None, "") else int(t["upper_bound"]),
              t["unit_price"])
             for t in channel.rows(
                 "SELECT lower_bound::text AS lower_bound, upper_bound::text AS upper_bound,"
                 " unit_price::text AS unit_price FROM usage.price_tier WHERE item_id = "
                 + _lit(item_id) + " ORDER BY lower_bound")]
    eff_to = None if r["effective_to"] in (None, "") else _dt.date.fromisoformat(r["effective_to"])
    st = display_state(_dt.date.fromisoformat(r["effective_from"]), eff_to)
    return {"item": {"itemId": r["item_id"], "serviceRef": r["service_ref"],
                     "model": r["model"], "modelLabel": MODEL_LABELS.get(r["model"], r["model"]),
                     "currency": r["currency"], "effectiveFrom": r["effective_from"],
                     "effectiveTo": r["effective_to"] or None, "remark": r["remark"] or "",
                     "state": st, "stateLabel": STATE_LABELS[st],
                     "createdAt": r["created_at"], "updatedAt": r["updated_at"]},
            "tiers": [{"lowerBound": lo, "upperBound": hi, "unitPrice": p,
                       "range": fmt_range(lo, hi)} for lo, hi, p in tiers],
            "tierSummary": tier_summary(tiers), "footnote": FOOTNOTE_SNAPSHOT}


def build_selector(services: dict | None) -> dict:
    """计价对象选择器（服务∪已发布代理合并命名空间；纯函数可单测）。

    选择器值＝endpoint 原值（P0-4）：A 型点查＝service:/v1/query/{sid}、带路由
    专用端点＝service:{route}（metering_api._service_names 同一映射口径）；
    B 型代理＝service:{route} 归一形（剥审计后缀前的基形）。代理行带 kind 徽标
    ＋「有条件延后」角标（§2.8）。
    """
    merged = dict(services or {})
    try:
        from app import ops_config as _opsc
        for pid, svc in _opsc.proxy_service_map().items():
            merged.setdefault(pid, svc)
    except Exception:  # noqa: BLE001 —— 登记表缺席时选择器降级为已注册服务集
        pass
    items = []
    for sid, entry in sorted(merged.items()):
        entry = entry or {}
        route = str(entry.get("route") or "")
        kind = "proxy" if entry.get("kind") == "proxy" else "service"
        endpoint = f"service:{route}" if route else f"service:/v1/query/{sid}"
        items.append({"id": str(sid), "name": str(entry.get("name") or sid), "kind": kind,
                      "endpoint": endpoint,
                      "note": B_TYPE_NOTE if kind == "proxy" else ""})
    return {"status": "ok", "items": items,
            "caliber": "选择器值＝计价引擎真匹配键＝usage_daily.endpoint 原值（B 型为剥 ?retention= 归一形）",
            "bTypeOpen": B_TYPE_CREATE_OPEN}


def hit_preview(channel: PricingDbChannel, service_ref: str, unhit_limit: int = 20) -> dict:
    """命中预演（P0-4 配套）：选定 service_ref 对最近一个完整账期 usage_daily 的命中行数报告。

    hit_rows/calls/success＝endpoint 归一后精确匹配的日行数与量；unhitTop＝窗内
    未覆盖 endpoint 按调用量倒序样本（判断「选错了对象」）。全部只读。
    """
    ref = normalize_service_ref(service_ref)
    if not ref:
        raise PricingValidationError("service_ref 不能为空")
    lo, hi_ex = _prev_complete_month()
    agg = channel.rows(
        "SELECT count(*)::text AS endpoints, coalesce(sum(calls),0)::text AS calls"
        " FROM usage.usage_daily WHERE day >= " + _lit(lo.isoformat())
        + " AND day < " + _lit(hi_ex.isoformat()))
    total_endpoints = int(agg[0]["endpoints"]) if agg else 0
    window_calls = int(agg[0]["calls"]) if agg else 0
    hit = channel.rows(
        "SELECT count(*)::text AS hit_rows, coalesce(sum(calls),0)::text AS calls,"
        " coalesce(sum(success),0)::text AS success"
        " FROM usage.usage_daily WHERE day >= " + _lit(lo.isoformat())
        + " AND day < " + _lit(hi_ex.isoformat())
        + " AND endpoint = " + _lit(ref))
    hit_rows = int(hit[0]["hit_rows"]) if hit else 0
    unhit = channel.rows(
        "SELECT u.endpoint::text AS endpoint, sum(u.calls)::text AS calls"
        " FROM usage.usage_daily u WHERE u.day >= " + _lit(lo.isoformat())
        + " AND u.day < " + _lit(hi_ex.isoformat())
        + " AND u.endpoint <> " + _lit(ref)
        + " GROUP BY u.endpoint ORDER BY sum(u.calls) DESC, u.endpoint LIMIT " + str(int(unhit_limit)))
    return {"status": "ok" if total_endpoints else "empty",
            "message": None if total_endpoints else
            "最近一个完整账期 usage_daily 无数据（空价目试运行不受影响）；可先起栈灌数再预演",
            "serviceRef": ref, "period": lo.strftime("%Y-%m"),
            "windowFrom": lo.isoformat(), "windowToExclusive": hi_ex.isoformat(),
            "distinctEndpoints": total_endpoints, "windowCalls": window_calls,
            "hitRows": hit_rows, "hitCalls": int(hit[0]["calls"]) if hit else 0,
            "hitSuccess": int(hit[0]["success"]) if hit else 0,
            "willHit": hit_rows > 0,
            "unhitTop": [{"endpoint": u["endpoint"], "calls": int(u["calls"])} for u in unhit],
            "caliber": "命中判据＝usage_daily.endpoint 对归一 service_ref 精确匹配（引擎同源）"}


def coverage_report(channel: PricingDbChannel, period: str | None = None, limit: int = 50) -> dict:
    """priced 覆盖率报告（出账前只读统计；billing_run 引擎零改动）。

    覆盖判据＝引擎同源窗谓词（load_price 原式）：存在 price_item 使
    service_ref＝endpoint 且生效窗罩住账期。未覆盖 endpoint 归一清单按调用量
    倒序（出账将 priced=false 0 元不计应收——空价目口径）。
    """
    if period:
        if not isinstance(period, str) or not PERIOD_RE.match(period.strip()):
            raise PricingValidationError("period 须为 YYYY-MM（账期＝自然月首日）")
        y, m = int(period[:4]), int(period[5:7])
        lo = _dt.date(y, m, 1)
        hi_ex = _dt.date(y + (1 if m == 12 else 0), (m % 12) + 1, 1)
    else:
        lo, hi_ex = _prev_complete_month()
    lo_s, hi_s = _lit(lo.isoformat()), _lit(hi_ex.isoformat())
    rows = channel.rows(
        "SELECT a.endpoint::text AS endpoint, a.calls::text AS calls, a.success::text AS success,"
        " (p.item_id IS NOT NULL) AS covered, coalesce(p.item_id::text,'') AS item_id,"
        " coalesce(p.model::text,'') AS model"
        " FROM (SELECT endpoint, sum(calls) AS calls, sum(success) AS success"
        "       FROM usage.usage_daily WHERE day >= " + lo_s + " AND day < " + hi_s
        + "     GROUP BY endpoint) a"
        " LEFT JOIN usage.price_item p ON p.service_ref = a.endpoint AND "
        + _effective_window_sql("p.effective_from", "p.effective_to", lo_s, hi_s)
        + " ORDER BY a.calls DESC, a.endpoint")
    covered = [r for r in rows if r["covered"]]
    uncovered = [r for r in rows if not r["covered"]]
    total_calls = sum(int(r["calls"]) for r in rows)
    covered_calls = sum(int(r["calls"]) for r in covered)
    return {"status": "ok" if rows else "empty",
            "message": None if rows else "该账期 usage_daily 无数据（空价目试运行不受影响）",
            "period": lo.strftime("%Y-%m"), "windowFrom": lo.isoformat(),
            "windowToExclusive": hi_ex.isoformat(),
            "totalEndpoints": len(rows), "coveredEndpoints": len(covered),
            "uncoveredEndpoints": len(uncovered),
            "coverageRatio": round(covered_calls / total_calls, 6) if total_calls else None,
            "covered": [{"endpoint": r["endpoint"], "itemId": r["item_id"],
                         "model": r["model"], "calls": int(r["calls"])}
                        for r in covered[:limit]],
            "uncovered": [{"endpoint": r["endpoint"], "calls": int(r["calls"]),
                           "success": int(r["success"])}
                          for r in uncovered[:limit]],
            "caliber": ("覆盖判据＝引擎同源生效窗谓词（billing_run.load_price 原式）；"
                        "未覆盖 endpoint 出账按空价目口径 priced=false 0 元不计应收")}

# ── 写面（新建/停用——单事务＋advisory lock＋事务内互斥＋审计窄表同发） ────
def _admin_event_sql(event_id: str, operator: str, action: str, target: str,
                     result: str = "ok") -> str:
    return ("INSERT INTO meta.admin_event (event_id, operator, action, target, result, occurred_at)"
            " VALUES (" + ", ".join(_lit(x) for x in
                                    (event_id, operator, action, target, result))
            + ", now());")


def _overlap_guard_sql(prep: dict) -> str:
    """事务内生效窗互斥（P1-⑨）：命中重叠即 RAISE 人话（消息随 psql stderr 回传）。"""
    ref = _lit(prep["service_ref"])
    to_lit = _lit(prep["effective_to"].isoformat() if prep["effective_to"] else "infinity")
    from_lit = _lit(prep["effective_from"].isoformat())
    return ("DO $$ DECLARE bad record; BEGIN"
            " SELECT item_id, effective_from, effective_to INTO bad FROM usage.price_item"
            " WHERE service_ref = " + ref +
            "   AND effective_from <= " + to_lit +
            "   AND COALESCE(effective_to, DATE 'infinity') >= " + from_lit +
            " ORDER BY effective_from LIMIT 1;"
            " IF FOUND THEN RAISE EXCEPTION"
            " '服务 % 已有生效区间与之重叠的价目（item_id=%，区间 %~%）——请改生效起或先停用既有条目',"
            " " + ref + ", bad.item_id, bad.effective_from,"
            " coalesce(bad.effective_to::text,'长期');"
            " END IF; END $$;")


def build_create_script(prep: dict, item_id: str, operator: str) -> str:
    """新建单事务脚本：advisory lock＋互斥检查＋item/tier/admin_event＋COMMIT。"""
    vals_item = ", ".join(_lit(v) for v in (
        item_id, prep["service_ref"], prep["model"], prep["currency"],
        prep["effective_from"].isoformat(),
        prep["effective_to"].isoformat() if prep["effective_to"] else None,
        prep["remark"] or None))
    tier_vals = ",\n  ".join(
        "(" + _lit(item_id) + ", %d, %s, %s)" % (int(lo), _lit(hi), _lit(price))
        for lo, hi, price in prep["tiers"])
    return ("BEGIN;\n"
            "SELECT pg_advisory_xact_lock(hashtext(" + _lit(prep["service_ref"]) + "));\n"
            + _overlap_guard_sql(prep) + "\n"
            "INSERT INTO usage.price_item (item_id, service_ref, model, currency,"
            " effective_from, effective_to, remark) VALUES (" + vals_item + ");\n"
            "INSERT INTO usage.price_tier (item_id, lower_bound, upper_bound, unit_price) VALUES\n  "
            + tier_vals + ";\n"
            + _admin_event_sql(uuid.uuid4().hex, operator, "price_create", item_id)
            + "\nCOMMIT;\n")


def create_item(channel: PricingDbChannel, body: dict, operator: str) -> dict:
    """新建价目（校验→单事务落库＋审计窄表同发）；校验拒绝由端点区转 422。"""
    prep = validate_create(body)
    item_id = new_item_id()
    channel.script(build_create_script(prep, item_id, operator))
    detail = get_item(channel, item_id)
    return {"ok": True, "itemId": item_id, "item": (detail or {}).get("item"),
            "tiers": (detail or {}).get("tiers"),
            "note": "价目变更不追溯历史账单（常驻口径）；已出账单按成交单价快照固化"}


def preview_item(channel: PricingDbChannel, body: dict) -> dict:
    """新建预演（两段式确认的第一段）：校验＋派生档位全览＋重叠窗冲突检测，零落库。"""
    prep = validate_create(body)
    to_lit = _lit(prep["effective_to"].isoformat() if prep["effective_to"] else "infinity")
    from_lit = _lit(prep["effective_from"].isoformat())
    conflict = channel.rows(
        "SELECT item_id::text AS item_id, effective_from::text AS effective_from,"
        " effective_to::text AS effective_to FROM usage.price_item"
        " WHERE service_ref = " + _lit(prep["service_ref"])
        + "   AND effective_from <= " + to_lit
        + "   AND COALESCE(effective_to, DATE 'infinity') >= " + from_lit
        + " ORDER BY effective_from")
    return {"ok": True, "preview": True,
            "serviceRef": prep["service_ref"], "model": prep["model"],
            "modelLabel": MODEL_LABELS.get(prep["model"], prep["model"]),
            "currency": prep["currency"], "effectiveFrom": prep["effective_from"].isoformat(),
            "effectiveTo": prep["effective_to"].isoformat() if prep["effective_to"] else None,
            "tiers": [{"lowerBound": lo, "upperBound": hi, "unitPrice": p,
                       "range": fmt_range(lo, hi)} for lo, hi, p in prep["tiers"]],
            "tierSummary": tier_summary(prep["tiers"]),
            "conflicts": [{"itemId": c["item_id"], "effectiveFrom": c["effective_from"],
                           "effectiveTo": c["effective_to"] or None} for c in conflict],
            "conflictMessage": ("无重叠：可将该生效窗落库" if not conflict else
                                "存在生效窗重叠（见 conflicts），提交将被事务内互斥拒绝"),
            "priceItemRow": {"item_id": "(新建时生成 pi_xxx)",
                             "service_ref": prep["service_ref"], "model": prep["model"],
                             "currency": prep["currency"],
                             "effective_from": prep["effective_from"].isoformat(),
                             "effective_to": prep["effective_to"].isoformat() if prep["effective_to"] else None,
                             "remark": prep["remark"]}}


RETIRE_GUARD_SQL = (
    "DO $$ DECLARE r record; today date := {today}::date; BEGIN"
    " SELECT effective_from, effective_to INTO r FROM usage.price_item"
    " WHERE item_id = {item} FOR UPDATE;"
    " IF NOT FOUND THEN RAISE EXCEPTION '价目 % 不存在（可能已撤回）', {item}; END IF;"
    " IF r.effective_from > today THEN"
    "   DELETE FROM usage.price_tier WHERE item_id = {item};"
    "   DELETE FROM usage.price_item WHERE item_id = {item};"
    " ELSE"
    "   IF r.effective_to IS NOT NULL AND r.effective_to < today THEN"
    "     RAISE EXCEPTION '价目 % 已过期（生效止 % 早于今日 %），自然到期无需停用',"
    "       {item}, r.effective_to, today;"
    "   END IF;"
    "   UPDATE usage.price_item SET effective_to = today, updated_at = now()"
    "    WHERE item_id = {item};"
    " END IF; END $$;")


def build_retire_script(item_id: str, operator: str, today: _dt.date | None = None) -> str:
    """停用单事务脚本（P1-⑧ 展示态分派，SQL 内判定防「查—改」竞态）。"""
    body = RETIRE_GUARD_SQL.replace("{today}", _lit((today or today_cn()).isoformat()))
    body = body.replace("{item}", _lit(item_id))
    return ("BEGIN;\n" + body + "\n"
            + _admin_event_sql(uuid.uuid4().hex, operator, "price_retire", item_id)
            + "\nCOMMIT;\n")


def retire_item(channel: PricingDbChannel, item_id: str, operator: str,
                today: _dt.date | None = None) -> dict:
    """停用按展示态分派：在窗→置止日＝今日；未生效→撤回删除；已过期→拒绝（409）。"""
    channel.script(build_retire_script(item_id, operator, today))
    detail = get_item(channel, item_id)
    if detail is None:
        return {"ok": True, "action": "withdrawn", "itemId": item_id,
                "message": "未生效条目已撤回删除（零历史账耦合；effective_to=今日 会撞生效区间 CHECK，不可走置止日）"}
    return {"ok": True, "action": "set_end_date", "itemId": item_id,
            "effectiveTo": detail["item"]["effectiveTo"],
            "message": "已在窗条目置生效止＝今日；已出账单不受影响，今日其后的出账按无价处理或命中下一生效价目"}


def record_denied(channel, action: str, target: str) -> None:
    """被拒提交窄表落账（尽力而为：库不可达＝仅管线事件承载，不伪造落库位）。"""
    if channel is None:
        return
    try:
        channel.script("INSERT INTO meta.admin_event (event_id, operator, action, target,"
                       " result, occurred_at) VALUES ("
                       + ", ".join(_lit(x) for x in (uuid.uuid4().hex, "system", action,
                                                     target, "denied")) + ", now());")
    except Exception:  # noqa: BLE001 —— 审计尽力而为，不阻断业务 422
        pass


# ── HTTP 编排区（宿主机无 FastAPI 不定义；main.py 挂载归收口批） ─────────────
_HAS_FASTAPI = True
try:
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse
except Exception:  # noqa: BLE001
    _HAS_FASTAPI = False

if _HAS_FASTAPI:
    import time as _time

    from app import errors as _errors
    from app.admin_api import _admin_gate, _audit_admin_action   # 同一挂点/同形态审计（先例同款）

    router = APIRouter(dependencies=[Depends(_admin_gate)])

    def _channel() -> PricingDbChannel:
        return PricingDbChannel()

    def _operator(request: Request) -> str:
        claims = getattr(request.state, "oidc_claims", None) or {}
        return str(claims.get("sub") or "admin")

    def _err(code: str, message: str, request: Request) -> JSONResponse:
        trace_id = str(getattr(request.state, "trace_id", "") or "")
        return JSONResponse(_errors.envelope(code, message, trace_id=trace_id),
                            status_code=_errors.http_status_of(code) or 400)

    def _db_down(request: Request) -> JSONResponse:
        return _err(_errors.PRICING_DB_UNAVAILABLE, MSG_PG_DOWN, request)

    def _denied(request: Request, action: str, operator: str, params: dict,
                message: str, t0: float) -> JSONResponse:
        """校验拒绝：422 人话信封＋管线八元组 result=denied（窄表由 record_denied 尽力落）。"""
        _audit_admin_action(request, action, operator, params,
                            {"error": "denied", "message": message},
                            int((_time.perf_counter() - t0) * 1000), status=422)
        return _err(_errors.PRICING_VALIDATION, message, request)

    def _channel_safe():
        """denied 窄表落账通道（尽力而为：构造失败/库不可达都返回 None 跳过）。"""
        try:
            ch = PricingDbChannel()
            ch.script("SELECT 1;")
            return ch
        except Exception:  # noqa: BLE001
            return None

    @router.get("/v1/admin/pricing/items", response_model=None)
    async def pricing_items_ep(request: Request, page: int = 1, page_size: int = 50,
                               service: str = "", state: str = "") -> JSONResponse:
        """价目列表（分页＋展示态推算＋档位摘要；库不可达＝degraded 降级态）。"""
        try:
            page, page_size = parse_paging(page, page_size)
        except ValueError as exc:
            return _err(_errors.BAD_QUERY, str(exc), request)
        try:
            data = list_items(_channel(), page, page_size,
                              service=service or None, state=state or None)
        except PricingDbError:
            return JSONResponse({"status": "degraded", "message": MSG_PG_DOWN,
                                 "total": 0, "page": page, "pageSize": page_size,
                                 "items": []})
        return JSONResponse(data)

    @router.get("/v1/admin/pricing/items/{item_id}", response_model=None)
    async def pricing_item_detail_ep(request: Request, item_id: str) -> JSONResponse:
        """价目详情（档位全览＋历史账快照脚注）。"""
        try:
            detail = get_item(_channel(), item_id)
        except PricingDbError:
            return _db_down(request)
        if detail is None:
            return _err(_errors.PRICE_ITEM_NOT_FOUND,
                        f"价目 {item_id} 不存在（或已撤回）", request)
        return JSONResponse(detail)

    @router.get("/v1/admin/pricing/selector", response_model=None)
    async def pricing_selector_ep(request: Request) -> JSONResponse:
        """计价对象选择器：服务∪已发布代理（选择器值＝endpoint 原值，P0-4）。"""
        services = getattr(request.app.state, "services", None)
        return JSONResponse(build_selector(services if isinstance(services, dict) else None))

    @router.get("/v1/admin/pricing/hit-preview", response_model=None)
    async def pricing_hit_preview_ep(request: Request, service_ref: str = "") -> JSONResponse:
        """命中预演（P0-4 配套）：选定 service_ref 对最近完整账期 usage_daily 命中行数。"""
        if not service_ref.strip():
            return _err(_errors.BAD_QUERY, "service_ref 必填（选择器给出的 endpoint 原值）", request)
        try:
            return JSONResponse(hit_preview(_channel(), service_ref))
        except PricingValidationError as exc:
            return _err(_errors.PRICING_VALIDATION, str(exc), request)
        except PricingDbError:
            return _db_down(request)

    @router.get("/v1/admin/pricing/coverage", response_model=None)
    async def pricing_coverage_ep(request: Request, period: str = "") -> JSONResponse:
        """priced 覆盖率报告（出账前只读统计；引擎同源谓词，billing_run 零改动）。"""
        try:
            return JSONResponse(coverage_report(_channel(), period or None))
        except PricingValidationError as exc:
            return _err(_errors.BAD_QUERY, str(exc), request)
        except PricingDbError:
            return _db_down(request)

    @router.post("/v1/admin/pricing/items", response_model=None)
    async def pricing_create_ep(request: Request) -> JSONResponse:
        """新建价目（三模型分流；body.preview=true＝预演不落库）。写＝admin（域9）。"""
        t0 = _time.perf_counter()
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, dict):
            return _err(_errors.INVALID_JSON, "请求体须为 JSON 对象", request)
        operator = _operator(request)
        try:
            if payload.get("preview"):
                return JSONResponse(preview_item(_channel(), payload))
            out = create_item(_channel(), payload, operator)
        except PricingValidationError as exc:
            record_denied(_channel_safe(), "price_create",
                          str(payload.get("service_ref", payload.get("serviceRef")) or ""))
            return _denied(request, "price_create", operator, payload, str(exc), t0)
        except PricingDbError as exc:
            msg = str(exc)
            if "已有生效区间与之重叠的价目" in msg:
                # 重叠窗＝校验拒绝族（设计 §2.3「拒绝 422 人话报因」；§2.10-5 后提交者 422）
                reason = msg[msg.index("服务"):] if "服务" in msg else "服务已有生效区间与之重叠的价目"
                record_denied(_channel_safe(), "price_create",
                              str(payload.get("service_ref", payload.get("serviceRef")) or ""))
                return _denied(request, "price_create", operator, payload,
                               reason.split("\n")[0], t0)
            return _db_down(request)
        _audit_admin_action(request, "price_create", operator, payload, out,
                            int((_time.perf_counter() - t0) * 1000))
        return JSONResponse(out)

    @router.post("/v1/admin/pricing/items/{item_id}/retire", response_model=None)
    async def pricing_retire_ep(request: Request, item_id: str) -> JSONResponse:
        """停用按展示态分派（P1-⑧）：置止日／撤回删除／已过期拒绝。写＝admin（域9）。"""
        t0 = _time.perf_counter()
        operator = _operator(request)
        try:
            out = retire_item(_channel(), item_id, operator)
        except PricingDbError as exc:
            msg = str(exc)
            if "已过期（生效止" in msg:
                return _err(_errors.PRICING_STATE_CONFLICT, msg[msg.index("价目"):].split("\n")[0], request)
            if "不存在（可能已撤回）" in msg:
                return _err(_errors.PRICE_ITEM_NOT_FOUND, msg[msg.index("价目"):].split("\n")[0], request)
            return _db_down(request)
        _audit_admin_action(request, "price_retire", operator, {"item_id": item_id}, out,
                            int((_time.perf_counter() - t0) * 1000))
        return JSONResponse(out)