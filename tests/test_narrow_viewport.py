#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窄屏（≤768px）适配三卡静态断言（tests/test_narrow_viewport.py）。

背景（docs/reviews/narrow-viewport-20261015/结论.md，2026-10-15）：375px 五页
（index/catalog/admin/load/admin/appkeys/my）全横向溢出。实证根因：
  - ≤1024 断点虽已把侧栏转顶部，但 .os-nav-list 未转横排——导航组被排成竖列
    （两列并排，「管理」组 8 项整列推出视口右缘）；
  - admin/appkeys 凭证表 11 列，窄屏「授权服务」列出视口；
  - 密钥掩码/URL/字段名等连续长串无换行兜底。

三卡守卫口径（2026-10-15 批，最小实现＝纯 CSS/既有工具类，零 JS）：
  卡1 侧栏断点：portal.css ≤768px——侧栏收为顶部横滚单行导航：组/列表全转
     横排＋flex-wrap:nowrap，溢出收进 .os-nav 自身 overflow-x:auto（页面级
     恒无横向溢出）；侧栏全宽、分组标签隐藏、脚注隐藏；
  卡2 宽表滚动：appkeys 凭证表包 .table-scroll 横滚容器（既有模式，portal.css）
     ——真源 admin/appkeys.html 与产物 portal/admin/appkeys.html 同步持有；
  卡3 长文本兜底：.os-break-anywhere 工具类（overflow-wrap:anywhere）＋
     ≤768px 断点对 .mono/.svc .api/.svc .desc/.kpi-note/.src-note 生效。

注：375px 运行期结论（Chrome headless 逐页 scrollWidth<=375）由复测截图＋
CDP 断言出证（docs/reviews/narrow-viewport-20261015/ 与 tests/evidence/），
不入本套件；本套件只锁「断点/容器/兜底类存在且产物同步」的静态事实。

留卡补遗（2026-10-15，394bfab 批按 390px 验收漏掉的两处存量）：
补① trial 控件自适应：select 固有最小宽（最长 option，2026-10-14 新发布服务
    名变长后≈360px）把 ≤720px flex line 撑到 362px——trial.html 375px 实测
    sw=395；修＝.trial-control / .trial-input / .trial-select 补 max-width:100%；
