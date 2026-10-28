#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""审计消费者＋计量统计预聚合测试套件（#76 Phase 2）。

分层：
  - StatsAggregator：纯逻辑聚合（mock Redis pipeline）；
  - metering_from_stats：Redis 统计快照读取（mock aioredis）；
  - MonitorThresholds：量化判据告警逻辑；
  - 分片写入：按日 JSONL 分片路径生成。

Kafka 未起时全部测试仍绿（mock 覆盖所有外部依赖）。
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))
sys.path.insert(0, str(REPO))

from scripts.audit_consumer import (  # noqa: E402
    StatsAggregator, MonitorThresholds, _parse_ts, _day_key_cn, _shard_path,
    STATS_KEY_PREFIX, THRESHOLD_DAILY_COUNT, THRESHOLD_QPS,
)
from app.metering import metering_from_stats, _window_days  # noqa: E402


_TZ_CN = timezone(timedelta(hours=8))


# ========== 辅助 ==========

class FakeRedisPipeline:
    """模拟 aioredis pipeline（同步 execute 返回预设值）。"""

    def __init__(self, results=None):
        self._ops = []
        self._results = results or []

    def hincrby(self, key, field, value):
        self._ops.append(("hincrby", key, field, value))
        return self

    def hset(self, key, mapping=None, **kwargs):
        self._ops.append(("hset", key, mapping or kwargs))
        return self

    def expire(self, key, ttl):
        self._ops.append(("expire", key, ttl))
        return self

    def hgetall(self, key):
        self._ops.append(("hgetall", key))
        return self

    def sadd(self, key, *values):
        self._ops.append(("sadd", key, values))
        return self

    async def execute(self):
        return self._results[:len(self._ops)] if self._results else [None] * len(self._ops)


class FakeRedis:
    """模拟 aioredis client。"""

    def __init__(self, data=None, pipeline_results=None):
        self._data = data or {}
        self._pipeline_results = pipeline_results or []

    def pipeline(self, transaction=False):
        return FakeRedisPipeline(results=self._pipeline_results)

    async def scan(self, cursor, match="", count=100):
        """返回匹配键列表。"""
        matched = [k for k in self._data if self._match(match, k)]
        return (0, matched)

    @staticmethod
    def _match(pattern: str, key: str) -> bool:
        """简单 glob 匹配（仅支持尾部 *）。"""
        if pattern.endswith("*"):
            return key.startswith(pattern[:-1])
        return key == pattern

    async def info(self, section=""):
        return {"used_memory": 500_000_000, "maxmemory": 1_000_000_000}


# ========== StatsAggregator 测试 ==========

