#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卡2 套件③：数据装载页构建产物机检（T5/T6 · AC-1/7/8/9）。

判据映射（开工门槛-卡2 §A3）：
  AC-1 /admin/load 渲染四步向导（①选源→②发现表→③装载→④结果；产物 id 断言）；
  AC-7 结果区三要素：表:版本／行数／校验（对账 pass/diffCount）；
  AC-8 向导**不存在**勾字段/选列步骤与控件：源页与产物零 checkbox、零「勾选」文案；
       load.js 装载请求体零 columns 键（前端不传；后端忽略口径见 test_load_api）；
  AC-9 注册进 build_portal（NAV_KEYS/ADMIN_PAGES）＋侧栏「管理」组「数据装载」，
       产物侧栏 active 唯一指向本页，管理组顺序＝装载→发布→AppKey→上游（拆分设计 §四）。

产物缺失时先补构建（同 test_admin_shell 口径，套件可独立运行）。

P3 二批（2026-10-08，维护者 七条中的 1/2/3/5/6）：
  · 删选源步骤右下角页脚降级口径句（页内样式隐藏——句属全站共享 footer.html，
    非本页真源；全站删除须另卡）；
  · 「发现时间」→「扫描时间」，本地时区 YYYY-MM-DD HH:MM:SS（去 ISO T/时差后缀）；
  · 字段清单重做：未装载表＝discovery 样本列直渲染（类型/释义「源侧待补」），
    已装载表＝sync:schema（对外只称「HBase 元数据」）；承诺性文案删除；
  · 删装载步骤列族选择器（静默取 discovery 列族，请求体照常带 cf）；
  · 结果区「字段元数据 N 个字段」——来源括号删除，load 两文件「dict」零残留。

P3 追加（2026-10-08，键展示）：字段清单头部「查询主键」行——keyHint 来自注册表
  console 写区 key 描述（/v1/admin/load/tables 表行带出，如「usr_id 的 phone_sm3」），
  未装载表（样本列态）同样显示（键信息来自注册表，不受装载影响）；未登记表
  如实标注「未登记」，不猜默认键型。

P3-装载409分支修复卡（2026-10-08）：409 两情形前端分流——目标版本==该表行
  loaded.latestVersion（当前线上版）→ 前端预判**不发请求**＋琥珀（result-warn）提示
  换版本日期（含次日建议；该路径无覆盖按钮——线上版后端连 replace 也拒，线上版
  保护，正确设计；旧版一律引导覆盖重灌即死循环）；非线上版 409 → 覆盖面板照旧
  （replace:true 实测可行）＋覆盖按钮旁小字「线上版不可覆盖；若 {V} 已成为线上版
  请换日期」。

P3 三批（2026-10-08，维护者 实测两修）：
  · 修一 schema 缺失态回退——已装载表最新版本 schema 读不到（404/缺失，实测
    usr_basic_info 20261009）时回退渲染 discovery 样本列；两态统一：可读→完整
    字段表、不可读→样本列（旧死文案态整段删除）；
  · 修二 键行来源如实化——拆「查询主键字段名：key（HBase rowkey）」固定行＋描述行
    （有登记「含义：…」＝发布登记人工填写；无登记不显示描述行），列清单紧随
    其后；旧合并式键行文案退役。

字段释义收权（2026-10-14，门槛稿 v1 · 维护者 拍板）：
  · AC-4 发现表页瘦身——只列源表（表名/描述/已装载版本保留），「登记卡」与字段
    折叠整块删除（旧「源侧待补」占位文案零残留）；
  · 字段释义清单挪至③装载环节（选表后 initFieldDefs 带出：carry 沿用上次版本
    sync:schema 释义／sample 样本列回退；贴 DDL 覆盖与未释义/新增标记的机检在
    tests/test_load_flow_schema.py）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PORTAL = REPO / "portal"
SRC_PAGE = REPO / "admin" / "load.html"
SRC_JS = REPO / "admin" / "load.js"
OUT_PAGE = PORTAL / "admin" / "load.html"
OUT_JS = PORTAL / "admin" / "load.js"   # build copytree 的逐字节副本（构建同步断言用）
SIDEBAR = REPO / "portal" / "partials" / "sidebar.html"

if not OUT_PAGE.is_file():
    subprocess.run([sys.executable, str(REPO / "scripts" / "build_portal.py")],
                   check=True, capture_output=True)

