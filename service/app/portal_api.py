# -*- coding: utf-8 -*-
"""门户只读数据端点（v3 · 离线包预灌演示数据配套）。

背景：门户六页（portal/api.js）同源拉取 /v1/portal/* 五个只读端点；此前后端无实现，
页面只能降级 mock（「演示数据」角标＝占位）。本模块补齐端点，数据口径分两类：
  ① 真实同步元信息（ecs_seed.sh 走 sync 链路 loader/reconcile/switch 真写）：
     tags:user:latest / sync:ver:<版本> / tags:news:latest / sync:ver:news:<版本>
     → 覆盖用户数、字段在册数、各域同步时间/行数、data_as_of 全部取真值；
  ③ P1-6 组件探活（service/app/monitor.py）：/v1/portal/status 追加 components，
     运行视图四状态卡由真探活驱动（探不到＝error→页面「未知」，不写死「正常」）。
  ② 真实调用统计（service/app/stats.py，挂审计链路实时计数）＋公告种子：
     hash portal:stats:live 及日滚动键（累计调用/近7日趋势/今日调用/成功率——
     每个数字对应一次真实到达平台网关/服务层的 API 调用，成败按状态码分计，
     失败含网关 key-auth 401；无调用如实为 0，不灌演示数字）
     list portal:announcements（公告条目，ecs_seed 灌入）。
未发生调用时端点仍可调（数字为 0 / 空列表），页面零崩溃。

部署形态：经门户 nginx（configs/nginx/portal-nginx.conf）反代到本服务同源伺服，
仅 compose 网内可达，不新增宿主端口；只读 GET，无鉴权语义（与门户静态页同暴露级）。
Redis 访问复用 main 的 redis.asyncio 客户端（app.state 注入，handler 全 async）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import dev_token as devtok   # 2026-10-09：管理令牌闸恢复，占位令牌可见性提示
from app import monitor as mon
from app import stats as live_stats
from app.ops_config import load_console_services   # P3 目录计数修复：发布台列表同一真源

logger = logging.getLogger("oneserve.service")

router = APIRouter()

# 公告列表键（与 scripts/ecs_seed.sh 约定；写入方唯一＝ecs_seed）
ANNOUNCE_KEY = "portal:announcements"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _kv_cfg(services: dict, service_id: str) -> dict:
    """取某 KV 服务的 backend.kv 配置（alias_key/version_meta_key 以 services.yaml 为单一事实来源）。"""
    return (services.get(service_id) or {}).get("backend", {}).get("kv", {}) or {}


async def _domain_state(redis, services: dict, service_id: str) -> dict | None:
    """读一个 KV 域的线上状态：{version, rows, tag_cols, loaded_at, data_as_of, recon_pass}；未上线返回 None。"""
    cfg = _kv_cfg(services, service_id)
    alias, meta_tpl = cfg.get("alias_key"), cfg.get("version_meta_key", "")
    if not alias:
        return None
    version = await redis.get(alias)
    if not version:
        return None
    meta = await redis.hgetall(meta_tpl.format(version=version)) or {}
    if not meta:
        return None
    # 对账闸（仅 user 域有 reconcile 链路；news 域直接 SET 上线无对账）
    recon = await redis.hgetall(meta_tpl.replace("sync:ver:", "sync:recon:").format(version=version))
    return {
        "version": version,
        "rows": int(meta.get("rows", 0) or 0),
        "tag_cols": int(meta.get("tag_cols", 0) or 0),
        "loaded_at": meta.get("loaded_at"),
        "data_as_of": meta.get("data_as_of"),
        "recon_pass": recon.get("pass") == "1",
    }


async def _stats(redis) -> dict:
    """真实调用统计（stats.read_stats：审计链路实时计数；无调用全 0，不造数）。"""
    return await live_stats.read_stats(redis)


def _audit_mode(services: dict) -> tuple[bool, str]:
    """审计开关/口径＝服务注册配置真实值（现役服务集聚合：任一 enabled 即 on，
    口径取该服务 log_params。原「user_tag_query 单代表」系死引用——服务 09-29 已删，
    2026-10-09 修复：曾致首页审计旗标恒 false，而审计实际在落盘）。"""
    for svc in services.values():
        audit = (svc or {}).get("audit") or {}
        if audit.get("enabled"):
            return True, str(audit.get("log_params", "hash_only"))
    return False, "hash_only"


def _endpoint_of(auth_matrix: list, service_id: str) -> str:
    """从授权矩阵取服务的对外端点（真实登记值；取不到给占位路径）。"""
    for row in auth_matrix or []:
        for g in row.get("grants", []):
            if g.get("service") == service_id and g.get("endpoint"):
                return str(g["endpoint"])
    return f"/v1/{service_id}"


def _ctx(request: Request):
    """从 app.state 取运行期依赖（main.lifespan 注入：redis/services/auth_matrix）。"""
    return request.app.state.redis, request.app.state.services, request.app.state.auth_matrix


@router.get("/v1/portal/overview")
async def portal_overview(request: Request) -> JSONResponse:
    """首页数字条：覆盖用户数/在线 API/接入方/累计调用＋近 7 日趋势（api.js ENDPOINTS.overview）。"""
    redis, services, auth_matrix = _ctx(request)
    # 2026-10-14 P0 死引用修复：原读已删服务 user_tag_query（09-29 随全服务下线删除）
    # → coveredUsers 恒 0（盘点实证，与 totalCalls 并存的假 0）。改读现役用户标签
    # 主表 v1-svc-usr-asset-tag（与 portal_status 同源）；覆盖用户＝该表线上版本
    # 装载键数（每键一户），口径随行下发 caliber 字段如实说明。
    user = await _domain_state(redis, services, "v1-svc-usr-asset-tag")
    stats = await _stats(redis)
    rows = user["rows"] if user else 0
    partners = len({row.get("tenant") for row in (auth_matrix or []) if row.get("tenant")})
    scope = (
        f"口径：数仓同步任务报告（数据截至 {user['data_as_of']}，"
        f"全量对账 {rows} 户在库、{user['tag_cols'] + 1} 列蓝本）" if user
        else "口径：尚未灌入同步数据（跑 scripts/ecs_seed.sh）"
    )
    caliber = (
        f"coveredUsers＝现役用户标签主表 usr_asset_tag 线上版本 {user['version']} 的"
        f"装载键数（每键一户；来源 sync:ver:usr_asset_tag:{user['version']} 元信息 rows，"
        "装载链写入、对账闸核对，非实时 COUNT）" if user
        else "coveredUsers＝现役表键数口径；当前无已上线的用户标签表版本（跑装载/发布链路后出数）"
    )
    return JSONResponse({
        "coveredUsers": rows,
        "coveredUsersScope": scope,
        "caliber": caliber,
        "dataAsOf": user.get("data_as_of") if user else None,   # P0-1 环境条「数据快照」时间（真值；未同步为 null）
        "onlineApis": len(services or {}),
        "authorizedPartners": partners,
        "totalCalls": stats["totalCalls"],
        "callsTrend7d": stats["callsTrend7d"],
        "generatedAt": _now_iso(),
    })


GATEWAY_PROBE_HOST = os.environ.get("APISIX_PROBE_HOST", "oneserve-apisix")
GATEWAY_PROBE_PORT = int(os.environ.get("APISIX_PROBE_PORT", "9080"))


@router.get("/v1/portal/status")
async def portal_status(request: Request) -> JSONResponse:
    """首页/运行视图状态条：最近同步|今日调用·成功率|审计＋组件探活。

    P1-6 追加 components：运行视图底部四状态卡的真实探活结果（网关 TCP／服务本进程／
    Redis PING／审计注册配置）——此前该四卡为前端写死常量「永远亮绿」（评估报告 F5/B4
    缺陷），现改为真探活；探不到＝error，页面如实显示「未知」，不回退「正常」。
    """
    redis, services, _ = _ctx(request)
    # 大盘自保（P1-6）：Redis 故障时本端点仍须可用——否则运维恰恰在 Redis 挂掉时
    # 拿不到「Redis=error」而是整个状态条 500（探活的意义就在故障时可读）。
    try:
        # 2026-10-09：同步域状态改读现役主标签服务（user_tag_query 已删，原死引用致 lastSyncAt 恒 null）
        user = await _domain_state(redis, services, "v1-svc-usr-asset-tag")
    except Exception:  # noqa: BLE001 —— 同步域读失败＝未同步口径（lastSyncAt=null）
        logger.warning("运行状态：同步域读取失败（按未同步降级，组件探活照常回报）", exc_info=True)
        user = None
    stats = await _stats(redis)
    audit_on, audit_mode = _audit_mode(services)
    components = await mon.components_view(
        redis, services, version=str(request.app.version or ""),
        started_monotonic=getattr(request.app.state, "started_monotonic", 0.0),
        gateway_host=GATEWAY_PROBE_HOST, gateway_port=GATEWAY_PROBE_PORT)
    return JSONResponse({
        "lastSyncAt": (user or {}).get("loaded_at"),
        "lastSyncResult": ("success" if user["recon_pass"] else "loaded") if user else "never",
        "todayCalls": stats["todayCalls"],
        "todaySuccessRate": stats["todaySuccessRate"],
        "auditEnabled": audit_on,
        "auditMode": audit_mode,
        "components": components,
        "generatedAt": _now_iso(),
    })


# ── P3 目录计数修复（2026-10-08）：目录改注册表驱动 ──
# 缺陷：旧实现按 user_tag_query/news_tag_query 等历史硬编码 service_id 拼域；
# 2026-09-29 全下线＋发布台改发 v1-svc-* 命名后旧 id 均不在注册表 → /v1/portal/catalog
# 恒回 domains:[]，目录页/首页搜索条恒显 0 服务（与发布台列表脱节）。
# 修法：条目＝console_services 写区（与 GET /v1/admin/publish 的 services 列表同一真源、
# 同一加载器 load_console_services），业务域按 field_space 派生（口径同
# scripts/gen_openapi.py _domain_of：空间名册优先、缺省用户标签域；scripts/ 不在 service
# 容器挂载内，小表就地复制）。聚合逻辑收敛在纯函数 _catalog_view（无 IO，
# tests/test_portal_catalog.py 直测口径）。
_FIELD_SPACE_DOMAIN = {
    "news": ("news", "资讯标签域"),
    "proxy": ("proxy", "合作方代理域"),
    "usr_basic_info": ("user", "用户基础信息域"),
    "usr_asset_tag": ("user", "用户资产标签域"),
    "card_acct_summary": ("user", "卡账户汇总域"),
}
_STATUS_VIEW = {"published": ("ok", "正常"), "offline": ("warn", "已下线"), "draft": ("warn", "草稿")}


def _domain_of(entry: dict) -> tuple[str, str]:
    """服务条目 → (域 id, 域名)：字段空间名册优先，未登记空间回落用户标签域（同 gen_openapi）。"""
    return _FIELD_SPACE_DOMAIN.get(str(entry.get("field_space") or "")) or ("user", "用户标签域")


def _catalog_view(entries: list[dict], runtime_ids, auth_matrix: list,
                  sync_times: dict | None = None) -> tuple[list[dict], int]:
    """目录聚合纯函数（无 IO）：console_services 条目 → 按域分组视图＋totalFields。

    - 服务数/字段数＝注册表真实值：条目数即计数，与发布台列表恒等（本卡验收口径）；
    - serviceId 仅对运行时注册表在册服务下发（详情壳存在面＝构建期按注册表生成），
      offline/草稿条目不带 → 前端按既有口径回落目录页，不出死链；
    - 域状态：域内含在册 published 服务＝正常，否则如实已下线/草稿；条目级 status
      随目录下发（published/offline/draft，页面条目态展示归 UI 卡）；
    - lastSyncAt：sync_times（sid→loaded_at，端点层预取真值）命中首条即用，缺省"—"。
    - desc（N5 2026-10-14）：业务描述＝条目 desc 字段；未登记＝「暂无描述」如实占位。
    - updateFrequency/owner（维护者 2026-10-15 批「按建议」）：目录模板属性两件——
      更新频率＝条目 update_frequency（表数据装载节奏事实）、责任主体＝条目 owner；
      未登记/空白＝「未标注」如实占位，不造假值（同 desc 口径）。
    """
    sync_times = sync_times or {}
    domains: dict[str, dict] = {}
    for e in entries:
        sid = str(e.get("id") or "")
        status = str(e.get("status") or "draft")
        dom_id, dom_name = _domain_of(e)
        dom = domains.setdefault(dom_id, {
            "id": dom_id, "name": dom_name, "fields": 0, "services": 0,
            "lastSyncAt": "—", "status": "warn", "statusText": "已下线", "items": [],
        })
        item = {
            "name": str(e.get("name") or sid),
            "api": str(e.get("route") or _endpoint_of(auth_matrix, sid)),
            # N5（2026-10-14 门槛稿 AC-1）：desc＝服务卡业务描述（services.yaml 条目 desc，
            #   中文一句、无技术黑话）；条目未登记描述时如实「暂无描述」，不拿查询键
            #   口径（key）冒充业务描述，也不占位造假。
            "desc": str(e.get("desc") or "").strip() or "暂无描述",
            # 维护者 2026-10-15 批「按建议」：目录模板属性——更新频率（表数据装载节奏
            # 事实，services.yaml 条目 update_frequency）/责任主体（owner）；未登记
            # 如实「未标注」，不拿默认节奏或平台名冒充（口径同 desc 占位）。
            "updateFrequency": str(e.get("update_frequency") or "").strip() or "未标注",
            "owner": str(e.get("owner") or "").strip() or "未标注",
            "status": status,
        }
        if sid in runtime_ids:
            item["serviceId"] = sid
            dom["status"], dom["statusText"] = _STATUS_VIEW.get(status, ("warn", "草稿"))
            if dom["lastSyncAt"] == "—" and sync_times.get(sid):
                dom["lastSyncAt"] = sync_times[sid]
        dom["fields"] += len(e.get("fields") or [])
        dom["services"] += 1
        dom["items"].append(item)
    view = list(domains.values())
    return view, sum(d["fields"] for d in view)


@router.get("/v1/portal/catalog")
async def portal_catalog(request: Request) -> JSONResponse:
    """服务目录（按业务域分组）：注册表驱动，与发布台列表同一真源（ENDPOINTS.catalog）。

    同步时间对带 KV 别名的条目预取真值；读取失败按未同步降级（"—"），不砸整页目录。
    """
    redis, services, auth_matrix = _ctx(request)
    entries = load_console_services()
    sync_times: dict[str, str] = {}
    for e in entries:
        sid = str(e.get("id") or "")
        try:
            st = await _domain_state(redis, {sid: e}, sid)
        except Exception:  # noqa: BLE001 —— 同步域读取失败按未同步展示（P1-6 同款自保）
            st = None
        loaded = (st or {}).get("loaded_at")
        if loaded:
            sync_times[sid] = loaded
    domains, total_fields = _catalog_view(entries, services or {}, auth_matrix, sync_times)
    return JSONResponse({
        "totalFields": total_fields,
        "domains": domains,
        "generatedAt": _now_iso(),
    })


@router.get("/v1/portal/freshness")
async def portal_freshness(request: Request) -> JSONResponse:
    """数据新鲜度：现役各表最近一次装载（真值；运行视图同步链路图同源）。

    2026-10-14 P0 死引用修复：原硬编码 user_tag_query/news_tag_query 两服务（均已
    随 09-29 下线删除）→ domains 恒 []（盘点实证）。改注册表驱动：遍历现役 KV 服
    务，按表（kv.namespace 尾段）去重——同表多服务只出一条（取 service_id 字典序
    首条）；域名沿用目录 _FIELD_SPACE_DOMAIN 名册；未上线（无线上版本指针）的表
    如实不列。新鲜度＝各表最近装载版本日期（lastSyncAt=装载时刻，version=版本）。
    """
    redis, services, _ = _ctx(request)
    out, seen_tables = [], set()
    for sid in sorted(services or {}):
        table = str(_kv_cfg(services, sid).get("namespace") or "").rsplit(":", 1)[-1]
        if not table or table in seen_tables:
            continue
        st = await _domain_state(redis, services, sid)
        if st is None:
            continue
        seen_tables.add(table)
        _, dom_name = _FIELD_SPACE_DOMAIN.get(table, ("user", "用户标签域"))
        out.append({
            "id": table, "name": dom_name,
            "lastSyncAt": st["loaded_at"], "rows": st["rows"],
            "dataAsOf": st["data_as_of"], "version": st["version"],
            "status": "ok",   # 线上指针＋版本元信息在即在线（对账闸在 switch 三闸）
        })
    return JSONResponse({
        "generatedAt": _now_iso(), "domains": out,
        "caliber": "domains＝现役 KV 表各表最近一次装载（alias 线上指针→sync:ver 版本"
                   "元信息 loaded_at/rows/data_as_of）；同表多服务按表去重（取 service_id "
                   "字典序首条）；未上线表如实不列",
    })


@router.get("/v1/portal/announcements")
async def portal_announcements(request: Request) -> JSONResponse:
    """公告栏：读 ecs_seed 灌入的 list（LRANGE 只读；空列表＝未灌数）。"""
    redis, _, _ = _ctx(request)
    items = []
    for raw in await redis.lrange(ANNOUNCE_KEY, 0, 9):
        try:
            items.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return JSONResponse({"items": items, "generatedAt": _now_iso()})


# 2026-10-09：管理令牌闸恢复（维护者 拍板：目标＝部署生产直接使用），占位令牌提示端点随之恢复。
# 安全口径（纯逻辑见 app/dev_token.py，单一裁决点）：
#   · 只有「开发占位态」（ADMIN_API_TOKEN 未显式配置或＝内置默认值）才返回 token 真值；
#   · 一旦部署方显式设为非默认值，返回 token=null（不泄漏，连掩码也不给）；
#   · 只读 GET、无鉴权语义（与门户静态页同暴露级，nginx 同源反代，仅 compose 网内可达）。
@router.get("/v1/portal/admin-token-hint")
async def admin_token_hint(request: Request) -> JSONResponse:
    """【仅开发用途】运营页管理令牌提示（开发占位令牌可见卡）。"""
    return JSONResponse(devtok.dev_token_view(devtok.runtime_admin_token()))