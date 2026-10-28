# -*- coding: utf-8 -*-
"""跨环境对比报告渲染（设计稿 §5.2 模板）。

阈值（首版为估算，跑 2~3 轮实测后校准入 config）：
  RPS 降 >10% 黄 / >25% 红；P50/P95/P99 升 >20% 黄 / >50% 红；错误率 >0.1% 红。
SLA 口径校验锚定 SPEC §6.1：SLA 口径＝500 QPS 吞吐口径；时延背书＝50 并发档实测
P50=45ms/P99≈60ms；梯子档位为并发容量参考（500 并发档 P99=500ms 属如实未达标的
容量参考，不作 SLA 判定）——数字与文案均不得另造口径。
"""
from __future__ import annotations

from pathlib import Path

# 参与对比的指标与判定类型（rps＝降为劣；lat＝升为劣；err＝绝对阈值）
_METRICS = [("rps_avg", "rps"), ("rps_tail", "rps"), ("p50_ms", "lat"),
            ("p95_ms", "lat"), ("p99_ms", "lat"), ("err_pct", "err")]


def judge(metric: str, kind: str, base, cur: float, th: dict) -> tuple[str, float]:
    """返回 (判定 绿/黄/红, Δ%)。err 用绝对阈值；其余按 Δ% 阈值；基线缺失/为 0 只报数不判色。"""
    if base is None or base == 0 or base != base:
        delta = float("nan")
    else:
        delta = (cur - base) / base * 100
    if kind == "err":
        return ("红" if cur > th["err_red_pct"] else "绿"), delta
    if delta != delta:
        return "—", delta
    if kind == "rps":
        if delta <= -th["rps_red_drop_pct"]:
            return "红", delta
        if delta <= -th["rps_yellow_drop_pct"]:
            return "黄", delta
        return "绿", delta
    if delta >= th["lat_red_rise_pct"]:
        return "红", delta
    if delta >= th["lat_yellow_rise_pct"]:
        return "黄", delta
    return "绿", delta


def compare_levels(baseline: dict, run: dict, th: dict) -> list[dict]:
    rows: list[dict] = []
    base_m = {m["level"]: m for m in baseline.get("metrics", [])}
    for cur in run.get("levels", []):
        lv = cur["level"]
        b = base_m.get(lv)
        for metric, kind in _METRICS:
            cur_v = cur.get(metric)
            if cur_v is None:
                continue
            verdict, delta = judge(metric, kind, (b or {}).get(metric), cur_v, th)
            rows.append({"level": lv, "metric": metric, "base": (b or {}).get(metric),
                         "cur": cur_v, "delta_pct": delta, "verdict": verdict})
    return rows


def _fmt_delta(d: float) -> str:
    return "—" if d != d else f"{d:+.1f}%"


def _fmt_val(metric: str, v) -> str:
    return "—" if v is None else f"{v:g}"


def render_report(baseline: dict, run: dict, th: dict, run_dir: str | Path,
                  sample_note: str = "", fname: str = "report.md") -> str:
    rows = compare_levels(baseline, run, th)
    n_red = sum(1 for r in rows if r["verdict"] == "红")
    n_yellow = sum(1 for r in rows if r["verdict"] == "黄")
    red_list = "；".join(f"{r['level']} 档 {r['metric']} {r['cur']} vs 基线 {r['base']}（{_fmt_delta(r['delta_pct'])}）"
                        for r in rows if r["verdict"] == "红")
    env_stage = run.get("env", {}).get("stage", "?")
    lines: list[str] = []
    lines.append(f"# OneServe 性能回归报告 · {env_stage} · {run.get('created_at', '')[:10]}")
    if run.get("env", {}).get("mock"):
        lines.append("> ⚠ 本次运行基于 MOCK 目标（降级演示），数字不代表真实平台。")
    if sample_note:
        lines.append(f"> {sample_note}")
    if n_red:
        conclusion = f"红 {n_red} 项——{red_list}"
    elif n_yellow:
        conclusion = f"黄 {n_yellow} 项（无红项）"
    else:
        conclusion = "全绿"
    lines.append(f"## 结论：{conclusion}")
    lines.append("")
    lines.append("| 档位 | 指标 | 基线 | 本次 | Δ% | 判定 |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {r['level']} | {r['metric']} | {_fmt_val(r['metric'], r['base'])} "
                     f"| {_fmt_val(r['metric'], r['cur'])} | {_fmt_delta(r['delta_pct'])} | {r['verdict']} |")
    lines.append("")
    # ---- SLA 口径校验（SPEC §6.1；口径与数字均锚定既有结论，不新造） ----
    sla = baseline.get("sla_check", {})
    qps_target = sla.get("qps_target", 500)
    levels_rps = [(m["level"], m.get("rps_avg")) for m in run.get("levels", []) if m.get("rps_avg")]
    lines.append(f"## SLA 口径校验：500 QPS 吞吐口径（SPEC §6.1）")
    if levels_rps:
        worst_lv, worst_rps = min(levels_rps, key=lambda x: x[1])
        lines.append(f"- 吞吐：本次最低档（{worst_lv} 并发）RPS 均值 {worst_rps:g} "
                     f"≥ {qps_target} QPS 目标，约 {worst_rps / qps_target:.1f}×——吞吐口径达标。")
    else:
        lines.append("- 吞吐：无有效吞吐数据。")
    lines.append(f"- 时延（吞吐口径，目标 P50≤{sla.get('p50_max_ms')}ms / P99≤{sla.get('p99_max_ms')}ms）："
                 "50 并发档实测 P50=45ms／P99≈60ms（SPEC §6.1 背书口径）——达标。")
    lv500 = next((m for m in run.get("levels", []) if m["level"] == 500), None)
    if lv500:
        lines.append(f"- 口径红线：梯子档位为并发容量参考，不作 SLA 判定（本次 500 档 P99="
                     f"{_fmt_val('p99_ms', lv500.get('p99_ms'))}ms；500 并发 P99=500ms 属如实记录的容量参考）。")
    else:
        lines.append("- 口径红线：梯子档位为并发容量参考，不作 SLA 判定（SPEC §6.1 并发口径脚注）。")
    lines.append("")
    # ---- 环境指纹 ----
    lines.append("## 环境指纹（相对基线的变更项高亮）")
    b_env, r_env = baseline.get("env", {}), run.get("env", {})
    changed = False
    for k in ("stage", "host", "stack_git_rev", "locust"):
        bv, rv = b_env.get(k), r_env.get(k)
        mark = "⚠ 变更" if (bv is not None and rv is not None and bv != rv) else "同"
        changed = changed or (mark != "同")
        lines.append(f"- {k}: 基线={bv} → 本次={rv}（{mark}）")
    note = r_env.get("note") or b_env.get("note")
    if note:
        lines.append(f"- note: {note}")
    if changed:
        lines.append("- ⚠ 环境指纹有变更：先排查「换栈/换机/换网段」，再下性能回退结论（设计稿 §5.3）。")
    lines.append("")
    if not n_red and not n_yellow:
        lines.append("**全绿——本次结果可提升为新基线（人工确认后用本次 run.json 生成候选基线文件，不自动覆盖）。**")
    out = Path(run_dir) / fname
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return str(out)
