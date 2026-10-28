# -*- coding: utf-8 -*-
"""梯子结果汇总：locust 终表＋docker stats 采样＋审计对账 → 一档一行的 summary CSV＋Markdown 表。

所有数字均来自实测产物（locust 统计/采样文件），零手写；图表与明细同源计算。
P3 通用化：run 目录（含 meta.json）或旧式平铺 results 目录都可读；容器名按
后缀归位（*-apisix/*-service/*-redis），容器名前缀不再硬编码。
用法：python3 loadtest/summarize.py [run_dir] [level ...]
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


def _f(v: str) -> float:
    return float(v) if v not in ("", "N/A", None) else float("nan")


def _mem_to_mib(v: str) -> float:
    v = v.strip()
    num, unit = float(v[:-3]), v[-3:]
    return num * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[unit]


def load_meta(results: Path) -> dict:
    p = results / "meta.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def level_secs(results: Path) -> int:
    """单档窗口秒数：meta.ladder.secs 优先（通用化后口径），缺省 70（旧目录兼容）。"""
    meta = load_meta(results)
    try:
        return int((meta.get("ladder") or {}).get("secs", 70))
    except (TypeError, ValueError):
        return 70


def parse_error_report(results: Path, u: int) -> list[tuple[int, str]]:
    """summary.log 的 Error report 段 → [(次数, 错误文本)]。

    权威口径＝locust 退出时内存终表（与审计 delta 可对平：20261008 冒烟实证
    429 少计 3885 条于 failures.csv，终表 307829 与客户端 2xx＝审计 delta 343 互证）。
    """
    log = results / f"locust_{u}_summary.log"
    if not log.exists():
        return []
    out: list[tuple[int, str]] = []
    in_errors = False
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.startswith("# occurrences"):
            in_errors = True
            continue
        if not in_errors:
            continue
        m = re.match(r"^(\d+)\s+(\S.*)$", line)
        if m:
            out.append((int(m.group(1)), m.group(2)))
        elif line.strip() and not line.lstrip().startswith("-"):
            break  # 段结束（空行/分隔线之后的第一段非数字行）
    return out


def load_fails_429(results: Path, u: int) -> int:
    """429 次数（2026-10-14 分列口径）：Error report 文本含 429 的次数合计。

    与 classify_failures(report_gen) 同源同正则；缺段回落 failures.csv Error 列。
    非限流错误（超时/5xx/连接中断）不计入——有效 RPS 分母只扣真失败。
    """
    import re as _re
    n429 = 0
    for n, text in parse_error_report(results, u):
        if _re.search(r"\b429\b", text):
            n429 += n
    if n429:
        return n429
    fpath = results / f"locust_{u}_failures.csv"
    if not fpath.exists():
        return 0
    with open(fpath, newline="", encoding="utf-8") as f:
        return sum(int(float(r["Occurrences"])) for r in csv.DictReader(f)
                   if r.get("Occurrences") and _re.search(r"\b429\b", r.get("Error", "")))


def load_fail_count(results: Path, u: int) -> int:
    """失败数：summary.log Error report 次数合计（权威）；缺段回落 failures.csv
    Occurrences 列合计（一行＝一种错误类型，次数在列）。

    旧版按 CSV 行数计数，单种错误重复出现时低估（如 429 限流只有一行）——P3 修正；
    冒烟再实证 CSV Occurrences 亦少计尾部错误，故终表优先。
    """
    errs = parse_error_report(results, u)
    if errs:
        return sum(n for n, _ in errs)
    fpath = results / f"locust_{u}_failures.csv"
    if not fpath.exists():
        return 0
    with open(fpath, newline="", encoding="utf-8") as f:
        return sum(int(float(r["Occurrences"])) for r in csv.DictReader(f)
                   if r.get("Occurrences"))


def load_level_stats(results: Path, u: int, secs: int | None = None) -> dict:
    """权威口径＝tee 留存的 locust 终表（locust_{u}_summary.log 末个 Aggregated 行）。

    原因（实测核对）：--csv 的 _stats.csv 是退出前最后周期快照，比终表少 ~0.8s 尾部
    流量（100 档 75379 vs 76293）；终表与审计 delta 相互印证（76293+78≈76371），
    故总数/百分位以终表为准，RPS=总数/档位时长。终表行格式：
    Aggregated, p50, p66, p75, p80, p90, p95, p98, p99, p999, p9999, p100, #reqs
    """
    fails = load_fail_count(results, u)
    fails_429 = load_fails_429(results, u)
    last = None
    with open(results / f"locust_{u}_summary.log", encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.split()]
            if len(parts) >= 13 and parts[0] == "Aggregated":
                last = parts          # 反复出现取最后一次（即最终表）
    if last is None:
        raise SystemExit(f"终表缺失：locust_{u}_summary.log")
    nums = [float(x) for x in last[1:]]
    p50, p90, p95, p99, total = nums[0], nums[4], nums[5], nums[7], int(nums[-1])
    window = secs if secs is not None else level_secs(results)
    return {
        "total": total, "fails": fails, "fails_429": fails_429,
        "error_rate": fails / total if total else float("nan"),
        "rps": total / window,
        # 2026-10-14 分列口径：有效 RPS＝(总数-错误)/窗口（429 是网关拒绝，不是服务吞吐）；
        # 429 RPS/命中率单列，读报告者一眼分辨「总吞吐 vs 真容量 vs 限流占比」。
        "rps_valid": (total - fails) / window if total else float("nan"),
        "rps_429": fails_429 / window,
        "hit_429_pct": (fails_429 / total * 100) if total else float("nan"),
        "p50": p50, "p95": p95, "p99": p99,
        "avg": float("nan"), "max": nums[-2],
    }


def load_tail_rps(results: Path, u: int, tail_rows: int = 5) -> float | None:
    """档位尾部吞吐（stats history 末尾 tail_rows 个时间片均值）——反映衰减后的可持续吞吐。"""
    path = results / f"locust_{u}_stats_history.csv"
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r.get("Name") == "Aggregated"
                and (r.get("Requests/s") or "").replace(".", "", 1).isdigit()]
    if len(rows) < tail_rows:
        return None
    vals = [_f(r["Requests/s"]) for r in rows[-tail_rows:]]
    return sum(vals) / len(vals)


def _container_role(name: str) -> str | None:
    """容器名 → 角色（后缀归位；容器名前缀随栈项目名浮动，不再硬编码）。"""
    for role in ("apisix", "redis"):
        if name.endswith(f"-{role}") or name == role:
            return role
    if name.endswith("-service") or name == "service":
        return "service"
    return None


def load_container_stats(results: Path, u: int) -> dict[str, dict]:
    """读 stats_{u}.csv：每容器 CPU 均值/峰值、内存均值/峰值（MiB）。键＝角色。"""
    per: dict[str, dict] = {}
    path = results / f"stats_{u}.csv"
    if not path.exists():
        return per
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            role = _container_role(row.get("container", ""))
            if not role:
                continue
            d = per.setdefault(role, {"cpu": [], "mem": []})
            try:
                d["cpu"].append(float(row["cpu_pct"].rstrip("%")))
                d["mem"].append(_mem_to_mib(row["mem_used"].split("/")[0].strip()))  # "46.6MiB / 7.7GiB" 取已用段
            except (KeyError, ValueError, IndexError):
                continue  # 半行采样（docker stats 偶发截断）跳过
    return {role: {"cpu_avg": sum(d["cpu"]) / len(d["cpu"]) if d["cpu"] else float("nan"),
                   "cpu_max": max(d["cpu"]) if d["cpu"] else float("nan"),
                   "mem_avg": sum(d["mem"]) / len(d["mem"]) if d["mem"] else float("nan"),
                   "mem_max": max(d["mem"]) if d["mem"] else float("nan"),
                   "n": len(d["cpu"])} for role, d in per.items()}


def load_audit_recon(results: Path) -> dict[int, dict]:
    path = results / "audit_recon_raw.txt"
    out: dict[int, dict] = {}
    if not path.exists():
        return out
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[int(row["level"])] = {k: int(row[k]) for k in ("audit_baseline", "audit_after", "delta")}
    return out


def default_levels(results: Path) -> list[int]:
    """档位缺省：meta.ladder.levels 优先；否则按产物 glob（旧目录兼容）。"""
    meta = load_meta(results)
    lv = (meta.get("ladder") or {}).get("levels") or ""
    if isinstance(lv, str) and lv.strip():
        try:
            return [int(x) for x in lv.replace(",", " ").split()]
        except ValueError:
            pass
    elif isinstance(lv, list) and lv:
        return [int(x) for x in lv]
    return sorted(int(p.stem.split("_")[1]) for p in results.glob("locust_*_stats.csv"))


def main() -> int:
    results = Path(sys.argv[1] if len(sys.argv) > 1 else "loadtest/results")
    levels = [int(x) for x in sys.argv[2:]] or default_levels(results)
    secs = level_secs(results)
    recon = load_audit_recon(results)

    fields = ["level", "req_total", "req_fail", "error_rate_pct",
              "rps_avg", "rps_valid", "rps_429", "hit_429_pct", "rps_tail",
              "p50_ms", "p95_ms", "p99_ms", "avg_ms", "max_ms",
              "audit_delta", "recon_total_minus_audit",
              "apisix_cpu_avg", "apisix_cpu_max", "service_cpu_avg", "service_cpu_max",
              "redis_cpu_avg", "redis_cpu_max",
              "apisix_mem_max_mib", "service_mem_max_mib", "redis_mem_max_mib"]
    out_rows = []
    for u in levels:
        s = load_level_stats(results, u, secs=secs)
        tail = load_tail_rps(results, u)
        cs = load_container_stats(results, u)
        a = recon.get(u, {})
        delta = a.get("delta")
        row = {
            "level": u, "req_total": s["total"], "req_fail": s["fails"],
            "error_rate_pct": round(s["error_rate"] * 100, 4),
            "rps_avg": round(s["rps"], 1),
            "rps_valid": round(s["rps_valid"], 1) if s["rps_valid"] == s["rps_valid"] else "",
            "rps_429": round(s["rps_429"], 1) if s["rps_429"] == s["rps_429"] else "",
            "hit_429_pct": round(s["hit_429_pct"], 2) if s["hit_429_pct"] == s["hit_429_pct"] else "",
            "rps_tail": round(tail, 1) if tail is not None else "",
            "p50_ms": s["p50"], "p95_ms": s["p95"], "p99_ms": s["p99"],
            "avg_ms": round(s["avg"], 1), "max_ms": round(s["max"], 1),
            "audit_delta": delta if delta is not None else "",
            "recon_total_minus_audit": (s["total"] - delta) if delta is not None else "",
        }
        for role in ("apisix", "service", "redis"):
            d = cs.get(role, {})
            row[f"{role}_cpu_avg"] = round(d.get("cpu_avg", float("nan")), 1)
            row[f"{role}_cpu_max"] = round(d.get("cpu_max", float("nan")), 1)
            row[f"{role}_mem_max_mib"] = round(d.get("mem_max", float("nan")), 1)
        out_rows.append(row)

    out_path = results / "ladder_summary.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)

    print(f"已写 {out_path}（窗口 {secs}s/档）\n")
    hdr = ("| 档位 | 请求数 | 错误数 | 错误率 | RPS(总) | RPS(有效) | RPS(429) | 429命中率 | "
           "RPS(尾部) | P50ms | P95ms | P99ms | 审计delta | 对账差(total-审计) |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    print(hdr); print(sep)
    for r in out_rows:
        print(f"| {r['level']} | {r['req_total']} | {r['req_fail']} | "
              f"{r['error_rate_pct']}% | {r['rps_avg']} | {r['rps_valid']} | {r['rps_429']} | "
              f"{r['hit_429_pct']}% | {r['rps_tail']} | "
              f"{r['p50_ms']} | {r['p95_ms']} | {r['p99_ms']} | "
              f"{r['audit_delta']} | {r['recon_total_minus_audit']} |")
    print("\n容器资源（采样均值/峰值）：")
    print("| 档位 | apisix CPU% | service CPU% | redis CPU% | service 内存峰值MiB |")
    print("|---|---|---|---|---|")
    for r in out_rows:
        print(f"| {r['level']} | {r['apisix_cpu_avg']}/{r['apisix_cpu_max']} | "
              f"{r['service_cpu_avg']}/{r['service_cpu_max']} | "
              f"{r['redis_cpu_avg']}/{r['redis_cpu_max']} | {r['service_mem_max_mib']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
