# -*- coding: utf-8 -*-
"""B 型代理中枢（本地自转发闭环卡，2026-10-15）：upstreams.yaml 注册→运行时消费。

职责（SPEC §1.2 B 型＝转发而非 KV；上游＝本地自身 API 的测试期闭环）：
  1. 数据面：/proxy/* 透传端点——按 upstreams.yaml 已发布条目最长前缀匹配，
     转发到 upstream_url（＋子路径），方法/头/体白名单转发，响应原样回传；
  2. 控制面：已发布代理 → APISIX 网关路由幂等下发（key-auth＋request-id＋
     错误信封钩子，与 registry_render 既有形状同源）；下线/草稿 → 删路由；
  3. 热加载：upstreams.yaml mtime 轮询看门（lifespan 起，≤轮询间隔生效）；
  4. 试连：控制台三步向导第②步 probe（服务端真实 HTTP 探测，无浏览器 CORS 限制）。

设计约束：
  - 复用既有执行器（proxy_executor）的错误口径（TagQueryError 三件套＋机读码）
    与留存钩子（retention 缺省关＝零落盘）；fetch/URL 拼装因需 POST/请求体/头
    转发（proxy_executor 仅 GET 无头）在本模块扩展，不改既有签名（其测试零破坏）；
  - 依赖注入：grant_check/audit_call 经 create_router 注入（main 提供，测试可替），
    本模块不 import main/admin_api——无环；
  - 数据面每请求直读 upstreams.yaml（小文件；登记即生效，无缓存陈旧窗口），
    网关路由侧走看门收敛（≤轮询间隔），两侧同源单一权威文件。
"""
from __future__ import annotations

import asyncio
import http.client
import inspect
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Awaitable, Callable

from app import errors
from app import meta_snapshot as ms         # 读侧热载切快照（ADR-0021 收口小批）：hot_read_path 落点
from app import ops_config as opsc          # upstreams.yaml 权威源读写（O-2）
from app import proxy_executor as pxe       # B 型执行器：错误口径＋留存钩子复用
from app import registry_hot as rhot        # Admin API 幂等下发通道（复用）
from app import registry_render as rend     # 网关插件形状单一来源（request-id/信封）
from app.kv_executor import TagQueryError

# A窗 2026-10-20 修复：本模块 7 处用 logger 但从未定义——看门协程启动即 NameError
# （热加载自 10-15 合入以来从未生效，路由收敛一直靠每次重启的启动全量推兜底）。
logger = logging.getLogger(__name__)

# FastAPI 类型在模块级可见（try/except 兜底）：本文件 from __future__ import
# annotations，端点签名里的 Request/Response 注解由 FastAPI 经模块全局解析——
# 函数内懒加载会让 Request 解析失败、被误判为 query 参数（422 回归实证）。宿主
# 无 fastapi 时置 None：纯逻辑核心（匹配/渲染/试连/转发）仍可单测，工厂不被调用。
try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, Response
except ImportError:  # noqa: E402 —— 宿主纯逻辑单测环境
    APIRouter = Request = JSONResponse = Response = None

# 子路径白名单字符集（#37 2026-10-15 复审 P2-4：黑名单正则→白名单）：
# 只放行字母数字与 . _ - /（URL 编码变体在 match_proxy 之前已被 Starlette 解码，
# %2e%2e 到不了这里；即便到达，"." 允许单双成段但 ".." 上跳被显式段检查拒）。
_SUBPATH_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def subpath_ok(sub: str) -> bool:
    """代理子路径白名单判定（纯逻辑可测）：字符集＋无 ".." 段＋无双斜线。

    越界一律调用方 403（原 400 invalid_proxy_path 语义保留——见 build_target_url）。"""
    if not _SUBPATH_OK.match(sub):
        return False
    if "//" in sub:
        return False
    return not any(seg == ".." for seg in sub.split("/"))

# 转发头白名单（透传安全口径）：apikey（服务层鉴权/审计 caller 同源）、
# authorization（上游自鉴权通用）、content-type（体语义）、x-trace-id（贯通）。
# 不转发 host/cookie/自定义头——防透传面被当作头注入通道。
FORWARD_HEADERS = ("apikey", "authorization", "content-type", "x-trace-id")

