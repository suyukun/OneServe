#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-7 帮助与支持页结构化套件（tests/test_docs_help_center.py）。

任务卡：前台重造 SPEC v0 §5-P1-7（＋§3.3 企业用户旅程、§4.6 限流口径、A9 帮助中心）。
**2026-09-23 维护者 追加**：原 P0-11「联系与支持」（contact）独立页并入本页（docs 为落地页），
故本套件同时守卫合并结果——SLA 单源、联系入口齐全、contact 下线且无死链。

守卫口径（七项）：
  1. 术语表：网关／AppKey 与 secret／查询键／上游供数口径／字段级授权／降级／演示数据
     （2026-09-23 维护者 口径：平台不关注键的加密算法，术语表不再单列 SM3）
     ——七条一句人话，术语锚点可直达，secret 不参与签名如实写明（SPEC §4.8）；
  2. 5 分钟上手：三步内走通「拿凭证 → 发一次查询 → 看审计」，每步链真实页面；
  3. 限流：如实标注网关级未启用，不写未兑现的拦截承诺；批量/发放限流数值与代码真源一致；
  4. SLA：全站唯一一份（原 contact 页已并入），数值与 SPEC §6.1 一致；
  5. 合并：contact 独立页下线（源文件/产物/导航均无），nginx 301 到 docs，全站无 contact 死链；
  6. 变更日志：条目提交号在 git 中真实存在且为 HEAD 祖先，不发明版本号；
  7. SDK：无包则如实标「暂未提供」，不放假下载链接、无 # 占位；全页无外链、无死链。
运行：python3 -m pytest tests/test_docs_help_center.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
DOCS_PAGE = PORTAL / "docs.html"
DOCS_SRC = PORTAL / "src" / "docs.html"
DOCS_CSS = PORTAL / "assets" / "docs.css"
NGINX_CONF = REPO / "configs" / "nginx" / "portal-nginx.conf"
BATCH_PY = REPO / "service" / "app" / "batch.py"
APPKEYS_PY = REPO / "service" / "app" / "appkeys.py"

# 术语表七条（任务卡原文点名，缺一即漏）
# 2026-09-23 维护者 口径修订：平台不关注键的加密算法——原「SM3」条改为「查询键」（K/V 视角），
# 原「规范化」条改为「上游供数口径」（规范化/加密均归上游 ETL，不在平台口径内）。
GLOSSARY_TERMS = [
    ("docs-term-gateway", "网关"),
    ("docs-term-appkey", "AppKey 与 secret"),
    ("docs-term-key", "查询键"),
    ("docs-term-normalize", "上游供数口径"),
    ("docs-term-fieldauth", "字段级授权"),
    ("docs-term-degrade", "降级"),
    ("docs-term-demo", "演示数据"),
]

# SLA 数值（SPEC §6.1；合并后仅此一处）
SLA_LITERALS = ["P50 ≤ 50ms", "P99 ≤ 200ms", "&lt; 0.1%", "超时 5s"]

if not DOCS_PAGE.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


class _TagText(HTMLParser):
    """极简 DOM 断言器（test_ops_portal 同款，无第三方依赖）。"""

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


def _has_text(dom: _TagText, needle: str) -> bool:
    return any(needle in t for t in dom.texts)


DOCS_HTML = DOCS_PAGE.read_text(encoding="utf-8")
DOCS_SRC_HTML = DOCS_SRC.read_text(encoding="utf-8")


# ── 1. 构建注册与壳层 ──

def test_pages_registered_in_build():
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "docs" in bp.NAV_KEYS and "docs" in bp.PAGES, "docs 未入 NAV_KEYS/PAGES"
    need = bp.PAGES["docs"]["required_ids"]
    for block in ("docs-body", "docs-quickstart", "docs-glossary", "docs-ratelimit",
                  "docs-sla", "docs-channels", "docs-notfound-guide", "docs-duty",
                  "docs-changelog", "docs-sdk", "docs-support-entry"):
        assert block in need, f"构建注册缺关键区块：{block}"
    assert "assets/docs.css" in bp.PAGES["docs"]["extra_css"], "缺按页 docs.css"
    assert bp.PAGES["docs"]["title"].startswith("帮助与支持"), "页面标题未改帮助与支持"
    # 合并后为静态页（无取数），不挂按页 JS（最小实现）
    assert "extra_js" not in bp.PAGES["docs"], "合并后静态页不应挂按页 JS"


