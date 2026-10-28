#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计消费者（#76 Phase 2）：消费 oneserve-audit → ①按日写本地 JSONL 分片
＋②更新计量统计快照（Redis）。

SPEC §5④/D20 落地：
  - 统计预聚合：消费者按 AppKey×日 聚合调用量/成功数/失败数/延迟分位，
    落 Redis 统计键；metering.py 改读统计快照，**终结直读审计原始日志**。
  - 本地 JSONL 分片：M1 过渡存储（OBS 3 年归档为远端目标，另卡接入）；
    按日分片 audit-YYYY-MM-DD.jsonl，不做删除/清理（OBS 接入后由归档管线处置）。

消费组固定命名（对账口径）：oneserve-audit-consumer
offset 持久化语义：auto_commit=True（默认 5s），消费者重启从 committed offset 续消；
对账验证用全新组 --from-beginning（见 audit_reconcile.py）。

量化判据监控（选型报告 §六）：
  - Redis 水位 >70% → 日志告警
  - 日增 >860 万条 → 日志告警
  - 峰值 >200 QPS → 日志告警
  最简实现：消费者循环内定期检视并 logger.warning。

用法：
  python3 scripts/audit_consumer.py [--bootstrap SERVERS] [--topic TOPIC]
                                     [--shard-dir DIR] [--redis-url URL]

环境变量（缺省值对齐 compose.kafka.yml / docker-compose.yml）：
  KAFKA_BOOTSTRAP_SERVERS  127.0.0.1:9092
  KAFKA_AUDIT_TOPIC        oneserve-audit
  AUDIT_SHARD_DIR          /data/audit/shards
  REDIS_URL                redis://oneserve-redis:6379/0
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("oneserve.audit-consumer")

# ---------- 配置 ----------
DEFAULT_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
DEFAULT_TOPIC = os.environ.get("KAFKA_AUDIT_TOPIC", "oneserve-audit")
DEFAULT_SHARD_DIR = os.environ.get("AUDIT_SHARD_DIR", "/data/audit/shards")
DEFAULT_REDIS_URL = os.environ.get("REDIS_URL", "redis://oneserve-redis:6379/0")
CONSUMER_GROUP = "oneserve-audit-consumer"  # 固定命名（对账口径）

_TZ_CN = timezone(timedelta(hours=8))

# 量化判据阈值（选型报告 §六）
THRESHOLD_DAILY_COUNT = 8_600_000   # 日增 >860 万条
THRESHOLD_QPS = 200                  # 峰值 >200 QPS
THRESHOLD_REDIS_PCT = 70             # Redis 水位 >70%

# Redis 统计键前缀（与 metering.py 读取口径对齐）
STATS_KEY_PREFIX = "audit:stats:"       # audit:stats:{YYYYMMDD}:{caller}
STATS_DAY_INDEX_KEY = "audit:stats:days"  # SET of active day keys


def _parse_ts(ts_str: str) -> datetime | None:
    """ISO8601 容错解析；失败返回 None。"""
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _day_key_cn(dt: datetime) -> str:
    """东八区日期键 YYYYMMDD。"""
    return dt.astimezone(_TZ_CN).strftime("%Y%m%d")


def _shard_path(shard_dir: str, day_str: str) -> Path:
    """按日分片路径：{shard_dir}/audit-YYYY-MM-DD.jsonl。"""
    formatted = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:]}"
    return Path(shard_dir) / f"audit-{formatted}.jsonl"


# svc 维度字段（ADR-0023 第一实施项）：hash 内按 endpoint 的计数字段前缀——
# svc:{endpoint}:{calls|success|fail|lat_sum|lat_cnt}。键空间模型不变（仍是
# audit:stats:{YYYYMMDD}:{caller} 一个 hash），服务维度只加字段不加键；
# metering.py metering_from_stats 读侧同步解析，旧键无 svc 字段＝服务明细为空。
SVC_FIELD_PREFIX = "svc:"
# svc 五指标 → metering.py services 行五元组 [calls,success,fail,latSum,latCnt] 的下标
SVC_METRIC_ORDER = ("calls", "success", "fail", "lat_sum", "lat_cnt")


