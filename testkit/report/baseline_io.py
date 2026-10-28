# -*- coding: utf-8 -*-
"""基线 JSON 读写＋首版基线转换（设计稿 §5.1）。

schema oneserve.testkit.baseline/1；文件名 <env.stage>-<YYYYMMDD>.json 落 testkit/baselines/。
首版基线＝从 loadtest/results/（2026-09-20 造数·09-21 实测四档）转换，数据复用不重跑。

终表解析口径移植自 loadtest/summarize.py（commit 05ccdd0）：
  - 权威总数/百分位＝locust_{u}_summary.log 末个 Aggregated 百分位行（_stats.csv 快照少 ~0.8s 尾部）；
  - 失败数＝locust_{u}_failures.csv 行数；RPS＝总数/70s；
  - 尾部 RPS＝stats history 末 5 个 Aggregated 时间片均值。
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCHEMA = "oneserve.testkit.baseline/1"
CST = timezone(timedelta(hours=8))

# loadtest 09-20 造数四档实测的环境指纹（git log + summary.log 时间戳核对）
LOADTEST_MEASURED_AT = "2026-09-21T09:04:22+08:00"   # locust_100_summary.log 首行
LOADTEST_STACK_REV = "02036ae"                        # 压测时段栈 rev（设计稿 §5.1 示例同源）


def _load_level_stats(results: Path, u: int, secs: float = 70) -> dict:
    """终表口径（移植自 summarize.load_level_stats，非 import）。"""
    fails = 0
    fpath = results / f"locust_{u}_failures.csv"
    if fpath.exists():
        with open(fpath, newline="", encoding="utf-8") as f:
            fails = sum(1 for _ in f) - 1
    last = None
    with open(results / f"locust_{u}_summary.log", encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.split()]
            if len(parts) >= 13 and parts[0] == "Aggregated":
                last = parts
    if last is None:
        raise FileNotFoundError(f"终表缺失：locust_{u}_summary.log")
    nums = [float(x) for x in last[1:]]
    p50, p95, p99, total = nums[0], nums[5], nums[7], int(nums[-1])
    return {"reqs": total, "fails": fails,
            "err_pct": round(fails / total * 100, 4) if total else float("nan"),
            "rps_avg": round(total / secs, 1), "p50_ms": p50, "p95_ms": p95, "p99_ms": p99}


def _load_tail_rps(results: Path, u: int, tail_rows: int = 5):
    path = results / f"locust_{u}_stats_history.csv"
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r.get("Name") == "Aggregated"
                and re.fullmatch(r"[\d.]+", r.get("Requests/s") or "")]
    if len(rows) < tail_rows:
        return None
    vals = [float(r["Requests/s"]) for r in rows[-tail_rows:]]
    return round(sum(vals) / len(vals), 1)


def convert_from_loadtest(results_dir: str | Path, levels: list[int] | None = None,
                          secs: float = 70, env_stage: str = "本机M0",
                          host: str = "http://localhost:9080") -> dict:
    """loadtest/results CSV → 基线 dict（schema/1）。"""
    results = Path(results_dir)
    if levels is None:
        levels = sorted(int(p.stem.split("_")[1]) for p in results.glob("locust_*_stats.csv"))
    metrics = []
    for u in levels:
        s = _load_level_stats(results, u, secs)
        s["rps_tail"] = _load_tail_rps(results, u)
        metrics.append({"level": u, **s})
    return {
        "schema": SCHEMA,
        "created_at": datetime.now(CST).isoformat(timespec="seconds"),
        "promoted_by": "待 维护者 确认（首版＝loadtest/results 转换稿）",
        "env": {"stage": env_stage, "host": host, "stack_git_rev": LOADTEST_STACK_REV,
                "locust": "2.46.6",
                "note": "Docker Desktop macOS，数字不可跨环境比"},
        "scenario": "regression",
        "metrics": metrics,
        "sla_check": {"qps_target": 500, "p50_max_ms": 50, "p99_max_ms": 200,
                      "basis": "SPEC §6.1 吞吐口径；时延背书＝50 并发档实测 P50=45ms/P99≈60ms"},
        "source": {"type": "loadtest-csv-conversion", "dir": str(results),
                   "measured_at": LOADTEST_MEASURED_AT,
                   "note": "2026-09-20 造数·09-21 实测四档；数据复用不重跑（设计稿 §5.1）"},
    }


def write_baseline(baseline: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_baseline(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest_baseline(baselines_dir: str | Path, env_stage: str) -> Path | None:
    """同环境最新基线（<env>-<YYYYMMDD>.json 取日期最大；-candidate 视同候选不算正式）。"""
    d = Path(baselines_dir)
    if not d.exists():
        return None
    cands = sorted(p for p in d.glob(f"{env_stage}-????????.json"))
    return cands[-1] if cands else None


def baseline_metric(baseline: dict, level: int) -> dict | None:
    for m in baseline.get("metrics", []):
        if m.get("level") == level:
            return m
    return None


def baseline_to_run(baseline: dict, levels: list[int] | None = None, note: str = "") -> dict:
    """把基线数据包装成 run dict（同源自比，用于演示对比管线；数字零改动）。"""
    metrics = baseline["metrics"]
    if levels:
        metrics = [m for m in metrics if m["level"] in levels]
    return {"schema": "oneserve.testkit.run/1",
            "created_at": datetime.now(CST).isoformat(timespec="seconds"),
            "env": dict(baseline["env"]),
            "scenario": baseline.get("scenario", "regression"),
            "levels": metrics,
            "notes": [note or "样例：与基线同源（loadtest 09-21 实测自比），仅演示对比管线"]}


def run_path_for_write(reports_dir: str | Path, tag: str) -> Path:
    d = Path(reports_dir) / f"{datetime.now(CST).strftime('%Y%m%d-%H%M%S')}-{tag}"
    d.mkdir(parents=True, exist_ok=True)
    return d
