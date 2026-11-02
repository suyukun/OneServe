#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-1 审计查询门户套件（tests/test_audit_portal.py）——构建注册＋DOM＋组件契约。

守卫口径（任务卡：新页 portal/src/audit.html 侧栏「合规」组；行展示八元组
（trace_id 可点反查）；统计摘要卡；只读面；SPEC §2-C1/§5-P1-1）：
  1. 构建注册：audit 入 NAV_KEYS/PAGES（区块 id/按页 css+js/无越页注入）；构建幂等；
  2. DOM：八元组八列表头（audit.py 真源七列＋trace_id 第八列）；过滤控件齐全
     （时间窗快捷＋自定义/caller/endpoint/状态/追踪号/每页）；统计摘要三卡＋
     耗时 Top；只读角标；分页控件；trace_id 可点反查（useTrace）；
  3. 侧栏：「合规」组独立成组且含 audit 链接（C 面 IA），active 态构建期注入；
  4. 组件契约（audit.js）：取数直达 /v1/portal/audit/logs、Alpine 注册、深链参数、
     小基数「样本量不足」文案、无 mock 降级；
  5. 链路：详情壳子目录链接改写（../audit.html）；外链红线/占位符残留零容忍。
运行：python3 -m pytest tests/test_audit_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
AUDIT_PAGE = PORTAL / "audit.html"

if not AUDIT_PAGE.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


class _TagText(HTMLParser):
    """极简 DOM 断言器（test_build_portal 同款，无第三方依赖）。"""

    def __init__(self):
        super().__init__()
        self.tags = []
        self.texts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(data.strip())


def _dom(html: str) -> _TagText:
    p = _TagText()
    p.feed(html)
    return p


def _has_text(dom: _TagText, needle: str) -> bool:
    return any(needle in t for t in dom.texts)


# ── 1. 构建注册 ──

def test_pages_registered_in_build():
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "audit" in bp.NAV_KEYS and "audit" in bp.PAGES
    assert bp.PAGES["audit"]["required_ids"] == ["audit-toolbar", "audit-summary", "audit-table"]
    assert "assets/audit.css" in bp.PAGES["audit"]["extra_css"]
    assert "assets/audit.js" in bp.PAGES["audit"]["extra_js"]


def test_page_rendered_with_shell_and_active_nav():
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    assert not re.search(r"@@[A-Za-z0-9_]+@@", html), "残留模板占位符"
    assert 'aria-current="page" href="audit.html"' in html, "侧栏 active 态未注入"
    assert "js-canary" in html, "JS 错误探针缺失"
    assert 'x-data="auditQueryPage"' in html, "Alpine 组件未挂载"
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not externals, "外链红线：" + str(externals)


def test_required_blocks_present():
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    for block_id in ("audit-toolbar", "audit-summary", "audit-table"):
        assert ('id="' + block_id + '"') in html, "缺关键区块 id：" + block_id


def test_per_page_assets_not_leaked():
    for other in ("index.html", "my.html", "ops.html"):   # docs.html 随 ux-polish 2026-11-02 下线移出
        html = (PORTAL / other).read_text(encoding="utf-8")
        assert "assets/audit.css" not in html and "assets/audit.js" not in html,             other + " 被注入审计页资产"


def test_build_idempotent():
    before = AUDIT_PAGE.read_bytes()
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    assert AUDIT_PAGE.read_bytes() == before, "构建非幂等"


# ── 2. DOM：八元组＋过滤＋摘要＋只读＋分页 ──

def test_dom_eight_tuple_columns():
    """八列表头＝audit.py 真源七元组顺序＋trace_id 第八列（SPEC C1 口径）。"""
    dom = _dom(AUDIT_PAGE.read_text(encoding="utf-8"))
    for col in ("时间", "调用方", "端点", "参数摘要", "状态", "耗时", "响应摘要", "追踪号"):
        assert col in dom.texts, "表头缺八元组列：" + col
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    assert "sha256" in html and "不留存参数明文" in html   # 摘要列不出明文口径的就地说明