class TestStatsAggregator:
    """统计聚合器纯逻辑测试。"""

    def test_record_and_flush(self):
        """record 累积计数，flush 写入 Redis pipeline。"""
        redis = FakeRedis()
        agg = StatsAggregator(redis)
        agg.set_day("20261015")
        agg.record("caller-a", 200, 10)
        agg.record("caller-a", 200, 20)
        agg.record("caller-a", 500, 30)
        agg.record("caller-b", 200, 5)

        assert agg._counters["caller-a"]["calls"] == 3
        assert agg._counters["caller-a"]["success"] == 2
        assert agg._counters["caller-a"]["fail"] == 1
        assert agg._counters["caller-a"]["lat_sum"] == 60
        assert agg._counters["caller-b"]["calls"] == 1

    def test_flush_clears_buffer(self):
        """flush 后缓冲清空。"""
        redis = FakeRedis()
        agg = StatsAggregator(redis)
        agg.set_day("20261015")
        agg.record("caller-a", 200, 10)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(agg.aflush())
        finally:
            loop.close()
        assert len(agg._counters) == 0
        assert len(agg._buffer) == 0

    def test_set_day_change_triggers_flush(self):
        """跨日自动 flush 旧日数据。"""
        redis = FakeRedis()
        agg = StatsAggregator(redis)
        agg.set_day("20261015")
        agg.record("caller-a", 200, 10)
        # 切日应触发 flush
        agg.set_day("20261016")
        assert len(agg._counters) == 0  # 旧日已 flush

    def test_auto_flush_at_1000(self):
        """累积 1000 条自动 flush。"""
        redis = FakeRedis()
        agg = StatsAggregator(redis)
        agg.set_day("20261015")
        for i in range(1000):
            agg.record("caller-a", 200, i)
        # 第 1000 条触发 flush
        assert len(agg._counters) == 0

    # ── svc 维度（ADR-0023 第一实施项）正负例 ──

    @staticmethod
    def _record_pipes(redis):
        """捕获 pipeline 对象（断言 flush 产出的 hincrby 命令序列用）。"""
        made = []
        orig = redis.pipeline

        def _pipe(transaction=False):
            p = orig(transaction)
            made.append(p)
            return p

        redis.pipeline = _pipe
        return made

    def test_record_with_endpoint_writes_svc_fields(self):
        """正例：record 带 endpoint → 内存 svc 五元组正确＋flush 产 svc:{endpoint}:* hincrby。

        服务维度只加字段不加键（键空间模型不变）；零值指标不写（省字段空间）；
        成败口径与总量一致（2xx 计成功、非 2xx 计失败、非数字状态只计总量）。
        """
        redis = FakeRedis()
        made = self._record_pipes(redis)
        agg = StatsAggregator(redis)
        agg.set_day("20261020")
        agg.record("caller-a", 200, 10, "service:/v1/query/a")
        agg.record("caller-a", 500, None, "service:/v1/query/a")     # 失败不计均值
        agg.record("caller-a", 200, 5, "service:/v1/query/b")
        agg.record("caller-b", 200, 7, "service:/v1/query/a")        # 跨 caller 同服务各自累计
        # 内存缓冲：caller-a 两服务五元组、caller-b 一服务
        # （500 行 latency=None：计失败、不计均值 → lat_cnt=1）
        assert agg._svc["caller-a"]["service:/v1/query/a"] == [2, 1, 1, 10, 1]
        assert agg._svc["caller-a"]["service:/v1/query/b"] == [1, 1, 0, 5, 1]
        assert agg._svc["caller-b"]["service:/v1/query/a"] == [1, 1, 0, 7, 1]
        # 总量计数不受 svc 维度影响（两套计数同源并行，非第二份账）
        assert agg._counters["caller-a"]["calls"] == 3
        assert agg._counters["caller-a"]["fail"] == 1
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(agg.aflush())
        finally:
            loop.close()
        ops = made[0]._ops
        key_a = f"{STATS_KEY_PREFIX}20261020:caller-a"
        svc_a = [op for op in ops if op[0] == "hincrby" and op[1] == key_a
                 and str(op[2]).startswith("svc:service:/v1/query/a:")]
        fields_a = {op[2]: op[3] for op in svc_a}
        assert fields_a == {
            "svc:service:/v1/query/a:calls": 2,
            "svc:service:/v1/query/a:success": 1,
            "svc:service:/v1/query/a:fail": 1,
            "svc:service:/v1/query/a:lat_sum": 10,
            "svc:service:/v1/query/a:lat_cnt": 1,
        }
        key_b = f"{STATS_KEY_PREFIX}20261020:caller-a"
        fields_b = {op[2]: op[3] for op in ops if op[0] == "hincrby" and op[1] == key_b
                    and str(op[2]).startswith("svc:service:/v1/query/b:")}
        assert fields_b == {"svc:service:/v1/query/b:calls": 1,
                            "svc:service:/v1/query/b:success": 1,
                            "svc:service:/v1/query/b:lat_sum": 5,
                            "svc:service:/v1/query/b:lat_cnt": 1}   # fail=0 不写
        key_c = f"{STATS_KEY_PREFIX}20261020:caller-b"
        fields_c = {op[2]: op[3] for op in ops if op[0] == "hincrby" and op[1] == key_c
                    and str(op[2]).startswith("svc:")}
        assert fields_c == {"svc:service:/v1/query/a:calls": 1,
                            "svc:service:/v1/query/a:success": 1,
                            "svc:service:/v1/query/a:lat_sum": 7,
                            "svc:service:/v1/query/a:lat_cnt": 1}
        # flush 后 svc 缓冲清空（与总量缓冲同纪律）
        assert agg._svc == {}

    def test_record_without_endpoint_no_svc_fields(self):
        """负例：不带 endpoint（旧事件形态/缺 endpoint）→ 不产任何 svc 字段。

        缺维度如实不落——不造「—」占位桶污染服务列；总量计数照常。
        """
        redis = FakeRedis()
        made = self._record_pipes(redis)
        agg = StatsAggregator(redis)
        agg.set_day("20261020")
        agg.record("caller-a", 200, 10)                    # 旧口径四参调用（缺 endpoint）
        agg.record("caller-a", 404, None, "")              # 空 endpoint 同样不计
        assert agg._counters["caller-a"]["calls"] == 2     # 总量不受影响（flush 前取数）
        assert agg._counters["caller-a"]["fail"] == 1
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(agg.aflush())
        finally:
            loop.close()
        assert agg._svc == {}
        ops = made[0]._ops
        assert not [op for op in ops if op[0] == "hincrby" and str(op[2]).startswith("svc:")]


