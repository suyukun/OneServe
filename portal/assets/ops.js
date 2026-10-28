/* ============================================================
 * OneServe 门户 · P-3 运行视图驾驶舱（assets/ops.js，新增文件）
 * 依据：门户修订包-议题二 v1.1 §六——
 *   - ECharts 固定引 vendor/echarts.common.min.js（无构建链，哈希见 SHA256SUMS）；
 *   - 深色主题必须 registerTheme 自定义 JSON（禁用内置 dark，防与 CSS 令牌漂移）；
 *   - 首载骨架屏，此后 60s 静默轮询原位更新（visibilitychange 暂停；不回退
 *     骨架屏防闪屏）；周期定时器＝数据轮询 60s＋顶栏时钟文字（1s，纯文本非动画）；
 *     无 count-up、无常驻循环动画；
 *   - 面板数字口径与实测一致不新造：TPS/时延取 loadtest/results/ladder_summary.csv
 *     与 testkit/reports（50 并发档 P50=45ms／P99≈60ms，SPEC §6.1 背书口径）。
 * 加载序：本文件以经典 script 置于页尾（echarts 先载），先于 head 中全部 defer
 * 脚本执行——alpine:init 监听先挂、Alpine 最后启动，初始化不被打断（同 P-1R 纪律）。
 * 复用：app.js PortalComponent（取数三态/格式化直通）、api.js、data-format.js 均冻结不改。
 * ============================================================ */
