#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-10/P0-11 申请接入＋帮助与支持套件（tests/test_apply_contact_portal.py）。

守卫口径（任务卡范围；SPEC v0 §5-P0-10/P0-11、§3.3-E2/E4、§6-D10）：
  1. PAGES 注册：apply 入注册表与 NAV_KEYS（区块 id/按页 css/无越页注入）；
  2. P0-10 DOM：OA 入口联系链（docs.html#docs-channels，无假外链）、四态时间线（评审中/
     审批中/待发放凭证/已生效静态在位）、演示角标＋如实咨询文案、已批字段清单对照
     service/auth_matrix.yaml 真源（demo_bank 数值＝yaml 事实，防文案漂移）；
  3. P0-11 DOM（**2026-09-23 起并入帮助与支持页 docs，独立 contact 页已下线**）：
     联系人/工单占位如实标注（无假电话/假链接）、SLA 表三行口径引自 SPEC §6.1
     （P50≤50ms/P99≤200ms/错误率<0.1%/超时 5s）、值班说明、查不到数三步引导
     （状态页→traceId 反查→值班）＋ my.html?trace_id= 深链形态；
  4. 链路可达：首页引导条→apply/docs；apply→docs；侧栏入口；
     详情壳新页链接随子目录改写（含 fields/my 先例断链修复）。

**合并处置（维护者 2026-09-23 追加）**：原 contact 独立页内容已并入 docs（帮助与支持），
故 P0-11 的 DOM 断言迁移到 docs 页；本套件不删覆盖——contact 的下线/301/无死链守卫
另见 tests/test_docs_help_center.py（P1-7 套件）。
运行：python3 -m pytest tests/test_apply_contact_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"

APPLY_PAGE = PORTAL / "apply.html"
# P0-11 内容并入目标页（原 contact.html 已下线）
DOCS_PAGE = PORTAL / "docs.html"

# 产物缺失时先补齐（保证套件可独立运行）
if not APPLY_PAGE.is_file() or not DOCS_PAGE.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


class _TagText(HTMLParser):
    """极简 DOM 断言器（test_build_portal 同款，无第三方依赖）。"""

    def __init__(self):
        super().__init__()
        self.tags = []          # (tag, attrs dict)
        self.texts = []         # 可见文本
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


APPLY_HTML = APPLY_PAGE.read_text(encoding="utf-8")
DOCS_HTML = DOCS_PAGE.read_text(encoding="utf-8")


# ── 1. 构建注册：PAGES ＋ NAV_KEYS ──

def test_pages_registered_in_build():
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "apply" in bp.NAV_KEYS and "apply" in bp.PAGES, "apply 未入 NAV_KEYS/PAGES"
    assert "docs" in bp.NAV_KEYS and "docs" in bp.PAGES, "docs 未入 NAV_KEYS/PAGES"
    assert bp.PAGES["apply"]["required_ids"] == ["apply-oa-entry", "apply-progress", "apply-approved"]
    # P0-11 内容并入 docs 后：原 contact-* 区块 id 已更名 docs-*（同语义）
    for block in ("docs-channels", "docs-notfound-guide", "docs-sla", "docs-duty"):
        assert block in bp.PAGES["docs"]["required_ids"], f"docs 注册缺 P0-11 区块：{block}"
    assert "assets/apply.css" in bp.PAGES["apply"]["extra_css"]
    assert "assets/docs.css" in bp.PAGES["docs"]["extra_css"]
    # 最小实现：apply/docs 均无按页 JS（静态内容，不挂 Alpine 组件）
    assert "extra_js" not in bp.PAGES["apply"] and "extra_js" not in bp.PAGES["docs"]
    # 合并下线：contact 不得再出现在注册表/导航
    assert "contact" not in bp.NAV_KEYS and "contact" not in bp.PAGES,         "contact 独立页未从构建注册表下线"


def test_pages_rendered_with_shell_and_active_nav():
    for page, label in ((APPLY_PAGE, "申请接入"), (DOCS_PAGE, "帮助与支持")):
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"@@[A-Za-z0-9_]+@@", html), f"{page.name} 残留模板占位符"
        assert f'aria-current="page" href="{page.stem}.html"' in html, f"{page.name} 侧栏 active 态未注入"
        assert "js-canary" in html, f"{page.name} JS 错误探针缺失"
        externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        assert not externals, f"{page.name} 出现外链（内网白屏红线）：{externals}"


def test_per_page_css_not_leaked_to_other_pages():
    apply_css = 'href="assets/apply.css"'
    docs_css = 'href="assets/docs.css"'
    for other in ("index.html", "my.html", "audit.html"):
        html = (PORTAL / other).read_text(encoding="utf-8")
        assert apply_css not in html and docs_css not in html, f"{other} 被注入他页 css"


