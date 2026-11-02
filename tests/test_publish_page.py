#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡3 套件①：服务发布页（流程②五步）构建产物与纯函数机检（AC-1/3/4/6/10/11/12/13）。

判据映射（开工门槛-卡3 §A3）：
  AC-1  五步向导（①选表→②选版本→③定服务→④选字段→⑤发布）＋进度 n/5；
  AC-3  assets 空态→引导数据装载页（不显示示例/假表）；
  AC-4  ②步默认选中线上版本（live:true；无线上取最新＋「尚未设为线上」标注）；
  AC-6  slug 机检（真源 route-naming.js；违规＝红框＋人话＋发布禁用）；
  AC-10 完成页 T10 三段引导（SQ-07 顺序：先授权登记→后签发凭证→403 提示）；
  AC-11 curl 样例带 apikey 头（SQ-11）且零「服务修订号」字样（ADR-0008）；
  AC-12 服务表「数据版本（tabver）」列，不渲染服务修订号；
  AC-13 下线确认/结果含「数据保留，可随时重新发布」（交接卡 E 卡）。

产物缺失先补构建（同 test_admin_shell 口径）；JS 纯函数经 node 直调（沿 test_publish_hbase）。
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
PUB_HTML_SRC = ADMIN / "publish.html"
PUB_JS = ADMIN / "publish.js"
ADAPTER = ADMIN / "publish-hbase-adapter.js"
OUT_PAGE = REPO / "portal" / "admin" / "publish.html"
# P3 二批（维护者 六条③）：已发布服务拆独立页 published.html（＋published.js）
PUBD_HTML_SRC = ADMIN / "published.html"
PUBD_JS = ADMIN / "published.js"
OUT_PUBD = REPO / "portal" / "admin" / "published.html"

if not OUT_PAGE.is_file() or not OUT_PUBD.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

HAS_NODE = shutil.which("node") is not None


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_json(script: str):
    """node 直调向导纯函数（WIZ）：须先载 route-naming（机检真源）→adapter（O1HBase）
    →publish.js（WIZ 组件纯函数）——与页面加载序同构（硬规矩）。

    node/CommonJS 细节：route-naming.js 把 ROUTE_NAMING 挂在模块 this（＝exports），
    浏览器里同走 window 天然共享——此处显式提升到 globalThis 供 publish.js 读到。
    """
    assert HAS_NODE, "需要 node 运行向导纯函数 headless 断言"
    pre = ("var __rn = require(" + json.dumps(str(ADMIN / "route-naming.js")) + ");\n"
           "globalThis.ROUTE_NAMING = __rn.ROUTE_NAMING || __rn;\n")
    pre += "".join("require(" + json.dumps(str(p)) + ");\n" for p in (ADAPTER, PUB_JS))
    r = subprocess.run(["node", "-e", pre + script], capture_output=True, text=True, cwd=str(REPO))
    assert r.returncode == 0, f"node 失败：{r.stderr}"
    return json.loads(r.stdout.strip())


# ── AC-1：五步向导 ──

def test_publish_wizard_five_steps():
    html = _read(OUT_PAGE)
    for i in range(1, 6):
        assert f'id="step-{i}"' in html, f"缺第 {i} 步区块"
    assert 'id="step-6"' not in html, "不得出现第六步"
    src = _read(PUB_HTML_SRC)
    names = re.search(r"stepNames: \[([^\]]+)\]", _read(PUB_JS))
    assert names and names.group(1).count("'") == 10, "步骤名须恰五个"
    for label in ("选表", "选版本", "定服务", "选字段", "发布"):
        assert label in names.group(1), f"步骤名缺 {label}"
    assert "'/5'" in src, "进度计数文案须为 n/5（AC-1 窄屏口径）"
    # 五步之外无游离旧步骤（自动验证/加载上线不得复现）
    assert "自动验证" not in src and "加载上线" not in src, "旧四步语义未下线"


# ── AC-3：空态引导装载 ──

def test_empty_assets_guides_to_load():
    html = _read(OUT_PAGE)
    assert 'id="assets-empty"' in html and 'id="go-load-page"' in html
    assert 'href="load.html"' in html and "数据装载" in html
    # 空态不显示任何示例表（诚信线：空＝空，不造假）
    assert "示例" not in re.sub(r"不显示任何示例|不回落示例|示例数据冒充|不以示例", "", html) \
        or 'id="assets-empty"' in html


