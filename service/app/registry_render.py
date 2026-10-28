# -*- coding: utf-8 -*-
"""路由/执行器配置渲染器（TASK-M2-3.5）：services.yaml → 网关路由条目。

范围界定（门槛稿 TASK-M2-3.5 行＋AC-6 判据「只改 yaml、零代码改动，60s 内新服务 curl 可调」）：
  - 纯逻辑：yaml 注册表 → APISIX Admin API 路由条目 dict 的映射，模块不 import
    fastapi/redis/urllib——热加载（app.registry_hot）与 init_routes（容器内脚本）
    两处调用方共用同一渲染函数＝单一事实来源，不复制第二份路由形状；
  - 输入只认注册表契约：backend.source = redis | hbase（缺省 redis，AC-1）。
    A 型点查（kv_lookup 型，含 hbase 数据源）：条目带合规 routePath → 渲染对外路由
    /v1/svc/<slug>（routePath 原样，Q1 收敛口径），proxy-rewrite 转内部通道；
    无 routePath 的存量条目 → 仍渲染 /v1/query/{service_id} 通配路由（兼容零破坏，
    service 层按 service_id 分发执行器，消 main.py/init_routes 双端硬编码）；
    external_proxy 等非 KV 型按既有专用通道走，不进通配路由（增量新增）；
  - 幂等：渲染是注册表的纯函数——同输入恒同输出（同路由 id/uri/plugins），
    init_routes 与热加载可任意次重跑，Admin API 幂等 PUT 收敛无漂移。

声明式渲染器内核契约（TASK-M2-7-a，2026-09-23）：
  - **git 恒为唯一权威源**：service/services.yaml（运行时注册区）是路由唯一的声明式
    真源；本模块只读调用方传入的注册表 dict，自身不读盘/不取时间/不取随机——渲染
    产物完全由 git 源内容确定（同源恒同产物，重跑零差异）。判据机检见
    tests/test_registry_render_core.py；
  - **单一渲染入口**：render_route_specs() 是全部网关路由产物的唯一出口（通配点查
    ＋兼容期专用），三个调用方（configs/apisix/init_routes.py 下发、app.registry_hot
    热加载、scripts/gen_openapi.py 契约生成）都只经此入口取产物，不再各自拼装
    render_query_routes/legacy_route_specs；gen_openapi 对静态非渲染通道也不再从
    源码文本正则解析，而是 import init_routes.static_resources()（消第二渲染路径）。
  - **多列族扩展位**（维护者 2026-09-23 口径）：HBase 同步表未来可能是「rowkey＝key，
    其余大量列族＝字段」，故 backend.hbase.cf 契约预留「字符串或字符串列表」两种
    形态；本阶段只按单列族实现（cf 取首个，见 hbase_column_families），多列族语义
    （按列族分组投影）归 M2-4 之后，不在本卡实现。
"""
from __future__ import annotations

from app import route_naming as rn    # R8 渲染层兜底机检（单一来源，不写第二份正则）

# 与 init_routes.REQUEST_ID 同形（P0-5：traceId 贯通；X-Trace-Id 响应回传）
REQUEST_ID = {"header_name": "X-Trace-Id"}

# ── P1-2 §4.4：网关自产错误体统一（key-auth 401 等） ──
# 实测口径（2026-09-23，APISIX 3.18 真栈探针，见 tests/evidence/）：
#   · response-rewrite 顶层静态 body 不解析 $var（实测输出字面量）；
#   · response-rewrite filters.replace 在 body_filter 阶段取不到 key-auth 短路体（实测空体）；
#   · fault-injection 会把所有请求一律短路为配置状态码（不能只作用于已是错误响应）；
#   · serverless-post-function（phase=body_filter）可用且能解析动态变量（实测通过）——故选它。
# 码名/字段名与 request_log_url 落点同 service/app/errors.py 单一真源（此处仅网关侧渲染胶水，
# 不发明第二套码名；tests/test_error_envelope.py 机检两处一致）。
GATEWAY_ERROR_ENVELOPE = {
    "phase": "body_filter",
    "functions": ["""return function(conf, ctx)
    if ngx.status < 400 then return end
    -- 已是 §4.4 信封（含 traceId）则幂等跳过，不重复改写
    if ngx.arg[1] and string.find(ngx.arg[1], '"traceId"', 1, true) then return end
    local st = ngx.status
    local tid = ngx.var.http_x_trace_id or ""
    local code = (st == 401) and "auth_failed" or "internal_error"
    local msg = (st == 401) and "未携带或无效的 apikey 请求头（网关 key-auth 拒绝）"
                        or "网关请求处理失败"
    ngx.arg[1] = string.format(
        '{"error":"%s","message":"%s","param":"apikey","traceId":"%s",' ..
        '"request_log_url":"my.html?trace_id=%s","docs_url":"errors.html#%s"}',
        code, msg, tid, tid, code)
    ngx.arg[2] = true
    ngx.header["Content-Type"] = "application/json; charset=utf-8"
end"""],
}