# urllib 发请求时会把自设头名规范成首字母大写（apikey→Apikey）——上游按约定小写
# 键读取会拿不到值；转发时统一输出规范小写头名（HTTP/1.1 头大小写不敏感，对任何
# 规范客户端等价；同口径见 tests/test_proxy_hub.py::test_forward_passthrough）。
_CANON_HEADER_NAME = {name.lower(): name for name in FORWARD_HEADERS}


# ---------- upstreams.yaml → 运行时消费（数据面） ----------

def published_entries(path: Path | None = None) -> list[dict]:
    """读已发布代理条目（draft/offline 无线上语义；坏表如实抛 OpsConfigError）。

    读侧热载切快照（ADR-0021 收口小批）：缺省落点经 ms.hot_read_path——数据面
    每请求直读与看门同源（db 态读快照＝库镜像，快照缺文件回落仓内＋WARN；
    yaml 态原路径零改动）。
    """
    return [p for p in opsc.load_upstreams(path or ms.hot_read_path(opsc.UPSTREAMS_YAML))
            if p.get("status") == "published"]


def match_proxy(full_path: str, entries: list[dict]) -> tuple[dict | None, str]:
    """请求路径 → (命中条目, 子路径)：已发布条目 route 最长前缀匹配。

    子路径＝路径去掉注册 route 前缀后的余段（无则空串——1:1 端点形态）。
    无命中返回 (None, "")，调用方按 404 proxy_not_found 信封拒。
    """
    best: dict | None = None
    for p in entries:
        route = str(p.get("route") or "")
        if not route:
            continue
        if full_path == route or full_path.startswith(route.rstrip("/") + "/"):
            if best is None or len(route) > len(str(best.get("route") or "")):
                best = p
    if best is None:
        return None, ""
    route = str(best["route"]).rstrip("/")
    sub = full_path[len(route):].strip("/")
    return best, sub


def build_target_url(entry: dict, sub_path: str, query_string: str) -> str:
    """upstream_url ＋ 子路径 ＋ 原样查询串；子路径过 proxy_executor 同一黑名单。

    与 pxe.build_upstream_url 的差异：允许空子路径（登记条目 1:1 指向完整上游
    端点，如 http://localhost:8000/v1/query/v1-svc-usr-asset-tag）。
    """
    sub = (sub_path or "").strip("/")
    # #37（复审 P2-4 2026-10-15）：黑名单正则（pxe._BAD_REST）→ 白名单字符集——
    # 未列举即拒，编码变体/../、%2e%2e、scheme、host、空白、@、冒号一网打尽。
    if sub and not subpath_ok(sub):
        raise TagQueryError(403, errors.PROXY_TRAVERSAL,
                            "代理子路径越界（只许字母数字与 . _ - / 的相对路径段，禁上跳/host/scheme）")
    url = str(entry["upstream_url"]).rstrip("/") + (("/" + sub) if sub else "")
    return f"{url}?{query_string}" if query_string else url


def forward_headers(request: Request) -> dict[str, str]:
    """白名单头转发（值原样；缺失头不造空值；头名统一规范小写，见上注）。"""
    return {_CANON_HEADER_NAME[name.lower()]: value
            for name, value in request.headers.items()
            if name.lower() in _CANON_HEADER_NAME}


# ---------- 上游调用（GET/POST＋头＋体；传输层失败归类 502/504 同执行器口径） ----------