# ── AC-4：默认线上版本（WIZ.defaultVersionOf 纯函数） ──

def test_step2_defaults_to_live_version():
    out = _node_json("""
var t1 = { table: 'a', versions: [ { version: '20260929', live: false },
                                   { version: '20260930', live: true } ] };
var t2 = { table: 'b', versions: [ { version: '20260929', live: false },
                                   { version: '20260930', live: false } ] };
var t3 = { table: 'c', versions: [] };
console.log(JSON.stringify({
  live: WIZ.defaultVersionOf(t1), nolive: WIZ.defaultVersionOf(t2),
  liveof: WIZ.liveVersionOf(t1), liveof2: WIZ.liveVersionOf(t2), empty: WIZ.defaultVersionOf(t3) }));
""")
    assert out["live"] == "20260930" and out["liveof"] == "20260930", "有线上版须默认线上"
    assert out["nolive"] == "20260930" and out["liveof2"] == "", "无线上版取最新＋live 标注空"
    assert out["empty"] == ""
    # 「尚未设为线上」标注在位（最新且非线上时如实提示）
    html = _read(OUT_PAGE)
    assert "尚未设为线上" in html and "调用方仍读线上版本" in html, "②步缺非线上版本如实提示"


# ── AC-6：slug 机检拦截 ──

def test_route_machine_check_blocks():
    out = _node_json("""
console.log(JSON.stringify({
  good: WIZ.routeValid('/v1/svc/usr-basic-info'),
  upper: WIZ.routeValid('/v1/svc/Usr-Basic'),
  under: WIZ.routeValid('/v1/svc/usr_basic_info'),
  short: WIZ.routeValid('/v1/svc/ab'),
  verb: WIZ.routeValid('/v1/svc/usr-get'),   /* R4 只查 toks[1:]：首段＝域码位（同后端口径） */
  suggest: WIZ.routeSuggest({ table: 'usr_basic_info' }),
  nameSuggest: WIZ.nameSuggest({ table: 'usr_basic_info', tableCnName: '用户基本信息' })
}));
""")
    assert out["good"] is True
    for bad in ("upper", "under", "short", "verb"):
        assert out[bad] is False, f"机检未拦截：{bad}"
    assert out["suggest"] == "/v1/svc/usr-basic-info"
    assert out["nameSuggest"] == "用户基本信息查询"
    html = _read(OUT_PAGE)
    assert 'id="route-error"' in html and "routeErrorText" in _read(PUB_JS)


# ── AC-10/AC-11：完成页 T10 引导＋curl＋术语禁令 ──

def test_done_page_guides_auth_t10():
    html = _read(OUT_PAGE)
    assert 'id="auth-guide"' in html
    # SQ-07 顺序：①授权登记（先做）→②签发凭证（后做）→③403；文案体现因果（409/不回补）
    assert "授权登记（先做）" in html and "签发调用凭证（后做）" in html
    assert "auth_admin.py register" in html and "--oa-ticket" in html
    assert "不会回补" in html and "403" in html
    assert 'id="go-credentials"' in html and 'id="go-grants"' in html


def test_done_page_no_service_revision():
    html = _read(OUT_PAGE)
    assert "服务修订号" not in html, "ADR-0008 术语禁令"
    # curl 样例带 apikey 头（SQ-11：复制即用，防 401）
    assert "apikey" in html and 'id="curl-block"' in html


# ── AC-12：服务表数据版本列（P3 二批拆页后归 published.html） ──

def test_services_table_shows_tabver_not_revision():
    pubd = _read(OUT_PUBD)
    assert "数据版本" in pubd, "列头须为「数据版本」"
    # 渲染绑定＝s.dataVersion（tabver），不得绑定服务修订号 s.version
    assert "s.dataVersion" in pubd
    assert "s.version" not in pubd, "不得渲染服务修订号（卡4 彻底清列，本卡先停渲染）"
    # 列表五列齐（名称/地址/版本/状态/字段数）＋字段数绑定
    for col in ("col-svc-name", "col-svc-route", "col-svc-version", "col-svc-status", "col-svc-fields"):
        assert f'id="{col}"' in pubd, f"已发布页缺列 {col}"
    assert "s.fields.length" in pubd, "字段数列须绑定 s.fields.length"
    # 发布页不再渲染已发布服务列表（拆页）
    publish = _read(OUT_PAGE)
    assert 'id="services-section"' not in publish and 'id="service-table"' not in publish, \
        "已发布服务列表应拆往 published.html（发布页只留向导＋跳转链接）"


