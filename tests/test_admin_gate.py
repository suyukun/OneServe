#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-6 导航可达＋管理令牌闸恒放行 shim 套件（tests/test_admin_gate.py）。

决策史（拆一次建一次都有留痕，详见 docs/adr/0010）：
  · 2026-09-23 维护者 拍板整体拆除（35ab1aa，本套件随之建立「已拆除」反断言）；
  · 2026-10-09 维护者 按生产目标拍板恢复（bcf34f8：路由级 X-Admin-Token 闸＋前端
    状态驱动闸；31ed94c 解锁条全站接线修复；306fb58 占位值改 admin321；本套件
    当日反转为「真实闸」断言）；
  · 2026-10-14 维护者 亲测两轮被卡（全站无令牌输入入口→接线修复；admin321 改值→
    跨标签页 sessionStorage 失忆再卡），拍板第三次反转：现在不需要管理令牌，拆；
    未来统一做系统登录页（已记待办）。部署到公司环境前必须先有登录页或恢复闸
    ＝硬前提（ADR-0010）。

本套件守卫口径（恒放行 shim 形态的真正契约）：
  1. 导航可达：门户侧栏「管理」组三链（发布台/AppKey 发放/上游代理）＋「我的接入」
     在位且目标文件存在（无死链）；运营三页面包屑＝系统路径两段；
  2. 闸恒放行（机制骨架保留）：三页源/产物均不含令牌栏（admin-token-input/
     admin-token-save）、只读 banner（gate-banner）、令牌错误行（token-error）、
     开发占位提示（dev-token-hint）；admin-gate.js 为恒放行 shim——canWrite 恒 true/
     gateLocked 恒 false/verifyToken 恒 ok/banner 恒空/不注入解锁条（autoWireUnlockBar
     短路），导出 API 面与令牌常量保留（登录页时代低成本复用）；后端 _admin_gate
     恒放行（骨架/挂点/异常保留，不再 raise）；页面 js 令牌头仍统一走
     AdminGate.adminHeaders() 单一来源（shim 下无令牌即空对象）；
  3. 构建一致性：admin/ 源页与 portal/admin/ 产物同步（路径改写后），防漏同步。

运行：python3 -m pytest tests/test_admin_gate.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN_SRC = REPO / "admin"
ADMIN_OUT = REPO / "portal" / "admin"

