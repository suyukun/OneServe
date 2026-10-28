#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OneServe 门户 MPA 构建脚本（P-1R · 深色多页版）
====================================================
职责：
  shell 公共件（portal/partials/：侧导航/顶栏/页脚/头）与各页正文源（portal/src/）
  在构建期拼接，产出最终 portal/<page>.html——运行时零构建、无 fetch 注入，
  全部 <script defer> 保序加载，Alpine 初始化不被打断（选型 v0 §3-1 做法）。

输入（均入库，可追溯）：
  portal/partials/shell.head.html   头部模板（@@TITLE@@/@@EXTRA_CSS@@ 占位）
  portal/partials/sidebar.html      侧导航（@@A_<nav>@@/@@C_<nav>@@ active 占位，构建期注入）
  portal/partials/topbar.html       顶栏（@@CRUMB@@ 面包屑占位，≤3 级）
  portal/partials/footer.html       页脚
  portal/src/<page>.html            6 页正文源（只写内容区）

自检（每次构建必过，任一失败即退出非 0，不产出半成品）：
  1) vendor 完整性：portal/assets/vendor/SHA256SUMS 逐件校验（防本地件被静默篡改）；
  2) 关键区块 id：每页必须包含注册表声明的 id（防迁移丢区块，DoD 项）；
  3) 外链扫描：产出 html 不得出现 cdn/unpkg/jsdelivr/google 外链（内网白屏红线，DoD 项）；
  4) 占位符残留：产出 html 不得残留 @@…@@（防模板 typo 静默漏替换）。

幂等：内容未变的页面不重写（mtime 不动）；重复运行产出逐字节一致。
用法：python3 scripts/build_portal.py（仓库根或任意目录均可）
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "portal"
PARTIALS = PORTAL / "partials"
SRC = PORTAL / "src"
VENDOR_DIR = PORTAL / "assets" / "vendor"

# ── 侧导航项（与 partials/sidebar.html 的 @@A_*@@/@@C_*@@ 占位一一对应） ──
# P1-7：原 contact 独立页下线，内容并入 docs（帮助与支持）——无 contact 导航项
# P1-2：errors（错误码中心）；M2-7-a/admin-shell：admin_* 管理组三链（构建期注入 active）
# 卡5 2026-09-30：fields（字段字典）导航项随 297 字典体系下线移除；卡2/卡3 表级 schema
# 元数据就位后若重建字典类页面再增回。
# 三批（2026-10-08 修五）：admin_versions/admin_published 随 versions/published 两页
# 接入壳新增（占位键须与 sidebar.html 的 @@A_*@@/@@C_*@@ 一一对应）。
# admin_accounts 随无登录模式（ADR-0029）拆除：账号管理页下线，导航项同步移除。
NAV_KEYS = ("index", "catalog", "trial", "my", "apply", "ops", "docs", "errors",
            "audit", "grants", "admin_load", "admin_publish", "admin_published",
            "admin_appkeys", "admin_tenants", "admin_grants", "admin_pricing",
            "admin_versions", "admin_perf", "admin_upstreams",
            "admin_monitor", "reports_overview", "reports_subjects",
            "reports_services", "reports_trend", "reports_bills")

# ── P0-2 API 详情页：注册项与正页同构（crumb 增第 3 级服务名） ──
DETAIL_SOURCE = SRC / "detail.html"              # 正文＝portal/src/detail.html（与模板同一真源）
DETAIL_OUT_DIR = PORTAL / "catalog"              # 产出 portal/catalog/<service_id>.html（任务卡口径）
DETAIL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$") # service_id 白名单（防路径穿越）
# 子目录壳的相对路径改写（与 admin/ 同步同法）：shell 按门户根写相对路径，
# 产出在 catalog/ 子目录须整体加 ../ 前缀，否则资产/链接全断（内网静态伺服无 rewrite）。
DETAIL_PATH_REWRITES = (
    ('href="assets/', 'href="../assets/'),
    ('src="assets/', 'src="../assets/'),
    ('src="site-gate.js"', 'src="../site-gate.js"'),   # W2 整站守卫随详情壳同改写（子目录口径）
    ('src="data-format.js"', 'src="../data-format.js"'),
    ('src="api.js"', 'src="../api.js"'),
    ('href="index.html"', 'href="../index.html"'),
    ('href="catalog.html"', 'href="../catalog.html"'),
    ('href="trial.html"', 'href="../trial.html"'),
    ('href="ops.html"', 'href="../ops.html"'),
    ('href="docs.html"', 'href="../docs.html"'),
    ('href="admin/', 'href="../admin/'),
    # P0-10/11 新页随详情壳/子目录壳同步改写（含 x-show 内联串的 SPA 修复改写——先特例后通配）
    ('href="my.html?trace_id="', 'href="../my.html?trace_id="'),
    ('href="apply.html"', 'href="../apply.html"'),
    # 卡5：fields.html 改写规则随字段字典页下线移除（侧栏入口已删，无来源链接）
    ('href="errors.html"', 'href="../errors.html"'),   # P1-2 A6 错误码中心（详情壳侧栏）
    ('href="my.html"', 'href="../my.html"'),
    # P1-1 合规面新页随子目录壳同步改写
    ('href="audit.html"', 'href="../audit.html"'),
    # P1-8 授权登记全景页同款改写
    ('href="grants.html"', 'href="../grants.html"'),
    # 1b-9 报表中心组随子目录壳同步改写（admin/ 与 catalog/ 同为一级子目录，../ 同深度）
    ('href="reports/', 'href="../reports/'),
)  # noqa: E128 —— my 两条同时修复先例遗留断链（详情壳侧栏缺 ../ 前缀）