# ── AC-13：下线 E 卡文案（P3 二批拆页后归 published.html） ──

def test_offline_copy_data_retained():
    pubd = _read(OUT_PUBD)
    assert "数据保留，可随时重新发布" in pubd
    assert pubd.count("数据保留，可随时重新发布") >= 3, "确认弹窗两段＋确认项均须带 E 卡文案"
    # 下线动作接既有 offline 端点（CONTRACT.offline），两段式确认弹窗在位
    assert 'id="offline-modal"' in pubd and "confirmOffline" in _read(PUBD_JS)
    assert "offlineModal.stage === 1" in pubd, "两段式确认（stage 1→2）须保留"


# ── P3 二批（2026-10-08 维护者 六条⑤）：③步键说明＝一句话＋示例；安全口径折叠块整删 ──

def test_step3_key_copy_humanized():
    html = _read(OUT_PAGE)
    js = _read(PUB_JS)
    src = _read(PUB_HTML_SRC)
    # 主文案＝维护者 原文口径：一句话（唯一标识/64 位十六进制/数据提供方）＋字段名＋示例
    m = re.search(r'id="key-type-line"[^>]*>(.*?)</p>', html, re.S)
    assert m, "缺 key-type-line 主文案块"
    line = re.sub(r"\s+", "", m.group(1))
    assert "入参key＝用户唯一标识（64位十六进制，由数据提供方提供）。字段名：key。" in line, line[:80]
    assert "a87ae39b27b7f83c2328e8bf4850a36bf1380032c252a1b2120d0d65f03d10d4" in line, "示例键须随文案直出"
    # 可复制示例键在位（只读输入框＋复制按钮；键值＝usr_basic_info 实测存在的行键）
    assert 'id="key-example-input"' in html and 'id="key-example-copy"' in html
    assert "a87ae39b27b7f83c2328e8bf4850a36bf1380032c252a1b2120d0d65f03d10d4" in js
    # 旧口径退役：SM3/明文表述与键安全折叠块零残留
    assert "SM3" not in src and "明文" not in src, "SM3/明文安全口径表述须删除"
    assert 'id="key-adr-details"' not in html and "键安全口径说明" not in html
    # 全页（源＋产物）决策记录编号零残留
    assert "ADR" not in src, "源页不得残留决策记录编号字样"
    assert "ADR" not in html, "产物页不得残留决策记录编号字样"


# ── P3 二批（2026-10-08 维护者 六条⑥）：④步三处说明块整删，功能与计数保留 ──

def test_step4_schema_fields_and_folded_cf():
    html = _read(OUT_PAGE)
    src = _read(PUB_HTML_SRC)
    # 三处说明块零残留：投影白名单解释段／存储列族折叠块／字段元数据来源段
    assert 'id="storage-details"' not in html and "存储列族" not in html, "「存储列族」块须整删"
    assert "HBase 术语" not in html, "「存储列族（HBase 术语…」块须整删"
    assert "字段元数据来源" not in html and "hbase+dict" not in html, "「字段元数据来源」段须整删"
    m = re.search(r'id="step-4".*?<div class="wizard-actions"', html, re.S)
    assert m, "缺 step-4 区块"
    assert "投影白名单（没勾的字段" not in m.group(0), "「勾选的字段＝投影白名单…」解释段须整删"
    assert "权威字段源" not in m.group(0), "字段清单来源解释段须整删"
    # 功能与计数保留：全选＋已选 X / N 个字段
    assert 'id="select-all-check"' in html and 'id="checked-count"' in html
    assert "'已选 ' + checkedCount() + ' / ' + schemaFields.length + ' 个字段'" in html
    # 计数两处（②版本行／④已选行）统一为「个字段」
    assert "v.tagCols + ' 个字段'" in src
    assert "' 个字段'" in src
    assert "样本列" not in re.sub(r"<!--.*?-->", "", src, flags=re.S), "旧「样本列」口径不得残留"


