# -*- coding: utf-8 -*-
"""/healthz 组件探活纯逻辑（P2a）——不 import FastAPI/redis 客户端（分层同 monitor.py）。

职责边界（决策全在本模块，main.py 只做 HTTP 编排）：
  /healthz 是运维/容器编排健康端点，与门户大盘 /v1/portal/status
  （monitor.components_view，P1-6）口径一致但独立——探活判据同源（redis PING／
  网关 TCP），实现互不引用：改大盘组件卡不动本模块，反之亦然。

评级口径（overall，向后兼容红线）：
  - redis error ⇒ ("error", 503)：既有语义——redis 挂＝致命，compose 的 service
    healthcheck 走 urlopen，非 2xx 即判不健康（判据不变）；
  - hbase/网关/etcd 任一 degraded ⇒ ("degraded", 200)：如实降级但不算 fatal——
    200 让编排不打重启（组件可见性交给监控面与各容器自身 healthcheck）；
  - 全 ok（hbase 未配置＝off 不参与评级）⇒ ("ok", 200)。

组件出参统一 {"status": ok|degraded|error|off, "detail": str}
（off＝未配置不探不评；degraded＝探了但不可达；error＝致命组件失败，现仅 redis）。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from app import connections as conn_cfg   # HBase 探活目标装配（env > yaml > 默认，双轨同源）

HEALTHZ_PROBE_TIMEOUT_S = float(os.environ.get("HEALTHZ_PROBE_TIMEOUT_S", "2.0"))
# 探活目标 env 与 portal_api 同名（APISIX_PROBE_*，网关卡同一判据）；etcd 侧同构可配。
GATEWAY_PROBE_HOST = os.environ.get("APISIX_PROBE_HOST", "oneserve-apisix")
GATEWAY_PROBE_PORT = int(os.environ.get("APISIX_PROBE_PORT", "9080"))
ETCD_PROBE_HOST = os.environ.get("ETCD_PROBE_HOST", "oneserve-etcd")
ETCD_PROBE_PORT = int(os.environ.get("ETCD_PROBE_PORT", "2379"))
# 常驻容器健康聚合探活目标（蓝图 1a 批·监控最小集 2026-10-20）：与 compose 容器名
# 同源（dev/prod/m26 三形态网络别名一致），本机/异构部署可经 env 覆盖。
# 无登录模式（ADR-0029）：原统一登录两容器的探针目标随 compose 收口移除（形态见 git 史）。
PORTAL_PROBE_HOST = os.environ.get("PORTAL_PROBE_HOST", "oneserve-portal")
PORTAL_PROBE_PORT = int(os.environ.get("PORTAL_PROBE_PORT", "80"))


def overall(components: dict) -> tuple[str, int]:
    """组件探活结果 → 整体 status 与 HTTP 码（纯逻辑，tests/test_healthz.py 直测）。

    - redis error → ("error", 503)：既有语义保持——redis 挂＝致命（编排判据）；
    - 其余组件（hbase/网关/etcd）任一 degraded → ("degraded", 200)：如实降级但
      不算 fatal——200 让编排不打重启；
    - 全 ok（hbase 未配置＝off 不参与评级）→ ("ok", 200)。
    """
    if (components.get("redis") or {}).get("status") == "error":
        return "error", 503
    if any((c or {}).get("status") == "degraded" for c in components.values()):
        return "degraded", 200
    return "ok", 200


async def probe_tcp(host: str, port: int, *, what: str) -> dict:
    """轻探＝TCP 三次握手（网内端口在线判据，与网关容器 compose healthcheck 同判据）。

    可达＝ok；连接失败/超时＝degraded（healthz 口径：redis 以外组件不可达不算
    fatal，编排不应因此重启 service——降级如实进 components）。
    """
    t0 = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=HEALTHZ_PROBE_TIMEOUT_S)
        ms = int((time.monotonic() - t0) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 —— 关闭异常不影响「端口已应答」结论
            pass
        return {"status": "ok", "detail": f"{what} TCP 可达 {host}:{port}（{ms}ms）"}
    except Exception as exc:  # noqa: BLE001 —— 健康探针要吞异常报状态
        return {"status": "degraded",
                "detail": f"{what} 不可达 {host}:{port}（{type(exc).__name__}: {exc}）"}


def hbase_probe_target() -> tuple[str, int] | None:
    """HBase 轻探目标（未配置＝None → 组件如实列 off，不探不评）。

    「已配置」＝env HBASE_HOST 非空（compose 注入空串＝不覆盖）**或** connections.yaml
    含 sources.hbase 段（容器内 /app/config/connections.yaml 即配 oneserve-hbase）；
    两者皆无＝未配置——代码默认 127.0.0.1 只是宿主自测缺省，探它反而误报 degraded。
    坏 connections.yaml 不在本端点复检（启动已如实报错），按未配置处理不探。
    """
    env_host = os.environ.get("HBASE_HOST", "").strip()
    if env_host:
        try:
            port = int(os.environ.get("HBASE_PORT", "").strip() or conn_cfg.DEFAULT_PORT)
        except ValueError:
            port = conn_cfg.DEFAULT_PORT
        return env_host, port
    try:
        section = conn_cfg._load_hbase_section(None)   # 私有读段：同包复用，免二次实现装配链
    except conn_cfg.ConnectionsError:
        return None
    if section.get("host"):
        return str(section["host"]), int(section.get("port") or conn_cfg.DEFAULT_PORT)
    return None


async def probe_hbase() -> dict:
    """HBase 组件＝配置了才轻探（Thrift TCP；不可达 degraded 不 fatal），未配置＝off。"""
    target = hbase_probe_target()
    if target is None:
        return {"status": "off", "detail": "未配置（env 与 connections.yaml 均未给 host）——不探不评"}
    host, port = target
    return await probe_tcp(host, port, what="HBase Thrift")


async def probe_redis(redis: Any) -> dict:
    """Redis 组件＝真做一次 PING（同 monitor.probe_redis 判据；healthz 独立实现）。

    redis 为 None（lifespan 未走完/已收尾的极端窗口）也如实 error，不谎报在线。
    """
    if redis is None:
        return {"status": "error", "detail": "redis 客户端未初始化"}
    try:
        pong = await asyncio.wait_for(redis.ping(), timeout=HEALTHZ_PROBE_TIMEOUT_S)
        if pong:
            return {"status": "ok", "detail": "PING→PONG（命令通道可用）"}
        return {"status": "error", "detail": f"PING 返回异常值 {pong!r}"}
    except Exception as exc:  # noqa: BLE001 —— 探活失败如实报，不降级成「正常」
        return {"status": "error", "detail": f"PING 失败（{type(exc).__name__}: {exc}）"}

# 【无登录模式 ADR-0029·已摘】probe_http_get（HTTP GET 轻探，原统一登录 IdP 发现
# 端点判据）随登录容器退役移除；历史实现见 git 史。

