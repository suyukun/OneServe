#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整站登录守卫套件（W2/W3 · 2026-10-15 设计稿 A2/A3/A4，tests/test_site_gate.py）。

守卫口径（设计稿 A2 点名的加载序硬规矩＋A3 本地 exp 预判＋A4 影响面收口）：
  1. site-gate.js 行为（node 直调，免浏览器）：本地 exp 过期→存回址跳
     /login.html；无 token/exp 未过→零跳转零请求（W1 off 态浏览行为不变）；
     fetch 包装器 401 auth_required→清 token＋存回址＋跳登录；非 auth_required
     的 401（下游 appkey 无效）原样透传不跳；
  2. 加载序门禁（build_portal.check_gate_order 产物级复断言）：全部壳页——
     门户正页＋catalog 详情壳＋admin 壳页——site-gate.js 先于
     data-format/api/pages 装载（防 401 落 api.js mock 降级出假「演示数据」页）；
  3. mock 降级收口（A4①）：api.js 对 401 auth_required 打 authRequired 标记，
     loadSection 不落 mock（node 直调断言：auth 401 只发 1 个请求；非 auth
     失败照旧降级 mock——off 态行为不回退）；
  4. login.html 回址校验（A2「限本站 origin＋站内路径」放宽版）：跨 origin 拒、
     协议相对（//）拒、#hash 剥离、缺省回 AppKey 页；本页不载 site-gate/admin-gate
     （防循环）；
  5. 进墙标注（A4④）：portal×4＋admin×3 selftest 页注入守卫脚本＋标注注释
     （设计稿口径 admin×5，实盘 3 页——如实按实有页标注）；
  6. admin 导航「仅 admin 可见」（B2）：侧栏 accounts 项默认 display:none 挂
     data-os-admin-only，site-gate isAdmin tag 口径识别（收口批 2a 已按 W4 定稿
     ADR-0016 Decision.2 收窄：顶层 tag claim 即角色，与 admin_accounts.py
     claims_is_admin／auth_oidc.role_from_claims 同口径；其余 claim 一律不信）。

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
LOGIN = PORTAL / "login.html"
SIDEBAR = PORTAL / "partials" / "sidebar.html"

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402