def test_step3_route_live_validation_states():
    """P3（UX B3-3）：route 实时校验三态——违规红字带原因（违例分词可定位）、合法绿勾。"""
    html = _read(OUT_PAGE)
    assert 'id="route-ok"' in html, "缺合法态绿勾行"
    assert 'id="route-error"' in html and "routeErrorText" in _read(PUB_JS)
    out = _node_json("""
console.log(JSON.stringify({
  r3: WIZ.routeViolationInfo('/v1/svc/usr-query-x'),
  r4: WIZ.routeViolationInfo('/v1/svc/usr-get-info'),
  r7v: WIZ.routeViolationInfo('/v1/svc/usr-info-v2'),
  r1len: WIZ.routeViolationInfo('/v1/svc/ab'),
  good: WIZ.routeViolationInfo('/v1/svc/usr-basic-info'),
  m3: WIZ.routeErrorText('R3', { reason: 'reserved', token: 'query' }),
  m4: WIZ.routeErrorText('R4', { reason: 'verb', token: 'get' }),
  m7: WIZ.routeErrorText('R7', { reason: 'version', token: 'v2' }),
  mlen: WIZ.routeErrorText('R1', { reason: 'length', len: 2 })
}));
""")
    assert out["r3"] == {"clause": "R3", "reason": "reserved", "token": "query"}
    assert out["r4"]["clause"] == "R4" and out["r4"]["token"] == "get"
    assert out["r7v"] == {"clause": "R7", "reason": "version", "token": "v2"}
    assert out["r1len"]["reason"] == "length" and out["r1len"]["len"] == 2
    assert out["good"]["clause"] == "", "合规地址不得报违规"
    # 红字原因落到具体分词/数字（与后端 route_naming.route_violation 人话信息同口径）
    assert "「query」" in out["m3"] and "保留词" in out["m3"]
    assert "「get」" in out["m4"]
    assert "「v2」" in out["m7"] and "版本不进路径" in out["m7"]
    assert "当前 2" in out["mlen"], "长度违例须带当前长度"


# ── P3 二批（2026-10-08 维护者 六条①）：向导副标题换 Redis 缓存口径 ──

def test_wizard_subtitle_redis_cache_copy():
    html = _read(OUT_PAGE)
    m = re.search(r'id="page-hint"[^>]*>(.*?)</p>', html, re.S)
    assert m, "缺 page-hint 副标题"
    hint = re.sub(r"\s+", "", m.group(1))
    assert hint == "把已装载进入Redis缓存的数据表发布成对外可调用的查询服务。", hint
    assert "（流程②）" not in m.group(1), "副标题不得再带「（流程②）」"


# ── P3 二批（维护者 六条④）：定服务步副文案＝直接给要求与示例 ──

def test_step3_name_route_copy_with_examples():
    html = _read(OUT_PAGE)
    m = re.search(r'id="step-3".*?</section>', html, re.S)
    assert m, "缺 step-3 区块"
    step3 = m.group(0)
    assert "服务名：中文业务名，如「用户资产标签查询」。访问地址：小写英文与连字符，如 /v1/svc/usr-asset-tag" in step3
    assert "服务名给人看；访问地址给调用方用" not in step3, "旧口径文案须下线"


# ── P3 二批（维护者 六条②）：③步占用检测（同 route 异名＝占用，红字给占用者＋出路） ──

def test_route_occupancy_detection():
    js = _read(PUB_JS)
    out = _node_json("""
var svcs = [
  { serviceName: '用户资产标签查询', routePath: '/v1/svc/usr-asset-tag', id: 'v1-svc-usr-asset-tag' },
  { serviceName: '资讯公告查询', routePath: '/v1/svc/news-notice', id: 'v1-svc-news-notice' }
];
console.log(JSON.stringify({
  taken: WIZ.routeOccupantOf('/v1/svc/usr-asset-tag', '另一个服务', svcs),
  sameName: WIZ.routeOccupantOf('/v1/svc/usr-asset-tag', '用户资产标签查询', svcs),
  free: WIZ.routeOccupantOf('/v1/svc/usr-basic-info', 'x', svcs),
  emptyList: WIZ.routeOccupantOf('/v1/svc/usr-asset-tag', 'x', []),
  text: WIZ.routeOccupiedText('用户资产标签查询'),
  fail: WIZ.publishFailText({ code: 'route_exists',
    message: '该地址已被服务「用户资产标签查询」占用（/v1/svc 地址全网唯一）。请换一个地址后重试' })
}));
""")
    assert out["taken"] == "用户资产标签查询", "同 route 异名须判占用并给占用者"
    assert out["sameName"] == "", "同 route 同名＝同服务覆盖更新，不报占用（与后端 find_route_occupant 同口径）"
    assert out["free"] == "" and out["emptyList"] == ""
    assert "该访问地址已被服务「用户资产标签查询」占用" in out["text"]
    assert "请先在「已发布服务」页删除/下线它，或改用其他地址" in out["text"]
    assert "该访问地址已被服务「用户资产标签查询」占用" in out["fail"], "发布失败 route_exists 须映射为占用人话"
    # 输入防抖后调后端查重（GET /v1/admin/publish 列表刷新再判定）＋占用拦下推进
    assert "onRouteInput" in js and "setTimeout" in js, "缺防抖占用查重"
    assert "HB.CONTRACT.consoleView" in js, "防抖查重须走 /v1/admin/publish 列表"
    assert "routeTaken" in js and "!this.routeTaken" in js, "占用态须拦「下一步」"
    html = _read(OUT_PAGE)
    assert '@input="onRouteInput()"' in html, "地址输入须接防抖占用检测"


