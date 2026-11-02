#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""site-gate.js 空壳守卫套件（无登录模式决策，tests/test_site_gate.py）。

历史沿革：W2 整站登录守卫（2026-10-15 设计稿 A2/A3/A4：401 驱动＋本地 exp 预判＋
无感刷新＋admin 导航解锁）随无登录模式（维护者 2026-10-28 拍板）整体拆除；本文件
替换为空壳守卫口径（历史行为断言见 git 史）：
  1. 空壳契约：site-gate.js 为最小空壳——不包装 fetch、不读写令牌、不跳转、不操作
     DOM；壳页加载位与加载序门禁不变（build_portal 仍断言其先于取数层装载）；
  2. 加载序门禁（产物级复断言）：全部壳页——门户正页＋catalog 详情壳＋admin 壳
     ——site-gate.js 先于 data-format/api/pages 装载；selftest 页同序；
  3. api.js mock 降级行为不回退：非 auth 失败照旧降级 mock（off 态行为）；auth 401
     不落 mock 的防御分支已随 ux-fix-batch 清理（无登录模式下永不触发）；
  4. 一方代码 XSS 注入面基线（#36 全扫防回潮）。

运行：python3 tests/test_site_gate.py（或 python3 -m pytest tests/test_site_gate.py）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
GATE = PORTAL / "site-gate.js"
API_JS = PORTAL / "api.js"

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402

