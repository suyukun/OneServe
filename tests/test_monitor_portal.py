#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-6 异常与批量监控门户套件（tests/test_monitor_portal.py）——DOM＋组件契约＋状态带接真。

守卫口径（任务卡 SPEC §5-P1-6／§2-B7；维护者 追加：运维大盘四状态卡必须真）：
  1. 构建注册与产物：portal/admin/monitor.{html,js} 随 admin/ 同步；ops 关键区块
     id 更新为真探活与「未取到＝未知」占位；构建幂等；
  2. 运营页 DOM：异常摘要卡／检索条件（异常类/状态/追踪号/时间窗）／下钻两表／
     异常列表八元组列＋样本列／trace 详情表／批量运行视角四指标＋五态＋最近任务；
     空态文案「暂无异常」与「未接入」如实标注；只读角标；无外链；gate 四页一致；
  3. 组件契约（monitor.js）：取数直达三端点、Alpine 注册、下钻/样本/trace 交互在位、
     401 就地提示、**不做 mock 降级**（运行真值不塞演示数字）；
  4. **状态带接真（维护者 明示）**：portal/src/ops.html 四卡灯色与文字由 status 驱动，
     源码不得再出现写死的 is-ok / 「正常」；ops.js 不得再有 status:'ok' 常量；
     未知态映射为 is-unknown＋「未知」；容量梯子实测降为副注并标「非实时」；
     mock 降级不得伪造 components（演示态如实「未知」）；
  5. 侧栏入口可达（admin/monitor.html），无死链。
运行：python3 -m pytest tests/test_monitor_portal.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
PORTAL = REPO / "portal"
PORTAL_ADMIN = PORTAL / "admin"
MON_HTML = ADMIN / "monitor.html"
MON_JS = ADMIN / "monitor.js"
OPS_SRC = PORTAL / "src" / "ops.html"
OPS_JS = PORTAL / "assets" / "ops.js"

if not (PORTAL_ADMIN / "monitor.html").is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)


class _TagText(HTMLParser):
    """极简 DOM 断言器（test_monitor 同款，无第三方依赖）。"""

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


def _has_text(dom: _TagText, needle: str) -> bool:
    return any(needle in t for t in dom.texts)


MON_HTML_SRC = MON_HTML.read_text(encoding="utf-8")
MON_JS_SRC = MON_JS.read_text(encoding="utf-8")
OPS_SRC_SRC = OPS_SRC.read_text(encoding="utf-8")
OPS_JS_SRC = OPS_JS.read_text(encoding="utf-8")


# ── 1. 构建与产物 ──

def test_build_artifact_synced():
    out_html = PORTAL_ADMIN / "monitor.html"
    out_js = PORTAL_ADMIN / "monitor.js"
    assert out_html.is_file() and out_js.is_file(), "portal/admin/monitor.* 缺失（先跑 build_portal）"
    assert "monitorAdmin" in out_html.read_text(encoding="utf-8")
    # 路径改写与 admin 同步口径一致（../portal/ → ../）
    assert "../assets/alpine.min.js" in out_html.read_text(encoding="utf-8")
    assert "/v1/admin/monitor/errors" in out_js.read_text(encoding="utf-8")


def test_ops_required_ids_updated_for_real_probe():
    sys.path.insert(0, str(REPO / "scripts"))
    import build_portal as bp
    assert "ops-comp-unknown" in bp.PAGES["ops"]["required_ids"],         "ops 关键区块须含「未取到探活结果」占位（如实未知，不回退正常）"


def test_sidebar_entry_no_dead_link():
    html = (PORTAL / "index.html").read_text(encoding="utf-8")
    sidebar = html.split('id="app-sidebar"')[1].split("</aside>")[0]
    assert 'href="admin/monitor.html"' in sidebar, "侧栏缺异常监控入口"
    assert (PORTAL / "admin" / "monitor.html").is_file(), "侧栏死链：monitor.html 目标不存在"


def test_build_idempotent():
    before = (PORTAL / "ops.html").read_bytes()
    b2 = (PORTAL_ADMIN / "monitor.html").read_bytes()
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)
    assert (PORTAL / "ops.html").read_bytes() == before, "构建非幂等（ops.html）"
    assert (PORTAL_ADMIN / "monitor.html").read_bytes() == b2, "构建非幂等（monitor.html）"


# ── 2. 运营页 DOM ──

def test_required_blocks_present():
    ids = {a.get("id") for _, a in _dom(MON_HTML_SRC).tags if a.get("id")}
    for block_id in ("monitor-summary", "monitor-toolbar-card", "monitor-drill",
                     "monitor-errors", "monitor-trace", "monitor-batch",
                     "mon-caller", "mon-endpoint", "mon-error-class", "mon-status",
                     "mon-trace", "mon-limit", "mon-submit", "mon-reset",
                     "monitor-table", "monitor-drill-caller", "monitor-drill-api",
                     "monitor-trace-table", "monitor-batch-counts", "monitor-batch-recent",
                     "monitor-not-connected", "monitor-trace-submit", "mon-batch-refresh"):
        assert block_id in ids, "缺关键区块 id：" + block_id


