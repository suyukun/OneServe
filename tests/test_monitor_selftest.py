#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-6 异常与批量监控自测页断言套件（tests/test_monitor_selftest.py）。

对齐 P-3 先例（tests/test_ops_portal.py 由 selftest-ops.html 浏览器执行；本套件做
**纯 Node 可执行部分**的镜像断言）：
  1. selftest-ops.html 状态带断言组与新口径自洽（id 清单换成 ops-comp-unknown；
     新增「无写死组件常量/顺序对齐/实测档案标注」三例）；
  2. monitor.js 纯函数在 Node 直跑得真值（与 portal 的 selftest 同源同表达式）：
     fmtRate 小基数→「样本量不足」、progressText 无 total→「待物化」、
     backlogText 无 queued→「—」（不是 0 秒）、jobStatusText/jobBadge 五态映射；
  3. monitor.js 三端点常量与页面 DOM 引用一致（防单边改动漂移）。
运行：python3 -m pytest tests/test_monitor_selftest.py（或 python3 直跑，断言一致）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
PORTAL = REPO / "portal"


def _node_eval(expr_pairs: list[tuple[str, str]]) -> dict:
    """Node 直跑 monitor.js 的纯函数：expr_pairs＝[(标签, 表达式)]，返回 {标签: 实际值}。

    以 vm 沙箱执行模块（模块内的 document 判断分支已由 typeof 守卫，Node 下不注册
    Alpine）——与门户 selftest 的浏览器断言同源同表达式，仅宿主不同。
    """
    script = "\n".join(f'  out[{json.dumps(label)}] = String({expr});' for label, expr in expr_pairs)
    code = (
        "const fs = require('fs'), vm = require('vm');\n"
        "const src = fs.readFileSync(process.argv[1], 'utf8');\n"
        "const ctx = { window: {} , self: {}, console: console };\n"
        "ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;\n"
        "vm.createContext(ctx);\n"
        "vm.runInContext(src, ctx);\n"
        "const M = ctx.Monitor; const out = {};\n"
        + script + "\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    r = subprocess.run(["node", "-e", code, str(ADMIN / "monitor.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, "Node 直跑 monitor.js 失败：" + r.stderr
    return json.loads(r.stdout)


def test_monitor_pure_helpers_real_values():
    """monitor.js 纯函数真值（与页面 selftest 同表达式）——小基数/空积压口径如实。"""
    out = _node_eval([
        ("rate_none", "M.fmtRate(null)"),
        ("rate_value", "M.fmtRate(12.5)"),
        ("progress_ok", "M.progressText({total: 100, processed: 25, progress: 25})"),
        ("progress_nototal", "M.progressText({total: 0, processed: 0, progress: null})"),
        ("backlog_none", "M.backlogText({oldestWaitSec: null})"),
        ("backlog_sec", "M.backlogText({oldestWaitSec: 45})"),
        ("backlog_min", "M.backlogText({oldestWaitSec: 125})"),
        ("status_text", "M.jobStatusText('cancelling')"),
        ("status_unknown", "M.jobStatusText('weird')"),
        ("badge_failed", "M.jobBadge('failed')"),
        ("badge_queued", "M.jobBadge('queued')"),
        ("short_id", "M.shortId('0123456789abcdef')"),
        ("page_zero", "M.pageText(0, 0, 0)"),
    ])
    assert out == {
        "rate_none": "样本量不足",
        "rate_value": "12.5%",
        "progress_ok": "25%（25/100）",
        "progress_nototal": "待物化（total 未回填）",
        "backlog_none": "—",              # 无排队＝「—」，不是「0 秒」
        "backlog_sec": "45 秒",
        "backlog_min": "2 分",
        "status_text": "取消中",
        "status_unknown": "weird",        # 未知状态如实回显原值，不编造
        "badge_failed": "badge-5xx",
        "badge_queued": "badge-warn",
        "short_id": "0123456789ab…",
        "page_zero": "共 0 条",
    }, out


def test_selftest_ops_band_assertions_aligned():
    """selftest-ops.html：状态带断言组已换新口径（未知占位＋无写死常量＋档案标注）。"""
    st = (PORTAL / "selftest-ops.html").read_text(encoding="utf-8")
    assert "ops-comp-unknown" in st and "ops-comp-audit'" not in st.split("REQUIRED_IDS")[1][:400]
    assert "OC.COMPONENTS" in st, "缺「写死组件常量已退役」断言"
    assert "COMPONENT_ORDER" in st and "COMPONENT_ARCHIVE" in st
    assert "状态带接真" in st


def test_monitor_js_endpoints_match_page_dom():
    """三端点常量与页面 DOM 的取数出处一致（防单边改动漂移）。"""
    js = (ADMIN / "monitor.js").read_text(encoding="utf-8")
    html = (ADMIN / "monitor.html").read_text(encoding="utf-8")
    for path in ("/v1/admin/monitor/errors", "/v1/admin/monitor/trace",
                 "/v1/admin/monitor/batch"):
        assert path in js, "monitor.js 缺端点：" + path
        assert path in html, "monitor.html 未标注数据出处：" + path


def test_monitor_js_alpine_registration_order():
    """monitor.js 先于 alpine.min.js 加载（本仓 alpine 构建 microtask 即 start）。"""
    html = (ADMIN / "monitor.html").read_text(encoding="utf-8")
    assert html.find('src="monitor.js"') < html.find('src="../portal/assets/alpine.min.js"')


OPS_NODE_HARNESS = r'''
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');   // node -e 时首个附加参数在 argv[1]
const ctx = { console: console };
ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
ctx.Fmt = { formatCompact: v=>String(v), formatThousands: v=>String(v), formatClock: ()=>'',
            formatPercent: ()=>'', formatRelativeTime: ()=>'' };
ctx.PortalAPI = { POLL_INTERVAL_MS: 60000, ENDPOINTS: {} };
ctx.PortalComponent = function () {
  var o = { sections: {} };
  o.st = function (n) { return (this.sections[n] || {}).state; };
  o.data = function (n) { return (this.sections[n] || {}).data || {}; };
  return o;
};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const inst = ctx.OpsCockpitFactory();
function seed(status, ids) {
  inst.sections = { runtimeStatus: { state: 'ok', data: { components: (ids || [
    'ops-comp-gateway','ops-comp-service','ops-comp-redis','ops-comp-audit'
  ]).map(function (id) {
    return { id: id, name: id, tech: 't', status: status, detail: 'live-detail',
             checkedAt: 'T', probe: 'P' };
  }) } } };
}
const out = {};
seed('ok');
out.ok = inst.compStatusMeta(inst.components()[0].status).cls + '/' +
         inst.compStatusMeta(inst.components()[0].status).text;
seed('error');
out.error = inst.compStatusMeta('error').cls + '/' + inst.compStatusMeta('error').text;
seed(null);   // 后端漏 status 字段＝未知
out.unknown = inst.compStatusMeta(null).cls + '/' + inst.compStatusMeta(null).text;
seed('off');
out.off = inst.compStatusMeta('off').cls + '/' + inst.compStatusMeta('off').text;
inst.sections = { runtimeStatus: { state: 'error', data: null } };
out.unavailableLen = inst.components().length;          // 接口失败＝空（页面显未知占位）
inst.sections = { runtimeStatus: { state: 'ok', data: { todayCalls: 5 } } };
out.noFieldLen = inst.components().length;              // mock 降级无 components＝空（不伪造）
seed('ok');
out.archive = inst.components()[0].archivePerf;
out.liveDetail = inst.components()[0].detail;
out.order = inst.components().map(function (c) { return c.id; }).join(',');
console.log(JSON.stringify(out));
'''


def test_ops_status_band_real_driving():
    """真实执行 ops.js：状态带由 components[] 驱动，未知/不可用＝未知而非「正常」。"""
    r = subprocess.run(["node", "-e", OPS_NODE_HARNESS, str(PORTAL / "assets" / "ops.js")],
                       capture_output=True, text=True)
    assert r.returncode == 0, "Node 执行 ops.js 失败：" + r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] == "is-ok/正常"
    assert out["error"] == "is-error/异常"
    assert out["unknown"] == "is-unknown/未知", "未探到必须「未知」（不得回退「正常」）"
    assert out["off"] == "is-off/已关闭"
    assert out["unavailableLen"] == 0 and out["noFieldLen"] == 0,         "接口失败/无 components 时须为空，页面显未知占位（不得伪造正常）"
    assert "（100 并发档）" in out["archive"], "容量梯子实测应作副注（标非实时）"
    assert out["liveDetail"] == "live-detail", "实时明细不得被档案数据覆盖"
    assert out["order"] == "ops-comp-gateway,ops-comp-service,ops-comp-redis,ops-comp-audit"


def test_no_mock_fallback_in_monitor_path():
    """运行真值面不得降级 mock：monitor.js 与 ops.js 状态带均不得引用 mock 数据。"""
    for name in ("monitor.js",):
        code = re.sub(r"/\*.*?\*/", "", (ADMIN / name).read_text(encoding="utf-8"), flags=re.S)
        assert "mock/" not in code and "MOCK_" not in code, name + " 出现 mock 降级"
    ops = re.sub(r"/\*.*?\*/", "", (PORTAL / "assets" / "ops.js").read_text(encoding="utf-8"),
                 flags=re.S)
    assert "COMPONENTS = [" not in ops and not re.search(r"status:\s*'ok'", ops)


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
