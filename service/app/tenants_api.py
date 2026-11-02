# -*- coding: utf-8;
"""租户管理端点（管理功能群一期 A 节 · admin/tenants.html 后端；设计稿 v1.1 §1 全节）。

端点族（设计稿 §1.4 五条；原入 auth_oidc.ROUTE_MIN_ROLE 域8 扩注——读 viewer／写
admin，该矩阵与 tests/test_auth_oidc.py 清单守卫已随 v0.1.1 登录清理退役，无登录模式决策 §7）：
  GET  /v1/admin/tenants                        列表（分页沿 metering_api 先例：page/page_size
                                                ＋parse_paging＋total/page/pageSize/items 信封）
  POST /v1/admin/tenants                        新建（拍板①：yaml 态新建可用＝写矩阵登记行）
  POST /v1/admin/tenants/resync-consumers       幂等对账摘挂（P1-③；执行时点重核 frozen 态）
  POST /v1/admin/tenants/{tenant_id}/freeze     冻结（两段式：confirm=false 回影响预览）
  POST /v1/admin/tenants/{tenant_id}/unfreeze   解冻（必填工单号留痕，域模型 §4）

双模口径（设计稿 §1.5 拍板①）：
  · yaml 态（dev 缺省）：新建写「矩阵登记行」（tenant＋tenant_type＋display_name＋
    oa_ticket、grants 空、无 appkey——与快照投影同形；前置＝validate_matrix 载体行豁免
    P0-2）；冻结/解冻拒绝（409 tenant_yaml_mode＝§1.8 降级文案——矩阵行无 status 位、
    无生命周期源，不为过渡态发明第二存储形态）；
  · db 态：新建走 meta_writer.tenant_upsert（0001 DDL 三值/单号 CHECK 同源校验＋
    tenant_exists 双不复用前置查重）；冻结/解冻走 meta_writer.tenant_set_status
    （P1-①②动作名参数化；P0-6 事务内 tenant 行 FOR UPDATE 与签发族同锁、守卫哨兵
    核状态＋同锁下读凭证清单）＋网关侧全量摘 consumer／从快照 keys[] 带全量 quota 重建
    （P0-5：PUT＝覆盖写，必须带 quota，不传＝清成无限流）。

读侧（ADR-0021「读侧永不直读 DB」）：列表读快照矩阵投影——tenant_status 戳即生命
周期态（P0-3 随迁位）；无戳＝active（yaml 态恒 active，如实）。

冻结后行为（§1.6）：先库后网关——状态写事务提交后再摘 consumer；摘除失败＝库已冻结
而调用面未断：回执 partial=true＋failed 清单＋resync-consumers 幂等重试入口，db 态
admin_event 窄表补记 result=partial。

审计（§1.7/§0 总口径）：管线八元组 admin:tenant_create/tenant_freeze/tenant_unfreeze
（operator＝OIDC sub，admin_api._audit_admin_action 同形态本地实现）；db 态同发
admin_event 窄表（状态轨在 meta_writer 事务内；denied/partial 由 admin_event_log 补记）；
yaml 态仅管线事件＋文件 git 留痕（不伪造落库位）；被拒提交记 denied。

main.py 挂载归收口批（任务分工）；未挂载前端点不激活，端点契约由
tests/test_tenants_api.py 自挂 TestClient 覆盖（test_metering._client 同款先例）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:   # 宿主机单测环境无 fastapi：纯逻辑（校验/视图/规划）仍可导入直测
    from fastapi import APIRouter, Depends, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except Exception:   # noqa: BLE001
    APIRouter = Depends = Request = JSONResponse = None   # type: ignore[assignment]
    _HAS_FASTAPI = False

from app import appkeys as appk
from app import errors
from app import gw_consumer
from app import grants_admin as gam
from app import meta_writer as mw
from app import usage_report as ur
from app.audit import make_entry
from app.kv_executor import TagQueryError

if _HAS_FASTAPI:
    # router 级统一挂闸（admin_api._admin_gate，恒放行；原写方法 require_login／
    # ROUTE_MIN_ROLE 角色裁决随 v0.1.1 登录清理退役，无登录模式决策 §7）
    from app.admin_api import _admin_gate, _reload
    router = APIRouter(dependencies=[Depends(_admin_gate)])
else:
    router = None

TENANT_STATUSES = gam.TENANT_STATUSES
STATUS_LABELS = {"active": "在册", "frozen": "已冻结",
                 "terminating": "停用中", "cancelled": "已注销"}
TYPE_LABELS = gam.TENANT_TYPE_LABELS

# §1.8 降级文案（yaml 态冻结/解冻置灰提示；selftest-tenants 断言命中同串）
YAML_LIFECYCLE_COPY = ("当前为 yaml 配置模式（dev 缺省），冻结/解冻需元数据库模式"
                       "（META_WRITER=db）——yaml 态无生命周期状态源。")
# §1.8 未登记凭证租户串提示（「未登记」徽标行无操作按钮）
UNREGISTERED_COPY = ("凭证注册表存在该串但租户未登记实体；补登记归元数据迁移工具"
                     "（域模型 §7 迁移动作）。")


class TenantAdminError(TagQueryError):
    """租户管理写面校验错误（结构同 GrantAdminError：status/code/message，端点层直映射）。"""


# ── 读侧视图（列表＋指标卡；纯逻辑可直测，ADR-0021 读快照） ──

def tenant_rows(matrix, keys) -> list[dict]:
    """租户行装配：矩阵行＝已登记租户（tenant_status 戳即生命周期态）；仅凭证侧
    租户串＝「未登记」（§1.2/§1.8：无生命周期操作，补登记归迁移工具）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for row in matrix or []:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tenant", "")).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ttype = gam.tenant_type_of(row)
        status = gam.tenant_status_of(row)
        grants = [g for g in (row.get("grants") or []) if isinstance(g, dict)]
        out.append({
            "tenant": tid,
            "displayName": str(row.get("display_name") or tid),
            "tenantType": ttype, "tenantTypeLabel": TYPE_LABELS[ttype],
            "status": status, "statusLabel": STATUS_LABELS[status],
            "oaTicket": str(row.get("oa_ticket") or ""),
            "activeKeys": sum(1 for k in (keys or []) if str(k.get("tenant", "")) == tid
                              and k.get("status") == "active"),
            "grantsCount": len(grants),
            "registered": True,
            "createdAt": None,   # 快照投影无创建时间戳——一期如实展示「—」（§1.2 列在位）
        })
    for k in (keys or []):
        tid = str(k.get("tenant", "")).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ttype = "external" if any(str(x.get("tenant", "")) == tid and x.get("key_type") == "formal"
                                  for x in (keys or [])) else "sandbox"
        out.append({
            "tenant": tid,
            "displayName": str(k.get("caller_name") or tid),
            "tenantType": ttype, "tenantTypeLabel": TYPE_LABELS[ttype],
            "status": "active", "statusLabel": STATUS_LABELS["active"],
            "oaTicket": "",
            "activeKeys": sum(1 for x in (keys or []) if str(x.get("tenant", "")) == tid
                              and x.get("status") == "active"),
            "grantsCount": 0,
            "registered": False,
            "createdAt": None,
        })
    return out


