# -*- coding: utf-8 -*-
"""真实调用统计（portal:stats:live）：门户 KPI 的唯一数据来源（诚信线，2026-09-22 裁决）。

口径（与审计七元组同源）：
  - 一次计数＝一次经网关到达 service 业务端点的真实 API 调用（挂点＝main._audit_call，
    恰好覆盖 /v1/ping、/v1/tags/query、/v1/news/query、/demo/query；
    healthz 与 /v1/portal/* 只读端点不计）；成功＝2xx，其余（400/403/401/5xx…）＝失败。
  - 网关即拒的请求（key-auth 401，未到 service）由网关 log-phase 计数入同一组键
    （configs/apisix/init_routes.py serverless-pre-function），双计防护＝仅当
    upstream 未被触达时才计。
  - 键：hash portal:stats:live{totalCalls,successCount,failCount}（累计，HINCRBY）；
    日滚动 hash portal:stats:live:day:YYYYMMDD{calls,success,fail}（今日调用/成功率/
    近 7 日趋势数据源；日期按东八区，TTL 8 天＝趋势窗口＋缓冲）。
  - 持久性＝Redis AOF（compose 已 appendonly yes/everysec）：service/网关重启计数不丢。
纯 stdlib＋redis client 注入（无 FastAPI 依赖，宿主机可单测；计数失败只告警不阻断业务）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("oneserve.service")

LIVE_KEY = "portal:stats:live"                 # 累计计数（与任务书约定键名）
DAY_KEY_PREFIX = "portal:stats:live:day:"      # 日滚动键前缀（todayCalls/趋势/当日成败）
DAY_TTL_SECONDS = 8 * 86400                    # 近 7 日趋势窗口＋1 天缓冲后过期
_TZ_CN = timezone(timedelta(hours=8))          # 「今日」按东八区滚动（门户受众口径）

TREND_DAYS = 7


def _now(now: datetime | None) -> datetime:
    """当前时刻（可注入；默认东八区真实时间）。"""
    return now.astimezone(_TZ_CN) if now is not None else datetime.now(_TZ_CN)


def day_key(now: datetime | None = None) -> str:
    """日滚动键（YYYYMMDD 取东八区日期）。"""
    return DAY_KEY_PREFIX + _now(now).strftime("%Y%m%d")


def _last_days(now: datetime | None) -> list[str]:
    """近 7 日键（含今日，升序：最旧→今日）。"""
    t = _now(now)
    return [DAY_KEY_PREFIX + (t - timedelta(days=i)).strftime("%Y%m%d")
            for i in range(TREND_DAYS - 1, -1, -1)]


def _outcome_field(status: int) -> tuple[str, str]:
    """按状态码归类：返回（累计键字段, 日键字段）——2xx 成功，其余（4xx/5xx）失败。"""
    return ("successCount", "success") if 200 <= status < 300 else ("failCount", "fail")


async def record_call(redis, status: int, now: datetime | None = None) -> None:
    """计一次真实调用（管道原子递增累计＋当日两组键；失败只告警不阻断业务返回）。"""
    total_f, day_f = _outcome_field(status)
    day = day_key(now)
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.hincrby(LIVE_KEY, "totalCalls", 1)
        pipe.hincrby(LIVE_KEY, total_f, 1)
        pipe.hincrby(day, "calls", 1)
        pipe.hincrby(day, day_f, 1)
        pipe.expire(day, DAY_TTL_SECONDS)
        await pipe.execute()
    except Exception:  # noqa: BLE001 —— 统计挂点与审计同纪律：宁可少数不断业务
        logger.warning("live stats 计数失败（status=%s，业务继续返回）", status, exc_info=True)


async def read_stats(redis, now: datetime | None = None) -> dict:
    """读 KPI 数据面：{totalCalls, callsTrend7d, todayCalls, todaySuccessRate, successCount, failCount}。

    无调用全为 0/空轴（不造数）；todaySuccessRate＝当日 success/(success+fail)。
    """
    keys = [LIVE_KEY, *_last_days(now)]
    try:
        pipe = redis.pipeline(transaction=False)
        for k in keys:
            pipe.hgetall(k)
        rows = await pipe.execute()
    except Exception:  # noqa: BLE001 —— 读失败按零值降级（页面零崩溃，同 v3 纪律）
        logger.warning("live stats 读取失败，KPI 按零值降级", exc_info=True)
        rows = [{} for _ in keys]

    def _int(h: dict, f: str) -> int:
        try:
            return int(h.get(f, 0) or 0)
        except (TypeError, ValueError):
            return 0

    live, days = rows[0], rows[1:]
    today = days[-1]
    ok, bad = _int(today, "success"), _int(today, "fail")
    called = ok + bad
    return {
        "totalCalls": _int(live, "totalCalls"),
        "successCount": _int(live, "successCount"),
        "failCount": _int(live, "failCount"),
        "callsTrend7d": [_int(d, "calls") for d in days],   # 最旧→今日
        "todayCalls": _int(today, "calls"),
        "todaySuccessRate": (ok / called) if called else 0.0,
    }