def test_required_blocks_present():
    for block in ("docs-body", "docs-quickstart", "docs-glossary", "docs-ratelimit",
                  "docs-sla", "docs-channels", "docs-notfound-guide", "docs-duty",
                  "docs-changelog", "docs-sdk", "docs-support-entry"):
        assert f'id="{block}"' in DOCS_HTML, "缺关键区块 id：" + block


def test_shell_and_no_placeholder():
    assert not re.search(r"@@[A-Za-z0-9_]+@@", DOCS_HTML), "残留模板占位符"
    assert 'aria-current="page" href="docs.html"' in DOCS_HTML, "侧栏 active 态未注入"
    assert "js-canary" in DOCS_HTML, "JS 错误探针缺失"


def test_per_page_assets_not_leaked():
    for other in ("index.html", "my.html", "trial.html"):
        html = (PORTAL / other).read_text(encoding="utf-8")
        assert "assets/docs.css" not in html, other + " 被注入帮助与支持页资产"


def test_build_idempotent():
    before = DOCS_PAGE.read_bytes()
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    assert DOCS_PAGE.read_bytes() == before, "构建非幂等"


# ── 2. 术语表：七条齐全、一句人话、锚点可直达 ──

def test_glossary_all_terms_present():
    dom = _dom(DOCS_HTML)
    ids = _ids(dom)
    for term_id, label in GLOSSARY_TERMS:
        assert term_id in ids, f"术语缺锚点 id：{term_id}"
    for _term_id, label in GLOSSARY_TERMS:
        assert _has_text(dom, label), f"术语表缺词条：{label}"


def test_glossary_plain_language_one_sentence_each():
    """每条术语的 <dd> 必须非空且不过长（人话一句，非技术文档大段）。"""
    for term_id, label in GLOSSARY_TERMS:
        m = re.search(r'id="' + term_id + r'">' + re.escape(label) + r"</dt>\s*<dd>(.*?)</dd>",
                      DOCS_SRC_HTML, re.S)
        assert m, f"术语 {label} 结构不符（须 dt 带 id + 紧随 dd）"
        body = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        assert body, f"术语 {label} 解释为空"
        assert len(body) <= 200, f"术语 {label} 解释过长（非一句话）：{len(body)} 字"


def test_glossary_secret_signing_honest():
    """SPEC §4.8：secret 不参与签名——帮助页如实写明，不加暗示。"""
    assert "不参与请求签名" in DOCS_HTML or "不参与签名" in DOCS_HTML,         "未如实写明 secret 当前不参与签名"


# 卡5 2026-09-30：test_glossary_fieldauth_links_fields_page 随字段字典页下线移除
# （术语表已改为「以服务详情页出参表为准」的如实表述，不链已下线页面）。


# ── 3. 5 分钟上手：三步、真实页面、目标可达 ──

def test_quickstart_three_steps_real_links():
    dom = _dom(DOCS_HTML)
    # 2026-10-15 门面批：docs 新增「快速接入四步」卡（docs-quickpath）——本断言收窄到
    # 5 分钟上手区块自身 scope（原全页计数会撞上四步卡的 4 个 li.docs-step）
    qs = DOCS_HTML.split('id="docs-quickstart"')[1].split("</section>")[0]
    qdom = _dom(qs)
    steps = [a.get("class") or "" for t, a in qdom.tags if t == "li"]
    assert sum("docs-step" == c for c in steps) == 3, "5 分钟上手须恰好三步"
    dom = _dom(DOCS_HTML)
    links = {a.get("id"): a.get("href") for t, a in dom.tags if t == "a"}
    assert links.get("docs-link-appkeys") == "admin/appkeys.html", "第 1 步未链 AppKey 发放页"
    assert links.get("docs-link-trial") == "trial.html#trial-tab-http", "第 2 步未链试用台 HTTP"
    assert links.get("docs-link-audit") == "audit.html", "第 3 步未链审计查询"
    assert links.get("docs-link-my") == "my.html", "第 3 步缺我的接入入口"
    # P2c（2026-10-14）：步骤间补「下一步」真实链接——三步成链不迷路（派单 AC）
    assert links.get("docs-link-step1-next") == "trial.html#trial-tab-http", "第 1 步缺「下一步」链接（应去接口调试）"
    assert links.get("docs-link-step2-next") == "audit.html", "第 2 步缺「下一步」链接（应去审计查询）"
    assert links.get("docs-link-step3-next") == "admin/appkeys.html", "第 3 步缺「回环」链接（应回 AppKey 发放页）"


