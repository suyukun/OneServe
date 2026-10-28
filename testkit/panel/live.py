# -*- coding: utf-8 -*-
"""实时面板（设计稿 §4.1 布局／§4.3 必显六数＋降级链）。

降级链：rich 可用且终端交互 → rich Live 面板；rich 缺失或非交互终端 → ANSI 单行刷新
（每 2s 一行 TPS/P99/错误率）；再不行 --only-summary 事后表。
面板文案口径红线：吞吐口径（SLA 承诺，SPEC §6.1＝500 QPS）与并发档容量参考严格区分。
"""
from __future__ import annotations

import sys
import time
from typing import Callable

try:  # rich 装于 testkit/lib（requirements.txt；勿装全局）
    import rich.console
    import rich.live
    import rich.panel
    import rich.text
    _HAS_RICH = True
except ImportError:  # pragma: no cover - 降级链第二级
    _HAS_RICH = False

SPARK = "▁▂▃▄▆█▇█"


def verdict_color(kind: str, delta_pct: float, thresholds: dict) -> tuple[str, str]:
    """绿黄红判定：kind＝rps（降为劣）/lat（升为劣）。返回 (判定字, rich 样式)。NaN＝样本不足，不判色。"""
    if delta_pct != delta_pct:
        return "…", "dim"
    if kind == "rps":
        if delta_pct <= -thresholds["rps_red_drop_pct"]:
            return "红", "bold red"
        if delta_pct <= -thresholds["rps_yellow_drop_pct"]:
            return "黄", "bold yellow"
        return "绿", "bold green"
    if delta_pct >= thresholds["lat_red_rise_pct"]:
        return "红", "bold red"
    if delta_pct >= thresholds["lat_yellow_rise_pct"]:
        return "黄", "bold yellow"
    return "绿", "bold green"


def _bar(delta_pct: float, thresholds: dict, kind: str, width: int = 18) -> str:
    if delta_pct != delta_pct:      # 样本不足（起压初期）：空条
        return "▏"
    span = thresholds["rps_red_drop_pct" if kind == "rps" else "lat_red_rise_pct"]
    n = 1 + int(min(abs(delta_pct) / span, 1.0) * (width - 1))
    return "▏" + "█" * n


class PanelContext:
    """driver 每个阶段回调更新；面板只读渲染。"""

    def __init__(self, title: str, env_stage: str, mock: bool, baseline_name: str | None,
                 thresholds: dict, footnote: str = "") -> None:
        self.title = title
        self.env_stage = env_stage
        self.mock = mock
        self.baseline_name = baseline_name      # None＝无基线（面板显示「首跑建基线」）
        self.thresholds = thresholds
        self.phase = "初始化"
        self.next_hint = ""                     # 如「下一档 300 并发 8s 后」
        self.level_now: int | None = None
        self.level_plan: list[int] = []
        self.users = 0
        self.started = time.time()
        self.footnote = footnote
        # 基线对照条（driver 更新）：{"tps": (cur, base, delta_pct), "p99": (...)}
        self.baseline_rows: dict[str, tuple[float, float, float] | None] = {"tps": None, "p99": None}
        # S3 双用户组分列（scenarios/ratelelimit 计数器驱动）
        self.group_line: str | None = None


class BasePanel:
    def __init__(self, ctx: PanelContext, stats) -> None:
        self.ctx = ctx
        self.stats = stats

    def tick(self) -> None:  # pragma: no cover - 由子类实现节奏
        raise NotImplementedError

    def close(self) -> None:
        pass


class AnsiPanel(BasePanel):
    """降级面板：每 2s 一行（rich 缺失／非交互终端）。"""

    def __init__(self, ctx, stats, interval: float = 2.0) -> None:
        super().__init__(ctx, stats)
        self.interval = interval

    def tick(self) -> None:
        p = self.stats.percentiles(60.0)
        mock = "[MOCK] " if self.ctx.mock else ""
        base = ""
        row = self.ctx.baseline_rows.get("tps")
        if row:
            cur, ref, d = row
            base = f" 基线TPS {ref:.1f}({d:+.1f}%)"
        group = f" {self.ctx.group_line}" if self.ctx.group_line else ""
        print(f"{mock}{time.strftime('%H:%M:%S')} TPS {self.stats.tps(5.0):7.1f} "
              f"P50 {p['p50']:.0f}ms P95 {p['p95']:.0f}ms P99 {p['p99']:.0f}ms "
              f"错率 {self.stats.err_rate(60.0) * 100:.2f}% 并发 {self.ctx.users}"
              f"{base}{group}｜{self.ctx.phase}", flush=True)
        time.sleep(self.interval)