sys.path.insert(0, str(REPO / "scripts"))
import build_portal as bp  # noqa: E402


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── AC-1：四步向导 ──

def test_load_page_four_steps():
    html = _read(OUT_PAGE)
    for i, name in ((1, "选源"), (2, "发现表"), (3, "装载"), (4, "结果")):
        assert f'id="load-step-nav-{i}"' in html, f"缺第 {i} 步导航（{name}）"
    for sid in ("load-section-source", "load-section-tables",
                "load-section-load", "load-section-result"):
        assert f'id="{sid}"' in html, f"缺步骤区块 {sid}"
    assert html.index("load-step-nav-1") < html.index("load-step-nav-2")         < html.index("load-step-nav-3") < html.index("load-step-nav-4"), "步骤序错乱"
    assert 'x-data="loadWizard()"' in html, "页面未挂 loadWizard 组件"


# ── AC-7：结果三要素 ──

def test_result_shows_table_version_rows_recon():
    html = _read(OUT_PAGE)
    for bid, label in (("load-result-version", "表:版本"),
                       ("load-result-rows", "行数"),
                       ("load-result-recon", "校验（对账）"),
                       ("load-result-schema", "字段元数据")):
        assert f'id="{bid}"' in html, f"结果区缺 {label}（{bid}）"
    # P3（2026-10-08）：复制按钮退役——发布向导①自动带出已装载表，「去发布」直达即可
    assert "去发布" in html and 'id="load-goto-publish"' in html
    assert 'id="load-copy-version"' not in html and "复制 表:版本" not in html


# ── AC-8：无勾字段步骤与控件；请求不带 columns ──

def test_no_field_selection_step():
    """装载向导零选列控件：源页与产物都无 checkbox/「勾选」；JS 请求体零 columns。

    「不选列」是口径的**否定式**说明（如实告知），不算选列文案——剥除后再断言。
    """
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert "checkbox" not in html, f"{p.name} 出现勾选控件（装载不裁列）"
        assert "勾选" not in html, f"{p.name} 出现勾选文案"
        assert "选列" not in html.replace("不选列", ""), f"{p.name} 出现选列文案"
    js = _read(SRC_JS)
    assert "columns" not in js, "load.js 不得出现 columns（全量装载，AC-8 前端不传）"
    m = re.search(r"var payload = \{[^}]*\}", js, re.S)
    assert m and "version" in m.group(0) and "table" in m.group(0) and "cf" in m.group(0)
    # P3 交互重做：样本列 chip 展示退役，列信息＝可折叠字段清单（见 test_discovery_field_list）


# ── AC-9：注册与导航 ──

def test_load_page_registered_in_build_and_sidebar():
    assert "admin_load" in bp.NAV_KEYS, "NAV_KEYS 缺 admin_load"
    assert "load" in bp.ADMIN_PAGES and bp.ADMIN_PAGES["load"]["out"] == "load.html"
    assert bp.ADMIN_PAGES["load"]["script"] == "load.js"

    sidebar = _read(SIDEBAR)
    assert 'href="admin/load.html"' in sidebar and "数据装载" in sidebar
    # 管理组顺序：装载 → 发布 → AppKey → 上游（拆分设计 §四；装载先于发布）
    assert sidebar.index("admin/load.html") < sidebar.index("admin/publish.html")

    html = _read(OUT_PAGE)
    active = re.findall(r'os-nav-link is-active[^>]*href="([^"]+)"', html)
    # 管理页产物侧栏经子目录相对改写：admin/load.html → load.html（页内相对）
    assert active == ["load.html"], f"产物侧栏 active 应唯一指向本页，实得 {active}"
    crumb = re.search(r'id="admin-crumb".*?</nav>', html, re.S)
    assert crumb and "管理" in crumb.group(0) and "数据装载" in crumb.group(0)