def test_build_idempotent():
    before = (APPLY_PAGE.read_bytes(), DOCS_PAGE.read_bytes())
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    assert (APPLY_PAGE.read_bytes(), DOCS_PAGE.read_bytes()) == before, "构建非幂等"


# ── 2. P0-10 申请接入页 DOM ──

def test_apply_dom_oa_entry_honest_placeholder():
    dom = _dom(APPLY_HTML)
    # #44 占位回填（2026-10-15 维护者 口径）：申请授权联系平台运营组；
    # 不提 OA、不编造流程细节，流程与时效以平台运营组为准
    assert _has_text(dom, "平台运营组"), "申请入口缺联系对象（平台运营组）"
    assert any(a.get("id") == "apply-contact-line" for _, a in dom.tags), "缺申请联系行元素"
    assert _has_text(dom, "流程与时效以平台运营组为准"), "缺「流程与时效以平台运营组为准」口径"
    assert _has_text(dom, "联系方式与支持入口"), "缺联系行动链接"
    # 占位纪律：不给假外链（页面上不得出现 http 外部链接）
    links = [a.get("href") for t, a in dom.tags if t == "a"]
    assert all(not (h or "").startswith("http") for h in links), "出现假外链"
    # 合并后：联系入口链到帮助与支持页的联系区块（原 contact.html 已下线）
    assert "docs.html#docs-channels" in links, "联系入口未链到帮助与支持页联系区块"


def test_apply_dom_timeline_four_states():
    dom = _dom(APPLY_HTML)
    for sid in ("apply-timeline", "apply-step-reviewing", "apply-step-approving",
                "apply-step-issuing", "apply-step-active"):
        assert f'id="{sid}"' in APPLY_HTML, f"时间线缺节点：{sid}"
    for label in ("评审中", "审批中", "待发放凭证", "已生效"):
        assert _has_text(dom, label), f"时间线缺状态文案：{label}"


def test_apply_dom_demo_honest_no_fake_state_machine():
    dom = _dom(APPLY_HTML)
    # 时间线头＋演示条目块各一枚（演示区块必须带角标）
    assert APPLY_HTML.count('demo-badge') >= 2, "演示条目缺角标（演示数据必须标注）"
    assert _has_text(dom, "你的申请进度请咨询平台运营组"), "缺如实咨询文案"
    assert _has_text(dom, "模拟条目"), "演示条目未注明是模拟"
    assert _has_text(dom, "不显示任何真实申请状态"), "未声明不显示真实状态"
    # 演示单号用授权登记占位真值，不发明审批状态数字/时间
    assert "OA-PENDING-TASK008" in APPLY_HTML


# 【已删用例·碑注】test_apply_dom_approved_fields_match_auth_matrix：申请页「已批字段」文案对照 auth_matrix 演示租户 demo_bank 真源——授权条目已随 2026-09-29 全服务下线清空（matrix: []），页面文案属旧门户遗留形态；新向导自带新校验。

def test_apply_no_fake_status_data_source():
    """P0 数据侧最小实现：页面正文不接状态接口（无 fetch/mock 降级口径渗入本页）。"""
    # 环境条已源头删除（2026-09-29）：本页零 Alpine 挂载、全静态
    assert APPLY_HTML.count('x-data=') == 0, "本页应保持静态（零 Alpine 挂载）"
    assert "PortalAPI" not in APPLY_HTML and "mock/" not in APPLY_HTML, "本页不应有取数逻辑"


# ── 3. P0-11 帮助与支持页 DOM（原 contact 页内容，2026-09-23 并入 docs） ──

def test_docs_dom_channels_honest_placeholder():
    dom = _dom(DOCS_HTML)
    assert _has_text(dom, "工单"), "缺工单入口说明"
    assert _has_text(dom, "待运营提供"), "联系人/工单入口未如实占位标注"
    assert 'id="docs-placeholder-note"' in DOCS_HTML, "缺占位说明标注块"
    # 不放假电话/假邮箱/假外链：只看可见文本（原文注释里的 sha256 十六进制会长得像 11 位数字）
    visible = " ".join(dom.texts)
    assert not re.search(r"1[3-9]\d{9}", visible), "可见文案出现疑似编造手机号"
    links = [a.get("href") for t, a in dom.tags if t == "a"]
    assert all(not (h or "").startswith("http") for h in links), "出现假外链"
    assert _has_text(dom, "X-Trace-Id"), "缺追踪号自查口径"


def test_docs_dom_sla_table_from_spec_61():
    dom = _dom(DOCS_HTML)
    assert 'id="docs-sla-table"' in DOCS_HTML, "缺 SLA 表"
    for label in ("P50 ≤ 50ms", "P99 ≤ 200ms", "&lt; 0.1%"):
        assert label in DOCS_HTML, f"SLA 表缺 SPEC §6.1 数值：{label}"
    assert _has_text(dom, "A · 标签点查") and _has_text(dom, "B · 上游代理"), "缺 A/B 分档行"
    assert _has_text(dom, "目标值"), "未标注当前为设计目标值（承诺值 M2 后定版 NC-6）"
    assert _has_text(dom, "SPEC-统一数据服务平台_v1 §6.1"), "SLA 数值未标出处"


