/* ============================================================
 * OneServe 运营控制台 · 授权管理逻辑（grants.js，Alpine 组件；1b-8 一期）
 * 设计依据：docs/design/授权管理页设计_v1.md（已拍板·按此执行）
 *
 * 【走位】两级一流：租户列表 → 租户详情（服务∪整路由代理混排授权清单）→ 编辑流。
 *   · 列表：搜索＋状态过滤；行＝租户（状态/类型徽标、在用凭证数、在册授权数）；
 *   · 详情：租户卡（状态、类型、开通审批单号；沙箱整卡只读）＋授权清单
 *     （kind 徽标区分「字段服务／整路由」；行侧审批单号、生效区间、行状态）；
 *     名下凭证级授权只读摘要（凭证编辑归 AppKey 发放页，不在本页）；
 *   · 编辑流（拍板·混合形态）：新增走三步向导（①选服务→②字段勾选（整路由无字段列）
 *     →③预览确认）；变更/停用走行内直编＋确认弹窗；两态均强制变更预览；
 *   · 停用两段式：第一段出变更预览，第二段勾选确认开关才执行（服务端 confirm 字段
 *     双保险——缺确认一律 400 拒执行）；停用行保留可溯源、可再启用；
 *   · 后端不可达时降级演示数据（admin/mock/grants.json）并挂「演示数据」角标，
 *     演示模式下登记仅页面内存演示、如实标注，不伪造真实登记。
 *
 * 【口径文案】审批已在 OA 完成，此处仅登记结果（ADR-0003）；平台不做任何 OA 系统
 *   对接——单号只做格式提醒，真伪不核对。写操作仅管理员（服务端角色闸裁决）；
 *   只读详情全角色可达（viewer 地板）。
 *
 * 【可测性】纯函数挂 window.G8（selftest-grants 直调）；Alpine 组件只做编排。
 * ============================================================ */