def test_load_page_script_order_and_api_targets():
    """脚本序硬规矩（gate→业务→alpine）＋前端 API 目标域白名单。

    P3（2026-10-08）起新增两目标：/v1/admin/load/schema（发现表字段清单数据源，
    sync:schema:<表>:<版本> 元数据）＋/v1/admin/publish/hbase/load（409 覆盖重灌唯一
    replace 出口——既有发布装载端点；POST /v1/admin/load 自身仍严格幂等不覆盖）。
    """
    html = _read(OUT_PAGE)
    # cache-busting 后产物可带构建期版本参数，断言容忍可选 ?v=<hex>（只放宽匹配不改序契约）
    order = [re.search(pat, html).start() for pat in
             (r'src="admin-gate\.js(\?v=[0-9a-f]+)?"', r'src="load\.js(\?v=[0-9a-f]+)?"',
              r'assets/alpine\.min\.js(\?v=[0-9a-f]+)?"')]
    assert order == sorted(order), "脚本序须为 admin-gate → load.js → alpine"
    js = _read(SRC_JS)
    targets = set(re.findall(r"'/v1/admin/[^']*'", js))
    assert targets == {"'/v1/admin/load/sources'", "'/v1/admin/load/tables'",
                       "'/v1/admin/load'", "'/v1/admin/load/schema'",
                       "'/v1/admin/publish/hbase/load'",
                       "'/v1/admin/schema/parse'", "'/v1/admin/schema/save'",
                       "'/v1/admin/load/tasks/'"}, targets


# ── P3（2026-10-08）交互批次回归 ──

def test_today_str_zero_padded():
    """P3-1 bug：todayStr 须产 YYYY-MM-DD 零填充。

    旧 pad2（(''+x).slice(-2)）单 digit 不补零 → 2026-10-8 被 date input 判非法
    丢弃，状态残值直提交易得畸形版本号 2026108（后端 INVALID_VERSION 前的脏路径）。
    """
    js = _read(SRC_JS)
    assert "function pad2" in js and "('0' + x).slice(-2)" in js, "pad2 须零填充实现"
    assert "('' + x).slice(-2)" not in js, "pad2 旧非零填充实现残留"