# response-rewrite：仅把「≥400 响应」的 Content-Type 校正为 JSON（key-auth 默认
# 是 text/plain，体已被上面的钩子换成 JSON）。vars 门控（status>=400）确保成功响应
# 零改动；只 set 头、不 set body（顶层静态 body 不解析 $var，实测）。
GATEWAY_ERROR_CONTENT_TYPE = {
    "vars": [["status", ">=", 400]],
    "headers": {"set": {"Content-Type": "application/json; charset=utf-8"}},
}


def with_gateway_error_envelope(plugins: dict) -> dict:
    """给路由插件表补网关错误信封钩子（幂等；不改既有插件语义）。

    - serverless-post-function(body_filter)：把网关自产错误体改写成 §4.4 信封；
    - response-rewrite(vars: status>=400)：仅校正错误响应的 Content-Type 为 JSON。
    """
    plugins["serverless-post-function"] = GATEWAY_ERROR_ENVELOPE
    plugins["response-rewrite"] = GATEWAY_ERROR_CONTENT_TYPE
    return plugins

# 通配路由前缀（service 层 main.py /v1/query/{service_id} 与此对齐；改动即断链）。
# Q1 收敛后的口径（提案 v1 §2）：/v1/query 是**网关→service 内部转发通道**——对外
# 出口＝注册条目 routePath 原样渲染（见 svc_route），经 proxy-rewrite 转入本通道；
# 无 routePath 的存量条目仍按本前缀渲染（兼容，零破坏）。对外展示/文档一律 /v1/svc。
QUERY_ROUTE_PREFIX = "/v1/query"
# 既有专用端点兼容期路由（判据用新端点验证；旧通道保留到调用方迁移完）
LEGACY_ROUTE_URIS = ("/v1/tags/query", "/v1/news/query", "/v1/hbase/query")


def query_route_id(service_id: str) -> str:
    """通配路由的对象 id：oneserve-query-<service_id>（幂等 PUT 的稳定键）。"""
    return f"oneserve-query-{service_id}"


def svc_route_id(service_id: str) -> str:
    """对外路由（/v1/svc/<slug>）的对象 id：oneserve-svc-<service_id>（幂等 PUT 稳定键）。"""
    return f"oneserve-svc-{service_id}"


def svc_route(service_id: str, route_path: str, request_id: dict | None = None,
              stats_hook: dict | None = None) -> dict:
    """渲染单条对外路由：uri＝注册条目 routePath **原样**（登记什么就是什么，Q1 收敛）。

    /v1/query/<service_id> 降为内部转发通道（提案 v1 §2）：本路由经 proxy-rewrite
    regex_uri 把对外地址改写为内部通道再转 upstream（service 层 main.py 既有 handler
    零改动）——形状与 scripts/publish_load_e2e.sh 的网关助手同款（真 APISIX 3.18
    实证过的写法）。
    """
    plugins: dict = {"key-auth": {}}
    plugins["request-id"] = dict(request_id or REQUEST_ID)
    if stats_hook is not None:
        plugins["serverless-pre-function"] = stats_hook
    plugins["proxy-rewrite"] = {
        "regex_uri": [f"^{route_path}$", f"{QUERY_ROUTE_PREFIX}/{service_id}"],
    }
    with_gateway_error_envelope(plugins)   # P1-2 §4.4：网关 401 统一信封
    return {
        "name": svc_route_id(service_id),
        "uri": route_path,
        "upstream_id": "oneserve-service",
        "methods": ["POST"],
        "plugins": plugins,
    }


def query_route(service_id: str, request_id: dict | None = None,
                stats_hook: dict | None = None) -> dict:
    """渲染单条 A 型点查通配路由（key-auth＋request-id＋网关统计钩子）。"""
    plugins: dict = {"key-auth": {}}
    plugins["request-id"] = dict(request_id or REQUEST_ID)
    if stats_hook is not None:
        plugins["serverless-pre-function"] = stats_hook
    with_gateway_error_envelope(plugins)   # P1-2 §4.4：网关 401 统一信封
    return {
        "name": query_route_id(service_id),
        "uri": f"{QUERY_ROUTE_PREFIX}/{service_id}",
        "upstream_id": "oneserve-service",
        "methods": ["POST"],
        "plugins": plugins,
    }


# ---------- HBase 列族契约（TASK-M2-7-a 多列族扩展位；本阶段单列族实现） ----------

def hbase_column_families(backend: dict) -> list[str]:
    """backend.hbase.cf 形态归一（多列族扩展位）：字符串或字符串列表均可。

    维护者 2026-09-23 口径：HBase 同步表未来可能是「rowkey＝key，其余大量列族＝字段」。
    故配置契约允许 cf 为 str（单列族）或 str 列表（多列族扩展位）；本函数只做形态
    归一，不发明列族、不做语义：缺省/空/类型不符返回空表，由调用方显式拒（fail-closed）。
    M2-4+ 的「数据源发现按列族分组」接此扩展位；本阶段所有服务仍走单列族。
    """
    cf = (backend or {}).get("hbase", {}).get("cf")
    if isinstance(cf, str):
        return [cf] if cf else []
    if isinstance(cf, (list, tuple)):
        return [str(x) for x in cf if x]
    return []


