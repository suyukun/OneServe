/* ============================================================
 * OneServe 门户 · 审计查询页组件（P1-1 · assets/audit.js，新增文件）
 * 纪律（同 my.js）：Alpine.data 注册；跨页传参只用 URL query
 * （audit.html?trace_id=… / ?caller=… 深链直达）；取数直达
 * /v1/portal/audit/logs；失败进 error 态（重试），不做 mock 降级——
 * 审计面是台账真源，演示数字只会误导。
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

  function auditQueryPage() {
    return {
      /* ── 筛选状态（range 快捷窗＋custom 自定义时间；其余多条件 AND） ── */
      range: 'today',            // today | 24h | 7d | all | custom
      tsFromInput: '', tsToInput: '',   // datetime-local（custom 生效）
      callerInput: '', endpointInput: '', statusInput: '', traceInput: '',
      limitSel: 20,

      /* ── 结果状态 ── */
      state: 'idle',             // idle | loading | ok | error
      errMsg: '',
      items: [], total: 0, offset: 0,
      summary: null, generatedAt: '',

      init: function () {
        var q = new URLSearchParams(location.search);
        var tid = (q.get('trace_id') || '').trim();
        var caller = (q.get('caller') || '').trim();
        var ep = (q.get('endpoint') || '').trim();
        var st = (q.get('status') || '').trim();
        var from = (q.get('ts_from') || '').trim();
        var to = (q.get('ts_to') || '').trim();
        var lim = parseInt(q.get('limit') || '', 10);
        var off = parseInt(q.get('offset') || '', 10);
        if (tid) { this.traceInput = tid; this.range = 'all'; }
        if (caller) { this.callerInput = caller; }
        if (ep) { this.endpointInput = ep; }
        if (st) { this.statusInput = st; }
        if (from || to) { this.range = 'custom'; this.tsFromInput = from.slice(0, 16); this.tsToInput = to.slice(0, 16); }
        if (lim === 20 || lim === 50 || lim === 100) { this.limitSel = lim; }
        this.query(isNaN(off) || off < 0 ? 0 : off, true);
      },

      /* ── 快捷时间窗 → [from, to]（本地时区转 ISO；all＝不给时间条件） ── */
      rangeBounds: function () {
        var now = new Date();
        if (this.range === 'all') return [null, null];
        if (this.range === 'custom') {
          var f = this.tsFromInput ? new Date(this.tsFromInput) : null;
          var t = this.tsToInput ? new Date(this.tsToInput) : null;
          return [isNaN(f) ? null : f, isNaN(t) ? null : t];
        }
        if (this.range === '24h') return [new Date(now.getTime() - 24 * 3600 * 1000), now];
        if (this.range === '7d') return [new Date(now.getTime() - 7 * 24 * 3600 * 1000), now];
        var d = new Date(now); d.setHours(0, 0, 0, 0);   // today：本地当日 0 点起
        return [d, now];
      },

      rangeLabel: function () {
        return { today: '今日', '24h': '近 24 小时', '7d': '近 7 天', all: '全部时间', custom: '自定义时间' }[this.range] || '';
      },

      setRange: function (r) {
        this.range = r;
        this.query(0);
      },

      onCustomChange: function () {
        this.range = 'custom';
        this.query(0);
      },

      /* ── 查询（offset 翻页共用；keepOffset＝init 深链恢复） ── */
      query: function (offset, silentUrl) {
        var self = this;
        this.state = 'loading'; this.errMsg = '';
        var bounds = this.rangeBounds();
        var qs = new URLSearchParams();
        if (bounds[0]) qs.set('ts_from', bounds[0].toISOString());
        if (bounds[1]) qs.set('ts_to', bounds[1].toISOString());
        var c = this.callerInput.trim(); if (c) qs.set('caller', c);
        var e = this.endpointInput.trim(); if (e) qs.set('endpoint', e);
        var s = this.statusInput; if (s) qs.set('status', s);
        var t = this.traceInput.trim(); if (t) qs.set('trace_id', t);
        qs.set('limit', String(this.limitSel));
        qs.set('offset', String(offset));
        fetchJson('/v1/portal/audit/logs?' + qs.toString())
          .then(function (j) {
            self.items = j.items || [];
            self.total = j.total || 0;
            self.offset = j.offset || 0;
            self.summary = j.summary || null;
            self.generatedAt = j.generatedAt || '';
            self.state = 'ok';
            self.syncUrl(qs, silentUrl);
          })
          .catch(function (err) {
            self.state = 'error';
            self.errMsg = (err && err.message) || '审计查询暂不可用';
          });
      },

      prevPage: function () { if (this.offset > 0) this.query(Math.max(0, this.offset - this.limitSel)); },
      nextPage: function () {
        if (this.offset + this.limitSel < this.total) this.query(this.offset + this.limitSel);
      },
      pageText: function () {
        if (!this.total) return '共 0 条';
        var a = this.offset + 1, b = Math.min(this.offset + this.items.length, this.total);
        return '第 ' + a + '–' + b + ' 条 · 共 ' + this.total + ' 条';
      },

      /* ── traceId 反查：填入过滤框就地精确定位（不清其他条件，AND 语义） ── */
      useTrace: function (tid) {
        if (!tid) return;
        this.traceInput = tid;
        this.query(0);
      },
      clearTrace: function () {
        this.traceInput = '';
        this.query(0);
      },
      resetAll: function () {
        this.range = 'today'; this.tsFromInput = ''; this.tsToInput = '';
        this.callerInput = ''; this.endpointInput = ''; this.statusInput = ''; this.traceInput = '';
        this.query(0);
      },

      /* ── 展示辅助 ── */
      fmtTs: function (ts) {
        if (!ts) return '—';
        var d = new Date(ts);
        if (isNaN(d.getTime())) return String(ts);
        function p(n) { return (n < 10 ? '0' : '') + n; }
        return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
               ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
      },
      statusClass: function (st) {
        if (st == null) return 'text-bg-secondary';
        if (st >= 200 && st < 300) return 'text-bg-success';
        if (st >= 400 && st < 500) return 'text-bg-warning';
        return 'text-bg-danger';
      },
      latClass: function (ms) {
        if (ms == null) return '';
        return ms > 200 ? 'audit-lat-slow' : '';
      },
      digestShort: function (d) {
        return d ? d.slice(0, 8) + '…' : '—';
      },
      rateText: function () {
        if (!this.summary) return '—';
        return this.summary.successRate == null ? '样本量不足' : this.summary.successRate + '%';
      },
      ratePending: function () {
        return !this.summary || this.summary.successRate == null;
      },

      /* ── 深链同步：查询条件写回 URL（可刷新/可分享），不产生历史 ── */
      syncUrl: function (qs, silent) {
        if (silent) return;
        history.replaceState(null, '', location.pathname + '?' + qs.toString());
      }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('auditQueryPage', auditQueryPage);
  });
})();