# ── 页面注册表：标题 / 面包屑（≤3 级：(标签, 链接或 None)）/ 必须保留的关键区块 id ──
PAGES: dict[str, dict] = {
    "index": {
        "title": "OneServe · 统一数据服务平台 · 首页",
        "crumb": [("平台总览", None)],
        "required_ids": [
            "global-search", "kpi-bar", "status-band",
            "announcements", "freshness-card",
        ],
    },
    "catalog": {
        "title": "服务目录 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("服务目录", None)],
        "required_ids": ["catalog-toolbar", "catalog-domain-chips", "service-catalog"],
    },
    "trial": {
        # 门面批 2026-10-08：试用台→接口调试（已发布服务在线调试台，三区单页）。
        # 关键区块＝①控制行（服务下拉＋AppKey）②接口信息卡（地址/入参/出参）
        # ③调用与结果（key 输入＋调用＋字段表/原始 JSON/错误信封）＋调用示例折叠区
        # （curl 为主；trial-tab-http/trial-tab-sdk 锚 id 沿用旧页签名，docs 深链不断）。
        "title": "接口调试 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("接口调试", None)],
        "required_ids": [
            "trial-panel", "trial-controls", "trial-service-select", "trial-apikey",
            "trial-api-info", "trial-endpoint", "trial-param-table", "trial-fields-table",
            "trial-invoke", "trial-key-input", "trial-invoke-btn", "trial-result",
            "trial-error-envelope", "trial-error-code", "trial-error-message",
            "trial-error-docs-link", "trial-not-found", "trial-raw-json",
            "trial-tab-http", "trial-tab-sdk", "trial-example-curl", "trial-example-python",
            # #47 试用告知（2026-10-15 门面批）：告知条＋「试用 vs 生产」差异表
            "trial-notice", "trial-vs-prod",
        ],
    },
    "ops": {
        "title": "运行视图 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("运行视图", None)],
        # P-3 驾驶舱区块 id（顶栏/KPI×4/两图/状态带）；extra_css 仅本页注入 ops.css
        "required_ids": [
            "ops-cockpit", "ops-top",
            "ops-kpi-bar", "ops-kpi-calls", "ops-kpi-success", "ops-kpi-latency", "ops-kpi-tps",
            "ops-chart-trend", "ops-chart-freshness",
            # P1-6：状态带四卡真探活渲染＋「未取到探活结果」如实未知占位（不回退「正常」）
            "ops-statusband", "ops-comp-unknown",
        ],
        "extra_css": '<link rel="stylesheet" href="assets/ops.css"><!-- P-3 运行视图驾驶舱（仅引用既有令牌） -->',
    },
    # A9 帮助与支持（P1-7 结构化；P0-11 联系与支持已并入，故不再有 contact 页）：
    # 5 分钟上手＋术语表＋限流与配额＋SLA＋联系/工单/值班＋变更日志＋SDK 状态
    "docs": {
        "title": "帮助与支持 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("帮助与支持", None)],
        "required_ids": [
            "docs-body", "docs-quickstart", "docs-glossary", "docs-ratelimit",
            "docs-sla", "docs-channels", "docs-notfound-guide", "docs-duty",
            "docs-changelog", "docs-sdk", "docs-support-entry",
            # #59 快速接入四步卡（2026-10-15 门面批，首屏第一卡）
            "docs-quickpath",
        ],
        "extra_css": '<!-- P1-7 帮助与支持（含原 P0-11 联系与支持，均仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/docs.css">',
        # 最小实现：合并后本页为静态内容（无取数），不挂按页 JS
    },
    # 卡5 2026-09-30：fields（P0-9 字段字典）页注册随 297 字典体系下线移除（assets/fields.css
    # 保留——errors 错误码中心复用其码表结构；fields.js/fields.html/sidecar 均已删）。
    # P0-7 我的接入（SPEC A7 最小版，D1=b 凭证兼身份）：凭证/配额/日志三块自助视图
    "my": {
        "title": "我的接入 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("我的接入", None)],
        "required_ids": ["my-caller-bar", "my-credentials", "my-quota", "my-logs",
                         # v1.1 裁决#4：viewer 计量入口块（侧栏「用量统计」锚点）
                         "my-metering", "my-metering-table"],
        "extra_css": '<!-- P0-7 我的接入（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/my.css">',
        # 按页注入组件脚本（extra_js，机制同 extra_css）：只在本页加载 my.js
        "extra_js": '<!-- P0-7 我的接入页组件（Alpine；defer 保序在 alpine.min.js 前注册） -->\n<script defer src="assets/my.js"></script>',
    },
    # P0-10 申请接入与授权进度（SPEC A10 · D10 引 ADR-0003）：OA 外链＋四态时间线＋已批字段清单
    "apply": {
        "title": "申请接入 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("申请接入", None)],
        "required_ids": ["apply-oa-entry", "apply-progress", "apply-approved"],
        "extra_css": '<!-- P0-10 申请接入（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/apply.css">',
    },
    # P1-2 A6 错误码中心（SPEC §2-A6/§4.4）：网关＋服务层单张错误码表；API 错误体
    # docs_url（errors.html#<code>）的锚点落点——每码一行 <tr id="<code>">
    "errors": {
        "title": "错误码中心 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("错误码中心", None)],
        "required_ids": ["errors-envelope", "errors-toolbar", "errors-table-card",
                         "errors-code-ranges"],   # #46 码段规则块（2026-10-15 门面批）
        "extra_css": ('<!-- P1-2 错误码中心（复用 fields.css 的码表结构＋本页专属信封样式） -->\n'
                      '<link rel="stylesheet" href="assets/fields.css">\n'
                      '<link rel="stylesheet" href="assets/errors.css">'),
        "extra_js": '<!-- P1-2 错误码中心页组件（Alpine；defer 保序在 alpine.min.js 前注册） -->\n<script defer src="assets/errors.js"></script>',
    },
    # P1-1 审计查询（SPEC §2-C1 合规审计面首卡）：多条件检索审计台账八元组＋统计摘要
    "audit": {
        "title": "审计查询 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("审计查询", None)],
        "required_ids": ["audit-toolbar", "audit-summary", "audit-table"],
        "extra_css": '<!-- P1-1 审计查询（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/audit.css">',
        "extra_js": '<!-- P1-1 审计查询页组件（Alpine；defer 保序在 alpine.min.js 前注册） -->\n<script defer src="assets/audit.js"></script>',
    },
    # P1-8 授权登记可视化（SPEC §2-C2/§5-P1-8）：租户×appkey×服务×字段全景＋OA 溯源＋字段反查
    "grants": {
        "title": "授权与凭证全景 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("授权与凭证全景", None)],
        "required_ids": ["grants-about", "grants-summary", "grants-toolbar",
                         "grants-table", "grants-field-lookup", "grants-oa"],
        "extra_css": '<!-- P1-8 授权登记全景（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/grants.css">',
        "extra_js": '<!-- P1-8 授权登记全景页组件（Alpine；defer 保序在 alpine.min.js 前注册） -->\n<script defer src="assets/grants.js"></script>',
    },
}

# ── 管理页（本卡：布局统一）──
# 背景：admin/{publish,upstreams,appkeys}.html 原先各自手写完整 <html><head>，
# 只引 tokens.css 拿令牌，没有门户侧栏/顶栏/容器 → 点进去"像两个系统"（维护者 2026-09-23）。
# 做法：把三页纳入本脚本的 partials 拼接机制（与门户正页同一套 shell），
# 正文/脚本/页内样式仍以 admin/<page>.html 为唯一真源（构建期抽取，零手工复制）。
# 契约：admin-gate.js → 业务 js → alpine.min.js 的加载序原样保留（O-2 实证硬规矩）。
# 三批（2026-10-08 修五）：全部接壳管理页的 active 键（与 ADMIN_PAGES 的 nav 值一一对应；
# 管理功能群一期收口批增 admin_pricing；billing 页 nav＝reports_bills（报表中心组，
# 在 NAV_KEYS 内）——本元组只收 admin_* 前缀键，纯声明无消费）。
ADMIN_NAV_KEYS = ("admin_load", "admin_publish", "admin_published",
                  "admin_appkeys", "admin_tenants", "admin_grants", "admin_pricing",
                  "admin_versions", "admin_perf", "admin_upstreams",
                  "admin_monitor")

