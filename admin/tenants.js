/* ============================================================
 * OneServe 运营控制台 · 租户管理逻辑（tenants.js，Alpine 组件；管理功能群一期 A 节）
 * 设计依据：docs/design/管理功能群一期设计_v1.md §1（已拍板·按此执行）
 *
 * 【走位】单页三区：指标卡 → 筛选＋主表 → 新建抽屉／冻结两段确认／解冻工单号弹层。
 *   · 列表：服务端分页（page/page_size，metering_api 信封先例）＋类型/状态/前缀筛选；
 *   · 新建（拍板①）：yaml 态可用＝写矩阵登记行；双不复用（ID 永不改写不复用）；
 *     沙箱免开通单号（表单置灰）＋首签自动挂试用集说明行；
 *   · 冻结两段式：第一段服务端影响预览，第二段勾确认开关才执行；先库后网关；
 *     摘除不完整＝partial 告警条＋「重试摘除」（resync-consumers 幂等对账）；
 *   · 解冻：必填工单号（纯登记留痕，域模型 §4）；
 *   · 双模降级（§1.8）：mode==='yaml' 时冻结/解冻按钮置灰＋横幅文案（与服务端
 *     YAML_LIFECYCLE_COPY 同源串）；db 态全部可用。
 *
 * 【口径文案】审批在 OA，此处只做登记与生命周期管理（ADR-0003）；单号只做格式提醒。
 *   读全角色可达（viewer 地板）；新建/冻结/解冻＝admin（服务端角色闸裁决）。
 *
 * 【可测性】纯函数挂 window.T8（selftest-tenants 直调）；Alpine 组件只做编排。
 * ============================================================ */
