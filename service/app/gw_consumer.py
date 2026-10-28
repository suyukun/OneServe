# -*- coding: utf-8 -*-
"""网关 consumer 动态下发（P0-4；P1-5 加限流插件）——经 APISIX Admin API 建/删 consumer。

与 configs/apisix/init_routes.py 同款通道：urllib 直调 Admin API（X-API-KEY 头），
9180 仅 compose 内网可达；幂等口径＝新建 201／覆盖 200 皆成功、删除 404 视作已删。

P1-5：consumer 除 key-auth 外按登记配额挂 limit-count/limit-conn（route×consumer
维度），插件形状由 app/ratelimit.py 单一构造（阈值＝appkeys.yaml 登记值，与门户
四要素同源）。配额未登记＝只挂 key-auth（如实不限，不揣测默认值）。

为什么 urllib 而非 httpx：服务层依赖最小化（requirements 零新增）；阻塞调用经
asyncio.to_thread 包一层，不占事件循环。传输函数可注入（_req 参数），单元测试
用假实现即可全路径覆盖，不起真网关。
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable

from app import ratelimit as rl

ADMIN = os.environ.get("APISIX_ADMIN", "http://oneserve-apisix:9180")
ADMIN_KEY = os.environ.get("APISIX_ADMIN_KEY", "")   # 与 init_routes.py 一致（真值来自 .env；密钥出库批 2026-10-20）

ReqFn = Callable[[str, str, dict | None], tuple[int, dict]]


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """调 Admin API，返回 (状态码, 响应 JSON)——结构对齐 init_routes.req（同款通道）。"""
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


async def put_consumer(appkey: str, *, quota_qps=None, quota_daily=None,
                       _req: ReqFn = _req) -> bool:
    """创建/覆盖 consumer（username=appkey，key-auth 凭证=appkey 本身）。

    key-auth 的 key 即调用方随 apikey 头发送的凭证；服务层审计七元组的 caller
    字段＝同一值（main 端点读 apikey 头），三处同源零映射。

    P1-5：quota_qps/quota_daily 给定时同挂网关限流插件（阈值即登记值，与门户
    四要素同源）；未给（如 auth_matrix 直发、或登记缺失）＝只挂 key-auth，
    保持 P0-4 既有形状（存量调用方零行为变化）。
    """
    code, _resp = await asyncio.to_thread(
        _req, "PUT", f"consumers/{appkey}",
        rl.consumer_body(appkey, quota_qps=quota_qps, quota_daily=quota_daily))
    return code in (200, 201)


async def delete_consumer(appkey: str, *, _req: ReqFn = _req) -> bool:
    """删除 consumer（禁用即时生效：网关侧立即 401）；404＝本就不存在，视作成功。"""
    code, _resp = await asyncio.to_thread(_req, "DELETE", f"consumers/{appkey}")
    return code in (200, 404)


async def admin_ready(*, _req: ReqFn = _req) -> bool:
    """Admin API 可达性探针（GET routes 200）。"""
    code, _ = await asyncio.to_thread(_req, "GET", "routes")
    return code == 200