补② 详情页首表（服务标识）包 .table-scroll：th nowrap＋长 service_id 的固有
    表宽（375px 视口下表格自身 375px＋卡片左边距 33px）——5 张详情页实测
    sw=408/409；修＝真源 src/detail.html 首表补包裹，产物 portal/catalog/*.html
    构建同步。

走查修复批补遗（2026-10-15，docs/reviews/全站走查_20261015.md）：
补③ monitor 6 表包 .table-scroll：桌面 1280 数据就绪后 6 张表固有宽度撑破
    页面（scrollWidth=1587，溢出 307px；375px 反而无溢出——≤720 断点在位，
    问题仅在桌面~中屏）；修＝真源 admin/monitor.html 6 表补包裹＋内联样式
    自带横滚定义（本页不载 portal.css），产物 portal/admin/monitor.html 构建
    同步；运行期 1280/375 数据就绪复测由 CDP 出证（tests/evidence/
    walkthrough-fixes-20261015/）。
运行：python3 -m pytest tests/test_narrow_viewport.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
PORTAL_CSS = PORTAL / "assets" / "portal.css"
ADMIN_SRC = REPO / "admin" / "appkeys.html"        # 管理页唯一真源（build_portal.py 抽取）
ADMIN_OUT = REPO / "portal" / "admin" / "appkeys.html"  # 构建产物（nginx 挂载伺服）


def _media_block(css: str, query: str) -> str:
    """按花括号配平截取整段 @media 块文本（守卫其内部规则，防「块存在但内容被掏空」）。"""
    m = re.search(re.escape(query) + r"\s*\{", css)
    assert m, f"portal.css 缺 {query} 媒体查询块"
    depth, i = 0, m.end() - 1
    while i < len(css):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[m.start():i + 1]
        i += 1
    raise AssertionError(f"{query} 块花括号不配平")


def test_card1_sidebar_breakpoint_768():
    """卡1：≤768px 侧栏收为顶部横滚单行导航（断点存在且关键规则齐全）。"""
    css = PORTAL_CSS.read_text(encoding="utf-8")
    block = _media_block(css, "@media (max-width: 768px)")
    # 侧栏转全宽顶部条（高度/定位复位，不再占左列）
    assert ".os-sidebar" in block and "width: 100%" in block and "height: auto" in block
    # 横滚导航：单行不换行＋溢出收进自身（页面恒无横向溢出的结构保证）
    assert ".os-nav" in block and "flex-wrap: nowrap" in block and "overflow-x: auto" in block
    # 根因修复：导航列表也转横排（≤1024 块漏改＝竖列双排、管理组整列出视口）
    assert re.search(r"\.os-nav-list\s*\{[^}]*flex-direction:\s*row", block), \
        "≤768px 块必须把 .os-nav-list 转横排（评审实证根因）"
    # 收纳：分组标签/脚注隐藏；active 指示转下划线
    assert ".os-nav-label" in block and "display: none" in block
    assert ".os-sidebar-foot" in block
    assert "border-bottom-color" in block


def test_card1_breakpoint_covers_375():
    """卡1：断点口径覆盖 375px（768≥375，且与既有 1024/720 断点并存不互斥）。"""
    css = PORTAL_CSS.read_text(encoding="utf-8")
    for q in ("@media (max-width: 1024px)", "@media (max-width: 768px)", "@media (max-width: 720px)"):
        assert q in css, f"{q} 缺失（既有断点不许被本批挤掉）"


def test_card2_appkeys_table_scroll_container():
    """卡2：凭证表包 .table-scroll 横滚容器——真源与构建产物同步持有且顺序正确。"""
    for path in (ADMIN_SRC, ADMIN_OUT):
        html = path.read_text(encoding="utf-8")
        wrap = html.find('<div class="table-scroll">')
        table = html.find('<table id="appkey-table"')
        close_table = html.find("</table>", table)
        close_div = html.find('</div><!-- /.table-scroll -->', close_table)
        assert 0 <= wrap < table, f"{path}：.table-scroll 容器必须在 #appkey-table 之前开"
        assert close_table < close_div, f"{path}：容器必须在 </table> 之后闭"
    # 既有模式本身仍在 portal.css（overflow-x:auto 横滚定义不许丢）
    css = PORTAL_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto", css)


def test_card3_break_anywhere_utility():
    """卡3：长串兜底类存在，且 ≤768px 对等宽串/已知长文本位生效。"""
    css = PORTAL_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.os-break-anywhere\s*\{[^}]*overflow-wrap:\s*anywhere", css), \
        "缺 .os-break-anywhere 工具类（overflow-wrap:anywhere）"
    block = _media_block(css, "@media (max-width: 768px)")
    for sel in (".mono", ".kpi-note", ".src-note"):
        assert sel in block, f"≤768px 块缺 {sel} 的 overflow-wrap 兜底"
    assert "overflow-wrap: anywhere" in block


def test_built_pages_ship_portal_css():
    """产物页确实引用 portal.css（断点改动真正上线到 nginx 挂载目录）。"""
    pages = [
        PORTAL / "index.html",
        PORTAL / "catalog.html",
        PORTAL / "my.html",
        PORTAL / "admin" / "load.html",
        PORTAL / "admin" / "appkeys.html",
    ]
    for p in pages:
        html = p.read_text(encoding="utf-8")
        rel = 'href="assets/portal.css"' if "admin/" not in str(p) else 'href="../assets/portal.css"'
        # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（rel 去闭引号拼接）
        assert re.search(re.escape(rel[:-1]) + r'(\?v=[0-9a-f]+)?"', html), \
            f"{p} 未引用 portal.css（{rel}）"


TRIAL_CSS = PORTAL / "assets" / "trial.css"
DETAIL_SRC = REPO / "portal" / "src" / "detail.html"   # 详情页唯一真源（构建模板）
CATALOG_OUT = PORTAL / "catalog"                      # 构建产物（nginx 挂载伺服）


def test_leftover_trial_controls_adaptive():
    """补①：trial 控件自适应——max-width:100% 防回潮（select 固有最小宽撑破 flex line）。"""
    css = TRIAL_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.trial-control\s*\{[^}]*max-width:\s*100%", css), \
        ".trial-control 缺 max-width:100%（≤720px 列向 stretch 曾被最长 option 撑到 362px）"
    m = re.search(r"\.trial-input,\s*\.trial-select\s*\{[^}]*\}", css)
    assert m and re.search(r"max-width:\s*100%", m.group(0)), \
        ".trial-input/.trial-select 缺 max-width:100%（表单控件不随卡片自适应）"


def test_leftover_detail_ident_table_scroll():
    """补②：详情页首表（服务标识）包 .table-scroll——真源与全部构建产物同步持有。"""
    paths = [DETAIL_SRC, *sorted(CATALOG_OUT.glob("*.html"))]
    assert len(paths) >= 2, "详情页真源或产物缺失"
    for path in paths:
        html = path.read_text(encoding="utf-8")
        i = html.find('id="detail-identification"')
        assert i >= 0, f"{path}：缺 #detail-identification 区块"
        sec = html[i:html.find("</section>", i)]
        wrap = sec.find('<div class="table-scroll">')
        table = sec.find("<table")
        close_table = sec.find("</table>", table)
        assert 0 <= wrap < table, f"{path}：首表缺 .table-scroll 包裹（开）"
        assert close_table >= 0 and close_table < sec.find("</div>", close_table), \
            f"{path}：首表 .table-scroll 包裹未闭合"


# ── 走查修复批补③（2026-10-15）：monitor 页宽表横滚守卫（走查 P1-2）──

MONITOR_SRC = REPO / "admin" / "monitor.html"           # 管理页唯一真源（build_portal.py 抽取）
MONITOR_OUT = PORTAL / "admin" / "monitor.html"         # 构建产物（nginx 挂载伺服）
MONITOR_TABLES = (                                      # 数据就绪后固有宽度可破 1280 的 7 张表
    "monitor-drill-caller",
    "monitor-drill-api",
    "monitor-table",
    "monitor-trace-table",
    "monitor-batch-counts",
    "monitor-batch-recent",
    "monitor-containers-table",   # 蓝图 1a 监控最小集（2026-10-20）：常驻容器健康一览
)


def test_monitor_wide_tables_scroll_containers():
    """补③：monitor 6 张表包 .table-scroll——真源与产物同步持有且各表落位正确。

    背景（docs/reviews/全站走查_20261015.md P1-2）：桌面 1280 数据就绪后
    scrollWidth=1587（溢出 307px），页内 6 表（th nowrap＋长 mono 值的固有表宽）
    全无包裹。断言不变式：容器开/闭计数＝6（配平）；每张表开标签之前有未闭合的
    .table-scroll 容器、</table> 之后该容器才闭合；横滚定义本体（overflow-x:auto）
    在真源内联样式与 portal.css 双在位——monitor 不载 portal.css，独立打开态也须横滚。
    """
    for path in (MONITOR_SRC, MONITOR_OUT):
        html = path.read_text(encoding="utf-8")
        opens = html.count('<div class="table-scroll">')
        closes = html.count('</div><!-- /.table-scroll -->')
        assert opens == closes == len(MONITOR_TABLES), \
            f"{path}：.table-scroll 包裹数失衡（open={opens} close={closes} 应 {len(MONITOR_TABLES)}）"
        for tid in MONITOR_TABLES:
            table = html.find(f'<table id="{tid}"')
            assert table >= 0, f"{path}：缺表 #{tid}"
            wrap = html.rfind('<div class="table-scroll">', 0, table)
            last_close = html.rfind('</div><!-- /.table-scroll -->', 0, table)
            assert 0 <= wrap < table and last_close < wrap, \
                f"{path}：#{tid} 未落在 .table-scroll 容器内（容器必须先开后关）"
            close_table = html.find("</table>", table)
            close_div = html.find('</div><!-- /.table-scroll -->', close_table)
            assert close_table < close_div, f"{path}：#{tid} 的容器必须在 </table> 之后闭"
    # 横滚定义双在位：真源内联样式（独立态）＋ portal.css（壳产物态，既有模式不许丢）
    src = MONITOR_SRC.read_text(encoding="utf-8")
    assert re.search(r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto", src), \
        "admin/monitor.html 内联样式缺 .table-scroll{overflow-x:auto}（独立打开态不横滚）"
    css = PORTAL_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto", css)


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