def test_conflict_overwrite_option():
    """P3-2：409 LOAD_CONFLICT 面板提供显式「覆盖重灌」＋红字警示＋原生 confirm。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert 'id="load-overwrite"' in html and "覆盖重灌" in html, f"{p.name} 缺覆盖重灌按钮"
        assert "覆盖重灌将先清空" in html, f"{p.name} 缺红字覆盖警示"
        assert "btn-danger" in html, f"{p.name} 缺危险态按钮样式"
    js = _read(SRC_JS)
    assert "window.confirm" in js, "覆盖动作须原生 confirm 二次确认"
    assert "payload.replace = true" in js and "'/v1/admin/publish/hbase/load'" in js


def test_discovery_page_slim_ac4():
    """AC-4（收权 2026-10-14）：「发现表」页必须不再含「表结构登记」卡与「源侧待补」
    文案；字段折叠整块删除——发现页只列源表（表名/描述/已装载版本信息保留）。字段
    释义清单只在③装载环节出现（id=load-fields，见 test_load_step3_field_defs_block）。
    源页/产物（html＋js）零残留（含注释——退役短语不复述，防回潮 grep 干净）。
    """
    for p_ in (SRC_PAGE, OUT_PAGE):
        html = _read(p_)
        sec = html[html.index('id="load-section-tables"'):html.index('id="load-section-load"')]
        assert "field-toggle" not in sec and "字段清单" not in sec,             f"{p_.name} ② 发现表仍残留字段清单折叠（NC-1：发现页只列表）"
        assert "load-section-schema-import" not in html,             f"{p_.name} 「表结构登记」卡须删除（入口挪③贴 DDL 覆盖）"
        assert "源侧待补" not in html, f"{p_.name} 「源侧待补」占位文案须零残留"
        # 保留项：表名/描述/已装载版本行仍在②（维护者 拍板只删折叠与登记卡）
        assert "load-pick-" in sec and "当前线上版：" in sec and "ver-line" in sec
    for p_ in (SRC_JS, OUT_JS):
        js = _read(p_)
        assert "toggleSchema" not in js and "schemaOf" not in js,             f"{p_.name} ② 折叠逻辑须随 UI 退役"
        assert "源侧待补" not in js, f"{p_.name} js 残留「源侧待补」"


def test_load_step3_field_defs_block():
    """收权（2026-10-14）：③装载环节承载字段释义确认区——键行随清单搬家（口径不变），
    状态行/字段表/未释义引导/贴 DDL 区块在场。"""
    for p_ in (SRC_PAGE, OUT_PAGE):
        html = _read(p_)
        sec = html[html.index('id="load-section-load"'):html.index('id="load-section-result"')]
        for bid in ("load-fields", "load-fields-state", "load-fields-table",
                    "load-fields-missing", "load-ddl", "load-ddl-input",
                    "load-ddl-parse", "load-ddl-preview", "load-ddl-save"):
            assert f'id="{bid}"' in sec, f"{p_.name} ③ 缺字段释义区块 {bid}"
        assert "<th>字段名</th>" in sec and "<th>类型</th>" in sec and "<th>中文释义</th>" in sec
        assert "未释义" in sec and "新增" in sec, f"{p_.name} 缺未释义/新增标记"
        assert "missingText()" in sec, f"{p_.name} 缺未释义引导绑定"
        assert "贴 DDL 覆盖释义" in sec, f"{p_.name} 缺 DDL 覆盖入口"
    js = _read(SRC_JS)
    assert "sampleColumns" in js, "样本列须取自 discovery families[].sampleColumns"
    assert "familyNames" not in js, "familyNames 已随列族术语退场"


def test_field_list_key_line():
    """P3 追加→三批改版（修二）→收权搬家（2026-10-14）：键行拆两部分如实展示——
    「查询主键字段名：key（HBase rowkey）」固定行＋描述行（keyHint＝发布登记
    console 写区人工填写的 key 描述，非系统从 HBase 推断；未登记不显示描述行）。
    键行随字段释义清单挪至③（id=load-fields 块头部，绑定 keyDescLine(picked)），
    字段表紧随其后；旧合并式键行文案（以此 key 可查询…）退役。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert "查询主键字段名" in html and "HBase rowkey" in html, f"{p.name} 缺键字段名固定行"
        assert 'class="key-line"' in html and 'x-text="keyDescLine(picked)"' in html,             f"{p.name} 键行未绑定 keyDescLine(picked)（③ 选中表）"
        i_list = html.index('id="load-fields"')
        i_key = html.index('class="key-line"', i_list)
        i_tbl = html.index('id="load-fields-table"', i_list)
        assert i_list < i_key < i_tbl, f"{p.name} 键行须在③字段表之前"
    for p in (SRC_JS, OUT_JS):
        js = _read(p)
        assert "keyDescLine" in js and "keyHint" in js, f"{p.name} 须消费表行 keyHint"
        assert "含义：" in js, f"{p.name} 描述行须以「含义：」前缀展示登记值"
        assert "来自发布登记" not in js and "非系统推断" not in js, f"{p.name} 残留自我证明话术"
        assert "以此 key 可查询下列全部字段" not in js, f"{p.name} 旧合并式键行文案残留"


def test_schema_missing_falls_back_to_sample():
    """P3 三批·修一→收权搬家（2026-10-14）：③ initFieldDefs 两态统一——已装载表读
    上次版本 sync:schema 可读→carry（沿用上次释义）；未装载过或 schema 读不到
    （404/缺失/网络失败，实测 card_acct_summary 20261009）→ 回退 discovery 样本列
    （sample 态，释义全缺如实标出）。非 2xx 与网络失败两路都落 sample；旧 none/error
    死文案态零残留（源页/产物 html＋js）。"""
    js = _read(SRC_JS)
    seg = js[js.index("async initFieldDefs("):js.index("fieldStateText()")]
    assert "this.fieldState = 'carry'" in seg, "schema 可读须落 carry 态（沿用上次释义）"
    assert seg.count("this.fieldState = 'sample'") == 2,         "未装载过与读不到两路都须落 sample 态（回退样本列）"
    assert "if (!v) {" in seg, "未装载表（无版本）先走样本列短路"
    state = js[js.index("fieldStateText()"):js.index("missingText()")]
    for phrase in ("默认沿用上次释义（版本 ", "样本列 ", "已按本次 DDL 覆盖释义"):
        assert phrase in state, f"释义状态行缺文案「{phrase}」（三态俱全）"
    assert "this.fieldState = 'none'" not in js and "this.fieldState = 'error'" not in js,         "none/error 死文案态须退役"
    for p in (SRC_PAGE, OUT_PAGE):
        h = _read(p)
        assert "无字段元数据" not in h and "未生成 schema" not in h, f"{p.name} 死文案残留"
    for p in (SRC_JS, OUT_JS):
        j = _read(p)
        assert "无字段元数据" not in j and "未生成 schema" not in j, f"{p.name} js 死文案残留"
    assert "initFieldDefs" in _read(OUT_JS), "产物 load.js 须与真源同步（构建未跑？）"