def test_dom_filter_toolbar():
    dom = _dom(AUDIT_PAGE.read_text(encoding="utf-8"))
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    for label in ("今日", "近 24 小时", "近 7 天", "全部"):
        assert label in dom.texts, "时间窗快捷缺：" + label
    for iid in ("audit-ts-from", "audit-ts-to", "audit-caller", "audit-endpoint",
                "audit-status", "audit-trace", "audit-limit", "audit-submit", "audit-reset"):
        assert ('id="' + iid + '"') in html, "过滤控件缺：" + iid
    for opt in ('value="2xx"', 'value="4xx"', 'value="5xx"', 'value="403"', 'value="429"'):
        assert opt in html, "状态选项缺：" + opt
    assert "支持部分匹配" in html, "部分匹配口径未明示（placeholder 属性）"


def test_dom_summary_cards_and_readonly():
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    dom = _dom(html)
    for label in ("窗口内调用总量", "成功率", "慢查询", "耗时 Top 端点"):
        assert _has_text(dom, label), "统计摘要卡缺：" + label
    js = (PORTAL / "assets" / "audit.js").read_text(encoding="utf-8")
    assert "样本量不足" in js, "小基数口径文案缺失（js）"
    assert "只读" in dom.texts, "只读角标缺失"
    assert "SLA P99 同源" in html and "200" in html, "慢阈值口径未说明"


def test_dom_pagination_and_trace_click():
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    dom = _dom(AUDIT_PAGE.read_text(encoding="utf-8"))
    for iid in ("audit-prev", "audit-next"):
        assert ('id="' + iid + '"') in html, "分页控件缺：" + iid
    assert "上一页" in dom.texts and "下一页" in dom.texts
    assert "useTrace(row.traceId)" in html, "行内追踪号不可点反查"
    assert "退出反查" in dom.texts
    assert "当前条件下没有查到审计记录" in dom.texts, "空态文案缺失"


def test_dom_gateway_rejected_footnote_honest():
    """网关拒绝记录不在本台账（八元组真源口径）——脚注如实指引，不硬造合并。"""
    dom = _dom(AUDIT_PAGE.read_text(encoding="utf-8"))
    assert _has_text(dom, "未过认证") and _has_text(dom, "不落这份台账")


# ── 3. 侧栏「合规」组 ──

def test_sidebar_compliance_group():
    html = AUDIT_PAGE.read_text(encoding="utf-8")
    sidebar = html.split('id="app-sidebar"')[1].split("</aside>")[0]
    dom = _dom(sidebar)
    assert "合规" in dom.texts, "侧栏缺「合规」组"
    m = re.search(r'<div class="os-nav-label">合规</div>.*?</div>', sidebar, re.S)
    assert m, "「合规」组结构缺失"
    assert 'href="audit.html"' in m.group(0)
    assert "is-active" in m.group(0), "合规组内本页链接未激活"
    other = (PORTAL / "index.html").read_text(encoding="utf-8")
    assert 'href="audit.html"' in other, "首页侧栏缺审计查询入口"


# ── 4. 组件契约（audit.js） ──

def test_audit_js_contract():
    js = (PORTAL / "assets" / "audit.js").read_text(encoding="utf-8")
    assert "/v1/portal/audit/logs" in js, "取数未直达审计端点"
    assert "auditQueryPage" in js and "alpine:init" in js, "Alpine 组件未按纪律注册"
    for param in ("ts_from", "ts_to", "caller", "endpoint", "status", "trace_id",
                  "limit", "offset"):
        assert param in js, "查询参数缺：" + param
    assert "history.replaceState" in js, "深链同步缺失"
    # 不做 mock 降级：失败路径只进 error 态（errMsg 就地展示），不造演示数据
    assert "mockData" not in js and "MOCK_" not in js and "errMsg" in js
    assert "setHours(0, 0, 0, 0)" in js, "今日窗应按本地 0 点计算"


# ── 5. 详情壳改写与红线 ──

def test_detail_shells_link_rewritten():
    for shell in sorted((PORTAL / "catalog").glob("*.html")):
        html = shell.read_text(encoding="utf-8")
        assert 'href="../audit.html"' in html, shell.name + " 未改写审计页子目录链接"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("  ok " + fn.__name__)
        except Exception as exc:   # noqa: BLE001
            failed += 1
            print("  FAIL " + fn.__name__ + ": " + str(exc))
    print(str(len(fns) - failed) + "/" + str(len(fns)) + " passed")
    sys.exit(1 if failed else 0)
