/* ============================================================
 * OneServe 门户 · P-1 组件（Alpine.js）
 * 纪律（议题二 v1.1 防呆/防花哨）：
 *  - 无 count-up、无级联动画；首载一次性 ≤300ms 淡入由 CSS reveal 承担；
 *  - 三态齐备：loading 骨架屏（尺寸=最终组件）/ ok / error（数据暂不可用＋重试）；
 *  - 降级 mock 的区显示「演示数据」角标；每卡右下角标注数据来源接口名＋上次更新；
 *  - 轮询 60s，visibilitychange 暂停（后台页签不刷）。
 * ============================================================ */
(function () {
  'use strict';

  var FMT = window.Fmt;
  var SECTIONS = ['overview', 'runtimeStatus', 'catalog', 'freshness', 'announcements'];

  /* ── P0-1 小基数展示策略阈值（可配：页面先置 window.PORTAL_SMALL_BASE 覆盖默认） ──
   * callsThreshold：累计/今日调用低于该值→徽标样式「今日 N 次 · 真实计数」，不做大数字 KPI；
   * rateSampleMin：今日调用低于该值→不展示成功率，标「样本量不足」。 */
  var SMALL_BASE_DEFAULTS = { callsThreshold: 1000, rateSampleMin: 100 };

  function smallBaseConfig() {
    var o = (typeof window !== 'undefined' && window.PORTAL_SMALL_BASE) || {};
    return {
      callsThreshold: Number(o.callsThreshold) > 0 ? Number(o.callsThreshold) : SMALL_BASE_DEFAULTS.callsThreshold,
      rateSampleMin: Number(o.rateSampleMin) > 0 ? Number(o.rateSampleMin) : SMALL_BASE_DEFAULTS.rateSampleMin
    };
  }

  function blankSection() {
    return { state: 'loading', data: null, source: null, updatedAt: null };
  }

  function portalComponent() {
    return {
      /* ── 状态 ── */
      sections: {
        overview: blankSection(),
        runtimeStatus: blankSection(),
        catalog: blankSection(),
        freshness: blankSection(),
        announcements: blankSection()
      },
      query: '',
      pollTimer: null,
      now: Date.now(),
      smallBase: smallBaseConfig(),   /* P0-1：小基数阈值（组件内统一口，首页/运行视图同源同策略） */

      /* ── 生命周期 ── */
      init: function () {
        this.loadAll();
        var self = this;
        this.pollTimer = setInterval(function () {
          if (!document.hidden) { self.now = Date.now(); self.loadAll(); }   // 后台页签暂停轮询；now 随轮询同步（相对时间不冻结）
        }, window.PortalAPI.POLL_INTERVAL_MS);
        document.addEventListener('visibilitychange', function () {
          if (!document.hidden) { self.now = Date.now(); }
        });
      },

      loadAll: function () {
        var self = this;
        SECTIONS.forEach(function (name) { self.load(name); });
      },

      load: function (name) {
        var self = this;
        // 首载初始态即 loading（骨架屏）；轮询静默刷新原位更新——不回退骨架屏防闪屏；
        // error 态保留至下次成功或手动重试（retry 显式置 loading）。
        window.PortalAPI.loadSection(name).then(function (res) {
          var s = self.sections[name];
          s.data = res.data;
          s.source = res.source;                       // 'live' | 'demo'
          s.state = 'ok';
          s.updatedAt = FMT.formatClock(new Date());
        }).catch(function () {
          var s = self.sections[name];
          s.state = 'error';                           // 数据暂不可用＋重试，禁空白禁假 0
          s.source = null;
        });
      },

      retry: function (name) { this.sections[name].state = 'loading'; this.load(name); },

      /* ── 全局搜索（hero 位 · 前端过滤目录） ── */
      filteredDomains: function () {
        var sec = this.sections.catalog;
        if (sec.state !== 'ok' || !sec.data || !sec.data.domains) return [];
        var q = this.query.trim().toLowerCase();
        if (!q) return sec.data.domains;
        return sec.data.domains
          .map(function (d) {
            if (d.name.toLowerCase().indexOf(q) !== -1) return d;
            var items = (d.items || []).filter(function (it) {
              return (it.name + ' ' + it.api + ' ' + (it.desc || '')).toLowerCase().indexOf(q) !== -1;
            });
            if (!items.length) return null;
            var copy = {}; for (var k in d) copy[k] = d[k]; copy.items = items;
            return copy;
          })
          .filter(Boolean);
      },
      hasSearch: function () { return this.query.trim().length > 0; },
      catalogSummary: function () {
        var sec = this.sections.catalog;
        if (sec.state !== 'ok' || !sec.data) return '';
        var apis = 0;
        (sec.data.domains || []).forEach(function (d) { apis += (d.services || (d.items || []).length); });
        return '共 ' + (sec.data.domains || []).length + ' 个业务域 · ' + apis + ' 个在线服务 · ' +
          FMT.formatThousands(sec.data.totalFields) + ' 个字段在册';
      },

      /* ── 近 7 日迷你趋势（面积折线，纯 SVG，单色 --chart-1） ── */
      sparkline: function (points, w, h, pad) {
        var W = w || 96, H = h || 36, P = pad === undefined ? 3 : pad;
        if (!points || points.length < 2) return { line: '', area: '' };
        var min = Math.min.apply(null, points);
        var max = Math.max.apply(null, points);
        var span = (max - min) || 1;
        var stepX = (W - P * 2) / (points.length - 1);
        var coords = points.map(function (v, i) {
          var x = P + i * stepX;
          var y = P + (H - P * 2) * (1 - (v - min) / span);
          return [x, y];
        });
        var line = coords.map(function (c, i) { return (i === 0 ? 'M' : 'L') + c[0].toFixed(1) + ' ' + c[1].toFixed(1); }).join(' ');
        var area = line + ' L' + coords[coords.length - 1][0].toFixed(1) + ' ' + (H - P) +
          ' L' + coords[0][0].toFixed(1) + ' ' + (H - P) + ' Z';
        return { line: line, area: area };
      },

      /* ── 状态语义映射（灯旁必配文字） ── */
      statusMeta: function (code) {
        if (code === 'ok') return { cls: 'is-ok', text: '正常' };
        if (code === 'warn') return { cls: 'is-warn', text: '延迟' };
        if (code === 'error') return { cls: 'is-error', text: '异常' };
        return { cls: 'is-error', text: '未知' };
      },

      /* ── 视图直通 ── */
      st: function (name) { return this.sections[name].state; },
      data: function (name) { return this.sections[name].data || {}; },

      /* ── 格式化直通 ── */
      fmtCompact: function (v) { return FMT.formatCompact(v); },
      fmtThousands: function (v) { return FMT.formatThousands(v); },
      fmtPercent: function (v) { return FMT.formatPercent(v); },
      fmtRel: function (t) { return FMT.formatRelativeTime(t, this.now); },
      endpoint: function (name) { return 'GET ' + window.PortalAPI.ENDPOINTS[name]; },
      isDemo: function (name) { return this.sections[name].source === 'demo'; },

      /* ── P0-1 小基数判定（首页与运行视图同源同策略） ── */
      isSmallCallsBase: function (v) { return (Number(v) || 0) < this.smallBase.callsThreshold; },
      rateSampleEnough: function (n) { return (Number(n) || 0) >= this.smallBase.rateSampleMin; }
    };
  }

  /* ── P0-1 环境条组件（partials/topbar.html 全站挂载）：只取 overview 一件事＝data_as_of ──
   * live→真值日期；未同步→「未同步」；降级 mock→「演示数据」（与数据卡角标同一判定）；双失败→「—」。 */
  function envBarComponent() {
    return {
      source: null,
      dataAsOf: '',
      init: function () {
        var self = this;
        window.PortalAPI.loadSection('overview').then(function (res) {
          self.source = res.source;
          self.dataAsOf = (res.data && res.data.dataAsOf) || '';
        }).catch(function () { self.source = null; });
      },
      snapshotText: function () {
        if (this.source === 'demo') return '演示数据';
        if (this.source === 'live') return this.dataAsOf || '未同步';
        return '—';
      }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('portal', portalComponent);
    window.Alpine.data('envBar', envBarComponent);   /* P0-1 环境条（shell 顶栏，全站） */
  });

  /* P-1R：导出组件工厂，供 assets/pages.js 包装复用（homePage/catalogPage 继承取数三态/格式化/过滤）；
   * 本文件其余逻辑不变，api.js / data-format.js 仍冻结不改。 */
  window.PortalComponent = portalComponent;
})();
