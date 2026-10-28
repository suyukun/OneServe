#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-10 差量·运营两页门户套件（tests/test_ops_portal.py）——DOM ＋ 组件契约。

任务卡：前台重造 SPEC v0 §5-P1-10（B3 发布台 / B4 上游代理）四小项：
  1. 生效范围收敛为「先测试/直接上生产」（保留 scope-tuple 容器，退役 env/instance 双下拉；
     admin_api 读 releaseMode 传 ops_config 派生——链路修通，见 test_ops_config）；
  2. 错误提示就近（错误行内挂在出错控件旁，去掉顶部统一弹 form-error）；
  3. 字段表中文名＋列名双列（字段字典 cn_name 真源）；
  4. 上游页门槛提示条（本页为技术运维配置，业务无需操作）。
运行：python3 -m pytest tests/test_ops_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
PORTAL_ADMIN = REPO / "portal" / "admin"
PUB_HTML = ADMIN / "publish.html"
PUB_JS = ADMIN / "publish.js"
UPS_HTML = ADMIN / "upstreams.html"
UPS_JS = ADMIN / "upstreams.js"
ST_PUB_HTML = ADMIN / "selftest-publish.html"
ST_PUB_JS = ADMIN / "selftest-publish.js"
ST_UPS_HTML = ADMIN / "selftest-upstreams.html"
ST_UPS_JS = ADMIN / "selftest-upstreams.js"

