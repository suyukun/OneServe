#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实调用统计测试（诚信线 2026-09-22：门户 KPI 数字必须真）——纯 python 直跑或 pytest 收集均可。

覆盖四组判据（任务书第 5 条）：
  1. 计数正确：每次 record_call 恰好 +1 totalCalls，落对累计/当日两组键；
  2. 成败分计：2xx＝success，4xx/5xx（含网关 401、400、503）＝fail，
     todaySuccessRate＝success/(success+fail) 真实计算，无调用＝0.0；
  3. 日滚动：todayCalls 只取当日键；callsTrend7d＝近 7 日（最旧→今日）逐日 calls；
  4. 重启恢复：计数只落 Redis（模块零进程态）——「service 重启」＝重新 read 不变，
     「Redis 重启」＝同一持久后端的 新客户端实例 读值不变（AOF 语义，真栈另测）。

被测对象 service/app/stats.py 纯 stdlib（redis client 注入，同 batch.py 纪律）。
main._audit_call 挂点与网关 Lua 钩子为薄胶水（端到端归 compose 实测，见回执验证段）。
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "service"))

from app import stats  # noqa: E402

UTC = timezone.utc
DAY0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=UTC)   # 东八区 2026-09-22 20:00


class FakePipe:
    """假管道：缓存命令，execute 时对 FakeRedis 后端逐条执行（HINCRBY/EXPIRE 语义）。"""

    def __init__(self, redis):
        self._r = redis
        self._cmds = []

    def hincrby(self, key, field, amount=1):
        self._cmds.append(("hincrby", key, field, amount))
        return self

    def hgetall(self, key):
        self._cmds.append(("hgetall", key))
        return self

    def expire(self, key, seconds):
        self._cmds.append(("expire", key, seconds))
        return self

    async def execute(self):
        out = []
        for cmd in self._cmds:
            if cmd[0] == "hincrby":
                _, key, field, amount = cmd
                h = self._r.store.setdefault(key, {})
                h[field] = str(int(h.get(field, "0")) + amount)
                out.append(int(h[field]))
            elif cmd[0] == "hgetall":
                out.append(dict(self._r.store.get(cmd[1], {})))
            elif cmd[0] == "expire":
                self._r.ttl[cmd[1]] = cmd[2]
                out.append(True)
        self._cmds = []
        return out


class FakeRedis:
    """字典假 Redis：store＝持久后端（模拟 AOF 数据文件，跨实例存活）。"""

    def __init__(self, store=None, ttl=None, broken=False):
        self.store = store if store is not None else {}
        self.ttl = ttl if ttl is not None else {}
        self.broken = broken

    def pipeline(self, transaction=True):
        if self.broken:
            raise ConnectionError("redis down")
        return FakePipe(self)


async def _record(r, status, now=None):
    await stats.record_call(r, status, now=now)


# ----------------------------------------------------------------------
# 1) 计数正确＋成败分计（含 401/400/503 全部计失败）
# ----------------------------------------------------------------------

def test_counts_and_outcome_split():
    r = FakeRedis()
    for status in (200, 200, 204, 400, 401, 503):   # 成功口径＝2xx（含 204）
        asyncio.run(_record(r, status, now=DAY0))
    live = r.store[stats.LIVE_KEY]
    assert live["totalCalls"] == "6", "每次调用 totalCalls 恰好 +1"
    assert live["successCount"] == "3", "2xx 三次全计 success"
    assert live["failCount"] == "3", "400/401/503 三次全计 fail"

    out = asyncio.run(stats.read_stats(r, now=DAY0))
    assert out["totalCalls"] == 6 and out["successCount"] == 3 and out["failCount"] == 3
    assert out["todayCalls"] == 6
    assert out["todaySuccessRate"] == 0.5, "成功率＝success/(success+fail) 真实计算"
    day = stats.day_key(DAY0)
    assert r.store[day]["calls"] == "6" and r.store[day]["fail"] == "3"
    assert r.ttl[day] == stats.DAY_TTL_SECONDS, "日键须设 TTL（趋势窗口＋缓冲）"


# ----------------------------------------------------------------------
# 2) 日滚动：今日只取当日键；趋势＝近 7 日逐日（最旧→今日）
# ----------------------------------------------------------------------