class StatsAggregator:
    """按 AppKey×日 聚合统计快照（Redis hash）。

    每个 hash 键 audit:stats:{YYYYMMDD}:{caller} 包含字段：
      calls, success, fail, lat_sum, lat_cnt, lat_p50, lat_p99
      svc:{endpoint}:calls / svc:{endpoint}:success / svc:{endpoint}:fail /
      svc:{endpoint}:lat_sum / svc:{endpoint}:lat_cnt   （ADR-0023 第一实施项：
      服务维度扩展——同一 hash 内按 endpoint 计数，键空间模型不变）
    延迟分位用在线近似算法（t-digest 太重，排序缓冲每 1000 条刷新一次足够）。
    """

    def __init__(self, redis_client):
        self._redis = redis_client
        self._buffer: dict[str, list[int]] = {}  # caller -> [latency_ms...]
        self._counters: dict[str, dict] = {}     # caller -> {calls,success,fail,lat_sum,lat_cnt}
        # caller -> endpoint -> [calls,success,fail,lat_sum,lat_cnt]（svc 维度缓冲）
        self._svc: dict[str, dict[str, list[int]]] = {}
        self._day: str = ""
        self._flush_count = 0

    def set_day(self, day_str: str) -> None:
        """切换统计日（跨日时 flush 旧日数据）。"""
        if self._day and self._day != day_str:
            self.flush()
        self._day = day_str

    def record(self, caller: str, status: int, latency_ms: int | None,
               endpoint: str | None = None) -> None:
        """记录一次调用到内存缓冲。

        endpoint 缺省 None＝svc 维度不计（事件缺 endpoint 时如实不落服务维度，
        不造「—」桶污染服务列——与 metering_scan 的占位口径有意不同，见读侧注释）。
        """
        if caller not in self._counters:
            self._counters[caller] = {"calls": 0, "success": 0, "fail": 0,
                                       "lat_sum": 0, "lat_cnt": 0}
        c = self._counters[caller]
        c["calls"] += 1
        ok = isinstance(status, int) and 200 <= status < 300
        bad = isinstance(status, int) and not ok
        if ok:
            c["success"] += 1
        elif bad:
            c["fail"] += 1
        if isinstance(latency_ms, int):
            c["lat_sum"] += latency_ms
            c["lat_cnt"] += 1
            self._buffer.setdefault(caller, []).append(latency_ms)
        if endpoint:
            svc_row = self._svc.setdefault(caller, {}).setdefault(
                str(endpoint), [0, 0, 0, 0, 0])   # [calls,success,fail,lat_sum,lat_cnt]
            svc_row[0] += 1
            if ok:
                svc_row[1] += 1
            elif bad:
                svc_row[2] += 1
            if isinstance(latency_ms, int):
                svc_row[3] += latency_ms
                svc_row[4] += 1
        self._flush_count += 1
        if self._flush_count >= 1000:
            self.flush()

    def flush(self) -> None:
        """将内存缓冲写入 Redis（pipeline 批量）。"""
        if not self._counters or not self._day:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            for caller, c in self._counters.items():
                key = f"{STATS_KEY_PREFIX}{self._day}:{caller}"
                pipe.hincrby(key, "calls", c["calls"])
                pipe.hincrby(key, "success", c["success"])
                pipe.hincrby(key, "fail", c["fail"])
                pipe.hincrby(key, "lat_sum", c["lat_sum"])
                pipe.hincrby(key, "lat_cnt", c["lat_cnt"])
                pipe.expire(key, 8 * 86400)  # 8 天 TTL（与 stats.py DAY_TTL_SECONDS 对齐）
                # 延迟分位（缓冲排序近似）
                lats = self._buffer.get(caller, [])
                if lats:
                    lats_sorted = sorted(lats)
                    n = len(lats_sorted)
                    p50 = lats_sorted[n // 2]
                    p99 = lats_sorted[min(int(n * 0.99), n - 1)]
                    pipe.hset(key, mapping={"lat_p50": str(p50), "lat_p99": str(p99)})
                # svc 维度字段（ADR-0023 第一实施项）：同 hash 内按 endpoint hincrby
                for ep, row in self._svc.get(caller, {}).items():
                    for i, metric in enumerate(SVC_METRIC_ORDER):
                        if row[i]:
                            pipe.hincrby(key, f"{SVC_FIELD_PREFIX}{ep}:{metric}", row[i])
            # 记录活跃日索引（供 metering.py 枚举）
            pipe.sadd(STATS_DAY_INDEX_KEY, self._day)
            pipe.expire(STATS_DAY_INDEX_KEY, 8 * 86400)
            pipe.execute()
        except Exception:
            logger.warning("Redis 统计写入失败", exc_info=True)

        # 重置缓冲
        self._counters.clear()
        self._buffer.clear()
        self._svc.clear()
        self._flush_count = 0

    async def aflush(self) -> None:
        """异步版本（aioredis pipeline）。"""
        if not self._counters or not self._day:
            return
        try:
            pipe = self._redis.pipeline(transaction=False)
            for caller, c in self._counters.items():
                key = f"{STATS_KEY_PREFIX}{self._day}:{caller}"
                pipe.hincrby(key, "calls", c["calls"])
                pipe.hincrby(key, "success", c["success"])
                pipe.hincrby(key, "fail", c["fail"])
                pipe.hincrby(key, "lat_sum", c["lat_sum"])
                pipe.hincrby(key, "lat_cnt", c["lat_cnt"])
                pipe.expire(key, 8 * 86400)
                lats = self._buffer.get(caller, [])
                if lats:
                    lats_sorted = sorted(lats)
                    n = len(lats_sorted)
                    p50 = lats_sorted[n // 2]
                    p99 = lats_sorted[min(int(n * 0.99), n - 1)]
                    pipe.hset(key, mapping={"lat_p50": str(p50), "lat_p99": str(p99)})
                # svc 维度字段（ADR-0023 第一实施项）：同 hash 内按 endpoint hincrby
                for ep, row in self._svc.get(caller, {}).items():
                    for i, metric in enumerate(SVC_METRIC_ORDER):
                        if row[i]:
                            pipe.hincrby(key, f"{SVC_FIELD_PREFIX}{ep}:{metric}", row[i])
            pipe.sadd(STATS_DAY_INDEX_KEY, self._day)
            pipe.expire(STATS_DAY_INDEX_KEY, 8 * 86400)
            await pipe.execute()
        except Exception:
            logger.warning("Redis 统计写入失败", exc_info=True)
        self._counters.clear()
        self._buffer.clear()
        self._svc.clear()
        self._flush_count = 0


class MonitorThresholds:
    """量化判据监控（选型报告 §六）：定期检视并告警。"""

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._last_check = 0.0
        self._check_interval = 60.0  # 每 60s 检查一次
        self._daily_counts: dict[str, int] = {}
        self._qps_window: list[float] = []

    def record_message(self, day_str: str) -> None:
        """记录一条消息用于 QPS/日增计算。"""
        now = time.monotonic()
        self._qps_window.append(now)
        # 只保留最近 10s 的窗口
        cutoff = now - 10.0
        self._qps_window = [t for t in self._qps_window if t > cutoff]
        self._daily_counts[day_str] = self._daily_counts.get(day_str, 0) + 1

    async def check(self) -> None:
        """检查量化判据（每 60s 一次）。"""
        now = time.monotonic()
        if now - self._last_check < self._check_interval:
            return
        self._last_check = now

        # QPS 检查
        if self._qps_window:
            window_s = max(self._qps_window[-1] - self._qps_window[0], 1.0)
            qps = len(self._qps_window) / window_s
            if qps > THRESHOLD_QPS:
                logger.warning("⚠️ 量化判据：QPS=%.1f > %d（越线→评估切 Kafka/RocketMQ）",
                               qps, THRESHOLD_QPS)

        # 日增检查
        today = datetime.now(_TZ_CN).strftime("%Y%m%d")
        daily = self._daily_counts.get(today, 0)
        if daily > THRESHOLD_DAILY_COUNT:
            logger.warning("⚠️ 量化判据：日增=%d > %d（越线→评估扩容）",
                           daily, THRESHOLD_DAILY_COUNT)

        # Redis 水位检查
        if self._redis is not None:
            try:
                info = await self._redis.info("memory")
                used = info.get("used_memory", 0)
                maxmem = info.get("maxmemory", 0)
                if maxmem > 0:
                    pct = used / maxmem * 100
                    if pct > THRESHOLD_REDIS_PCT:
                        logger.warning("⚠️ 量化判据：Redis 水位=%.1f%% > %d%%（越线→扩容或切 MQ）",
                                       pct, THRESHOLD_REDIS_PCT)
            except Exception:
                pass  # Redis 不可用时跳过


async def run_consumer(bootstrap: str, topic: str, shard_dir: str,
                       redis_url: str) -> None:
    """主消费循环（异步，aioredis + kafka-python 同步消费在 executor 中）。"""
    import asyncio
    import aioredis

    # Redis 连接
    redis = aioredis.from_url(redis_url, decode_responses=True)
    logger.info("Redis 连接就绪：%s", redis_url)

    # 分片目录
    Path(shard_dir).mkdir(parents=True, exist_ok=True)
    logger.info("分片目录：%s", shard_dir)

    # 统计聚合器 + 监控
    agg = StatsAggregator(redis)
    monitor = MonitorThresholds(redis)

    # Kafka 消费者（同步 API，放 executor）
    from kafka import KafkaConsumer
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=CONSUMER_GROUP,
        auto_offset_reset="latest",       # 新消费者从最新开始（历史由对账脚本覆盖）
        enable_auto_commit=True,          # offset 自动提交（5s 间隔）
        auto_commit_interval_ms=5000,
        max_poll_records=500,
        request_timeout_ms=10000,
        session_timeout_ms=30000,
    )
    logger.info("Kafka 消费者就绪：group=%s topic=%s bootstrap=%s",
                CONSUMER_GROUP, topic, bootstrap)

    # 优雅退出
    running = True
    loop = asyncio.get_event_loop()

    def _stop(sig, frame):
        nonlocal running
        logger.info("收到信号 %s，准备退出…", sig)
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    msg_count = 0
    last_flush = time.monotonic()

    while running:
        # poll in executor to not block event loop
        batch = await loop.run_in_executor(None, lambda: consumer.poll(timeout_ms=1000))
        if not batch:
            # 空闲时定期 flush + 监控检查
            if time.monotonic() - last_flush > 5.0:
                await agg.aflush()
                await monitor.check()
                last_flush = time.monotonic()
            continue

        for tp, messages in batch.items():
            for msg in messages:
                try:
                    record = json.loads(msg.value.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    logger.warning("坏消息跳过：offset=%d", msg.offset)
                    continue

                ts_str = record.get("ts", "")
                dt = _parse_ts(ts_str)
                if dt is None:
                    logger.warning("脏 ts 跳过：offset=%d ts=%r", msg.offset, ts_str)
                    continue

                day_str = _day_key_cn(dt)
                caller = record.get("caller", "")
                status = record.get("status")
                latency = record.get("latency_ms")

                # ① 按日写本地 JSONL 分片（M1 过渡存储）
                shard = _shard_path(shard_dir, day_str)
                with shard.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

                # ② 更新统计快照（svc 维度：事件带 endpoint 才计，ADR-0023 第一实施项）
                agg.set_day(day_str)
                agg.record(caller, status, latency,
                           endpoint=str(record.get("endpoint") or "") or None)

                # 监控
                monitor.record_message(day_str)
                msg_count += 1

        # 定期 flush
        if time.monotonic() - last_flush > 5.0:
            await agg.aflush()
            await monitor.check()
            last_flush = time.monotonic()
            if msg_count % 1000 == 0 and msg_count > 0:
                logger.info("已消费 %d 条", msg_count)

    # 退出清理
    logger.info("消费者退出：共消费 %d 条", msg_count)
    await agg.aflush()
    consumer.close()
    await redis.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="审计消费者（Kafka→JSONL分片+Redis统计）")
    parser.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP)
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument("--shard-dir", default=DEFAULT_SHARD_DIR)
    parser.add_argument("--redis-url", default=DEFAULT_REDIS_URL)
    args = parser.parse_args()

    import asyncio
    try:
        asyncio.run(run_consumer(args.bootstrap, args.topic,
                                 args.shard_dir, args.redis_url))
    except KeyboardInterrupt:
        logger.info("Ctrl+C 退出")
    except Exception:
        logger.exception("消费者异常退出")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