def test_admin_shell_elements_identical_to_other_admin_pages():
    """四页管理壳一致：面包屑等公共区块 id 对齐、脚本加载序不变。

    2026-09-23：原测试名 ..._gate_elements_identical_...，断言「令牌栏/只读 banner/
    错误行 id 四页对齐」+「含 password 型令牌框」。管理令牌机制已整体拆除，这些
    区块（admin-token-input/-save、gate-banner[-text]、token-error）已随机制从四页删除，
    故相关断言下线；保留真正跨页一致的公共壳契约（admin-crumb）与脚本加载序。
    """
    ids = {a.get("id") for _, a in _dom(MON_HTML_SRC).tags if a.get("id")}
    for el_id in ("admin-crumb",):
        assert el_id in ids, "缺管理壳公共区块 id：" + el_id
    assert 'type="password"' not in MON_HTML_SRC, "令牌输入框已随机制拆除，不该残留"
    pos_gate = MON_HTML_SRC.find('src="admin-gate.js"')
    pos_js = MON_HTML_SRC.find('src="monitor.js"')
    pos_alpine = MON_HTML_SRC.find('src="../portal/assets/alpine.min.js"')
    assert 0 <= pos_gate < pos_js < pos_alpine, "脚本加载序须为 admin-gate → monitor → alpine"


def test_dom_error_list_columns_and_samples():
    dom = _dom(MON_HTML_SRC)
    for col in ("时间", "调用方", "状态", "耗时", "参数摘要", "响应摘要", "追踪号", "样本"):
        assert col in dom.texts, "异常列表缺列：" + col
    assert "sha256" in MON_HTML_SRC and "不留存参数明文" in MON_HTML_SRC
    assert "toggleSample(" in MON_HTML_SRC and "isSampleOpen(" in MON_HTML_SRC
    assert "错误样本" in MON_HTML_SRC, "样本区标题缺失"


def test_dom_summary_and_drill():
    dom = _dom(MON_HTML_SRC)
    for label in ("异常摘要", "窗口内调用总量", "异常调用数", "异常率", "按调用方 / 按 API 下钻"):
        assert _has_text(dom, label), "缺摘要/下钻文案：" + label
    assert "drillCaller(" in MON_HTML_SRC and "drillApi(" in MON_HTML_SRC
    assert "按前缀 " in MON_HTML_SRC, "下钻前缀口径未就地标注"


def test_dom_trace_detail_and_batch_view():
    dom = _dom(MON_HTML_SRC)
    for label in ("追踪号详情", "批量运行视角", "队列深度", "执行中", "失败任务",
                  "积压（最长等待）", "五态任务计数", "最近任务"):
        assert _has_text(dom, label), "缺区块文案：" + label
    for status_label in ("排队", "取消中", "成功", "失败", "已取消"):
        assert status_label in MON_HTML_SRC, "五态文案缺：" + status_label


def test_dom_honest_empty_and_not_connected():
    dom = _dom(MON_HTML_SRC)
    blob = MON_HTML_SRC + "".join(dom.texts)
    # 空态文案落在 x-text 表达式（非静态可见文本）——按源文案断言
    assert "暂无异常" in MON_HTML_SRC and "不填示例数据" in MON_HTML_SRC, "异常空态须如实"
    assert "如实空白，不编造链路" in blob, "trace 空态须如实"
    assert "未接入" in blob and "不造数" in blob, "未接入项须如实标注（告警送达归后续阶段）"
    assert "不做告警通道" in blob, "本卡边界（只做可看）须就地说明"


def test_dom_readonly_badge_and_no_external():
    dom = _dom(MON_HTML_SRC)
    assert "只读" in dom.texts, "缺只读角标"
    externals = re.findall(r'(?:src|href)="(https?://[^"]+)"', MON_HTML_SRC)
    assert not externals, "外链红线：" + str(externals)


# ── 3. 组件契约（monitor.js） ──

def test_monitor_js_contract():
    assert "/v1/admin/monitor/errors" in MON_JS_SRC
    assert "/v1/admin/monitor/trace" in MON_JS_SRC
    assert "/v1/admin/monitor/batch" in MON_JS_SRC
    assert "monitorAdmin" in MON_JS_SRC and "alpine:init" in MON_JS_SRC
    for fn in ("drillCaller", "drillApi", "useTrace", "lookupTrace", "toggleSample",
               "loadBatch", "activeConds", "clearCond"):
        assert fn + ":" in MON_JS_SRC, "缺交互函数：" + fn
    # 2026-09-23：原断言「monitor.js 须带 'X-Admin-Token' 头」+「401 令牌提示文案」。
    # 管理令牌机制已整体拆除，两条随机制下线，改为反向防回潮断言（只看代码、不误伤注释）。
    code = re.sub(r"/\*.*?\*/", "", MON_JS_SRC, flags=re.S)
    assert "X-Admin-Token" not in code, "monitor.js 不应再带令牌头（机制已拆）"
    # 不做 mock 降级：异常/任务状态是运行真值，塞演示数字只会误导值班
    assert "mock/" not in code and "MOCK_" not in code
    assert "样本量不足" in MON_JS_SRC, "小基数口径文案缺失"