ADMIN_PAGES: dict[str, dict] = {
    # page(源文件名/active 键) / out(portal 下产物名) / 标题 / 面包屑末级 / 顶部副标题
    # 卡2：load（数据装载）排管理组首位——装载（流程①）先于发布（流程②），
    # 与拆分设计 §四导航序一致：数据装载 / 服务发布 / AppKey 发放 / 上游代理。
    "load": {
        "nav": "admin_load", "out": "load.html", "script": "load.js",
        "title": "数据装载 · OneServe",
        "crumb": [("管理", None), ("数据装载", None)],
        "topbar_sub": "数据装载（流程①：源 → 热缓存）",
    },
    "publish": {
        "nav": "admin_publish", "out": "publish.html", "script": "publish.js",
        # 卡3（NC-4）：四步发布台→五步「服务发布」（流程②；title/crumb/topbar 同步，
        # nav key 不变避免构建契约连锁）。
        "title": "服务发布 · OneServe",
        "crumb": [("管理", None), ("服务发布", None)],
        "topbar_sub": "服务发布（流程②：已装载资产 → 对外服务）",
    },
    "appkeys": {
        "nav": "admin_appkeys", "out": "appkeys.html", "script": "appkeys.js",
        "title": "AppKey 自助发放 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("AppKey 发放", None)],
        "topbar_sub": "AppKey 发放 · 管理（仅管理员可见）",
    },
    # 管理功能群一期·租户管理（管理组第五链；设计稿 D 节 4.2）：租户主体实体
    # 生命周期面（登记/冻结/解冻）——授权页上游（域模型 §5 全链次序）。
    # 读 viewer／写 admin（ROUTE_MIN_ROLE 域8 扩注）；yaml 态新建可用（矩阵登记行）。
    "tenants": {
        "nav": "admin_tenants", "out": "tenants.html", "script": "tenants.js",
        "title": "租户管理 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("租户管理", None)],
        "topbar_sub": "租户管理 · 登记（审批在 OA，此处只做登记与生命周期管理）",
    },
    # 1b-8 授权管理（管理组链，居租户管理之后；设计稿 §1/§2）：租户级授权登记写面——
    # 租户列表 → 租户详情（服务∪代理混排矩阵）→ 编辑流（新增三步向导／变更停用直编）。
    # 只读详情地板 viewer（ROUTE_MIN_ROLE 域8），写操作 admin。
    "grants": {
        "nav": "admin_grants", "out": "grants.html", "script": "grants.js",
        "title": "授权管理 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("授权管理", None)],
        "topbar_sub": "授权管理 · 登记（审批已在 OA 完成，此处仅登记结果）",
    },
    # 管理功能群一期·价目管理（管理组第七链，居授权管理之后；设计稿 D 节 4.2）：
    # 三模型价目＋生效区间——历史账按成交快照不回改。单文件真源（0be2351 交付，
    # 收口批接线）：页内 Alpine 内联脚本，无独立业务 js（script 空＝仅测试契约消费）。
    "pricing": {
        "nav": "admin_pricing", "out": "pricing.html", "script": "",
        "title": "价目管理 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("价目管理", None)],
        "topbar_sub": "价目管理 · 按次/套餐/阶梯三模型＋生效区间（历史账按成交快照不回改）",
    },
    # 管理功能群一期·月账单（报表中心第五链，用量趋势之后；设计稿 D 节 4.2）：
    # 页面产物＝C 节 admin/billing.html（bb4476b＋a331210 交付，收口批接线侧栏/注册）：
    # 账期状态/三硬前置留痕/账单行明细＋CSV 导出。nav＝reports_bills（active 高亮在
    # 报表中心组），单文件真源无独立业务 js。
    "billing": {
        "nav": "reports_bills", "out": "billing.html", "script": "",
        "title": "月账单 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("月账单", None)],
        "topbar_sub": "月账单 · 账期状态/三硬前置留痕/账单行明细＋CSV 导出",
    },
    # 【已删·碑注】W5 账号管理台随无登录模式（ADR-0029）拆除：无人机账号概念，
    # accounts 页/导航项/后端挂载一并移除（历史形态见 git 史）。
    # 三批（2026-10-08 修五·维护者 反馈「孤立」）：versions/published 两页接入门户壳。
    # 沿 B2 回执遗留③口径：注册 ADMIN_PAGES＋NAV_KEYS（上面已加）＋test_admin_shell
    # 页清单各一行；页内 js 逻辑不动，仅外壳统一（sidebar/topbar 完整风格）。
    "published": {
        "nav": "admin_published", "out": "published.html", "script": "published.js",
        "title": "已发布服务 · OneServe",
        "crumb": [("管理", None), ("已发布服务", None)],
        "topbar_sub": "已发布服务一览（字段/授权聚合）＋下线管理",
    },
    "versions": {
        "nav": "admin_versions", "out": "versions.html", "script": "versions.js",
        "title": "版本管理 · OneServe",
        "crumb": [("管理", None), ("版本管理", None)],
        "topbar_sub": "Redis 版本空间一览＋定期清理（系统管理员用）",
    },
    # P3 性能测试页（2026-10-08）：梯子压测触发/状态轮询/历史报告页内查看。
    # 序（维护者 口径）：版本管理之后、上游代理之前。报告伺服双源：服务端点
    # /v1/admin/perf/report/* ＋ 门户静态镜像 perf-reports/（容器态服务层看不到
    # 仓库根，页面按镜像回落；见 sync_perf_reports）。
    "perf": {
        "nav": "admin_perf", "out": "perf.html", "script": "perf.js",
        "title": "性能测试 · OneServe",
        "crumb": [("管理", None), ("性能测试", None)],
        "topbar_sub": "梯子压测：触发／状态轮询／历史报告（run_ladder.sh 编排）",
    },
    "upstreams": {
        "nav": "admin_upstreams", "out": "upstreams.html", "script": "upstreams.js",
        "title": "上游/模拟代理配置 · 运营控制台 · OneServe 门户",
        "crumb": [("管理", None), ("上游代理", None)],
        "topbar_sub": "O-2 上游/模拟代理配置 · 管理（仅管理员可见）",
    },
    # P2c 导航一致性（2026-10-14）：monitor 面包屑与其他管理页不一致（自绘「首页›运营
    # 控制台›异常与批量监控」三段带链接）→ 接入门户壳统一为「管理 › 异常监控」两段
    # 系统路径（同 appkeys 等七页口径；源页 main/style/script 原样抽取，零业务改动）。
    "monitor": {
        "nav": "admin_monitor", "out": "monitor.html", "script": "monitor.js",
        "title": "异常监控 · OneServe",
        "crumb": [("管理", None), ("异常监控", None)],
        "topbar_sub": "B7 异常与批量监控 · 管理（仅管理员可见）",
    },
}

# admin 源页按仓库根写相对路径（../portal/assets/…）；同步进 portal/ 后统一改写为
# portal 根相对路径（../assets/…、../index.html）——单一服务根=portal/（与既有一致）。
ADMIN_PATH_REWRITES = (
    ("../portal/assets/", "../assets/"),
    ("../portal/index.html", "../index.html"),
)

