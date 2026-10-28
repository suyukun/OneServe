# -*- coding: utf-8 -*-
"""自动生成专业压测报告（P3 通用化 · 对抗复核 P0/P1 修正）：读本梯子 run 目录 →
performance-report.md ＋ performance-report.html（同一数据渲染双格式；HTML 为
版本管理式页面铺路：门户 tokens 配色、自包含单文件、无外部依赖）。

输入（全部由 run_ladder.sh 产出的实测产物，零手写）：
  meta.json            环境与被测事实（discover.py 写：服务/路由/凭证掩码/键池/配额）
  keys.txt             取样键池
  locust_<U>_summary.log / _failures.csv / _stats_history.csv   每档客户端指标
  stats_<U>.csv        docker stats 采样（apisix/service/redis）
  audit_recon_raw.txt  每档审计基线/终值
  audit_recon.json     audit_check.py 对账明细
  probe.txt            静置探针

报告固定六节：①环境与被测 ②四档结果表（09-21 报表列口径）③容器资源表
④审计守恒对账表 ⑤静置探针 ⑥结论与建议（AC「点查 P99<200ms」逐档自动判定＋
瓶颈观察＋一句话建议）。置顶固定声明（P0-3/P1-7）：
  - 共享栈压测声明：本机 M0 无隔离栈，结果含门户/管理流量干扰，尾部延迟供参考；
  - 延迟百分位唯一口径＝locust 客户端实测；审计 latency_ms 实测样本为 0
    （整数毫秒精度损失，不可信），不引用。

用法：python3 loadtest/report_gen.py <run_dir>
纯函数 render_report(data) / render_html(data) 可单测（loadtest/test_loadtest_tools.py）。
"""
from __future__ import annotations

import csv
import datetime as _dt
import html as _html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from summarize import (default_levels, load_audit_recon, load_container_stats,  # noqa: E402
                       load_level_stats, load_meta, load_tail_rps, parse_error_report)

AC_P99_MS = 200.0          # AC：点查 P99 < 200ms（SPEC/SLA 口径）
AC_ERR_PCT = 0.1           # 参考阈值：错误率 ≤0.1%（SLA 试测口径，随行展示）


def _fmt(v, nd=1) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    if f != f:  # NaN
        return "—"
    return f"{f:.{nd}f}"


def _pct(v, nd=2) -> str:
    return _fmt(v, nd) + "%"


def mask_secret(v: str) -> str:
    if not v:
        return ""
    if len(v) <= 11:
        return v[:2] + "****"
    return f"{v[:7]}****{v[-4:]}"


