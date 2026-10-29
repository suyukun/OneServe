/* ============================================================
 * OneServe 门户 · 数据层（P-1）——统一取数函数
 * 策略（任务书＋议题二 v1.1）：
 *   1) fetch 真实端点（端点名硬编码于下方常量表，同源部署）；
 *   2) 目标不可达/非 200/解析失败 → 降级读 mock/<name>.json（结构与真实端点一致），
 *      该区 UI 角落显示「演示数据」角标——不许无标注假数据；
 *   3) mock 也失败 → error 态：显示「数据暂不可用」＋重试，禁空白禁假 0；
 *   4) 刷新：60s 轮询＋各区记录上次更新 HH:mm:ss＋visibilitychange 暂停。
 * ============================================================ */
(function (root) {
  'use strict';

  /* ── 端点常量表（真实聚合只读端点；后端落地前自动走 mock 降级） ── */
  var API_BASE = ''; // 同源部署（经网关同域）；跨域部署仅改此一处
  var ENDPOINTS = {
    overview:      '/v1/portal/overview',      // 数字条：覆盖用户数(口径)/在线API/接入方/累计调用+近7日
    runtimeStatus: '/v1/portal/status',        // 受控叙事状态条：最近同步|今日调用·成功率|审计
    catalog:       '/v1/portal/catalog',       // 服务目录（按业务域分组）
    freshness:     '/v1/portal/freshness',     // 数据新鲜度（各域最后同步时间＋同步条数）
    announcements: '/v1/portal/announcements'  // 公告栏
  };
  var FETCH_TIMEOUT_MS = 4000;   // 真实端点超时，超时即降级
  var POLL_INTERVAL_MS = 60000;  // 轮询 60s（app.js 配合 visibilitychange 暂停）

  function fetchJson(url) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, FETCH_TIMEOUT_MS);
      fetch(url, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: ctrl ? ctrl.signal : undefined,
        cache: 'no-store'
      }).then(function (res) {
        clearTimeout(timer);
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        return res.json();
      }).then(function (json) { resolve(json); })
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  /* 统一取数：name ∈ ENDPOINTS；返回 { data, source: 'live'|'demo' }；
   * 双失败抛错（调用方进 error 态渲染「数据暂不可用」＋重试）。 */
  function loadSection(name) {
    var endpoint = ENDPOINTS[name];
    if (!endpoint) return Promise.reject(new Error('unknown section: ' + name));
    return fetchJson(API_BASE + endpoint).then(function (json) {
      return { data: json, source: 'live' };
    }).catch(function (err) {
      return fetchJson('mock/' + name + '.json').then(function (json) {
        return { data: json, source: 'demo' };
      }).catch(function (err2) {
        throw new Error('live+mock both failed for ' + name + ': ' + (err2 && err2.message));
      });
    });
  }

  root.PortalAPI = {
    ENDPOINTS: ENDPOINTS,
    API_BASE: API_BASE,
    POLL_INTERVAL_MS: POLL_INTERVAL_MS,
    loadSection: loadSection
  };
})(typeof self !== 'undefined' ? self : this);
