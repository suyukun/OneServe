#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TASK-M2-5 发布台 HBase 版套件（AC-4 发现 / AC-5 选表→配字段→真试查门禁）。

headless 口径（门槛稿 AC-5 原文）：机检以本套件（node 驱动适配层纯函数＋静态机检）
为准，浏览器 selftest-publish.html 仅作人肉复核通道，断言不依赖前端渲染。

覆盖：
  1. 数据适配层＝唯一解析后端响应处：M2-4 契约端点集中定义，publish.js 不重复字面量，
     adapter 先于 publish.js 加载（加载序硬规矩）；
  2. 发现响应归一化（AC-4）：业务域→namespace/表→列族→列，ns:table 拆分，
     键类型 usr_id/phone_sm3 与示例键就位；namespaces[]/扁平 tables[] 变体容错；
  3. 诚信线（本卡硬要求）：HBase 不可达/端点未就绪一律如实报错——normalizeError.fallback
     恒 false，且 publish.js 的发现失败分支不回落 mock 假表；
  4. 真试查门禁（AC-5）：缺示例值/缺示例键/试查不过 → 发布禁用；试查通过且签名一致才亮；
  5. 发布体 hbase 型：backend.source=hbase＋hbase{namespace,table,cf,key_type}；
  6. DOM（AC-5 人肉通道）：HBase 目录树/字段表/键类型选择/试查按钮/如实错误条在位，
     发布按钮按数据源分发门禁；Redis 存量控件与流程原样保留（向后兼容）；
  7. 构建产物同步（portal/admin 含 adapter）。

运行：python3 -m pytest tests/test_publish_hbase.py（或 python3 直跑，断言一致）。

【如实标注】M2-4（发布台后端 API）与本卡并行开发，发现/试查端点契约未定稿：
  本套件机检的是「前端适配层按假设契约正确解析/构造」＋「不可达如实报错」，
  **不是**与 M2-4 真端点的端到端联调；端到端联调待 M2-4 合入后进行。
  真 HBase 侧证据＝开发替身容器（compose.hbase.yml，非常驻）实测抓取的元数据
  fixture（tests/fixtures/hbase_discovery.real.json，若存在则一并断言可归一化）。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
PORTAL_ADMIN = REPO / "portal" / "admin"
ADAPTER = ADMIN / "publish-hbase-adapter.js"
PUB_JS = ADMIN / "publish.js"
PUB_HTML = ADMIN / "publish.html"
ST_PUB_JS = ADMIN / "selftest-publish.js"
ST_PUB_HTML = ADMIN / "selftest-publish.html"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_FIXTURE = FIXTURES / "hbase_discovery.real.json"

HAS_NODE = shutil.which("node") is not None