# ── 报表专区一期（1b-9 · 设计稿 docs/design/报表专区一期设计_v1.md §2）：portal/reports/ 子目录壳 ──
# 页面真源＝portal/src/reports/<name>.html（正文片段，与 src/ 同构）；构建期接门户壳
# （partials 同一真源拼接），产出 portal/reports/<out>.html。侧栏「报表中心」组四链
# （置于「合规」组之前）。子目录相对路径改写同 catalog/ 详情壳口径（REPORTS_PATH_REWRITES）。
REPORT_PAGES: dict[str, dict] = {
    "overview": {
        "nav": "reports_overview", "out": "overview.html",
        "title": "用量总览 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("报表中心", "reports/overview.html"), ("用量总览", None)],
        "required_ids": ["usage-kpi-calls", "usage-kpi-rate", "usage-kpi-callers",
                         "usage-kpi-top", "usage-daily-chart", "usage-top-callers"],
        "extra_css": '<!-- 1b-9 报表专区（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/reports.css">',
    },
    "subjects": {
        "nav": "reports_subjects", "out": "subjects.html",
        "title": "主体用量 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("报表中心", "reports/overview.html"), ("主体用量", None)],
        "required_ids": ["usage-subjects-toolbar", "usage-subjects-table"],
        "extra_css": '<!-- 1b-9 报表专区（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/reports.css">',
    },
    "services": {
        "nav": "reports_services", "out": "services.html",
        "title": "服务用量 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("报表中心", "reports/overview.html"), ("服务用量", None)],
        "required_ids": ["usage-services-toolbar", "usage-services-table"],
        "extra_css": '<!-- 1b-9 报表专区（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/reports.css">',
    },
    "trend": {
        "nav": "reports_trend", "out": "trend.html",
        "title": "用量趋势 · OneServe 门户",
        "crumb": [("首页", "index.html"), ("报表中心", "reports/overview.html"), ("用量趋势", None)],
        "required_ids": ["usage-trend-kpis", "usage-trend-chart", "usage-trend-legend"],
        "extra_css": '<!-- 1b-9 报表专区（仅引用既有令牌） -->\n<link rel="stylesheet" href="assets/reports.css">',
    },
}

REPORTS_SRC = SRC / "reports"                    # 真源目录 portal/src/reports/
REPORTS_OUT_DIR = PORTAL / "reports"             # 产出 portal/reports/

# reports 源页按门户根写相对路径；产出在 reports/ 子目录须整体加 ../ 前缀
# （同 catalog/ 详情壳口径）；专区互链 reports/x.html → 同目录兄弟文件（admin 同法）。
REPORTS_PATH_REWRITES = (
    ('href="assets/', 'href="../assets/'),
    ('src="assets/', 'src="../assets/'),
    ('src="site-gate.js"', 'src="../site-gate.js"'),
    ('src="data-format.js"', 'src="../data-format.js"'),
    ('src="api.js"', 'src="../api.js"'),
    ('href="index.html"', 'href="../index.html"'),
    ('href="catalog.html"', 'href="../catalog.html"'),
    ('href="trial.html"', 'href="../trial.html"'),
    ('href="ops.html"', 'href="../ops.html"'),
    ('href="docs.html"', 'href="../docs.html"'),
    ('href="admin/', 'href="../admin/'),
    ('href="my.html?trace_id="', 'href="../my.html?trace_id="'),
    ('href="apply.html"', 'href="../apply.html"'),
    ('href="errors.html"', 'href="../errors.html"'),
    ('href="my.html"', 'href="../my.html"'),
    ('href="audit.html"', 'href="../audit.html"'),
    ('href="grants.html"', 'href="../grants.html"'),
    ('href="reports/', 'href="'),   # 专区互链＝同目录兄弟文件
)

# admin 源页三处结构锚点（构建期抽取正文/脚本/页内样式；缺失＝致命，不产半成品）
ADMIN_STYLE_RE = re.compile(r"<style>(.*?)</style>", re.S)
ADMIN_BODY_ATTRS_RE = re.compile(r"<body([^>]*)>")
ADMIN_MAIN_RE = re.compile(r'<main class="page">(.*?)</main>', re.S)
# main 外、首个 script 前的尾巴段（单文件页 pricing 的四个 modal 真源在此；其余页零命中＝无害）
ADMIN_TAIL_RE = re.compile(r'</main>\s*(.*?)(?=<script)', re.S)
ADMIN_SCRIPT_RES = (
    re.compile(r'<script src="admin-gate\.js">.*?</script>'),
    # 业务 js（可能**多于一个**：M2-5 起 publish.js 前还有 publish-hbase-adapter.js）。
    # 用 findall 全取，防「只搬第一个、后续业务脚本被静默丢弃」——该缺陷由 admin-shell
    # 接入壳时暴露：构建产物只剩适配层、publish.js 缺失（test_admin_shell 脚本序断言逮住）。
    # 管理功能群一期收口批起此段**可选**：pricing/billing 单文件页（Alpine 脚本内联
    # 真源，0be2351/bb4476b 交付形态）零命中＝合法，不再「缺失即致命」。
    re.compile(r'<script src="(?!admin-gate\.js|\.\./)[A-Za-z0-9_-]+\.js">.*?</script>'),
    # 单文件页（pricing/billing）内联 Alpine 组件真源——必须排在 alpine 段**之前**搬运
    # （admin/README 硬规矩：业务 js 先于 alpine.min.js，晚放＝not defined）。非捕获组＝
    # findall 返回完整匹配（带 <script> 标签）。零命中＝合法（接壳页业务 js 全外链）。
    # 缺陷实证（2026-10-21 页面卡）：此段此前未建模，壳版产物 pricing/billing 组件定义
    # 整体缺失，Alpine 启动即 pricingAdmin is not defined、交互全废。
    re.compile(r'<script>(?:.*?)</script>', re.S),
    re.compile(r'<script src="\.\./portal/assets/alpine\.min\.js">.*?</script>'),
)
ADMIN_SCRIPT_OPTIONAL_IDX = (1, 2)   # 可零命中的段（外链业务 js＋内联组件脚本）；闸与 alpine 仍必选


# ── cache-busting（2026-10-15 P0 热修）：壳页静态引用统一追加构建版本参数 ──
# 背景：当日 Chrome 两次以旧缓存的 site-gate.js 浏览门户页，热修注入逻辑后旧缓存
# 不自知，复发「秒踢登录页」。对策：构建期给壳页全部本地 .js/.css 引用追加
# ?v=<内容指纹>——资产一变全站版本随动，旧缓存 URL 自然失效；内容不变版本不变
# （幂等纪律不破，mtime 不动）。
ASSET_ATTR_RE = re.compile(r'((?:src|href)=")((?:\../|assets/)?[A-Za-z0-9_./-]+\.(?:js|css))(")')
HTML_COMMENT_SPLIT_RE = re.compile(r'(<!--.*?-->)', re.S)

# W2 门禁产物级形态：守卫脚本与取数层引用必须已带版本参数（缺＝构建失败）
GATE_VERSIONED_RE = re.compile(r'src="(?:\.\./)?site-gate\.js\?v=[0-9a-f]+"')
LAYER_VERSIONED_RE = re.compile(r'"(?:\.\./|assets/)?(?:data-format|api|pages)\.js\?v=[0-9a-f]+"')