def tenants_view(matrix, keys, *, tenant_type: str = "", status: str = "",
                 q: str = "") -> dict:
    """列表视图：指标卡（全量口径，不随筛选变）＋筛选后行集（筛选/分页由端点续做）。"""
    all_rows = tenant_rows(matrix, keys)
    by_type = {"internal": 0, "external": 0, "sandbox": 0}
    for r in all_rows:
        by_type[r["tenantType"]] = by_type.get(r["tenantType"], 0) + 1
    summary = {
        "tenants": len(all_rows), "byType": by_type,
        "active": sum(1 for r in all_rows if r["status"] == "active"),
        "frozen": sum(1 for r in all_rows if r["status"] == "frozen"),
        "terminating": sum(1 for r in all_rows if r["status"] == "terminating"),
        "cancelled": sum(1 for r in all_rows if r["status"] == "cancelled"),
    }
    rows = all_rows
    if tenant_type:
        rows = [r for r in rows if r["tenantType"] == tenant_type]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if q:
        rows = [r for r in rows if r["tenant"].startswith(q) or r["displayName"].startswith(q)]
    return {"summary": summary, "rows": rows}


LIST_CALIBER = {
    "source": "快照矩阵投影（ADR-0021 读侧永不直读 DB）；分页沿 metering_api 先例",
    "statuses": "tenant_status 戳随迁（非 active 才输出；无戳＝active——yaml 态恒 active）",
    "lifecycle": "冻结/解冻仅元数据库模式（META_WRITER=db）；yaml 态新建写矩阵登记行可用",
    "unregistered": UNREGISTERED_COPY,
    "createdAt": "快照投影无创建时间戳，一期如实展示「—」",
}


