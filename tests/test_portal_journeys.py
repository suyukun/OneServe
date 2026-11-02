#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N1 页面动线补全静态断言（W-X4「拿凭证→调通→申请」引导链 · tests/test_portal_journeys.py）。

背景：W-X4 实证三类页互为孤岛——合作方用户任务链「看上哪个服务 → 申请凭证 → 调通」
之间缺引导出链。本卡（门槛稿 docs/plans/开工门槛-今夜批_v1.md 卡 N1）在三个页面补
纯静态引导（零业务逻辑），本套件固化三页新增引导元素与 href 真实性：

  AC-1 服务详情页（源 portal/src/detail.html → 壳 portal/catalog/v1-svc-*.html）
      出参表下方有「如何获得调用凭证」引导块（detail-cred-guide）→ AppKey 发放页
      （admin/appkeys.html）＋接口调试台（trial.html）
      （ux-fix-batch 2026-10-29：apply 页下线；ux-polish 2026-11-02：docs 页下线，
      出链随迁发放页/调试台——申请授权找平台运营组，纯文字无站内页）；
  AC-2 我的接入页（my.html）无凭证空态（凭证卡/配额卡 found=false）与凭证失效
      （expired）提示处有「领沙箱凭证（admin/appkeys.html）／去调试台（trial.html）」出链；
  AC-3 接口调试台（trial.html）调用成功态响应区底部「下一步」提示（trial-next-steps）
      → 服务目录（catalog.html）／my.html；
  AC-4 href 真实性：新增链接目标页面文件存在、锚点在目标页在位；catalog/ 子目录壳
      的相对路径已随构建改写 ../ 前缀（无 catalog/ 内死链、无未改写裸链接）。
      ux-polish 2026-11-02 追加：docs 页下线守卫（源/产物/按页 css 不复活）。

口径：源级＋产物级双层断言（沿 P2c 空态套件同法）——只改 src 不重建会被产物断言逮住。
运行：python3 -m pytest tests/test_portal_journeys.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
SRC = PORTAL / "src"

DETAIL_SRC = SRC / "detail.html"
MY_SRC = SRC / "my.html"
TRIAL_SRC = SRC / "trial.html"
TRIAL_BUILT = PORTAL / "trial.html"
MY_BUILT = PORTAL / "my.html"
DOCS_BUILT = PORTAL / "docs.html"          # ux-polish 2026-11-02：已下线，仅作守卫断言对象
DOCS_SRC = SRC / "docs.html"
DOCS_CSS = PORTAL / "assets" / "docs.css"
# ux-fix-batch 2026-10-29：apply.html（申请接入）页下线；
# ux-polish 2026-11-02：docs.html（帮助与支持）页下线——出链随迁发放页/调试台/目录


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── AC-1 服务详情页「如何获得调用凭证」引导块 ──

def test_detail_source_has_cred_guide_below_response():
    """源 detail.html：引导块在位，且位于出参表（detail-response）之后、错误码表之前。"""
    src = _read(DETAIL_SRC)
    assert 'id="detail-cred-guide"' in src, "detail.html 缺「如何获得调用凭证」引导块"
    assert "如何获得调用凭证" in src, "引导块缺标题文案"
    i_resp = src.index('id="detail-response"')
    i_guide = src.index('id="detail-cred-guide"')
    i_err = src.index('id="detail-errors"')
    assert i_resp < i_guide < i_err, "引导块未落在出参表下方（detail-response 与 detail-errors 之间）"


def test_detail_source_guide_links():
    """源引导块链接＝AppKey 发放页（admin/appkeys.html）＋接口调试台（trial.html），
    按构建改写口径写根相对路径（ux-polish：docs 页下线，出链随迁发放页/调试台）。"""
    src = _read(DETAIL_SRC)
    block = re.search(r'id="detail-cred-guide".*?</section>', src, re.S).group(0)
    assert 'href="admin/appkeys.html"' in block, "引导块缺「AppKey 发放页」链接"
    assert 'href="trial.html"' in block, "引导块缺「接口调试台」链接"
    assert 'href="apply.html"' not in block, "引导块仍链已下线的 apply.html"
    assert 'docs.html' not in block, "引导块仍链已下线的 docs.html"
    # 文案口径（N3 术语）：AppKey 正字＋沙箱凭证
    assert "AppKey" in block and "沙箱凭证" in block, "引导块文案不符 N3 术语口径（AppKey/沙箱凭证）"


def test_catalog_shells_have_guide_with_rewritten_hrefs():
    """产物：每个 catalog 详情壳都有引导块，且 href 已随构建改写 ../ 前缀（无裸链接死链）。"""
    shells = sorted(PORTAL.glob("catalog/v1-svc-*.html"))
    assert shells, "portal/catalog/ 无详情壳（构建缺失？）"
    for shell in shells:
        html = _read(shell)
        assert 'id="detail-cred-guide"' in html, shell.name + " 缺引导块"
        assert "如何获得调用凭证" in html, shell.name + " 缺引导块标题"
        i_resp = html.index('id="detail-response"')
        i_guide = html.index('id="detail-cred-guide"')
        i_err = html.index('id="detail-errors"')
        assert i_resp < i_guide < i_err, shell.name + " 引导块位置漂移（应在出参表下方）"
        block = re.search(r'id="detail-cred-guide".*?</section>', html, re.S).group(0)
        assert 'href="../admin/appkeys.html"' in block, shell.name + " 发放页链接未改写 ../ 前缀（catalog/ 内死链）"
        assert 'href="../trial.html"' in block, shell.name + " 调试台链接未改写 ../ 前缀（catalog/ 内死链）"
        assert 'apply.html' not in block, shell.name + " 残留已下线 apply 链接（catalog/ 内死链）"
        assert 'docs.html' not in block, shell.name + " 残留已下线 docs 链接（catalog/ 内死链）"


