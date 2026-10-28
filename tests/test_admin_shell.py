#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理页接入门户壳（布局统一）套件（tests/test_admin_shell.py）。

背景（维护者 2026-09-23）：管理页原先各自手写完整 <html><head>，只引 tokens.css
拿令牌，没有侧栏/顶栏/门户容器 → 点进去"像两个系统"。
本卡只做布局统一（不做令牌显示、不改业务逻辑）。

守卫口径：
  1. 构建注册：管理页纳入 build_portal.py 的 partials 拼接（NAV_KEYS/ADMIN_PAGES/侧栏占位）；
     三批（2026-10-08 修五）：versions/published 两页接壳（维护者 反馈「孤立」），
     页清单随 ADMIN_PAGES 扩为六张；
  2. 同一套壳：产物含与门户正页同源的侧栏/顶栏/容器/页脚（拼接自 portal/partials，
     非手抄——以 partials 独有文案为指纹；环境条已源头删除 2026-09-29）；
  3. active 态：侧栏「管理」组按页注入唯一 is-active/aria-current，href 指向本页；
  4. 面包屑：admin-crumb＝系统路径两段（管理 › 本页名＋末级 aria-current；2026-09-29 起无首页回链）；
  5. 脚本序硬规矩：admin-gate.js → 业务 js → alpine.min.js，逐页成立且各一次（O-2 实证）；
  6. 窄屏 P0-12：≤720px 断点覆盖门户顶栏（.os-topbar）＋宽表容器内滚动（防回归到 390px 溢出）；
  7. 幂等：重跑构建产物逐字节一致；零外链、零占位符残留。

注：JS 运行期结论（Alpine 挂载、390px 实测 scrollWidth<=clientWidth）由
tests/evidence/admin-shell/check_admin_shell.mjs（CDP 真浏览器）出证，不入本套件。
运行：python3 -m pytest tests/test_admin_shell.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
ADMIN_SRC = REPO / "admin"
ADMIN_OUT = PORTAL / "admin"

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402