def _fetch_full(url: str, timeout_ms: int, *, method: str = "GET",
                headers: dict | None = None, body: bytes = b"") -> dict:
    """单次上游调用（同步；生产经 asyncio.to_thread 包裹）：{status,content_type,body}。

    上游已应答（含 4xx/5xx）＝透传；超时→504 upstream_timeout，其余传输层失败
    →502 upstream_unreachable（与 proxy_executor.fetch_upstream 同口径，加方法/头/体）。
    """
    # 透传面走 http.client 直发（非 urlopen）：urllib 对自设头名有两道加工
    # （add_header→key.capitalize()、do_open→name.title()，apikey→Apikey、
    # x-trace-id→X-Trace-Id），而 B 型口径＝白名单头「名与值都不改写」——上游
    # 按登记小写键读取，键形漂移即断链（收口批续作 2026-10-15 实测修复；
    # putheader 接受 bytes 头名绕过大小写加工，wire 实证原名原样到达）。
    try:
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme == "https"
        hostport = parts.netloc.rsplit("@", 1)[-1]
        host = hostport.split(":", 1)[0]
        portnum = parts.port or (443 if secure else 80)
        conn_cls = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        conn = conn_cls(host, portnum, timeout=max(int(timeout_ms), 1) / 1000)
        selector = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        try:
            conn.putrequest(method.upper(), selector, skip_host=False,
                            skip_accept_encoding=True)
            # putrequest/endheaders 低层通道不自动补 Content-Length（那是
            # conn.request()→_send_request 的活）——有体须显式声明，否则上游
            # 按 0 长读走空体（收口批续作实测踩坑落注）。
            if body:
                conn.putheader(b"content-length", str(len(body)))
            for name, value in (headers or {}).items():
                conn.putheader(name.lower().encode("ascii"), value)
            conn.endheaders(body or None)
            resp = conn.getresponse()
            payload = resp.read()
            status = int(resp.status)
            ctype = resp.getheader("Content-Type", "application/json")
            allow = resp.getheader("Allow", "") or ""
        finally:
            conn.close()
        # allow：405 探测三态用（RFC 9110 允许的方法清单）；转发路径读不到此键也无害。
        return {"status": status, "content_type": ctype, "body": payload, "allow": allow}
    except (OSError, http.client.HTTPException) as exc:  # 传输层失败统一归类（连接拒绝/DNS/复位/超时皆 OSError 族；坏响应=HTTPException）
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise TagQueryError(504, errors.UPSTREAM_TIMEOUT,
                                f"上游超时（>{timeout_ms}ms），按配置熔断") from exc
        raise TagQueryError(502, errors.UPSTREAM_UNREACHABLE,
                            f"上游不可达：{type(exc).__name__}") from exc


async def forward(fetch: Callable[..., Awaitable[dict]], entry: dict, url: str,
                  method: str, headers: dict, body: bytes) -> dict:
    """一次代理转发（重试口径同 pxe.execute：仅传输层 502/504 重试，共 1+retry 次）。"""
    timeout_ms = int(entry.get("timeout_sec", 5)) * 1000
    attempts = 1 + max(int(entry.get("retry_count", 0)), 0)
    last_err: TagQueryError | None = None
    for _ in range(attempts):
        try:
            return await fetch(url, timeout_ms, method=method, headers=headers, body=body)
        except TagQueryError as exc:
            if exc.status not in (502, 504):
                raise                                # 非传输层错误不重试
            last_err = exc
    raise last_err                                   # type: ignore[misc]


# ---------- 控制面：网关路由下发（key-auth＋request-id＋错误信封，同 registry_render 形状） ----------

def proxy_route_id(pid: str) -> str:
    """代理网关路由对象 id（幂等 PUT 稳定键）。"""
    return f"oneserve-px-{pid}"


def proxy_route(entry: dict) -> dict:
    """单条已发布代理 → APISIX 路由（uri＝登记 route 原样；服务层直挂 /proxy/*）。"""
    plugins: dict = {"key-auth": {}, "request-id": dict(rend.REQUEST_ID)}
    rend.with_gateway_error_envelope(plugins)
    return {"name": proxy_route_id(str(entry["id"])),
            "uri": str(entry["route"]),
            "upstream_id": "oneserve-service",
            "methods": [str(entry.get("method", "GET"))],
            "plugins": plugins}


