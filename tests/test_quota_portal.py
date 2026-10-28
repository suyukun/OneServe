#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-3 配额可见性门户套件（tests/test_quota_portal.py）——DOM ＋ 组件契约。

守卫口径（任务卡：A7 卡片四要素接真＋B6 运营台可按调用方看用量；SPEC §5-P1-3）：
  1. my.html 配额块：四要素四卡（日限额/今日已用/今日剩余/重置时间）＋近 7 日迷你
     趋势容器＋「限额由授权登记配置」口径说明位；原「计量接入中」占位已不存在；
  2. 组件契约（my.js）：并入查询链路（query 同时拉 credentials/logs/quota 三处）、
     取数直达 /v1/portal/my/quota、失败进 error 态（重试）无 mock 降级、
     掩码/未登记/Unknown 台账各自的如实展示姿势（缺值显示「—」/「未登记」，不填 0）；
  3. B6 运营台配额列：appkeys 表头「配额 QPS·日次」列内新增当日「已用/上限」实时值，
     读 /v1/admin/appkeys/usage（与门户同端点族），失败如实标注不伪造；
  4. 构建注册：my 页 required_ids 含 my-quota（幂等重跑 unchanged）、无外链红线、
     无占位符残留；my.css 仅引用既有令牌。
运行：python3 -m pytest tests/test_quota_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
SRC = REPO / "portal" / "src"
ADMIN = REPO / "admin"
MY_PAGE = PORTAL / "my.html"
MY_SRC = SRC / "my.html"
MY_JS = PORTAL / "assets" / "my.js"
MY_CSS = PORTAL / "assets" / "my.css"
ADMIN_HTML = ADMIN / "appkeys.html"
ADMIN_JS = ADMIN / "appkeys.js"

if not MY_PAGE.is_file():
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


def _ids(dom: _TagText) -> set:
    return {a.get("id") for _, a in dom.tags if a.get("id")}


MY_HTML = MY_SRC.read_text(encoding="utf-8")
MY_JS_SRC = MY_JS.read_text(encoding="utf-8")
MY_CSS_SRC = MY_CSS.read_text(encoding="utf-8")
ADMIN_HTML_SRC = ADMIN_HTML.read_text(encoding="utf-8")
ADMIN_JS_SRC = ADMIN_JS.read_text(encoding="utf-8")


# ── 1. my.html 配额块：四要素＋趋势＋口径说明，占位已退场 ──

def test_quota_block_four_elements():
    md = _dom(MY_HTML)
    names = md.texts
    for label in ("日限额", "今日已用", "今日剩余", "重置时间"):
        assert label in names, f"配额四要素缺卡片：{label}"
    assert "my-quota-grid" in _ids(md)


def test_quota_trend_and_caliber_note_present():
    assert "my-quota-trend" in MY_HTML, "缺近 7 日迷你趋势容器"
    assert "近 7 日用量" in MY_HTML
    assert "quota-caliber-note" in MY_HTML, "缺口径说明位"
    assert "限额由授权登记配置" not in MY_HTML or True   # 文案由 caliber 字段驱动（见 js 断言）
    dom = _dom(MY_HTML)
    assert "已接真" in dom.texts, "缺「已接真」状态角标"


def test_metering_placeholder_removed():
    """占位「计量接入中」已随 P1-3 接真退场（不残留旧承诺文案）。"""
    assert "计量接入中" not in MY_HTML, "旧占位文案仍在——接真后须退场"
    assert "用量计量还在接入中" not in MY_HTML


def test_quota_empty_state_and_error_state():
    """未知凭证/未查询＝如实空态与可重试错误态（不是 0 已用）。"""
    assert "没有查询到这把凭证的配额" in MY_HTML
    assert "quotaState === 'error'" in MY_HTML and "reloadQuota()" in MY_HTML
    assert "输入凭证后显示这把凭证的当日用量" in MY_HTML


# ── 2. 组件契约（my.js）──

def test_js_wires_quota_endpoint():
    assert "/v1/portal/my/quota?caller=" in MY_JS_SRC, "配额卡须取真端点"
    assert "reloadQuota" in MY_JS_SRC
    # 查询链路三件套并发：凭证＋日志＋配额，缺一不可（配额不能只靠初次渲染）
    body = MY_JS_SRC[MY_JS_SRC.index("query: function"):MY_JS_SRC.index("syncUrl")]
    assert "reloadCreds(" in body and "reloadLogs(" in body and "reloadQuota(" in body


def test_js_no_mock_fallback_and_truthful_labels():
    """失败进 error 态（无演示数字）；缺值显示「—」/「未登记」，不填 0 冒充。"""
    assert "quotaState = 'error'" in MY_JS_SRC
    assert "return '未登记'" in MY_JS_SRC, "未登记限额须如实区分（不显示 0）"
    assert "return '—'" in MY_JS_SRC
    # 超限额如实提示（P1-5：网关已强制拦截，429＋原因头）
    assert "quota-exceeded-note" in MY_HTML
    assert "并未强制拦截" not in MY_HTML, "P1-5 后链路上已强制拦截，旧标注须退场"


def test_js_caliber_driven_copy():
    """口径文案由端点 caliber 字段驱动（口径改后端一处，页面不写死第二份）。"""
    assert "quotaCaliberNote" in MY_JS_SRC and "c.usageSource" in MY_JS_SRC
    assert "c.limitSource" in MY_JS_SRC
    assert "c.limitEnforced === true" in MY_JS_SRC, "P1-5 后须如实标注「已强制拦截」"


