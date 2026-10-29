#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-8 授权登记全景门户套件（tests/test_grants_portal.py）——构建注册＋DOM＋组件契约。

守卫口径（任务卡：新 C 面授权全景页，侧栏「合规」组；按租户/服务/字段筛；
复用既有令牌；窄屏容器内滚动；SPEC §2-C2/§5-P1-8；ux-fix-batch 2026-10-29：
「OA 单号溯源」「字段反查」两区随验收下线，全景表/KPI/凭证主体保留）：
  1. 构建注册：grants 入 NAV_KEYS/PAGES（区块 id/按页 css+js/无越页注入）；构建幂等；
     产物同步（重跑 build_portal 后 portal/grants.html 逐字节一致）；
  2. DOM：筛选控件齐全（租户/服务/字段＋OA 状态 chips）；十一列表头（#58 配额上限/
      今日已用两列，2026-10-15 门面批）；OA 状态徽标三类（已登记/占位/缺失）；
      只读角标；空态文案；
  3. 侧栏：「合规」组含 grants 链接（C 面 IA），active 态构建期注入；
  4. 组件契约（grants.js）：取数直达 /v1/portal/grants/*、Alpine 注册、深链参数、
     无 mock 降级、无写请求（只读面）；
  5. 链路：详情壳子目录链接改写（../grants.html）；字段字典页呼应（fields 页链接
     grants 或互为指引）；外链红线/占位符残留零容忍。
运行：python3 -m pytest tests/test_grants_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
GRANTS_PAGE = PORTAL / "grants.html"
GRANTS_JS = PORTAL / "assets" / "grants.js"

if not GRANTS_PAGE.is_file():
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
    assert "grants" in bp.NAV_KEYS and "grants" in bp.PAGES
    assert bp.PAGES["grants"]["required_ids"] == [
        "grants-about", "grants-summary", "grants-toolbar",
        "grants-table"]
    assert "assets/grants.css" in bp.PAGES["grants"]["extra_css"]
    assert "assets/grants.js" in bp.PAGES["grants"]["extra_js"]


def test_page_rendered_with_shell_and_active_nav():
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    assert not re.search(r"@@[A-Za-z0-9_]+@@", html), "残留模板占位符"
    assert 'aria-current="page" href="grants.html"' in html, "侧栏 active 态未注入"
    assert "js-canary" in html, "JS 错误探针缺失"
    assert 'x-data="grantsViewPage"' in html, "Alpine 组件未挂载"
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not externals, "外链红线：" + str(externals)


def test_required_blocks_present():
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    for block_id in ("grants-about", "grants-summary", "grants-toolbar",
                     "grants-table"):
        assert ('id="' + block_id + '"') in html, "缺关键区块 id：" + block_id


def test_per_page_assets_not_leaked():
    for other in ("index.html", "my.html", "docs.html", "ops.html", "audit.html"):
        html = (PORTAL / other).read_text(encoding="utf-8")
        assert "assets/grants.css" not in html and "assets/grants.js" not in html, \
            other + " 被注入授权全景页资产"


def test_build_idempotent_and_artifact_synced():
    """构建幂等＋产物同步：重跑 build_portal 后 portal/grants.html 逐字节一致。"""
    before = GRANTS_PAGE.read_bytes()
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                       check=True, capture_output=True)
    assert r.returncode == 0, "构建失败"
    assert GRANTS_PAGE.read_bytes() == before, "构建非幂等/产物未同步"


# ── 2. DOM：筛选＋表头＋状态徽标＋只读 ──

def test_dom_filter_controls():
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    dom = _dom(html)
    for iid in ("gr-tenant", "gr-service", "gr-field", "gr-field-list",
                "gr-submit", "gr-reset", "gr-oa-chips"):
        assert ('id="' + iid + '"') in html, "筛选控件缺：" + iid
    for label in ("租户", "服务", "字段", "已登记单号", "占位待回填", "缺单号", "全部状态"):
        assert _has_text(dom, label), "筛选/状态文案缺：" + label
    assert "支持部分匹配" in html, "部分匹配口径未明示（字段 placeholder）"


def test_dom_table_columns_and_oa_badges():
    dom = _dom(GRANTS_PAGE.read_text(encoding="utf-8"))
    for col in ("租户", "凭证指纹（脱敏）", "服务", "对外端点", "字段数",
                "配额上限", "今日已用",
                "授权字段", "OA 单号", "单号状态"):
        assert _has_text(dom, col), "表头缺列：" + col
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    assert "serviceVersion" not in html, "卡4 AC-3d：serviceVersion 列须删净（不留恒空列）"
    assert ":class=\"oaClass(" in html, "OA 状态徽标未按状态动态着色"
    js = GRANTS_JS.read_text(encoding="utf-8")
    css = (PORTAL / "assets" / "grants.css").read_text(encoding="utf-8")
    for cls in ("gr-oa-present", "gr-oa-placeholder", "gr-oa-missing"):
        assert cls in js, "组件缺 OA 状态类映射：" + cls
        assert ("." + cls) in css, "样式缺 OA 状态类：" + cls


def test_dom_fields_plain_no_lookup():
    """ux-fix-batch：字段反查区下线——表中字段纯展示（不再绑反查点击）。"""
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    assert 'x-for="f in row.fields"' in html, "字段列仍须逐项展示"
    assert "lookupField" not in html, "字段反查绑定残留（区已下线）"
    assert "OA 单号溯源" not in _dom(html).texts, "OA 溯源区残留（区已下线）"
    assert 'id="grants-field-lookup"' not in html, "字段反查区残留（区已下线）"
    assert 'id="grants-oa"' not in html, "OA 溯源区残留（区已下线）"


def test_dom_readonly_and_empty_states():
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    dom = _dom(html)
    assert "只读" in dom.texts, "只读角标缺失"
    assert "当前筛选条件下没有匹配的授权登记条目" in dom.texts, "全景表空态文案缺失"
    assert "不代填" in html, "缺单号如实标注口径缺失"


def test_dom_uses_existing_tokens_only():
    """复用既有 tokens，不新增设计变量（任务卡硬约束）。"""
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（只放宽匹配不改契约）
    assert re.search(r'<link rel="stylesheet" href="assets/tokens\.css(\?v=[0-9a-f]+)?">', html)
    assert re.search(r'<link rel="stylesheet" href="assets/grants\.css(\?v=[0-9a-f]+)?">', html)
    css = (PORTAL / "assets" / "grants.css").read_text(encoding="utf-8")
    # 只允许引用既有令牌；不得在本页私有样式里定义新的 --变量
    assert not re.search(r"^\s*--[a-z-]+\s*:", css, re.M), "grants.css 不得新增设计令牌"
    assert "var(--" in css, "未引用既有令牌"


def test_dom_narrow_screen_container_scroll():
    """窄屏（P0-12 口径）：宽表在容器内滚动，不撑破布局。"""
    css = (PORTAL / "assets" / "grants.css").read_text(encoding="utf-8")
    assert "overflow-x: auto" in css and ".gr-table-wrap" in css
    assert "@media (max-width: 720px)" in css, "缺窄屏断点"


# ── 3. 侧栏「合规」组 ──

def test_sidebar_compliance_group_has_grants():
    html = GRANTS_PAGE.read_text(encoding="utf-8")
    sidebar = html.split('id="app-sidebar"')[1].split("</aside>")[0]
    dom = _dom(sidebar)
    assert "合规" in dom.texts, "侧栏缺「合规」组"
    m = re.search(r'<div class="os-nav-label">合规</div>.*?</div>', sidebar, re.S)
    assert m, "「合规」组结构缺失"
    assert 'href="grants.html"' in m.group(0), "「合规」组缺授权全景入口"
    assert "is-active" in m.group(0), "合规组内本页链接未激活"
    other = (PORTAL / "index.html").read_text(encoding="utf-8")
    assert 'href="grants.html"' in other, "首页侧栏缺授权全景入口"


# ── 4. 组件契约（grants.js） ──

def test_grants_js_contract():
    js = GRANTS_JS.read_text(encoding="utf-8")
    assert "/v1/portal/grants/panorama" in js, "取数未直达授权端点"
    assert "/v1/portal/grants/field" not in js and "/v1/portal/grants/oa" not in js, \
        "反查/溯源端点残留（两区已下线）"
    assert "grantsViewPage" in js and "alpine:init" in js, "Alpine 组件未按纪律注册"
    for param in ("tenant", "service", "field"):
        assert param in js, "深链参数缺：" + param
    assert "history.replaceState" in js, "深链同步缺失"
    assert "mockData" not in js and "MOCK_" not in js and "errMsg" in js, "不得 mock 降级"


def test_grants_js_is_readonly_no_write_requests():
    """只读面：组件不得出现写请求（POST/PUT/DELETE/PATCH）。"""
    js = GRANTS_JS.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    assert "method: 'GET'" in code, "取数须为 GET"
    assert not re.search(r"method\s*:\s*['\"](POST|PUT|DELETE|PATCH)", code, re.I), \
        "只读页出现写请求"
    assert not re.search(r"\b(register|save|update|delete)\s*\(", code), "只读页出现写操作调用"


# ── 5. 详情壳改写与页面呼应 ──

def test_detail_shells_link_rewritten():
    for shell in sorted((PORTAL / "catalog").glob("*.html")):
        html = shell.read_text(encoding="utf-8")
        assert 'href="../grants.html"' in html, shell.name + " 未改写授权全景页子目录链接"


# 卡5 2026-09-30：test_fields_page_crosslinks_grants 随字段字典页下线移除
# （297 宽表字典体系下线；表级 schema 元数据由卡2/卡3 重建后按新形态补回呼应入口）。


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] " + fn.__name__)
        except Exception:
            failed += 1
            print("[FAIL] " + fn.__name__)
            traceback.print_exc()
    print(str(len(fns) - failed) + "/" + str(len(fns)) + " passed")
    sys.exit(1 if failed else 0)