# 产物缺失时先补齐（保证套件可独立运行；test_admin_shell 同口径）
if not (PORTAL / "admin" / "appkeys.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _run_node(harness: str, *args: str) -> dict:
    assert shutil.which("node") is not None, "需要 node 运行行为断言"
    r = subprocess.run(["node", "-e", harness, *[str(a) for a in args]],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── 1. site-gate.js＝无登录模式空壳 ──

_SHELL_HARNESS = r"""
const fs = require('fs');
const store = {};
let fetches = 0;
let fetchWrapped = false;
const nativeFetch = globalThis.fetch;
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
let navigated = '';
globalThis.location = { href: 'http://localhost:8000/index.html',
  replace: (u) => { navigated = String(u); } };
globalThis.fetch = async () => { fetches += 1; return { ok: false, status: 401 }; };
let domTouched = 0;
globalThis.document = {
  readyState: 'complete',
  getElementById: () => null,
  createElement: () => { domTouched += 1; return { style: {} }; },
  addEventListener: () => {},
  querySelectorAll: () => [],
  body: { appendChild: () => { domTouched += 1; }, firstChild: null, insertBefore: () => { domTouched += 1; } },
};
(async () => {
  (0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
  await new Promise((r) => setTimeout(r, 20));
  fetchWrapped = globalThis.fetch !== (() => { throw 0; }) && typeof globalThis.fetch === 'function'
    && globalThis.fetch.toString().indexOf('[native code]') < 0 && globalThis.fetch !== (() => {});
  await globalThis.fetch('/v1/portal/overview');
  await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify({
    navigated, fetches, domTouched,
    tokensUntouched: Object.keys(store).length === 0,
    exports: typeof globalThis.SiteGate === 'undefined',
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_site_gate_is_no_login_shell():
    """无登录模式决策：site-gate.js 为空壳——零跳转、零请求、零 token 读写、零 DOM 操作、
    不包装 fetch（全局 fetch 保持原样）、不再导出 SiteGate 行为面。"""
    out = _run_node(_SHELL_HARNESS, GATE)
    assert out["navigated"] == "", "空壳不得发生任何跳转"
    assert out["fetches"] <= 1, "空壳自身不得发请求（仅测试探针那一次）"
    assert out["domTouched"] == 0, "空壳不得操作 DOM（导航解锁/登录条已拆除）"
    assert out["tokensUntouched"] is True, "空壳不得读写 localStorage 令牌键"
    assert out["exports"] is True, "空壳不导出 SiteGate 行为面（upstreams.js 有守卫回落）"


def test_site_gate_static_no_login_machinery():
    """静态口径（剥注释判代码）：登录机制标识零出现；碑注在案。"""
    raw = _read(GATE)
    js = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    js = re.sub(r"^\s*//.*$", "", js, flags=re.M)
    for absent in ("LOGIN_PAGE", "redirectToLogin", "root.fetch = function",
                   "oneserve-oidc-token", "oneserve-auth-return", "revealAdminOnly",
                   "isAdmin", "tryRefresh", "XMLHttpRequest"):
        assert absent not in js, f"site-gate.js 空壳不得含登录机制代码：{absent}"
    assert "无登录模式" in raw and "无登录模式决策" in raw, "空壳须带无登录模式碑注"


# ── 2. 加载序门禁（产物级复断言） ──

def test_gate_order_in_all_shell_pages():
    """全部壳页 site-gate.js（空壳）先于 data-format/api/pages：门户正页＋catalog
    详情＋admin 壳——壳页引用与加载序契约不随登录拆除回退。"""
    pages = [PORTAL / f"{name}.html" for name in bp.PAGES]
    pages += sorted((PORTAL / "catalog").glob("*.html"))
    pages += [PORTAL / "admin" / meta["out"] for meta in bp.ADMIN_PAGES.values()]
    assert len(pages) >= 25, f"产物页数异常：{len(pages)}"
    for p in pages:
        html = _read(p)
        pos_gate = html.find('src="site-gate.js"')
        if pos_gate < 0:
            pos_gate = html.find('src="../site-gate.js"')
        assert pos_gate >= 0, f"{p.name} 未注入 site-gate.js（壳页引用门禁）"
        for js in ("data-format.js", "api.js", "pages.js"):
            pos = html.find(f'"{js}"')
            assert not (0 <= pos < pos_gate), f"{p.name} 加载序违规：{js} 先于 site-gate.js"


def test_selftest_pages_gate_wired():
    """selftest 页（portal×4＋admin×5）仍挂 site-gate.js 且先于页面脚本装载。"""
    portal_selftests = ["selftest.html", "selftest-p2.html", "selftest-p03.html",
                        "selftest-ops.html"]
    admin_selftests = ["selftest-admin.html", "selftest-publish.html",
                       "selftest-upstreams.html", "selftest-grants.html",
                       "selftest-tenants.html"]
    for rel in portal_selftests + [f"admin/{f}" for f in admin_selftests]:
        html = _read(PORTAL / rel)
        pos_gate = html.find('src="site-gate.js"')
        if pos_gate < 0:
            pos_gate = html.find('src="../site-gate.js"')
        assert pos_gate >= 0, f"{rel} 缺 site-gate.js 接线"
        first_js = min((html.find(f'"{j}"') for j in
                        ("data-format.js", "api.js", "pages.js", "admin-gate.js",
                         "selftest-publish.js", "assets/selftest-p03.js") if html.find(f'"{j}"') >= 0),
                       default=-1)
        assert first_js < 0 or pos_gate < first_js, f"{rel} site-gate.js 须先于页面脚本装载"
        assert "login.html" not in html and "auth-callback" not in html, \
            f"{rel} 残留登录页引用（无登录模式决策 已拆）"


# ── 3. api.js mock 降级行为（非 auth 失败照旧降级） ──

_API_HARNESS = r"""
const fs = require('fs');
const which = process.argv[1];   // server500（node -e：argv[1] 起为用户参数）
let calls = [];
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.location = { href: 'http://localhost:8000/index.html', replace: () => {} };
globalThis.fetch = async (url) => {
  calls.push(String(url));
  const isMock = String(url).indexOf('mock/') >= 0;
  if (isMock) return { ok: true, status: 200, json: async () => ({ demo: true }) };
  return { ok: false, status: 500, json: async () => ({ error: 'internal_error' }) };
};
(async () => {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));   // site-gate 空壳先装（加载序硬规矩）
  (0, eval)(fs.readFileSync(process.argv[3], 'utf8'));   // api.js 后装
  let out;
  try {
    const res = await globalThis.PortalAPI.loadSection('overview');
    out = { resolved: true, source: res.source };
  } catch (e) {
    out = { resolved: false, authRequired: !!(e && e.authRequired), msg: String(e && e.message) };
  }
  console.log(JSON.stringify(Object.assign(out, { calls: calls.length,
    mockTried: calls.some((u) => u.indexOf('mock/') >= 0) })));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_api_js_mock_fallback_unchanged():
    """非 auth 失败照旧降级 mock（source=demo）——无登录模式下 api.js 取数行为不回退；
    auth 401 死分支（W2/W3 登录墙遗留，后端无登录不再回该码）已随 ux-fix-batch 清理。"""
    server = _run_node(_API_HARNESS, "server500", GATE, API_JS)
    assert server["resolved"] is True and server["source"] == "demo", \
        "非 auth 失败须照旧降级 mock（既有行为不回退）"
    assert server["mockTried"] is True


# ---------- #36 XSS 全扫防回潮（2026-10-15 复审 P2-3） ----------

def test_first_party_xss_scan_baseline():
    """#36：门户/管理端一方 JS/HTML 注入面基线——innerHTML 动态插值须过 escapeHtml。

    机检口径（可执行的「清零」）：
      ① eval(/document.write/new Function/x-html/v-html 在一方代码零出现（vendor 除外）；
      ② innerHTML 只许出现在白名单文件（selftest 系渲染器），且这些文件的动态插值行
         必须走 escapeHtml/_esc 包裹——新注入点＝红（admin-gate.js 已随无登录模式
         拆为空壳，不再持有 innerHTML）；
      ③ admin-gate.js 导出 escapeHtml（基线函数单一来源，selftest 复用）。
    """
    VENDOR = ("assets/vendor/", "alpine.min.js", "echarts.common.min.js",
              "bootstrap.bundle.min.js")
    first_party = []
    for root_dir in (PORTAL, REPO / "admin"):
        for f in sorted(root_dir.rglob("*")):
            if not f.is_file() or f.suffix not in (".js", ".html"):
                continue
            rel = str(f.relative_to(REPO))
            if any(v in rel for v in VENDOR):
                continue
            first_party.append((rel, f))
    # ① 危险原语零出现
    for rel, f in first_party:
        text = _read(f)
        for bad in ("eval(", "document.write", "new Function("):
            assert bad not in text, f"{rel} 出现 {bad}（#36 基线：一方代码禁动态求值）"
        for bad in ("x-html", ":v-html"):
            assert bad not in text, f"{rel} 出现 {bad}（Alpine/Vue HTML 注入指令禁用）"
    # ② innerHTML 白名单＋escape 包裹
    ALLOW = ("selftest-admin.js", "selftest-publish.js", "selftest-upstreams.js",
             "selftest-p03.js", "selftest.html", "selftest-p2.html",
             "selftest-ops.html", "selftest-grants.js",   # 1b-8 授权管理自测渲染器（escapeHtml 包裹同 upstreams 惯例）
             "selftest-tenants.js",                       # 管理功能群一期租户管理自测渲染器（escapeHtml 包裹同 grants 惯例）
             )
    for rel, f in first_party:
        text = _read(f)
        if "innerHTML" not in text:
            continue
        assert any(rel.endswith(a) for a in ALLOW), \
            f"{rel} 新增 innerHTML 注入点（#36 基线：动态渲染一律 textContent/escapeHtml）"
        if rel.endswith(".js"):
            assert "escapeHtml" in text or "_esc(" in text, \
                f"{rel} innerHTML 渲染器未见 escapeHtml/_esc 包裹（#36 基线）"
        else:
            assert "_esc(" in text, f"{rel} innerHTML 渲染器未见 _esc 包裹（#36 基线）"
    # ③ 基线函数导出在位（admin 源＋portal 产物双侧）
    for rel in ("admin/admin-gate.js", "portal/admin/admin-gate.js"):
        assert "escapeHtml: escapeHtml," in _read(REPO / rel), \
            f"{rel} 未导出 escapeHtml（selftest 复用基线的单一来源）"
    print("  ok test_first_party_xss_scan_baseline（一方代码注入面清零＋白名单钉死）")


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