# 产物缺失时先补齐（保证套件可独立运行；test_admin_shell 同口径）
if not (PORTAL / "admin" / "appkeys.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

HAS_NODE = shutil.which("node") is not None


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _run_node(harness: str, *args: str) -> dict:
    assert HAS_NODE, "需要 node 运行 site-gate 行为断言"
    r = subprocess.run(["node", "-e", harness, *[str(a) for a in args]],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── 1. site-gate.js 行为（node 直调） ──

_GATE_HARNESS = r"""
const fs = require('fs');
const mode = process.argv[1];   // no-token | valid | expired（node -e：argv[1] 起为用户参数）
const store = {};
let fetches = 0;
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.location = { href: 'http://localhost:8000/admin/appkeys.html',
  replace: (u) => { globalThis.__nav = String(u); } };
globalThis.fetch = async () => { fetches += 1; return { ok: false, status: 401 }; };
if (mode === 'valid' || mode === 'expired') {
  const claims = { sub: 'oneserve/tester', exp: Math.floor(Date.now() / 1000) + (mode === 'valid' ? 600 : -10) };
  const payload = Buffer.from(JSON.stringify(claims)).toString('base64url');
  store['oneserve-oidc-token'] = 'h.' + payload + '.s';
}
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
const SG = globalThis.SiteGate;
console.log(JSON.stringify({
  nav: globalThis.__nav || '', fetches,
  returnKey: store['oneserve-auth-return'] || '',
  token: store['oneserve-oidc-token'] || '',
  exports: ['readToken', 'jwtClaims', 'isExpired', 'isAdmin', 'revealAdminOnly',
            'redirectToLogin', 'saveReturnUrl', 'TOKEN_KEY', 'RETURN_KEY', 'LOGIN_PAGE']
            .every((k) => k in SG),
}));
"""


def test_site_gate_boot_behaviors():
    """boot 双机制：exp 过→跳登录存回址；无 token/exp 未过→零跳转零请求。"""
    none = _run_node(_GATE_HARNESS, "no-token", GATE)
    assert none["nav"] == "" and none["fetches"] == 0, \
        "无 token（W1 off 态）须零跳转零请求——浏览行为不变"
    valid = _run_node(_GATE_HARNESS, "valid", GATE)
    assert valid["nav"] == "" and valid["fetches"] == 0, "exp 未过不得跳转"
    assert valid["exports"] is True, "导出 API 面须完整（selftest/单测直调）"
    expired = _run_node(_GATE_HARNESS, "expired", GATE)
    assert expired["nav"] == "/login.html", "本地 exp 预判过期须跳 /login.html（A3 增强）"
    assert expired["returnKey"] == "http://localhost:8000/admin/appkeys.html", \
        "跳登录前须存回址 oneserve-auth-return"


_401_HARNESS = r"""
const fs = require('fs');
const which = process.argv[1];   // auth_required | other（node -e：argv[1] 起为用户参数）
const store = {};
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
const payload = Buffer.from(JSON.stringify({ sub: 'oneserve/tester', exp: Math.floor(Date.now() / 1000) + 600 })).toString('base64url');
store['oneserve-oidc-token'] = 'h.' + payload + '.s';
globalThis.location = { href: 'http://localhost:8000/grants.html',
  replace: (u) => { globalThis.__nav = String(u); } };
globalThis.fetch = async () => ({ ok: false, status: 401,
  clone() { return { json: async () => ({ error: which === 'auth_required' ? 'auth_required' : 'appkey_invalid' }) }; } });
(async () => {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
  await fetch('/v1/portal/overview');
  await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify({ nav: globalThis.__nav || '',
    tokenCleared: !store['oneserve-oidc-token'],
    returnKey: store['oneserve-auth-return'] || '' }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_site_gate_401_wrapper():
    """fetch 包装器：auth 401→清 token＋存回址＋跳登录；非 auth 401 透传不跳。"""
    auth = _run_node(_401_HARNESS, "auth_required", GATE)
    assert auth["nav"] == "/login.html" and auth["tokenCleared"] is True, \
        "401 auth_required 须清 token 跳 /login.html"
    assert auth["returnKey"] == "http://localhost:8000/grants.html", "回址须为来源门户页"
    other = _run_node(_401_HARNESS, "other", GATE)
    assert other["nav"] == "" and other["tokenCleared"] is False, \
        "非 auth_required 的 401 须原样透传（不跳不清理）"


# ── 2. 加载序门禁（产物级复断言） ──

def test_gate_order_in_all_shell_pages():
    """全部壳页 site-gate.js 先于 data-format/api/pages：门户正页＋catalog 详情＋admin 壳。"""
    pages = [PORTAL / f"{name}.html" for name in bp.PAGES]
    pages += sorted((PORTAL / "catalog").glob("*.html"))
    pages += [PORTAL / "admin" / meta["out"] for meta in bp.ADMIN_PAGES.values()]
    assert len(pages) >= 25, f"产物页数异常：{len(pages)}"
    for p in pages:
        html = _read(p)
        pos_gate = html.find('src="site-gate.js"')
        if pos_gate < 0:
            pos_gate = html.find('src="../site-gate.js"')
        assert pos_gate >= 0, f"{p.name} 未注入 site-gate.js（W2 门禁）"
        for js in ("data-format.js", "api.js", "pages.js"):
            pos = html.find(f'"{js}"')
            assert not (0 <= pos < pos_gate), f"{p.name} 加载序违规：{js} 先于 site-gate.js"


def test_selftest_pages_gated_and_annotated():
    """selftest 7 页（portal×4＋admin×3）守卫在位＋进墙标注（A4④）。

    设计稿口径「admin×5」实盘为 3 页 selftest html（auth-callback.html 是 pre-token
    功能页不进墙）——如实按实有页接线并在注释标差。"""
    portal_selftests = ["selftest.html", "selftest-p2.html", "selftest-p03.html",
                        "selftest-ops.html"]
    admin_selftests = ["selftest-admin.html", "selftest-publish.html",
                       "selftest-upstreams.html"]
    for rel, src in ([(f, f) for f in portal_selftests] +
                     [(f"admin/{f}", f) for f in admin_selftests]):
        html = _read(PORTAL / rel)
        src_html = _read(REPO / "admin" / src) if rel.startswith("admin/") else html
        pos_gate = html.find('src="site-gate.js"')
        if pos_gate < 0:
            pos_gate = html.find('src="../site-gate.js"')
        assert pos_gate >= 0, f"{rel} 缺 site-gate 接线（进墙页）"
        first_js = min((html.find(f'"{j}"') for j in
                        ("data-format.js", "api.js", "pages.js", "admin-gate.js",
                         "selftest-publish.js", "assets/selftest-p03.js") if html.find(f'"{j}"') >= 0),
                       default=-1)
        assert first_js < 0 or pos_gate < first_js, f"{rel} 守卫须先于页面脚本装载"
        assert "进墙标注" in src_html or "进墙标注" in html, f"{rel} 缺进墙标注注释"
    # pre-token 页不进墙（防循环）：login 与 auth-callback
    for pre in (PORTAL / "login.html", PORTAL / "admin" / "auth-callback.html"):
        html = _read(pre)
        assert 'src="site-gate.js"' not in html and 'src="../site-gate.js"' not in html, \
            f"{pre.name} 是 pre-token 页，不得载 site-gate（防循环）"


# ── 3. api.js mock 降级收口（A4①） ──

_API_HARNESS = r"""
const fs = require('fs');
const which = process.argv[1];   // auth401 | server500（node -e：argv[1] 起为用户参数）
let calls = [];
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.location = { href: 'http://localhost:8000/index.html', replace: () => {} };
globalThis.fetch = async (url) => {
  calls.push(String(url));
  const isMock = String(url).indexOf('mock/') >= 0;
  if (isMock) return { ok: true, status: 200, json: async () => ({ demo: true }) };
  if (which === 'auth401') {
    return { ok: false, status: 401,
             json: async () => ({ error: 'auth_required', message: '未登录' }) };
  }
  return { ok: false, status: 500, json: async () => ({ error: 'internal_error' }) };
};
(async () => {
  (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));   // site-gate 先装（加载序硬规矩）
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


def test_api_js_skips_mock_on_auth_401():
    """auth 401→loadSection 直抛（authRequired 标记），**不落 mock**（防假「演示数据」页）；
    非 auth 失败照旧降级 mock（source=demo）——off 态行为不回退（A4① 收口点）。"""
    auth = _run_node(_API_HARNESS, "auth401", GATE, API_JS)
    assert auth["resolved"] is False and auth["authRequired"] is True, auth
    assert auth["mockTried"] is False and auth["calls"] == 1, \
        f"auth 401 不得触发 mock 降级：{auth}"
    server = _run_node(_API_HARNESS, "server500", GATE, API_JS)
    assert server["resolved"] is True and server["source"] == "demo", \
        "非 auth 失败须照旧降级 mock（既有行为不回退）"
    assert server["mockTried"] is True


# ── 4. 回址链契约（ADR-0018 授权码流版，2026-10-20 重写） ──
# 旧版：safeReturnUrl 函数住在 login.html（RETURN-GUARD 标记）。授权码流后 login.html
# 是引导壳（零表单零回址逻辑），回址消费移到 auth-callback.html（本站 origin 前缀校验，
# 行为断言在 tests/test_login_page.py#test_callback_state_and_return_contract）。

def test_login_return_guard():
    """回址链：site-gate 401 时存 oneserve-auth-return 再跳登录；auth-callback 按本站
    origin 前缀消费（防开放跳转不放松）；旧「限 /admin/」口径不得回潮。"""
    gate_js = _read(REPO / "portal" / "site-gate.js")
    assert "oneserve-auth-return" in gate_js, "site-gate 须存回址（登录后回原址）"
    cb_html = _read(REPO / "admin" / "auth-callback.html")
    assert "location.origin + '/'" in cb_html, "回址消费须限本站 origin 前缀"
    assert "location.origin + '/admin/'" not in cb_html, "旧「限 /admin/」口径不得回潮"
    assert "back.split('#')[0]" in cb_html, "#hash 须剥离"


def test_login_page_no_gate_and_default_target():
    """login.html（引导壳）不载任何 gate（防循环）；缺省回跳在 auth-callback＝门户首页。"""
    html = _read(LOGIN)
    assert 'src="site-gate.js"' not in html and 'src="admin-gate.js"' not in html,         "登录页不得载 gate（防循环跳转）"
    cb_html = _read(REPO / "admin" / "auth-callback.html")
    assert "location.replace('/');" in cb_html, "缺省回跳＝门户首页（ADR-0018 门户共用登录）"


# ── 5. admin 导航仅 admin 可见（B2） ──

_ISADMIN_HARNESS = r"""
const fs = require('fs');
const cases = JSON.parse(process.argv[1]);
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
globalThis.location = { href: 'http://localhost:8000/', replace: () => {} };
globalThis.fetch = async () => { throw new Error('加载期不得发请求'); };
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));
const SG = globalThis.SiteGate;
const out = {};
for (const [name, claims] of Object.entries(cases)) { out[name] = SG.isAdmin(claims); }
console.log(JSON.stringify(out));
"""


def test_admin_nav_visibility_contract():
    """accounts 导航项默认隐藏＋data-os-admin-only；site-gate isAdmin 只认顶层
    tag claim（收口批 2a 收窄，与 admin_accounts.py claims_is_admin 同口径——
    W4 定稿 ADR-0016 Decision.2：tokenFormat=JWT 顶层 tag claim 即角色）。"""
    sidebar = _read(SIDEBAR)
    i = sidebar.find('href="admin/accounts.html"')
    assert i >= 0, "侧栏缺 accounts 入口"
    li_start = sidebar.rfind("<li", 0, i)
    li_tag = sidebar[li_start:i]
    assert 'data-os-admin-only' in li_tag and 'display:none' in li_tag, \
        "accounts 导航项须默认隐藏（仅 admin 可见，B2）"
    assert "@@A_admin_accounts@@" in sidebar and "@@C_admin_accounts@@" in sidebar, \
        "accounts 占位键须在侧栏（构建期注入 active）"
    assert "admin_accounts" in bp.NAV_KEYS, "admin_accounts 须入 NAV_KEYS"
    cases = {
        "tagAdmin": {"tag": "admin"}, "tagSpaced": {"tag": " Admin "},
        "tagUpper": {"tag": "ADMIN"},
        "tagRolePrefix": {"tag": "role:admin"},
        "rolesArr": {"roles": ["admin"]}, "roleStr": {"role": "admin"},
        "isFlag": {"isAdmin": True},
        "viewer": {"tag": "viewer"}, "operator": {"tag": "operator"},
        "empty": {},
    }
    out = _run_node(_ISADMIN_HARNESS, json.dumps(cases), GATE)
    for name in ("tagAdmin", "tagSpaced", "tagUpper"):
        assert out[name] is True, f"isAdmin 须识别 {name}（tag 归一后）"
    for name in ("tagRolePrefix", "rolesArr", "roleStr", "isFlag",
                 "viewer", "operator", "empty"):
        assert out[name] is False, f"isAdmin 收窄后须不信 {name}（只认 tag claim）"


def test_gate_zero_fetch_at_load_static():
    """site-gate.js 加载期零网络请求（静态口径；行为口径已由 boot 断言 fetches==0）。"""
    js = re.sub(r"/\*.*?\*/", "", _read(GATE), flags=re.S)
    js = re.sub(r"^[ \t]*//.*$", "", js, flags=re.M)
    # 包装器＝绑定原生 fetch 后以 _nativeFetch 代发（实现不裸写 fetch( 字面）
    assert "root.fetch = function" in js and "_nativeFetch(input, init)" in js, \
        "缺 fetch 包装器（401 驱动机制本体）"
    assert "XMLHttpRequest" not in js and "new WebSocket" not in js
    assert "XMLHttpRequest" not in js and "new WebSocket" not in js



# ---------- #36 XSS 全扫防回潮（2026-10-15 复审 P2-3） ----------

def test_first_party_xss_scan_baseline():
    """#36：门户/管理端一方 JS/HTML 注入面基线——innerHTML 动态插值须过 escapeHtml。

    机检口径（可执行的「清零」）：
      ① eval(/document.write/new Function/x-html/v-html 在一方代码零出现（vendor 除外）；
      ② innerHTML 只许出现在白名单文件（selftest 系渲染器＋admin-gate 登录条），
         且这些文件的动态插值行必须走 escapeHtml/_esc 包裹——新注入点＝红；
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
             "admin-gate.js")
    for rel, f in first_party:
        text = _read(f)
        if "innerHTML" not in text:
            continue
        assert any(rel.endswith(a) for a in ALLOW), \
            f"{rel} 新增 innerHTML 注入点（#36 基线：动态渲染一律 textContent/escapeHtml）"
        if rel.endswith("admin-gate.js"):
            assert "escapeHtml(loginDisplayName())" in text and 'escapeHtml(loginUrl())' in text, \
                "登录条两处动态插值须保持 escapeHtml 包裹（回归哨兵）"
        elif rel.endswith(".js"):
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