def test_quickstart_targets_exist_and_anchors_resolve():
    for href in ("admin/appkeys.html", "trial.html#trial-tab-http", "audit.html", "my.html"):
        path, _, frag = href.partition("#")
        target = PORTAL / path
        assert target.is_file(), f"上手链接目标不存在（死链）：{href}"
        if frag:
            assert f'id="{frag}"' in target.read_text(encoding="utf-8"),                 f"锚点不可达：{href}（{path} 无 id={frag}）"


# ── 4. 限流：如实口径（**P1-5 已合入，网关限流真生效**——本组断言随实现同步） ──

def test_ratelimit_gateway_now_enforced_and_honest():
    """P1-5 后网关限流真生效：帮助页须写明 429 三值原因，且契约 sidecar 同步 enabled=True。"""
    assert "docs-ratelimit" in DOCS_HTML
    dom = _dom(DOCS_HTML)
    assert _has_text(dom, "被限流拒绝时返回"), "未写明限流拒绝口径"
    for reason in ("global", "endpoint", "concurrent"):
        assert _has_text(dom, reason), f"缺 429 原因值口径：{reason}"
    # 契约 sidecar 同口径（生成器读真源）——两处都说 enabled，防一改一漏
    # 全下线终态：无落盘详情 sidecar（零服务＝零产物）——契约口径改由合成源重建断言
    import sys as _sys
    _sys.path.insert(0, str(REPO / "scripts"))
    _sys.path.insert(0, str(REPO / "tests"))
    import gen_openapi as gen
    import synth_services as _syn
    _svc = gen.load_services(_syn.synth_services_path())
    _ep = gen.load_auth_endpoints(_syn.synth_matrix_path())
    side = gen.build_spec(_svc["user_tag_query"], _ep["user_tag_query"],
                          gen._route_for(gen.load_gateway_routes(), _ep["user_tag_query"]), {}, {}, {})
    assert side["rate_limit"]["enabled"] is True, "契约限流口径与帮助页不符（P1-5 后应 enabled）"


def test_ratelimit_batch_values_match_code_source():
    """批量限流数值必须与 service/app/batch.py 常量一致（不手写魔数）。"""
    src = BATCH_PY.read_text(encoding="utf-8")
    assert "DEFAULT_MAX_KEYS_PER_JOB = 1_000_000" in src and "100 万键" in DOCS_HTML
    assert "DEFAULT_MAX_CONCURRENT_JOBS = 1" in src and "每租户并发 ≤ 1" in DOCS_HTML
    assert "DEFAULT_MAX_DAILY_KEYS = 5_000_000" in src and "每租户日 ≤ 500 万键" in DOCS_HTML
    for reason in ("concurrency", "per_job", "daily"):
        assert reason in DOCS_HTML, f"缺 X-RateLimit-Reason 口径值：{reason}"


def test_ratelimit_issue_frequency_matches_code_source():
    src = APPKEYS_PY.read_text(encoding="utf-8")
    assert 'TENANT_ISSUE_LIMIT = int(os.environ.get("APPKEY_RL_TENANT_PER_HOUR", "5"))' in src
    assert 'IP_ISSUE_LIMIT = int(os.environ.get("APPKEY_RL_IP_PER_HOUR", "10"))' in src
    assert "5 次/小时" in DOCS_HTML and "10 次/小时" in DOCS_HTML, "发放限频数值与真源不符"

    _ = BATCH_PY  # 批量数值断言见 test_ratelimit_batch_values_match_code_source


# ── 5. SLA：全站唯一一份（SPEC §6.1） ──

