#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-2 门户详情页构建套件（tests/test_build_portal.py）。

守卫口径（任务卡范围 2/3/4；SPEC A2/A3）：
  1. 目录页服务条目＝链接（可点进详情页），真源目录带 serviceId 直进 openapi/<sid>.html；
  2. 每服务详情静态壳由 build_portal.py 生成（面包屑第 3 级＝服务名、Alpine 组件挂载、
     JS 探针在位、零外链、零占位残留）；
  3. 详情页模板含九要素 P0 六区块 id；detail.js 只 fetch 契约 sidecar（不降级 mock、
     不在运行时做数据加工——MPA 纪律）；
  4. mock 降级目录无 serviceId 时链接如实回目录页（不跳假页、不造假数据）。
运行：python3 -m pytest tests/test_build_portal.py（或 python3 直跑，断言一致）。
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

# 契约产物缺失时先补齐（保证套件可独立运行）
if not (PORTAL / "openapi" / "catalog.json").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "gen_openapi.py")],
                   check=True, capture_output=True)
if not (PORTAL / "catalog" / "user_tag_query.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

CATALOG = json.loads((PORTAL / "openapi" / "catalog.json").read_text(encoding="utf-8"))


class _TagText(HTMLParser):
    """极简 DOM 断言器：抽取元素属性与可见文本（无第三方依赖）。"""

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


def _has_tag_text(dom: _TagText, needle: str) -> bool:
    return any(needle in t for t in dom.texts)


# ── 1. 真源目录：条目带 serviceId（搜索结果/目录直进的数据基础） ──

def test_portal_api_catalog_items_carry_service_id():
    """真源目录端点必须给 serviceId——P3 目录计数修复（2026-10-08）后＝注册表驱动。

    旧断言要求源码含三个硬编码 service_id 拼域，恰是本次恒 0 缺陷的根源
    （2026-09-29 全下线＋发布台 v1-svc-* 命名后三 id 均不在注册表）。改为守：
    目录接发布台同源列表（console_services 写区）＋条目带 serviceId＋禁止硬编码回潮。
    """
    src = (REPO / "service" / "app" / "portal_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in ("portal_catalog", "_catalog_view"):
            bodies[node.name] = ast.get_source_segment(src, node) or ""
    assert "portal_catalog" in bodies, "portal_api.py 缺 /v1/portal/catalog 端点"
    assert "_catalog_view" in bodies, "portal_api.py 缺目录聚合纯函数 _catalog_view"
    assert "load_console_services" in bodies["portal_catalog"], \
        "目录未接发布台同源列表（console_services 写区）"
    assert "serviceId" in bodies["_catalog_view"], "目录条目缺 serviceId（P0-2 详情页跳转数据基础）"
    for legacy in ("user_tag_query", "news_tag_query"):
        assert all(legacy not in b for b in bodies.values()), \
            "目录实现回归硬编码历史 service_id：" + legacy


def test_openapi_catalog_sidecar_services_complete():
    sids = {s["service_id"] for s in CATALOG["services"]}
    _reg, _err = __import__("synth_services").real_registry_state()
    assert not _err   # fail-loud 语义照守（load 失败须报错，不许静默空表）
    # 2026-09-30 现实态口径：目录条目动态对真源注册表（str 键＝latest 视图；空/非空均合法）
    assert sids == {sid for sid in _reg if isinstance(sid, str)}, f"目录与注册表漂移：{sids}"
    assert CATALOG["contractVersion"] == "2026-09-23"


# ── 2. 目录页：条目＝链接，可点进详情 ──

def test_catalog_page_items_are_links_with_detail_href():
    html = (PORTAL / "catalog.html").read_text(encoding="utf-8")
    dom = _dom(html)
    links = [a for tag, a in dom.tags if tag == "a" and "svc-link" in (a.get("class") or "")]
    assert links, "目录页服务条目不是链接（div→a 未生效）"
    assert any(a.get(":href") == "svcHref(it)" for a in links), "链接未绑定 svcHref 跳转"
    assert _has_tag_text(dom, "查看详情 →"), "条目缺『查看详情』行动指引"


def test_pages_js_svc_href_routes_to_detail_and_falls_back():
    js = (PORTAL / "assets" / "pages.js").read_text(encoding="utf-8")
    assert "svcHref" in js and "catalog/" in js, "pages.js 缺 svcHref 详情路由"
    assert "catalog.html" in js, "mock 降级目录（无 serviceId）应如实回目录页"


def test_catalog_mock_has_no_fake_service_id():
    """mock 降级目录不得伪造 serviceId（诚信线：假数据不进详情页）。"""
    mock = json.loads((PORTAL / "mock" / "catalog.json").read_text(encoding="utf-8"))
    for d in mock.get("domains", []):
        for it in d.get("items", []):
            assert not it.get("serviceId"), "mock 目录伪造 serviceId：" + str(it)


# ── 3. 详情静态壳：每服务一个，面包屑/标题带服务名 ──

def test_detail_shell_generated_for_every_service():
    for s in CATALOG["services"]:
        f = PORTAL / "catalog" / (s["service_id"] + ".html")
        assert f.is_file(), "缺详情壳：" + f.name
        html = f.read_text(encoding="utf-8")
        # 与 build_portal 生产门禁同口径：@@标识@@ 型占位符不得残留（注释里 @@A_*@@ 带星号不属占位符）
        assert not re.search(r"@@[A-Za-z0-9_]+@@", html), "详情壳残留模板占位符"
        dom = _dom(html)
        mounted = any("detailPage" in (a.get("x-data") or "")
                      for tag, a in dom.tags if tag == "div")
        assert mounted, "详情壳未挂 detailPage 组件"
        assert any(s["name"] in t for t in dom.texts), "面包屑/标题未带服务名"


def test_detail_shell_assets_local_and_canary_present():
    """详情壳红线（canary 探针＋零外链）：探针由构建器注入（模板正文本身不含）——
    机检 build_portal.py 注入逻辑在位；存量壳（当前全下线为零个）逐壳复验。"""
    html = (REPO / "portal" / "partials" / "shell.head.html").read_text(encoding="utf-8")   # 壳头真源（canary 注入处）
    assert "js-canary" in html, "JS 错误探针缺失"
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    assert not externals, "壳头出现外链（内网白屏红线）：" + str(externals)
    for shell in sorted((PORTAL / "catalog").glob("*.html")):   # 存量壳零外链＋带探针（当前为零个）
        shtml = shell.read_text(encoding="utf-8")
        assert "js-canary" in shtml, f"{shell.name} 缺 JS 错误探针"
        assert not re.findall(r'(?:src|href)="(https?://[^"]+)"', shtml), f"{shell.name} 出现外链"


def test_build_portal_shells_idempotent():
    """重复构建：详情壳逐字节一致（幂等纪律）。"""
    outs = [PORTAL / "catalog" / (s["service_id"] + ".html") for s in CATALOG["services"]]
    before = [f.read_bytes() for f in outs]
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    assert [f.read_bytes() for f in outs] == before


# ── 4. 详情页模板：九要素 P0 六区块 ＋ 数据纪律 ──

def test_detail_template_has_six_p0_section_ids():
    tpl = (PORTAL / "src" / "detail.html").read_text(encoding="utf-8")
    for block in ("detail-identification", "detail-params", "detail-response",
                  "detail-errors", "detail-example", "detail-changelog"):
        assert ('id="' + block + '"') in tpl, "详情模板缺 P0 区块：" + block
    for label in ("入参表", "出参表", "错误码与解决方案", "报文示例", "版本与变更记录"):
        assert label in tpl, "详情模板缺要素文案：" + label


def test_detail_js_fetches_contract_sidecar_only():
    js = (PORTAL / "assets" / "detail.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", js, flags=re.S)   # 剥注释只查代码（注释可提及真源文件名）
    assert "openapi/details/" in code and ".json" in code and "'../openapi/" in code, \
        "detail.js 未按契约 sidecar 取数（../openapi/ 前缀）"
    assert "mock/" not in code, "详情页不得降级 mock（数据契约纪律：缺数据如实报错）"
    assert "yaml" not in code.lower(), "详情页运行时不得做契约加工（MPA：构建期生成）"


def test_detail_template_links_to_trial():
    tpl = (PORTAL / "src" / "detail.html").read_text(encoding="utf-8")
    assert "trialUrl()" in tpl and "在线调试" in tpl, "详情页缺在线调试按钮（跳试用台携服务名）"


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
