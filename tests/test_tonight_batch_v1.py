#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今夜批 v1 静态断言套件（tests/test_tonight_batch_v1.py，2026-10-14）。

收拢门槛稿 docs/plans/开工门槛-今夜批_v1.md 卡 N4 与 P2c 的机检 AC
（卡 N5 断言落 tests/test_portal_catalog.py；monitor 接壳全套壳断言
由 tests/test_admin_shell.py PAGES 八张口径守卫，本套件不重复）：

  N4-AC-1 侧栏「待完成」琥珀 chip 收敛：源/产物零 os-nav-todo，
           foot 区一行灰色纯计数「N 项待完善」；portal.css 保留策略注释在位。
  N4-AC-2 装载页步骤①卡片补连接状态时间戳行（load-source-probedat＋
           load.js probeTimeStr/probedAt），截图对比归视觉复评（不入机检）。
  P2c-1   monitor 面包屑统一为全站管理页模式「管理 › 异常监控」
           （两段纯文本、无首页回链、无自绘三段/环境 chip 残留）。
  P2c-2   docs.html「5 分钟上手」三步间补真实链接（链接真实性断言在
           tests/test_docs_help_center.py test_quickstart_*；此处守产物在位）。
  P2c-3   空状态文案 8 处对齐「一句话现状——一句指引」模式
           （样板＝版本页「还没有任何版本空间 / 去 数据装载…」）。

运行：python3 -m pytest tests/test_tonight_batch_v1.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
ADMIN_SRC = REPO / "admin"

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402