# ── 新建（拍板①：yaml 态写矩阵登记行；db 态走 tenant_upsert） ──

def validate_tenant_create(payload) -> dict:
    """新建校验（双模共用纯逻辑）：格式/类型/单号；人话报因（denied 由端点层记审计）。
    唯一性查重在 create_tenant 内做（yaml 对底稿、db 对实体表——双不复用硬规则）。"""
    if not isinstance(payload, dict):
        raise TenantAdminError(400, errors.INVALID_JSON, "请求体须为 JSON 对象")
    tid = str(payload.get("tenant_id") or "").strip()
    if not gam.TENANT_ID_RE.match(tid):
        raise TenantAdminError(400, errors.INVALID_TENANT,
                               "租户 ID 非法（小写字母/数字/_-，2~64 位）")
    tenant_type = str(payload.get("tenant_type") or "").strip()
    if tenant_type not in gam.TENANT_TYPES:
        raise TenantAdminError(400, errors.INVALID_REQUEST,
                               "类型须为 internal/external/sandbox 三选一")
    display_name = str(payload.get("display_name") or "").strip()
    oa_ticket = str(payload.get("oa_ticket") or "").strip()
    remark = str(payload.get("remark") or "").strip()
    if tenant_type == "sandbox":
        if oa_ticket:
            raise TenantAdminError(422, errors.INVALID_REQUEST,
                                   "沙箱租户免开通单号——请留空（表单单号栏自动置灰）")
    else:
        if not oa_ticket:
            raise TenantAdminError(422, errors.OA_TICKET_REQUIRED,
                                   "非沙箱租户开通 OA 单号必填（审批在 OA，此处登记单号；ADR-0003）")
        if not gam.OA_TICKET_RE.match(oa_ticket):
            raise TenantAdminError(422, errors.INVALID_OA_TICKET,
                                   "开通单号格式提醒：4~64 位字母数字-_（平台只登记，不做系统核对）")
    return {"tenant_id": tid, "display_name": display_name, "tenant_type": tenant_type,
            "oa_ticket": oa_ticket, "remark": remark}


def _create_hint(tenant_type: str, mode: str) -> str:
    """§1.3-1 结果提示（沙箱/正式两口径＋分模后缀）。"""
    tail = ("首次签发 AppKey 时将自动挂平台演示模板授权（测试用，正式凭证另行审批）。"
            if tenant_type == "sandbox" else "可前往授权管理页登记授权。")
    if mode == "yaml":
        return "已登记（yaml 态矩阵即租户真源）；冻结/解冻需 db 模式。" + tail
    return "已登记（元数据库）。" + tail


