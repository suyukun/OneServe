#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门面体验批套件（tests/test_portal_ux_20261015.py）——#42/44/46/47/58/59 六卡静态守卫。

口径（维护者 2026-10-15 批「做」；各卡改动均可追溯到决策清单 04/08/09/10/13/14/15/16）：
  #42-① /errors 渲染两源冲突定案：CDP 实测渲染＝完整错误码中心（截图侧误报），
         产物守卫＝errors 页特征在位＋审计页元素不在（防回归到所述误渲染形态）；
  #42-② 目录卡「查看详情」CTA 提级为明显按钮（去 aria-hidden＋按钮化样式）；
  #42-③ docs 术语表折叠置顶（历史卡；docs 页随 ux-polish 2026-11-02 整页下线，
        术语表迁 docs/opensource/ARCHITECTURE.md §10，守卫随迁断言迁移落点）；
  #44  apply 占位回填（已被 ux-fix-batch 2026-10-29 取代：apply 页整页下线；
        下线守卫现居 tests/test_portal_urls.py 配置级用例）；
  #46  错误码到动作闭环：详情页 401/429/配额类行内「领/重签凭证／查授权全景／查看配额」
       （零新接口；ux-polish 2026-11-02：原「联系运营组开通」docs 锚随 docs 下线拆分）＋
       errors 页服务级/系统级码段规则块；
  #47  trial 试用告知条（计费/配额/数据来源）＋「试用 vs 生产」差异表（纯静态）；
  #58  my 配额水电表图例（蓝条＝已用占比/留白＝剩余）＋ grants 配额上限/今日已用两列
       （kid join /v1/admin/appkeys/usage 匿名只读，端点已有，纯前端）；
  #59  docs 首屏「快速接入四步」卡（历史卡；随 docs 页下线退役，路径由 README Quick start 承接）。

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
# ux-polish 2026-11-02：docs 页三件已 git rm，DOCS_SRC 常量随之退役（勿回填）
ARCHITECTURE_MD = (REPO / "docs" / "opensource" / "ARCHITECTURE.md").read_text(encoding="utf-8")
ERRORS_SRC = (SRC / "errors.html").read_text(encoding="utf-8")
ERRORS_BUILT = (PORTAL / "errors.html").read_text(encoding="utf-8")
DETAIL_SRC = (SRC / "detail.html").read_text(encoding="utf-8")
DETAIL_JS = (PORTAL / "assets" / "detail.js").read_text(encoding="utf-8")
TRIAL_SRC = (SRC / "trial.html").read_text(encoding="utf-8")
MY_SRC = (SRC / "my.html").read_text(encoding="utf-8")
MY_JS = (PORTAL / "assets" / "my.js").read_text(encoding="utf-8")
GRANTS_SRC = (SRC / "grants.html").read_text(encoding="utf-8")
GRANTS_JS = (PORTAL / "assets" / "grants.js").read_text(encoding="utf-8")
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


# ── #42-③/#59 docs 页内容随 ux-polish 2026-11-02 整页下线：
#    术语表七条迁 ARCHITECTURE.md §10「术语」，限流口径迁 §9「已知边界」；
#    快速接入四步/5 分钟上手不迁（README Quick start 承接）。守卫改为迁移落点断言 ──

def test_docs_glossary_migrated_to_architecture():
    """术语表七条在 ARCHITECTURE.md §10 全数在位（docs 页下线后的唯一落点）。"""
    assert "## 10. 术语" in ARCHITECTURE_MD, "ARCHITECTURE.md 缺「## 10. 术语」节"
    for term in ("网关（APISIX）", "AppKey 与 secret", "查询键", "上游供数口径",
                 "字段级授权", "降级", "演示数据"):
        assert term in ARCHITECTURE_MD, "术语迁移缺词条：" + term


