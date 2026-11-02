#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""术语统一静态断言（W-X1 AppKey 拼法 ＋ W-X2 试用/沙箱 归一；2026-10-14 门面批）。

守卫口径（验收）：
  1. W-X1：用户可见文案层 AppKey 异体拼法清零——扫描 portal/*.html、portal/admin/*.html、
     admin/*.html 的可见文本（排除 HTML 注释与 script/style 代码体、code/pre 代码块；
     提示层属性 title/placeholder/aria-label/aria-description/alt 纳入——tooltip/占位符
     即用户可见文案）。正字＝AppKey；异体（appkey/APPKEY/ApiKey/apiKey/API key/
     API-key…）一律 0。豁免＝代码/API 真源字面：ONESERVE_APPKEY 环境变量、
     appkeys.py 等文件名（含路径）、/v1/**/appkeys API 路径、小写 apikey
     （HTTP 请求头名，术语表口径——「随请求头 apikey 携带」；术语表现居
     docs/opensource/ARCHITECTURE.md §10，docs 页已随 ux-polish 2026-11-02 下线）。
  2. W-X2：凭证语境「试用」清零（调试台白名单）——可见文本中「试用」仅允许以
     「试用台」（接口调试台旧名指称）形式存在；「试用 URL」「试用凭证」等一律 0；
     「体验凭证」同族异体一并清零。任务规定括注「测试用，正式凭证另行审批」
     自带「测试用」字样，先按原句摘除再断言（防误伤规定文案）。
  3. 防回潮（正向锚）：关键入口首现括注在位——trial 占位符／
     my 凭证提示／admin 发放页口径句均含「沙箱凭证」＋「测试用，正式凭证另行审批」；
     portal/admin/appkeys.html 镜像同步（构建产物）。
     （docs 快速起步锚随 docs 页 ux-polish 2026-11-02 下线移出清单。）
  注：JS 内文案（凭据文件「调试台 URL：」等）不在本静态扫描面（验收口径为 html）；
  由 admin/appkeys.js 真源＋test_appkeys_admin 镜像契约（js cmp）守。
运行：python3 -m pytest tests/test_terminology.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCAN_GLOBS = ("portal/*.html", "portal/admin/*.html", "admin/*.html")

# 用户可见文案属性（提示层：tooltip/占位符/无障碍名）
VISIBLE_ATTRS = ("title", "placeholder", "aria-label", "aria-description", "alt")

# 任务规定括注（自带「测试用」字样，先摘除再断言）
MANDATED_CLAUSE = "测试用，正式凭证另行审批"

# W-X1 豁免：代码/API 真源字面（环境变量／文件名／API 路径）
EXEMPT_PATTERNS = (
    re.compile(r"ONESERVE_APPKEY"),
    re.compile(r"[A-Za-z0-9_./-]*appkeys?\.(?:py|js|yaml|yml|html|css|json)"),
    re.compile(r"/v1/[A-Za-z0-9/_-]*appkeys?[A-Za-z0-9/_-]*"),
)

# W-X1 异体：AppKey 概念的非正字拼法（正字 AppKey；小写 apikey＝请求头名，不在列）
WX1_BAD = re.compile(r"appkey|api[ \-]key", re.IGNORECASE)

# W-X2：「试用」仅允许「试用台」（调试台白名单）；「体验凭证」同族异体清零
WX2_BAD = re.compile(r"试用(?!台)|体验凭证")