(function (root) {
  'use strict';

  var echartsLib = root.echarts;

  /* ── 深色主题 JSON（'oneserve-dark'）──────────────────────────────
   * 色值与 tokens.css [data-bs-theme="dark"] 语义别名逐一对齐
   * （ECharts 读不到 CSS 变量，此处是镜像副本：bg-inset/border-strong/
   *  border-light/text-body/text-muted/text-strong；分类色=v1.1 全站一份色板）。
   * animation:false——图表一次性静态渲染，无入场动画无循环。 */
  var THEME = {
    color: ['#5070dd', '#505372', '#785db0', '#0ca8df', '#ff994d', '#3fbe95', '#fb628b'],
    backgroundColor: 'transparent',
    animation: false,
    textStyle: {
      color: '#bcc4d0',
      fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif'
    },
    title: { textStyle: { color: '#e8ebf1' }, subtextStyle: { color: '#828d9c' } },
    legend: { textStyle: { color: '#828d9c' } },
    label: { color: '#bcc4d0' },
    tooltip: {
      backgroundColor: '#263244',
      borderColor: '#2e3c50',
      borderWidth: 1,
      padding: 8,
      textStyle: { color: '#e8ebf1', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: '#3a4658' } }
    },
    categoryAxis: {
      axisLine: { show: true, lineStyle: { color: '#2e3c50' } },
      axisTick: { show: false },
      axisLabel: { color: '#828d9c' },
      splitLine: { show: false },
      splitArea: { show: false }
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: '#828d9c' },
      splitLine: { show: true, lineStyle: { color: '#253040' } }   /* 网格线低对比 */
    },
    line: { itemStyle: { borderWidth: 1.5 }, lineStyle: { width: 2 }, symbolSize: 6 },
    bar: { itemStyle: { barBorderRadius: 2 } }
  };
  var THEME_NAME = 'oneserve-dark';
  if (echartsLib && typeof echartsLib.registerTheme === 'function') {
    echartsLib.registerTheme(THEME_NAME, THEME);   /* 自定义主题，非内置 'dark' */
  }

  /* 状态语义色（镜像 tokens.css 深底令牌：绿=正常 红=异常 黄=警告） */
  var STATUS_COLORS = { ok: '#3fb965', warn: '#e2a03f', error: '#ef6b6b' };

  /* ── 实测口径常量（不新造数字；来源文件入库可追溯）────────────── */
  var LOADTEST = {
    date: '2026-09-21',
    tps: 1089.9,            /* 100 并发档 RPS 均值（≈1.09k QPS） */
    tpsLevel: 100,
    latencyP50: 45,         /* 50 并发档实测 P50（ms）· SPEC §6.1 背书口径 */
    latencyP99: 60,         /* 50 并发档实测 P99≈（ms） */
    sourceFile: 'loadtest/results/ladder_summary.csv',
    slaNote: '50 并发档实测 P50=45ms／P99≈60ms（SPEC §6.1 背书口径）'
  };

  /* 底部四组件状态带（P1-6 接真）：灯色/文字一律由 /v1/portal/status 的 components[]
   * 驱动（后端 monitor.components_view 真探活）；此处只保留「容量梯子实测」档案数据，
   * 标为「实测档案·非实时」作副注——不再冒充实时状态（评估 F5/B4：仪表盘与监控台混淆）。
   * 组件 id 与后端对齐：ops-comp-gateway / -service / -redis / -audit。 */
  var COMPONENT_ORDER = ['ops-comp-gateway', 'ops-comp-service', 'ops-comp-redis', 'ops-comp-audit'];
  var COMPONENT_ARCHIVE = {
    'ops-comp-gateway': { tech: 'APISIX', perf: 'CPU 9.6% · 内存峰值 536MiB（100 并发档）' },
    'ops-comp-service': { tech: 'OneServe API', perf: 'CPU 42.0% · 内存峰值 61MiB（100 并发档）' },
    'ops-comp-redis': { tech: '缓存', perf: 'CPU 3.6% · 内存峰值 48MiB（100 并发档）' },
    'ops-comp-audit': { tech: '哈希脱敏', perf: '' }
  };

  function fmt() { return root.Fmt; }

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  /* 近 7 日标签（MM-DD）：以数据自带的 generatedAt 为锚回推 6 天，
   * 标签与数据快照同钟，不随浏览器墙钟漂移 */
  function trendLabels(endIso) {
    var t = (endIso && !isNaN(Date.parse(endIso))) ? Date.parse(endIso) : Date.now();
    var out = [];
    for (var i = 6; i >= 0; i--) {
      var d = new Date(t - i * 86400000);
      out.push(pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()));
    }
    return out;
  }

  /* ── 图一：调用趋势（近 7 日面积折线）────────────────────────── */
  function buildTrendOption(points, endIso) {
    var F = fmt();
    var safe = (points || []).map(function (v) { return Number(v) || 0; });
    return {
      animation: false,
      grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: function (v) { return F.formatThousands(v) + ' 次'; }
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: trendLabels(endIso),
        axisLabel: { color: '#828d9c' }
      },
      yAxis: {
        type: 'value',
        axisLabel: { color: '#828d9c', formatter: function (v) { return F.formatCompact(v); } },
        splitLine: { lineStyle: { color: '#253040' } }
      },
      series: [{
        name: '调用量',
        type: 'line',                       /* 面积折线（v1.1 图表选型映射） */
        data: safe,
        symbol: 'circle',
        itemStyle: { color: '#5070dd' },
        lineStyle: { color: '#5070dd', width: 2 },
        areaStyle: { color: '#5070dd', opacity: 0.14 }
      }]
    };
  }

  /* ── 图二：同步链路状态（各业务域新鲜度横向条形）──────────────
   * 条长=同步条数；条色=同步状态语义色；条尾标签=条数＋相对新鲜度（远看免悬停可读） */
  /* 1-2-5 取整到数量级（横轴稀疏刻度用：画布窄时刻度过密必叠压） */
  function niceStep(raw) {
    var pow = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var r = raw / pow;
    return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 5 ? 5 : 10) * pow;
  }
  function buildFreshnessOption(domains, now) {
    var F = fmt();
    var list = (domains || []).slice().reverse();   /* y 类目轴自下而上，反转入参序 */
    var data = list.map(function (d) {
      return {
        value: d.rows,
        name: d.name,
        status: d.status,
        lastSyncAt: d.lastSyncAt,
        itemStyle: { color: STATUS_COLORS[d.status] || STATUS_COLORS.error, borderRadius: 2 }
      };
    });
    return {
      animation: false,
      grid: { left: 8, right: 136, top: 12, bottom: 8, containLabel: true },   /* 右侧安全区≥最长条尾标签（x.x万 · 昨天 HH:MM） */
      tooltip: {
        trigger: 'item',
        formatter: function (p) {
          var d = p.data;
          return d.name + '<br/>同步 ' + F.formatThousands(d.value) + ' 条<br/>最后同步 ' +
            fmtDateTime(d.lastSyncAt);
        }
      },
      xAxis: {
        type: 'value',
        interval: niceStep(Math.max(Math.max.apply(null, (list || []).map(function (d) { return Number(d.rows) || 0; })) / 2, 1)),
        axisLabel: { color: '#828d9c', formatter: function (v) { return F.formatCompact(v); } },
        splitLine: { lineStyle: { color: '#253040' } }
      },
      yAxis: {
        type: 'category',                    /* 横向条形（构成类目>6 禁饼） */
        data: list.map(function (d) { return d.name; }),
        axisLabel: { color: '#bcc4d0' }
      },
      series: [{
        name: '同步条数',
        type: 'bar',
        barWidth: 16,
        data: data,
        label: {
          show: true,
          position: 'right',
          color: '#828d9c',
          fontSize: 11,
          formatter: function (p) {
            return F.formatCompact(p.data.value) + ' · ' + F.formatRelativeTime(p.data.lastSyncAt, now);
          }
        }
      }]
    };
  }

  function fmtDateTime(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return '—';
    var d = new Date(t);
    return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  /* ── 对外导出（selftest-ops.html 断言＋Node 冒烟双端可用） ── */
  var OpsCockpit = {
    THEME_NAME: THEME_NAME,
    THEME: THEME,
    STATUS_COLORS: STATUS_COLORS,
    LOADTEST: LOADTEST,
    COMPONENT_ORDER: COMPONENT_ORDER,
    COMPONENT_ARCHIVE: COMPONENT_ARCHIVE,
    DATA_POLLING: true,           /* 60s 数据轮询（visibilitychange 暂停；静默原位更新） */
    CLOCK_ONLY_INTERVAL: false,   /* 周期定时器=数据轮询＋顶栏时钟文字 */
    buildTrendOption: buildTrendOption,
    buildFreshnessOption: buildFreshnessOption,
    trendLabels: trendLabels
  };
  root.OpsCockpit = OpsCockpit;

  /* ── Alpine 页组件（复用 app.js PortalComponent 的取数三态/格式化） ── */
  function opsPage() {
    var c = root.PortalComponent();     /* 惰性取：此时 app.js（defer）已执行 */
    var OC = OpsCockpit;

    c.clock = '';
    c.clockTimer = null;
    c.charts = { trend: null, freshness: null };

    c.init = function () {
      var self = this;
      var F = root.Fmt;

      /* 顶栏时钟：1s 纯文本更新（非动画）；数据 60s 静默轮询（见下） */
      this.clock = F.formatClock(new Date());
      this.clockTimer = setInterval(function () {
        self.clock = F.formatClock(new Date());
      }, 1000);

      this.load('runtimeStatus');
      this.load('overview');
      this.load('freshness');

      /* 60s 数据轮询：静默原位更新（load 不回退骨架屏）；watch 锚 updatedAt——
       * 首载 loading→ok 与每次轮询成功都触发图表重绘；后台页签暂停 */
      this.pollTimer = setInterval(function () {
        if (!document.hidden) {
          self.now = Date.now();                     /* 相对时间锚随轮询同步（不冻结） */
          self.load('runtimeStatus');
          self.load('overview');
          self.load('freshness');
        }
      }, root.PortalAPI.POLL_INTERVAL_MS);

      this.$watch('sections.overview.updatedAt', function () { self.renderTrend(); });
      this.$watch('sections.freshness.updatedAt', function () { self.renderFreshness(); });

      window.addEventListener('resize', function () {
        clearTimeout(self._resizeTimer);
        self._resizeTimer = setTimeout(function () { self.resizeChartsNow(); }, 160);
      });
    };

    c.renderTrend = function () {
      if (this.st('overview') !== 'ok') return;
      var el = document.getElementById('ops-chart-trend');
      if (!el || !root.echarts) return;
      if (!this.charts.trend) this.charts.trend = root.echarts.init(el, OC.THEME_NAME);
      var d = this.data('overview');
      this.charts.trend.setOption(buildTrendOption(d.callsTrend7d, d.generatedAt), true);
    };

    c.renderFreshness = function () {
      if (this.st('freshness') !== 'ok') return;
      var el = document.getElementById('ops-chart-freshness');
      if (!el || !root.echarts) return;
      if (!this.charts.freshness) this.charts.freshness = root.echarts.init(el, OC.THEME_NAME);
      var d = this.data('freshness');
      this.charts.freshness.setOption(buildFreshnessOption(d.domains, this.now), true);
    };

    c.resizeCharts = function () {
      var self = this;
      this.$nextTick(function () { setTimeout(function () { self.resizeChartsNow(); }, 80); });
    };
    c.resizeChartsNow = function () {
      if (this.charts.trend) this.charts.trend.resize();
      if (this.charts.freshness) this.charts.freshness.resize();
    };

    /* KPI／状态带视图直通（口径常量→展示值） */
    c.tpsDisplay = function () { return root.Fmt.formatThousands(Math.round(OC.LOADTEST.tps)); };
    c.latencyDisplay = function () { return String(OC.LOADTEST.latencyP50); };
    /* P1-6 组件状态带：真探活结果直出（后端每项 {id,name,tech,status,detail,checkedAt,probe}）；
     * 未取到 components（首载/接口不可达）＝空数组，页面显示「未知」占位，不回退「正常」。 */
    c.components = function () {
      if (this.st('runtimeStatus') !== 'ok') return [];
      var raw = this.data('runtimeStatus').components;
      if (!raw || !raw.length) return [];
      var byId = {};
      raw.forEach(function (x) { if (x && x.id) byId[x.id] = x; });
      return OC.COMPONENT_ORDER.map(function (id) {
        var hit = byId[id] || { id: id, name: id, tech: '', status: null, detail: null };
        var arch = OC.COMPONENT_ARCHIVE[id] || {};
        var item = {};
        for (var k in hit) item[k] = hit[k];
        if (!item.tech) item.tech = arch.tech || '';
        item.archivePerf = arch.perf || '';
        return item;
      });
    };
    /* 灯色与文字：ok=正常／warn=延迟／error=异常／其余（未探到）=未知（灰灯，绝不写「正常」） */
    c.compStatusMeta = function (status) {
      if (status === 'ok') return { cls: 'is-ok', text: '正常' };
      if (status === 'warn') return { cls: 'is-warn', text: '延迟' };
      if (status === 'error') return { cls: 'is-error', text: '异常' };
      if (status === 'off') return { cls: 'is-off', text: '已关闭' };
      return { cls: 'is-unknown', text: '未知' };
    };
    c.auditState = function () {
      if (this.st('runtimeStatus') !== 'ok') return null;
      return {
        enabled: !!this.data('runtimeStatus').auditEnabled,
        mode: this.data('runtimeStatus').auditMode || '—'
      };
    };

    return c;
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('alpine:init', function () {
      if (root.Alpine) root.Alpine.data('opsPage', opsPage);
    });
  }
  root.OpsCockpitFactory = opsPage;
})(typeof self !== 'undefined' ? self : this);