(function (root) {
  'use strict';

  /* ── 后端端点（门户 nginx /v1/admin/ 反代到服务层） ── */
  var API = {
    list: '/v1/admin/tenants',
    create: '/v1/admin/tenants',
    freeze: function (tenant) { return '/v1/admin/tenants/' + encodeURIComponent(tenant) + '/freeze'; },
    unfreeze: function (tenant) { return '/v1/admin/tenants/' + encodeURIComponent(tenant) + '/unfreeze'; },
    resync: '/v1/admin/tenants/resync-consumers'
  };

  var STATUS_LABELS = { active: '在册', frozen: '已冻结', terminating: '停用中', cancelled: '已注销' };
  var TYPE_LABELS = { internal: '内部', external: '外部', sandbox: '沙箱' };
  var TENANT_ID_RE = /^[a-z0-9]([a-z0-9_-]{0,62}[a-z0-9])?$/;   // 与服务端 TENANT_ID_RE 同源
  var OA_TICKET_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$/;        // 格式提醒，非真伪核对

  /* §1.8 降级文案（与服务端 tenants_api.YAML_LIFECYCLE_COPY 同串；selftest 断言命中） */
  var YAML_LIFECYCLE_COPY = '当前为 yaml 配置模式（dev 缺省），冻结/解冻需元数据库模式（META_WRITER=db）——yaml 态无生命周期状态源。';
  var UNREGISTERED_COPY = '凭证注册表存在该串但租户未登记实体；补登记归元数据迁移工具（域模型 §7 迁移动作）。';
  var TYPE_NOTES = {
    internal: '内部租户：签约使用主体，开通须 OA 单号。',
    external: '外部租户：外部数据源合作方，开通须 OA 单号。',
    sandbox: '沙箱租户：免开通单号；首次签发 AppKey 时自动挂平台演示模板授权（测试用，正式凭证另行审批），不继承任何真实租户授权。'
  };

  /* ── 纯函数（window.T8：selftest 断言面） ── */
  function typeLabel(t) { return TYPE_LABELS[t] || t || '—'; }
  function typeBadgeClass(t) {
    return { internal: 'badge-plain', external: 'badge-plain', sandbox: 'badge-sandbox' }[t] || 'badge-plain';
  }
  function statusLabel(s) { return STATUS_LABELS[s] || s || '—'; }
  function statusBadgeClass(s) {
    /* frozen＝深琥珀实线（视觉评审④：与「沙箱」类型黄脱钩） */
    return { active: 'badge-active', frozen: 'badge-frozen', terminating: 'badge-warn', cancelled: 'badge-danger' }[s] || 'badge-muted';
  }
  function tenantIdFormatHint(id) {
    id = String(id || '');
    if (!id) return '';                          // 空由「必填」口径拦截
    return TENANT_ID_RE.test(id) ? '' : '格式提醒：小写字母/数字/_-，2~64 位（一经分配永不改写、永不复用）';
  }
  function ticketFormatHint(ticket) {
    ticket = String(ticket || '').trim();
    if (!ticket) return '';                      // 空单号由「必填」口径拦截（沙箱豁免）
    return OA_TICKET_RE.test(ticket) ? '' : '格式提醒：审批单号一般为 4~64 位字母数字或短横线（平台只登记，不做系统核对）';
  }
  /* 新建表单客户端校验（服务端同规则复核——前端提示、后端裁决） */
  function createFormError(form) {
    form = form || {};
    var id = String(form.tenantId || '').trim();
    if (!TENANT_ID_RE.test(id)) return '租户 ID 必填且须为小写字母/数字/_-（2~64 位）';
    var type = String(form.tenantType || '');
    if (['internal', 'external', 'sandbox'].indexOf(type) < 0) return '类型须为 内部/外部/沙箱 三选一';
    var ticket = String(form.oaTicket || '').trim();
    if (type === 'sandbox') {
      if (ticket) return '沙箱租户免开通单号——请留空';
    } else if (!ticket) {
      return '非沙箱租户开通 OA 单号必填（审批已在 OA 完成，此处登记单号）';
    } else if (!OA_TICKET_RE.test(ticket)) {
      return '开通单号格式提醒：4~64 位字母数字或短横线';
    }
    return '';
  }
  function impactsText(plan) {
    return ((plan && plan.impacts) || []).join('；');
  }
  function humanError(e) {
    var map = {
      tenant_yaml_mode: YAML_LIFECYCLE_COPY,
      tenant_frozen: '租户已冻结：写面动作禁止——先解冻（须工单号）再执行',
      tenant_not_found: '租户未登记（矩阵与注册表均无此实体）',
      oa_ticket_required: '工单号/单号必填（平台只登记，不做系统核对）',
      invalid_oa_ticket: '单号格式提醒：4~64 位字母数字或短横线',
      invalid_tenant: '租户 ID 非法（小写字母/数字/_-，2~64 位）',
      invalid_request: e && e.code === 'invalid_request' ? String(e.message || '请求未通过校验') : ''
    };
    return map[e && e.code] !== undefined && map[e && e.code] !== '' ? map[e.code] : String((e && e.message) || '操作未完成，请稍后重试');
  }

  function tenantsAdmin() {
    return {
      /* ── 状态 ── */
      tenants: [], summary: null, mode: '', total: 0, page: 1, pageSize: 50,
      search: '', typeFilter: '', statusFilter: '',
      loadError: '', pageError: '', lastTrace: '', partialInfo: null, caliberOpen: true,
      create: { open: false, tenantId: '', displayName: '', tenantType: '', oaTicket: '', remark: '', err: '', ok: '' },
      freeze: { open: false, tenant: '', stage: 1, armed: false, plan: null, hint: '' },
      unfreeze: { open: false, tenant: '', ticket: '', err: '' },
      T8: (typeof root.T8 !== 'undefined') ? root.T8 : {},
      G8: (typeof root.G8 !== 'undefined') ? root.G8 : {},

      /* ── 角色面（grants.js canWrite 同款：JWT tag 解码） ── */
      canWrite() {
        if (!root.AdminGate || !AdminGate.readOidcToken) return true;   // off/演示态：无令牌可用
        var token = AdminGate.readOidcToken();
        if (!token) return true;
        try {
          var part = String(token).split('.')[1] || '';
          var b64 = part.replace(/-/g, '+').replace(/_/g, '/');
          var claims = JSON.parse(decodeURIComponent(escape(atob(b64))) || '{}');
          return String(claims.tag || '').toLowerCase() === 'admin';
        } catch (e) { return false; }
      },

      /* ── 取数（服务端分页＋筛选） ── */
      async fetchList() {
        var self = this;
        var params = new URLSearchParams();
        params.set('page', String(this.page));
        params.set('page_size', String(this.pageSize));
        if (this.search) params.set('q', this.search);
        if (this.typeFilter) params.set('type', this.typeFilter);
        if (this.statusFilter) params.set('status', this.statusFilter);
        try {
          var resp = await fetch(API.list + '?' + params.toString(), { headers: { 'Accept': 'application/json' } });
          var j = await resp.json();
          if (!resp.ok) { self.loadError = humanError(j); return; }
          self.tenants = j.items || [];
          self.summary = j.summary || null;
          self.mode = j.mode || '';
          self.total = j.total || 0;
          self.loadError = '';
        } catch (e) {
          self.loadError = '服务层不可达：列表加载失败，请稍后重试';
        }
      },
      debouncedFetch() {
        var self = this;
        if (this._t) clearTimeout(this._t);
        this._t = setTimeout(function () { self.page = 1; self.fetchList(); }, 260);
      },
      goPage(p) { this.page = p; this.fetchList(); },
      async boot() {
        await this.fetchList();
        if (this.mode === 'yaml') this.trace('yaml 模式：新建可用（矩阵登记行）；冻结/解冻置灰');
      },

      /* ── 展示辅助 ── */
      trace(msg) { this.lastTrace = msg + ' · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false }); },
      typeNote(t) { return TYPE_NOTES[t] || '请选择类型（内部／外部／沙箱）。'; },
      ticketHintLive() { return this.create.tenantType === 'sandbox' ? '' : ticketFormatHint(this.create.oaTicket); },
      lifecycleButtonTitle(t) {
        if (this.mode !== 'db') return YAML_LIFECYCLE_COPY;
        if (t.status === 'active') return '冻结该租户（两段确认）';
        if (t.status === 'frozen') return '解冻该租户（必填工单号）';
        return '仅 active 可冻结／frozen 可解冻（当前 ' + statusLabel(t.status) + '）';
      },

      /* ── 新建 ── */
      openCreate() {
        if (!this.canWrite()) return;
        this.create = { open: true, tenantId: '', displayName: '', tenantType: '', oaTicket: '', remark: '', err: '', ok: '' };
      },
      closeCreate() { this.create.open = false; },
      createSubmitDisabled() {
        return createFormError(this.create) !== '' || !this.create.tenantType;
      },
      async submitCreate() {
        var err = createFormError(this.create);
        if (err) { this.create.err = err; return; }
        var self = this;
        this.create.err = ''; this.create.ok = '';
        try {
          var resp = await fetch(API.create, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              tenant_id: String(this.create.tenantId || '').trim(),
              display_name: String(this.create.displayName || '').trim(),
              tenant_type: this.create.tenantType,
              oa_ticket: String(this.create.oaTicket || '').trim(),
              remark: String(this.create.remark || '').trim()
            })
          });
          var j = await resp.json();
          if (!resp.ok) { self.create.err = humanError(j); return; }
          self.create.ok = (j.hint || '已登记。') + (j.remarkNote ? '（' + j.remarkNote + '）' : '');
          self.trace('已登记 ' + j.tenant.tenant + '（' + j.mode + ' 态）');
          await self.fetchList();
        } catch (e) {
          self.create.err = '服务层不可达：登记未提交，请稍后重试';
        }
      },

      /* ── 冻结（两段式） ── */
      async askFreeze(t) {
        if (this.mode !== 'db') { this.pageError = YAML_LIFECYCLE_COPY; return; }
        var self = this;
        this.freeze = { open: true, tenant: t.tenant, stage: 1, armed: false, plan: null, hint: '' };
        try {
          var resp = await fetch(API.freeze(t.tenant), {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({})
          });
          var j = await resp.json();
          if (!resp.ok) { self.freeze.hint = humanError(j); return; }
          self.freeze.plan = j.plan || null;
        } catch (e) {
          self.freeze.hint = '服务层不可达：影响预览失败';
        }
      },
      async freezeStep() {
        var self = this;
        if (this.freeze.stage === 1) { this.freeze.stage = 2; return; }
        if (!this.freeze.armed) { this.freeze.hint = '请先勾选确认开关'; return; }
        try {
          var resp = await fetch(API.freeze(this.freeze.tenant), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: true })
          });
          var j = await resp.json();
          if (!resp.ok) { self.freeze.hint = humanError(j); return; }
          self.freeze.open = false;
          if (j.partial) {
            self.partialInfo = { tenant: self.freeze.tenant,
              text: '冻结已提交，但网关摘除不完整（' + (j.failed || []).length + ' 把未摘）——调用面可能未全断，请重试摘除。' };
            self.trace('冻结 ' + j.tenant + ' 部分完成（partial）');
          } else {
            self.partialInfo = null;
            self.trace('已冻结 ' + j.tenant + '（摘除 consumer ' + j.removed + ' 把）');
          }
          await self.fetchList();
        } catch (e) {
          self.freeze.hint = '服务层不可达：冻结未执行';
        }
      },
      closeFreeze() { this.freeze.open = false; },

      /* ── 解冻 ── */
      askUnfreeze(t) {
        if (this.mode !== 'db') { this.pageError = YAML_LIFECYCLE_COPY; return; }
        this.unfreeze = { open: true, tenant: t.tenant, ticket: '', err: '' };
      },
      closeUnfreeze() { this.unfreeze.open = false; },
      async submitUnfreeze() {
        var ticket = String(this.unfreeze.ticket || '').trim();
        if (!ticket) { this.unfreeze.err = '解冻必填工单号（纯登记留痕）'; return; }
        var self = this;
        try {
          var resp = await fetch(API.unfreeze(this.unfreeze.tenant), {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ oa_ticket: ticket })
          });
          var j = await resp.json();
          if (!resp.ok) { self.unfreeze.err = humanError(j); return; }
          self.unfreeze.open = false;
          if (j.partial) {
            self.partialInfo = { tenant: self.unfreeze.tenant,
              text: '解冻已提交，但网关恢复不完整（' + (j.failed || []).length + ' 把未挂）——请重试摘挂。' };
          } else {
            self.partialInfo = null;
            self.trace('已解冻 ' + j.tenant + '（工单 ' + ticket + '，恢复 consumer ' + j.restored + ' 把，配额原样重建）');
          }
          await self.fetchList();
        } catch (e) {
          self.unfreeze.err = '服务层不可达：解冻未执行';
        }
      },

      async refreshList() { await this.fetchList(); this.trace('列表已刷新'); },

      /* 未登记提示小钮：点开口径折叠区（视觉评审③③——明示可点、有真行为） */
      openCaliber() {
        var el = document.getElementById('caliber-note');
        if (el) { el.open = true; el.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
        this.trace('未登记串口径见下方「口径与降级说明」');
      },

      /* ── 幂等对账摘挂（P1-③） ── */
      async resync(tenant) {
        var self = this;
        try {
          var resp = await fetch(API.resync, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(tenant ? { tenant: tenant } : {})
          });
          var j = await resp.json();
          if (!resp.ok) { self.pageError = humanError(j); return; }
          self.partialInfo = j.partial ? self.partialInfo : null;
          self.trace('对账完成：挂 ' + j.ensured + '／摘 ' + j.removed + (j.partial ? '（仍有失败，可重试）' : ''));
          await self.fetchList();
        } catch (e) {
          self.pageError = '服务层不可达：对账未执行';
        }
      }
    };
  }

  /* ── 导出（组件工厂＋纯函数面） ── */
  root.T8 = {
    typeLabel: typeLabel, typeBadgeClass: typeBadgeClass,
    statusLabel: statusLabel, statusBadgeClass: statusBadgeClass,
    TENANT_ID_RE: TENANT_ID_RE, OA_TICKET_RE: OA_TICKET_RE,
    tenantIdFormatHint: tenantIdFormatHint, ticketFormatHint: ticketFormatHint,
    createFormError: createFormError, impactsText: impactsText, humanError: humanError,
    YAML_LIFECYCLE_COPY: YAML_LIFECYCLE_COPY, UNREGISTERED_COPY: UNREGISTERED_COPY,
    TYPE_NOTES: TYPE_NOTES, tenantsAdmin: tenantsAdmin,
  };
  root.tenantsAdmin = tenantsAdmin;
})(typeof window !== 'undefined' ? window : globalThis);