def create_tenant(matrix_path, keys, norm: dict, operator: str) -> dict:
    """新建租户（双模分流）。yaml＝快照同形登记行追加＋validate_matrix 前置校验
    （P0-2 载体行豁免＝读回不炸）；db＝tenant_exists 查重（双不复用）＋tenant_upsert。
    返回视图 dict（mode/tenant/hint）；拒绝抛 TenantAdminError。"""
    tid = norm["tenant_id"]
    known = {str(k.get("tenant", "")) for k in (keys or []) if k.get("tenant")}
    if mw.writer_mode() == "db":
        # 双不复用硬规则：db 实体表再核一道（快照可能滞后）；tenant_upsert 的
        # upsert 语义只归显式登记面，新建绝不改写既有行（并发窗口＝单管理员操作，
        # 极小；如需绝对串行归收口批随 meta_writer 事务化另卡）
        if tid in known or mw.tenant_exists(tid):
            raise TenantAdminError(422, errors.INVALID_REQUEST,
                                   f"租户 ID {tid} 已存在（双不复用：一经分配永不改写、永不复用）")
        row = mw.tenant_upsert(tenant_id=tid,
                               display_name=norm["display_name"] or tid,
                               tenant_type=norm["tenant_type"],
                               oa_ticket_open=norm["oa_ticket"] or None,
                               remark=norm["remark"] or None,
                               operator=operator, oa_ticket=norm["oa_ticket"] or None)
        return {"mode": "db",
                "tenant": {"tenant": row["tenant_id"],
                           "displayName": row.get("display_name") or tid,
                           "tenantType": row.get("tenant_type"),
                           "status": row.get("status") or "active",
                           "oaTicket": row.get("oa_ticket_open") or "",
                           "remark": row.get("remark") or ""},
                "hint": _create_hint(norm["tenant_type"], "db")}
    # ---- yaml 模式：矩阵登记行（读最新底稿，唯一性对底稿∪注册表双查） ----
    data = gam.load_matrix_file(Path(matrix_path))
    errs, _ = gam.validate_matrix(data)
    if errs:
        raise TenantAdminError(422, errors.INVALID_REQUEST,
                               "授权矩阵底稿不合法，拒绝改写：" + "；".join(errs[:3]))
    matrix = data.setdefault("matrix", [])
    if tid in known or any(str(r.get("tenant", "")) == tid for r in matrix):
        raise TenantAdminError(422, errors.INVALID_REQUEST,
                               f"租户 ID {tid} 已存在（双不复用：一经分配永不改写、永不复用）")
    row: dict = {"tenant": tid}
    if norm["display_name"] and norm["display_name"] != tid:
        row["display_name"] = norm["display_name"]
    if norm["tenant_type"] != "external":   # 未标＝external（规划器缺省，往返零漂移）
        row["tenant_type"] = norm["tenant_type"]
    if norm["oa_ticket"]:
        row["oa_ticket"] = norm["oa_ticket"]
    row["grants"] = []                      # 登记行：只登记实体，授权归授权管理页
    matrix.append(row)
    data["meta"] = {"updated": gam._utc_now_iso(), "updated_by": operator or "tenants_admin"}
    gam.save_matrix(Path(matrix_path), data, operator=operator)   # 双模分流（yaml 态＝原文件写）
    out = {"mode": "yaml",
           "tenant": {"tenant": tid,
                      "displayName": norm["display_name"] or tid,
                      "tenantType": norm["tenant_type"],
                      "status": "active",
                      "oaTicket": norm["oa_ticket"],
                      "remark": ""},
           "hint": _create_hint(norm["tenant_type"], "yaml")}
    if norm["remark"]:
        out["remarkNote"] = "备注仅 db 模式持久化（矩阵行无备注列），本次未保存。"
    return out


# ── 冻结/解冻/resync（P0-6/P0-5/P1-③） ──

