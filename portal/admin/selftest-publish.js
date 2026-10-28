/* ============================================================
 * 服务发布 · 五步向导断言运行器（selftest-publish.js · 卡3 流程②）
 * 断言对象：iframe 内真实发布页（publish.html，挡板 API 驱动）
 *          ＋ iframe 内 window.WIZ 纯函数（与 publish.js 同源）。
 * 契约：#summary/#results 表格 + html[data-selftest=green|red] + data-selftest-done。
 * 覆盖：加载序 / 五步 id / ①assets 表源 / ②版本默认线上＋非线上提示＋设为线上 /
 *       ③slug 机检 / ④schema 字段源＋勾选计数 / ⑤发布→完成页 curl(apikey)＋T10 /
 *       服务表数据版本列 / 下线 E 卡文案 / 文案扫描 / 构建同步。
 * ============================================================ */
(function () {
  'use strict';

  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function tick() { await sleep(120); }
  async function until(fn, ms) {
    var end = Date.now() + (ms || 4000);
    while (Date.now() < end) { if (fn()) return true; await sleep(120); }
    return fn();
  }

  var results = [];
  function $(id) { return document.getElementById(id); }
  function record(id, name, ok, detail, skipped) {
    results.push({ id: id, name: name, ok: ok, detail: detail || '', skipped: !!skipped });
    renderRow(results[results.length - 1]);
    renderSummary();
  }
  function assert(id, name, fn) {
    return fn().then(function (d) { record(id, name, true, d); })
      .catch(function (e) { record(id, name, false, String(e && e.message || e)); });
  }
  function renderRow(r) {
    var box = $('results');
    if (!box.getAttribute('data-init')) {
      box.innerHTML = '<table><thead><tr><th>#</th><th>断言 id</th><th>断言内容</th><th>结果</th><th>详情</th></tr></thead><tbody></tbody></table>';
      box.setAttribute('data-init', '1');
    }
    var tr = document.createElement('tr');
    var status = r.skipped ? 'SKIP' : (r.ok ? 'PASS' : 'FAIL');
    // #36 XSS 全扫（2026-10-15）：r.name/r.detail 动态串统一 escapeHtml 基线。
    var esc = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };
    tr.innerHTML = '<td class="mono">' + results.length + '</td><td class="mono">' + esc(r.id) + '</td><td>' +
      esc(r.name) + '</td><td class="' + (r.skipped ? 'skip' : (r.ok ? 'pass' : 'fail')) + '">' + status + '</td><td>' +
      esc(r.detail || '') + '</td>';
    box.querySelector('tbody').appendChild(tr);
  }
  function renderSummary() {
    var pass = results.filter(function (r) { return r.ok && !r.skipped; }).length;
    var skip = results.filter(function (r) { return r.skipped; }).length;
    var fail = results.filter(function (r) { return !r.ok && !r.skipped; }).length;
    var allGreen = fail === 0 && pass >= 14;
    $('summary').innerHTML = '<b class="' + (allGreen ? 'pass' : 'fail') + '">' +
      (allGreen ? '全绿' : '未全绿') + '</b>：' + pass + ' 通过 · ' + skip + ' 跳过 · ' + fail + ' 失败（要求≥14 全绿）';
    document.title = allGreen ? '服务发布向导 selftest 全绿（' + pass + '）' : '服务发布向导 selftest 未全绿';
    document.documentElement.setAttribute('data-selftest', allGreen ? 'green' : 'red');
  }

  /* ── iframe 内的真实发布页 ── */
  var frame, doc, win, comp, W;
  function F(id) { return doc.getElementById(id); }
  function txt(id) { var el = F(id); return el ? (el.textContent || '').trim() : ''; }
  function vis(id) {
    var el = F(id);
    return !!el && win && win.getComputedStyle(el).display !== 'none';
  }
  function click(id) { var el = F(id); if (!el) throw new Error('缺 #' + id); el.click(); }

  /* 文案扫描（发布页正文 main；含 ADR-0008 服务修订号禁令） */
  var JARGON = [
    /O-\d+/, /M2-\d+/, /AC-\d+/, /\bT1\b/, /dual-source/, /\bscan\b/, /rowkey/, /\bmeta\b/, /CSV/,
    /业务域/, /数据域/, /字段表即表单/, /存量流程/,
    /测试环境/, /生产环境/, /仅管理员/, /快照\s?\d{4}-\d{2}-\d{2}/,
    /三步完成/, /目录树来自/,
    /校验\s?ok/, /零明文/, /自证/,
    /服务修订号/,
    /[0-9a-fA-F]{64}/
  ];

  var STEP_IDS = ['step-1', 'step-2', 'step-3', 'step-4', 'step-5', 'step-done'];
  function visibleSteps() { return STEP_IDS.filter(function (id) { return vis(id); }); }

  async function main() {
    var frameEl = $('wiz-frame');
    var ready = await until(function () {
      frame = frameEl.contentWindow;
      doc = frame && frame.document;
      if (!doc || !doc.body) return false;
      win = frame;
      W = win.WIZ;
      comp = win.Alpine ? win.Alpine.$data(doc.body) : null;
      return !!(W && comp && comp.assetsTables && comp.assetsTables.length > 0);
    }, 8000);
    if (!ready) throw new Error('向导页未就绪（iframe 未挂载或挡板 assets 未回填）');

    await assert('load-order', '向导组件 publishWizard 与纯函数 WIZ 均已注册（业务 js 先于 alpine）', async function () {
      if (!W || !win.O1HBase) throw new Error('WIZ/O1HBase 未注册');
      return 'WIZ + O1HBase 在位';
    });

    await assert('ids-all', '关键区块 id 全中（五步/资产/版本/字段/发布/完成/服务区/弹窗）', async function () {
      var ids = ['wizard-stepper', 'step-1', 'step-2', 'step-3', 'step-4', 'step-5', 'step-done',
        'table-cards', 'assets-empty', 'live-switch-row', 'switch-btn', 'advanced-details',
        'svc-name-input', 'route-input', 'route-error', 'field-list', 'select-all-check',
        'checked-count', 'publish-btn', 'done-name', 'curl-block', 'auth-guide',
        'services-section', 'service-table', 'offline-modal'];
      var miss = ids.filter(function (i) { return !F(i); });
      if (miss.length) throw new Error('缺 ' + miss.join(','));
      return ids.length + ' 个 id 全中';
    });

    await assert('build-sync', '同目录发布页正文含五步向导容器（构建同步）', async function () {
      var src = await frame.fetch('publish.html').then(function (r) { return r.text(); });
      var need = ['step-1', 'step-2', 'step-3', 'step-4', 'step-5', 'services-section', 'wizard-stepper'];
      var miss = need.filter(function (n) { return src.indexOf('id="' + n + '"') < 0; });
      if (miss.length) throw new Error('源页缺 ' + miss.join(','));
      return '五步容器在位';
    });

    await assert('built-title', '产物页 title=「服务发布 · OneServe」（卡3 NC-4）', async function () {
      var built = await frame.fetch('../portal/admin/publish.html').then(function (r) { return r.text(); });
      if (built.indexOf('服务发布 · OneServe') < 0) throw new Error('产物 title 未更新');
      return 'title 已换「服务发布」';
    });

    await assert('step1-assets', '① 已装载表卡片渲染（中文名＋版本数；挡板＝usr_basic_info）', async function () {
      var card = F('table-card-usr_basic_info');
      if (!card) throw new Error('缺表卡片');
      if (card.textContent.indexOf('用户基本信息') < 0) throw new Error('卡片缺中文名');
      if (card.textContent.indexOf('2 个已装载版本') < 0) throw new Error('卡片缺版本次数');
      return 'assets 表源渲染正确';
    });

    await assert('step1-pick', '① 选中表→② 默认线上版＋名称/地址自动建议', async function () {
      click('table-card-usr_basic_info');
      await tick(); await tick();
      if (comp.pickedTable !== 'usr_basic_info') throw new Error('未选中表');
      if (comp.pickedVersion !== '20260930') throw new Error('默认版本应＝live 20260930，实=' + comp.pickedVersion);
      if (F('svc-name-input').value.indexOf('用户基本信息') < 0) throw new Error('服务名未自动建议');
      if (F('route-input').value !== '/v1/svc/usr-basic-info') throw new Error('地址未自动建议：' + F('route-input').value);
      return '建议/默认版本正确';
    });

    await assert('step2-versions', '② 版本行可见（线上徽标＋行数/对账/数据日）', async function () {
      click('next-1'); await tick(); await tick();
      if (visibleSteps().join(',') !== 'step-2') throw new Error('可见=' + visibleSteps().join(','));
      var rows = doc.querySelectorAll('.ver-row');
      if (rows.length !== 2) throw new Error('版本行应 2 行，实=' + rows.length);
      if (rows[1].textContent.indexOf('线上') < 0) throw new Error('缺线上徽标');
      if (rows[0].textContent.indexOf('尚未设为线上') < 0) throw new Error('最新非线上缺标注');
      return '2 版本行渲染正确';
    });

    await assert('step2-nonlive-hint', '② 选非线上版→「调用方仍读线上版本」提示＋设为线上入口', async function () {
      var radio = F('ver-20260929');
      if (!radio) throw new Error('缺 20260929 版本单选');
      radio.click(); await tick(); await tick();
      if (comp.pickedVersion !== '20260929') throw new Error('未切到非线上版');
      if (!vis('live-switch-row')) throw new Error('非线上提示行不可见');
      if (txt('live-switch-row').indexOf('调用方仍读线上版本') < 0) throw new Error('缺提示语');
      if (!F('switch-btn')) throw new Error('缺「设为线上」按钮');
      return '非线上提示＋切换入口在位';
    });

    await assert('step2-switch', '② 设为线上（sync 三闸挡板）→已切换提示', async function () {
      click('switch-btn');
      var done = await until(function () { return comp.switched === true; }, 4000);
      if (!done) throw new Error('切换未完成：' + (comp.switchError || '无返回'));
      await until(function () { return vis('switched-note'); }, 2000);
      return '切换成功（调用方读 20260929）';
    });

    await assert('step3-route-check', '③ 地址机检：下划线→红框人话；恢复合规', async function () {
      click('next-2'); await tick(); await tick();
      var input = F('route-input');
      input.value = '/v1/svc/usr_basic_info';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await tick();
      if (!comp.routeError) throw new Error('机检未拦截下划线');
      input.value = '/v1/svc/usr-basic-info';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      await tick();
      if (comp.routeError) throw new Error('合规地址仍报错：' + comp.routeError);
      return '机检拦 R1、放行合规';
    });

    await assert('step4-schema', '④ 字段清单来自 schema（12 项，中文名＋类型可见）', async function () {
      click('next-3'); await tick();
      await until(function () { return comp.schemaFields.length === 12; }, 4000);
      var row = doc.getElementById('field-check-gender_cd');
      if (!row) throw new Error('缺 gender_cd 勾选行');
      var tr = row.closest('.field-row');
      if (tr.textContent.indexOf('性别代码') < 0) throw new Error('缺中文名');
      if (tr.textContent.indexOf('character varying') < 0) throw new Error('缺类型');
      if (txt('checked-count').indexOf('12 / 12') < 0) throw new Error('默认应全选：' + txt('checked-count'));
      return '12 字段（含类型/中文名）默认全选';
    });

    await assert('step4-uncheck', '④ 取消一个字段→计数 11/12，下一步仍可走', async function () {
      var row = doc.getElementById('field-check-blacklist_flag');
      if (!row) throw new Error('缺 blacklist_flag');
      row.click(); await tick();
      if (comp.checkedCount() !== 11) throw new Error('计数应 11，实=' + comp.checkedCount());
      if (F('next-4').disabled) throw new Error('11 个字段不应禁用下一步');
      return '11 / 12';
    });

    await assert('step5-publish', '⑤ 发布→完成页（名称/地址/数据版本回显）', async function () {
      click('next-4'); await tick(); await tick();
      if (visibleSteps().join(',') !== 'step-5') throw new Error('可见=' + visibleSteps().join(','));
      click('publish-btn');
      var ok = await until(function () { return comp.done === true; }, 5000);
      if (!ok) throw new Error('发布未完成：' + (comp.publishError || '无返回'));
      if (txt('done-name').indexOf('用户基本信息') < 0) throw new Error('完成页缺服务名');
      if (txt('done-route') !== '/v1/svc/usr-basic-info') throw new Error('完成页缺地址');
      if (txt('done-data-version').indexOf('20260929') < 0) throw new Error('完成页缺数据版本：' + txt('done-data-version'));
      return 'action=' + comp.publishedAction + '，dataVersion=' + comp.publishedDataVersion;
    });

    await assert('done-curl', '完成页 curl 样例带 apikey 头（SQ-11）', async function () {
      var curl = txt('curl-block');
      if (curl.indexOf("apikey: <调用凭证>") < 0) throw new Error('curl 缺 apikey 头');
      if (curl.indexOf('/v1/svc/usr-basic-info') < 0) throw new Error('curl 缺地址');
      return 'curl 复制即用';
    });

    await assert('done-t10', '完成页 T10 引导：①授权登记（先）→②签发凭证（后）→③403 提示', async function () {
      var g = txt('auth-guide');
      if (g.indexOf('授权登记（先做）') < 0) throw new Error('缺①授权登记');
      if (g.indexOf('签发调用凭证（后做）') < 0) throw new Error('缺②签发凭证');
      if (g.indexOf('不会回补') < 0) throw new Error('缺顺序因果说明');
      if (g.indexOf('403') < 0) throw new Error('缺③403 提示');
      if (g.indexOf('auth_admin.py register') < 0) throw new Error('缺登记 CLI 样例');
      return 'T10 三段引导（SQ-07 顺序）';
    });

    await assert('services-table', '服务表＝数据版本列（tabver），零服务修订号渲染', async function () {
      var head = txt('col-svc-version');
      if (head !== '数据版本') throw new Error('列头应＝数据版本，实=' + head);
      var body = txt('service-tbody');
      if (body.indexOf('20260929') < 0 && body.indexOf('20260930') < 0) throw new Error('缺数据版本值');
      var cleaned = body.replace(/20\d{6}/g, '');
      if (/[\s>]?[vV]\d[\s<]/.test(cleaned)) throw new Error('疑似服务修订号渲染');
      return '数据版本列在位';
    });

    await assert('offline-copy', '下线两段式确认含 E 卡文案（数据保留，可随时重新发布）', async function () {
      var btn = doc.querySelector('[data-action="offline"]');
      if (!btn) throw new Error('缺下线按钮');
      btn.click(); await tick();
      if (!comp.offlineModal.open) throw new Error('弹窗未开');
      var d = txt('offline-modal-detail');
      if (d.indexOf('数据保留，可随时重新发布') < 0) throw new Error('弹窗缺 E 卡文案');
      comp.offlineModal.open = false;
      return 'E 卡文案在位';
    });

    await assert('jargon-scan', '文案扫描零命中（含服务修订号禁令）', async function () {
      var text = (doc.querySelector('main') || doc.body).textContent;
      var hit = JARGON.filter(function (re) { return re.test(text); });
      if (hit.length) throw new Error('命中：' + hit.map(String).join(' | '));
      return JARGON.length + ' 类零命中';
    });

    renderSummary();
    document.documentElement.setAttribute('data-selftest-done', '1');
  }

  main().catch(function (e) {
    renderSummary();
    // #36：异常 message 动态串——escapeHtml（esc 为本文件 IIFE 内已定义的基线函数）
    var esc2 = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };
    $('summary').innerHTML += '<br><span class="fail">运行器异常：' + esc2(String(e && e.message || e)) + '</span>';
    document.documentElement.setAttribute('data-selftest', 'red');
    document.documentElement.setAttribute('data-selftest-done', '1');
  });
})();