def compute_asset_version() -> str:
    """壳页静态资产的内容指纹（sha256 前 12 hex）＝cache-busting 版本码。

    口径：只吃源侧输入——门户根 *.js（site-gate/data-format/api）＋ portal/assets/
    全件 ＋ admin/ 业务 js（真源目录，非 portal/admin 拷贝），排序后逐件
    「相对路径 \0 sha256」喂哈希。mtime 不参与（跨机/重拷贝稳定）；构建期拷贝
    （portal/admin 整目录 rmtree+copytree）不影响指纹。
    """
    files: list[Path] = []
    files.extend(sorted(PORTAL.glob("*.js")))
    assets_dir = PORTAL / "assets"
    if assets_dir.is_dir():
        files.extend(sorted(p for p in assets_dir.rglob("*") if p.is_file()))
    admin_src = ROOT / "admin"
    if admin_src.is_dir():
        files.extend(sorted(p for p in admin_src.rglob("*.js") if p.is_file()))
    if not files:
        sys.exit("[FAIL] 静态资产清单为空（portal/*.js 与 assets/ 俱失）——拒绝产出版本码")
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(ROOT)).encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:12]


def add_cache_busting(html: str, version: str) -> str:
    """给壳页产物里本地 .js/.css 的 src/href 引用追加 ?v=<version>。

    只处理 HTML 注释以外的文本片段（W2 门禁注释锚点原样保留）；已带 ? 的引用、
    外链（http(s): 的冒号不在值字符类内）、协议相对 //（回调显式拦）一律不动
    ＝外站 URL 绝不注入。"""
    def _bust_segment(seg: str) -> str:
        def _sub(m: re.Match) -> str:
            path = m.group(2)
            if path.startswith("//"):
                return m.group(0)
            return f"{m.group(1)}{path}?v={version}{m.group(3)}"
        return ASSET_ATTR_RE.sub(_sub, seg)

    parts = HTML_COMMENT_SPLIT_RE.split(html)
    return "".join(p if i % 2 else _bust_segment(p) for i, p in enumerate(parts))


# ── P0-2：API 详情页元数据（不进正页循环；产出 portal/openapi/<service_id>.html） ──
DETAIL_META = {
    "title": "API 详情 · OneServe 门户",
    "crumb": [("首页", "index.html"), ("服务目录", "catalog.html"), ("API 详情", None)],
    "required_ids": [],
}

# DoD 外链红线口径：https?://(cdn|unpkg|jsdelivr|google) 前缀主机
EXTERNAL_URL_RE = re.compile(r"https?://(?:cdn|unpkg|jsdelivr|google)")
# 模板占位符残留（构建后不得再出现）
PLACEHOLDER_RE = re.compile(r"@@[A-Za-z0-9_]+@@")


def read_text(path: Path) -> str:
    """读入 UTF-8 文本（源文件统一 LF）。"""
    return path.read_text(encoding="utf-8")


def render_crumb(crumb: list[tuple[str, str | None]]) -> str:
    """面包屑 ≤3 级；末项为当前页（无链接＋aria-current）。"""
    items = []
    for label, href in crumb:
        if href is None:
            items.append(f'<li aria-current="page">{label}</li>')
        else:
            items.append(f'<li><a href="{href}">{label}</a></li>')
    return '<ol class="os-crumb-list">' + "".join(items) + "</ol>"


def verify_vendor() -> None:
    """自检 1：SHA256SUMS 逐件校验（清单缺失/文件缺失/哈希不符均失败）。"""
    manifest = VENDOR_DIR / "SHA256SUMS"
    if not manifest.is_file():
        sys.exit(f"[FAIL] vendor 清单缺失：{manifest}")
    entries: list[tuple[str, str]] = []
    for line in read_text(manifest).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        entries.append((digest, name))
    if not entries:
        sys.exit("[FAIL] vendor 清单为空")
    for digest, name in entries:
        f = VENDOR_DIR / name
        if not f.is_file():
            sys.exit(f"[FAIL] vendor 文件缺失：{name}")
        actual = hashlib.sha256(f.read_bytes()).hexdigest()
        if actual != digest:
            sys.exit(f"[FAIL] vendor 哈希不符：{name} 期望 {digest} 实际 {actual}")
    print(f"[ok] vendor 完整性：{len(entries)} 件逐件 sha256 校验通过")


def render_page(name: str, meta: dict, head: str, sidebar: str, topbar: str, footer: str) -> str:
    """拼接单页：先组装完整文档，再统一做占位符替换（active 态/面包屑构建期注入）。"""
    content = read_text(SRC / f"{name}.html")
    html = (
        head
        + sidebar
        + '\n<div class="os-main">\n'
        + topbar
        + '\n<main class="os-content" id="app-content">\n'
        + content
        + "\n</main>\n"
        + footer
        + "\n</div>\n</body>\n</html>\n"
    )
    # @@EXTRA_CSS@@/@@EXTRA_JS@@ 支持按页注入（P-3 ops.css / P0-7 my.js）；未声明则为空串
    html = (html.replace("@@TITLE@@", meta["title"])
                .replace("@@EXTRA_CSS@@", meta.get("extra_css", ""))
                .replace("@@EXTRA_JS@@", meta.get("extra_js", "")))
    for nav in NAV_KEYS:
        active = " is-active" if nav == name else ""
        current = ' aria-current="page"' if nav == name else ""
        html = html.replace(f"@@A_{nav}@@", active).replace(f"@@C_{nav}@@", current)
    html = html.replace("@@CRUMB@@", render_crumb(meta["crumb"]))
    return html


def check_page(name: str, html: str, meta: dict | None = None) -> None:
    """自检 2/3/4：关键区块 id ＋ 外链红线 ＋ 占位符残留（meta 缺省取 PAGES 注册）。"""
    meta = meta if meta is not None else PAGES[name]
    for block_id in meta["required_ids"]:
        if f'id="{block_id}"' not in html:
            sys.exit(f"[FAIL] {name}.html 缺关键区块 id：{block_id}")
    m = EXTERNAL_URL_RE.search(html)
    if m:
        sys.exit(f"[FAIL] {name}.html 出现外链红线：{m.group(0)}")
    p = PLACEHOLDER_RE.search(html)
    if p:
        sys.exit(f"[FAIL] {name}.html 残留模板占位符：{p.group(0)}")


def check_gate_order(name: str, html: str) -> None:
    """自检 5（W2 · 2026-10-15 设计稿 A2 门禁；同日热修加 cache-busting 复验）：
    site-gate.js 必须先于取数层装载，且引用必须带构建版本参数。

    壳页须注入带 ?v=<指纹> 的 site-gate.js（src 形态含子目录 ../ 变体；版本参数由
    add_cache_busting 统一追加、此处复验，缺版本＝构建失败，防静默回退裸引用旧缓存），
    且其出现位置早于 data-format.js/api.js/pages.js——防 401 auth_required 落 api.js
    mock 降级出假「演示数据」页（设计稿 A2 点名的加载序硬规矩）。独立 selftest 页不经
    本函数（无壳），由 tests/test_site_gate.py 断言其手工标注。"""
    m_gate = GATE_VERSIONED_RE.search(html)
    if not m_gate:
        sys.exit(f"[FAIL] {name} 未注入带版本参数的 site-gate.js（W2 整站守卫门禁＋cache-busting）")
    m_layer = LAYER_VERSIONED_RE.search(html)
    if m_layer and m_layer.start() < m_gate.start():
        sys.exit(f"[FAIL] {name} 加载序违规：取数层脚本先于 site-gate.js 装载")


