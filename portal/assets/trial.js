/* ============================================================
 * OneServe 门户 · 接口调试台（assets/trial.js · 门面批 2026-10-08 重设计）
 * 定位＝已发布服务的在线调试台：发布后验证「凭证 → 网关 → 服务 → 数据」链路通不通。
 *
 * 数据源（全部真实端点，零 mock、零回落）：
 *   - GET  /v1/admin/publish              已发布服务列表（下拉；status=published 过滤）
 *   - GET  /v1/admin/load/schema          表级 schema 元数据（出参表：字段/类型/中文释义）
 *   - GET  /v1/admin/publish/hbase/discovery  数据表首键采样（入参示例值；取不到＝如实留空）
 *   - POST /v1/<route>                    被调试服务的正式调用（经门户 nginx /v1/ 反代 →
 *                                        网关 :9080，apikey 请求头由网关 key-auth 鉴权）
 *
 * 纪律（沿 P3 授权闭环口径）：
 *   - passThrough：网关应答（含 401/403/404/500）原样带回 UI，错误信封（error/message/
 *     docs_url/traceId）如实展示，绝不加工美化、绝不降级假数据；
 *   - 网络层失败（反代不可达/超时）＝ httpStatus null，如实提示，不冒充任何服务端应答；
 *   - 本页无演示数据概念（生产环境）；示例键取自所选服务数据表的真实首键。
 *
 * 自测：node 直调（module.exports 导出纯逻辑：TrialAPI 取数层＋trialPanel 组件工厂），
 * 判据见 tests/test_p3_auth_loop.py B 段。
 * ============================================================ */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) {
    module.exports = api;                                   // Node 自测（判据套件直调）
  } else {
    root.TrialAPI = api.TrialAPI;                           // 浏览器全局（页面组件用）
    root.__TrialDebug = api;                                // 断言页/联调出口
  }
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  /* API_BASE 复用门户取数层基址口径（api.js 先于本文件加载；Node 自测环境无此全局＝''） */
  var API_BASE = (root && root.PortalAPI && root.PortalAPI.API_BASE) || '';

  var GET_TIMEOUT_MS = 8000;
  var CALL_TIMEOUT_MS = 10000;

  /* ── GET JSON（管理端点读取；超时/非 2xx 抛错，调用方如实展示） ── */
  function getJson(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, timeoutMs || GET_TIMEOUT_MS);
      fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' },
                   signal: ctrl ? ctrl.signal : undefined, cache: 'no-store' })
        .then(function (res) {
          clearTimeout(timer);
          return res.json().catch(function () { return null; }).then(function (json) {
            if (!res.ok) { var e = new Error('HTTP ' + res.status); e.httpStatus = res.status; e.body = json; throw e; }
            resolve(json);
          });
        })
        .catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  /* ── POST＋自定义请求头；HTTP 有应答（含 401/403/404/500）原样带回
   *    {status, ok, json, traceId}；仅网络层失败才 reject（passThrough 口径） ── */
  function postJsonRaw(url, body, headers, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
      var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, timeoutMs || CALL_TIMEOUT_MS);
      fetch(url, {
        method: 'POST',
        headers: Object.assign({ 'Content-Type': 'application/json', 'Accept': 'application/json' }, headers || {}),
        body: JSON.stringify(body),
        signal: ctrl ? ctrl.signal : undefined,
        cache: 'no-store'
      }).then(function (res) {
        clearTimeout(timer);
        res.json().catch(function () { return null; }).then(function (json) {
          var tid = '';
          try { tid = res.headers.get('X-Trace-Id') || ''; } catch (e) { /* 老浏览器无 headers */ }
          resolve({ status: res.status, ok: res.ok, json: json, traceId: tid });
        });
      }).catch(function (err) { clearTimeout(timer); reject(err); });
    });
  }

  /* ── 取数层（真实端点；UI 三态：loading/ok/error） ── */
  var TrialAPI = {
    API_BASE: API_BASE,
    /* 发布台整页数据 → services 写区条目（camelCase 形状见 ops_config._svc_view） */
    publishList: function () { return getJson(API_BASE + '/v1/admin/publish'); },
    /* 表级 schema 元数据：{ok, table, version, fieldCount, source, fields:[{name,cf,type,cnName,desc}]} */
    loadSchema: function (table, version) {
      return getJson(API_BASE + '/v1/admin/load/schema?table=' + encodeURIComponent(table)
                     + '&version=' + encodeURIComponent(version));
    },
    /* HBase 发现（含每表首键采样 keySamples）；示例键唯一真实来源 */
    discovery: function () { return getJson(API_BASE + '/v1/admin/publish/hbase/discovery'); },
    /* 被调试服务的正式调用：route 由所选服务带出（如 /v1/svc/usr-basic-info） */
    gatewayCall: function (route, body, headers) {
      var started = Date.now();
      return postJsonRaw(API_BASE + route, body, headers).then(function (r) {
        r.latencyMs = Date.now() - started;
        return r;
      });
    },
    postJsonRaw: postJsonRaw, getJson: getJson
  };

  /* ── 工具 ── */
  /* 页面 origin（Node 自测环境无 location＝''，不抛 ReferenceError） */
  function locOrigin() {
    try { return (typeof location !== 'undefined' && location && location.origin) || ''; }
    catch (e) { return ''; }
  }
  function escShell(s) { return String(s).replace(/'/g, "'\\''"); }
  function escPy(s) { return String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"'); }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    return ok;
  }
  function copyToClipboard(text, onDone) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { onDone(true); },
        function () { onDone(fallbackCopy(text)); });
    } else { onDone(fallbackCopy(text)); }
  }

  /* ── Alpine 组件：接口调试台 ── */
  function trialPanel() {
    return {
      /* ① 服务列表（GET /v1/admin/publish，status=published 过滤） */
      services: [], servicesState: 'loading', servicesError: '',
      serviceId: '', apikey: '',
      pendingService: '',          // ?service= 预选（目录详情页「在线调试」携入）
      init: function () {
        var q = '';
        try { q = new URLSearchParams(location.search).get('service') || ''; } catch (e) { /* 老浏览器 */ }
        this.pendingService = q;
        this.loadServices();
        this.loadSamples();
      },
      loadServices: function () {
        var self = this;
        this.servicesState = 'loading'; this.servicesError = '';
        TrialAPI.publishList().then(function (d) {
          self.services = (d && d.services) || [];
          self.servicesState = 'ok';
          if (self.pendingService) {                     // 深链预选（详情页「在线调试」）
            var want = String(self.pendingService);
            if (self.published().some(function (s) { return s.id === want; })) self.selectService(want);
            self.pendingService = '';
          }
        }).catch(function (err) {
          self.services = [];
          self.servicesState = 'error';
          self.servicesError = '已发布服务列表读取失败（' + ((err && err.message) || '网络错误') + '）——请点重试';
        });
      },
      servicesOptionText: function () {
        if (this.servicesState === 'loading') return '正在读取已发布服务…';
        if (this.servicesState === 'error') return '读取失败（点右侧重试）';
        var n = this.published().length;
        return n ? '选择服务（已发布 ' + n + ' 个）' : '当前没有已发布服务';
      },
      published: function () {
        return this.services.filter(function (s) { return s && s.id && s.status === 'published'; });
      },
      svc: function () {
        var id = this.serviceId;
        return this.published().find(function (s) { return s.id === id; }) || null;
      },
      selectService: function (id) { this.serviceId = id; this.onServiceChange(); },
      onServiceChange: function () { this.loadSchemaFor(this.svc()); },

      /* ② 接口信息卡数据 */
      schemaState: 'idle', schemaError: '', schemaFields: [],
      samples: {},                 /* "table|keyType" → 首键（discovery keySamples） */
      samplesLoaded: false,
      loadSamples: function () {
        var self = this;
        TrialAPI.discovery().then(function (d) {
          var map = {};
          (d && d.domains || []).forEach(function (dom) {
            (dom.tables || []).forEach(function (t) {
              Object.keys(t.keySamples || {}).forEach(function (kt) {
                var k = (t.table || '') + '|' + kt;
                if (t.keySamples[kt] && !map[k]) map[k] = t.keySamples[kt];
              });
            });
          });
          self.samples = map; self.samplesLoaded = true;
          if (self.serviceId && !self.keyInput) self.keyInput = self.sampleKey();   // 服务已选则补填示例键
        }).catch(function () { self.samplesLoaded = true; });   /* 采样失败＝示例键留空（如实标注） */
      },
      loadSchemaFor: function (svc) {
        var self = this;
        this.state = 'idle'; this.result = null; this.err = null;
        this.httpStatus = null; this.latencyMs = null; this.traceId = '';
        this.keyInput = this.sampleKey();              // key 输入框预填示例值
        if (!svc) { this.schemaState = 'idle'; this.schemaFields = []; return; }
        var ls = svc.loadSource || {};
        if (!ls.table || !ls.version) {
          this.schemaState = 'error';
          this.schemaError = '该服务未登记装载来源（表/版本），出参类型与释义不可用；调用不受影响。';
          this.schemaFields = [];
          return;
        }
        this.schemaState = 'loading'; this.schemaError = '';
        TrialAPI.loadSchema(ls.table, ls.version).then(function (d) {
          self.schemaFields = (d && d.fields) || [];
          self.schemaState = 'ok';
        }).catch(function (err) {
          self.schemaFields = [];
          self.schemaState = 'error';
          var msg = (err && err.body && err.body.message) || (err && err.message) || '网络错误';
          self.schemaError = 'schema 元数据读取失败：' + msg + '（出参仅列字段名，调用不受影响）';
        });
      },
      endpointPath: function () { var s = this.svc(); return s ? (s.routePath || '') : ''; },
      endpointUrl: function () {
        return this.endpointPath() ? (locOrigin() + this.endpointPath()) : '';
      },
      sampleKey: function () {
        var s = this.svc(); if (!s) return '';
        var ls = s.loadSource || {};
        var table = ls.table || '', kt = ls.keyType || s.keyType || '';
        return (table && kt && this.samples[table + '|' + kt]) || '';
      },
      sampleKeyNote: function () {
        if (!this.svc()) return '';
        if (!this.sampleKey()) return '示例键未取到（数据表暂不可采样）——粘贴真实 key 后调用；不预造示例值。';
        return '示例键取自该服务数据表的真实首键，可直接调用验证链路。';
      },
      cnOf: function (name) {
        var f = this.schemaFields.find(function (x) { return x.name === name; });
        return (f && f.cnName) || '';
      },
      outFields: function () {
        var s = this.svc(); if (!s) return [];
        var self = this;
        return (s.fields || []).map(function (name) {
          var f = self.schemaFields.find(function (x) { return x.name === name; }) || {};
          return { name: name, type: f.type || '—', cnName: f.cnName || '—' };
        });
      },

      /* ③ 调用与结果 */
      keyInput: '', sampleCopied: false, curlCopied: false, pythonCopied: false,
      state: 'idle', result: null, err: null,
      httpStatus: null, latencyMs: null, traceId: '',
      canInvoke: function () {
        return !!this.serviceId && !!this.keyInput.trim() && this.state !== 'loading';
      },
      invokeHint: function () {
        if (!this.serviceId) return '';
        return 'POST ' + this.endpointPath() + ' · 请求头 apikey 鉴权；网关应答（含 401/403/404/500）原样展示';
      },
      invoke: function () {
        if (!this.canInvoke()) return;
        var self = this;
        this.state = 'loading';
        var headers = {};
        if (this.apikey.trim()) headers.apikey = this.apikey.trim();   // 空＝不带，网关回真实 401
        TrialAPI.gatewayCall(this.endpointPath(), { key: this.keyInput.trim() }, headers)
          .then(function (r) {
            self.httpStatus = r.status; self.latencyMs = r.latencyMs; self.traceId = r.traceId || '';
            if (!r.ok) {                                   // 服务端错误 → 错误信封原样入态
              self.result = null; self.err = r.json || {};
              self.state = 'error';
              return;
            }
            self.result = r.json; self.err = null;         // 2xx → 结果表（found:false 也如实）
            self.state = 'ok';
          })
          .catch(function () {                             // 网络层失败（未到达网关）
            self.result = null; self.err = null;
            self.httpStatus = null; self.latencyMs = null; self.traceId = '';
            self.state = 'error';
          });
      },
      resultRows: function () {
        var f = this.result && this.result.fields;
        if (!f) return [];
        var self = this;
        return Object.keys(f).map(function (k) {
          return { name: k, value: String(f[k]), cnName: self.cnOf(k) };
        });
      },
      rawJson: function () { return this.result ? JSON.stringify(this.result, null, 2) : ''; },

      /* 调用示例（curl 为主；Python 折叠二级）——随所选服务与当前输入实时生成 */
      curlCmd: function () {
        var url = this.endpointUrl() || 'http://<门户地址>' + (this.endpointPath() || '/v1/svc/<服务路由>');
        var key = this.keyInput.trim() || '<要查询的 key>';
        var ak = this.apikey.trim() || '你的AppKey';
        return "curl -X POST '" + escShell(url) + "' \\\n" +
               "  -H 'apikey: " + escShell(ak) + "' \\\n" +
               "  -H 'Content-Type: application/json' \\\n" +
               "  -d '{\"key\":\"" + escShell(key) + "\"}'";
      },
      pythonCmd: function () {
        var url = this.endpointUrl() || 'http://<门户地址>' + (this.endpointPath() || '/v1/svc/<服务路由>');
        var key = this.keyInput.trim() || '<要查询的 key>';
        var ak = this.apikey.trim() || '你的AppKey';
        return [
          '# OneServe 接口调用 · Python（requests）样例',
          'import requests',
          '',
          'resp = requests.post(',
          '    "' + escPy(url) + '",',
          '    headers={"apikey": "' + escPy(ak) + '"},',
          '    json={"key": "' + escPy(key) + '"},',
          '    timeout=3,',
          ')',
          'print(resp.status_code, resp.json())',
          '# 200 → {service_id, key, tenant, found, fields, data_as_of, live_version}'
        ].join('\n');
      },
      copySample: function () { this.copyText(this.sampleKey(), 'sampleCopied'); },
      copyCurl: function () { this.copyText(this.curlCmd(), 'curlCopied'); },
      copyPython: function () { this.copyText(this.pythonCmd(), 'pythonCopied'); },
      copyText: function (text, flag) {
        var self = this;
        copyToClipboard(text, function (ok) {
          if (!ok) return;
          self[flag] = true;
          setTimeout(function () { self[flag] = false; }, 1600);
        });
      }
    };
  }

  /* 浏览器注册（Node 自测环境无 document，跳过） */
  if (typeof document !== 'undefined') {
    document.addEventListener('alpine:init', function () {
      window.Alpine.data('trialPanel', trialPanel);
    });
  }

  return { TrialAPI: TrialAPI, trialPanel: trialPanel };
});