def freeze_plan(matrix, keys, tenant_id: str) -> dict:
    """冻结第一段：影响域预览（§1.2 弹层口径）——不执行不落盘。"""
    row = gam._matrix_row(matrix or [], tenant_id)
    active = [k for k in (keys or []) if str(k.get("tenant", "")) == tenant_id
              and k.get("status") == "active"]
    if row is None and not active:
        raise TenantAdminError(404, errors.TENANT_NOT_FOUND,
                               f"租户 {tenant_id} 未登记（矩阵与注册表均无此实体）")
    st = gam.tenant_status_of(row)
    if st != "active":
        raise TenantAdminError(409, errors.TENANT_FROZEN,
                               f"租户 {tenant_id} 当前状态 {STATUS_LABELS[st]}：仅在册租户可冻结")
    return {"tenant": tenant_id, "status": st,
            "activeKeys": len(active),
            "appkeys": [appk.mask_appkey(str(k.get("appkey"))) for k in active],
            "impacts": [
                f"名下 active 凭证 {len(active)} 把将全量摘除（轮换重叠期新旧键同摘）",
                "授权登记与变更冻结",
                "新增调用即拒不计",
                "历史账照出",
            ]}


async def _sync_consumers(matrix, keys, tenant_id: str | None = None) -> dict:
    """以库态（快照矩阵＋注册表）为准全量对账摘挂（P1-③幂等）：非 active 租户名下
    active 凭证全摘；active 租户名下 active 凭证全挂（带全量 quota——P0-5 同口径，
    PUT 覆盖写不传 quota＝清成无限流）。执行时点重核 frozen（matrix 实参即执行时快照）。
    幂等＝重复执行收敛同态、计数复现。"""
    ensured = removed = 0
    failed: list[dict] = []
    for r in tenant_rows(matrix, keys):
        if tenant_id and r["tenant"] != tenant_id:
            continue
        creds = [k for k in (keys or []) if str(k.get("tenant", "")) == r["tenant"]
                 and k.get("status") == "active"]
        for k in creds:
            ak = str(k.get("appkey") or "")
            if not ak:
                continue
            if r["status"] == "active":
                ok = await gw_consumer.put_consumer(ak, quota_qps=k.get("quota_qps"),
                                                    quota_daily=k.get("quota_daily"))
                if ok:
                    ensured += 1
                else:
                    failed.append({"tenant": r["tenant"], "appkey": appk.mask_appkey(ak),
                                   "op": "put"})
            else:
                ok = await gw_consumer.delete_consumer(ak)
                if ok:
                    removed += 1
                else:
                    failed.append({"tenant": r["tenant"], "appkey": appk.mask_appkey(ak),
                                   "op": "delete"})
    return {"ensured": ensured, "removed": removed, "failed": failed,
            "tenant": tenant_id}


def _operator(request) -> str:
    claims = getattr(request.state, "oidc_claims", None) or {}
    return str(claims.get("sub") or "admin")


def _audit(request, action: str, operator: str, params: dict, status: int,
           response: dict, latency_ms: int) -> str:
    """管理动作审计（admin_api._audit_admin_action 同形态本地实现；writer 缺位＝
    如实跳过不阻断）。endpoint＝admin:<动作名>（§1.7：tenant_create/freez/unfreeze）。"""
    writer = getattr(getattr(request, "app", None), "state", None)
    writer = getattr(writer, "audit", None)
    if writer is None:
        return ""
    try:
        entry = make_entry(caller=operator or "admin", endpoint=f"admin:{action}",
                           params=json.dumps(params, ensure_ascii=False, sort_keys=True),
                           status=status, latency_ms=latency_ms,
                           response_body=json.dumps(response, ensure_ascii=False,
                                                    sort_keys=True),
                           trace_id=str(getattr(request.state, "trace_id", "") or ""))
        writer.append(entry)
        return entry.trace_id or ""
    except Exception:   # noqa: BLE001 —— 审计写失败不阻断业务返回
        return ""