def render_detail_page(service_id: str, service_name: str, head: str, sidebar: str,
                       topbar: str, footer: str) -> str:
    """P0-2：单服务详情静态壳（portal/catalog/<service_id>.html）。

    与正页同构拼接：正文＝portal/src/detail.html（与详情模板同一真源，数据由 detail.js
    fetch ../openapi/details/<service_id>.json 注入）；面包屑第 3 级＝服务名
    （构建期已知，来自契约 sidecar）；active 态挂 catalog（详情页归属目录层）；
    shell 相对路径按子目录整体改写（DETAIL_PATH_REWRITES，同 admin/ 同步法）。
    """
    meta = DETAIL_META
    content = read_text(DETAIL_SOURCE)
    html = (
        head
        + sidebar
        + '\n<div class="os-main">\n'
        + topbar
        + '\n<main class="os-content" id="app-content">\n'
        + content
        + "\n</main>\n"
        + footer
        + "\n</div>\n</body>\n</html>\n"
    )
    title = f"{service_name} · API 详情 · OneServe 门户"
    html = html.replace("@@TITLE@@", title).replace("@@EXTRA_CSS@@", "").replace("@@EXTRA_JS@@", "")
    for nav in NAV_KEYS:
        active = " is-active" if nav == "catalog" else ""
        current = ' aria-current="page"' if nav == "catalog" else ""
        html = html.replace(f"@@A_{nav}@@", active).replace(f"@@C_{nav}@@", current)
    crumb = meta["crumb"][:-1] + [(service_name, None)]
    html = html.replace("@@CRUMB@@", render_crumb(crumb))
    for old, new in DETAIL_PATH_REWRITES:
        html = html.replace(old, new)
    return html


def write_openapi_shells(head: str, sidebar: str, topbar: str, footer: str, version: str) -> list:
    """P0-2：按契约 sidecar 生成每服务详情静态壳（缺契约＝致命，不产半成品）。

     纪律：catalog/ 壳须与 openapi/ sidecar 同构建期刷新——壳的存在性以
    openapi/catalog.json 的 services 列表为唯一口径，多余旧壳清除（防悬空死链）。
    """
    catalog_path = PORTAL / "openapi" / "catalog.json"
    if not catalog_path.is_file():
        sys.exit("[FAIL] portal/openapi/catalog.json 缺失——先跑 scripts/gen_openapi.py（契约真源）")
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    services = catalog.get("services") or []
    if not services:
        # 2026-09-29 全服务下线为合法终态（services: []）：目录壳清空＋响亮警告，构建不失败；
        # 真异常（catalog 缺失/坏 JSON）仍走上面的 sys.exit。
        print("[WARN] catalog 无服务条目（全服务下线态）：清空详情壳目录，跳过详情页构建")
    out_dir = DETAIL_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for svc in services:
        sid = str(svc.get("service_id", ""))
        name = str(svc.get("name") or sid)
        if not DETAIL_NAME_RE.fullmatch(sid):
            sys.exit(f"[FAIL] service_id 非法（白名单 [A-Za-z0-9_-]）：{sid!r}")
        html = render_detail_page(sid, name, head, sidebar, topbar, footer)
        html = add_cache_busting(html, version)          # cache-busting：详情壳同批版本化
        check_page("detail-shell", html, DETAIL_META)
        check_gate_order(f"catalog/{sid}.html", html)   # W2 门禁：详情壳同序
        out = out_dir / f"{sid}.html"
        results.append((out, write_if_changed(out, html), len(html.encode("utf-8"))))
    for stale in out_dir.glob("*.html"):
        if stale.stem not in {s["service_id"] for s in services}:
            stale.unlink()
            print(f"[ok] 清除陈旧详情壳：{stale.name}")
    return results


def _extract_admin_parts(src_html: str, name: str) -> tuple[str, str, str, list[str]]:
    """从 admin/<page>.html 抽取门户壳所需四件：body 属性 / 页内样式 / 正文 / 尾部脚本。

    纪律：正文与样式以 admin 源页为唯一真源，构建期抽取而非手抄（防漂移）；
    admin-gate.js → 业务 js → alpine.min.js 的加载序原样搬运（O-2 实证硬规矩）。
    任一锚点缺失＝构建失败（不产半成品）。
    """
    m_style = ADMIN_STYLE_RE.search(src_html)
    m_body = ADMIN_BODY_ATTRS_RE.search(src_html)
    m_main = ADMIN_MAIN_RE.search(src_html)
    if not (m_style and m_body and m_main):
        sys.exit(f"[FAIL] admin/{name}.html 结构锚点缺失（<style>/<body>/<main class=\"page\">）")
    scripts = []
    for idx, pat in enumerate(ADMIN_SCRIPT_RES):
        found = pat.findall(src_html)          # 全取：业务 js 可多于一个（适配层＋业务 js）
        if not found:
            if idx in ADMIN_SCRIPT_OPTIONAL_IDX:
                continue   # 单文件页（pricing/billing）无独立业务 js＝合法形态
            sys.exit(f"[FAIL] admin/{name}.html 缺脚本锚点：{pat.pattern}")
        for raw in found:
            # 统一加 defer：head 里 data-format/api/app.js 等也是 defer，defer 按文档序执行
            # → 组件注册（app.js 的 alpine:init）先于本页 alpine.min.js 执行，否则 shell 的
            # 环境条 x-data="envBar" 会在 Alpine 启动时报 "envBar is not defined"（浏览器实证）。
            # 相对顺序不变（admin-gate → 业务 js（按源页序）→ alpine），硬规矩保持。
            scripts.append(raw.replace("<script src=", "<script defer src=", 1))
    m_tail = ADMIN_TAIL_RE.search(src_html)
    content = m_main.group(1) + (m_tail.group(1) if m_tail else "")
    return m_body.group(1).strip(), m_style.group(0), content, scripts