# ── 三批修三②（2026-10-08 维护者 实测：幽灵版本）：向导按数据面键数过滤 ──

def test_wizard_filters_ghost_versions():
    """②步版本清单联查键数（/v1/admin/versions，SCAN 数据面）——键数 0/不在场的
    幽灵版本滤掉；全部版本皆幽灵的表（已删表）不再出现在①步选表列表。"""
    js = _read(PUB_JS)
    assert "filterGhostVersions" in js and "spaceKeyMap" in js, "缺过滤纯函数"
    assert "'/v1/admin/versions'" in js, "缺键数联查（/v1/admin/versions）"
    assert "WIZ.filterGhostVersions(norm.tables" in js, "loadAssets 须套过滤"
    out = _node_json("""
var tables = [
  { table: 'usr_basic_info', versions: [
    { version: '20261008', live: true },
    { version: '20261001', live: false } ] },        /* 20261001 已删＝幽灵 */
  { table: 'card_acct_summary', versions: [
    { version: '20260901', live: false } ] }         /* 全幽灵＝已删表 */
];
var spaces = [
  { namespace: 'usr_basic_info', version: '20261008', keys: 3 },
  { namespace: 'usr_basic_info', version: '20261001', keys: 0 }
];
var got = WIZ.filterGhostVersions(tables, spaces);
console.log(JSON.stringify({
  got: got,
  keepLive: WIZ.filterGhostVersions(
    [{ table: 't', versions: [{ version: 'v1' }] }],
    [{ namespace: 't', version: 'v1', keys: 1 }]).length,
  absentIsGhost: WIZ.filterGhostVersions(
    [{ table: 't', versions: [{ version: 'v1' }] }],
    [{ namespace: 'other', version: 'v1', keys: 9 }]).length,
  noSpaces: WIZ.filterGhostVersions(tables, []).length,
  nullSafe: WIZ.filterGhostVersions(null, null).length,
  inputUntouched: tables[0].versions.length
}));
""")
    assert len(out["got"]) == 1, "已删表（全部版本幽灵）须整表滤掉"
    assert out["got"][0]["table"] == "usr_basic_info"
    assert [v["version"] for v in out["got"][0]["versions"]] == ["20261008"], "幽灵版本须滤掉"
    assert out["got"][0]["versions"][0]["live"] is True
    assert out["keepLive"] == 1 and out["absentIsGhost"] == 0, "不在 spaces＝幽灵（键数真源口径）"
    assert out["noSpaces"] == 0 and out["nullSafe"] == 0
    assert out["inputUntouched"] == 2, "过滤不得改入参表对象（缓存原样）"


# ── 三批修四（2026-10-08）：覆盖更新提示——完成页「更新 vs 新增」二选一 ──