def test_docs_dom_duty_and_guide():
    dom = _dom(DOCS_HTML)
    assert 'id="docs-duty"' in DOCS_HTML, "缺值班说明块"
    assert _has_text(dom, "值班"), "缺值班说明文案"
    # 三步引导：状态页 → traceId 反查 → 联系值班
    assert 'id="docs-notfound-guide"' in DOCS_HTML
    for label in ("先看平台状态", "用追踪号反查这次调用", "联系值班"):
        assert _has_text(dom, label), f"三步引导缺步：{label}"
    assert 'href="ops.html"' in DOCS_HTML and 'id="docs-link-ops"' in DOCS_HTML, "第 1 步未链运行视图"
    assert 'href="my.html?trace_id="' in DOCS_HTML and 'id="docs-link-mylogs"' in DOCS_HTML,         "第 2 步未携 trace_id 深链到我的日志"


def test_docs_contact_entry_replaces_retired_page():
    """旧 contact 页的入口职责迁移：docs 自带联系/工单/SLA，且不再链已下线的 contact。"""
    assert "contact.html" not in DOCS_HTML, "docs 仍链已下线的 contact.html"
    assert 'id="docs-channels"' in DOCS_HTML, "缺联系与工单区块（原 contact 内容）"


# ── 4. 链路可达：首页引导条／docs 入口／侧栏／详情壳改写 ──

def test_index_guide_bar_links_apply_and_help():
    dom = _dom((PORTAL / "index.html").read_text(encoding="utf-8"))
    index_html = (PORTAL / "index.html").read_text(encoding="utf-8")
    assert 'id="guide-bar"' in index_html, "首页缺引导条"
    assert any(a.get("id") == "guide-apply-link" and a.get("href") == "apply.html"
               for t, a in dom.tags if t == "a"), "引导条缺申请接入链接"
    # 合并后：guide 的「报障」入口改指帮助与支持页（原 contact.html 已下线）
    assert any(a.get("id") == "guide-contact-link" and a.get("href") == "docs.html#docs-channels"
               for t, a in dom.tags if t == "a"), "引导条报障入口未改指 docs"
    assert _has_text(dom, "要正式接入") and _has_text(dom, "查不到数或要报障"), "引导条文案缺失"


def test_sidebar_entries_in_both_groups():
    dom = _dom((PORTAL / "index.html").read_text(encoding="utf-8"))
    navs = [(a.get("href"), a.get("title") or "") for t, a in dom.tags
            if t == "a" and "os-nav-link" in (a.get("class") or "")]
    apply_hits = [n for n in navs if n[0] == "apply.html" and "P0-10" in n[1]]
    docs_hits = [n for n in navs if n[0] == "docs.html" and "P1-7" in n[1]]
    assert apply_hits and docs_hits, "侧栏缺申请接入/帮助与支持入口"
    assert not any(n[0] == "contact.html" for n in navs), "侧栏仍有已下线的 contact 入口"


def test_detail_shells_new_pages_rewritten_and_legacy_fixed():
    """详情壳（catalog/ 子目录）：新页链接带 ../ 前缀；my 先例断链一并修复。

    卡5 2026-09-30：fields.html 断链改写断言随字段字典页下线移除（侧栏入口已删，
    详情壳不再出现 fields 链接；如出现＝残留，由下方 broken 断言兜底拦截）。
    """
    for shell in PORTAL.glob("catalog/*.html"):
        html = shell.read_text(encoding="utf-8")
        assert "contact.html" not in html, f"{shell.name} 仍有 contact 死链"
        assert "fields.html" not in html, f"{shell.name} 仍有已下线 fields 死链"
        for broken in ('href="apply.html"', 'href="my.html"'):
            assert broken not in html, f"{shell.name} 侧栏断链未改写：{broken}"
        assert 'href="../apply.html"' in html, f"{shell.name} 缺新页侧栏链接"
        assert 'href="../my.html"' in html, f"{shell.name} 缺 my 改写链接"


def test_apply_docs_cross_reachable():
    """两页互达：apply 的联系入口链 docs 联系区块；docs 三步引导可达 ops/my。"""
    assert 'href="docs.html#docs-channels"' in APPLY_HTML, "apply 缺 docs 联系入口"
    for target in ("ops.html", "my.html?trace_id="):
        assert f'href="{target}"' in DOCS_HTML, f"docs 页缺 {target} 链接"


if __name__ == "__main__":
    import traceback
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("[ok] " + name)
            except Exception:
                failed += 1
                print("[FAIL] " + name)
                traceback.print_exc()
    sys.exit(1 if failed else 0)