# ========== 分片路径测试 ==========

class TestShardPath:
    """按日分片路径生成。"""

    def test_shard_path_format(self):
        """路径格式 audit-YYYY-MM-DD.jsonl。"""
        p = _shard_path("/data/shards", "20261015")
        assert p == Path("/data/shards/audit-2026-10-15.jsonl")

    def test_day_key_cn(self):
        """东八区日期键 YYYYMMDD。"""
        dt = datetime(2026, 10, 15, 23, 0, tzinfo=timezone.utc)
        # UTC 23:00 = CN 次日 07:00
        assert _day_key_cn(dt) == "20261016"

        dt2 = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
        # UTC 10:00 = CN 18:00
        assert _day_key_cn(dt2) == "20261015"


# ========== 时间解析测试 ==========

class TestParseTs:
    """ISO8601 容错解析。"""

    def test_valid_iso(self):
        dt = _parse_ts("2026-10-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_z_suffix(self):
        dt = _parse_ts("2026-10-15T10:30:00Z")
        assert dt is not None

    def test_invalid(self):
        assert _parse_ts("") is None
        assert _parse_ts("not-a-date") is None
        assert _parse_ts(None) is None


# ========== metering_from_stats 测试 ==========

class TestMeteringFromStats:
    """Redis 统计快照读取测试。"""

    def test_empty_stats_returns_empty(self):
        """无统计键→空聚合。"""
        redis = FakeRedis(data={})
        result = asyncio.new_event_loop().run_until_complete(
            metering_from_stats(redis, days=1))
        assert result["callers"] == {}
        assert len(result["days"]) == 1

    def test_stats_with_data(self):
        """有统计键→正确聚合。"""
        day_str = datetime.now(_TZ_CN).strftime("%Y%m%d")
        data = {
            f"{STATS_KEY_PREFIX}{day_str}:caller-a": {
                "calls": "10", "success": "8", "fail": "2",
                "lat_sum": "100", "lat_cnt": "10",
            },
            f"{STATS_KEY_PREFIX}{day_str}:caller-b": {
                "calls": "5", "success": "5", "fail": "0",
                "lat_sum": "50", "lat_cnt": "5",
            },
        }
        # Pipeline execute 返回对应 hash 数据
        pipeline_results = [
            {"calls": "10", "success": "8", "fail": "2", "lat_sum": "100", "lat_cnt": "10"},
            {"calls": "5", "success": "5", "fail": "0", "lat_sum": "50", "lat_cnt": "5"},
        ]
        redis = FakeRedis(data=data, pipeline_results=pipeline_results)
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(metering_from_stats(redis, days=1))
        loop.close()
        assert "caller-a" in result["callers"]
        assert result["callers"]["caller-a"]["calls"] == 10
        assert result["callers"]["caller-a"]["success"] == 8
        assert "caller-b" in result["callers"]
        assert result["callers"]["caller-b"]["calls"] == 5

    # ── svc 维度读侧（ADR-0023 第一实施项）正负例 ──

    def test_stats_with_svc_fields_services_populated(self):
        """正例：hash 带 svc:{endpoint}:* 字段 → services 真实聚合（服务维度恒空缺陷补丁）。

        五元组形与 metering_scan 完全一致（[calls,success,fail,latSum,latCnt]），
        metering_view 无需区分数据源即可出真实服务明细。
        """
        day_str = datetime.now(_TZ_CN).strftime("%Y%m%d")
        key = f"{STATS_KEY_PREFIX}{day_str}:caller-a"
        data = {key: {"calls": "3", "success": "2", "fail": "1",
                      "lat_sum": "40", "lat_cnt": "3",
                      "svc:service:/v1/query/a:calls": "2",
                      "svc:service:/v1/query/a:success": "1",
                      "svc:service:/v1/query/a:fail": "1",
                      "svc:service:/v1/query/a:lat_sum": "30",
                      "svc:service:/v1/query/a:lat_cnt": "2",
                      "svc:service:/v1/query/b:calls": "1",
                      "svc:service:/v1/query/b:success": "1",
                      "svc:service:/v1/query/b:lat_sum": "10",
                      "svc:service:/v1/query/b:lat_cnt": "1"}}
        redis = FakeRedis(data=data, pipeline_results=[dict(data[key])])
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(metering_from_stats(redis, days=1))
        loop.close()
        j = result["callers"]["caller-a"]
        assert j["calls"] == 3 and j["success"] == 2 and j["fail"] == 1   # 总量列不受影响
        assert j["services"]["service:/v1/query/a"] == [2, 1, 1, 30, 2]
        assert j["services"]["service:/v1/query/b"] == [1, 1, 0, 10, 1]

    def test_stats_legacy_hash_without_svc_keeps_services_empty(self):
        """兼容负例：补丁前旧键（无 svc 字段）→ services 空（服务列降级「—」），他列照常。

        另含脏 svc 字段容错：非五指标形态/脏值跳过不猜，不污染聚合。
        """
        day_str = datetime.now(_TZ_CN).strftime("%Y%m%d")
        key = f"{STATS_KEY_PREFIX}{day_str}:caller-a"
        data = {key: {"calls": "4", "success": "3", "fail": "1",
                      "lat_sum": "50", "lat_cnt": "4",
                      "svc:service:/v1/odd:weird": "9",        # 非五指标形态：跳过
                      "svc::calls": "1",                        # 空 endpoint：跳过
                      "svc:service:/v1/query/x:calls": "zz"}}   # 脏值：跳过
        redis = FakeRedis(data=data, pipeline_results=[dict(data[key])])
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(metering_from_stats(redis, days=1))
        loop.close()
        j = result["callers"]["caller-a"]
        assert j["services"] == {}
        assert j["calls"] == 4 and j["success"] == 3 and j["fail"] == 1
        assert j["latSum"] == 50 and j["latCnt"] == 4


# ========== MonitorThresholds 测试 ==========

class TestMonitorThresholds:
    """量化判据监控逻辑。"""

    def test_qps_threshold(self):
        """QPS 超阈值记录。"""
        mon = MonitorThresholds()
        # 模拟高 QPS
        now = 1000.0
        with patch("time.monotonic", return_value=now):
            for _ in range(300):
                mon.record_message("20261015")
        # 300 条在同一时刻 → QPS 无穷大（窗口 0s → max 1s → 300 QPS）
        assert len(mon._qps_window) == 300

    def test_daily_count_tracking(self):
        """日增计数正确。"""
        mon = MonitorThresholds()
        for _ in range(100):
            mon.record_message("20261015")
        assert mon._daily_counts["20261015"] == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
