"""OneServe 服务层（task-002 骨架 ＋ task-003 标签点查）。

SPEC §3.1 服务层＝平台唯一自研代码：
  /healthz          健康端点（task-002 建；P2a 深化＝组件探活 redis/hbase/网关 apisix+etcd，
                    供运维/容器编排；既有 status/redis 字段与 503 语义保留向后兼容）
  /v1/ping          端到端验证路由：网关验签→转发→读 redis ping 值（task-002）
  /v1/tags/query    A 型 KV 点查（task-003，正式通道：网关 key-auth 保护，
                    调用方传上游生成的查询键；执行逻辑全部在 kv_executor.py，配置驱动）
  （2026-10-14 P0 死端点清理：/v1/news/query（资讯域已下线）、/v1/hbase/query
   （HBase 不作独立数据源发 API，2026-09-23 裁定）等死服务层路由已删——
   到达服务层一律 404 service_not_found 信封，不再有裸 500。）
  /v1/query/{sid}   通用查询端点（M2-3.5 渲染器判据通道：service_id 由路由路径给，
                    按 services.yaml 注册表分发 redis/hbase 执行器——新增服务＝
                    只改 yaml 免代码；既有专用端点保留兼容）
  /demo             演示页（输入手机号→出标签表）（task-003，经网关、不走 key-auth）
  /demo/query       演示通道端点（演示页把手机号转为查询键；真实接入传上游生成的键）
  /v1/admin/*       运营凭证端点（P0-4：列表/签发/禁用/热加载；nginx 反代＋管理令牌）

每次业务调用记审计八元组（audit.py，WAL 兜底；P0-5 起 trace_id 为第八列），
并累计真实调用统计（stats.py → Redis portal:stats:live，门户 KPI 唯一数据来源；不造数）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import appkeys as appk
from app import connections as conn_cfg   # 卡1：外部数据源连接配置（env > yaml > 默认双轨）
from app import errors
from app import healthz as hlz   # P2a：/healthz 组件探活纯逻辑（不依赖 FastAPI，宿主可单测）
from app import hbase_executor as hbx
from app import hbase_service as hbsvc
from app import kv_executor as kvx
from app import meta_writer as mw   # 卡-1b7 步5/8：写侧统一入口（MetaDbError 写冻结出口）
from app import meta_snapshot as ms  # 读侧热载切快照（ADR-0021 收口小批）：hot_read_path 落点
from app import registry_hot as rhot      # M2-3.5：注册表热加载（mtime 轮询＋网关下发）
from app import proxy_hub                  # B 型代理中枢（2026-10-15 自转发闭环）：/proxy/* 透传＋upstreams.yaml 消费
# 2026-10-14 P0：proxy_executor 导入随历史代理路由删除而移除（main 内零引用；
# 执行器模块本身保留——上游代理链方向待 维护者 另裁）。
from app import stats as live_stats
from app import admin_api   # P0-4：凭证发放/热加载端点（/v1/admin/*）；2026-10-09 闸异常类
from app.admin_api import router as admin_router   # /v1/admin/*：凭证发放/禁用/热加载/装载/发布
# 【无登录模式 ADR-0029·已摘接线】开源版拆除人机登录（维护者 2026-10-28 拍板）：
# · auth_router（/v1/admin/auth/* 登录闭环）不再挂载；
# · admin_accounts_router（/v1/admin/accounts/*，Casdoor 管理 API 代理）不再挂载；
# · Casdoor 登录审计消费（audit_casdoor_puller）线程不再启动；
# · auth_oidc.py / auth_native.py / admin_accounts.py / audit_casdoor_puller.py /
#   users.yaml 已随 v0.1.1 清理删除（ADR-0029 §7 兑现，2026-10-29）；admin 写闸里的
#   require_login / adjudicate_route_role 挂点同步摘除（恒放行不变）；历史接线见 git 史。
from app.audit import AuditWriter, make_entry
from app.my_api import router as my_router     # P0-7：我的接入自助端点（/v1/portal/my/*）
from app.audit_api import router as audit_router   # P1-1：审计查询端点（/v1/portal/audit/*）
from app.monitor_api import router as monitor_router  # P1-6：异常与批量监控端点（/v1/admin/monitor/*）
from app.metering_api import router as metering_router  # P1 计量报表端点（/v1/admin/metering/*，独立文件：admin_api 有并行卡）
from app.batch_api import router as batch_router     # M15-1：批量查询任务端点（/v1/batch/*）
from app.portal_api import router as portal_router   # v3：门户只读数据端点（/v1/portal/*）
from app.grants_api import router as grants_router   # P1-8：授权登记全景端点（/v1/portal/grants/*）
from app import tenants_api   # 管理功能群一期 A 节：租户管理端点（/v1/admin/tenants/*，b039a55 交付、挂载归收口批）
from app import pricing_api   # 管理功能群一期 B 节：价目管理端点（/v1/admin/pricing/*，0be2351 交付、挂载归收口批）
from app import billing_api   # 管理功能群一期 C 节：月账单端点（/v1/admin/billing/*，bb4476b/a331210 交付、挂载归收口批）
from app.trace import TRACE_HEADER, new_trace_id, resolve_trace_id  # P0-5：traceId 贯通（纯逻辑在 trace.py）

logger = logging.getLogger("oneserve.service")
logging.basicConfig(level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0")
AUDIT_DIR = os.environ.get("AUDIT_DIR", "/data/audit")
PING_KEY = "oneserve:ping"  # redis 中预置的 ping 值（端到端链路验证用）
PING_VALUE = "pong-m0-skeleton"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"   # /app/static（演示页）
APPKEYS_PATH = Path(os.environ.get("APPKEYS_YAML", str(appk.APPKEYS_YAML)))  # P0-4 凭证注册表（可注路径便于测试）

redis_client: aioredis.Redis | None = None
audit: AuditWriter | None = None
services: dict | None = None          # services.yaml 注册表（lifespan 加载；卡4＝service_id 单键）
auth_matrix: list | None = None       # auth_matrix.yaml 授权矩阵（lifespan 加载；热加载刷新）
appkeys_registry: list | None = None  # appkeys.yaml 控制台签发凭证注册表（P0-4，热加载刷新）
hbase_client: hbx.HBaseClient | None = None  # HBase 连接池客户端（启动即建，与注册表解耦；卡1）
hbase_pool_error: Exception | None = None     # 最近一次建池失败底因（诊断分类用；成功置 None）
registry_mtime: float | None = None   # services.yaml 当前生效 mtime（M2-3.5 热加载水位）
registry_watcher: asyncio.Task | None = None  # mtime 轮询后台任务（lifespan 建/取消）
upstreams_watcher: asyncio.Task | None = None  # upstreams.yaml 看门（B 型代理路由热加载，lifespan 建/取消）

# 演示通道：大陆手机号口径（gen_mock_source.fake_phone 生成的假号段一致）
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")


class RedisStore:
    """kv_executor 的 KV 访问适配器（异步 get/hget；decode_responses 已开，值即 str）。"""

    def __init__(self, client: aioredis.Redis):
        self._c = client

    async def get(self, key: str) -> str | None:
        return await self._c.get(key)

    async def hget(self, key: str, field: str) -> str | None:
        return await self._c.hget(key, field)


def _build_hbase_client(connection: "conn_cfg.HBaseConnection | None" = None) -> "hbx.HBaseClient | None":
    """建 HBase 连接池（卡1：host/port 走 connections.py 双轨装配——环境变量
    HBASE_HOST/HBASE_PORT > config/connections.yaml > 默认值；本机替身与未来 MRS
    只改配置不改代码）。**与 services.yaml 注册表解耦**：建池不问「有无 hbase 型
    服务在场」，服务上下线不影响池（池常在，发现/装载/查询随时可用；失效自愈
    归 admin_api._hbase_client 取用时惰性重建）。
    失败返回 None 并告警（故障域隔离 A4：HBase 起不来只降级 hbase 型服务，
    Redis 存量不受影响）；不抛断启动。
    """
    global hbase_pool_error
    try:
        conn = connection or conn_cfg.load_hbase_connection()
        out = hbx.HBaseClient(hbx.make_pool(host=conn.host, port=conn.port))
        hbase_pool_error = None
        return out
    except Exception as exc:  # noqa: BLE001 —— 故障域隔离（A4）
        hbase_pool_error = exc   # 卡1：留底因——发现端点据此做三分类（含未启动→不可达）
        logger.warning("HBase 连接池建池失败（hbase 通道将 503 拒服，Redis 存量不受影响）：%s", exc)
        return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动：恢复 WAL → 连 redis → 预置 ping 值 → 加载服务注册与授权矩阵。"""
    global redis_client, audit, services, auth_matrix, appkeys_registry, hbase_client
    global registry_mtime, registry_watcher, hbase_connection
    global upstreams_watcher
    audit = AuditWriter(AUDIT_DIR)
    recovered = audit.recover()
    if recovered:
        logger.info("审计 WAL 恢复：%d 条补入 JSONL", recovered)

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    await redis_client.set(PING_KEY, PING_VALUE, nx=True)  # 不覆盖已有值
    svc_read = ms.hot_read_path(rhot.SERVICES_YAML)   # services 注册表读侧不切快照（快照运行时区非 load_services 全集——console 覆盖行落写区，演练实证 404；恒仓内权威源，见 meta_snapshot.hot_read_path 注）
    services = kvx.load_services(svc_read)    # services.yaml（配置驱动；缺失/坏配置＝启动即失败）
    auth_matrix = kvx.load_auth_matrix(ms.hot_read_path(kvx.AUTH_MATRIX_YAML))   # auth_matrix.yaml（静态授权矩阵）
    appkeys_registry = appk.load_appkeys(ms.hot_read_path(APPKEYS_PATH))["keys"]  # P0-4：控制台签发凭证注册表
    # 卡1：连接池生命周期与 services.yaml 解耦——启动即建（不问注册表有无 hbase 型服务；
    # 服务上下线不影响池）。host/port 经 connections.py 双轨装配（env > yaml > 默认）；
    # 坏 connections.yaml＝启动即失败（如实报错，口径同 services.yaml），连接失败＝降级 None。
    hbase_connection = conn_cfg.load_hbase_connection()
    hbase_client = _build_hbase_client(hbase_connection)
    if hbase_client is not None:
        logger.info("HBase 连接池就绪：host=%s port=%s auth_mode=%s（独立于注册表；服务上下线不影响池）",
                    hbase_connection.host, hbase_connection.port, hbase_connection.auth_mode)
    registry_mtime = rhot._mtime(svc_read)   # 热加载水位＝启动文件 mtime（db 态＝快照件；看门与读径同源）
    registry_watcher = asyncio.create_task(             # M2-3.5：services.yaml mtime 轮询看门
        rhot.watcher_loop(
            lambda: (services, registry_mtime),
            _apply_registry_state,
            interval_s=float(os.environ.get("REGISTRY_POLL_INTERVAL_S", rhot.POLL_INTERVAL_S)),
            path=svc_read,
        ))
    # B 型代理（2026-10-15 自转发闭环）：启动即幂等下发已发布代理的网关路由
    # （存量路由收敛；下发失败不阻断启动——看门下拍重试）；随后起 upstreams.yaml
    # mtime 看门（登记/发布/下线 ≤轮询间隔生效：发布→PUT 路由，下线→DELETE 摘流）。
    try:
        _px_ok, _px_bad = await proxy_hub.push_proxy_routes()
        if _px_bad:
            logger.warning("启动代理路由下发部分失败（%d ok / %d bad），看门续跑追平", _px_ok, _px_bad)
    except Exception:  # noqa: BLE001 —— 网关暂不可达不阻断启动（看门重试）
        logger.warning("启动代理路由下发失败（网关暂不可达？看门续跑追平）", exc_info=True)
    upstreams_watcher = asyncio.create_task(proxy_hub.upstreams_watcher_loop())
    logger.info("service 就绪：redis=%s audit_dir=%s 注册服务=%s",
                REDIS_URL, AUDIT_DIR, sorted(k for k in services if isinstance(k, str)))
    _app.state.redis = redis_client          # v3：门户端点运行期依赖（portal_api 经 app.state 取用）
    _app.state.services = services
    _app.state.auth_matrix = auth_matrix
    _app.state.appkeys = appkeys_registry    # P0-4：admin_api 取用（热加载后同步刷新）
    _app.state.appkeys_path = ms.hot_read_path(APPKEYS_PATH)   # 读侧状态＝读径同源（db 态指快照＝库镜像，与 db e2e fixture 同口径）
    _app.state.auth_matrix_path = ms.hot_read_path(kvx.AUTH_MATRIX_YAML)   # 1b-8 授权写面（db 态指快照：save_matrix 规划面落库镜像，yaml 态同源不变）
    _app.state.hbase_client = hbase_client   # M2-4：admin 数据源发现（AC-4）取用
    _app.state.hbase_connection = hbase_connection  # 卡1：诊断文案取当前生效 host:port/auth_mode
    _app.state.audit = audit                  # M15-1：批量端点经 app.state 取审计写入器
    # 【无登录模式 ADR-0029·已摘】native 首启种子（ONESERVE_SEED_ADMIN_* 建 admin 账号）
    # 与 Casdoor 登录审计消费线程随登录拆除一并退役（历史接线见 git 史；users.yaml
    # 已随 v0.1.1 清理删除，无任何路径再写入）。
    _app.state.started_monotonic = time.monotonic()   # P1-6：大盘「服务」组件 uptime 真值起点
    yield
    for _watcher in (registry_watcher, upstreams_watcher):
        if _watcher is not None:
            _watcher.cancel()            # 优雅退出：先停看门（services.yaml＋upstreams.yaml 两只）再收尾
            try:
                await _watcher
            except asyncio.CancelledError:
                pass
    await redis_client.aclose()  # 优雅退出：释放连接；审计落盘由 audit-wal-flusher 后台批量刷（默认 200ms 间隔），atexit/close 兜底最终 fsync（夜卡012 白天复产）


