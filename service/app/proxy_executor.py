# -*- coding: utf-8 -*-
"""B 型外部代理执行器（task-007）：services.yaml 配置驱动的上游转发＋留存钩子。

实现 SPEC §1.2 B 型原型（外部代理：合作方调我们→网关→服务层转发上游→回传）：
  读 services.yaml（archetype: external_proxy）→ 拼 upstream URL →
  按 backend.proxy.timeout_ms/retry 超时重试转发 → 成功响应按 retention 开关落盘 →
  响应原样回传（透传语义：状态码/Content-Type/体均不改写）。

设计约束：
  - 零新增第三方依赖：上游调用用 urllib（asyncio.to_thread 包一层）；与 A 型同款
    「执行器不 import FastAPI」风格——fetch 经参数注入，单元测试用本地 http.server
    或桩函数即可全路径覆盖；
  - 留存钩子（U-10 留口，AC-3）：retention.enabled=false 时严禁写任何响应内容——
    连留存目录都不创建；开启时也只落「上游 2xx 成功响应」，错误一律不落（不留脏数据）；
  - 审计标注（AC-3）：审计七元组结构不动，endpoint 字段以 ?retention=on/off 后缀
    标注本次调用留存与否（annotate_endpoint）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from app import errors
from app.kv_executor import TagQueryError  # 复用可控错误（status/code/message 三件套）

# 文件名安全字符白名单（键是调用方输入，防路径穿越；超长截断）
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
# 上游相对路径黑名单模式：禁 scheme://、双斜线、@、冒号、反斜杠、空白、目录上跳
_BAD_REST = re.compile(r'(@|:|\\|//|\.\.|\s)')


# ---------- 配置读取 ----------

def proxy_conf(service: dict) -> dict:
    """取 backend.proxy 配置；缺失 upstream_url＝配置错误，拒服（fail-closed）。"""
    conf = service.get("backend", {}).get("proxy")
    if not conf or not conf.get("upstream_url"):
        raise TagQueryError(500, errors.BAD_PROXY_CONFIG, "服务缺 backend.proxy.upstream_url 配置")
    return conf


def retention_enabled(service: dict) -> bool:
    """U-10 留存开关（缺省＝关：留口不默认留存，最小收集口径；services.yaml 显式开）。"""
    return bool(service.get("retention", {}).get("enabled", False))


def annotate_endpoint(base: str, enabled: bool) -> str:
    """审计标注：endpoint 以 ?retention=on/off 后缀记录本次留存行为（七元组结构不变）。"""
    return f"{base}?retention={'on' if enabled else 'off'}"


# ---------- 上游 URL 拼装 ----------

def build_upstream_url(service: dict, rest: str, query_string: str) -> str:
    """upstream_url + 相对路径 + 原样查询串；rest 只许普通路径段（防改写转发目标）。"""
    conf = proxy_conf(service)
    rest = (rest or "").strip("/")
    if not rest or _BAD_REST.search(rest):
        raise TagQueryError(400, errors.INVALID_PROXY_PATH,
                            "代理路径须为上游相对路径（不含 host/scheme/特殊字符/上跳）")
    url = f"{conf['upstream_url'].rstrip('/')}/{rest}"
    return f"{url}?{query_string}" if query_string else url


# ---------- 上游调用（urllib→to_thread；传输层失败归类 502/504） ----------

async def fetch_upstream(url: str, timeout_ms: int) -> dict:
    """单次上游 GET：返回 {status, content_type, body}。

    上游已应答（含 4xx/5xx）＝响应，按状态码原样返回（透传，不重试）；
    传输层失败归类：超时→504 upstream_timeout，其余（连接拒绝/DNS/复位）→502。
    """

    def _sync() -> dict:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(int(timeout_ms), 1) / 1000) as resp:
                return {"status": resp.status,
                        "content_type": resp.headers.get("Content-Type", "application/json"),
                        "body": resp.read()}
        except urllib.error.HTTPError as exc:            # 上游应答了错误状态＝也透传
            return {"status": exc.code,
                    "content_type": exc.headers.get("Content-Type", "application/json"),
                    "body": exc.read()}
        except Exception as exc:  # noqa: BLE001 —— 传输层失败统一归类
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise TagQueryError(504, errors.UPSTREAM_TIMEOUT,
                                    f"上游超时（>{timeout_ms}ms），按配置熔断") from exc
            raise TagQueryError(502, errors.UPSTREAM_UNREACHABLE,
                                f"上游不可达：{type(exc).__name__}") from exc

    return await asyncio.to_thread(_sync)


# ---------- 留存钩子（U-10） ----------

def retain_body(service: dict, key: str, body: bytes,
                now: _dt.datetime | None = None) -> str | None:
    """留存开关开：响应体落盘 retention.dir，文件名＝UTC 时间戳_业务键.json。

    开关关（enabled=false/缺省）：直接返回 None——严禁写任何响应内容，
    连留存目录都不创建（AC-3「关→零落盘」的强口径）。
    """
    if not retention_enabled(service):
        return None
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"-{now.microsecond // 1000:03d}Z"
    safe_key = _UNSAFE_NAME.sub("_", (key or "").strip())[:64] or "nokey"
    dir_path = Path(service["retention"]["dir"])
    dir_path.mkdir(parents=True, exist_ok=True)          # 仅在确要落盘时才建目录
    path = dir_path / f"{stamp}_{safe_key}.json"
    path.write_bytes(body)
    return str(path)


# ---------- 执行器主体 ----------

async def execute(fetch: Callable[[str, int], Awaitable[dict]], service: dict,
                  rest: str, query_string: str, key: str) -> dict:
    """一次代理转发（AC-2/AC-3 主路径）。

    fetch：注入的上游调用（生产传 fetch_upstream；测试可注入桩）。
    重试口径：仅传输层失败（502/504）重试，共 1+retry 次；上游已应答不重试。
    留存口径：仅「开关开 ∧ 上游 2xx」落盘；错误响应零落盘（不留脏数据）。
    返回 {status, content_type, body, body_text, upstream_url, retained_path, retention}。
    """
    conf = proxy_conf(service)
    url = build_upstream_url(service, rest, query_string)

    attempts = 1 + max(int(conf.get("retry", 0)), 0)
    result: dict | None = None
    last_err: TagQueryError | None = None
    for _ in range(attempts):
        try:
            result = await fetch(url, int(conf.get("timeout_ms", 3000)))
            last_err = None
            break
        except TagQueryError as exc:
            if exc.status not in (502, 504):
                raise                                    # 非传输层错误不重试
            last_err = exc
    if last_err is not None:
        raise last_err

    status = int(result["status"])
    retained_path: str | None = None
    if 200 <= status < 300:                              # 错误响应一律不落（不留脏数据）
        retained_path = retain_body(service, key, result["body"])
    return {"status": status,
            "content_type": result["content_type"],
            "body": result["body"],
            "body_text": bytes(result["body"]).decode("utf-8", errors="replace"),
            "upstream_url": url,
            "retained_path": retained_path,
            "retention": retention_enabled(service)}