if not (PORTAL_ADMIN / "publish.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


class _TagText(HTMLParser):
    """极简 DOM 断言器（test_quota_portal 同款，无第三方依赖）。"""

    def __init__(self):
        super().__init__()
        self.tags = []
        self.texts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.texts.append(data.strip())


def _dom(html: str) -> _TagText:
    p = _TagText()
    p.feed(html)
    return p


def _ids(dom: _TagText) -> set:
    return {a.get("id") for _, a in dom.tags if a.get("id")}


PUB_HTML_SRC = PUB_HTML.read_text(encoding="utf-8")
PUB_JS_SRC = PUB_JS.read_text(encoding="utf-8")
UPS_HTML_SRC = UPS_HTML.read_text(encoding="utf-8")
UPS_JS_SRC = UPS_JS.read_text(encoding="utf-8")
ST_PUB_HTML_SRC = ST_PUB_HTML.read_text(encoding="utf-8")
ST_PUB_JS_SRC = ST_PUB_JS.read_text(encoding="utf-8")
ST_UPS_HTML_SRC = ST_UPS_HTML.read_text(encoding="utf-8")
ST_UPS_JS_SRC = ST_UPS_JS.read_text(encoding="utf-8")


# ── 1. 生效范围收敛（P1-10 差量）：单选择器，旧双下拉退役 ──

def test_publish_one_step_no_release_mode():
    """发布台重写（SPEC §八.3，维护者 2026-09-29 批准）：两段式发布砍除，一步发布。

    旧「先测试/直接上生产」选择器、环境×实例控件、留痕条全部退役。"""
    ids = _ids(_dom(PUB_HTML_SRC))
    for dead in ("scope-tuple", "release-mode-select", "release-scope-note",
                 "env-select", "instance-select", "scope-env", "scope-instance", "scope-x"):
        assert dead not in ids, f"两段式/旧环境控件未退役：{dead}"
    for dead in ("RELEASE_MODES", "releaseMode", "release_mode"):
        assert dead not in PUB_JS_SRC, f"publish.js 残留两段式逻辑：{dead}"
    for word in ("先测试", "直接上生产", "落盘"):
        assert word not in PUB_HTML_SRC, f"页面残留两段式文案：{word}"
    # 一步发布：④ 只有 [发布]，完成页直达
    assert 'id="publish-btn"' in PUB_HTML_SRC and 'id="step-done"' in PUB_HTML_SRC


# ── 2. 错误提示就近（P1-10 差量）──

def test_publish_errors_inline_at_controls():
    """错误就近（卡3 五步版）：每步失败条/错误位挂在对应控件旁，无顶部统一错误块。"""
    ids = _ids(_dom(PUB_HTML_SRC))
    # 卡3：①表源 assets-error；②切版 switch-error；③地址 route-error；
    #      ④字段源 schema-error；⑤发布 publish-error。
    for el_id in ("assets-error", "schema-error", "switch-error",
                  "route-error", "publish-error"):
        assert el_id in ids, f"缺控件旁行内错误位：{el_id}"
    # P3 二批（拆页 2026-10-08）：下线随已发布服务区迁至 published.html（行内错误位同迁）
    pubd_ids = _ids(_dom((ADMIN / "published.html").read_text(encoding="utf-8")))
    assert "offline-error" in pubd_ids, "缺控件旁行内错误位：offline-error（已发布页）"
    assert "form-error" not in ids, "顶部统一错误块 form-error 应退役（错误就近）"


def test_publish_js_exposes_per_control_errors():
    """失败文案集中＝纯函数（WIZ.*）：出了什么事＋你该干什么，逐控件取用。

    卡3 五步：验证/加载步骤已下线（归流程①），文案集＝切版/发布/下线/地址。"""
    for fn in ("switchFailText", "publishFailText", "offlineFailText", "routeErrorText"):
        assert fn in PUB_JS_SRC, f"缺逐控件失败文案函数：{fn}"
    # 每条失败文案都带「你该干什么」出口（重试/再试/联系平台管理员/换地址）；
    # 切版文案出口＝「稍后再试」＋保线上（不升级到管理员，沿旧口径），不入本组
    for fn in ("publishFailText", "offlineFailText"):
        start = PUB_JS_SRC.index("function " + fn)
        seg = PUB_JS_SRC[start:]
        nxt = PUB_JS_SRC.find("\n  function ", start + len(fn))   # 从函数起点向后找下一函数
        seg = seg[:nxt if nxt > 0 else len(seg)]
        assert ("重试" in seg or "再试" in seg or "换一个地址" in seg) and "管理员" in seg, \
            f"{fn} 缺处置出口（SPEC §二：报错不说人话→零容忍）"


def test_upstreams_errors_inline_at_controls():
    ids = _ids(_dom(UPS_HTML_SRC))
    for el_id in ("service-name-error", "route-path-error", "upstream-url-error",
                  "upstream-name-error", "save-error"):
        assert el_id in ids, f"上游页缺行内错误位：{el_id}"
    for fn in ("serviceNameError", "routeError", "upstreamUrlError", "upstreamNameError"):
        assert fn in UPS_JS_SRC, f"上游页缺逐控件错误函数：{fn}"
    assert "saveError" in UPS_JS_SRC, "保存失败须就近提示（saveError）"


def test_upstreams_ux_revamp_copy_and_controls():
    """2026-10-20 可用性改版：13 处文案/交互的产物级断言。

    覆盖：先存草稿文案（禁「git 权威源」）／界面无 HTTP 方法选择（固定跟随上游，
    探测后只读展示判定结论）／路由说明与覆盖警告／退役假「可复用 Upstream」／
    超时 1~30 与重试文案／发布方式单选（退役环境×实例）／片段导出退役＋原始配置折叠／
    探测 UI（2026-10-21 起含 ok_post_confirmed 四态）／
    列表底注两句／最近变更人取当前登录用户／删除草稿两段确认／上游 URL 清空与
    localhost 旁注。
    """
    html, js = UPS_HTML_SRC, UPS_JS_SRC
    ids = _ids(_dom(html))
    assert "先存草稿：写进配置文件但不对外转发，要生效请点发布" in html
    assert "git 权威源" not in html and "git 权威源" not in js
    # 2：用户界面彻底无方法选择（2026-10-20 维护者 更正）：不出现 GET/POST 手选；
    #    form.method 固定 AUTO，探测后自动落具体方法并只读展示判定结论
    assert "http-method-select" not in html, "仍残留 HTTP 方法选择控件"
    assert "METHOD_OPTIONS" not in js and "METHOD_OPTIONS" not in html
    assert "test-apply-method-btn" not in html and "applySuggestedMethod" not in js
    assert "平台将以该方法向你的上游转发请求" not in html
    assert "METHOD_AUTO" in js and "method: METHOD_AUTO" in js
    assert "test-method-note" in ids, "探测结果区缺只读方法结论位"
    assert "将使用 " in js and "转发请求" in js
    assert "无法识别上游接口方法，请检查上游地址" in js
    # AUTO 跟随上游：405＋Allow 建议方法时自动改用重测（无需用户手选）
    assert "suggestedMethod" in js and "this.test.suggestedMethod" in js
    # 3：路由说明 + 同路径覆盖警告
    assert "调用方将访问" in html and "保存将覆盖它" in html
    # 4：假可复用 Upstream 退役，名称仅登记备注
    assert "reuse-upstream-check" not in html and "存为可复用 Upstream" not in html
    assert "上游名称（仅登记备注" in html
    # 5/6：超时/重试文案与 1~30 上限
    assert "等待上游响应的最大秒数" in html
    assert 'max="30"' in html and "TIMEOUT_MAX = 30" in js
    assert "连接失败时自动重试次数（接口业务报错不重试）" in html
    # 7：发布方式单选（退役环境×实例假维度）
    assert "先测试再发布" in html and "直接发布" in html
    assert "发布后 ≤30 秒网关生效" in html
    for dead in ("scope-tuple", "scope-env", "scope-instance", "env-select", "instance-select"):
        assert dead not in html, f"假生效范围控件未退役：{dead}"
    assert "releaseMode" in js
    # 8：片段导出退役（2026-10-21 降噪裁决：发布即由服务端原样写 upstreams.yaml 真源，
    #    本机另存片段文件不在任何流程链上＝纯冗余）；「查看原始配置」保留为默认折叠的技术详情
    assert "导出本次片段" not in html and "download-yaml-btn" not in html, "导出按钮应已退役"
    assert "proxy-x.yaml" in html and "raw-config-details" in ids, "原始配置折叠区应保留"
    assert "仅本条，与线上完整配置文件不同" in html
    # 9：三态徽标与中文结论＋技术详情折叠（裸 JSON 展示退役）
    for el_id in ("test-verdict", "test-conclusion", "test-method-note", "test-tech-details"):
        assert el_id in ids, f"三态探测 UI 缺 id：{el_id}"
    assert "ok_2xx" in js and "method_mismatch" in js and "unreachable" in js
    assert "JSON.stringify(test.body)" not in html and "test-body-preview" not in html
    assert "testElapsedText" in js and "—" in js
    # 10：列表底注拆两句
    assert "发布后 ≤30 秒网关生效。" in html and "变更已写盘，历史版本可回溯。" in html
    # 11：最近变更人取当前登录用户（非写死常量）
    assert "currentOperator()" in js and "AdminGate.readOidcToken" in js
    # 12：草稿删除（两段确认）
    assert 'data-action="delete-draft"' in html and "draft-delete-modal" in html
    assert "confirmDeleteDraft" in js and "apiFetch(API.remove" in js
    # 13：上游 URL 默认清空＋示例占位＋localhost 旁注
    assert "upstreamUrl: ''" in js
    assert "https://上游地址/路径" in html
    assert "填 localhost 会指向平台容器自身" in html
    # C6：发布成功提示未授权＋直达签发页（预填服务）
    assert "尚未授权给任何凭证" in html
    assert "appkeys.html?service=" in html


def test_appkeys_proxy_option_and_prefill():
    """C3/C6（2026-10-20）：签发页代理选项文案、整路由授权块、?service= 预填。

    C3：代理服务选项带「代理服务」标注；选中时隐藏字段块、显示「整路由授权」；
    C6：上游代理发布成功后跳 appkeys.html?service=<id>，签发页应自动打开并预填。
    """
    ajs = (ADMIN / "appkeys.js").read_text(encoding="utf-8")
    ahtml = (ADMIN / "appkeys.html").read_text(encoding="utf-8")
    assert "isProxyService" in ajs and "kind === 'proxy'" in ajs
    assert "' · 代理服务'" in ajs
    assert "整路由授权" in ahtml and 'id="svc-route-grant-section"' in ahtml
    assert "isProxySelected()" in ahtml and "!isProxySelected()" in ahtml
    assert "URLSearchParams(root.location.search).get('service')" in ajs
    assert "applyServicePrefill" in ajs and "this.openIssue()" in ajs


# ── 3. 字段表中文名＋列名双列（P1-10 差量）──

def test_publish_field_list_cnname_and_no_sensitive_badge():
    """向导①字段清单（SPEC §三）：中文名为主标签＋默认全选；敏感徽标零残留
    （2026-09-29 裁决：敏感概念整体移除），无「字段表即表单」标题。"""
    html = PUB_HTML_SRC
    assert "f.cnName || f.name" in html, "字段行未以中文名为主标签（缺名回落列名，如实）"
    assert "敏感" not in html and "badge-warn" not in html, "敏感徽标/样式应零残留"
    assert 'id="select-all-check"' in html, "缺全选开关（默认全选）"
    assert "toggleAllFields" in PUB_JS_SRC, "缺全选切换逻辑"
    # 文案纪律：无中生有概念「字段表即表单」不出现（SPEC §二）
    assert "字段表即表单" not in html


def test_publish_wizard_mock_has_cnname():
    """挡板数据（admin/mock/publish-wizard.json）与五步向导结构同步（卡3 B3.1 契约形）。"""
    import json
    data = json.loads((ADMIN / "mock" / "publish-wizard.json").read_text(encoding="utf-8"))
    tables = data["assets"]["tables"]
    assert len(tables) >= 1, "挡板已装载表数量不足"
    missing_cn = [t["table"] for t in tables if not t.get("tableCnName")]
    assert not missing_cn, f"挡板表缺中文名：{missing_cn}"
    for t in tables:
        assert t["versions"] and all("version" in v for v in t["versions"]), \
            f"挡板表缺版本清单：{t['table']}"
    fields = data["schema"]["fields"]
    assert fields and all(f.get("cnName") and f.get("type") for f in fields), "挡板字段缺中文名/类型"
    assert "services" in data["console"], "挡板缺服务列表（管理区驱动）"
    assert "syncStatus" in data, "挡板缺切版状态（②步回显驱动）"


# 卡2 2026-09-30：test_mock_schema_domains_has_cnname 随 admin/mock/schema-domains.json
# 删除而移除（卡5 遗留清理；字段中文名真源＝表级字典 sync/schema_*.csv，gen_openapi
# load_cn_desc 已换源，契约侧由 tests/test_gen_openapi.py 守卫）。


# ── 4. 上游页门槛提示（P1-10 差量·SPEC B4）──

def test_probe_copy_visual_qc_20261020():
    """视觉 QC（2026-10-20）：状态码/耗时只进技术详情、不可达不露异常类名、405 不提示「连不上」。

    5 处展示层修正的产物级断言：
    ① 头部行不再有「响应码 HTTP x · 耗时」；正文纯自然语言（探测通过/上游只接受 X）；
    ② 不可达主文案固定「上游不可达，请检查上游地址」，不拼接服务端 message（异常类名）；
    ③ 状态码 0 是哨兵值 → 技术详情显示「—」；
    ④ 「连不上？」提示只在真不可达（无 HTTP 响应）时出现，405 已连通不显示；
    ⑤ 签发页代理说明不与「整路由授权」块同屏近重复（由下方单独块承担）。
    """
    html, js = UPS_HTML_SRC, UPS_JS_SRC
    # ①/③：头部行不再显示响应码/耗时/错误码；技术详情收纳全部技术值
    for dead in ("test-code", "test-elapsed", "test-error-kind"):
        assert f'id="{dead}"' not in html, f"头部行仍暴露技术值：{dead}"
    assert "'探测通过，可以发布'" in js
    assert "'上游只接受 ' + t.suggestedMethod + '，建议改用 ' + t.suggestedMethod" in js
    assert "'状态码: ' + (t.code > 0 ? t.code : '—')" in js, "状态码 0 哨兵未转「—」"
    assert "'耗时: ' + this.testElapsedText()" in js and "'原始信息: '" in js
    assert "errorKind: j.errorKind || ''" in js, "probe 回包错误码未透传到技术详情"
    assert "上游返回 HTTP ' + t.code" not in js
    # ②：不可达主文案固定自然语言，不回显服务端 message
    assert "'上游不可达，请检查上游地址'" in js
    assert "t.message || t.error || '连不上上游'" not in js
    # ④：仅真不可达（无 HTTP 响应）显示「连不上？」提示
    assert "showUnreachableHint" in html and "showUnreachableHint()" in js
    assert "verdictKey(t) === 'unreachable' && !(t.code > 0)" in js
    assert "test.via === 'live-probe' && !test.passed" not in html
    # 头部徽标也不带状态码数字
    assert "响应异常 HTTP " not in js


def test_appkeys_proxy_grant_note_not_duplicated():
    """视觉 QC ⑤：选中代理服务时，下拉说明让位给下方「整路由授权」块，不再近重复。"""
    ajs = (ADMIN / "appkeys.js").read_text(encoding="utf-8")
    ahtml = (ADMIN / "appkeys.html").read_text(encoding="utf-8")
    assert "if (this.isProxySelected()) return '';" in ajs, "代理态下拉说明未让位"
    assert "该服务是上游代理：整路由授权" not in ajs, "代理说明仍与整路由授权块重复"
    assert 'id="svc-grant-note"' in ahtml and 'x-show="svcGrantNote()"' in ahtml
    assert 'id="svc-route-grant-note"' in ahtml and "按整条 /proxy/* 路由授权" in ahtml


def test_upstreams_ops_gate_note():
    ids = _ids(_dom(UPS_HTML_SRC))
    assert "ops-gate-note" in ids, "上游页缺门槛提示条 ops-gate-note"
    assert "本页为技术运维配置，业务无需操作" in UPS_HTML_SRC, "缺任务书指定提示文案"
    assert ".gate-note" in UPS_HTML_SRC


# ── 5. 自检套件与新 DOM 自洽 ──

def test_selftest_publish_assertions_aligned():
    """selftest-publish.js：断言五步向导不变式（互斥/黑话扫描/构建同步），旧断言清单退场。"""
    st = ST_PUB_JS_SRC
    for group in ("ids-all", "step2-versions", "step2-switch", "step3-route-check",
                  "step4-schema", "step5-publish", "done-t10", "services-table",
                  "offline-copy", "jargon-scan", "build-sync"):
        assert f"'{group}'" in st, f"selftest-publish.js 缺断言组：{group}"
    # ids 清单不再要求旧控件（旧 id 只出现在负向断言里）
    ids_seg = st.split("var ids = [")[1].split("];")[0]
    for dead in ("hbase-tree", "ds-switch", "key-input", "release-mode-select",
                 "scope-env", "env-select", "form-error"):
        assert dead not in ids_seg, f"ids 清单混入旧控件：{dead}"
    assert "JARGON" in st and "rowkey" in st, "缺黑话扫描规则"
    assert "服务修订号" in st, "缺 ADR-0008 术语禁令扫描"
    assert 'id="wiz-frame"' in ST_PUB_HTML_SRC, "断言页未以 iframe 挂真实发布页"


def test_selftest_upstreams_assertions_aligned():
    assert "service-name-error" in ST_UPS_JS_SRC and "upstream-url-error" in ST_UPS_JS_SRC
    assert "error-inline" in ST_UPS_JS_SRC
    assert "service-name-error" in ST_UPS_HTML_SRC and "save-error" in ST_UPS_HTML_SRC
    # 视觉 QC：自测页断言同步（主文案不含状态码、405 不提示连不上、不可达不露类名）
    for group in ("probe-qc-405", "probe-qc-unreachable"):
        assert f"'{group}'" in ST_UPS_JS_SRC, f"selftest-upstreams.js 缺 QC 断言：{group}"
    assert "showUnreachableHint" in ST_UPS_HTML_SRC


def test_js_syntax():
    # 三批（2026-10-08 修五）：versions/published 两页接壳，组件 js 一并纳入机检
    for js in (PUB_JS, UPS_JS, ST_PUB_JS, ST_UPS_JS,
               ADMIN / "versions.js", ADMIN / "published.js"):
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, f"{js.name}: {r.stderr}"


# ── 6. 构建同步与纪律 ──

def test_portal_admin_artifacts_synced():
    """portal/admin 产物含向导结构（陈旧则先重建，防漏同步）。"""
    for name, needle in (("publish.html", 'id="wizard-stepper"'),
                         ("upstreams.html", "ops-gate-note")):
        out = PORTAL_ADMIN / name
        if needle not in out.read_text(encoding="utf-8"):
            subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                           check=True, capture_output=True)
        assert needle in out.read_text(encoding="utf-8"), f"产物陈旧：{name}"
    assert 'id="step-4"' in (PORTAL_ADMIN / "publish.html").read_text(encoding="utf-8"), \
        "产物缺向导④步"


def test_admin_pages_no_external_refs():
    # 三批（2026-10-08 修五）：versions/published 接壳后同守外链红线（内网白屏）
    for name in ("publish.html", "upstreams.html", "versions.html", "published.html"):
        html = (ADMIN / name).read_text(encoding="utf-8")
        externals = [u for u in re.findall(r'(?:src|href)="(https?://[^"]+)"', html)]
        assert not externals, f"{name} 外链红线：{externals}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"[ok] {fn.__name__}")
        except Exception as exc:   # noqa: BLE001 —— 直跑模式聚合报错
            failed += 1
            import traceback
            print(f"[FAIL] {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