# ---------- §4.4 envelope 统一：框架默认错误也入同一信封（P1-2） ----------
# FastAPI/Starlette 自带的 422（参数类型校验）与 404/405（路由/方法不存在）默认体为
# {"detail": ...}，不含机读码。这里把它们收敛进同一 envelope：error 用机读码中心
# 的登记码，traceId/request_log_url 由 trace_middleware 统一补齐（与端点自产错误
# 同一收口点）。既有 detail 字段保留（向后兼容，接入方可能已在读）。

def _http_exception_to_envelope(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    status = exc.status_code
    code = errors.SERVICE_NOT_FOUND if status == 404 else (
        errors.INVALID_REQUEST if status in (400, 405, 422) else errors.INTERNAL_ERROR)
    message = str(exc.detail) if exc.detail is not None else "请求失败"
    body = errors.envelope(code, message)
    if isinstance(exc.detail, str):
        body["detail"] = exc.detail          # 既有字段不改名（向后兼容）
    return JSONResponse(body, status_code=status)


async def _http_exc_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _http_exception_to_envelope(request, exc)


async def _validation_exc_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic 参数校验失败（422）→ envelope；报首个出错参数名（param 指明改哪里）。"""
    errs = exc.errors() or []
    loc = [str(p) for p in (errs[0].get("loc") or []) if p not in ("body", "query", "path")]
    param = loc[0] if loc else ""
    body = errors.envelope(errors.INVALID_REQUEST, "请求参数校验失败（类型/取值不合法）",
                           param=param)
    body["detail"] = errs                     # 既有 detail 结构保留（向后兼容）
    return JSONResponse(body, status_code=422)


async def _admin_gate_denied_handler(request: Request, exc: Exception) -> JSONResponse:
    """管理令牌闸拒绝 → 项目错误信封 401（与前端 apiFetch 错误规整同契约）。"""
    return JSONResponse(
        {"error": errors.UNAUTHORIZED, "message": "管理令牌缺失或不正确（X-Admin-Token）"}, 401)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """未预期异常全局兜底（2026-10-14 P0）：任何未捕获异常 → 统一错误信封，不再裸 500。

    盘点实证缺口：历史服务 KeyError / hbase TypeError 等未注册兜底 → Internal Server
    Error 裸文本。本 handler 注册在 Exception 上（Starlette ServerErrorMiddleware 层，
    位于 trace_middleware 之外）——异常穿透中间件后其后置注入不再执行，故：
    - 日志在此记完整栈（logger.exception）；
    - traceId 直接读 request.state（trace_middleware 进口已注入；读不到则空串，
      信封按 errors.envelope 口径不造死链）；
    - X-Trace-Id 响应头由本 handler 自挂（与中间件同值同口径）。
    状态码 500＝真未预期异常；存储降级拒绝仍走 _run_query 的 internal_error 503。
    """
    logger.exception("未预期异常兜底：%s %s", request.method, request.url.path)
    trace_id = getattr(request.state, "trace_id", "") or ""
    body = errors.envelope(errors.INTERNAL_ERROR, "服务内部错误（未预期异常）",
                           trace_id=trace_id)
    resp = JSONResponse(body, status_code=500)
    if trace_id:
        resp.headers[TRACE_HEADER] = trace_id
    return resp


app = FastAPI(title="OneServe service", version="0.2.0", lifespan=lifespan)
# 框架级错误（404/405/422）统一入 §4.4 信封（须在建 app 之后注册）
app.add_exception_handler(StarletteHTTPException, _http_exc_handler)
app.add_exception_handler(RequestValidationError, _validation_exc_handler)
# 管理令牌闸拒绝 → 项目错误信封 401（依赖必须 raise 才短路；自定义异常避免动全局
# HTTPException 信封格式）。2026-10-09 恢复期接入；2026-10-14 闸恒放行后不再触发，
# handler 注册保留供恢复闸期原样复用（ADR-0010）。
app.add_exception_handler(admin_api.AdminGateDenied, _admin_gate_denied_handler)
# ── 登录闸休眠 handler 骨架（ADR-0029；休眠是既定架构决策，非尸体） ──────────
# 无登录模式下零触发：异常类与信封构造原在 auth_oidc.py（v0.1.1 已清），现就地
# 休眠登记于此；错误码 auth_required/role_forbidden 文案中心仍同源 errors.py。
# 历史形态（JWKS 验签/角色矩阵/登录闭环端点）见 git 史。
class AuthRequiredError(Exception):
    """管理写操作未登录/令牌无效（休眠：无登录模式 ADR-0029 下零触发）。"""

    def __init__(self, reason: str = "missing_token"):
        self.reason = reason
        super().__init__(reason)


class RoleForbiddenError(Exception):
    """已登录但角色不足（休眠：无登录模式 ADR-0029 下零触发）。"""

    def __init__(self, min_role: str, actual: str):
        self.min_role = min_role
        self.actual = actual
        super().__init__(f"min_role={min_role} actual={actual}")


_AUTH_REQUIRED_REASONS = {
    "missing_token": "未登录（缺少 Bearer 令牌）",
    "bad_token": "登录令牌无效或已过期",
    "jwks_unavailable": "登录服务不可达（JWKS/令牌端点拉取失败），稍后重试",
    "dependency_missing": "服务端登录校验依赖缺失",
    "secret_missing": "服务端登录密钥未配置",
    "bad_state": "登录回跳校验失败（state 不符或已过期），请重新发起",
}


def _auth_required_response(exc: AuthRequiredError):
    """401 auth_required 信封（错误码中心同源；休眠骨架，供恢复闸期原样复用）。"""
    return JSONResponse(
        errors.envelope(errors.AUTH_REQUIRED,
                        _AUTH_REQUIRED_REASONS.get(exc.reason, _AUTH_REQUIRED_REASONS["bad_token"])),
        status_code=401)


def _role_forbidden_response(exc: RoleForbiddenError):
    """403 role_forbidden 信封（错误码中心同源；休眠骨架，供恢复闸期原样复用）。"""
    return JSONResponse(
        errors.envelope(errors.ROLE_FORBIDDEN,
                        f"当前账号角色（{exc.actual}）无权执行该操作，需要 {exc.min_role} 及以上"),
        status_code=403)


# OIDC 登录闸拒绝（管理写操作无/坏 Bearer）→ 401 auth_required 信封（2026-10-14
# Casdoor 集成；ADR-0010 硬前提兑现——前端检测本码跳统一登录页）。休眠登记。
app.add_exception_handler(AuthRequiredError,
                          lambda req, exc: _auth_required_response(exc))
# W4 角色闸拒绝（已登录但角色不足）→ 403 role_forbidden 信封（错误码中心新码，
# ADR-0016；require_role/adjudicate_route_role raise，前端据此提示权限不足）。休眠登记。
app.add_exception_handler(RoleForbiddenError,
                          lambda req, exc: _role_forbidden_response(exc))
# 元数据库故障统一出口（卡-1b7 步8 写冻结拒绝码；注册须在 Exception 兜底之前——
# 专用 handler 优先）。写端点经 meta_writer db 模式碰库失败抛 MetaDbError：
#   探针不可达＝写冻结态 → 503 WRITE_FROZEN（DR runbook §2 影响面：发布/凭证/
#   授权等管理面写操作全部拒绝，数据面读快照照常；冻结为派生态——库恢复即自动
#   解冻，无独立开关，解冻验证＝对账清零后试写成功，详 DR runbook §3）＋
#   P1·写冻结 告警日志（runbook §2 告警模板口径，docker logs 可采集）；
#   探针可达＝非连接类 DB 错误（约束/SQL/数据形状）→ 500 META_DB_ERROR 原样人话，
#   不冒充冻结（误报冻结会掩盖真故障）。
async def _metadb_exc_handler(request: Request, exc: "mw.MetaDbError") -> JSONResponse:
    if mw.db_reachable():
        logger.error("meta 库写径错误（库可达，非冻结态）：%s %s：%s",
                     request.method, request.url.path, exc)
        return JSONResponse(
            {"error": "META_DB_ERROR",
             "message": "元数据写径错误（库可达，非冻结态）：" + str(exc)[:200]}, 500)
    logger.warning("【P1·写冻结】metadata-db 不可达自本条告警起；管理面写端点拒绝服务"
                   "（%s %s）——数据面照常（读快照），恢复后自动解冻，处置见 "
                   "docs/runbooks/元数据库DR_runbook.md", request.method, request.url.path)
    return JSONResponse(
        {"error": "WRITE_FROZEN",
         "message": "元数据库不可达，管理面写操作已冻结（数据面照常服务）；"
                    "恢复并解冻后重试，详见 docs/runbooks/元数据库DR_runbook.md"}, 503)


app.add_exception_handler(mw.MetaDbError, _metadb_exc_handler)
# 未预期异常全局兜底（2026-10-14 P0）：Exception 兜底注册在最后——任何未被上面
# 专用 handler 接住的异常都进 §4.4 信封（internal_error 500），全端点无裸 500。
app.add_exception_handler(Exception, _unhandled_exception_handler)
# 单测/裸跑（TestClient 直用 app、不进 lifespan）安全缺省：管理/门户端点经
# app.state.* 取运行期依赖（admin_api._ctx 等），缺 lifespan 装配时 AttributeError
# 逃逸＝主请求 500（2026-10-15 回归实锤：GET /v1/admin/appkeys 恒放行后直达 _ctx，
# 审计链断在 state.redis 缺失）。挂只读缺省：redis=None＝端点侧各自降级（审计失败
# 不炸主请求），凭证列表读 appkeys.yaml 即时值本不依赖 redis；lifespan 装配时按
# 真引用覆盖，生产行为零变化。
app.state.redis = None
app.state.services = {}
app.state.auth_matrix = []
app.state.appkeys_path = APPKEYS_PATH
app.state.auth_matrix_path = kvx.AUTH_MATRIX_YAML   # 1b-8 授权管理写面（grants 端点落盘路径；lifespan 起按快照优先重定向）
app.include_router(portal_router)   # /v1/portal/*：门户 KPI/目录/新鲜度/公告（只读，nginx 同源反代）
# 【无登录模式 ADR-0029·已摘挂载】登录闭环与账号管理代理不再挂载（文件保留，v0.1.1 清尸体）：
# app.include_router(auth_router)          # /v1/admin/auth/*：登录闭环（config 发现/login/callback/refresh）
# app.include_router(admin_accounts_router)  # /v1/admin/accounts/*：账号管理代理（Casdoor 管理 API 五件）
app.include_router(batch_router)    # /v1/batch/jobs*：批量查询异步任务（M15-1 提交/状态；AC-1/2/4）
app.include_router(admin_router)    # /v1/admin/*：凭证发放/禁用/热加载（P0-4，nginx 反代＋管理令牌）
app.include_router(my_router)       # /v1/portal/my/*：我的接入自助视图（P0-7，只读掩码口径）
app.include_router(audit_router)    # /v1/portal/audit/*：C 面审计查询（P1-1，只读八元组）
app.include_router(grants_router)   # /v1/portal/grants/*：C 面授权全景/字段反查/OA 溯源（P1-8，只读）
app.include_router(monitor_router)  # /v1/admin/monitor/*：异常与批量监控（P1-6，只读，管理令牌闸）
app.include_router(metering_router) # /v1/admin/metering/*：计量报表（P1 首批，只读聚合，审计 JSONL 真源）
app.include_router(tenants_api.router)  # /v1/admin/tenants/*：管理功能群 A 节·租户管理（登记/冻结解冻/对账摘挂；收口批挂载）
app.include_router(pricing_api.router)  # /v1/admin/pricing/*：管理功能群 B 节·价目管理（三模型价目＋命中预演/覆盖率；收口批挂载）
app.include_router(billing_api.router)  # /v1/admin/billing/*：管理功能群 C 节·月账单（列表/详情/CSV/确认/补录；收口批挂载）


def reload_auth_state(fastapp: FastAPI | None = None) -> dict:
    """热加载（P0-4）：重读 auth_matrix.yaml＋appkeys.yaml，免重启生效。

    触发点：① 控制台签发/禁用后（admin_api._reload）；② POST /v1/admin/reload
    （外部 auth_admin CLI／手编文件后）。services.yaml 仍按既有口径改后重启。
    读侧热载切快照（ADR-0021 收口小批）：读径经 ms.hot_read_path——META_SNAPSHOT_DIR
    配置且快照在位读快照（db 态库镜像；快照缺文件回落仓内＋WARN），yaml 态原路径。
    """
    global auth_matrix, appkeys_registry
    auth_matrix = kvx.load_auth_matrix(ms.hot_read_path(kvx.AUTH_MATRIX_YAML))
    appkeys_registry = appk.load_appkeys(ms.hot_read_path(APPKEYS_PATH))["keys"]
    if fastapp is not None:
        fastapp.state.auth_matrix = auth_matrix
        fastapp.state.appkeys = appkeys_registry
    logger.info("热加载完成：矩阵租户=%d 签发凭证=%d",
                len({r.get("tenant") for r in auth_matrix}), len(appkeys_registry))
    return {"matrix_tenants": len({r.get("tenant") for r in auth_matrix if r.get("tenant")}),
            "issued_keys": len(appkeys_registry)}


def _caller_grant(caller: str, service_id: str) -> dict:
    """业务鉴权统一入口（P0-4）：控制台签发凭证先查注册表，未命中回落授权矩阵。

    注册表命中时禁用/过期即时生效（热加载后内存即为最新）；两路错误同为
    TagQueryError(403)，端点层既有 except 口径零改动。
    卡4（AC-3a）：授权定位只剩 grants.service == service_id 一条路径（服务修订号
    已删）；签发凭证注册表（appkeys.yaml）同为 service_id 粒度，两路同构。
    """
    grant = appk.resolve_grant(appkeys_registry or [], caller, service_id)
    if grant is not None:
        return grant
    return kvx.tenant_grant(auth_matrix, service_id, appkey=caller)


# 【无登录模式 ADR-0029·已摘】W1 站点墙中间件（ADR-0015 整站登录墙的后端数据闸，
# AUTH_OIDC_MODE=on 时墙域 401 auth_required）随登录拆除整体退役：全放行、零拦截，
# 打开门户/管理台即用。分域清单与验签逻辑随 auth_oidc.py 于 v0.1.1 清理删除；
# admin 写闸里的 require_login/adjudicate_route_role 挂点同步摘除（恒放行不变）。
# 历史实现见 git 史（site_wall_middleware，注册序口径：原先于 trace_middleware 定义）。


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """P0-5 traceId 贯通（SPEC §4.5）：入口解析 → request.state 注入 → 响应回传。

    - 网关 APISIX request-id 已生成则透传（X-Trace-Id；兼容读 X-OneServe-Trace-Id），
      缺则自生成（不经网关的直达调用同样有 trace）；
    - 响应头 X-Trace-Id 回传同值（成功/失败一律带，客户端拿头即知本次 traceId）；
    - 错误即入口（Stripe 模式）：状态码 ≥400 的 JSON 响应体统一补 §4.4 信封字段
      ——traceId（反查本次调用）＋request_log_url（审计深链）；机读码在错误码中心
      （app/errors.py）登记时另补 param（改哪个参数）＋docs_url（码表锚点）。
      成功响应不注入（体不膨胀）。
    """
    trace_id = resolve_trace_id(request.headers, new_trace_id)
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers[TRACE_HEADER] = trace_id
    if response.status_code >= 400:
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("application/json") and getattr(response, "body_iterator", None) is not None:
            body = b""
            async for chunk in response.body_iterator:   # 错误体都很小，缓冲无压力
                body += chunk
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                # P1-2 §4.4 envelope 收口（单一收口点）：所有 ≥400 JSON 响应统一补
                # traceId＋request_log_url（审计反查深链）；错误码在机读码中心登记时
                # 另补 param（错哪个参数）与 docs_url（码表锚点）。字段只增不改名，
                # 既有 error/message 原样保留（向后兼容）。
                data.setdefault(errors.FIELD_TRACE_ID, trace_id)
                data.setdefault(errors.FIELD_REQUEST_LOG_URL,
                                errors.request_log_url(trace_id))
                code = data.get(errors.FIELD_ERROR)
                if isinstance(code, str) and errors.is_known(code):
                    data.setdefault(errors.FIELD_PARAM, errors.param_of(code))
                    data.setdefault(errors.FIELD_DOCS_URL, errors.docs_url(code))
                # 空值字段不落体（无追踪号不造死链；无单一参数不带 param）
                data = {k: v for k, v in data.items() if v not in ("", None)}
                injected = JSONResponse(data, status_code=response.status_code)
                for name, value in response.headers.items():
                    # 原响应头随行（端点自挂业务头不丢——M15-7 s5 真栈回归发现
                    # 429 的 X-RateLimit-Reason/Retry-After 在此被重建丢弃）；
                    # 仅 content-length 除外（体已注入 traceId，长度重算）。
                    if name.lower() != "content-length":
                        injected.headers[name] = value
                injected.headers[TRACE_HEADER] = trace_id
                return injected
    return response


async def _audit_call(caller: str, endpoint: str, params: str, status: int,
                     latency_ms: int, body, trace_id: str = "") -> None:
    """审计挂点：记一条审计八元组＋累计一次真实调用统计（写失败均不阻断业务返回）。

    stats.record_call 恰好覆盖全部业务端点（本函数仅业务路径调用；healthz 与
    /v1/portal/* 不经此），成败按状态码分计——KPI 与审计八元组同口径同源。
    trace_id 由 trace_middleware 注入 request.state（P0-5 第八列；调用点透传）。
    """
    try:
        audit.append(make_entry(caller=caller, endpoint=endpoint, params=params,
                                status=status, latency_ms=latency_ms,
                                response_body=json.dumps(body, ensure_ascii=False),
                                trace_id=trace_id))
    except Exception:  # noqa: BLE001
        logger.exception("审计写入失败（业务继续返回）")
    await live_stats.record_call(redis_client, status)


# B 型代理透传路由（2026-10-15 自转发闭环）：/proxy/* → proxy_hub（upstreams.yaml
# 已发布条目；鉴权复用 _caller_grant 同闸、审计复用 _audit_call 八元组——透传也留痕）。
# 挂载点在 _caller_grant/_audit_call 定义之后（两依赖注入需已就位；grant 用 lambda
# 再延迟一层，热加载换表不受定义序影响）。
app.include_router(proxy_hub.create_router(
    grant_check=lambda caller, sid: _caller_grant(caller, sid),
    audit_call=_audit_call))


async def _run_query(service_id: str, grant: dict, key: str):
    """执行一次点查并组装响应（正式/演示两通道共用；数据源按 backend.source 分发）。

    卡4：注册表 service_id 单键直取（服务修订号复合键已删，ADR-0008 §Decision.2）。
    响应头带 X-OneServe-DataSource（kv|hbase）。
    """
    started = time.monotonic()
    try:
        service = services[service_id]
    except KeyError:
        return (errors.envelope(errors.SERVICE_NOT_FOUND,
                                f"服务 {service_id} 不存在"), 404, 0, "unknown")
    source = service.get("backend", {}).get("source", "redis")   # 缺省 redis 向后兼容
    try:
        if source == "hbase":
            # 漂移隔离闸（AC-8-b）：HBase 通道与 KV 通道同读 sync:drift:*（store 注入）
            body = await hbsvc.execute(hbase_client, service, grant, key,
                                       store=RedisStore(redis_client))
        else:
            body = await kvx.execute(RedisStore(redis_client), service, grant, key)
        status = 200
    except (kvx.TagQueryError, hbx.HBaseQueryError) as exc:
        # §4.4 信封：error/message 既有字段保留，param/traceId/docs_url 由本层与
        # trace_middleware 统一补齐（端点层不逐处硬编码参数名）。
        body = errors.envelope(exc.code, exc.message)
        status = exc.status
    except Exception:  # noqa: BLE001 —— redis/hbase 故障等：降级 503，不读脏
        logger.exception("%s 未预期异常", type(source).__name__)
        body = errors.envelope(errors.INTERNAL_ERROR, "服务内部错误")
        status = 503
    return body, status, int((time.monotonic() - started) * 1000), source


def _apply_registry_state(new_registry, new_mtime) -> None:
    """热加载换表（M2-3.5，watcher 回调）：模块全局＋app.state 同帧指新注册表。

    main.services 供演示/鉴权链（_run_query/_caller_grant），app.state.services
    供 portal/admin/batch 链（经 _ctx 取）——两处引用同帧换新，无半新半旧窗口。
    新服务授权随行生效：同一轮热加载里刷新授权矩阵＋签发凭证（复用 P0-4 的
    reload_auth_state——新增服务＝services.yaml＋授权登记两份配置，一次到位）；
    刷新失败不回滚注册表（可 POST /v1/admin/reload 补）。
    """
    global services, registry_mtime
    services = new_registry
    registry_mtime = new_mtime
    if app is not None:
        app.state.services = new_registry
        try:
            reload_auth_state(app)
        except Exception:  # noqa: BLE001 —— 授权刷新失败不砸看门（旧授权保留，可手动补）
            logger.exception("热加载：授权状态随行刷新失败（保留旧授权）")
    # 卡1：连接池与注册表解耦——热加载只换注册表与授权，不触碰连接池（服务上下线不影响池）


@app.post("/v1/query/{service_id}")
async def query_any(request: Request, service_id: str) -> JSONResponse:
    """通用查询端点（M2-3.5 判据通道）：service_id 取自路由路径，注册表分发执行器。

    新增服务＝只改 services.yaml（免代码）：网关侧渲染器为 A 型点查服务生成
    /v1/query/{service_id} 路由（registry_render，幂等 PUT），本端点按注册表
    backend.source 分发 redis/hbase 执行器；请求/响应/鉴权/审计与专用端点
    完全同链路（复用 _kv_query_endpoint 公共层）。既有专用端点保留兼容。
    """
    if services is None or service_id not in services:
        # 未注册服务如实 404（通配路由下路径即寻址；不泄露注册表内容）
        body = errors.envelope(errors.SERVICE_NOT_FOUND, f"服务 {service_id} 未注册")
        return JSONResponse(body, 404)
    return await _kv_query_endpoint(request, service_id, f"service:/v1/query/{service_id}")


# ---------- /healthz 组件探活（P2a：纯逻辑在 app/healthz.py，本层只做 HTTP 编排） ----------
# 分工红线：/v1/portal/status（portal_api＋monitor.components_view）是门户大盘组件卡，
# 本端点供运维/容器编排——两边独立实现互不引用，探活判据一致（redis PING／网关 TCP）；
# 评级差异只有一条：healthz 里 redis 挂＝error＋503（编排判据），大盘里照常出卡片。


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """健康检查（P2a 深化）：组件探活；既有 status/redis 字段与 503 语义保留（向后兼容）。

    components 细分（新增；既有字段不动）：
      redis   PING 探活——error ⇒ 整体 error＋503（既有语义：redis 挂＝致命，
              compose service healthcheck 依赖；此分支 body 的 status 由旧值 degraded
              改报 error，redis:down 字段不变，HTTP 码不变）
      hbase   env/yaml 配置了 HBASE_HOST 才轻探（Thrift TCP）；不可达＝degraded 不 fatal；
              未配置＝off（不探不评）
      gateway APISIX 数据面 TCP（APISIX_PROBE_HOST/PORT 可配，与 portal/status 网关卡
              同判据）；不可达＝degraded 不 fatal
      etcd    etcd 客户端口 TCP（ETCD_PROBE_HOST/PORT 可配）；不可达＝degraded 不 fatal
    整体 status＝ok | degraded（部分组件不可达）| error（仅 redis 挂）；
    HTTP 503 只随 error（编排判据不变），degraded 仍 200。
    """
    redis_res, hbase_res, gw_res, etcd_res = await asyncio.gather(
        hlz.probe_redis(redis_client),
        hlz.probe_hbase(),
        hlz.probe_tcp(hlz.GATEWAY_PROBE_HOST, hlz.GATEWAY_PROBE_PORT, what="网关数据面"),
        hlz.probe_tcp(hlz.ETCD_PROBE_HOST, hlz.ETCD_PROBE_PORT, what="网关配置存储 etcd"),
    )
    components = {"redis": redis_res, "hbase": hbase_res, "gateway": gw_res, "etcd": etcd_res}
    status, code = hlz.overall(components)
    if code == 503:
        logger.warning("healthz: redis 不可用：%s", redis_res.get("detail"))
    return JSONResponse(
        {"status": status,
         "redis": "up" if redis_res["status"] == "ok" else "down",   # 既有字段保留
         "components": components},
        status_code=code)


@app.get("/v1/ping")
async def ping(request: Request) -> JSONResponse:
    """端到端验证路由（经网关 key-auth 保护）。

    链路：合作方→APISIX 验签（request-id 透传 X-Trace-Id）→本服务→读 redis ping 值
    →返回＋记审计八元组（trace_id 为第八列，P0-5）。
    """
    started = time.monotonic()
    # 网关认证后透传的 apikey 头（审计八元组的 caller 字段；内网直连时为 unknown）
    caller = request.headers.get("apikey", "unknown")
    try:
        value = await redis_client.get(PING_KEY)
        body = {"ping": value or "", "source": "redis"}
        status = 200
    except Exception as exc:  # noqa: BLE001 —— redis 故障降级为 503，仍记审计
        logger.warning("ping: redis 读失败：%s", exc)
        body = {"ping": None, "error": errors.REDIS_UNAVAILABLE}
        status = 503

    latency_ms = int((time.monotonic() - started) * 1000)
    await _audit_call(caller, "service:/v1/ping",
                str(sorted(request.query_params.multi_items())), status, latency_ms, body,
                trace_id=request.state.trace_id)
    return JSONResponse(body, status_code=status)


async def _kv_query_endpoint(request: Request, service_id: str, audit_endpoint: str) -> JSONResponse:
    """A 型 KV 点查端点公共路径（/v1/tags/query 专用通道与 /v1/query/{sid} 通配通道共用）。

    请求：POST {"key": "<键>"}。卡4（NC-3=a）：请求体携带 "version" 一律 400
    （服务修订号已删；数据版本＝表级 tabver，经装载/切版管理，不在查询请求）。
    授权：网关认证后透传的 apikey 头 → 授权矩阵/签发凭证定位租户与字段白名单
    （未登记 403）；guard.max_items_per_call 防批量拖库。
    """
    caller = request.headers.get("apikey", "")
    trace_id = request.state.trace_id   # trace_middleware 注入（P0-5 审计第八列）
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 —— 非法 JSON
        payload = None
    if not isinstance(payload, dict):
        body = errors.envelope(errors.INVALID_JSON, "请求体须为 JSON 对象")
        await _audit_call(caller or "unknown", audit_endpoint, "", 400, 0, body,
                          trace_id=trace_id)
        return JSONResponse(body, 400)

    # 卡4 NC-3=a：服务修订号机制已删——请求体携带 "version" 一律 400 明确拒绝
    # （不静默忽略，老客户端看得见；数据版本＝tabver，由装载与「设为线上」管理）。
    if "version" in payload:
        body = errors.envelope(
            errors.INVALID_VERSION,
            "查询请求不再接受 version（服务修订号机制已移除）；"
            "数据版本为表级 YYYYMMDD（tabver），由装载与「设为线上」管理")
        await _audit_call(caller or "unknown", audit_endpoint, "", 400, 0, body,
                          trace_id=trace_id)
        return JSONResponse(body, 400)

    # 注册表防御（2026-10-09 /demo/query 与 /v1/tags/query 500 根因：端点引用的
    # 服务已下线时 KeyError 裸 500——信封化 404，调用方看得见原因，审计留痕）
    svc = services.get(service_id)
    if svc is None:
        body = errors.envelope(errors.SERVICE_NOT_FOUND,
                               f"服务 {service_id} 未在注册表登记（可能已下线，现役清单见服务目录）")
        await _audit_call(caller or "unknown", audit_endpoint, "", 404, 0, body,
                          trace_id=trace_id)
        return JSONResponse(body, 404)

    # guard.max_items_per_call＝1：单键点查；传键列表且超过上限即 400（防批量拖库）
    # （guard 随服务条目；卡4 单键注册表无版本条目分叉）
    max_items = svc.get("guard", {}).get("max_items_per_call", 1)
    key = payload.get("key")
    if isinstance(key, list):
        if len(key) > max_items:
            body = errors.envelope(errors.MAX_ITEMS_EXCEEDED,
                                   f"单次调用最多 {max_items} 键（批量走数仓通道）")
            await _audit_call(caller or "unknown", audit_endpoint,
                        json.dumps({"key": key}, ensure_ascii=False), 400, 0, body,
                        trace_id=trace_id)
            return JSONResponse(body, 400)
        key = key[0] if key else None

    try:
        grant = _caller_grant(caller, service_id)   # P0-4：签发凭证先查注册表，回落矩阵
    except kvx.TagQueryError as exc:
        await _audit_call(caller or "unknown", audit_endpoint,
                    json.dumps({"key": str(key)}, ensure_ascii=False), exc.status, 0,
                    {"error": exc.code}, trace_id=trace_id)
        return JSONResponse(errors.envelope(exc.code, exc.message), exc.status)

    body, status, latency_ms, datasource = await _run_query(service_id, grant, key)
    await _audit_call(caller or "unknown", audit_endpoint,
                json.dumps({"key": str(key)}, ensure_ascii=False), status, latency_ms, body,
                trace_id=trace_id)
    response = JSONResponse(body, status_code=status)
    response.headers["X-OneServe-DataSource"] = datasource           # 数据源可观测（M2-3）
    return response


@app.post("/v1/tags/query")
async def tags_query(request: Request) -> JSONResponse:
    """正式通道：A 型 KV 标签点查（SPEC §3.2；网关 /v1/tags/query key-auth 保护后转发到此）。

    请求：POST {"key": "<上游供数键>"}（键由上游 ETL 生成，平台全程无明文）；
    响应：{service_id, key, tenant, found, fields, data_as_of, live_version}。
    """
    # 2026-10-09：user_tag_query 已随 09-29 全服务下线删除，改指现役主标签服务
    return await _kv_query_endpoint(request, "v1-svc-usr-asset-tag", "service:/v1/tags/query")


# 2026-10-14 P0 死端点清理：/v1/news/query（资讯域已下线）、/v1/hbase/query
# （HBase 不作独立数据源发 API——2026-09-23 维护者 裁定，ADR/看板有据；原实现还带
# 三参函数传第四参的 TypeError 裸 500）等死服务层路由删除。到达服务层统一
# 404 service_not_found 信封；proxy_executor 执行器模块保留未动；
# /v1/query/{sid} 通配通道不受影响。


@app.get("/demo")
async def demo_page() -> FileResponse:
    """演示页（经网关 /demo 路由到达；无 key-auth——页面已标注为演示通道）。"""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.post("/demo/query")
async def demo_query(request: Request) -> JSONResponse:
    """演示通道：手机号→服务端生成查询键→与正式通道完全相同的执行/投影/脱敏路径。

    注意（页面亦有标注）：真实接入不走本端点——调用方传上游生成的查询键
    调 POST /v1/tags/query（key-auth 保护）；本端点仅为 M0 演示页便利而设。
    """
    trace_id = request.state.trace_id   # trace_middleware 注入（P0-5 审计第八列）
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = None
    if not isinstance(payload, dict):
        body = errors.envelope(errors.INVALID_JSON, "请求体须为 JSON 对象")
        await _audit_call("demo-page", "service:/demo/query", "", 400, 0, body,
                          trace_id=trace_id)
        return JSONResponse(body, 400)

    phone = str(payload.get("phone", "")).strip()
    raw_key = payload.get("key")
    if raw_key is not None:
        # SPEC v1.2 演示通道口径 B：门户试用台前端本地生成查询键后携 {"key": <键>}（不上传明文）；
        # 与口径 A（{"phone"} 服务端生成，旧演示页）并存，响应/审计链路完全一致。
        # 键模式沿用服务注册声明（平台不关注键的加密算法），不再硬校验 SM3 形态。
        pattern = services["v1-svc-usr-asset-tag"]["params"][0]["pattern"]
        try:
            key = kvx.normalize_key(str(raw_key), pattern)
        except kvx.TagQueryError:
            body = errors.envelope(errors.INVALID_KEY, f"key 须匹配 {pattern}（小写）")
            await _audit_call("demo-page", "service:/demo/query",
                        json.dumps({"key": str(raw_key)[:16] + "…"}), 400, 0, body,
                        trace_id=trace_id)
            return JSONResponse(body, 400)
        audit_params = json.dumps({"key": key})
    elif PHONE_RE.fullmatch(phone):
        key = kvx.sm3_hex(phone)                      # 演示通道专用：手机号→查询键
        audit_params = json.dumps({"phone": phone})
    else:
        body = errors.envelope(errors.INVALID_PHONE, "手机号须为 11 位大陆号段（1[3-9] 开头）")
        await _audit_call("demo-page", "service:/demo/query",
                    json.dumps({"phone": phone}), 400, 0, body, trace_id=trace_id)
        return JSONResponse(body, 400)
    try:
        grant = kvx.tenant_grant(auth_matrix, "v1-svc-usr-asset-tag", tenant="demo_merchant")
    except kvx.TagQueryError as exc:
        body = errors.envelope(exc.code, exc.message)
        await _audit_call("demo-page", "service:/demo/query", audit_params,
                    exc.status, 0, body, trace_id=trace_id)
        return JSONResponse(body, status_code=exc.status)
    body, status, latency_ms, _ds = await _run_query("v1-svc-usr-asset-tag", grant, key)   # demo 恒走 redis 通道
    note = ("演示通道：页面本地生成查询键（SPEC v1.2）；真实接入传上游生成的键调 /v1/tags/query"
            if raw_key is not None else
            "演示通道：服务端生成查询键；真实接入传上游生成的键调 /v1/tags/query")
    body = {"channel": "demo", "note": note, **body}
    await _audit_call("demo-page", "service:/demo/query",
                audit_params, status, latency_ms, body, trace_id=trace_id)  # params 仅落摘要
    return JSONResponse(body, status_code=status)