def classify_failures(results: Path, u: int) -> dict[str, int]:
    """错误构成：按 HTTP 状态码归并次数（限流/5xx/超时分类展示用）。

    口径与失败数同源＝summary.log Error report（权威）；缺段回落 failures.csv。
    """
    by: dict[str, int] = {}
    for n, text in parse_error_report(results, u):
        m = re.search(r"\b(\d{3})\b", text)
        key = m.group(1) if m else "other"
        by[key] = by.get(key, 0) + n
    if by:
        return by
    fpath = results / f"locust_{u}_failures.csv"
    if not fpath.exists():
        return by
    with open(fpath, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            n = int(float(row.get("Occurrences") or 0))
            m = re.search(r"\b(\d{3})\b", row.get("Error", ""))
            key = m.group(1) if m else "other"
            by[key] = by.get(key, 0) + n
    return by


def build_report_data(run_dir: Path) -> dict:
    """读 run 目录全部实测产物 → render_report 的数据包（IO 只在这一层）。"""
    meta = load_meta(run_dir)
    levels = default_levels(run_dir)
    recon_raw = load_audit_recon(run_dir)
    svc = meta.get("service") or {}
    cred = meta.get("credential") or {}
    redis_info = meta.get("redis") or {}
    ladder = meta.get("ladder") or {}

    level_rows = []
    for u in levels:
        s = load_level_stats(run_dir, u)
        tail = load_tail_rps(run_dir, u)
        cs = load_container_stats(run_dir, u)
        delta = recon_raw.get(u, {}).get("delta")
        level_rows.append({
            "level": u, "total": s["total"], "fails": s["fails"],
            "fails_429": s.get("fails_429", 0),
            "err_pct": s["error_rate"] * 100 if s["error_rate"] == s["error_rate"] else None,
            "rps": s["rps"], "rps_valid": s.get("rps_valid"), "rps_tail": tail,
            "p50": s["p50"], "p95": s["p95"], "p99": s["p99"], "max": s["max"],
            "audit_delta": delta,
            "fail_by_status": classify_failures(run_dir, u),
            "containers": cs,
        })

    audit = None
    aj = run_dir / "audit_recon.json"
    if aj.exists():
        audit = json.loads(aj.read_text(encoding="utf-8"))

    probes = []
    pt = run_dir / "probe.txt"
    if pt.exists():
        with open(pt, newline="", encoding="utf-8") as f:
            probes = list(csv.DictReader(f))

    keys_n = meta.get("keys_sampled")
    if keys_n is None:
        kp = run_dir / "keys.txt"
        keys_n = sum(1 for ln in kp.read_text(encoding="utf-8").splitlines() if ln.strip()) if kp.exists() else 0

    return {
        "generated_at": _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "run_dir": str(run_dir),
        "meta": meta, "svc": svc, "cred": cred, "redis": redis_info, "ladder": ladder,
        "keys_sampled": keys_n,
        "levels": level_rows, "audit": audit, "probes": probes,
    }


def render_report(d: dict) -> str:
    """数据包 → 六节 Markdown（纯函数，模板固定）。"""
    svc, cred, redis_info, ladder = d["svc"], d["cred"], d["redis"], d["ladder"]
    lv = d["ladder"].get("levels", "")
    L: list[str] = []
    L.append(f"# 压测报告 · {svc.get('name') or svc.get('service_id', '')}")
    L.append("")
    # P0-3 隔离声明：置顶（MD/HTML 同文）
    L.append("> **共享栈压测声明：本机 M0 无隔离栈，结果含门户/管理流量干扰，尾部延迟供参考。**")
    L.append("")
    L.append(f"> 服务 {svc.get('service_id')}｜{svc.get('method')} {svc.get('route')}"
             f"｜档位 {lv}｜生成 {d['generated_at']}｜工具 loadtest P3（report_gen.py，数字全部来自实测产物）")
    L.append("")

    # ---------- 1. 环境与被测 ----------
    L.append("## 1. 环境与被测")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 服务 | {svc.get('name')}（{svc.get('service_id')}，{svc.get('status') or '—'}） |")
    L.append(f"| 被测地址 | {d['meta'].get('host') or '—'} → {svc.get('method')} {svc.get('route')}（网关 key-auth） |")
    src = cred.get("source") or "—"
    L.append(f"| 凭证 | {cred.get('appkey_masked') or '—'}（来源 {src}；secret 只经内存/环境，不落盘） |")
    if cred.get("quota_qps") is not None:
        L.append(f"| 凭证配额 | quota_qps={cred.get('quota_qps')}｜quota_daily={cred.get('quota_daily')}"
                 f"｜status={cred.get('status')}｜expires={cred.get('expires_at')} |")
    gate = d["meta"].get("gate") or {}
    if gate:
        g = "✓ grants 含目标服务（授权门禁通过）"
        if gate.get("quota_below_level"):
            g += f"；⚠ quota_qps={gate.get('quota_qps')} 低于最低档位 {gate.get('min_level')}"
            g += "（--force 强制放行，429 单独计数）" if gate.get("forced") else "（未放行即止步）"
        L.append(f"| 授权门禁 | {g} |")
    L.append(f"| 键池 | 取样 {d['keys_sampled']} 键（{redis_info.get('live_version') or '全部版本'}，"
             f"来源 Redis SCAN {redis_info.get('scan_pattern') or redis_info.get('namespace')}；"
             f"池文件 {d['meta'].get('keys_file') or 'keys.txt'}） |")
    L.append(f"| 表数据量 | Redis 键 {redis_info.get('redis_keys_total', '—')}（数据键 {redis_info.get('data_keys', '—')}"
             f"·唯一实体 {redis_info.get('unique_entities', '—')}；{redis_info.get('version_note') or '—'}） |")
    L.append(f"| 时序口径 | 预热 {ladder.get('warmup_users')} 并发×{ladder.get('warmup_secs')}s（不计入统计）"
             f"＋每档起压 {ladder.get('ramp')}s＋全并发 {(ladder.get('secs') or 0) - (ladder.get('ramp') or 0)}s |")
    L.append("")

    # ---------- 2. 四档结果表 ----------
    L.append("## 2. 四档结果表")
    L.append("")
    L.append("| 档位 | 请求数 | 错误数 | 错误率 | RPS(总) | RPS(有效) | 429次 | 429命中率 | RPS(尾部) | P50ms | P95ms | P99ms | 审计delta |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in d["levels"]:
        hit429 = (r.get("fails_429", 0) / r["total"] * 100) if r.get("total") else None
        L.append(f"| {r['level']} | {r['total']} | {r['fails']} | {_pct(r['err_pct']) if r['err_pct'] is not None else '—'} "
                 f"| {_fmt(r['rps'])} | {_fmt(r.get('rps_valid'))} | {r.get('fails_429', 0)} | "
                 f"{_pct(hit429) if hit429 is not None else '—'} | {_fmt(r['rps_tail'])} | {_fmt(r['p50'])} | {_fmt(r['p95'])} | "
                 f"{_fmt(r['p99'])} | {r['audit_delta'] if r['audit_delta'] is not None else '—'} |")
    L.append("")
    err_note = []
    for r in d["levels"]:
        by = r["fail_by_status"]
        if by:
            parts = ", ".join(f"{k}×{v}" for k, v in sorted(by.items()))
            err_note.append(f"- 档位 {r['level']} 错误构成：{parts}")
    if err_note:
        L.append("错误构成（按 HTTP 状态码）：")
        L.extend(err_note)
        L.append("")
    L.append("口径：总数/百分位＝locust 退出终表（权威值）；RPS(总)＝总数/档位窗口；有效 RPS＝(总数-错误)/窗口"
             "（429 是网关拒绝、不是服务吞吐——分列看真容量，2026-10-14）；429 命中率＝429次/总数；"
             "尾部 RPS＝stats history 末 5 个时间片均值；错误数＝summary.log Error report 次数合计"
             "（缺段回落 failures.csv Occurrences）。客户端超时 30s；429＝网关限流拒绝（不经服务端、不进审计）。")
    L.append("")
    L.append("**延迟双口径声明（P1-7）：P50/P95/P99 唯一判定口径＝locust 客户端实测；"
             "审计 latency_ms 实测样本为 0（整数毫秒精度损失，不可信），本报告不引用审计延迟。**")
    L.append("")

    # ---------- 3. 容器资源表 ----------
    L.append("## 3. 容器资源表（docker stats 采样，2s 周期）")
    L.append("")
    L.append("| 档位 | apisix CPU%(均/峰) | service CPU%(均/峰) | redis CPU%(均/峰) | service 内存峰值MiB |")
    L.append("|---|---|---|---|---|")
    for r in d["levels"]:
        c = r["containers"]
        def _c(role, k): return _fmt((c.get(role) or {}).get(k))
        L.append(f"| {r['level']} | {_c('apisix','cpu_avg')}/{_c('apisix','cpu_max')} "
                 f"| {_c('service','cpu_avg')}/{_c('service','cpu_max')} "
                 f"| {_c('redis','cpu_avg')}/{_c('redis','cpu_max')} | {_c('service','mem_max')} |")
    L.append("")
    L.append("注：docker stats 采样本身有少量开销，属本机环境局限（单机自压自测，网络路径含本机回环）。")
    L.append("")

    # ---------- 4. 审计守恒对账表 ----------
    L.append("## 4. 审计守恒对账表")
    L.append("")
    audit = d["audit"]
    if audit:
        L.append(f"审计全量 {audit.get('audit_total_lines')} 行｜坏行 {audit.get('bad_lines')}"
                 f"｜seq 重复/倒序 {audit.get('dup_or_desc_seq')}｜口径 endpoint={audit.get('endpoint')}"
                 f"｜caller={audit.get('caller_masked') or '(未校验)'}")
        L.append("")
        L.append("| 档位 | 审计delta | 窗口内2xx | 窗口内非2xx | endpoint异常 | caller异常 | 客户端total | 客户端2xx | 差值(total-审计) |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in audit.get("levels", []):
            L.append(f"| {r['level']} | {r['audit_delta']} | {r['win_2xx']} | {r['win_non2xx']} "
                     f"| {r['endpoint_bad']} | {r['caller_bad']} | {r['cli_total']} | {r['cli_2xx']} "
                     f"| {r['diff_total_minus_audit']} |")
        L.append("")
        L.append(audit.get("verdict") or "")
    else:
        L.append("（audit_recon.json 缺失——先跑 loadtest/audit_check.py <run_dir>；"
                 "原始对账值在 audit_recon_raw.txt）")
    L.append("")
    L.append("差值来源：客户端口径计所有发出请求；服务端只审计实际到达并处理的请求——"
             "网关 429 限流拒绝、客户端超时/连接中断的请求两者天然有差，逐档显式列出。")
    L.append("")

    # ---------- 5. 静置探针 ----------
    L.append("## 5. 静置探针（梯子结束静置后单发计时）")
    L.append("")
    if d["probes"]:
        L.append("| 时间 | 探针 | HTTP | 耗时s |")
        L.append("|---|---|---|---|")
        for p in d["probes"]:
            L.append(f"| {p.get('ts')} | {p.get('probe')} | {p.get('http_code')} | {p.get('time_total_s')} |")
        codes = {p.get("http_code") for p in d["probes"]}
        lat = [float(p["time_total_s"]) for p in d["probes"] if p.get("time_total_s")]
        note = f"全部 2xx" if all(c and str(c).startswith("2") for c in codes) else f"状态码 {sorted(str(c) for c in codes)}"
        if lat:
            note += f"；单发时延 {min(lat)*1000:.0f}~{max(lat)*1000:.0f}ms（无并发压力基线）"
        L.append("")
        L.append(note)
    else:
        L.append("（probe.txt 缺失）")
    L.append("")

    # ---------- 6. 结论与建议 ----------
    L.append("## 6. 结论与建议")
    L.append("")
    L.append(f"AC 自动判定（点查 P99<{int(AC_P99_MS)}ms；错误率参考阈 {AC_ERR_PCT}%）：")
    L.append("")
    L.append("| 档位 | P99ms | P99<200ms | 错误率 | 判定 |")
    L.append("|---|---|---|---|---|")
    all_pass = True
    for r in d["levels"]:
        p99_ok = r["p99"] == r["p99"] and r["p99"] < AC_P99_MS
        err_ok = (r["err_pct"] is None) or (r["err_pct"] <= AC_ERR_PCT)
        ok = p99_ok and err_ok
        all_pass = all_pass and ok
        L.append(f"| {r['level']} | {_fmt(r['p99'])} | {'✓' if p99_ok else '✗'} "
                 f"| {_pct(r['err_pct']) if r['err_pct'] is not None else '—'} | {'✓ 达标' if ok else '✗ 未达标'} |")
    L.append("")

    # 瓶颈观察（自动）
    obs: list[str] = []
    rate_limited = any(int(v) for r in d["levels"] for k, v in r["fail_by_status"].items() if k == "429")
    if rate_limited:
        obs.append("- 限流是当前绑定约束：错误主要由 HTTP 429 构成（网关按凭证 quota_qps 拒绝），"
                   "此时 P99 反映的是被限流整形后的排队/拒绝口径，不代表后端容量上限。")
    for r in d["levels"]:
        c = r["containers"]
        hot = max(((role, (v or {}).get("cpu_max", float("nan"))) for role, v in c.items()),
                  key=lambda kv: (kv[1] == kv[1], kv[1] if kv[1] == kv[1] else -1), default=None)
        if hot and hot[1] == hot[1] and hot[1] >= 90:
            obs.append(f"- 档位 {r['level']}：{hot[0]} CPU 峰值 {hot[1]:.0f}%（≥90%，贴近单核饱和）。")
    svc_mem = [((r["containers"].get("service") or {}).get("mem_max")) for r in d["levels"]]
    # A窗 2026-10-19：容器内触发形态无资源采样（mem_max 缺席＝None）——原 NaN 滤波放行 None 致崩
    svc_mem = [m for m in svc_mem if m == m and m is not None]
    if svc_mem:
        obs.append(f"- service 内存峰值 {max(svc_mem):.0f}MiB（窗口内未见趋势性上涨即无泄漏迹象，以多档对比为准）。")
    if not obs:
        obs.append("- 各档容器 CPU/内存峰值均未触顶，未见单点饱和。")
    L.append("瓶颈观察：")
    L.extend(obs)
    L.append("")

    # 一句话建议（自动）
    if rate_limited:
        advice = ("建议：先为压测凭证临时调高 quota_qps（控制台签发/修订），再重跑本梯子测后端真实容量；"
                  "限流行为本身可用当前结果作为网关配额生效的佐证。")
    elif all_pass and d["levels"]:
        advice = "建议：全档达标，可按业务峰值放大梯子（如 1000/1500）或延长窗口复测稳定性。"
    else:
        bad = [r["level"] for r in d["levels"]
               if not (r["p99"] == r["p99"] and r["p99"] < AC_P99_MS
                       and ((r["err_pct"] is None) or (r["err_pct"] <= AC_ERR_PCT)))]
        advice = f"建议：档位 {bad} 未达标，按 §3 资源热点优先扩容对应组件后复测。"
    L.append(advice)
    L.append("")
    L.append("---")
    L.append(f"*产物目录 {d['run_dir']}：locust CSV/summary、stats 采样、审计对账原始值与明细均在该目录，本报告可由 "
             f"report_gen.py 从产物完整再生。*")
    L.append("")
    return "\n".join(L)


# ---------- HTML 渲染（P0-3/P1-7 同文声明；门户 tokens 配色，自包含单文件） ----------

_HTML_CSS = '''
/* OneServe 门户 tokens.css 取值（浅色口径）内联——报告自包含单文件，无外部依赖 */
:root{--brand:#C8102E;--ok:#009423;--err:#CB272D;--warn:#D4760A;--info:#2563eb;
--bg:#fafafa;--card:#ffffff;--inset:#f0f0f0;--border:#d9d9d9;--border-light:#f0f0f0;
--text:#434343;--strong:#262626;--muted:#8c8c8c}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:13px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei','Segoe UI',sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:24px 24px 8px}
h1{font-size:20px;color:var(--strong);margin:0}
.sub{color:var(--muted);font-size:12px;margin:4px 0 16px}
.banner{background:var(--card);border:1px solid var(--border);border-left:4px solid var(--brand);
border-radius:8px;box-shadow:0 2px 8px rgba(20,20,20,.08);padding:12px 16px;margin-bottom:16px;
color:var(--strong);font-weight:600}
section{background:var(--card);border:1px solid var(--border);border-radius:8px;
box-shadow:0 2px 8px rgba(20,20,20,.08);padding:16px 24px;margin:0 0 16px}
h2{font-size:16px;color:var(--strong);margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:12px;margin:0 0 8px}
th{text-align:left;color:var(--muted);font-weight:600;border-bottom:1px solid var(--border);
padding:6px 10px;white-space:nowrap}
td{border-bottom:1px solid var(--border-light);padding:6px 10px;vertical-align:top}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-variant-numeric:tabular-nums;white-space:nowrap}
.ok{color:var(--ok);font-weight:600}.err{color:var(--err);font-weight:600}
.warn{color:var(--warn);font-weight:600}
.note{color:var(--muted);font-size:12px;margin:8px 0 0}
.decl{color:var(--strong);font-weight:600;margin:8px 0 0}
ul.obs{margin:8px 0 0;padding-left:18px}
footer{color:var(--muted);font-size:11px;padding:4px 0 24px}
'''


def _esc(v) -> str:
    return _html.escape('' if v is None else str(v), quote=True)


def _cell(v, num: bool = False) -> str:
    '''单元格：转义＋✓/✗ 语义着色（门户状态色：绿＝正常 红＝异常）。'''
    s = _esc(v)
    s = s.replace('✓', '<span class="ok">✓</span>').replace('✗', '<span class="err">✗</span>')
    cls = ' class="num"' if num else ''
    if not s:
        s = '&mdash;'
    return f'<td{cls}>{s}</td>'


def _html_table(headers: list, rows: list, num_cols: frozenset = frozenset()) -> str:
    out = ['<table>', '<thead><tr>']
    out.extend(f'<th>{_esc(h)}</th>' for h in headers)
    out.append('</tr></thead><tbody>')
    for row in rows:
        out.append('<tr>' + ''.join(_cell(v, i in num_cols) for i, v in enumerate(row)) + '</tr>')
    out.append('</tbody></table>')
    return ''.join(out)


def _gate_text(gate: dict) -> str:
    g = '✓ grants 含目标服务（授权门禁通过）'
    if gate.get('quota_below_level'):
        g += f'；⚠ quota_qps={gate.get("quota_qps")} 低于最低档位 {gate.get("min_level")}'
        g += '（--force 强制放行，429 单独计数）' if gate.get('forced') else '（未放行即止步）'
    return g


def render_html(d: dict) -> str:
    '''同一数据包 → 自包含 HTML（与 render_report 同源同文：声明置顶＋六节）。'''
    svc, cred, redis_info, ladder = d['svc'], d['cred'], d['redis'], d['ladder']
    lv = ladder.get('levels', '')
    title = _esc(svc.get('name') or svc.get('service_id', ''))
    H: list[str] = []
    H.append('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">')
    H.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    H.append(f'<title>压测报告 · {title}</title>')
    H.append(f'<style>{_HTML_CSS}</style></head><body><div class="wrap">')
    H.append(f'<h1>压测报告 · {title}</h1>')
    H.append(f'<div class="sub">服务 {_esc(svc.get("service_id"))}｜{_esc(svc.get("method"))} {_esc(svc.get("route"))}'
             f'｜档位 {_esc(lv)}｜生成 {_esc(d["generated_at"])}｜工具 loadtest P3（report_gen.py，数字全部来自实测产物）</div>')
    H.append('<div class="banner">共享栈压测声明：本机 M0 无隔离栈，结果含门户/管理流量干扰，尾部延迟供参考。</div>')

    # 1 环境与被测
    env_rows = [
        ['服务', f'{svc.get("name")}（{svc.get("service_id")}，{svc.get("status") or "—"}）'],
        ['被测地址', f'{d["meta"].get("host") or "—"} → {svc.get("method")} {svc.get("route")}（网关 key-auth）'],
        ['凭证', f'{cred.get("appkey_masked") or "—"}（来源 {cred.get("source") or "—"}；secret 只经内存/环境，不落盘）'],
    ]
    if cred.get('quota_qps') is not None:
        env_rows.append(['凭证配额', f'quota_qps={cred.get("quota_qps")}｜quota_daily={cred.get("quota_daily")}'
                                    f'｜status={cred.get("status")}｜expires={cred.get("expires_at")}'])
    gate = d['meta'].get('gate') or {}
    if gate:
        env_rows.append(['授权门禁', _gate_text(gate)])
    env_rows.append(['键池', f'取样 {d["keys_sampled"]} 键（{redis_info.get("live_version") or "全部版本"}，'
                          f'来源 Redis SCAN {redis_info.get("scan_pattern") or redis_info.get("namespace")}；'
                          f'池文件 {d["meta"].get("keys_file") or "keys.txt"}）'])
    env_rows.append(['表数据量', f'Redis 键 {redis_info.get("redis_keys_total", "—")}'
                            f'（数据键 {redis_info.get("data_keys", "—")}·唯一实体 {redis_info.get("unique_entities", "—")}；'
                            f'{redis_info.get("version_note") or "—"}）'])
    env_rows.append(['时序口径', f'预热 {ladder.get("warmup_users")} 并发×{ladder.get("warmup_secs")}s（不计入统计）'
                            f'＋每档起压 {ladder.get("ramp")}s＋全并发 {(ladder.get("secs") or 0) - (ladder.get("ramp") or 0)}s'])
    H.append('<section><h2>1. 环境与被测</h2>')
    H.append(_html_table(['项', '值'], env_rows))
    H.append('</section>')

    # 2 结果表
    def _hit429(r):
        return _pct(r.get('fails_429', 0) / r['total'] * 100) if r.get('total') else '—'
    rows2 = [[r['level'], r['total'], r['fails'],
              _pct(r['err_pct']) if r['err_pct'] is not None else '—',
              _fmt(r['rps']), _fmt(r.get('rps_valid')), r.get('fails_429', 0), _hit429(r),
              _fmt(r['rps_tail']), _fmt(r['p50']), _fmt(r['p95']), _fmt(r['p99']),
              r['audit_delta'] if r['audit_delta'] is not None else '—'] for r in d['levels']]
    H.append('<section><h2>2. 四档结果表</h2>')
    H.append(_html_table(['档位', '请求数', '错误数', '错误率', 'RPS(总)', 'RPS(有效)', '429次', '429命中率',
                          'RPS(尾部)', 'P50ms', 'P95ms', 'P99ms', '审计delta'], rows2,
                         num_cols=frozenset(range(1, 13))))
    err_parts = ['档位 ' + str(r['level']) + '：' + ', '.join(f'{k}×{v}' for k, v in sorted(r['fail_by_status'].items()))
                 for r in d['levels'] if r['fail_by_status']]
    if err_parts:
        H.append(f'<p class="note">错误构成（按 HTTP 状态码）——{"；".join(err_parts)}</p>')
    H.append('<p class="note">口径：总数/百分位＝locust 退出终表（权威值）；RPS(总)＝总数/档位窗口；有效 RPS＝(总数-错误)/窗口'
             '（429 是网关拒绝、不是服务吞吐——分列看真容量，2026-10-14）；429 命中率＝429次/总数；'
             '尾部 RPS＝stats history 末 5 个时间片均值；错误数＝summary.log Error report 次数合计'
             '（缺段回落 failures.csv Occurrences）。客户端超时 30s；429＝网关限流拒绝（不经服务端、不进审计）。</p>')
    H.append('<p class="decl">延迟双口径声明（P1-7）：P50/P95/P99 唯一判定口径＝locust 客户端实测；'
             '审计 latency_ms 实测样本为 0（整数毫秒精度损失，不可信），本报告不引用审计延迟。</p>')
    H.append('</section>')

    # 3 容器资源
    rows3 = []
    for r in d['levels']:
        c = r['containers']
        def _c(role, k): return _fmt((c.get(role) or {}).get(k))
        rows3.append([r['level'], f'{_c("apisix", "cpu_avg")}/{_c("apisix", "cpu_max")}',
                      f'{_c("service", "cpu_avg")}/{_c("service", "cpu_max")}',
                      f'{_c("redis", "cpu_avg")}/{_c("redis", "cpu_max")}', _c('service', 'mem_max')])
    H.append('<section><h2>3. 容器资源表（docker stats 采样，2s 周期）</h2>')
    H.append(_html_table(['档位', 'apisix CPU%(均/峰)', 'service CPU%(均/峰)', 'redis CPU%(均/峰)',
                          'service 内存峰值MiB'], rows3))
    H.append('<p class="note">注：docker stats 采样本身有少量开销，属本机环境局限（单机自压自测，网络路径含本机回环）。</p>')
    H.append('</section>')

    # 4 审计对账
    H.append('<section><h2>4. 审计守恒对账表</h2>')
    audit = d['audit']
    if audit:
        H.append(f'<p>审计全量 {audit.get("audit_total_lines")} 行｜坏行 {audit.get("bad_lines")}'
                 f'｜seq 重复/倒序 {audit.get("dup_or_desc_seq")}｜口径 endpoint={_esc(audit.get("endpoint"))}'
                 f'｜caller={_esc(audit.get("caller_masked")) or "(未校验)"}</p>')
        rows4 = [[r['level'], r['audit_delta'], r['win_2xx'], r['win_non2xx'], r['endpoint_bad'],
                  r['caller_bad'], r['cli_total'], r['cli_2xx'], r['diff_total_minus_audit']]
                 for r in audit.get('levels', [])]
        H.append(_html_table(['档位', '审计delta', '窗口内2xx', '窗口内非2xx', 'endpoint异常',
                              'caller异常', '客户端total', '客户端2xx', '差值(total-审计)'], rows4,
                             num_cols=frozenset(range(0, 9))))
        H.append(f'<p class="note">{_esc(audit.get("verdict") or "")}</p>')
    else:
        H.append('<p>（audit_recon.json 缺失——先跑 loadtest/audit_check.py &lt;run_dir&gt;；原始对账值在 audit_recon_raw.txt）</p>')
    H.append('<p class="note">差值来源：客户端口径计所有发出请求；服务端只审计实际到达并处理的请求——'
             '网关 429 限流拒绝、客户端超时/连接中断的请求两者天然有差，逐档显式列出。</p>')
    H.append('</section>')

    # 5 静置探针
    H.append('<section><h2>5. 静置探针（梯子结束静置后单发计时）</h2>')
    if d['probes']:
        rows5 = [[p.get('ts'), p.get('probe'), p.get('http_code'), p.get('time_total_s')] for p in d['probes']]
        H.append(_html_table(['时间', '探针', 'HTTP', '耗时s'], rows5, num_cols={3}))
    else:
        H.append('<p>（probe.txt 缺失）</p>')
    H.append('</section>')

    # 6 结论与建议
    H.append('<section><h2>6. 结论与建议</h2>')
    H.append(f'<p>AC 自动判定（点查 P99&lt;{int(AC_P99_MS)}ms；错误率参考阈 {AC_ERR_PCT}%）：</p>')
    rows6, all_pass = [], True
    for r in d['levels']:
        p99_ok = r['p99'] == r['p99'] and r['p99'] < AC_P99_MS
        err_ok = (r['err_pct'] is None) or (r['err_pct'] <= AC_ERR_PCT)
        ok = p99_ok and err_ok
        all_pass = all_pass and ok
        rows6.append([r['level'], _fmt(r['p99']), '✓' if p99_ok else '✗',
                      _pct(r['err_pct']) if r['err_pct'] is not None else '—',
                      '✓ 达标' if ok else '✗ 未达标'])
    H.append(_html_table(['档位', 'P99ms', 'P99<200ms', '错误率', '判定'], rows6, num_cols={1, 3}))
    obs: list[str] = []
    rate_limited = any(int(v) for r in d['levels'] for k, v in r['fail_by_status'].items() if k == '429')
    if rate_limited:
        obs.append('限流是当前绑定约束：错误主要由 HTTP 429 构成（网关按凭证 quota_qps 拒绝），'
                   '此时 P99 反映的是被限流整形后的排队/拒绝口径，不代表后端容量上限。')
    for r in d['levels']:
        c = r['containers']
        hot = max(((role, (v or {}).get('cpu_max', float('nan'))) for role, v in c.items()),
                  key=lambda kv: (kv[1] == kv[1], kv[1] if kv[1] == kv[1] else -1), default=None)
        if hot and hot[1] == hot[1] and hot[1] >= 90:
            obs.append(f'档位 {r["level"]}：{hot[0]} CPU 峰值 {hot[1]:.0f}%（≥90%，贴近单核饱和）。')
    svc_mem = [((r['containers'].get('service') or {}).get('mem_max')) for r in d['levels']]
    # A窗 2026-10-19：同上（HTML 渲染器）——None 滤除
    svc_mem = [m for m in svc_mem if m == m and m is not None]
    if svc_mem:
        obs.append(f'service 内存峰值 {max(svc_mem):.0f}MiB（窗口内未见趋势性上涨即无泄漏迹象，以多档对比为准）。')
    if not obs:
        obs.append('各档容器 CPU/内存峰值均未触顶，未见单点饱和。')
    H.append('<p>瓶颈观察：</p><ul class="obs">')
    H.extend(f'<li>{_esc(o)}</li>' for o in obs)
    H.append('</ul>')
    if rate_limited:
        advice = ('建议：先为压测凭证临时调高 quota_qps（控制台签发/修订），再重跑本梯子测后端真实容量；'
                  '限流行为本身可用当前结果作为网关配额生效的佐证。')
    elif all_pass and d['levels']:
        advice = '建议：全档达标，可按业务峰值放大梯子（如 1000/1500）或延长窗口复测稳定性。'
    else:
        bad = [r['level'] for r in d['levels']
               if not (r['p99'] == r['p99'] and r['p99'] < AC_P99_MS
                       and ((r['err_pct'] is None) or (r['err_pct'] <= AC_ERR_PCT)))]
        advice = f'建议：档位 {bad} 未达标，按 §3 资源热点优先扩容对应组件后复测。'
    H.append(f'<p class="decl">{_esc(advice)}</p>')
    H.append('</section>')

    H.append(f'<footer>产物目录 {_esc(d["run_dir"])}：locust CSV/summary、stats 采样、审计对账原始值与明细均在该目录；'
             f'本页与 performance-report.md 为同一数据渲染，可由 report_gen.py 从产物完整再生。</footer>')
    H.append('</div></body></html>')
    return ''.join(H)

def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("loadtest/results")
    data = build_report_data(run_dir)
    md = run_dir / "performance-report.md"
    html_out = run_dir / "performance-report.html"
    md.write_text(render_report(data), encoding="utf-8")
    html_out.write_text(render_html(data), encoding="utf-8")
    print(f"已写 {md}")
    print(f"已写 {html_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