def test_monitor_deep_link_and_cross_page():
    """trace 深链（?trace_id=）＋跨到合规审计页的同追踪号入口（生产相对路径 ../audit.html）。"""
    assert "URLSearchParams(location.search)" in MON_JS_SRC
    assert "q.get('trace_id')" in MON_JS_SRC
    assert "history.replaceState" in MON_JS_SRC, "trace 详情未回写深链（刷新不可达）"
    assert "'../audit.html?trace_id='" in MON_HTML_SRC, "缺跨页同追踪号入口"
    assert "monitor-trace-audit-link" in MON_HTML_SRC


def test_monitor_js_syntax():
    r = subprocess.run(["node", "--check", str(MON_JS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pure_helpers_modules():
    """纯函数挂 window.Monitor（Node 冒烟直调）：格式化/判定不依赖 DOM。"""
    for name in ("fmtRate", "progressText", "backlogText", "jobStatusText", "shortId"):
        assert name + ":" in MON_JS_SRC, "缺纯函数：" + name
    assert "root.Monitor = {" in MON_JS_SRC


# ── 4. 状态带接真（维护者 明示：四卡不得再写死「正常」） ──

def test_ops_status_band_status_driven():
    """灯色与文字由 comp.status 驱动；未探到映射「未知」（灰灯），不写死 is-ok/正常。"""
    assert "compStatusMeta(comp.status)" in OPS_SRC_SRC, "状态带灯色未由 status 驱动"
    assert "compStatusMeta(comp.status).text" in OPS_SRC_SRC, "状态带文字未由 status 驱动"
    assert 'class="ops-comp-light is-ok"' not in OPS_SRC_SRC, "状态带仍有写死的绿灯"
    assert "' · 正常'" not in OPS_SRC_SRC and "· 正常" not in OPS_SRC_SRC, "状态带仍有写死的「正常」"


def test_ops_status_band_unknown_not_ok():
    """未取到探活结果＝「未知」占位；探测不到不得显示「正常」。"""
    assert "ops-comp-unknown" in OPS_SRC_SRC
    assert "is-unknown" in OPS_SRC_SRC
    dom = _dom(OPS_SRC_SRC)
    assert _has_text(dom, "未知"), "缺「未知」文案"
    assert _has_text(dom, "不回退「正常」"), "未知口径须就地说明"
    assert "components()" in OPS_SRC_SRC


def test_ops_js_no_hardcoded_component_status():
    """ops.js 不得再有写死的组件 status:'ok' 常量（评估 F5/B4 缺陷根因）。"""
    assert "COMPONENTS = [" not in OPS_JS_SRC, "写死的 COMPONENTS 常量未删除"
    assert not re.search(r"status:\s*'ok'", OPS_JS_SRC), "ops.js 仍含写死 status: 'ok'"
    assert "COMPONENT_ORDER" in OPS_JS_SRC, "缺与后端对齐的组件顺序常量"
    assert "ops-comp-gateway" in OPS_JS_SRC and "ops-comp-audit" in OPS_JS_SRC
    # 未知态映射
    assert "'is-unknown'" in OPS_JS_SRC and "'未知'" in OPS_JS_SRC


def test_ops_js_archive_perf_marked_not_realtime():
    """容量梯子实测降为副注并显式标「非实时」（不与实时状态混排冒充）。"""
    assert "COMPONENT_ARCHIVE" in OPS_JS_SRC
    assert "实测档案（非实时）" in OPS_SRC_SRC
    assert "archivePerf" in OPS_JS_SRC and "archivePerf" in OPS_SRC_SRC


def test_mock_fallback_does_not_fake_probe():
    """演示降级（mock/runtimeStatus.json）不得伪造 components 探活结果——如实「未知」。"""
    mock = json.loads((PORTAL / "mock" / "runtimeStatus.json").read_text(encoding="utf-8"))
    assert "components" not in mock, "mock 降级伪造了探活结果（应如实显示未知）"


def test_ops_built_page_matches_source_realness():
    """构建产物与源同口径：真探活/未知占位均已在 portal/ops.html 生效。"""
    built = (PORTAL / "ops.html").read_text(encoding="utf-8")
    assert "compStatusMeta(comp.status)" in built
    assert "ops-comp-unknown" in built
    assert 'class="ops-comp-light is-ok"' not in built
    assert "实测档案（非实时）" in built


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("[ok] " + fn.__name__)
        except Exception as exc:   # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print("[FAIL] " + fn.__name__ + ": " + str(exc))
    sys.exit(1 if failed else 0)