def primary_column_family(backend: dict) -> str:
    """本阶段单列族语义取值：取归一后首个列族（列表多于一个＝多列族未实现）。

    只做扩展位取值；多列族「按列族分组投影」语义不在本卡实现（hbase_service
    .validate_backend 对 >1 列族显式拒，防「有配置不生效」的静默降级）。
    """
    fams = hbase_column_families(backend)
    return fams[0] if fams else ""


def render_query_routes(services: dict, *, request_id: dict | None = None,
                        stats_hook: dict | None = None) -> list[tuple[str, dict]]:
    """注册表 → [(Admin API path, route body)]，按 service_id 稳定排序（幂等输出）。

    services：kv_executor.load_services() 的 service_id 单键注册表（卡4；str 键
    ＝latest 指针）——只数 str 键防复合键重复渲染；A 型点查各渲染一条路由，
    判定＝backend.kv 或 backend.hbase
    在场（source 缺省 redis 不能作判据——B 型代理 backend.proxy 无 source 会被
    误判；external_proxy 等非 KV 型不进通配路由，既有专用通道承接）。

    对外出口（Q1 收敛，消灭双口径）：条目带合规 routePath → 渲染 /v1/svc 对外路由
    （uri＝routePath 原样，proxy-rewrite 转内部通道 /v1/query/<service_id>）；
    R8 兜底：routePath 过不了命名机检（注册表被手编绕过发布端点）→ fail-closed
    跳过不渲染（宁可少路由不可错路由）；无 routePath 的存量条目 → 仍按
    /v1/query/<service_id> 渲染（存量逻辑零破坏，兼容内部通道）。
    """
    out: list[tuple[str, dict]] = []
    for key in sorted(k for k in services if isinstance(k, str)):
        entry = services[key]
        backend = entry.get("backend") or {}
        source = backend.get("source", "redis")
        if "kv" not in backend and "hbase" not in backend:
            continue   # 非点查型（如 external_proxy 的 backend.proxy）不进通配路由
        if source not in ("redis", "hbase"):
            continue   # 显式未知数据源不渲染（fail-closed：宁可少路由不可错路由）
        route_path = str(entry.get("route") or "").strip()
        if route_path.startswith(rn.ROUTE_PREFIX):
            if rn.check_route(route_path) is not None:
                continue   # R8：不合规 routePath 拒渲染（发布端点被绕过时的兜底）
            route = svc_route(key, route_path, request_id=request_id, stats_hook=stats_hook)
        else:
            route = query_route(key, request_id=request_id, stats_hook=stats_hook)
        out.append((f"routes/{route['name']}", route))
    return out


def legacy_route_specs(request_id: dict | None = None,
                       stats_hook: dict | None = None) -> list[tuple[str, dict]]:
    """兼容期专用端点路由（与 init_routes 既有条目同形同 id，幂等不漂移）。

    消的是「每加一个服务改一处硬编码」；既有三个专用端点的路由 id/形状保持
    既有值（tests/test_batch_audit.py 对 /v1/batch 路由形状有源码断言，同思路
    以同 id 幂等覆盖），调用方迁移到通配路由后整段删除。
    """
    rid = dict(request_id or REQUEST_ID)

    def _route(uri: str, methods: list[str], name: str) -> tuple[str, dict]:
        plugins: dict = {"key-auth": {}, "request-id": rid}
        if stats_hook is not None:
            plugins["serverless-pre-function"] = stats_hook
        with_gateway_error_envelope(plugins)   # P1-2 §4.4：网关 401 统一信封
        return (f"routes/{name}", {"name": name, "uri": uri,
                                   "upstream_id": "oneserve-service",
                                   "methods": methods, "plugins": plugins})

    return [
        _route("/v1/tags/query", ["POST"], "oneserve-v1-tags-query"),
        _route("/v1/news/query", ["POST"], "oneserve-v1-news-query"),
        _route("/v1/hbase/query", ["POST"], "oneserve-v1-hbase-query"),
    ]


def render_route_specs(services: dict, *, request_id: dict | None = None,
                       stats_hook: dict | None = None) -> list[tuple[str, dict]]:
    """【单一渲染入口】注册表 → 全部网关路由产物（通配点查＋兼容期专用）。

    TASK-M2-7-a 内核契约：git 源（services.yaml）经此唯一入口确定全部渲染产物；
    下发（init_routes）、热加载（registry_hot）、契约生成（gen_openapi）三处调用方
    一律只经本函数取产物，杜绝双份路由形状/第二条渲染路径。输出与
    render_query_routes()+legacy_route_specs() 拼接逐字节等价（同源同序），
    幂等：同输入恒同输出。
    """
    return (render_query_routes(services, request_id=request_id, stats_hook=stats_hook)
            + legacy_route_specs(request_id=request_id, stats_hook=stats_hook))