# 产物缺失时先补构建（同 test_admin_shell 口径，保证套件可独立运行）
if not (PORTAL / "admin" / "monitor.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── N4-AC-1：侧栏琥珀 chip 收敛 ──

def test_sidebar_amber_chips_gone_everywhere():
    """侧栏源与全部门户产物零「待完成」琥珀 chip（os-nav-todo 清零）。"""
    sidebar_src = _read(PORTAL / "partials" / "sidebar.html")
    assert '"os-nav-todo"' not in sidebar_src, "sidebar.html 仍残留琥珀 chip 标记"
    assert "待完成</span>" not in sidebar_src, "sidebar.html 仍残留「待完成」chip 元素"
    for page in sorted(PORTAL.glob("*.html")) + sorted((PORTAL / "admin").glob("*.html")):
        html = _read(page)
        assert '"os-nav-todo"' not in html, f"{page.name} 产物仍渲染琥珀 chip"


def test_sidebar_todo_count_removed():
    """P5（走查 2026-10-16 维护者 拍）：foot 区「N 项待完善」计数与「降级数据如实
    带角标标注」文案移除；#os-sidebar-auth（认证/登出区，DESIGN §1.5）不得误删。"""
    sidebar_src = _read(PORTAL / "partials" / "sidebar.html")
    assert "os-nav-todo-count" not in sidebar_src, "sidebar.html 仍有待办计数 span"
    assert "项待完善" not in sidebar_src, "sidebar.html 仍有「N 项待完善」计数文案"
    assert "降级数据如实带角标标注" not in sidebar_src, "sidebar.html 仍有降级标注文案"
    assert 'id="os-sidebar-auth"' in sidebar_src, "误删认证/登出区 #os-sidebar-auth"
    css = _read(PORTAL / "assets" / "portal.css")
    assert ".os-nav-todo-count" not in css, "portal.css 仍保留计数行样式（应随移除删除）"
    assert css.count(".os-nav-todo {") == 0, "portal.css 仍保留琥珀 pill 样式（应随收敛删除）"


# ── N4-AC-2：装载页步骤①连接状态时间戳行 ──

def test_load_step1_probe_timestamp_row():
    """步骤①卡片内时间戳行在位（源与产物），load.js 探测成功即记录时刻。"""
    src = _read(ADMIN_SRC / "load.html")
    assert 'id="load-source-probedat"' in src, "load.html 缺连接状态时间戳行"
    assert "probedAt" in src, "load.html 未绑定 probedAt 状态"
    built = _read(PORTAL / "admin" / "load.html")
    assert 'id="load-source-probedat"' in built, "装载页产物缺时间戳行"
    js = _read(ADMIN_SRC / "load.js")
    assert "probeTimeStr" in js and "probedAt" in js, "load.js 缺探测时刻记录"
    assert js.count("probedAt") >= 2, "load.js 应有 probedAt 状态位＋赋值两处以上"


# ── P2c-1：monitor 面包屑统一 ──

def test_monitor_crumb_unified_admin_pattern():
    """monitor 壳版面包屑＝「管理 › 异常监控」两段（无链接、末级 aria-current）。"""
    html = _read(PORTAL / "admin" / "monitor.html")
    m = re.search(r'<nav class="crumb" id="admin-crumb".*?</nav>', html, re.S)
    assert m, "monitor 壳版缺 admin-crumb（未接入门户壳？）"
    crumb = m.group(0)
    assert "<span>管理</span>" in crumb and "异常监控" in crumb, "面包屑须为「管理 › 异常监控」"
    assert "<a href" not in crumb, "面包屑不应含链接（全站管理页模式＝纯文本系统路径）"
    # 自绘三段旧导航与环境 chip 不得残留
    assert "运营控制台</span>" not in html, "monitor 残留自绘「运营控制台」段"
    assert "首页</a>" not in html, "monitor 残留首页回链（管理页模式无首页级）"


# ── P2c-2：docs 三步「下一步」链接在位（真实性断言在 test_docs_help_center） ──

def test_docs_quickstart_next_links_present():
    """docs.html 产物含三步间 next 链接三枚（step1→trial、step2→audit、step3→appkeys）。"""
    html = _read(PORTAL / "docs.html")
    for anchor, href in (("docs-link-step1-next", "trial.html#trial-tab-http"),
                         ("docs-link-step2-next", "audit.html"),
                         ("docs-link-step3-next", "admin/appkeys.html")):
        assert f'id="{anchor}"' in html, f"docs.html 缺步骤间链接 {anchor}"
        target = PORTAL / href.partition("#")[0]
        assert target.is_file(), f"{anchor} 目标死链：{href}"


# ── P2c-3：空状态文案「现状——指引」模式（8 处） ──

# (文件, 断言子串)：现状句保留原文，统一以「——」接一句可执行的指引（样板＝版本页空态）
EMPTY_STATES = (
    ("portal/src/catalog.html", "该业务域暂无在册服务——切回「全部」"),
    ("portal/src/my.html", "还没有查询到调用记录——先到"),
    ("portal/src/my.html", "暂无逐日用量数据——凭证开始调用产生计量后"),
    ("portal/src/audit.html", "窗口内暂无带耗时的记录——放宽时间窗"),
    ("portal/src/errors.html", "的错误码——换个关键词"),
    ("admin/monitor.html", "当前条件下暂无异常调用方——点「重置」"),
    ("admin/monitor.html", "当前条件下暂无异常 API——点「重置」"),
    ("admin/appkeys.html", "当前视图暂无凭证——调整筛选条件"),
)


def test_empty_states_status_plus_guidance_pattern():
    """8 处空态均含「现状——指引」文案（源文件级；产物随构建同步）。"""
    for rel, needle in EMPTY_STATES:
        src = _read(REPO / rel)
        assert needle in src, f"{rel} 空态缺「现状——指引」模式：{needle}"


def test_empty_states_sources_synced_to_built_pages():
    """空态改动随构建进产物（portal 产物/admin 壳版各抽一处核对）。"""
    assert "切回「全部」" in _read(PORTAL / "catalog.html"), "catalog 产物未同步空态文案"
    assert "点「重置」清除条件" in _read(PORTAL / "admin" / "monitor.html"), \
        "monitor 壳版产物未同步空态文案"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
