#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理台导航可达＋admin-gate 空壳守卫套件（tests/test_admin_gate.py）。

决策史（拆一次建一次都有留痕）：管理令牌闸三段史（2026-09-23 拆→2026-10-09 恢复→
2026-10-14 再拆，ADR-0010）→ OIDC 登录闭环（2026-10-14~15，ADR-0010 硬前提兑现）→
【无登录模式 ADR-0029，维护者 2026-10-28 拍板】开源版彻底拆除人机登录：admin-gate.js
整文件替换为最小空壳——保留文件名（各管理页 HTML 有 script 引用，避免逐页删标签），
不做任何拦截/跳转/令牌校验；AppKey 凭证体系仍在 /v1/admin/* 生效（机器鉴权不回退）。
历史形态见 git 史。

本套件守卫口径（无登录模式空壳的真正契约）：
  1. 导航可达：门户侧栏「管理」组链＋「我的接入」在位且目标文件存在（无死链）；
     管理页面包屑＝系统路径两段；
  2. 空壳无拦截：admin-gate.js 不含登录跳转/fetch 包装/OIDC 键；加载期零请求、
     零 DOM 注入；保留最小导出面（gateMixin/adminHeaders/verifyToken/readToken/
     storeToken/TOKEN_KEY/VERIFY_URL/escapeHtml——页面 js 消费契约不破坏）；
     管理源页与产物不引用 login.html/auth-callback.html；侧栏无账号管理项；
  3. 构建一致性：admin/ 源页与 portal/admin/ 产物同步（路径改写后），防漏同步。

运行：python3 -m pytest tests/test_admin_gate.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

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


def _strip_js_comments(src: str) -> str:
    """去掉 JS 的块注释与行注释，只留可执行代码（断言「代码里没有」而非「禁止提及」）。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)   # 块注释
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)      # 整行行注释
    return src


# ── 1. 导航可达（侧栏入口无死链＋面包屑回链） ──

def test_sidebar_admin_entries_resolve():
    """门户侧栏「管理」组三链＋「我的接入」在位，目标文件存在（无死链）。"""
    html = _read(REPO / "portal" / "index.html")
    sidebar = html.split('id="app-sidebar"')[1].split("</aside>")[0]
    for href in ("admin/publish.html", "admin/appkeys.html", "admin/upstreams.html", "my.html"):
        assert f'href="{href}"' in sidebar, f"侧栏缺入口 {href}"
        target = REPO / "portal" / href
        assert target.is_file(), f"侧栏死链：{href} 目标不存在"
    # 无登录模式（ADR-0029）：账号管理项与「仅 admin 可见」解锁属性不得回潮
    assert "accounts.html" not in sidebar, "侧栏不得残留账号管理入口（ADR-0029 已拆）"
    assert "data-os-admin-only" not in sidebar, "侧栏不得残留按角色解锁属性（无登录模式）"


def test_admin_pages_crumb_backlink():
    """运营页面包屑（admin-crumb）＝系统路径两段（管理 › 本页名）。"""
    for name, (html_name, _, crumb_leaf) in ADMIN_PAGES.items():
        html = _read(ADMIN_OUT / html_name)
        assert 'id="admin-crumb"' in html, f"{name} 缺面包屑区块 admin-crumb"
        crumb = html.split('id="admin-crumb"', 1)[1].split("</nav>", 1)[0]
        assert "<span>管理</span>" in crumb, f"{name} 面包屑首级须为「管理」"
        assert 'aria-current="page"' in html and crumb_leaf in html, \
            f"{name} 面包屑缺当前页标识/页名"


# ── 2. admin-gate.js＝无登录模式空壳（ADR-0029） ──

GATE_IDS = ("admin-token-input", "admin-token-save", "gate-banner",
            "gate-banner-text", "token-error", "dev-token-hint")


def test_token_gate_ui_removed_from_pages():
    """管理页源与产物均不再含令牌栏/只读 banner/令牌错误行/开发占位提示 id。"""
    for name, (html_name, _, _) in ADMIN_PAGES.items():
        for where, p in (("源", ADMIN_SRC / html_name), ("产物", ADMIN_OUT / html_name)):
            html = _read(p)
            for el_id in GATE_IDS:
                assert f'id="{el_id}"' not in html, \
                    f"{name}（{where}）仍含已拆除的令牌 UI id：{el_id}"
            assert 'type="password"' not in html, f"{name}（{where}）仍含 password 型令牌输入框"


def test_gate_script_loads_before_page_js():
    """admin-gate.js 必须先于页面业务 js 加载（组件工厂引用 AdminGate；加载序硬规矩）。"""
    for name, (html_name, js_name, _) in ADMIN_PAGES.items():
        html = _read(ADMIN_OUT / html_name)
        pos_gate = re.search(r'src="admin-gate\.js(\?v=[0-9a-f]+)?"', html)
        pos_js = re.search(rf'src="{re.escape(js_name)}(\?v=[0-9a-f]+)?"', html)
        assert pos_gate, f"{name} 未加载 admin-gate.js"
        assert pos_js and pos_js.start() > pos_gate.start(), \
            f"{name} {js_name} 须在 admin-gate.js 之后加载"


def test_admin_gate_is_no_login_shell():
    """ADR-0029：admin-gate.js 为无登录模式空壳——零拦截零跳转零令牌语义。

    静态口径（剥注释后判代码）：不含登录页常量/跳转函数/fetch 包装器/OIDC 键名；
    保留最小导出面（页面 js 消费契约：adminHeaders 恒空对象、verifyToken 恒 ok、
    gateMixin 恒放行、escapeHtml 基线、令牌常量）。历史实体实现见 git 史。"""
    raw = _read(ADMIN_SRC / "admin-gate.js")
    js = _strip_js_comments(raw)
    for absent in ("LOGIN_PAGE", "redirectToLogin", "root.fetch = function",
                   "oneserve-oidc-token", "oneserve-oidc-refresh",
                   "oneserve-auth-return", "/api/logout", "oidcLogout",
                   "os-sidebar-auth", "autoWireUnlockBar", "auth-callback"):
        assert absent not in js, f"admin-gate.js 空壳不得含登录机制代码：{absent}"
    assert "canWrite: function () { return true; }" in js, "空壳须恒放行 canWrite"
    assert "gateLocked: function () { return false; }" in js, "空壳须恒放行 gateLocked"
    assert "TOKEN_KEY" in js and "VERIFY_URL" in js, "令牌常量保留（导出面契约）"
    assert "escapeHtml: escapeHtml," in raw, "须导出 escapeHtml（selftest 渲染基线）"
    # 无登录模式声明注释在案（决策可追溯）
    assert "无登录模式" in raw and "ADR-0029" in raw, "空壳须带无登录模式碑注"


_SHELL_HARNESS = """
const fs = require('fs');
globalThis.window = globalThis;
const store = {};
globalThis.sessionStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
};
let fetches = 0;
let domTouched = 0;
globalThis.fetch = async () => { fetches += 1; return { ok: false, status: 401 }; };
globalThis.document = {
  readyState: 'complete',
  getElementById: () => null,
  createElement: () => { domTouched += 1; return { style: {}, setAttribute() {}, addEventListener() {} }; },
  addEventListener: () => {},
  querySelectorAll: () => [],
  body: { appendChild: () => { domTouched += 1; }, firstChild: null, insertBefore: () => { domTouched += 1; } },
};
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
              'TOKEN_KEY', 'VERIFY_URL', 'escapeHtml'].every((k) => k in AG),
    fetches, domTouched, locationUntouched: !globalThis.location,
  }));
})().catch((e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def test_admin_gate_shell_behavior():
    """空壳行为（node 直调，无浏览器）：恒放行、恒空对象、恒 ok、零请求、零 DOM 注入。"""
    assert HAS_NODE, "需要 node 运行空壳直调断言"
    r = subprocess.run(["node", "-e", _SHELL_HARNESS, str(ADMIN_SRC / "admin-gate.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    out = json.loads(r.stdout.strip())
    assert out["canWrite"] is True and out["gateLocked"] is False, "gateMixin 须恒放行"
    assert out["banner"] == "", "banner 文案须恒空（无「只读」提示）"
    assert out["verifyEmpty"] == "ok" and out["verify401"] == "ok", \
        "verifyToken 须恒 ok（无登录模式不校验人机令牌）"
    assert out["fetches"] == 0, "空壳加载期不得发任何请求"
    assert out["domTouched"] == 0, "空壳不得注入任何 DOM（解锁条/登录条已随登录拆除）"
    assert out["headers"] == "{}", "adminHeaders 须恒空对象（无凭证概念）"
    assert out["exports"] is True, "最小导出面须完整（页面/单测直调依赖）"


def test_no_login_page_references_in_admin_pages():
    """管理源页与产物零登录页引用：login.html/auth-callback.html 已随 ADR-0029 删除。"""
    for p in sorted(ADMIN_SRC.rglob("*.html")) + sorted(ADMIN_OUT.rglob("*.html")):
        html = _read(p)
        assert "login.html" not in html, f"{p.relative_to(REPO)} 残留 login.html 引用"
        assert "auth-callback" not in html, f"{p.relative_to(REPO)} 残留 auth-callback 引用"


def test_page_js_no_token_gate_and_no_token_header():
    """页面 js 契约：apiFetch 统一经 AdminGate.adminHeaders() 合并请求头（单一来源，
    页面不自写字面量头；空壳下恒空对象）；闸 UI 态经 gateMixin 合并。"""
    for js_name in ("appkeys.js", "publish.js", "upstreams.js", "versions.js", "perf.js",
                    "load.js", "monitor.js"):
        raw = _read(ADMIN_SRC / js_name)
        js = _strip_js_comments(raw)
        assert "'X-Admin-Token':" not in js and '"X-Admin-Token":' not in js, \
            f"{js_name} 自写令牌头字面量（须统一走 AdminGate.adminHeaders()）"
        if "apiFetch" in js:
            assert "adminHeaders()" in js, \
                f"{js_name} 有 apiFetch 但未合并 AdminGate.adminHeaders()（请求头单一来源）"


def test_backend_admin_gate_skeleton_kept():
    """后端闸挂点骨架保留（恒放行态；v0.1.1 清尸体后口径，ADR-0029 §7 兑现）：
    _admin_gate 挂点与 router 级依赖仍在（防漏挂设计不回退）；写方法 require_login/
    角色裁决挂点已随 auth_oidc.py 清理摘除（活引用归零，碑注文字除外）；
    errors.UNAUTHORIZED 契约保留。"""
    py = _read(REPO / "service" / "app" / "admin_api.py")
    assert "def _admin_gate" in py, "admin_api 须保留闸挂点骨架"
    assert "dependencies=[Depends(_admin_gate)]" in py, "router 挂点须保留（防漏挂设计）"
    assert "from app import auth_oidc" not in py and "await auth_oidc." not in py, \
        "auth_oidc 活引用残留（v0.1.1 已随登录清理删除，ADR-0029 §7；碑注文字除外）"
    mon = _read(REPO / "service" / "app" / "monitor_api.py")
    assert "from app.admin_api import _admin_gate" in mon and "Depends(_admin_gate)" in mon, \
        "monitor 三端点须与 admin API 同一挂点"
    assert "errors.UNAUTHORIZED" in py, "errors.UNAUTHORIZED 契约断言仍须保留"


# ── 3. 构建一致性（admin/ 源 → portal/admin/ 产物） ──

def test_admin_source_synced_to_portal():
    """portal/admin/* 与 admin/ 源同步（防漏跑构建）。"""
    assert ADMIN_OUT.is_dir(), "portal/admin/ 缺失（先跑 scripts/build_portal.py）"
    admin_pages = {"load.html", "publish.html", "appkeys.html", "grants.html", "upstreams.html",
                   "monitor.html", "published.html", "versions.html", "perf.html",
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


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