def _denied(request, action: str, operator: str, params: dict, exc: TenantAdminError,
            t0: float, db_mode: bool, tenant_id: str | None = None) -> JSONResponse:
    """被拒提交：管线八元组事件（result=denied 语义）＋db 态窄表补记（§0 分模承载）。"""
    _audit(request, action, operator, {**params, "denied": exc.code}, exc.status,
           {"denied": exc.code, "message": exc.message},
           int((time.monotonic() - t0) * 1000))
    if db_mode:
        mw.admin_event_log(action=action, operator=operator,
                           target=(params.get("tenant") or tenant_id or "-"),
                           result="denied", tenant_id=tenant_id,
                           oa_ticket=params.get("oa_ticket") or None)
    return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)


def _gate_map(exc: mw.MetaDbError) -> JSONResponse | None:
    """db 守卫哨兵 → 4xx 信封（P0-6：冻结竞争/行缺失在端点层给人话码）。"""
    code = mw.tenant_gate_code(exc)
    if code:
        return JSONResponse({"error": code, "message": str(exc)}, 409 if code == "tenant_frozen" else 404)
    return None


if _HAS_FASTAPI:
    # ══ 端点族（router 级挂 _admin_gate；main.py include 归收口批） ══

    def _ctx(request: Request):
        """运行期依赖（main.lifespan 注入）：快照矩阵 / 注册表 keys / 矩阵路径。"""
        state = request.app.state
        path = getattr(state, "auth_matrix_path", None) or gam.DEFAULT_MATRIX
        doc = appk.load_appkeys(state.appkeys_path)
        return state.auth_matrix, doc.get("keys") or [], Path(path)

    @router.get("/v1/admin/tenants", response_model=None)
    async def tenants_list_ep(request: Request, page: int = 1, page_size: int = None,
                              type: str = "", status: str = "", q: str = "") -> JSONResponse:
        """租户列表（§1.2 主表＋指标卡；分页沿 metering_api 先例信封）。"""
        matrix, keys, _path = _ctx(request)
        try:
            page, size = ur.parse_paging(page, page_size)
        except ValueError as exc:
            return JSONResponse({"error": errors.INVALID_REQUEST, "message": str(exc)}, 400)
        view = tenants_view(matrix, keys, tenant_type=(type or "").strip(),
                            status=(status or "").strip(), q=(q or "").strip())
        total, items = ur.paginate(view["rows"], page, size)
        return JSONResponse({"total": total, "page": page, "pageSize": size,
                             "items": items, "summary": view["summary"],
                             "mode": mw.writer_mode(),   # 前端分模降级（§1.8 按钮置灰口径）
                             "caliber": LIST_CALIBER})

    @router.post("/v1/admin/tenants", response_model=None)
    async def tenant_create_ep(request: Request) -> JSONResponse:
        """新建租户（§1.3-1；拍板① yaml 态写矩阵登记行）。成功与被拒都落审计。"""
        matrix, keys, path = _ctx(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = None
        operator = _operator(request)
        db_mode = mw.writer_mode() == "db"
        t0 = time.monotonic()
        try:
            norm = validate_tenant_create(payload)
            out = create_tenant(path, keys, norm, operator)
        except TenantAdminError as exc:
            return _denied(request, "tenant_create", operator,
                           {"tenant": (payload or {}).get("tenant_id"),
                            "tenantType": (payload or {}).get("tenant_type"),
                            "oaTicket": bool((payload or {}).get("oa_ticket"))},
                           exc, t0, db_mode)
        if db_mode:
            _reload(request)   # 热加载：快照已重导，矩阵/注册表内存态对齐
        audit_id = _audit(request, "tenant_create", operator,
                          {"tenant": out["tenant"]["tenant"],
                           "tenantType": out["tenant"]["tenantType"],
                           "oaTicket": bool(out["tenant"]["oaTicket"]),
                           "mode": out["mode"]}, 200,
                          {"tenant": out["tenant"]["tenant"], "mode": out["mode"]},
                          int((time.monotonic() - t0) * 1000))
        return JSONResponse({**out, "auditId": audit_id})

    @router.post("/v1/admin/tenants/resync-consumers", response_model=None)
    async def tenants_resync_ep(request: Request) -> JSONResponse:
        """幂等对账摘挂（P1-③）：以库态为准全量（或 ?tenant= 单租户）摘挂 consumer。
        yaml 态无戳＝全部按 active 处理（对账仍可用：补挂遗漏 consumer）。"""
        matrix, keys, _path = _ctx(request)
        operator = _operator(request)
        t0 = time.monotonic()
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        tid = str((payload or {}).get("tenant") or "").strip() or None
        out = await _sync_consumers(matrix, keys, tid)
        db_mode = mw.writer_mode() == "db"
        partial = bool(out["failed"])
        if db_mode and partial:
            mw.admin_event_log(action="tenant_resync_consumers", operator=operator,
                               target=tid or "*", result="partial", tenant_id=tid)
        audit_id = _audit(request, "tenant_resync_consumers", operator,
                          {"tenant": tid}, 200, {"ensured": out["ensured"],
                                                 "removed": out["removed"],
                                                 "partial": partial},
                          int((time.monotonic() - t0) * 1000))
        return JSONResponse({**out, "partial": partial, "auditId": audit_id})

    @router.post("/v1/admin/tenants/{tenant_id}/freeze", response_model=None)
    async def tenant_freeze_ep(tenant_id: str, request: Request) -> JSONResponse:
        """冻结（§1.3-2）：两段式（confirm=false 回影响预览）；db 态先库后网关——
        tenant_set_status 事务内行锁＋守卫＋同锁凭证清单（P0-6），提交后全量摘
        consumer；摘除失败 partial＋resync 重试入口。yaml 态 409 降级文案（§1.8）。"""
        matrix, keys, _path = _ctx(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        operator = _operator(request)
        db_mode = mw.writer_mode() == "db"
        t0 = time.monotonic()
        confirm = (payload or {}).get("confirm") is True
        params = {"tenant": tenant_id, "confirm": confirm}
        if not db_mode:
            exc = TenantAdminError(409, errors.TENANT_YAML_MODE, YAML_LIFECYCLE_COPY)
            return _denied(request, "tenant_freeze", operator, params, exc, t0, False, tenant_id)
        try:
            plan = freeze_plan(matrix, keys, tenant_id)
        except TenantAdminError as exc:
            return _denied(request, "tenant_freeze", operator, params, exc, t0, True, tenant_id)
        if not confirm:
            return JSONResponse({"confirmRequired": True, "plan": plan})
        try:
            res = mw.tenant_set_status(tenant_id=tenant_id, status="frozen",
                                       action=mw.TENANT_FREEZE_ACTION, operator=operator)
        except mw.MetaDbError as exc:
            mapped = _gate_map(exc)
            if mapped is not None:
                return mapped
            raise
        appkeys_in_lock = res.get("active_appkeys") or []
        failed: list[dict] = []
        removed = 0
        for ak in appkeys_in_lock:   # P0-6：清单取自事务内行锁下快照，并发签发不漏
            if await gw_consumer.delete_consumer(str(ak)):
                removed += 1
            else:
                failed.append({"tenant": tenant_id, "appkey": appk.mask_appkey(str(ak)),
                               "op": "delete"})
        partial = bool(failed)
        if partial:
            mw.admin_event_log(action=mw.TENANT_FREEZE_ACTION, operator=operator,
                               target=tenant_id, result="partial", tenant_id=tenant_id)
        _reload(request)   # 快照已重导：内存矩阵带上 frozen 戳（联动门即时生效）
        audit_id = _audit(request, "tenant_freeze", operator, params, 200,
                          {"frozen": True, "removed": removed, "partial": partial},
                          int((time.monotonic() - t0) * 1000))
        return JSONResponse({"frozen": True, "tenant": tenant_id, "removed": removed,
                             "partial": partial, "failed": failed,
                             "resync": "POST /v1/admin/tenants/resync-consumers",
                             "auditId": audit_id})

    @router.post("/v1/admin/tenants/{tenant_id}/unfreeze", response_model=None)
    async def tenant_unfreeze_ep(tenant_id: str, request: Request) -> JSONResponse:
        """解冻（§1.3-3）：必填工单号（纯登记留痕）；db 态状态写后从快照 keys[] 带全量
        quota 重建 consumer（P0-5——PUT 覆盖写不传 quota＝清成无限流）。恢复失败同
        resync 重试模式。yaml 态 409 降级文案。"""
        matrix, keys, _path = _ctx(request)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        operator = _operator(request)
        db_mode = mw.writer_mode() == "db"
        t0 = time.monotonic()
        oa_ticket = str((payload or {}).get("oa_ticket") or "").strip()
        params = {"tenant": tenant_id, "oaTicket": bool(oa_ticket)}
        if not db_mode:
            exc = TenantAdminError(409, errors.TENANT_YAML_MODE, YAML_LIFECYCLE_COPY)
            return _denied(request, "tenant_unfreeze", operator, params, exc, t0, False, tenant_id)
        if not oa_ticket:
            exc = TenantAdminError(422, errors.OA_TICKET_REQUIRED,
                                   "解冻必填工单号（纯登记留痕——域模型 §4：解冻走工单）")
            return _denied(request, "tenant_unfreeze", operator, params, exc, t0, True, tenant_id)
        if not gam.OA_TICKET_RE.match(oa_ticket):
            exc = TenantAdminError(422, errors.INVALID_OA_TICKET,
                                   "工单号格式提醒：4~64 位字母数字-_（平台只登记，不做系统核对）")
            return _denied(request, "tenant_unfreeze", operator, params, exc, t0, True, tenant_id)
        try:
            res = mw.tenant_set_status(tenant_id=tenant_id, status="active",
                                       action=mw.TENANT_UNFREEZE_ACTION, operator=operator,
                                       oa_ticket=oa_ticket)
        except mw.MetaDbError as exc:
            mapped = _gate_map(exc)
            if mapped is not None:
                return mapped
            raise
        # P0-5：从快照 keys[] 带全量 quota 重建（只恢复 status=active 凭证；
        # revoked/disabled 行保留不挂 consumer）。清单以库态返回为准＋快照 keys 并集
        # （keys[] 携 quota；事务内清单携锁序完整性）——同串去重。
        quota_by_key = {str(k.get("appkey")): k for k in (keys or [])
                        if str(k.get("tenant", "")) == tenant_id and k.get("status") == "active"}
        restore: dict = {}
        for ak in list(res.get("active_appkeys") or []):
            restore.setdefault(str(ak), quota_by_key.get(str(ak), {}))
        for ak, k in quota_by_key.items():
            restore.setdefault(ak, k)
        failed: list[dict] = []
        restored = 0
        for ak, k in sorted(restore.items()):
            if await gw_consumer.put_consumer(ak, quota_qps=(k or {}).get("quota_qps"),
                                              quota_daily=(k or {}).get("quota_daily")):
                restored += 1
            else:
                failed.append({"tenant": tenant_id, "appkey": appk.mask_appkey(ak), "op": "put"})
        partial = bool(failed)
        if partial:
            mw.admin_event_log(action=mw.TENANT_UNFREEZE_ACTION, operator=operator,
                               target=tenant_id, result="partial", tenant_id=tenant_id,
                               oa_ticket=oa_ticket)
        _reload(request)
        audit_id = _audit(request, "tenant_unfreeze", operator,
                          {**params, "oaTicket": oa_ticket}, 200,
                          {"unfrozen": True, "restored": restored, "partial": partial},
                          int((time.monotonic() - t0) * 1000))
        return JSONResponse({"unfrozen": True, "tenant": tenant_id, "restored": restored,
                             "partial": partial, "failed": failed,
                             "resync": "POST /v1/admin/tenants/resync-consumers",
                             "auditId": audit_id})


# 宿主机无 fastapi 时纯逻辑仍可导入直测