class _VisibleText(HTMLParser):
    """抽取可见文案：文本节点（去注释/script/style/code/pre）＋提示层属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[tuple[str, str]] = []   # (来源, 文本)
        self._skip = 0
        self._code = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("code", "pre"):
            self._code += 1
        elif not self._skip and not self._code:
            for k, v in attrs:
                if k in VISIBLE_ATTRS and v:
                    self.chunks.append(("@" + k, v))

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in ("code", "pre") and self._code:
            self._code -= 1

    def handle_data(self, data):
        if not self._skip and not self._code and data.strip():
            self.chunks.append(("text", data))


def _visible(rel: str) -> list[tuple[str, str]]:
    html = (REPO / rel).read_text(encoding="utf-8")
    p = _VisibleText()
    p.feed(html)
    return p.chunks


def _in_scope() -> list[str]:
    files: list[str] = []
    for g in SCAN_GLOBS:
        files.extend(str(p.relative_to(REPO)) for p in sorted(REPO.glob(g)))
    return files


def _wx1_stripped(text: str) -> str:
    """摘除任务括注与代码/API 真源字面后，再断言拼法。"""
    text = text.replace(MANDATED_CLAUSE, "")
    for pat in EXEMPT_PATTERNS:
        text = pat.sub("", text)
    return text


def _violations() -> tuple[list[str], list[str]]:
    wx1: list[str] = []
    wx2: list[str] = []
    for rel in _in_scope():
        for src, chunk in _visible(rel):
            body = _wx1_stripped(chunk)   # 摘规定括注（自带「测试用」）＋代码/API 真源字面再断言
            for m in WX1_BAD.finditer(body):
                if m.group(0) == "AppKey":              # 正字放行，异体（含大小写变体）记违例
                    continue
                wx1.append(f"{rel} [{src}] 异体拼法「{m.group(0)}」：…{body[max(0, m.start() - 20):m.end() + 20]}…")
            for m in WX2_BAD.finditer(body):
                wx2.append(f"{rel} [{src}] 违例「{m.group(0)}」：…{body[max(0, m.start() - 20):m.end() + 20]}…")
    return wx1, wx2


# ── W-X1：AppKey 异体拼法清零 ──

def test_wx1_appkey_spelling_variants_zero():
    bad, _ = _violations()
    assert not bad, f"AppKey 异体拼法残留 {len(bad)} 处（正字＝AppKey；豁免见模块 docstring）：\n" + "\n".join(bad)


# ── W-X2：凭证语境「试用」清零（调试台白名单＝试用台） ──

def test_wx2_trial_term_only_in_debug_console_context():
    _, bad = _violations()
    assert not bad, f"凭证语境「试用」/「体验凭证」残留 {len(bad)} 处（白名单＝试用台）：\n" + "\n".join(bad)


# ── 防回潮：关键入口「沙箱凭证」＋首现括注在位 ──

FIRST_MENTION_ANCHORS = (
    # (页面, 承载位置说明, 断言片段)
    # portal/docs.html 锚随 docs 页 ux-polish 2026-11-02 下线移出
    ("portal/trial.html", "AppKey 输入框占位符", "填你的沙箱凭证（测试用，正式凭证另行审批"),
    ("portal/my.html", "凭证提示 tooltip", "沙箱凭证，测试用，正式凭证另行审批"),
    ("admin/appkeys.html", "发放页口径句", "本页签发均为沙箱凭证（测试用，正式凭证另行审批）"),
)


def test_positive_anchors_sandbox_credential_first_mentions():
    missing = []
    for rel, where, frag in FIRST_MENTION_ANCHORS:
        joined = "".join(t for _, t in _visible(rel))   # 行内标签跨片段按渲染原文无缝拼接
        if frag not in joined:
            missing.append(f"{rel}（{where}）缺：{frag}")
    assert not missing, "沙箱凭证首现括注缺失：\n" + "\n".join(missing)


def test_positive_anchors_key_pages_carry_sandbox_term():
    for rel in ("portal/trial.html", "portal/my.html",
                "admin/appkeys.html", "portal/admin/appkeys.html"):
        joined = "".join(t for _, t in _visible(rel))
        assert "沙箱凭证" in joined, f"{rel} 缺「沙箱凭证」正字文案（术语统一回潮？）"
        assert MANDATED_CLAUSE in joined, f"{rel} 缺规定括注「{MANDATED_CLAUSE}」"


def test_mirror_portal_admin_appkeys_html_in_sync():
    """镜像壳版与源同批文案（构建产物随 build_portal 重建；锚点齐备）。"""
    mirror = "".join(t for _, t in _visible("portal/admin/appkeys.html"))
    for frag in ("调试台 URL", "沙箱凭证（测试用，正式凭证另行审批）", "AppKey 发放"):
        assert frag in mirror, f"portal/admin/appkeys.html 镜像缺「{frag}」（未重建？）"


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
