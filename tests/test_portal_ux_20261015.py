#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门面体验批套件（tests/test_portal_ux_20261015.py）——#42/44/46/47/58/59 六卡静态守卫。

口径（维护者 2026-10-15 批「做」；各卡改动均可追溯到决策清单 04/08/09/10/13/14/15/16）：
  #42-① /errors 渲染两源冲突定案：CDP 实测渲染＝完整错误码中心（截图侧误报），
         产物守卫＝errors 页特征在位＋审计页元素不在（防回归到所述误渲染形态）；
  #42-② 目录卡「查看详情」CTA 提级为明显按钮（去 aria-hidden＋按钮化样式）；
  #42-③ docs 术语表折叠置顶（details 默认收起＋深链自动展开脚本＋位置先于 5 分钟上手）；
  #44  apply 占位回填：申请授权联系平台运营组（不提 OA、流程与时效以平台运营组为准）；
  #46  错误码到动作闭环：详情页 401/429/配额类行内「去申请授权／查看配额」（零新接口）＋
       errors 页服务级/系统级码段规则块；
  #47  trial 试用告知条（计费/配额/数据来源）＋「试用 vs 生产」差异表（纯静态）；
  #58  my 配额水电表图例（蓝条＝已用占比/留白＝剩余）＋ grants 配额上限/今日已用两列
       （kid join /v1/admin/appkeys/usage 匿名只读，端点已有，纯前端）；
  #59  docs 首屏「快速接入四步」卡（申请授权→领凭证→在线调试→接入生产）。

运行：python3 -m pytest tests/test_portal_ux_20261015.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
SRC = PORTAL / "src"