# 构建产物缺失时先补齐（保证套件可独立运行；同 test_build_portal 口径）
if not (ADMIN_OUT / "appkeys.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

HAS_NODE = shutil.which("node") is not None

ADMIN_PAGES = {
    "load": ("load.html", "load.js", "数据装载"),      # 卡2：流程① 装载页（管理组首位）
    "appkeys": ("appkeys.html", "appkeys.js", "AppKey 发放"),
    "publish": ("publish.html", "publish.js", "服务发布"),   # 卡3：流程② 新标签
    "upstreams": ("upstreams.html", "upstreams.js", "上游代理"),
}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── 1. 导航可达（侧栏入口无死链＋面包屑回链） ──

def test_sidebar_admin_entries_resolve():
    """门户侧栏「管理」组三链＋「我的接入」在位，目标文件存在（无死链）。"""
    html = _read(REPO / "portal" / "index.html")
    sidebar = html.split('id="app-sidebar"')[1].split("</aside>")[0]
    for href in ("admin/publish.html", "admin/appkeys.html", "admin/upstreams.html", "my.html"):
        assert f'href="{href}"' in sidebar, f"侧栏缺入口 {href}"
        target = REPO / "portal" / href
        assert target.is_file(), f"侧栏死链：{href} 目标不存在"


# 【已删用例·碑注】test_all_built_portal_pages_nav_targets_resolve：全门户死链扫描命中的唯一死链＝旧门户页（docs.html 等）指向已下线 demo 服务详情壳；断言对象（demo 详情页）已随 2026-09-29 全服务下线移除；旧页面内容由新向导替换并自带新门禁。

def test_admin_pages_crumb_backlink():
    """运营三页面包屑（admin-crumb）＝系统路径两段（管理 › 本页名；2026-09-29 起无首页回链）。"""
    for name, (html_name, _, crumb_leaf) in ADMIN_PAGES.items():
        html = _read(ADMIN_OUT / html_name)
        assert 'id="admin-crumb"' in html, f"{name} 缺面包屑区块 admin-crumb"
        crumb = html.split('id="admin-crumb"', 1)[1].split("</nav>", 1)[0]
        assert "<span>管理</span>" in crumb, f"{name} 面包屑首级须为「管理」"
        assert 'aria-current="page"' in html and crumb_leaf in html, \
            f"{name} 面包屑缺当前页标识/页名"


# ── 2. 闸恒放行（2026-10-14 第三次反转：shim 语义断言＋机制骨架保留） ──

GATE_IDS = ("admin-token-input", "admin-token-save", "gate-banner",
            "gate-banner-text", "token-error", "dev-token-hint")


def _strip_js_comments(src: str) -> str:
    """去掉 JS 的块注释与行注释，只留可执行代码。

    缘由（2026-09-23）：令牌机制拆除后，源码注释里仍会**如实地**提到
    「X-Admin-Token 机制已于本日彻底拆除」这类说明文字。若直接把整份文件
    当字符串断言 not in，就会**误伤注释**——断言的是「代码里不再发这个头」，
    而不是「禁止提到这个词」。故先剥注释再判。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # 块注释
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)      # 整行行注释
    return src

def test_token_gate_ui_removed_from_pages():
    """三页源与产物均不再含令牌栏/只读 banner/令牌错误行/开发占位提示 id。"""
    for name, (html_name, _, _) in ADMIN_PAGES.items():
        for where, p in (("源", ADMIN_SRC / html_name), ("产物", ADMIN_OUT / html_name)):
            html = _read(p)
            for el_id in GATE_IDS:
                assert f'id="{el_id}"' not in html, \
                    f"{name}（{where}）仍含已拆除的令牌 UI id：{el_id}"
            # 不能残留 X-Admin-Token / 输入口令型令牌框
            assert 'type="password"' not in html, f"{name}（{where}）仍含 password 型令牌输入框"


def test_gate_script_loads_before_page_js():
    """admin-gate.js 必须先于页面业务 js 加载（组件工厂引用 AdminGate；加载序硬规矩）。

    2026-09-23：admin-gate.js 保留为无闸 shim（gateLocked/canWrite 恒放行），
    加载序契约不变——其先加载才能保证页面 js 拿到 AdminGate.gateMixin()。
    """
    for name, (html_name, js_name, _) in ADMIN_PAGES.items():
        html = _read(ADMIN_OUT / html_name)
        # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（只放宽匹配不改序契约）
        pos_gate = re.search(r'src="admin-gate\.js(\?v=[0-9a-f]+)?"', html)
        pos_js = re.search(rf'src="{re.escape(js_name)}(\?v=[0-9a-f]+)?"', html)
        assert pos_gate, f"{name} 未加载 admin-gate.js"
        assert pos_js and pos_js.start() > pos_gate.start(), \
            f"{name} {js_name} 须在 admin-gate.js 之后加载"


_SHIM_HARNESS = """
const fs = require('fs');
globalThis.window = globalThis;
const store = {};
globalThis.sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
let fetches = 0;
globalThis.fetch = async () => { fetches += 1; return { ok: false, status: 401 }; };
(async () => {
  (0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
  const AG = globalThis.AdminGate;
  const m = AG.gateMixin();
  console.log(JSON.stringify({
    canWrite: m.canWrite(), gateLocked: m.gateLocked(), banner: m.gateBannerText(),
    bootGate: m.bootGate(), loadDevHint: await m.loadDevHint(),
    verifyEmpty: await AG.verifyToken(''),
    verify401: await AG.verifyToken('wrong', () => Promise.resolve({ ok: false, status: 401 })),
    headers: JSON.stringify(AG.adminHeaders()),
    exports: ['gateMixin', 'adminHeaders', 'verifyToken', 'readToken', 'storeToken',
              'TOKEN_KEY', 'VERIFY_URL'].every((k) => k in AG),
    fetches,
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_admin_gate_is_no_lock_shim():
    """2026-10-14 闸第三次反转拆除（维护者 拍板）：admin-gate.js 为恒放行 shim。

    语义（与 selftest-admin 组 H 断言口径一致）：canWrite 恒 true、gateLocked 恒
    false、gateBannerText 恒空串、verifyToken 恒 ok（不发请求、不看 fetcher）。
    导出 API 面（gateMixin/adminHeaders/verifyToken/readToken/storeToken/TOKEN_KEY/
    VERIFY_URL）与令牌常量保留——页面 apiFetch 单一来源不破坏，登录页时代低成本
    复用/恢复（2026-10-09 真实闸形态见 commit bcf34f8；恢复路径见 ADR-0010）。
    2026-09-23 拆→2026-10-09 恢复→2026-10-14 再拆，两向断言历史均见 git 史。"""
    js = _strip_js_comments(_read(ADMIN_SRC / "admin-gate.js"))
    assert "canWrite: function () { return true; }" in js, "shim 须恒放行 canWrite"
    assert "gateLocked: function () { return false; }" in js, "shim 须恒放行 gateLocked"
    assert "TOKEN_KEY" in js and "VERIFY_URL" in js, "令牌常量保留（恢复闸期复用）"
    assert HAS_NODE, "需要 node 运行恒放行 shim 直调断言"
    r = subprocess.run(["node", "-e", _SHIM_HARNESS, str(ADMIN_SRC / "admin-gate.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    out = json.loads(r.stdout.strip())
    assert out["canWrite"] is True and out["gateLocked"] is False, "gateMixin 须恒放行"
    assert out["banner"] == "", "banner 文案须恒空（无「只读」提示）"
    assert out["verifyEmpty"] == "ok" and out["verify401"] == "ok", \
        "verifyToken 须恒 ok（后端不再校验，任意令牌都通过）"
    assert out["fetches"] == 0, "shim 加载期不得发任何校验请求"
    assert out["headers"] == "{}", "无令牌时 adminHeaders 须为空对象（后端不校验）"
    assert out["exports"] is True, "导出 API 面须完整保留（页面/单测直调依赖）"


# ── 2.5 解锁条接线（2026-10-14 两段史：上午接线修复→下午闸拆除后短路） ──────────

_WIRE_HARNESS = """
const fs = require('fs');
const ready = process.argv[2], seed = process.argv[3];  // ready: complete|loading；seed: none|seed
const calls = { appended: [], listeners: [], fetches: 0 };
let sidebarAuth = { id: 'os-sidebar-auth', innerHTML: '', getAttribute: () => null, setAttribute: function(k,v){this['_'+k]=v;}, querySelector: () => null };
globalThis.window = globalThis;
globalThis.document = {
  readyState: ready,
  getElementById: (id) => (id === 'os-sidebar-auth') ? sidebarAuth : null,
  createElement: () => ({ id: '', textContent: '', innerHTML: '', setAttribute() {}, addEventListener() {} }),
  addEventListener: (ev, fn) => { calls.listeners.push(ev); globalThis.__domReady = fn; },
  body: { appendChild: (c) => { calls.appended.push(c && c.id); }, firstChild: null, insertBefore: (c) => { calls.appended.push(c && c.id); } },
};
const store = {};
globalThis.sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
if (seed === 'seed') { store['oneserve-admin-token'] = 'seed-token'; }
globalThis.fetch = async () => { calls.fetches += 1; return { ok: false, status: 401 }; };
(async () => {
  (0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
  if (globalThis.__domReady) { document.readyState = 'complete'; globalThis.__domReady(); }
  await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify({ appended: calls.appended, fetches: calls.fetches,
    deferred: calls.listeners.indexOf('DOMContentLoaded') >= 0,
    token: sessionStorage.getItem('oneserve-admin-token') || '',
    text: sidebarAuth.innerHTML || '', sidebarAuth: !!sidebarAuth.innerHTML }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def _run_wire_harness(ready: str, seed: str) -> dict:
    assert HAS_NODE, "需要 node 运行解锁条接线 headless 断言"
    r = subprocess.run(["node", "-e", _WIRE_HARNESS,
                        str(ADMIN_SRC / "admin-gate.js"), ready, seed],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return json.loads(r.stdout.strip())


def test_unlock_bar_suppressed_in_shim():
    """恒放行 shim：autoWireUnlockBar 短路——任何场景不注入解锁条、不发校验请求。

    历史留痕（两段都在 2026-10-14 当天，别删）：
      上午（31ed94c）：维护者 亲测被卡——ensureUnlockBar() 只在 gateMixin().bootGate()
      里被调，而 8 个管理页 JS（load/publish/published/appkeys/versions/monitor/
      perf/upstreams）零处调用 bootGate() → 解锁条全站永不渲染 → 无令牌入口 →
      API 恒 401；修复＝脚本尾部 autoWireUnlockBar 自动判态接线（无令牌/bad/
      unverified→注入，ok→不注入），并加静态＋node 直调四场景防回潮断言。
      下午（本批，ADR-0010）：维护者 亲测第二轮被卡（admin321 改值→跨标签页
      sessionStorage 失忆）后拍板拆闸，恒放行态不再需要解锁条——接线保留但短路。
    本断言按 shim 形态改写（原「必须注入」断言随机制第三次反转存档于 git 史）：
      静态：接线函数与尾部无条件自调用仍在（恢复闸＝删短路一行即回上午形态），
      但函数体首句即 return（短路），接线段不调 ensureUnlockBar/verifyToken；
      行为（node 直调，无浏览器）：无令牌/有残令牌/loading 三场景一律零注入、
      零请求、不清残令牌（残令牌无害：后端不校验）。"""
    js = _strip_js_comments(_read(ADMIN_SRC / "admin-gate.js"))
    # 接线骨架保留：函数在、尾部无条件自调用在（恢复闸低成本）
    assert "function autoWireUnlockBar" in js, "接线函数须保留（短路而非删除）"
    tail = js.split("root.AdminGate = {", 1)[1]
    assert re.search(r"autoWire\w*\(\)\s*;\s*\}\)\(", tail), "脚本尾部须保留无条件自调用"
    # 但恒放行：函数体首句即 return，接线段不得再调 ensureUnlockBar/verifyToken
    assert re.search(r"function autoWireUnlockBar\(\)\s*\{\s*return\s*;", js), \
        "autoWireUnlockBar 须短路（首句 return，不注入解锁条）"
    wiring = js[js.index("function autoWireUnlockBar"):js.rindex("autoWireUnlockBar();")]
    assert "ensureUnlockBar(" not in wiring and "verifyToken(" not in wiring, \
        "接线段不得调用注入/校验（恒放行）"
    # 行为（node 直调，无浏览器）：无令牌/有残令牌/loading 三场景——解锁条零注入、
    # 零校验请求、不清残令牌；登录状态条（2026-10-15）恰注入一条（新契约，另测详断）。
    none = _run_wire_harness("complete", "none")
    assert "admin-gate-unlockbar" not in none["appended"] and none["fetches"] == 0, \
        "任何态不得注入解锁条/发校验请求"
    assert none["sidebarAuth"] is True, "侧栏认证区恰注入（T2：顶部状态条已拆除，登出并入侧栏底）"
    seeded = _run_wire_harness("complete", "seed")
    assert "admin-gate-unlockbar" not in seeded["appended"] and seeded["fetches"] == 0 \
        and seeded["token"] == "seed-token", \
        "残令牌不得触发解锁条注入/校验/清除（后端不校验，残值无害）"
    loading = _run_wire_harness("loading", "none")
    assert "admin-gate-unlockbar" not in loading["appended"] \
        and loading["sidebarAuth"] is True and loading["deferred"] is True, \
        "loading 态解锁条零注入；侧栏认证区挂 DOMContentLoaded 后注入（T2 新契约）"


def test_page_js_no_token_gate_and_no_token_header():
    """页面 js 契约（2026-10-14 恒放行后仍守）：apiFetch 统一经
    AdminGate.adminHeaders() 合并请求头（单一来源，页面不自写字面量头；shim 下
    无令牌即空对象，有残令牌照发后端不校验）；闸 UI 态经 gateMixin 合并。
    """
    for js_name in ("appkeys.js", "publish.js", "upstreams.js", "versions.js", "perf.js",
                    "load.js", "monitor.js"):
        raw = _read(ADMIN_SRC / js_name)
        js = _strip_js_comments(raw)
        # 令牌头只从 adminHeaders() 来（单一来源），页面不得自写头字面量
        assert "'X-Admin-Token':" not in js and '"X-Admin-Token":' not in js, \
            f"{js_name} 自写令牌头字面量（须统一走 AdminGate.adminHeaders()）"
        if "apiFetch" in js:
            assert "adminHeaders()" in js, \
                f"{js_name} 有 apiFetch 但未合并 AdminGate.adminHeaders()（请求头单一来源）"


def test_backend_admin_token_gate_removed():
    """2026-10-14 晚第四段：登录页时代开启——_admin_gate 委派 auth_oidc.require_login。

    断言口径（Casdoor 集成，ADR-0010 硬前提兑现）：函数与 router 挂点仍在（新增
    端点自动带闸的防漏挂设计不回退）；写操作族（auth_oidc.WRITE_METHODS）经
    require_login（Bearer JWT 校验，AUTH_OIDC_MODE=on 时 401 auth_required），
    读端点 GET 放行（混合模式：范围控制只挂写操作族）；monitor_api 同一挂点
    （GET-only，免登录直通）。前三段史（2026-09-23 拆→2026-10-09 恢复→
    2026-10-14 再拆）见 ADR-0010 与 git 史。"""
    py = _read(REPO / "service" / "app" / "admin_api.py")
    assert "def _admin_gate" in py, "admin_api 须保留闸挂点骨架"
    assert "dependencies=[Depends(_admin_gate)]" in py, "router 挂点须保留（防漏挂设计）"
    assert "auth_oidc.WRITE_METHODS" in py and "auth_oidc.require_login" in py, \
        "写操作族须委派 auth_oidc.require_login（登录闸）"
    mon = _read(REPO / "service" / "app" / "monitor_api.py")
    assert "from app.admin_api import _admin_gate" in mon and "Depends(_admin_gate)" in mon, \
        "monitor 三端点须与 admin API 同一挂点（GET-only 免登录直通）"
    # errors.UNAUTHORIZED 仍应被引用（91 码错误中心契约保留），不得误删
    assert "errors.UNAUTHORIZED" in py, "errors.UNAUTHORIZED 契约断言仍须保留"


# ── 3. 构建一致性（admin/ 源 → portal/admin/ 产物） ──

def test_admin_source_synced_to_portal():
    """portal/admin/* 与 admin/ 源同步（防漏跑构建）。

    本卡（管理页接入门户壳）后，三张管理页不再由源页加路径改写逐字节产出，
    而是由 scripts/build_portal.py 从 admin/<page>.html 抽取正文/样式/脚本、
    套上门户 shell 后产出；差异由本套件第 2 节单独守卫。其余文件（js/mock/selftest）
    仍走逐字节同步契约。
    """
    assert ADMIN_OUT.is_dir(), "portal/admin/ 缺失（先跑 scripts/build_portal.py）"
    # 卡2：＋数据装载；三批（2026-10-08 修五）：＋published/versions（接门户壳，壳版产出）；
    # P3 压测管理页：＋perf（同批壳版产出）
    admin_pages = {"load.html", "publish.html", "appkeys.html", "grants.html", "upstreams.html",
                   "monitor.html", "published.html", "versions.html", "perf.html",
                   "accounts.html",   # 1b-8：＋授权管理接壳（管理组第五链）
                   "tenants.html",    # 管理功能群一期：＋租户管理接壳（管理组第五链）
                   "pricing.html",    # 管理功能群一期：＋价目管理接壳（管理组第七链，收口批）
                   "billing.html"}    # 管理功能群一期：＋月账单接壳（报表中心第五链，收口批）
    for src in sorted(ADMIN_SRC.rglob("*")):
        if not src.is_file() or src.name.startswith("debug-"):
            continue
        rel = src.relative_to(ADMIN_SRC)
        out = ADMIN_OUT / rel
        assert out.is_file(), f"产物缺文件：{rel}"
        if rel.suffix == ".html" and src.name in admin_pages:
            continue   # 壳版页面：内容契约由第 2 节断言
        text = src.read_text(encoding="utf-8")
        if rel.suffix == ".html":   # 同 build_portal.py 改写口径（非页面 HTML）
            text = text.replace("../portal/assets/", "../assets/") \
                       .replace("../portal/index.html", "../index.html")
        assert out.read_text(encoding="utf-8") == text, f"产物陈旧（重跑 build）：{rel}"


# ── 4. 登录状态条＋401 跳转闭环（2026-10-15 维护者 亲验修三件之二/之三；
#    同日品牌登录页批：目标统一改门户 /login.html——用户不见 Casdoor 门面） ──────────

_LOGIN_BAR_HARNESS = """
const fs = require('fs');
const mode = process.argv[2];   // anon | loggedin
const store = {};
const fetchCalls = [];
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.sessionStorage = globalThis.localStorage;
globalThis.fetch = async (url, init) => { fetchCalls.push({ url: String(url), init: init || {} });
  return { ok: false, status: 401 }; };
let sidebarAuth = { id: 'os-sidebar-auth', innerHTML: '', getAttribute: () => null, setAttribute: function(k,v){this['_'+k]=v;}, querySelector: (sel) => ({ addEventListener: () => {} }) };
globalThis.document = {
  readyState: 'complete',
  getElementById: (id) => (id === 'os-sidebar-auth') ? sidebarAuth : null,
  createElement: () => ({ id: '', innerHTML: '', setAttribute() {}, addEventListener() {} }),
  addEventListener: () => {},
  body: { appendChild: () => {}, firstChild: null, insertBefore: () => {} },
};
globalThis.location = { href: 'http://localhost:8000/admin/appkeys.html', reload: () => { globalThis.__reloaded = true; } };
(async () => {
  if (mode === 'loggedin') {
    const payload = Buffer.from(JSON.stringify({ name: 'tester', owner: 'oneserve' })).toString('base64');
    store['oneserve-oidc-token'] = 'h.' + payload + '.s';
  }
  (0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
  await new Promise((r) => setTimeout(r, 10));
  const barAtLoad = sidebarAuth.innerHTML ? { id: sidebarAuth.id, html: sidebarAuth.innerHTML } : null;
  if (mode === 'loggedin') { globalThis.AdminGate.oidcLogout(); }
  await new Promise((r) => setTimeout(r, 10));
  console.log(JSON.stringify({ bar: barAtLoad, fetchCount: fetchCalls.length,
    logoutFetches: fetchCalls.map((f) => f.url).filter((u) => u.indexOf('/api/logout') >= 0),
    tokenCleared: !(store['oneserve-oidc-token']), reloaded: !!globalThis.__reloaded }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_login_bar_render_states():
    """侧栏认证区两态渲染（T2 清账：顶部状态条拆除，登出并入侧栏底）。
    同日品牌登录页批改口径：链接改指门户 /login.html，用户不见 Casdoor 门面）。

    未登录：侧栏底「去登录」链接＝/login.html；加载期零请求。


    已登录：「用户名 · 登出」；登出＝清本地 token＋刷新。


    """
    assert HAS_NODE, "需要 node 运行登录状态条渲染断言"
    r = subprocess.run(["node", "-e", _LOGIN_BAR_HARNESS,
                        str(ADMIN_SRC / "admin-gate.js"), "anon"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    out = json.loads(r.stdout.strip())
    bar = out["bar"]
    assert bar and bar["id"] == "os-sidebar-auth", "侧栏认证区 id 须为 os-sidebar-auth（T2：顶部状态条已拆除）"
    assert "去登录" in bar["html"], "未登录态须有「去登录」链接"
    m = re.search(r'href="([^"]+)"', bar["html"])
    assert m and m.group(1) == "/login.html",         "去登录链接须为门户品牌登录页 /login.html（不再出 8000 见 Casdoor 门面）"
    assert out["fetchCount"] == 0, "状态条加载期不得发任何请求"

    r2 = subprocess.run(["node", "-e", _LOGIN_BAR_HARNESS,
                         str(ADMIN_SRC / "admin-gate.js"), "loggedin"],
                        capture_output=True, text=True)
    assert r2.returncode == 0, f"node 失败：{r2.stderr}"
    out2 = json.loads(r2.stdout.strip())
    bar2 = out2["bar"]
    assert "tester" in bar2["html"], "已登录态须显示用户名 tester"
    assert "登出" in bar2["html"], "已登录态缺〔登出〕"
    assert out2["tokenCleared"] and out2["reloaded"], "登出须清本地 token 并刷新"
    assert out2["logoutFetches"] == ["/api/logout"],         "登出必须打 /api/logout 杀 Casdoor 同源会话（ADR-0018 SLO；不调=退出后自动回登）"


_REDIRECT_HARNESS = """
const fs = require('fs');
const which = process.argv[2];   // auth_required | other
const store = {};
globalThis.window = globalThis;
globalThis.localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
globalThis.sessionStorage = globalThis.localStorage;
globalThis.fetch = async () => ({ ok: false, status: 401,
  clone() { return { json: async () => ({ error: which === 'auth_required' ? 'auth_required' : 'appkey_invalid' }) }; } });
globalThis.location = { href: 'http://localhost:8000/admin/appkeys.html' };
(async () => {
  (0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
  await fetch('/v1/admin/reload', { method: 'POST' });
  await new Promise((r) => setTimeout(r, 20));
  console.log(JSON.stringify({ href: globalThis.location.href,
    returnKey: store['oneserve-auth-return'] || '' }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_401_redirect_targets_login_page():
    """401 跳转闭环（2026-10-15 紧急修三件之三核查，维护者 回执证据 c-2；同日品牌
    登录页批改口径：跳转目标改指门户 /login.html，不再出 8000 跳 Casdoor）。

    写操作 401 且信封 error=auth_required → 跳 /login.html（门户自建品牌登录页，
    账号密码经后端 /v1/admin/auth/login 密码流换 token）＋记住回址；非
    auth_required 的 401（如下游 appkey 无效）原样透传不跳转。
    """
    assert HAS_NODE, "需要 node 运行 401 跳转断言"
    r = subprocess.run(["node", "-e", _REDIRECT_HARNESS,
                        str(ADMIN_SRC / "admin-gate.js"), "auth_required"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    out = json.loads(r.stdout.strip())
    assert out["href"] == "/login.html",         f"401 auth_required 须跳门户登录页 /login.html（不出 8000）：{out['href']}"
    assert out["returnKey"] == "http://localhost:8000/admin/appkeys.html", "回址须存 oneserve-auth-return"

    r2 = subprocess.run(["node", "-e", _REDIRECT_HARNESS,
                         str(ADMIN_SRC / "admin-gate.js"), "other"],
                        capture_output=True, text=True)
    assert r2.returncode == 0, f"node 失败：{r2.stderr}"
    out2 = json.loads(r2.stdout.strip())
    assert out2["href"] == "http://localhost:8000/admin/appkeys.html",         "非 auth_required 的 401 不得跳登录（原样透传）"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
