/* ============================================================
 * OneServe 运营版 B7 · 异常与批量监控逻辑（monitor.js，Alpine 组件）
 * 依据：docs/plans/前台产品重造-SPEC_v0.md §5-P1-6／§2-B7；评估报告 UX-F5/B4。
 *
 * 【接真】三块全部读真端点（门户 nginx /v1/admin/ 反代服务层；令牌机制已拆，不再带头）：
 *   /v1/admin/monitor/errors  审计台账筛 4xx/5xx＋下钻聚合（app/monitor.error_query）
 *   /v1/admin/monitor/trace   trace_id 精确反查（服务侧＋批量任务级两份审计）
 *   /v1/admin/monitor/batch   批量运行视角（真源＝Redis 任务结构 JobStore）
 *   失败进 error 态（就地提示＋重试），**不做 mock 降级**——异常/任务状态是运行真值，
 *   塞演示数字只会误导值班（与审计页同纪律）。
 *
 * 【P0-6 最小鉴权 gate】无令牌＝只读模式（同 appkeys/publish/upstreams 三页）；令牌栏/
 *   会话存储/后端校验口径复用 admin-gate.js（AdminGate.gateMixin）。
 *
 * 【下钻口径】列表与下钻榜的 caller 均以掩码展示，下钻按**可辨识前缀**（凭证前 7 位／
 *   其它标识前 12 位）过滤——审计 caller 过滤本就是子串匹配，页面就地标注「前缀下钻」。
 *
 * 【可测性】纯格式化/判定函数挂在 window.Monitor（供 Node 冒烟与 selftest 直调），
 *   Alpine 组件只做取数与编排。
 * ============================================================ */