def test_load_tables_key_hint_chain():
    """P3 追加（后端链）：/v1/admin/load/tables 每表带 keyHint——值取注册表 console
    写区条目 key 描述（backend.load.table 匹配；先全名后裸表名），未登记表空串如实
    透出。源级机检（同 test_tables_endpoint_uses_hbase_only 的源检口径）＋真源在场
    自证（console 条目均带 key 描述）。"""
    src = (REPO / "service" / "app" / "admin_api.py").read_text(encoding="utf-8")
    start = src.index("async def load_tables_ep")
    seg = src[start:src.index('@router.post("/v1/admin/load"', start)]
    assert "keyHint" in seg and "load_console_services" in seg, \
        "load/tables 端点须注入 keyHint（console 写区 key 描述）"
    import yaml
    data = yaml.safe_load((REPO / "service" / "services.yaml").read_text(encoding="utf-8"))
    entries = data.get("console_services") or []
    assert entries and all(str(e.get("key") or "").strip() for e in entries), \
        "console 写区条目须带 key 描述（keyHint 真源）"


def test_scan_time_local_format():
    """P3 二批：表清单时间＝本地时区「YYYY-MM-DD HH:MM:SS」（去 ISO 的 T 与时差
    后缀），文案「发现时间」改「扫描时间」。格式化走真时区转换（Date 本地分量），
    非字符串截断——服务端 offset 与浏览时区不一致时须换算而非照抄数字。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert "扫描时间" in html, f"{p.name} 缺「扫描时间」文案"
        assert "发现时间" not in html, f"{p.name} 旧文案「发现时间」残留"
    js = _read(SRC_JS)
    assert "formatScanTime" in js and "getHours()" in js, "须本地时区格式化实现"
    assert "formatScanTime: formatScanTime" in js, "纯函数须挂 window.O2LOAD（selftest 可直调）"


def test_cf_selector_removed():
    """P3 二批：装载步骤「存储列族（HBase 术语，一般无需改动）」下拉整块移除
    （维护者：实际只有 cf 一个值，无意义）。内部逻辑不变：静默取 discovery 列族
    （默认 cf），请求体照常带 cf（后者由 test_no_field_selection_step 断言）。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert 'id="load-cf-details"' not in html and 'id="load-cf-select"' not in html, \
            f"{p.name} 列族选择器残留"
        assert "存储列族" not in html, f"{p.name} 「存储列族」字样残留"


def test_source_step_footer_note_hidden():
    """P3 二批＋门面批：页脚句「降级展示时各卡片带角标标注；数字口径以来源接口
    返回为准」全站物理删除（footer.html 源头删＋本页 CSS 隐藏块一并移除）——
    源页/产物/footer 三处零残留。"""
    DEAD_PHRASE = "降级展示时各卡片带角标标注"
    import pathlib
    footer = pathlib.Path(__file__).resolve().parent.parent / "portal" / "partials" / "footer.html"
    targets = list(SRC_PAGE) + list(OUT_PAGE) if isinstance(SRC_PAGE, (list, tuple)) else [SRC_PAGE, OUT_PAGE]
    targets.append(footer)
    for p in targets:
        path = pathlib.Path(p)
        html = path.read_text(encoding="utf-8") if path.exists() else ""
        assert DEAD_PHRASE not in html, f"{path.name} 页脚句残留（应已全站物理删除）"


def test_result_schema_source_paren_removed():
    """P3 二批：结果区「字段元数据 N 个字段」——来源括号删除；admin/load 两文件
    （源＋产物 × html＋js）内「dict」字样零残留（grep 口径）。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert "个字段（" not in html, f"{p.name} 结果区来源括号残留"
    for p in (SRC_PAGE, OUT_PAGE, SRC_JS, OUT_JS):
        assert "dict" not in _read(p), f"{p.name} 残留「dict」字样"


def test_version_line_copy():
    """P3-6：版本行文案＝每版本一行（线上版「当前线上版：V」绿字；其余「已装载 V · 未上线」）。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert "当前线上版：" in html, f"{p.name} 缺线上版文案"
        assert "· 线上" not in html, f"{p.name} 旧文案「已装载 V · 线上」残留"
        assert "未上线" in html, f"{p.name} 缺非线上版本标注"