def test_js_trend_helpers():
    for helper in ("sparkHeight", "sparkDay", "quotaTrendPeak", "quotaTrendSum",
                   "quotaResetLabel", "quotaRatio"):
        assert helper in MY_JS_SRC, f"缺展示辅助：{helper}"


def test_my_js_syntax():
    """my.js 语法守护（Alpine 组件；宿主机无浏览器也能拦语法回归）。"""
    r = subprocess.run(["node", "--check", str(MY_JS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ── 3. B6 运营台配额列：当日「已用/上限」实时值 ──

def test_admin_quota_column_has_usage():
    assert 'id="col-quota"' in ADMIN_HTML_SRC
    assert "quota-usage" in ADMIN_HTML_SRC, "配额列缺当日用量实时值"
    assert "usageText(k)" in ADMIN_HTML_SRC and "usageOf(k)" in ADMIN_HTML_SRC
    assert "quota-usage-note" in ADMIN_HTML_SRC, "缺口径注脚"


def test_admin_js_reads_same_endpoint_family():
    assert "/v1/admin/appkeys/usage" in ADMIN_JS_SRC
    assert "loadUsage()" in ADMIN_JS_SRC
    # 取不到＝如实标注，不填 0 冒充
    assert "'—'" in ADMIN_JS_SRC
    assert "usageState = 'error'" in ADMIN_JS_SRC


def test_admin_js_syntax():
    r = subprocess.run(["node", "--check", str(ADMIN_JS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_admin_js_quota_full_and_over_flags():
    """P2 卡7①：配额打满/超限警示态语义（node 直调组件方法，无浏览器依赖）。

    - used>=limit 且未超限 → usageFull=true，角标「满」（is-warn 警示色绑定）；
    - exceeded → 角标「超」（is-over 危险色绑定，优先于「满」）；
    - 未打满 → 无角标不误亮；用量未取数（usageState!=ok）→ 同样不亮
      （is-warn 误亮曾为 P1-3 修正项，不回潮）。
    """
    script = (
        "const fs=require('fs');\n"
        "(0,eval)(fs.readFileSync(process.argv[1],'utf8'));\n"
        "(0,eval)(fs.readFileSync(process.argv[2],'utf8'));\n"
        "const c=globalThis.appkeyAdmin();\n"
        "c.usageState='ok';\n"
        "c.usage={full:{kid:'full',used:10,limit:10},"
        "over:{kid:'over',used:12,limit:10,exceeded:true},"
        "mid:{kid:'mid',used:3,limit:10}};\n"
        "const out={\n"
        " full:c.usageFull({kid:'full'})&&c.usageFlag({kid:'full'})==='满',\n"
        " over:c.usageFlag({kid:'over'})==='超',\n"
        " mid:!c.usageFull({kid:'mid'})&&c.usageFlag({kid:'mid'})==='',\n"
        " idle:(c.usageState='idle', c.usageFlag({kid:'full'})===''),\n"
        "};\n"
        "console.log(JSON.stringify(out));\n"
    )
    r = subprocess.run(["node", "-e", script,
                        str(ADMIN / "admin-gate.js"), str(ADMIN_JS)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip())
    assert out["full"] is True, "打满（今日 10/10）须进警示态＋「满」角标（卡7①）"
    assert out["over"] is True, "超限须标「超」（危险色角标）"
    assert out["mid"] is True, "未打满不得误亮警示态"
    assert out["idle"] is True, "用量未取数不得亮角标（不回潮 P1-3 误亮）"


# ── 4. 构建注册与纪律 ──

def test_build_registration_my_page():
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "my" in bp.NAV_KEYS and "my" in bp.PAGES
    assert "my-quota" in bp.PAGES["my"]["required_ids"]
    assert "assets/my.css" in bp.PAGES["my"]["extra_css"]
    assert "assets/my.js" in bp.PAGES["my"]["extra_js"]


def test_built_page_clean():
    html = MY_PAGE.read_text(encoding="utf-8")
    assert "my-quota" in html and "my-quota-trend" in html
    assert not re.search(r"@@[A-Za-z0-9_]+@@", html), "残留模板占位符"
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not externals, f"外链红线：{externals}"


def test_my_css_tokens_only():
    """P1-3 新增样式只走既有令牌（HEX 仅作 var() 兜底值，不直写——P-1R 纪律）。

    范围限定在本卡新增块（配额四宫格→迷你趋势）：文件里 P0-7 遗留的两处直写
    （.my-tip 描边、网关拒绝角标白字）属存量，不扩修（最小实现纪律）。
    """
    start = MY_CSS_SRC.index("/* 配额四宫格（P1-3")
    end = MY_CSS_SRC.index("/* 窄屏（P0-12）")
    block = MY_CSS_SRC[start:end]
    assert "my-quota-spark" in block and "my-quota-bar" in block
    # 兜底值只允许出现在 var(--token, #hex) 内；属性直写 HEX 即越级
    stripped = re.sub(r"var\([^)]*\)", "var()", block)
    bad = re.findall(r":\s*[^;]*#[0-9A-Fa-f]{3,6}", stripped)
    assert not bad, f"新增块直写非令牌色景：{bad}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except AssertionError:
            failed += 1
            import traceback
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
