/* ============================================================
 * OneServe 门户 · 报表专区一期页组件（1b-9 · assets/reports.js，新增文件）
 * 纪律（同 my.js/audit.js）：Alpine.data 注册，HTML 不写内联表达式；跨页传参只用
 * URL query（subjects.html?q=…&service=… / services.html?subject=… 深链直达）。
 * 取数：直达 /v1/admin/usage/* 四端点族（viewer 地板，站点墙注入 Bearer）；失败进
 * 降级态（重试），不做 mock 降级——用量是真实账面，演示数字只会误导；也绝不
 * 落到任何本地现算（现算禁令 ADR-0023 §5）。
 * 三态：status ∈ ok｜empty｜degraded（端点随行）；loading＝页面本地态。
 * 纯函数导出面 window.RPT（fmtNum/pctText/bars max）供自测断言，无副作用。
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

  /* ── 纯函数导出面（自测/机检断言用；无副作用） ── */
  var RPT = {
    fmtNum: function (n) {
      if (n === null || n === undefined || n === '') return '—';
      return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    },
    pctText: function (v) { return (v === null || v === undefined) ? '—' : v + '%'; },
    msText: function (v) { return (v === null || v === undefined) ? '—' : v + ' ms'; },
    barsMax: function (trend) {
      var mx = 0;
      (trend || []).forEach(function (t) { if (t.calls > mx) mx = t.calls; });
      return mx || 1;
    }
  };
  window.RPT = RPT;

  /* 柱高：相对该行最大值的 3..28px（0 次＝1px 灰底线，连续轴不跳点） */
  function barHeight(trend, t) {
    if (!t.calls) return 1;
    return Math.max(3, Math.round(t.calls / RPT.barsMax(trend) * 28));
  }

  /* 服务主列显示名：登记名优先；无登记名显示路由原值（等宽码＝技术标识豁免） */
  function serviceLabel(row) {
    return row.registeredName || row.service;
  }

  /* ── 共享分页器（服务端分页：page/page_size 白名单 {20,50,100} 默认 50） ── */
  function pager() {
    return { page: 1, pageSize: 50, total: 0 };
  }

  function applyPage(self, j) {
    self.page = j.page || 1;
    self.pageSize = j.pageSize || 50;
    self.total = j.total || 0;
    self.items = j.items || [];
    self.serviceDim = j.serviceDim;
    self.caliber = j.caliber || null;
  }

  function base() {
    return {
      days: 7,                  // 时间窗：1=今日 | 7=近 7 天（一期窗口上限）
      status: 'loading',        // loading | ok | empty | degraded（端点三态＋本地加载态）
      message: '',
      items: [], total: 0, page: 1, pageSize: 50,
      serviceDim: null,         // null=未知/降级；false=快照旧键无服务维度（服务列降级「—」）
      caliber: null,
      tenants: [],              // 租户下拉选项（load 成功回包刷新；必须预初始化——裸引用在 Alpine
                                // 初始 effect 抛 ReferenceError 会间歇打断三态横幅渲染，20261021 走查实证）
      echartsReady: typeof window.echarts !== 'undefined'
    };
  }

  /* ══ 用量总览（overview） ══ */
  function reportsOverview() {
    var self;
    var st = Object.assign(base(), pager(), {
      totals: null, activeCallers: null, topServices: [], daily: [],
      chart: null,
      init: function () {
        self = this;
        var q = new URLSearchParams(location.search);
        if (q.get('days') === '1') this.days = 1;
        this.load();
      },
      load: function () {
        self.status = 'loading';
        /* page_size 走白名单最小档 20（10 会被 400 bad_query 拒——20261021 走查实证），
         * 榜面口径「前 10」由 slice 保住 */
        fetchJson('/v1/admin/usage/overview?days=' + self.days + '&page=1&page_size=20')
          .then(function (j) {
            self.totals = j.totals || null;
            self.activeCallers = j.activeCallers || null;
            self.topServices = j.topServices || [];
            self.daily = j.daily || [];
            self.items = (j.items || []).slice(0, 10);   // 调用方榜「前 10」（此前漏装填，恒空表）
            self.caliber = j.caliber || null;
            self.status = j.status || 'ok';
            self.message = j.message || '';
            /* 图表容器在 <template x-if="status === 'ok'"> 内：本回调同步执行时 x-if
             * 尚未挂载（Alpine 微任务 flush 在后），el=null 会静默空图——必须等本轮
             * DOM 更新完成再 init（20261021 浏览器走查实证） */
            if (window.Alpine && Alpine.nextTick) { Alpine.nextTick(function () { self.renderChart(); }); }
            else { self.renderChart(); }
          })
          .catch(function () {
            self.status = 'degraded';
            self.message = '用量数据暂不可用：统计快照未响应，请稍后重试';
          });
      },
      setDays: function (d) {
        if (self.days === d) return;
        self.days = d;
        self.load();
      },
      renderChart: function () {
        var el = document.getElementById('usage-daily-chart');
        if (!el) return;
        if (!(window.echarts && self.daily && self.daily.length)) return;   // 缺底座/无数据不画（容器留三态文案）
        /* x-if 卸载重挂后 el 是新节点：旧实例绑在已脱离 DOM 的旧容器上，setOption 画不进新图
         * （20261021 走查实证：切时间窗/维度后 canvas=0）——实例 DOM 不符即 dispose 换绑 */
        if (self.chart && self.chart.getDom && self.chart.getDom() !== el) {
          self.chart.dispose(); self.chart = null;
        }
        if (!self.chart) self.chart = window.echarts.init(el);
        var dates = self.daily.map(function (d) { return d.date; });
        self.chart.setOption({
          backgroundColor: 'transparent',
          tooltip: { trigger: 'axis' },
          legend: { data: ['调用量', '成功量', '失败量'], textStyle: { color: '#9aa3b2' } },
          grid: { left: 48, right: 16, top: 32, bottom: 28 },
          xAxis: { type: 'category', data: dates, axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' }, splitLine: { lineStyle: { color: 'rgba(154,163,178,.15)' } } },
          series: [
            { name: '调用量', type: 'bar', data: self.daily.map(function (d) { return d.calls; }),
              itemStyle: { color: '#cb272d' }, barMaxWidth: 26 },
            { name: '成功量', type: 'line', data: self.daily.map(function (d) { return d.success; }),
              lineStyle: { color: '#2e9e5b' }, itemStyle: { color: '#2e9e5b' } },
            { name: '失败量', type: 'line', data: self.daily.map(function (d) { return d.fail; }),
              lineStyle: { color: '#d4760a' }, itemStyle: { color: '#d4760a' } }
          ]
        });
      }
    });
    return st;
  }

  /* ══ 主体用量（subjects） ══ */
  function reportsSubjects() {
    var self;
    var st = Object.assign(base(), pager(), {
      tenant: '', registered: '', q: '', sort: 'calls', order: 'desc',
      expanded: {},                       // callerMasked → 行服务明细展开态
      init: function () {
        self = this;
        var q = new URLSearchParams(location.search);
        if (q.get('days') === '1') this.days = 1;
        if (q.get('tenant')) this.tenant = q.get('tenant');
        if (q.get('registered') === 'true' || q.get('registered') === 'false')
          this.registered = q.get('registered');
        if (q.get('q')) this.q = q.get('q');
        if (q.get('service')) this.q = q.get('q') || '';   // 服务下钻预填走 service 参数，q 保持独立
        this.serviceDrill = q.get('service') || '';
        this.load();
      },
      load: function () {
        self.status = 'loading';
        var qs = new URLSearchParams();
        qs.set('days', self.days);
        if (self.tenant) qs.set('tenant', self.tenant);
        if (self.registered) qs.set('registered', self.registered);
        if (self.serviceDrill) qs.set('service', self.serviceDrill);
        if (self.q) qs.set('q', self.q);
        qs.set('sort', self.sort);
        qs.set('order', self.order);
        qs.set('page', self.page);
        qs.set('page_size', self.pageSize);
        fetchJson('/v1/admin/usage/subjects?' + qs.toString())
          .then(function (j) {
            applyPage(self, j);
            self.tenants = j.tenants || [];
            self.status = j.status || 'ok';
            self.message = j.message || '';
          })
          .catch(function () {
            self.status = 'degraded';
            self.message = '用量数据暂不可用：统计快照未响应，请稍后重试';
          });
      },
      setDays: function (d) { if (self.days !== d) { self.days = d; self.page = 1; self.load(); } },
      query: function () { self.page = 1; self.load(); },
      go: function (p) { if (p >= 1 && p <= self.pageCount()) { self.page = p; self.load(); } },
      pageCount: function () { return Math.max(1, Math.ceil(self.total / self.pageSize)); },
      toggle: function (row) { self.expanded[row.callerMasked] = !self.expanded[row.callerMasked]; },
      barH: function (row, t) { return barHeight(row.trend, t); }
    });
    return st;
  }

  /* ══ 服务用量（services） ══ */
  function reportsServices() {
    var self;
    var st = Object.assign(base(), pager(), {
      tenant: '', subject: '', q: '', sort: 'calls', order: 'desc',
      init: function () {
        self = this;
        var q = new URLSearchParams(location.search);
        if (q.get('days') === '1') this.days = 1;
        if (q.get('tenant')) this.tenant = q.get('tenant');
        if (q.get('subject')) this.subject = q.get('subject');
        if (q.get('q')) this.q = q.get('q');
        this.load();
      },
      load: function () {
        self.status = 'loading';
        var qs = new URLSearchParams();
        qs.set('days', self.days);
        if (self.tenant) qs.set('tenant', self.tenant);
        if (self.subject) qs.set('subject', self.subject);
        if (self.q) qs.set('q', self.q);
        qs.set('sort', self.sort);
        qs.set('order', self.order);
        qs.set('page', self.page);
        qs.set('page_size', self.pageSize);
        fetchJson('/v1/admin/usage/services?' + qs.toString())
          .then(function (j) {
            applyPage(self, j);
            self.tenants = j.tenants || [];
            self.status = j.status || 'ok';
            self.message = j.message || '';
          })
          .catch(function () {
            self.status = 'degraded';
            self.message = '用量数据暂不可用：统计快照未响应，请稍后重试';
          });
      },
      setDays: function (d) { if (self.days !== d) { self.days = d; self.page = 1; self.load(); } },
      query: function () { self.page = 1; self.load(); },
      go: function (p) { if (p >= 1 && p <= self.pageCount()) { self.page = p; self.load(); } },
      pageCount: function () { return Math.max(1, Math.ceil(self.total / self.pageSize)); },
      /* 行展开＝该服务 Top 调用方：跳主体页（服务筛选预填） */
      drill: function (row) {
        var qs = new URLSearchParams();
        qs.set('service', row.service);
        if (self.days) qs.set('days', String(self.days));
        location.href = 'subjects.html?' + qs.toString();
      },
      label: serviceLabel
    });
    return st;
  }

  /* ══ 用量趋势（trend） ══ */
  function reportsTrend() {
    var self;
    var st = Object.assign(base(), {
      dim: 'caller',              // caller=调用方 | service=服务（一期 7 天窗；30/90 随用量库上线）
      series: [], others: null, totals: null, chart: null,
      init: function () {
        self = this;
        var q = new URLSearchParams(location.search);
        if (q.get('days') === '1') this.days = 1;
        if (q.get('dim') === 'service') this.dim = 'service';
        this.load();
      },
      load: function () {
        self.status = 'loading';
        fetchJson('/v1/admin/usage/trend?days=' + self.days + '&dim=' + self.dim)
          .then(function (j) {
            self.series = j.series || [];
            self.others = j.others || null;
            self.totals = j.totals || null;
            self.caliber = j.caliber || null;
            self.status = j.status || 'ok';
            self.message = j.message || '';
            /* 图表容器在 <template x-if="status === 'ok'"> 内：本回调同步执行时 x-if
             * 尚未挂载（Alpine 微任务 flush 在后），el=null 会静默空图——必须等本轮
             * DOM 更新完成再 init（20261021 浏览器走查实证） */
            if (window.Alpine && Alpine.nextTick) { Alpine.nextTick(function () { self.renderChart(); }); }
            else { self.renderChart(); }
          })
          .catch(function () {
            self.status = 'degraded';
            self.message = '用量数据暂不可用：统计快照未响应，请稍后重试';
          });
      },
      setDays: function (d) { if (self.days !== d) { self.days = d; self.load(); } },
      setDim: function (d) { if (self.dim !== d) { self.dim = d; self.load(); } },
      total: function (key) { return self.totals ? self.totals[key] : null; },
      renderChart: function () {
        var el = document.getElementById('usage-trend-chart');
        if (!el) return;
        if (!(window.echarts && self.series && self.series.length)) return;
        /* 同 overview：x-if 重挂载后旧实例必须 dispose 换绑（20261021 走查实证） */
        if (self.chart && self.chart.getDom && self.chart.getDom() !== el) {
          self.chart.dispose(); self.chart = null;
        }
        if (!self.chart) self.chart = window.echarts.init(el);
        var dates = (self.series[0].data || []).map(function (p) { return p.date; });
        var legend = [];
        var series = self.series.map(function (s, i) {
          legend.push(s.label);
          var colors = ['#cb272d', '#2e9e5b', '#3b82f6', '#d4760a', '#8b5cf6',
                        '#0ea5e9', '#eab308', '#ec4899', '#14b8a6', '#f97316'];
          return { name: s.label, type: 'line', smooth: false, showSymbol: dates.length <= 8,
                   data: s.data.map(function (p) { return p.calls; }),
                   lineStyle: { color: colors[i % colors.length] }, itemStyle: { color: colors[i % colors.length] } };
        });
        if (self.others) {
          legend.push(self.others.label);
          series.push({ name: self.others.label, type: 'line', smooth: false,
                        data: self.others.data.map(function (p) { return p.calls; }),
                        lineStyle: { color: '#6b7280', type: 'dashed' }, itemStyle: { color: '#6b7280' } });
        }
        self.chart.setOption({
          backgroundColor: 'transparent',
          tooltip: { trigger: 'axis' },
          legend: { data: legend, textStyle: { color: '#9aa3b2' }, type: 'scroll' },
          grid: { left: 48, right: 16, top: 40, bottom: 28 },
          xAxis: { type: 'category', data: dates, axisLabel: { color: '#9aa3b2' } },
          yAxis: { type: 'value', axisLabel: { color: '#9aa3b2' }, splitLine: { lineStyle: { color: 'rgba(154,163,178,.15)' } } },
          series: series
        });
      }
    });
    return st;
  }

  /* Alpine 注册（alpine:init 先于 head 内 defer 的 alpine.min.js——经典 script 保序） */
  document.addEventListener('alpine:init', function () {
    if (!window.Alpine) return;
    Alpine.data('reportsOverview', reportsOverview);
    Alpine.data('reportsSubjects', reportsSubjects);
    Alpine.data('reportsServices', reportsServices);
    Alpine.data('reportsTrend', reportsTrend);
  });
})();