# GET 全量路由 -> 本代理域(oneserve-px- 前缀)pid 集合(失败容错空集)。
# rhot._admin_req 只回状态码; 本函数是只读 GET 的带体变体(同 Admin API 通道/鉴权
# 口径)。网关不可达=空集: 本轮跳过孤儿收敛(PUT 侧仍告警重试, 孤儿下拍再摘)。
def _list_px_route_ids(admin: str, admin_key: str) -> set[str]:
    req = urllib.request.Request(f"{admin}/apisix/admin/routes", method="GET")
    req.add_header("X-API-KEY", admin_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 —— 容错: 孤儿收敛失败不阻断主下发
        return set()
    out: set[str] = set()
    raw = data.get("list") or []
    nodes = raw if isinstance(raw, list) else (raw.get("nodes") or [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        value = node.get("value") or node   # 两种形态：{key,value} 包装或直接路由 dict
        name = str(value.get("name") or "")
        if not name and node.get("key"):
            name = str(node["key"]).rsplit("/", 1)[-1]
        if name.startswith("oneserve-px-"):
            out.add(name[len("oneserve-px-"):])
    return out


async def push_proxy_routes(path: Path | None = None, *, admin: str = rhot.ADMIN,
                            admin_key: str = rhot.ADMIN_KEY) -> tuple[int, int]:
    # 幂等收敛(发布->PUT; draft/offline->DELETE; 整条移除->孤儿摘除); 返回(成功数, 失败数)。
    # 残留根治(验完核残留纪律): 登记表内非发布条目摘流之外, 再对 oneserve-px-* 全量
    # 做差集——条目被整段删除(而非走 offline)也不留残路由。
    entries = opsc.load_upstreams(path or ms.hot_read_path(opsc.UPSTREAMS_YAML))   # 读径与看门同源（db 态读快照）

    def _settle(code: int) -> bool:
        # DELETE 404=本无路由(幂等摘流达成); PUT 200/201=收敛
        return code in (200, 201, 404)

    ok = bad = 0
    for p in entries:
        pid = str(p.get("id") or "")
        route_path = f"routes/{proxy_route_id(pid)}"
        if p.get("status") == "published":
            code = await asyncio.to_thread(rhot._admin_req, admin, admin_key,
                                           "PUT", route_path, proxy_route(p))
        else:
            code = await asyncio.to_thread(rhot._admin_req, admin, admin_key,
                                           "DELETE", route_path, None)
        if _settle(code):
            ok += 1
        else:
            bad += 1
            logger.warning("代理网关路由下发失败：%s -> %s", route_path, code)
    live = await asyncio.to_thread(_list_px_route_ids, admin, admin_key)
    known = {str(p.get("id") or "") for p in entries}
    for pid in sorted(live - known):
        code = await asyncio.to_thread(rhot._admin_req, admin, admin_key,
                                       "DELETE", f"routes/{proxy_route_id(pid)}", None)
        if _settle(code):
            ok += 1
            logger.info("孤儿代理路由摘除：%s", pid)
        else:
            bad += 1
            logger.warning("孤儿代理路由摘除失败：%s -> %s", pid, code)
    return ok, bad


async def upstreams_watcher_loop(path: Path | None = None,
                                 interval_s: float | None = None) -> None:
    """upstreams.yaml mtime 轮询看门（与 registry_hot 同节拍；变更→幂等下发）。

    读表失败仅告警挂起（下拍重试），循环永不退出——配置事故不打断看门。
    """
    target = path or ms.hot_read_path(opsc.UPSTREAMS_YAML)   # 看门与数据面读径同源（db 态看快照）
    interval = interval_s or float(os.environ.get("REGISTRY_POLL_INTERVAL_S", rhot.POLL_INTERVAL_S))
    state = rhot._mtime(target)
    logger.info("代理配置热加载看门启动：path=%s interval=%.0fs", target, interval)
    while True:
        try:
            await asyncio.sleep(interval)
            mtime = rhot._mtime(target)
            if mtime is None or mtime == state:
                continue
            ok, bad = await push_proxy_routes(target)
            state = mtime
            if bad:
                logger.warning("代理路由部分下发失败（%d ok / %d bad）：下拍重试同文件", ok, bad)
            else:
                logger.info("代理配置热加载完成：网关路由已收敛（%d 条）", ok)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 —— 看门兜底：任何异常吞掉记日志，下拍再来
            logger.exception("代理热加载看门单拍异常（继续轮询）")


# ---------- 试连（三步向导第②步；服务端真实探测） ----------

# 探测结论（bug7 2026-10-20 三态 → 2026-10-21 四态）：
#   ok_2xx＝通过（2xx）；ok_post_confirmed＝上游在线、POST 方法已确认（AUTO 两步下
#   POST 复探被参数校验 400/422 拒绝——查询型 POST-only 上游拒绝空探测属正当反应，
#   非故障，发布闸放行）；method_mismatch＝AUTO 两步复探后仍 405 或显式方法 405；
#   其余不可达。FastAPI 4xx 不带 Allow 头，建议方法仅在真有 Allow 时给出。
PROBE_OK = "ok_2xx"
PROBE_METHOD_MISMATCH = "method_mismatch"
PROBE_UNREACHABLE = "unreachable"
PROBE_POST_CONFIRMED = "ok_post_confirmed"


def suggest_method(allow: str, requested: str = "") -> str:
    """从 Allow 头挑一个可用方法（GET/POST；优先与当前不同的那个）。

    Allow 可能是 "POST, OPTIONS"/"GET,HEAD" 多值；本平台代理只支持 GET/POST，
    其他方法忽略。找不到可建议方法＝空串（页面如实提示人工确认）。
    """
    allowed = [m.strip().upper() for m in str(allow or "").split(",") if m.strip()]
    usable = [m for m in ("GET", "POST") if m in allowed]
    if not usable:
        return ""
    req = str(requested or "").upper()
    for m in usable:
        if m != req:
            return m
    return usable[0]


async def probe(upstream_url: str, *, method: str = "AUTO",
                timeout_sec: int = 5) -> dict:
    """上游试连：真实发 HTTP 探测 → 结论＋原状态码/耗时（不造假，逐字面口径）。

    返回 {ok, verdict, status, latencyMs, error, errorKind, allow,
          suggestedMethod, methodUsed, forwardMethod, probeSeq, message}：
      - ok_2xx（ok=true）：探测 2xx＝通过，发布闸放行；
      - ok_post_confirmed（ok=true）：AUTO 两步下 GET 405、POST 复探被参数校验
        拒绝（400/422）＝上游在线且 POST 方法已确认——查询型 POST 接口拒绝空
        探测属正常，发布闸放行；status 保留真实校验码（技术详情可见）；
      - method_mismatch（ok=false）：405。AUTO（默认「跟随上游」）两步：GET 405
        →自动 POST 复探同 URL（空体）；POST 也 405 才判失配（文案「上游对
        GET/POST 均 405」）。显式指定 GET/POST 时尊重配置方法单步探测，不擅自
        换方法复探；
      - unreachable（ok=false）：连不上/超时，或其他非 2xx（status 保留真值，
        不把 404/500 伪装成「通过」）。
    suggest_method 仅在真有 Allow 头时给建议（FastAPI 4xx 不带 Allow → 无建议，
    如实「请人工确认」）。
    字段口径：methodUsed＝实际探测方法序列（单步如 "POST"，两步如 "GET→POST"）；
    forwardMethod＝探测确认的转发方法（两步＝POST；失配/不可达＝空）；probeSeq＝
    人读探测序列（如 "GET→405→POST"，进前端技术详情）。耗时含真实网络往返。
    """
    req = str(method or "AUTO").upper()
    explicit = req in ("GET", "POST")
    if not explicit:
        req = "GET"                     # AUTO/非法值：以 GET 作为跟随探测的起点
    started = time.monotonic()

    async def attempt(m: str) -> dict:
        return await asyncio.to_thread(
            _fetch_full, upstream_url, int(timeout_sec) * 1000, method=m)

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def merged(ok: bool, verdict: str, status: int, allow: str, *,
               suggested: str = "", used: str = "", fwd: str = "",
               seq: str = "", message: str = "", error: str = "",
               error_kind: str = "") -> dict:
        return {"ok": ok, "verdict": verdict, "status": status,
                "latencyMs": elapsed_ms(), "error": error or message,
                "errorKind": error_kind, "allow": allow,
                "suggestedMethod": suggested, "methodUsed": used or req,
                "forwardMethod": fwd, "probeSeq": seq or (used or req),
                "message": message or error}

    try:
        r1 = await attempt(req)
        s1 = int(r1["status"])
        a1 = str(r1.get("allow") or "")
        if 200 <= s1 < 300:
            return merged(True, PROBE_OK, s1, a1, used=req, fwd=req,
                          message=f"上游返回 HTTP {s1}，探测通过")
        if s1 == 405:
            suggested1 = suggest_method(a1, req)
            if explicit:
                # 显式方法：尊重配置，单步判定——405＋真 Allow 给建议，否则人工确认
                hint = (f"上游只接受 {suggested1}，建议改用 {suggested1}"
                        if suggested1 else
                        f"上游不接受 {req}（HTTP 405），且未返回 Allow 头，请人工确认可用方法")
                return merged(False, PROBE_METHOD_MISMATCH, s1, a1,
                              suggested=suggested1, error=hint,
                              error_kind="method_mismatch")
            # AUTO 两步：POST 复探同 URL（空体）——POST-only 查询上游在此转绿
            try:
                r2 = await attempt("POST")
            except TagQueryError as exc2:
                return merged(False, PROBE_UNREACHABLE, 0, "", used="GET→POST",
                              seq="GET→405→POST", error=exc2.message,
                              error_kind=exc2.code)
            s2 = int(r2["status"])
            a2 = str(r2.get("allow") or "")
            seq2 = "GET→405→POST"
            if 200 <= s2 < 300:
                return merged(True, PROBE_OK, s2, a2, used="GET→POST", fwd="POST",
                              seq=seq2,
                              message=f"GET 探测 405，POST 复探 HTTP {s2} 通过：将使用 POST 转发")
            if s2 in (400, 422):
                return merged(True, PROBE_POST_CONFIRMED, s2, a2,
                              used="GET→POST", fwd="POST", seq=seq2,
                              message=(f"POST 探测被参数校验拒绝（HTTP {s2}）属正常："
                                       "上游在线且 POST 方法已确认，将使用 POST 转发"))
            if s2 == 405:
                suggested2 = suggest_method(a2, "POST")
                hint = "上游对 GET/POST 均 405"
                hint += (f"（上游声明接受 {suggested2}，请人工确认）" if suggested2
                         else "，且未返回 Allow 头，请人工确认可用方法")
                return merged(False, PROBE_METHOD_MISMATCH, s2, a2,
                              suggested=suggested2, used="GET→POST", seq=seq2,
                              error=hint, error_kind="method_mismatch")
            return merged(False, PROBE_UNREACHABLE, s2, a2, used="GET→POST",
                          seq=seq2, error_kind="upstream_status",
                          message=f"POST 复探返回 HTTP {s2}，未通过（发布要求 2xx）")
        return merged(False, PROBE_UNREACHABLE, s1, a1, error_kind="upstream_status",
                      error=f"上游返回 HTTP {s1}（可达但未通过，发布要求 2xx）",
                      message=f"上游返回 HTTP {s1}，未通过（发布要求 2xx）")
    except TagQueryError as exc:
        return merged(False, PROBE_UNREACHABLE, 0, "", error=exc.message,
                      error_kind=exc.code)


# ---------- 数据面 HTTP 编排（路由工厂；依赖注入防环） ----------

GrantCheck = Callable[[str, str], dict]
AuditCall = Callable[..., Awaitable[None]]


def create_router(*, grant_check: GrantCheck, audit_call: AuditCall,
                  fetch: Callable[..., Awaitable[dict]] | None = None,
                  upstreams_path: Path | None = None):
    """建 /proxy/* 透传路由（main 挂载；grant/audit/fetch 注入——测试可替）。

    fastapi 类型经模块级 try/except 导入（见文件头注释）：纯逻辑核心（匹配/渲染/试连/转发）
    宿主无 fastapi 也可单测；本工厂仅在容器/装齐环境调用。

    鉴权：与 A 型同闸——apikey 头→grant_check(caller, pid)（未登记 403 信封）；
    授权字段白名单对 B 型无投影语义（field_space=proxy），grant 在场即可调。
    审计：透传调用也留痕——endpoint＝service:<登记route>（retention 后缀标注），
    与直连调用同八元组口径，一次代理闭环＝审计两条（proxy＋嵌套真实执行）。
    """
    do_fetch = fetch or (lambda url, t, **kw: asyncio.to_thread(_fetch_full, url, t, **kw))
    path = upstreams_path
    router = APIRouter()

    @router.api_route("/proxy/{rest:path}", methods=["GET", "POST"])
    async def proxy_passthrough(request: Request, rest: str) -> Response:
        full_path = "/proxy/" + (rest or "")
        trace_id = getattr(request.state, "trace_id", "") or ""
        try:
            entries = published_entries(path)
        except opsc.OpsConfigError as exc:   # 权威源损坏：fail-closed 拒服（503 同降级口径）
            body = errors.envelope(errors.BAD_UPSTREAMS, str(exc.message))
            await audit_call("unknown", f"service:{full_path}", "", 503, 0, body,
                             trace_id=trace_id)
            return JSONResponse(body, 503)
        entry, sub = match_proxy(full_path, entries)
        if entry is None:
            body = errors.envelope(errors.PROXY_NOT_FOUND,
                                   f"代理路由 {full_path} 未发布（登记表无命中）")
            await audit_call(request.headers.get("apikey") or "unknown",
                             f"service:{full_path}", "", 404, 0, body, trace_id=trace_id)
            return JSONResponse(body, 404)
        pid = str(entry.get("id") or "")
        caller = request.headers.get("apikey", "")
        audit_endpoint = pxe.annotate_endpoint(f"service:{entry['route']}",
                                               pxe.retention_enabled(
                                                   {"retention": entry.get("retention") or {}}))
        try:
            # B 型授权＝可调登记（无字段投影语义）。grant 同步/异步双形态兼容：
            # main 注入同步 _caller_grant（与 A 型执行器同闸），测试可注 async 桩——
            # 原样同步调用 async 桩会让协程从未执行＝授权闸形同虚设（never awaited
            # 告警实证，收口批续作 2026-10-15 修）。
            maybe_coro = grant_check(caller, pid)
            if inspect.isawaitable(maybe_coro):
                await maybe_coro
        except TagQueryError as exc:
            body = errors.envelope(exc.code, exc.message)
            await audit_call(caller or "unknown", audit_endpoint, "", exc.status, 0, body,
                             trace_id=trace_id)
            return JSONResponse(body, exc.status)
        try:
            url = build_target_url(entry, sub, str(request.query_params))
            headers = forward_headers(request)
            body_bytes = await request.body()
            started = time.monotonic()
            result = await forward(do_fetch, entry, url, str(entry.get("method", "GET")),
                                   headers, body_bytes)
        except TagQueryError as exc:
            body = errors.envelope(exc.code, exc.message)
            await audit_call(caller or "unknown", audit_endpoint,
                             sub or "", exc.status,
                             int((time.monotonic() - started) * 1000) if "started" in dir() else 0,
                             body, trace_id=trace_id)
            return JSONResponse(body, exc.status)
        except Exception:  # noqa: BLE001 —— 未预期异常降级 503（不读脏）
            logger.exception("代理转发未预期异常：%s -> %s", full_path, url if "url" in dir() else "?")
            body = errors.envelope(errors.INTERNAL_ERROR, "服务内部错误（代理转发）")
            await audit_call(caller or "unknown", audit_endpoint, sub or "", 503, 0, body,
                             trace_id=trace_id)
            return JSONResponse(body, 503)
        latency_ms = int((time.monotonic() - started) * 1000)
        # 留存钩子（U-10 同口径）：开关缺省关＝零落盘；开＝仅 2xx 落盘
        retained = pxe.retain_body(
            {"retention": entry.get("retention") or {}}, sub or pid, result["body"])
        summary = {"upstream_status": int(result["status"]),
                   "upstream_url": url, "passthrough_bytes": len(result["body"]),
                   "retained_path": retained}
        await audit_call(caller or "unknown", audit_endpoint, sub or "",
                         int(result["status"]), latency_ms, summary, trace_id=trace_id)
        return Response(content=result["body"], status_code=int(result["status"]),
                        media_type=result["content_type"])

    return router