def _node(script: str) -> str:
    """在 node 中执行脚本（已加载适配层），返回 stdout；非 0 即失败。"""
    if not HAS_NODE:
        raise AssertionError("需要 node 运行适配层 headless 断言")
    full = "require(" + json.dumps(str(ADAPTER)) + ");\n" + script
    r = subprocess.run(["node", "-e", full], capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return r.stdout.strip()


def _node_json(script: str):
    return json.loads(_node(script))


def _code_only(js: str) -> str:
    """剥掉 /* … */ 块注释与 // 行注释（机检只看可执行代码，注释里的端点/文件名不算重复）。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)//.*$", "", js)


# ---------- 1. 适配层＝唯一解析点 ＋ 加载序 ----------

def test_adapter_is_single_parse_point():
    """契约端点只在适配层定义；publish.js 经 CONTRACT 对象引用，不重复字面量。

    卡3（流程②）：表源/字段源换卡2 装载域（loadAssets/loadSchema）；旧 discovery/test/
    load 常量保留（NC-5 端点不删）但发布页链路不再引用。"""
    a = ADAPTER.read_text(encoding="utf-8")
    for key, ep in (("loadAssets", "/v1/admin/load/assets"),
                    ("loadSchema", "/v1/admin/load/schema"),
                    ("discovery", "/v1/admin/publish/hbase/discovery"),
                    ("test", "/v1/admin/publish/hbase/test"),
                    ("selftest", "/v1/admin/publish/hbase/selftest"),
                    ("load", "/v1/admin/publish/hbase/load"),
                    ("consoleView", "/v1/admin/publish"),
                    ("syncStatus", "/v1/admin/publish/sync/status"),
                    ("syncSwitch", "/v1/admin/publish/sync/switch"),
                    ("syncRollback", "/v1/admin/publish/sync/rollback")):
        assert f"{key}: '{ep}'" in a, f"适配层 CONTRACT 未定义端点：{key}"
    pub = _code_only(PUB_JS.read_text(encoding="utf-8"))
    for ep in ("/v1/admin/load/assets", "/v1/admin/load/schema",
               "/v1/admin/publish/hbase/discovery", "/v1/admin/publish/hbase/test",
               "/v1/admin/publish/hbase/load", "/v1/admin/publish/sync/status",
               "/v1/admin/publish/sync/switch", "/v1/admin/publish/sync/rollback"):
        assert ep not in pub, f"publish.js 代码重复了契约端点字面量（应集中在适配层）：{ep}"
    # 发布链路引用面（卡3）：assets/schema/publish/consoleView/switch/rollback；
    # 旧 discovery/test/load 引用＝迁移清单违规（AC-2 grep 口径）。
    for ref in ("HB.CONTRACT.loadAssets", "HB.loadSchemaUrl", "HB.CONTRACT.publish",
                "HB.CONTRACT.consoleView", "HB.CONTRACT.syncSwitch", "HB.CONTRACT.syncRollback"):
        assert ref in pub, f"publish.js 未引用适配层端点：{ref}"
    # 旧端点引用＝迁移清单违规（AC-2 grep 口径）；load 需词边界（loadAssets/loadSchema 合法）
    for dead in ("HB.CONTRACT.discovery", "HB.CONTRACT.test"):
        assert dead not in pub, f"发布页链路不得再引用旧端点（迁移清单）：{dead}"
    assert not re.search(r"HB\.CONTRACT\.load(?!Assets|Schema)", pub), \
        "发布页链路不得再引用旧装载端点（迁移清单；loadAssets/loadSchema 除外）"
    assert "O1HBase" in pub, "publish.js 未引用适配层"
    # 表级中文名/描述契约（B5：卡片标题与服务名自动建议依赖）
    for needle in ("tableCnName", "tableDesc"):
        assert needle in a, f"适配层未归一化表级元数据：{needle}"


def test_adapter_loads_before_publish_js():
    """加载序硬规矩：适配层须先于 publish.js（组件工厂引用 O1HBase）。

    发布台重写：selftest 页改为 iframe 挂真实发布页（断言页零探针 DOM 拷贝）。"""
    html = PUB_HTML.read_text(encoding="utf-8")
    pos_a = html.find('src="publish-hbase-adapter.js"')
    pos_p = html.find('src="publish.js"')
    assert pos_a >= 0, "publish.html 未加载适配层"
    assert pos_p > pos_a, "publish.html 适配层须在 publish.js 之前"
    st = ST_PUB_HTML.read_text(encoding="utf-8")
    assert 'id="wiz-frame"' in st and 'src="publish.html"' in st, \
        "selftest-publish.html 须以 iframe 挂真实发布页（wiz-frame）"
    assert 'src="publish.js"' not in st, "断言页不应自带组件脚本（应经 iframe 走真实页加载序）"


def test_js_syntax():
    for js in (ADAPTER, PUB_JS, ST_PUB_JS):
        r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, f"{js.name}: {r.stderr}"


# ---------- 2. 发现响应归一化（AC-4） ----------

def test_normalize_discovery_domains_tables_families():
    """业务域→namespace/表→列族→列；ns:table 拆分；键类型/示例键就位（不编造 usr_id 键）。"""
    out = _node_json("""
var v = O1HBase.normalizeDiscovery({
  checkedAt: '2026-09-23T10:00:00+08:00',
  dualSource: { status: 'ok', message: 'meta 与字典一致' },
  domains: [{ id: 'marketing', name: '营销域', tables: [{
    namespace: 'demo_src', table: 'tags_wide',
    keyTypes: ['usr_id', 'phone_sm3'],
    keySamples: { phone_sm3: 'a'.repeat(64) },
    families: [{ name: 'cf', columns: [
      { name: 'ac_active_sts_01', cnName: '行为活跃度字段001', type: 'tinyint', sensitive: '内部', sample: '1' },
      { name: 'if_acct_fct_24', cnName: '账户事实字段024', type: 'varchar', sensitive: '敏感' }
    ] }]
  }] }]
});
var t = v.domains[0].tables[0];
console.log(JSON.stringify({
  ok: v.ok, domains: v.domains.length, path: t.path, ns: t.namespace, table: t.table,
  cfCount: t.cfCount, columnCount: t.columnCount, keyTypes: t.keyTypes,
  sample: O1HBase.sampleKeyFor(t, 'phone_sm3'),
  usrSample: O1HBase.sampleKeyFor(t, 'usr_id'),
  dual: v.dualSource.status,
  col: O1HBase.flattenFields(t)[0]
}));
""")
    assert out["ok"] is True and out["domains"] == 1
    assert out["path"] == "demo_src:tags_wide"
    assert out["ns"] == "demo_src" and out["table"] == "tags_wide"
    assert out["cfCount"] == 1 and out["columnCount"] == 2
    assert out["keyTypes"] == ["usr_id", "phone_sm3"]
    assert re.fullmatch(r"[0-9a-f]{64}", out["sample"]), "phone_sm3 示例键未被识别"
    assert out["usrSample"] == "", "usr_id 无示例键时不得编造"
    assert out["dual"] == "ok"
    assert out["col"]["name"] == "ac_active_sts_01" and out["col"]["cnName"] == "行为活跃度字段001"
    assert "sensitive" not in out["col"], "适配层输出不应再含 sensitive 键（显示用途透传已删）"


def test_normalize_discovery_variants_tolerated():
    """容错：namespaces[]（ns:table 拆分）与扁平 tables[]（按业务域分组）。"""
    out = _node_json("""
var v2 = O1HBase.normalizeDiscovery({ namespaces: [{ name: 'default', tables: [
  { table: 'oneserve_demo', families: [{ name: 'cf', columns: ['a', 'b'] }] }] }] });
var v3 = O1HBase.normalizeDiscovery({ tables: [
  { namespace: 'rec', table: 't1', businessDomain: '营销域', families: [{ name: 'cf', columns: ['x'] }] },
  { namespace: 'rec', table: 't2', businessDomain: '营销域', families: [] }] });
console.log(JSON.stringify({
  v2path: v2.domains[0].tables[0].path,
  v3domains: v3.domains.length, v3name: v3.domains[0].name, v3tables: v3.domains[0].tables.length
}));
""")
    assert out["v2path"] == "default:oneserve_demo"
    assert out["v3domains"] == 1 and out["v3name"] == "营销域" and out["v3tables"] == 2


def test_normalize_discovery_dual_source_mismatch():
    """双源校验不一致（AC-4 拦截）：如实标 mismatch＋差异条目。"""
    out = _node_json("""
var v = O1HBase.normalizeDiscovery({ dualSource: { status: 'mismatch',
  mismatches: [{ field: 'ac_x', reason: 'HBase 有而字典无' }] }, domains: [] });
console.log(JSON.stringify({ status: v.dualSource.status, n: v.dualSource.mismatches.length,
  first: v.dualSource.mismatches[0] }));
""")
    assert out["status"] == "mismatch" and out["n"] == 1
    assert "ac_x" in out["first"]


# ---------- 3. 诚信线：不可达如实报错，不回落 mock ----------

def test_normalize_error_is_honest_no_fallback():
    """503/超时/404 归一为机读码＋人话，fallback 恒 false（不回落演示数据）。"""
    out = _node_json("""
var o = { '503': O1HBase.normalizeError({ status: 503, message: 'HBase Thrift 连接被拒' }),
          '404': O1HBase.normalizeError({ status: 404 }),
          'net': O1HBase.normalizeError({ message: 'Failed to fetch' }),
          '401': O1HBase.normalizeError({ status: 401, error: 'unauthorized' }),
          'mm': O1HBase.normalizeError({ status: 409, error: 'HBASE_SCHEMA_MISMATCH' }) };
Object.keys(o).forEach(function (k) { if (o[k].fallback) throw new Error(k + ' fallback 非 false'); });
console.log(JSON.stringify(o));
""")
    assert out["503"]["code"] == "HBASE_UNREACHABLE" and out["503"]["hint"]
    assert out["net"]["code"] == "NETWORK"
    assert out["404"]["code"] == "NOT_FOUND"
    # 发布台重写（SPEC §二）：错误标题/提示＝人话（出了什么事），无内部代号
    assert out["401"]["title"] == "没有操作权限"
    assert "登记信息" in out["mm"]["hint"], "结构变化提示须说人话并给出口"
    for k, v in out.items():
        assert v["fallback"] is False, f"{k} fallback 应恒 false"
        assert not re.search(r"O-\d+|M2-\d+|AC-\d+|dual-source|rowkey", v["title"] + v["hint"]), \
            f"{k} 错误文案含内部代号"


def test_publish_js_hbase_failure_does_not_fall_back_to_mock():
    """诚信线（卡3）：assets/schema 读取失败＝如实错误条＋重试；页面零 mock 逻辑。

    挡板（admin/mock/publish-wizard.json）只由 scripts/m25_selftest_browser.mjs 以
    同源 API 伺服——页面自身不得读取任何 mock 文件冒充真实资产。"""
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    assert "mock/" not in js, "publish.js 不得出现 mock 文件路径（诚信线：不回落示例数据）"
    start = js.index("async loadAssets() {")
    end = js.index("pickedTableObj()", start)
    body = js[start:end]
    assert "this.assetsError = {" in body, "失败分支未置如实错误对象"
    assert "assetsError" in PUB_HTML.read_text(encoding="utf-8"), "页面缺资产读取失败错误条"
    # 失败态文案口径：出了什么事＋你该干什么
    assert "暂时读不到已装载资产" in body, "失败标题未用人话句式"
    assert "请稍后重试" in body and "联系平台管理员" in body, "失败未给重试/求助出口"
    assert 'id="assets-retry-btn"' in PUB_HTML.read_text(encoding="utf-8"), "缺重试按钮"
    # 空态：零已装载表＝引导数据装载页，不回落示例（AC-3）
    html = PUB_HTML.read_text(encoding="utf-8")
    assert 'id="assets-empty"' in html and 'id="go-load-page"' in html \
        and 'href="load.html"' in html, "空态缺「去数据装载」引导"


# ---------- 4. 真试查门禁（AC-5） ----------

def test_dom_wizard_steps_and_gate():
    """卡3（流程②）：五步向导 DOM 锚点＋单一发布门禁＋完成页 T10 引导要素。"""
    html = PUB_HTML.read_text(encoding="utf-8")
    for el_id in ("wizard-stepper", "step-1", "step-2", "step-3", "step-4", "step-5", "step-done",
                  "table-cards", "assets-empty", "go-load-page",
                  "live-switch-row", "switch-btn", "switched-note", "advanced-details",
                  "rollback-btn", "next-1", "next-2", "next-3", "next-4",
                  "svc-name-input", "route-input", "route-error",
                  "field-list", "select-all-check", "checked-count", "schema-error",
                  "publish-btn", "publish-error", "prev-5",
                  "done-name", "done-route", "curl-block", "done-data-version",
                  "auth-guide", "go-credentials", "go-grants"):
        assert f'id="{el_id}"' in html, f"向导缺区块 id：{el_id}"
    # P3 二批（拆页 2026-10-08）：已发布服务区块随拆分迁至 published.html（发布页只留向导＋跳转链接）
    pubd = (REPO / "portal" / "admin" / "published.html").read_text(encoding="utf-8")
    for el_id in ("published-section", "published-table", "published-tbody", "offline-modal"):
        assert f'id="{el_id}"' in pubd, f"已发布页缺区块 id：{el_id}"
    flat = html.replace("\n", " ")
    # 发布按钮恒经单一门禁入口 canPublish()（⑤步）
    assert re.search(r'id="publish-btn"[^>]*:disabled="!canPublish\(\)"', flat), \
        "发布按钮未绑单一门禁入口 canPublish()"
    # 步进门禁：②未选版本/④零字段不可前进（一步一屏的硬闸）
    assert ':disabled="!pickedVersion"' in flat, "② 下一步未按已选版本门禁"
    assert ':disabled="checkedCount() === 0"' in flat, "④ 下一步未按字段数门禁"
    # 旧四步件彻底下线（迁移清单）：验证/加载/覆盖重灌控件不得复现
    for dead in ("verify-ok", "verify-fail", "load-date", "load-btn", "load-replace",
                 "fields-summary", "credential-note"):
        assert f'id="{dead}"' not in html, f"旧向导控件未下线：{dead}"
    assert "覆盖同日版本" not in html, "覆盖重灌开关不得出现在发布页（卡2 NC-2）"
    # 完成页要素：curl 带 apikey 头（SQ-11）＋T10 三段引导（SQ-07 顺序：登记→签发→403）
    assert "apikey" in html, "curl 样例缺 apikey 头"
    assert "授权登记（先做）" in html and "签发调用凭证（后做）" in html, "T10 引导缺先登记后签发顺序"
    assert "403" in html and 'href="appkeys.html"' in html, "T10 缺凭证/403 提示"
    assert "服务修订号" not in html, "ADR-0008 术语禁令：页面不得出现服务修订号字样"
    # 服务表：数据版本列（tabver）＋下线 E 卡文案（P3 二批拆页后归 published.html）
    assert "数据版本" in pubd, "已发布服务表缺数据版本列头"
    assert "数据保留，可随时重新发布" in pubd, "已发布页下线弹窗缺 E 卡文案"


def test_redis_branch_ui_removed():
    """发布台重写（SPEC §五.1，维护者 批准）：Redis 存量分支 UI 整体删除，只留一条路。

    后端存量流程不动（news_tag_query 仍依赖）；本断言只针对前端入口下线。"""
    html = PUB_HTML.read_text(encoding="utf-8")
    for dead in ("ds-switch", "ds-kind-hbase", "ds-kind-redis", "hbase-tree", "key-banner",
                 "key-input", "key-type-select", "field-table", "scenario-select",
                 "test-btn", "test-result-box", "scope-tuple", "release-mode-select",
                 "trace-bar", "diff-modal", "version-section"):
        assert f'id="{dead}"' not in html, f"Redis 存量/旧流程控件未删除：{dead}"
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    for dead in ("switchDs", "redisCanPublish", "runTestNow", "RELEASE_MODES", "releaseMode",
                 "schema-domains", "hbaseKeyType"):
        assert dead not in js, f"publish.js 残留旧分支逻辑：{dead}"
    assert "dual-source" not in html and "dual-source" not in js, "页面残留内部术语"


def test_wizard_step_gates():
    """向导门禁（卡3 五步）：canPublish 单一入口守 名称/地址/字段/版本；
    换表/换版本作废后续结论；④进入即取 schema。"""
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    for fn in ("canPublish", "publishNow", "pickTable", "gotoStep", "ensureSchema", "switchNow"):
        assert fn in js, f"缺向导方法：{fn}"
    assert "WIZ.routeValid(this.route)" in js, "canPublish 未校验地址格式"
    assert "this.checkedCount() > 0" in js, "canPublish 未守住至少一个字段"
    assert "String(this.svcName || '').trim() !== ''" in js, "canPublish 未守住服务名非空"
    assert "!!this.pickedVersion" in js, "canPublish 未守住已选数据版本"
    # ④进入即取 schema（字段源＝表级元数据，B3.1）；schema 随版本走
    assert "if (n === 4) this.ensureSchema()" in js, "进入④未自动取 schema"
    # 换表作废：后续步骤结论清空
    seg = js[js.index("pickTable(t) {"):]
    seg = seg[:seg.index("pickVersion(v)")]
    for reset in ("schemaFields = []", "done = false", "publishError = ''"):
        assert reset in seg, f"换表未作废后续结论：{reset}"
    # 换版本作废：字段面结论清空（schema 随版本走）
    seg2 = js[js.index("pickVersion(v) {"):]
    seg2 = seg2[:seg2.index("gotoStep(n)")]
    assert "schemaFields = []" in seg2, "换版本未作废字段结论"


def test_load_schema_url_carries_table_and_version():
    """P3 回归（2026-10-08 卡面事故）：④步字段元数据 URL 须带真表名/版本。

    事故链：loadSchemaUrl 把字符串表名误经 syncTable 表单包装——syncTable 期望
    {table:{table:…}} 嵌套对象，字符串被 obj() 洗成 {} → ?table=&version=… →
    后端按缺参回 400「缺少 table/version 查询参数」被裸报给用户，而装载链
    （write_space 尾部 schema→tabver）其实一切正常。"""
    out = _node_json("""
var u1 = O1HBase.loadSchemaUrl('usr_asset_tag', '20261008');
var u2 = O1HBase.loadSchemaUrl('demo_src:tags_wide', '20260930');
console.log(JSON.stringify({ u1: u1, u2: u2,
  sync: [O1HBase.syncTable({ table: { table: 'rec:t1' } }),
         O1HBase.syncTable({ table: { name: 't2' } })] }));
""")
    assert out["u1"] == "/v1/admin/load/schema?table=usr_asset_tag&version=20261008",         "字符串表名被洗空（卡面事故回归）"
    assert "table=tags_wide" in out["u2"], "ns:table 形式须剥前缀（与后端 key_domain 同口径）"
    assert "table=&" not in out["u1"] and "table=&" not in out["u2"]
    # syncTable 表单口径回归锁定：嵌套对象读 table/name，ns 前缀照剥
    assert out["sync"] == ["t1", "t2"]
    # 降级文案（P3）：错误分类给行动指引，不裸报后端 message；参数错误不甩给用户
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    seg = js[js.index("async ensureSchema()"):]
    seg = seg[:seg.index("checkedCount()")]
    for branch in ("SCHEMA_NOT_FOUND", "SCHEMA_QUERY_INVALID", "schemaNeedReload"):
        assert branch in seg, f"ensureSchema 缺分类分支/指引状态：{branch}"
    html = PUB_HTML.read_text(encoding="utf-8")
    assert 'x-show="schemaNeedReload"' in html, "「去装载页」指引须只在元数据缺失类错误显示"


def test_test_and_publish_payload_shapes():
    """试查/加载请求体（旧适配层能力，端点保留 NC-5）；发布体＝卡3 B3.2 扁平契约：
    table(裸表名)/source/fields[{name,cf,type,cnName}]/keyType/loadVersion(tabver 留痕)。"""
    out = _node_json("""
var table = { namespace: 'default', table: 'oneserve_demo', families: [{ name: 'cf', columns: [] }],
              keyTypes: ['phone_sm3'], keySamples: {} };
var t = O1HBase.buildTestPayload({ table: table, keyType: 'phone_sm3', sampleKey: 'k'.repeat(64),
  fields: [{ name: 'ac_active_sts_01', cf: 'cf', sample: 'band_C', desc: 'x' }] });
var l = O1HBase.buildLoadPayload({ table: table, replace: true });
var p = O1HBase.buildPublishPayload({ serviceName: '演示', routePath: '/v1/svc/demo',
  table: { table: 'usr_basic_info' }, keyType: 'phone_sm3', loadVersion: '20261001', cf: 'cf',
  fields: [{ name: 'gender_cd', cf: 'cf', type: 'character varying(16)', cnName: '性别代码' }] });
console.log(JSON.stringify({ t: t, l: l, p: p }));
""")
    t = out["t"]
    assert t["namespace"] == "default" and t["table"] == "oneserve_demo"
    assert t["families"] == ["cf"] and t["keyType"] == "phone_sm3"
    assert t["fields"][0]["name"] == "ac_active_sts_01" and t["sampleKey"]
    l = out["l"]
    assert l["table"] == "oneserve_demo" and l["cf"] == "cf"
    assert "columns" not in l and l["replace"] is True
    p = out["p"]
    # B3.2 扁平契约（卡3）：无 backend 嵌套、无 sample/desc；字段对象带 type/cnName
    assert "backend" not in p, "发布体不得再发 backend 嵌套（B3.2 扁平式）"
    assert p["table"] == "usr_basic_info" and p["source"] == "hbase"
    assert p["keyType"] == "phone_sm3" and p["cf"] == "cf"
    assert p["loadVersion"] == "20261001"          # tabver 留痕（不参与执行）
    assert p["fields"][0] == {"name": "gender_cd", "cf": "cf",
                              "type": "character varying(16)", "cnName": "性别代码"}
    assert "columns" not in p, "发布体不得携带装载语义的 columns（AC-8）"


def test_normalize_load_result_honest_reconcile():
    """本卡：加载响应归一如实——对账差异 0 才 ok；有差异/未确认不谎报通过。"""
    out = _node_json("""
var good = O1HBase.normalizeLoadResult({ ok: true, version: '20260923', loaded: 2000,
  elapsedMs: 291, dataAsOf: '2026-09-23',
  reconcile: { pass: true, diffCount: 0, checks: {}, diffs: [] } });
var bad = O1HBase.normalizeLoadResult({ ok: true, version: '20260923', loaded: 2000,
  reconcile: { pass: false, diffCount: 2, diffs: [{ kind: 'value_mismatch', key: 'ab…' }] } });
var missing = O1HBase.normalizeLoadResult({ ok: true, version: '20260923', loaded: 3 });
console.log(JSON.stringify({ good: good, bad: bad, missing: missing }));
""")
    assert out["good"]["reconcile"]["ok"] is True and out["good"]["loaded"] == 2000
    assert out["bad"]["reconcile"]["ok"] is False and out["bad"]["reconcile"]["diffCount"] == 2
    assert out["bad"]["reconcile"]["diffs"] and "value_mismatch" in out["bad"]["reconcile"]["diffs"][0]
    # 缺对账结果＝未确认绿（不谎报通过）
    assert out["missing"]["reconcile"]["ok"] is False


def test_key_type_carried_from_table():
    """键类型不露给用户：从流程②上下文内部带出（卡3：随表固定 phone_sm3，ADR-0001 键安全）。"""
    a = ADAPTER.read_text(encoding="utf-8")
    assert "keyTypes: ['usr_id', 'phone_sm3']" in a, "适配层键类型集合（内部带出口径）"
    html = PUB_HTML.read_text(encoding="utf-8")
    assert 'id="key-type-select"' not in html and "keyTypeLabels" not in html, \
        "键类型不得再出现在页面 UI"
    js = _code_only(PUB_JS.read_text(encoding="utf-8"))
    assert "keyType: 'phone_sm3'" in js, "键类型未在向导状态内部带出"


def test_selftest_channel_contract_and_dom():
    """AC-5 机检口径：后端 selftest API 响应归一（PASS/FAIL＋断言清单）＋页面如实不可用态。"""
    out = _node_json("""
var ok = O1HBase.normalizeSelftest({ passed: true, total: 3, assertions: [
  { id: 'a1', name: '发现列出 namespace/表', ok: true },
  { id: 'a2', name: '列族/列与字典一致', ok: true },
  { id: 'a3', name: '试查命中', passed: true, detail: 'HTTP 200' }] });
var bad = O1HBase.normalizeSelftest({ assertions: [{ id: 'x', ok: false, detail: 'HBase 不可达' }] });
var bare = O1HBase.normalizeSelftest({ total: 2, assertions: [{ id: 's1', ok: true }, { id: 's2', status: 'pass' }] });
console.log(JSON.stringify({ ok: ok, bad: bad, bare: bare }));
""")
    assert out["ok"]["ok"] is True and out["ok"]["passed"] == 3 and out["ok"]["failed"] == 0
    assert out["bad"]["ok"] is False and out["bad"]["failed"] == 1
    assert out["bare"]["ok"] is True and out["bare"]["passed"] == 2
    st_html = ST_PUB_HTML.read_text(encoding="utf-8")
    st_js = ST_PUB_JS.read_text(encoding="utf-8")
    # 发布台重写：断言页＝iframe 挂真实发布页；机检口径不变（以后端 selftest API 为准）
    for anchor in ('id="wiz-frame"', 'id="summary"', 'id="results"'):
        assert anchor in st_html, f"断言页缺运行器契约锚点：{anchor}"
    a = ADAPTER.read_text(encoding="utf-8")
    assert "selftest: '/v1/admin/publish/hbase/selftest'" in a, "适配层未定义自检端点"
    # 断言组覆盖向导不变式：四步互斥/七类扫描/构建同步
    # 卡3 五步组名（互斥＝step2-versions 内 visibleSteps 判定；旧四步组退场）
    for aid in ("'ids-all'", "'step2-versions'", "'jargon-scan'", "'build-sync'",
                "'step2-switch'", "'step4-schema'", "'step5-publish'",
                "'done-t10'", "'services-table'", "'offline-copy'"):
        assert aid in st_js, f"断言页缺向导不变式断言：{aid}"
    assert "data-selftest" in st_js and "data-selftest-done" in st_js, \
        "断言页缺驱动契约（data-selftest/data-selftest-done）"


# ---------- 5. 真 HBase 元数据 fixture（若已抓取） ----------

def test_real_hbase_fixture_normalizes():
    """真 HBase（开发替身容器实测）抓取的元数据 fixture 可归一化为非空目录树。"""
    if not REAL_FIXTURE.is_file():
        return   # 未抓取＝跳过（如实：不伪造）；抓取脚本 scripts/m25_hbase_fixture.py
    fixture = json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))
    out = _node_json("""
var v = O1HBase.normalizeDiscovery(JSON.parse(process.env.FIX));
var tables = O1HBase.allTables(v.domains);
console.log(JSON.stringify({ domains: v.domains.length, tables: tables.length,
  names: tables.map(function (t) { return t.path; }),
  cols: tables.reduce(function (n, t) { return n + t.columnCount; }, 0) }));
""".replace("process.env.FIX", json.dumps(json.dumps(fixture))))
    assert out["domains"] >= 1, "真 fixture 归一化后无业务域"
    assert out["tables"] >= 1, "真 fixture 归一化后无表"
    assert out["cols"] >= 1, "真 fixture 归一化后无列"


# ---------- 6. headless 可跑脚本与证据（AC-5「headless 可跑＝判据成立」） ----------

def test_headless_scripts_present_and_documented():
    """人肉复核通道的可跑脚本在位：浏览器断言页跑法＋真实发布页检查＋真链路 e2e＋fixture 抓取。"""
    for rel in ("scripts/m25_selftest_browser.mjs", "scripts/m25_publish_page_check.mjs",
                "scripts/m25_hbase_e2e.py", "scripts/m25_hbase_fixture.py"):
        f = REPO / rel
        assert f.is_file(), f"缺 headless 脚本：{rel}"
        assert "M2-5" in f.read_text(encoding="utf-8"), f"{rel} 未标注本卡"
    # 浏览器脚本声明「人肉复核通道」口径（机检以后端 API 断言为准）
    m = (REPO / "scripts" / "m25_selftest_browser.mjs").read_text(encoding="utf-8")
    assert "人肉复核通道" in m and "headless" in m


def test_e2e_evidence_recorded():
    """真链路 e2e 证据已落档（tests/evidence/m25-hbase-e2e.txt），含真 HBase 元数据与试查命中。"""
    ev = REPO / "tests" / "evidence" / "m25-hbase-e2e.txt"
    assert ev.is_file(), "缺真链路 e2e 证据（bash scripts/m25_hbase_e2e.py）"
    text = ev.read_text(encoding="utf-8")
    assert "真 HBase 点查命中" in text, "证据缺真 HBase 点查命中"
    assert "适配层归一化" in text, "证据缺适配层归一化"
    assert "e2e 全绿" in text, "证据未标全绿"


def test_real_meta_fixture_captured_from_container():
    """真 meta fixture 证据：HBase meta 只到 namespace/表/列族（列归字段字典 CSV，AC-4 双源）。"""
    meta = FIXTURES / "hbase_meta.real.json"
    if not meta.is_file():
        return
    data = json.loads(meta.read_text(encoding="utf-8"))
    assert data["host"] and data["port"], "meta fixture 缺连接地址（走 HBASE_HOST/HBASE_PORT）"
    assert data["tables"] and all("families" in t and "namespace" in t for t in data["tables"])
    # 如实标注：HBase meta 不含列（qualifier）——AC-4 的「列」须来自字段字典 CSV
    assert all("columns" not in t and "qualifiers" not in t for t in data["tables"]), \
        "HBase meta fixture 出现列级信息（与实测结论矛盾，请复核）"


# ---------- 7. 构建产物同步 ----------

def test_portal_artifacts_rebuilt():
    """portal/admin 产物含适配层与四步向导（陈旧则先重建，防漏同步）。"""
    if not (PORTAL_ADMIN / "publish-hbase-adapter.js").is_file() or \
       "publishWizard" not in (PORTAL_ADMIN / "publish.js").read_text(encoding="utf-8"):
        subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                       check=True, capture_output=True)
    assert (PORTAL_ADMIN / "publish-hbase-adapter.js").is_file(), "产物缺适配层"
    assert "publishWizard" in (PORTAL_ADMIN / "publish.js").read_text(encoding="utf-8"), \
        "产物 publish.js 缺向导组件"
    built = (PORTAL_ADMIN / "publish.html").read_text(encoding="utf-8")
    assert 'id="step-1"' in built and 'id="wizard-stepper"' in built, "产物缺向导容器"
    assert 'id="step-4"' in built, "产物缺④步"
    # P3 二批（拆页）：服务管理区迁至 portal/admin/published.html
    assert 'id="published-section"' in (PORTAL_ADMIN / "published.html").read_text(encoding="utf-8"), \
        "产物缺已发布服务页（published.html）"
    assert (PORTAL_ADMIN / "selftest-publish.js").is_file(), "产物缺断言页脚本"


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