(function (root) {
  'use strict';

  /* ── 后端端点（门户 nginx /v1/admin/ 反代到服务层） ── */
  var API = {
    list: '/v1/admin/grants/tenants',
    detail: function (tenant) { return '/v1/admin/grants/tenants/' + encodeURIComponent(tenant); },
    write: '/v1/admin/grants'
  };

  var STATUS_LABELS = { active: '在册', frozen: '已冻结', terminating: '停用中', cancelled: '已注销' };
  var TYPE_LABELS = { internal: '内部', external: '外部', sandbox: '沙箱' };
  var OA_TICKET_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$/;   // 与服务端同源（格式提醒，非真伪核对）
  var DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  /* ── 纯函数（window.G8：selftest 断言面） ── */
  function statusLabel(s) { return STATUS_LABELS[s] || s || '—'; }
  function statusBadgeClass(s) {
    return { active: 'badge-active', frozen: 'badge-warn', terminating: 'badge-warn', cancelled: 'badge-danger' }[s] || 'badge-muted';
  }
  function typeLabel(t) { return TYPE_LABELS[t] || t || '—'; }
  function typeBadgeClass(t) {
    return { internal: 'badge-plain', external: 'badge-plain', sandbox: 'badge-sandbox' }[t] || 'badge-plain';
  }
  function normalizeFields(raw) {
    /* 接受两种形态：字段名数组（视图数据）／勾选映射对象（表单 x-model），输出去重数组 */
    var out = [], seen = {};
    function add(name) {
      name = String(name == null ? '' : name).trim();
      if (name && !seen[name]) { seen[name] = true; out.push(name); }
    }
    if (Array.isArray(raw)) raw.forEach(add);
    else if (raw && typeof raw === 'object') Object.keys(raw).forEach(function (k) { if (raw[k]) add(k); });
    return out;
  }
  function emptyEditModel() {
    /* 视觉评审R1(2026-10-21)：行内编辑输入的 x-model 绑定常驻求值（x-show 只藏不卸），
       editModel 为 null 时每次重渲染即抛 TypeError 触发页底「JS错误」红条——恒持空模型，不置 null */
    return { fields: {}, oaTicket: '', effectiveFrom: '', effectiveTo: '' };
  }
  /* 变更预览（diff）：新增/移除字段＋单号/区间变化——两态强制预览的共用底座 */
  function computeDiff(before, after) {
    before = before || {}; after = after || {};
    var b = normalizeFields(before.fields), a = normalizeFields(after.fields);
    function has(v) { return v != null && String(v) !== ''; }
    return {
      addedFields: a.filter(function (f) { return b.indexOf(f) < 0; }),
      removedFields: b.filter(function (f) { return a.indexOf(f) < 0; }),
      oaChanged: has(after.oaTicket) && after.oaTicket !== (before.oaTicket || ''),
      rangeChanged: (after.effectiveFrom || '') !== (before.effectiveFrom || '') ||
                    (after.effectiveTo || '') !== (before.effectiveTo || ''),
      newService: !before.service && !!after.service
    };
  }
  function diffSummary(d) {
    if (!d) return '';
    var parts = [];
    if (d.newService) parts.push('新增服务登记');
    if (d.addedFields.length) parts.push('新增字段 ' + d.addedFields.length + ' 个');
    if (d.removedFields.length) parts.push('移除字段 ' + d.removedFields.length + ' 个');
    if (d.oaChanged) parts.push('登记审批单号');
    if (d.rangeChanged) parts.push('调整生效区间');
    return parts.length ? parts.join('；') : '无实质变更';
  }
  function effectiveText(from, to) {
    if (!from && !to) return '长期有效';
    return (from || '…') + ' ~ ' + (to || '长期');
  }
  function ticketFormatHint(ticket) {
    ticket = String(ticket || '').trim();
    if (!ticket) return '';                       // 空单号由「必填」口径拦截（沙箱豁免）
    return OA_TICKET_RE.test(ticket) ? '' : '格式提醒：审批单号一般为 4~64 位字母数字或短横线（平台只登记，不做系统核对）';
  }
  function dateHint(d) {
    d = String(d || '').trim();
    if (!d) return '';
    return DATE_RE.test(d) ? '' : '日期格式：YYYY-MM-DD（留空＝长期有效）';
  }
  /* 向导步内校验：每步意图交互后才亮（错误就近，不先亮红字） */
  function wizardStepValid(step, form, svc) {
    if (step === 1) return !!svc;
    if (step === 2) {
      if (!svc) return false;
      return svc.kind === 'proxy' ? true : normalizeFields(form.fields).length > 0;
    }
    return true;
  }
  /* 提交禁用判定：非沙箱缺审批单号＝禁用（验收口径）；日期/单号格式未过＝禁用 */
  function submitDisabled(form, svc, readonly) {
    if (readonly || !svc) return true;
    if (svc.kind !== 'proxy' && normalizeFields(form.fields).length === 0) return true;
    if (!String(form.oaTicket || '').trim()) return true;      // 审批单号必填（此处仅登记结果）
    if (ticketFormatHint(form.oaTicket)) return true;
    if (dateHint(form.effectiveFrom) || dateHint(form.effectiveTo)) return true;
    if (form.effectiveFrom && form.effectiveTo && form.effectiveFrom > form.effectiveTo) return true;
    return false;
  }
  function relTime(iso) {
    if (!iso) return '—';
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString('zh-CN', { hour12: false });
  }

  /* ── Alpine 组件（编排层） ── */
  function grantsAdmin() {
    return {
      /* ── 状态 ── */
      view: 'list', tenants: [], detail: null, source: 'live',
      search: '', statusFilter: 'all', busy: false, lastTrace: '', pageError: '',
      mockDetails: null,
      /* 行内编辑（变更/停用直编） */
      editIndex: -1, editModel: emptyEditModel(), editError: '',
      confirmModal: { open: false, stage: 1, kind: '', index: -1, title: '', detail: '', diff: null, armed: false, hint: '', pending: null },
      /* 新增三步向导 */
      wizard: { open: false, step: 1, touched: false, service: '', fields: {}, oaTicket: '', effectiveFrom: '', effectiveTo: '', err: '' },

      async boot() {
        var live = await this.loadLive();
        if (!live) {
          var stored = await this.loadJson('mock/grants.json');
          this.tenants = (stored && stored.tenants) || [];
          this.mockDetails = (stored && stored.details) || {};
          this.source = 'demo';
        }
      },
      loadJson(path) {
        return fetch(path, { cache: 'no-store' })
          .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
          .catch(function () { return null; });
      },
      apiFetch(url, opts) {
        var headers = Object.assign({}, (root.AdminGate && AdminGate.adminHeaders()) || {});
        if (opts && opts.body) headers['Content-Type'] = 'application/json';
        return fetch(url, Object.assign({}, opts || {}, { headers: headers, cache: 'no-store' }))
          .then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (j) {
              if (!r.ok) { var e = new Error(j.message || ('HTTP ' + r.status)); e.status = r.status; e.code = j.error; throw e; }
              return j;
            });
          });
      },
      loadLive() {
        var self = this;
        return this.apiFetch(API.list)
          .then(function (j) { self.tenants = j.tenants || []; self.source = 'live'; return true; })
          .catch(function () { self.source = 'demo'; return false; });
      },

      /* ── 列表 ── */
      filteredTenants() {
        var q = this.search.trim().toLowerCase(), st = this.statusFilter;
        return this.tenants.filter(function (t) {
          if (st !== 'all' && t.status !== st) return false;
          if (!q) return true;
          return String(t.tenant).toLowerCase().indexOf(q) >= 0 ||
                 String(t.displayName || '').toLowerCase().indexOf(q) >= 0;
        });
      },
      statusOptions() {
        var seen = { all: true }, out = [{ value: 'all', label: '全部状态' }];
        this.tenants.forEach(function (t) {
          if (!seen[t.status]) { seen[t.status] = true; out.push({ value: t.status, label: statusLabel(t.status) }); }
        });
        return out;
      },

      /* ── 详情 ── */
      async openDetail(tenant) {
        var self = this;
        this.pageError = '';
        if (this.source === 'live') {
          try {
            this.detail = await this.apiFetch(API.detail(tenant));
            this.view = 'detail';
            return;
          } catch (e) {
            this.pageError = this.humanError(e);
            return;
          }
        }
        var d = this.mockDetails && this.mockDetails[tenant];
        if (!d) { this.pageError = '演示数据中没有该租户的详情'; return; }
        this.detail = JSON.parse(JSON.stringify(d));
        this.view = 'detail';
      },
      backToList() { this.view = 'list'; this.detail = null; this.editIndex = -1; this.editModel = emptyEditModel(); },
      svcOf(sid) {
        var list = (this.detail && this.detail.availableServices) || [];
        return list.filter(function (s) { return s.id === sid; })[0] || null;
      },
      isProxyRow(g) { return g.kind === 'proxy'; },
      rowReadonly(g) { return !!(this.detail && this.detail.readonly) || !!g.readonly; },

      /* ── 写权限（展示层收敛；服务端角色闸是权威裁决）── */
      canWrite() {
        if (!root.AdminGate || !AdminGate.readOidcToken) return true;   // off/演示态：无令牌可用
        var token = AdminGate.readOidcToken();
        if (!token) return true;
        try {
          var part = String(token).split('.')[1] || '';
          var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
          var claims = JSON.parse(decodeURIComponent(escape(atob(b64)))) || {};
          return String(claims.tag || '').toLowerCase() === 'admin';
        } catch (e) { return true; }   // 坏令牌交给服务端裁决，不本地误伤
      },

      /* ── 行内编辑（变更字段集/单号/区间） ── */
      startEdit(i) {
        var g = this.detail.grants[i];
        if (this.rowReadonly(g)) return;
        var model = { fields: {}, oaTicket: g.oaTicket || '', effectiveFrom: g.effectiveFrom || '', effectiveTo: g.effectiveTo || '' };
        (g.fields || []).forEach(function (f) { model.fields[f] = true; });
        this.editIndex = i; this.editModel = model; this.editError = '';
      },
      cancelEdit() { this.editIndex = -1; this.editModel = emptyEditModel(); this.editError = ''; },
      editAvailable(g) { return g.availableFields || []; },
      editDiff() {
        if (this.editIndex < 0) return null;
        var g = this.detail.grants[this.editIndex];
        var fields = Object.keys(this.editModel.fields).filter(function (k) { return this.editModel.fields[k]; }, this);
        return computeDiff({ fields: g.fields, oaTicket: g.oaTicket, effectiveFrom: g.effectiveFrom, effectiveTo: g.effectiveTo },
                           { fields: fields, oaTicket: this.editModel.oaTicket,
                             effectiveFrom: this.editModel.effectiveFrom, effectiveTo: this.editModel.effectiveTo });
      },
      askSaveEdit() {
        if (this.editIndex < 0) return;
        var g = this.detail.grants[this.editIndex];
        if (!this.detail.readonly && g.kind !== 'proxy' && this.editFields().length === 0) {
          this.editError = '至少保留一个字段（整条路由收回请用「停用」）'; return;
        }
        if (ticketFormatHint(this.editModel.oaTicket) && this.editModel.oaTicket) {
          this.editError = ticketFormatHint(this.editModel.oaTicket); return;
        }
        this.openConfirm('save', this.editIndex);
      },
      editFields() {
        var self = this;
        return Object.keys(this.editModel.fields).filter(function (k) { return self.editModel.fields[k]; });
      },

      /* ── 停用/启用（两段式确认） ── */
      askDisable(i) {
        var g = this.detail.grants[i];
        if (this.rowReadonly(g) || g.status !== 'active') return;
        this.openConfirm('disable', i);
      },
      askEnable(i) {
        var g = this.detail.grants[i];
        if (this.rowReadonly(g) || g.status === 'active') return;
        this.openConfirm('enable', i);
      },
      openConfirm(kind, i) {
        var g = this.detail.grants[i];
        var after = kind === 'disable' ? { status: '已停用（调用即时被拒；登记保留可溯源）' }
                  : kind === 'enable' ? { status: '恢复在册生效' }
                  : { fields: this.editFields(), oaTicket: this.editModel.oaTicket,
                      effectiveFrom: this.editModel.effectiveFrom, effectiveTo: this.editModel.effectiveTo };
        var diff = kind === 'save' ? this.editDiff()
                 : computeDiff({ fields: g.fields, oaTicket: g.oaTicket }, { fields: g.fields, oaTicket: g.oaTicket });
        this.confirmModal = {
          open: true, stage: 1, kind: kind, index: i, armed: false, hint: '',
          title: kind === 'disable' ? '停用授权登记' : kind === 'enable' ? '恢复授权登记' : '确认变更授权登记',
          diff: diff, detail: diffSummary(diff), pending: { service: g.service, action: kind === 'save' ? 'upsert' : kind }
        };
      },
      confirmStep() {
        var m = this.confirmModal;
        if (m.stage === 1) { m.stage = 2; return; }
        if (!m.armed) { m.hint = '第二段确认：请先勾选下方确认开关'; return; }
        this.executeConfirm();
      },
      closeConfirm() { this.confirmModal.open = false; this.confirmModal.stage = 1; this.confirmModal.armed = false; },

      /* ── 提交（live＝服务端登记；demo＝页面内存演示） ── */
      async submitPayload(payload, doneLabel) {
        var self = this;
        if (this.source !== 'live') {
          this.applyDemo(payload);
          this.trace('演示数据模式：' + doneLabel + '仅页面演示，未写入真实登记');
          return true;
        }
        try {
          var j = await this.apiFetch(API.write, { method: 'POST', body: JSON.stringify(payload) });
          if (this.detail && payload.tenant === this.detail.tenant && j.grants) this.detail.grants = j.grants;
          this.trace(doneLabel + '完成（即时生效）');
          return true;
        } catch (e) {
          this.trace('登记未完成：' + this.humanError(e));
          if (this.confirmModal.open) this.confirmModal.hint = this.humanError(e);
          return false;
        }
      },
      async executeConfirm() {
        var m = this.confirmModal;
        var g = this.detail.grants[m.index];
        var payload = { action: m.pending.action, tenant: this.detail.tenant, service: g.service };
        if (m.kind === 'save') {
          payload.fields = this.editFields();
          payload.oa_ticket = this.editModel.oaTicket.trim();
          payload.effective_from = this.editModel.effectiveFrom;
          payload.effective_to = this.editModel.effectiveTo;
        }
        if (m.kind === 'disable') payload.confirm = true;    // 两段式：服务端 confirm 字段双保险
        var ok = await this.submitPayload(payload, m.kind === 'disable' ? '停用' : m.kind === 'enable' ? '恢复' : '变更');
        if (ok) { this.closeConfirm(); this.cancelEdit(); if (this.source === 'live') this.loadLive(); }
      },
      applyDemo(payload) {
        var d = this.detail;
        var g = (d.grants || []).filter(function (x) { return x.service === payload.service; })[0];
        if (!g) {
          var svc = this.svcOf(payload.service) || {};
          g = { service: payload.service, serviceName: svc.name || payload.service,
                kind: svc.kind || 'service', kindLabel: svc.kindLabel || '字段服务', route: svc.route || '',
                fields: [], availableFields: svc.fields || [], oaTicket: '', oaEffective: '',
                oaSource: 'none', effectiveFrom: '', effectiveTo: '', status: 'active', readonly: false };
          d.grants.push(g);
        }
        if (payload.action === 'upsert') {
          g.fields = (payload.fields || []).slice();
          if (payload.oa_ticket) { g.oaTicket = payload.oa_ticket; g.oaEffective = payload.oa_ticket; }
          if (payload.effective_from) g.effectiveFrom = payload.effective_from;
          if (payload.effective_to) g.effectiveTo = payload.effective_to;
          g.status = 'active';
        } else if (payload.action === 'disable') { g.status = 'revoked'; }
        else if (payload.action === 'enable') { g.status = 'active'; }
      },

      /* ── 新增三步向导 ── */
      openWizard() {
        if (!this.canWrite()) return;
        this.wizard = { open: true, step: 1, touched: false, service: '', fields: {}, oaTicket: '', effectiveFrom: '', effectiveTo: '', err: '' };
      },
      closeWizard() { this.wizard.open = false; },
      wizardSvc() { return this.svcOf(this.wizard.service); },
      wizardFields() {
        var self = this;
        return Object.keys(this.wizard.fields).filter(function (k) { return self.wizard.fields[k]; });
      },
      wizardTouchedNext() {
        this.wizard.touched = true;
        if (!wizardStepValid(this.wizard.step, this.wizard, this.wizardSvc())) return;
        if (this.wizard.step < 3) { this.wizard.step += 1; this.wizard.touched = false; }
      },
      wizardPrev() { if (this.wizard.step > 1) { this.wizard.step -= 1; this.wizard.touched = false; } },
      wizardErr() {
        if (!this.wizard.touched) return '';
        if (this.wizard.step === 1 && !this.wizardSvc()) return '请选择一个服务';
        if (this.wizard.step === 2) {
          var svc = this.wizardSvc();
          if (svc && svc.kind !== 'proxy' && this.wizardFields().length === 0) return '请至少勾选一个字段';
        }
        return '';
      },
      wizardDiff() {
        var svc = this.wizardSvc();
        return computeDiff({}, { service: this.wizard.service, fields: this.wizardFields(),
                                 oaTicket: this.wizard.oaTicket,
                                 effectiveFrom: this.wizard.effectiveFrom, effectiveTo: this.wizard.effectiveTo });
      },
      wizardDiffText() { return diffSummary(this.wizardDiff()); },
      wizardSubmitDisabled() {
        return submitDisabled(this.wizard, this.wizardSvc(), !!(this.detail && this.detail.readonly));
      },
      async submitWizard() {
        if (this.wizardSubmitDisabled()) return;
        var payload = {
          action: 'upsert', tenant: this.detail.tenant, service: this.wizard.service,
          fields: this.wizardSvc().kind === 'proxy' ? [] : this.wizardFields(),
          oa_ticket: this.wizard.oaTicket.trim(),
          effective_from: this.wizard.effectiveFrom, effective_to: this.wizard.effectiveTo
        };
        var ok = await this.submitPayload(payload, '新增授权登记');
        if (ok) this.closeWizard();
      },

      /* ── 展示辅助 ── */
      trace(msg) { this.lastTrace = msg + ' · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }); },
      humanError(e) {
        var map = {
          tenant_not_registered: '该租户未登记——新租户请先走线下审批登记',
          unknown_service: '服务未注册或已下线，请刷新后重试',
          empty_fields: '至少勾选一个字段',
          field_not_in_dict: '存在不在该服务开放清单内的字段',
          oa_ticket_required: '审批单号必填（审批已在 OA 完成，此处仅登记结果）',
          invalid_oa_ticket: '审批单号格式提醒：4~64 位字母数字或短横线',
          fields_not_granted: '收窄后名下在用凭证会越出授权范围——请先调整凭证或保留登记',
          service_not_granted: '名下在用凭证仍持有该服务授权——请先回收凭证或保留登记',
          invalid_request: e && e.code === 'invalid_request' ? String(e.message || '请求未通过校验') : ''
        };
        return map[e && e.code] !== undefined && map[e && e.code] !== '' ? map[e.code] : String((e && e.message) || '操作未完成，请稍后重试');
      },
      rowStatusText(g) { return g.status === 'active' ? '在册生效' : '已停用'; },
      rowStatusClass(g) { /* 族先例 appkeys「已禁用」＝琥珀 warn；红保留给过期/注销（本页 statusBadgeClass cancelled→danger），停用可逆不复用红 */
        return g.status === 'active' ? 'badge-active' : 'badge-warn'; },
      oaText(g) { return g.oaEffective || '—'; },
      oaSourceText(g) { return g.oaSource === 'grant' ? '本服务单独批' : g.oaSource === 'tenant' ? '租户统一批' : '—'; },
      effText(g) { return effectiveText(g.effectiveFrom, g.effectiveTo); },
      ticketHintLive() { return ticketFormatHint(this.wizard.oaTicket); },
      /* 纯函数面（模板内 G8.* 直调；组件工厂被 Alpine 调用时已就绪） */
      G8: (typeof root.G8 !== 'undefined') ? root.G8 : {},
    };
  }

  /* ── 导出（组件工厂＋纯函数面） ── */
  root.G8 = {
    statusLabel: statusLabel, statusBadgeClass: statusBadgeClass,
    typeLabel: typeLabel, typeBadgeClass: typeBadgeClass,
    normalizeFields: normalizeFields, computeDiff: computeDiff, diffSummary: diffSummary,
    effectiveText: effectiveText, ticketFormatHint: ticketFormatHint, dateHint: dateHint,
    wizardStepValid: wizardStepValid, submitDisabled: submitDisabled,
    OA_TICKET_RE: OA_TICKET_RE, relTime: relTime,
    grantsAdmin: grantsAdmin,
  };
  root.grantsAdmin = grantsAdmin;
})(typeof window !== 'undefined' ? window : globalThis);