def test_daily_rolling_and_trend7d():
    r = FakeRedis()
    asyncio.run(_record(r, 200, now=DAY0 - timedelta(days=2)))   # D-2：1 成
    asyncio.run(_record(r, 200, now=DAY0 - timedelta(days=2)))
    asyncio.run(_record(r, 500, now=DAY0 - timedelta(days=1)))   # D-1：1 败
    asyncio.run(_record(r, 200, now=DAY0))                       # D（今日）：1 成
    out = asyncio.run(stats.read_stats(r, now=DAY0))
    assert out["totalCalls"] == 4, "累计跨日滚动累加"
    assert out["todayCalls"] == 1, "今日只计当日键（昨日/前日不串档）"
    assert out["todaySuccessRate"] == 1.0, "成功率只按当日成败算"
    assert out["callsTrend7d"] == [0, 0, 0, 0, 2, 1, 1], "近 7 日趋势＝最旧→今日"
    assert out["callsTrend7d"][-1] == out["todayCalls"], "趋势末位＝今日"

    # 滚入新一天：昨日变前日，今日清零重计
    out2 = asyncio.run(stats.read_stats(r, now=DAY0 + timedelta(days=1)))
    assert out2["todayCalls"] == 0 and out2["todaySuccessRate"] == 0.0
    assert out2["callsTrend7d"] == [0, 0, 0, 2, 1, 1, 0]
    assert out2["totalCalls"] == 4, "累计不受日滚动影响"


# ----------------------------------------------------------------------
# 3) 无调用显示 0（不造数）
# ----------------------------------------------------------------------

def test_empty_shows_zero():
    r = FakeRedis()
    out = asyncio.run(stats.read_stats(r, now=DAY0))
    assert out == {"totalCalls": 0, "successCount": 0, "failCount": 0,
                   "callsTrend7d": [0] * 7, "todayCalls": 0, "todaySuccessRate": 0.0}


# ----------------------------------------------------------------------
# 4) 重启恢复：计数只落 Redis（模块零进程态）＋客户端换实例值不变
# ----------------------------------------------------------------------

def test_restart_recovery_state_only_in_redis():
    store, ttl = {}, {}                       # 持久后端（模拟 AOF 文件）
    r1 = FakeRedis(store=store, ttl=ttl)
    asyncio.run(_record(r1, 200, now=DAY0))
    asyncio.run(_record(r1, 401, now=DAY0))

    before = asyncio.run(stats.read_stats(r1, now=DAY0))

    r2 = FakeRedis(store=store, ttl=ttl)      # 「Redis 重启」：同后端新客户端实例
    after_redis = asyncio.run(stats.read_stats(r2, now=DAY0))
    assert after_redis == before, "Redis 重启（AOF 恢复）后计数不丢"

    # 「service 重启」：stats 模块无进程态，重新读数即恢复
    assert asyncio.run(stats.read_stats(r1, now=DAY0)) == before


# ----------------------------------------------------------------------
# 5) 纪律：计数失败不断业务（假 Redis 断连时 record_call 不抛）
# ----------------------------------------------------------------------

def test_record_call_swallows_redis_failure():
    r = FakeRedis(broken=True)
    asyncio.run(_record(r, 200, now=DAY0))    # 不应抛异常（业务继续返回）
    out = asyncio.run(stats.read_stats(r, now=DAY0))
    assert out["totalCalls"] == 0 and out["todayCalls"] == 0, "读失败按零值降级"


# ----------------------------------------------------------------------
# 6) 日键口径：东八区日期（UTC 傍晚已滚入北京次日）
# ----------------------------------------------------------------------

def test_day_key_uses_utc8():
    assert stats.day_key(DAY0) == "portal:stats:live:day:20260922"
    evening_utc = datetime(2026, 9, 22, 17, 30, 0, tzinfo=UTC)   # 北京 23 日 01:30
    assert stats.day_key(evening_utc) == "portal:stats:live:day:20260923"


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  ✓ " + name)
        except AssertionError as exc:
            print("  ✗ " + name + "：" + str(exc))
            failed.append(name)
    if failed:
        print("== " + str(len(failed)) + " 组失败 ==")
        return 1
    print("== 真实调用统计测试全绿（" + str(len(tests)) + " 组断言）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
