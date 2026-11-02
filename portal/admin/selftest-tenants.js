/* ============================================================
 * OneServe 运营版 · selftest-tenants（断言运行器；管理功能群一期 A 节 §1.10-8）
 * 断言对象：tenants.js 纯函数（window.T8）＋ selftest 页内 DOM 探针
 * （以 tenants.html 同套 id 挂载的 Alpine 组件；全部经 DOM 驱动＋镜像 span 读态）。
 * 覆盖（设计稿 §1.10-8 全项＋交互不变式）：
 *   纯函数口径（状态/类型徽标）／ID 格式提醒／单号格式提醒／新建表单校验
 *   （沙箱免单号＝置灰留空、正式缺单号＝提交禁用）／yaml 态新建按钮可用＋
 *   冻结/解冻置灰＋降级文案命中（§1.8）／未登记行无操作按钮（只读徽标＋提示）／
 *   冻结两段确认开关／解冻缺工单号禁提交／零外网引用。
 * 打开方式：http.server 下全跑；file:// 下「无外网引用」断言自动 SKIP，其余照跑。
 * ============================================================ */
(function () {
  'use strict';

  /* ── 小工具（selftest-grants 同款；DOM 驱动不碰组件内部） ── */
  function $(id) { return document.getElementById(id) || document.querySelector('[id="' + id + '"]'); }
  function txt(id) { var el = $(id); return el ? (el.textContent || '').trim() : ''; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function tick() { await sleep(90); }
  function fill(id, val) {
    var el = $(id); if (!el) throw new Error('缺 #' + id);
    el.value = val;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function check(id, val) {
    var el = $(id); if (!el) throw new Error('缺 #' + id);
    el.checked = !!val;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function disabled(id) { var el = $(id); if (!el) throw new Error('缺 #' + id); return el.disabled === true; }

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
    document.title = allGreen ? '租户管理 selftest 全绿（' + pass + '）' : '租户管理 selftest 未全绿';
    document.documentElement.setAttribute('data-selftest', allGreen ? 'green' : 'red');
  }

  /* 测试夹具行（不经网络：直接注组件态，Alpine x-for 自渲染） */
  var FIXTURE = [
    { tenant: 't-fin', displayName: '演示调用方', tenantType: 'external', status: 'active',
      oaTicket: 'DEMO-2026-000001', activeKeys: 2, grantsCount: 1, registered: true, createdAt: null },
    { tenant: 't-frozen', displayName: '冻结样本', tenantType: 'external', status: 'frozen',
      oaTicket: 'DEMO-2026-000002', activeKeys: 0, grantsCount: 1, registered: true, createdAt: null },
    { tenant: 't-sbx', displayName: '演示沙箱', tenantType: 'sandbox', status: 'active',
      oaTicket: '', activeKeys: 1, grantsCount: 0, registered: true, createdAt: null },
    { tenant: 't-ghost', displayName: 't-ghost', tenantType: 'sandbox', status: 'active',
      oaTicket: '', activeKeys: 1, grantsCount: 0, registered: false, createdAt: null }
  ];
  function inject(mode) {
    var comp = window.tenantsAdmin ? document.querySelector('[x-data="tenantsAdmin()"]') : null;
    var rootEl = document.getElementById('dom-probe');
    var data = rootEl._x_dataStack && rootEl._x_dataStack[0];
    if (!data) throw new Error('Alpine 组件未挂载');
    data.mode = mode;
    data.tenants = JSON.parse(JSON.stringify(FIXTURE));
    data.summary = { tenants: 4, byType: { internal: 0, external: 2, sandbox: 2 }, active: 3, frozen: 1, terminating: 0, cancelled: 0 };
    data.loadError = '';
    return data;
  }
  function rowSel(tenant) { return '#probe-tenants-tbody tr[data-testid="tenant-row-' + tenant + '"]'; }

  /* ── 断言清单 ── */
  async function main() {
    var T8 = window.T8;
    await sleep(500);   /* 等 Alpine 挂载＋boot() 取数（离线失败不影响注入断言） */

    /* 1. 纯函数：状态/类型徽标 */
    await assert('pure-fns', '纯函数：状态/类型徽标口径', async function () {
      if (T8.statusLabel('active') !== '在册' || T8.statusLabel('frozen') !== '已冻结') throw new Error('状态徽标口径漂移');
      if (T8.statusLabel('terminating') !== '停用中' || T8.statusLabel('cancelled') !== '已注销') throw new Error('四态口径漂移');
      if (T8.typeLabel('sandbox') !== '沙箱' || T8.typeLabel('external') !== '外部' || T8.typeLabel('internal') !== '内部') throw new Error('类型徽标口径漂移');
      if (T8.statusBadgeClass('frozen') !== 'badge-frozen' || T8.statusBadgeClass('cancelled') !== 'badge-danger') throw new Error('徽标样式漂移');
      return '状态/类型徽标口径在位';
    });

    /* 2. ID 格式提醒（TENANT_ID_RE 同源） */
    await assert('id-hint', '租户 ID 格式提醒：合法空／非法提示', async function () {
      if (T8.tenantIdFormatHint('') !== '') throw new Error('空串应无提示（必填口径拦截）');
      if (T8.tenantIdFormatHint('demo_merchant') !== '') throw new Error('demo_merchant 应合法（容下划线）');
      if (T8.tenantIdFormatHint('Bad ID') === '') throw new Error('大写/空格应提示');
      if (T8.tenantIdFormatHint('-abc') === '') throw new Error('首字符连字符应提示');
      return 'ID 格式提醒口径在位';
    });

    /* 3. 单号格式提醒 */
    await assert('ticket-hint', '单号格式提醒（4~64 位，平台只登记）', async function () {
      if (T8.ticketFormatHint('') !== '') throw new Error('空串应无提示');
      if (T8.ticketFormatHint('DEMO-2026-000123') !== '') throw new Error('合法单号不应提示');
      if (T8.ticketFormatHint('o!') === '') throw new Error('非法单号应提示');
      return '单号提醒口径在位';
    });

    /* 4. 新建表单校验：三负一正 */
    await assert('create-validate', '新建校验：ID 非法／沙箱带单号／正式缺单号＝错；全齐＝空', async function () {
      if (!T8.createFormError({ tenantId: 'Bad!', tenantType: 'external', oaTicket: 'OA-1' })) throw new Error('ID 非法应报错');
      if (!T8.createFormError({ tenantId: 't-ok', tenantType: 'sandbox', oaTicket: 'OA-1' })) throw new Error('沙箱带单号应报错');
      if (!T8.createFormError({ tenantId: 't-ok', tenantType: 'external', oaTicket: '' })) throw new Error('正式缺单号应报错');
      if (T8.createFormError({ tenantId: 't-ok', tenantType: 'sandbox', oaTicket: '' }) !== '') throw new Error('沙箱免单号应通过');
      if (T8.createFormError({ tenantId: 't-ok', tenantType: 'external', oaTicket: 'DEMO-2026-000123' }) !== '') throw new Error('全齐应通过');
      return '新建校验四态在位';
    });

    /* 5. yaml 态：新建按钮可用＋冻结/解冻置灰＋降级文案命中（§1.10-8 核心项） */
    await assert('yaml-degrade', 'yaml 态：新建可用＋冻结/解冻置灰＋降级文案命中', async function () {
      var data = inject('yaml');
      await tick();
      if (txt('probe-banner-show') !== 'show') throw new Error('yaml 横幅未显示');
      if (txt('yaml-banner') !== T8.YAML_LIFECYCLE_COPY) throw new Error('降级文案与服务端同源串不符');
      if (txt('probe-create-disabled') !== 'no') throw new Error('yaml 态新建按钮应可用（拍板①）');
      var row = document.querySelector(rowSel('t-fin'));
      if (!row) throw new Error('缺 t-fin 行');
      var fb = row.querySelector('button[data-action="freeze"]');
      var ub = row.querySelector('button[data-action="unfreeze"]');
      if (!fb || !ub) throw new Error('登记行缺冻结/解冻按钮');
      if (!fb.disabled || !ub.disabled) throw new Error('yaml 态冻结/解冻应置灰');
      return 'yaml 三断言全过（按钮态＋文案）';
    });

    /* 6. db 态：active 行冻结可用、frozen 行解冻可用 */
    await assert('db-buttons', 'db 态：active 行冻结可用／frozen 行解冻可用', async function () {
      var data = inject('db');
      await tick();
      if (txt('probe-banner-show') !== 'hide') throw new Error('db 态不应显示 yaml 横幅');
      var fin = document.querySelector(rowSel('t-fin'));
      if (!fin || fin.querySelector('button[data-action="freeze"]').disabled) throw new Error('active 行冻结应可用');
      var fro = document.querySelector(rowSel('t-frozen'));
      if (!fro || fro.querySelector('button[data-action="unfreeze"]').disabled) throw new Error('frozen 行解冻应可用');
      if (!fro.querySelector('button[data-action="freeze"]').disabled) throw new Error('frozen 行冻结应禁用');
      return 'db 态按钮态在位';
    });

    /* 7. 未登记行：无操作按钮＋提示文案（§1.8） */
    await assert('unregistered-row', '未登记串：只读徽标＋无操作按钮＋迁移工具提示', async function () {
      inject('db');
      await tick();
      var row = document.querySelector(rowSel('t-ghost'));
      if (!row) throw new Error('缺 t-ghost 行');
      if (row.querySelector('button[data-action="freeze"]') || row.querySelector('button[data-action="unfreeze"]')) {
        throw new Error('未登记行不应有生命周期操作按钮');
      }
      if (!txt('probe-unregistered-note') && !row.textContent.indexOf('凭证注册表存在该串') >= 0) {
        /* 提示在行内 x-show 控制下渲染（隐藏态文本仍在 DOM）——直接查行内文本 */
      }
      if (row.textContent.indexOf('凭证注册表存在该串') < 0 && !row.querySelector('[id="probe-unregistered-note"]')) {
        throw new Error('未登记行缺迁移工具提示');
      }
      return '未登记行只读口径在位';
    });

    /* 8. 冻结两段式：第一段出预览，第二段需勾确认开关 */
    await assert('freeze-two-phase', '冻结两段式：stage 切换＋确认开关门', async function () {
      var data = inject('db');
      data.freeze = { open: true, tenant: 't-fin', stage: 1, armed: false,
                      plan: { tenant: 't-fin', impacts: ['名下 active 凭证 2 把将全量摘除', '授权登记与变更冻结'] }, hint: '' };
      await tick();
      if (txt('probe-freeze-stage') !== '1') throw new Error('初始应为第一段');
      if (!txt('freeze-plan')) throw new Error('影响预览为空');
      data.freezeStep();
      await tick();
      if (txt('probe-freeze-stage') !== '2') throw new Error('未进入第二段');
      if (txt('probe-freeze-armed') !== 'no') throw new Error('开关默认未勾');
      check('freeze-switch', true);
      await tick();
      if (txt('probe-freeze-armed') !== 'yes') throw new Error('勾选未生效');
      return '两段式＋确认开关在位';
    });

    /* 9. 解冻：缺工单号禁提交（纯登记留痕口径） */
    await assert('unfreeze-ticket-required', '解冻：缺工单号提交禁用', async function () {
      var data = inject('db');
      data.unfreeze = { open: true, tenant: 't-frozen', ticket: '', err: '' };
      await tick();
      if (txt('probe-unfreeze-disabled') !== 'yes') throw new Error('缺工单号应禁用提交');
      fill('unfreeze-ticket', 'DEMO-2026-000456');
      await tick();
      if (txt('probe-unfreeze-disabled') !== 'no') throw new Error('填工单号后应解禁');
      return '工单号必填口径在位';
    });

    /* 10. 新建抽屉：沙箱选中＝单号栏置灰；类型说明行命中试用集口径 */
    await assert('create-sandbox-grey', '新建：沙箱免单号＝输入置灰＋说明行含试用集口径', async function () {
      var data = inject('yaml');
      data.create = { open: true, tenantId: '', displayName: '', tenantType: 'sandbox', oaTicket: '', remark: '', err: '', ok: '' };
      await tick();
      if (txt('probe-ticket-disabled') !== 'yes') throw new Error('沙箱单号栏应置灰');
      if (T8.TYPE_NOTES.sandbox.indexOf('演示模板') < 0) throw new Error('沙箱说明行缺演示模板口径');
      if (T8.TYPE_NOTES.sandbox.indexOf('不继承') < 0) throw new Error('沙箱说明行缺不继承真实授权口径');
      data.create.tenantType = 'external';
      await tick();
      if (txt('probe-ticket-disabled') !== 'no') throw new Error('外部单号栏应解禁');
      return '沙箱置灰＋说明行在位';
    });

    /* 11. 指标卡锚点＋关键区块 id 在位（§1.2 线框机检） */
    await assert('section-ids', '关键区块 id：tenant-table／metrics／筛选行／分页', async function () {
      inject('db');
      await tick();
      ['tenant-table', 'metrics', 'metric-total', 'metric-active', 'metric-frozen',
       'tenant-search', 'tenant-type-filter', 'tenant-status-filter', 'pager', 'caliber-note',
       'create-modal', 'freeze-modal', 'unfreeze-modal', 'create-btn'].forEach(function (id) {
        if (!$(id)) throw new Error('缺 #' + id);
      });
      if (txt('probe-count') !== '4') throw new Error('夹具行数应为 4');
      return '区块 id 全在位';
    });

    /* 12. 零外网引用（http.server 下检查页面源；file:// 跳过） */
    await assert('no-external', '零外网引用', async function () {
      if (location.protocol === 'file:') return 'file:// 跳过';
      var src = await fetch(location.href).then(function (r) { return r.text(); });
      var ext = src.match(/(?:src|href)="(https?:\/\/[^"]+)"/g);
      if (ext && ext.length) throw new Error('出现外链：' + ext.slice(0, 2).join(' , '));
      return 'selftest-tenants.html 无外链';
    });

    await sleep(80);
  }

  /* 结束标记（headless 驱动以此判定运行完成；selftest-publish 同款契约） */
  function markDone() { document.documentElement.setAttribute('data-selftest-done', '1'); }

  document.addEventListener('DOMContentLoaded', function () {
    setTimeout(function () {
      if (window.__t8selftestRunning) return;   /* 防重复执行（双跑会互踩 DOM 驱动） */
      window.__t8selftestRunning = true;
      main().catch(function (e) { record('fatal', '断言运行器异常', false, String(e)); })
        .then(markDone);
    }, 200);
  });
})();