# 产物缺失时先补齐（保证套件可独立运行）
if not (PORTAL / "errors.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

CATALOG_SRC = (SRC / "catalog.html").read_text(encoding="utf-8")
DOCS_SRC = (SRC / "docs.html").read_text(encoding="utf-8")
ERRORS_SRC = (SRC / "errors.html").read_text(encoding="utf-8")
ERRORS_BUILT = (PORTAL / "errors.html").read_text(encoding="utf-8")
DETAIL_SRC = (SRC / "detail.html").read_text(encoding="utf-8")
DETAIL_JS = (PORTAL / "assets" / "detail.js").read_text(encoding="utf-8")
TRIAL_SRC = (SRC / "trial.html").read_text(encoding="utf-8")
MY_SRC = (SRC / "my.html").read_text(encoding="utf-8")
MY_JS = (PORTAL / "assets" / "my.js").read_text(encoding="utf-8")
GRANTS_SRC = (SRC / "grants.html").read_text(encoding="utf-8")
GRANTS_JS = (PORTAL / "assets" / "grants.js").read_text(encoding="utf-8")
APPLY_SRC = (SRC / "apply.html").read_text(encoding="utf-8")
APPLY_BUILT = (PORTAL / "apply.html").read_text(encoding="utf-8")
PORTAL_CSS = (PORTAL / "assets" / "portal.css").read_text(encoding="utf-8")

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402


# ── #42-① /errors 渲染定案守卫（CDP 实测 2026-10-15：渲染＝完整错误码中心，截图侧误报） ──

def test_errors_page_is_error_center_not_audit():
    assert "<title>错误码中心 · OneServe 门户</title>" in ERRORS_BUILT, "errors 产物标题漂移"
    assert 'x-data="errorsCenterPage"' in ERRORS_BUILT, "errors 产物缺页组件挂载"
    assert 'id="errors-table-card"' in ERRORS_BUILT, "errors 产物缺码表区块"
    assert 'id="audit-toolbar"' not in ERRORS_BUILT, "errors 产物混入审计页元素（两源冲突所述形态）"
    assert "auditPage" not in ERRORS_BUILT, "errors 产物混入审计页组件"


# ── #42-② 目录卡 CTA 提级 ──

def test_catalog_cta_upgraded_to_prominent_button():
    # 弱化三件套退场：aria-hidden 与 muted 弱化样式不再存在
    assert '<span class="svc-go" aria-hidden="true">' not in CATALOG_SRC, "CTA 仍是 aria-hidden 弱化文字"
    m = re.search(r"\.svc \.svc-go \{[^}]*\}", PORTAL_CSS, re.S)
    assert m, "portal.css 缺 .svc-go 按钮化样式块"
    block = m.group(0)
    for token in ("border:", "padding:", "background:", "border-radius:"):
        assert token in block, ".svc-go 样式缺按钮要素：" + token
    assert "查看详情 →" in CATALOG_SRC, "CTA 文案漂移（test_build_portal 同款断言依赖此文案）"


# ── #42-③ docs 术语表折叠置顶 ──

def test_docs_glossary_folded_and_on_top():
    assert 'id="docs-glossary-fold"' in DOCS_SRC, "术语表缺折叠容器"
    m = re.search(r'<details class="docs-fold" id="docs-glossary-fold"([^>]*)>', DOCS_SRC)
    assert m, "折叠容器结构漂移"
    assert "open" not in m.group(1), "术语表默认未收起（应默认折叠）"
    assert DOCS_SRC.index('id="docs-glossary"') < DOCS_SRC.index('id="docs-quickstart"'), \
        "术语表未置顶（应先于 5 分钟上手）"
    assert "fold.open = true" in DOCS_SRC, "深链打开自动展开脚本缺失"
    for term in ("docs-term-gateway", "docs-term-appkey", "docs-term-key",
                 "docs-term-normalize", "docs-term-fieldauth", "docs-term-degrade",
                 "docs-term-demo"):
        assert f'id="{term}"' in DOCS_SRC, "术语表词条丢失：" + term
    # 原位置（span-16 独立卡）不再有第二份术语表
    assert DOCS_SRC.count('id="docs-glossary"') == 1, "术语表区块重复"
    fold_css = (PORTAL / "assets" / "docs.css").read_text(encoding="utf-8")
    assert ".docs-fold" in fold_css, "docs.css 缺折叠样式"


# ── #59 docs 快速接入四步卡 ──

def test_docs_quickpath_card_first_screen():
    assert 'id="docs-quickpath"' in DOCS_SRC, "缺快速接入四步卡"
    assert DOCS_SRC.index('id="docs-quickpath"') < DOCS_SRC.index('id="docs-glossary"'), \
        "四步卡不在首屏第一卡位"
    for step in ("docs-qp-step-apply", "docs-qp-step-key", "docs-qp-step-trial", "docs-qp-step-prod"):
        assert f'id="{step}"' in DOCS_SRC, "四步缺步骤：" + step
    for label in ("第 1 步 · 申请授权", "第 2 步 · 领取凭证（AppKey）",
                  "第 3 步 · 在线调试", "第 4 步 · 接入生产"):
        assert label in DOCS_SRC, "四步缺文案：" + label
    # 每步都有跳转链接
    for link_id in ("docs-link-qp-apply", "docs-link-qp-appkeys",
                    "docs-link-qp-trial", "docs-link-qp-catalog"):
        assert f'id="{link_id}"' in DOCS_SRC, "四步缺跳转链接：" + link_id
    assert "联系<b>平台运营组</b>申请授权" in DOCS_SRC, "第 1 步缺联系对象"
    assert "docs-quickpath" in bp.PAGES["docs"]["required_ids"], "docs-quickpath 未入构建注册表"


# ── #44 apply 占位回填 ──

def test_apply_contact_copy_filled():
    assert "申请授权请联系<b>平台运营组</b>" in APPLY_SRC, "缺联系对象（平台运营组）"
    assert "流程与时效以平台运营组为准" in APPLY_SRC, "缺口径说明"
    # 入口区不再回填 OA 指引与旧占位（ 维护者 口径：不提 OA、不编造流程细节）
    entry = APPLY_SRC.split('id="apply-oa-entry"')[1].split("</section>")[0]
    assert "OA" not in entry, "入口区仍提 OA"
    assert "待运营提供" not in APPLY_SRC, "旧占位未回填干净"
    assert "流程细节以" not in APPLY_SRC and "OA 系统真实地址" not in APPLY_SRC, "旧占位文案残留"
    # 口径同步到产物
    assert "申请授权请联系<b>平台运营组</b>" in APPLY_BUILT, "产物未同步新文案"


# ── #46 错误码到动作闭环 ──

def test_detail_error_inline_actions():
    assert "下一步" in DETAIL_SRC, "详情页错误表缺「下一步」列"
    assert 'errActionApply(e)' in DETAIL_SRC and 'errActionQuota(e)' in DETAIL_SRC, \
        "详情页错误行缺行动按钮绑定"
    # 静态 href 随构建改写（详情壳在 catalog/ 子目录须 ../ 前缀）
    assert '<a class="err-act" href="apply.html">去申请授权</a>' in DETAIL_SRC, "缺去申请授权按钮"
    assert '<a class="err-act" href="my.html">查看配额</a>' in DETAIL_SRC, "缺查看配额按钮"
    assert "errActionApply" in DETAIL_JS and "errActionQuota" in DETAIL_JS, "detail.js 缺分类函数"
    # 分类口径：401/403/授权凭证码→apply；429/quota_*/rate_limited→my
    assert "401" in DETAIL_JS and "403" in DETAIL_JS and "404" not in DETAIL_JS.split("errActionApply")[1].split("errActionQuota")[0], \
        "授权类分类口径漂移"
    assert "service_not_granted" in DETAIL_JS and "fields_not_granted" in DETAIL_JS, "缺授权类机读码"
    assert "'quota_'" in DETAIL_JS and "rate_limited" in DETAIL_JS, "缺配额类机读码"
    # 产物侧：详情壳改写生效（../apply.html / ../my.html 出现在错误行动作里）
    shells = sorted((PORTAL / "catalog").glob("*.html"))
    assert shells, "详情壳缺失（先跑 build_portal）"
    shell = shells[0].read_text(encoding="utf-8")
    assert 'href="../apply.html">去申请授权' in shell, "详情壳错误行动作未随子目录改写"
    assert 'href="../my.html">查看配额' in shell, "详情壳配额动作未随子目录改写"


def test_errors_page_code_ranges_block():
    assert 'id="errors-code-ranges"' in ERRORS_SRC, "errors 页缺码段规则块"
    for kw in ("系统级", "服务级", "auth_failed", "kv_read_failed", "job_state_inconsistent"):
        assert kw in ERRORS_SRC, "码段规则块缺关键词：" + kw
    assert 'href="apply.html"' in ERRORS_SRC and 'href="my.html"' in ERRORS_SRC, \
        "码段规则块缺「去申请授权／查看配额」跳转"
    assert "errors-code-ranges" in bp.PAGES["errors"]["required_ids"], "码段规则块未入注册表"
    assert 'id="errors-code-ranges"' in ERRORS_BUILT, "产物缺码段规则块"


# ── #47 trial 试用告知 ──

def test_trial_notice_and_vs_prod_table():
    assert 'id="trial-notice"' in TRIAL_SRC, "trial 缺告知条"
    for kw in ("计费", "配额", "数据"):
        assert kw in TRIAL_SRC.split('id="trial-notice"')[1].split("</details>")[0], \
            "告知条缺维度：" + kw
    assert "当前阶段不计费" in TRIAL_SRC, "计费口径文案缺失"
    assert "计入这把凭证的当日用量" in TRIAL_SRC, "配额占用口径缺失"
    assert "不造演示数" in TRIAL_SRC, "数据来源口径缺失"
    assert 'id="trial-vs-prod"' in TRIAL_SRC, "缺「试用 vs 生产」差异表"
    vs = TRIAL_SRC.split('id="trial-vs-prod"')[1].split("</details>")[0]
    for row in ("调用链路", "数据", "凭证", "计量与配额", "计费"):
        assert row in vs, "差异表缺维度行：" + row
    assert "trial-notice" in bp.PAGES["trial"]["required_ids"], "trial-notice 未入注册表"
    assert "trial-vs-prod" in bp.PAGES["trial"]["required_ids"], "trial-vs-prod 未入注册表"
    css = (PORTAL / "assets" / "trial.css").read_text(encoding="utf-8")
    assert ".trial-notice" in css and ".trial-vs-prod" in css, "trial.css 缺告知条样式"


# ── #58 my 水电表图例＋grants 配额两列 ──

def test_my_quota_bar_legend():
    assert 'class="my-quota-bar"' in MY_SRC, "my 配额进度条缺失（P1-3 既有件不得回退）"
    assert "my-quota-bar-legend" in MY_SRC, "缺进度条读法图例"
    assert "quotaRatioLabel" in MY_JS, "my.js 缺 ratio 图例函数"
    assert "quotaRemainingLabel()" in MY_SRC, "图例缺今日剩余"
    css = (PORTAL / "assets" / "my.css").read_text(encoding="utf-8")
    assert ".my-quota-bar-legend" in css, "my.css 缺图例样式"


def test_grants_quota_columns_join_existing_endpoint():
    for col in ("配额上限", "今日已用"):
        assert col in GRANTS_SRC, "grants 表缺列：" + col
    assert "quotaLimitLabel" in GRANTS_JS and "quotaUsedLabel" in GRANTS_JS, \
        "grants.js 缺配额列展示函数"
    assert "/v1/admin/appkeys/usage" in GRANTS_JS, "配额两列未接既有 usage 端点（端点已有，零新接口）"
    assert "quotaByKid" in GRANTS_JS and "kid" in GRANTS_JS, "缺 kid join"
    # 只读纪律不破：无写方法
    code = re.sub(r"/\*.*?\*/", "", GRANTS_JS, flags=re.S)
    assert not re.search(r"method\s*:\\s*['\"](POST|PUT|DELETE|PATCH)", code), \
        "grants.js 出现写请求"
    # 缺值如实：不在册凭证显示「—」，未登记与 0 区分
    assert "未登记" in GRANTS_JS, "未登记限额未与 0 区分"
    css = (PORTAL / "assets" / "grants.css").read_text(encoding="utf-8")
    assert ".gr-quota-warn" in css, "grants.css 缺超额警告样式"


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
