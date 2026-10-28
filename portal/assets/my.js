/* ============================================================
 * OneServe 门户 · 我的接入页组件（P0-7 · assets/my.js，新增文件）
 * 纪律（同 pages.js）：Alpine.data 注册，HTML 不写内联表达式；
 * 跨页传参只用 URL query（my.html?caller=… / ?trace_id=… 深链直达）。
 * 取数：直达 /v1/portal/my/* 两端点；失败进 error 态（重试），
 * 不做 mock 降级——「我的」视图是用户自己的数据，演示数字只会误导。
 * ============================================================ */
(function () {
  'use strict';

  var FETCH_TIMEOUT_MS = 6000;

  function fetchJson(url) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
      fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' },
                   cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
        .then(function (res) {
          clearTimeout(timer);
          if (!res.ok) {   // 端点契约：400 带人话 message——如实的输入提示也走 error 展示
            return res.json().catch(function () { return {}; })
              .then(function (j) { throw new Error(j.message || ('HTTP ' + res.status)); });
          }
          return res.json();
        })
        .then(function (json) { resolve(json); })
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  function myAccessPage() {
    return {
      /* ── 状态：凭证块/日志块各自三态（idle=尚未查询） ── */
      callerInput: '',
      callerState: 'idle',            // idle | loading | ok | error
      credState: 'idle',
      credentials: [],
      logsState: 'idle',
      logItems: [],
      logsLimit: 20,
      logsNote: '',
      traceInput: '',
      traceMode: false,
      logsTraceId: '',
      logsQueriedCaller: '',

      /* ── ② 配额状态（P1-3 四要素接真：登记值 × 当日审计真实用量） ── */
      quotaState: 'idle',          // idle | loading | ok | error
      quotaFound: false,
      quotaObj: null,              // 端点 quota 段原样（limit/used/remaining/ratio/trend）
      quotaTrend: [],
      quotaResetAt: '',
      quotaCaliber: null,

      /* ── 用量统计（权限方案 v1.1 裁决#4·2026-10-16）：GET /v1/admin/metering/summary ──
       * 域7 计量查看＝viewer 地板（登录即可见）；viewer 此前「有权无门」，本块即入口。
       * 失败进 error 态（重试），不 mock 降级（计量是真实用量，演示值误导）。 */
      meterState: 'idle',          // idle | loading | ok | error
      meterRows: [],
      meterTotal: null,
      meterGeneratedAt: '',

      init: function () {
        var q = new URLSearchParams(location.search);
        var caller = (q.get('caller') || '').trim();
        var tid = (q.get('trace_id') || '').trim();
        if (tid) {                     // 深链反查（错误体 request_log_url 的落点形态）
          this.traceInput = tid;
          this.lookupTrace();
        }
        if (caller) {
          this.callerInput = caller;
          this.query();
        }
        this.reloadMeter();   // 用量统计（全角色可见，页面加载即取）
      },

      /* ── 用量统计取数：近 7 日计量聚合（掩码行） ── */
      reloadMeter: function () {
        var self = this;
        this.meterState = 'loading';
        fetchJson('/v1/admin/metering/summary?days=7')
          .then(function (j) {
            self.meterRows = (j && j.callers) || [];
            self.meterTotal = (j && j.total) || null;
            self.meterGeneratedAt = (j && j.generatedAt) || '';
            self.meterState = 'ok';
          })
          .catch(function () { self.meterState = 'error'; });
      },
      meterNum: function (n) {
        if (n == null) return '—';
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      },
      meterTotalLabel: function () { return this.meterNum(this.meterTotal && this.meterTotal.calls); },
      meterSuccessLabel: function () { return this.meterNum(this.meterTotal && this.meterTotal.success); },
      meterFailLabel: function () { return this.meterNum(this.meterTotal && this.meterTotal.fail); },

      /* ── 按凭证查询：①凭证/配额＋③日志 两个端点并发 ── */
      query: function () {
        var caller = this.callerInput.trim();
        if (!caller) {
          this.callerState = 'error';
          return;
        }
        this.reloadCreds(caller);
        this.reloadLogs(caller);
        this.reloadQuota(caller);
        this.syncUrl({ caller: caller });
      },

      /* ── ② 配额：GET /v1/portal/my/quota（四要素真值＋近 7 日逐日用量） ──
       * 失败进 error 态（重试），不做 mock 降级——配额是接入方据以安排调用节奏的
       * 数字，演示值只会误导；未知凭证＝found=false，页面如实空态。 */
      reloadQuota: function (caller) {
        var c = (caller || this.callerInput).trim();
        if (!c) return;
        var self = this;
        this.quotaState = 'loading';
        this.quotaFound = false;
        fetchJson('/v1/portal/my/quota?caller=' + encodeURIComponent(c))
          .then(function (j) {
            self.quotaFound = !!j.found;
            self.quotaObj = j.quota || null;
            self.quotaTrend = (j.quota && j.quota.trend) || [];
            self.quotaResetAt = j.resetAt || '';
            self.quotaCaliber = j.caliber || null;
            self.quotaState = 'ok';
          })
          .catch(function () { self.quotaState = 'error'; });
      },

      reloadCreds: function (caller) {
        var c = (caller || this.callerInput).trim();
        if (!c) return;
        var self = this;
        this.callerState = 'loading'; this.credState = 'loading';
        fetchJson('/v1/portal/my/credentials?caller=' + encodeURIComponent(c))
          .then(function (j) {
            self.credentials = j.credentials || [];
            self.callerState = 'ok'; self.credState = 'ok';
          })
          .catch(function () { self.callerState = 'error'; self.credState = 'error'; });
      },

      reloadLogs: function (caller) {
        var c = (caller || this.callerInput).trim();
        if (!c && !this.traceMode) return;
        var self = this;
        this.logsState = 'loading';
        if (c) this.logsQueriedCaller = c;
        var url = '/v1/portal/my/logs?caller=' + encodeURIComponent(c) +
                  '&limit=' + this.logsLimit;
        fetchJson(url)
          .then(function (j) {
            self.logItems = j.items || [];
            self.logsNote = j.gwRejectedNote || '';
            self.logsState = 'ok';
          })
          .catch(function () { self.logsState = 'error'; });
      },

      /* ── 追踪号反查：查审计＋网关拒记两处（含别人被拒的记录＝反查本义） ── */
      lookupTrace: function () {
        var tid = this.traceInput.trim();
        if (!tid) return;
        var self = this;
        this.traceMode = true;
        this.logsTraceId = tid;
        this.logsState = 'loading';
        fetchJson('/v1/portal/my/logs?trace_id=' + encodeURIComponent(tid) +
                  '&limit=' + this.logsLimit)
          .then(function (j) {
            self.logItems = j.items || [];
            self.logsNote = j.gwRejectedNote || '';
            self.logsState = 'ok';
          })
          .catch(function () { self.logsState = 'error'; });
        this.syncUrl({ trace_id: tid });
      },

      clearTrace: function () {
        this.traceMode = false;
        this.traceInput = '';
        this.logsTraceId = '';
        if (this.logsQueriedCaller) { this.reloadLogs(this.logsQueriedCaller); }
        else { this.logsState = 'idle'; this.logItems = []; }
        this.syncUrl({ trace_id: null });
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
      credStatusClass: function (c) {
        if (c.status === 'active') return 'text-bg-success';
        if (c.status === 'disabled') return 'text-bg-secondary';
        return 'text-bg-danger';
      },
      credStatusLabel: function (c) {
        if (c.status === 'active') return '启用中';
        if (c.status === 'disabled') return '已停用';
        if (c.status === 'expired') return '已过期';
        return '状态未知';
      },
      /* 配额展示辅助（数字一律取自端点真值；缺值显示「—」，不填 0 冒充） */
      quotaNum: function (v) {
        return (v === null || v === undefined) ? null : v;
      },
      quotaLimit: function () { return this.quotaObj ? this.quotaObj.limit : null; },
      quotaUsed: function () { return this.quotaObj ? this.quotaObj.used : null; },
      quotaRemaining: function () { return this.quotaObj ? this.quotaObj.remaining : null; },
      quotaRatio: function () { return this.quotaObj ? this.quotaObj.ratio : null; },
      quotaExceeded: function () { return !!(this.quotaObj && this.quotaObj.exceeded); },
      quotaLimitLabel: function () {
        var n = this.quotaLimit();
        if (n === null) return '未登记';                 // 未登记 ≠ 0 次：如实区分
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      },
      quotaUsedLabel: function () {
        var n = this.quotaUsed();
        if (n === null) return '—';
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      },
      quotaRemainingLabel: function () {
        var n = this.quotaRemaining();
        if (n === null) return '—';
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      },
      /* #58 水电表（2026-10-15）：进度条图例用——已用占比带 % 号（端点 ratio＝百分数） */
      quotaRatioLabel: function () {
        var r = this.quotaRatio();
        return r === null || r === undefined ? '—' : r + '%';
      },
      /* 重置时间：端点给东八区次日 00:00 的 ISO，页面按本地时区显示「MM-DD HH:mm」 */
      quotaResetLabel: function () {
        if (!this.quotaResetAt) return '—';
        var d = new Date(this.quotaResetAt);
        if (isNaN(d.getTime())) return String(this.quotaResetAt);
        function p(x) { return ('0' + x).slice(-2); }
        return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
      },
      sparkHeight: function (used) {
        var peak = 0;
        for (var i = 0; i < this.quotaTrend.length; i++) {
          if (this.quotaTrend[i].used > peak) peak = this.quotaTrend[i].used;
        }
        var pct = peak > 0 ? Math.round(used * 100 / peak) : 0;
        return 'height:' + Math.max(2, pct) + '%';       // 有量才起柱；0 次留底线
      },
      sparkDay: function (date) { return String(date || '').slice(5); },   // MM-DD
      quotaTrendPeak: function () {
        var peak = 0;
        for (var i = 0; i < this.quotaTrend.length; i++) {
          if (this.quotaTrend[i].used > peak) peak = this.quotaTrend[i].used;
        }
        return peak;
      },
      quotaTrendSum: function () {
        var s = 0;
        for (var i = 0; i < this.quotaTrend.length; i++) s += this.quotaTrend[i].used || 0;
        return s;
      },
      /* 口径说明：随端点 caliber 字段就地解释（口径改了页面文案跟着改，不写死第二份） */
      quotaCaliberNote: function () {
        var c = this.quotaCaliber || {};
        var parts = [];
        if (c.usageSource) parts.push('已用口径：' + c.usageSource + '，' + (c.usageIncludes || ''));
        if (c.limitSource) parts.push('限额口径：' + c.limitSource);
        // P1-5：登记值＝网关拦截值（同源）；真值时说明按 route×consumer 的强制口径
        if (c.limitEnforced === true) {
          parts.push('网关按此限额强制拦截' + (c.gatewayEnforcedBy ? '（' + c.gatewayEnforcedBy + '）' : ''));
        } else if (c.limitEnforced === false) {
          parts.push('链路上暂未按此限额强制拦截');
        }
        if (c.resetAt) parts.push('重置：' + c.resetAt);
        return parts.length ? parts.join('；') + '。' : '口径说明暂不可用';
      },
      /* P1-4 轮换重叠期倒计时（后端口径：daysLeft<=0 即已失效） */
      rotationLabel: function (c) {
        var r = c.rotation || {};
        if (!r.rotated) return '';
        if (r.expired) return '重叠期已结束';
        return '新旧凭证并行中·旧凭证剩 ' + (r.daysLeft != null ? r.daysLeft : '?') + ' 天';
      },
      quotaDailyLabel: function () {
        var c = this.credentials[0];
        return c && c.quotaDaily != null ? String(c.quotaDaily) : '—';
      },
      logsFootnote: function () {
        var n = '近 ' + this.logItems.length + ' 次（最多 ' + this.logsLimit + ' 条，按时间倒序）';
        if (this.traceMode) n = '按追踪号检索到 ' + this.logItems.length + ' 条';
        return n + '；' + (this.logsNote || '');
      },

      /* ── 深链同步：查询条件写回 URL（可刷新/可分享），不产生历史 ── */
      syncUrl: function (patch) {
        var q = new URLSearchParams(location.search);
        Object.keys(patch).forEach(function (k) {
          if (patch[k] == null) { q.delete(k); } else { q.set(k, patch[k]); }
        });
        var s = q.toString();
        history.replaceState(null, '', location.pathname + (s ? '?' + s : '') + location.hash);
      }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('myAccessPage', myAccessPage);
  });
})();
