/* ============================================================
 * 已发布服务页组件（published.js · P3 二批 2026-10-08 维护者 六条③）
 * 从发布页拆出：全部已发布服务列表（名称/地址/数据版本/状态/字段数）＋
 * 点入详情视图（出参字段中文名〔schema 联查〕/授权租户/关联凭证配额）＋下线（两段式确认）。
 *
 * 数据聚合（全部现成只读端点，零后端新增）：
 *   GET /v1/admin/publish            服务列表（写区条目；O1HBase.normalizeServices 归一）
 *   GET /v1/admin/load/schema        字段中文名（按 loadSource 表+版本联查；缺＝回退英文名）
 *   GET /v1/portal/grants/panorama   授权租户（serviceId 匹配，appkey 掩码）
 *   GET /v1/admin/appkeys            关联凭证（P3 弹窗卡改口径：kid ∪ 租户双联——
 *                                     矩阵 demo_merchant 行不带 kid，仅 kid 联 join 会漏
 *                                     demo_merchant 已签发凭证；租户口径按 callerId 补齐）
 * 加载序：publish-hbase-adapter.js（契约层）→ 本文件 → alpine.min.js（硬规矩）。
 * 诚信线：各源失败＝如实降级（显示已知部分＋缺失说明），不编造租户/凭证/中文名。
 *
 * P3 弹窗卡（2026-10-08 维护者 ③）两处根因修复：
 *   ① 详情恒空根因＝Alpine 响应性：闭包捕获的是裸对象 d（赋入 this.details 前的
 *     引用），异步回填裸对象不触发重渲染——修复＝赋入后改持 this.details[s.id]
 *     返回的响应式代理再回填（showDetail 内标注）。
 *   ② 详情重排＝接口文档主体：入参（查询键）/出参表（字段名/类型/中文释义）/
 *     curl 调用示例；授权租户/关联凭证压成末行状态，删 403 恐吓文案。
 * ============================================================ */