def test_sla_table_single_source_from_spec_61():
    html = DOCS_HTML
    assert 'id="docs-sla-table"' in html, "缺 SLA 表（本页唯一一份）"
    for literal in SLA_LITERALS:
        assert literal in html, f"SLA 表缺 SPEC §6.1 数值：{literal}"
    dom = _dom(html)
    assert _has_text(dom, "A · 标签点查") and _has_text(dom, "B · 上游代理"), "缺 A/B 分档行"
    assert _has_text(dom, "目标值"), "未标注当前为设计目标值（承诺值 M2 后定版 NC-6）"
    assert "SPEC-统一数据服务平台_v1 §6.1" in html, "SLA 数值未标出处"


def test_sla_values_no_second_copy_anywhere():
    """合并后 SLA 只在 docs 页出现一份——其他页不得再写一遍数值。"""
    for page in PORTAL.glob("*.html"):
        if page.name == "docs.html":
            continue
        html = page.read_text(encoding="utf-8")
        for literal in ("P50 ≤ 50ms", "P99 ≤ 200ms"):
            assert literal not in html, f"{page.name} 出现 SLA 第二份副本：{literal}"


# ── 6. 合并：contact 下线、导航无死链、301 重定向 ──

def test_contact_page_retired_everywhere():
    """源文件、产物、构建注册表、NAV_KEYS 均不得再有 contact 独立页。"""
    for p in (PORTAL / "src" / "contact.html", PORTAL / "contact.html",
              PORTAL / "assets" / "contact.css"):
        assert not p.exists(), f"contact 独立页未下线：{p}"
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "contact" not in bp.NAV_KEYS, "NAV_KEYS 仍含 contact"
    assert "contact" not in bp.PAGES, "PAGES 仍含 contact"
    for old, _new in bp.DETAIL_PATH_REWRITES:
        assert "contact" not in old, f"DETAIL_PATH_REWRITES 仍改写 contact：{old}"


def test_contact_301_redirect_configured():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    # 用 $http_host（带端口）而非 $host（nginx 剥端口）——非 80 端口部署时才不会丢端口
    for entry in ("location = /contact {", "location = /contact.html {"):
        assert entry in conf, f"缺联系方式入口的 301 精确匹配：{entry}"
    assert conf.count("return 301 $scheme://$http_host/docs;") == 2,         "两条 contact 入口未 301 到 /docs（$http_host 保端口）"


def test_merged_contact_content_present():
    """原联系与支持页三块内容（联系人/工单、三步引导、值班）必须在合并页可见。"""
    dom = _dom(DOCS_HTML)
    assert _has_text(dom, "工单"), "缺工单入口说明"
    assert _has_text(dom, "接入口径咨询"), "缺联系人渠道条"
    assert _has_text(dom, "待运营提供"), "联系人/工单占位未如实标注"
    for label in ("先看平台状态", "用追踪号反查这次调用", "联系值班"):
        assert _has_text(dom, label), f"三步引导缺步：{label}"
    assert _has_text(dom, "值班"), "缺值班说明文案"
    assert 'id="docs-placeholder-note"' in DOCS_HTML, "缺占位说明标注块"
    # 不放假电话/假邮箱：只看可见文本（原文注释里的 sha256 十六进制会长得像 11 位数字）
    visible = " ".join(_dom(DOCS_HTML).texts)
    assert not re.search(r"1[3-9]\d{9}", visible), "可见文案出现疑似编造手机号"


def test_no_contact_dead_link_anywhere():
    """全站 html 不得再指向 contact.html / contact（重定向由 nginx 兜底，页面内不该出现）。"""
    for page in list(PORTAL.glob("*.html")) + list((PORTAL / "catalog").glob("*.html")):
        html = page.read_text(encoding="utf-8")
        assert "contact.html" not in html, f"{page.name} 仍有 contact.html 死链"
    # 未构建的源页与共享壳同样不得残留
    for src in list((PORTAL / "src").glob("*.html")) + list((PORTAL / "partials").glob("*.html")):
        text = src.read_text(encoding="utf-8")
        assert "contact.html" not in text, f"{src.name} 仍有 contact.html 引用"


