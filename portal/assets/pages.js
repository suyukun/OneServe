/* ============================================================
 * OneServe 门户 · P-1R 多页组件（assets/pages.js，新增文件）
 * 依据：门户视觉参照选型 v0 §3——Alpine 只管交互岛；组件一律 Alpine.data 注册，
 *      HTML 里 x-data 一行不写内联表达式；跨页传参只用 URL query/hash；
 *      active 菜单态由构建期注入，前端不判断路径。
 * 复用：app.js 导出的 PortalComponent（P-1 取数三态/格式化/搜索过滤原样继承）；
 *      api.js / data-format.js / trial.js / sm3.min.js 冻结不改。
 * ============================================================ */
(function () {
  'use strict';

  var PC = window.PortalComponent;

  /* ── 首页：portal 组件 ＋ 全局搜索跳转（回车/按钮 → catalog.html?q=…） ── */
  function homePage() {
    var c = PC();
    c.searchHint = function () {
      var st = this.st('catalog');
      if (st === 'ok') {
        var s = this.catalogSummary();
        return this.hasSearch() ? s + ' · 回车进入服务目录查看匹配结果' : s;
      }
      if (st === 'error') return '目录数据暂不可用，稍后自动重试';
      return '服务目录加载中…';
    };
    c.goCatalog = function () {          // 跨页传参只用 URL query（选型 v0 §3-5）
      location.href = 'catalog.html?q=' + encodeURIComponent(this.query.trim());
    };
    return c;
  }

  /* ── 目录页：仅加载 catalog 区 ＋ 业务域筛选 ＋ ?q= 深链承接首页搜索 ── */
  function catalogPage() {
    var c = PC();
    c.domainFilter = 'all';

    c.init = function () {               // 覆写：目录页只取 catalog，不拉无关区块
      var q = new URLSearchParams(location.search).get('q');
      if (q) this.query = q;             // 首页全局搜索携参进入
      var self = this;
      this.load('catalog');
      this.pollTimer = setInterval(function () {
        if (!document.hidden) { self.load('catalog'); }   // 后台页签暂停轮询
      }, window.PortalAPI.POLL_INTERVAL_MS);
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) { self.now = Date.now(); }
      });
    };

    c.domains = function () {
      var sec = this.sections.catalog;
      return (sec.state === 'ok' && sec.data && sec.data.domains) ? sec.data.domains : [];
    };
    c.domainCount = function (id) {
      if (id === 'all') {
        var n = 0;
        this.domains().forEach(function (d) { n += d.services || (d.items || []).length; });
        return n;
      }
      var hit = this.domains().filter(function (d) { return d.id === id; })[0];
      return hit ? (hit.services || (hit.items || []).length) : 0;
    };
    c.visibleDomains = function () {     // 搜索词 × 业务域筛选 交集
      var f = this.domainFilter;
      return this.filteredDomains().filter(function (d) { return f === 'all' || d.id === f; });
    };
    /* P0-2：目录条目 → API 详情页链接。真源 catalog 带 serviceId 时跳构建期生成的
       catalog/<service_id>.html；mock 降级目录无真源映射，如实回目录页（不跳假页）。 */
    c.svcHref = function (it) {
      if (it && it.serviceId) return 'catalog/' + it.serviceId + '.html';
      return 'catalog.html';
    };
    return c;
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('homePage', homePage);
    window.Alpine.data('catalogPage', catalogPage);
  });
})();