class RichPanel(BasePanel):
    """rich Live 面板（设计稿 §4.1 示意布局）；结束时可选导出 SVG 留证。"""

    def __init__(self, ctx, stats, svg_path: str | None = None, refresh_hz: float = 2.0) -> None:
        super().__init__(ctx, stats)
        self.console = rich.console.Console(record=True)
        self.live = rich.live.Live(console=self.console, refresh_per_second=refresh_hz)
        self.svg_path = svg_path
        self._refresh = 1.0 / refresh_hz

    def _render(self) -> "rich.text.Text":
        t = rich.text.Text()
        c = self.ctx
        p = self.stats.percentiles(60.0)
        elapsed = int(time.time() - c.started)
        mock_tag = "[MOCK 降级模式] " if c.mock else ""
        levels_done = [x for x in c.level_plan if c.level_now is not None and x < c.level_now]
        head = (f"{c.title} │ env={c.env_stage}{mock_tag} │ 已跑 {elapsed // 60:02d}:{elapsed % 60:02d}"
                f" │ 档 {'/'.join(str(x) for x in [c.level_now] if c.level_now) or '—'}"
                f"（计划 {'→'.join(map(str, c.level_plan))}）")
        t.append(head + "\n", style="bold")
        spark = self.stats.sparkline()
        t.append(f"TPS   {self.stats.tps(5.0):7.1f}   {spark or '…':<9}（5s 窗 · 累计 {self.stats.cumulative_reqs()} 请求）\n")
        t.append(f"P50 {p['p50']:.0f}ms │ P95 {p['p95']:.0f}ms │ P99 {p['p99']:.0f}ms │ "
                 f"错误率 {self.stats.err_rate(60.0) * 100:.2f}% │ 并发 {c.users}\n")
        if c.baseline_name:
            t.append(f"基线对比（vs {c.baseline_name}）：\n")
            for label, key, fmt, kind in (("TPS", "tps", "{:.1f}", "rps"), ("P99", "p99", "{:.0f}ms", "lat")):
                row = c.baseline_rows.get(key)
                if not row:
                    continue
                cur, ref, delta = row
                word, style = verdict_color(kind, delta, c.thresholds)
                bar = _bar(delta, c.thresholds, kind)
                t.append(f"  {label} {fmt.format(cur)} vs {fmt.format(ref)}  {delta:+.1f}% ")
                t.append(bar + " ", style=style)
                t.append(word + "\n", style=style)
        else:
            t.append("基线对比：无（首跑建基线）\n", style="dim")
        if c.group_line:
            t.append(f"[S3 限流] {c.group_line}\n")
        foot = f"状态：{c.phase}" + (f" · {c.next_hint}" if c.next_hint else "")
        t.append(foot + "\n", style="bold")
        t.append((c.footnote or "").rstrip() + "\n", style="dim")
        return t

    def tick(self) -> None:
        self.live.update(rich.panel.Panel(self._render(), border_style="cyan",
                                          title="OneServe testkit", expand=False))
        time.sleep(self._refresh)

    def close(self) -> None:
        try:
            if self.svg_path:
                self.console.save_svg(self.svg_path, title="OneServe testkit 面板留证")
        except Exception:  # noqa: BLE001 —— 留证失败不影响主流程
            pass


def make_panel(ctx: PanelContext, stats, mode: str, svg_path: str | None = None) -> BasePanel | None:
    """mode＝live/headless/only-summary；live 下按降级链选择实现。返回 None＝无实时输出。"""
    if mode == "only-summary":
        return None
    if mode != "live":
        return None
    if _HAS_RICH and sys.stdout.isatty():
        panel = RichPanel(ctx, stats, svg_path=svg_path)
    else:
        panel = AnsiPanel(ctx, stats)
    return panel