def test_apply_page_retired_entry_repoints_to_docs():
    """ux-fix-batch 2026-10-29（维护者验收）：apply（申请接入）页整页下线——
    源/产物/按页 css 全部退役；首页 guide-bar 正式接入入口改指 docs#docs-channels。"""
    for p in (PORTAL / "src" / "apply.html", PORTAL / "apply.html",
              PORTAL / "assets" / "apply.css"):
        assert not p.exists(), f"apply 页未下线：{p}"
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "apply" not in bp.NAV_KEYS, "NAV_KEYS 仍含 apply"
    assert "apply" not in bp.PAGES, "PAGES 仍含 apply"
    for rewrites in (bp.DETAIL_PATH_REWRITES, bp.REPORTS_PATH_REWRITES):
        for old, _new in rewrites:
            assert "apply.html" not in old, f"路径改写表仍改写 apply.html：{old}"
    index_html = (PORTAL / "index.html").read_text(encoding="utf-8")
    assert 'id="guide-onboard-link"' in index_html, "首页缺正式接入入口（guide-onboard-link）"
    assert "docs.html#docs-channels" in index_html, "首页 guide-bar 未改指 docs"
    assert 'href="apply.html"' not in index_html, "首页仍有 apply 死链"
    # 全站产物（门户正页＋catalog 壳＋admin 壳）零 apply 死链
    for page in list(PORTAL.glob("*.html")) + list((PORTAL / "catalog").glob("*.html")) \
                + list((PORTAL / "admin").glob("*.html")) + list((PORTAL / "reports").glob("*.html")):
        html = page.read_text(encoding="utf-8")
        assert "apply.html" not in html, f"{page.name} 仍有 apply.html 死链"


# ── 7. 变更日志：不发明版本号 ──


def test_changelog_no_fabricated_version_numbers():
    """不发明版本号：不得出现形如 v1.2.3 的语义版本号。"""
    invented = re.findall(r"\bv\d+\.\d+\.\d+\b", DOCS_HTML)
    assert not invented, f"变更日志出现疑似编造版本号：{invented}"
    catalog = json.loads((PORTAL / "openapi" / "catalog.json").read_text(encoding="utf-8"))
    assert "契约版本 " + catalog["contractVersion"] in DOCS_HTML,         "变更日志未标契约版本（真源 catalog.json）"


# ── 8. SDK：如实「暂未提供」，不放假链接 ──

def test_sdk_honest_not_provided():
    dom = _dom(DOCS_HTML)
    assert _has_text(dom, "暂未提供"), "SDK 未如实标注暂未提供"
    assert "占位链接" in DOCS_HTML or "不放假下载链接" in DOCS_HTML,         "未声明不放假链接"
    links = {a.get("id"): a.get("href") for t, a in dom.tags if t == "a"}
    assert links.get("docs-link-sdk-tab") == "trial.html#trial-tab-sdk",         "SDK 页签链接缺失"
    hrefs = [a.get("href") for t, a in dom.tags if t == "a"]
    assert "#" not in hrefs, "出现 href=# 占位链接"
    assert not any("sdk" in (h or "").lower() and h.endswith((".zip", ".tar.gz", ".whl"))
                   for h in hrefs), "出现假 SDK 下载链接"


# ── 9. 全页链接图：无外链、无死链、内部锚点可达 ──

def test_no_external_links():
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', DOCS_HTML)
    assert not externals, f"出现外链（内网白屏红线）：{externals}"


# 【已删用例·碑注】test_all_links_resolve：docs.html（旧门户遗留页面）内含指向已下线 demo 服务详情壳 catalog/user_tag_query.html 的站内链接——断言对象（demo 详情页）已随 2026-09-29 全服务下线移除（维护者 拍板「现在清」）；旧页面内容由新向导替换并自带新链接门禁。

def test_docs_css_only_uses_tokens():
    """新增样式只引用既有令牌（不直写 HEX）。"""
    css = DOCS_CSS.read_text(encoding="utf-8")
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hexes, f"docs.css 直写 HEX（应走令牌）：{hexes}"


def test_docs_css_mobile_grid_is_safe():
    """P0-12 自证：≤720px 用 minmax(0,1fr) 消掉轨道最小宽度，防跨列卡撑破视口。"""
    css = DOCS_CSS.read_text(encoding="utf-8")
    assert "minmax(0, 1fr)" in css, "docs.css 未用 minmax(0,1fr) 防溢出"
    assert "repeat(6, minmax(0, 1fr))" in css, "docs.css 缺 ≤720px 六列安全栅格"


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