# 产物缺失时先补齐（同 test_admin_gate 口径，保证套件可独立运行）
if not (ADMIN_OUT / "appkeys.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

# 管理页（卡2 起四张；三批 2026-10-08 修五接 versions/published 成六张；P3 压测管理页
# 接 perf 成七张：数据装载/服务发布/已发布服务/AppKey/版本管理/性能测试/上游；
# P2c 2026-10-14 接 monitor 成八张：面包屑统一为「管理 › 异常监控」）：
# 源文件名 → (产物名, 业务 js, 面包屑末级, 侧栏 active href)
PAGES = {
    "load": ("load.html", "load.js", "数据装载", "load.html"),
    "publish": ("publish.html", "publish.js", "服务发布", "publish.html"),   # 卡3：流程② 新标签
    "published": ("published.html", "published.js", "已发布服务", "published.html"),  # 三批修五：接壳
    "appkeys": ("appkeys.html", "appkeys.js", "AppKey 发放", "appkeys.html"),
    # 1b-8 授权管理（管理组第五链）：租户授权登记写面（租户列表→详情矩阵→编辑流）
    "grants": ("grants.html", "grants.js", "授权管理", "grants.html"),
    # 管理功能群一期·租户管理（管理组第五链；设计稿 D 节 4.2）：登记/冻结/解冻生命周期面
    "tenants": ("tenants.html", "tenants.js", "租户管理", "tenants.html"),
    # 管理功能群一期·价目管理（管理组第七链；设计稿 D 节 4.2）：三模型价目＋生效窗（收口批接线；
    # 单文件真源无独立业务 js，script 空串＝仅测试契约消费）
    "pricing": ("pricing.html", "", "价目管理", "pricing.html"),
    # 管理功能群一期·月账单（报表中心第五链；设计稿 D 节 4.2）：页面产物＝C 节 billing 页
    # （收口批接线；nav=reports_bills 高亮报表中心组；单文件真源）
    "billing": ("billing.html", "", "月账单", "billing.html"),
    "versions": ("versions.html", "versions.js", "版本管理", "versions.html"),        # 三批修五：接壳
    "perf": ("perf.html", "perf.js", "性能测试", "perf.html"),              # P3 压测管理页：接壳
    "upstreams": ("upstreams.html", "upstreams.js", "上游代理", "upstreams.html"),
    "monitor": ("monitor.html", "monitor.js", "异常监控", "monitor.html"),  # P2c 导航一致性：接壳
    # W5 账号管理台（2026-10-15 设计稿 B2）：accounts 接壳（导航项仅 admin 可见）
    "accounts": ("accounts.html", "accounts.js", "账号管理", "accounts.html"),
}

# 门户壳同一套标识（与 portal 正页逐一对齐；本卡"不再是两个系统"的机检口径）
SHELL_MARKERS = (
    # body 用**前缀**匹配：管理页源页会在 body 上挂 Alpine 组件
    # （admin/publish.html → x-data="publishAdmin()" x-init="boot()"），构建期合并进产物。
    # 原写成整标签精确匹配 '<body class="os-layout">'，源页一多带属性就误报「缺门户壳」；
    # 2026-09-23 实测反例：三个管理页的 x-data 曾被 build_portal 整个丢弃（页面全空白），
    # 而修好之后本断言反而变红。断言本意是「产物用了门户壳布局基类」，前缀即可表达。
    '<body class="os-layout"',
    'class="os-sidebar" id="app-sidebar"',
    'class="os-topbar" id="app-topbar"',
    'class="os-main"',
    'class="os-content" id="app-content"',
    'class="os-container"',
    'class="os-footer" id="page-footer"',
)

# portal/partials 独有指纹（只有拼接公共件才会出现，手抄副本不会带）
PARTIAL_FINGERPRINTS = (
    'id="os-sidebar-auth"',                      # sidebar.html foot 认证区（P5 移除计数文案后改此指纹）
    "OneServe 统一数据服务平台",                  # footer.html（口径句已全站删除，指纹改品牌句）
    # topbar.html 环境条指纹已删：os-envbar 源头删除（2026-09-29），topbar 无独有文案指纹
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _built(name: str) -> str:
    return _read(ADMIN_OUT / PAGES[name][0])


# ── 1. 构建注册（管理页走 partials 拼接，不再是各自手写 head） ──

def test_admin_pages_registered_in_build():
    """各管理页登记进 ADMIN_PAGES，且对应侧栏 active 占位键在 NAV_KEYS。"""
    assert set(bp.ADMIN_PAGES) == set(PAGES), "ADMIN_PAGES 与套件口径不一致"
    for name, meta in bp.ADMIN_PAGES.items():
        assert meta["nav"] in bp.NAV_KEYS, f"{name} 的 nav 键未入 NAV_KEYS：{meta['nav']}"
        assert meta["out"] == PAGES[name][0] and meta["script"] == PAGES[name][1]
        assert meta["crumb"][0] == ("管理", None) and len(meta["crumb"]) == 2, \
            f"{name} 面包屑须为系统路径两段（管理 › 本页名）"


def test_sidebar_has_admin_active_placeholders():
    """侧栏「管理」组三链带 @@A_/@@C_ 占位（构建期注入 active，非前端判断路径）。"""
    sidebar = _read(PORTAL / "partials" / "sidebar.html")
    for meta in bp.ADMIN_PAGES.values():
        assert f"@@A_{meta['nav']}@@" in sidebar and f"@@C_{meta['nav']}@@" in sidebar, \
            f"侧栏缺 active 占位：{meta['nav']}"
        assert f'href="admin/{meta["out"]}"' in sidebar, f"侧栏缺入口 admin/{meta['out']}"


def test_admin_pages_excluded_from_head_template_rewrite():
    """管理页由壳渲染，不是 admin 源页的路径改写副本（防退回旧同步法）。"""
    for name in PAGES:
        html = _built(name)
        assert "../portal/assets/" not in html, f"{name} 残留仓库根相对路径（未走壳渲染）"
        assert html.lstrip().startswith("<!DOCTYPE html>"), f"{name} 不是完整文档"


# ── 2. 同一套门户壳（拼接自 partials，非手抄） ──

def test_admin_pages_share_portal_shell_structure():
    """各管理页含与门户正页同一套壳标识（侧栏/顶栏/主列/容器/页脚）。

    环境标注已废（SPEC §四 由发布台特删升级为全站源头删除，2026-09-29）：
    任何页不得残留 os-envbar；发布台另不得出现「测试环境」文案。"""
    for name in PAGES:
        html = _built(name)
        missing = [m for m in SHELL_MARKERS if m not in html]
        assert not missing, f"{name} 缺门户壳标识：{missing}"
        assert 'class="os-envbar"' not in html, f"{name} 残留环境条（已源头删除）"
        if name == "publish":
            assert "测试环境" not in html, "发布台成品页不得残留环境标注（SPEC §四）"


def test_admin_shell_spliced_from_portal_partials():
    """壳由 portal/partials 拼接（以 partials 独有文案为指纹），非手抄副本。

    环境条指纹已随 os-envbar 源头删除整体移出指纹表（2026-09-29），无页级豁免。"""
    for name in PAGES:
        html = _built(name)
        for fp in PARTIAL_FINGERPRINTS:
            assert fp in html, f"{name} 未拼接门户公共件（缺指纹：{fp[:24]}…）"


def test_admin_pages_have_exactly_one_active_nav_and_it_is_self():
    """侧栏唯一 active 且指向本页（管理组高亮正确，"点进去还在同一系统"）。"""
    for name in PAGES:
        html = _built(name)
        actives = re.findall(r'<a class="os-nav-link is-active"[^>]*href="([^"]+)"', html)
        assert len(actives) == 1, f"{name} active 项数={len(actives)}（应唯一）"
        assert actives[0] == PAGES[name][3], f"{name} active 指向 {actives[0]}（应 {PAGES[name][3]}）"
        assert 'aria-current="page"' in html, f"{name} active 项缺 aria-current"
        # 其余管理链接不得被误标 active
        for other, (ohtml, _, _, ohref) in PAGES.items():
            if other != name:
                assert f'class="os-nav-link is-active" aria-current="page" href="{ohref}"' not in html, \
                    f"{name} 误将 {ohtml} 标为 active"


def test_admin_content_wrapped_in_portal_container():
    """正文包在门户内容容器里（宽度/内边距与门户一致，布局不再两个样）。"""
    for name in PAGES:
        html = _built(name)
        assert '<main class="os-content" id="app-content">' in html
        assert '<div class="os-container">' in html
        main = html.split('id="app-content">', 1)[1].split("</main>", 1)[0]
        assert 'class="os-container"' in main, f"{name} 容器不在 os-content 内"


# ── 3. 面包屑（P0-6 契约 id 不变） ──

def test_admin_crumb_system_path_two_segments():
    """admin-crumb＝系统路径两段（管理 › 本页名）：纯文本定位、无首页回链、末级 aria-current。"""
    for name, (_out, _js, leaf, _href) in PAGES.items():
        html = _built(name)
        m = re.search(r'<nav class="crumb" id="admin-crumb".*?</nav>', html, re.S)
        assert m, f"{name} 缺 admin-crumb 面包屑"
        crumb = m.group(0)
        assert "<span>管理</span>" in crumb, f"{name} 面包屑首级须为「管理」"
        assert "<a href" not in crumb, f"{name} 面包屑不应含链接（顶栏=系统路径，非导航）"
        assert leaf in crumb, f"{name} 面包屑缺本页名 {leaf}"
        assert 'aria-current="page"' in crumb, f"{name} 面包屑末级缺 aria-current"


# ── 4. 脚本加载序硬规矩（O-2 实证：乱序会 xxxAdmin is not defined） ──

def test_admin_script_order_gate_then_business_then_alpine():
    """admin-gate.js → 业务 js → alpine.min.js，逐页成立且各一次、均 defer。"""
    for name, (_out, js_name, _leaf, _href) in PAGES.items():
        html = _built(name)
        # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（只放宽匹配不改序契约）
        pos_gate = re.search(r'src="admin-gate\.js(\?v=[0-9a-f]+)?"', html)
        pos_alpine = re.search(r'src="[^"]*alpine\.min\.js(\?v=[0-9a-f]+)?"', html)
        # 单文件页（管理功能群一期 pricing/billing，收口批接线：业务脚本 Alpine 内联真源）
        # 无独立业务 js＝闸→alpine 两段序；有业务 js 页保持 闸→业务→alpine 三段序（原契约）。
        if js_name:
            pos_js = re.search(rf'src="{re.escape(js_name)}(\?v=[0-9a-f]+)?"', html)
            assert pos_gate and pos_js and pos_alpine \
                and pos_js.start() > pos_gate.start() and pos_alpine.start() > pos_js.start(), \
                f"{name} 脚本序错：gate={pos_gate} js={pos_js} alpine={pos_alpine}"
            defer_frags = (r'<script defer src="admin-gate\.js(\?v=[0-9a-f]+)?"',
                           rf'<script defer src="{re.escape(js_name)}(\?v=[0-9a-f]+)?"',
                           r'<script defer src="\.\./assets/alpine\.min\.js(\?v=[0-9a-f]+)?"')
        else:
            assert pos_gate and pos_alpine and pos_alpine.start() > pos_gate.start(), \
                f"{name} 脚本序错：gate={pos_gate} alpine={pos_alpine}"
            defer_frags = (r'<script defer src="admin-gate\.js(\?v=[0-9a-f]+)?"',
                           r'<script defer src="\.\./assets/alpine\.min\.js(\?v=[0-9a-f]+)?"')
        assert html.count("alpine.min.js") == 1, f"{name} alpine 加载次数≠1（head 重复未摘）"
        for frag in defer_frags:
            assert re.search(frag, html), f"{name} 缺 defer 脚本：{frag}"


# ── 5. 窄屏 P0-12（≤720px 无横向溢出）──

def test_admin_narrow_screen_rules_cover_portal_topbar_and_wide_tables():
    """源页 ≤720px 断点须覆盖门户顶栏 .os-topbar（可换行）＋宽表容器内滚动。

    历史坑（本卡浏览器实证）：接入壳后顶栏由 .topbar 变为 .os-topbar，
    原窄屏规则只写 .topbar → 390px 实测 scrollWidth 516 > 390；此断言防回归。
    """
    for name, (out, _js, _leaf, _href) in PAGES.items():
        src = _read(ADMIN_SRC / out)
        m = re.search(r"@media \(max-width: 720px\) \{", src)
        assert m, f"{name} 缺 ≤720px 断点"
        block = src[m.end():]
        assert ".os-topbar" in block, f"{name} 窄屏规则未覆盖门户顶栏 .os-topbar"
        assert "flex-wrap: wrap" in block, f"{name} 窄屏顶栏未允许换行"
        assert re.search(r"table \{[^}]*overflow-x: auto", block), f"{name} 窄屏宽表未容器内滚动"


# ── 6. 构建幂等 / 红线 ──

def test_admin_shell_build_idempotent():
    """重跑构建：各管理页产物逐字节一致（幂等纪律，同详情壳口径）。"""
    before = {n: (ADMIN_OUT / PAGES[n][0]).read_bytes() for n in PAGES}
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    for n in PAGES:
        assert (ADMIN_OUT / PAGES[n][0]).read_bytes() == before[n], f"构建非幂等：{PAGES[n][0]}"


def test_admin_pages_no_external_refs_and_no_placeholders():
    """零外网引用（内网白屏红线）＋零模板占位符残留。"""
    for name in PAGES:
        html = _built(name)
        externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
        assert not externals, f"{name} 出现外链：{externals[:3]}"
        leftover = re.findall(r"@@[A-Za-z0-9_]+@@", html)
        assert not leftover, f"{name} 残留模板占位符：{leftover[:3]}"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
