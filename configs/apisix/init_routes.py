#!/usr/bin/env python3
"""APISIX 初始化（task-002）：consumer＋key-auth 凭证＋upstream＋路由，全部走 Admin API（幂等 PUT）。

为什么是 python：APISIX 官方镜像内没有 curl/wget；本脚本借 service 容器（python 镜像）
经 compose 内网调 Admin API——因此 9180 可保持「仅内部」不发布宿主机端口。
用法：docker compose exec -T service python /init/init_routes.py

M2-3.5 起路由由 services.yaml 渲染（configs/apisix 与 service/app 两包路径注入
app.registry_render 共用同一渲染函数＝单一事实来源）：A 型点查服务逐条渲染
/v1/query/{service_id} 通配路由（新增服务零代码改动，判据 AC-6），既有专用端点
路由保留兼容期；/v1/batch/*、/demo*、/v1/proxy/* 等非点查通道仍静态声明。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# 渲染器在 service/app 包内（本脚本由容器 /init 挂载运行）：把「app 包所在目录」
# 注入 sys.path 后 import app.registry_render——与 service 层热加载共用同一渲染
# 函数（不复制形状）。容器内包在 /app（镜像 WORKDIR），仓库现场在 <repo>/service。
_REPO = Path(__file__).resolve().parent.parent.parent          # 仓库根（容器内＝/）
_PKG_ROOT = next((p for p in (_REPO / "service", Path("/app"))
                  if (p / "app" / "__init__.py").is_file()), None)
if _PKG_ROOT is None:
    raise SystemExit("app 包未找到（候选：<repo>/service、/app）")
sys.path.insert(0, str(_PKG_ROOT))

from app import appkeys as appk          # noqa: E402
from app import kv_executor as kvx       # noqa: E402
from app import ratelimit as rl          # noqa: E402
from app import registry_render as rend  # noqa: E402

ADMIN = os.environ.get("APISIX_ADMIN", "http://oneserve-apisix:9180")
ADMIN_KEY = os.environ.get("APISIX_ADMIN_KEY", "")   # 真值来自 .env（与 config.yaml 的 ${{APISIX_ADMIN_KEY}} 同源）；空缺省＝main() 快速失败（密钥出库批 2026-10-20）
APPKEY = os.environ.get("APISIX_APPKEY", "")         # 调用方凭证，来自 .env

# ---------------------------------------------------------------------------
# 真实调用统计（诚信线 2026-09-22）：网关即拒的请求也要计入门户 KPI。
#
# service 层计数挂在审计链路（service/app/stats.py，经 main._audit_call），
# 但 key-auth 401 等在网关就被拒掉的请求到不了 service——漏计＝数字不真。
# 这里给每条业务路由挂 log 阶段钩子（serverless-pre-function）：仅当 upstream
# 未被触达（upstream_status 为空）时补计一次，成败按响应状态码分计，键与
# service 层完全同组（portal:stats:live + 日滚动键；日期按东八区，同 stats.py）。
# 已转发到 service 的请求 upstream_status 非空 → 直接跳过，杜绝双计。
# 实现注记：log_by_lua 里禁用 cosocket（APISIX patch 实测），与 APISIX 自带
# http-logger 同法——ngx.timer.at(0) 在 timer 上下文里连 Redis（lua-resty-redis
# 为 APISIX 内置依赖）直写；计数异步毫秒级落键，失败只记 error.log 不影响请求。
# ---------------------------------------------------------------------------
# P0-5 traceId 贯通（SPEC §4.5）：全部业务路由启用 request-id 插件，header 名
# X-Trace-Id（任务卡口径；service 读侧同时兼容 SPEC 行文名 X-OneServe-Trace-Id）。
# include_in_response 默认开——响应头即回传 traceId；客户端已带同名头时插件复用透传
# （APISIX 3.18 request-id 实现口径），与 service 层「有则透传、缺则生成」一致。
REQUEST_ID = {"header_name": "X-Trace-Id"}

GW_STATS = {
    "phase": "log",
    "functions": ["""