def test_docs_ratelimit_caliber_migrated_to_architecture():
    """限流与配额口径核心事实并入 ARCHITECTURE.md「已知边界」（docs-ratelimit 区迁移落点）。"""
    assert "限流按登记配额生效" in ARCHITECTURE_MD, "已知边界缺限流口径条目"
    for kw in ("quota_daily", "quota_qps", "X-RateLimit-Reason", "global", "endpoint", "concurrent"):
        assert kw in ARCHITECTURE_MD, "限流口径缺关键事实：" + kw


# ── #44 apply 占位回填 → ux-fix-batch 2026-10-29：apply 页整页下线；
#    原守卫居 test_docs_help_center（随该套件退役），apply＋docs 下线守卫统一移居
#    tests/test_portal_urls.py 配置级用 test_retired_pages_guard_apply_and_docs ──


# ── #46 错误码到动作闭环 ──

def test_detail_error_inline_actions():
    assert "下一步" in DETAIL_SRC, "详情页错误表缺「下一步」列"
    assert 'errActionKey(e)' in DETAIL_SRC and 'errActionGrant(e)' in DETAIL_SRC \
        and 'errActionQuota(e)' in DETAIL_SRC, "详情页错误行缺行动按钮绑定"
    # 静态 href 随构建改写（详情壳在 catalog/ 子目录须 ../ 前缀；ux-polish 2026-11-02：
    # 原「联系运营组开通」docs 锚随 docs 页下线拆分——凭证类→发放页、授权类→授权全景）
    assert '<a class="err-act" href="admin/appkeys.html">领/重签凭证</a>' in DETAIL_SRC, "缺领/重签凭证按钮"
    assert '<a class="err-act" href="grants.html">查授权全景</a>' in DETAIL_SRC, "缺查授权全景按钮"
    assert '<a class="err-act" href="my.html">查看配额</a>' in DETAIL_SRC, "缺查看配额按钮"
    assert "docs.html" not in DETAIL_SRC, "详情源仍链已下线的 docs 页"
    assert "errActionKey" in DETAIL_JS and "errActionGrant" in DETAIL_JS \
        and "errActionQuota" in DETAIL_JS, "detail.js 缺分类函数"
    # 分类口径：401/credential_*→key；403/unknown_tenant/*_not_granted→grant；429/quota_*/rate_limited→my
    key_body = DETAIL_JS.split("errActionKey")[1].split("errActionGrant")[0]
    grant_body = DETAIL_JS.split("errActionGrant")[1].split("errActionQuota")[0]
    assert "401" in key_body and "403" not in key_body, "凭证类分类口径漂移"
    assert "403" in grant_body and "401" not in grant_body, "授权类分类口径漂移"
    assert "service_not_granted" in grant_body and "fields_not_granted" in grant_body, "缺授权类机读码"
    assert "'quota_'" in DETAIL_JS and "rate_limited" in DETAIL_JS, "缺配额类机读码"
    # 产物侧：详情壳改写生效（../admin/ ../grants.html ../my.html 出现在错误行动作里）
    shells = sorted((PORTAL / "catalog").glob("*.html"))
    assert shells, "详情壳缺失（先跑 build_portal）"
    shell = shells[0].read_text(encoding="utf-8")
    assert 'href="../admin/appkeys.html">领/重签凭证' in shell, "详情壳凭证动作未随子目录改写"
    assert 'href="../grants.html">查授权全景' in shell, "详情壳授权动作未随子目录改写"
    assert 'href="../my.html">查看配额' in shell, "详情壳配额动作未随子目录改写"


def test_errors_page_code_ranges_block():
    assert 'id="errors-code-ranges"' in ERRORS_SRC, "errors 页缺码段规则块"
    for kw in ("系统级", "服务级", "auth_failed", "kv_read_failed", "job_state_inconsistent"):
        assert kw in ERRORS_SRC, "码段规则块缺关键词：" + kw
    assert 'href="admin/appkeys.html"' in ERRORS_SRC and 'href="my.html"' in ERRORS_SRC, \
        "码段规则块缺「AppKey 发放页／查看配额」跳转"
    assert 'docs.html' not in ERRORS_SRC, "errors 源仍链已下线的 docs 页"
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
