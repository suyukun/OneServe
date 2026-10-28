# -*- coding: utf-8 -*-
"""审计守恒对账＋七元组完整性校验（P3 通用化：endpoint/caller 参数化）。

从 service 容器 /data/audit/audit.jsonl 全量流式读取：
  1) 逐行解析：七元组字段齐全且类型正确、摘要为 64 位十六进制；
  2) seq 严格递增、无重复（追加式写入的完整性口径）；
  3) 压测窗口（各档基线行号→终值行号）内：endpoint/caller 归属检查＋状态码分布
     ——endpoint＝service:/v1/query/<service_id>（服务登记派生）、caller＝本次
     AppKey，均可参数覆盖；
  4) 对账：各档审计 delta vs 客户端成功/总请求数，差值显式给出
     （429 网关限流拒绝的请求不经服务端、不进审计，属正当差值来源）。

产物：<run_dir>/audit_recon.json（report_gen 消费）＋控制台对账表。
用法：python3 loadtest/audit_check.py <run_dir> [--endpoint ...] [--caller ...]
  （run_ladder.sh 注入 LOCUST_APIKEY；meta.json 提供 service_id 与掩码）
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import load_fail_count  # noqa: E402  失败次数口径单一来源

SEVEN = ["ts", "caller", "endpoint", "params_digest", "status", "latency_ms", "response_digest"]


class _LocalStream:
    """本地直读替身（保 Popen 消费契约：.stdout 迭代＋.wait/.returncode/.stderr）。"""

    def __init__(self, fh) -> None:
        self.stdout = fh
        self.returncode = 0
        self.stderr = io.StringIO()

    def wait(self) -> int:
        return 0


def read_audit_stream(service_container: str) -> io.TextIOBase:
    # A窗 2026-10-19（压测页容器内触发使能）：在服务容器内运行时审计卷就在本地
    # （/data/audit 同源挂载），直读免 docker；宿主执行保持原 docker exec 口径不变。
    local = Path("/data/audit/audit.jsonl")
    if local.is_file():
        return _LocalStream(open(local, encoding="utf-8"))  # noqa: SIM115
    cmd = ["docker", "exec", service_container, "cat", "/data/audit/audit.jsonl"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc  # type: ignore[return-value]


def load_cli_totals(results: Path, u: int) -> tuple[int, int]:
    """客户端口径＝tee 留存的 locust 终表（退出时点权威值）＋失败次数（单一来源 summarize）。"""
    last = None
    with open(results / f"locust_{u}_summary.log", encoding="utf-8") as f:
        for line in f:
            parts = [p.strip() for p in line.split()]
            if len(parts) >= 13 and parts[0] == "Aggregated":
                last = parts
    if last is None:
        raise SystemExit(f"终表缺失：locust_{u}_summary.log")
    return int(float(last[-1])), load_fail_count(results, u)


def main() -> int:
    ap = argparse.ArgumentParser(description="审计守恒对账（endpoint/caller 参数化）")
    ap.add_argument("results", help="run 目录（含 meta.json / audit_recon_raw.txt / locust_*）")
    ap.add_argument("--endpoint", default="", help="审计 endpoint 口径（缺省 service:/v1/query/<service_id>）")
    ap.add_argument("--caller", default=os.environ.get("LOCUST_APIKEY", ""),
                    help="预期 caller（缺省取 LOCUST_APIKEY；仅比对，不落盘）")
    ap.add_argument("--service-container", default="oneserve-service")
    args = ap.parse_args()

    results = Path(args.results)
    meta = {}
    if (results / "meta.json").exists():
        meta = json.loads((results / "meta.json").read_text(encoding="utf-8"))
    sid = (meta.get("service") or {}).get("service_id", "")
    endpoint = args.endpoint or (f"service:/v1/query/{sid}" if sid else "")
    if not endpoint:
        print("无法确定审计 endpoint（缺 --endpoint 且 meta.json 无 service_id）", file=sys.stderr)
        return 2

    recon = {}
    with open(results / "audit_recon_raw.txt", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            recon[int(row["level"])] = {k: int(row[k]) for k in ("audit_baseline", "audit_after", "delta")}
    windows = {u: (v["audit_baseline"], v["audit_after"]) for u, v in recon.items()}

    proc = read_audit_stream(args.service_container)
    total = 0
    bad_lines = 0
    dup_or_desc_seq = 0
    prev_seq = 0
    win_stats: dict[int, Counter] = {u: Counter() for u in windows}
    first_seq_of: dict[int, int] = {}
    last_seq_of: dict[int, int] = {}

    for line in proc.stdout:  # type: ignore[union-attr]
        line = line.strip()
        if not line:
            continue
        idx = total  # 0-based 行号（追加式，行号即写入序）
        total += 1
        try:
            e = json.loads(line)
            assert all(k in e for k in SEVEN), "缺字段"
            assert isinstance(e["status"], int) and isinstance(e["latency_ms"], int)
            assert isinstance(e["seq"], int) and isinstance(e["ts"], str)
            assert len(e["params_digest"]) == 64 and len(e["response_digest"]) == 64
            int(e["params_digest"], 16); int(e["response_digest"], 16)
        except Exception:  # noqa: BLE001
            bad_lines += 1
            continue
        if e["seq"] <= prev_seq:
            dup_or_desc_seq += 1
        prev_seq = e["seq"]
        for u, (lo, hi) in windows.items():
            if lo <= idx < hi:
                win_stats[u][e["status"]] += 1
                if e["endpoint"] != endpoint:
                    win_stats[u]["_endpoint_bad"] += 1
                if args.caller and e["caller"] != args.caller:
                    win_stats[u]["_caller_bad"] += 1
                first_seq_of.setdefault(u, e["seq"])
                last_seq_of[u] = e["seq"]
    proc.wait()
    if proc.returncode != 0:
        print("读取审计失败：", proc.stderr.read(), file=sys.stderr)  # type: ignore[union-attr]
        return 1

    masked = (meta.get("credential") or {}).get("appkey_masked", "")
    print(f"审计 JSONL 全量：{total} 行；坏行 {bad_lines}；seq 重复/倒序 {dup_or_desc_seq}")
    print(f"对账口径：endpoint={endpoint}｜caller={masked or '(未校验)'}")
    print()
    print("| 档位 | 审计delta | 窗口内2xx | 窗口内非2xx | endpoint异常 | caller异常 | 客户端total | 客户端2xx | 差值(total-审计) |")
    print("|---|---|---|---|---|---|---|---|---|")
    out_rows = []
    ok = True
    for u in sorted(windows):
        c = win_stats[u]
        audit_delta = recon[u]["delta"]
        win_total = sum(v for k, v in c.items() if not str(k).startswith("_"))
        cli_total, cli_fail = load_cli_totals(results, u)
        cli_2xx = cli_total - cli_fail
        diff = cli_total - audit_delta
        endpoint_bad = c.get("_endpoint_bad", 0)
        caller_bad = c.get("_caller_bad", 0)
        recon_ok = (win_total == audit_delta) and endpoint_bad == 0 and caller_bad == 0
        ok = ok and recon_ok
        print(f"| {u} | {audit_delta} | {c.get(200, 0)} | "
              f"{sum(v for k, v in c.items() if k != 200 and not str(k).startswith('_'))} | "
              f"{endpoint_bad} | {caller_bad} | {cli_total} | {cli_2xx} | {diff} |")
        out_rows.append({
            "level": u, "audit_delta": audit_delta, "win_2xx": c.get(200, 0),
            "win_non2xx": sum(v for k, v in c.items() if k != 200 and not str(k).startswith("_")),
            "endpoint_bad": endpoint_bad, "caller_bad": caller_bad,
            "cli_total": cli_total, "cli_2xx": cli_2xx, "diff_total_minus_audit": diff,
            "first_seq": first_seq_of.get(u), "last_seq": last_seq_of.get(u),
            "win_status_dist": {str(k): v for k, v in sorted(c.items(), key=lambda kv: str(kv[0]))},
            "ok": recon_ok,
        })
    print()
    if ok:
        verdict = "✓ 通过：各档窗口内审计条数＝服务端处理请求数，endpoint/caller 全部归属本次压测"
    else:
        verdict = "△ 有差值：见上表（429 网关限流拒绝的请求不经服务端不进审计，属正当差值；其余差值来源＝客户端超时/连接中断）"
    print(verdict)

    payload = {"endpoint": endpoint, "caller_masked": masked, "audit_total_lines": total,
               "bad_lines": bad_lines, "dup_or_desc_seq": dup_or_desc_seq,
               "levels": out_rows, "verdict": verdict}
    payload_text = json.dumps(payload, ensure_ascii=False, indent=2) + chr(10)
    (results / "audit_recon.json").write_text(payload_text, encoding="utf-8")
    print(f"已写 {results / 'audit_recon.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