return function(conf, ctx)
    if ngx.var.upstream_status then return end   -- 已到 service：service 侧审计链路已计
    local st = tonumber(ngx.var.status) or 0
    local live_field, day_field = "successCount", "success"
    if not (st >= 200 and st < 300) then
        live_field, day_field = "failCount", "fail"
    end
    local day = "portal:stats:live:day:" .. os.date("%Y%m%d", ngx.time() + 8 * 3600)
    -- P0-5：网关即拒（如 key-auth 401）的请求到不了 service，trace 断在网关——旁路把
    -- trace_id 记入 Redis 列表 portal:trace:gw-rejected（留最近 1000 条，LRANGE 查询）。
    -- id 取自响应头：request-id 插件在 header_filter 阶段已把 X-Trace-Id 补进响应头
    -- （401 拒绝同样经过 header_filter），log 阶段读响应头即得；拿不到头则记空串
    -- 跳过——如实留断点，不硬造。
    local tid = ngx.resp.get_headers()["x-trace-id"] or ""
    local uri = ngx.var.uri or ""
    local ok, err = ngx.timer.at(0, function(premature, lf, df, day_key, tid, uri, st)
        if premature then return end
        local redis = require("resty.redis")
        local r = redis:new()
        r:set_timeout(500)
        local function attempt()
            if not r:connect("oneserve-redis", 6379) then return false end
            local first = r:hincrby("portal:stats:live", "totalCalls", 1)
            if not first then r:close(); return false end  -- 池中死连接（Redis 重启后）：重试
            r:hincrby("portal:stats:live", lf, 1)
            r:hincrby(day_key, "calls", 1)
            r:hincrby(day_key, df, 1)
            r:expire(day_key, 691200)
            if tid ~= "" then
                local cjson = require("cjson")
                r:lpush("portal:trace:gw-rejected", cjson.encode({
                    ts = os.date("!%Y-%m-%dT%H:%M:%SZ", ngx.time()),
                    status = st, trace_id = tid, uri = uri }))
                r:ltrim("portal:trace:gw-rejected", 0, 999)
            end
            return true
        end
        if not attempt() and not attempt() then
            ngx.log(ngx.ERR, "gw stats: redis write failed twice")
            return
        end
        r:set_keepalive(10000, 100)
    end, live_field, day_field, day, tid, uri, st)
    if not ok then
        ngx.log(ngx.ERR, "gw stats: timer create failed: ", err)
    end
end"""]
}


def req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """调 Admin API，返回 (状态码, 响应 JSON)。"""
    url = f"{ADMIN}/apisix/admin/{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("X-API-KEY", ADMIN_KEY)
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def wait_admin_ready(timeout_s: int = 90) -> None:
    """等 Admin API 就绪（apisix 连上 etcd 后才会应答 200）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, _ = req("GET", "routes")
        if code == 200:
            return
        time.sleep(1)
    raise SystemExit(f"admin api 未就绪（{ADMIN}，{timeout_s}s 超时）")


def _load_appkeys() -> list[dict]:
    """读凭证注册表（service/registry/appkeys.yaml）：只取 appkey 在场的条目。

    缺少 appkeys 路径/文件＝空表（stack 首跑、尚未签发属正常态，不阻断初始化）。
    禁用（disabled）凭证不重建 consumer——与 admin_api 禁用即删 consumer 同口径。
    """
    path = _PKG_ROOT / "registry" / "appkeys.yaml"
    if not path.is_file():
        return []
    doc = appk.load_appkeys(path)
    # 禁用凭证不下发（admin_api 禁用即删 consumer；重建会把已禁用 key 重新放行）
    return [k for k in doc.get("keys") or []
            if k.get("appkey") and k.get("status") != "disabled"]


def _load_registry() -> dict:
    """读 services.yaml 注册表（渲染输入）：容器内权威路径 /app/services.yaml。

    注册表读不到即失败退出（up.sh 首跑语境＝栈未就绪，宁可红不可静默少路由）。
    """
    candidates = [_PKG_ROOT / "services.yaml", Path("/app/services.yaml")]
    for p in candidates:
        if p.is_file():
            return kvx.load_services(p)
    raise SystemExit(f"services.yaml 未找到（候选：{[str(p) for p in candidates]}）")


