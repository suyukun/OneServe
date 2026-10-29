#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N1 页面动线补全静态断言（W-X4「拿凭证→调通→申请」引导链 · tests/test_portal_journeys.py）。

背景：W-X4 实证三类页互为孤岛——合作方用户任务链「看上哪个服务 → 申请凭证 → 调通」
之间缺引导出链。本卡（门槛稿 docs/plans/开工门槛-今夜批_v1.md 卡 N1）在三个页面补
纯静态引导（零业务逻辑），本套件固化三页新增引导元素与 href 真实性：

  AC-1 服务详情页（源 portal/src/detail.html → 壳 portal/catalog/v1-svc-*.html）
      出参表下方有「如何获得调用凭证」引导块（detail-cred-guide）→ docs#docs-channels
      （ux-fix-batch 2026-10-29：apply 页下线，出链随迁 docs）；
  AC-2 我的接入页（my.html）无凭证空态（凭证卡/配额卡 found=false）与凭证失效
      （expired）提示处有「联系运营组（docs.html#docs-channels）／去调试台（trial.html）」两链接；
  AC-3 接口调试台（trial.html）调用成功态响应区底部「下一步」提示（trial-next-steps）
      → docs.html 申请段锚点（#docs-step-key）／my.html；
  AC-4 href 真实性：新增链接目标页面文件存在、锚点在目标页在位；catalog/ 子目录壳
      的相对路径已随构建改写 ../ 前缀（无 catalog/ 内死链、无未改写裸链接）。

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
DOCS_BUILT = PORTAL / "docs.html"
TRIAL_BUILT = PORTAL / "trial.html"
MY_BUILT = PORTAL / "my.html"
# ux-fix-batch 2026-10-29：apply.html（申请接入）页下线——原 apply 出链改指 docs#docs-channels


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
    """源引导块链接＝联系平台运营组（docs#docs-channels）＋帮助快起步（docs.html），
    按构建改写口径写根相对路径（ux-fix-batch：apply 页下线，出链改指 docs）。"""
    src = _read(DETAIL_SRC)
    block = re.search(r'id="detail-cred-guide".*?</section>', src, re.S).group(0)
    assert 'href="docs.html#docs-channels"' in block, "引导块缺「联系平台运营组」链接（docs#docs-channels）"
    assert 'href="apply.html"' not in block, "引导块仍链已下线的 apply.html"
    assert 'href="docs.html"' in block, "引导块缺帮助快起步链接（docs.html）"
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
        assert 'href="../docs.html#docs-channels"' in block, shell.name + " 运营组链接未改写 ../ 前缀（catalog/ 内死链）"
        assert 'href="../docs.html"' in block, shell.name + " docs 链接未改写 ../ 前缀（catalog/ 内死链）"
        assert 'apply.html' not in block, shell.name + " 残留已下线 apply 链接（catalog/ 内死链）"


# ── AC-2 我的接入页无凭证/失效态指引 ──

# 空态「现状——指引」模式上的两条出链（联系运营组 docs#docs-channels／去调试台 trial.html；
# ux-fix-batch 2026-10-29：apply 页下线，出链改指 docs）
MY_GUIDANCE_NEEDLES = (
    # 凭证卡空态（查无此凭证）
    '还没有自己的凭证？<a href="docs.html#docs-channels">联系平台运营组</a>，或先到<a href="trial.html">接口调试台</a>体验',
    # 配额卡空态（quotaFound=false）
    '需要配额？<a href="docs.html#docs-channels">联系平台运营组</a>完成授权登记，或先到<a href="trial.html">接口调试台</a>体验',
)


def test_my_empty_states_have_contact_and_trial_links():
    """源 my.html：两处无凭证/未登记空态都带「联系运营组＋去调试台」两链接。"""
    src = _read(MY_SRC)
    for needle in MY_GUIDANCE_NEEDLES:
        assert needle in src, "my.html 空态缺指引出链：" + needle[:40]


def test_my_expired_hint_has_contact_and_trial_links():
    """源 my.html：凭证失效（expired）提示处带「联系运营组＋去调试台」两链接。"""
    src = _read(MY_SRC)
    line = next((ln for ln in src.splitlines() if "c.status === 'expired'" in ln), "")
    assert line, "my.html 未找到凭证失效（expired）提示行"
    assert 'href="docs.html#docs-channels"' in line, "失效提示缺联系运营组链接（docs#docs-channels）"
    assert 'href="apply.html"' not in line, "失效提示仍链已下线的 apply.html"
    assert 'href="trial.html"' in line, "失效提示缺去调试台链接（trial.html）"


def test_my_built_page_synced():
    """产物 portal/my.html 随构建同步两条指引（防只改源不重建）。"""
    html = _read(MY_BUILT)
    for needle in MY_GUIDANCE_NEEDLES:
        assert needle in html, "my.html 产物未同步空态指引链接（需重跑 build_portal）"


# ── AC-3 接口调试台成功态「下一步」提示 ──

NEXT_STEPS_TEXT = ('下一步：<a href="docs.html#docs-step-key">申请正式凭证</a>'
                   '（找平台管理员审批），或在<a href="my.html">我的接入</a>查看用量。')


def test_trial_success_state_has_next_steps_hint():
    """源 trial.html：成功态响应区底部有「下一步」提示条，且两链接在位。"""
    src = _read(TRIAL_SRC)
    assert 'id="trial-next-steps"' in src, "trial.html 缺成功态「下一步」提示条"
    assert NEXT_STEPS_TEXT in src, "trial.html 下一步提示文案/链接不符（docs 申请段锚点＋my）"


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
    """新增链接全部指向真实存在的产物页面（my/trial/docs 均在 portal/ 下；apply 已下线不计）。"""
    for path in (MY_BUILT, TRIAL_BUILT, DOCS_BUILT):
        assert path.is_file(), "链接目标页面缺失：" + str(path.relative_to(REPO))


def test_docs_apply_anchor_exists():
    """docs.html#docs-step-key（申请段＝「拿一把凭证」一步）锚点在产物 docs 页在位。"""
    docs = _read(DOCS_BUILT)
    assert 'id="docs-step-key"' in docs, "docs.html 缺 #docs-step-key 锚点（申请段深链断）"
    assert "拿一把凭证" in docs, "docs.html 申请段内容不在位（锚点指向空段）"


def test_catalog_shell_targets_resolve():
    """catalog 壳内 ../ 前缀链接按子目录相对路径解析后真实存在（catalog/x.html → portal/x.html）。

    href 里的 #fragment / ?query 不是路径成分——解析前剥掉（ux-fix-batch：引导块出链
    改指 docs.html#docs-channels 带锚形态，锚指向 docs 页内区块，路径主体仍须存在）。
    """
    for shell in sorted(PORTAL.glob("catalog/v1-svc-*.html")):
        block = re.search(r'id="detail-cred-guide".*?</section>', _read(shell), re.S).group(0)
        for href in re.findall(r'href="([^"]+)"', block):
            path = re.sub(r"[#?].*$", "", href)
            target = (shell.parent / path).resolve()
            assert target.is_file(), shell.name + " 引导块链接悬空：" + href


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