def render_admin_page(name: str, head: str, sidebar: str, topbar: str, footer: str) -> str:
    """把管理页接入门户壳：同一套侧导航/环境条/顶栏/容器/页脚（本卡唯一目标）。

    只做布局统一——页内样式、正文、脚本、id 契约原样保留：
      · 侧栏/页脚/环境条 = portal/partials 同一真源（构建期拼接，零手抄）；
      · 顶栏 = 门户 os-topbar 换装箱：品牌副标题＋管理令牌栏＋admin-crumb 面包屑；
      · 正文 = admin/<page>.html 的 <main class="page"> 内容，外包 .os-container；
      · 尾部脚本 = admin-gate.js → 业务 js → alpine.min.js（顺序不变）。
    """
    meta = ADMIN_PAGES[name]
    src_html = read_text(ROOT / "admin" / f"{name}.html")
    body_attrs, style_block, content, scripts = _extract_admin_parts(src_html, name)

    # 2026-09-23：管理令牌机制已彻底拆除（维护者 原话：完全拆掉，现在用不上，等以后再加）；
    # 本机/内网阶段授权边界＝网络层可达，写操作不再校验任何令牌，故顶栏不再生成
    # 「管理令牌」输入框/保存按钮（admin-token-input/admin-token-save）。原 token_row 拼接删除。
    # 2026-09-29 维护者 定调：admin 顶栏＝系统路径（树状位置），如「管理 › 发布台」；
    # 品牌名在侧栏 logo（不重复）；环境标注已废（SPEC §四）；无「首页」级（管理页不在首页路径下）。
    tail = meta["crumb"]
    crumb = ('<nav class="crumb" id="admin-crumb" aria-label="面包屑">'
             + "".join(
                 ('<span class="crumb-sep">›</span>' if i else "")
                 + "<span"
                 + (' aria-current="page"' if i == len(tail) - 1 else "")
                 + f">{label}</span>"
                 for i, (label, _href) in enumerate(tail)
             ) + "</nav>")
    admin_topbar = topbar.replace(
        '<nav class="os-crumb" aria-label="面包屑">@@CRUMB@@</nav>', crumb)

    # 源页 <body> 属性（如 x-data="publishAdmin()" x-init="boot()"）必须落到产物 body 上：
    # shell.head.html 里的 <body class="os-layout"> 只是布局基类，源页属性缺了＝Alpine 组件
    # 从不实例化 → 页面整块空白（2026-09-23 实机走查：发布台"选数据源后下方空白"，
    # 根因＝body_attrs 抽取后从未拼接，属构建期丢弃而非页面写错）。合并＝基类在前、源页属性在后。
    body_open = '<body class="os-layout"' + (f' {body_attrs}' if body_attrs else "") + '>'
    html = (
        # 门户默认在 head 尾部 defer 加载 alpine；管理页须由尾部"闸→业务→alpine"自管，故摘除
        head.replace('<script defer src="assets/alpine.min.js"></script>', "")
        .replace('<body class="os-layout">', body_open, 1)
        + sidebar
        + '\n<div class="os-main">\n'
        + admin_topbar
        + '\n<main class="os-content" id="app-content">\n'
        + '<div class="os-container">' + content + "</div>"
        + "\n</main>\n"
        + footer
        + "\n</div>\n"
        + "\n".join(scripts) + "\n"
        + "</body>\n</html>\n"
    )
    html = html.replace("@@TITLE@@", meta["title"])
    # 【已删·碑注】发布台环境标注特删块（os-envbar re.sub＋env_chip 两处残留）随 2026-09-29
    # 「环境条源头删除」作废：partials/topbar.html 已无 os-envbar，env_chip 变量已先删。
    # 页内 <style> 经 EXTRA_CSS 注入（位于 portal.css 之后 → 管理页语义别名覆盖生效，外观不变）
    html = html.replace("@@EXTRA_CSS@@", style_block).replace("@@EXTRA_JS@@", "")
    # 占位符清零：nav 处已在 admin_topbar 内替换为 admin-crumb；此处清 topbar 注释里的同名残留
    html = html.replace("@@CRUMB@@", "")
    for nav in NAV_KEYS:
        active = " is-active" if nav == meta["nav"] else ""
        current = ' aria-current="page"' if nav == meta["nav"] else ""
        html = html.replace(f"@@A_{nav}@@", active).replace(f"@@C_{nav}@@", current)
    # 相对路径改写：源页按仓库根写（../portal/…）；shell 按门户根写 → portal/admin/ 子目录口径
    for old, new in ADMIN_PATH_REWRITES + DETAIL_PATH_REWRITES:
        html = html.replace(old, new)
    html = html.replace('href="../admin/', 'href="')   # 管理页互链＝同目录兄弟文件
    return html


def write_admin_pages(head: str, sidebar: str, topbar: str, footer: str, version: str) -> list:
    """产出 portal/admin/{publish,appkeys,upstreams}.html（门户壳版），并机检闸契约 id。"""
    out_dir = PORTAL / "admin"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 2026-09-23 起管理令牌闸已彻底拆除，gate 相关 id（admin-token-input/admin-token-save/
    # gate-banner/gate-banner-text/token-error）不再生成，因此不再强制；仅保留布局契约 id。
    required = ("app-sidebar", "app-topbar", "app-content", "admin-crumb")
    results = []
    for name, meta in ADMIN_PAGES.items():
        html = render_admin_page(name, head, sidebar, topbar, footer)
        html = add_cache_busting(html, version)          # cache-busting：管理壳页同批版本化
        for block_id in required:
            if f'id="{block_id}"' not in html:
                sys.exit(f"[FAIL] admin/{meta['out']} 缺关键区块 id：{block_id}")
        if 'class="os-container"' not in html:
            sys.exit(f"[FAIL] admin/{meta['out']} 正文未包门户容器 os-container")
        check_page(f"admin/{name}", html, {"required_ids": []})
        check_gate_order(f"admin/{meta['out']}", html)   # W2 门禁：管理壳页同序
        out = out_dir / meta["out"]
        results.append((meta["out"], write_if_changed(out, html), len(html.encode("utf-8"))))
    return results


def write_if_changed(out: Path, content: str) -> str:
    """幂等落盘：内容一致则不重写（mtime 不动）。"""
    if out.is_file() and out.read_text(encoding="utf-8") == content:
        return "unchanged"
    out.write_text(content, encoding="utf-8", newline="\n")
    return "written"


def write_reports_pages(head: str, sidebar: str, topbar: str, footer: str,
                        version: str) -> list:
    """报表专区一期四页（1b-9）：portal/src/reports/*.html 接门户壳 → portal/reports/。

    与正页同一套 partials 拼接（侧栏「报表中心」组 active 态构建期注入）；正文真源
    为 src 片段（extra_css/extra_js 按页注入，tail 经典 script 随源页原样搬运——
    echarts→reports.js 加载序沿 ops 页惯例）；产出做子目录相对路径改写＋
    cache-busting＋关键区块 id/外链红线/占位残留/W2 加载序门禁（同正页全闸）。
    """
    out_dir = REPORTS_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name, meta in REPORT_PAGES.items():
        content = read_text(REPORTS_SRC / f"{name}.html")
        html = (
            head
            + sidebar
            + '\n<div class="os-main">\n'
            + topbar
            + '\n<main class="os-content" id="app-content">\n'
            + content
            + "\n</main>\n"
            + footer
            + "\n</div>\n</body>\n</html>\n"
        )
        html = (html.replace("@@TITLE@@", meta["title"])
                    .replace("@@EXTRA_CSS@@", meta.get("extra_css", ""))
                    .replace("@@EXTRA_JS@@", meta.get("extra_js", "")))
        for nav in NAV_KEYS:
            active = " is-active" if nav == meta["nav"] else ""
            current = ' aria-current="page"' if nav == meta["nav"] else ""
            html = html.replace(f"@@A_{nav}@@", active).replace(f"@@C_{nav}@@", current)
        html = html.replace("@@CRUMB@@", render_crumb(meta["crumb"]))
        for old, new in REPORTS_PATH_REWRITES:
            html = html.replace(old, new)
        html = add_cache_busting(html, version)
        check_page(f"reports/{name}", html, meta)
        check_gate_order(f"reports/{meta['out']}", html)
        out = out_dir / meta["out"]
        results.append((meta["out"], write_if_changed(out, html), len(html.encode("utf-8"))))
    return results