def test_done_page_update_vs_created_notice():
    """同名同地址发布＝覆盖更新 200（端点 action=updated）；完成页一句明确告知
    「本次发布更新了既有服务「X」（地址 /v1/… 原样保留）」，新增则二选一显示新建文案。"""
    js = _read(PUB_JS)
    html = _read(OUT_PAGE)
    out = _node_json("""
console.log(JSON.stringify({
  updated: WIZ.doneLineText('用户资产标签查询', '/v1/svc/usr-asset-tag', 'updated'),
  created: WIZ.doneLineText('用户资产标签查询', '/v1/svc/usr-asset-tag', 'created'),
  fallback: WIZ.doneLineText('', '', '')
}));
""")
    assert out["updated"] == ("本次发布更新了既有服务「用户资产标签查询」"
                               "（地址 /v1/svc/usr-asset-tag 原样保留），已上线，可被调用。")
    assert "新增了服务「用户资产标签查询」" in out["created"] and "为本次新建" in out["created"]
    assert "新增" in out["fallback"], "action 缺省按新增口径（不谎称更新）"
    # 完成页主文案绑定 doneLineText()（判定源＝端点响应 action，publishNow 已采）
    assert re.search(r'id="done-line"[^>]*x-text="doneLineText\(\)"', html), \
        "done-line 须绑定 doneLineText()（更新 vs 新增二选一）"
    assert "publishedAction = String(res.body.action" in js, "action 须取自发布端点响应"
    assert "原样保留" in js, "覆盖更新文案须含「原样保留」（地址不变口径）"


# ── P3 二批（维护者 六条③）：拆页后导航断言（装载/发布/已发布/凭证） ──

def test_published_page_split_and_nav():
    publish = _read(OUT_PAGE)
    # 发布页保留向导＋「查看已发布服务 →」链接跳新页
    assert 'id="view-published-link"' in publish and 'href="published.html"' in publish
    # 新页两件在位（源＋门户镜像；镜像由 build_portal.py 同步）
    assert PUBD_HTML_SRC.is_file() and PUBD_JS.is_file()
    pubd = _read(OUT_PUBD)
    assert (REPO / "portal" / "admin" / "published.js").is_file()
    assert "publishedConsole" in _read(PUBD_JS)
    assert 'id="published-section"' in pubd and 'id="published-table"' in pubd
    assert 'id="go-publish-link"' in pubd and 'href="publish.html"' in pubd, "新页须可回发布页"
    # 详情视图＝现成端点聚合（字段中文名 schema 联查＋授权全景＋凭证配额），零后端新增写路径
    pjs = _read(PUBD_JS)
    assert "grants/panorama" in pjs and "/v1/admin/appkeys" in pjs, "详情须聚合授权租户与凭证配额"
    assert "loadSchemaUrl" in pjs, "字段中文名须经 schema 联查"
    # 侧栏导航行：装载→发布→已发布→AppKey 发放（门户侧栏同一真源）
    sidebar = _read(REPO / "portal" / "partials" / "sidebar.html")
    pos = {k: sidebar.index(f'href="admin/{v}"') for k, v in
           (("load", "load.html"), ("publish", "publish.html"),
            ("published", "published.html"), ("appkeys", "appkeys.html"))}
    assert pos["load"] < pos["publish"] < pos["published"] < pos["appkeys"], \
        f"导航序须为 装载/发布/已发布/凭证：{pos}"
    assert "已发布服务" in sidebar, "导航行缺「已发布服务」文案"
    # 产物侧栏（门户首页）同步出现入口
    assert 'href="admin/published.html"' in _read(REPO / "portal" / "index.html"), \
        "门户首页侧栏缺已发布服务入口（需重跑 build_portal.py）"


# ── AC-14（P3 弹窗卡 2026-10-08）：弹窗「遮罩＋内容同现」＋详情＝接口文档主体 ──

def test_admin_modal_panel_class_no_vendor_collision():
    """AC-14a：弹窗面板类不得叫 .modal——门户壳注入 bootstrap/tabler 自带
    .modal{display:none;position:fixed}，同名碰撞＝遮罩现/面板隐整页锁死
    （复现证据 /tmp/fix-shots/02-before-rotate-confirm.png：overlay=flex/panel=none）。
    面板类统一 .dlg，源页与门户镜像同口径。"""
    for f in (PUBD_HTML_SRC, ADMIN / "appkeys.html"):
        html = _read(f)
        assert 'class="modal"' not in html, f"{f.name} 仍有裸 .modal 面板类（vendor 碰撞锁死回归）"
        assert ".dlg {" in html and 'class="dlg"' in html, f"{f.name} 缺 .dlg 面板类"
        assert 'class="overlay"' in html, "遮罩层类应在位（遮罩＋内容同现口径）"
    for out in (OUT_PUBD, REPO / "portal" / "admin" / "appkeys.html"):
        html = _read(out)
        assert 'class="modal"' not in html, f"门户镜像 {out.name} 未同步（须重跑 build_portal.py）"
        assert 'class="dlg"' in html, f"门户镜像 {out.name} 缺 .dlg 面板类"