(function (root) {
  'use strict';

  var API = {
    errors: '/v1/admin/monitor/errors',
    trace: '/v1/admin/monitor/trace',
    batch: '/v1/admin/monitor/batch',
    containers: '/v1/admin/monitor/containers'
  };
  var FETCH_TIMEOUT_MS = 8000;
  var JOB_STATUS_TEXT = {
    queued: '排队中', running: '执行中', cancelling: '取消中',
    succeeded: '成功', failed: '失败', cancelled: '已取消'
  };

  /* ── 纯函数（可直调断言） ── */
  function fmtRate(rate) { return rate == null ? '样本量不足' : rate + '%'; }

  function digestShort(d) { return d ? d.slice(0, 8) + '…' : '—'; }

  function shortId(jobId) {
    var s = String(jobId || '');
    return s.length > 12 ? s.slice(0, 12) + '…' : (s || '—');
  }

  function jobStatusText(status) { return JOB_STATUS_TEXT[status] || (status || '未知'); }

  function jobBadge(status) {
    if (status === 'succeeded') return 'badge-ok';
    if (status === 'failed') return 'badge-5xx';
    if (status === 'running' || status === 'cancelling') return 'badge-run';
    if (status === 'cancelled') return 'badge-4xx';
    return 'badge-warn';
  }

  function progressText(job) {
    if (!job) return '—';
    var total = Number(job.total || 0), done = Number(job.processed || 0);
    if (!total) return '待物化（total 未回填）';
    return job.progress + '%（' + done + '/' + total + '）';
  }

  /* 积压时长人话：无 queued＝None（如实「无排队」而不是「0 秒」） */
  function backlogText(backlog) {
    if (!backlog || backlog.oldestWaitSec == null) return '—';
    var s = backlog.oldestWaitSec;
    if (s < 60) return s + ' 秒';
    if (s < 3600) return Math.floor(s / 60) + ' 分';
    return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
  }

  function pageText(total, offset, shown) {
    if (!total) return '共 0 条';
    var a = offset + 1, b = Math.min(offset + shown, total);
    return '第 ' + a + '–' + b + ' 条 · 共 ' + total + ' 条';
  }

  /* 常驻容器健康（蓝图 1a 监控最小集）：状态→人话/徽标（off＝无探针，如实中性） */
  function containerStatusText(status) {
    if (status === 'ok') return '健康';
    if (status === 'degraded') return '不可达';
    if (status === 'error') return '故障';
    if (status === 'off') return '无探针';
    return status || '未知';
  }

  function containerBadge(status) {
    if (status === 'ok') return 'badge-ok';
    if (status === 'error') return 'badge-5xx';
    return 'badge-warn';   // degraded/off/未知＝警示色中性档
  }

  function fetchJson(url, opts) {
    opts = opts || {};
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
      var headers = Object.assign({ 'Accept': 'application/json' },
                   (window.AdminGate && window.AdminGate.adminHeaders()) || {});
      fetch(url, { method: opts.method || 'GET', headers: headers, cache: 'no-store',
                   signal: ctrl ? ctrl.signal : undefined })
        .then(function (res) {
          clearTimeout(timer);
          return res.json().catch(function () { return {}; }).then(function (j) {
            if (!res.ok) {
              var e = new Error(j.message || ('HTTP ' + res.status));
              e.status = res.status;
              throw e;
            }
            return j;
          });
        })
        .then(resolve)
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  function monitorAdmin() {
    var base = root.AdminGate ? root.AdminGate.gateMixin() : {};
    return Object.assign(base, {
      /* ── 检索状态（与审计查询同口径；时间窗＋多条件 AND） ── */
      range: 'today', tsFromInput: '', tsToInput: '',
      callerInput: '', endpointInput: '', errorClassInput: '', statusInput: '',
      traceInput: '', limitSel: 30,

      /* ── 异常列表状态 ── */
      readState: 'loading', readError: '', queryError: '',
      items: [], total: 0, offset: 0, errorSummary: null,
      drillCallers: [], drillApis: [], dimTruncated: false,
      generatedAt: '', openSamples: {}, lastTrace: '',

      /* ── trace 详情状态 ── */
      traceIdLookup: '', traceResult: null, traceError: '',

      /* ── 批量运行视角状态 ── */
      batch: null, batchError: '', batchGeneratedAt: '',

      /* ── 常驻容器健康状态（蓝图 1a 监控最小集） ── */
      containers: null, containersError: '',

      boot: function () {
        /* 2026-09-23：管理令牌机制已彻底拆除，不再恢复/校验令牌 */
        this.loadBatch();
        this.loadContainers();
        /* 深链：?trace_id=… 直达本页 trace 详情（复用既有 trace_id 查询参数口径，
         * 同 audit.html / my.html 的自助反查约定）；其余筛选条件同审计页解析 */
        var q = new URLSearchParams(location.search);
        var tid = (q.get('trace_id') || '').trim();
        var caller = (q.get('caller') || '').trim();
        var ep = (q.get('endpoint') || '').trim();
        if (caller) this.callerInput = caller;
        if (ep) this.endpointInput = ep;
        this.query(0, true);
        if (tid) { this.traceIdLookup = tid; this.lookupTrace(); }
      },

      /* 2026-09-23：管理令牌机制已彻底拆除，保存令牌按钮不再存在（HTML 中已删） */
      saveToken: function () {
        this.loadBatch();
        this.query(0);
      },

      /* ── 时间窗 → [from, to]（ISO；all＝不给时间条件） ── */
      rangeBounds: function () {
        var now = new Date();
        if (this.range === 'all') return [null, null];
        if (this.range === '24h') return [new Date(now.getTime() - 24 * 3600 * 1000), now];
        if (this.range === '7d') return [new Date(now.getTime() - 7 * 24 * 3600 * 1000), now];
        if (this.range === 'custom') {
          var f = this.tsFromInput ? new Date(this.tsFromInput) : null;
          var t = this.tsToInput ? new Date(this.tsToInput) : null;
          return [isNaN(f) ? null : f, isNaN(t) ? null : t];
        }
        var d = new Date(now); d.setHours(0, 0, 0, 0);   /* today：本地当日 0 点起 */
        return [d, now];
      },
      rangeLabel: function () {
        return { today: '今日', '24h': '近 24 小时', '7d': '近 7 天', all: '全部时间',
                 custom: '自定义时间' }[this.range] || '';
      },
      setRange: function (r) { this.range = r; this.query(0); },
      onCustomChange: function () { this.range = 'custom'; this.query(0); },

      /* ── 异常查询（offset 翻页共用；silentUrl＝init 不写 URL） ── */
      query: function (offset, silentUrl) {
        var self = this;
        this.readState = 'loading'; this.readError = ''; this.queryError = '';
        var bounds = this.rangeBounds();
        var qs = new URLSearchParams();
        if (bounds[0] && !isNaN(bounds[0])) qs.set('ts_from', bounds[0].toISOString());
        if (bounds[1] && !isNaN(bounds[1])) qs.set('ts_to', bounds[1].toISOString());
        var c = this.callerInput.trim(); if (c) qs.set('caller', c);
        var e = this.endpointInput.trim(); if (e) qs.set('endpoint', e);
        var ec = this.errorClassInput; if (ec) qs.set('error_class', ec);
        var s = this.statusInput; if (s) qs.set('status', s);
        var t = this.traceInput.trim(); if (t) qs.set('trace_id', t);
        qs.set('limit', String(this.limitSel));
        qs.set('offset', String(offset));
        fetchJson(API.errors + '?' + qs.toString())
          .then(function (j) {
            self.items = j.items || [];
            self.total = j.total || 0;
            self.offset = j.offset || 0;
            self.limitSel = j.limit || self.limitSel;
            self.errorSummary = j.summary || null;
            self.drillCallers = j.byCaller || [];
            self.drillApis = j.byApi || [];
            self.dimTruncated = !!j.dimTruncated;
            self.generatedAt = j.generatedAt || '';
            self.readState = 'ok';
            self.lastTrace = '已读取异常列表：' + self.total + ' 条异常（窗口总量 '
              + ((j.summary || {}).windowTotal || 0) + '）· 生成 ' + self.generatedAt;
            if (!silentUrl) {
              history.replaceState(null, '', location.pathname + '?' + qs.toString());
            }
          })
          .catch(function (err) {
            self.readState = 'error';
            self.readError = (err && err.message) || '异常列表暂不可用';
            self.items = []; self.total = 0;
            self.drillCallers = []; self.drillApis = [];
          });
      },

      prevPage: function () { if (this.offset > 0) this.query(Math.max(0, this.offset - this.limitSel)); },
      nextPage: function () {
        if (this.offset + this.limitSel < this.total) this.query(this.offset + this.limitSel);
      },
      pageText: function () { return pageText(this.total, this.offset, this.items.length); },

      /* ── 下钻：把榜内/列内的键填进检索条件就地重查 ── */
      drillCaller: function (row) {
        var q = (row && (row.callerQuery || row.callerMasked)) || '';
        if (!q) return;
        this.callerInput = q;
        this.query(0);
      },
      drillApi: function (row) {
        var ep = (row && row.endpoint) || '';
        if (!ep) return;
        this.endpointInput = ep;
        this.query(0);
      },
      /* ── 条件 chips（就地清除） ── */
      activeConds: function () {
        var out = [];
        if (this.callerInput.trim()) out.push({ key: 'caller', label: '调用方：' + this.callerInput.trim() });
        if (this.endpointInput.trim()) out.push({ key: 'endpoint', label: 'API：' + this.endpointInput.trim() });
        if (this.errorClassInput) out.push({ key: 'errorClass', label: '异常类：' + this.errorClassInput });
        if (this.statusInput) out.push({ key: 'status', label: '状态：' + this.statusInput });
        if (this.traceInput.trim()) out.push({ key: 'trace', label: '追踪号：' + this.traceInput.trim() });
        return out;
      },
      clearCond: function (key) {
        if (key === 'caller') this.callerInput = '';
        else if (key === 'endpoint') this.endpointInput = '';
        else if (key === 'errorClass') this.errorClassInput = '';
        else if (key === 'status') this.statusInput = '';
        else if (key === 'trace') this.traceInput = '';
        this.query(0);
      },
      resetAll: function () {
        this.range = 'today'; this.tsFromInput = ''; this.tsToInput = '';
        this.callerInput = ''; this.endpointInput = ''; this.errorClassInput = '';
        this.statusInput = ''; this.traceInput = '';
        this.query(0);
      },

      /* ── 错误样本展开 ── */
      toggleSample: function (key) {
        var cur = !!this.openSamples[key];
        this.openSamples[key] = !cur;
      },
      isSampleOpen: function (key) { return !!this.openSamples[key]; },

      /* ── trace 详情 ── */
      useTrace: function (tid) {
        if (!tid) return;
        this.traceIdLookup = tid;
        this.lookupTrace();
        /* 深链回写：刷新/分享可直达该追踪号详情（与审计页 history.replaceState 同纪律） */
        try {
          history.replaceState(null, '', location.pathname + '?trace_id=' + encodeURIComponent(tid));
        } catch (e) { /* 非浏览器/受限上下文：忽略 */ }
      },
      lookupTrace: function () {
        var self = this;
        var tid = (this.traceIdLookup || '').trim();
        this.traceError = '';
        if (!tid) { this.traceResult = null; this.traceError = '请填写追踪号（X-Trace-Id）'; return; }
        fetchJson(API.trace + '?trace_id=' + encodeURIComponent(tid))
          .then(function (j) { self.traceResult = j; })
          .catch(function (err) {
            self.traceResult = null;
            self.traceError = (err && err.status === 401)
              ? '管理令牌缺失或不正确：请在页头填写令牌'
              : ((err && err.message) || '追踪号反查暂不可用');
          });
      },

      /* ── 批量运行视角 ── */
      loadBatch: function () {
        var self = this;
        this.batchError = '';
        fetchJson(API.batch)
          .then(function (j) {
            self.batch = j;
            self.batchGeneratedAt = j.generatedAt || '';
          })
          .catch(function (err) {
            self.batch = null;
            self.batchError = (err && err.message) || '批量任务状态暂不可用';
          });
      },

      /* ── 常驻容器健康（真探活，无 mock 降级——同本页其余块纪律） ── */
      loadContainers: function () {
        var self = this;
        this.containersError = '';
        fetchJson(API.containers)
          .then(function (j) { self.containers = j; })
          .catch(function (err) {
            self.containers = null;
            self.containersError = (err && err.message) || '容器健康探活暂不可用';
          });
      },
      containerStatusText: function (status) { return containerStatusText(status); },
      containerBadge: function (status) { return containerBadge(status); },
      backlogText: function () { return backlogText(this.batch && this.batch.backlog); },
      backlogNote: function () {
        var b = (this.batch && this.batch.backlog) || {};
        if (b.oldestWaitSec == null) return '当前无排队任务（无积压可比）';
        return '排队 ' + b.queued + ' 个 · 最长等待即上值'
          + (b.staleQueueItems > 0 ? (' · 队列滞留 ' + b.staleQueueItems + ' 项（如实暴露）') : '');
      },
      caliberText: function () {
        var c = (this.batch && this.batch.caliber) || {};
        return [c.queueDepth, c.backlog].filter(Boolean).join('；');
      },
      progressText: progressText,
      shortId: shortId,
      jobStatusText: jobStatusText,
      jobBadge: jobBadge,

      /* ── 摘要卡口径 ── */
      errorRateText: function () {
        if (!this.errorSummary) return '—';
        return this.errorSummary.errorRate == null ? '样本量不足' : this.errorSummary.errorRate + '%';
      },
      rateNote: function () {
        if (!this.errorSummary) return '—';
        if (this.errorSummary.errorRate == null) {
          return '窗口内总量低于 ' + this.errorSummary.rateSampleMin + ' 条不出比率（小基数口径）';
        }
        return '异常数 ÷ 窗口内总量（HTTP 4xx/5xx）';
      },
      codesText: function () {
        var codes = (this.errorSummary && this.errorSummary.codes) || [];
        if (!codes.length) return '窗口内无异常状态码';
        return codes.map(function (x) { return 'HTTP ' + x.status + '×' + x.count; }).join(' · ');
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
      fmtRate: fmtRate,
      digestShort: digestShort
    });
  }

  root.Monitor = {
    API: API,
    fmtRate: fmtRate,
    digestShort: digestShort,
    shortId: shortId,
    jobStatusText: jobStatusText,
    jobBadge: jobBadge,
    progressText: progressText,
    backlogText: backlogText,
    pageText: pageText,
    containerStatusText: containerStatusText,
    containerBadge: containerBadge,
    JOB_STATUS_TEXT: JOB_STATUS_TEXT
  };

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('alpine:init', function () {
      if (root.Alpine && root.Alpine.data) root.Alpine.data('monitorAdmin', monitorAdmin);
    });
  }
  root.MonitorFactory = monitorAdmin;
})(typeof window !== 'undefined' ? window : this);