# ── AC-2 我的接入页无凭证/失效态指引 ──

# 空态「现状——指引」模式上的出链（发放页/调试台；ux-fix-batch 2026-10-29：apply 下线；
# ux-polish 2026-11-02：docs 下线，「联系运营组」改纯文字、自助动作改指发放页）
MY_GUIDANCE_NEEDLES = (
    # 凭证卡空态（查无此凭证）
    '还没有自己的凭证？到<a href="admin/appkeys.html">AppKey 发放页</a>自助签发一把沙箱凭证，或先到<a href="trial.html">接口调试台</a>体验',
    # 配额卡空态（quotaFound=false）
    '需要配额？完成授权登记后随凭证下发（找平台管理员审批），或先到<a href="trial.html">接口调试台</a>体验',
)


def test_my_empty_states_have_contact_and_trial_links():
    """源 my.html：两处无凭证/未登记空态都带「领沙箱凭证/授权登记口径＋去调试台」指引。"""
    src = _read(MY_SRC)
    for needle in MY_GUIDANCE_NEEDLES:
        assert needle in src, "my.html 空态缺指引出链：" + needle[:40]


def test_my_expired_hint_has_contact_and_trial_links():
    """源 my.html：凭证失效（expired）提示带重签指引＋去调试台链接（docs 链接已随 docs 下线移除）。"""
    src = _read(MY_SRC)
    line = next((ln for ln in src.splitlines() if "c.status === 'expired'" in ln), "")
    assert line, "my.html 未找到凭证失效（expired）提示行"
    assert 'href="docs.html' not in line, "失效提示仍链已下线的 docs 页"
    assert 'href="apply.html"' not in line, "失效提示仍链已下线的 apply.html"
    assert "AppKey 发放页重新签发" in line, "失效提示缺重签指引"
    assert 'href="trial.html"' in line, "失效提示缺去调试台链接（trial.html）"


def test_my_built_page_synced():
    """产物 portal/my.html 随构建同步两条指引（防只改源不重建）。"""
    html = _read(MY_BUILT)
    for needle in MY_GUIDANCE_NEEDLES:
        assert needle in html, "my.html 产物未同步空态指引链接（需重跑 build_portal）"


# ── AC-3 接口调试台成功态「下一步」提示 ──

NEXT_STEPS_TEXT = ('下一步：到<a href="catalog.html">服务目录</a>选定服务接入（正式凭证找平台管理员审批），'
                   '或在<a href="my.html">我的接入</a>查看用量。')


def test_trial_success_state_has_next_steps_hint():
    """源 trial.html：成功态响应区底部有「下一步」提示条，且两链接在位。"""
    src = _read(TRIAL_SRC)
    assert 'id="trial-next-steps"' in src, "trial.html 缺成功态「下一步」提示条"
    assert NEXT_STEPS_TEXT in src, "trial.html 下一步提示文案/链接不符（服务目录＋my）"


def test_trial_next_steps_inside_success_block():
    """提示条确在调用成功（state === 'ok'）响应区内：原始 JSON 之后、调用示例区之前。"""
    src = _read(TRIAL_SRC)
    i_ok = src.index("x-if=\"state === 'ok'\"")
    i_raw = src.index('id="trial-raw-json"')
    i_next = src.index('id="trial-next-steps"')
    i_example = src.index('id="trial-tab-http"')
    assert i_ok < i_raw < i_next < i_example, "下一步提示条未落在成功态响应区底部（raw-json 之后、示例区之前）"


def test_trial_built_page_synced():
    """产物 portal/trial.html 随构建同步提示条。"""
    html = _read(TRIAL_BUILT)
    assert 'id="trial-next-steps"' in html, "trial.html 产物未同步下一步提示（需重跑 build_portal）"
    assert NEXT_STEPS_TEXT in html, "trial.html 产物提示文案/链接不符"


# ── AC-4 href 真实性：目标页存在＋锚点在位 ──

def test_new_link_targets_exist():
    """新增链接全部指向真实存在的产物页面（my/trial 均在 portal/ 下；apply/docs 已下线不计）。"""
    for path in (MY_BUILT, TRIAL_BUILT):
        assert path.is_file(), "链接目标页面缺失：" + str(path.relative_to(REPO))


def test_docs_page_retired():
    """【下线守卫·ux-polish 2026-11-02】docs（帮助与支持）页三件不复活：

    源/产物/按页 css 全部退役（沿 apply 下线守卫同款口径）——任何一次「顺手重建」
    （如旧分支误合并、构建脚本回退）都会在此逮住。
    """
    for path in (DOCS_BUILT, DOCS_SRC, DOCS_CSS):
        assert not path.exists(), "已下线的 docs 页件复活：" + str(path.relative_to(REPO))


def test_catalog_shell_targets_resolve():
    """catalog 壳内 ../ 前缀链接按子目录相对路径解析后真实存在（catalog/x.html → portal/x.html）。

    href 里的 #fragment / ?query 不是路径成分——解析前剥掉（历史口径：引导块出链曾
    改指 docs.html#docs-channels 带锚形态；ux-polish 2026-11-02 起指向发放页/调试台，
    路径主体仍须存在）。
    """
    for shell in sorted(PORTAL.glob("catalog/v1-svc-*.html")):
        block = re.search(r'id="detail-cred-guide".*?</section>', _read(shell), re.S).group(0)
        for href in re.findall(r'href="([^"]+)"', block):
            path = re.sub(r"[#?].*$", "", href)
            target = (shell.parent / path).resolve()
            assert target.is_file(), shell.name + " 引导块链接悬空：" + href


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