# ── P3-装载超时修复卡（2026-10-08）：四项判据（nginx 600s／三分支人话／计时器／
#    覆盖重灌全程反馈）。背景：usr_asset_tag 100 万行装载超 nginx 默认 60s → 504
#    HTML → 裸 SyntaxError；服务端继续跑完（Redis 已满），重试全 409。──


def test_nginx_long_timeout_for_load_chain():
    """项1：/v1/admin/ 与 /v1/ 反代段各加 proxy_read_timeout 600s＋proxy_send_timeout
    600s（只这两条，别段不动——/v1/portal/ 与 /demo 保持默认）。"""
    conf = _read(REPO / "configs" / "nginx" / "portal-nginx.conf")
    for loc in ("location /v1/admin/ {", "location /v1/ {"):
        seg = conf[conf.index(loc):conf.index("}", conf.index(loc))]
        assert "proxy_read_timeout 600s;" in seg, f"{loc} 段缺 proxy_read_timeout 600s"
        assert "proxy_send_timeout 600s;" in seg, f"{loc} 段缺 proxy_send_timeout 600s"
    untouched = conf[conf.index("location /v1/portal/ {"):conf.index("}", conf.index("location /v1/portal/ {"))]
    assert "proxy_read_timeout" not in untouched and "proxy_send_timeout" not in untouched, \
        "/v1/portal/ 段不在本卡范围（须保持不动）"


def test_load_response_three_branches_humanized():
    """项2：装载响应三分支——非 JSON（504 HTML/解析失败）→ 人话「仍在后台进行」；
    409 → 人话「可能此前已成功」；其余非 2xx 沿用错误信封。res.json() 必在
    try/catch 内（doLoad 段零裸 await r.json()，裸 SyntaxError 根除）。"""
    js = _read(SRC_JS)
    assert "装载仍在后台进行（大表需数分钟），网关已超时返回" in js, "缺网关超时人话文案"
    assert "请勿重复提交" in js and "刷新本页查看结果" in js, "缺行动指引（勿重复提交/稍后刷新）"
    assert "text/html" in js, "须按 content-type 识别网关 HTML 错页"
    do_load = js[js.index("async doLoad("):js.index("async schemaAfterLoad(")]
    assert "await r.json()" not in do_load, "doLoad 内不得裸 r.json()（SyntaxError 源头）"
    assert "readJson(r)" in do_load, "装载响应须走安全解析 readJson"
    assert "该表该版本已装载（可能此前已成功）。如需重灌请使用覆盖重灌" in js, "缺 409 人话文案"
    assert "errorText(body, '装载失败。')" in do_load, "其余非 2xx 须沿用错误信封展示"


def test_load_timer_feedback():
    """项3：装载计时器——提交后按钮禁用＋「装载中…已用时 Xs（大表首次装载约
    2-5 分钟，请勿重复提交）」每秒跳；响应到达（任何形态）即停表（finally 停表）。"""
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        assert 'id="load-progress"' in html, f"{p.name} 缺装载计时反馈区"
        assert "已用时 " in html and "大表首次装载约 2-5 分钟，请勿重复提交" in html, \
            f"{p.name} 计时文案缺「已用时/请勿重复提交」"
        back = re.search(r'id="load-back"[^>]*', html)
        assert back and ':disabled="loading"' in back.group(0), \
            f"{p.name} 装载中「返回表清单」须禁用（防中途离开丢结果观感）"
    js = _read(SRC_JS)
    assert "loadElapsed" in js and "setInterval" in js, "缺计时器实现（每秒跳）"
    assert "stopLoadTimer" in js and "clearInterval" in js, "缺停表实现"
    do_load = js[js.index("async doLoad("):js.index("async schemaAfterLoad(")]
    assert "startLoadTimer()" in do_load, "提交即起表"
    tail = do_load[do_load.index("finally"):]
    assert tail.index("stopLoadTimer()") < tail.index("loading = false"), "停表先于解禁"
    assert "stopLoadTimer()" in tail, "响应到达（任何形态）finally 即停表"