def test_published_detail_is_api_doc_layout():
    """AC-14b：详情主体＝接口文档——入参表/出参表（字段名/类型/中文释义）/curl 示例区块在位；
    授权租户/关联凭证压成末行状态（auth-status-line）；403 恐吓文案不再渲染。
    根因①修复标注（闭包改持 this.details[s.id] 响应式代理）须留在真源。"""
    for f in (PUBD_HTML_SRC, OUT_PUBD):
        html = _read(f)
        for el_id in ("doc-input-table", "doc-output-table", "doc-curl-block", "auth-status-line"):
            assert f'id="{el_id}"' in html, f"{f.name} 缺接口文档区块 {el_id}"
        assert "尚未登记授权（调用方将被 403 拒绝）" not in html, f"{f.name} 仍渲染 403 恐吓文案"
    pjs = _read(PUBD_JS)
    for fn_name in ("outFieldsOf", "inputDescOf", "curlOfService", "authTextOf"):
        assert fn_name in pjs, f"published.js 缺详情纯函数 {fn_name}"
    assert "var d = this.details[s.id];" in pjs, "闭包须改持响应式代理（裸对象回填不触发重渲染）"


def test_published_detail_pure_functions():
    """AC-14c：详情聚合纯函数（node 直调 PUBSVC）——出参表名/类型/中文释义形状、
    授权一行文案、凭证 kid∪租户双口径联查（demo_merchant 修复）、curl 带 apikey＋示例键。"""
    pubd_require = "require(" + json.dumps(str(PUBD_JS)) + ");\n"
    out = _node_json(pubd_require + """
const P = globalThis.PUBSVC;
const of = P.outFieldsOf(['age_band', 'x1'], [{name: 'age_band', type: 'varchar(16)', cnName: '年龄段'}]);
const curl = P.curlOfService('http://127.0.0.1:8000', '/v1/svc/usr-basic-info',
                             'a87ae39b27b7f83c2328e8bf4850a36bf1380032c252a1b2120d0d65f03d10d4');
const curlNoKey = P.curlOfService('http://x', '/v1/svc/a', '');
const creds = P.credsOfService(
  [{kid: '', callerId: 'demo_merchant'}, {kid: 'k2', callerId: 'partner-demo'}, {kid: 'k9', callerId: 'other'}],
  ['k2'], ['demo_merchant', 'partner-demo']).map(function (k) { return k.callerId; });
console.log(JSON.stringify({
  of: of,
  curlHead: curl.split(String.fromCharCode(10))[0], curlKey: curl.indexOf('a87ae39b27b7f83c') >= 0,
  curlApikey: curl.indexOf("apikey: <调用凭证>") >= 0, curlNoKey: curlNoKey.indexOf('<用户标识>') >= 0,
  auth: [P.authTextOf([{tenant: 'demo_merchant'}, {tenant: 'partner-demo'}], 3),
         P.authTextOf([], 0), P.authTextOf([{tenant: 'a'}], null)],
  creds: creds,
  example: P.exampleKeyOf('usr_basic_info').slice(0, 8)
}));
""")
    assert out["of"] == [
        {"name": "age_band", "type": "varchar(16)", "cn": "年龄段"},
        {"name": "x1", "type": "—", "cn": "—"}], "出参表须为 名/类型/中文释义；schema 未命中如实回退 —"
    assert out["curlHead"].startswith("curl -X POST 'http://127.0.0.1:8000/v1/svc/usr-basic-info'"), "curl 首行须为真实地址"
    assert out["curlApikey"] and out["curlKey"], "curl 须带 apikey 头＋实测示例键"
    assert out["curlNoKey"], "无实测键的表用 <用户标识> 占位（不编造示例键）"
    assert out["auth"][0] == "授权租户：demo_merchant、partner-demo · 关联凭证 3 个", "授权一行口径"
    assert out["auth"][1] == "暂无授权", "无授权时一行「暂无授权」"
    assert "凭证数暂不可读" in out["auth"][2], "凭证列表不可读须如实标注，不冒充零"
    assert out["creds"] == ["demo_merchant", "partner-demo"], "凭证联查须 kid∪租户口径（demo_merchant 无 kid 也不漏）"
    assert out["example"] == "a87ae39b", "usr_basic_info 实测示例键在位"



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
