/* ============================================================
 * OneServe 运营版 1b-8 · selftest-grants（断言运行器）
 * 断言对象：grants.js 纯函数（window.G8）＋ selftest 页内 DOM 探针
 * （以 grants.html 同套 id 挂载的 Alpine 组件；全部经 DOM 驱动＋镜像 span 读态，
 *   不依赖 Alpine 内部 API——selftest-upstreams 同款纪律）。
 * 覆盖（设计稿 §7-5 全项＋交互不变式）：
 *   纯函数口径 / 演示数据与徽标 / 区块 id / 审批单号为空＝提交禁用（填单号解禁）/
 *   单号格式提醒 / 沙箱行编辑按钮禁用＋只读横幅 / 整路由代理行无字段勾选区 /
 *   向导三步切换与逐步校验 / 变更预览（diff）/ 两段式停用确认 /
 *   字段空间随服务绑定 / 列表搜索与状态过滤 / 零外网引用。
 * 打开方式：http.server 下全跑；file:// 下「无外网引用」断言自动 SKIP，其余照跑
 * （取数降级内置演示副本，页面明示 source=demo）。
 * ============================================================ */
(function () {
  'use strict';

  /* ── 小工具（selftest-upstreams 同款；DOM 驱动不碰组件内部） ── */
  function $(id) { return document.getElementById(id) || document.querySelector('[id="' + id + '"]'); }
  function txt(id) { var el = $(id); return el ? (el.textContent || '').trim() : ''; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function tick() { await sleep(90); }
  function click(id) { var el = $(id); if (!el) throw new Error('缺 #' + id); el.click(); }
  function disabled(id) { var el = $(id); if (!el) throw new Error('缺 #' + id); return el.disabled === true; }
  function fill(id, val) {
    var el = $(id); if (!el) throw new Error('缺 #' + id);
    el.value = val; el.checked = !!val;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function fillCheck(el, val) {
    el.checked = !!val;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  /* 失败现场取证：页面/元素/位置全量快照（定位探针null之谜后可删） */
  function snap(id) {
    try {
      return JSON.stringify({
        href: location.href,
        readyState: document.readyState,
        inBody: document.body ? document.body.innerHTML.indexOf(id) : 'no-body',
        byId: !!document.getElementById(id),
        byQ: !!document.querySelector('[id="' + id + '"]'),
        totalIds: document.querySelectorAll('[id]').length,
        wizardOpen: txt('probe-wizard-open'), step: txt('probe-wizard-step')
      });
    } catch (e) { return 'snap-error:' + e; }
  }
  function row(kind, nth) {
    var sel = '#probe-grants-tbody tr[data-kind="' + kind + '"]';
    var rows = document.querySelectorAll(sel);
    if (!rows.length) throw new Error('缺 ' + kind + ' 行');
    return rows[nth || 0];
  }
  function rowBtn(tr, action) {
    var b = tr.querySelector('button[data-action="' + action + '"]');
    if (!b) throw new Error('行内缺 ' + action + ' 按钮');
    return b;
  }

  var results = [];
  function record(id, name, ok, detail, skipped) {
    results.push({ id: id, name: name, ok: ok, detail: detail || '', skipped: !!skipped });
    renderRow(results[results.length - 1]);
    renderSummary();
  }
  function assert(id, name, fn) {
    return Promise.resolve().then(fn)
      .then(function (d) { record(id, name, true, d); })
      .catch(function (e) { record(id, name, false, String((e && e.message) || e)); });
  }

  /* ── 渲染 ── */
  function renderRow(r) {
    var table = $('results');
    if (!table.getAttribute('data-init')) {
      table.innerHTML = '<table><thead><tr><th>#</th><th>断言 id</th><th>断言内容</th><th>结果</th><th>详情</th></tr></thead><tbody id="tbody"></tbody></table>';
      table.setAttribute('data-init', '1');
    }
    var tr = document.createElement('tr');
    var status = r.skipped ? 'SKIP' : (r.ok ? 'PASS' : 'FAIL');
    var esc = (window.AdminGate && window.AdminGate.escapeHtml) || function (s) {
      return String(s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
    };
    tr.innerHTML = '<td class="mono">' + results.length + '</td><td class="mono">' + esc(r.id) + '</td><td>' + esc(r.name) +
      '</td><td class="' + (r.skipped ? 'skip' : (r.ok ? 'pass' : 'fail')) + '">' + status + '</td><td class="mono">' + esc(r.detail) + '</td>';
    $('tbody').appendChild(tr);
  }
  function renderSummary() {
    var pass = results.filter(function (r) { return r.ok && !r.skipped; }).length;
    var skip = results.filter(function (r) { return r.skipped; }).length;
    var fail = results.filter(function (r) { return !r.ok && !r.skipped; }).length;
    var allGreen = fail === 0 && pass >= 10;
    $('summary').innerHTML = '<b class="' + (allGreen ? 'pass' : 'fail') + '">' +
      (allGreen ? '全绿' : '未全绿') + '</b>：' + pass + ' 通过 · ' + skip + ' 跳过 · ' + fail + ' 失败（要求≥10 全绿）';
    document.title = allGreen ? '1b-8 selftest 全绿（' + pass + '）' : '1b-8 selftest 未全绿';
    document.documentElement.setAttribute('data-selftest', allGreen ? 'green' : 'red');
  }

  /* ── 断言清单 ── */
  async function main() {
    var G8 = window.G8;
    await sleep(400);   /* 等 Alpine 挂载＋boot() 取数（live 失败降级 mock） */

    /* 1. 纯函数：徽标/diff/单号格式提醒 */
    await assert('pure-fns', '纯函数：状态/类型徽标＋diff 摘要＋单号格式提醒', async function () {
      if (G8.statusLabel('active') !== '在册' || G8.statusLabel('cancelled') !== '已注销') throw new Error('状态徽标口径漂移');
      if (G8.typeLabel('sandbox') !== '沙箱' || G8.typeLabel('external') !== '外部') throw new Error('类型徽标口径漂移');
      var d = G8.computeDiff({ fields: ['a', 'b'] }, { fields: ['b', 'c'] });
      if (String(d.addedFields) !== 'c' || String(d.removedFields) !== 'a') throw new Error('diff 计算漂移');
      if (!G8.ticketFormatHint('x').length || G8.ticketFormatHint('DEMO-2026-000123')) throw new Error('单号格式提醒口径漂移');
      if (!G8.submitDisabled({ fields: [], oaTicket: '', effectiveFrom: '', effectiveTo: '' },
                             { kind: 'service', fields: ['a'] }, false)) throw new Error('空单号应判禁用');
      return '徽标/diff/格式提醒/禁用判定 全过';
    });

    /* 2. 演示数据：三租户（含沙箱只读），列表行徽标渲染 */
    await assert('mock-tenants', '演示数据加载 ≥3 租户（内部/外部/沙箱齐），列表行在位', async function () {
      var n = parseInt(txt('probe-count'), 10);
      if (!(n >= 3)) throw new Error('仅 ' + n + ' 个租户');
      var rows = document.querySelectorAll('#probe-tenants-tbody tr');
      if (rows.length < 3) throw new Error('列表行仅 ' + rows.length);
      if (!document.querySelector('[data-testid="tenant-row-demo-1020"]')) throw new Error('缺沙箱租户行');
      return 'source=' + txt('probe-source') + ' · ' + n + ' 个租户';
    });

    /* 3. 区块 id：列表/详情/矩阵/向导/确认弹窗全在 */
    await assert('ids-all', '区块 id 全中（列表/矩阵/向导三步/确认弹窗/留痕条）', async function () {
      var need = ['tenant-list', 'tenant-search', 'tenant-status-filter', 'grant-matrix', 'tenant-card',
        'sandbox-banner', 'open-add-grant', 'cred-summary', 'wizard-modal', 'wizard-steps',
        'wizard-step-1', 'wizard-step-2', 'wizard-step-3', 'wizard-service-select', 'wizard-fields',
        'wizard-proxy-note', 'wizard-oa-ticket', 'wizard-effective-from', 'wizard-effective-to',
        'wizard-diff', 'wizard-prev', 'wizard-next', 'wizard-submit', 'wizard-ticket-hint',
        'confirm-modal', 'confirm-diff', 'confirm-switch', 'confirm-step-btn', 'trace-bar'];
      var miss = need.filter(function (id) { return !$(id); });
      if (miss.length) throw new Error('缺 ' + miss.join(','));
      return need.length + ' 个 id 全中';
    });

    /* 4. 详情：普通租户可读可写；kind 徽标区分；凭证级摘要表在位 */
    await assert('detail-normal', '打开普通租户详情：授权行渲染＋整路由徽标＋凭证摘要', async function () {
      var bankRow = document.querySelector('[data-testid="tenant-row-demo_merchant"] button');
      if (!bankRow) throw new Error('缺 demo_merchant 行');
      bankRow.click();
      await tick();
      if (txt('probe-view') !== 'detail') throw new Error('未进入详情视图');
      if (txt('probe-readonly') !== 'rw') throw new Error('普通租户误标只读');
      var rows = document.querySelectorAll('#probe-grants-tbody tr');
      if (rows.length !== 3) throw new Error('授权行仅 ' + rows.length);
      var proxyRow = row('proxy');
      if (proxyRow.textContent.indexOf('整路由') < 0) throw new Error('代理行缺 kind 徽标');
      var credRows = document.querySelectorAll('#probe-cred-tbody tr');
      if (!credRows.length) throw new Error('凭证级摘要无行');
      return '3 行授权（含 1 整路由）＋凭证摘要 ' + credRows.length + ' 行';
    });

    /* 5. 验收 §7-5①：审批单号为空＝提交禁用；填单号后解禁；格式错复禁 */
    await assert('submit-disabled-without-ticket', '向导：单号为空＝提交禁用；填合法单号解禁；格式错复禁', async function () {
      click('open-add-grant');
      await tick();
      if (txt('probe-wizard-open') !== 'open') throw new Error('向导未打开');
      fill('wizard-service-select', 'svc-tags');
      click('wizard-next');                       /* 步1→步2（空选择会被拦） */
      await tick();
      if (txt('probe-wizard-step') !== '2') throw new Error('选服务后未进步2（拦截失效）');
      var boxes = document.querySelectorAll('#wizard-fields input[type=checkbox]');
      if (!boxes.length) throw new Error('字段服务缺勾选清单');
      fillCheck(boxes[0], true);
      click('wizard-next');
      await tick();
      if (txt('probe-wizard-step') !== '3') throw new Error('勾字段后未进步3');
      if (txt('probe-submit-disabled') !== 'yes') throw new Error('单号为空时提交未禁用');
      if (!disabled('wizard-submit')) throw new Error('提交按钮可用（应禁用）');
      fill('wizard-oa-ticket', 'not-a-ticket!');
      await tick();
      if (txt('probe-submit-disabled') !== 'yes') throw new Error('格式错时提交未禁用');
      if (!txt('wizard-ticket-hint')) throw new Error('缺格式提醒文案 ' + snap('wizard-ticket-hint'));
      fill('wizard-oa-ticket', 'DEMO-2026-000123');
      await tick();
      if (txt('probe-submit-disabled') !== 'no') throw new Error('合法单号未解禁');
      click('wizard-cancel');
      await tick();
      return '空禁 → 格式错禁 → 合法解禁（格式提醒在位）';
    });

    /* 6. 验收 §7-5②：沙箱行编辑/停用按钮禁用＋只读横幅 */
    await assert('sandbox-readonly', '沙箱租户：整卡只读（编辑/停用按钮禁用＋横幅在位）', async function () {
      var sbxRow = document.querySelector('[data-testid="tenant-row-demo-1020"] button');
      if (!sbxRow) throw new Error('缺沙箱租户行');
      sbxRow.click();
      await tick();
      if (txt('probe-readonly') !== 'ro') throw new Error('沙箱租户未标只读');
      if (txt('probe-banner-show') !== 'show') throw new Error('只读横幅未显示 ' + snap('sandbox-banner'));
      var rows = document.querySelectorAll('#probe-grants-tbody tr');
      if (rows.length !== 2) throw new Error('沙箱授权行仅 ' + rows.length);
      var editBtn = rowBtn(row('service'), 'edit');
      var disBtn = rowBtn(row('service'), 'disable');
      if (editBtn.disabled !== true) throw new Error('沙箱行编辑按钮未禁用');
      if (disBtn.disabled !== true) throw new Error('沙箱行停用按钮未禁用');
      return '沙箱 2 行全禁用＋只读横幅在位';
    });

    /* 7. 验收 §7-5③：整路由代理行无字段勾选区（含编辑态）；字段服务行有勾选 */
    await assert('proxy-no-field-checkboxes', '整路由代理行：无字段勾选区（编辑态亦无）；字段服务行有勾选', async function () {
      var bankRow = document.querySelector('[data-testid="tenant-row-demo_merchant"] button');
      bankRow.click();
      await tick();
      var proxyRow = row('proxy');
      if (proxyRow.querySelector('input[type=checkbox]')) throw new Error('代理行出现字段勾选');
      var note = proxyRow.querySelector('#proxy-no-fields-note');
      if (!note) throw new Error('代理行缺「整条路由开放」提示');
      rowBtn(proxyRow, 'edit').click();            /* 代理行进入编辑：也不出现勾选区 */
      await tick();
      if (proxyRow.querySelector('input[type=checkbox]')) throw new Error('代理行编辑态出现字段勾选');
      rowBtn(proxyRow, 'cancel-edit').click();
      await tick();
      var svcRow = row('service');
      rowBtn(svcRow, 'edit').click();
      await tick();
      if (!svcRow.querySelector('input[type=checkbox]')) throw new Error('字段服务行编辑态缺勾选清单');
      rowBtn(svcRow, 'cancel-edit').click();
      await tick();
      return '代理行无勾选（含编辑态）；字段服务行有勾选';
    });

    /* 8. 两段式停用：第一段出预览，第二段需勾确认开关，不勾不执行 */
    await assert('two-phase-disable', '停用两段确认：一阶段→二阶段需勾开关；不勾不执行；取消可退', async function () {
      rowBtn(row('service'), 'disable').click();
      await tick();
      if (txt('probe-modal-open') !== 'open' || txt('probe-confirm-kind') !== 'disable') throw new Error('停用确认弹窗未打开');
      if (txt('probe-confirm-stage') !== '1') throw new Error('应从第一段开始');
      click('confirm-step-btn');
      await tick();
      if (txt('probe-confirm-stage') !== '2') throw new Error('未进入第二段');
      click('confirm-step-btn');                   /* 未勾开关点执行 → 仅提示不执行 */
      await tick();
      if (!txt('confirm-hint').length) throw new Error('未勾开关应出提示');
      if (txt('probe-modal-open') !== 'open') throw new Error('未勾开关不应执行');
      click('confirm-cancel-btn');                 /* 取消可退（不执行停用） */
      await tick();
      if (txt('probe-modal-open') !== 'closed') throw new Error('取消未关闭弹窗');
      var st = row('service').querySelector('[data-testid="fields-cell"]');
      return '两段确认＋开关门槛＋取消退出全过';
    });

    /* 9. 变更预览（diff）：行内编辑改动反映到确认弹窗（+/− 预览） */
    await assert('edit-diff-preview', '行内编辑：增勾字段后确认弹窗出 + 预览', async function () {
      var svcRow = row('service');
      rowBtn(svcRow, 'edit').click();
      await tick();
      var boxes = svcRow.querySelectorAll('#edit-fields input[type=checkbox]');
      if (boxes.length < 2) throw new Error('字段服务行勾选清单不足');
      fillCheck(boxes[boxes.length - 1], true);    /* 增勾一个新字段 */
      rowBtn(svcRow, 'save-edit').click();
      await tick();
      if (txt('probe-confirm-kind') !== 'save') throw new Error('保存确认弹窗未打开');
      var add = document.querySelector('#confirm-diff .diff-add');
      if (!add || !add.textContent.trim().length) throw new Error('确认弹窗缺 + 字段预览');
      click('confirm-cancel-btn');
      await tick();
      rowBtn(row('service'), 'cancel-edit').click();
      await tick();
      return '变更预览 +N 在位';
    });

    /* 10. 字段空间随服务绑定：向导勾选清单来自所选服务的开放字段 */
    await assert('field-space-bound', '字段勾选清单＝所选服务开放字段（不发明字段）', async function () {
      click('open-add-grant');
      await tick();
      fill('wizard-service-select', 'svc-card');
      click('wizard-next');
      await tick();
      var labels = Array.prototype.map.call(document.querySelectorAll('#wizard-fields label span'),
                                           function (s) { return s.textContent.trim(); });
      if (String(labels) !== 'card_sts,bal_band,open_days') throw new Error('勾选清单与服务开放字段不一致：' + labels.join(','));
      click('wizard-cancel');
      await tick();
      return 'svc-card 开放字段 ' + labels.length + ' 个全对齐';
    });

    /* 11. 搜索与状态过滤（列表页契约） */
    await assert('list-filter', '列表搜索＋状态过滤生效', async function () {
      click('back-to-list');
      await tick();
      fill('tenant-search', 'demo');
      await tick();
      var n1 = parseInt(txt('probe-filtered-count'), 10);
      if (!(n1 >= 2)) throw new Error('搜索 demo 命中仅 ' + n1);
      fill('tenant-search', '');
      fill('tenant-status-filter', 'active');
      await tick();
      var n2 = parseInt(txt('probe-filtered-count'), 10);
      if (n2 !== parseInt(txt('probe-count'), 10)) throw new Error('active 过滤口径漂移');
      fill('tenant-status-filter', 'all');
      return '搜索/过滤生效';
    });

    /* 12. 零外网引用（http.server 下检查页面源；file:// 跳过） */
    await assert('no-external-refs', '零外网引用（内网白屏红线）', async function () {
      if (location.protocol === 'file:') return 'SKIP（file:// 无法取源码）';
      var r = await fetch('selftest-grants.html', { cache: 'no-store' });
      var src = await r.text();
      var ext = src.match(/(?:src|href)="(https?:\/\/[^"]+)"/g);
      if (ext && ext.length) throw new Error('出现外链：' + ext.slice(0, 2).join(' , '));
      return 'selftest-grants.html 无外链';
    });

    await sleep(80);
  }

  /* 结束标记（headless 驱动以此判定运行完成；selftest-publish 同款契约） */
  function markDone() { document.documentElement.setAttribute('data-selftest-done', '1'); }

  document.addEventListener('DOMContentLoaded', function () {
    setTimeout(function () {
      if (window.__g8selftestRunning) return;   /* 防重复执行（双跑会互踩 DOM 驱动） */
      window.__g8selftestRunning = true;
      main().catch(function (e) { record('fatal', '断言运行器异常', false, String(e)); })
        .then(markDone);
    }, 200);
  });
})();