# ── P3 性能测试页（2026-10-08）：压测报告静态镜像 ──
# 背景：报告真源在 loadtest/results/<run>/performance-report.{html,md}；容器栈里
# 服务进程看不到仓库根（loadtest/ 未挂载）→ /v1/admin/perf/reports 空表、
# /v1/admin/perf/report/* 404。最小静态伺服＝构建期把报告镜像进 portal/perf-reports/
# （nginx 已直出 portal/ 根，零站点配置改动），并写 index.json 清单供性能测试页回落
# 展示。只镜像报告两件（locust CSV/键池等大件不入门户）；多余旧镜像随源清理（同
# catalog 壳口径，防悬空）。产物可再生，不入库（git 不 commit）。
PERF_REPORTS_DIR = PORTAL / "perf-reports"
PERF_RUNDIR_RE = re.compile(r"(\d{8}-\d{6})(?:-(.+))?")


def sync_perf_reports() -> int:
    """loadtest/results/*/performance-report.{html,md} → portal/perf-reports/＋清单。

    返回镜像的 run 目录数（供构建日志）。results/ 整体缺失＝空清单如实落盘
    （页面显示「还没有历史报告」，不残留旧镜像冒充现存数据）。
    """
    src_root = ROOT / "loadtest" / "results"
    manifest: list[dict] = []
    sources: dict[str, Path] = {}
    if src_root.is_dir():
        for d in sorted(src_root.iterdir(), key=lambda p: p.name, reverse=True):
            if not d.is_dir() or d.name.startswith("."):
                continue
            html_ok = (d / "performance-report.html").is_file()
            md_ok = (d / "performance-report.md").is_file()
            if not (html_ok or md_ok):
                continue
            svc, svc_name = "", ""
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                svc = str((meta.get("service") or {}).get("service_id") or "")
                svc_name = str((meta.get("service") or {}).get("name") or "")
            except Exception:  # noqa: BLE001 —— meta 缺/坏＝目录短名兜底
                pass
            m = PERF_RUNDIR_RE.fullmatch(d.name)
            short = svc or (m.group(2) if m else "")
            manifest.append({
                "dir": d.name,
                "ts": m.group(1) if m else d.name,
                "service": short,
                "service_name": svc_name or short,   # 镜像也带全名（页面名/ID 不同值双行）
                "report_html": html_ok,
                "report_md": md_ok,
            })
            sources[d.name] = d
    PERF_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, src in sources.items():
        out_dir = PERF_REPORTS_DIR / name
        out_dir.mkdir(parents=True, exist_ok=True)
        for fname in ("performance-report.html", "performance-report.md"):
            s = src / fname
            if s.is_file():
                write_if_changed(out_dir / fname, s.read_text(encoding="utf-8"))
    write_if_changed(PERF_REPORTS_DIR / "index.json",
                     json.dumps({"reports": manifest}, ensure_ascii=False, indent=1) + "\n")
    for stale in PERF_REPORTS_DIR.iterdir():     # 防悬空：源已删的镜像随清
        if stale.name == "index.json":
            continue
        if stale.is_dir() and stale.name not in sources:
            shutil.rmtree(stale)
            print(f"[ok] 清除陈旧报告镜像：{stale.name}")
    return len(manifest)


def main() -> int:
    verify_vendor()
    version = compute_asset_version()
    print(f"[ok] cache-busting 版本码：?v={version}（静态资产内容指纹，内容不变则不变）")

    head = read_text(PARTIALS / "shell.head.html")
    sidebar = read_text(PARTIALS / "sidebar.html")
    topbar = read_text(PARTIALS / "topbar.html")
    footer = read_text(PARTIALS / "footer.html")

    results = []
    for name, meta in PAGES.items():
        html = render_page(name, meta, head, sidebar, topbar, footer)
        html = add_cache_busting(html, version)   # cache-busting：壳页静态引用全量版本化
        check_page(name, html)
        check_gate_order(name + ".html", html)   # W2 门禁：守卫先于取数层＋带版本
        out = PORTAL / f"{name}.html"
        results.append((out.name, write_if_changed(out, html), len(html.encode("utf-8"))))

    for fname, status, size in results:
        print(f"[ok] {fname:<14} {status:<10} {size:>7} bytes")
    print(f"[done] {len(results)} 页构建完成 → portal/（幂等：重跑 unchanged）")

    # ── P0-2：API 详情页静态壳（每服务一个 portal/catalog/<service_id>.html）──
    detail_results = write_openapi_shells(head, sidebar, topbar, footer, version)
    for out, status, size in detail_results:
        print(f"[ok] catalog/{out.name:<28} {status:<10} {size:>7} bytes")
    print(f"[done] {len(detail_results)} 个 API 详情页构建完成 → portal/catalog/")

    # ── admin 同步（修复 Docker 29 嵌套挂载 mkdirat 只读失败：改构建期拷贝进 portal/）──
    # 顺序：先整目录拷贝（js/mock/selftest 等随门户单挂载伺服），再改写非页面 HTML 的相对
    # 路径；最后用门户壳生成三张管理页覆盖拷贝来的源页（源页仍是唯一真源，构建期抽取）。
    src_admin = ROOT / "admin"
    dst_admin = PORTAL / "admin"
    if src_admin.is_dir():
        if dst_admin.exists():
            shutil.rmtree(dst_admin)
        shutil.copytree(src_admin, dst_admin,
                        ignore=shutil.ignore_patterns("debug-*", "__pycache__"))
        page_names = {m["out"] for m in ADMIN_PAGES.values()}
        n = 0
        for f in dst_admin.rglob("*.html"):
            if f.name in page_names:
                continue   # 三张管理页改由 render_admin_page 生成（门户壳）
            t = f.read_text(encoding="utf-8")
            t2 = t.replace("../portal/assets/", "../assets/").replace("../portal/index.html", "../index.html")
            if t2 != t:
                f.write_text(t2, encoding="utf-8")
            n += 1
        print(f"[ok] admin/       synced     {n:>7} files → portal/admin/（随门户单挂载伺服）")

    # ── 管理三页接入门户壳（本卡交付）：同一套侧导航/顶栏/容器/页脚 ──
    admin_results = write_admin_pages(head, sidebar, topbar, footer, version)
    for fname, status, size in admin_results:
        print(f"[ok] admin/{fname:<8} {status:<10} {size:>7} bytes")
    print(f"[done] {len(admin_results)} 个管理页构建完成 → portal/admin/（门户壳版）")

    # ── 报表专区一期四页（1b-9 · 设计稿 §2）：portal/reports/ 子目录壳 ──
    reports_results = write_reports_pages(head, sidebar, topbar, footer, version)
    for fname, status, size in reports_results:
        print(f"[ok] reports/{fname:<12} {status:<10} {size:>7} bytes")
    print(f"[done] {len(reports_results)} 个报表专区页构建完成 → portal/reports/")

    # ── P3 性能测试页：压测报告静态镜像（容器态页面回落数据源） ──
    n_perf = sync_perf_reports()
    print(f"[done] perf-reports/ 镜像 {n_perf} 个 run 的报告 → portal/perf-reports/（幂等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
