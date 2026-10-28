/* ============================================================
 * 性能测试页组件（perf.js · P3 2026-10-08）
 * 压测管理台：触发（选服务＋档位＋凭证在场性）→ 状态轮询（5s）→
 * 历史报告列表 → 页内 iframe 内嵌自包含 performance-report.html。
 *
 * 数据面（本页后端，P3 新增）：
 *   GET  /v1/admin/publish          服务下拉（published 过滤；发布台写区真源）
 *   POST /v1/admin/perf/run         触发梯子 {service, ladder}；运行中 → 409
 *                                   perf_running「压测进行中」；凭证缺失 → 400
 *   GET  /v1/admin/perf/status      轮询（running/run/latest/unknown/appkey_configured）
 *   GET  /v1/admin/perf/reports     历史报告聚合（容器态空表 → 回落门户静态镜像
 *                                   ../perf-reports/index.json，build_portal.py 同步）
 * 加载序：admin-gate.js → 本文件 → alpine.min.js（硬规矩）。
 * 诚信线：触发失败＝如实错误条（409/400/503 原因人话化，不假装已开跑）；
 * 服务重启后句柄丢失＝如实「后台进程状态未知」；列表两源皆败＝如实空表＋
 * 说明，不编造报告行。
 * ============================================================ */
(function (root) {
  'use strict';

  var POLL_MS = 5000;   // 任务口径：页面轮询间隔 5s

  /* ── 纯函数（供机检/走查直调 PERF） ── */
  function ladderValid(s) {
    return /^[0-9]+(,[0-9]+)*$/.test(String(s || '').trim());
  }

  /* 时间戳人话化：20261008-203426 → 2026-10-08 20:34:26 */
  function fmtTs(ts) {
    var m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(String(ts || ''));
    return m ? (m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] + ':' + m[6])
             : String(ts || '');
  }

  /* 触发失败文案（按机读码映射，不自造原因） */
  function runFailText(err, status) {
    var e = err || {};
    if (status === 409 || e.error === 'perf_running') {
      return e.message || '压测进行中：等当前批次完成后再触发。';
    }
    if (e.error === 'perf_appkey_missing') {
      /* P4（走查 2026-10-16）：缺凭证给三步可操作指引，人话无黑话。
         服务端 message 只有一句概述时优先展示本页完整指引。 */
      return '还没配置压测用的凭证（这是触发压测的前提）。请按三步操作：' +
        '① 在管理台「AppKey 发放」页创建一把沙箱压测凭证（sb_ 开头，自助创建）；' +
        '② 把这串凭证配置到服务进程的环境变量 ONESERVE_APPKEY 中（service 的 compose/env 注入）；' +
        '③ 重建服务进程（up -d --force-recreate；注意仅 restart 不会重新读取环境变量）后，再回本页触发。';
    }
    if (e.error === 'invalid_ladder' || e.error === 'invalid_service') {
      return e.message || '参数不合法：请检查已选服务与档位格式（如 20 或 100,300,500,800）。';
    }
    if (e.error === 'loadtest_unavailable') {
      return e.message || '压测工具不在服务进程可达路径（容器部署）；请用本机 CLI 触发，报告生成后仍可在本页查看。';
    }
    return '触发没有完成（' + String(e.message || '原因未知') + '）。请重试，或联系平台管理员。';
  }

  /* 报告行归一（服务端点与静态镜像两源同形） */
  function normalizeReportRow(r, base) {
    r = r || {};
    var ok = r.report_html !== false;
    var href = ok ? String(r.href || (base + String(r.dir || '') + '/performance-report.html')) : '';
    return {
      dir: String(r.dir || ''),
      ts: fmtTs(r.ts || String(r.dir || '').split('-').slice(0, 2).join('-')),
      service: String(r.service || ''),
      service_name: String(r.service_name || r.service || ''),
      report_html: !!ok,
      href: href
    };
  }

  root.PERF = {
    ladderValid: ladderValid, fmtTs: fmtTs,
    runFailText: runFailText, normalizeReportRow: normalizeReportRow,
    POLL_MS: POLL_MS
  };

  root.perfConsole = function () {
    return {
      /* 触发区 */
      services: [], servicesBusy: false, servicesError: '', serviceId: '',
      ladder: '100,300,500,800',
      runBusy: false, runError: '', runOkNote: '',
      /* 状态区（5s 轮询） */
      status: null, _wasRunning: false, timer: null,
      /* 报告列表＋查看 */
      reports: [], reportsBusy: false, reportsNote: '', activeReport: null,

      /* ── 取数基础设施（与 versions.js 同款：fail-closed 如实错误） ── */
      async apiFetch(url, opts) {
        try {
          var r = await root.fetch(url, Object.assign({
            headers: Object.assign({ 'Content-Type': 'application/json' },
                                   (root.AdminGate && root.AdminGate.adminHeaders()) || {})
          }, opts || {}));
          var j = {};
          try { j = await r.json(); } catch (e) { j = {}; }
          return { ok: r.ok, status: r.status, body: j };
        } catch (e) {
          return { ok: false, status: 0, body: { error: 'NETWORK', message: '网络连不上服务端' } };
        }
      },

      ladderValid: ladderValid,

      get appkeyOk() { return !!(this.status && this.status.appkey_configured); },
      get running() { return !!(this.status && this.status.running); },
      get runDisabled() {
        return this.runBusy || this.running || !this.serviceId || !ladderValid(this.ladder);
      },
      get statusNote() {
        var st = this.status;
        if (!st) return '状态尚未取到。';
        if (st.loadtest_available === false) {
          return '服务进程看不到压测工具（容器栈未挂载仓库根 loadtest/）：触发会被如实拒绝，请用本机 CLI；历史报告按门户静态镜像展示。';
        }
        if (st.latest && st.latest.report_html && !this.running) {
          return '最近一次梯子已完成并生成报告（见下方历史报告）。';
        }
        return '结果落盘：' + String(st.results_dir || '');
      },

      async boot() {
        await this.loadServices();
        await this.refreshStatus();
        await this.loadReports();
        var self = this;
        this.timer = root.setInterval(function () { self.refreshStatus(); }, POLL_MS);
      },

      /* ── 触发区：已发布服务下拉（GET /v1/admin/publish） ── */
      async loadServices() {
        this.servicesBusy = true; this.servicesError = '';
        var res = await this.apiFetch('/v1/admin/publish');
        this.servicesBusy = false;
        if (!res.ok) {
          this.services = [];
          this.servicesError = '已发布服务读取失败（' + ((res.body && res.body.message) || ('HTTP ' + res.status)) + '）；请刷新重试。';
          return;
        }
        var list = (res.body && res.body.services) || [];
        /* 服务标识双字段兼容：写区新条目带 service_id（serviceId），存量条目只有 id
         * （v1-svc-usr-basic-info 形态，与 run_ladder --service 同一口径）——两者都认。 */
        this.services = list.filter(function (s) {
          return s && s.status === 'published' && (s.serviceId || s.id);
        }).map(function (s) {
          var sid = s.serviceId || s.id;
          return { serviceId: sid, serviceName: s.serviceName || sid };
        });
        if (this.services.length === 1) this.serviceId = this.services[0].serviceId;
      },

      /* ── 触发（POST /v1/admin/perf/run） ── */
      async startRun() {
        if (this.runDisabled) return;
        this.runBusy = true; this.runError = ''; this.runOkNote = '';
        var res = await this.apiFetch('/v1/admin/perf/run', {
          method: 'POST',
          body: JSON.stringify({ service: this.serviceId, ladder: String(this.ladder || '').trim() })
        });
        this.runBusy = false;
        if (res.ok) {
          var b = res.body || {};
          this.runOkNote = String(b.note || '梯子已在后台开跑') +
            (b.run_id ? '（run_id：<span class="mono">' + b.run_id + '</span>）' : '');
          await this.refreshStatus();
          return;
        }
        this.runError = runFailText(res.body, res.status);
      },

      /* ── 状态轮询（GET /v1/admin/perf/status；运行→空闲沿刷新报告列表） ── */
      async refreshStatus() {
        var res = await this.apiFetch('/v1/admin/perf/status');
        if (!res.ok) return;                 /* 轮询静默：保留上一拍，不闪错 */
        this.status = res.body || null;
        if (this._wasRunning && !this.running) this.loadReports();
        this._wasRunning = this.running;
      },

      /* ── 历史报告（GET /v1/admin/perf/reports；容器态回落门户静态镜像） ── */
      async loadReports() {
        this.reportsBusy = true; this.reportsNote = '';
        var res = await this.apiFetch('/v1/admin/perf/reports');
        this.reportsBusy = false;
        if (res.ok && res.body && res.body.loadtest_available !== false) {
          this.reports = (res.body.reports || []).map(function (r) { return normalizeReportRow(r, ''); });
          if (!this.reports.length) this.reportsNote = '还没有历史报告（loadtest/results/ 为空）。';
          return;
        }
        var fb = await this.apiFetch('../perf-reports/index.json');
        if (fb.ok && fb.body && Array.isArray(fb.body.reports)) {
          var base = '../perf-reports/';
          this.reports = fb.body.reports.map(function (r) { return normalizeReportRow(r, base); });
          this.reportsNote = '服务层看不到 loadtest/results（容器部署），按门户静态镜像展示（bash scripts/build_portal.py 构建期同步）。';
          return;
        }
        this.reports = [];
        this.reportsNote = '历史报告暂时读不到（服务端点与门户静态镜像均未命中）；可稍后刷新。';
      },

      /* ── 报告查看（页内 iframe） ── */
      viewReport(row) {
        this.activeReport = row;
        var el = document.getElementById('perf-report-panel');
        if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
      closeReport() { this.activeReport = null; }
    };
  };
})(window);
