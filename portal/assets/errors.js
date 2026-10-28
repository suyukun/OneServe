/* ============================================================
 * OneServe 门户 · A6 错误码中心页组件（assets/errors.js，P1-2 新增文件）
 * 取数：只 fetch 契约 sidecar openapi/errors.json（构建期由 scripts/gen_openapi.py
 * 从错误码中心 service/app/errors.py 同源生成；MPA 纪律运行时零计算，不降级 mock）。
 * 锚点：每码一个 <tr id="<code>">——API 错误体 docs_url 即指向本页锚点，真可达；
 *       ?code=<码> 或 #<码> 深链打开即定位（跨页传参只用 URL）。
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
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .then(function (json) { resolve(json); })
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  function errorsCenterPage() {
    return {
      state: 'loading',            // loading | ok | error
      center: null,
      query: '',
      source: 'all',               // all | service | gateway

      init: function () {
        var q = new URLSearchParams(location.search).get('code');
        if (q) this.query = q;
        this.hash = (location.hash || '').replace(/^#/, '');
        this.load();
      },

      load: function () {
        var self = this;
        this.state = 'loading';
        fetchJson('openapi/errors.json').then(function (json) {
          if (!json || !json.codes || !json.meta) throw new Error('sidecar 结构缺失');
          self.center = json;
          self.state = 'ok';
          self.focusHash();
        }).catch(function () {
          self.state = 'error';   // 数据暂不可用＋重试，不造假
        });
      },

      /* 深链锚点：加载完成后滚到 #<code> 行（docs_url 落点） */
      focusHash: function () {
        var code = this.hash || new URLSearchParams(location.search).get('code');
        if (!code) return;
        var el = document.getElementById(code);
        if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
      },

      visibleCodes: function () {
        if (this.state !== 'ok') return [];
        var q = this.query.trim();
        var ql = q.toLowerCase();
        var src = this.source;
        return this.center.codes.filter(function (c) {
          if (src !== 'all' && c.source !== src) return false;
          if (!q) return true;
          return (c.code || '').toLowerCase().indexOf(ql) >= 0 ||
            (c.meaning || '').indexOf(q) >= 0;
        });
      },

      srcLabel: function (s) { return s === 'gateway' ? '网关' : '服务层'; },
      scopeLabel: function (s) {
        return s === 'row' ? '结果行' : (s === 'job' ? '任务级' : 'HTTP');
      },
      statusText: function (s) { return s == null ? '—' : String(s); },
      paramText: function (p) { return p ? p : '—'; },
      serviceText: function (arr) { return (arr && arr.length) ? arr.join('、') : '—'; }
    };
  }

  document.addEventListener('alpine:init', function () {
    window.Alpine.data('errorsCenterPage', errorsCenterPage);
  });
})();
