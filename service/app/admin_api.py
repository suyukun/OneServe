# -*- coding: utf-8 -*-
"""运营管理端点（P0-4 · 凭证发放基建）——FastAPI 薄胶水。

端点（均经门户 nginx /v1/admin/ 反代到本服务；写操作无需令牌）：
  GET  /v1/admin/appkeys                    凭证列表（掩码＋摘要展示口径）＋可签发调用方
                                            （?format=yaml 返回权威源文件原文）
  GET  /v1/admin/appkeys/usage              配额用量（P1-3 B6：按调用方看当日「已用/上限」
                                            实时值；与门户 /v1/portal/my/quota 同函数同口径）
  POST /v1/admin/appkeys                    签发（网关 consumer 先行→注册表落盘→热加载）
  POST /v1/admin/appkeys/{appkey}/status    禁用/启用（两段语义见 appkeys.set_status）
  POST /v1/admin/appkeys/{appkey}/rotate    轮换（P1-4：签发新 key＋旧 key 进重叠期，
                                            重叠期默认 7 天，见 appkeys.ROTATION_OVERLAP_DAYS）
  POST /v1/admin/appkeys/{appkey}/quota     调整配额（登记值落权威源并同步网关限流插件）
  POST /v1/admin/appkeys/{appkey}/delete    受限删（2026-10-14 AC-2：仅从未被调用的凭证；
                                            有调用记录 409 人话拒绝；成功留审计事件）
  POST /v1/admin/appkeys/{appkey}/expires   续期（AC-3：只能延长；permanent=永久；留审计）
  POST /v1/admin/reload                     热加载（外部改 auth_matrix/appkeys 后免重启生效）

  GET  /v1/admin/publish                    发布台整页数据（目录树真源生成＋写区列表；
                                            ?format=yaml 返回 services.yaml 原文）
  GET  /v1/admin/publish/sync/status        切版状态（当前域指针/版本元信息/最近切换
                                            日志；只读，向导③「当前线上版本」回显）
  POST /v1/admin/publish/sync/switch        切换到新版（过三道闸切指针；复用
                                            sync/switch.py 闸逻辑，只包不改）
  POST /v1/admin/publish/sync/rollback      回滚到指定旧版（同款复用 rollback.py；
                                            to 缺省＝自动取上一版）
  GET  /v1/admin/datasources/hbase          HBase 数据源发现（AC-4：namespace/表/列族/列
                                            ＋字段字典双源校验；不可达如实 503）
  POST /v1/admin/datasources/hbase/verify   双源校验独立端点（同一口径，供发布前置校验）
  POST /v1/admin/publish                    发布/存草稿（写 services.yaml console_services
                                            写区；hbase 型另落运行时服务条目，P1-10/11＋M2-4）
  POST /v1/admin/publish/{sid}/offline      下线（published→offline，版本栏留痕）
  GET  /v1/admin/upstreams                  代理登记表（?format=yaml 返回 upstreams.yaml 原文）
  POST /v1/admin/upstreams                  存草稿/发布（写 upstreams.yaml，同口径）
  POST /v1/admin/upstreams/{pid}/offline    下线代理

  GET  /v1/admin/versions                   版本空间列表（P3 2026-10-08：SCAN tags:* 按
                                            数据面聚合 namespace×版本×键数；每行标
                                            isLive/registered/verMeta/schemaMeta）
  POST /v1/admin/versions/delete            删除一个版本空间（系统管理员定期清理；
                                            保护闸只锁线上版，物理上一版允许删除；
                                            成功后同步 console_services 登记
                                            backend.load 命中→loaded:false）

  {appkey} 寻址接受完整 appkey 或列表视图的 kid（sha256 前 12 位）——列表不回完整值。

分层纪律：决策全在 app/appkeys.py（纯逻辑）＋app/gw_consumer.py（网关通道）＋
app/ops_config.py（发布台/代理写区，P1-10/11）；本模块只做 HTTP 编排
（token/限频计数/文件读写/ reload 调用），无业务判断。
安全：管理令牌（X-Admin-Token）三段决策史——2026-09-23 整体拆除（35ab1aa）→
2026-10-09 按生产目标恢复路由级闸（bcf34f8/31ed94c/306fb58）→ 2026-10-14 维护者 亲测
两轮被卡后拍板再拆除（现在不需要管理令牌，未来统一做系统登录页，已记待办）。
当前形态＝_admin_gate 恒放行：/v1/admin/* 与 monitor 三端点不再校验任何令牌，机制
骨架保留供登录页时代低成本恢复；授权边界＝网络层可达（仅本机/内网可达即视为已授权）。
部署到公司环境前必须先有登录页或恢复闸（硬前提，见 ADR-0010）。
errors.UNAUTHORIZED 等错误码常量仍保留（错误码中心 91 个码口径契约，不得删）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit   # 2026-10-15 走查修复：/v1/admin/upstreams/probe NameError→500

import hmac   # 2026-10-14 闸恒放行后暂无调用方；保留＝恢复闸时恒时比较直接可用（ADR-0010）

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app import appkeys as appk
from app import auth_oidc   # 登录闸（写操作族 Bearer JWT 校验；ADR-0010 硬前提兑现）
from app import dev_token as devtok
from app import schema_import as sim   # B1：表结构登记（贴 DDL→COMMENT→写回 schema csv 真源）
from app import load_tasks as lt       # B2：异步装载任务（后台线程＋单进度数字）
from app import connections
from app.audit import make_entry   # 卡4 NC-2c：发布/下线管理动作接审计八元组
from app import errors
from app import gw_consumer
from app import hbase_executor as hbx     # M2-4：HBase meta 发现（AC-4）
from app import hbase_load as hbl        # 本卡：HBase→Redis 加载（复用既有加载器/对账）
from app import my_access as mya          # P1-3：配额用量聚合（与门户同函数同口径）
from app import ops_config as opsc
from app import meta_writer as mw   # 卡-1b7 步5：写侧统一入口（META_WRITER 缺省 yaml＝行为不变）
from app import kv_executor as kvx      # 1b-8：AUTH_MATRIX_YAML 缺省路径（state 缺装配时回落）
from app import grants_admin as gam     # 1b-8：授权管理写面（三动作＋派生视图；auth_admin CLI 同层同调）
from app import route_naming as rn     # 路由命名机器闸（namingHint 域码建议提示，单一来源）
from app import schema_drift as sd   # 字段空间缺省口径单一事实来源（发布台回环防线）
from app import sync_ops as syo      # B4：切版/回滚（复用 sync CLI，零第二闸实现）

# 管理令牌闸三段决策史（拆一次建一次都有决策留痕）：
#   2026-09-23 整体拆除（35ab1aa）→ 2026-10-09 按生产目标恢复（bcf34f8 起，
#   X-Admin-Token 头＋hmac 恒时比较，ADMIN_API_TOKEN 环境变量可覆盖，缺省开发
#   占位值 dev_token 单一事实来源）→ 2026-10-14 维护者 亲测两轮被卡后拍板再拆除
#   （现在不需要管理令牌；未来统一做系统登录页，已记待办）。
# 当前形态（2026-10-14 起）：_admin_gate 恒放行——/v1/admin/* 与 monitor 三端点
# 不再校验任何令牌；机制骨架（常量/异常/handler/挂点）全保留，恢复路径与「部署
# 到公司环境前必须先有登录页或恢复闸」硬前提见 ADR-0010。
ADMIN_TOKEN = devtok.runtime_admin_token()   # 令牌真源保留（登录页时代复用；恒放行期不校验


class AdminGateDenied(Exception):
    """管理令牌闸拒绝（main 注册 handler 转项目错误信封 401——依赖里 return
    Response 不会短路请求，必须 raise；自定义异常避免动全局 HTTPException 格式）。
    2026-10-14 起闸恒放行，本异常不再被触发；保留供恢复闸期原样复用。"""


async def _admin_gate(request: Request) -> None:
    """路由级依赖：全部 /v1/admin/* 端点统一挂点（新增端点自动带闸，防漏挂）。

    决策史第四段（2026-10-14 晚·登录页时代开启）：Casdoor OIDC 登录闸上线
    （ADR-0010 硬前提兑现起点，调研报告 Top1 落地路径第 2 步）——写操作族
    （POST/PUT/DELETE/PATCH）经 auth_oidc.require_login 验 Bearer JWT。
    第五段（2026-10-15·登录墙设计 A1/W4，ADR-0015/0016）：「读公开」废除——
    GET 读端点登录前置上收站点墙中间件（墙域含 GET）；本依赖保留写方法登录
    校验作**双保险**（墙失效时不裸奔），并按 ROUTE_MIN_ROLE 权限矩阵做角色
    裁决（写路由 min_role 漏标由清单测试拦截；不足 403 role_forbidden）。
    AUTH_OIDC_MODE=off 时全放行（宿主机单测/裸跑口径，语义同恒放行期）。
    前三段史（拆→恢复→再拆）见 ADR-0010；X-Admin-Token 恢复形态见 git bcf34f8。
    """
    if request.method in auth_oidc.WRITE_METHODS:
        await auth_oidc.require_login(request)
    await auth_oidc.adjudicate_route_role(request)   # W4：矩阵本地裁决（off＝no-op）
    return None


router = APIRouter(dependencies=[Depends(_admin_gate)])


def _ctx(request: Request):
    """运行期依赖（main.lifespan 注入）：redis / services / auth_matrix / 注册表路径。"""
    return request.app.state.redis, request.app.state.services, request.app.state.auth_matrix, \
        request.app.state.appkeys_path


def _client_ip(request: Request) -> str:
    """发放限频的 IP 口径：门户 nginx 反代已置 X-Real-IP；直连时回落 client.host。"""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _namespace_with_proxies(services: dict) -> dict:
    """服务命名空间 ＋ upstreams.yaml 已发布代理（C2/C4：代理也可被签发/被解析）。

    代理不是 services.yaml 运行时条目，但签发授权与授权列表都要能识别它，
    故在 HTTP 编排层做一次只读合并（不写回 app.state.services，避免影响
    网关渲染/查询链——代理的运行时消费走 proxy_hub 自己那张表）。
    """
    merged = dict(services or {})
    for pid, svc in opsc.proxy_service_map().items():
        merged.setdefault(pid, svc)
    return merged


def _callers_view(matrix: list, doc: dict) -> list[dict]:
    """可签发调用方：矩阵已登记租户（OA 通道）＋注册表已签发租户（控制台通道）。"""
    out, seen = [], set()
    for row in matrix or []:
        tid = row.get("tenant")
        if tid and tid not in seen:
            seen.add(tid)
            out.append({"id": tid, "name": row.get("display_name") or tid})
    for k in doc["keys"]:
        tid = k.get("tenant")
        if tid and tid not in seen:
            seen.add(tid)
            out.append({"id": tid, "name": k.get("caller_name") or tid})
    return out


@router.get("/v1/admin/appkeys", response_model=None)
async def list_appkeys(request: Request, format: str = "json") -> JSONResponse | PlainTextResponse:
    """凭证列表（读盘即时值）：掩码 appkey＋secret 摘要展示口径，绝不返回明文/完整 appkey。

    每行附 grants（B·授权可见性，_grants_view）：[{service, serviceName, endpoint,
    fieldCount}]——管理员在列表即可判断该凭证能查哪些服务（该续期哪把）。

    ?format=yaml 返回权威源文件原文（控制台「查看 yaml」即真文件，防预览与落盘漂移）。
    """
    _, services, matrix, path = _ctx(request)   # _ctx 顺序＝redis/services/matrix/path（首参 redis；2026-10-14 收口修正）
    doc = appk.load_appkeys(path)
    if format == "yaml":
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")
    rows = appk.list_view(doc)
    services = _namespace_with_proxies(services)   # C4：代理 id 也能解析出服务名/路由
    grants = _grants_view(doc, services)            # B（授权可见性）：kid → 授权服务清单
    for r in rows:
        r["grants"] = grants.get(r["kid"], [])
    return JSONResponse({"keys": rows, "callers": _callers_view(matrix, doc),
                         "sandboxTtlDays": appk.SANDBOX_TTL_DAYS,
                         "rotateOverlapDays": appk.ROTATION_OVERLAP_DAYS,  # P1-4 倒计时文案口径
                         "tenantIssueLimit": appk.TENANT_ISSUE_LIMIT})


@router.get("/v1/admin/appkeys/usage", response_model=None)
async def list_appkeys_usage(request: Request) -> JSONResponse:
    """B6 配额用量（P1-3）：按注册凭证聚合当日「已用/上限」实时值（与门户同口径）。

    一次扫描审计 JSONL（运营台一次看全表，不逐行扫文件）；出参只带 kid＋掩码
    （完整 appkey 不出端点，与 list_view 同纪律）。口径随行 caliber 字段——
    运营台与接入方看到的「已用」必须是同一个数，故复用 my_access.admin_usage，
    不另写一份聚合。
    2026-10-14 凭证管理补全（AC-6）：换 appkeys.usage_view——同源增补
    lastUsedAt（最近调用时间）/everUsed（从未使用标记），今日次数口径不变。
    """
    _, _, _, path = _ctx(request)
    doc = appk.load_appkeys(path)
    return JSONResponse(appk.usage_view(doc, request.app.state.audit.jsonl))


@router.post("/v1/admin/appkeys")
async def issue(request: Request) -> JSONResponse:
    """签发凭证：令牌→限频→纯逻辑签发→网关 consumer→注册表落盘→热加载（免重启生效）。"""
    redis, services, matrix, path = _ctx(request)
    services = _namespace_with_proxies(services)   # C2：代理服务 id 可被显式授权
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": errors.INVALID_JSON, "message": "请求体须为 JSON 对象"}, 400)

    tenant = str(payload.get("tenant") or "")
    # 发放限频（同租户/同 IP 固定窗计数；阈值常量可经环境变量覆盖）
    rl = appk.rate_limit_keys(tenant, _client_ip(request))
    t_count = await redis.incr(rl["tenant"])
    if t_count == 1:
        await redis.expire(rl["tenant"], appk.RATE_LIMIT_WINDOW_S)
    i_count = await redis.incr(rl["ip"])
    if i_count == 1:
        await redis.expire(rl["ip"], appk.RATE_LIMIT_WINDOW_S)
    ok, scope = appk.rate_verdict(int(t_count), int(i_count))
    if not ok:
        return JSONResponse({"error": errors.RATE_LIMITED, "scope": scope,
                             "message": f"{'同租户' if scope == 'tenant' else '同 IP'}签发过于频繁",
                             "retry_after_s": appk.RATE_LIMIT_WINDOW_S}, 429)

    try:
        doc = appk.load_appkeys(path)   # 读盘即时值：多实例/CLI 改动不互相覆盖
        record, secret = mw.credential_issue(
            doc, matrix, services,
            tenant=tenant,
            app_name=str(payload.get("app_name") or ""),
            key_type=str(payload.get("key_type") or ""),
            caller_name=str(payload.get("caller_name") or ""),
            oa_ticket=str(payload.get("oa_ticket") or ""),
            tag_sets=payload.get("tag_sets") or [],
            quota_qps=int(payload.get("quota_qps") or 50),
            quota_daily=int(payload.get("quota_daily") or 10000),
            expires_at=str(payload.get("expires_at") or ""),
            grants=payload.get("grants"))
    except appk.AppkeyIssueError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)

    # 网关先行：consumer（含登记配额限流插件）下发失败则不落盘（避免「登记了却调不通」
    # 或「配额登记了却是另一份口径」的假状态）
    if not await gw_consumer.put_consumer(record["appkey"],
                                          quota_qps=record["quota_qps"],
                                          quota_daily=record["quota_daily"]):
        return JSONResponse({"error": errors.GATEWAY_UNREACHABLE,
                             "message": "网关 Admin API 下发失败，本次未签发（稍后重试）"}, 502)
    doc["keys"].append(record)
    try:
        mw.credential_save(path, doc, secret_plain=secret)
    except Exception:  # noqa: BLE001 —— 落盘失败：回滚网关 consumer，不留半状态
        await gw_consumer.delete_consumer(record["appkey"])
        raise

    _reload(request)   # 热加载：签发后不重启即生效（AC 组成部分）
    view = appk.list_view({"keys": [record]})[0]
    return JSONResponse({"appkey": record["appkey"], "secret": secret,   # secret 仅此一次返回
                         "record": view, "expiresAt": record["expires_at"]})


@router.post("/v1/admin/appkeys/{appkey}/status")
async def set_appkey_status(appkey: str, request: Request) -> JSONResponse:
    """禁用/启用：注册表状态迁移＋网关 consumer 删/建＋热加载（禁用即时 401、启用即时放行）。"""
    _, _, _, path = _ctx(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    action = str((payload or {}).get("action") or "")

    try:
        doc = appk.load_appkeys(path)
        rec = appk.find_record(doc["keys"], appkey)
        if rec is None:
            raise appk.AppkeyIssueError(404, errors.APPKEY_NOT_FOUND,
                                        f"凭证 {appkey} 未在注册表登记")
        record = mw.credential_set_status(doc, rec["appkey"], action)
    except appk.AppkeyIssueError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)

    if action == "disable":
        await gw_consumer.delete_consumer(rec["appkey"])   # 网关侧立即 401（用完整 appkey，非 kid）
    else:
        # 启用须网关确认（含该凭证登记配额的限流插件），否则回滚状态
        if not await gw_consumer.put_consumer(rec["appkey"],
                                              quota_qps=rec.get("quota_qps"),
                                              quota_daily=rec.get("quota_daily")):
            record["status"] = "disabled"
            mw.credential_save(path, doc)
            return JSONResponse({"error": errors.GATEWAY_UNREACHABLE,
                                 "message": "网关 Admin API 下发失败，凭证保持禁用"}, 502)
    mw.credential_save(path, doc)
    _reload(request)
    return JSONResponse({"record": appk.list_view({"keys": [record]})[0]})


@router.post("/v1/admin/appkeys/{appkey}/rotate")
async def rotate_appkey_ep(appkey: str, request: Request) -> JSONResponse:
    """轮换凭证（P1-4）：签发新 key（原身份/授权/配额继承）＋旧 key 进入重叠期。

    网关先行：新 key consumer 下发失败则不落盘（旧 key 保持原状，不留「登记了却调不通」
    的新凭证）。旧 key 重叠期内仍可用，期满由 resolve_grant 自动失效
    （credential_rotated_expired）。secret 仅本次响应返回一次。
    """
    _, services, matrix, path = _ctx(request)
    services = _namespace_with_proxies(services)   # C2：轮换继承代理授权时同口径可解析
    payload = await _payload(request)
    try:
        qps = int(payload["quota_qps"]) if payload.get("quota_qps") not in (None, "") else None
        daily = int(payload["quota_daily"]) if payload.get("quota_daily") not in (None, "") else None
    except (TypeError, ValueError):
        return JSONResponse({"error": errors.INVALID_QUOTA, "message": "quota_qps/quota_daily 须为正整数"}, 400)

    try:
        doc = appk.load_appkeys(path)     # 读盘即时值：多实例/CLI 改动不互相覆盖
        new_rec, secret, old_rec = mw.credential_rotate(
            doc, matrix, services, appkey, quota_qps=qps, quota_daily=daily)
    except appk.AppkeyIssueError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)

    if not await gw_consumer.put_consumer(new_rec["appkey"],
                                          quota_qps=new_rec["quota_qps"],
                                          quota_daily=new_rec["quota_daily"]):
        return JSONResponse({"error": errors.GATEWAY_UNREACHABLE,
                             "message": "网关 Admin API 下发失败，本次未轮换（旧凭证保持原状，稍后重试）"}, 502)
    doc["keys"].append(new_rec)
    try:
        mw.credential_save(path, doc, secret_plain=secret)
    except Exception:  # noqa: BLE001 —— 落盘失败：回滚网关 consumer，不留半状态
        await gw_consumer.delete_consumer(new_rec["appkey"])
        raise

    _reload(request)   # 热加载：新旧 key 即时生效/进入重叠期
    return JSONResponse({
        "appkey": new_rec["appkey"], "secret": secret,          # secret 仅此一次返回
        "record": appk.list_view({"keys": [new_rec]})[0],
        "previous": appk.list_view({"keys": [old_rec]})[0],
        "expiresAt": new_rec["expires_at"],
        "overlapExpiresAt": old_rec.get("overlap_expires_at"),
        "overlapDays": appk.ROTATION_OVERLAP_DAYS,
    })


@router.post("/v1/admin/appkeys/{appkey}/quota")
async def set_appkey_quota(appkey: str, request: Request) -> JSONResponse:
    """调整配额：登记值落权威源＋网关限流插件同步（P1-5 起同源生效）。

    顺序＝先落盘登记（单一事实来源）再下发网关：网关不可达时如实报 502，登记值
    已更新（下次启用/轮换会重新同步）——不假装限流已按新值生效。
    """
    _, _, _, path = _ctx(request)
    try:
        payload = await request.json()
        qps, daily = int(payload.get("quota_qps") or 0), int(payload.get("quota_daily") or 0)
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": errors.INVALID_JSON, "message": "请求体须为 JSON 对象"}, 400)
    if qps < 1 or daily < 1:
        return JSONResponse({"error": errors.INVALID_QUOTA, "message": "QPS 与日次配额须为正整数"}, 400)
    doc = appk.load_appkeys(path)
    rec = appk.find_record(doc["keys"], appkey)
    if rec is None:
        return JSONResponse({"error": errors.APPKEY_NOT_FOUND,
                             "message": f"凭证 {appkey} 未在注册表登记"}, 404)
    rec = mw.credential_quota_set(doc, rec["appkey"], qps, daily)
    mw.credential_save(path, doc)
    if not await gw_consumer.put_consumer(rec["appkey"], quota_qps=qps, quota_daily=daily):
        return JSONResponse({"error": errors.GATEWAY_UNREACHABLE,
                             "message": "配额已登记，但网关限流插件同步失败（稍后重试）",
                             "record": appk.list_view({"keys": [rec]})[0]}, 502)
    _reload(request)
    return JSONResponse({"record": appk.list_view({"keys": [rec]})[0]})


@router.post("/v1/admin/appkeys/{appkey}/delete")
async def delete_appkey_ep(appkey: str, request: Request) -> JSONResponse:
    """受限删（AC-2，2026-10-14 凭证管理补全）：仅「从未被调用」的凭证可删；
    有调用记录一律 409（人话文案指引禁用——审计账不断链）。
    成功＝注册表移除＋网关 consumer 清除＋热加载＋审计事件（admin:appkey_delete，
    复用 _audit_admin_action 既有通道）。调用计数＝审计 JSONL 聚合（appk.scan_usage）。"""
    _, _, _, path = _ctx(request)
    doc = appk.load_appkeys(path)
    rec = appk.find_record(doc["keys"], appkey)
    if rec is None:
        return JSONResponse({"error": errors.APPKEY_NOT_FOUND,
                             "message": f"凭证 {appkey} 未在注册表登记"}, 404)
    full = rec["appkey"]
    usage = appk.scan_usage(request.app.state.audit.jsonl, [full])
    used_total = int((usage.get(full) or {}).get("total") or 0)
    t0 = time.monotonic()
    try:
        removed = mw.credential_delete(doc, full, used_count=used_total)
    except appk.AppkeyIssueError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message,
                             "callCount": used_total}, exc.status)
    mw.credential_save(path, doc)
    await gw_consumer.delete_consumer(full)   # 网关侧同步清除（404 视作成功，同禁用口径）
    _reload(request)
    audit_id = _audit_admin_action(
        request, "appkey_delete", "admin",
        {"appkey": appk.record_kid(full), "callerName": removed.get("caller_name"),
         "appName": removed.get("app_name"), "callCount": used_total},
        {"deleted": True, "expiresAt": removed.get("expires_at")},
        int((time.monotonic() - t0) * 1000))
    return JSONResponse({"deleted": True, "callCount": used_total, "auditId": audit_id,
                         "record": appk.list_view({"keys": [removed]})[0]})


@router.post("/v1/admin/appkeys/{appkey}/expires")
async def renew_appkey_ep(appkey: str, request: Request) -> JSONResponse:
    """续期（AC-3）：只能延长 expires_at（缩短 409 指引「作废请用禁用」）；
    支持 permanent（永久）。到期判定在服务层 resolve_grant（网关 consumer 不含
    到期日），无需网关变更。成功留审计事件（admin:appkey_renew）。"""
    _, _, matrix, path = _ctx(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    expires_at = str(((payload or {}).get("expires_at")) or "")
    doc = appk.load_appkeys(path)
    rec = appk.find_record(doc["keys"], appkey)
    if rec is None:
        return JSONResponse({"error": errors.APPKEY_NOT_FOUND,
                             "message": f"凭证 {appkey} 未在注册表登记"}, 404)
    # 冻结联动门（管理功能群一期 §1.5③）：frozen 租户禁止续期（端点层门——服务层
    # renew_appkey 不持 matrix，签发/轮换门在 appkeys 服务层、本处同口径补齐）
    try:
        gam.ensure_not_frozen(matrix, str(rec.get("tenant") or ""), "凭证续期")
    except gam.GrantAdminError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)
    t0 = time.monotonic()
    try:
        rec = mw.credential_renew(doc, rec["appkey"], expires_at)
    except appk.AppkeyIssueError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)
    mw.credential_save(path, doc)
    _reload(request)
    audit_id = _audit_admin_action(
        request, "appkey_renew", "admin",
        {"appkey": appk.record_kid(rec["appkey"]), "expiresAt": rec["expires_at"]},
        {"expiresAt": rec["expires_at"], "renewedAt": rec.get("renewed_at")},
        int((time.monotonic() - t0) * 1000))
    return JSONResponse({"record": appk.list_view({"keys": [rec]})[0], "auditId": audit_id})


@router.post("/v1/admin/reload")
async def reload_auth(request: Request) -> JSONResponse:
    """热加载端点：外部（auth_admin CLI／手编）改文件后免重启生效（P0-4 最小实现）。"""
    state = _reload(request)
    return JSONResponse(state)


def _reload(request: Request) -> dict:
    """重读授权矩阵＋凭证注册表进 app.state/main 模块全局（main.reload_auth_state 复用）。"""
    from app.main import reload_auth_state   # 延迟导入：main 顶部 import 本路由，避免环
    return reload_auth_state(request.app)


# ---------- 发布台＋上游代理写区（P1-10/11 · D2 写侧接真） ----------
# 决策全在 app/ops_config.py（原子写＋校验＋真加载器零漂移证明）；此处仅 HTTP 编排。
# git 留痕口径占位（P1-10/11）：容器内无 git 二进制/无 .git 挂载，本端点只落
# trace{action,by,at,commit:null} 登记字段，不伪造 commit hash；真实 commit 归
# 宿主侧 git 执行器（M2-4 门槛稿口径）。热加载已接通（services.yaml＝registry_watcher、
# upstreams.yaml＝proxy_hub 看门，均 mtime 轮询幂等收敛）；写后无需显式 reload；
# GET 一律读盘即时值，写后控制台立即可见。
GIT_PLACEHOLDER_NOTE = "git 留痕占位：文件已落仓库路径，commit 待宿主侧 git 执行（不伪造 hash）"


def _audit_admin_action(request: Request, action: str, operator: str, params: dict,
                        response: dict, latency_ms: int, status: int = 200) -> str:
    """卡4 NC-2c：发布/下线等管理动作记审计八元组（endpoint 语义＝admin:<action>）。

    写入器经 app.state.audit（main lifespan 装配的同一 AuditWriter；单测/离线场景
    缺位＝如实跳过，不阻断业务返回）。参数与响应经 make_entry 只存 sha256 摘要。
    返回 auditId＝trace_id（无则参数摘要前 12 位），回填给调用方响应。
    """
    writer = getattr(request.app.state, "audit", None)
    if writer is None:
        return ""
    trace_id = str(getattr(request.state, "trace_id", "") or "")
    try:
        entry = make_entry(caller=operator or "admin", endpoint=f"admin:{action}",
                           params=json.dumps(params, ensure_ascii=False, sort_keys=True),
                           status=status, latency_ms=latency_ms,
                           response_body=json.dumps(response, ensure_ascii=False,
                                                    sort_keys=True),
                           trace_id=trace_id)
        writer.append(entry)
    except Exception:  # noqa: BLE001 —— 审计写失败不阻断发布/下线业务返回
        return ""
    return trace_id or entry.params_digest[:12]


async def _payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


async def _strict_payload(request: Request):
    """发布族端点严格 JSON 解析：malformed/非 JSON 对象 → (None, 400 invalid_json 信封)。

    P2（2026-10-08）：发布族 POST 此前把解析失败静默当空体（_payload→{}），错形请求
    落进误导性的字段级 4xx（如 invalid_name）；fields 等非对象数组的错形体更是直穿
    纯逻辑层抛未捕获 TypeError/AttributeError → 500 无信封。对齐 appkeys 端点既有
    口径（issue/quota 对错形体 invalid_json 400）：错形是调用方问题，4xx＋机读码。
    """
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return None, JSONResponse({"error": errors.INVALID_JSON,
                                   "message": "请求体不是合法 JSON"}, 400)
    if not isinstance(payload, dict):
        return None, JSONResponse({"error": errors.INVALID_JSON,
                                   "message": "请求体须为 JSON 对象"}, 400)
    return payload, None


def _obj_list(value) -> bool:
    """「对象数组」形状判据（fields 等：list 且元素全为 dict）。"""
    return isinstance(value, list) and all(isinstance(x, dict) for x in value)


def _ops_error_response(exc: opsc.OpsConfigError) -> JSONResponse:
    return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)


def _hbase_client(request: Request):
    """取运行期 HBase 客户端；**无池或池已失效时惰性重建一次（自愈）**。

    背景（2026-09-23 手工走查发现的真实缺陷）：`app.state.hbase_client` 只在 lifespan
    启动时建一次；若 service 先于 HBase 容器启动，池里是**死连接**且被永久缓存
    → 之后所有 discovery/load 都报 `TSocket read 0 bytes / Broken pipe`，
    **只能靠重启 service 恢复**（演示栈上实测复现）。
    修法＝取用时做一次**轻量健康检查**（list_tables）；失败则**重建池**并写回 state，
    仍失败则返回 None，由调用方按既有口径如实报 503（不返回假元数据）。
    """
    client = getattr(request.app.state, "hbase_client", None)
    healthy = False
    if client is not None:
        try:
            client.tables()                     # 轻量探活：失败即认为池已死
            healthy = True
        except Exception:                       # noqa: BLE001 —— 任何 thrift 异常都当"池失效"
            healthy = False
    if healthy:
        return client
    # 无池 / 池已失效 → 重建一次（自愈）。建池失败返回 None（调用方如实 503）。
    # 惰性 import：main 在模块顶层 import 本模块（装 router），此处再 import main 会成环，
    # 故**函数内**导入（仅重建这条极少走的路径需要）。
    from app import main as _main
    fresh = _main._build_hbase_client()
    request.app.state.hbase_client = fresh
    _main.hbase_client = fresh
    return fresh


def _hbase_endpoint(request: Request) -> str:
    """当前生效的 HBase 端点串（诊断文案用；取 lifespan 装配的连接参数，缺席现算）。"""
    conn = getattr(request.app.state, "hbase_connection", None)
    if conn is None:
        try:
            conn = connections.load_hbase_connection()
        except Exception:  # noqa: BLE001 —— 诊断文案不因配置坏而二次抛错
            return "配置的 HBase 地址"
    return f"{conn.host}:{conn.port}"


def _hbase_unavailable(request: Request) -> JSONResponse:
    """HBase 不可用如实 503（卡1 三分类之「不可达」：机读码＋分类＋行动指引）。"""
    return JSONResponse(
        {"error": "HBASE_UNAVAILABLE", "category": "unreachable",
         "message": (f"HBase 连接池未就绪（{_hbase_endpoint(request)} 建池失败或不可达）；"
                     "数据源发现/试查需要真连接，不返回假元数据。开发态一键起："
                     "bash scripts/up_hbase.sh；生产态请核对 MRS 地址/端口与网络策略。")},
        503)


async def _discover(request: Request, client=None) -> dict:
    """HBase meta 发现（阻塞 Thrift 调用经线程池；超时可配 HBASE_DISCOVER_TIMEOUT_S）。

    HBase 不可达/无连接池→HBaseQueryError 上抛，端点层如实回 503（不静默回退假数据）。
    client 可由调用方传入复用（discovery 端点同请求内还要做 P0-1 scan 采样）。
    """
    if client is None:
        client = _hbase_client(request)
    if client is None:
        # 卡1：把最近一次建池失败的**底层异常**原样上抛——端点层按三分类归因
        #（happybase 连不上＝不可达〔含未启动〕，缺驱动＝other 保留 HBASE_DRIVER_MISSING）。
        from app import main as _main
        cause = getattr(_main, "hbase_pool_error", None)
        if cause is not None:
            raise cause
        raise hbx.HBaseQueryError(
            503, "HBASE_UNAVAILABLE",
            "HBase 连接池未就绪（建池失败原因见服务端日志）；数据源发现需要真连接，"
            "不返回假元数据")
    timeout_s = float(os.environ.get("HBASE_DISCOVER_TIMEOUT_S", "10"))
    return await asyncio.wait_for(asyncio.to_thread(client.discover), timeout=timeout_s)


@router.get("/v1/admin/publish/hbase/discovery", response_model=None)
async def hbase_discovery(request: Request) -> JSONResponse:
    """AC-4 数据源发现（M2-5 契约）：HBase meta（namespace/表/列族）×字段字典 CSV 双源校验，
    按业务域目录树分组展示。

    **列（qualifier）来自字段字典 CSV**——HBase meta 只到列族（M2-5 真容器实测：meta 不
    提供列级元数据），这与 AC-4「双源校验」口径一致。两源不一致（HBase 有字典外列）
    → dualSource.status=mismatch＋mismatches[]（发布前置拦截依据）。HBase 不可达→503
    HBASE_UNAVAILABLE（如实报错，不静默回退成假数据）。
    """
    client = _hbase_client(request)
    endpoint = _hbase_endpoint(request)   # 卡1：诊断文案带当前生效 host:port（双轨装配值）
    try:
        meta = await _discover(request, client)
    except hbx.HBaseQueryError as exc:
        # 卡1：失败诊断三分类（不可达〔含未启动，文案带行动指引〕/认证失败/其他）
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": d["message"]}, d["status"])
    except asyncio.TimeoutError as exc:
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)   # 超时＝不可达类
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": f"发现超时（HBASE_DISCOVER_TIMEOUT_S）。{d['message']}"},
                             d["status"])
    except Exception as exc:  # noqa: BLE001 —— 未预期异常如实分类回错，不静默
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": d["message"]}, d["status"])
    # P0-1：示例键＝每表 scan(limit=1) 实取真实 rowkey；空库/失败＝留空＋如实标注原因
    rowkey_samples: dict[str, str] = {}
    sample_note = "示例键留空：HBase 不可达，未采样（真连接才采样，不造假键）"
    if client is not None:
        try:
            rowkey_samples, sample_note = await asyncio.wait_for(
                asyncio.to_thread(opsc.collect_rowkey_samples, meta, client),
                timeout=float(os.environ.get("HBASE_DISCOVER_TIMEOUT_S", "10")))
        except asyncio.TimeoutError:
            sample_note = "示例键留空：scan 采样超时（HBASE_DISCOVER_TIMEOUT_S）"
        except Exception as exc:  # noqa: BLE001 —— 采样失败不拖垮发现响应，如实标注
            sample_note = f"示例键留空：scan 采样失败：{str(exc)[:120]}"
    return JSONResponse(opsc.build_datasource_view(
        meta, rowkey_samples=rowkey_samples, sample_key_note=sample_note))


@router.post("/v1/admin/publish/hbase/test", response_model=None)
async def hbase_test(request: Request) -> JSONResponse:
    """AC-5 真试查（M2-5 契约）：以示例键对 HBase 真实点查，返回 {passed,code,elapsedMs,body}。

    请求 {namespace,table,families[],fields[{name,cf,sample,desc}],keyType,sampleKey,timeoutMs}。
    平台只读（门槛稿：HBase 数据由数仓 T+1 直写，平台零写逻辑）。HBase 不可达→503；
    缺示例键/键格式非法→passed=false＋机读码（门禁语义：前端据此 disable 发布按钮）。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    # P2：形状闸前置（错形体不得等 HBase 探活才炸）——fields 元素参与 f.get('name')
    # 拼列名，非对象元素曾漏成未捕获 AttributeError → 500 无信封
    if p.get("fields") not in (None, "") and not _obj_list(p.get("fields")):
        return JSONResponse({"error": errors.INVALID_REQUEST,
                             "message": "fields 须为对象数组（每项 {name, cf, …}）"}, 400)
    try:
        timeout_ms = int(p.get("timeoutMs") or 3000)
    except (TypeError, ValueError):
        return JSONResponse({"error": errors.INVALID_REQUEST,
                             "message": "timeoutMs 须为整数（毫秒）"}, 400)
    client = _hbase_client(request)
    if client is None:
        return _hbase_unavailable(request)

    key_type = str(p.get("keyType") or "phone_sm3").strip()
    sample_key = str(p.get("sampleKey") or "").strip()
    if key_type not in opsc.HBASE_KEY_TYPES:
        return JSONResponse({"passed": False, "code": 400, "elapsedMs": 0,
                             "error": errors.INVALID_KEY_TYPE,
                             "message": f"键类型未定/非法：{key_type or '(空)'}",
                             "body": None}, 200)
    if not sample_key:
        return JSONResponse({"passed": False, "code": 400, "elapsedMs": 0,
                             "error": "sample_key_required",
                             "message": "缺示例键：真试查门禁要求以示例键实查（不编造键）",
                             "body": None}, 200)
    namespace = str(p.get("namespace") or "default").strip()
    table = str(p.get("table") or "").strip()
    if not table:
        return JSONResponse({"passed": False, "code": 400, "elapsedMs": 0,
                             "error": "invalid_hbase_table", "message": "表名必填",
                             "body": None}, 200)
    # 表名口径与 services.yaml backend.hbase.table 一致（namespace:table；demo_hbase_query 同款）
    full_table = table if ":" in table else f"{namespace}:{table}"
    cf = ""
    fams = p.get("families") or []
    if isinstance(fams, list) and fams:
        cf = str(fams[0] or "").strip()
    if not cf:
        cf = "cf"
    columns = [f"{cf}:{str(f.get('name') or '').strip()}"
               for f in (p.get("fields") or []) if str(f.get("name") or "").strip()]

    started = time.monotonic()
    try:
        row = await asyncio.wait_for(
            asyncio.to_thread(client.get, full_table, sample_key, columns or None),
            timeout=max(timeout_ms, 1) / 1000)
    except asyncio.TimeoutError:
        return JSONResponse({"passed": False, "code": 503, "elapsedMs": timeout_ms,
                             "error": "HBASE_UPSTREAM_ERROR",
                             "message": f"试查超时（>{timeout_ms}ms）", "body": None}, 200)
    except hbx.HBaseQueryError as exc:
        return JSONResponse({"passed": False, "code": exc.status, "elapsedMs": 0,
                             "error": exc.code, "message": exc.message, "body": None}, 200)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"passed": False, "code": 503, "elapsedMs": 0,
                             "error": "HBASE_UPSTREAM_ERROR",
                             "message": f"试查失败：{str(exc)[:200]}", "body": None}, 200)
    elapsed = int((time.monotonic() - started) * 1000)
    found = bool(row)
    return JSONResponse({
        "passed": found, "code": 200 if found else 404, "elapsedMs": elapsed,
        "error": None if found else "row_not_found",
        "message": "试查命中" if found else "行不存在（示例键在真库无对应行；不伪造回显）",
        "body": {k.split(":", 1)[1]: v for k, v in row.items()},
    }, 200)


@router.get("/v1/admin/publish/hbase/selftest", response_model=None)
async def hbase_selftest(request: Request) -> JSONResponse:
    """AC-5 headless 机检端点（M2-5 契约）：{passed,total,assertions[{id,name,ok,detail}]}。

    HBase 不可达 → 相关断言 ok=false（如实标红，不跳过不谎报通过）；字典真源/双源校验/
    发布拦截/渲染器四项纯逻辑断言照跑。前端机检面板直读本端点。
    """
    meta = None
    reachable, reach_err = False, ""
    try:
        meta = await _discover(request)
        reachable = True
    except hbx.HBaseQueryError as exc:
        reach_err = exc.message
    except asyncio.TimeoutError:
        reach_err = "HBase 元数据发现超时（HBASE_DISCOVER_TIMEOUT_S）"
    except Exception as exc:  # noqa: BLE001
        reach_err = f"HBase 元数据发现失败：{str(exc)[:200]}"
    assertions = opsc.hbase_selftest_assertions(
        meta, reachable=reachable, reach_error=reach_err,
        services=getattr(request.app.state, "services", None))
    passed = sum(1 for a in assertions if a["ok"])
    return JSONResponse({"passed": passed == len(assertions) and len(assertions) > 0,
                         "total": len(assertions), "passedCount": passed,
                         "assertions": assertions,
                         "checkedAt": opsc._now_iso()})


@router.get("/v1/admin/publish", response_model=None)
async def publish_console(request: Request, format: str = "json") -> JSONResponse | PlainTextResponse:
    """发布台整页数据（读盘即时值）：目录树由真源生成（字段字典 CSV×services.yaml
    注册区），services 列表＝console_services 写区条目。?format=yaml 出权威源原文。"""
    if format == "yaml":
        return PlainTextResponse(opsc.SERVICES_YAML.read_text(encoding="utf-8"),
                                 media_type="text/plain; charset=utf-8")
    return JSONResponse(opsc.build_console_view())


@router.post("/v1/admin/publish/hbase/load", response_model=None)
async def hbase_load_ep(request: Request) -> JSONResponse:
    """【本卡核心】HBase（冷存储）→ Redis（热存储）加载：页面选表 → 自动加载到 Redis。

    复用既有加载链路（sync/hbase_to_redis.py 的读写＋sync/hbase_reconcile.py 的对账
    compare/read_*，经 app/hbase_load.py 编排，**零第二实现**）；本端点只做 HTTP 编排
    （令牌闸 / JSON / 阻塞 Thrift 调用入线程池）。

    请求 {namespace,table,cf,columns[],keys[],version,replace,sourceCsv}：
      - version 缺省＝后端按当日生成（YYYYMMDD），与 sync/loader.py 同口径；
      - columns 缺省＝列族全部列（整表 scan）；keys 给出＝按 rowkey 批量点查；
      - replace=true＝目标空间非空时先清空重灌（幂等闸，沿既有口径）；
      - sourceCsv（**开发期可选**，透传既有加载器 --source-csv）：记录源指纹供既有
        sync/reconcile.py 前置查复用——同一份造数 CSV 灌 HBase 再加载的场景下，
        该指纹与 CSV 一致，方可通过 switch.py 的对账闸上线。生产从 HBase 加载时
        不需要（源指纹＝HBase 快照 hbase_snapshot_sha256，仍如实记录）。

    响应：加载结果（loaded/tagCols/elapsedMs）＋同源对账（reconcile{pass,diffCount,checks}）。
    幂等/失败：目标空间非空且未 replace → 409（拒灌，零副作用）；version==线上版 → 拒灌；
    HBase/Redis 不可达 → 503 如实报错（**不写一滴脏数据、不回退假数据**）。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    # P2：columns/keys 给出时须为数组（int 等不可迭代错形体曾漏成 TypeError → 500）
    for _list_field in ("columns", "keys"):
        _v = p.get(_list_field)
        if _v not in (None, "") and not isinstance(_v, list):
            return JSONResponse({"error": errors.INVALID_REQUEST,
                                 "message": f"{_list_field} 须为数组（元素为字符串）"}, 400)
    norm = opsc.normalize_publish_payload(p)
    table = norm["hbase"]["table"]
    cf = norm["hbase"]["cf"]
    if not table or ":" not in table:
        namespace = str(p.get("namespace") or "default").strip() or "default"
        table = table if ":" in table else f"{namespace}:{table}"
    if not cf:
        fams = p.get("families") or []
        cf = str(fams[0]).strip() if isinstance(fams, list) and fams else "cf"
    version = str(p.get("version") or "").strip() or time.strftime("%Y%m%d")
    columns = [str(c).strip() for c in (p.get("columns") or []) if str(c).strip()] or None
    keys = [str(k).strip() for k in (p.get("keys") or []) if str(k).strip()] or None
    source_csv = str(p.get("sourceCsv") or p.get("source_csv") or "").strip() or None
    try:
        res = await asyncio.wait_for(
            asyncio.to_thread(
                hbl.load_into_redis,
                table=table, cf=cf, version=version, columns=columns, rowkeys=keys,
                replace=bool(p.get("replace")), source_csv=source_csv),
            timeout=float(os.environ.get("HBASE_LOAD_TIMEOUT_S", "300")))
    except asyncio.TimeoutError:
        return JSONResponse({"ok": False, "error": "LOAD_TIMEOUT",
                             "message": "加载超时（HBASE_LOAD_TIMEOUT_S）——未确认完成，"
                                        "如需重灌请显式 replace"}, 504)
    except hbl.HBaseLoadServiceError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message,
                             "version": version, "table": table, "cf": cf,
                             "fallback": "无（HBase/Redis 不可达即如实报错，不写脏、不回退假数据）"},
                            exc.status)
    except Exception as exc:  # noqa: BLE001 —— 未预期异常如实回 503，不静默
        return JSONResponse({"ok": False, "error": "LOAD_UPSTREAM_ERROR",
                             "message": f"加载失败：{str(exc)[:200]}"}, 503)
    # HTTP 契约边界：服务层报告（snake，＝既有加载器口径）归一为前端契约（camelCase）——
    # 唯一解析点仍是适配层（publish-hbase-adapter.js normalizeLoadResult）。
    return JSONResponse({
        "ok": True, "mode": "hbase->redis-load",
        "table": table, "cf": cf, "version": version,
        "loaded": res.get("loaded"), "tagCols": res.get("tag_cols"),
        "nonemptyCells": res.get("nonempty_cells"),
        "clearedBeforeLoad": res.get("cleared_before_load"),
        "coverageSrc": res.get("coverage_src"),
        "sourceSha256": res.get("source_sha256"),
        "dataAsOf": res.get("data_as_of"),
        "elapsedMs": res.get("elapsedMs"),
        "reconcile": {"pass": (res.get("reconcile") or {}).get("pass"),
                      "diffCount": (res.get("reconcile") or {}).get("diffCount"),
                      "checks": (res.get("reconcile") or {}).get("checks") or {},
                      "diffs": (res.get("reconcile") or {}).get("diffs") or [],
                      "error": (res.get("reconcile") or {}).get("error", "")},
    }, 200)



# ══════════════════ 卡2：数据装载域（流程① · B3.1 契约端点×5） ══════════════════
# 设计依据：开工门槛-卡2 §B3.1（Lead 已冻结）＋装载与发布流程拆分设计_v1 §二。
# 纪律：本域端点只做 HTTP 编排（JSON/线程池/错误信封）；装载逻辑全复用
# hbl.load_into_redis（零第二实现）；发现＝HBaseClient.discover（不读 services.yaml）；
# 已装载信息＝tabver 索引（sync:schema / sync:tables / sync:tabver，卡3 同源消费）。
# 不变量：绝不触碰 tags:<表>:latest 指针；装载页不提供 replace（严格幂等，NC-2）。

def _sync_mod(name: str):
    """取 sync/ 既有模块（经 hbl 的目录定位：env SYNC_DIR > 容器 /sync > 宿主 sync/）。"""
    return hbl._import(name)


def _load_sources_view(auth_mode: str, endpoint: str, probe_exc: BaseException | None,
                       table_count: int | None) -> dict:
    """B3.1-1 纯拼装：源卡片（探活异常→卡1 三分类归因；None＝ok）。"""
    if probe_exc is None:
        status = "ok"
        category = code = message = None
    else:
        d = hbx.classify_hbase_failure(probe_exc, endpoint=endpoint)
        status = d["category"]
        category, code, message = d["category"], d["code"], d["message"]
    return {"ok": True, "sources": [{
        "id": "hbase", "name": "HBase", "kind": "column_family_kv",
        "endpoint": endpoint, "authMode": auth_mode,
        "status": status, "category": category, "code": code, "message": message,
        "tableCount": table_count,
    }]}


@router.get("/v1/admin/load/sources", response_model=None)
async def load_sources_ep(request: Request) -> JSONResponse:
    """B3.1-1 源与连接状态：当前唯一源 HBase；状态＝运行期连接池真探活（不假报）。"""
    conn = getattr(request.app.state, "hbase_connection", None)
    if conn is None:
        conn = connections.load_hbase_connection()
    endpoint = f"{conn.host}:{conn.port}"
    client = _hbase_client(request)   # 惰性自愈（死池重建一次）
    probe_exc: BaseException | None = None
    table_count = None
    if client is None:
        from app import main as _main
        probe_exc = getattr(_main, "hbase_pool_error", None) or hbx.HBaseQueryError(
            503, "HBASE_UNAVAILABLE", "HBase 连接池未就绪（建池失败原因见服务端日志）")
    else:
        try:
            names = await asyncio.wait_for(asyncio.to_thread(client.tables),
                                           timeout=float(os.environ.get("HBASE_DISCOVER_TIMEOUT_S", "10")))
            table_count = len(names)
        except Exception as exc:  # noqa: BLE001 —— 探活失败如实归因（三分类）
            probe_exc = exc
    return JSONResponse(_load_sources_view(conn.auth_mode, endpoint, probe_exc, table_count))


_SYNC_REDIS = None


def _sync_redis():
    """同步 Redis 客户端（SYNC_REDIS_URL，与 hbl 装载链同源同库；懒建缓存复用）。

    卡2 实测教训：app.state.redis 是 redis.asyncio 客户端，而 sync/ 系列（schema_meta/
    common/索引）是同步实现（CLI 共用）——把 async 客户端传进同步模块会得到
    'coroutine' object is not iterable。装载/索引路径统一走本同步客户端（零第二实现、
    与既有 POST load 链 h2r.redis_conn() 同一连接口径）。
    """
    global _SYNC_REDIS
    if _SYNC_REDIS is None:
        _SYNC_REDIS = hbl._import("hbase_to_redis").redis_conn()
    return _SYNC_REDIS


def _tabver_snapshot(r, meta: dict) -> dict:
    """阻塞段（to_thread 内跑，同步客户端）：meta 全表 × tabver 索引＋live 指针 → loaded 段。

    Redis 异常按表兜底：versions 空＋诊断分类 note（不透原始异常文案——傻问题扫描
    SQ-02 口径：错误态走分类文案＋行动指引；异常类型仅作定位线索，详情归服务端日志）。
    """
    def _redis_fail_note(exc: BaseException) -> str:
        name = type(exc).__name__
        if "Connection" in name or "Timeout" in name:
            return ("已装载信息暂不可用（Redis 不可达）——表发现不受影响，"
                    "装载索引待 Redis 恢复后刷新；服务端日志有连接详情")
        return (f"已装载信息暂不可用（Redis 异常：{name}）——"
                "表发现不受影响；详情见服务端日志")

    sm, C, common = _sync_mod("schema_meta"), _sync_mod("sync_config"), _sync_mod("common")
    counts = _scan_space_counts(r)   # P2 举一反三（维护者 16:40 实测回打）：表清单与资产总览同口径滤幽灵版
    out: dict[str, dict] = {}
    for ns, tbls in (meta.get("namespaces") or {}).items():
        for tbl in tbls:
            full = tbl if ns == "default" else f"{ns}:{tbl}"
            entry = {"versions": [], "latestVersion": None}
            try:
                entry["versions"] = [v for v in sm.tabver_versions(r, full)
                                     if counts.get((full, v), 0) > 0]   # 数据面键数>0 才算「已装载」（数据键 0＝tabver 惰性残留）
                with C.table_scope(full):
                    entry["latestVersion"] = common.get_live(r)
            except Exception as exc:  # noqa: BLE001 —— 索引侧故障如实标注，不静默空
                entry["note"] = _redis_fail_note(exc)
            out[full] = entry
    return out


def _load_tables_view(meta: dict, tables_meta: dict, snapshot: dict) -> dict:
    """B3.1-2 纯拼装：discovery meta × tables_meta × tabver 快照 → 表清单视图。"""
    tables = []
    for ns in sorted(meta.get("namespaces") or {}):
        for tbl in sorted((meta.get("namespaces") or {}).get(ns) or {}):
            full = tbl if ns == "default" else f"{ns}:{tbl}"
            info = (meta["namespaces"][ns].get(tbl) or {})
            m = tables_meta.get(full) or {}
            loaded = dict(snapshot.get(full) or {"versions": [], "latestVersion": None})
            row = {
                "full": full, "namespace": ns, "table": tbl,
                "tableCnName": m.get("cn_name", ""), "tableDesc": m.get("desc", ""),
                "families": [{"name": cf, "sampleColumns": cols}
                             for cf, cols in sorted((info.get("families") or {}).items())],
                "loaded": {"versions": loaded.get("versions", []),
                           "latestVersion": loaded.get("latestVersion")},
            }
            if loaded.get("note"):
                row["loaded"]["note"] = loaded["note"]
            tables.append(row)
    return {"ok": True, "source": "hbase", "reachable": True,
            "checkedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tableCount": meta.get("table_count", len(tables)), "tables": tables}


@router.get("/v1/admin/load/tables", response_model=None)
async def load_tables_ep(request: Request) -> JSONResponse:
    """B3.1-2 HBase 真表清单（发现/装载是数据面的事，判定链零注册表依赖）。

    HBase 不可达→503 三分类（与发现端点同口径：机读码/分类/行动指引，不返回假清单）。
    P3 追加（2026-10-08）：每表带 keyHint（查询主键描述「usr_id 的 phone_sm3」）——
    唯一读注册表 console 写区的一处（backend.load.table → 条目 key），展示专用、
    装载态无关（未装载表同样带出）；发现/装载判定链仍不读注册表（AC-2 口径不变）。
    """
    client = _hbase_client(request)
    endpoint = _hbase_endpoint(request)
    try:
        meta = await _discover(request, client)
    except hbx.HBaseQueryError as exc:
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": d["message"]}, d["status"])
    except asyncio.TimeoutError as exc:
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": f"发现超时（HBASE_DISCOVER_TIMEOUT_S）。{d['message']}"},
                            d["status"])
    except Exception as exc:  # noqa: BLE001 —— 未预期异常如实分类回错，不静默
        d = hbx.classify_hbase_failure(exc, endpoint=endpoint)
        return JSONResponse({"error": d["code"], "category": d["category"],
                             "message": d["message"]}, d["status"])
    tables_meta = opsc.load_tables_meta()
    snapshot = await asyncio.to_thread(_tabver_snapshot, _sync_redis(), meta)
    view = _load_tables_view(meta, tables_meta, snapshot)
    # P3 追加：keyHint（查询主键行）——注册表 console 写区 backend.load.table → 条目
    # key 描述；注册态决定、装载态无关。仅展示用，发现/装载判定零注册表依赖（AC-2）。
    # 未登记表＝空串如实透出（不猜默认键型）；先全名后裸名（注册口径＝裸表名）。
    hints: dict[str, str] = {}
    for e in opsc.load_console_services():
        b = e.get("backend") or {}
        tbl = str((b.get("load") or {}).get("table")
                  or (b.get("hbase") or {}).get("table") or "").strip()
        if tbl:
            hints.setdefault(tbl, str(e.get("key") or "").strip())
    for row in view["tables"]:
        row["keyHint"] = (hints.get(row["full"])
                          or hints.get(row["full"].rsplit(":", 1)[-1]) or "")
    return JSONResponse(view)


@router.post("/v1/admin/load", response_model=None)
async def load_ep(request: Request) -> JSONResponse:
    """B3.1-3 装载（全量，B2 2026-10-09 起异步）：校验后启动后台任务，立即回 taskId。

    严格幂等（NC-2）：replace 一律强制 False（覆盖重灌归运维 CLI/旧发布页）；请求带
    columns/replace 时显式忽略并在响应注明（fullLoad/columnsIgnored/replaceIgnored）。
    版本缺省＝当日，允许显式改日期补历史（NC-7）；同表同日已装载 → 任务终态
    LOAD_CONFLICT（异步化后冲突在执行期暴露，进度端点如实可见）。
    进度轮询：GET /v1/admin/load/tasks/{taskId}（progress 百分比；维护者 拍板 B 方案——
    单进度数字，不做三阶段）。任务态内存存放不持久化（服务重启即失，如实）。
    """
    p = await _payload(request)
    source = str(p.get("source") or "hbase").strip() or "hbase"
    if source != "hbase":
        return JSONResponse({"ok": False, "error": "UNSUPPORTED_SOURCE",
                             "message": f"暂不支持数据源 {source!r}（当前唯一源＝HBase）"}, 400)
    table_raw = str(p.get("table") or "").strip()
    if not table_raw:
        return JSONResponse({"ok": False, "error": "INVALID_TABLE",
                             "message": "缺少 table（HBase 表名，可带 namespace 前缀）"}, 400)
    try:
        domain = _sync_mod("sync_config").key_domain(table_raw)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "INVALID_TABLE", "message": str(exc)}, 400)
    cf = str(p.get("cf") or "").strip() or "cf"
    version = str(p.get("version") or "").strip() or time.strftime("%Y%m%d")
    if not re.fullmatch(r"[0-9]{8}", version):
        return JSONResponse({"ok": False, "error": "INVALID_VERSION",
                             "message": f"version 须为 YYYYMMDD（8 位日期），实为 {version!r}"}, 400)
    columns_ignored = bool(p.get("columns"))
    replace_ignored = bool(p.get("replace"))
    task_id = lt.new_task(domain, version)
    lt.start_thread(task_id, hbl.load_into_redis,
                    kwargs=dict(table=table_raw, cf=cf, version=version,
                                columns=None, rowkeys=None, replace=False))
    return JSONResponse({
        "ok": True, "mode": "hbase->redis-load-async", "source": "hbase",
        "taskId": task_id, "table": domain, "cf": cf, "version": version,
        "pollUrl": f"/v1/admin/load/tasks/{task_id}",
        "fullLoad": True, "columnsIgnored": columns_ignored, "replaceIgnored": replace_ignored,
    }, 202)


@router.get("/v1/admin/load/tasks/{task_id}", response_model=None)
async def load_task_view_ep(task_id: str) -> JSONResponse:
    """装载任务进度查询（B2）：running（progress 0-99）/ done（result 全量同旧同步响应）/
    failed（error 机读码＋消息）。任务不存在＝404（服务重启后任务态即失，如实）。"""
    v = lt.view(task_id)
    if v is None:
        return JSONResponse({"ok": False, "error": "TASK_NOT_FOUND",
                             "message": f"任务 {task_id} 不存在（或服务已重启，任务态不持久化——"
                                        "装载幂等闸兜底，可重新提交）"}, 404)
    if v["status"] == "failed":
        err = v.get("error") or {}
        message = err.get("message", "")
        if err.get("error") == "LOAD_CONFLICT":
            message = (f"{message}。装载页严格幂等不提供覆盖：同表同日如实拒绝——"
                       "换一个版本日期重灌，或走运维 CLI sync/hbase_to_redis.py --replace")
        return JSONResponse({"ok": False, "taskId": task_id, "status": "failed",
                             "error": err.get("error"), "message": message,
                             "table": v["table"], "version": v["version"],
                             "fallback": "无（HBase/Redis 不可达即如实报错，不写脏、不回退假数据）"}, 409)
    if v["status"] == "done":
        res = v.get("result") or {}
        schema = res.get("schema") or {}
        rec = res.get("reconcile") or {}
        schema_registered = False
        try:
            schema_registered = bool(sim.load_rows(v["table"]))
        except sim.SchemaImportError:
            schema_registered = False
        return JSONResponse({
            "ok": True, "taskId": task_id, "status": "done", "progress": 100,
            "mode": "hbase->redis-load", "source": "hbase",
            "table": v["table"], "version": v["version"],
            "loaded": res.get("loaded"), "tagCols": res.get("tag_cols"),
            "nonemptyCells": res.get("nonempty_cells"),
            "coverageSrc": res.get("coverage_src"),
            "sourceSha256": res.get("source_sha256"),
            "dataAsOf": res.get("data_as_of"),
            "elapsedMs": res.get("elapsedMs"),
            "reconcile": {"pass": rec.get("pass"), "diffCount": rec.get("diffCount"),
                          "checks": rec.get("checks") or {}, "diffs": rec.get("diffs") or [],
                          "error": rec.get("error", "")},
            "schema": {"fieldCount": schema.get("field_count"),
                       "source": schema.get("source"),
                       "generatedAt": schema.get("generated_at")},
            "schemaRegistered": schema_registered,
        }, 200)
    return JSONResponse({"ok": True, "taskId": task_id, "status": "running",
                         "progress": v["progress"], "table": v["table"],
                         "version": v["version"]}, 200)


def _load_assets_view(r) -> dict:
    """B3.1-4 阻塞段（to_thread＋同步客户端）：tabver 索引 × ver/recon/schema → 资产清单。

    卡3 消费契约（跨表枚举）；live＝tags:<表>:latest 指针比对（只读，绝不写）。
    """
    sm, C, common = _sync_mod("schema_meta"), _sync_mod("sync_config"), _sync_mod("common")
    tables_meta = opsc.load_tables_meta()
    counts = _scan_space_counts(r)   # P2 幽灵版过滤：数据面键数为准（publish.js filterGhostVersions 同思路）
    out = []
    for tbl in sm.tabver_tables(r):
        m = tables_meta.get(tbl) or {}
        with C.table_scope(tbl):
            live = common.get_live(r)
        versions = []
        for v in sm.tabver_versions(r, tbl):
            if counts.get((tbl, v), 0) <= 0:
                continue   # 幽灵版：物理删除后 tabver 惰性残留（数据键 0），不再以「已装载」面目出现
            with C.table_scope(tbl):
                ver = common.ver_meta_get(r, v) or {}
                recon = r.hgetall(C.RECON_META.format(version=v)) or {}
            sch = sm.schema_meta_get(r, tbl, v) or {}
            versions.append({
                "version": v,
                "rows": int(ver.get("rows") or 0),
                "tagCols": int(ver.get("tag_cols") or 0),
                "dataAsOf": ver.get("data_as_of", ""),
                "loadedAt": ver.get("loaded_at", ""),
                "reconPass": recon.get("pass") == "1",
                "schemaFieldCount": sch.get("field_count", 0),
                "live": live == v,
            })
        if versions:
            out.append({"table": tbl, "domain": tbl,
                        "tableCnName": m.get("cn_name", ""), "versions": versions})
    return {"ok": True, "tables": out}


@router.get("/v1/admin/load/assets", response_model=None)
async def load_assets_ep(request: Request) -> JSONResponse:
    """B3.1-4 已装载资产（tabver 清单；流程②「选已装载的表」的数据源，卡3 消费）。"""
    return JSONResponse(await asyncio.to_thread(_load_assets_view, _sync_redis()))


@router.get("/v1/admin/load/schema", response_model=None)
async def load_schema_ep(request: Request, table: str = "", version: str = "") -> JSONResponse:
    """B3.1-5 表级 schema 元数据（装载副产品；发布向导字段勾选的权威字段源，卡3 消费）。"""
    table = (table or "").strip()
    version = (version or "").strip()
    if not table or not version:
        return JSONResponse({"ok": False, "error": "SCHEMA_QUERY_INVALID",
                             "message": "缺少 table/version 查询参数"}, 400)
    try:
        domain = _sync_mod("sync_config").key_domain(table)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "INVALID_TABLE", "message": str(exc)}, 400)
    sm = _sync_mod("schema_meta")
    client = getattr(request.app.state, "sync_redis", None) or _sync_redis()
    got = await asyncio.to_thread(sm.schema_meta_get, client, table, version)
    if got is None:
        return JSONResponse({"ok": False, "error": "SCHEMA_NOT_FOUND",
                             "message": f"表 {domain} 版本 {version} 无 schema（未装载或版本不存在）"},
                            404)
    return JSONResponse({"ok": True, "table": got["table"], "version": got["version"],
                         "fieldCount": got["field_count"], "source": got["source"],
                         "generatedAt": got["generated_at"], "fields": got["fields"]}, 200)


# ══════════════════ P3（2026-10-08）：版本空间管理域（独立页 · 系统管理员定期清理） ══════════════════
# 背景：版本空间只进不出（实测 Redis 已堆 204 万键）。本域＝一页看全（SCAN tags:*
# 按数据面聚合 namespace×版本×键数）＋按空间物理删除（数据键＋ver/schema 元信息）。
# 保护闸**只锁线上版**（tags:<ns>:latest 指针指向的那一版）；物理上一版允许删除＝
# 接受失去该版回滚窗口（维护者 2026-10-08 拍板：空间优先）。明确不做：装载成功后
# 提示、TTL 自动化（本卡裁掉）。
# 纪律：键规则经 sync/common＋sync_config（单一事实来源；table_scope 切域，服务
# 进程并发安全）；审计沿 sync/common.audit_append 既有口径（LPUSH sync:delete:<表>:log）。
# 范围限定（规格逐条）：删除只动数据键＋sync:ver:<ns>:<version>＋sync:schema:<ns>:<version>；
# sync:tabver 成员与 sync:recon 对账 hash 按规格不在本卡删除清单（惰性残留，零读方危害）。
# 三批（2026-10-08 修三·维护者 实测）：删除成功后另同步 services.yaml console_services
# 该表 backend.load 的登记（version 命中→loaded:false；version 保留＝来源留痕口径
# 不变）——向导/登记面不再出现已删空间的幽灵版本；tabver 残留由发布向导按键数
# 过滤兜底（publish.js filterGhostVersions，键数真源＝GET /v1/admin/versions）。


class _VersionSpaceError(Exception):
    """版本空间域业务拒绝（闸语义）：status/code/message 三元组（端点层窄映射 HTTP）。"""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _scan_space_counts(r) -> dict:
    """SCAN tags:* 聚合「(表, 版本) → 键数」（数据面为准，不信 meta 残留）。

    逐批累计、不物化全键清单——线上已 204 万键，common.scan_keys 式全量收集＝
    百 MB 级瞬时内存；本函数每批只留计数。SCAN 迭代口径与 common.scan_keys
    一致（count=1000、禁 KEYS 不阻塞线上）；4 段键＝tags:<表>:<版本>:<键>，
    latest 指针（3 段）与任何非 4 段键不计入。
    """
    counts: dict = {}
    cur = 0
    while True:
        cur, batch = r.scan(cursor=cur, match="tags:*", count=1000)
        for k in batch:
            parts = k.split(":")
            if len(parts) == 4:
                counts[(parts[1], parts[2])] = counts.get((parts[1], parts[2]), 0) + 1
        if cur == 0:
            return counts


def _registered_tables() -> set:
    """console_services 写区登记的装载表名集合（backend.load.table，缺省回落 hbase.table）。

    注册口径＝裸表名（剥 namespace 前缀，与 load_tables_ep 的 keyHint 同法）；
    未登记的表在列表里标 registered=false＝孤儿（数据在、无服务引用，清理优先候选）。
    """
    out: set = set()
    for e in opsc.load_console_services():
        b = e.get("backend") or {}
        tbl = str((b.get("load") or {}).get("table")
                  or (b.get("hbase") or {}).get("table") or "").strip()
        if tbl:
            out.add(tbl.rsplit(":", 1)[-1])
    return out


def _versions_view(r) -> dict:
    """阻塞段（to_thread＋同步客户端）：版本空间列表视图（只读，绝不写指针）。

    每行：namespace/version/keys＋isLive（比对 tags:<ns>:latest 指针值）＋
    registered（console_services 是否登记此表；False＝孤儿徽标）＋
    verMeta/schemaMeta（装载副产品在场性；缺＝如实 False，不猜）。
    """
    common, C = _sync_mod("common"), _sync_mod("sync_config")
    sm = _sync_mod("schema_meta")
    registered = _registered_tables()
    counts = _scan_space_counts(r)
    spaces = []
    for ns, ver in sorted(counts):
        with C.table_scope(ns):
            live = common.get_live(r)
            ver_meta = common.ver_meta_get(r, ver) is not None
        spaces.append({
            "namespace": ns, "version": ver, "keys": counts[(ns, ver)],
            "isLive": live == ver,
            "registered": ns in registered,
            "verMeta": ver_meta,
            "schemaMeta": sm.schema_meta_get(r, ns, ver) is not None,
        })
    return {"ok": True, "spaces": spaces, "totalKeys": sum(counts.values())}


def _delete_version_space(r, ns: str, ver: str, operator: str) -> dict:
    """阻塞段：闸（线上版保护/存在性）→ SCAN 批删数据键＋ver/schema 元信息 → 审计。

    闸序（规格逐条）：① version == tags:<ns>:latest 指针值 → 409（先于存在性判）；
    ② namespace+version 组合不存在（数据键与两类元信息全不在场）→ 404。删除＝
    SCAN 该空间数据键逐批 pipeline DEL（count 实删数，非扫描数——并发写入不虚报）
    ＋sync:ver/sync:schema 元信息（有则删）；审计沿 audit_append 口径留痕。
    """
    common, C = _sync_mod("common"), _sync_mod("sync_config")
    t0 = time.perf_counter()
    with C.table_scope(ns):
        live = common.get_live(r)
        if ver == live:
            raise _VersionSpaceError(409, "PROTECTED_LIVE_VERSION",
                                     "线上版不可删除；如需淘汰请先装载新版本并切换上线")
        space_pattern = f"{C.NS}:{ver}:*"
        ver_meta_key = C.VER_META.format(version=ver)
        schema_meta_key = C.SCHEMA_META_KEY.format(table=ns, version=ver)
        has_ver_meta = bool(r.exists(ver_meta_key))
        has_schema_meta = bool(r.exists(schema_meta_key))
        exists_any = has_ver_meta or has_schema_meta
        cur, deleted = 0, 0
        while True:
            cur, batch = r.scan(cursor=cur, match=space_pattern, count=1000)
            if batch:
                exists_any = True
                pipe = r.pipeline(transaction=False)
                for k in batch:
                    pipe.delete(k)
                deleted += sum(1 for x in pipe.execute() if x == 1)   # DEL 返回 1＝真删到
            if cur == 0:
                break
        if not exists_any:
            raise _VersionSpaceError(404, "VERSION_NOT_FOUND",
                                     f"版本空间不存在：{ns}:{ver}（无数据键，也无 ver/schema 元信息）")
        meta_deleted = 0
        if has_ver_meta:
            meta_deleted += int(r.delete(ver_meta_key) or 0)
        if has_schema_meta:
            meta_deleted += int(r.delete(schema_meta_key) or 0)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        common.audit_append(r, C.DELETE_LOG, {
            "at": common.now_iso(), "action": "version_space_delete",
            "namespace": ns, "version": ver, "by": operator or "console",
            "deleted_keys": deleted, "deleted_meta": meta_deleted,
            "elapsed_ms": elapsed_ms,
        })
    return {"ok": True, "namespace": ns, "version": ver, "deletedKeys": deleted,
            "deletedVerMeta": has_ver_meta, "deletedSchemaMeta": has_schema_meta,
            "auditLog": f"sync:delete:{ns}:log", "elapsedMs": elapsed_ms}


def _sync_registry_after_delete(ns: str, ver: str) -> int:
    """修三①：删除成功后同步 console_services 写区登记（阻塞段，to_thread 内跑）。

    幽灵版本的登记面根因：删除只清 Redis（数据键＋ver/schema 元信息），
    services.yaml console_services 里该表 backend.load 的 version/loaded 残留。
    此处补齐：被删 (namespace, version) 命中某条目 backend.load.{table,version}
    （表名剥 ns 前缀，与 _registered_tables 同口径）→ 置 loaded:false。
    version 原样保留——来源留痕口径不变（执行器只认 backend.kv，不读 load 块；
    trace.commit 占位不伪造 hash 的 git 留痕口径亦不变）。未命中＝登记本就干净，
    零写盘。写回沿 ops_config._save_services_yaml 原子写（yaml 回读＋真加载器
    校验），运行时 services: 区与注释逐字节保留。返回同步条数。
    """
    path = opsc.SERVICES_YAML
    entries = opsc.load_console_services(path)
    changed = 0
    for e in entries:
        load = (e.get("backend") or {}).get("load") or {}
        bare = str(load.get("table") or "").strip().rsplit(":", 1)[-1]
        if bare == ns and str(load.get("version") or "") == ver and load.get("loaded") is not False:
            load["loaded"] = False
            changed += 1
    if not changed:
        return 0
    text = path.read_text(encoding="utf-8") if path.is_file() else "services: []\n"
    head, _ = opsc._split_marker(text)
    mw.service_yaml_save(path, head, entries)
    return changed


@router.get("/v1/admin/versions", response_model=None)
async def versions_list_ep(request: Request) -> JSONResponse:
    """P3 版本空间列表（「版本管理」独立页数据源）：SCAN tags:* 聚合全部版本空间。

    只读端点，绝不触碰 tags:<ns>:latest 指针。Redis 不可达＝如实 503（不回假清单）；
    注册表（services.yaml 写区）读坏＝沿 ops_config 错误信封原样透出。
    """
    client = getattr(request.app.state, "sync_redis", None) or _sync_redis()
    try:
        view = await asyncio.to_thread(_versions_view, client)
    except opsc.OpsConfigError as exc:
        return _ops_error_response(exc)
    except Exception as exc:  # noqa: BLE001 —— Redis 等基础设施故障如实报，不静默空清单
        return JSONResponse({"ok": False, "error": "VERSIONS_VIEW_FAILED",
                             "message": f"版本空间清单暂不可用（{type(exc).__name__}）；"
                                        "请稍后重试，详情见服务端日志"}, 503)
    return JSONResponse(view)


@router.post("/v1/admin/versions/delete", response_model=None)
async def versions_delete_ep(request: Request) -> JSONResponse:
    """P3 删除一个版本空间（系统管理员定期清理；两段式确认在前端）。

    闸：① version == tags:<ns>:latest 指针值 → 409 PROTECTED_LIVE_VERSION（人话：
    线上版不可删除；如需淘汰请先装载新版本并切换上线）——保护闸只锁线上版，
    物理上一版允许删除（接受失去该版回滚窗口）；② namespace+version 组合不存在
    → 404。通过则：SCAN 删该空间全部数据键＋sync:ver:<ns>:<version>＋
    sync:schema:<ns>:<version>（有则删）＋audit_append 审计留痕。
    响应带 deletedKeys（实际删到的数据键数）＋registrySynced（修三①：console_services
    写区 backend.load 命中被删空间的条目数——命中者置 loaded:false，version 保留＝
    留痕口径不变；登记同步失败不回滚已完成的删除，如实以 registrySyncError 带回）。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    ns = str(p.get("namespace") or "").strip()
    ver = str(p.get("version") or "").strip()
    if not ns:
        return JSONResponse({"ok": False, "error": "INVALID_TABLE",
                             "message": "缺少 namespace（版本空间所属表名）"}, 400)
    try:
        _sync_mod("sync_config").key_domain(ns)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": "INVALID_TABLE", "message": str(exc)}, 400)
    if not re.fullmatch(r"[0-9]{8}", ver):
        return JSONResponse({"ok": False, "error": "INVALID_VERSION",
                             "message": f"version 须为 YYYYMMDD（8 位日期），实为 {ver!r}"}, 400)
    client = getattr(request.app.state, "sync_redis", None) or _sync_redis()
    try:
        result = await asyncio.to_thread(
            _delete_version_space, client, ns, ver, str(p.get("operator") or "console"))
    except _VersionSpaceError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message,
                             "namespace": ns, "version": ver}, exc.status)
    except Exception as exc:  # noqa: BLE001 —— 基础设施故障如实报，不静默半删
        return JSONResponse({"ok": False, "error": "DELETE_FAILED",
                             "message": f"删除未确认完成（{type(exc).__name__}）；"
                                        "请重试或查服务端日志核对实际残留"}, 503)
    # 修三①：登记面同步（善后段）。删除已成功，不因登记同步失败回滚或谎报——
    # registrySynced=同步条数；失败时 registrySyncError 如实标注（Redis 侧已删净）。
    result["registrySynced"] = 0
    try:
        result["registrySynced"] = await asyncio.to_thread(_sync_registry_after_delete, ns, ver)
    except Exception as exc:  # noqa: BLE001 —— 同步失败如实带回，不吞不美化
        result["registrySyncError"] = (f"版本空间已删除，但服务登记同步未完成"
                                       f"（{type(exc).__name__}: {str(exc)[:160]}）；"
                                       "请重试删除（幂等）或检查 services.yaml")
    return JSONResponse(result)


def _grants_view(doc: dict, services: dict) -> dict:
    """B（AppKey 二批 · 授权可见性）：kid → 授权服务清单视图（签发凭证列表行展示用）。

    list_view（appkeys.py 纯逻辑层）不含 grants——本视图在 HTTP 编排层按注册表原文
    补齐：{service, serviceName, endpoint, fieldCount}。服务名自运行时注册表解析
    （单键 service_id → 条目；覆盖手编/下线条目，比发布台 published 视图面广）；
    解析不到回落 service id（如实，不造名）。字段只出数量不出清单（列表不灌长表，
    掩码纪律同 list_view）。
    """
    by_sid = {k: s for k, s in (services or {}).items()
              if isinstance(k, str) and isinstance(s, dict)}
    out: dict = {}
    for k in doc["keys"]:
        gs = []
        for g in k.get("grants") or []:
            if not isinstance(g, dict):
                continue
            sid = str(g.get("service") or "")
            svc = by_sid.get(sid) or {}
            is_proxy = svc.get("kind") == "proxy"
            row = {"service": sid,
                   "serviceName": str(svc.get("name") or sid),
                   "endpoint": str(g.get("endpoint") or svc.get("route") or ""),
                   "fieldCount": len(g.get("fields") or []) if not is_proxy else 0}
            if is_proxy:
                row["kind"] = "proxy"     # 普通服务保持旧形状（兼容既有断言）；代理标注整路由
            gs.append(row)
        out[appk.record_kid(k["appkey"])] = gs
    return out


def _find_name_conflict(name: str, route: str, path: Path = None) -> str | None:
    """A1（服务重名治理）：同名且不同路由的既有服务 → 返回冲突路由（人话文案用），否则 None。

    查全量与 R6 路由查重（route_naming.find_route_occupant）同口径：console 写区
    （含草稿，草稿同样占名）＋运行时 services: 区（单键注册表，只看字符串键）。
    同名同路由＝同服务重发（原地覆盖/升版本，既有语义）不拦；同名异路由＝409 治本。
    比对前两侧 strip（手编名带尾随空格不算不同名）。
    """
    name = (name or "").strip()
    route = (route or "").strip()
    if not name or not route:
        return None
    if path is None:
        path = opsc.SERVICES_YAML
    for e in opsc.load_console_services(path):
        other = str((e or {}).get("route") or "").strip()
        if str((e or {}).get("name") or "").strip() == name and other and other != route:
            return other
    runtime: dict = opsc.load_services(path) if path.is_file() else {}
    for key, s in runtime.items():
        if not isinstance(key, str) or not isinstance(s, dict):
            continue
        other = str(s.get("route") or "").strip()
        if str(s.get("name") or "").strip() == name and other and other != route:
            return other
    return None


@router.post("/v1/admin/publish")
async def publish_service_ep(request: Request) -> JSONResponse:
    """发布/存草稿：校验→原子写 services.yaml（hbase 选表发布在运行时 services: 列表
    追加新条目＝可调；redis 型写区就地更新＝向后兼容）→ 返回 (entry, before)。

    hbase 选表发布前置校验（AC-6/AC-7 + 发布台重设计 B1/B2/B3）：字典按所选表取
    （表级 CSV 优先、回落宽表 297，不在即拒）；发布态缺示例值自动从表样本行实取
    （scan 采样经脱敏后填充，向导无输入框）；敏感列未显式配脱敏自动套默认
    band_mask（显式配置优先）；路由/表名/列族/键类型非法亦拒——拒绝时文件零变更。

    **产物口径（ADR-0005 架构纠正）**：发布产物＝「加载后 Redis 型服务」（backend.kv，
    指向 tags:user:＜版本＞:<key> 新版本键空间）＋field_space=wide297（ADR-0004）；
    **不再产出 backend.source=hbase**（HBase 直连能力保留代码，归 M3 候选）。
    loadVersion＝加载动作落定的 Redis 版本空间（来源留痕，执行器不读）。git 留痕占位不伪造 hash。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    # P2（2026-10-08）：fields 形状闸——非对象数组（字符串/数字列表/散对象）此前直穿
    # 纯逻辑层抛未捕获异常 → 500 无信封；此处 4xx 拦下且零副作用（不触发布/采样）
    fields = p.get("fields") or []
    if not _obj_list(fields):
        return JSONResponse({"error": errors.INVALID_REQUEST,
                             "message": "fields 须为对象数组（每项 {name, cf, type, cnName}）"}, 400)
    # 数据源口径归一到纯逻辑层（M2-5 契约：backend.source/hbase{namespace,table,cf,key_type}
    # 二式兼容；判定可被 tests 机检，HTTP 层不做业务判断）
    norm = opsc.normalize_publish_payload(p)
    # A1（服务重名治理，AppKey 二批 M 级 2026-10-14）：发布时服务名查重——同名且
    # 路由不同 → 409 service_name_conflict（人话指路既有路由）；治本＝杜绝签发凭证
    # 「授权服务」下拉同名歧义。同名同路由＝同服务重发（原地覆盖/升版本）不在此拦。
    name_conflict_route = _find_name_conflict(
        str(p.get("serviceName") or ""), str(p.get("routePath") or ""))
    if name_conflict_route is not None:
        return JSONResponse(
            {"error": errors.SERVICE_NAME_CONFLICT,
             "message": f"已有同名服务「{str(p.get('serviceName') or '').strip()}」"
                        f"（路由 {name_conflict_route}），请换一个名字；"
                        "如是同一服务更新，请保持路由不变重发（原地覆盖，版本史保留）"}, 409)
    # B2：hbase 型发布态缺示例值 → 从所选表 scan 实取样例行（discovery 同款连接层），
    # 交纯逻辑层实取+脱敏填充。采样失败＝如实 503/504（示例值不造假值顶位）。
    sample_rows: list[dict] = []
    if norm["datasource"] == "hbase" and str(p.get("status") or "published") == "published":
        client = _hbase_client(request)
        if client is None:
            return _hbase_unavailable(request)
        try:
            sample_rows = await asyncio.wait_for(
                asyncio.to_thread(client.scan, norm["hbase"]["table"],
                                  limit=opsc.SAMPLE_SCAN_LIMIT),
                timeout=float(os.environ.get("HBASE_DISCOVER_TIMEOUT_S", "10")))
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": "HBASE_SCAN_TIMEOUT",
                 "message": "表样本行采样超时（HBASE_DISCOVER_TIMEOUT_S）——示例值自动"
                            "生成需真采样，不造假值。可重试或稍后再发"}, 504)
        except Exception as exc:  # noqa: BLE001 —— 采样失败如实报错，不吞不美化
            return JSONResponse(
                {"error": "HBASE_UNAVAILABLE",
                 "message": f"表样本行采样失败（示例值自动生成需真采样，不造假值）："
                            f"{str(exc)[:200]}"}, 503)
    started = time.monotonic()   # 卡4 NC-2c：发布动作审计时延（含校验与写盘）
    try:
        entry, before = mw.service_publish(
            opsc.SERVICES_YAML,
            name=str(p.get("serviceName") or ""),
            route=str(p.get("routePath") or ""),
            fields=fields,
            field_space=str(p.get("fieldSpace") or sd.FIELD_SPACE_WIDE),
            env=str(p.get("env") or ""),
            instance=str(p.get("instance") or ""),
            status=str(p.get("status") or "published"),
            operator=str(p.get("operator") or "console"),
            # P1-10 差量：前端「先测试/直接上生产」发布选择（env/instance 缺省时派生落盘）
            release_mode=str(p.get("releaseMode") or ""),
            # M2-4：数据源类型（缺省 redis 向后兼容）；hbase 型带 table/cf/key_type
            datasource=norm["datasource"],
            hbase=norm["hbase"],
            # 本卡：加载动作落定的 Redis 版本空间（来源留痕；执行器不读）
            load_version=str(p.get("loadVersion") or norm.get("load_version") or ""),
            oa_ticket=norm["oa_ticket"],
            # B2：表样本行（hbase 型发布态示例值自动生成；redis 型不传＝行为零变化）
            sample_rows=sample_rows)
    except opsc.OpsConfigError as exc:
        # P3 二批（2026-10-08 维护者 六条②）：重复 route＝冲突语义 → 409（原 400）。
        # 占用拦截本体在纯逻辑层 route_naming.find_route_occupant（R6，同 route 异名拒），
        # 此处仅窄映射 HTTP 状态：code=route_exists → 409；其余 OpsConfigError 保持原状态码。
        if exc.code == errors.ROUTE_OCCUPIED:
            return JSONResponse({"error": exc.code, "message": exc.message}, 409)
        return _ops_error_response(exc)
    # R2 域码建议（不阻断，宽进严出）：首段命中域码表时回提示信息；未命中/不合规为 None。
    # 卡3（B3.2 前向契约）：补 ok/serviceId/routePath/action/dataVersion/auditId——覆盖
    # 更新机制（action=updated 时不追加条目）归卡4 实装；auditId 待卡4 接 audit.py 八元组
    # 后回填（现如实 null）。entry/before/namingHint 保留（既有适配层与测试兼容）。
    load_version = str(p.get("loadVersion") or norm.get("load_version") or "")
    # 卡4 NC-2c：发布动作接正式审计八元组（覆盖更新的旧值留痕保底在条目 trace.history）
    audit_id = _audit_admin_action(
        request, "publish", operator=str(p.get("operator") or "console"),
        params={"serviceId": entry.get("serviceId") or entry.get("id") or "",
                "routePath": str(p.get("routePath") or ""), "status": str(p.get("status") or "published"),
                "loadVersion": load_version},
        response={"action": "updated" if before else "created"},
        latency_ms=int((time.monotonic() - started) * 1000))
    return JSONResponse({"entry": entry, "before": before,
                         "namingHint": rn.domain_hint(str(p.get("routePath") or "")),
                         "trace": {"action": "publish", "note": GIT_PLACEHOLDER_NOTE},
                         "ok": True,
                         "serviceId": entry.get("serviceId") or entry.get("id") or "",
                         "routePath": str(p.get("routePath") or ""),
                         "action": "updated" if before else "created",
                         "dataVersion": load_version,
                         "auditId": audit_id or None})


@router.get("/v1/admin/publish/sync/status", response_model=None)
async def publish_sync_status_ep(request: Request, table: str = "") -> JSONResponse:
    """切版状态（B4 · 发布台重设计向导③）：指定表指针（tags:<表>:latest）→线上版本、
    版本元信息（rows/data_as_of 等）、最近切换/回滚日志——只读。

    表参数（query ?table=usr_basic_info）：键空间按表隔离后每表各一条 latest 指针；
    **缺省不切域**＝存量单域口径（宽表 297 演示链路的旧键空间；向导③显式带所选
    表名）。非法表名＝400 如实拒绝。
    Redis 不可达＝如实 503（不回假版本）；未上线＝liveVersion=null＋如实 note
    （「当前线上版本：无」）。键规则经 sync/common.py（单一事实来源），不复制。
    """
    try:
        r = await asyncio.to_thread(syo.get_sync_redis)
        view = await asyncio.to_thread(syo.sync_status, r, table)
    except syo.SyncOpsError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message},
                            exc.status)
    return JSONResponse(view)


@router.post("/v1/admin/publish/sync/switch", response_model=None)
async def publish_sync_switch_ep(request: Request) -> JSONResponse:
    """切换到新版（B4）：过三道闸（已加载/对账全绿/新空间非空）原子切指针——
    **复用 sync/switch.py 闸逻辑（子进程原样调用，只包不改，零第二实现）**。

    请求 {version, table}（version＝YYYYMMDD；table 缺省不切域＝存量单域口径——
    向导③带所选表名，三闸在该表键空间判定，别的表零影响）。闸拒绝＝409 ＋人话
    reason（线上仍旧版）；脚本层故障/超时＝502/504 如实；成功/幂等无动作＝200。
    失败不吞不美化。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    try:
        status, body = await asyncio.to_thread(syo.run_switch, str(p.get("version") or ""),
                                               str(p.get("table") or ""))
    except syo.SyncOpsError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message},
                            exc.status)
    return JSONResponse(body, status)


@router.post("/v1/admin/publish/sync/rollback", response_model=None)
async def publish_sync_rollback_ep(request: Request) -> JSONResponse:
    """回滚到指定旧版（B4）：安全闸（目标≠当前、目标空间非空）原子 SET 指回——
    **复用 sync/rollback.py 闸逻辑（子进程原样调用，只包不改）**。

    请求 {to, table}（to 缺省＝CLI 按切换日志自动取上一版；table 缺省不切域＝存量
    单域口径——传表名则回滚只动本表指针，别的表零影响）。拒绝＝409＋人话 reason；
    成功＝200（from/to/target_rows 如实带回）。
    """
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    try:
        status, body = await asyncio.to_thread(syo.run_rollback, str(p.get("to") or ""),
                                               str(p.get("table") or ""))
    except syo.SyncOpsError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message},
                            exc.status)
    return JSONResponse(body, status)


@router.post("/v1/admin/publish/{sid}/offline")
async def publish_offline(sid: str, request: Request) -> JSONResponse:
    """下线（published→offline；两段式确认在前端，后端只做状态机与留痕）。"""
    p, bad = await _strict_payload(request)
    if bad is not None:
        return bad
    started = time.monotonic()   # 卡4 NC-2c：下线动作审计时延
    try:
        entry, before = mw.service_offline(opsc.SERVICES_YAML, sid,
                                             operator=str(p.get("operator") or "console"))
    except opsc.OpsConfigError as exc:
        return _ops_error_response(exc)
    # 卡4 NC-2c：下线动作接正式审计八元组（状态机语义不变：published→offline/409 拒绝）
    audit_id = _audit_admin_action(
        request, "offline", operator=str(p.get("operator") or "console"),
        params={"serviceId": sid}, response={"action": "offline"},
        latency_ms=int((time.monotonic() - started) * 1000))
    return JSONResponse({"entry": entry, "before": before,
                         "auditId": audit_id or None,
                         "trace": {"action": "offline", "note": GIT_PLACEHOLDER_NOTE}})


@router.get("/v1/admin/upstreams", response_model=None)
async def upstreams_console(request: Request, format: str = "json") -> JSONResponse | PlainTextResponse:
    """代理登记表（读盘即时值）；?format=yaml 出 upstreams.yaml 权威源原文。"""
    if format == "yaml":
        text = opsc.UPSTREAMS_YAML.read_text(encoding="utf-8") if opsc.UPSTREAMS_YAML.is_file() else ""
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")
    return JSONResponse({"proxies": opsc.upstreams_view()})


@router.post("/v1/admin/upstreams")
async def save_upstream_ep(request: Request) -> JSONResponse:
    """存草稿/发布代理配置（同 route＝同 id 更新；原子写＋校验同发布台口径）。"""
    p = await _payload(request)
    try:
        entry, before = mw.upstream_save(
            opsc.UPSTREAMS_YAML,
            service_name=str(p.get("serviceName") or ""),
            route=str(p.get("routePath") or ""),
            method=str(p.get("method") or "GET"),
            upstream_url=str(p.get("upstreamUrl") or ""),
            timeout_sec=int(p.get("timeoutSec") or 5),
            retry_count=int(p.get("retryCount") or 2),
            reuse_upstream=bool(p.get("reuse") or p.get("reuseUpstream") or False),
            upstream_name=str(p.get("upstreamName") or ""),
            status=str(p.get("status") or "published"),
            env=str(p.get("env") or ""),
            instance=str(p.get("instance") or ""),
            operator=str(p.get("operator") or "console"),
            # P1-10 差量：前端「先测试/直接上生产」发布选择（env/instance 缺省时派生落盘）
            release_mode=str(p.get("releaseMode") or ""))
    except opsc.OpsConfigError as exc:
        return _ops_error_response(exc)
    except (TypeError, ValueError):
        return JSONResponse({"error": errors.INVALID_JSON,
                             "message": "timeoutSec/retryCount 须为整数"}, 400)
    return JSONResponse({"entry": entry, "before": before,
                         "trace": {"action": "save", "note": GIT_PLACEHOLDER_NOTE}})


@router.post("/v1/admin/upstreams/{pid}/delete")
async def upstream_delete_draft(pid: str, request: Request) -> JSONResponse:
    """删除草稿（仅 draft；两段式确认在前端，后端只做状态机与原子写）。"""
    p = await _payload(request)
    try:
        entry, before = mw.upstream_draft_delete(
            opsc.UPSTREAMS_YAML, pid, operator=str(p.get("operator") or "console"))
    except opsc.OpsConfigError as exc:
        return _ops_error_response(exc)
    return JSONResponse({"entry": entry, "before": before,
                         "trace": {"action": "delete_draft", "note": GIT_PLACEHOLDER_NOTE}})


@router.post("/v1/admin/upstreams/{pid}/offline")
async def upstream_offline(pid: str, request: Request) -> JSONResponse:
    """下线代理（published→offline）。"""
    p = await _payload(request)
    try:
        entry, before = mw.upstream_offline(opsc.UPSTREAMS_YAML, pid,
                                              operator=str(p.get("operator") or "console"))
    except opsc.OpsConfigError as exc:
        return _ops_error_response(exc)
    return JSONResponse({"entry": entry, "before": before,
                         "trace": {"action": "offline", "note": GIT_PLACEHOLDER_NOTE}})


# ── B 型代理补完（2026-10-15 本地自转发闭环卡）：试连接真端点（三步向导第②步） ──
# 本块为该卡在 admin_api.py 的唯一插入（并行卡避让：集中一处连续插入）。
# proxy_hub 局部导入＝避免模块顶部再开第二编辑点；probe 为服务端真实 HTTP 探测
# （浏览器 CORS 不再挡跨源上游；耗时含真实网络往返）。bug7（2026-10-20）三态，
# 2026-10-21 起四态：ok_2xx=通过 / ok_post_confirmed=上游在线、POST 方法已确认
# （GET 405→POST 复探被参数校验 400/422 拒＝查询型 POST-only 上游正当反应）/
# method_mismatch=405（AUTO 两步复探后仍失配，或显式方法 405）/
# unreachable=连不上或非 2xx；method="AUTO"（默认「跟随上游」）GET 探底→405 自动
# POST 复探；建议方法仅在真有 Allow 头时经 suggestedMethod 回给前端。
@router.post("/v1/admin/upstreams/probe")
async def upstream_probe(request: Request) -> JSONResponse:
    """试连上游：POST {upstreamUrl, method, timeoutSec} → 结论＋原状态码/耗时。"""
    from app import proxy_hub as pxh   # 局部导入：单块插入纪律（见上注）
    p = await _payload(request)
    url = str(p.get("upstreamUrl") or "").strip()
    method = str(p.get("method") or "AUTO").upper()
    try:
        timeout_sec = int(p.get("timeoutSec") or 5)
    except (TypeError, ValueError):
        return JSONResponse({"error": errors.INVALID_JSON,
                             "message": "timeoutSec 须为整数"}, 400)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return JSONResponse({"error": errors.INVALID_UPSTREAM_URL,
                             "message": "上游 URL 须为合法 http(s) 地址"}, 400)
    if method not in ("GET", "POST", "AUTO"):
        return JSONResponse({"error": errors.INVALID_METHOD,
                             "message": "method 须为 GET、POST 或 AUTO（跟随上游）"}, 400)
    if timeout_sec <= 0 or timeout_sec > 30:
        return JSONResponse({"error": errors.INVALID_TIMEOUT,
                             "message": "超时秒数须在 1~30 之间"}, 400)
    result = await pxh.probe(url, method=method, timeout_sec=timeout_sec)
    return JSONResponse(result)


# ══════════════════ P3（2026-10-08）：性能测试管理域（perf 三端点＋报告伺服） ══════════════════
# 前置已就绪的 loadtest/ 梯子工具（run_ladder.sh：授权/配额门禁内置，凭证走
# ONESERVE_APPKEY 环境变量，产出 results/<时间戳>-<服务短名>/performance-report.md
# ＋.html）在此接入管理端（admin/perf.html）。
#
#   POST /v1/admin/perf/run                      触发一次梯子（subprocess.Popen 后台起
#                                                run_ladder.sh，立即返回 run_id；
#                                                ONESERVE_APPKEY 由本进程环境继承）
#   GET  /v1/admin/perf/status                   轮询口径（页面 5s 一拍）：运行中否/
#                                                凭证在场性/最新 results 目录/
#                                                报告文件是否已生成
#   GET  /v1/admin/perf/reports                  results/ 历史报告聚合（时间戳/服务/
#                                                报告链接）
#   GET  /v1/admin/perf/report/{run}/{file}      报告静态伺服（iframe src；file 白名单
#                                                performance-report.html/.md）
#
# 保护（任务口径）：同一时刻仅一个压测进程——内存句柄在跑即 409；服务重启后句柄
# 丢失但状态文件 pid 存活 → 同样 409（防双跑），status 如实报「后台进程状态未知」。
# 触发前校验 ONESERVE_APPKEY 已配置（凭证不回显，页面只看布尔）。
# 部署口径（如实）：容器栈内本进程看不到仓库根（service/ 整目录挂载为 /app，
# loadtest/ 未挂载）→ run 如实 503 LOADTEST_UNAVAILABLE、reports 空表＋note；
# 宿主进程（tests/本机直跑）全功能。历史报告在容器态由门户静态镜像
# portal/perf-reports/ 承接（build_portal.py 构建期同步，nginx 直出）。

_PERF_LADDER_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")
_PERF_RUNDIR_RE = re.compile(r"^(\d{8}-\d{6})(?:-(.+))?$")
_PERF_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PERF_STATE_FILE = ".perf-run.json"      # results/ 下状态留痕（重启后判定 pid 存活）
_PERF_LOG_FILE = ".perf-last.log"        # run_ladder 后台输出汇总（排障用）
_PERF_REPORT_FILES = {"performance-report.html": "text/html; charset=utf-8",
                      "performance-report.md": "text/markdown; charset=utf-8"}
_perf_state: dict | None = None          # {run_id, service, ladder, pid, started_at, proc}


def _perf_paths() -> tuple:
    """loadtest 工具根与 results 根（ONESERVE_LOADTEST_DIR 可覆盖，缺省按仓库根推算）。

    admin_api.py 位于 <repo>/service/app/ → parents[2]＝仓库根；容器内该推算不存在
    （loadtest/ 未挂载）→ 调用方按「不可用」如实处理，不假启动。env 覆盖供测试/直跑。
    """
    root = Path(os.environ["ONESERVE_LOADTEST_DIR"]) if os.environ.get("ONESERVE_LOADTEST_DIR") \
        else Path(__file__).resolve().parents[2] / "loadtest"
    return root, root / "results"


def _perf_pid_alive(pid) -> bool:
    """pid 存活探测（os.kill 0 信号；EPERM＝存在但非本用户，按存活保守处理）。"""
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:   # 存在但非本用户＝保守按存活（单飞闸宁可误拒不可双跑）
        return True
    except (ProcessLookupError, ValueError, TypeError, OSError):
        return False


def _perf_read_state_file(results_dir) -> dict | None:
    try:
        data = json.loads((results_dir / _PERF_STATE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 —— 状态文件尽力而为：坏/缺＝无
        return None


def _perf_unlink_state(results_dir) -> None:
    try:
        (results_dir / _PERF_STATE_FILE).unlink()
    except OSError:
        pass


def _perf_busy() -> tuple:
    """单飞闸：内存句柄在跑 → (True, 'in-memory')；句柄丢但状态文件 pid 存活
    （服务重启过）→ (True, 'state-file')；否则 (False, '')。"""
    proc = (_perf_state or {}).get("proc")
    if proc is not None and proc.poll() is None:
        return True, "in-memory"
    _, results_dir = _perf_paths()
    st = _perf_read_state_file(results_dir)
    if st and _perf_pid_alive(st.get("pid")):
        return True, "state-file"
    return False, ""


@router.post("/v1/admin/perf/run")
async def perf_run_ep(request: Request) -> JSONResponse:
    """触发一次梯子压测（重操作：四档约 8-12 分钟＋高负载，端点只拉后台进程即返）。

    校验序：service/ladder 形状 → ONESERVE_APPKEY 在场 → 单飞闸（运行中 409）→
    工具可达（容器态如实 503）。通过则 Popen 后台起 run_ladder.sh（start_new_session
    脱离请求生命周期；env 原样继承＝ONESERVE_APPKEY 传导），stdout/stderr 汇入
    results/.perf-last.log，立即返回 run_id＋时长提示。
    """
    payload = await _payload(request)
    service = str(payload.get("service") or "").strip()
    ladder = str(payload.get("ladder") or "").strip() or "100,300,500,800"
    if not service:
        return JSONResponse({"error": "invalid_service",
                             "message": "缺少 service（先在上方选择已发布服务）"}, 400)
    if not _PERF_LADDER_RE.fullmatch(ladder):
        return JSONResponse({"error": "invalid_ladder",
                             "message": f"梯子格式非法：{ladder}（须如 20 或 100,300,500,800）"}, 400)
    if not os.environ.get("ONESERVE_APPKEY"):
        return JSONResponse({"error": "perf_appkey_missing",
                             "message": "服务进程未配置 ONESERVE_APPKEY 环境变量（压测凭证从环境变量"
                                        "读取，页面不收集明文）；请配置后重启服务进程再触发"}, 400)
    busy, via = _perf_busy()
    if busy:
        who = "本进程一次梯子仍在跑" if via == "in-memory" else "上次触发的梯子进程仍在跑（服务可能重启过）"
        return JSONResponse({"error": "perf_running",
                             "message": f"压测进行中（{who}，四档约 8-12 分钟）；请等当前批次完成后再次触发"}, 409)
    loadtest_dir, results_dir = _perf_paths()
    if not (loadtest_dir / "run_ladder.sh").is_file():
        return JSONResponse({"error": "loadtest_unavailable",
                             "message": "压测工具不在服务进程可达路径（容器栈未挂载仓库根 loadtest/）；"
                                        "本机改用 CLI 触发：ONESERVE_APPKEY=<key> bash loadtest/run_ladder.sh "
                                        "--service <服务ID> --ladder <档位>，报告生成后仍可在本页查看"}, 503)
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        log_f = open(results_dir / _PERF_LOG_FILE, "ab")   # noqa: SIM115 —— 生存期＝服务进程，不关
        proc = subprocess.Popen(
            ["bash", str(loadtest_dir / "run_ladder.sh"), "--service", service, "--ladder", ladder],
            cwd=str(loadtest_dir.parent), stdout=log_f, stderr=subprocess.STDOUT,
            env=os.environ.copy(), start_new_session=True)
    except OSError as exc:
        return JSONResponse({"error": "perf_spawn_failed",
                             "message": f"后台进程拉起失败：{exc}"}, 500)
    global _perf_state
    run_id = f"perf-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    _perf_state = {"run_id": run_id, "service": service, "ladder": ladder,
                   "pid": proc.pid,
                   "started_at": datetime.now().isoformat(timespec="seconds"), "proc": proc}
    try:   # 状态留痕（重启后单飞闸/未知态判定用；写失败不阻断——内存句柄仍是主闸）
        (results_dir / _PERF_STATE_FILE).write_text(
            json.dumps({k: v for k, v in _perf_state.items() if k != "proc"},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return JSONResponse({"ok": True, "run_id": run_id, "service": service, "ladder": ladder,
                         "note": "梯子已在后台开跑（四档约 8-12 分钟）；页面每 5s 轮询状态，"
                                 "完成后在下方历史报告列表查看",
                         "status_href": "/v1/admin/perf/status"})


@router.get("/v1/admin/perf/status", response_model=None)
async def perf_status_ep(request: Request) -> JSONResponse:
    """压测状态轮询（页面 5s 一拍）：运行中否/凭证在场性/工具可达性/最新 results 目录。

    服务重启不追进程（M0 口径不过度设计）：内存句柄丢失而状态文件在 → running=false
    ＋unknown 如实报「后台进程状态未知，请查看 results 目录」；状态文件 pid 已死 →
    判定梯子已结束，顺手清状态文件（不留永久未知）。本进程句柄已退出 → 同样清理。
    """
    global _perf_state
    loadtest_dir, results_dir = _perf_paths()
    proc = (_perf_state or {}).get("proc")
    running = proc is not None and proc.poll() is None
    if not running and _perf_state is not None and proc is not None:
        _perf_state, proc = None, None          # 本进程视角梯子已退出：清句柄
        _perf_unlink_state(results_dir)         # ＋清状态文件（unknown 只走「句柄丢失」路径）
    unknown = None
    if not running and results_dir.is_dir():
        st = _perf_read_state_file(results_dir)
        if st:
            if _perf_pid_alive(st.get("pid")):
                unknown = {"run_id": st.get("run_id"), "service": st.get("service"),
                           "started_at": st.get("started_at"),
                           "note": "后台进程状态未知（服务可能已重启，M0 不追进程）；请查看 results 目录确认实际进度"}
            else:
                _perf_unlink_state(results_dir)  # pid 已死＝梯子已结束：清留痕
    latest = None
    if results_dir.is_dir():
        dirs = sorted((d for d in results_dir.iterdir()
                       if d.is_dir() and not d.name.startswith(".")),
                      key=lambda p: p.name, reverse=True)
        if dirs:
            d = dirs[0]
            latest = {"dir": d.name,
                      "report_html": (d / "performance-report.html").is_file(),
                      "report_md": (d / "performance-report.md").is_file(),
                      "report_href": f"/v1/admin/perf/report/{d.name}/performance-report.html"}
    return JSONResponse({
        "running": running,
        "run": ({k: v for k, v in _perf_state.items() if k != "proc"}
                if _perf_state else None),
        "unknown": unknown,
        "appkey_configured": bool(os.environ.get("ONESERVE_APPKEY")),
        "loadtest_available": (loadtest_dir / "run_ladder.sh").is_file(),
        "results_dir": str(results_dir),
        "latest": latest,
    })


@router.get("/v1/admin/perf/reports", response_model=None)
async def perf_reports_ep(request: Request) -> JSONResponse:
    """历史报告聚合：扫 results/ 每次跑的时间戳/服务/报告链接（新→旧）。

    目录名 <时间戳>-<服务短名>（run_ladder 口径）；服务全名/ID 尽力读该目录
    meta.json（discover 产物）。容器态看不到 results/ → 空表＋note 如实（页面回落
    门户静态镜像 perf-reports/index.json）。
    """
    _, results_dir = _perf_paths()
    if not results_dir.is_dir():
        return JSONResponse({"reports": [], "loadtest_available": False,
                             "note": "服务进程看不到 loadtest/results（容器栈未挂载仓库根）；"
                                     "历史报告按门户静态镜像 /perf-reports/ 展示"})
    out = []
    for d in sorted(results_dir.iterdir(), key=lambda p: p.name, reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        m = _PERF_RUNDIR_RE.fullmatch(d.name)
        if not m:
            continue
        ts, short = m.group(1), m.group(2) or ""
        svc_id, svc_name = short, short
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            svc = meta.get("service") or {}
            svc_id = str(svc.get("service_id") or short)
            svc_name = str(svc.get("name") or svc_id)
        except Exception:  # noqa: BLE001 —— meta 缺/坏＝用目录短名，不猜
            pass
        html_ok = (d / "performance-report.html").is_file()
        out.append({"dir": d.name, "ts": ts, "service": svc_id, "service_name": svc_name,
                    "report_html": html_ok,
                    "report_md": (d / "performance-report.md").is_file(),
                    "href": f"/v1/admin/perf/report/{d.name}/performance-report.html" if html_ok else None,
                    "mtime": datetime.fromtimestamp(d.stat().st_mtime).isoformat(timespec="seconds")})
    return JSONResponse({"reports": out, "loadtest_available": True, "note": None})


@router.get("/v1/admin/perf/report/{run}/{fname}", response_model=None)
async def perf_report_ep(run: str, fname: str, request: Request):
    """报告静态伺服（页面 iframe src）：白名单文件名＋目录名形状闸（防路径穿越）。

    报告为自包含单文件 HTML（report_gen 产物，无外部依赖），读出经 PlainTextResponse
    按 text/html 出——容器态路径不可达＝如实 404，不回占位假报告。
    """
    if fname not in _PERF_REPORT_FILES or not _PERF_NAME_RE.fullmatch(run):
        return JSONResponse({"error": "not_found",
                             "message": "报告不存在（仅伺服 performance-report.html/.md）"}, 404)
    path = _perf_paths()[1] / run / fname
    if not path.is_file():
        return JSONResponse({"error": "not_found",
                             "message": f"报告文件不存在：{run}/{fname}"}, 404)
    return PlainTextResponse(path.read_text(encoding="utf-8"),
                             media_type=_PERF_REPORT_FILES[fname])


# ---------- B1 表结构登记（2026-10-09）：贴 DDL → sqlglot 解析 → 写回 schema csv 真源 ----------

@router.post("/v1/admin/schema/parse", response_model=None)
async def schema_parse_ep(request: Request) -> JSONResponse:
    """DDL 解析预览（不落盘）：返回 6 列草稿行＋与既有真源的差异概要。

    表名校验＝白名单正则（前端表名下拉来自 HBase 平台清单端点，消灭贴错表的主路径；
    后端不在此调 HBase——登记是元数据动作，不应因数据面不可达而阻塞）。
    """
    p = await _payload(request)
    table = str(p.get("table") or "").strip()
    ddl = str(p.get("ddl") or "")
    if not sim.table_ok(table):
        return JSONResponse({"ok": False, "error": "INVALID_TABLE",
                             "message": f"表名须匹配 {sim.TABLE_NAME_RE.pattern}"}, 400)
    try:
        parsed = sim.parse_ddl(ddl)
        existing = sim.load_rows(table)
        rows = sim.merge_rows(parsed, existing)
    except sim.SchemaImportError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message},
                            exc.status)
    old_names = {r.get("字段名", "") for r in existing}
    new_names = {r[1] for r in rows}
    return JSONResponse({
        "ok": True, "table": table,
        "rows": [{"attnum": r[0], "字段名": r[1], "数据类型": r[2],
                  "cn_name": r[3], "业务口径": r[4], "敏感级": r[5]} for r in rows],
        "diff": {"total": len(rows),
                 "added": sorted(new_names - old_names),
                 "existingKept": sorted(new_names & old_names),
                 "registered": bool(old_names)},
    })


@router.post("/v1/admin/schema/save", response_model=None)
async def schema_save_ep(request: Request) -> JSONResponse:
    """确认写回真源 sync/schema_<table>.csv（原子写；既有业务口径/敏感级已由 parse 合并保留）。

    管理动作接审计八元组（同发布/下线口径）；写端点经管理令牌闸（B3）。
    """
    p = await _payload(request)
    table = str(p.get("table") or "").strip()
    rows_in = p.get("rows")
    if not sim.table_ok(table):
        return JSONResponse({"ok": False, "error": "INVALID_TABLE",
                             "message": f"表名须匹配 {sim.TABLE_NAME_RE.pattern}"}, 400)
    if not isinstance(rows_in, list) or not rows_in:
        return JSONResponse({"ok": False, "error": "INVALID_ROWS",
                             "message": "rows 须为非空列表（来自 parse 预览回传）"}, 400)
    rows = [[str(r.get("attnum", "")), str(r.get("字段名", "")), str(r.get("数据类型", "")),
             str(r.get("cn_name", "")), str(r.get("业务口径", "")), str(r.get("敏感级", ""))]
            for r in rows_in if isinstance(r, dict)]
    try:
        path = sim.save_csv(table, rows)
    except sim.SchemaImportError as exc:
        return JSONResponse({"ok": False, "error": exc.code, "message": exc.message},
                            exc.status)
    _audit_admin_action(request, "schema-import", "console",
                        params={"table": table, "rowCount": len(rows)},
                        response={"action": "schema-import", "path": path.name},
                        latency_ms=0)
    return JSONResponse({"ok": True, "table": table, "rowCount": len(rows),
                         "path": str(path),
                         "message": f"已写回 {path.name}（{len(rows)} 字段；既有业务口径/敏感级已保留）"})

# ══ 授权管理写面（1b-8 一期 · 授权管理页 admin/grants.html）══
# 决策全在 app/grants_admin.py（矩阵 codec 下沉＋双层五条校验＋三动作写）；
# 本段只做 HTTP 编排（读侧视图/落盘/热加载/审计）。auth_admin CLI 同调同一服务层。
# 角色口径（设计稿 §1 拍板）：只读详情地板＝viewer（下钻地板三处统一裁定）；
# 写操作＝admin（ROUTE_MIN_ROLE 矩阵裁决，漏标由清单测试拦截）。

def _grants_ctx(request: Request):
    """授权写面运行期依赖：（矩阵文件路径, 合并代理的服务命名空间, 注册表 keys）。"""
    path = getattr(request.app.state, "auth_matrix_path", None) or kvx.AUTH_MATRIX_YAML
    _, services, _matrix, appkeys_path = _ctx(request)
    doc = appk.load_appkeys(appkeys_path)
    return Path(path), _namespace_with_proxies(services), doc["keys"]


@router.get("/v1/admin/grants/tenants", response_model=None)
async def grants_tenants(request: Request) -> JSONResponse:
    """派生租户清单（列表页数据源；一期＝auth_matrix ∪ appkeys 去重，1b-7 后切实体表）。"""
    _, _, matrix, _ = _ctx(request)
    _path, _services, keys = _grants_ctx(request)
    view = gam.derived_tenants(matrix, keys)
    return JSONResponse({"tenants": view,
                         "summary": {"tenants": len(view),
                                     "grants": sum(t["grantsCount"] for t in view)},
                         "caliber": {"derived": "一期租户清单＝授权矩阵∪凭证注册表派生（1b-7 后切租户实体表）",
                                     "statuses": "租户四态（active/frozen/terminating/cancelled）一期派生清单无生命周期源，恒 active"}})


@router.get("/v1/admin/grants/tenants/{tenant}", response_model=None)
async def grants_tenant_detail(tenant: str, request: Request) -> JSONResponse:
    """租户详情：租户卡＋服务∪代理混排授权矩阵＋可选服务清单＋凭证级只读摘要。"""
    path, services, keys = _grants_ctx(request)
    _, _, matrix, _ = _ctx(request)
    try:
        view = gam.tenant_detail(matrix, services, keys, tenant)
    except gam.GrantAdminError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)
    return JSONResponse(view)


@router.post("/v1/admin/grants", response_model=None)
async def grants_write(request: Request) -> JSONResponse:
    """授权登记三动作（新增/变更 upsert、停用 disable、启用 enable）。

    校验五条全落服务层（grants_admin.validate_grant_action／assert_subset_invariant）；
    落盘→热加载即时生效（沿 appkeys 免重启口径）；成功与被拒提交都落审计八元组
    （endpoint＝admin:grant.*；被拒记 result=denied 语义事件，设计稿 §6）。
    两段式停用：payload.confirm=false 时第一段拒绝执行并回 diff 预览（前端弹窗确认后
    带 confirm=true 重提）；服务端不做「记住第一阶段」的会话态（无状态、可重放校验）。
    """
    path, services, keys = _grants_ctx(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        return JSONResponse({"error": errors.INVALID_JSON, "message": "请求体须为 JSON 对象"}, 400)
    claims = getattr(request.state, "oidc_claims", None) or {}
    operator = str(claims.get("sub") or "admin")
    action = str(payload.get("action") or "")
    t0 = time.monotonic()
    try:
        view, diff = gam.apply_grant_action(path, services, keys, payload, operator)
    except gam.GrantAdminError as exc:
        _audit_admin_action(request, f"grant_{action or 'unknown'}", operator,
                            {"tenant": payload.get("tenant"), "service": payload.get("service"),
                             "action": action, "denied": exc.code},
                            {"denied": exc.code, "message": exc.message},
                            int((time.monotonic() - t0) * 1000), status=exc.status)
        return JSONResponse({"error": exc.code, "message": exc.message}, exc.status)
    except gam.MatrixFormatError as exc:
        return JSONResponse({"error": errors.INVALID_REQUEST,
                             "message": f"授权登记底稿无法解析，拒绝改写：{exc}"}, 422)
    _reload(request)   # 热加载：停用/启用/字段变更免重启即时生效
    audit_id = _audit_admin_action(
        request, f"grant_{action}", operator,
        {"tenant": view.get("tenant"), "action": action,
         "service": payload.get("service"),
         "oaTicket": bool(payload.get("oa_ticket")),
         "fieldCount": len(payload.get("fields") or [])},
        {"diff": diff, "tenantType": view.get("tenantType")},
        int((time.monotonic() - t0) * 1000))
    return JSONResponse({"grants": view.get("grants"), "diff": diff, "auditId": audit_id})