(function (root) {
  'use strict';

  var HB = root.O1HBase;

  /* ── 纯函数（详情聚合；selftest/机检直调入口 PUBSVC） ── */
  /* 出参表行（P3 弹窗卡 维护者 ③）：schema 命中给 名/类型/中文释义；未命中如实回退
   * type='—'、cn='—'（页面叠加 fieldsNote 说明），不编造类型与释义。 */
  function outFieldsOf(names, schemaFields) {
    var by = {};
    (schemaFields || []).forEach(function (f) {
      if (f && f.name) by[f.name] = { type: String(f.type || ''), cn: String(f.cnName || '') };
    });
    return (names || []).map(function (n) {
      var hit = by[String(n)] || {};
      return { name: String(n || ''), type: hit.type || '—', cn: hit.cn || '—' };
    });
  }
  /* 入参说明行（查询键）：键型口径如实带出（phone_sm3＝手机号 SM3 摘要）。 */
  function inputDescOf(s) {
    var kt = String((s && s.keyType) || '');
    return kt === 'phone_sm3' ? '查询键：手机号 SM3 摘要后的用户标识（64 位十六进制）'
      : (kt ? '查询键：' + kt + ' 口径的用户标识' : '查询键：用户标识');
  }
  /* 示例键：只放有实测凭据的表（同 publish.js EXAMPLE_KEYS 先例，诚信线不编造）。 */
  var EXAMPLE_KEYS = {
    usr_basic_info: 'a87ae39b27b7f83c2328e8bf4850a36bf1380032c252a1b2120d0d65f03d10d4'
  };
  function exampleKeyOf(table) {
    return EXAMPLE_KEYS[String(table || '')] || '';
  }
  /* curl 调用示例：POST＋apikey 头＋查询键（有实测示例键则带入，否则占位符）。 */
  function curlOfService(origin, routePath, exampleKey) {
    var o = (origin && String(origin).indexOf('http') === 0) ? origin : 'https://<平台访问地址>';
    var key = exampleKey || '<用户标识>';
    return "curl -X POST '" + o + routePath + "' \\\n"
      + "  -H 'Content-Type: application/json' \\\n"
      + "  -H 'apikey: <调用凭证>' \\\n"
      + "  -d '{\"key\": \"" + key + "\"}'";
  }
  /* 授权状态一行（维护者 ③）：有则「授权租户：A、B · 关联凭证 N 个」；无则「暂无授权」；
   * 凭证数不可得（列表读取失败）如实标注，不冒充零。 */
  function authTextOf(tenants, credsCount) {
    var names = (tenants || []).map(function (t) { return t.tenant; });
    if (!names.length) return '暂无授权';
    var head = '授权租户：' + names.join('、');
    if (credsCount === null || credsCount === undefined) return head + '（凭证数暂不可读）';
    return head + ' · 关联凭证 ' + credsCount + ' 个';
  }
  /* 授权全景行 → 本服务租户聚合（serviceId 匹配；一租户多行合并，字段数求和、kid 去重）。 */
  function tenantsOfService(rows, sid) {
    var by = {}, order = [];
    (rows || []).forEach(function (r) {
      var o = r || {};
      if (String(o.serviceId || o.service || '') !== String(sid || '')) return;
      var t = String(o.tenant || '').trim();
      if (!t) return;
      if (!by[t]) { by[t] = { tenant: t, fieldsCount: 0, kids: [] }; order.push(t); }
      by[t].fieldsCount += Number(o.fieldsCount || (o.fields || []).length || 0);
      var kid = String(o.kid || '').trim();
      if (kid && by[t].kids.indexOf(kid) < 0) by[t].kids.push(kid);
    });
    return order.sort().map(function (t) { return by[t]; });
  }
  /* 凭证列表联查（P3 弹窗卡双口径）：kid 命中（矩阵行带凭证绑定，如 partner-demo）
   * ∪ callerId∈本服务租户（矩阵 demo_merchant 行不带 kid，其已签发凭证按租户口径关联）。 */
  function credsOfService(appkeys, kids, tenants) {
    var want = kids || [];
    var tn = (tenants || []).map(function (x) { return String(x || ''); });
    return (appkeys || []).filter(function (k) {
      var kid = String((k || {}).kid || '');
      var caller = String((k || {}).callerId || (k || {}).tenant || '');
      return want.indexOf(kid) >= 0 || (kid === '' && tn.indexOf(caller) >= 0)
        || (kid !== '' && want.indexOf(kid) < 0 && tn.indexOf(caller) >= 0);
    });
  }

  function offlineFailText(err) {
    return '下线没有完成（' + String((err && err.message) || (err && err.title) || '原因未知')
      + '）。请重试，或联系平台管理员。';
  }

  root.PUBSVC = {
    outFieldsOf: outFieldsOf, inputDescOf: inputDescOf, exampleKeyOf: exampleKeyOf,
    curlOfService: curlOfService, authTextOf: authTextOf,
    tenantsOfService: tenantsOfService, credsOfService: credsOfService,
    offlineFailText: offlineFailText
  };

  root.publishedConsole = function () {
    return {
      /* 列表 */
      services: [], servicesBusy: false, servicesError: null,
      /* 详情（点入视图）：selected＝行号；details 按 sid 缓存聚合结果 */
      selected: -1, details: {},
      /* 下线（两段式确认，E 卡文案＝数据保留，可随时重新发布） */
      offlineModal: { open: false, stage: 1, armed: false, index: -1, name: '' },
      offlineError: '', offlineDoneNote: false,

      /* ── 取数基础设施（与 publish.js 同款：fail-closed 如实错误） ── */
      async apiFetch(url, opts) {
        try {
          var r = await root.fetch(url, Object.assign({
            headers: { 'Content-Type': 'application/json' }
          }, opts || {}));
          var j = {};
          try { j = await r.json(); } catch (e) { j = {}; }
          return { ok: r.ok, status: r.status, body: j };
        } catch (e) {
          return { ok: false, status: 0, body: { error: 'NETWORK', message: '网络连不上服务端' } };
        }
      },
      errOf(res) {
        var r = res || {};
        return HB.normalizeError(Object.assign({ status: r.status }, r.body || {}))
          || { code: 'UNKNOWN', status: 0, title: '', message: '原因未知', hint: '', fallback: false };
      },

      async boot() { await this.loadServices(); },

      /* ── 列表（GET /v1/admin/publish → O1HBase.normalizeServices） ── */
      async loadServices() {
        this.servicesBusy = true; this.servicesError = null;
        var res = await this.apiFetch(HB.CONTRACT.consoleView);
        this.servicesBusy = false;
        if (res.ok) {
          this.services = HB.normalizeServices(res.body);
          return true;
        }
        var err = this.errOf(res);
        var why = (err && err.message && err.message !== '网络连不上服务端') ? (err.message + '。') : '';
        this.servicesError = {
          title: '暂时读不到已发布服务',
          message: why + '请稍后重试；若持续出现，请联系平台管理员。'
        };
        return false;
      },

      /* ── 详情（点入视图）：首次点入聚合三源，之后读缓存 ── */
      detailOf(i) {
        var s = this.services[i];
        return (s && this.details[s.id]) || null;
      },
      async showDetail(i) {
        var s = this.services[i];
        if (!s) return;
        this.selected = i;
        if (this.details[s.id]) return;
        var fresh = { busy: true, address: '', inputDesc: '', exampleKey: '', outFields: [],
                      fieldsNote: '', tenants: [], grantsNote: '', creds: [], credsNote: '',
                      authText: '', curl: '' };
        this.details[s.id] = fresh;
        /* P3 弹窗卡根因①修复：闭包必须改持响应式代理。直接用赋入前的裸对象 fresh，
         * 异步回填不触发 Alpine 重渲染——详情恒空（出参空白＋「尚未登记授权」假象，
         * 实测 state 已有 12 字段/2 租户而 DOM 零 chip）。 */
        var d = this.details[s.id];
        /* 接口文档静态件（不依赖远端）：地址/入参说明/示例键/curl */
        var origin = (root.location && root.location.origin && String(root.location.origin).indexOf('http') === 0)
          ? root.location.origin : '';
        d.address = origin + s.routePath;
        d.inputDesc = root.PUBSVC.inputDescOf(s);
        d.exampleKey = root.PUBSVC.exampleKeyOf(s.table);
        d.curl = root.PUBSVC.curlOfService(origin, s.routePath, d.exampleKey);
        d.outFields = root.PUBSVC.outFieldsOf(s.fields, []);
        /* ① 出参表：有加载来源（表+版本）才查 schema；缺＝字段名如实回退 */
        var schemaP = (async function (self) {
          if (!s.table || !s.dataVersion) {
            d.fieldsNote = '该服务条目无加载来源留痕，类型与中文释义未知（已显示字段名）';
            return;
          }
          var res = await self.apiFetch(HB.loadSchemaUrl(s.table, s.dataVersion));
          if (res.ok) {
            var norm = HB.normalizeSchema(res.body);
            if (norm.ok && norm.fields.length > 0) {
              d.outFields = root.PUBSVC.outFieldsOf(s.fields, norm.fields);
              d.fieldsNote = '';
              return;
            }
          }
          d.fieldsNote = '类型与中文释义暂不可读（schema 元数据缺失或不可达），已显示字段名';
        })(this);
        /* ②③ 授权租户＋关联凭证：全景行（serviceId 匹配）＋凭证列表（kid ∪ 租户联查） */
        var grantsP = (async function (self) {
          var res = await self.apiFetch('/v1/portal/grants/panorama');
          if (!res.ok) {
            d.grantsNote = '授权登记暂不可读，稍后可重新点入刷新';
            d.authText = '授权状态暂不可读';
            return;
          }
          var rows = (res.body || {}).rows || [];
          d.tenants = root.PUBSVC.tenantsOfService(rows, s.id);
          var kids = [], names = [];
          d.tenants.forEach(function (t) { kids = kids.concat(t.kids); names.push(t.tenant); });
          if (!kids.length && !names.length) {
            d.creds = [];
            d.authText = root.PUBSVC.authTextOf([], 0);
            return;
          }
          var kr = await self.apiFetch('/v1/admin/appkeys');
          if (!kr.ok) {
            d.credsNote = '凭证列表暂不可读，稍后可重新点入刷新';
            d.authText = root.PUBSVC.authTextOf(d.tenants, null);
            return;
          }
          d.creds = root.PUBSVC.credsOfService((kr.body || {}).keys || (kr.body || []), kids, names);
          d.authText = root.PUBSVC.authTextOf(d.tenants, d.creds.length);
        })(this);
        await Promise.all([schemaP, grantsP]).catch(function () {});
        d.busy = false;
      },
      closeDetail() { this.selected = -1; },

      /* ── 下线（两段式确认；E 卡：数据保留，可随时重新发布） ── */
      askOffline(i) {
        var s = this.services[i];
        if (!s) return;
        this.offlineError = '';
        this.offlineModal = { open: true, stage: 1, armed: false, index: i, name: s.serviceName };
      },
      async confirmOffline() {
        var m = this.offlineModal;
        var s = this.services[m.index];
        if (!s) { m.open = false; return; }
        if (m.stage === 1) { this.offlineModal.stage = 2; return; }
        if (!m.armed) return;
        this.offlineError = '';
        var res = await this.apiFetch(HB.CONTRACT.offline(s.id), {
          method: 'POST', body: JSON.stringify({ operator: 'console' })
        });
        if (res.ok) {
          this.services[m.index].status = 'offline';
          this.offlineModal.open = false;
          this.offlineDoneNote = true;   /* E 卡：结果提示＝数据保留，可随时重新发布 */
        } else {
          this.offlineError = root.PUBSVC.offlineFailText(this.errOf(res));
        }
      }
    };
  };
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
