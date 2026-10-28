# -*- coding: utf-8 -*-
"""滚动窗指标聚合（设计稿 §4.2）：locust 进程内事件钩子 → 滚动窗（TPS 5s 窗、百分位 60s 窗），
运行中不解析 CSV；跑后终数沿用 loadtest/summarize.py 终表口径（总数/70s、尾部 5 片均值），
两口径差异由面板脚注显式标注，不混报。

移植来源（非 import，复制改写）：loadtest/summarize.py load_level_stats/load_tail_rps
（commit 05ccdd0，M0 证据链原样保留）。
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field


def pct_nearest_rank(sorted_vals: list[int], p: float) -> float:
    """最近秩法百分位（p∈(0,1]）——与实测响应时间分布逐毫秒对齐，不插值。"""
    if not sorted_vals:
        return float("nan")
    idx = max(0, math.ceil(p * len(sorted_vals)) - 1)
    return float(sorted_vals[idx])


@dataclass
class Event:
    t: float
    rt_ms: float | None   # None＝未取到响应时间（连接失败等）
    ok: bool
    name: str = ""


class RollingStats:
    """全部请求事件的时序存储＋滚动窗视图＋档位切片统计（单 greenlet 事件循环内使用，免锁）。"""

    def __init__(self, keep_s: float = 120.0) -> None:
        self.events: deque[Event] = deque()
        self.keep_s = keep_s
        self.tps_history: deque[tuple[float, float]] = deque()  # (t, 5s 窗 TPS) 供火花线
        self._last_spark = 0.0

    # ---- 喂数（locust request 事件钩子） --------------------------------
    def add(self, rt_ms: float | None, ok: bool, exception: object, name: str = "") -> None:
        now = time.time()
        self.events.append(Event(now, rt_ms, ok, name))
        cutoff = now - self.keep_s
        while self.events and self.events[0].t < cutoff:
            self.events.popleft()
        if now - self._last_spark >= 1.0:      # 1s 采样一次火花线
            self._last_spark = now
            self.tps_history.append((now, self.tps(5.0)))
            while self.tps_history and self.tps_history[0][0] < now - 90:
                self.tps_history.popleft()

    # ---- 滚动窗视图（面板） ----------------------------------------------
    def tps(self, window_s: float) -> float:
        cutoff = time.time() - window_s
        return sum(1 for e in self.events if e.t >= cutoff) / window_s

    def percentiles(self, window_s: float = 60.0) -> dict[str, float]:
        cutoff = time.time() - window_s
        vals = sorted(int(e.rt_ms) for e in self.events if e.t >= cutoff and e.rt_ms is not None)
        return {"p50": pct_nearest_rank(vals, 0.50), "p95": pct_nearest_rank(vals, 0.95),
                "p99": pct_nearest_rank(vals, 0.99)}

    def err_rate(self, window_s: float = 60.0) -> float:
        cutoff = time.time() - window_s
        n = sum(1 for e in self.events if e.t >= cutoff)
        bad = sum(1 for e in self.events if e.t >= cutoff and not e.ok)
        return bad / n if n else 0.0

    def cumulative_reqs(self) -> int:
        return len(self.events)

    def sparkline(self, width: int = 9) -> str:
        blocks = "▁▂▃▄▅▆▇█"
        vals = [v for _, v in list(self.tps_history)[-width:]]
        if not vals:
            return ""
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        return "".join(blocks[min(len(blocks) - 1, int((v - lo) / span * (len(blocks) - 1)))] for v in vals)

    # ---- 档位切片（跑后终数，终表口径） -----------------------------------
    def level_result(self, t0: float, t1: float, secs: float, tail_slices: int = 5,
                      names: list[str] | None = None) -> dict:
        """[t0,t1) 窗口统计：RPS=总数/档位时长（summarize.py 同式）；尾部 RPS＝末 N 个整秒桶均值
        （对应 loadtest stats history 末 5 时间片均值口径）。names 非空时只统计该组请求名。"""
        evs = [e for e in self.events if t0 <= e.t < t1 and (names is None or e.name in names)]
        total = len(evs)
        fails = sum(1 for e in evs if not e.ok)
        vals = sorted(int(e.rt_ms) for e in evs if e.rt_ms is not None)
        buckets: dict[int, int] = {}
        for e in evs:
            buckets[int(e.t)] = buckets.get(int(e.t), 0) + 1
        full_secs = sorted(s for s in buckets if s + 1 <= t1)  # 只取完整秒桶
        tail = (sum(buckets[s] for s in full_secs[-tail_slices:]) / len(full_secs[-tail_slices:])
                if len(full_secs) >= tail_slices else float("nan"))
        return {"reqs": total, "fails": fails,
                "err_pct": round(fails / total * 100, 4) if total else float("nan"),
                "rps_avg": round(total / secs, 1) if secs else float("nan"),
                "rps_tail": round(tail, 1) if tail == tail else None,
                "p50_ms": pct_nearest_rank(vals, 0.50), "p95_ms": pct_nearest_rank(vals, 0.95),
                "p99_ms": pct_nearest_rank(vals, 0.99)}