def test_overwrite_keeps_conflict_panel():
    """项4：覆盖重灌全程有反馈——loading 期间 409 面板保持在场（按钮禁用而非
    消失，重灌入口不丢）；提交/覆盖双入口重入闸（loading 中再点无效）。"""
    js = _read(SRC_JS)
    assert "if (!replace) this.conflict = ''" in js, \
        "覆盖重灌不得清 409 面板（普通提交才清）——面板瞬失即「没反应」观感根源"
    assert "if (!this.picked || this.loading) return" in js, "缺 loading 重入闸"
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        ow = re.search(r'id="load-overwrite"[^>]*', html)
        assert ow and ':disabled="loading"' in ow.group(0), f"{p.name} 覆盖按钮装载中须禁用"


# ── P3-装载409分支修复卡（2026-10-08）：409 两情形前端分流 ──


def test_live_version_target_no_request():
    """判据1：目标版本==该表行 loaded.latestVersion（当前线上版）→ 不发请求＋琥珀提示。

    根因：线上版后端连 replace 也拒（线上版保护，正确设计）；旧版 409 一律引导
    「覆盖重灌」→ 死循环（维护者 实测 card_acct_summary 卡死）。修法：submit 前预判
    （预判数据＝发现表列表行自带 loaded.latestVersion），琥珀（result-warn）提示
    换版本日期（含次日建议），该路径无覆盖按钮。
    """
    js = _read(SRC_JS)
    submit = js[js.index("async submit("):js.index("async overwrite(")]
    assert "isLiveVersion(this.picked, this.version)" in submit, "submit 须先判目标版本==线上版"
    assert submit.index("if (isLiveVersion") < submit.index("doLoad(false)"), \
        "线上版命中须在发请求前直接 return（不发请求）"
    assert "latestVersion" in js, "预判数据源须为发现表行 loaded.latestVersion"
    assert "function nextDateStr" in js, "缺次日建议日期推导"
    assert "是当前线上版，线上版不可覆盖重灌（保护在线调用）" in js, "缺琥珀文案主体"
    assert "请换一个版本日期（如" in js and "装载成功后可在发布台切换上线" in js, \
        "缺琥珀文案出路指引"
    assert "isLiveVersion: isLiveVersion" in js and "liveBlockText: liveBlockText" in js, \
        "纯函数须挂 window.O2LOAD（selftest 可直调）"
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        i_blk = html.find('id="load-live-block"')
        assert i_blk >= 0, f"{p.name} 缺线上版琥珀提示块"
        open_tag = html[html.rfind("<div", 0, i_blk):i_blk]
        assert "result-warn" in open_tag and "--status-warn" in html, \
            f"{p.name} 提示须琥珀色（result-warn ＋ --status-warn 令牌）"
        seg = html[i_blk:html.index("</div>", i_blk)]
        assert 'x-show="liveBlock"' in seg and 'x-text="liveBlock"' in seg, \
            f"{p.name} 提示块未绑定 liveBlock"
        assert 'id="load-overwrite"' not in seg, f"{p.name} 线上版路径不得出现覆盖按钮"
    assert "liveBlockText" in _read(OUT_JS), "产物 load.js 须与真源同步（构建未跑？）"


def test_non_live_409_keeps_overwrite_panel():
    """判据2：非线上版 409 → 现有覆盖面板照旧（replace:true 实测可行）＋面板补小字。

    小字口径：线上版不可覆盖；若 {V} 已成为线上版请换日期（动态带目标版本号）。
    """
    js = _read(SRC_JS)
    assert "r.status === 409" in js and "this.conflict = conflictText(body)" in js, \
        "409 分支须维持既有覆盖面板路径（conflict 文案＋覆盖入口不变）"
    for p in (SRC_PAGE, OUT_PAGE):
        html = _read(p)
        seg = html[html.index('id="load-conflict"'):html.index('id="load-error"')]
        assert 'id="load-overwrite"' in seg and "覆盖重灌将先清空" in seg, \
            f"{p.name} 覆盖面板/红字警示须照旧"
        assert "线上版不可覆盖；若" in seg and "已成为线上版请换日期" in seg, \
            f"{p.name} 覆盖按钮旁缺小字提示"
        assert "versionNum()" in seg, f"{p.name} 小字须动态带目标版本号"


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