def static_resources() -> list[tuple[str, dict]]:
    """非渲染型（静态声明）网关资源：上游＋consumer＋静态通道路由。

    TASK-M2-7-a：点查路由（/v1/query/*＋兼容期专用端点）全部来自单一渲染入口
    registry_render.render_route_specs；本函数只声明点查以外的固定通道
    （/v1/ping、/demo*、/v1/proxy、/v1/batch），供下发与契约构建同源读取——
    scripts/gen_openapi.py 经本函数取静态路由，不再对源码文本正则解析（消第二
    渲染路径）。纯数据构造：不读盘、不联网。
    """
    return [
        ("upstreams/oneserve-service", {
            "name": "oneserve-service",
            "type": "roundrobin",
            "nodes": {"oneserve-service:8000": 1},
        }),
        # 调用方：key-auth 凭证（M0 用简单 AppKey；hmac 升级留 M1，样例见 POC sign-hmac.sh）
        ("consumers/oneserve-demo", {
            "username": "oneserve-demo",
            "plugins": {"key-auth": {"key": APPKEY}},
        }),
        # 路由：/v1/ping → service，key-auth 保护（无 key 401 / 带 key 200 的端到端口径）
        ("routes/oneserve-v1-ping", {
            "name": "oneserve-v1-ping",
            "uri": "/v1/ping",
            "upstream_id": "oneserve-service",
            "plugins": {"key-auth": {}, "request-id": REQUEST_ID,
                       "serverless-pre-function": GW_STATS,
                       "serverless-post-function": rend.GATEWAY_ERROR_ENVELOPE,   # P1-2 §4.4 错误信封
                       "response-rewrite": rend.GATEWAY_ERROR_CONTENT_TYPE},   # 错误响应 Content-Type 校正
        }),
        # task-003：演示页通道（/demo 页面 + /demo/query 服务端代算 SM3 端点）。
        # 不挂 key-auth＝有意为之：演示页给未持密钥的观摩者用，页面与响应均标注
        # 「演示通道：真实接入传 SM3」；正式数据出口只有上面的 /v1/tags/query。
        ("routes/oneserve-demo", {
            "name": "oneserve-demo",
            "uri": "/demo*",
            "upstream_id": "oneserve-service",
            "plugins": {"request-id": REQUEST_ID, "serverless-pre-function": GW_STATS,
                       "serverless-post-function": rend.GATEWAY_ERROR_ENVELOPE,   # P1-2 §4.4 错误信封
                       "response-rewrite": rend.GATEWAY_ERROR_CONTENT_TYPE},   # 错误响应 Content-Type 校正
        }),
        # M15-5（task-015）：批量查询任务通道（/v1/batch/*——提交/状态/取消/重试/
        # 结果交付，门槛稿 §B2「批量端点并入 main.py 既有网关 key-auth 通道」）。
        # key-auth 与点查同口径（无 key 401 / 带 key 200）；request-id 即 trace_id
        # 接线（P0-5 注记落点：网关注入 X-Trace-Id → service trace_middleware 透传
        # → 批量端点审计第八列）。配额 429/限流头由 service 侧生成（AC-10），网关只
        # 透传——NC-4 批复后再评估网关级限流插件（limit-count 等）是否前置。
        ("routes/oneserve-v1-batch", {
            "name": "oneserve-v1-batch",
            "uri": "/v1/batch/*",
            "upstream_id": "oneserve-service",
            "methods": ["GET", "POST", "DELETE"],
            "plugins": {"key-auth": {}, "request-id": REQUEST_ID,
                       "serverless-pre-function": GW_STATS,
                       "serverless-post-function": rend.GATEWAY_ERROR_ENVELOPE,   # P1-2 §4.4 错误信封
                       "response-rewrite": rend.GATEWAY_ERROR_CONTENT_TYPE},   # 错误响应 Content-Type 校正
        }),
        # （/v1/tags/query、/v1/news/query、/v1/hbase/query 三个既有专用路由与全部
        #   /v1/query/* 通配路由由渲染器生成——见上方 M2-3.5 渲染块）
    ]


def main() -> int:
    # 密钥出库批（2026-10-20）：凭证只经环境注入（.env→compose），缺省即明确失败而非拿旧默认值硬跑
    if not ADMIN_KEY or not APPKEY:
        raise SystemExit("APISIX_ADMIN_KEY / APISIX_APPKEY 未设置——请经 compose（.env）注入后重跑")
    wait_admin_ready()

    # ---------- M2-3.5／M2-7-a：路由由 services.yaml 经单一渲染入口生成 ----------
    # ① A 型点查服务逐条渲染通配路由 /v1/query/{service_id}（key-auth 同口径；
    #    新增服务＝只改 yaml，重跑本脚本/热加载幂等 PUT 即生效——判据 AC-6）；
    # ② 既有专用端点路由保留兼容期（调用方迁移到通配路由后整段删除）。
    # TASK-M2-7-a：单一渲染入口 render_route_specs（消除第二渲染路径）。
    services = _load_registry()
    rendered = rend.render_route_specs(services, stats_hook=GW_STATS)

    # 上游＋consumer＋静态通道先行——APISIX 建路由时校验 upstream_id 存在，
    # 渲染路由必须排在上游 PUT 之后（M2-3.5 e2e 实测：上游缺席时路由 400）。
    resources = static_resources() + rendered

    # ---------- P1-5：注册表凭证 → 网关 consumer（含登记配额的限流插件） ----------
    # 控制台签发的凭证由 admin_api 在签发/启用/调额时即时下发；此处补冷启动同步，
    # 保证 etcd 卷重建后「登记值＝网关拦截值」仍然成立（同一 ratelimit.consumer_body
    # 构造，不复制插件形状）。未登记配额＝只挂 key-auth（如实不限）。
    for rec in _load_appkeys():
        resources.append((f"consumers/{rec['appkey']}", rl.consumer_body(
            rec["appkey"], quota_qps=rec.get("quota_qps"), quota_daily=rec.get("quota_daily"))))

    failed = False
    for path, body in resources:
        code, _ = req("PUT", path, body)
        print(f"PUT {path:<34} -> {code}")
        # 幂等口径：新建＝201，重复执行（etcd 卷保留时覆盖同 id）＝200，两者皆成功
        failed = failed or code not in (200, 201)

    if failed:
        print("存在初始化失败项", file=sys.stderr)
        return 1
    print("APISIX 初始化完成（幂等，可重复执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
