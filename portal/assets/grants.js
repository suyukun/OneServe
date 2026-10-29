/* ============================================================
 * OneServe 门户 · 授权登记全景页组件（P1-8 · assets/grants.js，新增文件）
 * 纪律（同 audit.js）：Alpine.data 注册；跨页传参只用 URL query
 * （grants.html?tenant=… / ?service=… / ?field=… / ?oa=… 筛选深链直达）；取数直达
 * /v1/portal/grants/*；失败进 error 态（重试），不做 mock 降级——
 * 授权登记是合规真源，演示数字只会误导。
 * 只读：本页不发出任何写请求。
 * ============================================================ */
(function () {
  'use strict';

  var FETCH_TIMEOUT_MS = 8000;

  function fetchJson(url) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
      fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' },
                   cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
        .then(function (res) {
          clearTimeout(timer);
          if (!res.ok) {   // 端点契约：400 带人话 message——输入提示就地展示
            return res.json().catch(function () { return {}; })
              .then(function (j) { throw new Error(j.message || ('HTTP ' + res.status)); });
          }
          return res.json();
        })
        .then(function (json) { resolve(json); })
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  var OA_LABELS = { present: '已登记单号', placeholder: '占位待回填', missing: '缺失' };

  function grantsViewPage() {
    return {
      /* ── 全景状态 ── */
      state: 'loading',               // loading | ok | error
      errMsg: '',
      rows: [], facets: { tenants: [], services: [], fields: [] },
      summary: null, oaTickets: [], caliber: {}, generatedAt: '',

      /* ── #58 配额水电表（2026-10-15）：kid → 用量行（/v1/admin/appkeys/usage 匿名只读，
       * 与「我的配额」同口径：登记限额 × 当日审计真实调用）。取数失败＝quotaUsageState
       * error，两列如实「—」，不造数。 ── */
      quotaByKid: {}, quotaUsageState: 'idle', quotaUsageResetAt: '',   // idle | ok | error

      /* ── 筛选状态 ── */
      tenantFilter: '', serviceFilter: '', fieldFilter: '', oaFilter: 'all',

      init: function () {
        var q = new URLSearchParams(location.search);
        var f = (q.get('field') || '').trim();
        var ten = (q.get('tenant') || '').trim();
        var svc = (q.get('service') || '').trim();
        if (ten) this.tenantFilter = ten;
        if (svc) this.serviceFilter = svc;
        if (f) this.fieldFilter = f;
        this.load();
      },

      load: function () {
        var self = this;
        this.state = 'loading'; this.errMsg = '';
        this.reloadQuotaUsage();          // 配额两列独立取数：全景失败不影响它，反之亦然
        fetchJson('/v1/portal/grants/panorama').then(function (json) {
          self.rows = json.rows || [];
          self.facets = json.facets || { tenants: [], services: [], fields: [] };
          self.summary = json.summary || null;
          self.oaTickets = json.oaTickets || [];
          self.caliber = json.caliber || {};
          self.generatedAt = json.generatedAt || '';
          self.state = 'ok';
        }).catch(function (err) {
          self.state = 'error';
          self.errMsg = (err && err.message) || '授权登记暂不可用';
        });
      },

      /* ── 筛选：全在数据面上按真值过滤（后端已给全表，前端不加工口径） ── */
      visibleRows: function () {
        var ten = this.tenantFilter, svc = this.serviceFilter;
        var f = this.fieldFilter.trim().toLowerCase(), oa = this.oaFilter;
        return this.rows.filter(function (r) {
          if (ten && r.tenant !== ten) return false;
          if (svc && r.service !== svc) return false;
          if (oa !== 'all' && r.oaState !== oa) return false;
          if (f) {
            var hit = r.fields.some(function (x) { return x.toLowerCase().indexOf(f) !== -1; });
            if (!hit) return false;
          }
          return true;
        });
      },
      applyFilter: function () { this.syncUrl(); },
      setOaFilter: function (v) { this.oaFilter = v; this.syncUrl(); },
      resetFilter: function () {
        this.tenantFilter = ''; this.serviceFilter = ''; this.fieldFilter = ''; this.oaFilter = 'all';
        this.syncUrl();
      },
      tableText: function () {
        if (this.state !== 'ok') return '';
        var n = this.visibleRows().length;
        return n === this.rows.length ? '共 ' + this.rows.length + ' 条登记'
                                      : '筛出 ' + n + ' / ' + this.rows.length + ' 条登记';
      },

      /* ── #58 配额两列（2026-10-15）：kid join＋展示辅助（缺值「—」/未登记如实区分） ── */
      reloadQuotaUsage: function () {
        var self = this;
        this.quotaUsageState = 'loading';
        fetchJson('/v1/admin/appkeys/usage').then(function (json) {
          var map = {};
          (json.items || []).forEach(function (it) { if (it && it.kid) map[it.kid] = it; });
          self.quotaByKid = map;
          self.quotaUsageResetAt = json.resetAt || '';
          self.quotaUsageState = 'ok';
        }).catch(function () { self.quotaUsageState = 'error'; });
      },
      quotaOf: function (row) {
        return (row && row.kid && this.quotaByKid[row.kid]) || null;
      },
      fmtNum: function (n) {
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      },
      quotaLimitLabel: function (row) {
        var it = this.quotaOf(row);
        if (!it) return '—';                                   // 凭证不在用量表（如实留白，不填 0）
        return (it.limit === null || it.limit === undefined) ? '未登记' : this.fmtNum(it.limit);
      },
      quotaUsedLabel: function (row) {
        var it = this.quotaOf(row);
        if (!it) return '—';
        return this.fmtNum(it.used || 0);
      },
      quotaExceeded: function (row) {
        var it = this.quotaOf(row);
        return !!(it && it.exceeded);
      },

      /* ── 展示辅助 ── */
      oaLabel: function (st) { return OA_LABELS[st] || st || '—'; },
      oaClass: function (st) {
        if (st === 'present') return 'gr-oa-present';
        if (st === 'placeholder') return 'gr-oa-placeholder';
        return 'gr-oa-missing';
      },
      sourceNote: function () {
        var note = '真源 ' + (this.caliber.source || 'service/auth_matrix.yaml')
             + ' · 生成时间 ' + (this.generatedAt || '—')
             + ' · 本页只读，不提供任何写操作';
        // #58 配额两列口径随行：数据态如实标注，缺失时不冒充有数
        if (this.quotaUsageState === 'ok') {
          note += ' · 配额两列：/v1/admin/appkeys/usage（当日真实调用计数' +
                  (this.quotaUsageResetAt ? '，重置 ' + this.quotaUsageResetAt : '') + '）';
        } else if (this.quotaUsageState === 'error') {
          note += ' · 配额两列暂不可用（用量真值取不到，如实留「—」）';
        }
        return note;
      },

      /* ── 深链同步：筛选条件写回 URL（可刷新/可分享），不产生历史 ── */
      syncUrl: function () {
        var qs = new URLSearchParams();
        if (this.tenantFilter) qs.set('tenant', this.tenantFilter);
        if (this.serviceFilter) qs.set('service', this.serviceFilter);
        if (this.fieldFilter) qs.set('field', this.fieldFilter);
        if (this.oaFilter !== 'all') qs.set('oa', this.oaFilter);
        var s = qs.toString();
        history.replaceState(null, '', location.pathname + (s ? '?' + s : ''));
      }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('grantsViewPage', grantsViewPage);
  });
})();
